import os
from dotenv import load_dotenv

load_dotenv()

# ── Trading Mode ─────────────────────────────────────────────────────────────
# "paper" = simulated fills using live order books (default)
# "live"  = real orders via py-clob-client (requires WALLET_PRIVATE_KEY in .env)
TRADING_MODE = os.getenv("TRADING_MODE", "paper")

# ── Live Trading Wallet ──────────────────────────────────────────────────────
WALLET_PRIVATE_KEY = os.getenv("WALLET_PRIVATE_KEY", "")
# 0 = EOA (MetaMask/hardware), 1 = POLY_PROXY (Magic Link), 2 = GNOSIS_SAFE
WALLET_SIGNATURE_TYPE = int(os.getenv("WALLET_SIGNATURE_TYPE", "0"))
# Only needed for POLY_PROXY or GNOSIS_SAFE signature types
WALLET_FUNDER_ADDRESS = os.getenv("WALLET_FUNDER_ADDRESS", "")

# ── Polymarket CLOB V2 (cutover 2026-04-28) ──────────────────────────────────
# Settlement collateral migrated USDC.e → pUSD; same proxy address.
# Verify against https://docs.polymarket.com/resources/contracts before trusting.
POLY_PUSD_ADDRESS         = "0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB"
POLY_USDC_E_ADDRESS       = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
POLY_COLLATERAL_ONRAMP    = "0x93070a847efEf7F70739046A929D47a521F5B8ee"
POLY_EXCHANGE_V2          = "0xE111180000d2663C0091e4f400237545B87B996B"
POLY_NEG_RISK_EXCHANGE_V2 = "0xe2222d279d744050d28e00520010520000310F59"

# ── Paper Trading ─────────────────────────────────────────────────────────────
PAPER_STARTING_BALANCE = 2000.00   # USDC
LIVE_STARTING_BALANCE  = 4000.00    # USDC — actual funded amount when live trading began

# ── Market Filters (used by Polymarket API fetcher) ───────────────────────────
MIN_LIQUIDITY_USDC = 1000
MAX_HOURS_TO_CLOSE = 120   # 5 days — weather markets run days out
MIN_HOURS_TO_CLOSE = 1

