from datetime import datetime, timezone
from executor.base import BaseExecutor
from notifications import notify, COLOR_GREEN
import markets.polymarket as polymarket
import db
from config import WEATHER


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

    def place_order(self, market: dict, direction: str, size_usdc: float, estimate: dict) -> bool:
        balance = db.get_balance()
        if size_usdc > balance:
            print(f"[paper] Skipping — insufficient balance ${balance:.2f} < ${size_usdc:.2f}")
            return False

        # One open position per market at a time
        if db.get_open_trade_for_market(market["id"]):
            return False

        # Select the token we are actually buying.
        # YES trades buy YES tokens; NO trades buy NO tokens.
        # Both use the BUY (ask) side — we always purchase shares of the winning token.
        if direction == "YES":
            token_id = market.get("token_id")
        else:
            token_id = market.get("no_token_id") or market.get("token_id")

        if not token_id:
            return False

        fill_price, filled_usdc = self._simulate_fill(token_id, "BUY", size_usdc)

        if filled_usdc < 1.0:
            print(f"[paper] Skipping — order book too thin to fill: {market['question'][:60]}")
            return False

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

        trade_id = db.insert_trade(trade)
        db.update_balance(-filled_usdc)
        db.record_account_value()

        # Link this trade back to the sizing_decision that authorized it, so
        # downstream A/B + Phase 3 attribution can join the chain
        # sizing_decisions -> trades. Wrapped — link failure must NOT block
        # the trade flow. See feedback_verify_writer_has_callers.md.
        sizing_id = estimate.get("sizing_id") if isinstance(estimate, dict) else None
        if sizing_id is not None and trade_id is not None:
            try:
                db.link_sizing_decision_to_trade(int(sizing_id), int(trade_id))
            except Exception as e:
                print(f"[paper] sizing<->trade link failed sizing_id={sizing_id} trade_id={trade_id}: {e}")

        try:
            snap = db.snapshot_trader_forecasts_on_entry(
                market_id=trade["market_id"],
                city=trade.get("city") or "",
                end_date=(trade.get("end_date") or "")[:10],
                threshold=trade.get("threshold"),
            )
            if snap > 0:
                print(f"              snapshot: {snap} trader forecast(s) frozen at entry")
        except Exception as e:
            print(f"              [warn] trader-forecast snapshot failed: {e}")

        print(f"[paper] OPEN  {direction:3s}  {market['question'][:55]}")
        print(f"              Size: ${filled_usdc:.2f}  Fill: {fill_price:.4f}  "
              f"Slippage: {slippage:.4f}  Edge: {edge_display:+.3f}")

        notify("info", "Order Filled",
               f"Bought {direction} on {market['question'][:60]}",
               fields={"Price": f"${fill_price:.4f}",
                        "Size": f"${filled_usdc:.2f}",
                        "Shares": f"{shares:.1f}"},
               color=COLOR_GREEN)
        return True

    # ── Extended position add-on ─────────────────────────────────────────────

    def place_extended_order(self, market: dict, direction: str, size_usdc: float,
                              estimate: dict, parent_trade_id: int, leg_number: int):
        """Place an add-on leg for an existing extended position."""
        balance = db.get_balance()
        if size_usdc > balance:
            print(f"[paper] Skipping add-on — insufficient balance ${balance:.2f} < ${size_usdc:.2f}")
            return

        # Parent trade already stores the correct directional token_id
        token_id = market.get("token_id")
        if not token_id:
            return

        fill_price, filled_usdc = self._simulate_fill(token_id, "BUY", size_usdc)

        if filled_usdc < 1.0:
            print(f"[paper] Skipping add-on — order book too thin")
            return

        # Slippage check with size reduction fallback (75%, 50%, 25%)
        yes_price = market.get("price", 0.5)
        if direction == "YES":
            token_mid = yes_price
        else:
            token_mid = 1.0 - yes_price

        max_slippage = WEATHER.get("entry_max_slippage_pct", 0.05)
        slippage_pct = abs(fill_price - token_mid) / token_mid if token_mid > 0 else 0
        if slippage_pct > max_slippage:
            for frac in (0.75, 0.50, 0.25):
                reduced = size_usdc * frac
                if reduced < 1.0:
                    break
                fill_price, filled_usdc = self._simulate_fill(token_id, "BUY", reduced)
                slippage_pct = abs(fill_price - token_mid) / token_mid if token_mid > 0 else 0
                if slippage_pct <= max_slippage:
                    break
            else:
                print(f"[paper] Skipping add-on — slippage too high even at 25% size")
                return
            if slippage_pct > max_slippage:
                print(f"[paper] Skipping add-on — slippage {slippage_pct:.1%} > {max_slippage:.0%}")
                return

        shares = filled_usdc / fill_price

        trade = {
            "market_id":       market["id"],
            "market_name":     market["question"],
            "token_id":        token_id,
            "end_date":        market.get("end_date"),
            "direction":       direction,
            "size_usdc":       filled_usdc,
            "shares":          shares,
            "entry_price":     market.get("price", 0.5),
            "fill_price":      fill_price,
            "current_price":   fill_price,
            "peak_price":      fill_price,
            "exit_price":      None,
            "opened_at":       datetime.utcnow().isoformat(),
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
        }

        db.insert_trade(trade)
        db.update_balance(-filled_usdc)
        db.record_account_value()

        slippage = abs(fill_price - token_mid)
        print(f"[paper] ADD-ON Leg {leg_number}  {direction:3s}  {market['question'][:50]}")
        print(f"              Size: ${filled_usdc:.2f}  Fill: {fill_price:.4f}  "
              f"Slippage: {slippage:.4f}")

    # ── Shotgun fire (parent + leg bets) ──────────────────────────────────────

    def place_fire(self, city: str, resolution_date: str, lead_hours: float,
                   center_f: float, density_json: str, budget_usd: float,
                   bets: list[dict]) -> int:
        """Record one shotgun fire (parent) + all its leg bets (children).
        Each leg is filled against the live book via _simulate_fill. Legs that
        can't fill (>0 stake but empty book) are skipped. Returns the fire_id,
        or 0 if no leg filled."""
        placed = []
        for b in bets:
            token_id = b.get("token_id") if b["side"] == "yes" else (b.get("no_token_id") or b.get("token_id"))
            if not token_id or b["stake_usd"] <= 0:
                continue
            fill_price, filled_usdc = self._simulate_fill(token_id, "BUY", b["stake_usd"])
            if filled_usdc <= 0 or fill_price <= 0:
                continue
            shares = filled_usdc / fill_price
            placed.append({**b, "fill_price": fill_price, "stake_usd": filled_usdc, "shares": shares})

        if not placed:
            return 0

        total_staked = sum(p["stake_usd"] for p in placed)
        fire_id = db.insert_fire(dict(
            fired_at_utc=datetime.now(timezone.utc).isoformat(), city=city,
            resolution_date=resolution_date, lead_hours=lead_hours, center_f=center_f,
            density_json=density_json, budget_usd=budget_usd, total_staked_usd=total_staked,
            n_legs=len(placed), status="open"))

        for p in placed:
            db.insert_bet(dict(
                fire_id=fire_id, market_id=p.get("market_id"),
                sub_market_condition_id=p.get("sub_market_condition_id"),
                group_item_title=p.get("group_item_title"), side=p["side"],
                ladder_idx=p.get("ladder_idx"), density=p.get("density"),
                mid_price=p.get("mid_price"), edge=p.get("edge"), stake_usd=p["stake_usd"],
                fill_price=p["fill_price"], shares=p["shares"], status="open",
                resolved_outcome=None, pnl=None,
                winset_kind=p.get("winset_kind"), winset_payload_json=p.get("winset_payload_json"),
                price_trajectory_json="[]"))

        db.update_balance(-total_staked)
        db.record_account_value()
        print(f"[paper] FIRE {city} {resolution_date}  legs={len(placed)}  staked=${total_staked:.2f}")
        return fire_id

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

    def close_position(self, trade: dict, reason: str):
        """
        Close all legs of an extended position (or a single non-extended trade).
        Each leg gets its own close_full with individual P&L.
        """
        parent_id = trade.get("parent_trade_id") or trade["id"]
        legs = db.get_position_legs(parent_id)
        if not legs:
            self.close_full(trade, reason=reason)
            return
        for leg in legs:
            self.close_full(leg, reason=reason)

    def initiate_exit(self, trade: dict, reason: str):
        """Paper mode: no GTC orders — close immediately."""
        self.close_position(trade, reason=reason)

    def manage_pending_exit(self, trade: dict):
        """Paper mode: no pending exits — no-op."""
        pass

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

        notify("info", "Position Closed",
               f"Exited {trade['market_name'][:60]} — {reason}",
               fields={"P&L": f"${pnl:+.2f}",
                        "Reason": reason},
               color=COLOR_GREEN)

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
