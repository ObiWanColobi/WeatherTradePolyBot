"""
Live Executor
──────────────
Real order execution on Polymarket via py-clob-client.
Implements the same BaseExecutor interface as PaperExecutor.
"""
import logging
import math
import os
import time
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler

from py_clob_client_v2.client import ClobClient
from py_clob_client_v2.clob_types import (
    BalanceAllowanceParams, AssetType, OrderArgs, MarketOrderArgs, OrderType,
    TradeParams,
)
from py_clob_client_v2.order_builder.constants import BUY, SELL


# V2 SDK error path logs full HTTP response bodies on transient errors
# (e.g. Cloudflare challenge HTML before the SDK retries through). We:
#   1. Mirror the original full body to logs/clob_v2_errors.log (rotating,
#      forensic record kept for real failures).
#   2. Truncate the version bound for stderr/journalctl so the systemd log
#      stays readable.

_PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
_FORENSIC_LOG_DIR = os.path.join(_PROJECT_ROOT, "logs")
_FORENSIC_LOG_PATH = os.path.join(_FORENSIC_LOG_DIR, "clob_v2_errors.log")

os.makedirs(_FORENSIC_LOG_DIR, exist_ok=True)

_forensic_logger = logging.getLogger("py_clob_client_v2.forensic")
_forensic_logger.setLevel(logging.DEBUG)
_forensic_logger.propagate = False
if not _forensic_logger.handlers:
    _fh = RotatingFileHandler(_FORENSIC_LOG_PATH, maxBytes=5_000_000, backupCount=3)
    _fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    _forensic_logger.addHandler(_fh)


class _TruncateLongBodies(logging.Filter):
    _CAP = 240

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:
            return True
        if len(msg) > self._CAP:
            _forensic_logger.log(record.levelno, msg)
            record.msg = (
                msg[: self._CAP]
                + f" ... [+{len(msg) - self._CAP} chars; full body in logs/clob_v2_errors.log]"
            )
            record.args = ()
        return True


logging.getLogger("py_clob_client_v2").addFilter(_TruncateLongBodies())

from executor.base import BaseExecutor
from chain.claimer import Claimer
from config import (
    POLYMARKET_CLOB_API, WALLET_PRIVATE_KEY,
    WALLET_SIGNATURE_TYPE, WALLET_FUNDER_ADDRESS, WEATHER,
)
from notifications import notify, COLOR_GREEN
import api_monitor
import markets.polymarket as polymarket
import db


# Polymarket uses USDC with 6 decimals on Polygon
_USDC_DECIMALS = 1_000_000

# Polygon mainnet chain ID (module-level so tests can patch it)
CHAIN_ID = 137


