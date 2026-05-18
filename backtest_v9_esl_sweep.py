"""
V9 3-min ESL Minimization Sweep
Goal: Find which filters reduce ESL% on 3-min candles while keeping edge.
Baseline: V9 current gates (G1-G5: green, close>ema9l, slope2, BW 13-17, RSI 50-65 rising)

Sweeps:
  - RSI range tightening
  - RSI delta (momentum strength)
  - BW tightening
  - Slope strictness
  - Candle body size
  - Time of day
  - Previous candle confirmation
  - All combos ranked by ESL

Run: bash ~/VISHAL_RAJPUT/run_backtest.sh v9_esl_sweep
"""
import sqlite3, os, sys
import pandas as pd
import numpy as np

DB = os.path.expanduser("~/lab_data/vrl_data.db")
if not os.path.exists(DB):
    sys.exit(f"DB not found: {DB}")

con = sqlite3.connect(DB)
print("Loading 3-min data...", flush=True)

df = pd.read_sql("""
    SELECT timestamp, strike, type, open, high, low, close,
           rsi, ema9_high, ema9_low
    FROM option_3min
    WHERE time(timestamp) >= '09:18:00' AND time(timestamp) < '15:30:00'
    ORDER BY strike, type, timestamp
""", con, parse_dates=['timestamp'])
con.close()

print(f"Rows loaded: {len(df)}", flush=True)

df = df.sort_values(['strike','type','timestamp']).copy()
g  = df.groupby(['strike','type'])

# Forward returns
df['fwd_3c']  = g['close'].transform(lambda x: x.shift(-3))   # 9 min
df['fwd_6c']  = g['close'].transform(lambda x: x.shift(-6))   # 18 min
df['fwd_10c'] = g['close'].transform(lambda x: x.shift(-10))  # 30 min

# Indicators
df['bw']           = df['ema9_high'] - df['ema9_low']
df['band_mid']     = (df['ema9_high'] + df['ema9_low']) / 2
df['rsi_prev']     = g['rsi'].transform(lambda x: x.shift(1))
df['rsi_prev2']    = g['rsi'].transform(lambda x: x.shift(2))
df['ema9l_prev']   = g['ema9_low'].transform(lambda x: x.shift(1))
df['ema9l_prev2']  = g['ema9_low'].transform(lambda x: x.shift(2))
df['bw_prev']      = g['bw'].transform(lambda x: x.shift(1))
df['close_prev']   = g['close'].transform(lambda x: x.shift(1))
df['open_prev']    = g['open'].transform(lambda x: x.shift(1))
df['high_prev']    = g['high'].transform(lambda x: x.shift(1))
df['low_prev']     = g['low'].transform(lambda x: x.shift(1))
df['rsi_prev3']    = g['rsi'].transform(lambda x: x.shift(3))

df = df.dropna(subset=['fwd_3c','rsi_prev','ema9l_prev','bw'])

# Return computation
v = df[df['fwd_3c'].notna() & (df['close']>0)].iloc[0]
use_abs = abs(float(v['fwd_3c'])) > float(v['close'])*0.5
df['ret']    = df['fwd_3c'] - df['close'] if use_abs else df['fwd_3c']
df['ret_6c'] = df['fwd_6c'] - df['close'] if use_abs else df['fwd_6c']
df['ret_10c']= df['fwd_10c']- df['close'] if use_abs else df['fwd_10c']

df = df.dropna(subset=['ret'])

# Feature flags
df['green']        = df['close'] > df['open']
df['prev_green']   = df['close_prev'] > df['open_prev']
df['above_ema9l']  = df['close'] > df['ema9_low']
df['rsi_rising']   = df['rsi'] > df['rsi_prev']
df['rsi_delta']    = df['rsi'] - df['rsi_prev']
df['rsi_delta2']   = df['rsi'] - df['rsi_prev2']  # 2-candle RSI change
df['slope1']       = df['ema9_low'] - df['ema9l_prev']
df['slope2']       = df['ema9l_prev'] - df['ema9l_prev2']
df['slope_ok1']    = df['slope1'] >= 0
df['slope_ok2']    = (df['slope1'] >= 0) & (df['slope2'] >= 0)
df['body']         = df['close'] - df['open']
df['body_pct']     = df['body'] / df['open'] * 100
df['prev_body']    = df['close_prev'] - df['open_prev']
df['hour']         = df['timestamp'].dt.hour
df['minute']       = df['timestamp'].dt.minute
df['hhmm']         = df['hour']*60 + df['minute']
df['session']      = pd.cut(
    df['hhmm'],
    bins=[9*60+15, 11*60, 13*60, 15*60+30],
    labels=['morning','midday','afternoon'],
    right=False
)
# Candle position within EMA band
df['close_in_band_pct'] = (df['close'] - df['ema9_low']) / df['bw'].replace(0, np.nan) * 100
# Previous candle closed above EMA9_low too (confirmation)
df['prev_above_ema9l'] = df['close_prev'] > df['ema9l_prev']

