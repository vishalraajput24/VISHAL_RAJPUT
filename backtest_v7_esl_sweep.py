"""
V7 15-min ESL Minimization Sweep
Goal: Find which filters reduce ESL% the most on 15-min candles.
Sweeps: RSI range, BW, slope, time-of-day, candle color, RSI delta.

Run: bash ~/VISHAL_RAJPUT/run_backtest.sh v7_esl_sweep
"""
import sqlite3, os, sys
import pandas as pd
import numpy as np

DB = os.path.expanduser("~/lab_data/vrl_data.db")
if not os.path.exists(DB):
    sys.exit(f"DB not found: {DB}")

con = sqlite3.connect(DB)
print("Resampling 3-min → 15-min...", flush=True)

df3 = pd.read_sql("""
    SELECT timestamp, strike, type, open, high, low, close,
           rsi, ema9_high, ema9_low
    FROM option_3min
    WHERE time(timestamp) >= '09:15:00' AND time(timestamp) < '15:30:00'
    ORDER BY strike, type, timestamp
""", con, parse_dates=['timestamp'])
con.close()

df3 = df3.set_index('timestamp')
groups = []
for (strike, typ), grp in df3.groupby(['strike', 'type']):
    r = grp.resample('15min', closed='left', label='left')
    g = pd.DataFrame({
        'open':      r['open'].first(),
        'high':      r['high'].max(),
        'low':       r['low'].min(),
        'close':     r['close'].last(),
        'rsi':       r['rsi'].last(),
        'ema9_high': r['ema9_high'].last(),
        'ema9_low':  r['ema9_low'].last(),
    })
    g['strike'] = strike
    g['type']   = typ
    groups.append(g.reset_index())

df = pd.concat(groups, ignore_index=True)
df = df[df['timestamp'].dt.time >= pd.Timestamp('09:30').time()]
df = df[df['timestamp'].dt.time <  pd.Timestamp('15:00').time()]
df = df.sort_values(['strike','type','timestamp']).copy()

g = df.groupby(['strike','type'])
df['fwd_3c']      = g['close'].transform(lambda x: x.shift(-3))
df['rsi_prev']    = g['rsi'].transform(lambda x: x.shift(1))
df['rsi_prev2']   = g['rsi'].transform(lambda x: x.shift(2))
df['ema9l_prev']  = g['ema9_low'].transform(lambda x: x.shift(1))
df['ema9l_prev2'] = g['ema9_low'].transform(lambda x: x.shift(2))
df['close_prev']  = g['close'].transform(lambda x: x.shift(1))

df['bw']          = df['ema9_high'] - df['ema9_low']
df['bw_prev']     = g['bw'].transform(lambda x: x.shift(1)) if False else (df['ema9_high'] - df['ema9_low'])  # placeholder
# recompute bw_prev properly
df['bw_prev']     = g['bw'].transform(lambda x: x.shift(1))

df = df.dropna(subset=['fwd_3c','rsi_prev','ema9l_prev'])

# Forward return
v = df[df['fwd_3c'].notna() & (df['close']>0)].iloc[0]
if abs(float(v['fwd_3c'])) > float(v['close'])*0.5:
    df['ret'] = df['fwd_3c'] - df['close']
else:
    df['ret'] = df['fwd_3c']

df = df.dropna(subset=['ret'])

# Feature flags
df['green']        = df['close'] > df['open']
df['above_ema9l']  = df['close'] > df['ema9_low']
df['rsi_rising']   = df['rsi'] > df['rsi_prev']
df['rsi_delta']    = df['rsi'] - df['rsi_prev']
df['slope1']       = df['ema9_low'] - df['ema9l_prev']
df['slope2']       = df['ema9l_prev'] - df['ema9l_prev2']
df['slope_ok2']    = (df['slope1'] >= 0) & (df['slope2'] >= 0)
df['hour']         = df['timestamp'].dt.hour
df['minute']       = df['timestamp'].dt.minute
# time bucket: morning (9:30-11:30), midday (11:30-13:30), afternoon (13:30-15:00)
df['session'] = pd.cut(
    df['timestamp'].dt.hour * 60 + df['timestamp'].dt.minute,
    bins=[9*60+30, 11*60+30, 13*60+30, 15*60],
    labels=['morning','midday','afternoon'],
    right=False
)

DAYS = df['timestamp'].dt.date.nunique()
BAR  = '━' * 76
MIN_N = 20

print(f"Candles: {len(df)} | Days: {DAYS}\n")

def stats(r):
    if len(r) < MIN_N:
        return None
    return {
        'n':    len(r),
        'avg':  r.mean(),
        'win':  (r>0).mean()*100,
        'esl':  (r<-12).mean()*100,
    }

