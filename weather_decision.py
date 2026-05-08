"""
Weather Decision Layer
-----------------------
Takes scanner candidates, runs the full entry gate, applies portfolio
constraints, scores survivors, and returns a ranked list of approved
trades alongside full audit logs for every rejection.

Pipeline per evaluation:
  1. Entry gate      -- 5 data checks (conviction, edge, volume, spread, hours)
  2. Duplicate check -- no same city + same direction already open
  3. Position cap    -- total open trades < max_open_positions
  4. Kelly sizing    -- size with horizon discount; skip if zero
  5. Slippage check  -- simulate fill; reduce size or skip if book too thin
  6. Exposure cap    -- total balance at risk stays under max_exposure_pct
  7. Score & rank    -- edge x conviction x horizon; best trade first

Every candidate gets a verdict logged ('APPROVED' / 'REJECTED') with a
plain-English reason. This audit trail is what makes the bot improvable
over time -- you can see exactly why each opportunity was passed over.

Usage (called by weather_bot.py each poll):
    from weather_decision import evaluate
    results = evaluate(candidates, balance, max_bet_override=50.0)
    approved = [r for r in results if r.verdict == "APPROVED"]
"""
from dataclasses import dataclass, field

from config import WEATHER
import db
from markets.polymarket import simulate_fill
from markets.open_meteo import get_deterministic_per_model
from layers.layer3_weather import CITY_COORDS
from weather_entry import check_entry
from datetime import datetime, timezone
from weather_sizing import kelly_size_with_diagnostics
from weather_risk import RiskManager
import trader_monitor

# -- Config -------------------------------------------------------------------
_MAX_OPEN_POSITIONS          = WEATHER.get("decision_max_open_positions",          5)
_MAX_EXPOSURE_PCT            = WEATHER.get("decision_max_exposure_pct",            0.30)
_MAX_POSITIONS_PER_CITY_DATE = WEATHER.get("decision_max_positions_per_city_date", 2)
_MAX_BET_USDC        = WEATHER.get("kelly_max_bet_usdc",          50.00)
_MAX_SLIPPAGE_PCT    = WEATHER.get("entry_max_slippage_pct",      0.05)
_MIN_BET_USDC        = WEATHER.get("kelly_min_bet_usdc",          5.00)
_MIN_NET_EDGE        = WEATHER.get("entry_min_net_edge_pct",      0.05)

# Horizon discount table (mirrors weather_sizing.py — single source of truth
# kept there; replicated here only for scoring, not for actual sizing)
_HORIZON_DISCOUNTS  = {0: 1.00, 1: 0.85, 2: 0.65}
_HORIZON_DEFAULT    = 0.45


@dataclass
class DecisionResult:
    candidate:  dict
    verdict:    str    # "APPROVED" | "REJECTED"
    reason:     str
    direction:  str  = ""
    score:      float = 0.0
    size_usdc:  float = 0.0
    checks:     dict  = field(default_factory=dict)


