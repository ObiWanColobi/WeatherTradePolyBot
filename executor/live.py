"""
Live Executor
──────────────
Real order execution on Polymarket via py-clob-client.
Implements the same BaseExecutor interface as PaperExecutor.
"""
import math
import time
from datetime import datetime, timezone

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import (
    BalanceAllowanceParams, AssetType, OrderArgs, MarketOrderArgs, OrderType,
    TradeParams,
)
from py_clob_client.order_builder.constants import BUY, SELL

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
        creds = self._client.create_or_derive_api_creds()
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
            print("[live] WARNING: No USDC allowance detected — first order may fail. "
                  "Run scripts/setup_allowances.py if orders are rejected.")

        balance_usdc = int(bal_info.get("balance", "0")) / _USDC_DECIMALS
        print(f"[live] CLOB client initialized. Balance: ${balance_usdc:.2f} USDC")

    def _get_exchange_balance(self) -> float:
        """Fetch current USDC balance from the exchange."""
        bal_info = self._client.get_balance_allowance(
            BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
        )
        return int(bal_info.get("balance", "0")) / _USDC_DECIMALS

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
                    side: str) -> dict | None:
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
            # BUY: maker_amount = USDC to spend. Pass as amount, SDK derives shares.
            amount = math.floor(size * price * 100) / 100  # USDC, truncate to 2 dp
            order_args = MarketOrderArgs(
                token_id=token_id,
                price=price,
                amount=amount,
                side=clob_side,
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
                    response = self._client.post_order(signed, OrderType.FOK)
                    print(f"[live] Order posted: {response.get('orderID', '?')[:12]}  "
                          f"status={response.get('status', '?')}")
                    return response
                except Exception as e:
                    if "fully filled" in str(e).lower():
                        print(f"[live] FOK rejected — insufficient liquidity")
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
                        return price, matched, 0.0

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
            result = self._lookup_buy_fills(token_id, order_id)
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

    def _lookup_buy_fills(self, token_id: str, order_id: str
                          ) -> tuple[float, float, float] | None:
        """
        Query CLOB trade history for BUY fills matching this order.
        Returns (avg_price, total_shares, total_fee) or None if no fills found.
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
            # Reset balance history when switching from paper to live
            # (paper history at $2000 distorts the chart)
            from db import get_conn
            with get_conn() as conn:
                conn.execute("DELETE FROM balance_history")
            db.record_account_value()
            print(f"[live] Balance history reset for live mode.")
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

        db_trades = db.get_open_trades()
        db_token_ids = {t.get("token_id", "") for t in db_trades}

        # Check for orphaned exchange positions (not in DB)
        orphaned = []
        for tid, pos in exchange_by_token.items():
            if tid not in db_token_ids:
                orphaned.append(pos)

        if orphaned:
            print(f"[live] Found {len(orphaned)} position(s) on exchange not in DB — importing:")
            for pos in orphaned:
                self._import_orphaned_position(pos)

        # Auto-close stale DB positions (in DB but gone from exchange)
        for t in db_trades:
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

        Queries CLOB trade history to find the actual sell fills and compute
        real P&L. Falls back to midpoint estimate if no trade history found.
        """
        tid      = trade.get("token_id", "")
        trade_id = trade["id"]
        name     = trade["market_name"][:50]
        cost     = trade.get("size_usdc", 0)

        # Try to find actual sell trades from CLOB history
        exit_price, proceeds, fee = self._lookup_sell_fills(tid, trade)

        if proceeds is not None:
            pnl     = proceeds - cost - fee
            pnl_pct = (pnl / cost * 100) if cost > 0 else 0.0
            source  = "trade history"
        else:
            # Fallback: no trade history found. Use current midpoint as estimate.
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

    def _cancel_all_open_orders(self):
        """Cancel all open orders on startup to prevent ghost fills."""
        try:
            self._throttle_clob()
            result = self._client.cancel_market_orders()
            # Result format varies — may be list of cancelled IDs or a status dict
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

    def process_pending_claims(self):
        """
        Process on-chain claims for resolved winning trades.

        Checks all trades with claim_status='claim_pending', respects backoff
        schedule, submits CTF redeemPositions(), and credits balance on confirmation.
        """
        if self._claimer is None:
            return

        pending = db.get_pending_claims()
        if not pending:
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

            # Determine index set: [1] for YES tokens, [2] for NO tokens
            direction = (trade.get("direction") or "YES").upper()
            index_sets = [1] if direction == "YES" else [2]

            market_id = trade.get("market_id", "")
            print(f"[claims] Attempting claim for trade #{trade['id']}  "
                  f"{trade['market_name'][:40]}...")

            tx_hash = self._claimer.claim_winnings(
                condition_id=market_id,
                index_sets=index_sets,
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
                proceeds = trade["shares"] * 1.0  # winning shares = $1.00 each
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
