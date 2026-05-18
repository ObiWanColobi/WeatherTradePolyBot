#!/usr/bin/env python3
"""
Weather Trading Bot
--------------------
Paper-trading bot for Polymarket daily temperature threshold markets.
Runs a continuous poll loop — entry is gated entirely by the decision layer.

Each poll:
  1. Exit pass  — check open positions for early-exit triggers
  2. Entry pass — scan markets, evaluate candidates, place approved trades

Usage:
    python weather_bot.py
    python weather_bot.py --dry-run  # scan + evaluate but never place orders
"""
import argparse
import os
import time
from datetime import datetime, timezone

import db
import health
from config import WEATHER, PAPER_STARTING_BALANCE, TRADING_MODE
from notifications import notify
from weather_risk import RiskManager, RiskState
from layers.layer3_weather import WeatherLayer
from executor.weather_exit import check_weather_exit
import markets.metar_observer as metar_observer
from markets.flip_detector import FlipDetector, is_enabled as _flip_enabled
from weather_scanner import run_scan
from weather_decision import evaluate, print_audit
from weather_extended import check_extended_position
from weather_resolver import run_resolve_pass
import weather_catalog
import weather_calibration_scheduler as _calibration
import trader_monitor as _trader_monitor

POLL_INTERVAL          = WEATHER.get("bot_poll_interval_seconds",  60)
CACHE_REFRESH          = WEATHER.get("bot_cache_refresh_polls",    10)
DEFAULT_MAX_BET        = WEATHER.get("kelly_max_bet_usdc",         50.00)
TRADER_MONITOR_EVERY_N = WEATHER.get("trader_monitor_poll_every_n", 10)
POSITION_SYNC_EVERY_N  = WEATHER.get("position_sync_poll_every_n",  10)

_layer        = WeatherLayer()
_risk_manager: RiskManager | None = None
_flip_detector = FlipDetector()


from executor import create_executor

_executor = create_executor()


# -- Startup prompt -----------------------------------------------------------

def _prompt_max_bet() -> float:
    """Ask the user for a max-bet cap at startup. Enter to accept default."""
    try:
        raw = input(f"  Max bet per trade [${DEFAULT_MAX_BET:.0f} USDC]: ").strip()
        if raw:
            val = float(raw)
            if val > 0:
                return val
    except (ValueError, EOFError):
        pass
    return DEFAULT_MAX_BET


# -- Exit pass ----------------------------------------------------------------

