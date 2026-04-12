"""
Live Executor
──────────────
Real order execution on Polymarket via py-clob-client.
Implements the same BaseExecutor interface as PaperExecutor.
"""
import time
from datetime import datetime, timezone

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import (
    BalanceAllowanceParams, AssetType, OrderArgs, OrderType,
)
from py_clob_client.order_builder.constants import BUY, SELL

from executor.base import BaseExecutor
from config import (
    POLYMARKET_CLOB_API, WALLET_PRIVATE_KEY,
    WALLET_SIGNATURE_TYPE, WALLET_FUNDER_ADDRESS, WEATHER,
)
import markets.polymarket as polymarket
import db


# Polymarket uses USDC with 6 decimals on Polygon
_USDC_DECIMALS = 1_000_000


class LiveExecutor(BaseExecutor):
    """
    Executes real trades on Polymarket via the CLOB API.

    Fill prices come from actual order matching, not simulated book walks.
    Balance is synced from the exchange on startup and tracked locally
    after each trade (with periodic re-sync).
    """

    def __init__(self):
        self._client: ClobClient | None = None
        self._init_client(
            WALLET_PRIVATE_KEY,
            137,  # Polygon mainnet
            WALLET_SIGNATURE_TYPE,
            WALLET_FUNDER_ADDRESS,
        )

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

        # Verify allowance — if 0, orders will silently fail
        bal_info = self._client.get_balance_allowance(
            BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
        )
        allowance = int(bal_info.get("allowance", "0"))
        if allowance == 0:
            raise RuntimeError(
                "CLOB allowance is 0 — run scripts/setup_allowances.py first. "
                "Orders will fail without USDC approval on Polygon."
            )

        balance_usdc = int(bal_info.get("balance", "0")) / _USDC_DECIMALS
        print(f"[live] CLOB client initialized. Balance: ${balance_usdc:.2f} USDC")

    def _get_exchange_balance(self) -> float:
        """Fetch current USDC balance from the exchange."""
        bal_info = self._client.get_balance_allowance(
            BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
        )
        return int(bal_info.get("balance", "0")) / _USDC_DECIMALS

    # ── CLOB order helpers ───────────────────────────────────────────────────

    def _post_order(self, token_id: str, price: float, size: float,
                    side: str) -> dict | None:
        """Sign and post an order to the CLOB. Returns response dict or None."""
        clob_side = BUY if side == "BUY" else SELL

        order_args = OrderArgs(
            token_id=token_id,
            price=price,
            size=size,
            side=clob_side,
        )

        try:
            signed = self._client.create_order(order_args)
            response = self._client.post_order(signed, OrderType.FOK)
            print(f"[live] Order posted: {response.get('orderID', '?')[:12]}  "
                  f"status={response.get('status', '?')}")
            return response
        except Exception as e:
            print(f"[live] Order post failed: {e}")
            return None

    def _confirm_fill(self, order_id: str, token_id: str,
                      expected_price: float) -> tuple[float, float, float]:
        """
        Poll order status to confirm fill.

        Returns:
            (fill_price, filled_shares, total_fee)
        """
        for attempt in range(5):
            try:
                order = self._client.get_order(order_id)
                status = order.get("status", "")

                if status in ("matched", "filled"):
                    trades = order.get("associate_trades", [])
                    if trades:
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

                    # Matched but no trade details yet — use order-level fields
                    matched = float(order.get("size_matched", 0))
                    if matched > 0:
                        price = float(order.get("price", expected_price))
                        return price, matched, 0.0

                if status in ("cancelled", "expired", "dead"):
                    return 0.0, 0.0, 0.0

            except Exception as e:
                print(f"[live] Fill check attempt {attempt + 1} failed: {e}")

            if attempt < 4:
                time.sleep(2)

        # Timeout — treat as unfilled
        return 0.0, 0.0, 0.0

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
        1. Update DB balance to match exchange USDC balance
        2. Log open positions being resumed
        3. Warn about any discrepancies
        """
        exchange_balance = self._get_exchange_balance()
        db_balance = db.get_balance()

        if abs(exchange_balance - db_balance) > 0.01:
            print(f"[live] Balance sync: DB=${db_balance:.2f} → Exchange=${exchange_balance:.2f}")
            db.set_balance(exchange_balance)
        else:
            print(f"[live] Balance in sync: ${exchange_balance:.2f}")

        open_trades = db.get_open_trades()
        if not open_trades:
            print("[live] No open positions to resume.")
            return

        print(f"[live] Resuming {len(open_trades)} open position(s):")
        for t in open_trades:
            print(f"       {t['direction']:3s}  {t['market_name'][:60]}  "
                  f"fill={t['fill_price']:.4f}  size=${t['size_usdc']:.2f}")

    # ── Order placement ──────────────────────────────────────────────────────

    def place_order(self, market: dict, direction: str, size_usdc: float, estimate: dict):
        balance = db.get_balance()
        if size_usdc > balance:
            print(f"[live] Skipping — insufficient balance ${balance:.2f} < ${size_usdc:.2f}")
            return

        if db.get_open_trade_for_market(market["id"]):
            return

        # Select token — YES buys YES token, NO buys NO token
        if direction == "YES":
            token_id = market.get("token_id")
        else:
            token_id = market.get("no_token_id") or market.get("token_id")

        if not token_id:
            return

        # Get current best ask to set limit price
        mid = polymarket.get_midpoint(token_id)
        if mid is None or mid <= 0:
            print(f"[live] Skipping — no midpoint for {market.get('question', '')[:50]}")
            return

        # Place a FOK order at slightly above mid for fast fill
        limit_price = round(min(mid + 0.01, 0.99), 2)
        shares = size_usdc / limit_price

        order_response = self._post_order(token_id, limit_price, shares, "BUY")
        if order_response is None:
            return

        order_id = order_response.get("orderID", "")

        # Poll for fill confirmation
        fill_price, filled_shares, fee = self._confirm_fill(order_id, token_id, limit_price)
        if filled_shares <= 0:
            print(f"[live] Order {order_id[:12]} not filled — cancelling")
            self._cancel_order(order_id)
            return

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

        shares = size_usdc / limit_price

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

        On-chain claiming of winnings is Phase 2 work. For now, this records
        the resolution in the DB and updates the balance. Winning shares
        pay $1.00 each, losing shares pay $0.00.
        """
        direction = trade.get("direction", "YES").upper()
        won = (direction == "YES" and resolved_yes) or \
              (direction == "NO" and not resolved_yes)
        actual = "YES" if resolved_yes else "NO"
        close_price = 1.0 if resolved_yes else 0.0

        exit_price = 1.00 if won else 0.00
        proceeds = trade["shares"] * exit_price

        cost = trade["size_usdc"]
        pnl = proceeds - cost
        pnl_pct = (pnl / cost * 100) if cost > 0 else 0.0

        db.update_balance(proceeds)
        db.update_trade(trade["id"], {
            "exit_price":              exit_price,
            "closed_at":               datetime.now(timezone.utc).isoformat(),
            "status":                  "closed",
            "pnl":                     pnl,
            "pnl_pct":                 pnl_pct,
            "exit_reason":             "resolved",
            "actual_resolution":       actual,
            "forecast_correct":        1 if won else 0,
            "resolution_price":        close_price,
            "hours_to_close_at_exit":  _hours_until(trade.get("end_date")),
        })
        db.record_account_value()

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

        outcome = "WIN" if won else "LOSS"
        print(f"  [resolve] {outcome}  {trade['market_name'][:52]}")
        print(f"            P&L: ${pnl:+.2f} ({pnl_pct:+.1f}%)")

    # ── Position price refresh ───────────────────────────────────────────────

    def update_open_positions(self):
        """
        Refresh current_price, peak_price, and liquidity for every open position.
        Uses CLOB midpoint for price, Gamma API for liquidity/volume.
        """
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
