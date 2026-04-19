# Lessons Learned

## 2026-04-13: CLOB API returns uppercase status strings
- `get_order()` returns `status=MATCHED` (uppercase), not `matched` (lowercase)
- Always `.lower()` external API string fields before comparing
- This caused 4 real fills to be silently dropped — positions existed on Polymarket but not in the bot's DB

## 2026-04-13: FOK orders can show matched status without trade details
- CLOB may return `matched` status but `associate_trades` is empty and `size_matched` is 0
- Need a fallback: query `get_trades()` by order_id to find actual fills
- Never assume "no trade details = no fill" — always verify via trade history

## 2026-04-13: place_order must return success/failure
- Callers need to know if the order actually filled to avoid misleading counts
- "Entered 4 new positions" was a lie — none were recorded in DB
- Return bool from place_order, only count actual fills

## 2026-04-19 — Phantom claims post-mortem

1. **On-chain status=1 does NOT prove effect.** A tx can mine successfully and move $0 (e.g., USDC-collateralized CTF redemption when the proxy holds wcol-collateralized positions; or NegRiskAdapter.redeemPositions on a half-resolved questionId). For every value-moving call, verify by parsing event logs (PayoutRedemption, Transfer), not receipt status.

2. **eth_call != state-change proof.** Simulation only proves the call path doesn't revert at the current block. For any value op, only a live tx with post-state inspection is conclusive.

3. **Polymarket weather markets use wcol as CTF collateral, not USDC.** The NegRiskAdapter wraps USDC -> wcol on deposit, so CTF positions are keyed by (wcol, conditionId), not (USDC, conditionId). Our old claim path redeemed against the USDC collateral (balance=0 -> phantom). Fix: redeem against wcol directly then unwrap, bundled in one Factory.proxy tx.

4. **The NegRiskAdapter can be half-resolved.** getDetermined=True and getResult=1 do NOT imply redeemPositions will work; getPayout / getMetadata must also be set. If they aren't, the adapter underflows. There's no user-facing way to fix this — use the CTF bypass.
