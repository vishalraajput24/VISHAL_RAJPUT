"""
Backtest: Dual-TF entry — 3-min alignment + 1-min EMA9_high breakout
Uses real 1-min + 3-min option data from the production DB.

Run: python3 ~/VISHAL_RAJPUT/backtest_dual_tf.py
"""
import sqlite3, os, sys
import pandas as pd
import numpy as np

DB = os.path.expanduser("~/lab_data/vrl_data.db")
if not os.path.exists(DB):
    sys.exit(f"DB not found: {DB}")

con = sqlite3.connect(DB)

# ── Load 1-min option data ──────────────────────────────────────────
print("Loading 1-min option data...", flush=True)
df1 = pd.read_sql("""
    SELECT timestamp, strike, type, open, high, low, close, volume, rsi
    FROM option_1min
    WHERE time(timestamp) >= '09:15:00' AND time(timestamp) < '15:30:00'
    ORDER BY timestamp
""", con, parse_dates=['timestamp'])

# ── Load 3-min option data ──────────────────────────────────────────
print("Loading 3-min option data...", flush=True)
df3 = pd.read_sql("""
    SELECT timestamp, strike, type, open, high, low, close,
           rsi, ema9_high, ema9_low, fwd_3c, fwd_outcome
    FROM option_3min
    WHERE time(timestamp) >= '09:15:00' AND time(timestamp) < '15:30:00'
    ORDER BY timestamp
""", con, parse_dates=['timestamp'])
con.close()

print(f"1-min: {len(df1)} rows | {df1['timestamp'].dt.date.nunique()} days")
print(f"3-min: {len(df3)} rows | {df3['timestamp'].dt.date.nunique()} days")

# ── Compute 1-min EMA9_high / EMA9_low ────────────────────────────
print("Computing 1-min EMA9 bands...", flush=True)
df1 = df1.sort_values(['strike','type','timestamp'])
df1['ema9_high_1m'] = df1.groupby(['strike','type'])['high'].transform(
    lambda x: x.ewm(span=9, adjust=False).mean())
df1['ema9_low_1m']  = df1.groupby(['strike','type'])['low'].transform(
    lambda x: x.ewm(span=9, adjust=False).mean())

# ── 3-min: add slope + forward return ─────────────────────────────
df3 = df3.sort_values(['strike','type','timestamp'])
df3['ema9l_slope'] = df3.groupby(['strike','type'])['ema9_low'].diff()
df3['ret_3c']      = df3['fwd_3c']   # fwd_3c already stores the forward close price delta

# Check if fwd_3c is absolute price or return
# Use first non-null pair to decide
valid = df3[df3['fwd_3c'].notna() & (df3['close'] > 0)].iloc[0]
sample_close, sample_fwd = valid['close'], valid['fwd_3c']
if abs(sample_fwd) > sample_close * 0.5:
    df3['ret_3c'] = df3['fwd_3c'] - df3['close']
    print("fwd_3c is absolute price — computing return as fwd_3c - close")
else:
    print("fwd_3c appears to be a return already")

# ── Time filter ────────────────────────────────────────────────────
t3 = df3['timestamp']
df3 = df3[
    (t3.dt.time >= pd.Timestamp('09:45').time()) &
    (t3.dt.time <  pd.Timestamp('15:00').time())
].copy().dropna(subset=['ret_3c', 'ema9l_slope', 'rsi', 'ema9_high', 'ema9_low'])

# ── 3-min alignment gates ──────────────────────────────────────────
# G1: green candle, G2: close > ema9_low, G2B: ema9_low rising, G5: RSI 45-75
align_mask = (
    (df3['close'] > df3['open'])       &
    (df3['close'] > df3['ema9_low'])   &
    (df3['ema9l_slope'] >= 0)          &
    (df3['rsi'] > 45) & (df3['rsi'] < 75)
)
cur = df3[align_mask].copy()

