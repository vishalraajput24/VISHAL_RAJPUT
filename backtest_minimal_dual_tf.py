"""
Backtest: Minimal Dual-TF Strategy — parameter sweep to find best settings

Strategy:
  3-min: EMA9_low rising + RSI rising
  1-min: close > EMA9_high + RSI rising

Sweeps:
  - RSI rise minimum (1m and 3m)
  - RSI level floor (3m)
  - EMA9_low slope threshold
  - With/without BW filter

Run: python3 ~/VISHAL_RAJPUT/backtest_minimal_dual_tf.py
"""
import sqlite3, os, sys
import pandas as pd
import numpy as np

DB = os.path.expanduser("~/lab_data/vrl_data.db")
if not os.path.exists(DB):
    sys.exit(f"DB not found: {DB}")

con = sqlite3.connect(DB)

print("Loading data...", flush=True)
df1 = pd.read_sql("""
    SELECT timestamp, strike, type, open, high, low, close, rsi
    FROM option_1min
    WHERE time(timestamp) >= '09:15:00' AND time(timestamp) < '15:30:00'
    ORDER BY strike, type, timestamp
""", con, parse_dates=['timestamp'])

df3 = pd.read_sql("""
    SELECT timestamp, strike, type, open, high, low, close,
           rsi, ema9_high, ema9_low, fwd_3c
    FROM option_3min
    WHERE time(timestamp) >= '09:15:00' AND time(timestamp) < '15:30:00'
    ORDER BY strike, type, timestamp
""", con, parse_dates=['timestamp'])
con.close()

print(f"1-min: {len(df1)} rows | {df1['timestamp'].dt.date.nunique()} days")
print(f"3-min: {len(df3)} rows | {df3['timestamp'].dt.date.nunique()} days")

# ── 1-min indicators ──────────────────────────────────────────────
df1 = df1.sort_values(['strike','type','timestamp'])
df1['ema9_high_1m'] = df1.groupby(['strike','type'])['high'].transform(
    lambda x: x.ewm(span=9, adjust=False).mean())
df1['rsi_prev_1m']  = df1.groupby(['strike','type'])['rsi'].shift(1)
df1['rsi_rise_1m']  = df1['rsi'] - df1['rsi_prev_1m']

# ── 3-min indicators ──────────────────────────────────────────────
df3 = df3.sort_values(['strike','type','timestamp'])
df3['fwd_3c']      = pd.to_numeric(df3['fwd_3c'], errors='coerce')
df3['ema9l_slope'] = df3.groupby(['strike','type'])['ema9_low'].diff()
df3['rsi_prev_3m'] = df3.groupby(['strike','type'])['rsi'].shift(1)
df3['rsi_rise_3m'] = df3['rsi'] - df3['rsi_prev_3m']
df3['bw']          = df3['ema9_high'] - df3['ema9_low']

# fwd_3c absolute price → return
valid = df3[df3['fwd_3c'].notna() & (df3['close'] > 0)].iloc[0]
if abs(float(valid['fwd_3c'])) > float(valid['close']) * 0.5:
    df3['ret_3c'] = df3['fwd_3c'] - df3['close']
else:
    df3['ret_3c'] = df3['fwd_3c']

# ── Time filter ───────────────────────────────────────────────────
t3 = df3['timestamp']
df3 = df3[
    (t3.dt.time >= pd.Timestamp('09:45').time()) &
    (t3.dt.time <  pd.Timestamp('15:00').time())
].copy().dropna(subset=['ret_3c','ema9l_slope','rsi','rsi_prev_3m'])

t1 = df1['timestamp']
df1 = df1[
    (t1.dt.time >= pd.Timestamp('09:45').time()) &
    (t1.dt.time <  pd.Timestamp('15:00').time())
].copy().dropna(subset=['rsi_prev_1m'])

# ── Tag 1-min rows with parent 3-min bucket ───────────────────────
df1['bucket'] = df1['timestamp'].dt.floor('3min')

# ── BASELINE: current 3-min strategy (G1+G2+G2B+RSI 45-75) ──────
baseline_mask = (
    (df3['close'] > df3['open'])       &
    (df3['close'] > df3['ema9_low'])   &
    (df3['ema9l_slope'] >= 0)          &
    (df3['rsi'] > 45) & (df3['rsi'] < 75)
)
baseline = df3[baseline_mask]
print(f"\nBaseline (current strategy): n={len(baseline)}, "
      f"avg={baseline['ret_3c'].mean():+.1f}, "
      f"win={( baseline['ret_3c']>0).mean()*100:.1f}%, "
      f"ESL={(baseline['ret_3c']<-12).mean()*100:.1f}%")

# ── DUAL-TF SWEEP ─────────────────────────────────────────────────
# 3-min alignment options
slope_thresholds  = [0, 0.05]
rsi3_rise_mins    = [0, 1, 2]
rsi3_floors       = [0, 40, 45]
# 1-min trigger options
rsi1_rise_mins    = [0, 1, 2]
bw_filters        = [0, 11]   # 0 = no BW filter

results = []

