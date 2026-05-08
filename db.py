import json
import sqlite3
from datetime import datetime, timezone
from config import DB_PATH, PAPER_STARTING_BALANCE

_DB_PATH = DB_PATH
_MEM_CONN: sqlite3.Connection | None = None  # shared connection for :memory: testing


def _norm_city(d: dict) -> dict:
    c = d.get("city")
    if isinstance(c, str):
        return {**d, "city": c.strip().lower()}
    return d


def get_conn() -> sqlite3.Connection:
    global _MEM_CONN
    if _DB_PATH == ":memory:":
        if _MEM_CONN is None:
            _MEM_CONN = sqlite3.connect(":memory:", check_same_thread=False)
            _MEM_CONN.row_factory = sqlite3.Row
        return _MEM_CONN
    conn = sqlite3.connect(_DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with get_conn() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS trades (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                market_id        TEXT    NOT NULL,
                market_name      TEXT    NOT NULL,
                token_id         TEXT,
                end_date         TEXT,
                direction        TEXT    NOT NULL,
                size_usdc        REAL    NOT NULL,
                shares           REAL    NOT NULL,
                entry_price      REAL    NOT NULL,
                fill_price       REAL    NOT NULL,
                current_price    REAL,
                peak_price       REAL,
                exit_price       REAL,
                opened_at        TEXT    NOT NULL,
                closed_at        TEXT,
                status           TEXT    NOT NULL DEFAULT 'open',
                pnl              REAL,
                pnl_pct          REAL,
                layers_used      TEXT,
                estimated_prob   REAL,
                bleed_rungs_hit  INTEGER DEFAULT 0,
                exit_reason      TEXT,
                edge_score       REAL
            );

            CREATE TABLE IF NOT EXISTS balance (
                id         INTEGER PRIMARY KEY,
                amount     REAL NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS balance_history (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                amount      REAL    NOT NULL,
                recorded_at TEXT    NOT NULL
            );
        """)

        # Seed balance if first run
        row = conn.execute("SELECT amount FROM balance WHERE id = 1").fetchone()
        if not row:
            now = datetime.utcnow().isoformat()
            conn.execute(
                "INSERT INTO balance (id, amount, updated_at) VALUES (1, ?, ?)",
                (PAPER_STARTING_BALANCE, now),
            )
            conn.execute(
                "INSERT INTO balance_history (amount, recorded_at) VALUES (?, ?)",
                (PAPER_STARTING_BALANCE, now),
            )

        # Scanner results cache — written by the bot, read by the dashboard
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS scanner_cache (
                id          INTEGER PRIMARY KEY,
                results_json TEXT    NOT NULL,
                cached_at   TEXT    NOT NULL
            );
        """)

        # Weather volume catalog table
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS weather_city_log (
                id                INTEGER PRIMARY KEY AUTOINCREMENT,
                logged_date       TEXT    NOT NULL,
                city              TEXT    NOT NULL,
                market_type       TEXT    NOT NULL,
                threshold         TEXT    NOT NULL,
                volume_24h        REAL    NOT NULL,
                yes_price         REAL    NOT NULL,
                model_prob        REAL,
                ensemble_pct      REAL,
                ensemble_n        INTEGER,
                resolved_yes      INTEGER,
                logged_at         TEXT    NOT NULL,
                UNIQUE(logged_date, city, threshold)
            );
        """)

        # Trader position snapshots — live positions of tracked traders in weather markets
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS trader_positions (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                wallet      TEXT    NOT NULL,
                market_id   TEXT    NOT NULL,
                outcome     TEXT    NOT NULL,
                size        REAL    NOT NULL,
                avg_price   REAL    NOT NULL,
                snapshot_at TEXT    NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_trader_positions_market
                ON trader_positions (market_id, outcome);
        """)

        # Trader shadow forecasts — frozen snapshots of tracked-trader positions
        # at market resolution. Enables per-trader accuracy scoring by city/season.
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS trader_forecasts (
                id                 INTEGER PRIMARY KEY AUTOINCREMENT,
                wallet             TEXT    NOT NULL,
                market_id          TEXT    NOT NULL,
                city               TEXT    NOT NULL,
                end_date           TEXT    NOT NULL,
                threshold          TEXT,
                direction          TEXT    NOT NULL,
                entry_price        REAL    NOT NULL,
                net_size           REAL    NOT NULL,
                gross_size         REAL    NOT NULL,
                frozen_at          TEXT    NOT NULL,
                actual_resolution  TEXT,
                resolution_price   REAL,
                was_correct        INTEGER,
                actual_temperature REAL,
                temperature_delta  REAL,
                UNIQUE(wallet, market_id)
            );
            CREATE INDEX IF NOT EXISTS idx_trader_forecasts_wallet
                ON trader_forecasts (wallet);
            CREATE INDEX IF NOT EXISTS idx_trader_forecasts_city
                ON trader_forecasts (city, end_date);
            CREATE INDEX IF NOT EXISTS idx_trader_forecasts_market
                ON trader_forecasts (market_id);
        """)

        # Trader discovery — wallets identified as high-activity weather traders
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS tracked_traders (
                wallet           TEXT    PRIMARY KEY,
                pseudonym        TEXT,
                display_name     TEXT,
                n_weather_trades INTEGER DEFAULT 0,
                n_unique_markets INTEGER DEFAULT 0,
                n_resolved       INTEGER DEFAULT 0,
                win_rate         REAL,
                last_active      INTEGER,
                discovered_at    TEXT    NOT NULL,
                updated_at       TEXT    NOT NULL,
                is_active        INTEGER DEFAULT 1
            );
        """)

        # Forecast cache — persists Open-Meteo responses across restarts to avoid burst rate-limiting
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS weather_forecast_cache (
                city_key TEXT PRIMARY KEY,
                ts       REAL NOT NULL,
                forecast TEXT NOT NULL,
                ensemble TEXT NOT NULL
            );
        """)

        # METAR observations — paired with ensemble snapshot for bias-correction training
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS metar_observations (
                city                  TEXT    NOT NULL,
                icao                  TEXT    NOT NULL,
                observed_at_utc       TEXT    NOT NULL,
                observed_temp_c       REAL    NOT NULL,
                dewpoint_c            REAL,
                source                TEXT    NOT NULL,
                raw_report            TEXT,
                ensemble_point_mean_c REAL,
                ensemble_p05_c        REAL,
                ensemble_p50_c        REAL,
                ensemble_p95_c        REAL,
                forecast_run_at_utc   TEXT,
                fetched_at_utc        TEXT    NOT NULL,
                PRIMARY KEY (icao, observed_at_utc)
            );
            CREATE INDEX IF NOT EXISTS idx_metar_city_obs
                ON metar_observations(city, observed_at_utc);
        """)

        # Notifications — system alerts and trade events logged for the dashboard
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS notifications (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp   TEXT NOT NULL,
                severity    TEXT NOT NULL,
                channel     TEXT NOT NULL,
                title       TEXT NOT NULL,
                message     TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_notifications_severity
                ON notifications(severity);
            CREATE INDEX IF NOT EXISTS idx_notifications_timestamp
                ON notifications(timestamp DESC);
        """)

        # Sizing decisions — full Kelly diagnostic snapshot per APPROVED candidate.
        # Lets us answer "why did the bot bet $X" without having to back-derive
        # from float arithmetic. Written by weather_decision.evaluate() once size
        # is finalized (after slippage reducer). trade_id is set later if/when
        # the executor actually opens the trade.
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS sizing_decisions (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                trade_id            INTEGER,
                recorded_at         TEXT    NOT NULL,
                market_id           TEXT,
                market_name         TEXT,
                city                TEXT,
                direction           TEXT    NOT NULL,
                -- inputs
                balance             REAL    NOT NULL,
                model_prob          REAL,
                market_price        REAL,
                p_used              REAL,
                price_used          REAL,
                ensemble_n          INTEGER,
                days_to_resolution  INTEGER,
                ensemble_margin_c   REAL,
                is_unanimous        INTEGER NOT NULL,
                -- intermediates (every multiplier the live formula applies)
                edge                REAL,
                odds                REAL,
                kelly_raw           REAL,
                ensemble_scale      REAL,
                horizon_mult        REAL,
                margin_mult         REAL,
                kelly_fraction      REAL,
                kelly_final         REAL,
                -- caps + final size
                size_pre_cap        REAL,
                cap_per_bet         REAL,
                cap_balance_pct     REAL,
                size_after_caps     REAL    NOT NULL,
                slippage_reduced_to REAL,
                final_size          REAL    NOT NULL,
                binding_constraint  TEXT    NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_sizing_recorded
                ON sizing_decisions(recorded_at DESC);
            CREATE INDEX IF NOT EXISTS idx_sizing_trade
                ON sizing_decisions(trade_id);

            -- Phase 2 logging foundation (2026-05-08): shadow capture of
            -- decision-time signals not used to gate trades. One row per
            -- sized decision, keyed on sizing_decisions.id. Columns added
            -- incrementally by Phase2-0N items via _safe_add_column.
            CREATE TABLE IF NOT EXISTS decision_snapshots (
                id                   INTEGER PRIMARY KEY AUTOINCREMENT,
                sizing_decision_id   INTEGER NOT NULL,
                captured_at          TEXT    NOT NULL,
                det_icon_temp_c      REAL,
                det_gfs_temp_c       REAL,
                det_source_tag       TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_decsnap_sizing
                ON decision_snapshots(sizing_decision_id);
        """)

        # Safe schema migrations — no-op if column already exists
        _safe_add_column(conn, "trades", "edge_score",         "REAL")
        _safe_add_column(conn, "trades", "liquidity",          "REAL")
        _safe_add_column(conn, "trades", "city",               "TEXT")
        _safe_add_column(conn, "trades", "entry_ensemble_pct",   "REAL")
        _safe_add_column(conn, "trades", "entry_ensemble_yes",  "REAL")
        _safe_add_column(conn, "trades", "entry_ensemble_n",    "INTEGER")
        _safe_add_column(conn, "trades", "current_ensemble_yes","REAL")
        _safe_add_column(conn, "trades", "current_ensemble_n",  "INTEGER")
        _safe_add_column(conn, "trades", "current_ensemble_read_at", "TEXT")  # ISO ts of last valid ensemble read; None while stale
        _safe_add_column(conn, "trades", "market_url",          "TEXT")
        _safe_add_column(conn, "trades", "threshold",           "TEXT")
        _safe_add_column(conn, "trades", "actual_resolution",   "TEXT")     # 'YES' | 'NO' | NULL
        _safe_add_column(conn, "trades", "forecast_correct",    "INTEGER")  # 1 | 0 | NULL
        _safe_add_column(conn, "trades", "resolution_price",    "REAL")     # exact CLOB midpoint at settlement
        _safe_add_column(conn, "trades", "resolution_fetched_at", "TEXT")   # ISO timestamp of settlement confirmation
        _safe_add_column(conn, "trades", "actual_temperature",  "REAL")     # recorded daily max temp °C (Open-Meteo archive)
        _safe_add_column(conn, "trades", "temperature_delta",   "REAL")     # margin: positive = condition met (YES), negative = missed (NO)
        _safe_add_column(conn, "trades", "volume_24h",              "REAL")     # 24h volume at time of entry (refreshed each poll)
        _safe_add_column(conn, "trades", "hours_to_close_at_entry", "REAL")     # hours until market close at time of entry
        _safe_add_column(conn, "trades", "hours_to_close_at_exit",  "REAL")     # hours until market close at time of exit
        _safe_add_column(conn, "trades", "resolution_attempts",     "INTEGER")  # no_data/404 hit count; stop retrying at threshold
        _safe_add_column(conn, "trades", "parent_trade_id",  "INTEGER")
        _safe_add_column(conn, "trades", "leg_number",       "INTEGER DEFAULT 1")
        _safe_add_column(conn, "trades", "order_id",  "TEXT")      # CLOB order ID (live trades only)
        _safe_add_column(conn, "trades", "fee_usdc",  "REAL")      # Polymarket fees deducted from fill
        _safe_add_column(conn, "trades", "claim_status",       "TEXT")              # claim_pending | claim_confirmed | claim_failed
        _safe_add_column(conn, "trades", "claim_tx_hash",      "TEXT")              # Polygon tx hash
        _safe_add_column(conn, "trades", "claim_retries",      "INTEGER DEFAULT 0") # retry count
        _safe_add_column(conn, "trades", "claim_last_attempt", "TEXT")              # ISO timestamp of last attempt
        # Resilient exit execution (2026-04-15) — GTC order tracking
        _safe_add_column(conn, "trades", "exit_order_id",        "TEXT")
        _safe_add_column(conn, "trades", "exit_order_price",     "REAL")
        _safe_add_column(conn, "trades", "exit_order_placed_at", "TEXT")

        # E1-06 (2026-05-08) — calibration + dual-mode stake logging.
        # raw_prob and calibrated_prob are equal until Phase 3 ships per-city Platt
        # calibration; columns exist now so downstream code can write divergent
        # values without another migration. fixed_mode_stake_usdc records what a
        # flat-stake reference (the E15.4 fixed_50 baseline) would have wagered.
        _safe_add_column(conn, "sizing_decisions", "raw_prob",              "REAL")
        _safe_add_column(conn, "sizing_decisions", "calibrated_prob",       "REAL")
        _safe_add_column(conn, "sizing_decisions", "fixed_mode_stake_usdc", "REAL")

        # Phase2-02 (2026-05-08): GEFS-31 ensemble spread at decision time.
        # Derived from existing ensemble fetch (no new API calls) — std,
        # interquartile range, and 5/95 percentile bounds for the market's
        # resolution date.
        _safe_add_column(conn, "decision_snapshots", "ens_std", "REAL")
        _safe_add_column(conn, "decision_snapshots", "ens_iqr", "REAL")
        _safe_add_column(conn, "decision_snapshots", "ens_p05", "REAL")
        _safe_add_column(conn, "decision_snapshots", "ens_p95", "REAL")

        # E4-05 (2026-05-08): prev-day same-city resolution outcome. Read from
        # weather_city_log at decision time — the cheapest free signal per E4
        # findings (+19.5pp lift). prev_day_outcome is 'YES' / 'NO' / NULL;
        # prev_day_question stores the threshold string for coverage debugging.
        _safe_add_column(conn, "decision_snapshots", "prev_day_outcome",  "TEXT")
        _safe_add_column(conn, "decision_snapshots", "prev_day_question", "TEXT")

        # Phase2-06 (2026-05-08): live flip detector. Shadow-only; piggybacks
        # on the 60s scanner tick. Each row is one detected price-flip event
        # for downstream Phase 3 NO-flip continuation gate (E2-02).
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS flip_events (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                detected_at     TEXT    NOT NULL,
                market_id       TEXT    NOT NULL,
                city            TEXT,
                direction       TEXT    NOT NULL,
                price_before    REAL    NOT NULL,
                price_after     REAL    NOT NULL,
                delta_pct       REAL    NOT NULL,
                minutes_window  REAL    NOT NULL,
                poll_tick_id    TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_flip_events_market
                ON flip_events(market_id, detected_at DESC);
            CREATE INDEX IF NOT EXISTS idx_flip_events_city
                ON flip_events(city, detected_at DESC);
        """)

        # Backfill hours_to_close for trades that predate this column
        conn.executescript("""
            UPDATE trades
               SET hours_to_close_at_entry = (julianday(end_date) - julianday(opened_at)) * 24.0
             WHERE hours_to_close_at_entry IS NULL
               AND end_date IS NOT NULL
               AND opened_at IS NOT NULL;

            UPDATE trades
               SET hours_to_close_at_exit = (julianday(end_date) - julianday(closed_at)) * 24.0
             WHERE hours_to_close_at_exit IS NULL
               AND end_date IS NOT NULL
               AND closed_at IS NOT NULL
               AND status = 'closed';
        """)

        # At most one active (open or claim_pending) trade per on-chain
        # token_id. Prevents reconciliation from re-importing positions that
        # are still held pending an on-chain redeem. Closed trades are
        # excluded so the same market can legitimately be re-entered later.
        try:
            conn.execute("""
                CREATE UNIQUE INDEX IF NOT EXISTS idx_trade_active_token
                    ON trades(token_id)
                    WHERE token_id IS NOT NULL
                      AND status IN ('open', 'claim_pending')
                      AND parent_trade_id IS NULL
            """)
        except sqlite3.IntegrityError as e:
            # Existing duplicates block index creation. Surface loudly so the
            # operator cleans the DB instead of silently losing the guardrail.
            print(f"[db] WARNING: cannot create idx_trade_active_token — {e}")
            print("[db]          duplicate active token_id rows exist. "
                  "Clean the DB and restart to enable the guardrail.")


def _safe_add_column(conn: sqlite3.Connection, table: str, column: str, col_type: str):
    try:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {col_type}")
    except sqlite3.OperationalError:
        pass  # column already exists


# ── Balance ───────────────────────────────────────────────────────────────────

def get_balance() -> float:
    with get_conn() as conn:
        row = conn.execute("SELECT amount FROM balance WHERE id = 1").fetchone()
        return float(row["amount"]) if row else 0.0


def update_balance(delta: float):
    now = datetime.utcnow().isoformat()
    with get_conn() as conn:
        conn.execute(
            "UPDATE balance SET amount = amount + ?, updated_at = ? WHERE id = 1",
            (delta, now),
        )


def set_balance(amount: float):
    """Set the balance to an exact value (used for exchange sync)."""
    with get_conn() as conn:
        now = datetime.utcnow().isoformat()
        conn.execute(
            "UPDATE balance SET amount = ?, updated_at = ? WHERE id = 1",
            (amount, now),
        )


def record_account_value():
    """
    Compute and record current account value = cash + open position market value.
    Call this after any balance-changing event (open, close, settle).
    This replaces recording raw cash balance in history, so the balance chart
    reflects true portfolio value rather than dropping when capital is deployed.
    """
    now = datetime.utcnow().isoformat()
    with get_conn() as conn:
        cash = conn.execute("SELECT amount FROM balance WHERE id = 1").fetchone()
        cash = float(cash["amount"]) if cash else 0.0

        open_trades = conn.execute(
            "SELECT current_price, fill_price, shares, status FROM trades WHERE status IN ('open', 'claim_pending')"
        ).fetchall()

        position_value = sum(
            t["shares"] if t["status"] == "claim_pending"
            else (t["current_price"] or t["fill_price"]) * t["shares"]
            for t in open_trades
        )

        account_value = cash + position_value
        conn.execute(
            "INSERT INTO balance_history (amount, recorded_at) VALUES (?, ?)",
            (account_value, now),
        )


def get_balance_history() -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT amount, recorded_at FROM balance_history ORDER BY id ASC"
        ).fetchall()
        return [dict(r) for r in rows]


# ── Trades ────────────────────────────────────────────────────────────────────

def insert_trade(trade: dict) -> int:
    trade        = _norm_city(trade)
    cols         = ", ".join(trade.keys())
    placeholders = ", ".join("?" for _ in trade)
    with get_conn() as conn:
        cur = conn.execute(
            f"INSERT INTO trades ({cols}) VALUES ({placeholders})",
            list(trade.values()),
        )
        return cur.lastrowid


def get_open_trades() -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM trades WHERE status = 'open' ORDER BY opened_at DESC"
        ).fetchall()
        return [dict(r) for r in rows]


def get_exit_pending_trades() -> list[dict]:
    """Return parent trades that have a pending GTC exit order on the CLOB."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM trades WHERE status = 'exit_pending' "
            "AND exit_order_id IS NOT NULL ORDER BY opened_at ASC"
        ).fetchall()
        return [dict(r) for r in rows]