# ── DUAL-TF: find 1-min EMA9_high breakout within aligned 3-min candles ──
# Tag each 1-min row with its parent 3-min bucket
df1['bucket'] = df1['timestamp'].dt.floor('3min')

# Get aligned 3-min candles info
aligned_3m = df3[align_mask][['strike','type','timestamp','ema9_high','ema9_low','close','ret_3c']].copy()
aligned_3m = aligned_3m.rename(columns={
    'timestamp': 'bucket',
    'close':     '3m_close',
    'ret_3c':    '3m_ret',
})

# Join 1-min rows with their parent 3-min bucket (only for aligned candles)
df1m = df1.merge(aligned_3m, on=['strike','type','bucket'], how='inner')

# 1-min trigger: close > 1-min EMA9_high (price breaks above upper band on 1-min)
trigger = df1m[df1m['close'] > df1m['ema9_high_1m']].copy()

# First triggered 1-min candle per 3-min bucket
trigger = trigger.sort_values(['strike','type','bucket','timestamp'])
first_trig = trigger.groupby(['strike','type','bucket']).first().reset_index()

# Forward return from 1-min entry price
first_trig['fwd_close'] = first_trig['3m_close'] + first_trig['3m_ret']
first_trig['ret_1m']    = first_trig['fwd_close'] - first_trig['close']  # close = 1-min entry
first_trig['saving']    = first_trig['3m_close']  - first_trig['close']  # pts saved vs 3-min close

# ── Print results ──────────────────────────────────────────────────
def show(d, label, ret_col):
    n   = len(d)
    avg = d[ret_col].mean()
    med = d[ret_col].median()
    win = (d[ret_col] > 0).mean() * 100
    esl = (d[ret_col] < -12).mean() * 100
    big = (d[ret_col] > 20).mean() * 100
    print(f"\n{'━'*60}")
    print(f"  {label}  (n={n})")
    print(f"{'━'*60}")
    print(f"  Avg return      : {avg:+.1f} pts")
    print(f"  Median return   : {med:+.1f} pts")
    print(f"  Win  rate (>0)  : {win:.1f}%")
    print(f"  ESL  rate (<-12): {esl:.1f}%")
    print(f"  Big win  (>20)  : {big:.1f}%")

show(cur,        "CURRENT  — 3-min close entry (G1+G2+G2B+RSI)", 'ret_3c')
show(first_trig, "DUAL-TF  — 1-min EMA9_high breakout trigger",  'ret_1m')

sig_pct = len(first_trig) / max(len(cur), 1) * 100
print(f"\n  1-min trigger fires on: {sig_pct:.1f}% of 3-min aligned candles")
print(f"  Avg pts saved at entry : {first_trig['saving'].mean():+.1f} pts")

print(f"\n  Saving distribution (pts saved vs 3-min close):")
for s in [3, 5, 10, 15, 20]:
    pos = first_trig[first_trig['saving'] >= s]
    if len(pos) > 0:
        print(f"    saving >= {s:2d} pts: {len(pos)/len(first_trig)*100:4.1f}% of triggers | avg {pos['saving'].mean():.1f} pts")

print(f"\n  Day-by-day comparison:")
print(f"  {'Date':12} {'Cur n':>6} {'Cur avg':>9} {'Dual n':>7} {'Dual avg':>9} {'Saving':>8}")
for date in sorted(cur['timestamp'].dt.date.unique()):
    c  = cur[cur['timestamp'].dt.date == date]
    d2 = first_trig[first_trig['bucket'].dt.date == date]
    if len(c) > 0 or len(d2) > 0:
        d2_avg = d2['ret_1m'].mean() if len(d2) > 0 else float('nan')
        sav    = d2['saving'].mean()  if len(d2) > 0 else float('nan')
        print(f"  {str(date):12} {len(c):6d} {c['ret_3c'].mean():+9.1f} "
              f"{len(d2):7d} {d2_avg:+9.1f} {sav:+8.1f}")