def run_exit_pass():
    # ── Phase 1: Manage pending GTC exit orders ──────────────────────────────
    pending_exits = db.get_exit_pending_trades()
    exit_pending_parents = set()
    for ptrade in pending_exits:
        parent_id = ptrade.get("parent_trade_id") or ptrade["id"]
        if parent_id in exit_pending_parents:
            continue
        exit_pending_parents.add(parent_id)
        _executor.manage_pending_exit(ptrade)

    # ── Phase 2: Evaluate new exit signals ───────────────────────────────────
    if not db.get_open_trades():
        return

    _executor.update_open_positions()
    db.record_account_value()

    if WEATHER.get("metar_enabled", False):
        try:
            metar_observer.refresh_all()
        except Exception as e:
            print(f"  [metar] refresh failed: {e}")

    # Re-fetch after update so the exit loop sees the freshly-written prices,
    # not the stale values from before update_open_positions() ran.
    open_trades = db.get_open_trades()
    closed_this_pass = set()

    for trade in open_trades:
        if trade["id"] in closed_this_pass:
            continue
        # Skip trades already being exited via GTC
        parent_id = trade.get("parent_trade_id") or trade["id"]
        if parent_id in exit_pending_parents:
            continue
        # Reconstruct the minimal market dict from stored trade fields.
        # The Gamma API has no reliable single-market lookup endpoint —
        # ?conditionId= ignores the filter, /markets/<id> returns 422.
        # _layer.scan() needs: question, id, end_date, price.
        # entry_price is always the YES market price (stored at open time).
        market_data = {
            "question": trade["market_name"],
            "id":       trade["market_id"],
            "end_date": trade.get("end_date", ""),
            "price":    trade.get("entry_price") or trade.get("fill_price", 0.5),
            "token_id": trade.get("token_id"),
        }

        current_ens_pct, ens_yes, ens_n = _get_current_ensemble(trade, market_data)

        # Persist latest ensemble counts so the dashboard can show current vs entry.
        # Only write when the read is valid — if _get_current_ensemble returned None
        # (target date outside forecast window), leave the last-good values in place
        # and let the UI render them with a (stale) tag via current_ensemble_read_at.
        if ens_yes is not None and ens_n is not None:
            db.update_trade(trade["id"], {
                "current_ensemble_yes":     ens_yes,
                "current_ensemble_n":       ens_n,
                "current_ensemble_read_at": datetime.now(timezone.utc).isoformat(),
            })

        metar_state = (
            metar_observer.get_state(trade.get("city") or "")
            if WEATHER.get("metar_enabled", False) else None
        )
        sig = check_weather_exit(trade, market_data, current_ens_pct, metar_state=metar_state)

        _city      = (trade.get("city") or "?").title()
        _dir       = (trade.get("direction") or "?").upper()
        _thr       = trade.get("threshold") or "?"
        _fill      = trade.get("fill_price") or trade.get("entry_price")
        _now       = trade.get("current_price")
        _shares    = trade.get("shares") or 0
        _pnl       = (_now - _fill) * _shares if (_now is not None and _fill is not None) else None
        _fill_str  = f"{_fill:.0%}" if _fill is not None else "?"
        _now_str   = f"{_now:.0%}"  if _now  is not None else "?"
        _pnl_str   = f"{_pnl:+.2f}" if _pnl is not None else "?"
        _ent_yes   = trade.get("entry_ensemble_yes")
        _ent_n     = trade.get("entry_ensemble_n")
        _ent_ens   = f"{int(_ent_yes)}/{_ent_n}" if _ent_yes is not None and _ent_n else "?"
        _cur_ens   = f"{ens_yes}/{ens_n}" if ens_yes is not None else "?"
        _pos_line  = (
            f"{_city:<12} {_dir:<3}  {_thr:<6}  "
            f"entry={_fill_str}→now={_now_str}  pnl=${_pnl_str}  "
            f"ens={_ent_ens}→{_cur_ens}"
        )

        if sig.should_exit:
            parent_id = trade.get("parent_trade_id") or trade["id"]
            legs = db.get_position_legs(parent_id)
            n_legs = len(legs)
            # Propagate the freshly-fetched ensemble to all legs so every
            # leg records the same exit ensemble (they close simultaneously)
            if ens_yes is not None and ens_n is not None and n_legs > 1:
                _now_iso = datetime.now(timezone.utc).isoformat()
                for leg in legs:
                    if leg["id"] != trade["id"]:
                        db.update_trade(leg["id"], {
                            "current_ensemble_yes":     ens_yes,
                            "current_ensemble_n":       ens_n,
                            "current_ensemble_read_at": _now_iso,
                        })
            leg_suffix = f" (closing all {n_legs} legs)" if n_legs > 1 else ""
            print(
                f"  [exit] {_pos_line}  -- {sig.reason}{leg_suffix}"
                + (" [URGENT]" if sig.urgent else "")
            )
            _executor.initiate_exit(trade, reason=sig.reason)
            for leg in legs:
                closed_this_pass.add(leg["id"])
        else:
            print(f"  [hold] {_pos_line}  -- {sig.reason}")


def _get_current_ensemble(trade: dict, market_data: dict) -> tuple[float | None, int | None, int | None]:
    """Returns (pct, yes_count, n_count) from the latest ensemble, or (None, None, None)."""
    try:
        scan_data = _layer.scan(market_data)
        if scan_data and scan_data.get("ensemble_n", 0) >= 10:
            yes = scan_data.get("yes_ensemble")
            n   = scan_data.get("ensemble_n")
            pct = yes / n if yes is not None and n else None
            return pct, yes, n
    except Exception:
        pass
    return None, None, None


# -- Entry pass ---------------------------------------------------------------