def get_all_trades() -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM trades ORDER BY opened_at DESC"
        ).fetchall()
        return [dict(r) for r in rows]


def get_closed_trades() -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM trades WHERE status = 'closed' ORDER BY closed_at DESC"
        ).fetchall()
        return [dict(r) for r in rows]


def update_trade(trade_id: int, updates: dict):
    if not updates:
        return
    set_clause = ", ".join(f"{k} = ?" for k in updates)
    with get_conn() as conn:
        conn.execute(
            f"UPDATE trades SET {set_clause} WHERE id = ?",
            [*updates.values(), trade_id],
        )


def get_open_trade_for_market(market_id: str) -> dict | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM trades WHERE market_id = ? AND status = 'open' LIMIT 1",
            (market_id,),
        ).fetchone()
        return dict(row) if row else None


def get_trade_by_id(trade_id: int) -> dict | None:
    """Return a single trade row by ID, or None if not found."""
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM trades WHERE id = ?", (trade_id,)).fetchone()
        return dict(row) if row else None


def get_trades_today() -> int:
    """Count new positions opened since midnight UTC today."""
    today = datetime.now(timezone.utc).date().isoformat()  # "YYYY-MM-DD"
    with get_conn() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS cnt FROM trades WHERE opened_at >= ?",
            (today,),
        ).fetchone()
        return row["cnt"] if row else 0


