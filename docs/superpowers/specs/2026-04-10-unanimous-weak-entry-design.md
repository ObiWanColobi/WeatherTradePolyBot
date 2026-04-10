# Spec: WEAK-Edge Unanimous Ensemble Entry

**Date:** 2026-04-10
**Status:** Approved

---

## Problem

The entry gate requires `edge_pct >= 12%` for all trades. When the meteorological ensemble is
near-unanimous (≥97% of members agree), there are trades where the market has mostly priced in
the outcome but not completely — leaving a small positive edge (5–11%) that the bot currently
skips entirely. These are "sure winners" with thin remaining spread, not weak model conviction.
The WEAK label describes price edge, not model confidence.

---

## Solution

Make the edge floor conviction-aware. When ensemble conviction meets a "unanimous" threshold,
lower the edge floor from 12% to 7%. Apply a separate, smaller bet cap to right-size these trades
relative to higher-edge opportunities.

---

## Entry Gate Changes (`weather_entry.py`)

### Existing logic (unchanged for normal trades)
- Conviction check: `conviction >= 0.70` (i.e., ens_pct >= 70% or <= 30%)
- Edge check: `edge_pct >= entry_min_edge_pct` (default 12%)

### New unanimous path
When `conviction >= entry_unanimous_min_conviction` (default 0.97, ~67/69 members):
- Edge floor is relaxed to `entry_unanimous_min_edge_pct` (default 0.07 = 7%)
- All other checks (ensemble margin, volume, spread, hours-to-close, min-fill-price) remain
  unchanged

**Why 7% floor:**
- Polymarket CLOB taker fee: ~2%
- Realistic slippage on high-conviction, mostly-priced-in markets: 1–3%
- 7% floor leaves ~2% net cushion under normal conditions

### Implementation
The existing edge check in `weather_entry.py` becomes:

```python
# Determine effective edge floor (unanimous conviction gets a relaxed floor)
_unanimous = conviction >= _UNANIMOUS_MIN_CONVICTION
effective_edge_floor = _UNANIMOUS_MIN_EDGE_PCT if _unanimous else _MIN_EDGE_PCT

checks["edge"] = {
    "ok":       edge_pct >= effective_edge_floor,
    "value":    f"{edge_pct:.1%}",
    "need":     f">={effective_edge_floor:.1%}",
    "unanimous": _unanimous,
}
```

The `unanimous` flag is included in `checks["edge"]` so the decision layer and logs can detect it.

---

## Sizing Changes (`weather_sizing.py`)

`kelly_size()` gains an `unanimous: bool = False` parameter. When `True`, the hard cap uses
`kelly_max_bet_usdc_unanimous` instead of `kelly_max_bet_usdc`.

**Why a separate cap instead of a Kelly fraction multiplier:**
For near-certain model probabilities (p ≈ 0.97–0.99), raw Kelly is always enormous (~0.8+)
regardless of thin edge, because the near-certain probability inflates the fraction. The $50 max
cap is the only lever that matters at current account size. A fraction multiplier would do nothing
meaningful here.

---

## Decision Layer Changes (`weather_decision.py`)

- Detects the `unanimous` flag from `checks["edge"]` after `check_entry()` returns
- Passes `unanimous=True` to `kelly_size()` when detected
- Appends `[unanimous]` tag to the log line for approved and skipped trades so these entries are
  visually distinct in bot output

---

## Config Keys (new)

```python
# config.py WEATHER dict
"entry_unanimous_min_conviction":   0.97,   # ≥97% of ensemble members (~67/69)
"entry_unanimous_min_edge_pct":     0.07,   # 7% floor when unanimous (vs 12% normal)
"kelly_max_bet_usdc_unanimous":    50.00,   # separate cap for unanimous-weak trades
```

---

## What Does Not Change

| Component | Status |
|---|---|
| Ensemble data hard block (`n < 10`) | Unchanged |
| Ensemble margin check (conviction-scaled) | Unchanged — still guards coin-flip zone |
| Volume, spread, hours-to-close, min-fill-price | Unchanged |
| Trader consensus | Informational only, unchanged |
| Normal trade sizing / Kelly fraction | Unchanged |
| Exit logic | Unchanged |

---

## Example Trade Scenarios

Balance: ~$1,700. `kelly_max_bet_usdc_unanimous = $50`.

| Ensemble | Direction | Market Price | Edge | Same-Day Size | 1-Day Size |
|---|---|---|---|---|---|
| 69/69 YES | YES | $0.90 | 9% | $50 (cap) | $50 (cap) |
| 69/69 YES | YES | $0.93 | 6% | $50 (cap) | $50 (cap) |
| 67/69 YES | YES | $0.91 | 8% | $50 (cap) | $50 (cap) |
| 0/69 (NO) | NO  | $0.08 YES ($0.92 NO) | 7% | $50 (cap) | $50 (cap) |

At current balance, unanimous-weak trades will almost always hit the $50 cap. As account grows,
Kelly fractions remain large (near-certain p), so the cap continues to bind — these will stay at
$50 until `kelly_max_bet_usdc_unanimous` is manually raised.

---

## Files Changed

| File | Change |
|---|---|
| `config.py` | Add 3 new keys to WEATHER dict |
| `weather_entry.py` | Conviction-aware edge floor; `unanimous` flag in checks |
| `weather_sizing.py` | `unanimous` param; apply separate cap |
| `weather_decision.py` | Detect `unanimous` flag; pass to sizing; log tag |

---

## Testing

- Dry-run bot pass: confirm trades that previously logged `edge too small` at ≥97% conviction now
  log `APPROVED [unanimous]`
- Confirm normal trades (conviction 70–96%) are unaffected
- Confirm trade size on unanimous entries is capped at `kelly_max_bet_usdc_unanimous`, not the
  normal cap
- Confirm ensemble margin check still blocks unanimous trades where margin < 1.5°C