class LiveExecutor(BaseExecutor):
    """
    Executes real trades on Polymarket via the CLOB API.

    Fill prices come from actual order matching, not simulated book walks.
    Balance is synced from the exchange on startup and tracked locally
    after each trade (with periodic re-sync).
    """

    def __init__(self):
        self._client: ClobClient | None = None
        self._claimer: Claimer | None = None
        self._claimer_none_alerted: bool = False
        self._oracle_alerted: set[int] = set()
        self._last_clob_call_ts: float = 0.0
        self._init_client(
            WALLET_PRIVATE_KEY,
            CHAIN_ID,
            WALLET_SIGNATURE_TYPE,
            WALLET_FUNDER_ADDRESS,
        )

        # Initialize on-chain claimer for CTF redemption
        rpc_url = WEATHER.get("polygon_rpc_url", "https://polygon-rpc.com")
        try:
            self._claimer = Claimer(rpc_url=rpc_url, private_key=WALLET_PRIVATE_KEY)
            print(f"[live] On-chain claimer initialized (RPC: {rpc_url})")
        except Exception as e:
            print(f"[live] WARNING: Claimer init failed ({e}) — claims will be deferred")
            self._claimer = None

    # ── Client initialization ────────────────────────────────────────────────

    def _init_client(self, private_key: str, chain_id: int,
                     signature_type: int, funder: str):
        """Initialize the CLOB client and verify allowance."""
        kwargs = {
            "host": POLYMARKET_CLOB_API,
            "key": private_key,
            "chain_id": chain_id,
            "signature_type": signature_type,
        }
        if funder:
            kwargs["funder"] = funder

        self._client = ClobClient(**kwargs)
        creds = self._client.create_or_derive_api_key()
        self._client.set_api_creds(creds)

        # Verify allowance — API returns "allowances" dict keyed by contract address
        bal_info = self._client.get_balance_allowance(
            BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
        )
        # Handle both response formats: "allowance" (single) or "allowances" (dict)
        allowances = bal_info.get("allowances", {})
        if allowances:
            has_allowance = any(int(v) > 0 for v in allowances.values())
        else:
            has_allowance = int(bal_info.get("allowance", "0")) > 0

        if not has_allowance:
            print("[live] WARNING: No pUSD allowance detected — first order may fail. "
                  "Run scripts/setup_allowances.py if orders are rejected.")

        balance_pusd = int(bal_info.get("balance", "0")) / _USDC_DECIMALS
        self._cached_balance = balance_pusd
        print(f"[live] CLOB client initialized. Balance: ${balance_pusd:.2f} pUSD")

    def _get_exchange_balance(self) -> float:
        """Fetch current collateral balance (pUSD post-V2; USDC.e legacy).

        Tries CLOB API first; falls back to direct on-chain read summing the
        proxy's USDC.e + pUSD balances when CLOB returns empty/zero/malformed.
        The chain is the ultimate source of truth, so CLOB outages never
        silently overwrite real funds. Discrepancies fire a Discord alert but
        the bot proceeds with the chain value. Caches the resolved value at
        self._cached_balance so the V2 BUY path can pass user_usdc_balance to
        MarketOrderArgs without an extra API hop.
        """
        clob_value: float | None = None
        clob_error: str | None = None
        try:
            bal_info = self._client.get_balance_allowance(
                BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
            )
            raw = bal_info.get("balance")
            if raw is not None and raw != "":
                clob_value = int(raw) / _USDC_DECIMALS
            else:
                clob_error = f"empty balance field in response: {bal_info!r}"
        except Exception as e:
            clob_error = str(e)

        if clob_value is not None and clob_value >= 0.01:
            self._cached_balance = clob_value
            return clob_value

        # CLOB unhealthy or zero — consult the chain
        chain_value: float | None = None
        if self._claimer is not None and WALLET_FUNDER_ADDRESS:
            chain_value = self._claimer.get_usdc_balance(WALLET_FUNDER_ADDRESS)

        if chain_value is not None:
            if clob_error:
                print(f"[live] CLOB balance read failed ({clob_error}) — using on-chain ${chain_value:.2f}")
                notify("warning", "CLOB balance unavailable",
                       f"CLOB read failed; proceeding with on-chain collateral balance.",
                       fields={"On-chain": f"${chain_value:.2f}", "Error": clob_error[:200]})
            elif clob_value is not None and clob_value < 0.01 and chain_value >= 0.01:
                print(f"[live] CLOB returned $0 but chain shows ${chain_value:.2f} — using chain")
                notify("warning", "CLOB/chain balance mismatch",
                       f"CLOB reported $0.00 but on-chain shows ${chain_value:.2f}. Proceeding with chain value.",
                       fields={"CLOB": "$0.00", "On-chain": f"${chain_value:.2f}"})
            else:
                # Both agree (or chain also ~0) — quiet path
                print(f"[live] Balance: ${chain_value:.2f} (chain)")
            self._cached_balance = chain_value
            return chain_value

        # Chain unavailable too — fall through with whatever CLOB returned (incl. real 0)
        if clob_value is not None:
            print(f"[live] Chain fallback unavailable; trusting CLOB value ${clob_value:.2f}")
            self._cached_balance = clob_value
            return clob_value

        # Total blackout — refuse to proceed
        raise RuntimeError(
            f"Cannot read balance: CLOB failed ({clob_error}) and on-chain fallback unavailable."
        )

    # ── CLOB order helpers ───────────────────────────────────────────────────

    def _throttle_clob(self) -> None:
        """Enforce minimum delay between CLOB API calls."""
        delay = WEATHER.get("clob_inter_request_delay", 0.3)
        last_ts = getattr(self, "_last_clob_call_ts", 0.0)
        elapsed = time.time() - last_ts
        if elapsed < delay:
            time.sleep(delay - elapsed)
        self._last_clob_call_ts = time.time()

    def _post_order(self, token_id: str, price: float, size: float,
                    side: str, order_type: str = "FOK") -> dict | None:
        """
        Sign and post an order to the CLOB. Returns response dict or None.

        For BUY orders, uses MarketOrderArgs with USDC amount (maker amount,
        max 2 dp). For SELL orders, uses OrderArgs with shares (maker amount,
        max 2 dp). This avoids floating-point precision issues where
        price * shares produces too many decimal places.
        """
        self._throttle_clob()
        clob_side = BUY if side == "BUY" else SELL
        price = round(price, 2)

        # Sentinel for FOK rejections (thin book, not an API failure)
        _FOK_REJECTED = {"_fok_rejected": True}

        if side == "BUY":
            # BUY: maker_amount = pUSD to spend. Pass as amount, SDK derives shares.
            amount = math.floor(size * price * 100) / 100  # pUSD, truncate to 2 dp
            # V2 fee-aware fill math wants the trader's current balance — pass
            # the cached value from the most recent _get_exchange_balance() so
            # the SDK can size shares correctly on thin books.
            user_bal = getattr(self, "_cached_balance", 0.0) or 0.0
            order_args = MarketOrderArgs(
                token_id=token_id,
                price=price,
                amount=amount,
                side=clob_side,
                user_usdc_balance=float(user_bal),
            )
            print(f"[live] BUY order: price={price}, amount=${amount:.2f}, token={token_id[:12]}...")

            def _do_post():
                try:
                    signed = self._client.create_market_order(order_args)
                    response = self._client.post_order(signed, OrderType.FOK)
                    print(f"[live] Order posted: {response.get('orderID', '?')[:12]}  "
                          f"status={response.get('status', '?')}")
                    return response
                except Exception as e:
                    if "fully filled" in str(e).lower():
                        print(f"[live] FOK rejected — insufficient liquidity")
                        return _FOK_REJECTED
                    raise
        else:
            # SELL: maker_amount = shares to sell, truncate to 2 dp
            size = math.floor(size * 100) / 100
            order_args = OrderArgs(
                token_id=token_id,
                price=price,
                size=size,
                side=clob_side,
            )
            print(f"[live] SELL order: price={price}, shares={size}, token={token_id[:12]}...")

            def _do_post():
                try:
                    signed = self._client.create_order(order_args)
                    ot = OrderType.GTC if order_type == "GTC" else OrderType.FOK
                    response = self._client.post_order(signed, ot)
                    print(f"[live] Order posted ({order_type}): {response.get('orderID', '?')[:12]}  "
                          f"status={response.get('status', '?')}")
                    return response
                except Exception as e:
                    if "fully filled" in str(e).lower():
                        print(f"[live] {order_type} rejected — insufficient liquidity")
                        return _FOK_REJECTED
                    raise

        result = api_monitor.call("clob", _do_post)
        if result is not None and result.get("_fok_rejected"):
            return None
        return result

    def _confirm_fill(self, order_id: str, token_id: str,
                      expected_price: float) -> tuple[float, float, float]:
        """
        Poll order status to confirm fill.

        Returns:
            (fill_price, filled_shares, total_fee)
        """
        saw_matched = False

        for attempt in range(10):
            try:
                self._throttle_clob()
                order = self._client.get_order(order_id)

                if order is None:
                    print(f"[live] Fill check attempt {attempt + 1}: order not found yet")
                    if attempt < 9:
                        time.sleep(3)
                    continue

                status = order.get("status", "").lower()
                print(f"[live] Fill check attempt {attempt + 1}: status={status}")

                if status in ("matched", "filled"):
                    saw_matched = True
                    trades = order.get("associate_trades") or []
                    # trades may be dicts with fill details or just ID strings
                    if trades and isinstance(trades[0], dict):
                        total_cost = 0.0
                        total_shares = 0.0
                        total_fee = 0.0
                        for t in trades:
                            p = float(t.get("price", 0))
                            s = float(t.get("size", 0))
                            f = float(t.get("fee", 0))
                            total_cost += p * s
                            total_shares += s
                            total_fee += f
                        avg_price = total_cost / total_shares if total_shares > 0 else expected_price
                        return avg_price, total_shares, total_fee

                    # Use order-level fields (trade IDs only, or no details yet)
                    matched = float(order.get("size_matched", 0))
                    if matched > 0:
                        price = float(order.get("price", expected_price))
                        # Best-effort fee lookup from trade history. /trades returns
                        # actual fees per fill; size_matched alone doesn't carry them.
                        fee = 0.0
                        try:
                            details = self._lookup_fill_details(token_id, order_id)
                            if details is not None:
                                fee = details[2]
                        except Exception as e:
                            print(f"[live] fee lookup failed for {order_id[:12]}: {e}")
                        return price, matched, fee

                    # Status is matched but no details yet — keep polling
                    if attempt < 9:
                        time.sleep(3)
                    continue

                if status == "delayed":
                    # CLOB hasn't processed yet — keep polling
                    if attempt < 9:
                        time.sleep(3)
                    continue

                if status in ("cancelled", "expired", "dead", "canceled"):
                    return 0.0, 0.0, 0.0

            except Exception as e:
                print(f"[live] Fill check attempt {attempt + 1} failed: {e}")

            if attempt < 9:
                time.sleep(3)

        # Order showed matched/filled but trade details never populated.
        # Fall back to get_trades() to find actual fills on this token.
        if saw_matched:
            result = self._lookup_fill_details(token_id, order_id)
            if result is not None:
                return result
            # Last resort: FOK matched = filled at the limit price.
            # Use order-level price and amount to derive shares.
            print(f"[live] Order {order_id[:12]} matched but no trade details — "
                  f"using limit price {expected_price}")
            try:
                order = self._client.get_order(order_id)
                if order:
                    orig_amount = float(order.get("original_size", 0) or
                                        order.get("size", 0) or 0)
                    if orig_amount > 0:
                        return expected_price, orig_amount, 0.0
            except Exception:
                pass

        # Truly unfilled
        return 0.0, 0.0, 0.0

    def _lookup_fill_details(self, token_id: str, order_id: str
                             ) -> tuple[float, float, float] | None:
        """
        Query CLOB trade history for fills matching this order_id.
        Returns (avg_price, total_shares, total_fee) or None if no fills found.
        Side-agnostic — the /trades endpoint returns both buys and sells, and we
        match strictly by order_id.
        """
        try:
            self._throttle_clob()
            result = self._client.get_trades(
                TradeParams(asset_id=token_id)
            )
        except Exception as e:
            print(f"[live] Trade history lookup failed for {token_id[:12]}: {e}")
            return None

        trades_list = result if isinstance(result, list) else (
            result.get("data", []) if isinstance(result, dict) else []
        )

        total_cost = 0.0
        total_shares = 0.0
        total_fee = 0.0

        for t in trades_list:
            # Match by order_id if available
            t_order = t.get("order_id") or t.get("orderId", "")
            if t_order and t_order == order_id:
                price = float(t.get("price", 0))
                size = float(t.get("size", 0))
                fee = float(t.get("fee", 0))
                total_cost += price * size
                total_shares += size
                total_fee += fee

        if total_shares > 0:
            avg_price = total_cost / total_shares
            print(f"[live] Found fills via trade history: "
                  f"{total_shares:.2f} shares @ {avg_price:.4f}")
            return avg_price, total_shares, total_fee

        return None

    def _cancel_order(self, order_id: str):
        """Cancel an open order. Best-effort — logs but doesn't raise."""
        try:
            self._client.cancel(order_id)
            print(f"[live] Cancelled order {order_id[:12]}")
        except Exception as e:
            print(f"[live] Cancel failed for {order_id[:12]}: {e}")

    # ── Restart reconciliation ───────────────────────────────────────────────

    def reconcile_positions(self):
        """
        On restart, sync with exchange state:
        1. Cancel any leftover open orders (prevent ghost fills while bot is down)
        2. Update DB balance to match exchange USDC balance
        3. Fetch actual positions from Polymarket and reconcile with DB
        4. Import orphaned positions (exist on exchange but not in DB)
        5. Auto-close stale DB positions (in DB but gone from exchange)
        """
        # ── Cancel leftover orders ───────────────────────────────────────────
        # Orders left on the book from a previous session can fill while the
        # bot is down, creating orphaned positions and balance mismatches.
        self._cancel_all_open_orders()

        # ── Balance sync ─────────────────────────────────────────────────────
        exchange_balance = self._get_exchange_balance()
        db_balance = db.get_balance()

        if abs(exchange_balance - db_balance) > 0.01:
            print(f"[live] Balance sync: DB=${db_balance:.2f} → Exchange=${exchange_balance:.2f}")
            db.set_balance(exchange_balance)
            db.record_account_value()
        else:
            print(f"[live] Balance in sync: ${exchange_balance:.2f}")

        # ── Position reconciliation ──────────────────────────────────────────
        # Fetch what Polymarket says we own
        wallet_addr = WALLET_FUNDER_ADDRESS or self._client.get_address()
        exchange_positions = polymarket.get_wallet_positions(wallet_addr)

        if exchange_positions is None:
            exchange_positions = []

        # Build lookup of exchange positions by token_id
        exchange_by_token = {}
        for p in exchange_positions:
            tid = p.get("token_id", "")
            if tid and p.get("size", 0) > 0.01:
                exchange_by_token[tid] = p

        # Include claim_pending trades when matching — they still hold the
        # on-chain position until the redeemPositions() tx confirms, so they
        # must NOT be treated as orphaned or stale.
        open_db_trades    = db.get_open_trades()
        pending_db_trades = db.get_pending_claims()
        tracked_token_ids = {
            t.get("token_id", "")
            for t in (*open_db_trades, *pending_db_trades)
            if t.get("token_id")
        }

        # Detect any lingering duplicate token_ids across active rows before
        # we touch the exchange. If this fires, the partial unique index in
        # init_db() should have already blocked the dup — getting here means
        # something wrote around the index or the DB predates it.
        dup_tokens = _find_duplicate_active_tokens(
            (*open_db_trades, *pending_db_trades)
        )
        if dup_tokens:
            print(
                f"[live] WARNING: {len(dup_tokens)} duplicate token_id(s) across "
                f"active trades — not importing or closing anything until resolved:"
            )
            for tok, ids in dup_tokens.items():
                print(f"         token={tok[:20]}… ids={ids}")
            notify(
                "critical",
                "Duplicate active trades detected",
                "Bot found multiple open/claim_pending rows sharing a token_id. "
                "Reconciliation is halted to avoid double-counting. Clean the DB "
                "(keep the lowest id per token) and restart.",
                fields={"Duplicate tokens": str(len(dup_tokens))},
            )
            return

        # Check for orphaned exchange positions (not tracked by any active row)
        orphaned = [
            pos for tid, pos in exchange_by_token.items()
            if tid not in tracked_token_ids
        ]

        if orphaned:
            print(f"[live] Found {len(orphaned)} position(s) on exchange not in DB — importing:")
            for pos in orphaned:
                self._import_orphaned_position(pos)

        # Auto-close stale DB positions (in DB but gone from exchange).
        # Only consider status='open' — claim_pending rows are expected to
        # disappear from the exchange when the redeem tx confirms, and the
        # claim flow handles their status transition itself.
        for t in open_db_trades:
            tid = t.get("token_id", "")
            if tid and tid not in exchange_by_token:
                self._close_stale_position(t)

        # Summary
        db_trades = db.get_open_trades()  # re-fetch after imports
        if not db_trades:
            print("[live] No open positions to resume.")
        else:
            print(f"[live] Resuming {len(db_trades)} open position(s):")
            for t in db_trades:
                print(f"       {t['direction']:3s}  {t['market_name'][:60]}  "
                      f"fill={t['fill_price']:.4f}  size=${t['size_usdc']:.2f}")

    def _import_orphaned_position(self, pos: dict):
        """Import a position that exists on exchange but not in DB.

        Enriches the trade record with city, threshold, market URL,
        volume, liquidity, and current ensemble snapshot from the
        Polymarket APIs and forecast layer.
        """
        from layers.layer3_weather import parse_weather_market, WeatherLayer

        token_id = pos.get("token_id", "")
        market_id = pos.get("market_id", "")
        shares = pos.get("size", 0)
        avg_price = pos.get("avg_price", 0)
        current_price = pos.get("current_price", 0)

        # Fetch market details from Gamma API
        market_info = polymarket.get_market_by_id(market_id) if market_id else None
        question   = ""
        end_date   = ""
        market_url = ""
        liquidity  = None
        volume     = None
        yes_price  = 0.5

        if market_info:
            question   = market_info.get("question", "")
            end_date   = market_info.get("end_date", "")
            market_url = market_info.get("market_url", "")
            liquidity  = market_info.get("liquidity")
            volume     = market_info.get("volume")
            yes_price  = market_info.get("price") or 0.5

        # ── Guard: skip stubs for closed/resolved markets ────────────────────
        # Orphaned tokens from already-closed markets should be redeemed
        # on-chain (or manually), not re-hydrated as open trades. Re-importing
        # them causes infinite exit loops because there's no live orderbook.
        # When market_info is None (Gamma outage) we fall through to preserve
        # the legitimate "short outage, resume a new position" case.
        is_resolved   = bool(market_info and market_info.get("resolved"))
        hours_left    = _hours_until(end_date) if end_date else None
        is_past_close = hours_left is not None and hours_left < -0.5  # 30-min grace
        if is_resolved or is_past_close:
            reason = "resolved" if is_resolved else f"past close ({hours_left:.1f}h)"
            label  = (question or market_id or token_id or "?")[:50]
            print(f"       SKIPPED: orphaned wallet stub — {reason}  "
                  f"{label}  shares={shares:.2f}  avg=${avg_price:.4f}")
            notify(
                "warning",
                "Orphaned wallet stub skipped",
                f"Wallet holds {shares:.2f} shares for a {reason} market — "
                f"not importing as an open trade. Redeem manually on "
                f"Polymarket if the tokens are worth anything.",
                fields={
                    "Market": question or market_id or "?",
                    "Shares": f"{shares:.2f}",
                    "Avg $":  f"{avg_price:.4f}",
                },
            )
            return

        # Determine direction from outcome field or token matching
        outcome = pos.get("outcome", "").upper()
        if outcome in ("YES", "NO"):
            direction = outcome
        else:
            if market_info and token_id == market_info.get("token_id"):
                direction = "YES"
            else:
                direction = "NO"

        # Parse city and threshold from the question string
        city = None
        threshold_str = None
        parsed = parse_weather_market(question) if question else None
        if parsed:
            city = parsed["city_raw"].title()
            if parsed["market_type"] == "threshold":
                op = ">=" if parsed["direction"] == "above" else "<="
                threshold_str = f"{op}{parsed['threshold']:.0f}{parsed['unit']}"

        # Snapshot current ensemble as entry baseline
        ens_pct = None
        ens_yes = None
        ens_n   = None
        if market_info and question:
            try:
                layer = WeatherLayer()
                scan_data = layer.scan({
                    "question": question,
                    "end_date": end_date,
                    "price":    yes_price,
                })
                if scan_data and scan_data.get("ensemble_n", 0) >= 10:
                    ens_yes = scan_data.get("yes_ensemble")
                    ens_n   = scan_data.get("ensemble_n")
                    ens_pct = ens_yes / ens_n if ens_yes is not None and ens_n else None
            except Exception as e:
                print(f"       [reconcile] ensemble snapshot failed: {e}")

        entry_price = yes_price
        model_prob = None
        edge_score = None

        size_usdc = shares * avg_price if avg_price > 0 else shares * current_price

        trade = {
            "market_id":       market_id,
            "market_name":     question,
            "token_id":        token_id,
            "end_date":        end_date,
            "direction":       direction,
            "size_usdc":       round(size_usdc, 2),
            "shares":          shares,
            "entry_price":     entry_price,
            "fill_price":      avg_price,
            "current_price":   current_price,
            "peak_price":      current_price,
            "exit_price":      None,
            "opened_at":       datetime.now(timezone.utc).isoformat(),
            "hours_to_close_at_entry": _hours_until(end_date),
            "closed_at":       None,
            "status":          "open",
            "pnl":             None,
            "pnl_pct":         None,
            "layers_used":     "reconciled",
            "estimated_prob":  model_prob,
            "edge_score":      edge_score,
            "liquidity":       liquidity,
            "volume_24h":      volume,
            "city":            city,
            "entry_ensemble_pct": ens_pct,
            "entry_ensemble_yes": ens_yes,
            "entry_ensemble_n":   ens_n,
            "market_url":      market_url,
            "threshold":       threshold_str,
            "bleed_rungs_hit": 0,
            "exit_reason":     None,
            "order_id":        None,
            "fee_usdc":        0.0,
        }

        db.insert_trade(trade)
        city_display = city or "?"
        ens_str = f"{int(ens_yes)}/{ens_n}" if ens_yes is not None and ens_n else "?"
        print(f"       IMPORTED: {direction:3s}  {city_display:<12}  {threshold_str or '?':<8}  "
              f"ens={ens_str}  {question[:40]}  shares={shares:.2f}  avg=${avg_price:.4f}")

    def _close_stale_position(self, trade: dict):
        """
        Close a DB position that no longer exists on the exchange.

        Resolution order:
          1. Resolved-market check — if the market resolved while we were
             offline and the position was redeemed (auto-claim, sibling, or
             manual), settle at $1/$0 against the actual outcome. Balance is
             not credited here; startup balance_sync already absorbed the
             on-chain redeem proceeds.
          2. CLOB SELL-fill lookup — for genuinely sold-externally positions.
          3. Midpoint estimate — last-resort fallback for unresolved markets.
        """
        tid      = trade.get("token_id", "")
        trade_id = trade["id"]
        name     = trade["market_name"][:50]
        cost     = trade.get("size_usdc", 0)
        market_id = trade.get("market_id", "")
        direction = (trade.get("direction") or "YES").upper()

        # Step 1: resolved-market path. Avoids the midpoint-returns-zero
        # phantom loss when a winning position gets redeemed externally.
        resolution = polymarket.get_resolution_status(market_id) if market_id else None
        if resolution and resolution.get("resolved"):
            yes_price    = resolution.get("yes_price", 0.0)
            resolved_yes = yes_price >= 0.5
            won = (direction == "YES" and resolved_yes) or \
                  (direction == "NO" and not resolved_yes)
            exit_price = 1.0 if won else 0.0
            proceeds   = trade.get("shares", 0) * exit_price
            pnl        = proceeds - cost
            pnl_pct    = (pnl / cost * 100) if cost > 0 else 0.0

            db.update_trade(trade_id, {
                "exit_price":             exit_price,
                "closed_at":              datetime.now(timezone.utc).isoformat(),
                "status":                 "closed",
                "pnl":                    pnl,
                "pnl_pct":                pnl_pct,
                "exit_reason":            "resolved (reconciled)",
                "actual_resolution":      "YES" if resolved_yes else "NO",
                "forecast_correct":       1 if won else 0,
                "resolution_price":       1.0 if resolved_yes else 0.0,
                "claim_status":           "claimed_externally" if won else None,
                "hours_to_close_at_exit": _hours_until(trade.get("end_date")),
            })
            db.record_account_value()

            outcome = "WIN " if won else "LOSS"
            print(f"[live] AUTO-CLOSED trade #{trade_id} ({name}) — {outcome}")
            print(f"       P&L: ${pnl:+.2f} ({pnl_pct:+.1f}%)  source=resolved market")

            notify("info" if won else "warning",
                   "Position Reconciled (Resolved)",
                   f"Trade #{trade_id} resolved while offline — settled at "
                   f"${exit_price:.2f}/share",
                   fields={"Market": name,
                           "P&L": f"${pnl:+.2f}",
                           "Outcome": "WIN" if won else "LOSS"},
                   color=COLOR_GREEN if won else None)
            return

        # Step 2: try CLOB trade history for actual sell fills
        exit_price, proceeds, fee = self._lookup_sell_fills(tid, trade)

        if proceeds is not None:
            pnl     = proceeds - cost - fee
            pnl_pct = (pnl / cost * 100) if cost > 0 else 0.0
            source  = "trade history"
        else:
            # Step 3: midpoint estimate (unresolved markets only)
            mid = polymarket.get_midpoint(tid)
            if mid is not None and mid > 0:
                exit_price = mid
                proceeds   = trade.get("shares", 0) * mid
            else:
                exit_price = 0.0
                proceeds   = 0.0
            fee     = 0.0
            pnl     = proceeds - cost
            pnl_pct = (pnl / cost * 100) if cost > 0 else 0.0
            source  = "midpoint estimate"

        db.update_trade(trade_id, {
            "exit_price":             exit_price,
            "closed_at":              datetime.now(timezone.utc).isoformat(),
            "status":                 "closed",
            "pnl":                    pnl,
            "pnl_pct":                pnl_pct,
            "exit_reason":            "sold externally (reconciled)",
            "hours_to_close_at_exit": _hours_until(trade.get("end_date")),
            "fee_usdc":               (trade.get("fee_usdc") or 0) + fee,
        })
        db.record_account_value()

        print(f"[live] AUTO-CLOSED trade #{trade_id} ({name})")
        print(f"       P&L: ${pnl:+.2f} ({pnl_pct:+.1f}%)  source={source}")

        notify("warning", "Position Reconciled",
               f"Trade #{trade_id} gone from exchange — auto-closed",
               fields={"Market": name, "P&L": f"${pnl:+.2f}", "Source": source},
               color=COLOR_GREEN)

    def _lookup_sell_fills(self, token_id: str, trade: dict
                           ) -> tuple[float | None, float | None, float]:
        """
        Query CLOB trade history for sell fills on this token.

        Returns (avg_exit_price, total_proceeds, total_fee) or (None, None, 0)
        if no relevant sells found.
        """
        try:
            self._throttle_clob()
            result = self._client.get_trades(
                TradeParams(asset_id=token_id)
            )
        except Exception as e:
            print(f"[live] Could not fetch trade history for {token_id[:12]}: {e}")
            return None, None, 0.0

        # result may be a list or have a "data" key
        trades_list = result if isinstance(result, list) else (
            result.get("data", []) if isinstance(result, dict) else []
        )

        # Filter to SELL trades that happened after the position was opened
        opened_at = trade.get("opened_at", "")
        total_proceeds = 0.0
        total_shares   = 0.0
        total_fee      = 0.0

        for t in trades_list:
            side = (t.get("side") or t.get("type", "")).upper()
            if side != "SELL":
                continue

            # Only count sells after position open time
            trade_ts = t.get("timestamp") or t.get("created_at", "")
            if opened_at and trade_ts and str(trade_ts) < opened_at:
                continue

            price  = float(t.get("price", 0))
            size   = float(t.get("size", 0))
            fee    = float(t.get("fee", 0))
            total_proceeds += price * size
            total_shares   += size
            total_fee      += fee

        if total_shares <= 0:
            return None, None, 0.0

        avg_price = total_proceeds / total_shares
        return avg_price, total_proceeds, total_fee

    # ── Mid-loop position sync ────────────────────────────────────────────────

    def sync_positions_with_exchange(self):
        """
        Periodic check: detect positions gone from the exchange and close them.

        Handles two cases:
        1. status='open' trades whose token vanished (manually sold, resolved
           externally, or resolver missed it) → close via _close_stale_position
        2. status='claim_pending' trades whose token vanished (user claimed
           manually on Polymarket) → close and credit balance
        """
        wallet_addr = WALLET_FUNDER_ADDRESS or self._client.get_address()
        exchange_positions = polymarket.get_wallet_positions(wallet_addr)
        if not exchange_positions:
            # API failure returns [] — skip sync rather than mass-closing trades.
            # A wallet with genuinely zero positions but open DB trades is also
            # ambiguous, so always skip when empty.
            print("[sync] No exchange positions returned — skipping sync")
            return

        exchange_tokens = {
            p.get("token_id", "")
            for p in exchange_positions
            if p.get("token_id") and p.get("size", 0) > 0.01
        }

        now = datetime.now(timezone.utc)

        # Case 1: Open trades gone from exchange
        for trade in db.get_open_trades():
            tid = trade.get("token_id", "")
            if tid and tid not in exchange_tokens:
                print(f"[sync] Open trade #{trade['id']} gone from exchange — closing")
                self._close_stale_position(trade)

        # Case 2: Claim-pending trades gone from exchange (manually claimed)
        for trade in db.get_pending_claims():
            tid = trade.get("token_id", "")
            if tid and tid not in exchange_tokens:
                proceeds = trade.get("shares", 0) * 1.0  # winning = $1/share
                db.update_balance(proceeds)
                db.update_trade(trade["id"], {
                    "status":       "closed",
                    "closed_at":    now.isoformat(),
                    "claim_status": "claimed_externally",
                })
                db.record_account_value()
                name = trade.get("market_name", "unknown")[:60]
                print(f"[sync] Claim-pending trade #{trade['id']} gone from "
                      f"exchange — closed as externally claimed (+${proceeds:.2f})")
                notify("info", "External Claim Detected",
                       f"Trade #{trade['id']} was claimed outside the bot.",
                       fields={"Market": name, "Proceeds": f"${proceeds:.2f}"},
                       color=COLOR_GREEN)

    def _cancel_all_open_orders(self):
        """Cancel all open orders on startup to prevent ghost fills."""
        try:
            self._throttle_clob()
            # V2 SDK: cancel_all() takes no args and cancels every open order.
            # (V1's cancel_market_orders() did the same; V2 renamed it and
            # repurposed cancel_market_orders to require an OrderMarketCancelParams.)
            result = self._client.cancel_all()
            if result:
                cancelled = result if isinstance(result, list) else result.get("canceled", [])
                if cancelled:
                    print(f"[live] Startup: cancelled {len(cancelled)} leftover order(s)")
                    return
            print("[live] Startup: no leftover orders to cancel.")
        except Exception as e:
            print(f"[live] Startup: cancel open orders failed: {e}")

    # ── Order placement ──────────────────────────────────────────────────────

    def place_order(self, market: dict, direction: str, size_usdc: float, estimate: dict) -> bool:
        balance = db.get_balance()
        if size_usdc > balance:
            print(f"[live] Skipping — insufficient balance ${balance:.2f} < ${size_usdc:.2f}")
            return False

        if db.get_open_trade_for_market(market["id"]):
            return False

        # Select token — YES buys YES token, NO buys NO token
        if direction == "YES":
            token_id = market.get("token_id")
        else:
            token_id = market.get("no_token_id") or market.get("token_id")

        if not token_id:
            return False

        # Get current best ask to set limit price
        mid = polymarket.get_midpoint(token_id)
        if mid is None or mid <= 0:
            print(f"[live] Skipping — no midpoint for {market.get('question', '')[:50]}")
            return False

        # Place a FOK order at slightly above mid for fast fill
        # CLOB requires: price max 2 decimals, shares (taker amount) max 4 decimals
        limit_price = round(min(mid + 0.01, 0.99), 2)
        shares = math.floor(size_usdc / limit_price * 100) / 100  # truncate to 2 dp (CLOB requirement)

        order_response = self._post_order(token_id, limit_price, shares, "BUY")
        if order_response is None:
            return False

        order_id = order_response.get("orderID", "")

        # Poll for fill confirmation
        fill_price, filled_shares, fee = self._confirm_fill(order_id, token_id, limit_price)
        if filled_shares <= 0:
            print(f"[live] Order {order_id[:12]} not filled — cancelling")
            self._cancel_order(order_id)
            return False

        filled_usdc = filled_shares * fill_price

        # Calculate display values
        yes_price = market.get("price", 0.5)
        model_prob = estimate.get("probability", 0)
        if direction == "YES":
            token_mid = yes_price
            edge_display = model_prob - yes_price
        else:
            token_mid = 1.0 - yes_price
            edge_display = yes_price - model_prob
        slippage = abs(fill_price - token_mid)

        trade = {
            "market_id":       market["id"],
            "market_name":     market.get("question", ""),
            "token_id":        token_id,
            "end_date":        market.get("end_date"),
            "direction":       direction,
            "size_usdc":       filled_usdc,
            "shares":          filled_shares,
            "entry_price":     market.get("price", 0.5),
            "fill_price":      fill_price,
            "current_price":   fill_price,
            "peak_price":      fill_price,
            "exit_price":      None,
            "opened_at":       datetime.now(timezone.utc).isoformat(),
            "hours_to_close_at_entry": _hours_until(market.get("end_date")),
            "closed_at":       None,
            "status":          "open",
            "pnl":             None,
            "pnl_pct":         None,
            "layers_used":     ", ".join(estimate.get("sources", [])),
            "estimated_prob":  estimate.get("probability"),
            "edge_score":      estimate.get("edge_score"),
            "liquidity":       market.get("liquidity"),
            "volume_24h":      market.get("volume"),
            "city":            market.get("city"),
            "entry_ensemble_pct": estimate.get("entry_ensemble_pct"),
            "entry_ensemble_yes": estimate.get("entry_ensemble_yes"),
            "entry_ensemble_n":   estimate.get("entry_ensemble_n"),
            "market_url":      market.get("market_url", ""),
            "threshold":       estimate.get("threshold"),
            "bleed_rungs_hit": 0,
            "exit_reason":     None,
            "order_id":        order_id,
            "fee_usdc":        fee,
        }

        db.insert_trade(trade)
        db.update_balance(-filled_usdc)
        db.record_account_value()

        try:
            snap = db.snapshot_trader_forecasts_on_entry(
                market_id=trade["market_id"],
                city=trade.get("city") or "",
                end_date=(trade.get("end_date") or "")[:10],
                threshold=trade.get("threshold"),
            )
            if snap > 0:
                print(f"             snapshot: {snap} trader forecast(s) frozen at entry")
        except Exception as e:
            print(f"             [warn] trader-forecast snapshot failed: {e}")

        print(f"[live] OPEN  {direction:3s}  {market.get('question', '')[:55]}")
        print(f"             Size: ${filled_usdc:.2f}  Fill: {fill_price:.4f}  "
              f"Slippage: {slippage:.4f}  Edge: {edge_display:+.3f}  Fee: ${fee:.2f}")

        notify("info", "Order Filled",
               f"Bought {direction} on {market.get('question', '')[:60]}",
               fields={"Price": f"${fill_price:.4f}",
                        "Size": f"${filled_usdc:.2f}",
                        "Shares": f"{filled_shares:.1f}"},
               color=COLOR_GREEN)
        return True

    # ── Extended position add-on ─────────────────────────────────────────────

    def place_extended_order(self, market: dict, direction: str, size_usdc: float,
                              estimate: dict, parent_trade_id: int, leg_number: int):
        """Place an add-on leg for an existing extended position via CLOB."""
        balance = db.get_balance()
        if size_usdc > balance:
            print(f"[live] Skipping add-on — insufficient balance ${balance:.2f} < ${size_usdc:.2f}")
            return

        token_id = market.get("token_id")
        if not token_id:
            return

        mid = polymarket.get_midpoint(token_id)
        if mid is None or mid <= 0:
            print(f"[live] Skipping add-on — no midpoint")
            return

        # Slippage check before ordering
        max_slippage = WEATHER.get("entry_max_slippage_pct", 0.05)
        limit_price = round(min(mid + 0.01, 0.99), 2)

        # Try full size, then reduce if slippage too high
        for frac in (1.0, 0.75, 0.50, 0.25):
            attempt_usdc = size_usdc * frac
            if attempt_usdc < 1.0:
                print(f"[live] Skipping add-on — size too small after slippage reduction")
                return
            slippage_pct = abs(limit_price - mid) / mid if mid > 0 else 0
            if slippage_pct <= max_slippage:
                size_usdc = attempt_usdc
                break
        else:
            print(f"[live] Skipping add-on — slippage too high even at 25% size")
            return

        shares = math.floor(size_usdc / limit_price * 100) / 100  # truncate to 2 dp (CLOB requirement)

        order_response = self._post_order(token_id, limit_price, shares, "BUY")
        if order_response is None:
            return

        order_id = order_response.get("orderID", "")
        fill_price, filled_shares, fee = self._confirm_fill(order_id, token_id, limit_price)

        if filled_shares <= 0:
            print(f"[live] Add-on order {order_id[:12]} not filled — cancelling")
            self._cancel_order(order_id)
            return

        filled_usdc = filled_shares * fill_price
        yes_price = market.get("price", 0.5)
        token_mid = yes_price if direction == "YES" else 1.0 - yes_price
        slippage = abs(fill_price - token_mid)

        trade = {
            "market_id":       market["id"],
            "market_name":     market.get("question", ""),
            "token_id":        token_id,
            "end_date":        market.get("end_date"),
            "direction":       direction,
            "size_usdc":       filled_usdc,
            "shares":          filled_shares,
            "entry_price":     market.get("price", 0.5),
            "fill_price":      fill_price,
            "current_price":   fill_price,
            "peak_price":      fill_price,
            "exit_price":      None,
            "opened_at":       datetime.now(timezone.utc).isoformat(),
            "hours_to_close_at_entry": _hours_until(market.get("end_date")),
            "closed_at":       None,
            "status":          "open",
            "pnl":             None,
            "pnl_pct":         None,
            "layers_used":     ", ".join(estimate.get("sources", [])),
            "estimated_prob":  estimate.get("probability"),
            "edge_score":      estimate.get("edge_score"),
            "liquidity":       market.get("liquidity"),
            "volume_24h":      market.get("volume"),
            "city":            market.get("city"),
            "entry_ensemble_pct": estimate.get("entry_ensemble_pct"),
            "entry_ensemble_yes": estimate.get("entry_ensemble_yes"),
            "entry_ensemble_n":   estimate.get("entry_ensemble_n"),
            "market_url":      market.get("market_url", ""),
            "threshold":       estimate.get("threshold"),
            "bleed_rungs_hit": 0,
            "exit_reason":     None,
            "parent_trade_id": parent_trade_id,
            "leg_number":      leg_number,
            "order_id":        order_id,
            "fee_usdc":        fee,
        }

        db.insert_trade(trade)
        db.update_balance(-filled_usdc)
        db.record_account_value()

        print(f"[live] ADD-ON Leg {leg_number}  {direction:3s}  {market.get('question', '')[:50]}")
        print(f"             Size: ${filled_usdc:.2f}  Fill: {fill_price:.4f}  "
              f"Slippage: {slippage:.4f}  Fee: ${fee:.2f}")

    # ── Position closing ─────────────────────────────────────────────────────

    def close_full(self, trade: dict, reason: str):
        """Sell all shares of a position on the CLOB."""
        token_id = trade.get("token_id")
        shares = trade.get("shares", 0)
        if not token_id or shares <= 0:
            return

        mid = polymarket.get_midpoint(token_id)
        if mid is None or mid <= 0:
            print(f"[live] Cannot close — no midpoint for {trade['market_name'][:50]}")
            return

        # Aggressive sell — limit price slightly below mid for fast fill
        sell_price = round(max(mid - 0.01, 0.01), 2)

        order_response = self._post_order(token_id, sell_price, shares, "SELL")
        if order_response is None:
            # Fallback: try at 1 cent (effectively market sell)
            print(f"[live] Retrying close at $0.01 floor...")
            order_response = self._post_order(token_id, 0.01, shares, "SELL")
            if order_response is None:
                print(f"[live] FAILED to close {trade['market_name'][:50]} — manual intervention needed")
                return

        order_id = order_response.get("orderID", "")
        fill_price, filled_shares, fee = self._confirm_fill(order_id, token_id, sell_price)

        if filled_shares <= 0:
            print(f"[live] Close order not filled — {trade['market_name'][:50]}")
            self._cancel_order(order_id)
            return

        exit_price = fill_price
        proceeds = filled_shares * exit_price
        cost = trade["size_usdc"]
        pnl = proceeds - cost - fee
        pnl_pct = (pnl / cost * 100) if cost > 0 else 0.0

        db.update_balance(proceeds)
        db.update_trade(trade["id"], {
            "exit_price":             exit_price,
            "closed_at":              datetime.now(timezone.utc).isoformat(),
            "status":                 "closed",
            "pnl":                    pnl,
            "pnl_pct":                pnl_pct,
            "exit_reason":            _append_reason(trade.get("exit_reason"), reason),
            "hours_to_close_at_exit": _hours_until(trade.get("end_date")),
            "order_id":               order_id,
            "fee_usdc":               (trade.get("fee_usdc") or 0) + fee,
        })
        db.record_account_value()

        print(f"[live] CLOSE {trade['market_name'][:55]}")
        print(f"             Reason: {reason}  P&L: ${pnl:+.2f} ({pnl_pct:+.1f}%)  Fee: ${fee:.2f}")

        notify("info", "Position Closed",
               f"Exited {trade.get('market_name', 'unknown')[:60]} — {reason}",
               fields={"P&L": f"${pnl:+.2f}",
                        "Reason": reason},
               color=COLOR_GREEN)

    def close_partial(self, trade: dict, sell_pct: float, reason: str):
        """Sell a fraction of shares. If remainder is tiny, close fully."""
        shares_to_sell = trade["shares"] * sell_pct
        if shares_to_sell < 0.01:
            return

        remaining_shares = trade["shares"] - shares_to_sell
        if remaining_shares < 0.01:
            self.close_full(trade, reason)
            return

        token_id = trade.get("token_id")
        if not token_id:
            return

        mid = polymarket.get_midpoint(token_id)
        if mid is None or mid <= 0:
            return

        sell_price = round(max(mid - 0.01, 0.01), 2)
        order_response = self._post_order(token_id, sell_price, shares_to_sell, "SELL")
        if order_response is None:
            return

        order_id = order_response.get("orderID", "")
        fill_price, filled_shares, fee = self._confirm_fill(order_id, token_id, sell_price)
        if filled_shares <= 0:
            self._cancel_order(order_id)
            return

        proceeds = filled_shares * fill_price
        partial_pnl = proceeds - (filled_shares * trade["fill_price"]) - fee

        db.update_balance(proceeds)
        db.record_account_value()

        actual_remaining = trade["shares"] - filled_shares
        remaining_usdc = actual_remaining * trade["fill_price"]

        db.update_trade(trade["id"], {
            "shares":          actual_remaining,
            "size_usdc":       remaining_usdc,
            "bleed_rungs_hit": trade.get("bleed_rungs_hit", 0) + 1,
            "exit_reason":     _append_reason(trade.get("exit_reason"), reason),
            "fee_usdc":        (trade.get("fee_usdc") or 0) + fee,
        })

        print(f"[live] PARTIAL ({sell_pct*100:.0f}%)  {trade['market_name'][:50]}")
        print(f"             Reason: {reason}  Proceeds: ${proceeds:.2f}  "
              f"Partial P&L: ${partial_pnl:+.2f}  Fee: ${fee:.2f}")

    def close_position(self, trade: dict, reason: str):
        """Close all legs of an extended position (or a single trade)."""
        parent_id = trade.get("parent_trade_id") or trade["id"]
        legs = db.get_position_legs(parent_id)
        if not legs:
            self.close_full(trade, reason=reason)
            return
        for leg in legs:
            self.close_full(leg, reason=reason)

    def _reconcile_position_shares(self, legs: list[dict], token_id: str) -> float:
        """
        Reconcile DB-recorded leg shares against the on-chain ERC-1155 CTF balance
        just before posting a sell. Prevents CLOB "not enough balance" rejects when
        DB has drifted higher than actual claimable shares (partial-fill dust,
        sibling-redeem remnants). Mirrors the JIT balance read at the claim path.
        """
        recorded = sum(leg.get("shares", 0) for leg in legs)
        if self._claimer is None:
            return recorded

        try:
            balance_raw = self._claimer.get_token_balance(
                proxy_address=WALLET_FUNDER_ADDRESS, token_id=int(token_id),
            )
        except Exception as e:
            print(f"[live] reconcile: balance read failed ({e}) — using DB shares {recorded:.4f}")
            return recorded

        on_chain = math.floor((balance_raw / 1e6) * 100) / 100

        if on_chain >= recorded:
            return recorded

        name = legs[0].get("market_name", "unknown")[:55] if legs else "unknown"

        if on_chain == 0:
            print(f"[live] RECONCILE {name}  DB={recorded:.4f} → chain=0 — skipping exit "
                  f"(RPC hiccup or already off-chain)")
            return 0.0

        scale = on_chain / recorded
        for leg in legs:
            new_shares = math.floor(leg.get("shares", 0) * scale * 10000) / 10000
            db.update_trade(leg["id"], {"shares": new_shares})
            leg["shares"] = new_shares

        adjusted = sum(leg.get("shares", 0) for leg in legs)
        print(f"[live] RECONCILE {name}  DB={recorded:.4f} → chain={on_chain:.4f} "
              f"(adjusted legs sum={adjusted:.4f})")
        notify("warning", "Position Reconciled",
               f"Exit sizing reduced to match on-chain balance for {name}",
               fields={"DB Shares": f"{recorded:.4f}",
                        "On-Chain": f"{on_chain:.4f}",
                        "Market": name})
        return adjusted

    def initiate_exit(self, trade: dict, reason: str):
        """Post a GTC sell to begin exiting a position. Tracks order across cycles."""
        parent_id = trade.get("parent_trade_id") or trade["id"]
        legs = db.get_position_legs(parent_id)
        if not legs:
            legs = [trade]

        token_id = trade.get("token_id")
        if not token_id:
            print(f"[live] Cannot initiate exit — no token_id for {trade['market_name'][:50]}")
            return

        total_shares = self._reconcile_position_shares(legs, token_id)
        if total_shares <= 0:
            return

        best_bid = polymarket.get_best_bid(token_id)
        if best_bid is None:
            print(f"[live] Skip exit — orderbook unavailable for {trade['market_name'][:50]}")
            return
        if best_bid > 0.01:
            sell_price = round(max(best_bid - 0.01, 0.01), 2)
        else:
            sell_price = 0.01

        order_response = self._post_order(token_id, sell_price, total_shares, "SELL", order_type="GTC")
        if order_response is None:
            print(f"[live] GTC sell failed for {trade['market_name'][:50]} — will retry next cycle")
            return

        order_id = order_response.get("orderID", "")
        now_iso = datetime.now(timezone.utc).isoformat()

        parent_trade = next((l for l in legs if l["id"] == parent_id), legs[0])
        db.update_trade(parent_trade["id"], {
            "exit_order_id":        order_id,
            "exit_order_price":     sell_price,
            "exit_order_placed_at": now_iso,
        })

        for leg in legs:
            db.update_trade(leg["id"], {
                "status":      "exit_pending",
                "exit_reason": _append_reason(leg.get("exit_reason"), reason),
            })

        print(f"[live] EXIT INITIATED {trade['market_name'][:55]}")
        print(f"             GTC sell: {total_shares:.2f} shares @ ${sell_price:.2f}  "
              f"order={order_id[:12]}  reason={reason}")

    def _settle_exit(self, parent_trade: dict, legs: list[dict],
                     fill_price: float, filled_shares: float, fee: float):
        """Settle a filled GTC exit order across all legs proportionally."""
        total_shares = sum(leg.get("shares", 0) for leg in legs)
        if total_shares <= 0:
            return

        total_proceeds = filled_shares * fill_price
        now_iso = datetime.now(timezone.utc).isoformat()

        for leg in legs:
            leg_share_frac = leg.get("shares", 0) / total_shares
            leg_proceeds = total_proceeds * leg_share_frac
            leg_fee = fee * leg_share_frac
            leg_cost = leg["size_usdc"]
            leg_pnl = leg_proceeds - leg_cost - leg_fee
            leg_pnl_pct = (leg_pnl / leg_cost * 100) if leg_cost > 0 else 0.0

            db.update_trade(leg["id"], {
                "exit_price":             fill_price,
                "closed_at":              now_iso,
                "status":                 "closed",
                "pnl":                    leg_pnl,
                "pnl_pct":                leg_pnl_pct,
                "hours_to_close_at_exit": _hours_until(leg.get("end_date")),
                "fee_usdc":               (leg.get("fee_usdc") or 0) + leg_fee,
                "exit_order_id":          None,
                "exit_order_price":       None,
                "exit_order_placed_at":   None,
            })

        db.update_balance(total_proceeds)
        db.record_account_value()

        reason = parent_trade.get("exit_reason", "exit")
        name = parent_trade.get("market_name", "unknown")[:55]
        total_cost = sum(leg["size_usdc"] for leg in legs)
        total_pnl = total_proceeds - total_cost - fee

        print(f"[live] EXIT FILLED {name}")
        print(f"             {filled_shares:.2f} shares @ ${fill_price:.3f}  "
              f"P&L: ${total_pnl:+.2f}  Fee: ${fee:.2f}")

        notify("info", "Position Closed",
               f"Exited {name} — {reason}",
               fields={"P&L": f"${total_pnl:+.2f}",
                        "Reason": reason},
               color=COLOR_GREEN)

    def manage_pending_exit(self, trade: dict):
        """Check fill status of a pending GTC exit, reprice if needed, settle if filled."""
        order_id = trade.get("exit_order_id")
        token_id = trade.get("token_id")
        parent_id = trade.get("parent_trade_id") or trade["id"]

        if not order_id or not token_id:
            return

        legs = db.get_position_legs(parent_id)
        if not legs:
            legs = [trade]

        # Check order status on CLOB
        try:
            self._throttle_clob()
            order = self._client.get_order(order_id)
        except Exception as e:
            print(f"[live] Exit order check failed for {order_id[:12]}: {e}")
            return

        if order is None:
            print(f"[live] Exit order {order_id[:12]} not found — will retry next cycle")
            return

        status = (order.get("status") or "").lower()

        # ── Filled ────────────────────────────────────────────────────────────
        if status in ("matched", "filled"):
            trades = order.get("associate_trades") or []
            if trades and isinstance(trades[0], dict):
                total_cost = 0.0
                total_shares = 0.0
                total_fee = 0.0
                for t in trades:
                    p = float(t.get("price", 0))
                    s = float(t.get("size", 0))
                    f = float(t.get("fee", 0))
                    total_cost += p * s
                    total_shares += s
                    total_fee += f
                fill_price = total_cost / total_shares if total_shares > 0 else trade.get("exit_order_price", 0)
            else:
                matched = float(order.get("size_matched", 0))
                fill_price = float(order.get("price", trade.get("exit_order_price", 0)))
                total_shares = matched
                total_fee = 0.0

            if total_shares > 0:
                self._settle_exit(trade, legs, fill_price, total_shares, total_fee)
                return

        # ── Cancelled/expired ─────────────────────────────────────────────────
        if status in ("cancelled", "expired", "dead", "canceled"):
            result = self._lookup_sell_fills(token_id, trade)
            if result and result[0] is not None and result[1] is not None and result[1] > 0:
                self._settle_exit(trade, legs, result[0], result[1], result[2])
                return
            print(f"[live] Exit order {order_id[:12]} was cancelled — reverting to open")
            for leg in legs:
                db.update_trade(leg["id"], {"status": "open"})
            db.update_trade(trade["id"], {
                "exit_order_id": None,
                "exit_order_price": None,
                "exit_order_placed_at": None,
            })
            return

        # ── Partial fill ──────────────────────────────────────────────────────
        size_matched = float(order.get("size_matched", 0))
        orig_size = float(order.get("original_size", 0) or order.get("size", 0) or 0)
        if size_matched > 0 and orig_size > 0 and size_matched < orig_size * 0.99:
            trades_data = order.get("associate_trades") or []
            if trades_data and isinstance(trades_data[0], dict):
                tc = sum(float(t.get("price", 0)) * float(t.get("size", 0)) for t in trades_data)
                ts = sum(float(t.get("size", 0)) for t in trades_data)
                tf = sum(float(t.get("fee", 0)) for t in trades_data)
                fp = tc / ts if ts > 0 else trade.get("exit_order_price", 0)
            else:
                fp = float(order.get("price", trade.get("exit_order_price", 0)))
                ts = size_matched
                tf = 0.0

            total_leg_shares = sum(leg.get("shares", 0) for leg in legs)
            if total_leg_shares > 0 and ts > 0:
                fill_frac = ts / total_leg_shares
                for leg in legs:
                    filled_leg = leg["shares"] * fill_frac
                    remaining = leg["shares"] - filled_leg
                    leg_proceeds = filled_leg * fp
                    leg_fee = tf * (leg["shares"] / total_leg_shares)
                    leg_pnl = leg_proceeds - (filled_leg * leg["fill_price"]) - leg_fee
                    db.update_balance(leg_proceeds)
                    db.update_trade(leg["id"], {
                        "shares": remaining,
                        "size_usdc": remaining * leg["fill_price"],
                        "fee_usdc": (leg.get("fee_usdc") or 0) + leg_fee,
                    })

            self._cancel_order(order_id)
            unfilled = orig_size - size_matched
            best_bid = polymarket.get_best_bid(token_id)
            if best_bid is None:
                print(f"[live] Partial repost skipped — orderbook unavailable")
                return
            reprice = round(max(best_bid - 0.01, 0.01), 2)
            new_resp = self._post_order(token_id, reprice, unfilled, "SELL", order_type="GTC")
            if new_resp:
                db.update_trade(trade["id"], {
                    "exit_order_id": new_resp.get("orderID", ""),
                    "exit_order_price": reprice,
                    "exit_order_placed_at": datetime.now(timezone.utc).isoformat(),
                })
            else:
                print(f"[live] Partial repost failed — will retry next cycle")
            return

        # ── Still open / delayed — reprice if needed ──────────────────────────
        if status in ("live", "open", "delayed", ""):
            best_bid = polymarket.get_best_bid(token_id)
            current_price = trade.get("exit_order_price", 0)
            reprice_step = WEATHER.get("exit_reprice_min_step", 0.01)

            if best_bid is None:
                print(f"[live] Reprice skipped — orderbook unavailable for {trade['market_name'][:50]}")
                return

            target_price = round(max(best_bid - 0.01, 0.01), 2)

            if current_price > target_price and (current_price - target_price) >= reprice_step:
                self._cancel_order(order_id)
                total_shares = orig_size or sum(l.get("shares", 0) for l in legs)
                new_resp = self._post_order(token_id, target_price, total_shares, "SELL", order_type="GTC")
                if new_resp:
                    db.update_trade(trade["id"], {
                        "exit_order_id": new_resp.get("orderID", ""),
                        "exit_order_price": target_price,
                        "exit_order_placed_at": datetime.now(timezone.utc).isoformat(),
                    })
                    print(f"[live] Exit repriced: ${current_price:.2f} -> ${target_price:.2f}  "
                          f"best_bid=${best_bid:.2f}  {trade['market_name'][:40]}")
                else:
                    print(f"[live] Exit reprice repost failed — will retry next cycle")
            else:
                print(f"[live] Exit order competitive @ ${current_price:.2f}  "
                      f"best_bid=${best_bid:.2f}  {trade['market_name'][:40]}")

    # ── Resolution settlement ────────────────────────────────────────────────

    def settle_resolved(self, trade: dict, resolved_yes: bool):
        """
        Settle a trade at market resolution.

        Losing trades: close immediately ($0 proceeds, no on-chain action).
        Winning trades: record resolution metadata and set claim_pending.
        Balance is NOT credited until the on-chain claim is confirmed.
        """
        direction = trade.get("direction", "YES").upper()
        won = (direction == "YES" and resolved_yes) or \
              (direction == "NO" and not resolved_yes)
        actual = "YES" if resolved_yes else "NO"
        close_price = 1.0 if resolved_yes else 0.0

        exit_price = 1.00 if won else 0.00
        cost = trade["size_usdc"]
        proceeds = trade["shares"] * exit_price
        pnl = proceeds - cost
        pnl_pct = (pnl / cost * 100) if cost > 0 else 0.0

        if won:
            # Winning trade — defer balance credit until on-chain claim confirms
            db.update_trade(trade["id"], {
                "exit_price":              exit_price,
                "status":                  "claim_pending",
                "pnl":                     pnl,
                "pnl_pct":                 pnl_pct,
                "exit_reason":             "resolved",
                "actual_resolution":       actual,
                "forecast_correct":        1,
                "resolution_price":        close_price,
                "hours_to_close_at_exit":  _hours_until(trade.get("end_date")),
                "claim_status":            "claim_pending",
                "claim_retries":           0,
            })
            print(f"  [resolve] WIN   {trade['market_name'][:52]}")
            print(f"            P&L: ${pnl:+.2f} ({pnl_pct:+.1f}%) — claim pending")
        else:
            # Losing trade — close immediately, no on-chain action needed
            db.update_balance(0)  # $0 proceeds, but record the event
            db.update_trade(trade["id"], {
                "exit_price":              exit_price,
                "closed_at":               datetime.now(timezone.utc).isoformat(),
                "status":                  "closed",
                "pnl":                     pnl,
                "pnl_pct":                 pnl_pct,
                "exit_reason":             "resolved",
                "actual_resolution":       actual,
                "forecast_correct":        0,
                "resolution_price":        close_price,
                "hours_to_close_at_exit":  _hours_until(trade.get("end_date")),
            })
            db.record_account_value()
            print(f"  [resolve] LOSS  {trade['market_name'][:52]}")
            print(f"            P&L: ${pnl:+.2f} ({pnl_pct:+.1f}%)")

        # Freeze tracked-trader positions for this resolved market
        city = (trade.get("city") or "").lower()
        end_date = (trade.get("end_date") or "")[:10]
        market_id = trade.get("market_id")
        if market_id and city and end_date:
            try:
                frozen = db.freeze_trader_forecasts(
                    market_id=market_id,
                    close_price=close_price,
                    city=city,
                    end_date=end_date,
                    threshold=trade.get("threshold"),
                )
                if frozen > 0:
                    print(f"            froze {frozen} trader forecast(s) for {city} {end_date}")
            except Exception as e:
                print(f"            [warn] freeze_trader_forecasts failed: {e}")

    # ── On-chain claim processing ──────────────────────────────────────────

    def _close_via_sibling(self, trade: dict, sibling: dict, now: datetime) -> None:
        """Close a trade whose shares were redeemed as part of a sibling's claim tx.
        Does NOT credit balance — the sibling's claim already credited the full
        combined on-chain token balance (see claim path: balance_raw / 1e6).
        Same-token neg-risk extended positions share a token_id across legs."""
        sibling_tx = sibling.get("claim_tx_hash")
        db.update_trade(trade["id"], {
            "status": "closed",
            "closed_at": now.isoformat(),
            "claim_status": "claim_via_sibling",
            "claim_tx_hash": sibling_tx,
            "claim_last_attempt": now.isoformat(),
        })
        db.record_account_value()
        tx_short = (sibling_tx[:16] + "...") if sibling_tx else "none"
        notify("info", "Claim Resolved via Sibling",
               f"Trade #{trade['id']} ({trade.get('city', '?')}) redeemed via "
               f"sibling #{sibling['id']}",
               fields={"Market": (trade.get("market_name") or "")[:60],
                       "Sibling tx": tx_short},
               color=COLOR_GREEN)
        print(f"[claims] VIA-SIBLING — trade #{trade['id']} closed "
              f"(sibling #{sibling['id']} tx={tx_short})")

    def _sweep_sibling_redeemed(self) -> None:
        """Close any claim_pending trades whose token_id already has a confirmed
        sibling claim. Self-heals same-token neg-risk positions where the second
        claim attempt found $0 on-chain because the sibling's claim redeemed the
        full combined balance."""
        now = datetime.now(timezone.utc)
        for trade in db.get_stuck_claim_pending_trades():
            token_id = trade.get("token_id")
            if not token_id:
                continue
            sibling = db.find_confirmed_sibling(str(token_id), trade["id"])
            if sibling:
                self._close_via_sibling(trade, sibling, now)

    def process_pending_claims(self):
        """
        Process on-chain claims for resolved winning trades.

        Checks all trades with claim_status='claim_pending', respects backoff
        schedule, submits CTF redeemPositions(), and credits balance on confirmation.
        """
        # Pre-sweep: close any claim_pending trades whose sibling already redeemed
        # on-chain. Handles same-token neg-risk extended positions where the
        # sibling's claim tx redeemed the wallet's full token balance.
        self._sweep_sibling_redeemed()

        pending = db.get_pending_claims()
        if not pending:
            return

        if self._claimer is None:
            if not getattr(self, "_claimer_none_alerted", False):
                self._claimer_none_alerted = True
                notify("critical", "Claimer Not Initialized",
                       f"{len(pending)} claim(s) pending but on-chain claimer "
                       f"failed to initialize. Claims cannot proceed.",
                       fields={"Pending claims": str(len(pending))})
            print(f"[claims] Claimer not initialized — {len(pending)} claim(s) waiting")
            return

        backoff = WEATHER.get("claim_retry_backoff_minutes", [5, 30, 120, 480, 1440])
        min_matic = WEATHER.get("claim_min_matic_balance", 0.01)

        # Gas guard — check once for all pending claims
        try:
            matic_balance = self._claimer.get_matic_balance()
        except Exception as e:
            print(f"[claims] MATIC balance check failed: {e}")
            return

        if matic_balance < min_matic:
            notify("warning", "Gas Too Low",
                   f"MATIC balance {matic_balance:.4f} below threshold. "
                   f"Deferring {len(pending)} claim(s).",
                   fields={"MATIC": f"{matic_balance:.4f}"})
            print(f"[claims] Low MATIC ({matic_balance:.4f}) — deferring {len(pending)} claim(s)")
            return

        now = datetime.now(timezone.utc)

        for trade in pending:
            retries = trade.get("claim_retries") or 0
            last_attempt = trade.get("claim_last_attempt")

            # Check if max retries exceeded (retries > len = exhausted all backoff slots)
            if retries > len(backoff):
                db.update_trade(trade["id"], {"claim_status": "claim_failed"})
                notify("critical", "Claim Failed — Max Retries",
                       f"Trade #{trade['id']} exhausted all {len(backoff)} claim "
                       f"retries. Manual intervention required.",
                       fields={
                           "Market": trade.get("market_name", "")[:60],
                           "Shares": f"{trade.get('shares', 0):.2f}",
                           "Last tx": (trade.get("claim_tx_hash") or "none")[:20],
                       })
                print(f"[claims] FAILED (max retries) — trade #{trade['id']} "
                      f"{trade['market_name'][:40]}")
                continue

            # Check backoff timing
            if last_attempt and retries > 0:
                try:
                    last = datetime.fromisoformat(last_attempt.replace("Z", "+00:00"))
                    if last.tzinfo is None:
                        last = last.replace(tzinfo=timezone.utc)
                    wait_minutes = backoff[retries - 1] if retries <= len(backoff) else backoff[-1]
                    if (now - last).total_seconds() < wait_minutes * 60:
                        continue  # still in backoff window
                except Exception:
                    pass  # unparseable timestamp — proceed with claim

            market_id = trade.get("market_id", "")
            token_id = trade.get("token_id")
            if not token_id:
                print(f"[claims] Trade #{trade['id']} missing token_id — skipping")
                continue

            # Gate on CTF payoutDenominator (neg-risk adapter path is broken —
            # we redeem directly against CTF with wcol as collateral, then
            # unwrap wcol -> USDC in one bundled tx).
            ready = self._claimer.is_condition_redeemable(market_id)
            if ready is False:
                # Alert once if oracle is >3h overdue (2h UMA window should be done)
                end_str = trade.get("end_date", "")
                if end_str and trade["id"] not in self._oracle_alerted:
                    try:
                        end_dt = datetime.fromisoformat(end_str.replace("Z", "+00:00"))
                        if end_dt.tzinfo is None:
                            end_dt = end_dt.replace(tzinfo=timezone.utc)
                        hours_waiting = (now - end_dt).total_seconds() / 3600
                        if hours_waiting > 3:
                            self._oracle_alerted.add(trade["id"])
                            notify("warning", "Oracle Delayed",
                                   f"Trade #{trade['id']} has been waiting "
                                   f"{hours_waiting:.0f}h for UMA oracle. "
                                   f"Normal window is ~2h.",
                                   fields={
                                       "Market": trade.get("market_name", "")[:60],
                                       "Hours waiting": f"{hours_waiting:.1f}h",
                                   })
                    except Exception:
                        pass
                print(f"[claims] Oracle not yet posted for trade #{trade['id']}  "
                      f"{trade['market_name'][:40]}… — deferring")
                continue
            if ready is None:
                # Transient RPC failure — do not burn a retry, try next cycle
                print(f"[claims] Redeemable check failed for trade #{trade['id']} "
                      f"(RPC error) — will retry next cycle")
                continue

            # If a prior cycle already submitted a tx for this trade that
            # stayed pending (mempool delay), re-check it instead of submitting
            # another. Resubmitting at the same nonce causes "replacement
            # transaction underpriced" and burns a retry slot.
            existing_tx = trade.get("claim_tx_hash")
            if existing_tx:
                existing_status = self._claimer.check_tx_status(existing_tx)
                if existing_status == "pending":
                    print(f"[claims] Prior tx still pending for trade #{trade['id']}  "
                          f"tx={existing_tx[:16]}... — skipping resubmit")
                    continue
                if existing_status == "confirmed":
                    proceeds = float(trade["shares"])
                    db.update_balance(proceeds)
                    db.update_trade(trade["id"], {
                        "status": "closed",
                        "closed_at": now.isoformat(),
                        "claim_status": "claim_confirmed",
                    })
                    db.record_account_value()
                    notify("info", "Claim Completed",
                           f"Redeemed {trade.get('market_name', 'unknown')[:60]}",
                           fields={"Amount": f"${proceeds:.2f}",
                                    "Market": trade.get("market_name", "")[:60]},
                           color=COLOR_GREEN)
                    print(f"[claims] CONFIRMED (prior tx) — trade #{trade['id']}  "
                          f"+${proceeds:.2f}  tx={existing_tx[:16]}...")
                    continue
                # "failed" → fall through; clear tx_hash so next submit is fresh
                db.update_trade(trade["id"], {"claim_tx_hash": None})

            # Read the proxy's token balance just-in-time (don't trust db shares).
            # For binary neg-risk markets, the winning-side CTF balance equals the
            # wcol amount we'll unwrap.
            balance_raw = self._claimer.get_token_balance(
                proxy_address=WALLET_FUNDER_ADDRESS, token_id=int(token_id),
            )
            if balance_raw == 0:
                sibling = db.find_confirmed_sibling(str(token_id), trade["id"])
                if sibling:
                    self._close_via_sibling(trade, sibling, now)
                    continue
                # Market is resolved (ready=True checked above) and shares are gone
                # with no sibling claim. Polymarket's auto-redeem feature or a manual
                # UI redeem already pulled them — proceeds went straight to the
                # proxy. Close as externally claimed and credit shares*$1 so the
                # bot's accounting catches up. Mirrors the CLOB-sync handler's
                # behavior for the same scenario detected via the API path.
                proceeds = float(trade.get("shares") or 0)
                db.update_balance(proceeds)
                db.update_trade(trade["id"], {
                    "status": "closed",
                    "closed_at": now.isoformat(),
                    "claim_status": "claimed_externally",
                    "claim_last_attempt": now.isoformat(),
                })
                db.record_account_value()
                notify("info", "Claim Resolved Externally",
                       f"Trade #{trade['id']} ({trade.get('city', '?')}) shares "
                       f"already redeemed (likely Polymarket auto-redeem)",
                       fields={"Market": (trade.get("market_name") or "")[:60],
                               "Credited": f"${proceeds:.2f}"},
                       color=COLOR_GREEN)
                print(f"[claims] EXTERNAL — trade #{trade['id']} closed "
                      f"(shares already redeemed, +${proceeds:.2f})")
                continue

            print(f"[claims] Attempting claim for trade #{trade['id']}  "
                  f"{trade['market_name'][:40]}... ({balance_raw/1e6:.4f} shares)")

            tx_hash = self._claimer.claim_winnings(
                condition_id=market_id,
                expected_wcol=balance_raw,
            )

            if tx_hash is None:
                # Submission failed — consume retry
                db.update_trade(trade["id"], {
                    "claim_retries": retries + 1,
                    "claim_last_attempt": now.isoformat(),
                    "claim_status": "claim_pending",
                })
                print(f"[claims] Claim tx failed for trade #{trade['id']} "
                      f"(retry {retries + 1}/{len(backoff)})")
                continue

            # Poll for confirmation (up to 30s)
            status = "pending"
            for _ in range(15):
                status = self._claimer.check_tx_status(tx_hash)
                if status != "pending":
                    break
                time.sleep(2)

            if status == "confirmed":
                proceeds = balance_raw / 1_000_000  # wcol is 6-decimal, unwraps 1:1 to USDC
                db.update_balance(proceeds)
                db.update_trade(trade["id"], {
                    "status": "closed",
                    "closed_at": now.isoformat(),
                    "claim_status": "claim_confirmed",
                    "claim_tx_hash": tx_hash,
                })
                db.record_account_value()
                notify("info", "Claim Completed",
                       f"Redeemed {trade.get('market_name', 'unknown')[:60]}",
                       fields={"Amount": f"${proceeds:.2f}",
                                "Market": trade.get("market_name", "")[:60]},
                       color=COLOR_GREEN)
                print(f"[claims] CONFIRMED — trade #{trade['id']}  "
                      f"+${proceeds:.2f}  tx={tx_hash[:16]}...")
            elif status == "failed":
                # Tx was mined but reverted — consume retry
                db.update_trade(trade["id"], {
                    "claim_retries": retries + 1,
                    "claim_last_attempt": now.isoformat(),
                    "claim_tx_hash": tx_hash,
                    "claim_status": "claim_pending",
                })
                print(f"[claims] Tx reverted for trade #{trade['id']}  "
                      f"tx={tx_hash[:16]}... (retry {retries + 1}/{len(backoff)})")
            else:
                # Still pending after 30s — record tx hash, don't consume retry,
                # next cycle will re-check
                db.update_trade(trade["id"], {
                    "claim_tx_hash": tx_hash,
                    "claim_last_attempt": now.isoformat(),
                })
                print(f"[claims] Tx pending for trade #{trade['id']}  "
                      f"tx={tx_hash[:16]}... (will re-check)")

    # ── Position price refresh ───────────────────────────────────────────────

    def update_open_positions(self):
        """
        Refresh current_price, peak_price, and liquidity for every open position.
        Uses CLOB midpoint for price, Gamma API for liquidity/volume.
        Also backfills missing city/threshold/market_url for reconciled positions.
        """
        from layers.layer3_weather import parse_weather_market, WeatherLayer

        open_trades = db.get_open_trades()
        for trade in open_trades:
            new_price = self._fetch_current_price(trade)
            if new_price is None:
                continue

            peak = max(trade.get("peak_price") or 0.0, new_price)
            updates = {"current_price": new_price, "peak_price": peak}

            market = polymarket.get_market_by_id(trade["market_id"])
            if market:
                liq = market.get("liquidity") or 0
                if liq > 0:
                    updates["liquidity"] = liq
                vol = market.get("volume") or 0
                if vol > 0:
                    updates["volume_24h"] = vol
                if not trade.get("market_url") and market.get("market_url"):
                    updates["market_url"] = market["market_url"]
                if not trade.get("market_name") and market.get("question"):
                    updates["market_name"] = market["question"]
                if not trade.get("end_date") and market.get("end_date"):
                    updates["end_date"] = market["end_date"]

            # Backfill city/threshold from question if missing (reconciled trades)
            question = trade.get("market_name") or (market.get("question", "") if market else "")
            if question and (not trade.get("city") or not trade.get("threshold")):
                parsed = parse_weather_market(question)
                if parsed:
                    if not trade.get("city"):
                        updates["city"] = parsed["city_raw"].title()
                    if not trade.get("threshold") and parsed["market_type"] == "threshold":
                        op = ">=" if parsed["direction"] == "above" else "<="
                        updates["threshold"] = f"{op}{parsed['threshold']:.0f}{parsed['unit']}"

            # Backfill ensemble for reconciled trades that have none
            if trade.get("entry_ensemble_n") is None and question and market:
                try:
                    layer = WeatherLayer()
                    scan_data = layer.scan({
                        "question": question,
                        "end_date": market.get("end_date", ""),
                        "price":    market.get("price") or 0.5,
                    })
                    if scan_data and scan_data.get("ensemble_n", 0) >= 10:
                        ens_yes = scan_data.get("yes_ensemble")
                        ens_n   = scan_data.get("ensemble_n")
                        ens_pct = ens_yes / ens_n if ens_yes is not None and ens_n else None
                        updates["entry_ensemble_yes"] = ens_yes
                        updates["entry_ensemble_n"]   = ens_n
                        updates["entry_ensemble_pct"] = ens_pct
                        print(f"[backfill] ensemble for trade #{trade['id']}: "
                              f"{int(ens_yes)}/{ens_n}")
                except Exception as e:
                    print(f"[backfill] ensemble failed for trade #{trade['id']}: {e}")

            db.update_trade(trade["id"], updates)

    def _fetch_current_price(self, trade: dict) -> float | None:
        """Fetch the current token price via CLOB midpoint, with Gamma fallback."""
        token_id = trade.get("token_id")
        if token_id:
            price = polymarket.get_midpoint(token_id)
            if price and 0 < price < 1:
                return price
        market = polymarket.get_market_by_id(trade["market_id"])
        if market:
            yes_price = market.get("price")
            if yes_price is not None and 0 < yes_price < 1:
                direction = trade.get("direction", "YES").upper()
                return yes_price if direction == "YES" else (1.0 - yes_price)
        return None


