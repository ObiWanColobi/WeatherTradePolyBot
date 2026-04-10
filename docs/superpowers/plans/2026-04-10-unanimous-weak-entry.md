# Unanimous Weak-Edge Entry Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Relax the entry edge floor from 12% to 7% when ensemble conviction is ≥97% (~67/69 members), with a separate $50 bet cap, so the bot captures near-certain trades the market hasn't fully priced in.

**Architecture:** Conviction-aware edge floor in `weather_entry.py` (mirrors the existing conviction-scaled margin logic). The `unanimous` flag flows outward through `checks["edge"]` into the decision layer and extended positions pass, where it selects a separate bet cap in `weather_sizing.py`.

**Tech Stack:** Python, SQLite (via `db.py`), existing config pattern (`WEATHER` dict in `config.py`)

---

## File Map

| File | Change |
|---|---|
| `config.py` | Add 3 new keys to `WEATHER` dict |
| `weather_entry.py` | Read 2 new config keys; conviction-aware edge floor; `unanimous` flag in `checks["edge"]` |
| `weather_sizing.py` | Read 1 new config key; `unanimous: bool = False` param; apply separate cap |
| `weather_decision.py` | Extract `unanimous` from entry checks; pass to `kelly_size`; add `[unanimous]` log tag |
| `weather_extended.py` | Read unanimous threshold from config; re-derive `is_unanimous` from current ensemble; pass to `kelly_size` |

No new files. No DB schema changes.

---

### Task 1: Add config keys

**Files:**
- Modify: `config.py:57-71` (Kelly + entry conditions block)

- [ ] **Step 1: Add the three new keys after `entry_max_slippage_pct`**

In `config.py`, find this line:
```python
    "entry_max_slippage_pct":        0.05,  # max simulated fill slippage as % of mid (5%)
```

Add immediately after it:
```python
    # ── Unanimous weak-edge entry ─────────────────────────────────────────────
    # When ensemble conviction is >= this threshold (~67/69 members), the edge
    # floor is relaxed from entry_min_edge_pct to entry_unanimous_min_edge_pct.
    # 7% floor accounts for ~2% Polymarket taker fee + ~2-3% slippage cushion.
    "entry_unanimous_min_conviction":  0.97,   # ≥97% of ensemble members (~67/69)
    "entry_unanimous_min_edge_pct":    0.07,   # relaxed floor when unanimous (vs 12% normal)
    "kelly_max_bet_usdc_unanimous":   50.00,   # separate hard cap for unanimous-weak trades
```

- [ ] **Step 2: Commit**

```bash
git add config.py
git commit -m "config: add unanimous weak-edge entry keys"
```

---

### Task 2: Conviction-aware edge floor in `weather_entry.py`

**Files:**
- Modify: `weather_entry.py:32-44` (config block), `weather_entry.py:113-121` (edge check)

- [ ] **Step 1: Add two new config vars to the constants block**

In `weather_entry.py`, find the constants block at the top:
```python
_MIN_ENSEMBLE_MARGIN_C       = WEATHER.get("entry_min_ensemble_margin_c",       2.0)
_MIN_ENSEMBLE_MARGIN_FLOOR   = WEATHER.get("entry_min_ensemble_margin_c_floor", 1.5)
```

Add immediately after:
```python
_UNANIMOUS_MIN_CONVICTION    = WEATHER.get("entry_unanimous_min_conviction",    0.97)
_UNANIMOUS_MIN_EDGE_PCT      = WEATHER.get("entry_unanimous_min_edge_pct",      0.07)
```

- [ ] **Step 2: Replace the edge check with a conviction-aware version**

Find this block in `check_entry()` (around line 113):
```python
    # ── 2. Edge ───────────────────────────────────────────────────────────────
    edge_pct = scan_data.get("edge_prob", 0)
    checks["edge"] = {
        "ok":    edge_pct >= _MIN_EDGE_PCT,
        "value": f"{edge_pct:.1%}",
        "need":  f">={_MIN_EDGE_PCT:.1%}",
    }
    if not checks["edge"]["ok"]:
        return EntryDecision(ok=False, reason="edge too small", checks=checks)
```

