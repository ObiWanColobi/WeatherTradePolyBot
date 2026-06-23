"""Render the ranked kill/promote verdict (spec §9)."""
from __future__ import annotations
from pathlib import Path


def scorecard_row(name, stage, opt_score, pess_score, calib):
    def verdict():
        if calib is not None and not calib.get("passed", True):
            return "KILL (calibration gate failed)"
        if pess_score["roi"] > 0 and pess_score["city_majority_positive"] and pess_score["bootstrap_ci_low"] > 0:
            return "WINNER (survives pessimistic fee)"
        if opt_score["roi"] > 0:
            return "promising, not proven (fee-fragile)"
        return "KILL (no edge)"
    return {
        "name": name, "stage": stage,
        "opt_roi": opt_score["roi"], "pess_roi": pess_score["roi"],
        "city_majority": pess_score["city_majority_positive"],
        "ci_low": pess_score["bootstrap_ci_low"],
        "n_trades": pess_score["n_trades"],
        "calib": "n/a" if calib is None else ("pass" if calib["passed"] else "FAIL"),
        "verdict": verdict(),
    }


def render_verdict(rows) -> str:
    head = ("| Candidate | Stage | ROI (low-fee / full-fee) | City majority? | CI low | "
            "Trades | Calib | Verdict |\n"
            "|---|---|---|---|---|---|---|---|\n")
    body = ""
    for r in rows:
        body += (f"| {r['name']} | {r['stage']} | {r['opt_roi']:+.1%} / {r['pess_roi']:+.1%} | "
                 f"{'yes' if r['city_majority'] else 'no'} | {r['ci_low']:+.1%} | "
                 f"{r['n_trades']} | {r['calib']} | {r['verdict']} |\n")
    return head + body


def write_verdict(rows, path):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(render_verdict(rows), encoding="utf-8")
