"""
Backtest V2: Dual-TF alignment — correct 1-min trigger = close > EMA9_high

Why V1 failed: BW 13-17 never occurs on 1-min (bands too narrow at 1-min scale).
The right 1-min signal is a BREAKOUT: close > EMA9_high (price breaks above the
upper EMA9 band). This is exactly what shadow mode tests live.

Alignment tested two ways:
  SAME-BUCKET : 3-min (BW 13-17, RSI 50-65) AND 1-min breakout in same 3-min bucket
                → early entry before 3-min candle closes (cheaper entry price)
  NEXT-BUCKET : 3-min passes at T3, 1-min breakout in [T3+3, T3+6)
                → shadow-mode style (enter after 3-min confirmed, on 1-min trigger)

Parts:
  1 - 1-min breakout standalone quality + RSI sweep
  2 - Diagnostic: 1-min properties inside 3-min tight signal buckets
  3 - Same-bucket alignment configs + entry saving
  4 - Next-bucket alignment (shadow mode)
  5 - Head-to-head: A=current 3m | B=tight 3m | C=same-bkt | D=next-bkt
  6 - Day-by-day
"""
import sqlite3, os, sys
import pandas as pd
import numpy as np

DB = os.path.expanduser("~/lab_data/vrl_data.db")
if not os.path.exists(DB):
    sys.exit(f"DB not found: {DB}")

# ── Load ─────────────────────────────────────────────────────────
con = sqlite3.connect(DB)
print("Loading 1-min option data...", flush=True)
df1 = pd.read_sql("""
    SELECT timestamp, strike, type, open, high, low, close, rsi
    FROM option_1min
    WHERE time(timestamp) >= '09:45:00' AND time(timestamp) < '15:10:00'
    ORDER BY strike, type, timestamp
""", con, parse_dates=['timestamp'])

print("Loading 3-min option data...", flush=True)
df3 = pd.read_sql("""
    SELECT timestamp, strike, type, open, high, low, close,
           rsi, ema9_high, ema9_low, fwd_3c
    FROM option_3min
    WHERE time(timestamp) >= '09:45:00' AND time(timestamp) < '15:00:00'
    ORDER BY strike, type, timestamp
""", con, parse_dates=['timestamp'])
con.close()

print(f"1-min: {len(df1)} rows | {df1['timestamp'].dt.date.nunique()} days")
print(f"3-min: {len(df3)} rows | {df3['timestamp'].dt.date.nunique()} days")

# ── Prepare 1-min ─────────────────────────────────────────────────
df1 = df1.sort_values(['strike','type','timestamp']).copy()
g1  = df1.groupby(['strike','type'])
df1['ema9_high']   = g1['high'].transform(lambda x: x.ewm(span=9,adjust=False).mean())
df1['ema9_low']    = g1['low'].transform(lambda x:  x.ewm(span=9,adjust=False).mean())
df1['bw']          = df1['ema9_high'] - df1['ema9_low']
df1['ema9l_slope'] = g1['ema9_low'].transform(lambda x: x.diff())
df1['rsi_prev']    = g1['rsi'].transform(lambda x: x.shift(1))
df1['ret_3c']      = g1['close'].transform(lambda x: x.shift(-3) - x)
df1['ret_9c']      = g1['close'].transform(lambda x: x.shift(-9) - x)
for n, col in [(3,'ret_3c'), (9,'ret_9c')]:
    ts_n  = g1['timestamp'].transform(lambda x: x.shift(-n))
    df1.loc[ts_n.isna() | (ts_n.dt.date != df1['timestamp'].dt.date), col] = np.nan