def row(label, r):
    s = stats(r)
    if s is None:
        return f"  {label:40} {'<min':>6}"
    return (f"  {label:40} {s['n']:6d} {s['avg']:+7.1f}  "
            f"{s['win']:6.1f}%  {s['esl']:6.1f}%")

# Base: V7 current gates
base = df[df['above_ema9l'] & df['rsi_rising'] & (df['rsi']>=40)]

print(f"{BAR}")
print("  PART 1 — BASE + RSI RANGE (above_ema9l, rising)")
print(BAR)
print(f"\n  {'Filter':40} {'n':>6} {'avg':>8} {'win%':>7} {'ESL%':>7}")
print(f"  {'─'*68}")
print(row("Baseline (RSI>=40, rising)", base['ret']))
for lo, hi in [(40,99),(45,99),(50,99),(55,99),(40,60),(40,65),(45,60),(45,65),(50,60),(50,65),(55,65),(55,70)]:
    label = f"RSI {lo}-{hi if hi<99 else 'all'}"
    sub = df[df['above_ema9l'] & df['rsi_rising'] & (df['rsi']>=lo) & (df['rsi']<hi)]
    print(row(label, sub['ret']))

print(f"\n{BAR}")
print("  PART 2 — RSI DELTA (momentum strength)")
print(BAR)
print(f"\n  {'Filter':40} {'n':>6} {'avg':>8} {'win%':>7} {'ESL%':>7}")
print(f"  {'─'*68}")
b = df[df['above_ema9l'] & (df['rsi']>=40)]
print(row("RSI>=40, any direction", b['ret']))
for delta in [1, 2, 3, 5]:
    sub = b[b['rsi_delta'] >= delta]
    print(row(f"RSI>=40 + delta>={delta}", sub['ret']))
for delta in [2, 3, 5]:
    sub = df[df['above_ema9l'] & (df['rsi']>=50) & (df['rsi_delta']>=delta)]
    print(row(f"RSI>=50 + delta>={delta}", sub['ret']))

print(f"\n{BAR}")
print("  PART 3 — SLOPE CONDITION (EMA9_low rising)")
print(BAR)
print(f"\n  {'Filter':40} {'n':>6} {'avg':>8} {'win%':>7} {'ESL%':>7}")
print(f"  {'─'*68}")
b = df[df['above_ema9l'] & df['rsi_rising'] & (df['rsi']>=40)]
print(row("V7 base (no slope)", b['ret']))
b1 = b[b['slope1'] >= 0]
print(row("+ slope1 >= 0", b1['ret']))
b2 = b[b['slope_ok2']]
print(row("+ slope1&2 >= 0", b2['ret']))
b3 = b[b['slope1'] > 0]
print(row("+ slope1 > 0 (strictly rising)", b3['ret']))
b4 = b[b['slope1'] >= 1]
print(row("+ slope1 >= 1pt", b4['ret']))
b5 = b[b['slope1'] >= 2]
print(row("+ slope1 >= 2pt", b5['ret']))

print(f"\n{BAR}")
print("  PART 4 — BW FILTER (band width)")
print(BAR)
print(f"\n  {'Filter':40} {'n':>6} {'avg':>8} {'win%':>7} {'ESL%':>7}")
print(f"  {'─'*68}")
b = df[df['above_ema9l'] & df['rsi_rising'] & (df['rsi']>=40)]
for lo, hi in [(0,99),(5,15),(8,15),(10,20),(13,20),(13,17),(15,25),(15,30),(20,99)]:
    label = f"BW {lo}-{hi if hi<99 else 'all'}"
    sub = b[(b['bw']>=lo) & (b['bw']<hi)] if hi < 99 else b[b['bw']>=lo]
    print(row(label, sub['ret']))

print(f"\n{BAR}")
print("  PART 5 — TIME OF DAY")
print(BAR)
print(f"\n  {'Filter':40} {'n':>6} {'avg':>8} {'win%':>7} {'ESL%':>7}")
print(f"  {'─'*68}")
b = df[df['above_ema9l'] & df['rsi_rising'] & (df['rsi']>=40)]
for sess in ['morning','midday','afternoon']:
    sub = b[b['session']==sess]
    print(row(f"Session: {sess}", sub['ret']))
# Hour-by-hour
for h in range(9, 15):
    sub = b[b['hour']==h]
    if len(sub) >= MIN_N:
        print(row(f"Hour {h}:xx", sub['ret']))
# Avoid first candle of day (9:30) and last (14:45)
print(row("Exclude 9:30 candle", b[b['timestamp'].dt.time > pd.Timestamp('09:30').time()]['ret']))
print(row("Exclude 9:30 + 14:45", b[
    (b['timestamp'].dt.time > pd.Timestamp('09:30').time()) &
    (b['timestamp'].dt.time < pd.Timestamp('14:45').time())
]['ret']))

