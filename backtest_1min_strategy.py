"""
Backtest: Current V8 strategy applied on 1-min timeframe
Sweep BW thresholds to find optimal band width for 1-min candles

Gates (same as V8 but on 1-min):
  G1: Green candle (close > open)
  G2: Close > EMA9_low
  G2B: EMA9_low slope >= 0
  G3: BW >= X  ← SWEEP THIS
  G5: RSI 45-75 AND rising

Run: python3 ~/VISHAL_RAJPUT/backtest_1min_strategy.py
"""
import sqlite3, os, sys
import pandas as pd
import numpy as np

DB = os.path.expanduser("~/lab_data/vrl_data.db")
if not os.path.exists(DB):
    sys.exit(f"DB not found: {DB}")

con = sqlite3.connect(DB)
print("Loading 1-min option data...", flush=True)
df = pd.read_sql("""
    SELECT timestamp, strike, type, open, high, low, close, rsi,
           fwd_1c, fwd_3c, fwd_5c
    FROM option_1min
    WHERE time(timestamp) >= '09:15:00' AND time(timestamp) < '15:30:00'
    ORDER BY strike, type, timestamp
""", con, parse_dates=['timestamp'])
con.close()

print(f"Rows: {len(df)} | Days: {df['timestamp'].dt.date.nunique()}")

# ── Compute EMA9 bands from 1-min OHLC ────────────────────────────
df = df.sort_values(['strike','type','timestamp'])
grp = df.groupby(['strike','type'])
df['ema9_high'] = grp['high'].transform(lambda x: x.ewm(span=9, adjust=False).mean())
df['ema9_low']  = grp['low'].transform(lambda x:  x.ewm(span=9, adjust=False).mean())
df['bw']        = df['ema9_high'] - df['ema9_low']
df['ema9l_slope'] = grp['ema9_low'].transform(lambda x: x.diff())
df['rsi_prev']    = grp['rsi'].transform(lambda x: x.shift(1))

# ── Forward returns (cast string → numeric) ────────────────────────
for col in ['fwd_1c','fwd_3c','fwd_5c']:
    df[col] = pd.to_numeric(df[col], errors='coerce')

# Detect if absolute price or return
valid = df[df['fwd_3c'].notna() & (df['close'] > 0)].iloc[0]
is_abs = abs(float(valid['fwd_3c'])) > float(valid['close']) * 0.5
if is_abs:
    df['ret_1c'] = df['fwd_1c'] - df['close']
    df['ret_3c'] = df['fwd_3c'] - df['close']
    df['ret_5c'] = df['fwd_5c'] - df['close']
    print("fwd is absolute price — converted to return")
else:
    df['ret_1c'] = df['fwd_1c']
    df['ret_3c'] = df['fwd_3c']
    df['ret_5c'] = df['fwd_5c']

# ── Time filter: 09:45 – 15:00 ────────────────────────────────────
t = df['timestamp']
df = df[
    (t.dt.time >= pd.Timestamp('09:45').time()) &
    (t.dt.time <  pd.Timestamp('15:00').time())
].copy().dropna(subset=['ret_3c','ema9l_slope','rsi','rsi_prev'])

df['rsi_rising'] = df['rsi'] > df['rsi_prev']

# ── Base gates (no BW filter) ──────────────────────────────────────
base = (
    (df['close'] > df['open'])       &   # G1: green
    (df['close'] > df['ema9_low'])   &   # G2: above support
    (df['ema9l_slope'] >= 0)         &   # G2B: slope rising
    (df['rsi'] > 45) & (df['rsi'] < 75) &  # G5: RSI range
    (df['rsi_rising'])                    # G5: RSI rising
)

# ── BW sweep ───────────────────────────────────────────────────────
bw_values = [0, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 18, 20]

print(f"\n{'━'*72}")
print(f"  BW SWEEP — 1-min strategy (ret_3c = 3-min forward return)")
print(f"{'━'*72}")
print(f"  {'BW>=':>5} {'n':>5} {'avg':>7} {'median':>8} {'win%':>7} "
      f"{'ESL%':>7} {'big%':>7} {'score':>8}")
print(f"  {'─'*70}")