def get_pending_claims() -> list[dict]:
    """Return all trades with claim_status = 'claim_pending'."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM trades WHERE claim_status = 'claim_pending' ORDER BY id ASC"
        ).fetchall()
        return [dict(r) for r in rows]


def get_stuck_claim_pending_trades() -> list[dict]:
    """Return all trades still in status='claim_pending', regardless of claim_status.
    Used by the sibling-redemption sweep to resolve trades whose on-chain shares
    were already redeemed as part of a sibling's claim tx (same token_id)."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM trades WHERE status = 'claim_pending' ORDER BY id ASC"
        ).fetchall()
        return [dict(r) for r in rows]


def find_confirmed_sibling(token_id: str, exclude_trade_id: int) -> dict | None:
    """Return a sibling trade (same token_id, different id) that has already been
    redeemed on-chain (claim_status='claim_confirmed'). Neg-risk positions share a
    token_id across extended-position legs; the first claim redeems the wallet's
    full balance for that token, leaving $0 for later sibling claim attempts."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM trades WHERE token_id = ? AND id != ? "
            "AND claim_status = 'claim_confirmed' "
            "ORDER BY claim_last_attempt DESC LIMIT 1",
            (token_id, exclude_trade_id),
        ).fetchone()
        return dict(row) if row else None


def get_open_position_count() -> int:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS cnt FROM trades WHERE status = 'open'"
        ).fetchone()
        return row["cnt"] if row else 0


def get_open_city_directions() -> set[tuple[str, str]]:
    """
    Return the set of (city, direction) pairs that currently have an open trade.
    Kept for reference; decision layer now uses get_open_city_dates().
    """
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT city, direction FROM trades WHERE status = 'open' AND city IS NOT NULL"
        ).fetchall()
        return {(r["city"].lower(), r["direction"].lower()) for r in rows}


def get_open_cities() -> set[str]:
    """Return the set of city keys (lowercase) that currently have an open trade."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT city FROM trades WHERE status = 'open' AND city IS NOT NULL"
        ).fetchall()
    return {r["city"].lower() for r in rows if r["city"]}