# ── Weather Layer ─────────────────────────────────────────────────────────────
WEATHER = {

    # ╔═══════════════════════════════════════════════════════════════════════════╗
    # ║  SCANNER & MARKET FILTERS                                               ║
    # ║  Controls which markets appear in the scanner and qualify for evaluation ║
    # ╚═══════════════════════════════════════════════════════════════════════════╝
    "min_edge":                  0.05,  # minimum probability gap used by the bot decision layer
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
        # E12-01 additions (2026-05-08): full-stake unlock.
        "taipei", "ankara", "tokyo", "seattle",
    ],

    # Polymarket slug overrides (non-default city → slug mappings).
    # Default is city.lower().replace(" ", "-"); only list exceptions here.
    # Resolved 2026-05-18 by scripts/verify_city_slugs.py.
    "city_slugs": {
        "new york city": "nyc",
    },

    # Catalog discovery universe — every city Polymarket lists temperature
    # markets for, verified 2026-05-18 by scripts/verify_city_slugs.py.
    # Used by weather_catalog to snapshot the full universe; scanner stays
    # on top_cities. Re-run the verifier periodically to detect new cities.
    "catalog_cities": [
        "amsterdam", "ankara", "atlanta", "austin", "beijing",
        "buenos aires", "chengdu", "chicago", "dallas", "denver",
        "helsinki", "hong kong", "houston", "istanbul", "jakarta",
        "karachi", "london", "los angeles", "lucknow", "madrid",
        "mexico city", "miami", "milan", "moscow", "munich",
        "new york city", "paris", "san francisco", "sao paulo",
        "seattle", "seoul", "shanghai", "shenzhen", "singapore",
        "taipei", "tel aviv", "tokyo", "toronto", "warsaw",
        "wellington", "wuhan",
    ],

    # Top N markets the bot will actually evaluate for trades.
    # All others are ignored by estimate() to avoid wasting API calls.
    # Candidates are ranked by 24h volume and refreshed on each WeatherLayer.refresh() call.
    "top_n_markets":             100,

    # ╔═══════════════════════════════════════════════════════════════════════════╗
    # ║  BOT LOOP & CACHING                                                     ║
    # ║  Poll timing, cache freshness, and forecast staleness                    ║
    # ╚═══════════════════════════════════════════════════════════════════════════╝
    "bot_poll_interval_seconds": 60,    # seconds between polls
    "bot_cache_refresh_polls":   10,    # refresh ensemble cache every N polls
    "cache_ttl_minutes":         90,    # scan-only cities (no open position)
    "position_cache_ttl_minutes": 25,   # cities with an open trade — prioritise freshness

    # How fresh the forecast must be when making a trade decision.
    # 180 min = 3 hours, aligned to ICON ensemble update cadence.
    # estimate() will force-fetch if the cached data is older than this.
    "decision_cache_ttl_minutes": 180,

    # ╔═══════════════════════════════════════════════════════════════════════════╗
    # ║  ENTRY CONDITIONS                                                       ║
    # ║  Gates a market must pass before the bot will enter a position           ║
    # ╚═══════════════════════════════════════════════════════════════════════════╝
    "entry_min_ensemble_conviction": 0.85,  # ensemble must be >=85% or <=15% YES (raised from 0.70 on 2026-04-15 — trade review showed 43% WR in 70–85% tier)
    "entry_min_edge_pct":            0.12,  # model vs market gap (tighter than scanner)
    "entry_min_fill_price":          0.15,  # block entries where the token costs < $0.15 (avoids ultra-cheap tokens where noise dominates price action)
    "entry_min_fill_price_yes":      0.25,  # higher floor for YES tokens — cheap YES bets get killed by adverse exits
    "entry_max_spread_cents":        0.08,  # max bid/ask spread ($0.08)
    "entry_min_hours_to_close":      2.0,   # must have >=2h before resolution
    "entry_min_ensemble_margin_c":       2.0,   # ensemble mean must be >=2°C from threshold at minimum conviction (lowered from 3.0 on 2026-05-02 — 32-trade review showed 0 losses from <1°C-margin entries; 4-day dry spell from blocking high-conviction Hong Kong candidates by 0.12°C)
    "entry_min_ensemble_margin_c_floor": 1.0,   # margin floor for unanimous ensembles (0/69 or 69/69); scales linearly up to entry_min_ensemble_margin_c at min conviction (lowered from 1.5 on 2026-05-02 alongside the ceiling)
    "entry_max_slippage_pct":        0.03,  # max simulated fill slippage as % of mid (5%)
    "entry_min_net_edge_pct":        0.06,  # minimum edge remaining after slippage, any tier (5%)

    # ── Unanimous weak-edge entry ─────────────────────────────────────────────
    # When ensemble conviction is >= this threshold (~67/69 members), the edge
    # floor is relaxed from entry_min_edge_pct to entry_unanimous_min_edge_pct.
    # 7% floor accounts for ~2% Polymarket taker fee + ~2-3% slippage cushion.
    "entry_unanimous_min_conviction":  0.97,   # ≥97% of ensemble members (~67/69)
    "entry_unanimous_min_edge_pct":    0.07,   # relaxed floor when unanimous (vs 12% normal)

    # ── Per-city strict overrides (2026-04-15) ────────────────────────────────
    # Cities with weak ensemble reliability or frequent coastal temp swings
    # require near-unanimous conviction and a wider margin than the default.
    # Research TODO: build a data-driven reliability score after 100+ resolved trades.
    "entry_city_adjustment_enabled": True,
    "entry_city_strict_cities": [
        "chicago", "dallas", "atlanta", "toronto",   # US inland — weak ensemble reliability
        "london", "wellington",                       # coastal — temp swings
    ],
    "entry_city_strict_min_conviction": 0.90,  # require near-unanimous for these cities (vs 0.85 default)
    "entry_city_strict_min_margin_c":   4.0,   # require wider margin for these cities (vs 3.0 default)

    # ── Tampering-defense blocklist (2026-05-11) ─────────────────────────────
    # Cities removed from the tradeable universe due to confirmed/suspected
    # resolution-source tampering. METAR shadow capture continues unchanged
    # (CITY_RESOLVERS in markets/metar_observer.py is untouched).
    # See tasks/plans/2026-05-11_tampering_defense_tier_system.md for the
    # generalized tier system that will eventually replace this shim.
    "entry_blocked_cities": [
        "tokyo",   # 2026-05-10 RJTT 25.0°C single-spike + dewpoint-unchanged signature; lost NO #51 -$75.83
    ],

    # ╔═══════════════════════════════════════════════════════════════════════════╗
    # ║  EXIT CONDITIONS                                                        ║
    # ║  When to close an open position before market resolution                ║
    # ╚═══════════════════════════════════════════════════════════════════════════╝
    "exit_ensemble_flip_threshold":     0.25,  # exit if ensemble shifts >25pts from entry
    "exit_adverse_price_move_pct":      0.30,  # exit if price moves >30% of fill against position
    "exit_adverse_min_move_cents":      0.10,  # floor: never exit on moves smaller than 10 cents (prevents noise exits on cheap tokens)
    "exit_adverse_min_hold_minutes":    60,    # no adverse exit within first 60 min (post-entry price settling)
    # NOTE: prior "exit_adverse_skip_unanimous_pct" bypass removed 2026-05-15 after backfill showed
    # it doubled loss magnitude on unanimous-but-wrong trades; see tasks/plans/2026-05-15_remove_unanimous_bypass.md

    # ── Late-game market divergence exit (2026-04-15) ─────────────────────────
    # In the final hours before close, if our token price has collapsed but the
    # ensemble still shows high conviction for us, the ensemble is stale and the
    # market is already pricing the true outcome — exit.
    "exit_late_game_hours":              8.0,   # window: last 8h before close
    "exit_late_game_market_floor":       0.40,  # exit if our token drops below this
    "exit_late_game_ensemble_threshold": 0.70,  # ...AND ensemble still shows >= this conviction for us

    # ── Resilient exit execution (2026-04-15) ────────────────────────────────
    "exit_reprice_min_step":  0.01,   # minimum price drop per reprice cycle (GTC exit orders)

    # ╔═══════════════════════════════════════════════════════════════════════════╗
    # ║  KELLY POSITION SIZING                    [paper defaults, see Live      ║
    # ║  Half-Kelly with hard caps                 Overrides section for live]   ║
    # ╚═══════════════════════════════════════════════════════════════════════════╝
    "kelly_fraction":                0.50,    # fractional Kelly multiplier (0.5 = half-Kelly)
    "kelly_max_bet_usdc":           15.00,   # PAPER: hard cap per trade in USDC
    "kelly_min_bet_usdc":             1.00,   # minimum bet size (below this = skip) — 2026-05-12 dropped 5→1 for near-dry-run shadow window
    "kelly_max_balance_pct":          0.02,   # never risk more than 10% of balance per trade
    "kelly_max_bet_usdc_unanimous":  50.00,   # PAPER: separate hard cap for unanimous-weak trades

    # ╔═══════════════════════════════════════════════════════════════════════════╗
    # ║  DECISION LAYER / PORTFOLIO LIMITS                                      ║
    # ║  Concentration and exposure caps across all open positions               ║
    # ╚═══════════════════════════════════════════════════════════════════════════╝
    "decision_max_open_positions":          15,   # max concurrent open trades
    "decision_max_exposure_pct":            .25,  # max total balance % at risk across all open trades
    "decision_max_positions_per_city_date":  1,   # max positions per (city, resolution-date) — allows different thresholds, caps concentration

    # ╔═══════════════════════════════════════════════════════════════════════════╗
    # ║  EXTENDED POSITIONS (SCALE-IN)                                          ║
    # ║  Adding to winning positions over time                                  ║
    # ╚═══════════════════════════════════════════════════════════════════════════╝
    "extended_positions_enabled":                True,   # master toggle — False skips pass entirely
    "extended_positions_max_add_ons":            1,      # max add-on legs (3 total with initial entry)
    "extended_positions_leg_spacing_hours":      12.0,   # time-to-close band spacing per leg
    "extended_positions_min_cooldown_hours":     12.0,   # minimum hours between any two legs
    "extended_positions_rejection_cooldown_hours": 1.0,  # cooldown after a rejected extend attempt (slippage/thin book)
    "extended_positions_min_net_edge":           0.06,   # minimum edge after slippage cost to bother extending (5%)

    # ╔═══════════════════════════════════════════════════════════════════════════╗
    # ║  RISK MANAGEMENT                          [paper defaults, see Live     ║
    # ║  Daily loss limits and breaker behavior    Overrides section for live]   ║
    # ╚═══════════════════════════════════════════════════════════════════════════╝
    "risk_daily_loss_limit_pct":   0.25,    # PAPER: 15% of account value
    "risk_auto_reset":             True,    # PAPER: midnight UTC auto-reset; live overrides to False

    # ╔═══════════════════════════════════════════════════════════════════════════╗
    # ║  ★ LIVE TRADING OVERRIDES ★               (TRADING_MODE == "live" ONLY) ║
    # ║  These REPLACE the paper defaults above when running with real money.   ║
    # ║  More conservative caps to limit downside while validating the system.  ║
    # ╚═══════════════════════════════════════════════════════════════════════════╝
    "live_kelly_max_bet_usdc":            2.00,   # 2026-05-12: dropped 50→2 — near-dry-run shadow window while Phase 2 data accumulates (gate 2026-06-07)
    "live_kelly_max_bet_usdc_unanimous":  2.00,   # 2026-05-12: dropped 75→2 — same reason
    "live_risk_daily_loss_limit_pct":     0.10,   # overrides risk_daily_loss_limit_pct (same for now, tighten as needed)
    "live_risk_auto_reset":              True,   # overrides risk_auto_reset — (False = manual override only, no midnight reset)

    # ╔═══════════════════════════════════════════════════════════════════════════╗
    # ║  ★ ON-CHAIN CLAIMS ★                      (TRADING_MODE == "live" ONLY) ║
    # ║  Polygon transaction settings for redeeming winning positions           ║
    # ╚═══════════════════════════════════════════════════════════════════════════╝
    "claim_retry_backoff_minutes":  [5, 30, 120, 480, 1440],  # 5min, 30min, 2hr, 8hr, 24hr
    "claim_min_matic_balance":      0.05,                       # defer claims if MATIC below this
    "polygon_rpc_url":              os.getenv("POLYGON_RPC_URL", "https://rpc.ankr.com/polygon"),

    # ╔═══════════════════════════════════════════════════════════════════════════╗
    # ║  TRADER ENSEMBLE (SOCIAL SIGNAL)                                        ║
    # ║  Secondary signal from tracked Polymarket wallets. Informational only — ║
    # ║  never blocks a trade. Logs CONFIRM/DIVERGE/NEUTRAL alongside entries.  ║
    # ╚═══════════════════════════════════════════════════════════════════════════╝
    "trader_monitor_poll_every_n":     10,    # run trader position poll every N bot polls (~10 min)
    "trader_monitor_max_wallets":     100,    # cap wallets polled per update pass (top by n_resolved)
    "trader_consensus_min_resolved":    6,    # min resolved trades before a wallet counts in consensus
    "trader_consensus_min_win_rate":   0.55,  # min win rate to count toward consensus signal
    "trader_consensus_confirm_count":   2,    # min same-direction traders to trigger CONFIRM signal

    # ── Trader discovery ──────────────────────────────────────────────────────
    "trader_discovery_min_trades":      5,    # min unique weather markets to qualify as a tracked trader
    "trader_discovery_max_markets":   400,    # max unique markets — above this = AMM/market maker
    "trader_discovery_max_tpm":        5.0,   # max avg trades-per-market — above this = HFT bot
    "trader_discovery_max_avg_price":  0.85,  # max avg entry price — above this = certainty harvester
    "trader_discovery_recency_days":   60,    # wallet must have traded within last N days

    # ╔═══════════════════════════════════════════════════════════════════════════╗
    # ║  CALIBRATION SCHEDULER                                                  ║
    # ║  Resolution checks, temperature archive fetches, rate-limit guards      ║
    # ╚═══════════════════════════════════════════════════════════════════════════╝
    "trader_forecast_freeze_max_per_cycle":          50,   # cap untraded-market CLOB checks per resolution pass
    "calibration_temp_pass_max_cities":              20,   # cap cities fetched per temperature pass (archive API burst control)
    "calibration_temp_pass_cooldown_after_429_minutes": 10,  # skip temp pass if any Open-Meteo 429 seen within N min
    "calibration_temp_pass_skip_if_ensemble_within_seconds": 60,  # skip temp pass if ensemble burst fired recently

    # ╔═══════════════════════════════════════════════════════════════════════════╗
    # ║  NOTIFICATIONS                                                          ║
    # ║  Discord webhooks and optional email alerts                             ║
    # ╚═══════════════════════════════════════════════════════════════════════════╝
    # Discord — leave empty string to disable
    "discord_webhook_alerts":       os.getenv("DISCORD_WEBHOOK_ALERTS", ""),
    "discord_webhook_trades":       os.getenv("DISCORD_WEBHOOK_TRADES", ""),

    # Email — set risk_email_enabled to True to activate
    "risk_email_enabled":          False,
    "risk_email_smtp_host":        "",      # e.g. "smtp.gmail.com"
    "risk_email_smtp_port":        587,
    "risk_email_from":             "",
    "risk_email_to":               "",
    "risk_email_password":         "",      # app password, not account password

    # ╔═══════════════════════════════════════════════════════════════════════════╗
    # ║  API RELIABILITY                                                        ║
    # ║  Circuit breaker, rate limiting, and health monitoring                  ║
    # ╚═══════════════════════════════════════════════════════════════════════════╝
    "api_breaker_trip_threshold":    3,                           # consecutive failures before trip
    "api_breaker_cooldowns":        [120, 300, 600, 1800, 3600], # escalating seconds
    "clob_inter_request_delay":     0.3,                         # min seconds between CLOB calls
    "heartbeat_stale_threshold":    300,                         # seconds before crash detection fires
    "dashboard_bot_down_threshold": 300,                         # seconds before dashboard shows offline

    # ╔═══════════════════════════════════════════════════════════════════════════╗
    # ║  METAR / OBSERVED-RESOLUTION FRONT-RUNNING  (Phase 1)                  ║
    # ║  All flags default OFF — merge is a zero-behavior-change deploy.        ║
    # ║  Rollout: METAR_ENABLED=True (shadow) → METAR_EXIT_ON_LOCK=True (live)  ║
    # ╚═══════════════════════════════════════════════════════════════════════════╝
    "metar_enabled":               os.getenv("METAR_ENABLED",        "false").lower() == "true",
    "metar_record_to_db":          os.getenv("METAR_RECORD_TO_DB",   "true").lower()  == "true",
    "metar_exit_on_lock":          os.getenv("METAR_EXIT_ON_LOCK",   "false").lower() == "true",
    "metar_poll_interval_sec":     60,
    "metar_stale_ttl_sec":         300,
    "metar_plausibility_delta_c":  5.0,
    "metar_avwx_url":              "https://aviationweather.gov/api/data/metar",
    "metar_hko_url":               "https://data.weather.gov.hk/weatherAPI/opendata/weather.php",
    "metar_user_agent":            "tradebot0/1.0 (colby.pearson55@gmail.com)",

    # Live override — defaults identical to paper for Phase 1; tune post-shadow
    "live_metar_exit_on_lock":     os.getenv("LIVE_METAR_EXIT_ON_LOCK", "false").lower() == "true",
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


# ── Live-Mode Override Application ────────────────────────────────────────────
# Maps paper key → live override key. Any process that needs the resolved
# (mode-aware) values should call apply_live_overrides() once at startup.
_LIVE_OVERRIDES = {
    "kelly_max_bet_usdc":           "live_kelly_max_bet_usdc",
    "kelly_max_bet_usdc_unanimous": "live_kelly_max_bet_usdc_unanimous",
    "risk_daily_loss_limit_pct":    "live_risk_daily_loss_limit_pct",
    "risk_auto_reset":              "live_risk_auto_reset",
    "metar_exit_on_lock":           "live_metar_exit_on_lock",
}

_overrides_applied = False

def apply_live_overrides() -> bool:
    """Mutate WEATHER in place with live_* overrides when TRADING_MODE == 'live'.

    Idempotent — safe to call from multiple modules. Returns True if applied.
    """
    global _overrides_applied
    if _overrides_applied or TRADING_MODE != "live":
        _overrides_applied = True
        return False
    for paper_key, live_key in _LIVE_OVERRIDES.items():
        if live_key in WEATHER:
            WEATHER[paper_key] = WEATHER[live_key]
    _overrides_applied = True
    return True
