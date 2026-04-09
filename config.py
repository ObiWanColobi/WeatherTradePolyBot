import os
from dotenv import load_dotenv

load_dotenv()

# ── Paper Trading ─────────────────────────────────────────────────────────────
PAPER_STARTING_BALANCE = 2000.00   # USDC

# ── Market Filters (used by Polymarket API fetcher) ───────────────────────────
MIN_LIQUIDITY_USDC = 1000
MAX_HOURS_TO_CLOSE = 120   # 5 days — weather markets run days out
MIN_HOURS_TO_CLOSE = 1

# ── Weather Layer ─────────────────────────────────────────────────────────────
WEATHER = {
    "min_edge":                  0.05,  # minimum probability gap used by the bot decision layer

    # ── Scanner display filters ───────────────────────────────────────────────
    "scanner_min_yes":           0.05,  # hide markets with YES price below this (crowd too certain NO)
    "scanner_max_yes":           0.95,  # hide markets with YES price above this (crowd too certain YES)
    "scanner_min_edge_pct":      0.05,  # minimum model vs market gap to show (5% = 0.05)
    "scanner_min_volume":        5000,  # minimum 24h volume for unknown cities
    "scanner_min_volume_tier1":  1000,  # minimum 24h volume for known high-volume cities

    # Top cities by historical volume — lower volume floor applied to these.
    # Temporary seed list; replaced by catalog data once enough days are logged.
    "top_cities": [
        "paris", "london", "new york city", "dallas", "istanbul",
        "hong kong", "toronto", "seoul", "buenos aires", "chicago",
        "miami", "moscow", "madrid", "munich", "wellington",
        "atlanta", "sao paulo", "tel aviv", "lucknow", "shanghai",
    ],

    # ── Bot loop ──────────────────────────────────────────────────────────────
    "bot_poll_interval_seconds": 60,    # seconds between polls
    "bot_cache_refresh_polls":   10,    # refresh ensemble cache every N polls

    # Scanner cache — used by weather_scanner.py for discovery/display only
    "cache_ttl_minutes":         90,   # scan-only cities (no open position)
    "position_cache_ttl_minutes": 25,  # cities with an open trade — prioritise freshness

    # Decision layer — top N markets the bot will actually evaluate for trades.
    # All others are ignored by estimate() to avoid wasting API calls.
    # Candidates are ranked by 24h volume and refreshed on each WeatherLayer.refresh() call.
    "top_n_markets":             30,

    # ── Exit conditions ───────────────────────────────────────────────────────
    "exit_ensemble_flip_threshold":  0.25,  # exit if ensemble shifts >25pts from entry
    "exit_adverse_price_move_pct":  0.30,  # exit if price moves >30% of fill against position
    "exit_adverse_min_move_cents":  0.10,  # floor: never exit on moves smaller than 10 cents (prevents noise exits on cheap tokens)
    "exit_adverse_min_hold_minutes": 60,   # no adverse exit within first 60 min (post-entry price settling)
    "exit_adverse_skip_unanimous_pct": 0.90,  # skip adverse exit when ensemble conviction >= 90% (trust the model)
    "exit_max_spread_cents":        0.22,  # exit if spread widens past $0.22 (only checked within final 12h)
    "exit_no_exit_hours_to_close":  2.0,   # never exit within 2h of resolution

    # ── Kelly position sizing ─────────────────────────────────────────────────
    "kelly_fraction":        0.50,   # fractional Kelly multiplier (0.5 = half-Kelly)
    "kelly_max_bet_usdc":   200.00,   # hard cap per trade in USDC
    "kelly_min_bet_usdc":    5.00,   # minimum bet size (below this = skip)
    "kelly_max_balance_pct": 0.10,   # never risk more than 10% of balance per trade

    # ── Entry conditions ──────────────────────────────────────────────────────
    "entry_min_ensemble_conviction": 0.70,  # ensemble must be >=70% or <=30% YES
    "entry_min_edge_pct":            0.12,  # model vs market gap (tighter than scanner)
    "entry_min_fill_price":          0.15,  # block entries where the token costs < $0.15 (avoids ultra-cheap tokens where noise dominates price action)
    "entry_min_fill_price_yes":      0.25,  # higher floor for YES tokens — cheap YES bets get killed by adverse exits
    "entry_max_spread_cents":        0.08,  # max bid/ask spread ($0.08)
    "entry_min_hours_to_close":      2.0,   # must have >=2h before resolution
    "entry_min_ensemble_margin_c":       3.0,   # ensemble mean must be >=3°C from threshold at minimum conviction (raised from 2.0 on 2026-04-07)
    "entry_min_ensemble_margin_c_floor": 1.5,   # margin floor for unanimous ensembles (0/69 or 69/69); scales linearly up to entry_min_ensemble_margin_c at min conviction
    "entry_max_slippage_pct":        0.05,  # max simulated fill slippage as % of mid (5%)

    # ── Decision layer ────────────────────────────────────────────────────────
    "decision_max_open_positions":        20,   # max concurrent open trades (increase freely)
    "decision_max_exposure_pct":          0.9,   # max total balance % at risk across all open trades
    "decision_max_positions_per_city_date": 2,  # max positions per (city, resolution-date) — allows different thresholds, caps concentration

    # ── Trader ensemble (social signal) ──────────────────────────────────────
    # Secondary ensemble from tracked Polymarket traders. Informational only —
    # never blocks a trade. Used to log CONFIRM/DIVERGE/NEUTRAL alongside each entry.
    "trader_monitor_poll_every_n":     10,    # run trader position poll every N bot polls (~10 min)
    "trader_monitor_max_wallets":     100,    # cap wallets polled per update pass (top by n_resolved)
    "trader_consensus_min_resolved":    6,    # min resolved trades before a wallet counts in consensus
    "trader_consensus_min_win_rate":   0.55,  # min win rate to count toward consensus signal
    "trader_consensus_confirm_count":   2,    # min same-direction traders to trigger CONFIRM signal
    "trader_discovery_min_trades":      5,    # min unique weather markets to qualify as a tracked trader
    "trader_discovery_max_markets":   400,    # max unique markets — above this = AMM/market maker
    "trader_discovery_max_tpm":        5.0,   # max avg trades-per-market — above this = HFT bot
    "trader_discovery_max_avg_price":  0.85,  # max avg entry price — above this = certainty harvester
    "trader_discovery_recency_days":   60,    # wallet must have traded within last N days

    # How fresh the forecast must be when making a trade decision.
    # 180 min = 3 hours, aligned to ICON ensemble update cadence.
    # estimate() will force-fetch if the cached data is older than this.
    "decision_cache_ttl_minutes": 180,

    # ── Trader shadow forecast DB (Stage 2) ───────────────────────────────────
    "trader_forecast_freeze_max_per_cycle": 50,         # cap untraded-market CLOB checks per resolution pass
    "calibration_temp_pass_max_cities": 20,             # cap cities fetched per temperature pass (archive API burst control)
    "calibration_temp_pass_cooldown_after_429_minutes": 10,  # skip temp pass if any Open-Meteo 429 seen within N min
    "calibration_temp_pass_skip_if_ensemble_within_seconds": 60,  # skip temp pass if ensemble burst fired recently

    # ── Risk management ──────────────────────────────────────────────────────
    "risk_daily_loss_limit_pct":   0.15,    # 15% of account value — loose for paper, tighten for live
    "risk_auto_reset":             True,    # True = paper (midnight UTC reset), False = live (restart or UI override to clear)
    "risk_email_enabled":          False,   # opt-in email alerts
    "risk_email_smtp_host":        "",      # e.g. "smtp.gmail.com"
    "risk_email_smtp_port":        587,
    "risk_email_from":             "",
    "risk_email_to":               "",
    "risk_email_password":         "",      # app password, not account password

    # ── Extended positions (scale-in) ─────────────────────────────────────────
    "extended_positions_enabled":          True,    # master toggle — False skips pass entirely
    "extended_positions_max_add_ons":      2,       # max add-on legs (3 total with initial entry)
    "extended_positions_leg_spacing_hours": 12.0,   # time-to-close band spacing per leg
    "extended_positions_min_cooldown_hours": 12.0,  # minimum hours between any two legs
    "extended_positions_rejection_cooldown_hours": 1.0,  # cooldown after a rejected extend attempt (slippage/thin book)
    "extended_positions_min_net_edge":     0.03,  # minimum edge after slippage cost to bother extending (3%)
}

# ── API Endpoints ─────────────────────────────────────────────────────────────
POLYMARKET_GAMMA_API    = "https://gamma-api.polymarket.com"
POLYMARKET_CLOB_API     = "https://clob.polymarket.com"
POLYMARKET_DATA_API     = "https://data-api.polymarket.com"
OPEN_METEO_FORECAST_API = "https://api.open-meteo.com/v1/forecast"
OPEN_METEO_ENSEMBLE_API = "https://ensemble-api.open-meteo.com/v1/ensemble"
OPEN_METEO_ARCHIVE_API  = "https://archive-api.open-meteo.com/v1/archive"

# ── Database ──────────────────────────────────────────────────────────────────
DB_PATH = "weather_bot.db"