DAYS  = df['timestamp'].dt.date.nunique()
BAR   = '━' * 76
MIN_N = 30

print(f"Candles: {len(df)} | Days: {DAYS}\n")

def stats(r):
    if len(r) < MIN_N:
        return None
    return {'n': len(r), 'avg': r.mean(), 'win': (r>0).mean()*100, 'esl': (r<-12).mean()*100}

def row(label, r, extra=''):
    s = stats(r)
    if s is None:
        return f"  {label:45} {'<min':>6}"
    return (f"  {label:45} {s['n']:6d} {s['avg']:+7.1f}  "
            f"{s['win']:6.1f}%  {s['esl']:6.1f}%{extra}")

hdr = f"\n  {'Filter':45} {'n':>6} {'avg':>8} {'win%':>7} {'ESL%':>7}"
sep = f"  {'─'*72}"

# ── V9 current baseline ───────────────────────────────────────────
v9 = df[
    df['green'] &
    df['above_ema9l'] &
    df['slope_ok2'] &
    (df['bw'] >= 13) & (df['bw'] <= 17) &
    (df['rsi'] > 50) & (df['rsi'] < 65) &
    df['rsi_rising']
]
print(f"{BAR}")
print(f"  V9 CURRENT BASELINE")
print(BAR)
print(hdr); print(sep)
print(row("V9 current (BW 13-17, RSI 50-65)", v9['ret']))

# ── PART 1: RSI range ─────────────────────────────────────────────
print(f"\n{BAR}")
print("  PART 1 — RSI RANGE TIGHTENING (V9 base + BW 13-17 + slope2 + green)")
print(BAR); print(hdr); print(sep)

v9_base = df[df['green'] & df['above_ema9l'] & df['slope_ok2'] & (df['bw']>=13) & (df['bw']<=17) & df['rsi_rising']]
for lo, hi in [(40,99),(45,99),(50,99),(50,60),(50,63),(50,65),(52,65),(53,65),(55,65),(55,63),(55,60),(57,65),(52,60)]:
    label = f"RSI {lo}-{hi if hi<99 else 'all'}"
    sub = v9_base[(v9_base['rsi']>lo) & (v9_base['rsi']<hi)]
    print(row(label, sub['ret']))

# ── PART 2: RSI delta ─────────────────────────────────────────────
print(f"\n{BAR}")
print("  PART 2 — RSI DELTA (V9 base + RSI 50-65)")
print(BAR); print(hdr); print(sep)

v9_rsi = df[df['green'] & df['above_ema9l'] & df['slope_ok2'] & (df['bw']>=13) & (df['bw']<=17) & (df['rsi']>50) & (df['rsi']<65)]
print(row("V9 + RSI 50-65 (any delta)", v9_rsi['ret']))
for d in [0.5, 1, 1.5, 2, 3]:
    print(row(f"  + RSI delta >= {d}", v9_rsi[v9_rsi['rsi_delta']>=d]['ret']))
print(row("  + RSI delta2 (2-candle) >= 2", v9_rsi[v9_rsi['rsi_delta2']>=2]['ret']))
print(row("  + RSI delta2 >= 3", v9_rsi[v9_rsi['rsi_delta2']>=3]['ret']))
print(row("  + RSI delta2 >= 5", v9_rsi[v9_rsi['rsi_delta2']>=5]['ret']))

# ── PART 3: BW tightening ─────────────────────────────────────────
print(f"\n{BAR}")
print("  PART 3 — BW RANGE (V9 base + RSI 50-65 + slope2 + green)")
print(BAR); print(hdr); print(sep)

v9_rsi2 = df[df['green'] & df['above_ema9l'] & df['slope_ok2'] & (df['rsi']>50) & (df['rsi']<65) & df['rsi_rising']]
for lo, hi in [(10,20),(11,18),(12,17),(13,17),(13,16),(14,17),(14,16),(15,17),(13,18),(12,18)]:
    label = f"BW {lo}-{hi}"
    sub = v9_rsi2[(v9_rsi2['bw']>=lo) & (v9_rsi2['bw']<=hi)]
    print(row(label, sub['ret']))

# ── PART 4: Slope strictness ──────────────────────────────────────
print(f"\n{BAR}")
print("  PART 4 — SLOPE STRICTNESS (V9 base + BW 13-17 + RSI 50-65)")
print(BAR); print(hdr); print(sep)

