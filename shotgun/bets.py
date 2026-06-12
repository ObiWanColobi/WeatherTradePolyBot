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

    For closed ladder buckets, density is split uniformly across the bucket's integer-°F
    values. For a market TAIL bucket, the win set is the market's own threshold (the
    payload), NOT the ladder's tail edge — these differ whenever the market tail starts
    at a different temperature than `center ± offset`. We therefore integrate the density
    over exactly the integer °F values the payload threshold admits (H2 fix, 2026-06-11):
    pricing the tail against the ladder's fixed tail mass produced phantom edge because
    settlement (winset_resolved) already used the payload threshold.

    tail_top payload T: win iff f >= T.   tail_bottom payload T: win iff f <= T.
    The ladder's own open tail can't be subdivided by integer (unbounded), so its full
    mass is attributed when the threshold reaches into it (a small, conservative-leaning
    approximation; closed buckets in between are apportioned exactly).
    """
    kind, payload = winset
    if kind == "tail_bottom":
        assert isinstance(payload, int)
        return _tail_density(ladder, density, payload, top=False)
    if kind == "tail_top":
        assert isinstance(payload, int)
        return _tail_density(ladder, density, payload, top=True)
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


def _tail_density(
    ladder: Sequence[tuple[Optional[float], Optional[float]]],
    density: Sequence[float],
    threshold: int,
    top: bool,
) -> float:
    """Density mass consistent with a market tail threshold.
    top=True  -> win iff f >= threshold (tail_top).
    top=False -> win iff f <= threshold (tail_bottom).
    Closed ladder buckets are apportioned by the fraction of their integer °F values
    that satisfy the threshold. An open ladder tail is unbounded, so it can't be
    apportioned by integer — it contributes its FULL mass whenever its unbounded range
    can satisfy the query (a top tail [lo,∞) always reaches above any threshold for a
    f>=threshold query; a bottom tail (-∞,hi] always reaches below for a f<=threshold
    query). A tail that points the wrong way only qualifies if its finite edge already
    satisfies the threshold."""
    total = 0.0
    for i, (lo, hi) in enumerate(ladder):
        if lo is None:                      # bottom open tail (-∞, hi]
            hi_i = int(round(hi))
            if not top:
                total += float(density[i])  # unbounded below always reaches f <= threshold
            elif hi_i >= threshold:
                total += float(density[i])  # finite top edge already satisfies f >= threshold
            continue
        if hi is None:                      # top open tail [lo, ∞)
            lo_i = int(round(lo))
            if top:
                total += float(density[i])  # unbounded above always reaches f >= threshold
            elif lo_i <= threshold:
                total += float(density[i])  # finite bottom edge already satisfies f <= threshold
            continue
        lo_i, hi_i = int(round(lo)), int(round(hi))
        width = hi_i - lo_i + 1
        if width <= 0:
            continue
        if top:
            n_in = sum(1 for f in range(lo_i, hi_i + 1) if f >= threshold)
        else:
            n_in = sum(1 for f in range(lo_i, hi_i + 1) if f <= threshold)
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


def _exec_prices(r: dict) -> tuple[float, float]:
    """Executable per-share prices for (YES, NO) of a candidate row (H3 fix).

    A taker BUYS at the touch, not the mid: YES pays best_ask, NO pays (1 - best_bid)
    [NO-ask = 1 - YES-bid]. Scoring edge at the mid systematically over-states edge by
    half the spread on each side — fatal on the thin longshot books this strategy
    fires into. Falls back to the YES mid (NO: 1-mid) when the book wasn't captured,
    so legacy rows / tests that pass only mid_price keep their old behavior.

    Returns (yes_price, no_cost): the price YES would pay and the price NO would pay.
    """
    mid = r["mid_price"]
    ba = r.get("best_ask")
    bb = r.get("best_bid")
    yes_price = float(ba) if ba is not None else float(mid)
    no_cost = (1.0 - float(bb)) if bb is not None else (1.0 - float(mid))
    return yes_price, no_cost


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
        in_core = bool(core is not None and idx in core)

        # H3: score at the executable touch (best_ask for YES, 1-best_bid for NO),
        # not the mid. yes_edge = d - ask; no_edge = (1-d) - (1-bid) = bid - d.
        yes_price, no_cost = _exec_prices(r)
        yes_edge = d - yes_price
        yes_price_ok = price_min <= yes_price <= price_max
        no_edge = (1.0 - d) - no_cost
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
        # M1: carry the book state so the lever analysis can price the would-side at
        # an executable touch (YES best_ask / NO 1-best_bid), not the frictionless mid.
        best_bid=r.get("best_bid"), best_ask=r.get("best_ask"),
        liquidity_num=r.get("liquidity_num"),
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
        # H3: score at the executable touch (YES best_ask / NO 1-best_bid), not mid.
        yes_price, no_cost = _exec_prices(r)
        if mode == "edge_shotgun":
            if (d - yes_price) >= edge_threshold and price_min <= yes_price <= price_max:
                selected.append((r, "yes"))
        elif mode in ("dist_yes", "dist_yes_no"):
            in_core = idx in core
            yes_ok = in_core and (d - yes_price) >= edge_threshold and price_min <= yes_price <= price_max
            if yes_ok:
                selected.append((r, "yes"))
                continue
            if mode == "dist_yes_no":
                no_edge = (1.0 - d) - no_cost  # NO fair value (1-d) minus NO cost
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
        yes_price, no_cost = _exec_prices(r)
        bet = dict(r)
        bet["side"] = side
        bet["stake_usd"] = stake
        # Store the touch-based edge actually realized at selection (H3): YES = d-ask,
        # NO = (1-d)-no_cost. Mirrors the selection test above so the logged edge is
        # the executable one, not the optimistic mid edge.
        bet["edge"] = (r["density"] - yes_price) if side == "yes" else ((1.0 - r["density"]) - no_cost)
        bets.append(bet)
    return bets
