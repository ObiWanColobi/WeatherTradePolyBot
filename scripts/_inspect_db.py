"""Backcheck the prev_day_outcome=NO anomaly for HK >=30C on 2026-05-11."""
import sqlite3
DB = r'f:\CodeProjects\TestCode1\weather_bot.db'
c = sqlite3.connect(DB)
c.row_factory = sqlite3.Row

print("======================================================================")
print("  Step 1: weather_city_log rows for HK on 2026-05-11")
print("======================================================================")
for r in c.execute("""
SELECT id, logged_date, city, market_type, threshold, yes_price,
       model_prob, ensemble_pct, ensemble_n, resolved_yes, condition_id,
       logged_at
FROM weather_city_log
WHERE city = 'hong kong' AND logged_date LIKE '2026-05-11%'
ORDER BY threshold
""").fetchall():
    print(f"  thr={r['threshold']:>8s}  yes_price={r['yes_price']:.2f}  "
          f"ens={r['ensemble_pct']:.2f} ({r['ensemble_n']})  "
          f"resolved_yes={r['resolved_yes']}  "
          f"cond={r['condition_id'][:20] if r['condition_id'] else 'NULL'}")
    print(f"      logged_at={r['logged_at']}")

print("\n======================================================================")
print("  Step 2: HK trades that closed on 2026-05-11 or 2026-05-12 (ground truth)")
print("======================================================================")
for r in c.execute("""
SELECT id, threshold, direction, fill_price, exit_price, pnl,
       opened_at, closed_at, actual_resolution, resolution_price, actual_temperature
FROM trades
WHERE city = 'hong kong'
  AND (closed_at LIKE '2026-05-11%' OR closed_at LIKE '2026-05-12%')
ORDER BY closed_at
""").fetchall():
    print(f"  id={r['id']} thr={r['threshold']:>8s} dir={r['direction']}  "
          f"fill={r['fill_price']:.2f}  exit={r['exit_price']:.2f}  pnl=${r['pnl']:+.2f}  "
          f"actual_res={r['actual_resolution']}  actual_temp={r['actual_temperature']}")
    print(f"      closed={r['closed_at']}")

print("\n======================================================================")
print("  Step 3: METAR observations for HK on 2026-05-10 / 2026-05-11 (UTC)")
print("======================================================================")
print("\n-- All HK METAR rows observed in last 4 days --")
for r in c.execute("""
SELECT observed_at_utc, observed_temp_c, ensemble_p50_c, source
FROM metar_observations
WHERE city = 'hong kong'
  AND observed_at_utc >= '2026-05-10'
ORDER BY observed_at_utc DESC
LIMIT 30
""").fetchall():
    print(f"  {r['observed_at_utc']}  temp={r['observed_temp_c']:.1f}  ens_p50={r['ensemble_p50_c']:.1f}  src={r['source']}")

print("\n-- Peak HK temp per day from METAR --")
for r in c.execute("""
SELECT substr(observed_at_utc, 1, 10) AS day,
       MAX(observed_temp_c) AS peak_c,
       COUNT(*) AS n
FROM metar_observations
WHERE city = 'hong kong'
  AND observed_at_utc >= '2026-05-08'
GROUP BY day ORDER BY day
""").fetchall():
    print(f"  {r['day']}  peak={r['peak_c']:.1f}C  n={r['n']}")

print("\n======================================================================")
print("  Step 4: The 70 backfilled resolved_yes rows — distribution")
print("======================================================================")
for r in c.execute("""
SELECT city, threshold, resolved_yes, COUNT(*) AS n
FROM weather_city_log
WHERE resolved_yes IS NOT NULL
GROUP BY city, threshold, resolved_yes
ORDER BY city, threshold
""").fetchall():
    print(f"  {r['city']:14s} {r['threshold']:>8s}  resolved_yes={r['resolved_yes']}  n={r['n']}")
