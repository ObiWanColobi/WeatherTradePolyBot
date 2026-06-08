"""Per-bucket liquidity stake cap. The feasibility study (#17) flagged thin
>=50c NO-leg books (median liquidity $44) as fragile to size — cap each leg's
stake at cap_frac * liquidity_num. cap_frac<=0 disables. Mutates+returns the
same list (caller passes a fresh bet list)."""
from __future__ import annotations


def apply_liquidity_cap(bets: list[dict], cap_frac: float) -> list[dict]:
    if cap_frac <= 0.0:
        return bets
    for b in bets:
        liq = b.get("liquidity_num")
        if liq is not None and liq > 0:
            cap = cap_frac * float(liq)
            if b["stake_usd"] > cap:
                b["stake_usd"] = cap
    return bets
