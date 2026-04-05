import json
import sqlite3
from datetime import datetime, timezone
from config import DB_PATH, PAPER_STARTING_BALANCE


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
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

        # Safe schema migrations — no-op if column already exists
        _safe_add_column(conn, "trades", "edge_score",         "REAL")
        _safe_add_column(conn, "trades", "liquidity",          "REAL")
        _safe_add_column(conn, "trades", "city",               "TEXT")
        _safe_add_column(conn, "trades", "entry_ensemble_pct",   "REAL")
        _safe_add_column(conn, "trades", "entry_ensemble_yes",  "REAL")
        _safe_add_column(conn, "trades", "entry_ensemble_n",    "INTEGER")
        _safe_add_column(conn, "trades", "current_ensemble_yes","REAL")
        _safe_add_column(conn, "trades", "current_ensemble_n",  "INTEGER")
        _safe_add_column(conn, "trades", "market_url",          "TEXT")
        _safe_add_column(conn, "trades", "threshold",           "TEXT")
        _safe_add_column(conn, "trades", "actual_resolution",   "TEXT")     # 'YES' | 'NO' | NULL
        _safe_add_column(conn, "trades", "forecast_correct",    "INTEGER")  # 1 | 0 | NULL
        _safe_add_column(conn, "trades", "resolution_price",    "REAL")     # exact CLOB midpoint at settlement
        _safe_add_column(conn, "trades", "resolution_fetched_at", "TEXT")   # ISO timestamp of settlement confirmation
        _safe_add_column(conn, "trades", "actual_temperature",  "REAL")     # recorded daily max temp °C (Open-Meteo archive)
        _safe_add_column(conn, "trades", "temperature_delta",   "REAL")     # margin: positive = condition met (YES), negative = missed (NO)
        _safe_add_column(conn, "trades", "volume_24h",           "REAL")     # 24h volume at time of entry (refreshed each poll)


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
            "SELECT current_price, fill_price, shares FROM trades WHERE status = 'open'"
        ).fetchall()

        position_value = sum(
            (t["current_price"] or t["fill_price"]) * t["shares"]
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


def get_all_trades() -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM trades ORDER BY opened_at DESC"
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


def get_trades_today() -> int:
    """Count new positions opened since midnight UTC today."""
    today = datetime.now(timezone.utc).date().isoformat()  # "YYYY-MM-DD"
    with get_conn() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS cnt FROM trades WHERE opened_at >= ?",
            (today,),
        ).fetchone()
        return row["cnt"] if row else 0


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