df1.dropna(subset=['ema9l_slope','rsi','rsi_prev'], inplace=True)
df1['rsi_rising']  = df1['rsi'] > df1['rsi_prev']
df1['breakout']    = df1['close'] > df1['ema9_high']   # KEY 1-min signal
df1['bucket_ts']   = df1['timestamp'].apply(
    lambda t: t.replace(minute=(t.minute//3)*3, second=0, microsecond=0))

# ── Prepare 3-min ─────────────────────────────────────────────────
df3['fwd_3c'] = pd.to_numeric(df3['fwd_3c'], errors='coerce')
df3 = df3.sort_values(['strike','type','timestamp']).copy()
df3['bw']          = df3['ema9_high'] - df3['ema9_low']
df3['ema9l_slope'] = df3.groupby(['strike','type'])['ema9_low'].diff()
df3['rsi_prev']    = df3.groupby(['strike','type'])['rsi'].shift(1)
v = df3[df3['fwd_3c'].notna() & (df3['close']>0)].iloc[0]
if abs(float(v['fwd_3c'])) > float(v['close'])*0.5:
    df3['ret_3c'] = df3['fwd_3c'] - df3['close']
    print("3-min fwd is absolute price — converted to return")
else:
    df3['ret_3c'] = df3['fwd_3c']
df3.dropna(subset=['ret_3c','ema9l_slope','rsi','rsi_prev'], inplace=True)
df3['rsi_rising'] = df3['rsi'] > df3['rsi_prev']
df3['bucket_ts']  = df3['timestamp']

DAYS = df3['timestamp'].dt.date.nunique()

# ── Gates ─────────────────────────────────────────────────────────
base1 = ((df1['close']>df1['open']) & (df1['close']>df1['ema9_low']) & (df1['ema9l_slope']>=0))
base3 = ((df3['close']>df3['open']) & (df3['close']>df3['ema9_low']) & (df3['ema9l_slope']>=0))

# ── Helpers ───────────────────────────────────────────────────────
def stats(d, col='ret_3c'):
    s = d[col].dropna()
    if len(s) < 5: return None
    return dict(n=len(s), avg=s.mean(), win=(s>0).mean()*100,
                esl=(s<-12).mean()*100, big=(s>20).mean()*100,
                score=s.mean()-((s<-12).mean()*100-40)*0.5)

def pr(label, d, col='ret_3c', w=44):
    r = stats(d, col)
    if r:
        print(f"  {label:{w}} {r['n']:5d} {r['avg']:+7.1f} {r['win']:7.1f} "
              f"{r['esl']:7.1f} {r['big']:7.1f} {r['score']:+7.1f}")
    else:
        print(f"  {label:{w}} (too few: {len(d)})")

HDR = (f"  {'':44} {'n':>5} {'avg':>7} {'win%':>7} {'ESL%':>7} {'big%':>7} {'score':>7}")
BAR = f"{'━'*86}"

# ════════════════════════════════════════════════════════════════
#  PART 1 — 1-min BREAKOUT (close > EMA9_high) standalone
# ════════════════════════════════════════════════════════════════
print(f"\n{BAR}")
print(f"  PART 1 — 1-min BREAKOUT (close>EMA9_high) standalone  [{DAYS} days]")
print(BAR)
print(HDR)
print(f"  {'─'*84}")

b = base1 & df1['breakout']
pr("1m Breakout only (no RSI)", df1[b])
for rlo, rhi in [(45,75),(50,75),(50,70),(50,65),(55,70),(55,65),(55,60),(60,70)]:
    m = b & (df1['rsi']>rlo) & (df1['rsi']<rhi) & df1['rsi_rising']
    pr(f"Breakout + RSI {rlo}-{rhi} rising", df1[m])

# Forward windows for best config
best1m = b & (df1['rsi']>50) & (df1['rsi']<65) & df1['rsi_rising']
d_b1 = df1[best1m]
if len(d_b1) >= 5:
    print(f"\n  Forward window (Breakout + RSI 50-65 rising, n={len(d_b1)}):")
    for col, lbl in [('ret_3c','3-min fwd (3m)'),('ret_9c','9-min fwd (9m)')]:
        r = stats(d_b1, col)
        if r: print(f"    {lbl}: avg {r['avg']:+.1f}  win {r['win']:.1f}%  ESL {r['esl']:.1f}%  score {r['score']:+.1f}")

# ════════════════════════════════════════════════════════════════
#  PART 2 — DIAGNOSTIC inside 3-min tight buckets
# ════════════════════════════════════════════════════════════════
print(f"\n{BAR}")
print(f"  PART 2 — DIAGNOSTIC: 1-min properties inside 3-min tight signal buckets")
print(BAR)

mask3_tight = (base3 & (df3['bw']>=13) & (df3['bw']<=17)
                      & (df3['rsi']>50) & (df3['rsi']<65) & df3['rsi_rising'])
df3_tight = df3[mask3_tight].copy()
n3 = len(df3_tight)

inside = df1.merge(df3_tight[['strike','type','bucket_ts']], on=['strike','type','bucket_ts'], how='inner')
print(f"\n  3-min tight signals (BW 13-17, RSI 50-65): n={n3}")
if len(inside) > 0:
    print(f"  1-min candles in same buckets: {len(inside)}")
    print(f"  ├─ avg BW on 1-min:            {inside['bw'].mean():.2f}  "
          f"(p25={inside['bw'].quantile(.25):.1f} p50={inside['bw'].quantile(.5):.1f} "
          f"p75={inside['bw'].quantile(.75):.1f} p90={inside['bw'].quantile(.9):.1f})")
    brk_pct  = (inside['close'] > inside['ema9_high']).mean()*100
    rsi_pct  = ((inside['rsi']>50) & (inside['rsi']<65) & inside['rsi_rising']).mean()*100
    both_pct = ((inside['close']>inside['ema9_high']) & (inside['rsi']>50)
                & (inside['rsi']<65) & inside['rsi_rising']).mean()*100
    print(f"  ├─ % candles breakout (close>ema9h):  {brk_pct:.1f}%")
    print(f"  ├─ % candles RSI 50-65 rising:        {rsi_pct:.1f}%")
    print(f"  └─ % candles breakout + RSI 50-65:    {both_pct:.1f}%")
    # Buckets that have at least 1 breakout candle
    bkt_brk = (inside[inside['close']>inside['ema9_high']]
               .groupby(['strike','type','bucket_ts']).size())
    print(f"\n  3-min buckets that contain ≥1 breakout 1-min candle: "
          f"{len(bkt_brk)} / {n3}  ({len(bkt_brk)/n3*100:.1f}%)")

# ════════════════════════════════════════════════════════════════
#  PART 3 — SAME-BUCKET ALIGNMENT
#  1-min breakout WITHIN the 3-min signal candle → early entry
# ════════════════════════════════════════════════════════════════
print(f"\n{BAR}")
print(f"  PART 3 — SAME-BUCKET: 1-min breakout within 3-min signal candle")
print(f"  (early entry before 3-min closes — saves pts vs waiting for 3m close)")
print(BAR)
print(HDR)
print(f"  {'─'*84}")

def first_per_bucket(mask_1m, cols_out):
    return (df1[mask_1m].sort_values('timestamp')
            .groupby(['strike','type','bucket_ts']).first().reset_index()
            [['strike','type','bucket_ts'] + cols_out])

align_cfgs = [
    ("Breakout only",                  base1 & df1['breakout']),
    ("Breakout + RSI rising",          base1 & df1['breakout'] & df1['rsi_rising']),
    ("Breakout + RSI 45-75 rising",    base1 & df1['breakout'] & df1['rsi_rising'] & (df1['rsi']>45) & (df1['rsi']<75)),
    ("Breakout + RSI 50-65 rising",    base1 & df1['breakout'] & df1['rsi_rising'] & (df1['rsi']>50) & (df1['rsi']<65)),
    ("Breakout + RSI 55-65 rising",    base1 & df1['breakout'] & df1['rsi_rising'] & (df1['rsi']>55) & (df1['rsi']<65)),
    ("Breakout + RSI 50-70 rising",    base1 & df1['breakout'] & df1['rsi_rising'] & (df1['rsi']>50) & (df1['rsi']<70)),
]

for name, mask_1m in align_cfgs:
    bkts = first_per_bucket(mask_1m, ['close','ret_3c','ret_9c','rsi','bw','timestamp'])
    bkts = bkts.rename(columns={'close':'close_1m','ret_3c':'ret_3c_1m',
                                  'ret_9c':'ret_9c_1m','rsi':'rsi_1m',
                                  'bw':'bw_1m','timestamp':'ts_1m'})
    merged  = df3_tight.merge(bkts, on=['strike','type','bucket_ts'], how='left')
    aligned = merged.dropna(subset=['ret_3c_1m']).copy()
    retain  = len(aligned)/n3*100
    if len(aligned) < 5:
        print(f"  {name:44} (aligned {len(aligned)}/{n3} = {retain:.0f}%)  too few")
        continue
    aligned['saving'] = aligned['close'] - aligned['close_1m']
    r1 = stats(aligned, 'ret_3c_1m')
    r3 = stats(aligned, 'ret_3c')
    sav = aligned['saving'].mean()
    sav_pct = (aligned['saving']>0).mean()*100
    print(f"  {name:44} {r1['n']:5d} {r1['avg']:+7.1f} {r1['win']:7.1f} "
          f"{r1['esl']:7.1f} {r1['big']:7.1f} {r1['score']:+7.1f}"
          f"  retain={retain:.0f}% sav={sav:+.1f}pts cheap={sav_pct:.0f}% 3m-base={r3['avg']:+.1f}")

# ════════════════════════════════════════════════════════════════
#  PART 4 — NEXT-BUCKET ALIGNMENT (shadow mode)
#  3-min passes at T3, enter on 1-min breakout in [T3+3, T3+6)
# ════════════════════════════════════════════════════════════════
print(f"\n{BAR}")
print(f"  PART 4 — NEXT-BUCKET: shadow mode (enter in next 3-min candle on 1m trigger)")
print(BAR)
print(HDR)
print(f"  {'─'*84}")

df3_tnext = df3_tight.copy()
df3_tnext['next_bkt'] = df3_tnext['timestamp'] + pd.Timedelta(minutes=3)

def first_per_bucket_nb(mask_1m):
    return (df1[mask_1m].sort_values('timestamp')
            .groupby(['strike','type','bucket_ts']).first().reset_index()
            [['strike','type','bucket_ts','close','ret_3c','ret_9c','timestamp']]
            .rename(columns={'close':'close_1m','ret_3c':'ret_3c_1m',
                              'ret_9c':'ret_9c_1m','timestamp':'ts_1m'}))

for name, mask_1m in align_cfgs:
    bkts    = first_per_bucket_nb(mask_1m)
    merged  = df3_tnext.merge(bkts, left_on=['strike','type','next_bkt'],
                               right_on=['strike','type','bucket_ts'], how='left')
    aligned = merged.dropna(subset=['ret_3c_1m']).copy()
    retain  = len(aligned)/n3*100
    if len(aligned) < 5:
        print(f"  {name:44} (aligned {len(aligned)}/{n3} = {retain:.0f}%)  too few")
        continue
    r1  = stats(aligned, 'ret_3c_1m')
    r9  = stats(aligned, 'ret_9c_1m')
    r3b = stats(aligned, 'ret_3c')
    r9_str = f"{r9['avg']:+.1f}" if r9 else "n/a"
    print(f"  {name:44} {r1['n']:5d} {r1['avg']:+7.1f} {r1['win']:7.1f} "
          f"{r1['esl']:7.1f} {r1['big']:7.1f} {r1['score']:+7.1f}"
          f"  retain={retain:.0f}% 9m-avg={r9_str} 3m-base={r3b['avg']:+.1f}")

# ════════════════════════════════════════════════════════════════
#  PART 5 — HEAD-TO-HEAD SUMMARY
# ════════════════════════════════════════════════════════════════
print(f"\n{BAR}")
print(f"  PART 5 — HEAD-TO-HEAD ({DAYS} days)")
print(BAR)
print(HDR)
print(f"  {'─'*84}")

mask3_cur = (base3 & (df3['bw']>=11) & (df3['rsi']>45) & (df3['rsi']<75) & df3['rsi_rising'])

# Build C and D using best config: Breakout + RSI 50-65 rising
best_1m = base1 & df1['breakout'] & df1['rsi_rising'] & (df1['rsi']>50) & (df1['rsi']<65)

# C: same-bucket
bkts_C  = first_per_bucket(best_1m, ['close','ret_3c','ret_9c'])
bkts_C  = bkts_C.rename(columns={'close':'close_1m','ret_3c':'ret_3c_1m','ret_9c':'ret_9c_1m'})
df3_C   = df3_tight.merge(bkts_C, on=['strike','type','bucket_ts'], how='inner').copy()
df3_C['saving'] = df3_C['close'] - df3_C['close_1m']

# D: next-bucket
bkts_D  = first_per_bucket_nb(best_1m)
df3_D   = df3_tnext.merge(bkts_D, left_on=['strike','type','next_bkt'],
                            right_on=['strike','type','bucket_ts'], how='inner').copy()

pr("A. CURRENT 3-min (BW>=11, RSI 45-75)",           df3[mask3_cur])
pr("B. TIGHT   3-min (BW 13-17, RSI 50-65)",         df3_tight)
pr("C. SAME-BKT dual (1m Breakout+RSI50-65) 3m-ret", df3_C, col='ret_3c_1m')
pr("C. SAME-BKT dual (1m Breakout+RSI50-65) 9m-ret", df3_C, col='ret_9c_1m')
pr("D. NEXT-BKT dual (shadow mode)         3m-ret",  df3_D, col='ret_3c_1m')
pr("D. NEXT-BKT dual (shadow mode)         9m-ret",  df3_D, col='ret_9c_1m')

if len(df3_C) >= 5:
    print(f"\n  Strategy C early-entry saving (1m price vs 3m close):")
    print(f"    avg = {df3_C['saving'].mean():+.2f} pts")
    print(f"    cheaper in {(df3_C['saving']>0).mean()*100:.0f}% of cases")

# ════════════════════════════════════════════════════════════════
#  PART 6 — DAY-BY-DAY
# ════════════════════════════════════════════════════════════════
print(f"\n{BAR}")
print(f"  PART 6 — DAY-BY-DAY (A=current3m | B=tight3m | C=same-bkt-1m | D=next-bkt-1m)")
print(BAR)
print(f"  {'Date':12} {'A n':>4} {'A avg':>8} {'B n':>4} {'B avg':>8} "
      f"{'C n':>4} {'C avg':>8} {'D n':>4} {'D avg':>8}")
print(f"  {'─'*84}")

dA = df3[mask3_cur]
dB = df3_tight
all_dates = sorted(set(dA['timestamp'].dt.date) | set(dB['timestamp'].dt.date))
for date in all_dates:
    a  = dA[dA['timestamp'].dt.date == date]
    b  = dB[dB['timestamp'].dt.date == date]
    c  = df3_C[df3_C['timestamp'].dt.date == date] if len(df3_C)>0 else pd.DataFrame()
    d  = df3_D[df3_D['timestamp'].dt.date == date] if len(df3_D)>0 else pd.DataFrame()
    a_avg = a['ret_3c'].mean()    if len(a)>0 else float('nan')
    b_avg = b['ret_3c'].mean()    if len(b)>0 else float('nan')
    c_avg = c['ret_3c_1m'].mean() if len(c)>0 else float('nan')
    d_avg = d['ret_3c_1m'].mean() if len(d)>0 else float('nan')
    print(f"  {str(date):12} {len(a):4d} {a_avg:+8.1f} {len(b):4d} {b_avg:+8.1f} "
          f"{len(c):4d} {c_avg:+8.1f} {len(d):4d} {d_avg:+8.1f}")
