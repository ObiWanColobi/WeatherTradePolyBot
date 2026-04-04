"""
Calibration Scheduler
----------------------
Manages automatic, passive execution of the two calibration backfill passes
inside the bot's main poll loop. Designed to be called from weather_bot.py —
no manual intervention needed.

── What runs when ─────────────────────────────────────────────────────────────

  startup()
    Called once at bot start (including restarts after downtime).
    Runs both passes immediately to catch up on any trades that closed
    while the bot was offline. DB-as-cache makes this safe and cheap —
    already-resolved/already-temped rows are skipped in O(1).

  on_poll(poll_number)
    Called every poll cycle (every 60s). Internally rate-limits each pass:

    Resolution pass (CLOB):
      Fires every RESOLUTION_POLL_INTERVAL polls (~15 min).
      Does a cheap DB pre-check first — if there are no early-exit trades
      lacking actual_resolution, the CLOB is never touched.

    Temperature pass (Open-Meteo archive):
      Fires at most once per UTC calendar day.
      Also fires mid-day if newly closed trades appear that lack temperature
      data — checked via a cheap DB count query before any API calls.
      After firing, next run is suppressed until the next UTC day OR until
      new uncovered trades appear (whichever comes first, respecting the
      daily cap).

── Resilience ─────────────────────────────────────────────────────────────────
  If the bot was down for N hours:
    - startup() runs both passes and fills all gaps in one shot
    - on_poll() resumes normal cadence from there

  If Open-Meteo archive returns an error for a city:
    - That city's trades are skipped silently; rows stay NULL
    - Next temperature pass retries them (DB-as-cache handles idempotency)

  If CLOB returns 404 for a token:
    - Trade stays unresolved; retried next resolution pass
    - Once CLOB stops serving a token permanently, it stays NULL (not an error)

── Rate impact ────────────────────────────────────────────────────────────────
  Resolution: 1 CLOB call per unresolved early-exit trade, up to 4x/hour
  Temperature: 1 archive call per unique city, once per UTC day max
  Both negligible vs ~57 ensemble calls/hour from the main bot loop.
"""
from datetime import datetime, timezone

import db
from weather_calibration import _run_resolution_pass, _run_temperature_pass

# How many polls between resolution checks (~15 min at 60s poll interval)
RESOLUTION_POLL_INTERVAL = 15


# ── Module-level state (in-memory, resets on bot restart — intentional) ───────

_last_temp_date: str | None = None      # UTC date string 'YYYY-MM-DD' of last temp pass
_last_temp_closed_count: int = 0        # closed trade count seen at last temp pass


# ── Internal helpers ──────────────────────────────────────────────────────────

def _resolution_candidates_exist() -> bool:
    """
    Cheap DB check: are there any early-exit closed trades without actual_resolution?
    Avoids firing CLOB calls when there's nothing to resolve.
    """
    all_trades = db.get_all_trades()
    return any(
        t.get("status") == "closed"
        and t.get("exit_reason")
        and "resolved" not in (t.get("exit_reason") or "").lower()
        and t.get("actual_resolution") is None
        and t.get("token_id")
        for t in all_trades
    )


def _temperature_candidates_exist() -> int:
    """
    Returns count of closed trades that still need actual_temperature.
    Zero means temperature pass can be skipped entirely.
    """
    now    = datetime.now(timezone.utc)
    trades = db.get_all_trades()
    count  = 0
    for t in trades:
        if t.get("status") != "closed":
            continue
        if t.get("actual_temperature") is not None:
            continue
        if not t.get("city") or not t.get("end_date") or not t.get("threshold"):
            continue
        # Only count trades whose end_date has passed
        try:
            ed = datetime.fromisoformat(t["end_date"].replace("Z", "+00:00"))
            if ed.tzinfo is None:
                ed = ed.replace(tzinfo=timezone.utc)
            if ed < now:
                count += 1
        except Exception:
            pass
    return count


def _today_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


# ── Public interface ──────────────────────────────────────────────────────────

def startup():
    """
    Run both passes at bot startup. Catches up from any downtime.
    Quiet mode — only prints summary lines, not per-trade details.
    """
    global _last_temp_date, _last_temp_closed_count

    print("[calibration] Startup catch-up...")

    # Resolution pass
    if _resolution_candidates_exist():
        print("[calibration] Running resolution pass (catch-up)...")
        counts = _run_resolution_pass(quiet=True)
        print(f"[calibration] Resolution: resolved={counts['resolved']}  "
              f"settling={counts['settling']}  no_data={counts['no_data']}")
    else:
        print("[calibration] Resolution: nothing to backfill.")

    # Temperature pass
    temp_pending = _temperature_candidates_exist()
    if temp_pending > 0:
        print(f"[calibration] Running temperature pass ({temp_pending} trade(s) pending)...")
        counts = _run_temperature_pass(quiet=True)
        print(f"[calibration] Temperature: filled={counts['filled']}  "
              f"no_coords={counts['no_coords']}  no_archive={counts['no_archive']}")
    else:
        print("[calibration] Temperature: nothing to backfill.")

    # Record state so on_poll knows we already ran today
    _last_temp_date = _today_utc()
    _last_temp_closed_count = len([
        t for t in db.get_all_trades() if t.get("status") == "closed"
    ])

    print("[calibration] Startup catch-up complete.\n")


def on_poll(poll_number: int):
    """
    Called every poll. Rate-limits each pass internally.
    Prints nothing if there's nothing to do — zero log noise on idle polls.
    """
    global _last_temp_date, _last_temp_closed_count

    # ── Resolution pass: every RESOLUTION_POLL_INTERVAL polls ────────────────
    if poll_number % RESOLUTION_POLL_INTERVAL == 0:
        if _resolution_candidates_exist():
            print("[calibration] Running resolution pass...")
            counts = _run_resolution_pass(quiet=True)
            if counts["resolved"] > 0:
                print(f"[calibration] Resolution: {counts['resolved']} resolved  "
                      f"{counts['settling']} settling  {counts['no_data']} no_data")

    # ── Temperature pass: once per UTC day, or when new trades appear ─────────
    today         = _today_utc()
    is_new_day    = today != _last_temp_date

    # Count current closed trades — cheap way to detect newly closed positions
    current_closed = len([t for t in db.get_all_trades() if t.get("status") == "closed"])
    new_trades_closed = current_closed > _last_temp_closed_count

    should_run_temp = False
    if is_new_day:
        # New UTC day — run regardless, reset the daily gate
        should_run_temp = True
    elif new_trades_closed:
        # New trades closed since last temp pass — check if any need temperature
        temp_pending = _temperature_candidates_exist()
        if temp_pending > 0:
            should_run_temp = True

    if should_run_temp:
        temp_pending = _temperature_candidates_exist()
        if temp_pending > 0:
            print(f"[calibration] Running temperature pass ({temp_pending} pending)...")
            counts = _run_temperature_pass(quiet=True)
            if counts["filled"] > 0:
                print(f"[calibration] Temperature: filled={counts['filled']}  "
                      f"no_coords={counts['no_coords']}  no_archive={counts['no_archive']}")

        # Update state regardless of whether anything was filled —
        # prevents hammering the archive API if fills repeatedly return 0
        _last_temp_date = today
        _last_temp_closed_count = current_closed
