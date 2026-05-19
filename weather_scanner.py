#!/usr/bin/env python3
"""
Polymarket Weather Scanner
──────────────────────────
Scans Polymarket for temperature THRESHOLD markets (>=X / <=X only),
pulls real forecasts from Open-Meteo, and ranks opportunities by
model vs market edge using a 71-member multi-model ensemble.

Filters applied:
  - Threshold markets only (>=X / <=X) — exact and range excluded
  - Future markets only by default (today's already-resolved highs hidden)
  - YES price between scanner_min_yes and scanner_max_yes
  - Edge >= scanner_min_edge_pct
  - 24h volume >= scanner_min_volume

Sorted by edge % descending — best opportunities at the top.

Usage:
    python weather_scanner.py                   # refresh every 5 minutes
    python weather_scanner.py --once            # run once and exit
    python weather_scanner.py --interval 10     # refresh every 10 minutes
    python weather_scanner.py --include-today   # also show same-day markets
"""
import argparse
import json
import time
from datetime import datetime, timezone, timedelta

from rich import box
from rich.console import Console
from rich.text import Text
from rich.table import Table

from layers.layer3_weather import WeatherLayer
from markets.polymarket import iter_weather_markets
from config import WEATHER

console = Console(legacy_windows=False)
_layer  = WeatherLayer()

# ── Scanner filter thresholds (from config, with safe defaults) ───────────────
_MIN_YES        = WEATHER.get("scanner_min_yes",           0.05)
_MAX_YES        = WEATHER.get("scanner_max_yes",           0.95)
_MIN_EDGE_PCT   = WEATHER.get("scanner_min_edge_pct",      0.05)
_MIN_VOLUME     = WEATHER.get("scanner_min_volume",        5000)
_MIN_VOLUME_T1  = WEATHER.get("scanner_min_volume_tier1",  1000)
_TOP_CITIES     = set(WEATHER.get("top_cities", []))
_MIN_HOURS_TO_CLOSE = WEATHER.get("entry_min_hours_to_close", 2.0)


# ── Market fetching ───────────────────────────────────────────────────────────

def _parse_json_field(value) -> list:
    if isinstance(value, list):
        return value
    try:
        return json.loads(value)
    except Exception:
        return []


def fetch_weather_markets(pages: int = 25) -> list[dict]:
    """
    Fetch threshold weather markets for the configured top_cities via
    deterministic slug discovery (`/events/slug/{slug}`).

    `pages` is preserved for API stability with the prior paginate-based
    implementation but no longer applies — discovery now reads one event
    per (city × forward-day × kind) probe.

    Sorted by volume24hr descending so top markets surface early.
    """
    cities = WEATHER.get("top_cities", [])
    markets = []

    for m in iter_weather_markets(cities, days_ahead=2):
        # Only threshold markets — the parent-event slug always starts with
        # `highest-temperature-in-` or `lowest-temperature-in-`.
        slug_check = (m.get("slug") or m.get("_event_slug") or "").lower()
        if "highest-temperature" not in slug_check and "lowest-temperature" not in slug_check:
            continue

        outcomes  = _parse_json_field(m.get("outcomes",      "[]"))
        prices    = _parse_json_field(m.get("outcomePrices", "[]"))
        token_ids = _parse_json_field(m.get("clobTokenIds",  "[]"))

        yes_idx = next(
            (i for i, o in enumerate(outcomes) if str(o).lower() == "yes"), None
        )
        no_idx = next(
            (i for i, o in enumerate(outcomes) if str(o).lower() == "no"), None
        )
        if yes_idx is None or yes_idx >= len(prices):
            continue

        try:
            price = float(prices[yes_idx])
        except (TypeError, ValueError):
            continue
        if not (0 < price < 1):
            continue

        event_slug = m.get("_event_slug") or ""
        markets.append({
            "id":           m.get("conditionId") or m.get("id"),
            "question":     m.get("question", ""),
            "token_id":     token_ids[yes_idx] if yes_idx < len(token_ids) else None,
            "no_token_id":  token_ids[no_idx]  if no_idx  is not None and no_idx  < len(token_ids) else None,
            "price":        price,
            "liquidity":    float(m.get("liquidityNum") or m.get("liquidity") or 0),
            "volume":       float(m.get("volume24hr") or m.get("volumeNum") or 0),
            "end_date":     m.get("_event_end") or m.get("endDateIso") or m.get("endDate") or "",
            "market_url":   f"https://polymarket.com/event/{event_slug}" if event_slug else "",
        })

    markets.sort(key=lambda x: x.get("volume") or 0, reverse=True)
    return markets


# ── Display helpers ───────────────────────────────────────────────────────────

