"""
V9 3-min ESL Combo Analysis
Combines the 3 key findings from deep dive:
  1. Time window (avoid 09:45-10:15 and 13:45-14:15)
  2. RSI 62-65 vs lower RSI bands
  3. BW upper limit at 15 instead of 17
  4. RSI delta cap (avoid chasing)
  5. Prior slope steepness filter (slope2 cap)

Goal: quantify how much ESL drops when these are stacked,
and find the best combo that keeps enough signal count.

Run: bash ~/VISHAL_RAJPUT/run_backtest.sh v9_esl_combo
"""
import sqlite3, os, sys
import pandas as pd
import numpy as np

DB = os.path.expanduser("~/lab_data/vrl_data.db")
if not os.path.exists(DB):
    sys.exit(f"DB not found: {DB}")

con = sqlite3.connect(DB)
df = pd.read_sql("""
    SELECT timestamp, strike, type, open, high, low, close,
           rsi, ema9_high, ema9_low
    FROM option_3min
    WHERE time(timestamp) >= '09:18:00' AND time(timestamp) < '15:30:00'
    ORDER BY strike, type, timestamp
""", con, parse_dates=['timestamp'])
con.close()

df = df.sort_values(['strike','type','timestamp']).copy()
g  = df.groupby(['strike','type'])

df['fwd_3c']    = g['close'].transform(lambda x: x.shift(-3))
df['rsi_prev']  = g['rsi'].transform(lambda x: x.shift(1))
df['rsi_prev2'] = g['rsi'].transform(lambda x: x.shift(2))
df['ema9l_prev']= g['ema9_low'].transform(lambda x: x.shift(1))
df['ema9l_prev2']= g['ema9_low'].transform(lambda x: x.shift(2))
df['ema9l_prev3']= g['ema9_low'].transform(lambda x: x.shift(3))
df['bw']        = df['ema9_high'] - df['ema9_low']
df['slope1']    = df['ema9_low'] - df['ema9l_prev']
df['slope2']    = df['ema9l_prev'] - df['ema9l_prev2']
df['slope3']    = df['ema9l_prev2'] - df['ema9l_prev3']
df['slope_sum'] = df['slope1'] + df['slope2'] + df['slope3']
df['close_prev']= g['close'].transform(lambda x: x.shift(1))
df['open_prev'] = g['open'].transform(lambda x: x.shift(1))

df = df.dropna(subset=['fwd_3c','rsi_prev','ema9l_prev','bw'])

v = df[df['fwd_3c'].notna() & (df['close']>0)].iloc[0]
use_abs = abs(float(v['fwd_3c'])) > float(v['close'])*0.5
df['ret'] = df['fwd_3c'] - df['close'] if use_abs else df['fwd_3c']
df = df.dropna(subset=['ret'])

df['green']      = df['close'] > df['open']
df['prev_green'] = df['close_prev'] > df['open_prev']
df['above_ema9l']= df['close'] > df['ema9_low']
df['rsi_rising'] = df['rsi'] > df['rsi_prev']
df['rsi_delta']  = df['rsi'] - df['rsi_prev']
df['hhmm']       = df['timestamp'].dt.hour*60 + df['timestamp'].dt.minute

# Time flags
df['bad_open']   = df['hhmm'] < 9*60+45          # first 30min
df['bad_1345']   = (df['hhmm'] >= 13*60+45) & (df['hhmm'] < 14*60+15)
df['good_window']= (~df['bad_open']) & (~df['bad_1345'])
df['golden_win'] = (df['hhmm'] >= 10*60+45) & (df['hhmm'] < 12*60+45)

DAYS = df['timestamp'].dt.date.nunique()
BAR  = '━' * 76
MIN_N = 15

print(f"Days: {DAYS} | Total candles: {len(df)}\n")

def s(r):
    if len(r) < MIN_N: return None
    return dict(n=len(r), avg=r.mean(), win=(r>0).mean()*100, esl=(r<-12).mean()*100, big=(r>12).mean()*100)

def row(label, r, highlight=False):
    st = s(r)
    if st is None:
        return f"  {label:48} {'<min':>5}"
    marker = " ◀◀" if highlight else ""
    return (f"  {label:48} {st['n']:5d} {st['avg']:+7.1f}  "
            f"{st['win']:5.1f}%  {st['esl']:5.1f}%  {st['big']:5.1f}%{marker}")

