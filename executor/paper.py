from datetime import datetime, timezone
from executor.base import BaseExecutor
import markets.polymarket as polymarket
import db


class PaperExecutor(BaseExecutor):
    """
    Simulates trade execution using live Polymarket order books.

    Fill prices are calculated by walking the real bid/ask book at the time
    of the order, matching how a real market order would fill with slippage.
    This makes paper trading P&L directly comparable to what live trading
    would have produced.
    """

    # ── Restart reconciliation ────────────────────────────────────────────────

    def reconcile_positions(self):
        """
        Paper mode: positions are already in the DB — nothing to import.
        Just logs what's being resumed so the operator can see it at startup.
        """
        open_trades = db.get_open_trades()
        if not open_trades:
            print("[paper] No open positions to resume.")
            return
        print(f"[paper] Resuming {len(open_trades)} open paper position(s):")
        for t in open_trades:
            print(f"         {t['direction']:3s}  {t['market_name'][:60]}  "
                  f"fill={t['fill_price']:.4f}  size=${t['size_usdc']:.2f}")

    # ── Order placement ───────────────────────────────────────────────────────

    def place_order(self, market: dict, direction: str, size_usdc: float, estimate: dict):
        balance = db.get_balance()
        if size_usdc > balance:
            print(f"[paper] Skipping — insufficient balance ${balance:.2f} < ${size_usdc:.2f}")
            return

        # One open position per market at a time
        if db.get_open_trade_for_market(market["id"]):
            return

        # Select the token we are actually buying.
        # YES trades buy YES tokens; NO trades buy NO tokens.
        # Both use the BUY (ask) side — we always purchase shares of the winning token.
        if direction == "YES":
            token_id = market.get("token_id")
        else:
            token_id = market.get("no_token_id") or market.get("token_id")

        if not token_id:
            return

        fill_price, filled_usdc = self._simulate_fill(token_id, "BUY", size_usdc)

        if filled_usdc < 1.0:
            print(f"[paper] Skipping — order book too thin to fill: {market['question'][:60]}")
            return

        shares = filled_usdc / fill_price

        # For display: slippage and edge are relative to the token we actually bought.
        # YES trades: token mid ≈ market["price"] (YES mid)
        # NO trades:  token mid ≈ 1 - market["price"] (NO mid)
        yes_price  = market["price"]
        model_prob = estimate.get("probability", 0)
        if direction == "YES":
            token_mid = yes_price
            edge_display = model_prob - yes_price          # positive when model > market
        else:
            token_mid = 1.0 - yes_price
            edge_display = yes_price - model_prob          # positive when model < market (NO underpriced)
        slippage = abs(fill_price - token_mid)

        trade = {
            "market_id":       market["id"],
            "market_name":     market["question"],
            "token_id":        token_id,
            "end_date":        market.get("end_date"),
            "direction":       direction,
            "size_usdc":       filled_usdc,
            "shares":          shares,
            "entry_price":     market["price"],
            "fill_price":      fill_price,
            "current_price":   fill_price,
            "peak_price":      fill_price,
            "exit_price":      None,
            "opened_at":       datetime.utcnow().isoformat(),
            "hours_to_close_at_entry":  _hours_until(market.get("end_date")),
            "closed_at":       None,
            "status":          "open",
            "pnl":             None,
            "pnl_pct":         None,
            "layers_used":     ", ".join(estimate.get("sources", [])),
            "estimated_prob":  estimate.get("probability"),
            "edge_score":      estimate.get("edge_score"),
            "liquidity":          market.get("liquidity"),
            "volume_24h":         market.get("volume"),
            "city":               market.get("city"),
            "entry_ensemble_pct": estimate.get("entry_ensemble_pct"),
            "entry_ensemble_yes": estimate.get("entry_ensemble_yes"),
            "entry_ensemble_n":   estimate.get("entry_ensemble_n"),
            "market_url":         market.get("market_url", ""),
            "threshold":          estimate.get("threshold"),
            "bleed_rungs_hit":    0,
            "exit_reason":        None,
        }

        db.insert_trade(trade)
        db.update_balance(-filled_usdc)
        db.record_account_value()

        print(f"[paper] OPEN  {direction:3s}  {market['question'][:55]}")
        print(f"              Size: ${filled_usdc:.2f}  Fill: {fill_price:.4f}  "
              f"Slippage: {slippage:.4f}  Edge: {edge_display:+.3f}")

    # ── Position closing ──────────────────────────────────────────────────────

    def close_partial(self, trade: dict, sell_pct: float, reason: str):
        """Sell `sell_pct` fraction of remaining shares, leave the rest open."""
        shares_to_sell   = trade["shares"] * sell_pct
        current_price    = trade.get("current_price") or trade["fill_price"]
        proceeds         = shares_to_sell * current_price
        partial_pnl      = proceeds - (shares_to_sell * trade["fill_price"])

        db.update_balance(proceeds)
        db.record_account_value()

        remaining_shares = trade["shares"] - shares_to_sell
        remaining_usdc   = remaining_shares * trade["fill_price"]

        if remaining_shares < 0.01:
            # Nothing meaningful left — treat as full close
            self._finalise_close(trade, current_price, reason)
        else:
            db.update_trade(trade["id"], {
                "shares":          remaining_shares,
                "size_usdc":       remaining_usdc,
                "bleed_rungs_hit": trade.get("bleed_rungs_hit", 0) + 1,
                "exit_reason":     _append_reason(trade.get("exit_reason"), reason),
            })
            print(f"[paper] PARTIAL ({sell_pct*100:.0f}%)  {trade['market_name'][:50]}")
            print(f"              Reason: {reason}  Proceeds: ${proceeds:.2f}  "
                  f"Partial P&L: ${partial_pnl:+.2f}")

    def close_full(self, trade: dict, reason: str):
        current_price = trade.get("current_price") or trade["fill_price"]
        self._finalise_close(trade, current_price, reason)

    def settle_resolved(self, trade: dict, resolved_yes: bool):
        """
        Settle a trade at market resolution.

        Both YES and NO trades now hold shares of their respective token
        (bought via BUY on the correct order book). At resolution the winning
        token pays $1.00 per share, the losing token pays $0.00.
        P&L is symmetric for both directions.
        """
        direction       = trade.get("direction", "YES").upper()
        won             = (direction == "YES" and resolved_yes) or \
                          (direction == "NO"  and not resolved_yes)
        actual          = "YES" if resolved_yes else "NO"
        close_price     = 1.0 if resolved_yes else 0.0

        exit_price = 1.00 if won else 0.00
        proceeds   = trade["shares"] * exit_price

        cost    = trade["size_usdc"]
        pnl     = proceeds - cost
        pnl_pct = (pnl / cost * 100) if cost > 0 else 0.0

        db.update_balance(proceeds)
        db.update_trade(trade["id"], {
            "exit_price":              1.00 if won else 0.00,
            "closed_at":               datetime.utcnow().isoformat(),
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
        city      = (trade.get("city") or "").lower()
        end_date  = (trade.get("end_date") or "")[:10]
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

    def _finalise_close(self, trade: dict, exit_price: float, reason: str):
        proceeds = trade["shares"] * exit_price
        cost     = trade["size_usdc"]
        pnl      = proceeds - cost
        pnl_pct  = (pnl / cost * 100) if cost > 0 else 0.0

        db.update_balance(proceeds)
        db.update_trade(trade["id"], {
            "exit_price":              exit_price,
            "closed_at":               datetime.utcnow().isoformat(),
            "status":                  "closed",
            "pnl":                     pnl,
            "pnl_pct":                 pnl_pct,
            "exit_reason":             _append_reason(trade.get("exit_reason"), reason),
            "hours_to_close_at_exit":  _hours_until(trade.get("end_date")),
        })
        db.record_account_value()

        print(f"[paper] CLOSE {trade['market_name'][:55]}")
        print(f"              Reason: {reason}  P&L: ${pnl:+.2f} ({pnl_pct:+.1f}%)")

    # ── Position price refresh ────────────────────────────────────────────────

    def update_open_positions(self):
        """
        Refresh current_price, peak_price, and liquidity for every open position.
        Uses the stored token_id for a direct CLOB midpoint call when available,
        otherwise falls back to the Gamma API.
        Liquidity is always refreshed from the Gamma API since CLOB doesn't expose it.
        """
        open_trades = db.get_open_trades()
        for trade in open_trades:
            new_price = self._fetch_current_price(trade)
            if new_price is None:
                continue

            peak    = max(trade.get("peak_price") or 0.0, new_price)
            updates = {"current_price": new_price, "peak_price": peak}

            # Refresh liquidity from Gamma API — only update if we get a non-zero value
            # (zero likely means the field was absent in the response, not actual zero liquidity)
            market = polymarket.get_market_by_id(trade["market_id"])
            if market:
                liq = market.get("liquidity") or 0
                if liq > 0:
                    updates["liquidity"] = liq
                vol = market.get("volume") or 0
                if vol > 0:
                    updates["volume_24h"] = vol
                # Backfill market_url for trades that were opened before this field existed
                if not trade.get("market_url") and market.get("market_url"):
                    updates["market_url"] = market["market_url"]

            db.update_trade(trade["id"], updates)

    def _fetch_current_price(self, trade: dict) -> float | None:
        token_id = trade.get("token_id")
        if token_id:
            price = polymarket.get_midpoint(token_id)
            if price and 0 < price < 1:
                return price
        # Fallback: re-fetch YES price from Gamma and convert to token price.
        # For NO trades current_price tracks the NO token value = 1 - YES price.
        market = polymarket.get_market_by_id(trade["market_id"])
        if market:
            yes_price = market.get("price")
            if yes_price is not None and 0 < yes_price < 1:
                direction = trade.get("direction", "YES").upper()
                return yes_price if direction == "YES" else (1.0 - yes_price)
        return None

    # ── Fill simulator ────────────────────────────────────────────────────────

    def _simulate_fill(self, token_id: str, side: str, size_usdc: float) -> tuple[float, float]:
        """
        Walk the real live order book to simulate a realistic market-order fill.

        BUY  → consume asks in ascending price order  (cheapest first)
        SELL → consume bids in descending price order (highest first)

        Returns:
            avg_fill_price: weighted average price across all consumed levels
            filled_usdc:    total USDC actually filled (may be < size_usdc if book is thin)
        """
        book = polymarket.get_orderbook(token_id)

        if side == "BUY":
            raw_levels = book.get("asks", [])
            levels = sorted(
                [{"price": float(l["price"]), "size": float(l["size"])} for l in raw_levels if l.get("price")],
                key=lambda x: x["price"],   # ascending — cheapest first
            )
        else:
            raw_levels = book.get("bids", [])
            levels = sorted(
                [{"price": float(l["price"]), "size": float(l["size"])} for l in raw_levels if l.get("price")],
                key=lambda x: x["price"],
                reverse=True,               # descending — highest bid first
            )

        remaining_usdc = size_usdc
        total_shares   = 0.0
        total_cost     = 0.0

        for level in levels:
            price = level["price"]
            if price <= 0:
                continue

            available_usdc = level["size"] * price
            fill_usdc      = min(remaining_usdc, available_usdc)
            shares         = fill_usdc / price

            total_shares   += shares
            total_cost     += fill_usdc
            remaining_usdc -= fill_usdc

            if remaining_usdc < 0.01:
                break

        if total_shares == 0:
            # Book is empty — use midpoint as fallback so we can still record the trade
            mid          = polymarket.get_midpoint(token_id) or 0.50
            total_shares = size_usdc / mid
            total_cost   = size_usdc

        avg_fill_price = total_cost / total_shares
        filled_usdc    = size_usdc - remaining_usdc

        return avg_fill_price, filled_usdc


def _append_reason(existing: str | None, new: str) -> str:
    if existing:
        return f"{existing} → {new}"
    return new


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