def get_open_city_dates() -> set[tuple[str, str]]:
    """
    Return the set of (city, resolution_date) pairs that currently have an open trade.
    Kept for reference; decision layer now uses get_open_market_ids() and
    get_open_city_date_counts() for finer-grained duplicate control.
    """
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT city, end_date FROM trades WHERE status = 'open' AND city IS NOT NULL"
        ).fetchall()
    result = set()
    for r in rows:
        city     = r["city"]
        end_date = r["end_date"]
        if city and end_date:
            result.add((city.lower(), end_date[:10]))   # "YYYY-MM-DD"
    return result


def get_open_market_ids() -> set[str]:
    """Return the set of market_ids that currently have an open trade."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT market_id FROM trades WHERE status = 'open'"
        ).fetchall()
    return {r["market_id"] for r in rows if r["market_id"]}


def get_position_legs(parent_id: int) -> list[dict]:
    """Return all legs of an extended position (parent + children), ordered by leg_number."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM trades WHERE (id = ? OR parent_trade_id = ?) "
            "AND status IN ('open', 'exit_pending') "
            "ORDER BY leg_number",
            (parent_id, parent_id),
        ).fetchall()
        return [dict(r) for r in rows]


def get_leg_count(parent_id: int) -> int:
    """Count open legs for an extended position (parent + children)."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT COUNT(*) FROM trades WHERE (id = ? OR parent_trade_id = ?) AND status = 'open'",
            (parent_id, parent_id),
        ).fetchone()
        return row[0] if row else 0


def get_latest_leg(parent_id: int) -> dict | None:
    """Return the most recently opened leg of an extended position."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM trades WHERE (id = ? OR parent_trade_id = ?) AND status = 'open' "
            "ORDER BY opened_at DESC LIMIT 1",
            (parent_id, parent_id),
        ).fetchone()
        return dict(row) if row else None


def get_all_position_legs(parent_id: int) -> list[dict]:
    """Return all legs of a position regardless of status, ordered by leg_number."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM trades WHERE (id = ? OR parent_trade_id = ?) "
            "ORDER BY leg_number",
            (parent_id, parent_id),
        ).fetchall()
        return [dict(r) for r in rows]


def get_open_city_date_counts() -> dict[tuple[str, str], int]:
    """
    Return the number of open positions per (city, resolution_date) pair.
    Used by the decision layer to enforce a per-city-date concentration cap
    while still allowing multiple distinct threshold markets on the same event.
    """
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT city, end_date FROM trades WHERE status = 'open' AND city IS NOT NULL"
        ).fetchall()
    counts: dict[tuple[str, str], int] = {}
    for r in rows:
        if r["city"] and r["end_date"]:
            key = (r["city"].lower(), r["end_date"][:10])
            counts[key] = counts.get(key, 0) + 1
    return counts


# ── Reset / recovery ─────────────────────────────────────────────────────────

def reset_paper_trading():
    """
    Wipe all trades and balance history, reset balance to starting value.
    Used when the user wants a clean slate on paper trading restart.
    """
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")
    with get_conn() as conn:
        conn.execute("DELETE FROM trades")
        conn.execute("DELETE FROM balance_history")
        conn.execute(
            "UPDATE balance SET amount = ?, updated_at = ? WHERE id = 1",
            (PAPER_STARTING_BALANCE, now),
        )
        conn.execute(
            "INSERT INTO balance_history (amount, recorded_at) VALUES (?, ?)",
            (PAPER_STARTING_BALANCE, now),
        )


def get_session_summary() -> dict:
    """
    Returns a brief summary of the current DB state.
    Used by the startup prompt to tell the user what they're resuming.
    """
    with get_conn() as conn:
        open_count   = conn.execute("SELECT COUNT(*) FROM trades WHERE status='open'").fetchone()[0]
        closed_count = conn.execute("SELECT COUNT(*) FROM trades WHERE status='closed'").fetchone()[0]
        balance      = conn.execute("SELECT amount FROM balance WHERE id=1").fetchone()
        balance      = float(balance[0]) if balance else PAPER_STARTING_BALANCE
        oldest       = conn.execute(
            "SELECT opened_at FROM trades ORDER BY opened_at ASC LIMIT 1"
        ).fetchone()
        first_trade  = oldest[0][:10] if oldest else None
    return {
        "open_count":   open_count,
        "closed_count": closed_count,
        "balance":      balance,
        "first_trade":  first_trade,
    }


# ── Stats helpers (used by dashboard) ────────────────────────────────────────

def get_stats() -> dict:
    with get_conn() as conn:
        all_trades = conn.execute("SELECT * FROM trades").fetchall()
        closed     = [t for t in all_trades if t["status"] == "closed"]
        open_      = [t for t in all_trades if t["status"] == "open"]

        total_pnl  = sum(t["pnl"] for t in closed if t["pnl"] is not None)
        wins       = [t for t in closed if (t["pnl"] or 0) > 0]
        losses     = [t for t in closed if (t["pnl"] or 0) <= 0]
        win_rate   = (len(wins) / len(closed) * 100) if closed else 0.0

        unrealized = sum(
            (t["current_price"] - t["fill_price"]) * t["shares"]
            for t in open_
            if t["current_price"] and t["fill_price"]
        )

        return {
            "total_pnl":    total_pnl,
            "unrealized":   unrealized,
            "win_rate":     win_rate,
            "total_trades": len(all_trades),
            "open_count":   len(open_),
            "closed_count": len(closed),
            "wins":         len(wins),
            "losses":       len(losses),
        }


def get_today_realized_losses() -> tuple[int, float]:
    """Return (count, total_usdc) of losing trades closed today (UTC).

    Only counts trades with negative P&L. Winning trades do not offset.
    Used by the daily loss circuit breaker in weather_risk.py.
    """
    with get_conn() as conn:
        row = conn.execute(
            """
            SELECT COUNT(*) AS cnt, COALESCE(SUM(ABS(pnl)), 0.0) AS total
            FROM trades
            WHERE status = 'closed'
              AND pnl < 0
              AND DATE(closed_at) = DATE('now')
            """
        ).fetchone()
        return int(row["cnt"]), float(row["total"])


# ── Scanner cache ────────────────────────────────────────────────────────────

_SCAN_CACHE_FIELDS = {
    "city_display", "resolves_str", "threshold_str", "volume",
    "market_price", "model_prob", "edge_pct", "conviction_ok",
    "days_to_resolution", "hours_to_close", "city", "ens_pct", "ens_n", "ens_yes", "rank",
    "market_url", "market_id",
}


def save_scan_cache(candidates: list[dict]):
    """Persist scan results so the dashboard can display without re-scanning."""
    serializable = [
        {k: v for k, v in c.items() if k in _SCAN_CACHE_FIELDS}
        for c in candidates
    ]
    now = datetime.now(timezone.utc).isoformat()
    with get_conn() as conn:
        conn.execute("DELETE FROM scanner_cache")
        conn.execute(
            "INSERT INTO scanner_cache (id, results_json, cached_at) VALUES (1, ?, ?)",
            (json.dumps(serializable), now),
        )


def get_scan_cache() -> tuple[list[dict], str | None]:
    """Returns (results, cached_at_str) or ([], None) if no cache exists yet."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT results_json, cached_at FROM scanner_cache LIMIT 1"
        ).fetchone()
        if not row:
            return [], None
        return json.loads(row["results_json"]), row["cached_at"]


