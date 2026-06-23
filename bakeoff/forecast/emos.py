"""EMOS / Non-homogeneous Gaussian Regression. μ=a+b·x̄, σ²=c+d·s². Pooled fit by CRPS."""
from __future__ import annotations
import numpy as np
from scipy.stats import norm
from scipy.optimize import minimize


def _gaussian_crps(mu, sigma, y):
    """Closed-form CRPS for a Gaussian forecast (Gneiting 2005)."""
    sigma = np.maximum(sigma, 1e-6)
    z = (y - mu) / sigma
    return sigma * (z * (2 * norm.cdf(z) - 1) + 2 * norm.pdf(z) - 1.0 / np.sqrt(np.pi))


def fit_emos(records: list[dict]):
    xbar = np.array([np.mean(r["member_temps"]) for r in records])
    svar = np.array([np.var(r["member_temps"]) for r in records])
    y = np.array([r["truth_f"] for r in records])

    def obj(theta):
        a, b, log_c, log_d = theta
        mu = a + b * xbar
        sigma2 = np.exp(log_c) + np.exp(log_d) * svar  # keep c,d > 0
        sigma = np.sqrt(sigma2)
        return np.mean(_gaussian_crps(mu, sigma, y))

    # Warm-start from a method-of-moments estimate so the optimizer begins NEAR the
    # inflating optimum rather than at an underdispersed point where Nelder-Mead stalls
    # on the locally-flat objective. b0=1 (ensemble mean ~unbiased); a0 = mean residual;
    # the residual variance (truth vs ensemble mean) seeds c, which is the dispersion the
    # raw ensemble lacks.
    resid = y - xbar
    a0 = float(np.mean(resid))
    resid_var = float(np.var(resid))
    log_c0 = np.log(max(resid_var, 1e-3))
    x0 = np.array([a0, 1.0, log_c0, np.log(1.0)])
    res = minimize(obj, x0, method="Nelder-Mead",
                   options={"maxiter": 50000, "xatol": 1e-7, "fatol": 1e-7})
    a, b, log_c, log_d = res.x
    return (float(a), float(b), float(np.exp(log_c)), float(np.exp(log_d)))


def bucket_probs(member_temps, params, ladder):
    a, b, c, d = params
    xbar = float(np.mean(member_temps))
    svar = float(np.var(member_temps))
    mu = a + b * xbar
    sigma = float(np.sqrt(max(c + d * svar, 1e-6)))
    probs = []
    for lo, hi in ladder:
        lo_cdf = 0.0 if lo is None else norm.cdf(lo, mu, sigma)
        hi_cdf = 1.0 if hi is None else norm.cdf(hi, mu, sigma)
        probs.append(max(0.0, hi_cdf - lo_cdf))
    total = sum(probs)
    return [p / total for p in probs] if total > 0 else probs