def evaluate(
    candidates:       list[dict],
    balance:          float,
    max_bet_override: float | None = None,
    risk_manager:     RiskManager | None = None,
) -> list[DecisionResult]:
    """
    Evaluate all scanner candidates and return decision results.

    Args:
        candidates:       list of candidate dicts from weather_scanner.run_scan()
        balance:          current paper/live balance in USDC
        max_bet_override: runtime cap per trade (overrides config default if set)

    Returns:
        List of DecisionResult — approved trades first (sorted by score desc),
        then all rejections. Caller filters by verdict == "APPROVED" to trade.
    """
    max_bet        = max_bet_override if max_bet_override is not None else _MAX_BET_USDC
    open_trades         = db.get_open_trades()
    open_market_ids     = db.get_open_market_ids()       # market_ids already held
    city_date_counts    = db.get_open_city_date_counts() # positions per (city, date)
    total_exposure      = sum(t.get("size_usdc", 0) for t in open_trades)
    open_count          = len(open_trades)

    # Track approvals provisionally so constraints are applied in rank order
    provisional_exposure        = 0.0
    provisional_count           = 0
    provisional_market_ids      = set(open_market_ids)
    provisional_city_date_counts = dict(city_date_counts)

    approved  = []
    rejected  = []

    for candidate in candidates:
        city      = candidate.get("city", "").lower()
        mkt_price = candidate["market_price"]
        mdl_prob  = candidate["model_prob"]
        direction = "yes" if mdl_prob > mkt_price else "no"

        # ── 0a. YES trades disabled (2026-04-15 — trade review) ──────────────
        if direction == "yes":
            rejected.append(DecisionResult(
                candidate=candidate,
                verdict="REJECTED",
                reason="YES trades disabled (algorithm tuning 2026-04-15)",
                direction=direction,
                checks={},
            ))
            continue

        days      = candidate.get("days_to_resolution", 0)
        ens_n     = candidate.get("ens_n", 0)
        ens_pct   = candidate.get("ens_pct")
        try:
            res_date  = candidate["_market"]["end_date"][:10]   # "YYYY-MM-DD"
            market_id = candidate["_market"].get("id", "")
        except Exception:
            res_date  = ""
            market_id = ""
        city_date_key = (city, res_date)

        # ── 0. Trader consensus (runs for every candidate regardless of gate) ──
        consensus = trader_monitor.get_consensus(market_id, direction)
        # Will be attached to entry.checks after check_entry runs

        # ── 1. Entry gate ─────────────────────────────────────────────────────
        entry = check_entry(candidate["_market"], candidate["_scan_data"], direction=direction)
        # Attach consensus to checks so it flows through to all log paths
        entry.checks["trader_consensus"] = {
            "ok":     True,
            "signal": consensus["signal"],
            "same":   consensus["same"],
            "opp":    consensus["opposite"],
        }
        if not entry.ok:
            rejected.append(DecisionResult(
                candidate=candidate,
                verdict="REJECTED",
                reason=f"entry gate: {entry.reason}",
                direction=direction,
                checks=entry.checks,
            ))
            continue

        # ── 2a. Duplicate: exact same market already held ────────────────────
        # Block re-entry into a market we currently own.
        if market_id and market_id in provisional_market_ids:
            rejected.append(DecisionResult(
                candidate=candidate,
                verdict="REJECTED",
                reason=f"duplicate: already holding this market ({city} {res_date})",
                direction=direction,
                checks=entry.checks,
            ))
            continue

        # ── 2c. Manual close block — re-entry prevention ────────────────────
        if risk_manager and market_id and risk_manager.is_blocked(market_id):
            rejected.append(DecisionResult(
                candidate=candidate,
                verdict="REJECTED",
                reason=f"manually closed this session ({city} {res_date})",
                direction=direction,
                checks=entry.checks,
            ))
            continue

        # ── 2b. City-date concentration cap ──────────────────────────────────
        # Different threshold markets on the same city/date are independent bets
        # but share correlated weather risk. Cap at N positions per city/date.
        existing_count = provisional_city_date_counts.get(city_date_key, 0)
        if existing_count >= _MAX_POSITIONS_PER_CITY_DATE:
            rejected.append(DecisionResult(
                candidate=candidate,
                verdict="REJECTED",
                reason=(
                    f"city-date cap: {existing_count}/{_MAX_POSITIONS_PER_CITY_DATE} "
                    f"positions already open for {city} {res_date}"
                ),
                direction=direction,
                checks=entry.checks,
            ))
            continue

        # ── 3. Position count cap ─────────────────────────────────────────────
        if open_count + provisional_count >= _MAX_OPEN_POSITIONS:
            rejected.append(DecisionResult(
                candidate=candidate,
                verdict="REJECTED",
                reason=f"position cap: already at max {_MAX_OPEN_POSITIONS} open trades",
                direction=direction,
                checks=entry.checks,
            ))
            continue

        # ── 4. Kelly size (with horizon discount + margin scaling) ───────────
        # Unanimous entries use a separate, smaller cap (see weather_sizing.py).
        is_unanimous    = entry.checks.get("edge", {}).get("unanimous", False)
        scan_data       = candidate.get("_scan_data") or {}
        ens_margin_c    = scan_data.get("ensemble_margin_c")
        size, sizing_diag = kelly_size_with_diagnostics(
            balance, mdl_prob, mkt_price, direction, ens_n, days,
            unanimous=is_unanimous,
            ensemble_margin_c=ens_margin_c,
        )
        if not is_unanimous and size > max_bet:
            size = max_bet
            sizing_diag["binding_constraint"] = "cap_per_bet_override"
            sizing_diag["cap_per_bet"]        = max_bet

        if size == 0.0:
            rejected.append(DecisionResult(
                candidate=candidate,
                verdict="REJECTED",
                reason="kelly size zero after discounts (edge too thin)",
                direction=direction,
                checks=entry.checks,
            ))
            continue

        # ── 5. Order book depth / slippage check ──────────────────────────────
        # Simulate the fill at the intended size. If slippage exceeds the
        # threshold, reduce size to the largest amount that stays within it.
        # If even the minimum bet can't be filled cleanly, skip the trade.
        market    = candidate["_market"]
        token_id  = market.get("token_id") if direction == "yes" else (
                        market.get("no_token_id") or market.get("token_id"))
        mid_price = market["price"] if direction == "yes" else (1.0 - market["price"])

        slippage_reduced_to: float | None = None
        if token_id and mid_price > 0:
            _, slippage_pct, fillable = simulate_fill(token_id, size, mid_price)

            if slippage_pct > _MAX_SLIPPAGE_PCT:
                # Binary-search for the largest size that keeps slippage within threshold.
                # We try 75%, 50%, 25% of original size in steps.
                reduced = None
                for pct in (0.75, 0.50, 0.25):
                    trial_size = max(size * pct, _MIN_BET_USDC)
                    _, trial_slip, _ = simulate_fill(token_id, trial_size, mid_price)
                    if trial_slip <= _MAX_SLIPPAGE_PCT:
                        reduced = trial_size
                        break

                if reduced is None:
                    rejected.append(DecisionResult(
                        candidate=candidate,
                        verdict="REJECTED",
                        reason=(
                            f"slippage too high at any size: {slippage_pct:.1%} > "
                            f"{_MAX_SLIPPAGE_PCT:.0%} threshold (thin book)"
                        ),
                        direction=direction,
                        checks=entry.checks,
                    ))
                    continue

                slippage_reduced_to = reduced
                sizing_diag["binding_constraint"] = "slippage"
                size = reduced  # proceed with reduced size

            # ── 5b. Post-slippage net edge check ──────────────────────────────
            slippage_abs = slippage_pct * mid_price
            net_edge     = candidate["edge_pct"] - slippage_abs
            if net_edge < _MIN_NET_EDGE:
                rejected.append(DecisionResult(
                    candidate=candidate,
                    verdict="REJECTED",
                    reason=(
                        f"net edge too low after slippage: "
                        f"{net_edge:.3f} < {_MIN_NET_EDGE:.2f}"
                    ),
                    direction=direction,
                    checks=entry.checks,
                ))
                continue

        # ── 6. Exposure cap ──────────────────────────────────────────────────────
        projected_exposure = total_exposure + provisional_exposure + size
        if projected_exposure / balance > _MAX_EXPOSURE_PCT:
            rejected.append(DecisionResult(
                candidate=candidate,
                verdict="REJECTED",
                reason=(
                    f"exposure cap: adding ${size:.0f} would put "
                    f"{projected_exposure / balance:.0%} of balance at risk "
                    f"(limit {_MAX_EXPOSURE_PCT:.0%})"
                ),
                direction=direction,
                checks=entry.checks,
            ))
            continue

        # ── 7. Score: edge x conviction_strength x horizon_mult ──────────────
        conviction_strength = max(ens_pct, 1.0 - ens_pct) if ens_pct is not None else 0.5
        horizon_mult        = _HORIZON_DISCOUNTS.get(days, _HORIZON_DEFAULT)
        score               = candidate["edge_pct"] * conviction_strength * horizon_mult

        # Register provisionally so later candidates feel this trade's impact
        provisional_market_ids.add(market_id)
        provisional_city_date_counts[city_date_key] = existing_count + 1
        provisional_count    += 1
        provisional_exposure += size

        unanimous_tag = " [unanimous]" if is_unanimous else ""
        # E1-06: log raw + calibrated probability separately + fixed-mode reference.
        # Until Phase 3 Platt calibration ships, raw_prob == calibrated_prob == model_prob.
        # fixed_mode_stake_usdc is the E15.4 fixed_50 baseline, captured for later
        # Kelly-vs-fixed-stake research on real trade outcomes.
        sizing_id: int | None = None
        try:
            sizing_id = db.write_sizing_decision({
                "recorded_at":         datetime.now(timezone.utc).isoformat(),
                "market_id":           market_id,
                "market_name":         candidate.get("_market", {}).get("question") or candidate.get("city_display", ""),
                "city":                city,
                "direction":           direction,
                **{k: sizing_diag[k] for k in (
                    "balance", "model_prob", "market_price", "p_used", "price_used",
                    "ensemble_n", "days_to_resolution", "ensemble_margin_c", "is_unanimous",
                    "edge", "odds", "kelly_raw", "ensemble_scale", "horizon_mult",
                    "margin_mult", "kelly_fraction", "kelly_final", "size_pre_cap",
                    "cap_per_bet", "cap_balance_pct", "size_after_caps", "binding_constraint",
                )},
                "slippage_reduced_to":    slippage_reduced_to,
                "final_size":             size,
                "raw_prob":               mdl_prob,
                "calibrated_prob":        mdl_prob,
                "fixed_mode_stake_usdc":  50.0,
            })
        except Exception as e:
            print(f"[decision] sizing_decision write failed: {e}")

        # Phase2-01: shadow-capture deterministic ICON + GFS at decision time.
        # Wrapped — never blocks the trade flow. Hist-API freshness probe
        # 2026-05-08 confirmed live-forecast endpoint serves real regional data
        # on intraday calls, so seamless models suffice.
        if sizing_id is not None:
            try:
                _write_phase2_snapshot(sizing_id, city, res_date)
            except Exception as e:
                print(f"[decision] phase2 snapshot failed sizing_id={sizing_id}: {e}")

        approved.append(DecisionResult(
            candidate=candidate,
            verdict="APPROVED",
            reason=f"all checks passed{unanimous_tag}",
            direction=direction,
            score=score,
            size_usdc=size,
            checks=entry.checks,
        ))

    # Best score first among approved
    approved.sort(key=lambda r: r.score, reverse=True)

    return approved + rejected