# ── Weather city catalog ──────────────────────────────────────────────────────

def upsert_city_log(entry: dict):
    """
    Insert or update a daily city volume snapshot.
    Unique on (logged_date, city, threshold) — safe to call multiple times per day.
    """
    entry = _norm_city(entry)
    with get_conn() as conn:
        conn.execute("""
            INSERT INTO weather_city_log
                (logged_date, city, market_type, threshold, volume_24h,
                 yes_price, model_prob, ensemble_pct, ensemble_n, logged_at)
            VALUES (:logged_date, :city, :market_type, :threshold, :volume_24h,
                    :yes_price, :model_prob, :ensemble_pct, :ensemble_n, :logged_at)
            ON CONFLICT(logged_date, city, threshold) DO UPDATE SET
                volume_24h   = excluded.volume_24h,
                yes_price    = excluded.yes_price,
                model_prob   = excluded.model_prob,
                ensemble_pct = excluded.ensemble_pct,
                ensemble_n   = excluded.ensemble_n,
                logged_at    = excluded.logged_at
        """, entry)


def mark_city_log_resolved(logged_date: str, city: str, threshold: str, resolved_yes: bool):
    """Fill in the resolution outcome after the market closes."""
    with get_conn() as conn:
        conn.execute("""
            UPDATE weather_city_log
               SET resolved_yes = ?
             WHERE logged_date = ? AND city = ? AND threshold = ?
        """, (1 if resolved_yes else 0, logged_date, city, threshold))


def get_prev_day_resolution(city: str, before_date: str) -> dict | None:
    """E4-05: most recent resolved weather_city_log row for `city` with
    logged_date strictly before `before_date`. Returns dict with
    'logged_date', 'threshold', 'resolved_yes' or None if no resolved row
    exists in the window. Picks the highest-volume threshold when multiple
    rows share the same most-recent date.
    """
    with get_conn() as conn:
        row = conn.execute("""
            SELECT logged_date, threshold, resolved_yes, volume_24h
              FROM weather_city_log
             WHERE city = ?
               AND logged_date < ?
               AND resolved_yes IS NOT NULL
             ORDER BY logged_date DESC, volume_24h DESC
             LIMIT 1
        """, (city.lower(), before_date)).fetchone()
    return dict(row) if row else None


def get_city_avg_volume(city: str, days: int = 14) -> float | None:
    """
    Return average 24h volume for a city over the last N days of catalog data.
    Used by the decision layer to rank cities when current-day volume is low.
    """
    with get_conn() as conn:
        row = conn.execute("""
            SELECT AVG(volume_24h) AS avg_vol
              FROM weather_city_log
             WHERE city = ?
               AND logged_date >= date('now', ?)
        """, (city, f"-{days} days")).fetchone()
        return float(row["avg_vol"]) if row and row["avg_vol"] is not None else None


