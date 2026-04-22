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

## 2026-04-22 — Sibling-redeemed claims (extended positions)

**Symptom:** Trades 17, 21, 27 stuck at `status='claim_pending'` / `claim_status='claim_no_balance'` indefinitely. Dashboard account_value over-stated by $33.08 (sum of their shares at $1/ea).

**Root cause:** Extended-position legs share a `token_id` (same neg-risk market, same side). The FIRST leg's claim tx reads the wallet's entire balance for that token and redeems ALL of it — crediting the combined USDC to the bot's ledger via `update_balance(balance_raw/1e6)`. When the SECOND leg's claim cycle runs, `get_token_balance` returns 0 → bot marks it `claim_no_balance` and leaves `status='claim_pending'`. The dashboard's `pending_val = sum(shares for claim_pending)` then double-counts: the USDC is already in `cash`, AND the shares are still in `pending_val`.

**Fix shape:** `_sweep_sibling_redeemed()` runs each cycle — for every `status='claim_pending'` trade, look up a sibling with same `token_id` and `claim_status='claim_confirmed'`; if found, close the trade referencing sibling's tx without re-crediting balance (`claim_status='claim_via_sibling'`). Self-heals existing phantoms and prevents future ones.

**General principle:** On-chain redemption is keyed by `(wallet, token_id)`, not by trade. The bot's trade-per-leg accounting must reconcile against the shared on-chain reality — check for sibling redemptions before treating "zero balance" as a lost/mystery claim.
