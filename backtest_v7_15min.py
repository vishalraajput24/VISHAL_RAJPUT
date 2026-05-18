"""
V7 Strategy Backtest — 15-min candles (resampled from option_3min)
Gates: close > EMA9_low, RSI >= 40 AND rising
Goal: Find if V7 has edge, and what RSI threshold is optimal.

Run: bash ~/VISHAL_RAJPUT/run_backtest.sh v7_15min
"""
import sqlite3, os, sys
import pandas as pd
import numpy as np

DB = os.path.expanduser("~/lab_data/vrl_data.db")
if not os.path.exists(DB):
    sys.exit(f"DB not found: {DB}")

con = sqlite3.connect(DB)
print("Loading 3-min data to resample to 15-min...", flush=True)

# Check if option_15min table exists
tables = pd.read_sql("SELECT name FROM sqlite_master WHERE type='table'", con)['name'].tolist()

use_resample = True
if 'option_15min' in tables:
    cols_info = pd.read_sql("PRAGMA table_info(option_15min)", con)
    available = cols_info['name'].tolist()
    print(f"option_15min columns: {available}")
    if 'ema9_high' in available and 'ema9_low' in available:
        print("Using option_15min table directly.")
        df = pd.read_sql("""
            SELECT timestamp, strike, type, open, high, low, close,
                   rsi, ema9_high, ema9_low, fwd_3c
            FROM option_15min
            WHERE time(timestamp) >= '09:30:00' AND time(timestamp) < '15:00:00'
            ORDER BY strike, type, timestamp
        """, con, parse_dates=['timestamp'])
        use_resample = False
    else:
        print("option_15min lacks EMA columns — resampling from 3-min instead")

if use_resample:
    print("Resampling from option_3min...")
    df3 = pd.read_sql("""
        SELECT timestamp, strike, type, open, high, low, close, volume,
               rsi, ema9_high, ema9_low
        FROM option_3min
        WHERE time(timestamp) >= '09:15:00' AND time(timestamp) < '15:30:00'
        ORDER BY strike, type, timestamp
    """, con, parse_dates=['timestamp'])

    if df3.empty:
        sys.exit("No 3-min data found in DB.")

    # Resample to 15-min OHLC per strike/type
    df3 = df3.set_index('timestamp')
    groups = []
    for (strike, typ), grp in df3.groupby(['strike', 'type']):
        r = grp.resample('15min', closed='left', label='left')
        ohlc = r['close'].ohlc()
        ohlc.columns = ['open','high','low','close']
        ohlc['rsi']      = r['rsi'].last()
        ohlc['ema9_high'] = r['ema9_high'].last()
        ohlc['ema9_low']  = r['ema9_low'].last()
        ohlc['strike']    = strike
        ohlc['type']      = typ
        groups.append(ohlc.reset_index())
    df = pd.concat(groups, ignore_index=True)
    df = df.rename(columns={'timestamp': 'timestamp'})
    # Forward return: close of 3 candles later (45 min) — use shift
    df = df.sort_values(['strike','type','timestamp'])
    g = df.groupby(['strike','type'])
    df['fwd_3c'] = g['close'].transform(lambda x: x.shift(-3))
    df = df[df['timestamp'].dt.time >= pd.Timestamp('09:30').time()]
    df = df[df['timestamp'].dt.time < pd.Timestamp('15:00').time()]

con.close()

# ── Prepare ──────────────────────────────────────────────────────
df['fwd_3c'] = pd.to_numeric(df['fwd_3c'], errors='coerce')
df = df.sort_values(['strike','type','timestamp']).copy()
df['bw'] = df['ema9_high'] - df['ema9_low']

g = df.groupby(['strike','type'])
df['ema9l_slope'] = g['ema9_low'].transform(lambda x: x.diff())
df['rsi_prev']    = g['rsi'].transform(lambda x: x.shift(1))

# Forward return
v = df[df['fwd_3c'].notna() & (df['close']>0)].iloc[0]
if abs(float(v['fwd_3c'])) > float(v['close'])*0.5:
    df['ret'] = df['fwd_3c'] - df['close']
    print("fwd is absolute — converted to return")
else:
    df['ret'] = df['fwd_3c']

df.dropna(subset=['ret','ema9l_slope','rsi','rsi_prev'], inplace=True)
df['rsi_rising'] = df['rsi'] > df['rsi_prev']
df['green']      = df['close'] > df['open']
df['above_ema9l'] = df['close'] > df['ema9_low']

DAYS = df['timestamp'].dt.date.nunique()
BAR  = '━' * 72

print(f"\nCandles: {len(df)} | Days: {DAYS}")

# ── PART 1: Baseline filters ──────────────────────────────────────
print(f"\n{BAR}")
print("  PART 1 — BASELINE FILTER COMPARISON (15-min)")
print(BAR)