def run_entry_pass(already_traded: set, max_bet: float, dry_run: bool = False) -> tuple[int, set]:
    balance    = db.get_balance()
    open_market_ids = db.get_open_market_ids()
    all_candidates = run_scan(include_today=False, exclude_market_ids=open_market_ids)
    db.save_scan_cache(all_candidates)   # dashboard reads from here — no separate scan needed

    weather_condition_ids = {c["_market"]["id"] for c in all_candidates if c.get("_market")}

    # Phase2-06: shadow flip detector over the full candidate set (not just
    # not-yet-traded ones) so coverage continues post-entry. No new API —
    # midpoints come straight off the scanner-cached _market dicts.
    if _flip_enabled():
        for c in all_candidates:
            m = c.get("_market") or {}
            mid = m.get("price")
            mid_id = m.get("id")
            if mid is None or not mid_id:
                continue
            event = _flip_detector.observe(mid_id, float(mid))
            if event:
                try:
                    db.write_flip_event({
                        "detected_at":    datetime.now(timezone.utc).isoformat(),
                        "market_id":      mid_id,
                        "city":           c.get("city"),
                        "direction":      event["direction"],
                        "price_before":   event["price_before"],
                        "price_after":    event["price_after"],
                        "delta_pct":      event["delta_pct"],
                        "minutes_window": event["minutes_window"],
                        "poll_tick_id":   None,
                    })
                    print(
                        f"  [flip] {(c.get('city_display') or c.get('city') or '?'):<16} "
                        f"{event['direction']:<8}  {event['price_before']:.0%} -> {event['price_after']:.0%}  "
                        f"({event['delta_pct']:+.0%} in {event['minutes_window']:.0f}min)"
                    )
                except Exception as e:
                    print(f"[flip_detector] write failed market={mid_id}: {e}")

    # Strip markets already entered this session
    candidates = [c for c in all_candidates if c["_market"]["id"] not in already_traded]

    if not candidates:
        print("  [entry] No scanner candidates after session filter.")
        return 0, weather_condition_ids

    results  = evaluate(candidates, balance, max_bet_override=max_bet, risk_manager=_risk_manager)
    approved = [r for r in results if r.verdict == "APPROVED"]
    rejected = [r for r in results if r.verdict == "REJECTED"]

    # Always log rejections so you can see why opportunities were skipped
    for r in rejected:
        c      = r.candidate
        _ens_pct = c.get("ens_pct")
        _conv    = max(_ens_pct, 1 - _ens_pct) if _ens_pct is not None else 0.0
        _utag    = " [unanimous]" if _conv >= 0.97 else ""
        _tier    = "STRONG" if c["edge_pct"] >= 0.30 else "EDGE" if c["edge_pct"] >= 0.15 else f"WEAK{_utag}"
        _ens   = f"{c['ens_yes']}/{c['ens_n']}" if c.get("ens_yes") is not None else "?"
        _vol   = f"${c['volume']:,.0f}" if c.get("volume") is not None else "?"
        _hrs   = f"{c['hours_to_close']:.0f}h" if c.get("hours_to_close") is not None else "?"
        _thr   = c.get("threshold_str") or "?"
        _tc    = r.checks.get("trader_consensus", {})
        _tr    = f"  tr={_tc['signal']}({_tc['same']}v{_tc['opp']})" if _tc else ""
        print(
            f"  [skip] {c['city_display']:<16} {r.direction.upper():<4} {_thr:<6}  "
            f"mkt={c['market_price']:.0%}  mdl={c['model_prob']:.0%}  edge={c['edge_pct']:.0%}  "
            f"{_tier:<6}  ens={_ens}  vol={_vol}  closes={_hrs}{_tr}  -- {r.reason}"
        )

    if not approved:
        print("  [entry] No approved trades this poll.")
        return 0, weather_condition_ids

    entered = 0
    for result in approved:
        c      = result.candidate
        market = dict(c["_market"])   # copy — add city for the trade record
        market["city"] = c["city"]

        consensus = result.checks.get("trader_consensus", {})
        consensus_str = f"  traders={consensus['signal']}({consensus['same']}vs{consensus['opp']})" if consensus else ""
        utag = " [unanimous]" if "[unanimous]" in result.reason else ""
        print(
            f"\n  [entry] {c['city_display']} {result.direction.upper()}  "
            f"edge={c['edge_pct']:.0%}  score={result.score:.3f}  "
            f"size=${result.size_usdc:.2f}  days_out={c['days_to_resolution']}"
            f"{utag}{consensus_str}"
        )

        if dry_run:
            print("  [entry] DRY RUN — order not placed.")
            entered += 1
            continue

        estimate = {
            "probability":        c["model_prob"],
            "edge_score":         c["edge_pct"] * 100,
            "sources":            ["weather_forecast"],
            "entry_ensemble_pct": c["ens_pct"],
            "entry_ensemble_yes": c.get("ens_yes"),
            "entry_ensemble_n":   c.get("ens_n"),
            "threshold":          c["_scan_data"].get("target_str"),
            "sizing_id":          result.sizing_id,
        }

        filled = _executor.place_order(
            market    = market,
            direction = result.direction.upper(),
            size_usdc = result.size_usdc,
            estimate  = estimate,
        )

        already_traded.add(market["id"])
        if filled:
            entered += 1

    return entered, weather_condition_ids