v9_bw = df[df['green'] & df['above_ema9l'] & (df['bw']>=13) & (df['bw']<=17) & (df['rsi']>50) & (df['rsi']<65) & df['rsi_rising']]
print(row("No slope filter", v9_bw['ret']))
print(row("slope1 >= 0", v9_bw[v9_bw['slope_ok1']]['ret']))
print(row("slope1 & slope2 >= 0 (current)", v9_bw[v9_bw['slope_ok2']]['ret']))
print(row("slope1 > 0 (strictly positive)", v9_bw[v9_bw['slope1']>0]['ret']))
print(row("slope1 >= 0.5", v9_bw[v9_bw['slope1']>=0.5]['ret']))
print(row("slope1 >= 1", v9_bw[v9_bw['slope1']>=1]['ret']))
print(row("slope1 >= 2", v9_bw[v9_bw['slope1']>=2]['ret']))
s3 = (df['ema9_low'] - g['ema9_low'].transform(lambda x: x.shift(3)))[v9_bw.index]
print(row("slope 3-candle sum >= 0", v9_bw[s3>=0]['ret']))
print(row("slope 3-candle sum >= 1", v9_bw[s3>=1]['ret']))

# ── PART 5: Candle body ───────────────────────────────────────────
print(f"\n{BAR}")
print("  PART 5 — CANDLE BODY SIZE (V9 base + BW 13-17 + RSI 50-65 + slope2)")
print(BAR); print(hdr); print(sep)

v9_full = v9.copy()
print(row("V9 current (any body)", v9_full['ret']))
for bp in [0.2, 0.5, 1.0, 1.5, 2.0]:
    print(row(f"body% >= {bp}", v9_full[v9_full['body_pct']>=bp]['ret']))
print(row("body >= 1pt", v9_full[v9_full['body']>=1]['ret']))
print(row("body >= 2pt", v9_full[v9_full['body']>=2]['ret']))
print(row("body >= 3pt", v9_full[v9_full['body']>=3]['ret']))
print(row("prev candle also green", v9_full[v9_full['prev_green']]['ret']))
print(row("prev candle green + body>=1", v9_full[v9_full['prev_green'] & (v9_full['body']>=1)]['ret']))

# ── PART 6: Time of day ───────────────────────────────────────────
print(f"\n{BAR}")
print("  PART 6 — TIME OF DAY (V9 current gates)")
print(BAR); print(hdr); print(sep)

print(row("All sessions", v9['ret']))
for sess in ['morning','midday','afternoon']:
    print(row(f"Session: {sess}", v9[v9['session']==sess]['ret']))
for h in range(9, 15):
    sub = v9[v9['hour']==h]
    if len(sub) >= MIN_N:
        print(row(f"Hour {h}:xx", sub['ret']))
print(row("Exclude 9:15-9:30", v9[v9['hhmm']>9*60+30]['ret']))
print(row("Exclude 9:15-9:45", v9[v9['hhmm']>9*60+45]['ret']))
print(row("Exclude first 30min + last 30min", v9[(v9['hhmm']>9*60+45) & (v9['hhmm']<15*60)]['ret']))

# ── PART 7: Previous candle confirmation ──────────────────────────
print(f"\n{BAR}")
print("  PART 7 — PREV CANDLE CONFIRMATION (V9 current gates)")
print(BAR); print(hdr); print(sep)

print(row("V9 current", v9['ret']))
print(row("+ prev also above ema9l", v9[v9['prev_above_ema9l']]['ret']))
print(row("+ prev also green", v9[v9['prev_green']]['ret']))
print(row("+ prev green + prev above ema9l", v9[v9['prev_green'] & v9['prev_above_ema9l']]['ret']))
# Close above prev high (strong breakout)
above_prev_high = v9['close'] > v9['high_prev']
print(row("+ close > prev high", v9[above_prev_high]['ret']))
# Not too far above EMA band (close not > 50% into band extension)
in_band = v9['close_in_band_pct'] <= 150
print(row("+ close within 150% band", v9[in_band]['ret']))

# ── PART 8: Ranked by ESL ─────────────────────────────────────────
print(f"\n{BAR}")
print("  PART 8 — ALL COMBOS RANKED BY ESL%")
print(BAR); print(hdr); print(sep)

combos = {}

# V9 baseline
combos['V9 current (BW 13-17, RSI 50-65, slope2)'] = v9['ret']

# RSI tweaks on V9
for lo, hi in [(50,63),(52,65),(53,65),(55,65),(52,60),(55,60)]:
    base = df[df['green'] & df['above_ema9l'] & df['slope_ok2'] & (df['bw']>=13) & (df['bw']<=17) & df['rsi_rising']]
    combos[f'V9 + RSI {lo}-{hi}'] = base[(base['rsi']>lo) & (base['rsi']<hi)]['ret']

