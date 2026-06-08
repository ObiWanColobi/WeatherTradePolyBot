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
POLL_INTERVAL_SEC = 300  # 5 minutes
REQUEST_TIMEOUT_SEC = 15
MAX_CONSECUTIVE_FAILURES = 6  # 6 * 5min = 30min of dead polls before re-raise

_PAGE_SIZE = 100
_MAX_OFFSET = 2000
_ORDERBOOK_DEPTH = 3

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


def process_event_dict(event: dict, snapshot_at_utc: str) -> Iterator[dict]:
    """Yield one row dict per sub-market in this event.
    Skips non-daily-temp events and unparseable sub-markets.
    """
    parsed_slug = parse_event_slug(event.get("slug", ""))
    if parsed_slug is None:
        return

    event_slug = event["slug"]
    event_end_iso = event.get("endDate") or ""
    condition_id = event.get("conditionId")

    for m in event.get("markets") or []:
        gtitle = m.get("groupItemTitle", "")
        lo_f, hi_f = parse_bucket_bounds(gtitle)
        bucket_type = classify_bucket_type(gtitle)
        is_open_tail = 1 if (lo_f is None or hi_f is None) and bucket_type == "tail" else 0

        bb = _to_float(m.get("bestBid"))
        ba = _to_float(m.get("bestAsk"))
        ltp = _to_float(m.get("lastTradePrice"))
        mid = (bb + ba) / 2 if (bb is not None and ba is not None and bb > 0 and ba > 0) else None

        ob_bids = m.get("orderBook", {}).get("bids") if isinstance(m.get("orderBook"), dict) else None
        ob_asks = m.get("orderBook", {}).get("asks") if isinstance(m.get("orderBook"), dict) else None

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
            "orderbook_bids_json":     json.dumps(ob_bids[:_ORDERBOOK_DEPTH]) if ob_bids else None,
            "orderbook_asks_json":     json.dumps(ob_asks[:_ORDERBOOK_DEPTH]) if ob_asks else None,
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