results = []
for bw_min in bw_values:
    mask = base & (df['bw'] >= bw_min)
    d = df[mask]
    if len(d) < 20:
        continue
    n   = len(d)
    avg = d['ret_3c'].mean()
    med = d['ret_3c'].median()
    win = (d['ret_3c'] > 0).mean() * 100
    esl = (d['ret_3c'] < -12).mean() * 100
    big = (d['ret_3c'] > 20).mean() * 100
    score = avg - (esl - 40) * 0.5
    print(f"  {bw_min:5d} {n:5d} {avg:+7.1f} {med:+8.1f} {win:7.1f} "
          f"{esl:7.1f} {big:7.1f} {score:+8.1f}")
    results.append({'bw': bw_min, 'n': n, 'avg': avg, 'med': med,
                    'win': win, 'esl': esl, 'big': big, 'score': score})

res = pd.DataFrame(results)
best_bw = int(res.sort_values('score', ascending=False).iloc[0]['bw'])

# ── Compare 1-min best BW vs 3-min strategy (baseline) ───────────
print(f"\n{'━'*60}")
print(f"  BEST BW for 1-min: BW >= {best_bw}")
print(f"{'━'*60}")

# Compare forward returns: 1-min (3-min fwd) vs 3-min baseline
# Load 3-min for comparison
con2 = sqlite3.connect(DB)
df3 = pd.read_sql("""
    SELECT timestamp, strike, type, open, high, low, close,
           rsi, ema9_high, ema9_low, fwd_3c
    FROM option_3min
    WHERE time(timestamp) >= '09:45:00' AND time(timestamp) < '15:00:00'
    ORDER BY strike, type, timestamp
""", con2, parse_dates=['timestamp'])
con2.close()

df3['fwd_3c'] = pd.to_numeric(df3['fwd_3c'], errors='coerce')
df3 = df3.sort_values(['strike','type','timestamp'])
df3['ema9l_slope'] = df3.groupby(['strike','type'])['ema9_low'].diff()
df3['rsi_prev']    = df3.groupby(['strike','type'])['rsi'].shift(1)
df3 = df3.dropna(subset=['fwd_3c','ema9l_slope','rsi','rsi_prev'])

valid3 = df3[df3['fwd_3c'].notna() & (df3['close'] > 0)].iloc[0]
if abs(float(valid3['fwd_3c'])) > float(valid3['close']) * 0.5:
    df3['ret_3c'] = df3['fwd_3c'] - df3['close']
else:
    df3['ret_3c'] = df3['fwd_3c']

base3 = (
    (df3['close'] > df3['open']) &
    (df3['close'] > df3['ema9_low']) &
    (df3['ema9l_slope'] >= 0) &
    (df3['rsi'] > 45) & (df3['rsi'] < 75) &
    (df3['rsi'] > df3['rsi_prev']) &
    ((df3['ema9_high'] - df3['ema9_low']) >= 11)
)
d3 = df3[base3]

d1_best = df[base & (df['bw'] >= best_bw)]

print(f"\n  {'':30s} {'3-min V8':>12} {'1-min best':>12}")
print(f"  {'─'*54}")
print(f"  {'Signals':30s} {len(d3):12d} {len(d1_best):12d}")
print(f"  {'Avg return (3-min fwd)':30s} {d3['ret_3c'].mean():+12.1f} {d1_best['ret_3c'].mean():+12.1f}")
print(f"  {'Median return':30s} {d3['ret_3c'].median():+12.1f} {d1_best['ret_3c'].median():+12.1f}")
print(f"  {'Win rate (>0)':30s} {(d3['ret_3c']>0).mean()*100:11.1f}% {(d1_best['ret_3c']>0).mean()*100:11.1f}%")
print(f"  {'ESL rate (<-12)':30s} {(d3['ret_3c']<-12).mean()*100:11.1f}% {(d1_best['ret_3c']<-12).mean()*100:11.1f}%")
print(f"  {'Big win (>20)':30s} {(d3['ret_3c']>20).mean()*100:11.1f}% {(d1_best['ret_3c']>20).mean()*100:11.1f}%")

# ── Day-by-day ────────────────────────────────────────────────────
print(f"\n  Day-by-day (3-min fwd return):")
print(f"  {'Date':12} {'3m n':>6} {'3m avg':>8} {'1m n':>6} {'1m avg':>8}")
for date in sorted(d3['timestamp'].dt.date.unique()):
    c3 = d3[d3['timestamp'].dt.date == date]
    c1 = d1_best[d1_best['timestamp'].dt.date == date]
    c1_avg = c1['ret_3c'].mean() if len(c1) > 0 else float('nan')
    print(f"  {str(date):12} {len(c3):6d} {c3['ret_3c'].mean():+8.1f} "
          f"{len(c1):6d} {c1_avg:+8.1f}")
