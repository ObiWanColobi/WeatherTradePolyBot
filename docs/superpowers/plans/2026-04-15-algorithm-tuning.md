# Algorithm Tuning Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Apply five algorithm-level tuning changes from the 2026-04-15 trade review: block YES trades, add late-game divergence exit, raise ensemble conviction threshold to 0.85, scale Kelly by ensemble margin, and add per-city strict conviction/margin overrides.

**Architecture:** Config-driven gates in [weather_entry.py](weather_entry.py), direction guard in [weather_decision.py](weather_decision.py) and [weather_extended.py](weather_extended.py), sizing multiplier in [weather_sizing.py](weather_sizing.py), and a new exit check in [executor/weather_exit.py](executor/weather_exit.py). No new files, no DB schema changes.

**Tech Stack:** Python, existing `WEATHER` config dict in [config.py](config.py), existing dataclass-based decision pipeline.

**Ordering rationale:** Simplest → most invasive. Change 3 first (1-line), then Change 1 (guard), Change 5 (per-city), Change 4 (signature change), Change 2 (most complex — rewrites exit lock logic). Each task commits independently so any single change can be reverted cleanly.

**Spec:** [docs/superpowers/specs/2026-04-15-algorithm-tuning-design.md](docs/superpowers/specs/2026-04-15-algorithm-tuning-design.md)

---

## File Map

| File | Changes |
|---|---|
| [config.py](config.py) | Conviction 0.70→0.85, 3 late-game keys, 4 city-strict keys, remove `exit_no_exit_hours_to_close` |
| [weather_decision.py](weather_decision.py) | Reject `direction == "yes"`, pass `ensemble_margin_c` to `kelly_size` |
| [weather_entry.py](weather_entry.py) | City-strict conviction + margin override |
| [weather_sizing.py](weather_sizing.py) | New `ensemble_margin_c` kwarg; margin scaling multiplier |
| [executor/weather_exit.py](executor/weather_exit.py) | Late-game divergence check; remove 2h exit lock |
| [weather_extended.py](weather_extended.py) | Skip YES parent positions; pass `ensemble_margin_c` to `kelly_size` |

---

### Task 1: Raise ensemble conviction threshold to 0.85