Replace with:
```python
    # ── 2. Edge ───────────────────────────────────────────────────────────────
    # Unanimous ensembles (>=97% conviction) get a relaxed floor of 7% instead
    # of the normal 12%. The thin edge reflects a market that has mostly priced
    # in the outcome — there's still remaining value worth capturing.
    is_unanimous    = conviction >= _UNANIMOUS_MIN_CONVICTION
    effective_floor = _UNANIMOUS_MIN_EDGE_PCT if is_unanimous else _MIN_EDGE_PCT

    edge_pct = scan_data.get("edge_prob", 0)
    checks["edge"] = {
        "ok":        edge_pct >= effective_floor,
        "value":     f"{edge_pct:.1%}",
        "need":      f">={effective_floor:.1%}",
        "unanimous": is_unanimous,
    }
    if not checks["edge"]["ok"]:
        reason = (
            f"edge too small (unanimous: need >={_UNANIMOUS_MIN_EDGE_PCT:.0%})"
            if is_unanimous
            else "edge too small"
        )
        return EntryDecision(ok=False, reason=reason, checks=checks)
```

- [ ] **Step 3: Verify the file is syntactically valid**

```bash
cd f:/CodeProjects/TestCode1 && python -c "import weather_entry; print('OK')"
```
Expected: `OK`

- [ ] **Step 4: Commit**

```bash
git add weather_entry.py
git commit -m "feat: conviction-aware edge floor — unanimous entries use 7% floor"
```

---

### Task 3: Separate unanimous cap in `weather_sizing.py`

**Files:**
- Modify: `weather_sizing.py:37-40` (config block), `weather_sizing.py:47-109` (`kelly_size`), `weather_sizing.py:112-152` (`size_summary`)

- [ ] **Step 1: Add the new config var to the constants block**

In `weather_sizing.py`, find:
```python
_MAX_BET_USDC    = WEATHER.get("kelly_max_bet_usdc",   50.00)   # hard cap per trade
```

Replace with:
```python
_MAX_BET_USDC           = WEATHER.get("kelly_max_bet_usdc",           200.00)   # hard cap per trade
_MAX_BET_USDC_UNANIMOUS = WEATHER.get("kelly_max_bet_usdc_unanimous",  50.00)   # cap for unanimous-weak trades
```

- [ ] **Step 2: Add `unanimous` param to `kelly_size()` and apply the correct cap**

Find the function signature:
```python
def kelly_size(
    balance:            float,
    model_prob:         float,
    market_price:       float,
    direction:          str,
    ensemble_n:         int = 0,
    days_to_resolution: int = 0,
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
) -> float:
```

Then find the hard caps block inside `kelly_size()`:
```python
    # Hard caps
    size = min(size, _MAX_BET_USDC)
    size = min(size, balance * _MAX_BALANCE_PCT)
    size = max(size, 0.0)
```

Replace with:
```python
    # Hard caps — unanimous-weak trades use a separate, smaller cap
    cap  = _MAX_BET_USDC_UNANIMOUS if unanimous else _MAX_BET_USDC
    size = min(size, cap)
    size = min(size, balance * _MAX_BALANCE_PCT)
    size = max(size, 0.0)
```

- [ ] **Step 3: Add `unanimous` param to `size_summary()` for consistency**

Find:
```python
def size_summary(
    balance:            float,
    model_prob:         float,
    market_price:       float,
    direction:          str,
    ensemble_n:         int = 0,
    days_to_resolution: int = 0,
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
) -> dict:
```

Then find the `kelly_size` call inside `size_summary()`:
```python
    size = kelly_size(balance, model_prob, market_price, direction, ensemble_n, days_to_resolution)
```

Replace with:
```python
    size = kelly_size(balance, model_prob, market_price, direction, ensemble_n, days_to_resolution, unanimous)
```

- [ ] **Step 4: Verify**

```bash
cd f:/CodeProjects/TestCode1 && python -c "from weather_sizing import kelly_size; print(kelly_size(1700, 0.99, 0.90, 'yes', 69, 0, unanimous=True)); print(kelly_size(1700, 0.99, 0.90, 'yes', 69, 0, unanimous=False))"
```
Expected: first line prints `50.0`, second line prints `200.0` (or whatever max_bet is).

- [ ] **Step 5: Commit**

```bash
git add weather_sizing.py
git commit -m "feat: unanimous param in kelly_size — separate bet cap for unanimous-weak trades"
```

---

### Task 4: Wire unanimous flag through the decision layer

**Files:**
- Modify: `weather_decision.py:185-198` (Kelly size call), `weather_decision.py:205-212` (approval log)

- [ ] **Step 1: Extract `unanimous` from entry checks and pass to `kelly_size`**

