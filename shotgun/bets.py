"""Bucket bet selection/sizing + win-set resolution. Pure functions."""
from __future__ import annotations

from typing import Optional, Sequence, Any


def _f_to_c_int(f: float) -> int:
    return int(round((f - 32.0) * 5.0 / 9.0))


def integer_fs_for_exact_c(exact_f: float) -> list[int]:
    """All integer °F values whose round-to-int-°C equals the c-int of `exact_f`.
    Brute-force over a small window — at most 3-4 integers ever match.
    """
    target_c = _f_to_c_int(exact_f)
    f_center = int(round(exact_f))
    return [f for f in range(f_center - 2, f_center + 3) if _f_to_c_int(f) == target_c]


def compute_winset(
    bound_lo_f: Optional[float], bound_hi_f: Optional[float], is_open_tail: bool,
) -> Optional[tuple[str, list[int] | int]]:
    """Return win-set descriptor for a snapshot, or None if bounds are malformed."""
    if is_open_tail:
        if bound_lo_f is None and bound_hi_f is not None:
            return ("tail_bottom", int(round(bound_hi_f)))
        if bound_hi_f is None and bound_lo_f is not None:
            return ("tail_top", int(round(bound_lo_f)))
        return None
    if bound_lo_f is None or bound_hi_f is None:
        return None
    # exact (°C-derived): bot stores bound_lo_f == bound_hi_f
    if abs(bound_lo_f - bound_hi_f) < 1e-6:
        return ("closed", integer_fs_for_exact_c(bound_lo_f))
    # range (°F-integer): bounds are integer °F endpoints inclusive
    lo_i, hi_i = int(round(bound_lo_f)), int(round(bound_hi_f))
    if hi_i < lo_i:
        return None
    return ("closed", list(range(lo_i, hi_i + 1)))


def winset_density(
    winset: tuple[str, list[int] | int],
    ladder: Sequence[tuple[Optional[float], Optional[float]]],
    density: Sequence[float],
) -> float:
    """Estimate P(snapshot's win set occurs) from the ladder's per-bucket density.
    For closed ladder buckets, density is split uniformly across the bucket's integer-°F values.
    For tail buckets, full bucket density is attributed (a single ladder tail covers many integers).
    """
    kind, payload = winset
    if kind == "tail_bottom":
        # ladder index 0 is the bottom tail (lo=None); use its full density
        return float(density[0])
    if kind == "tail_top":
        # ladder index len-1 is the top tail (hi=None)
        return float(density[len(density) - 1])
    # closed
    assert isinstance(payload, list)
    # For each ladder bucket, count payload integers that fall in it
    total = 0.0
    for i, (lo, hi) in enumerate(ladder):
        if lo is None or hi is None:
            continue  # don't attribute tail density to a closed win-set
        lo_i, hi_i = int(round(lo)), int(round(hi))
        width = hi_i - lo_i + 1  # 2 for the 2°F-wide closed buckets
        if width <= 0:
            continue
        n_in = sum(1 for f in payload if lo_i <= f <= hi_i)
        if n_in:
            total += float(density[i]) * (n_in / width)
    return total


def winset_resolved(winset: tuple[str, list[int] | int], daily_max_f: float) -> bool:
    """True iff the observed integer-rounded daily max satisfies the win set."""
    f_int = int(round(daily_max_f))
    kind, payload = winset
    if kind == "tail_bottom":
        return f_int <= payload  # type: ignore[operator]
    if kind == "tail_top":
        return f_int >= payload  # type: ignore[operator]
    assert isinstance(payload, list)
    return f_int in payload


def mass_core_indices(density: list[float], frac: float) -> set[int]:
    """Smallest set of bucket indices whose densities sum to >= `frac` of total.

    Buckets are taken in descending density order; ties break by lower index.
    `frac >= 1.0` returns all nonzero-density buckets. Empty/zero density -> empty set.
    """
    total = float(sum(density))
    if total <= 0.0:
        return set()
    target = frac * total
    order = sorted(range(len(density)), key=lambda i: (-density[i], i))
    chosen: set[int] = set()
    acc = 0.0
    for i in order:
        if density[i] <= 0.0:
            break
        chosen.add(i)
        acc += density[i]
        if acc >= target:
            break
    return chosen