**Files:**
- Modify: [config.py:72](config.py#L72)

- [ ] **Step 1: Change the value**

In [config.py](config.py), find line 72:
```python
    "entry_min_ensemble_conviction": 0.70,  # ensemble must be >=70% or <=30% YES
```

Replace with:
```python
    "entry_min_ensemble_conviction": 0.85,  # ensemble must be >=85% or <=15% YES (raised from 0.70 on 2026-04-15 — trade review showed 43% WR in 70–85% tier)
```

- [ ] **Step 2: Sanity check the config loads**

Run:
```bash
python -c "from config import WEATHER; print(WEATHER['entry_min_ensemble_conviction'])"
```
Expected: `0.85`

- [ ] **Step 3: Commit**

```bash
git add config.py
git commit -m "config: raise ensemble conviction threshold to 0.85"
```

---

### Task 2: Remove YES trades — decision layer guard

**Files:**
- Modify: [weather_decision.py:100](weather_decision.py#L100) (insert new guard immediately after direction computed, before entry gate)

- [ ] **Step 1: Add the YES rejection guard**

In [weather_decision.py](weather_decision.py), find lines 100–103:
```python
        direction = "yes" if mdl_prob > mkt_price else "no"
        days      = candidate.get("days_to_resolution", 0)
        ens_n     = candidate.get("ens_n", 0)
        ens_pct   = candidate.get("ens_pct")
```

Insert a new block immediately after the `direction = ...` line:
```python
        direction = "yes" if mdl_prob > mkt_price else "no"

        # ── 0a. YES trades disabled (2026-04-15 — trade review) ──────────────
        if direction == "yes":
            rejected.append(DecisionResult(
                candidate=candidate,
                verdict="REJECTED",
                reason="YES trades disabled (algorithm tuning 2026-04-15)",
                direction=direction,
                checks={},
            ))
            continue

        days      = candidate.get("days_to_resolution", 0)
        ens_n     = candidate.get("ens_n", 0)
        ens_pct   = candidate.get("ens_pct")
```

- [ ] **Step 2: Sanity-check the module imports cleanly**

Run:
```bash
python -c "import weather_decision; print('ok')"
```
Expected: `ok`

- [ ] **Step 3: Commit**

```bash
git add weather_decision.py
git commit -m "feat: reject YES trades in decision layer"
```

---

### Task 3: Remove YES trades — extended positions guard

**Files:**
- Modify: [weather_extended.py:66](weather_extended.py#L66) (add guard immediately after `direction` is read)

- [ ] **Step 1: Add the YES skip guard**

In [weather_extended.py](weather_extended.py), find lines 65–67:
```python
    parent_id = trade["id"]
    direction = trade["direction"].upper()

    # 0. Rejected-attempt cooldown (in-memory, survives only current session)
```

Replace with:
```python
    parent_id = trade["id"]
    direction = trade["direction"].upper()

    # 0. YES positions disabled (2026-04-15) — skip any legacy open YES position
    if direction == "YES":
        return _reject(trade, "YES trades disabled (algorithm tuning 2026-04-15)")

    # 0. Rejected-attempt cooldown (in-memory, survives only current session)
```

- [ ] **Step 2: Sanity-check the module imports cleanly**

Run:
```bash
python -c "import weather_extended; print('ok')"
```
Expected: `ok`

- [ ] **Step 3: Commit**

```bash
git add weather_extended.py
git commit -m "feat: skip YES positions in extended pass"
```

---

### Task 4: Add city-strict config keys

**Files:**
- Modify: [config.py:88](config.py#L88) (insert after unanimous weak-edge block, before EXIT CONDITIONS block starting at line 90)

- [ ] **Step 1: Add the four new keys**

In [config.py](config.py), find line 88:
```python
    "entry_unanimous_min_edge_pct":    0.07,   # relaxed floor when unanimous (vs 12% normal)
```

Insert immediately after (before the blank line that precedes the EXIT CONDITIONS comment block):
```python
    "entry_unanimous_min_edge_pct":    0.07,   # relaxed floor when unanimous (vs 12% normal)

    # ── Per-city strict overrides (2026-04-15) ────────────────────────────────
    # Cities with weak ensemble reliability or frequent coastal temp swings
    # require near-unanimous conviction and a wider margin than the default.
    # Research TODO: build a data-driven reliability score after 100+ resolved trades.
    "entry_city_adjustment_enabled": True,
    "entry_city_strict_cities": [
        "chicago", "dallas", "atlanta", "toronto",   # US inland — weak ensemble reliability
        "london", "wellington",                       # coastal — temp swings
    ],
    "entry_city_strict_min_conviction": 0.95,  # require near-unanimous for these cities (vs 0.85 default)
    "entry_city_strict_min_margin_c":   4.0,   # require wider margin for these cities (vs 3.0 default)
```

- [ ] **Step 2: Sanity check**

Run:
```bash
python -c "from config import WEATHER; print(WEATHER['entry_city_strict_cities'], WEATHER['entry_city_strict_min_conviction'], WEATHER['entry_city_strict_min_margin_c'])"
```
Expected: `['chicago', 'dallas', 'atlanta', 'toronto', 'london', 'wellington'] 0.95 4.0`

- [ ] **Step 3: Commit**

```bash
git add config.py
git commit -m "config: add per-city strict conviction/margin overrides"
```

---

### Task 5: City-strict gate in `weather_entry.py`

**Files:**
- Modify: [weather_entry.py:32-44](weather_entry.py#L32-L44) (read new config keys)
- Modify: [weather_entry.py:82-113](weather_entry.py#L82-L113) (override thresholds when city is in strict list)

- [ ] **Step 1: Read the new config keys at module load**

In [weather_entry.py](weather_entry.py), find the config block (lines 41–44):
```python
_MIN_ENSEMBLE_MARGIN_C       = WEATHER.get("entry_min_ensemble_margin_c",       2.0)
_MIN_ENSEMBLE_MARGIN_FLOOR   = WEATHER.get("entry_min_ensemble_margin_c_floor", 1.5)
_UNANIMOUS_MIN_CONVICTION    = WEATHER.get("entry_unanimous_min_conviction",    0.97)
_UNANIMOUS_MIN_EDGE_PCT      = WEATHER.get("entry_unanimous_min_edge_pct",      0.07)
```

Add immediately after:
```python
_MIN_ENSEMBLE_MARGIN_C       = WEATHER.get("entry_min_ensemble_margin_c",       2.0)
_MIN_ENSEMBLE_MARGIN_FLOOR   = WEATHER.get("entry_min_ensemble_margin_c_floor", 1.5)
_UNANIMOUS_MIN_CONVICTION    = WEATHER.get("entry_unanimous_min_conviction",    0.97)
_UNANIMOUS_MIN_EDGE_PCT      = WEATHER.get("entry_unanimous_min_edge_pct",      0.07)
# Per-city strict overrides (2026-04-15) — cities with weak forecast reliability
_CITY_ADJUSTMENT_ENABLED     = WEATHER.get("entry_city_adjustment_enabled",     True)
_CITY_STRICT_CITIES          = {c.lower() for c in WEATHER.get("entry_city_strict_cities", [])}
_CITY_STRICT_MIN_CONVICTION  = WEATHER.get("entry_city_strict_min_conviction",  0.95)
_CITY_STRICT_MIN_MARGIN_C    = WEATHER.get("entry_city_strict_min_margin_c",    4.0)
```

- [ ] **Step 2: Override conviction and margin thresholds for strict cities**

In [weather_entry.py](weather_entry.py), find the ensemble conviction block starting at line 81:
```python
    ens_pct    = ens_yes / ens_n
    conviction = max(ens_pct, 1 - ens_pct)
    checks["ensemble"] = {
        "ok":    conviction >= _MIN_ENSEMBLE_CONVICTION,
        "value": f"{ens_pct:.0%} {ens_yes}/{ens_n}",
        "need":  f">={_MIN_ENSEMBLE_CONVICTION:.0%} or <={1-_MIN_ENSEMBLE_CONVICTION:.0%}",
    }

    if not checks["ensemble"]["ok"]:
        return EntryDecision(ok=False, reason="ensemble conviction too low", checks=checks)
```

Replace with:
```python
    ens_pct    = ens_yes / ens_n
    conviction = max(ens_pct, 1 - ens_pct)

    # Per-city strict override: weak-reliability cities require higher conviction
    # and a wider ensemble margin. Applied before the standard checks so the
    # stricter bar is the effective floor for these cities.
    city_lower        = (scan_data.get("city", "") or "").lower()
    city_is_strict    = _CITY_ADJUSTMENT_ENABLED and city_lower in _CITY_STRICT_CITIES
    min_conviction    = _CITY_STRICT_MIN_CONVICTION if city_is_strict else _MIN_ENSEMBLE_CONVICTION
    min_margin_ceiling = _CITY_STRICT_MIN_MARGIN_C if city_is_strict else _MIN_ENSEMBLE_MARGIN_C

    checks["ensemble"] = {
        "ok":    conviction >= min_conviction,
        "value": f"{ens_pct:.0%} {ens_yes}/{ens_n}",
        "need":  f">={min_conviction:.0%} or <={1-min_conviction:.0%}"
                 + (f" [strict city {city_lower}]" if city_is_strict else ""),
    }

    if not checks["ensemble"]["ok"]:
        reason = (
            f"ensemble conviction too low for strict city {city_lower} (need >={min_conviction:.0%})"
            if city_is_strict
            else "ensemble conviction too low"
        )
        return EntryDecision(ok=False, reason=reason, checks=checks)
```

- [ ] **Step 3: Feed the city-strict margin ceiling into the existing conviction-scaled margin check**

In [weather_entry.py](weather_entry.py), find the margin check block (lines 99–113 in the pre-edit file):
```python
    ens_margin = scan_data.get("ensemble_margin_c")
    if ens_margin is not None and _MIN_ENSEMBLE_MARGIN_C > 0:
        scale = (conviction - 1.0) / (_MIN_ENSEMBLE_CONVICTION - 1.0)   # 0 at unanimous → 1 at min conviction
        required_margin = _MIN_ENSEMBLE_MARGIN_FLOOR + (_MIN_ENSEMBLE_MARGIN_C - _MIN_ENSEMBLE_MARGIN_FLOOR) * scale
```

Replace with (use `min_margin_ceiling` in place of the constant, and scale from the effective `min_conviction`):
```python
    ens_margin = scan_data.get("ensemble_margin_c")
    if ens_margin is not None and min_margin_ceiling > 0:
        # Scale from unanimous (conviction=1.0, margin=floor) to the effective
        # min_conviction for this city (margin=min_margin_ceiling).
        scale = (conviction - 1.0) / (min_conviction - 1.0) if min_conviction < 1.0 else 0.0
        required_margin = _MIN_ENSEMBLE_MARGIN_FLOOR + (min_margin_ceiling - _MIN_ENSEMBLE_MARGIN_FLOOR) * scale
```

- [ ] **Step 4: Remove dead YES-branch code from the min_fill_price check**

Since Task 2 rejects `direction == "yes"` at the decision layer, the YES branch of the min_fill_price check in [weather_entry.py](weather_entry.py) is unreachable when called from the normal pipeline. Simplify to the NO path only and drop the `_MIN_FILL_PRICE_YES` module-level read.

In [weather_entry.py](weather_entry.py), find line 40:
```python
_MIN_FILL_PRICE          = WEATHER.get("entry_min_fill_price",          0.15)
_MIN_FILL_PRICE_YES      = WEATHER.get("entry_min_fill_price_yes",      0.25)
```

Replace with:
```python
_MIN_FILL_PRICE          = WEATHER.get("entry_min_fill_price",          0.15)
# _MIN_FILL_PRICE_YES removed 2026-04-15 — YES trades disabled at decision layer.
# Config key still present in config.py for reference but no longer read.
```

Then find the min_fill_price check block (lines 181–195):
```python
    if direction is not None:
        yes_price   = market.get("price", 0.5)
        fill_price  = yes_price if direction.lower() == "yes" else (1.0 - yes_price)
        floor       = _MIN_FILL_PRICE_YES if direction.lower() == "yes" else _MIN_FILL_PRICE
        checks["min_fill_price"] = {
            "ok":    fill_price >= floor,
            "value": f"${fill_price:.3f}",
            "need":  f">=${floor:.2f}",
        }
        if not checks["min_fill_price"]["ok"]:
            return EntryDecision(
                ok=False,
                reason=f"token price ${fill_price:.3f} below minimum ${floor:.2f} — noise risk too high",
                checks=checks,
            )
```

Replace with:
```python
    if direction is not None:
        # YES trades disabled at decision layer (2026-04-15) — only NO path remains.
        # The backward-compatible YES handling was removed along with _MIN_FILL_PRICE_YES.
        yes_price   = market.get("price", 0.5)
        fill_price  = 1.0 - yes_price if direction.lower() == "no" else yes_price
        floor       = _MIN_FILL_PRICE
        checks["min_fill_price"] = {
            "ok":    fill_price >= floor,
            "value": f"${fill_price:.3f}",
            "need":  f">=${floor:.2f}",
        }
        if not checks["min_fill_price"]["ok"]:
            return EntryDecision(
                ok=False,
                reason=f"token price ${fill_price:.3f} below minimum ${floor:.2f} — noise risk too high",
                checks=checks,
            )
```

- [ ] **Step 5: Sanity-check the module imports cleanly**

Run:
```bash
python -c "import weather_entry; print('ok')"
```
Expected: `ok`

- [ ] **Step 6: Smoke test the gate against a synthetic strict-city candidate**

Run:
```bash
python -c "
from weather_entry import check_entry
# chicago with 84% conviction (below 0.95 strict floor) — should be rejected
mkt = {'token_id': 'tkn', 'price': 0.30, 'volume': 10000, 'end_date': '2099-01-01T23:59:59Z', 'id': 'm1'}
scan = {'city': 'chicago', 'ensemble_n': 69, 'yes_ensemble': 11, 'ensemble_margin_c': 5.0, 'edge_prob': 0.20}
d = check_entry(mkt, scan, direction='no')
print('strict-city 84%:', d.ok, '-', d.reason)
# chicago with 97% conviction (above strict floor, margin 5°C) — should pass ensemble+margin checks
scan2 = {'city': 'chicago', 'ensemble_n': 69, 'yes_ensemble': 2, 'ensemble_margin_c': 5.0, 'edge_prob': 0.20}
d2 = check_entry(mkt, scan2, direction='no')
print('strict-city 97%:', d2.ok, '-', d2.reason)
"
```
Expected: first line reports `False` with \"strict city chicago\" in reason; second line gets past ensemble and margin (may fail later on volume/spread lookup — that's fine, we're verifying the city gate, not the full pipeline).

- [ ] **Step 7: Commit**

```bash
git add weather_entry.py
git commit -m "feat: per-city strict conviction and margin overrides; remove dead YES path"
```

---

### Task 6: Kelly margin scaling in `weather_sizing.py`

**Files:**
- Modify: [weather_sizing.py:46-112](weather_sizing.py#L46-L112) (add `ensemble_margin_c` kwarg + margin_mult)

- [ ] **Step 1: Add `ensemble_margin_c` kwarg to `kelly_size`**

In [weather_sizing.py](weather_sizing.py), find lines 46–54:
```python
def kelly_size(
    balance:            float,
    model_prob:         float,
    market_price:       float,
    direction:          str,
    ensemble_n:         int = 0,
    days_to_resolution: int = 0,
    unanimous:          bool = False,
) -> float:
```

Replace with:
```python
def kelly_size(
    balance:            float,
    model_prob:         float,
    market_price:       float,
    direction:          str,
    ensemble_n:         int = 0,
    days_to_resolution: int = 0,
    unanimous:          bool = False,
    ensemble_margin_c:  float | None = None,
) -> float:
```

- [ ] **Step 2: Apply the margin multiplier after the horizon discount**

In [weather_sizing.py](weather_sizing.py), find lines 92–97:
```python
    # Forecast horizon discount -- further out = less reliable forecast
    horizon_mult = _HORIZON_DISCOUNTS.get(days_to_resolution, _HORIZON_DISCOUNT_DEFAULT)
    kelly *= horizon_mult

    # Apply fractional Kelly and balance cap
    fraction = kelly * _KELLY_FRACTION
```

Replace with:
```python
    # Forecast horizon discount -- further out = less reliable forecast
    horizon_mult = _HORIZON_DISCOUNTS.get(days_to_resolution, _HORIZON_DISCOUNT_DEFAULT)
    kelly *= horizon_mult

    # Ensemble margin scaling (2026-04-15) — bet size proportional to forecast
    # distance from threshold. 0°C → 0.0x, 5°C+ → 1.0x. Small margin = coin-flip
    # risk = smaller bet. Large margin = high conviction = full Kelly.
    if ensemble_margin_c is not None:
        margin_mult = min(abs(ensemble_margin_c) / 5.0, 1.0)
        kelly *= margin_mult

    # Apply fractional Kelly and balance cap
    fraction = kelly * _KELLY_FRACTION
```

- [ ] **Step 3: Mirror the kwarg on `size_summary` so log diagnostics stay aligned**

In [weather_sizing.py](weather_sizing.py), find lines 115–123:
```python
def size_summary(
    balance:            float,
    model_prob:         float,
    market_price:       float,
    direction:          str,
    ensemble_n:         int = 0,
    days_to_resolution: int = 0,
    unanimous:          bool = False,
) -> dict:
```

Replace with:
```python
def size_summary(
    balance:            float,
    model_prob:         float,
    market_price:       float,
    direction:          str,
    ensemble_n:         int = 0,
    days_to_resolution: int = 0,
    unanimous:          bool = False,
    ensemble_margin_c:  float | None = None,
) -> dict:
```

And in the same function find line 141:
```python
    size = kelly_size(balance, model_prob, market_price, direction, ensemble_n, days_to_resolution, unanimous)
```

Replace with:
```python
    size = kelly_size(balance, model_prob, market_price, direction, ensemble_n, days_to_resolution, unanimous, ensemble_margin_c)
```

- [ ] **Step 4: Sanity-check the margin multiplier**

Run:
```bash
python -c "
from weather_sizing import kelly_size
base = kelly_size(1000.0, 0.85, 0.40, 'no', ensemble_n=69, days_to_resolution=0)
m0   = kelly_size(1000.0, 0.85, 0.40, 'no', ensemble_n=69, days_to_resolution=0, ensemble_margin_c=0.0)
m25  = kelly_size(1000.0, 0.85, 0.40, 'no', ensemble_n=69, days_to_resolution=0, ensemble_margin_c=2.5)
m5   = kelly_size(1000.0, 0.85, 0.40, 'no', ensemble_n=69, days_to_resolution=0, ensemble_margin_c=5.0)
print('baseline (no margin)=', base)
print('margin 0.0°C=', m0, '(should be 0.0)')
print('margin 2.5°C=', m25, '(should be ~half of baseline)')
print('margin 5.0°C=', m5, '(should be ~= baseline)')
"
```
Expected: baseline and `m5` are equal (margin_mult = 1.0); `m0` = 0.0; `m25` ≈ half of baseline (margin_mult = 0.5). Note that the `kelly_min_bet_usdc` floor may zero out small values — if so, raise the `model_prob` to 0.95 and re-run for clearer numbers.

- [ ] **Step 5: Commit**

```bash
git add weather_sizing.py
git commit -m "feat: ensemble margin scaling in Kelly sizing"
```

---

### Task 7: Pass `ensemble_margin_c` from decision layer

**Files:**
- Modify: [weather_decision.py:186-189](weather_decision.py#L186-L189) (pass kwarg into `kelly_size`)

- [ ] **Step 1: Thread `ensemble_margin_c` into the Kelly call**

In [weather_decision.py](weather_decision.py), find lines 186–191:
```python
        # ── 4. Kelly size (with horizon discount) ─────────────────────────────
        # Unanimous entries use a separate, smaller cap (see weather_sizing.py).
        is_unanimous = entry.checks.get("edge", {}).get("unanimous", False)
        size = kelly_size(balance, mdl_prob, mkt_price, direction, ens_n, days, unanimous=is_unanimous)
        if not is_unanimous:
            size = min(size, max_bet)   # only apply the override cap to normal trades
```

Replace with:
```python
        # ── 4. Kelly size (with horizon discount + margin scaling) ───────────
        # Unanimous entries use a separate, smaller cap (see weather_sizing.py).
        is_unanimous    = entry.checks.get("edge", {}).get("unanimous", False)
        scan_data       = candidate.get("_scan_data") or {}
        ens_margin_c    = scan_data.get("ensemble_margin_c")
        size = kelly_size(
            balance, mdl_prob, mkt_price, direction, ens_n, days,
            unanimous=is_unanimous,
            ensemble_margin_c=ens_margin_c,
        )
        if not is_unanimous:
            size = min(size, max_bet)   # only apply the override cap to normal trades
```

- [ ] **Step 2: Sanity-check the module imports cleanly**

Run:
```bash
python -c "import weather_decision; print('ok')"
```
Expected: `ok`

- [ ] **Step 3: Commit**

```bash
git add weather_decision.py
git commit -m "feat: pass ensemble_margin_c to Kelly in decision layer"
```

---

### Task 8: Pass `ensemble_margin_c` from extended positions

**Files:**
- Modify: [weather_extended.py:141-159](weather_extended.py#L141-L159) (pass kwarg into `kelly_size`)

- [ ] **Step 1: Thread `ensemble_margin_c` into the Kelly call**

In [weather_extended.py](weather_extended.py), find lines 141–159:
```python
    # 5. Kelly sizing
    # Re-derive unanimity from current ensemble — a normal-entry parent can reach
    # unanimous conviction by leg time, and vice versa. Don't inherit from parent.
    balance      = db.get_balance()
    model_prob   = current_scan.get("model_prob") or 0
    market_price = current_scan.get("market_price") or 0.5
    days_to_res  = current_scan.get("days_to_resolution") or 0
    cur_conviction = max(cur_ratio, 1.0 - cur_ratio)
    is_unanimous   = cur_conviction >= _UNANIMOUS_MIN_CONVICTION

    size = kelly_size(
        balance=balance,
        model_prob=model_prob,
        market_price=market_price,
        direction=direction,
        ensemble_n=cur_ens_n,
        days_to_resolution=days_to_res,
        unanimous=is_unanimous,
    )
```

Replace with:
```python
    # 5. Kelly sizing
    # Re-derive unanimity from current ensemble — a normal-entry parent can reach
    # unanimous conviction by leg time, and vice versa. Don't inherit from parent.
    balance        = db.get_balance()
    model_prob     = current_scan.get("model_prob") or 0
    market_price   = current_scan.get("market_price") or 0.5
    days_to_res    = current_scan.get("days_to_resolution") or 0
    ens_margin_c   = current_scan.get("ensemble_margin_c")
    cur_conviction = max(cur_ratio, 1.0 - cur_ratio)
    is_unanimous   = cur_conviction >= _UNANIMOUS_MIN_CONVICTION

    size = kelly_size(
        balance=balance,
        model_prob=model_prob,
        market_price=market_price,
        direction=direction,
        ensemble_n=cur_ens_n,
        days_to_resolution=days_to_res,
        unanimous=is_unanimous,
        ensemble_margin_c=ens_margin_c,
    )
```

- [ ] **Step 2: Sanity-check the module imports cleanly**

Run:
```bash
python -c "import weather_extended; print('ok')"
```
Expected: `ok`

- [ ] **Step 3: Commit**

```bash
git add weather_extended.py
git commit -m "feat: pass ensemble_margin_c to Kelly in extended pass"
```

---

### Task 9: Late-game exit — add config keys

**Files:**
- Modify: [config.py:94-99](config.py#L94-L99) (add 3 new keys, remove `exit_no_exit_hours_to_close`)

- [ ] **Step 1: Replace `exit_no_exit_hours_to_close` with the three late-game keys**

In [config.py](config.py), find lines 94–99:
```python
    "exit_ensemble_flip_threshold":     0.25,  # exit if ensemble shifts >25pts from entry
    "exit_adverse_price_move_pct":      0.30,  # exit if price moves >30% of fill against position
    "exit_adverse_min_move_cents":      0.10,  # floor: never exit on moves smaller than 10 cents (prevents noise exits on cheap tokens)
    "exit_adverse_min_hold_minutes":    60,    # no adverse exit within first 60 min (post-entry price settling)
    "exit_adverse_skip_unanimous_pct":  .9,  # skip adverse exit when ensemble conviction >= 90% (trust the model)
    "exit_no_exit_hours_to_close":      2.0,   # never exit within 2h of resolution
```

Replace with:
```python
    "exit_ensemble_flip_threshold":     0.25,  # exit if ensemble shifts >25pts from entry
    "exit_adverse_price_move_pct":      0.30,  # exit if price moves >30% of fill against position
    "exit_adverse_min_move_cents":      0.10,  # floor: never exit on moves smaller than 10 cents (prevents noise exits on cheap tokens)
    "exit_adverse_min_hold_minutes":    60,    # no adverse exit within first 60 min (post-entry price settling)
    "exit_adverse_skip_unanimous_pct":  .9,    # skip adverse exit when ensemble conviction >= 90% (trust the model)

    # ── Late-game market divergence exit (2026-04-15) ─────────────────────────
    # In the final hours before close, if our token price has collapsed but the
    # ensemble still shows high conviction for us, the ensemble is stale and the
    # market is already pricing the true outcome — exit.
    "exit_late_game_hours":              8.0,   # window: last 8h before close
    "exit_late_game_market_floor":       0.40,  # exit if our token drops below this
    "exit_late_game_ensemble_threshold": 0.70,  # ...AND ensemble still shows >= this conviction for us
```

(Note: the previous `exit_no_exit_hours_to_close` key is deleted. The 2h lock is removed in Task 10.)

- [ ] **Step 2: Sanity check**

Run:
```bash
python -c "from config import WEATHER; print(WEATHER['exit_late_game_hours'], WEATHER['exit_late_game_market_floor'], WEATHER['exit_late_game_ensemble_threshold']); print('no_exit_key_present=', 'exit_no_exit_hours_to_close' in WEATHER)"
```
Expected: `8.0 0.4 0.7` then `no_exit_key_present= False`

- [ ] **Step 3: Commit**

```bash
git add config.py
git commit -m "config: add late-game divergence exit keys; remove 2h lock"
```

---

### Task 10: Late-game exit — logic in `weather_exit.py`

**Files:**
- Modify: [executor/weather_exit.py:23-29](executor/weather_exit.py#L23-L29) (swap config keys)
- Modify: [executor/weather_exit.py:52-60](executor/weather_exit.py#L52-L60) (remove 2h lock, insert late-game check between flip and adverse)

- [ ] **Step 1: Swap the config block**

In [executor/weather_exit.py](executor/weather_exit.py), find lines 23–29:
```python
# ── Config ────────────────────────────────────────────────────────────────────
_ENSEMBLE_FLIP_THRESHOLD  = WEATHER.get("exit_ensemble_flip_threshold",   0.25)
_ADVERSE_PRICE_MOVE_PCT   = WEATHER.get("exit_adverse_price_move_pct",    0.30)
_ADVERSE_MIN_MOVE         = WEATHER.get("exit_adverse_min_move_cents",    0.10)
_ADVERSE_MIN_HOLD_MINUTES = WEATHER.get("exit_adverse_min_hold_minutes",  60)
_ADVERSE_SKIP_UNANIMOUS   = WEATHER.get("exit_adverse_skip_unanimous_pct", 0.90)
_NO_EXIT_HOURS            = WEATHER.get("exit_no_exit_hours_to_close",    2.0)
```

Replace with:
```python
# ── Config ────────────────────────────────────────────────────────────────────
_ENSEMBLE_FLIP_THRESHOLD  = WEATHER.get("exit_ensemble_flip_threshold",   0.25)
_ADVERSE_PRICE_MOVE_PCT   = WEATHER.get("exit_adverse_price_move_pct",    0.30)
_ADVERSE_MIN_MOVE         = WEATHER.get("exit_adverse_min_move_cents",    0.10)
_ADVERSE_MIN_HOLD_MINUTES = WEATHER.get("exit_adverse_min_hold_minutes",  60)
_ADVERSE_SKIP_UNANIMOUS   = WEATHER.get("exit_adverse_skip_unanimous_pct", 0.90)
# Late-game market divergence (2026-04-15) — replaces blanket 2h exit lock
_LATE_GAME_HOURS          = WEATHER.get("exit_late_game_hours",              8.0)
_LATE_GAME_MARKET_FLOOR   = WEATHER.get("exit_late_game_market_floor",       0.40)
_LATE_GAME_ENS_THRESHOLD  = WEATHER.get("exit_late_game_ensemble_threshold", 0.70)
```

- [ ] **Step 2: Update the module docstring**

In [executor/weather_exit.py](executor/weather_exit.py), find lines 7–15:
```python
Exit triggers (checked in priority order):
  1. Ensemble flip  — new model run shifts consensus >25pts from entry signal
  2. Price adverse  — market price moves >15c against position (crowd knows something)
  3. Within 2h close — never exit in final 2 hours (ride it out)

What we deliberately do NOT do:
  - Bleed-off ladders (leaving money on the table before resolution)
  - Trailing stops (too short a time horizon for meaningful price recovery)
  - Edge exhaustion exits (market correcting toward your price = hold, not exit)
```

Replace with:
```python
Exit triggers (checked in priority order):
  1. Ensemble flip             — new model run shifts consensus >25pts from entry signal
  2. Late-game divergence      — within 8h of close, our token < 40% yet ensemble >= 70% (stale forecast)
  3. Price adverse             — market price moves significantly against position (crowd knows something)

What we deliberately do NOT do:
  - Bleed-off ladders (leaving money on the table before resolution)
  - Trailing stops (too short a time horizon for meaningful price recovery)
  - Edge exhaustion exits (market correcting toward your price = hold, not exit)
  - Blanket final-hour lock (removed 2026-04-15 — the late-game divergence check now covers this window)
```

- [ ] **Step 3: Remove the 2h lock and add the late-game divergence check**

In [executor/weather_exit.py](executor/weather_exit.py), find lines 50–70:
```python
    """

    # ── Never exit within 2 hours of resolution ───────────────────────────────
    # Use the trade's stored end_date (validated at entry time) rather than
    # re-fetching from the API, which can return a different field or stale value.
    hours_left = _hours_to_close(trade.get("end_date") or market_data.get("end_date", ""))
    if hours_left is not None and hours_left <= _NO_EXIT_HOURS:
        return WeatherExitSignal(
            should_exit=False,
            reason=f"within {_NO_EXIT_HOURS}h of close — holding to resolution",
        )

    fill_price    = trade.get("fill_price", 0.5)

    # current_price is maintained by update_open_positions() via CLOB midpoint.
    # For YES trades it's the YES token price; for NO trades it's the NO token price.
    # Both are on the same scale as fill_price, so the adverse check is symmetric
    # with no direction-aware logic needed. We never use the Gamma API price here —
    # that was the source of persistent 0.0000 false readings.
    current_price = trade.get("current_price")
```

Replace with:
```python
    """
    # Hours remaining until market closes — used by the late-game divergence check
    # and read from the trade's stored end_date to avoid stale API re-fetches.
    hours_left    = _hours_to_close(trade.get("end_date") or market_data.get("end_date", ""))

    fill_price    = trade.get("fill_price", 0.5)

    # current_price is maintained by update_open_positions() via CLOB midpoint.
    # For YES trades it's the YES token price; for NO trades it's the NO token price.
    # Both are on the same scale as fill_price, so the adverse check is symmetric
    # with no direction-aware logic needed. We never use the Gamma API price here —
    # that was the source of persistent 0.0000 false readings.
    current_price = trade.get("current_price")
```

- [ ] **Step 4: Insert the late-game divergence check between the ensemble flip block and the adverse price move block**

In [executor/weather_exit.py](executor/weather_exit.py), find the end of the ensemble flip block and the start of the adverse check (post-edit, these will be around lines 85–95):
```python
            if was_high and flip >= _ENSEMBLE_FLIP_THRESHOLD:
                return WeatherExitSignal(
                    should_exit=True,
                    reason=(
                        f"ensemble flipped {entry_pyes:.0%} -> "
                        f"{current_ensemble_pct:.0%} ({flip:.0%} shift)"
                    ),
                    urgent=True,
                )

    # ── 2. Adverse price move ─────────────────────────────────────────────────
```

Replace with:
```python
            if was_high and flip >= _ENSEMBLE_FLIP_THRESHOLD:
                return WeatherExitSignal(
                    should_exit=True,
                    reason=(
                        f"ensemble flipped {entry_pyes:.0%} -> "
                        f"{current_ensemble_pct:.0%} ({flip:.0%} shift)"
                    ),
                    urgent=True,
                )

    # ── 2. Late-game market divergence (2026-04-15) ───────────────────────────
    # In the final hours, if our token price has collapsed but the ensemble
    # still shows strong conviction for us, the ensemble is stale and the
    # market is already pricing the true outcome. Exit before resolution.
    #
    # Both `current_price` and `direction_adjusted_conviction` are on the same
    # scale: the price/probability of the token we hold. Direction handling is
    # already baked into `current_price` (see comment above); for the ensemble
    # conviction we derive it from the trade direction + raw P(YES).
    if (
        hours_left is not None
        and hours_left <= _LATE_GAME_HOURS
        and current_price is not None
        and current_ensemble_pct is not None
    ):
        trade_dir = (trade.get("direction") or "").lower()
        ens_conviction_for_us = (
            current_ensemble_pct if trade_dir == "yes" else (1.0 - current_ensemble_pct)
        )
        if (
            current_price < _LATE_GAME_MARKET_FLOOR
            and ens_conviction_for_us >= _LATE_GAME_ENS_THRESHOLD
        ):
            return WeatherExitSignal(
                should_exit=True,
                reason=(
                    f"late-game market divergence — {hours_left:.1f}h left, "
                    f"token at {current_price:.1%}, ensemble still "
                    f"{ens_conviction_for_us:.0%} conviction (stale forecast)"
                ),
                urgent=True,
            )

    # ── 3. Adverse price move ─────────────────────────────────────────────────
```

- [ ] **Step 5: Sanity-check the module imports cleanly**

Run:
```bash
python -c "import executor.weather_exit; print('ok')"
```
Expected: `ok`

- [ ] **Step 6: Smoke test the late-game divergence path**

Run:
```bash
python -c "
from datetime import datetime, timedelta, timezone
from executor.weather_exit import check_weather_exit

close_in_4h = (datetime.now(timezone.utc) + timedelta(hours=4)).isoformat()

# NO trade, token collapsed to 30%, ensemble still 0/69 (100% NO conviction) — should EXIT
trade = {
    'direction': 'NO',
    'fill_price': 0.75,
    'current_price': 0.30,
    'end_date': close_in_4h,
    'opened_at': (datetime.now(timezone.utc) - timedelta(hours=10)).isoformat(),
    'entry_ensemble_yes': 0,
    'entry_ensemble_n': 69,
}
sig = check_weather_exit(trade, {}, current_ensemble_pct=0.0)
print('divergence NO:', sig.should_exit, '-', sig.reason)

# Same trade but close_in is 20h away — outside late-game window, should HOLD
far = (datetime.now(timezone.utc) + timedelta(hours=20)).isoformat()
trade2 = {**trade, 'end_date': far}
sig2 = check_weather_exit(trade2, {}, current_ensemble_pct=0.0)
print('outside window: ', sig2.should_exit, '-', sig2.reason)
"
```
Expected: first line: `True - late-game market divergence ...`; second line: `False - hold — no exit condition met` (or adverse exit depending on the `current_price` move; either way, NOT the late-game reason).

Note: the \"NO trade\" smoke test uses an existing (legacy) NO position — Task 2's decision-layer guard blocks new YES entries, not exits on existing positions. Exit logic remains direction-agnostic.

- [ ] **Step 7: Commit**

```bash
git add executor/weather_exit.py
git commit -m "feat: late-game divergence exit, remove 2h lock"
```

---

### Task 11: Final integration verification

**Files:** none modified — runs existing test suite + full-app import.

- [ ] **Step 1: Run the existing test suite**

Run:
```bash
python -m pytest tests/ -q
```
Expected: same pass/fail baseline as before Task 1. No weather-layer unit tests exist (confirmed during planning), so the suite should report identical results to `git stash && pytest tests/ -q && git stash pop`. If any test that previously passed now fails, stop and investigate.

- [ ] **Step 2: Full-app import smoke test**

Run:
```bash
python -c "import weather_bot; import weather_decision; import weather_entry; import weather_extended; import weather_sizing; import executor.weather_exit; print('all modules ok')"
```
Expected: `all modules ok`

- [ ] **Step 3: Dry-run audit against live catalog (read-only)**

Run:
```bash
python -c "
import weather_scanner, weather_decision, db
cands = weather_scanner.run_scan()
results = weather_decision.evaluate(cands, balance=db.get_balance())
weather_decision.print_audit(results)
yes_rejected = [r for r in results if r.verdict == 'REJECTED' and 'YES trades disabled' in r.reason]
strict_rejected = [r for r in results if 'strict city' in r.reason]
print(f'YES rejections: {len(yes_rejected)}')
print(f'strict-city rejections: {len(strict_rejected)}')
"
```
Expected: audit prints without exceptions. YES rejections and strict-city rejections are reported (counts depend on what's currently in the catalog — 0 is valid if no candidates match those conditions).

- [ ] **Step 4: Update memory notes and commit if changes touched anything**

If Step 1 or Step 2 failed and required a follow-up fix, commit that fix separately:
```bash
git add -u
git commit -m "fix: post-algorithm-tuning integration fix"
```

If no changes, skip this step. (No commit for a no-op verification pass.)

---

## Post-Implementation

After all tasks complete:

1. Update `tasks/todo.md` with a review section noting what was implemented.
2. Update the project memory entry `project_algorithm_tuning.md` — change status from "NOT YET IMPLEMENTED" to "IMPLEMENTED 2026-04-15" with commit refs.
3. Deploy to Kamatera live bot (see `project_kamatera_deployment.md` ops runbook) only after a clean paper-trading cycle has run locally with the new config.