# -- Extended positions pass ---------------------------------------------------

def run_extended_positions_pass(dry_run: bool = False):
    """Check open parent trades for scale-in add-on eligibility."""
    if not WEATHER.get("extended_positions_enabled", True):
        return

    open_trades = db.get_open_trades()
    parents = [t for t in open_trades if not t.get("parent_trade_id")]

    if not parents:
        return

    for trade in parents:
        market_data = {
            "question": trade["market_name"],
            "id":       trade["market_id"],
            "end_date": trade.get("end_date", ""),
            "price":    trade.get("entry_price") or trade.get("fill_price", 0.5),
            "token_id": trade.get("token_id"),
        }

        try:
            scan_data = _layer.scan(market_data)
        except Exception:
            continue

        if not scan_data or scan_data.get("ensemble_n", 0) < 10:
            continue

        ens_yes = scan_data.get("yes_ensemble")
        ens_n   = scan_data.get("ensemble_n")
        model_prob = scan_data.get("probability") or scan_data.get("prob") or 0
        market_price = trade.get("entry_price") or 0.5

        try:
            import markets.polymarket as _pm
            market_info = _pm.get_market_by_id(trade["market_id"])
            if market_info and market_info.get("price") is not None:
                market_price = market_info["price"]
        except Exception:
            pass

        hours_to_close = None
        end_date = trade.get("end_date", "")
        if end_date:
            try:
                end = datetime.fromisoformat(end_date.replace("Z", "+00:00"))
                if end.tzinfo is None:
                    end = end.replace(hour=23, minute=59, second=59, tzinfo=timezone.utc)
                hours_to_close = (end - datetime.now(timezone.utc)).total_seconds() / 3600
            except Exception:
                pass

        days_to_res = int(hours_to_close / 24) if hours_to_close is not None else 0

        current_scan = {
            "ens_yes":            ens_yes,
            "ens_n":              ens_n,
            "ens_pct":            ens_yes / ens_n if ens_yes is not None and ens_n else None,
            "model_prob":         model_prob,
            "market_price":       market_price,
            "hours_to_close":     hours_to_close,
            "days_to_resolution": days_to_res,
        }

        city_name = (trade.get("city") or "?").title()
        dir_label = trade["direction"].upper()
        ens_label = f"{int(ens_yes)}/{ens_n}" if ens_yes is not None and ens_n else "?"
        htc_label = f"{hours_to_close:.1f}h" if hours_to_close is not None else "?"
        print(
            f"  [extended] {city_name:<14} {dir_label}  "
            f"mkt={market_price:.0%}  mdl={model_prob:.0%}  "
            f"ens={ens_label}  close={htc_label}  legs={db.get_leg_count(trade['id'])}"
        )

        result = check_extended_position(trade, current_scan)
        if result is None:
            continue

        city      = (trade.get("city") or "?").title()
        direction = result["direction"]
        leg_num   = result["leg_number"]

        print(
            f"\n  [extend] {city} {direction} — adding leg {leg_num}  "
            f"size=${result['size_usdc']:.2f}  ens={int(result['ens_yes'])}/{result['ens_n']}  "
            f"hours_left={result['hours_to_close']:.1f}h"
        )

        if dry_run:
            print("  [extend] DRY RUN — add-on not placed.")
            continue

        market = {
            "id":          trade["market_id"],
            "question":    trade["market_name"],
            "end_date":    trade.get("end_date"),
            "price":       market_price,
            "token_id":    trade.get("token_id"),
            "city":        trade.get("city"),
            "market_url":  trade.get("market_url"),
            "liquidity":   trade.get("liquidity"),
            "volume":      trade.get("volume_24h"),
        }

        estimate = {
            "probability":        result["model_prob"],
            "edge_score":         (result["model_prob"] - market_price) * 100 if direction == "YES"
                                  else (market_price - result["model_prob"]) * 100,
            "sources":            ["weather_forecast"],
            "entry_ensemble_pct": result["ens_pct"],
            "entry_ensemble_yes": result["ens_yes"],
            "entry_ensemble_n":   result["ens_n"],
            "threshold":          trade.get("threshold"),
        }

        legs_before = db.get_leg_count(result["parent_trade_id"])
        _executor.place_extended_order(
            market=market,
            direction=direction,
            size_usdc=result["size_usdc"],
            estimate=estimate,
            parent_trade_id=result["parent_trade_id"],
            leg_number=result["leg_number"],
        )
        if db.get_leg_count(result["parent_trade_id"]) == legs_before:
            # Order was rejected — suppress this trade from extend checks for cooldown period
            from weather_extended import record_extend_rejection
            record_extend_rejection(result["parent_trade_id"])