def classify_rejections(
    rows: list[dict],
    density_vector: list[float],
    mode: str,
    mass_core_frac: float,
    edge_threshold: float,
    price_min: float,
    price_max: float,
) -> list[dict]:
    """For each row that build_bucket_bets would NOT select, return a dict with
    the would-be side, its edge, in_core flag, and the gating skip_reason.

    Pure mirror of build_bucket_bets' selection branch — kept in lockstep so the
    counterfactual log records exactly why each bucket was filtered. Rows that
    WOULD be selected are omitted (they become real bets, not shadows).

    skip_reason values:
      not-core      — YES had edge+price but the bucket isn't in the mass-core
      edge<thresh   — neither side cleared edge_threshold
      yes-price-band— YES had core+edge but mid outside [price_min, price_max]
      no-cost-band  — NO had edge but (1-mid) outside [price_min, price_max]
    """
    core = mass_core_indices(density_vector, mass_core_frac) if mode in (
        "dist_yes", "dist_yes_no") else None
    out: list[dict] = []
    for r in rows:
        idx = r["ladder_idx"]
        d = r["density"]
        mid = r["mid_price"]
        in_core = bool(core is not None and idx in core)

        yes_edge = d - mid
        yes_price_ok = price_min <= mid <= price_max
        no_cost = 1.0 - mid
        no_edge = mid - d
        no_cost_ok = price_min <= no_cost <= price_max

        if mode == "edge_shotgun":
            if yes_edge >= edge_threshold and yes_price_ok:
                continue  # would be selected
            reason = "edge<thresh" if yes_edge < edge_threshold else "yes-price-band"
            out.append(_shadow_row(r, "yes", yes_edge, in_core, reason))
            continue

        # dist_yes / dist_yes_no
        yes_ok = in_core and yes_edge >= edge_threshold and yes_price_ok
        if yes_ok:
            continue  # would be selected as YES
        no_ok = (mode == "dist_yes_no" and no_edge >= edge_threshold and no_cost_ok)
        if no_ok:
            continue  # would be selected as NO

        # Rejected — pick the most informative (side, reason).
        if no_edge >= edge_threshold and not no_cost_ok:
            out.append(_shadow_row(r, "no", no_edge, in_core, "no-cost-band"))
        elif yes_edge >= edge_threshold and not in_core:
            out.append(_shadow_row(r, "yes", yes_edge, in_core, "not-core"))
        elif yes_edge >= edge_threshold and not yes_price_ok:
            out.append(_shadow_row(r, "yes", yes_edge, in_core, "yes-price-band"))
        else:
            # Whichever side is closer to clearing edge is the "would" side.
            if no_edge > yes_edge:
                out.append(_shadow_row(r, "no", no_edge, in_core, "edge<thresh"))
            else:
                out.append(_shadow_row(r, "yes", yes_edge, in_core, "edge<thresh"))
    return out


def _shadow_row(r: dict, side: str, edge: float, in_core: bool, reason: str) -> dict:
    """Project a candidate row into the shadow-bet record shape."""
    return dict(
        market_id=r.get("market_id"),
        sub_market_condition_id=r.get("sub_market_condition_id"),
        group_item_title=r.get("group_item_title"),
        would_side=side, skip_reason=reason,
        ladder_idx=r.get("ladder_idx"), density=r.get("density"),
        mid_price=r.get("mid_price"), edge=float(edge), in_core=int(in_core),
        winset_kind=r.get("winset_kind"),
        winset_payload_json=r.get("winset_payload_json"),
    )


def build_bucket_bets(
    rows: list[dict],
    density_vector: list[float],
    mode: str,
    mass_core_frac: float,
    edge_threshold: float,
    price_min: float,
    price_max: float,
    sizing: str,
    budget_per_city_day: float = 1.0,
) -> list[dict]:
    """Select + size bucket bets for ONE (city, resolution_date) group.

    rows: replayed sub-market rows; each has ladder_idx, density (its bucket's), mid_price,
          plus passthrough fields copied verbatim onto each emitted bet.
    density_vector: the 11-bucket forecast density for this city/day (for mass-core).
    Returns a list of bet dicts with added keys: side ('yes'|'no'), stake_usd.
    """
    core = mass_core_indices(density_vector, mass_core_frac) if mode in (
        "dist_yes", "dist_yes_no") else None

    selected: list[tuple[dict, str]] = []  # (row, side)
    for r in rows:
        idx = r["ladder_idx"]
        d = r["density"]
        mid = r["mid_price"]
        if mode == "edge_shotgun":
            if (d - mid) >= edge_threshold and price_min <= mid <= price_max:
                selected.append((r, "yes"))
        elif mode in ("dist_yes", "dist_yes_no"):
            in_core = idx in core
            yes_ok = in_core and (d - mid) >= edge_threshold and price_min <= mid <= price_max
            if yes_ok:
                selected.append((r, "yes"))
                continue
            if mode == "dist_yes_no":
                no_cost = 1.0 - mid
                no_edge = (1.0 - d) - no_cost  # = mid - d
                if no_edge >= edge_threshold and price_min <= no_cost <= price_max:
                    selected.append((r, "no"))
        else:
            raise ValueError(f"unknown mode: {mode}")

    if not selected:
        return []

    # Sizing
    if sizing == "flat":
        weights = [1.0] * len(selected)
        budget = float(len(selected))  # flat: each gets budget_per_city_day
    elif sizing == "weighted":
        weights = [r["density"] if side == "yes" else (1.0 - r["density"])
                   for r, side in selected]
        budget = budget_per_city_day  # shared budget per city/day
    else:
        raise ValueError(f"unknown sizing: {sizing}")

    wsum = float(sum(weights)) or 1.0
    bets: list[dict] = []
    for (r, side), w in zip(selected, weights):
        stake = budget_per_city_day if sizing == "flat" else budget * (w / wsum)
        bet = dict(r)
        bet["side"] = side
        bet["stake_usd"] = stake
        if side == "no":
            bet["edge"] = r["mid_price"] - r["density"]   # NO-side edge; YES edge was density - mid
        bets.append(bet)
    return bets