In `weather_decision.py`, find the Kelly sizing block (around line 185):
```python
        # ── 4. Kelly size (with horizon discount) ─────────────────────────────
        size = kelly_size(balance, mdl_prob, mkt_price, direction, ens_n, days)
        size = min(size, max_bet)
```

Replace with:
```python
        # ── 4. Kelly size (with horizon discount) ─────────────────────────────
        # Unanimous entries use a separate, smaller cap (see weather_sizing.py).
        is_unanimous = entry.checks.get("edge", {}).get("unanimous", False)
        size = kelly_size(balance, mdl_prob, mkt_price, direction, ens_n, days, unanimous=is_unanimous)
        if not is_unanimous:
            size = min(size, max_bet)   # only apply the override cap to normal trades
```

- [ ] **Step 2: Add `[unanimous]` tag to the approved entry log**

In `weather_decision.py`, find the approval append block (around line 264):
```python
        approved.append(DecisionResult(
            candidate=candidate,
            verdict="APPROVED",
            reason="all checks passed",
            direction=direction,
            score=score,
            size_usdc=size,
            checks=entry.checks,
        ))
```

Replace with:
```python
        unanimous_tag = " [unanimous]" if is_unanimous else ""
        approved.append(DecisionResult(
            candidate=candidate,
            verdict="APPROVED",
            reason=f"all checks passed{unanimous_tag}",
            direction=direction,
            score=score,
            size_usdc=size,
            checks=entry.checks,
        ))
```

- [ ] **Step 3: Add `[unanimous]` tag to the rejected entry log in `weather_bot.py`**

In `weather_bot.py`, find the rejected log print (around line 182):
```python
        _tier  = "STRONG" if c["edge_pct"] >= 0.30 else "EDGE" if c["edge_pct"] >= 0.15 else "WEAK"
```

Replace with:
```python
        _ens_pct = c.get("ens_pct") or 0
        _conv    = max(_ens_pct, 1 - _ens_pct)
        _utag    = " [unanimous]" if _conv >= 0.97 else ""
        _tier    = "STRONG" if c["edge_pct"] >= 0.30 else "EDGE" if c["edge_pct"] >= 0.15 else f"WEAK{_utag}"
```

- [ ] **Step 4: Verify**

```bash
cd f:/CodeProjects/TestCode1 && python -c "import weather_decision; print('OK')"
```
Expected: `OK`

- [ ] **Step 5: Commit**

```bash
git add weather_decision.py weather_bot.py
git commit -m "feat: unanimous flag wired through decision layer — [unanimous] log tag + separate cap"
```

---

### Task 5: Unanimous cap for legs in `weather_extended.py`

**Files:**
- Modify: `weather_extended.py:21-28` (config block), `weather_extended.py:140-153` (Kelly sizing)

- [ ] **Step 1: Add the unanimous threshold to the config block**

In `weather_extended.py`, find:
```python
_MAX_EXPOSURE        = WEATHER.get("decision_max_exposure_pct", 0.90)
```

Add immediately after:
```python
_UNANIMOUS_MIN_CONVICTION = WEATHER.get("entry_unanimous_min_conviction", 0.97)
```

- [ ] **Step 2: Detect unanimity from current ensemble and pass to `kelly_size`**

Find the Kelly sizing block (around line 140):
```python
    # 5. Kelly sizing
    balance = db.get_balance()
    model_prob = current_scan.get("model_prob") or 0
    market_price = current_scan.get("market_price") or 0.5
    days_to_res = current_scan.get("days_to_resolution") or 0

    size = kelly_size(
        balance=balance,
        model_prob=model_prob,
        market_price=market_price,
        direction=direction,
        ensemble_n=cur_ens_n,
        days_to_resolution=days_to_res,
    )
```

Replace with:
```python
    # 5. Kelly sizing
    # Re-derive unanimity from current ensemble — a normal-entry parent can reach
    # unanimous conviction by leg time, and vice versa. Don't inherit from parent.
    balance    = db.get_balance()
    model_prob = current_scan.get("model_prob") or 0
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

Note: `cur_ratio` is already computed above this block as `cur_ens_yes / cur_ens_n`.

- [ ] **Step 3: Verify**

```bash
cd f:/CodeProjects/TestCode1 && python -c "import weather_extended; print('OK')"
```
Expected: `OK`

- [ ] **Step 4: Commit**

```bash
git add weather_extended.py
git commit -m "feat: unanimous cap applied to add-on legs when current ensemble is unanimous"
```

---

### Task 6: End-to-end verification

No test infrastructure exists in this repo — verification is via dry-run bot pass and log inspection.

- [ ] **Step 1: Confirm all modules import cleanly**

```bash
cd f:/CodeProjects/TestCode1 && python -c "
import config, weather_entry, weather_sizing, weather_decision, weather_extended
print('All imports OK')
"
```
Expected: `All imports OK`

- [ ] **Step 2: Spot-check sizing at unanimous threshold**

```bash
cd f:/CodeProjects/TestCode1 && python -c "
from weather_sizing import kelly_size
b = 1700