_last_digest_date: str = ""


def _send_daily_digest(date_str: str) -> None:
    """Compile and send daily P&L digest for the given UTC date."""
    balance = db.get_balance()
    closed_today = [t for t in db.get_closed_trades()
                    if t.get("closed_at", "").startswith(date_str)]
    opened_today = [t for t in db.get_open_trades()
                    if t.get("created_at", "").startswith(date_str)]

    realized_pnl = sum(t.get("pnl", 0) or 0 for t in closed_today)
    wins = sum(1 for t in closed_today if (t.get("pnl", 0) or 0) > 0)
    losses = len(closed_today) - wins
    open_count = len(db.get_open_trades())

    notify("info", f"Daily Digest — {date_str}",
           f"End-of-day summary for {date_str}",
           fields={
               "Balance": f"${balance:.2f}",
               "Realized P&L": f"${realized_pnl:+.2f}",
               "Opened": str(len(opened_today)),
               "Closed": str(len(closed_today)),
               "Win/Loss": f"{wins}W-{losses}L",
               "Open Positions": str(open_count),
           })
    print(f"[bot] Daily digest sent for {date_str}")


# -- Main loop ----------------------------------------------------------------

def _prompt_startup() -> bool:
    """
    Ask whether to resume existing session or reset.
    Returns True if the user chose to reset.
    """
    summary = db.get_session_summary()
    open_n   = summary["open_count"]
    closed_n = summary["closed_count"]
    balance  = summary["balance"]

    print("\n  ┌─ Previous session ─────────────────────────────────┐")
    print(f"  │  Balance : ${balance:>10,.2f}                          │")
    print(f"  │  Open    : {open_n:>4} position(s)                       │")
    print(f"  │  Closed  : {closed_n:>4} trade(s)                         │")
    print("  └────────────────────────────────────────────────────┘")
    print()
    print("  [R] Resume — keep open positions and continue")
    print("  [C] Clear  — wipe all trades, reset balance to "
          f"${PAPER_STARTING_BALANCE:,.0f}")
    print()

    while True:
        try:
            choice = input("  Choice [R/C]: ").strip().upper()
        except EOFError:
            choice = "R"

        if choice in ("R", ""):
            print("  Resuming previous session.\n")
            return False

        if choice == "C":
            print(f"\n  WARNING: This will permanently delete {open_n} open position(s) "
                  f"and {closed_n} closed trade(s).")
            try:
                confirm1 = input("  Type YES to confirm: ").strip().upper()
            except EOFError:
                confirm1 = ""
            if confirm1 != "YES":
                print("  Reset cancelled — resuming instead.\n")
                return False

            try:
                confirm2 = input("  Type YES again to proceed: ").strip().upper()
            except EOFError:
                confirm2 = ""
            if confirm2 != "YES":
                print("  Reset cancelled — resuming instead.\n")
                return False

            db.reset_paper_trading()
            print("  All trades cleared. Balance reset to "
                  f"${PAPER_STARTING_BALANCE:,.0f}.\n")
            return True

        print("  Please enter R to resume or C to clear.")


