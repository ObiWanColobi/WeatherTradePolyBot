"""Polymarket bucket-market snapshot logger.

Polls Gamma API every 5 minutes for active daily-temperature events,
parses each event's sub-markets, and writes rows to `bucket_snapshots`.

Designed to run as a daemon (systemd) on the `live` VPS alongside
the existing weather bot. Data feeds Backtest B and forward analysis
once enough history accumulates (~30 days).
"""
import json
import time
from datetime import datetime, timezone
from typing import Iterator

import requests

import db
from snapshot_parse import (
    parse_event_slug,
    parse_bucket_bounds,
    classify_bucket_type,
)

GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API = "https://clob.polymarket.com"
POLL_INTERVAL_SEC = 300  # 5 minutes
REQUEST_TIMEOUT_SEC = 15
BOOK_TIMEOUT_SEC = 8     # per-token CLOB /book call; short so a slow book can't stall the poll
MAX_CONSECUTIVE_FAILURES = 6  # 6 * 5min = 30min of dead polls before re-raise

_PAGE_SIZE = 100
_MAX_OFFSET = 2000
_ORDERBOOK_DEPTH = 10    # real ladder depth (was 3, but the source field was always NULL)

# --- Targeted depth capture (option B) -------------------------------------
# Real orderbook depth is NOT in the Gamma payload; it must be fetched per-token
# from the CLOB /book endpoint. That is ~1 extra HTTP call per market, so we only
# do it for markets that (a) resolve soon (within the fire window the strategies
# act in) AND (b) carry enough liquidity to be tradeable — capped per event so a
# poll can't explode into hundreds of book calls. Everything else keeps NULL depth
# exactly as before, so the baseline top-of-book snapshot is never at risk.
DEPTH_CAPTURE_ENABLED = True
DEPTH_MAX_HOURS_TO_RESOLVE = 24     # only book markets resolving within 24h
DEPTH_MIN_LIQUIDITY_NUM = 50.0      # skip near-dead books
DEPTH_MAX_BOOKS_PER_EVENT = 12      # hard cap on book calls per event per poll

_session = requests.Session()
_session.headers.update({"User-Agent": "weather-bot-snapshot/1.0"})


def fetch_active_weather_events() -> list[dict]:
    """Pull all currently-active weather-tagged events from Gamma.

    Raises requests.RequestException (or similar) on fetch failure so the
    caller can distinguish a transport error from an empty result.
    """
    out: list[dict] = []
    offset = 0
    while True:
        r = _session.get(
            f"{GAMMA_API}/events",
            params={
                "tag_slug": "weather",
                "active": "true",
                "closed": "false",
                "limit": _PAGE_SIZE,
                "offset": offset,
            },
            timeout=REQUEST_TIMEOUT_SEC,
        )
        r.raise_for_status()
        batch = r.json()
        if not batch:
            break
        out.extend(batch)
        if len(batch) < _PAGE_SIZE:
            break
        offset += _PAGE_SIZE
        if offset > _MAX_OFFSET:
            print(f"[snapshot] WARN: pagination cap hit at offset {offset}, possibly truncated")
            break
    return out


def _to_float(v) -> float | None:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _first_clob_token_id(m: dict) -> str | None:
    """The YES-side CLOB token id for a market, or None. Gamma serializes
    clobTokenIds as a JSON string list; the first id is the YES outcome."""
    raw = m.get("clobTokenIds")
    if not raw:
        return None
    try:
        toks = json.loads(raw) if isinstance(raw, str) else raw
        return str(toks[0]) if toks else None
    except (json.JSONDecodeError, IndexError, TypeError):
        return None


def fetch_book_depth(token_id: str) -> tuple[str | None, str | None]:
    """Fetch the real bid/ask ladder for one CLOB token.

    Returns (bids_json, asks_json) as top-`_ORDERBOOK_DEPTH` [price,size] lists,
    or (None, None) on ANY failure — depth is best-effort and must NEVER break
    the baseline snapshot. CLOB returns bids best (highest) first and asks best
    (lowest) first already, but we sort defensively.
    """
    try:
        r = _session.get(
            f"{CLOB_API}/book",
            params={"token_id": token_id},
            timeout=BOOK_TIMEOUT_SEC,
        )
        r.raise_for_status()
        b = r.json()
    except (requests.RequestException, ValueError):
        return None, None

    def _levels(side, *, descending):
        raw = b.get(side) or []
        out = []
        for lvl in raw:
            p = _to_float(lvl.get("price"))
            s = _to_float(lvl.get("size"))
            if p is not None and s is not None:
                out.append([p, s])
        out.sort(key=lambda x: x[0], reverse=descending)
        return out[:_ORDERBOOK_DEPTH]

    bids = _levels("bids", descending=True)    # highest bid first
    asks = _levels("asks", descending=False)   # lowest ask first
    return (json.dumps(bids) if bids else None,
            json.dumps(asks) if asks else None)


def _hours_to_resolution(event_end_iso: str, now_utc: datetime) -> float | None:
    """Hours from now until the event's end (resolution). None if unparseable."""
    if not event_end_iso:
        return None
    try:
        end = datetime.fromisoformat(event_end_iso.replace("Z", "+00:00"))
        if end.tzinfo is None:
            end = end.replace(tzinfo=timezone.utc)
        return (end - now_utc).total_seconds() / 3600.0
    except (ValueError, TypeError):
        return None