def upsert_trader_positions(wallet: str, positions: list[dict]):
    """
    Replace all current position snapshots for a wallet with the latest batch.
    Called each time we poll a tracked trader's live positions.
    """
    now = datetime.now(timezone.utc).isoformat()
    with get_conn() as conn:
        conn.execute("DELETE FROM trader_positions WHERE wallet = ?", (wallet,))
        for p in positions:
            conn.execute("""
                INSERT INTO trader_positions (wallet, market_id, outcome, size, avg_price, snapshot_at)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (wallet, p["market_id"], p["outcome"].upper(), p["size"], p["avg_price"], now))


def freeze_trader_forecasts(
    market_id: str,
    close_price: float,
    city: str,
    end_date: str,
    threshold: str | None,
) -> int:
    """
    Freeze all tracked-trader positions for a resolved market into trader_forecasts.
    Idempotent via UNIQUE(wallet, market_id) — safe to re-call.

    Args:
        market_id:  condition_id of the resolved market
        close_price: CLOB midpoint at settlement (~0.001 or ~0.999), expressed as
                     the YES-token price. IMPORTANT: callers must invert NO-token
                     midpoints before passing (i.e. close_price = 1.0 - raw_midpoint
                     for NO trades). This function always interprets close_price from
                     the YES side to determine actual_resolution.
        city:       city this market was for (denormalized)
        end_date:   YYYY-MM-DD resolution date (denormalized)
        threshold:  threshold string e.g. ">=75" (denormalized, nullable)

    Returns: number of new rows inserted.
    """
    now_iso = datetime.now(timezone.utc).isoformat()
    actual_resolution = "YES" if close_price > 0.5 else "NO"
    inserted = 0

    with get_conn() as conn:
        # Group current positions by wallet for this market
        rows = conn.execute("""
            SELECT wallet, outcome, size, avg_price
              FROM trader_positions
             WHERE market_id = ?
        """, (market_id,)).fetchall()

        by_wallet: dict[str, dict] = {}
        for r in rows:
            w = r["wallet"]
            slot = by_wallet.setdefault(w, {"YES": None, "NO": None})
            slot[r["outcome"].upper()] = {"size": r["size"], "avg_price": r["avg_price"]}

        for wallet, sides in by_wallet.items():
            yes_size  = sides["YES"]["size"]      if sides["YES"] else 0.0
            no_size   = sides["NO"]["size"]       if sides["NO"]  else 0.0
            yes_price = sides["YES"]["avg_price"] if sides["YES"] else 0.0
            no_price  = sides["NO"]["avg_price"]  if sides["NO"]  else 0.0

            net_size   = abs(yes_size - no_size)
            gross_size = yes_size + no_size
            if net_size < 0.01:
                continue  # fully hedged or dust — skip

            if yes_size >= no_size:
                direction   = "YES"
                entry_price = yes_price
            else:
                direction   = "NO"
                entry_price = no_price

            was_correct = 1 if direction == actual_resolution else 0

            cur = conn.execute("""
                INSERT INTO trader_forecasts
                    (wallet, market_id, city, end_date, threshold,
                     direction, entry_price, net_size, gross_size, frozen_at,
                     actual_resolution, resolution_price, was_correct)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(wallet, market_id) DO UPDATE SET
                    actual_resolution = excluded.actual_resolution,
                    resolution_price  = excluded.resolution_price,
                    was_correct       = excluded.was_correct
                  WHERE actual_resolution IS NULL
            """, (wallet, market_id, city.lower(), end_date, threshold,
                  direction, entry_price, net_size, gross_size, now_iso,
                  actual_resolution, round(close_price, 4), was_correct))
            inserted += cur.rowcount

    return inserted


def snapshot_trader_forecasts_on_entry(
    market_id: str,
    city: str,
    end_date: str,
    threshold: str | None,
) -> int:
    """
    Snapshot tracked-trader positions on this market at trade-entry time, with
    resolution fields NULL. Resolution data is filled in later by
    freeze_trader_forecasts() when the market settles.

    Reads from the cached trader_positions table (refreshed by trader_monitor).
    Returns number of new rows inserted (0 if no tracked traders hold positions
    on this market at the moment of call — that is normal for first-trade-on-market).
    """
    now_iso = datetime.now(timezone.utc).isoformat()
    inserted = 0

    with get_conn() as conn:
        rows = conn.execute("""
            SELECT wallet, outcome, size, avg_price
              FROM trader_positions
             WHERE market_id = ?
        """, (market_id,)).fetchall()

        by_wallet: dict[str, dict] = {}
        for r in rows:
            w = r["wallet"]
            slot = by_wallet.setdefault(w, {"YES": None, "NO": None})
            slot[r["outcome"].upper()] = {"size": r["size"], "avg_price": r["avg_price"]}

        for wallet, sides in by_wallet.items():
            yes_size  = sides["YES"]["size"]      if sides["YES"] else 0.0
            no_size   = sides["NO"]["size"]       if sides["NO"]  else 0.0
            yes_price = sides["YES"]["avg_price"] if sides["YES"] else 0.0
            no_price  = sides["NO"]["avg_price"]  if sides["NO"]  else 0.0

            net_size   = abs(yes_size - no_size)
            gross_size = yes_size + no_size
            if net_size < 0.01:
                continue

            if yes_size >= no_size:
                direction   = "YES"
                entry_price = yes_price
            else:
                direction   = "NO"
                entry_price = no_price

            cur = conn.execute("""
                INSERT OR IGNORE INTO trader_forecasts
                    (wallet, market_id, city, end_date, threshold,
                     direction, entry_price, net_size, gross_size, frozen_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (wallet, market_id, city.lower(), end_date, threshold,
                  direction, entry_price, net_size, gross_size, now_iso))
            inserted += cur.rowcount

    return inserted


def get_trader_consensus(market_id: str, direction: str, min_resolved: int = 15, min_win_rate: float = 0.55) -> dict:
    """
    For a given market and direction (YES/NO), count how many qualified tracked
    traders currently hold that position vs the opposite.

    Only counts traders with enough resolved history (min_resolved) and a
    win rate above threshold (min_win_rate) — probation wallets are excluded.

    Returns:
        {
            "same":     int,   # traders holding same direction
            "opposite": int,   # traders holding opposite direction
            "signal":   str,   # CONFIRM | DIVERGE | NEUTRAL
        }
    """
    opposite = "NO" if direction.upper() == "YES" else "YES"
    with get_conn() as conn:
        same = conn.execute("""
            SELECT COUNT(*) FROM trader_positions tp
            JOIN tracked_traders tt ON tp.wallet = tt.wallet
            WHERE tp.market_id = ?
              AND tp.outcome    = ?
              AND tt.n_resolved >= ?
              AND tt.win_rate   >= ?
              AND tt.is_active  = 1
        """, (market_id, direction.upper(), min_resolved, min_win_rate)).fetchone()[0]

        opp = conn.execute("""
            SELECT COUNT(*) FROM trader_positions tp
            JOIN tracked_traders tt ON tp.wallet = tt.wallet
            WHERE tp.market_id = ?
              AND tp.outcome    = ?
              AND tt.n_resolved >= ?
              AND tt.win_rate   >= ?
              AND tt.is_active  = 1
        """, (market_id, opposite, min_resolved, min_win_rate)).fetchone()[0]

    if same >= 2 and opp == 0:
        signal = "CONFIRM"
    elif opp >= 1:
        signal = "DIVERGE"
    else:
        signal = "NEUTRAL"

    return {"same": same, "opposite": opp, "signal": signal}


def upsert_tracked_trader(trader: dict):
    """Insert or update a tracked trader record."""
    now = datetime.now(timezone.utc).isoformat()
    with get_conn() as conn:
        conn.execute("""
            INSERT INTO tracked_traders
                (wallet, pseudonym, display_name, n_weather_trades, n_unique_markets,
                 n_resolved, win_rate, last_active, discovered_at, updated_at, is_active)
            VALUES
                (:wallet, :pseudonym, :display_name, :n_weather_trades, :n_unique_markets,
                 :n_resolved, :win_rate, :last_active, :discovered_at, :updated_at, 1)
            ON CONFLICT(wallet) DO UPDATE SET
                pseudonym        = excluded.pseudonym,
                display_name     = excluded.display_name,
                n_weather_trades = excluded.n_weather_trades,
                n_unique_markets = excluded.n_unique_markets,
                n_resolved       = excluded.n_resolved,
                win_rate         = excluded.win_rate,
                last_active      = excluded.last_active,
                updated_at       = excluded.updated_at,
                is_active        = 1
        """, {**trader, "updated_at": now, "discovered_at": trader.get("discovered_at", now)})


def get_tracked_traders(active_only: bool = True) -> list[dict]:
    """Return all tracked traders, optionally filtered to active ones."""
    with get_conn() as conn:
        query = "SELECT * FROM tracked_traders"
        if active_only:
            query += " WHERE is_active = 1"
        query += " ORDER BY win_rate DESC NULLS LAST"
        rows = conn.execute(query).fetchall()
        return [dict(r) for r in rows]


def get_resolved_market_outcomes() -> dict[str, str]:
    """
    Return {condition_id: actual_resolution} for all our resolved trades.
    Used by trader_discovery to score wallet accuracy.
    """
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT market_id, actual_resolution FROM trades WHERE actual_resolution IS NOT NULL"
        ).fetchall()
    return {r["market_id"]: r["actual_resolution"] for r in rows}


# ── Forecast cache (persists Open-Meteo data across restarts) ────────────────

def save_forecast_cache(city_key: str, ts: float, forecast: list, ensemble: list):
    """Upsert a city's forecast into the persistent cache."""
    with get_conn() as conn:
        conn.execute("""
            INSERT INTO weather_forecast_cache (city_key, ts, forecast, ensemble)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(city_key) DO UPDATE SET
                ts       = excluded.ts,
                forecast = excluded.forecast,
                ensemble = excluded.ensemble
        """, (city_key, ts, json.dumps(forecast), json.dumps(ensemble)))


def load_forecast_cache() -> dict:
    """
    Return all rows as a dict keyed by city_key, matching the in-memory _cache format:
    {city_key: {"ts": float, "forecast": list, "ensemble": list}}
    Returns {} if the table doesn't exist yet (first run before init_db()).
    """
    try:
        with get_conn() as conn:
            rows = conn.execute(
                "SELECT city_key, ts, forecast, ensemble FROM weather_forecast_cache"
            ).fetchall()
        return {
            r["city_key"]: {
                "ts":       r["ts"],
                "forecast": json.loads(r["forecast"]),
                "ensemble": json.loads(r["ensemble"]),
            }
            for r in rows
        }
    except sqlite3.OperationalError:
        return {}