hdr = f"\n  {'Filter':48} {'n':>5} {'avg':>8} {'win%':>6} {'ESL%':>6} {'big%':>6}"
sep = f"  {'─'*76}"

# V9 current baseline
v9 = df[
    df['green'] & df['above_ema9l'] &
    (df['slope1']>=0) & (df['slope2']>=0) &
    (df['bw']>=13) & (df['bw']<=17) &
    (df['rsi']>50) & (df['rsi']<65) &
    df['rsi_rising']
]

print(f"{BAR}")
print("  BASELINE")
print(BAR); print(hdr); print(sep)
print(row("V9 current (BW 13-17, RSI 50-65, slope2)", v9['ret']))

# ── PART 1: Single filters applied to V9 ─────────────────────────
print(f"\n{BAR}")
print("  PART 1 — SINGLE FILTERS ON V9 BASELINE")
print(BAR); print(hdr); print(sep)

print(row("V9 baseline", v9['ret']))
print(row("+ avoid open (no 09:15-09:45)", v9[~v9['bad_open']]['ret']))
print(row("+ avoid 13:45-14:15", v9[~v9['bad_1345']]['ret']))
print(row("+ avoid both bad windows", v9[v9['good_window']]['ret']))
print(row("+ golden window only (10:45-12:45)", v9[v9['golden_win']]['ret']))
print(row("+ RSI 62-65 only", v9[(v9['rsi']>=62)]['ret']))
print(row("+ RSI >= 58", v9[(v9['rsi']>=58)]['ret']))
print(row("+ BW <= 15 (cap upper)", v9[v9['bw']<=15]['ret']))
print(row("+ BW <= 16", v9[v9['bw']<=16]['ret']))
print(row("+ RSI delta <= 3.0 (no chase)", v9[v9['rsi_delta']<=3.0]['ret']))
print(row("+ RSI delta <= 2.5", v9[v9['rsi_delta']<=2.5]['ret']))
print(row("+ slope2 <= 5 (prior not steep)", v9[v9['slope2']<=5]['ret']))
print(row("+ slope2 <= 3", v9[v9['slope2']<=3]['ret']))
print(row("+ slope_sum >= 3 (sustained rise)", v9[v9['slope_sum']>=3]['ret']))
print(row("+ prev green", v9[v9['prev_green']]['ret']))

# ── PART 2: Stacked combinations ─────────────────────────────────
print(f"\n{BAR}")
print("  PART 2 — STACKED COMBOS (building up from best singles)")
print(BAR); print(hdr); print(sep)

base = v9

# Stack time first
t1 = base[base['good_window']]
print(row("V9 + avoid bad windows", t1['ret']))

t2 = t1[t1['bw']<=15]
print(row("  + BW <= 15", t2['ret']))

t3 = t1[t1['rsi_delta']<=3.0]
print(row("  + RSI delta <= 3.0", t3['ret']))

t4 = t1[t1['slope2']<=5]
print(row("  + slope2 <= 5", t4['ret']))

t5 = t1[t1['bw']<=15][t1[t1['bw']<=15]['rsi_delta']<=3.0]
print(row("  + BW<=15 + delta<=3.0", t5['ret']))

t6 = t1[t1['bw']<=15][t1[t1['bw']<=15]['slope2']<=5]
print(row("  + BW<=15 + slope2<=5", t6['ret']))

t7 = t1[t1['rsi_delta']<=3.0][t1[t1['rsi_delta']<=3.0]['slope2']<=5]
print(row("  + delta<=3.0 + slope2<=5", t7['ret']))

t8 = t1[(t1['bw']<=15) & (t1['rsi_delta']<=3.0) & (t1['slope2']<=5)]
print(row("  + BW<=15 + delta<=3.0 + slope2<=5", t8['ret']))

print()
# RSI bucket focus
r1 = base[(base['rsi']>=62)]
print(row("V9 + RSI 62-65", r1['ret']))
r2 = r1[r1['good_window']]
print(row("  + avoid bad windows", r2['ret']))
r3 = r2[r2['bw']<=15]
print(row("  + BW <= 15", r3['ret']))
r4 = r2[r2['rsi_delta']<=3.0]
print(row("  + RSI delta <= 3.0", r4['ret']))

print()
# Golden window
g1 = base[base['golden_win']]
print(row("V9 + golden window (10:45-12:45)", g1['ret']))
g2 = g1[g1['bw']<=15]
print(row("  + BW <= 15", g2['ret']))
g3 = g1[g1['rsi_delta']<=3.0]
print(row("  + RSI delta <= 3.0", g3['ret']))
g4 = g1[(g1['bw']<=15) & (g1['rsi_delta']<=3.0)]
print(row("  + BW<=15 + delta<=3.0", g4['ret']))

