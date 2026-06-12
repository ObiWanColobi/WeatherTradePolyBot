"""Shotgun strategy core: turn one city-day's live buckets + live ensemble into
a sized bet list. Pure-ish — network access is injected (ensemble_fetch) so this
is unit-testable. Wraps the validated research math in shotgun/."""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Callable

from shotgun.forecast import members_f_for_date, build_density
from shotgun.bucket_boundaries import build_ladder, daily_max_to_bucket_idx
from shotgun.bets import compute_winset, winset_density, build_bucket_bets, classify_rejections
from shotgun.sizing import apply_liquidity_cap


@dataclass
class ShotgunConfig:
    mode: str = "dist_yes_no"
    edge_threshold: float = 0.12
    mass_core_frac: float = 0.50
    price_min: float = 0.05
    price_max: float = 0.50
    vol_min: float = 200.0
    sizing_mode: str = "weighted"
    budget_per_city_day: float = 50.0
    per_bucket_liq_cap_frac: float = 0.10
    # Clamp a bucket's forecast density to >= density_floor before edge is computed.
    # Prevents a model-blind density=0 bucket from registering a free +(1-mid) NO
    # edge (e.g. tel-aviv 31°C NO lost when 31°C actually happened — the model had
    # assigned literal 0 to the realized outcome). See review (2026-06-11). 0 disables.
    density_floor: float = 0.01


def plan_fire(
    city: str,
    resolution_date: str,
    buckets: list[dict],
    cfg: ShotgunConfig,
    coords: dict,
    ensemble_fetch: Callable,
) -> tuple[list[dict], float | None, list | None, list[dict]]:
    """Return (bets, center_f, density, rejected) for this city-day.

    `rejected` is the counterfactual log: candidate buckets that passed the
    cheap pre-filters (volume/winset/mid) but were rejected by a strategy gate
    (mass-core / price-band / edge). Each is tagged with would_side + skip_reason
    so a later analysis can ask "would loosening a lever have made money?". It is
    [] when there are no reviewable candidates.

    Returns ([], None, None, []) when there is no usable ensemble; ([], center_f,
    density, rejected) when the ensemble is fine but no bets pass the filters.
    Returning the center_f/density computed here lets the caller persist the EXACT
    provenance the bets were sized against (no second fetch, no update race).

    buckets: live sub-market dicts. Each needs: sub_market_condition_id,
      group_item_title, bound_lo_f, bound_hi_f, is_open_tail, mid_price,
      best_bid, best_ask, liquidity_num, volume_24h, token_id, no_token_id, market_id.
    coords: {"lat","lon","tz"}. ensemble_fetch(lat,lon,tz) -> get_ensemble_forecasts shape.
    """
    lat = coords.get("lat")
    lon = coords.get("lon")
    if lat is None or lon is None:
        return [], None, None, []
    ensemble = ensemble_fetch(lat, lon, coords.get("tz", "auto"))
    members_f = members_f_for_date(ensemble, resolution_date)
    center_f, density = build_density(members_f)
    if center_f is None:
        return [], None, None, []
    ladder = build_ladder(center_f)
    dv = list(density)

    rows = []
    for m in buckets:
        if (m.get("volume_24h") or 0.0) < cfg.vol_min:
            continue
        ws = compute_winset(m.get("bound_lo_f"), m.get("bound_hi_f"), bool(m.get("is_open_tail")))
        if ws is None:
            continue
        d = winset_density(ws, ladder, density)
        # Probability floor: a winset the model assigns ~0 mass is exactly where the
        # model is blindest, not a free NO edge. Clamp before edge is derived so a
        # density-0 bucket can't masquerade as a high-confidence NO. (mass-core uses
        # the unclamped density_vector below, so the forecast shape is untouched.)
        if cfg.density_floor > 0.0:
            d = max(d, cfg.density_floor)
        try:
            if ws[0] == "closed":
                if not ws[1]:
                    continue          # malformed closed winset — don't misclassify
                rep = ws[1][len(ws[1]) // 2]
                idx = daily_max_to_bucket_idx(rep, ladder)
            elif ws[0] == "tail_bottom":
                idx = 0
            elif ws[0] == "tail_top":
                idx = len(ladder) - 1
            else:
                continue              # unknown winset kind — skip, don't misclassify
        except ValueError:
            continue
        mid = m.get("mid_price")
        if mid is None:
            continue
        rows.append(dict(
            # bucket_idx is the DB-schema alias for ladder_idx (always equal)
            ladder_idx=idx, density=float(d), mid_price=float(mid),
            sub_market_condition_id=m.get("sub_market_condition_id"),
            group_item_title=m.get("group_item_title"),
            winset_kind=ws[0], winset_payload_json=json.dumps(ws[1]),
            center_f=center_f, bucket_idx=idx, edge=float(d) - float(mid),
            volume_24h=m.get("volume_24h"),
            best_bid=m.get("best_bid"), best_ask=m.get("best_ask"),
            liquidity_num=m.get("liquidity_num"),
            token_id=m.get("token_id"), no_token_id=m.get("no_token_id"),
            market_id=m.get("market_id"),
            city=city, resolution_date=resolution_date,
        ))

    if not rows:
        return [], center_f, density, []

    rejected = classify_rejections(
        rows, dv, cfg.mode, cfg.mass_core_frac, cfg.edge_threshold,
        cfg.price_min, cfg.price_max,
    )
    bets = build_bucket_bets(
        rows, dv, cfg.mode, cfg.mass_core_frac, cfg.edge_threshold,
        cfg.price_min, cfg.price_max, cfg.sizing_mode, cfg.budget_per_city_day,
    )
    bets = apply_liquidity_cap(bets, cfg.per_bucket_liq_cap_frac)
    return bets, center_f, density, rejected
