"""Fresh minimal v1 config for the shotgun paper bot.

The 330-line threshold-era config was archived to
`_archive/threshold_era/config.py` (reference only). This v1 keeps ONLY what
the shotgun strategy and the surviving "kept" modules (markets/, db.py,
notifications.py, executor/, snapshot_*, weather_resolver, weather_catalog,
calibration, trader_monitor, metar_observer, api_monitor, …) actually import.

Grow this file ADDITIVELY in v2. Do NOT resurrect the old WEATHER ladder —
`WEATHER` here is an intentionally empty dict that every kept module reads
through `.get(key, default)`, so the defaults baked into those modules win.
"""

import os
from dotenv import load_dotenv

load_dotenv()

# ── Trading mode ─────────────────────────────────────────────────────────────
TRADING_MODE = os.getenv("TRADING_MODE", "paper")   # "paper" | "live"
PAPER_STARTING_BALANCE = 2000.00

# ── Live wallet (carried forward; unused in v1 paper) ────────────────────────
WALLET_PRIVATE_KEY    = os.getenv("WALLET_PRIVATE_KEY", "")
WALLET_SIGNATURE_TYPE = int(os.getenv("WALLET_SIGNATURE_TYPE", "0"))
WALLET_FUNDER_ADDRESS = os.getenv("WALLET_FUNDER_ADDRESS", "")

# ── Polymarket CLOB V2 addresses (carried forward) ───────────────────────────
POLY_PUSD_ADDRESS         = "0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB"
POLY_EXCHANGE_V2          = "0xE111180000d2663C0091e4f400237545B87B996B"
POLY_NEG_RISK_EXCHANGE_V2 = "0xe2222d279d744050d28e00520010520000310F59"

# ── API endpoints ────────────────────────────────────────────────────────────
POLYMARKET_GAMMA_API    = "https://gamma-api.polymarket.com"
POLYMARKET_CLOB_API     = "https://clob.polymarket.com"
POLYMARKET_DATA_API     = "https://data-api.polymarket.com"
OPEN_METEO_FORECAST_API = "https://api.open-meteo.com/v1/forecast"
OPEN_METEO_ENSEMBLE_API = "https://ensemble-api.open-meteo.com/v1/ensemble"
OPEN_METEO_ARCHIVE_API  = "https://archive-api.open-meteo.com/v1/archive"

# ── Market filters (read by markets/polymarket.py discovery) ─────────────────
MIN_LIQUIDITY_USDC = 1000
MAX_HOURS_TO_CLOSE = 120   # 5 days — weather markets run days out
MIN_HOURS_TO_CLOSE = 1

# ── Database ──────────────────────────────────────────────────────────────────
# Test/CI isolation hook — when DB_PATH_OVERRIDE is set, point the DB at the
# override path so `importlib.reload(config); importlib.reload(db)` picks up a
# throwaway file instead of clobbering the production DB. No-op otherwise.
DB_PATH = os.environ.get("DB_PATH_OVERRIDE", "weather_bot.db")

# Shadow snapshot logger (VPS) writes to a SEPARATE file so its nightly VACUUM
# (whole-DB exclusive lock + ~DB-size scratch space, on a 2GB/day file) can never
# lock or risk the paper-trading DB. Local dev/tests never run the logger, so this
# default is harmless off-VPS. Override on the VPS via SNAPSHOT_DB_PATH env.
SNAPSHOT_DB_PATH = os.environ.get("SNAPSHOT_DB_PATH", "snapshots.db")

# ── Legacy compatibility shim ────────────────────────────────────────────────
# Many kept modules still `from config import WEATHER` and read tunables via
# WEATHER.get("key", default). The shotgun strategy no longer uses the old
# threshold-era ladder, so this is an empty dict on purpose: every reader's
# baked-in default applies. Add real keys here only if v2 needs to override a
# kept module's default — do NOT copy the archived WEATHER dict back wholesale.
WEATHER: dict = {}

# ── Shotgun strategy (v1) ────────────────────────────────────────────────────
SHOTGUN = {
    "mode":                       "dist_yes_no",
    # Default 12h (validated window). Env-overridable for testing wider windows
    # without a code edit — e.g. SHOTGUN_FIRE_WINDOW_HOURS=24 to dry-run more
    # city-days. Leave unset in production.
    "fire_window_hours":          float(os.getenv("SHOTGUN_FIRE_WINDOW_HOURS", "12.0")),
    "budget_per_city_day":        50.00,
    "edge_threshold":             0.12,
    "mass_core_frac":             0.50,
    "price_min":                  0.05,
    "price_max":                  0.50,
    "vol_min":                    200.0,
    "sizing_mode":                "weighted",
    "per_bucket_liq_cap_frac":    0.10,
    "portfolio_exposure_cap_pct": 0.80,
    # Execution-cost parity with the research harness (snapshot_pnl.simulate_fill_realistic).
    # Paper fills now charge the same Polymarket taker fee + per-fill gas the backtest did,
    # so paper P&L is directly comparable. See review H1 (2026-06-11).
    "taker_fee_rate":             0.0125,   # Polymarket weather taker fee: shares*rate*p*(1-p)
    "gas_per_fill_usd":           0.004,    # per-leg fixed cost taken off the top
    # H3: emulate a limit order in paper — reject a leg if its book-walk fill price
    # exceeds the scored price (best_ask for YES / 1-best_bid for NO) by more than this.
    # None disables the cap. See review H3 (2026-06-11).
    "max_fill_slippage_per_leg":  0.10,
    # Probability floor: the forecast density assigned to a bucket is clamped to at
    # least this before edge is computed, so a model-blind density=0 bucket can't look
    # like a free +(1-mid) NO edge. See review density=0 anomaly (tel-aviv 31°C).
    "density_floor":              0.01,
    "cities": [
        "toronto", "chicago", "denver", "dallas", "munich", "shanghai", "tel aviv",
        "istanbul", "hong kong", "moscow", "paris", "buenos aires", "beijing", "seoul",
        "london", "nyc", "taipei", "miami", "ankara", "atlanta", "tokyo", "seattle",
        "wellington",
    ],
    "discovery_days_ahead": 2,
}

# ── Loop / infra ──────────────────────────────────────────────────────────────
POLL_INTERVAL_SECONDS     = 60
HEARTBEAT_STALE_THRESHOLD = 300
DISCORD_WEBHOOK_ALERTS = os.getenv("DISCORD_WEBHOOK_ALERTS", "")
DISCORD_WEBHOOK_TRADES = os.getenv("DISCORD_WEBHOOK_TRADES", "")

# ── Shadow-data toggles (kept-running passes) ────────────────────────────────
METAR_ENABLED      = os.getenv("METAR_ENABLED", "false").lower() == "true"
METAR_RECORD_TO_DB = os.getenv("METAR_RECORD_TO_DB", "true").lower() == "true"