def _temp_for_date(forecast: list[dict], target_date: str) -> float | None:
    for entry in forecast:
        if entry.get("date") == target_date:
            t = entry.get("temp_max_c")
            return float(t) if t is not None else None
    return None


def _write_phase2_snapshot(sizing_id: int, city: str, res_date: str) -> None:
    """Phase2-01 capture: deterministic ICON + GFS at decision time.

    Always inserts a row keyed on `sizing_id`. NULL temps mean either the
    OM call failed or the city is missing from CITY_COORDS — in both cases
    the row's existence is itself useful for coverage analysis.
    """
    coords = CITY_COORDS.get(city)
    icon_temp: float | None = None
    gfs_temp:  float | None = None
    src_tag = "skipped/no-coords"

    if coords:
        # Day count: open-meteo forecast_days param accepts 1..16. We need
        # whatever day the market resolves on, capped at 7 for the Phase2-01
        # snapshot scope. Days >7 are rare for Polymarket weather markets.
        target_dt = datetime.fromisoformat(res_date).date()
        today_dt  = datetime.now(timezone.utc).date()
        days_out  = max(1, min(7, (target_dt - today_dt).days + 1))

        try:
            icon_fc = get_deterministic_per_model(
                coords["lat"], coords["lon"], "icon_seamless",
                tz=coords.get("tz", "auto"), days=days_out,
            )
            icon_temp = _temp_for_date(icon_fc, res_date)
        except Exception as e:
            print(f"[decision] phase2 icon fetch failed city={city}: {e}")

        try:
            gfs_fc = get_deterministic_per_model(
                coords["lat"], coords["lon"], "gfs_seamless",
                tz=coords.get("tz", "auto"), days=days_out,
            )
            gfs_temp = _temp_for_date(gfs_fc, res_date)
        except Exception as e:
            print(f"[decision] phase2 gfs fetch failed city={city}: {e}")

        src_tag = "live-forecast/icon_seamless+gfs_seamless"

    db.write_decision_snapshot({
        "sizing_decision_id": sizing_id,
        "captured_at":        datetime.now(timezone.utc).isoformat(),
        "det_icon_temp_c":    icon_temp,
        "det_gfs_temp_c":     gfs_temp,
        "det_source_tag":     src_tag,
    })


def print_audit(results: list[DecisionResult]):
    """
    Print a human-readable audit of every decision. Useful for dry runs
    and bot startup logging.
    """
    approved  = [r for r in results if r.verdict == "APPROVED"]
    rejected  = [r for r in results if r.verdict == "REJECTED"]

    print(f"\n  Decision audit: {len(approved)} approved, {len(rejected)} rejected\n")

    for r in approved:
        c = r.candidate
        consensus = r.checks.get("trader_consensus", {})
        consensus_str = f"  traders={consensus['signal']}({consensus['same']}vs{consensus['opp']})" if consensus else ""
        print(
            f"  APPROVED  {c['city_display']:<18} {r.direction.upper():<4} "
            f"edge={c['edge_pct']:.0%}  score={r.score:.3f}  size=${r.size_usdc:.2f}"
            f"{consensus_str}"
        )

    if rejected:
        print()
        for r in rejected:
            c = r.candidate
            print(
                f"  rejected  {c['city_display']:<18} {r.direction.upper():<4} "
                f"edge={c['edge_pct']:.0%}  -- {r.reason}"
            )
    print()