# BW tweaks
for lo, hi in [(13,16),(14,17),(13,18)]:
    base = df[df['green'] & df['above_ema9l'] & df['slope_ok2'] & (df['rsi']>50) & (df['rsi']<65) & df['rsi_rising']]
    combos[f'V9 + BW {lo}-{hi}'] = base[(base['bw']>=lo) & (base['bw']<=hi)]['ret']

# RSI delta
base = df[df['green'] & df['above_ema9l'] & df['slope_ok2'] & (df['bw']>=13) & (df['bw']<=17) & (df['rsi']>50) & (df['rsi']<65)]
for d in [1, 1.5, 2]:
    combos[f'V9 + RSI delta >= {d}'] = base[base['rsi_delta']>=d]['ret']

# Slope strict
base2 = df[df['green'] & df['above_ema9l'] & (df['bw']>=13) & (df['bw']<=17) & (df['rsi']>50) & (df['rsi']<65) & df['rsi_rising']]
combos['V9 + slope1 >= 1'] = base2[base2['slope1']>=1]['ret']

# Body filter
combos['V9 + body >= 1pt'] = v9[v9['body']>=1]['ret']
combos['V9 + body >= 2pt'] = v9[v9['body']>=2]['ret']
combos['V9 + prev green'] = v9[v9['prev_green']]['ret']
combos['V9 + prev above ema9l'] = v9[v9['prev_above_ema9l']]['ret']

# Time filters
combos['V9 + exclude first 30min'] = v9[v9['hhmm']>9*60+45]['ret']
combos['V9 + midday only'] = v9[v9['session']=='midday']['ret']

# Combo: RSI delta + body
combos['V9 + RSI delta>=1.5 + body>=1'] = base[base['rsi_delta']>=1.5][base[base['rsi_delta']>=1.5]['body']>=1]['ret']

# Sort by ESL, show only those with MIN_N
rows = []
for name, r in combos.items():
    s = stats(r)
    if s:
        rows.append((name, s))
rows.sort(key=lambda x: x[1]['esl'])

for name, s in rows:
    marker = " ◀ BEST" if s['esl'] == rows[0][1]['esl'] else ""
    print(f"  {name:45} {s['n']:6d} {s['avg']:+7.1f}  {s['win']:6.1f}%  {s['esl']:6.1f}%{marker}")

# ── PART 9: 6c and 10c forward returns for best combos ────────────
print(f"\n{BAR}")
print("  PART 9 — LONGER HOLD (18min & 30min) FOR TOP ESL COMBOS")
print(BAR)
print(f"\n  {'Combo':45} {'9min avg':>9} {'18min avg':>10} {'30min avg':>10} {'ESL 9m':>8}")
print(f"  {'─'*85}")

top_rows = sorted(rows, key=lambda x: x[1]['esl'])[:5]
# Map combo names back to indices
combo_index = {
    'V9 current (BW 13-17, RSI 50-65, slope2)': v9,
}
base_delta = df[df['green'] & df['above_ema9l'] & df['slope_ok2'] & (df['bw']>=13) & (df['bw']<=17) & (df['rsi']>50) & (df['rsi']<65)]
combo_index['V9 + RSI delta >= 1'] = base_delta[base_delta['rsi_delta']>=1]
combo_index['V9 + RSI delta >= 1.5'] = base_delta[base_delta['rsi_delta']>=1.5]
combo_index['V9 + RSI delta >= 2'] = base_delta[base_delta['rsi_delta']>=2]
combo_index['V9 + body >= 1pt'] = v9[v9['body']>=1]
combo_index['V9 + body >= 2pt'] = v9[v9['body']>=2]
combo_index['V9 + prev green'] = v9[v9['prev_green']]
combo_index['V9 + prev above ema9l'] = v9[v9['prev_above_ema9l']]
combo_index['V9 + exclude first 30min'] = v9[v9['hhmm']>9*60+45]

for name, s in top_rows:
    if name not in combo_index:
        continue
    sub = combo_index[name]
    r3  = sub['ret'].dropna()
    r6  = (sub['ret_6c'].dropna() if use_abs else sub['ret_6c'].dropna())
    r10 = (sub['ret_10c'].dropna() if use_abs else sub['ret_10c'].dropna())
    r6  = sub['fwd_6c'].sub(sub['close']) if use_abs else sub['ret_6c']
    r10 = sub['fwd_10c'].sub(sub['close']) if use_abs else sub['ret_10c']
    r6  = r6.dropna(); r10 = r10.dropna()
    print(f"  {name:45} {r3.mean():+8.1f}  {r6.mean() if len(r6)>5 else float('nan'):+9.1f}  "
          f"{r10.mean() if len(r10)>5 else float('nan'):+9.1f}  {s['esl']:7.1f}%")

print(f"\n  Note: {DAYS} days of data. MIN_N={MIN_N} for display.")
print(f"  ESL = trade lost more than 12 pts in 9-min forward return.")