# Unanimous YES — should cap at 50
u = kelly_size(b, 0.99, 0.90, 'yes', 69, 0, unanimous=True)
# Normal strong trade — should cap at 200
n = kelly_size(b, 0.85, 0.50, 'yes', 60, 0, unanimous=False)
# Near-unanimous (67/69) — unanimous=True
u2 = kelly_size(b, 0.97, 0.91, 'yes', 69, 0, unanimous=True)

print(f'unanimous YES (mkt=0.90): \${u:.2f}  (expect 50.00)')
print(f'normal strong (mkt=0.50): \${n:.2f}  (expect 200.00)')
print(f'near-unanimous (mkt=0.91): \${u2:.2f}  (expect 50.00)')
"
```
Expected output:
```
unanimous YES (mkt=0.90): $50.00  (expect 50.00)
normal strong (mkt=0.50): $200.00  (expect 200.00)
near-unanimous (mkt=0.91): $50.00  (expect 50.00)
```

- [ ] **Step 3: Confirm entry gate passes at 7% with unanimous conviction**

```bash
cd f:/CodeProjects/TestCode1 && python -c "
from weather_entry import check_entry

# Simulate a unanimous YES market at 8% edge (currently blocked at 12%)
market   = {'price': 0.91, 'volume': 10000, 'end_date': '2099-01-01T23:59:59Z', 'token_id': None, 'id': 'test'}
scan     = {'edge_prob': 0.08, 'yes_ensemble': 68, 'ensemble_n': 69, 'ensemble_margin_c': 4.0, 'city': 'paris'}
decision = check_entry(market, scan, direction='yes')
print(f'ok={decision.ok}  reason={decision.reason}')
print(f'unanimous flag={decision.checks[\"edge\"].get(\"unanimous\")}')
"
```
Expected:
```
ok=True  reason=all checks passed
unanimous flag=True
```

- [ ] **Step 4: Confirm normal trade at 8% edge is still blocked**

```bash
cd f:/CodeProjects/TestCode1 && python -c "
from weather_entry import check_entry

# 70% conviction (just above normal threshold) — NOT unanimous — 8% edge should fail
market   = {'price': 0.62, 'volume': 10000, 'end_date': '2099-01-01T23:59:59Z', 'token_id': None, 'id': 'test'}
scan     = {'edge_prob': 0.08, 'yes_ensemble': 48, 'ensemble_n': 69, 'ensemble_margin_c': 4.0, 'city': 'paris'}
decision = check_entry(market, scan, direction='yes')
print(f'ok={decision.ok}  reason={decision.reason}')
print(f'unanimous flag={decision.checks[\"edge\"].get(\"unanimous\")}')
"
```
Expected:
```
ok=False  reason=edge too small
unanimous flag=False
```

- [ ] **Step 5: Commit verification confirmation**

```bash
git add .
git commit -m "chore: unanimous weak-edge entry — all verification checks pass"
```

---

### Task 7: Deploy to PythonAnywhere

- [ ] **Step 1: Push to remote**

```bash
git push origin main
```

- [ ] **Step 2: Pull on PythonAnywhere and restart bot**

SSH into PythonAnywhere, then:
```bash
cd ~/weather-bot && git pull origin main
# Restart via the PythonAnywhere dashboard task scheduler, or:
pkill -f weather_bot.py && python weather_bot.py &
```

- [ ] **Step 3: Watch bot logs for first unanimous entry**

Monitor logs for lines containing `[unanimous]`. A successful unanimous trade will look like:
```
  [entry] Paris          YES   edge=9%  score=0.842  size=$50.00  days_out=0  [unanimous]
```

And a skipped unanimous candidate (edge below 7%) will show:
```
  [skip] Paris           YES   ...  edge=5%  WEAK [unanimous]  -- entry gate: edge too small (unanimous: need >=7%)
```