def get_city_win_rate(city: str, days: int = 30) -> dict | None:
    """
    Return model accuracy stats for a city over the last N days.
    Only counts rows where resolution outcome is known.
    """
    with get_conn() as conn:
        row = conn.execute("""
            SELECT
                COUNT(*)                                    AS total,
                SUM(CASE WHEN resolved_yes IS NOT NULL
                         THEN 1 ELSE 0 END)                AS resolved,
                SUM(CASE
                    WHEN resolved_yes = 1 AND model_prob >= 0.5 THEN 1
                    WHEN resolved_yes = 0 AND model_prob <  0.5 THEN 1
                    ELSE 0 END)                             AS correct
              FROM weather_city_log
             WHERE city = ?
               AND logged_date >= date('now', ?)
               AND resolved_yes IS NOT NULL
        """, (city, f"-{days} days")).fetchone()
        if not row or not row["resolved"]:
            return None
        return {
            "total":    row["total"],
            "resolved": row["resolved"],
            "correct":  row["correct"],
            "accuracy": row["correct"] / row["resolved"],
        }


def backfill_trader_forecast_temperatures(
    city: str,
    date_to_temp: dict[str, float],
    threshold_parser,
    delta_calc,
) -> int:
    """
    Update trader_forecasts rows for (city, each date) with actual_temperature
    and derived temperature_delta.

    Args:
        city:             lowercase city key
        date_to_temp:     {'YYYY-MM-DD': temp_celsius}
        threshold_parser: callable(threshold_str) -> (op, threshold_c) | None
        delta_calc:       callable(temp_c, op, threshold_c) -> delta_c

    Returns: count of rows updated.
    """
    updated = 0
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT id, end_date, threshold
              FROM trader_forecasts
             WHERE lower(city) = ?
               AND actual_temperature IS NULL
        """, (city.lower(),)).fetchall()

        for r in rows:
            d = (r["end_date"] or "")[:10]
            temp = date_to_temp.get(d)
            if temp is None:
                continue
            if not r["threshold"]:
                conn.execute(
                    "UPDATE trader_forecasts SET actual_temperature = ? WHERE id = ?",
                    (round(temp, 2), r["id"]),
                )
                updated += 1
                continue
            parsed = threshold_parser(r["threshold"])
            if parsed is None:
                conn.execute(
                    "UPDATE trader_forecasts SET actual_temperature = ? WHERE id = ?",
                    (round(temp, 2), r["id"]),
                )
                updated += 1
                continue
            op, threshold_c = parsed
            delta = delta_calc(temp, op, threshold_c)
            conn.execute(
                "UPDATE trader_forecasts SET actual_temperature = ?, temperature_delta = ? WHERE id = ?",
                (round(temp, 2), round(delta, 2), r["id"]),
            )
            updated += 1
    return updated


def get_trader_forecast_cities_needing_temp() -> dict[str, set[str]]:
    """
    Return {city: {dates}} for trader_forecasts rows missing actual_temperature
    where end_date is in the past.
    """
    from datetime import datetime as _dt, timezone as _tz
    now_date = _dt.now(_tz.utc).strftime("%Y-%m-%d")
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT lower(city) AS city, substr(end_date, 1, 10) AS d
              FROM trader_forecasts
             WHERE actual_temperature IS NULL
               AND substr(end_date, 1, 10) < ?
        """, (now_date,)).fetchall()
    result: dict[str, set[str]] = {}
    for r in rows:
        result.setdefault(r["city"], set()).add(r["d"])
    return result


def get_trader_accuracy_summary(
    min_resolved: int = 10,
    require_temp_delta_ge: float | None = None,
) -> list[dict]:
    """
    Per-wallet accuracy summary for the dashboard.

    Returns list of dicts: {
        wallet, pseudonym, n_forecasts, n_resolved, accuracy, brier,
        best_city, worst_city, best_season, edge_tier_breakdown, last_forecast
    }

    Filters:
        min_resolved: only include wallets with >= this many resolved forecasts
        require_temp_delta_ge: if set, only score forecasts where |temperature_delta| >= this value
    """
    delta_clause = ""
    params: list = []
    if require_temp_delta_ge is not None:
        delta_clause = " AND abs(temperature_delta) >= ? "
        params.append(require_temp_delta_ge)

    with get_conn() as conn:
        wallets = conn.execute(f"""
            SELECT tf.wallet,
                   COUNT(*) AS n_forecasts,
                   SUM(CASE WHEN was_correct IS NOT NULL THEN 1 ELSE 0 END) AS n_resolved,
                   SUM(CASE WHEN was_correct = 1 THEN 1 ELSE 0 END) AS n_correct,
                   AVG(CASE WHEN was_correct IS NOT NULL
                            THEN (entry_price - was_correct) * (entry_price - was_correct)
                       END) AS brier,
                   MAX(frozen_at) AS last_forecast
              FROM trader_forecasts tf
             WHERE 1=1 {delta_clause}
          GROUP BY tf.wallet
            HAVING n_resolved >= ?
          ORDER BY (CAST(n_correct AS REAL) / NULLIF(n_resolved, 0)) DESC
        """, (*params, min_resolved)).fetchall()

        results = []
        for w in wallets:
            wallet   = w["wallet"]
            accuracy = (w["n_correct"] / w["n_resolved"]) if w["n_resolved"] else None

            # Pseudonym
            pseudo_row = conn.execute(
                "SELECT pseudonym FROM tracked_traders WHERE wallet = ?", (wallet,)
            ).fetchone()
            pseudonym = pseudo_row["pseudonym"] if pseudo_row else None

            # Best / worst city (min 5 resolved per city)
            city_rows = conn.execute(f"""
                SELECT city,
                       SUM(CASE WHEN was_correct = 1 THEN 1 ELSE 0 END) * 1.0 /
                         NULLIF(SUM(CASE WHEN was_correct IS NOT NULL THEN 1 ELSE 0 END), 0) AS acc
                  FROM trader_forecasts
                 WHERE wallet = ? AND was_correct IS NOT NULL {delta_clause}
              GROUP BY city
                HAVING SUM(CASE WHEN was_correct IS NOT NULL THEN 1 ELSE 0 END) >= 5
            """, (wallet, *params)).fetchall()
            best_city  = max(city_rows, key=lambda r: r["acc"])["city"] if city_rows else None
            worst_city = min(city_rows, key=lambda r: r["acc"])["city"] if city_rows else None

            # Season breakdown (by month of end_date)
            season_rows = conn.execute(f"""
                SELECT substr(end_date, 6, 2) AS mm,
                       SUM(CASE WHEN was_correct = 1 THEN 1 ELSE 0 END) * 1.0 /
                         NULLIF(SUM(CASE WHEN was_correct IS NOT NULL THEN 1 ELSE 0 END), 0) AS acc,
                       SUM(CASE WHEN was_correct IS NOT NULL THEN 1 ELSE 0 END) AS n
                  FROM trader_forecasts
                 WHERE wallet = ? AND was_correct IS NOT NULL {delta_clause}
              GROUP BY mm
            """, (wallet, *params)).fetchall()
            season_map = {"Winter": [], "Spring": [], "Summer": [], "Fall": []}
            for r in season_rows:
                mm = int(r["mm"]) if r["mm"] else 0
                if mm in (12, 1, 2):    season_map["Winter"].append((r["acc"], r["n"]))
                elif mm in (3, 4, 5):   season_map["Spring"].append((r["acc"], r["n"]))
                elif mm in (6, 7, 8):   season_map["Summer"].append((r["acc"], r["n"]))
                elif mm in (9, 10, 11): season_map["Fall"].append((r["acc"], r["n"]))
            season_best, best_acc = None, -1.0
            for s, entries in season_map.items():
                if not entries:
                    continue
                total_n  = sum(e[1] for e in entries)
                weighted = sum(e[0] * e[1] for e in entries) / total_n if total_n else 0
                if weighted > best_acc:
                    best_acc, season_best = weighted, s

            # Edge-tier breakdown (by |entry_price - 0.5|)
            tier_rows = conn.execute(f"""
                SELECT CASE
                         WHEN abs(entry_price - 0.5) >= 0.30 THEN 'S'
                         WHEN abs(entry_price - 0.5) >= 0.15 THEN 'E'
                         ELSE 'W'
                       END AS tier,
                       SUM(CASE WHEN was_correct = 1 THEN 1 ELSE 0 END) * 1.0 /
                         NULLIF(SUM(CASE WHEN was_correct IS NOT NULL THEN 1 ELSE 0 END), 0) AS acc
                  FROM trader_forecasts
                 WHERE wallet = ? AND was_correct IS NOT NULL {delta_clause}
              GROUP BY tier
            """, (wallet, *params)).fetchall()
            tier_map = {r["tier"]: r["acc"] for r in tier_rows}
            tier_str = " ".join(
                f"{t}:{int(round((tier_map.get(t) or 0) * 100))}%"
                for t in ("W", "E", "S")
                if t in tier_map
            ) or "—"

            results.append({
                "wallet":              wallet,
                "pseudonym":           pseudonym,
                "n_forecasts":         w["n_forecasts"],
                "n_resolved":          w["n_resolved"],
                "accuracy":            accuracy,
                "brier":               w["brier"],
                "best_city":           best_city,
                "worst_city":          worst_city,
                "best_season":         season_best,
                "edge_tier_breakdown": tier_str,
                "last_forecast":       w["last_forecast"],
            })
        return results