def _should_capture_depth(m: dict, hours_to_res: float | None) -> bool:
    """Option-B gate: only book markets resolving soon AND liquid enough."""
    if not DEPTH_CAPTURE_ENABLED:
        return False
    if hours_to_res is None or hours_to_res < 0 or hours_to_res > DEPTH_MAX_HOURS_TO_RESOLVE:
        return False
    liq = _to_float(m.get("liquidityNum"))
    if liq is None or liq < DEPTH_MIN_LIQUIDITY_NUM:
        return False
    return True


def process_event_dict(event: dict, snapshot_at_utc: str) -> Iterator[dict]:
    """Yield one row dict per sub-market in this event.
    Skips non-daily-temp events and unparseable sub-markets.

    For markets that resolve soon and are liquid (option-B gate), fetches the
    REAL orderbook ladder from CLOB /book — capped at DEPTH_MAX_BOOKS_PER_EVENT
    book calls per event. All other markets keep NULL depth, unchanged.
    """
    parsed_slug = parse_event_slug(event.get("slug", ""))
    if parsed_slug is None:
        return

    event_slug = event["slug"]
    event_end_iso = event.get("endDate") or ""
    condition_id = event.get("conditionId")

    now_utc = datetime.now(timezone.utc)
    hours_to_res = _hours_to_resolution(event_end_iso, now_utc)
    books_fetched = 0

    for m in event.get("markets") or []:
        gtitle = m.get("groupItemTitle", "")
        lo_f, hi_f = parse_bucket_bounds(gtitle)
        bucket_type = classify_bucket_type(gtitle)
        is_open_tail = 1 if (lo_f is None or hi_f is None) and bucket_type == "tail" else 0

        bb = _to_float(m.get("bestBid"))
        ba = _to_float(m.get("bestAsk"))
        ltp = _to_float(m.get("lastTradePrice"))
        mid = (bb + ba) / 2 if (bb is not None and ba is not None and bb > 0 and ba > 0) else None

        # Real depth only for qualifying markets, under the per-event cap.
        ob_bids_json = None
        ob_asks_json = None
        if (books_fetched < DEPTH_MAX_BOOKS_PER_EVENT
                and _should_capture_depth(m, hours_to_res)):
            token_id = _first_clob_token_id(m)
            if token_id:
                ob_bids_json, ob_asks_json = fetch_book_depth(token_id)
                books_fetched += 1

        yield {
            "snapshot_at_utc":         snapshot_at_utc,
            "event_slug":              event_slug,
            "event_end_iso":           event_end_iso,
            "condition_id":            condition_id,
            "city":                    parsed_slug["city"],
            "kind":                    parsed_slug["kind"],
            "resolution_date":         parsed_slug["resolution_date"],
            "sub_market_id":           str(m.get("id", "")),
            "sub_market_condition_id": m.get("conditionId", ""),
            "group_item_title":        gtitle,
            "bucket_type":             bucket_type,
            "bound_lo_f":              lo_f,
            "bound_hi_f":              hi_f,
            "is_open_tail":            is_open_tail,
            "best_bid":                bb,
            "best_ask":                ba,
            "mid_price":               mid,
            "last_trade_price":        ltp,
            "orderbook_bids_json":     ob_bids_json,
            "orderbook_asks_json":     ob_asks_json,
            "volume_24h":              _to_float(m.get("volume24hr")),
            "liquidity_num":           _to_float(m.get("liquidityNum")),
            "raw_market_json":         json.dumps(m, default=str),
        }


def insert_snapshot_rows(rows: list[dict]) -> int:
    if not rows:
        return 0
    conn = db.snapshot_conn()   # SEPARATE DB — never the paper-trading file
    cols = list(rows[0].keys())
    placeholders = ",".join("?" for _ in cols)
    sql = f"INSERT INTO bucket_snapshots ({','.join(cols)}) VALUES ({placeholders})"
    with conn:
        conn.executemany(sql, [tuple(r[c] for c in cols) for r in rows])
    return len(rows)


def run_one_poll() -> int:
    snapshot_at = datetime.now(timezone.utc).isoformat()
    events = fetch_active_weather_events()
    rows: list[dict] = []
    for ev in events:
        rows.extend(process_event_dict(ev, snapshot_at))
    n = insert_snapshot_rows(rows)
    print(f"[snapshot] {snapshot_at}: {len(events)} events, {n} sub-market rows inserted")
    return n


def main() -> None:
    print("[snapshot] starting daemon loop")
    db.init_snapshot_db()   # bucket_snapshots lives in the SEPARATE snapshot DB
    consecutive_failures = 0
    while True:
        try:
            run_one_poll()
            consecutive_failures = 0
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception as e:
            consecutive_failures += 1
            print(f"[snapshot] poll failed ({consecutive_failures}/{MAX_CONSECUTIVE_FAILURES}): {e}")
            if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                print(f"[snapshot] FATAL: {consecutive_failures} consecutive failures, re-raising for systemd")
                raise
        time.sleep(POLL_INTERVAL_SEC)


if __name__ == "__main__":
    main()