filters = {
    'All candles':              df,
    'close > ema9l':           df[df['above_ema9l']],
    'close > ema9l + rising RSI': df[df['above_ema9l'] & df['rsi_rising']],
    'V7 current (RSI>=40)':    df[df['above_ema9l'] & df['rsi_rising'] & (df['rsi']>=40)],
    'RSI>=45':                  df[df['above_ema9l'] & df['rsi_rising'] & (df['rsi']>=45)],
    'RSI>=50':                  df[df['above_ema9l'] & df['rsi_rising'] & (df['rsi']>=50)],
    'RSI 45-65':               df[df['above_ema9l'] & df['rsi_rising'] & (df['rsi']>45) & (df['rsi']<65)],
    'RSI 50-65':               df[df['above_ema9l'] & df['rsi_rising'] & (df['rsi']>50) & (df['rsi']<65)],
    'Green + G2 + RSI>=40':    df[df['green'] & df['above_ema9l'] & df['rsi_rising'] & (df['rsi']>=40)],
    'Green + G2 + RSI 50-65':  df[df['green'] & df['above_ema9l'] & df['rsi_rising'] & (df['rsi']>50) & (df['rsi']<65)],
}

print(f"\n  {'Filter':35} {'n':>6} {'avg':>8} {'win%':>7} {'ESL%':>7}")
print(f"  {'─'*68}")
for name, sub in filters.items():
    if len(sub) < 5:
        continue
    r = sub['ret']
    score = r.mean() - ((r<-12).mean()*100 - 40)*0.5
    print(f"  {name:35} {len(sub):6d} {r.mean():+7.1f}  "
          f"{(r>0).mean()*100:6.1f}%  {(r<-12).mean()*100:6.1f}%")

# ── PART 2: RSI Sweep ─────────────────────────────────────────────
print(f"\n{BAR}")
print("  PART 2 — RSI THRESHOLD SWEEP (close > ema9l, RSI rising)")
print(BAR)
base = df[df['above_ema9l'] & df['rsi_rising']]
print(f"\n  {'RSI range':15} {'n':>6} {'avg':>8} {'win%':>7} {'ESL%':>7}")
print(f"  {'─'*50}")
rsi_ranges = [(35,99),(40,99),(45,99),(50,99),(40,70),(40,65),(45,65),(50,65),(50,60)]
for lo, hi in rsi_ranges:
    sub = base[(base['rsi']>lo) & (base['rsi']<hi)]['ret']
    if len(sub) < 5:
        continue
    print(f"  RSI {lo:2d}-{hi:3d}       {len(sub):6d} {sub.mean():+7.1f}  "
          f"{(sub>0).mean()*100:6.1f}%  {(sub<-12).mean()*100:6.1f}%")

# ── PART 3: BW sweep (does band width matter for 15-min?) ────────
print(f"\n{BAR}")
print("  PART 3 — BAND WIDTH SWEEP (V7 current gates + BW filter)")
print(BAR)
v7_base = df[df['above_ema9l'] & df['rsi_rising'] & (df['rsi']>=40)]
print(f"\n  {'BW range':15} {'n':>6} {'avg':>8} {'win%':>7} {'ESL%':>7}")
print(f"  {'─'*50}")
for lo, hi in [(0,99),(5,20),(8,15),(10,20),(13,17),(15,25),(20,99)]:
    sub = v7_base[(v7_base['bw']>=lo) & (v7_base['bw']<hi)]['ret'] if hi < 99 else v7_base[v7_base['bw']>=lo]['ret']
    if len(sub) < 5:
        continue
    label = f"BW {lo:2d}-{hi if hi<99 else 'all':>3}"
    print(f"  {label:15} {len(sub):6d} {sub.mean():+7.1f}  "
          f"{(sub>0).mean()*100:6.1f}%  {(sub<-12).mean()*100:6.1f}%")

# ── PART 4: Best gate combo day-by-day ───────────────────────────
print(f"\n{BAR}")
print("  PART 4 — BEST COMBO DAY-BY-DAY")
print(BAR)

# Find best combo from Part 1 by score
best_name = 'Green + G2 + RSI 50-65'
best = df[df['green'] & df['above_ema9l'] & df['rsi_rising'] & (df['rsi']>50) & (df['rsi']<65)]
v7_curr = df[df['above_ema9l'] & df['rsi_rising'] & (df['rsi']>=40)]

print(f"\n  {'Date':12} {'V7curr n':>9} {'V7curr avg':>11} {'Best n':>8} {'Best avg':>10}")
print(f"  {'─'*55}")
all_dates = sorted(df['timestamp'].dt.date.unique())
for d in all_dates:
    c  = v7_curr[v7_curr['timestamp'].dt.date==d]['ret']
    b  = best[best['timestamp'].dt.date==d]['ret']
    c_avg = c.mean() if len(c)>0 else float('nan')
    b_avg = b.mean() if len(b)>0 else float('nan')
    print(f"  {str(d):12} {len(c):9d} {c_avg:+10.1f}  {len(b):8d} {b_avg:+9.1f}")

print(f"\n  V7 current (RSI>=40): n={len(v7_curr)} avg={v7_curr['ret'].mean():+.1f} "
      f"win={( v7_curr['ret']>0).mean()*100:.1f}% ESL={(v7_curr['ret']<-12).mean()*100:.1f}%")
print(f"  Best combo (RSI 50-65 green): n={len(best)} avg={best['ret'].mean():+.1f} "
      f"win={(best['ret']>0).mean()*100:.1f}% ESL={(best['ret']<-12).mean()*100:.1f}%")