def _resolves_str(end_date_str: str) -> tuple[str, bool, int]:
    """
    Returns (display_str, is_today, days_out).
    display_str : 'Today', 'Tomorrow', or 'Apr 4' etc.
    is_today    : True when the market resolves today
    days_out    : whole days until resolution (0 = today, 1 = tomorrow, ...)
    """
    today    = datetime.now(timezone.utc).date()
    tomorrow = today + timedelta(days=1)
    try:
        end_date = datetime.fromisoformat(
            end_date_str.replace("Z", "+00:00")
        ).date()
    except Exception:
        return "Unknown", False, 0

    days_out = (end_date - today).days
    days_out = max(0, days_out)

    if end_date == today:
        return "Today", True, 0
    if end_date == tomorrow:
        return "Tomorrow", False, 1
    return end_date.strftime("%b ") + str(end_date.day), False, days_out


def _direction_text(model_prob: float, market_price: float, conviction_ok: bool) -> Text:
    """BUY YES/NO — dimmed when ensemble conviction is uncertain."""
    if not conviction_ok:
        label = "BUY YES" if model_prob > market_price else "BUY NO"
        return Text(label, style="dim")
    if model_prob > market_price:
        return Text("BUY YES", style="bold green")
    return Text("BUY NO", style="bold red")


def _status_text(edge_pct: float, conviction_ok: bool) -> Text:
    """Edge label — appends '?' and dims when ensemble is in the uncertain zone."""
    if edge_pct >= 0.30:
        label = f"{edge_pct:.0%} STRONG"
        style = "bold red" if conviction_ok else "dim"
        suffix = "" if conviction_ok else "?"
        return Text(label + suffix, style=style)
    if edge_pct >= 0.15:
        label = f"{edge_pct:.0%} EDGE"
        style = "yellow bold" if conviction_ok else "dim"
        suffix = "" if conviction_ok else "?"
        return Text(label + suffix, style=style)
    return Text(f"{edge_pct:.0%} WEAK", style="dim")


def _ensemble_text(ens_yes, ens_n: int, conviction_ok: bool) -> Text:
    """Ensemble fraction — green for strong conviction, dim orange for uncertain zone."""
    if ens_yes is None or ens_n == 0:
        return Text("n/a", style="dim")
    label = f"{ens_yes}/{ens_n}"
    if conviction_ok:
        return Text(label, style="bold green")
    return Text(label + " ?", style="yellow dim")


def build_table(rows: list[dict], include_today: bool) -> Table:
    now_str  = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    scope    = "incl. today" if include_today else "future only"
    table = Table(
        box=box.SIMPLE_HEAD,
        show_header=True,
        header_style="bold white",
        title=(
            f"[bold cyan]Polymarket Weather Scanner[/bold cyan]  "
            f"[dim]{now_str}  |  threshold markets  |  {scope}[/dim]"
        ),
        title_justify="left",
    )

    table.add_column("#",        style="dim",     width=3,  justify="right")
    table.add_column("City",                       width=16, no_wrap=True)
    table.add_column("Resolves",  style="dim",    width=10, justify="center")
    table.add_column("Market",    style="white",  width=7,  justify="center")
    table.add_column("Vol 24h",   style="cyan",   width=10, justify="right")
    table.add_column("Mkt",       style="magenta",width=5,  justify="right")
    table.add_column("Model",     style="blue",   width=6,  justify="right")
    table.add_column("Edge",                      width=13, no_wrap=True)
    table.add_column("Ens (71)",  width=9,  justify="center", no_wrap=True)
    table.add_column("Signal",    width=9,  no_wrap=True)

    for r in rows:
        row_style = "dim" if not r["conviction_ok"] else ""
        table.add_row(
            str(r["rank"]),
            r["city_display"],
            r["resolves_str"],
            r["threshold_str"],
            f"${r['volume']:,.0f}",
            f"{r['market_price']:.0%}",
            f"{r['model_prob']:.0%}",
            r["edge_text"],
            r["ensemble_text"],
            r["signal"],
            style=row_style,
        )

    return table


# ── Core scan logic ───────────────────────────────────────────────────────────