# ── PART 3: Full grid — time × RSI × BW ──────────────────────────
print(f"\n{BAR}")
print("  PART 3 — GRID: TIME WINDOW × RSI RANGE × BW LIMIT")
print(BAR)
print(hdr); print(sep)

time_opts = [
    ('all time',        base),
    ('good window',     base[base['good_window']]),
    ('golden window',   base[base['golden_win']]),
]
rsi_opts = [
    ('RSI 50-65', (50, 65)),
    ('RSI 56-65', (56, 65)),
    ('RSI 62-65', (62, 65)),
]
bw_opts = [
    ('BW 13-17', 17),
    ('BW 13-15', 15),
]

for tname, tsub in time_opts:
    for bname, bmax in bw_opts:
        for rname, (rlo, rhi) in rsi_opts:
            sub = tsub[(tsub['bw']<=bmax) & (tsub['rsi']>rlo) & (tsub['rsi']<rhi)]
            label = f"{tname} | {bname} | {rname}"
            st = s(sub['ret'])
            if st is None:
                continue
            hi_flag = st['esl'] < 15.0
            print(row(label, sub['ret'], highlight=hi_flag))

# ── PART 4: RSI delta cap effect ─────────────────────────────────
print(f"\n{BAR}")
print("  PART 4 — RSI DELTA CAP: exact threshold sweep")
print(BAR); print(hdr); print(sep)

for cap in [1.5, 2.0, 2.5, 3.0, 3.5, 4.0, 5.0, 99]:
    sub = v9[v9['rsi_delta'] <= cap] if cap < 99 else v9
    label = f"RSI delta <= {cap}" if cap < 99 else "no delta cap"
    print(row(label, sub['ret']))

# ── PART 5: Slope2 cap effect ─────────────────────────────────────
print(f"\n{BAR}")
print("  PART 5 — SLOPE2 CAP: prior EMA slope steepness")
print(BAR); print(hdr); print(sep)

for cap in [1, 2, 3, 4, 5, 7, 10, 99]:
    sub = v9[v9['slope2'] <= cap] if cap < 99 else v9
    label = f"slope2 <= {cap}" if cap < 99 else "no slope2 cap"
    print(row(label, sub['ret']))

# ── PART 6: ESL day breakdown for best combo ─────────────────────
print(f"\n{BAR}")
print("  PART 6 — DAY-BY-DAY FOR BEST COMBO (good_window + BW<=15 + delta<=3)")
print(BAR)
best = base[base['good_window'] & (base['bw']<=15) & (base['rsi_delta']<=3.0)]
print(f"\n  V9 total:   n={len(v9)}  avg={v9['ret'].mean():+.1f}  "
      f"win={( v9['ret']>0).mean()*100:.1f}%  ESL={(v9['ret']<-12).mean()*100:.1f}%")
print(f"  Best combo: n={len(best)}  avg={best['ret'].mean():+.1f}  "
      f"win={(best['ret']>0).mean()*100:.1f}%  ESL={(best['ret']<-12).mean()*100:.1f}%\n")

print(f"  {'Date':12} {'V9 n':>5} {'V9 avg':>8} {'V9 ESL':>7}  "
      f"{'Best n':>6} {'Best avg':>9} {'Best ESL':>9}")
print(f"  {'─'*70}")
for d in sorted(df['timestamp'].dt.date.unique()):
    c = v9[v9['timestamp'].dt.date==d]['ret']
    b = best[best['timestamp'].dt.date==d]['ret']
    if len(c)==0 and len(b)==0: continue
    c_avg = c.mean() if len(c)>0 else float('nan')
    b_avg = b.mean() if len(b)>0 else float('nan')
    c_esl = (c<-12).mean()*100 if len(c)>0 else float('nan')
    b_esl = (b<-12).mean()*100 if len(b)>0 else float('nan')
    print(f"  {str(d):12} {len(c):5d} {c_avg:+7.1f}  {c_esl:6.1f}%  "
          f"{len(b):6d} {b_avg:+8.1f}  {b_esl:8.1f}%")

print(f"\n  Note: {DAYS} days. MIN_N={MIN_N}. ESL = ret < -12 pts in 9 min.")