print(f"\n{BAR}")
print("  PART 6 — GREEN CANDLE + COMBOS")
print(BAR)
print(f"\n  {'Filter':40} {'n':>6} {'avg':>8} {'win%':>7} {'ESL%':>7}")
print(f"  {'─'*68}")
b = df[df['above_ema9l'] & df['rsi_rising'] & (df['rsi']>=40)]
print(row("V7 base", b['ret']))
print(row("+ green candle", b[b['green']]['ret']))
print(row("+ green + slope2", b[b['green'] & b['slope_ok2']]['ret']))
print(row("+ green + RSI>=50", b[b['green'] & (b['rsi']>=50)]['ret']))
print(row("+ green + RSI 50-65", b[b['green'] & (b['rsi']>=50) & (b['rsi']<65)]['ret']))
print(row("+ green + RSI 50-65 + slope2", b[b['green'] & (b['rsi']>=50) & (b['rsi']<65) & b['slope_ok2']]['ret']))
print(row("+ green + RSI 50-65 + BW 10-20", b[b['green'] & (b['rsi']>=50) & (b['rsi']<65) & (b['bw']>=10) & (b['bw']<20)]['ret']))
print(row("+ green + RSI 50-65 + BW 13-20", b[b['green'] & (b['rsi']>=50) & (b['rsi']<65) & (b['bw']>=13) & (b['bw']<20)]['ret']))
print(row("+ green + RSI 50-65 + BW 13-20 + slope2",
    b[b['green'] & (b['rsi']>=50) & (b['rsi']<65) & (b['bw']>=13) & (b['bw']<20) & b['slope_ok2']]['ret']))

print(f"\n{BAR}")
print("  PART 7 — BEST COMBOS RANKED BY ESL%")
print(BAR)
print(f"\n  {'Combo':40} {'n':>6} {'avg':>8} {'win%':>7} {'ESL%':>7}")
print(f"  {'─'*68}")

combos = {
    'V7 base (RSI>=40, rising)':
        df[df['above_ema9l'] & df['rsi_rising'] & (df['rsi']>=40)],
    'RSI 50-65':
        df[df['above_ema9l'] & df['rsi_rising'] & (df['rsi']>=50) & (df['rsi']<65)],
    'RSI 50-65 + green':
        df[df['above_ema9l'] & df['rsi_rising'] & (df['rsi']>=50) & (df['rsi']<65) & df['green']],
    'RSI 50-65 + green + slope2':
        df[df['above_ema9l'] & df['rsi_rising'] & (df['rsi']>=50) & (df['rsi']<65) & df['green'] & df['slope_ok2']],
    'RSI 50-65 + green + BW 13-20':
        df[df['above_ema9l'] & df['rsi_rising'] & (df['rsi']>=50) & (df['rsi']<65) & df['green'] & (df['bw']>=13) & (df['bw']<20)],
    'RSI 50-65 + green + BW 13-20 + slope2':
        df[df['above_ema9l'] & df['rsi_rising'] & (df['rsi']>=50) & (df['rsi']<65) & df['green'] & (df['bw']>=13) & (df['bw']<20) & df['slope_ok2']],
    'RSI 50-65 + slope2':
        df[df['above_ema9l'] & df['rsi_rising'] & (df['rsi']>=50) & (df['rsi']<65) & df['slope_ok2']],
    'RSI 50-65 + BW 13-20':
        df[df['above_ema9l'] & df['rsi_rising'] & (df['rsi']>=50) & (df['rsi']<65) & (df['bw']>=13) & (df['bw']<20)],
    'RSI 50-65 + delta>=2':
        df[df['above_ema9l'] & (df['rsi']>=50) & (df['rsi']<65) & (df['rsi_delta']>=2)],
    'Morning + RSI>=40':
        df[df['above_ema9l'] & df['rsi_rising'] & (df['rsi']>=40) & (df['session']=='morning')],
    'Morning + RSI 50-65':
        df[df['above_ema9l'] & df['rsi_rising'] & (df['rsi']>=50) & (df['rsi']<65) & (df['session']=='morning')],
}

rows = []
for name, sub in combos.items():
    s = stats(sub['ret'])
    if s:
        rows.append((name, s))

# Sort by ESL ascending
rows.sort(key=lambda x: x[1]['esl'])
for name, s in rows:
    print(f"  {name:40} {s['n']:6d} {s['avg']:+7.1f}  {s['win']:6.1f}%  {s['esl']:6.1f}%")

print(f"\n  Note: {DAYS} days of data. MIN_N={MIN_N} for display.")
