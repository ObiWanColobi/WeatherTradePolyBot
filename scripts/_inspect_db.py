import sqlite3
DB = r'f:\CodeProjects\TestCode1\weather_bot.db'
c = sqlite3.connect(DB)
c.row_factory = sqlite3.Row

print("=== Recent sizing_decisions (last 20 by recorded_at) — binding constraint + size flow ===")
rows = c.execute("""
SELECT s.id, s.trade_id, s.recorded_at, s.city, s.direction,
       s.balance, s.market_price, s.calibrated_prob,
       s.edge, s.ensemble_n, s.is_unanimous,
       s.kelly_fraction, s.kelly_final, s.size_pre_cap,
       s.cap_per_bet, s.cap_balance_pct, s.size_after_caps,
       s.slippage_reduced_to, s.final_size, s.binding_constraint,
       s.traj_regime_flag, s.direction_agreement_flag
FROM sizing_decisions s
ORDER BY s.recorded_at DESC
LIMIT 20
""").fetchall()
for r in rows:
    print(f"id={r['id']:>3} tid={r['trade_id'] if r['trade_id'] is not None else '--':>4} {r['recorded_at'][:19]} {r['city']:13s} {r['direction']:3s}  "
          f"bal=${r['balance']:>7.0f}  edge={r['edge']*100:>5.1f}%  unan={r['is_unanimous']}  "
          f"kelly_frac={r['kelly_fraction']:.3f}  kelly_final=${r['kelly_final']:>6.2f}  "
          f"pre=${r['size_pre_cap']:>6.2f}  cap_bet=${r['cap_per_bet']:>6.0f}  cap_bal=${r['cap_balance_pct']:>6.0f}  "
          f"after=${r['size_after_caps']:>6.2f}  final=${r['final_size']:>6.2f}  "
          f"bind={r['binding_constraint'] or '--':16s}  traj={r['traj_regime_flag']}  dir={r['direction_agreement_flag']}")

print("\n=== Binding-constraint histogram (last 50) ===")
for r in c.execute("""
SELECT binding_constraint, COUNT(*) FROM sizing_decisions
WHERE recorded_at >= '2026-05-01'
GROUP BY binding_constraint ORDER BY 2 DESC""").fetchall():
    print(f"  {r[0] or '(null)':30s} {r[1]}")

print("\n=== decision_snapshots — what's populated since Phase 2 went live (2026-05-08 19:27)? ===")
r = c.execute("""
SELECT
    COUNT(*) AS total,
    SUM(CASE WHEN ds.det_icon_temp_c   IS NOT NULL THEN 1 ELSE 0 END) AS det_icon_n,
    SUM(CASE WHEN ds.ens_std            IS NOT NULL THEN 1 ELSE 0 END) AS ens_n,
    SUM(CASE WHEN ds.prev_day_outcome   IS NOT NULL THEN 1 ELSE 0 END) AS prev_n,
    SUM(CASE WHEN ds.trades_per_min_60  IS NOT NULL THEN 1 ELSE 0 END) AS tpm_n,
    SUM(CASE WHEN ds.traj_init_d1       IS NOT NULL THEN 1 ELSE 0 END) AS traj_d1_n,
    SUM(CASE WHEN ds.traj_init_d5       IS NOT NULL THEN 1 ELSE 0 END) AS traj_d5_n,
    SUM(CASE WHEN s.traj_regime_flag    IS NOT NULL THEN 1 ELSE 0 END) AS traj_flag_n,
    SUM(CASE WHEN s.direction_agreement_flag IS NOT NULL THEN 1 ELSE 0 END) AS dir_flag_n
FROM sizing_decisions s
LEFT JOIN decision_snapshots ds ON ds.sizing_decision_id = s.id
WHERE s.recorded_at >= '2026-05-08T19:27:00'""").fetchone()
print(dict(r))