def run(dry_run: bool = False):
    db.init_db()
    global _risk_manager
    _risk_manager = RiskManager()
    _risk_manager.startup_cleanup()

    # Apply live-mode config overrides (centralized in config.py)
    from config import apply_live_overrides
    apply_live_overrides()
    if TRADING_MODE == "live":
        print("[bot] Live mode — skipping session reset prompt.")
    else:
        _prompt_startup()

    mode_label = "LIVE" if TRADING_MODE == "live" else "paper"
    print(f"[bot] Weather Trading Bot — {mode_label} mode")
    print(f"      Poll interval : {POLL_INTERVAL}s")
    if dry_run:
        print("      Mode          : DRY RUN (no orders will be placed)")

    # Live mode has no stdin under systemd — read the (post-override) cap
    # directly from WEATHER so the interactive prompt's stale module-level
    # DEFAULT_MAX_BET can't leak in. Paper mode keeps the prompt.
    if TRADING_MODE == "live":
        max_bet = WEATHER["kelly_max_bet_usdc"]
    else:
        max_bet = _prompt_max_bet()
    print(f"      Max bet       : ${max_bet:.2f} USDC per trade\n")

    # Calibration catch-up — fills any resolution/temperature gaps from downtime
    try:
        _calibration.startup()
    except Exception as e:
        print(f"[bot] Calibration startup pass failed (non-fatal): {e}\n")

    # Reconcile positions with exchange on startup
    _executor.reconcile_positions()

    # Crash detection
    _heartbeat_path = os.path.join(os.path.dirname(__file__), "heartbeat.json")
    crash_info = health.check_crash(
        _heartbeat_path,
        stale_threshold=WEATHER.get("heartbeat_stale_threshold", 300),
    )
    if crash_info:
        notify("critical", "Bot Restarted After Crash",
               f"Last heartbeat was {crash_info['minutes_ago']} minutes ago. "
               f"Poll count at crash: {crash_info['poll_count']}. "
               f"Open positions: {crash_info['open_positions']}.",
               fields={"Last Seen": crash_info["last_timestamp"],
                        "Balance at Crash": f"${crash_info['balance']:.2f}"})

    # Catalog snapshot on startup
    print("[bot] Running catalog snapshot...")
    try:
        weather_catalog.run_snapshot()
        weather_catalog.backfill_resolutions()
    except Exception as e:
        print(f"[bot] Catalog snapshot failed (non-fatal): {e}")
    _last_catalog_date = datetime.now(timezone.utc).date().isoformat()

    # Initial layer refresh (loads top-N market IDs + clears forecast cache)
    print("[bot] Refreshing weather layer...")
    _layer.refresh()

    balance = db.get_balance()
    print(f"[bot] Balance: ${balance:,.2f}\n")

    poll             = 0
    already_traded: set = set()
    _prev_open_map: dict = {}   # id → trade dict, snapshot from end of last poll

    while True:
        poll += 1
        # Daily UTC rollover — run a fresh catalog snapshot + resolution backfill
        # so weather_city_log accumulates without requiring a bot restart.
        _today_utc = datetime.now(timezone.utc).date().isoformat()
        if _today_utc != _last_catalog_date:
            print(f"[bot] UTC day rollover ({_last_catalog_date} → {_today_utc}) — refreshing catalog")
            try:
                weather_catalog.run_snapshot()
                weather_catalog.backfill_resolutions()
            except Exception as e:
                print(f"[bot] Daily catalog pass failed (non-fatal): {e}")
            _last_catalog_date = _today_utc
        now_str = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")
        print(f"\n{'─' * 60}")
        _cur_open     = db.get_open_trades()
        _cur_open_ids = {t["id"] for t in _cur_open}
        print(f"[bot] Poll #{poll}  {now_str}  balance=${db.get_balance():,.2f}  open={len(_cur_open_ids)}")

        # Detect trades closed externally between polls (e.g. dashboard manual close)
        # _prev_open_map is set at end of last poll — anything missing now wasn't closed by the bot
        if _prev_open_map:
            for tid, t in _prev_open_map.items():
                if tid not in _cur_open_ids:
                    city      = (t.get("city") or "?").title()
                    direction = t.get("direction", "?").upper()
                    threshold = t.get("threshold") or "?"
                    closed    = db.get_trade_by_id(tid)
                    pnl       = (closed.get("pnl") or 0.0) if closed else 0.0
                    print(f"[bot] Externally closed: {city} {direction} {threshold}  P&L: ${pnl:+.2f}")

        # Periodic layer refresh (clears stale ensemble cache)
        if poll % CACHE_REFRESH == 0:
            print("[bot] Refreshing layer caches...")
            _layer.refresh()

        # Calibration pass — passive data gathering, rate-limited internally
        try:
            _calibration.on_poll(poll)
        except Exception as e:
            print(f"[calibration] on_poll error (non-fatal): {e}")

        # Resolve pass — settle expired markets before checking exits/entries
        settled = run_resolve_pass(_executor)
        if settled:
            print(f"\n[bot] Settled {settled} resolved position(s).")
            # Clean up block list — remove resolved markets
            if _risk_manager:
                open_mids = db.get_open_market_ids()
                for mid in _risk_manager._read_block_list():
                    if mid not in open_mids:
                        _risk_manager.clear_resolved(mid)

        # Claims pass — process on-chain claims for winning live trades
        try:
            _executor.process_pending_claims()
        except Exception as e:
            print(f"[claims] process_pending_claims error (non-fatal): {e}")

        # Position sync — detect trades gone from exchange (manual claims, missed resolutions)
        if poll % POSITION_SYNC_EVERY_N == 0:
            try:
                _executor.sync_positions_with_exchange()
            except Exception as e:
                print(f"[sync] sync_positions_with_exchange error (non-fatal): {e}")

        # Risk check — portfolio-level circuit breaker
        risk_state = _risk_manager.check()

        # Exit pass (always runs — existing per-trade exit logic is independent of risk state)
        run_exit_pass()

        # Entry pass — blocked when circuit breaker is tripped
        if risk_state == RiskState.NORMAL:
            entered, weather_condition_ids = run_entry_pass(already_traded, max_bet, dry_run=dry_run)
            if entered:
                print(f"\n[bot] Entered {entered} new position(s) this poll.")
        else:
            print(f"[risk] Entries halted — daily losses: ${_risk_manager._today_losses:.2f} "
                  f"({_risk_manager.get_loss_pct():.1%} of account)")
            entered = 0
            weather_condition_ids = set()

        # Extended positions pass — scale into existing positions
        if risk_state == RiskState.NORMAL:
            run_extended_positions_pass(dry_run=dry_run)

        # Trader monitor — poll tracked wallets for live positions every 10 polls.
        # Union open-trade markets so coverage continues for markets we hold even
        # after the scanner moves on (otherwise trader_positions is filtered out
        # and trader_forecasts has nothing to freeze at resolution).
        if poll % TRADER_MONITOR_EVERY_N == 0:
            monitored_ids = set(weather_condition_ids) | {
                t["market_id"] for t in db.get_open_trades() if t.get("market_id")
            }
            try:
                _trader_monitor.update(monitored_ids)
            except Exception as e:
                print(f"[trader_monitor] update error (non-fatal): {e}")

        # Snapshot open trades at end of poll for external-close detection next poll
        _prev_open_map = {t["id"]: t for t in db.get_open_trades()}

        # Heartbeat
        health.write_heartbeat(
            _heartbeat_path,
            poll_count=poll,
            open_positions=len(_prev_open_map),
            balance=db.get_balance(),
        )

        # Daily digest — fires once after UTC date rolls over
        global _last_digest_date
        today_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if _last_digest_date and _last_digest_date != today_utc:
            _send_daily_digest(_last_digest_date)
        _last_digest_date = today_utc

        time.sleep(POLL_INTERVAL)


# -- Entry point --------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Weather trading bot")
    parser.add_argument("--dry-run", action="store_true",
                        help="Evaluate trades but do not place any orders")
    args = parser.parse_args()
    run(dry_run=args.dry_run)


if __name__ == "__main__":
    main()