def run_scan(include_today: bool = False, exclude_market_ids: set | None = None) -> list[dict]:
    console.print("[dim]Fetching Polymarket weather markets...[/dim]")
    markets = fetch_weather_markets()
    console.print(
        f"[dim]Found {len(markets)} weather markets. Applying filters + fetching forecasts...[/dim]"
    )

    candidates = []

    for market in markets:
        # ── Skip markets we already hold (avoid wasting forecast API calls) ──
        if exclude_market_ids and market.get("id") in exclude_market_ids:
            continue

        # ── YES price bounds (cheap pre-filter before API call) ──────────────
        price = market["price"]
        if not (_MIN_YES <= price <= _MAX_YES):
            continue

        # ── Resolve date + hours-to-close ────────────────────────────────────
        # Filter on hours_to_close, not UTC-date equality. end_date is a UTC
        # timestamp at city-local midnight, so a UTC-date `is_today` check
        # silently drops markets that are still hours from close right after
        # UTC rollover (e.g. Shanghai end_date 2026-05-19T12:00Z is "today"
        # at 00:01 UTC May 19 but has 12h left to trade). The entry layer
        # uses the same min_hours_to_close threshold downstream.
        resolves_str, _is_today, days_out = _resolves_str(market["end_date"])

        try:
            end_dt    = datetime.fromisoformat(market["end_date"].replace("Z", "+00:00"))
            if end_dt.tzinfo is None:
                end_dt = end_dt.replace(hour=23, minute=59, second=59, tzinfo=timezone.utc)
            hours_to_close = max(0.0, (end_dt - datetime.now(timezone.utc)).total_seconds() / 3600)
        except Exception:
            hours_to_close = days_out * 24.0

        if hours_to_close < _MIN_HOURS_TO_CLOSE and not include_today:
            continue

        # ── Forecast + ensemble ──────────────────────────────────────────────
        scan_data = _layer.scan(market)
        if scan_data is None:
            continue

        # ── Threshold markets only ───────────────────────────────────────────
        if scan_data["market_type"] != "threshold":
            continue

        # ── Volume filter (two-tier) ─────────────────────────────────────────
        # Known high-volume cities get a lower floor so fresh polls aren't
        # excluded before they build liquidity. All others use the standard floor.
        city     = scan_data.get("city", "")
        vol_floor = _MIN_VOLUME_T1 if city in _TOP_CITIES else _MIN_VOLUME
        if market["volume"] < vol_floor:
            continue

        model_prob = scan_data["probability"]
        edge_pct   = abs(model_prob - price)

        # ── Minimum edge filter ──────────────────────────────────────────────
        if edge_pct < _MIN_EDGE_PCT:
            continue

        ens_n   = scan_data["ensemble_n"]
        ens_yes = scan_data["yes_ensemble"]

        # Ensemble conviction: passes if >70% or <30% of members agree
        ens_pct       = (ens_yes / ens_n) if ens_yes is not None and ens_n >= 10 else None
        conviction_ok = ens_pct is not None and (ens_pct >= 0.70 or ens_pct <= 0.30)

        candidates.append({
            # ── Display fields ────────────────────────────────────────────────
            "city_display":      scan_data["city"].title(),
            "resolves_str":      resolves_str,
            "threshold_str":     scan_data["target_str"],
            "volume":            market["volume"],
            "market_price":      price,
            "model_prob":        model_prob,
            "edge_pct":          edge_pct,
            "edge_text":         _status_text(edge_pct, conviction_ok),
            "ensemble_text":     _ensemble_text(ens_yes, ens_n, conviction_ok),
            "conviction_ok":     conviction_ok,
            "signal":            _direction_text(model_prob, price, conviction_ok),
            # ── Decision layer fields ─────────────────────────────────────────
            "city":              scan_data["city"].lower(),
            "days_to_resolution": days_out,
            "hours_to_close":    round(hours_to_close, 1),
            "ens_pct":           ens_pct,
            "ens_n":             ens_n,
            "ens_yes":           ens_yes,
            "market_url":        market.get("market_url", ""),
            "market_id":         market.get("id", ""),
            "_market":           market,
            "_scan_data":        scan_data,
        })

    # Sort by edge % descending — best opportunity at top
    candidates.sort(key=lambda x: x["edge_pct"], reverse=True)

    # Assign rank after sort
    for i, row in enumerate(candidates):
        row["rank"] = i + 1

    return candidates


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Polymarket weather market scanner")
    parser.add_argument("--once",          action="store_true",
                        help="Run one scan and exit")
    parser.add_argument("--interval",      type=int, default=5,
                        help="Refresh interval in minutes (default: 5)")
    parser.add_argument("--include-today", action="store_true",
                        help="Include same-day markets (daily high likely already set)")
    args = parser.parse_args()

    include_today = args.include_today

    while True:
        console.clear()
        rows = run_scan(include_today=include_today)

        if rows:
            console.print(build_table(rows, include_today))
            tradeable = [r for r in rows if r["conviction_ok"]]
            uncertain = [r for r in rows if not r["conviction_ok"]]
            strong    = sum(1 for r in tradeable if r["edge_pct"] >= 0.30)
            edge      = sum(1 for r in tradeable if 0.15 <= r["edge_pct"] < 0.30)
            console.print(
                f"\n  {len(tradeable)} tradeable  |  "
                f"[bold red]{strong} strong (>=30%)[/bold red]  |  "
                f"[yellow]{edge} edge (15-30%)[/yellow]"
                + (f"  |  [dim]{len(uncertain)} skipped (uncertain ensemble)[/dim]" if uncertain else "")
                + ("" if args.once else f"  |  [dim]next refresh in {args.interval}m[/dim]")
            )
        else:
            console.print(
                "[yellow]No opportunities found matching current filters.\n"
                "Try: --include-today, or loosen filters in config.py "
                "(scanner_min_edge_pct, scanner_min_volume, scanner_min_yes/max_yes)[/yellow]"
            )

        if args.once:
            break

        time.sleep(args.interval * 60)


if __name__ == "__main__":
    main()