# ── Module-level helpers ─────────────────────────────────────────────────────

def _find_duplicate_active_tokens(trades) -> dict[str, list[int]]:
    """Return {token_id: [trade_id, ...]} for token_ids held by >1 active parent row.

    Extended-position child legs (parent_trade_id set) legitimately share a
    token_id with their parent, so they are excluded from duplicate detection.
    """
    seen: dict[str, list[int]] = {}
    for t in trades:
        if t.get("parent_trade_id"):
            continue
        tid = t.get("token_id") or ""
        if not tid:
            continue
        seen.setdefault(tid, []).append(t["id"])
    return {tok: ids for tok, ids in seen.items() if len(ids) > 1}


def _hours_until(end_date_str: str | None) -> float | None:
    """Hours from now until end_date_str. Returns None if unparseable or missing."""
    if not end_date_str:
        return None
    try:
        end = datetime.fromisoformat(end_date_str.replace("Z", "+00:00"))
        if end.tzinfo is None:
            end = end.replace(hour=23, minute=59, second=59, tzinfo=timezone.utc)
        return (end - datetime.now(timezone.utc)).total_seconds() / 3600
    except Exception:
        return None


def _append_reason(existing: str | None, new: str) -> str:
    if existing:
        return f"{existing} → {new}"
    return new
