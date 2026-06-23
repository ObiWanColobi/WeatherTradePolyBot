"""Disqualify forecast candidates whose probabilities aren't calibrated, BEFORE P&L."""
from __future__ import annotations
import numpy as np
from scipy.stats import norm, kstest

from bakeoff.forecast.emos import _gaussian_crps


def pit_values(records, params):
    a, b, c, d = params
    out = []
    for r in records:
        xbar = float(np.mean(r["member_temps"]))
        svar = float(np.var(r["member_temps"]))
        mu = a + b * xbar
        sigma = float(np.sqrt(max(c + d * svar, 1e-6)))
        out.append(float(norm.cdf(r["truth_f"], mu, sigma)))
    return out


def calibration_gate(records, params, raw_crps):
    a, b, c, d = params
    xbar = np.array([np.mean(r["member_temps"]) for r in records])
    svar = np.array([np.var(r["member_temps"]) for r in records])
    y = np.array([r["truth_f"] for r in records])
    mu = a + b * xbar
    sigma = np.sqrt(np.maximum(c + d * svar, 1e-6))
    emos_crps = float(np.mean(_gaussian_crps(mu, sigma, y)))

    pit = pit_values(records, params)
    ks_p = float(kstest(pit, "uniform").pvalue)
    pit_uniform = ks_p > 0.05
    beats_raw = emos_crps < raw_crps
    return {
        "pit_uniform": pit_uniform, "ks_pvalue": ks_p,
        "emos_crps": emos_crps, "beats_raw": beats_raw,
        "passed": bool(pit_uniform and beats_raw),
    }