def get_untraded_market_candidates(limit: int = 50) -> list[dict]:
    """
    Return distinct market_ids present in trader_positions but NOT yet in
    trader_forecasts AND NOT in our own trades table. These are markets
    tracked traders held that our bot didn't trade — candidates for
    second-pass freeze.

    Ordered by oldest snapshot_at first (stabilizes cycling order).
    """
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT tp.market_id, MIN(tp.snapshot_at) AS oldest_snapshot
              FROM trader_positions tp
             WHERE tp.market_id NOT IN (SELECT market_id FROM trader_forecasts)
               AND tp.market_id NOT IN (SELECT market_id FROM trades WHERE market_id IS NOT NULL)
          GROUP BY tp.market_id
          ORDER BY oldest_snapshot ASC
             LIMIT ?
        """, (limit,)).fetchall()
        return [dict(r) for r in rows]


def get_market_metadata_from_scanner_cache(market_id: str) -> dict | None:
    """
    Look up city / end_date / threshold for a market_id from the latest
    scanner_cache JSON blob. Returns None if not found.
    """
    with get_conn() as conn:
        row = conn.execute(
            "SELECT results_json FROM scanner_cache LIMIT 1"
        ).fetchone()
    if not row:
        return None
    try:
        candidates = json.loads(row["results_json"])
    except Exception:
        return None
    for c in candidates:
        if c.get("condition_id") == market_id or c.get("market_id") == market_id:
            city = (c.get("city") or "").lower()
            end_date = (c.get("end_date") or "")[:10]
            threshold = c.get("threshold")
            if city and end_date:
                return {"city": city, "end_date": end_date, "threshold": threshold}
    return None


# ── Notifications ──────────────────────────────────────────────

def save_notification(severity: str, channel: str, title: str, message: str) -> None:
    ts = datetime.now(timezone.utc).isoformat()
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO notifications (timestamp, severity, channel, title, message) "
            "VALUES (?, ?, ?, ?, ?)",
            (ts, severity, channel, title, message),
        )


def get_notifications(
    severity_in: list[str],
    limit: int = 20,
    since: str | None = None,
) -> list[dict]:
    placeholders = ",".join("?" for _ in severity_in)
    sql = (
        f"SELECT * FROM notifications WHERE severity IN ({placeholders})"
    )
    params: list = list(severity_in)
    if since:
        sql += " AND timestamp > ?"
        params.append(since)
    sql += " ORDER BY timestamp DESC LIMIT ?"
    params.append(limit)
    with get_conn() as conn:
        rows = conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]


# ── Sizing decisions ────────────────────────────────────────────────────────

def write_sizing_decision(decision: dict) -> int:
    """Persist a Kelly sizing snapshot. Caller fills every column except trade_id."""
    decision     = _norm_city(decision)
    cols         = ", ".join(decision.keys())
    placeholders = ", ".join("?" for _ in decision)
    with get_conn() as conn:
        cur = conn.execute(
            f"INSERT INTO sizing_decisions ({cols}) VALUES ({placeholders})",
            list(decision.values()),
        )
        return cur.lastrowid


def link_sizing_decision_to_trade(sizing_id: int, trade_id: int) -> None:
    with get_conn() as conn:
        conn.execute(
            "UPDATE sizing_decisions SET trade_id = ? WHERE id = ?",
            (trade_id, sizing_id),
        )


# ── Decision snapshots (Phase 2 logging foundation) ────────────────────────

def write_decision_snapshot(snapshot: dict) -> int:
    """Persist a Phase 2 shadow snapshot keyed on sizing_decisions.id.

    Caller fills whichever capture columns it has; missing columns stay NULL.
    Required: sizing_decision_id, captured_at.
    """
    cols         = ", ".join(snapshot.keys())
    placeholders = ", ".join("?" for _ in snapshot)
    with get_conn() as conn:
        cur = conn.execute(
            f"INSERT INTO decision_snapshots ({cols}) VALUES ({placeholders})",
            list(snapshot.values()),
        )
        return cur.lastrowid


def write_flip_event(event: dict) -> int:
    """Phase2-06: persist one detected flip event. Caller supplies all fields
    except id; poll_tick_id is optional.
    """
    event = _norm_city(event)
    cols         = ", ".join(event.keys())
    placeholders = ", ".join("?" for _ in event)
    with get_conn() as conn:
        cur = conn.execute(
            f"INSERT INTO flip_events ({cols}) VALUES ({placeholders})",
            list(event.values()),
        )
        return cur.lastrowid


# ── METAR observations ──────────────────────────────────────────────────────

def record_metar_observation(row: dict) -> None:
    """INSERT OR IGNORE a METAR observation. Primary key (icao, observed_at_utc) deduplicates."""
    row = _norm_city(row)
    try:
        with get_conn() as conn:
            conn.execute("""
                INSERT OR IGNORE INTO metar_observations
                    (city, icao, observed_at_utc, observed_temp_c, dewpoint_c,
                     source, raw_report, ensemble_point_mean_c, ensemble_p05_c,
                     ensemble_p50_c, ensemble_p95_c, forecast_run_at_utc, fetched_at_utc)
                VALUES
                    (:city, :icao, :observed_at_utc, :observed_temp_c, :dewpoint_c,
                     :source, :raw_report, :ensemble_point_mean_c, :ensemble_p05_c,
                     :ensemble_p50_c, :ensemble_p95_c, :forecast_run_at_utc, :fetched_at_utc)
            """, row)
    except Exception as e:
        print(f"[db] metar_observations insert failed: {e}")