for slope_th in slope_thresholds:
    for rsi3_rise in rsi3_rise_mins:
        for rsi3_floor in rsi3_floors:
            for rsi1_rise in rsi1_rise_mins:
                for bw_min in bw_filters:

                    # 3-min alignment
                    a3 = (
                        (df3['ema9l_slope'] >= slope_th)    &
                        (df3['rsi_rise_3m'] >= rsi3_rise)   &
                        (df3['rsi'] >= rsi3_floor)
                    )
                    if bw_min > 0:
                        a3 = a3 & (df3['bw'] >= bw_min)

                    aligned_3m = df3[a3][['strike','type','timestamp','close','ret_3c']].copy()
                    aligned_3m = aligned_3m.rename(columns={'timestamp':'bucket','close':'3m_close'})

                    # Join 1-min to aligned 3-min
                    df1m = df1.merge(aligned_3m, on=['strike','type','bucket'], how='inner')

                    # 1-min trigger: close > ema9_high AND RSI rising >= threshold
                    trig = df1m[
                        (df1m['close'] > df1m['ema9_high_1m']) &
                        (df1m['rsi_rise_1m'] >= rsi1_rise)
                    ]

                    if len(trig) < 30:
                        continue

                    # First trigger per bucket
                    first = trig.sort_values(['strike','type','bucket','timestamp'])
                    first = first.groupby(['strike','type','bucket']).first().reset_index()

                    fwd_close = first['3m_close'] + first['ret_3c']
                    ret       = fwd_close - first['close']

                    n    = len(first)
                    avg  = ret.mean()
                    win  = (ret > 0).mean() * 100
                    esl  = (ret < -12).mean() * 100
                    big  = (ret > 20).mean() * 100
                    med  = ret.median()

                    results.append({
                        'slope_th': slope_th, 'rsi3_rise': rsi3_rise,
                        'rsi3_floor': rsi3_floor, 'rsi1_rise': rsi1_rise,
                        'bw_min': bw_min,
                        'n': n, 'avg': avg, 'win': win,
                        'esl': esl, 'big': big, 'med': med
                    })

res = pd.DataFrame(results)

# ── Score: rank by avg return with ESL penalty ────────────────────
res['score'] = res['avg'] - (res['esl'] - 40) * 0.5

print(f"\n{'━'*80}")
print(f"  TOP 15 COMBINATIONS (sorted by score)")
print(f"{'━'*80}")
print(f"  {'slope':>6} {'rsi3r':>6} {'rsi3f':>6} {'rsi1r':>6} {'bw':>4} "
      f"{'n':>5} {'avg':>7} {'win%':>7} {'ESL%':>7} {'big%':>7} {'score':>7}")
print(f"  {'─'*78}")

top = res.sort_values('score', ascending=False).head(15)
for _, r in top.iterrows():
    print(f"  {r['slope_th']:6.2f} {r['rsi3_rise']:6.0f} {r['rsi3_floor']:6.0f} "
          f"{r['rsi1_rise']:6.0f} {r['bw_min']:4.0f} "
          f"{r['n']:5.0f} {r['avg']:+7.1f} {r['win']:7.1f} "
          f"{r['esl']:7.1f} {r['big']:7.1f} {r['score']:+7.1f}")

# ── Best setting detail ───────────────────────────────────────────
best = res.sort_values('score', ascending=False).iloc[0]
print(f"\n{'━'*60}")
print(f"  BEST SETTING")
print(f"{'━'*60}")
print(f"  3-min slope >= {best['slope_th']}")
print(f"  3-min RSI rise >= {best['rsi3_rise']} pts")
print(f"  3-min RSI floor >= {best['rsi3_floor']}")
print(f"  1-min RSI rise >= {best['rsi1_rise']} pts")
print(f"  BW filter >= {best['bw_min']}")
print(f"  Signals: {best['n']:.0f}")
print(f"  Avg return: {best['avg']:+.1f} pts")
print(f"  Win rate: {best['win']:.1f}%")
print(f"  ESL rate: {best['esl']:.1f}%")
print(f"  Big win:  {best['big']:.1f}%")

# ── Compare best vs baseline day-by-day ──────────────────────────
print(f"\n{'━'*60}")
print(f"  BEST vs BASELINE — day-by-day")
print(f"{'━'*60}")
# Reconstruct best
a3_best = (
    (df3['ema9l_slope'] >= best['slope_th']) &
    (df3['rsi_rise_3m'] >= best['rsi3_rise']) &
    (df3['rsi'] >= best['rsi3_floor'])
)
if best['bw_min'] > 0:
    a3_best = a3_best & (df3['bw'] >= best['bw_min'])

aligned_best = df3[a3_best][['strike','type','timestamp','close','ret_3c']].copy()
aligned_best = aligned_best.rename(columns={'timestamp':'bucket','close':'3m_close'})
df1m_best = df1.merge(aligned_best, on=['strike','type','bucket'], how='inner')
trig_best  = df1m_best[
    (df1m_best['close'] > df1m_best['ema9_high_1m']) &
    (df1m_best['rsi_rise_1m'] >= best['rsi1_rise'])
]
first_best = trig_best.sort_values(['strike','type','bucket','timestamp'])
first_best = first_best.groupby(['strike','type','bucket']).first().reset_index()
first_best['ret'] = (first_best['3m_close'] + first_best['ret_3c']) - first_best['close']
first_best['date'] = first_best['bucket'].dt.date

print(f"  {'Date':12} {'Base n':>7} {'Base avg':>9} {'Best n':>7} {'Best avg':>9}")
for date in sorted(baseline['timestamp'].dt.date.unique()):
    b  = baseline[baseline['timestamp'].dt.date == date]
    bt = first_best[first_best['date'] == date]
    bt_avg = bt['ret'].mean() if len(bt) > 0 else float('nan')
    print(f"  {str(date):12} {len(b):7d} {b['ret_3c'].mean():+9.1f} "
          f"{len(bt):7d} {bt_avg:+9.1f}")
