"""
Backtest: 1-min BW/RSI sweep + dual-TF alignment sweet spot

Part 1 : BW/RSI sweep on 1-min data  (mirrors 3-min analysis)
Part 2 : Dual-TF alignment — 3-min tight filter AND 1-min filter in same bucket
         Compares 3 strategies: current 3-min | tight 3-min | both-TF aligned
Part 3 : Early-entry benefit — how many pts cheaper the 1-min trigger is vs 3-min close

Run: python3 ~/VISHAL_RAJPUT/backtest_dual_tf_sweep.py
"""
import sqlite3, os, sys
import pandas as pd
import numpy as np

DB = os.path.expanduser("~/lab_data/vrl_data.db")
if not os.path.exists(DB):
    sys.exit(f"DB not found: {DB}")

# ════════════════════════════════════════════════════════════════
#  LOAD DATA
# ════════════════════════════════════════════════════════════════
con = sqlite3.connect(DB)
print("Loading 1-min option data...", flush=True)
df1 = pd.read_sql("""
    SELECT timestamp, strike, type, open, high, low, close, rsi
    FROM option_1min
    WHERE time(timestamp) >= '09:45:00' AND time(timestamp) < '15:00:00'
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

print(f"1-min rows: {len(df1)} | Days: {df1['timestamp'].dt.date.nunique()}")
print(f"3-min rows: {len(df3)} | Days: {df3['timestamp'].dt.date.nunique()}")

# ════════════════════════════════════════════════════════════════
#  PREPARE 1-MIN
# ════════════════════════════════════════════════════════════════
df1 = df1.sort_values(['strike','type','timestamp'])
g1 = df1.groupby(['strike','type'])
df1['ema9_high']   = g1['high'].transform(lambda x: x.ewm(span=9, adjust=False).mean())
df1['ema9_low']    = g1['low'].transform(lambda x:  x.ewm(span=9, adjust=False).mean())
df1['bw']          = df1['ema9_high'] - df1['ema9_low']
df1['ema9l_slope'] = g1['ema9_low'].transform(lambda x: x.diff())
df1['rsi_prev']    = g1['rsi'].transform(lambda x: x.shift(1))

# Forward returns from shifted close (fwd cols NULL in 1-min table)
df1['ret_3c'] = g1['close'].transform(lambda x: x.shift(-3) - x)
df1['ret_5c'] = g1['close'].transform(lambda x: x.shift(-5) - x)
df1['_ts3']   = g1['timestamp'].transform(lambda x: x.shift(-3))
df1.loc[df1['_ts3'].dt.date != df1['timestamp'].dt.date, 'ret_3c'] = np.nan
df1.drop(columns=['_ts3'], inplace=True)

df1 = df1.dropna(subset=['ret_3c','ema9l_slope','rsi','rsi_prev'])
df1['rsi_rising'] = df1['rsi'] > df1['rsi_prev']

# 3-min bucket timestamp for each 1-min row
df1['bucket_ts'] = df1['timestamp'].apply(
    lambda t: t.replace(minute=(t.minute // 3) * 3, second=0, microsecond=0)
)

# ════════════════════════════════════════════════════════════════
#  PREPARE 3-MIN
# ════════════════════════════════════════════════════════════════
df3['fwd_3c'] = pd.to_numeric(df3['fwd_3c'], errors='coerce')
df3 = df3.sort_values(['strike','type','timestamp'])
df3['bw']          = df3['ema9_high'] - df3['ema9_low']
df3['ema9l_slope'] = df3.groupby(['strike','type'])['ema9_low'].diff()
df3['rsi_prev']    = df3.groupby(['strike','type'])['rsi'].shift(1)
v = df3[df3['fwd_3c'].notna() & (df3['close'] > 0)].iloc[0]
if abs(float(v['fwd_3c'])) > float(v['close']) * 0.5:
    df3['ret_3c'] = df3['fwd_3c'] - df3['close']
    print("3-min fwd is absolute price — converted to return")
else:
    df3['ret_3c'] = df3['fwd_3c']
df3 = df3.dropna(subset=['ret_3c','ema9l_slope','rsi','rsi_prev'])
df3['rsi_rising']  = df3['rsi'] > df3['rsi_prev']
df3['bucket_ts']   = df3['timestamp']   # 3-min ts IS the bucket start

DAYS = df3['timestamp'].dt.date.nunique()

# ── base gates ────────────────────────────────────────────────────
base1 = (
    (df1['close'] > df1['open'])     &   # G1
    (df1['close'] > df1['ema9_low']) &   # G2
    (df1['ema9l_slope'] >= 0)            # G2B
)
base3 = (
    (df3['close'] > df3['open'])     &
    (df3['close'] > df3['ema9_low']) &
    (df3['ema9l_slope'] >= 0)
)

# ════════════════════════════════════════════════════════════════
#  PART 1A: FILTER COMPARISON — 1-MIN DATA
# ════════════════════════════════════════════════════════════════
print(f"\n{'━'*82}")
print(f"  PART 1A — FILTER COMPARISON on 1-min data ({DAYS} days, ret=3-candle fwd)")
print(f"{'━'*82}")
print(f"  {'Filter':42} {'n':>5} {'avg':>7} {'win%':>7} {'ESL%':>7} {'big%':>7} {'score':>7}")
print(f"  {'─'*80}")

def stats(d):
    if len(d) < 10: return None
    n   = len(d)
    avg = d['ret_3c'].mean()
    win = (d['ret_3c'] > 0).mean() * 100
    esl = (d['ret_3c'] < -12).mean() * 100
    big = (d['ret_3c'] > 20).mean() * 100
    score = avg - (esl - 40) * 0.5
    return dict(n=n, avg=avg, win=win, esl=esl, big=big, score=score)

filters_1m = {
    "CURRENT  (BW>=11, RSI 45-75)":       (11, 999, 45, 75),
    "TIGHT    (BW 13-17, RSI 55-65)":     (13,  17, 55, 65),
    "SWEET    (BW 13-17, RSI 50-65)":     (13,  17, 50, 65),
    "BW only  (BW 13-17, RSI 45-75)":     (13,  17, 45, 75),
    "RSI only (BW>=11, RSI 50-65)":       (11, 999, 50, 65),
}
for name, (blo, bhi, rlo, rhi) in filters_1m.items():
    mask = (base1 & (df1['bw']>=blo) & (df1['bw']<=bhi)
                  & (df1['rsi']>rlo) & (df1['rsi']<rhi) & df1['rsi_rising'])
    r = stats(df1[mask])
    if r:
        print(f"  {name:42} {r['n']:5d} {r['avg']:+7.1f} {r['win']:7.1f} "
              f"{r['esl']:7.1f} {r['big']:7.1f} {r['score']:+7.1f}")

# ════════════════════════════════════════════════════════════════
#  PART 1B: BW SWEEP on 1-min (within RSI 50-65)
# ════════════════════════════════════════════════════════════════
print(f"\n{'━'*82}")
print(f"  PART 1B — BW SWEEP on 1-min within RSI 50-65")
print(f"{'━'*82}")
print(f"  {'BW range':20} {'n':>5} {'avg':>7} {'win%':>7} {'ESL%':>7} {'big%':>7} {'score':>7}")
print(f"  {'─'*80}")

bw_ranges = [
    (0,999),(5,999),(8,999),(10,999),(11,999),(12,999),(13,999),
    (11,20),(12,20),(13,20),(11,17),(12,17),(13,17),
    (13,16),(14,17),(14,16),(15,20),(15,18),
]
for blo, bhi in bw_ranges:
    mask = (base1 & (df1['bw']>=blo) & (df1['bw']<=bhi)
                  & (df1['rsi']>50) & (df1['rsi']<65) & df1['rsi_rising'])
    r = stats(df1[mask])
    if r:
        lbl = f"BW {blo}-{'∞' if bhi==999 else bhi}"
        print(f"  {lbl:20} {r['n']:5d} {r['avg']:+7.1f} {r['win']:7.1f} "
              f"{r['esl']:7.1f} {r['big']:7.1f} {r['score']:+7.1f}")

# ════════════════════════════════════════════════════════════════
#  PART 1C: RSI SWEEP on 1-min (within BW 13-17)
# ════════════════════════════════════════════════════════════════
print(f"\n{'━'*82}")
print(f"  PART 1C — RSI SWEEP on 1-min within BW 13-17")
print(f"{'━'*82}")
print(f"  {'RSI range':20} {'n':>5} {'avg':>7} {'win%':>7} {'ESL%':>7} {'big%':>7} {'score':>7}")
print(f"  {'─'*80}")

rsi_ranges = [(45,75),(50,75),(50,70),(50,65),(55,75),(55,70),(55,65),(55,60),(60,75)]
for rlo, rhi in rsi_ranges:
    mask = (base1 & (df1['bw']>=13) & (df1['bw']<=17)
                  & (df1['rsi']>rlo) & (df1['rsi']<rhi) & df1['rsi_rising'])
    r = stats(df1[mask])
    if r:
        print(f"  {'RSI '+str(rlo)+'-'+str(rhi):20} {r['n']:5d} {r['avg']:+7.1f} "
              f"{r['win']:7.1f} {r['esl']:7.1f} {r['big']:7.1f} {r['score']:+7.1f}")

# ════════════════════════════════════════════════════════════════
#  PART 2: DUAL-TF ALIGNMENT
#  For each 3-min signal (bucket), check if a 1-min candle in the
#  SAME bucket ALSO passes the 1-min filter.
#  Measure quality of aligned vs non-aligned subsets.
# ════════════════════════════════════════════════════════════════
print(f"\n{'━'*82}")
print(f"  PART 2 — DUAL-TF ALIGNMENT (3-min BW 13-17 RSI 50-65 + 1-min filter)")
print(f"{'━'*82}")

# 3-min tight signal (best from previous backtest)
mask3_tight = (base3 & (df3['bw']>=13) & (df3['bw']<=17)
                      & (df3['rsi']>50) & (df3['rsi']<65) & df3['rsi_rising'])
df3_tight = df3[mask3_tight].copy()

print(f"\n  3-min anchor (BW 13-17, RSI 50-65): n={len(df3_tight)}, "
      f"avg={df3_tight['ret_3c'].mean():+.1f}, "
      f"win={(df3_tight['ret_3c']>0).mean()*100:.1f}%, "
      f"ESL={(df3_tight['ret_3c']<-12).mean()*100:.1f}%")

print(f"\n  {'1-min filter':38} {'aligned':>8} {'retain%':>8} "
      f"{'avg':>7} {'win%':>7} {'ESL%':>7} {'score':>7}")
print(f"  {'─'*80}")

alignment_tests = [
    ("1m: green+G2+G2B only (no BW/RSI)",     0,999,  0,100, False),
    ("1m: BW>=11,  RSI 45-75 rising",         11,999, 45, 75, True),
    ("1m: BW>=11,  RSI 50-65 rising",         11,999, 50, 65, True),
    ("1m: BW 13-17, RSI 45-75 rising",        13, 17, 45, 75, True),
    ("1m: BW 13-17, RSI 50-65 rising",        13, 17, 50, 65, True),
    ("1m: BW 13-17, RSI 55-65 rising",        13, 17, 55, 65, True),
    ("1m: BW 13-17, RSI 50-70 rising",        13, 17, 50, 70, True),
]

for name, blo, bhi, rlo, rhi, rsi_req in alignment_tests:
    if rsi_req:
        m1 = (base1 & (df1['bw']>=blo) & (df1['bw']<=bhi)
                     & (df1['rsi']>rlo) & (df1['rsi']<rhi) & df1['rsi_rising'])
    else:
        m1 = base1.copy()

    # Unique buckets that have at least one passing 1-min candle
    buckets_1m = (df1[m1]
        .groupby(['strike','type','bucket_ts'])
        .size()
        .reset_index(name='_cnt'))

    merged = df3_tight.merge(
        buckets_1m, on=['strike','type','bucket_ts'], how='left')
    aligned    = merged[merged['_cnt'] > 0]
    not_aligned = merged[merged['_cnt'].isna()]

    if len(aligned) < 5:
        retain = len(aligned) / max(len(df3_tight), 1) * 100
        print(f"  {name:38} {len(aligned):8d} {retain:7.1f}%   (too few)")
        continue

    r = stats(aligned)
    retain = len(aligned) / len(df3_tight) * 100
    print(f"  {name:38} {r['n']:8d} {retain:7.1f}% {r['avg']:+7.1f} "
          f"{r['win']:7.1f} {r['esl']:7.1f} {r['score']:+7.1f}")

# ════════════════════════════════════════════════════════════════
#  PART 3: SUMMARY — 4 strategies head-to-head
# ════════════════════════════════════════════════════════════════
print(f"\n{'━'*82}")
print(f"  PART 3 — HEAD-TO-HEAD SUMMARY ({DAYS} days)")
print(f"{'━'*82}")
print(f"  {'Strategy':45} {'n':>5} {'avg':>7} {'win%':>7} {'ESL%':>7} {'big%':>7} {'score':>7}")
print(f"  {'─'*80}")

# Strategy A: current 3-min
mask3_cur = (base3 & (df3['bw']>=11)
                   & (df3['rsi']>45) & (df3['rsi']<75) & df3['rsi_rising'])

# Strategy B: tight 3-min (already have df3_tight)

# Strategy C: dual-TF aligned (best 1-min filter from Part 2 = BW 13-17, RSI 50-65)
mask1_best = (base1 & (df1['bw']>=13) & (df1['bw']<=17)
                     & (df1['rsi']>50) & (df1['rsi']<65) & df1['rsi_rising'])
buckets_best = (df1[mask1_best]
    .groupby(['strike','type','bucket_ts'])
    .size()
    .reset_index(name='_cnt'))
df3_aligned = df3_tight.merge(
    buckets_best, on=['strike','type','bucket_ts'], how='left')
df3_aligned = df3_aligned[df3_aligned['_cnt'] > 0]

# Strategy D: dual-TF aligned using 1-min ret_3c (actual 1-min forward return)
# First 1-min trigger per bucket (earliest entry)
df1_first = (df1[mask1_best]
    .sort_values('timestamp')
    .groupby(['strike','type','bucket_ts'])
    .first()
    .reset_index()
    [['strike','type','bucket_ts','close','ret_3c','rsi','bw']]
    .rename(columns={'close':'close_1m', 'ret_3c':'ret_3c_1m',
                     'rsi':'rsi_1m', 'bw':'bw_1m'}))

df3_aligned_detail = df3_tight.merge(
    df1_first, on=['strike','type','bucket_ts'], how='inner')
df3_aligned_detail['entry_saving'] = (
    df3_aligned_detail['close'] - df3_aligned_detail['close_1m'])

strategies = [
    ("A. CURRENT 3-min (BW>=11, RSI 45-75)",     df3[mask3_cur],  'ret_3c'),
    ("B. TIGHT   3-min (BW 13-17, RSI 50-65)",   df3_tight,       'ret_3c'),
    ("C. DUAL-TF aligned (3m+1m both tight)",     df3_aligned,     'ret_3c'),
    ("D. DUAL-TF 1m-entry return (1m fwd ret)",  df3_aligned_detail, 'ret_3c_1m'),
]
for label, d, col in strategies:
    if len(d) < 5: continue
    r = stats(d.rename(columns={col:'ret_3c'}) if col != 'ret_3c' else d)
    if r:
        print(f"  {label:45} {r['n']:5d} {r['avg']:+7.1f} {r['win']:7.1f} "
              f"{r['esl']:7.1f} {r['big']:7.1f} {r['score']:+7.1f}")

# Early entry benefit
if len(df3_aligned_detail) > 5:
    avg_saving = df3_aligned_detail['entry_saving'].mean()
    pct_cheaper = (df3_aligned_detail['entry_saving'] > 0).mean() * 100
    print(f"\n  Early-entry benefit (1m trigger vs 3m close):")
    print(f"    avg saving  = {avg_saving:+.1f} pts  "
          f"(positive = 1m entered cheaper than 3m close)")
    print(f"    % cheaper   = {pct_cheaper:.1f}% of aligned signals")
    print(f"    avg 3m-ret from 3m close = {df3_aligned_detail['ret_3c'].mean():+.1f}")
    print(f"    avg 3m-ret from 1m entry = {df3_aligned_detail['ret_3c_1m'].mean():+.1f}")

# ════════════════════════════════════════════════════════════════
#  PART 4: DAY-BY-DAY — strategies A / B / C
# ════════════════════════════════════════════════════════════════
print(f"\n{'━'*82}")
print(f"  PART 4 — DAY-BY-DAY comparison")
print(f"{'━'*82}")
print(f"  {'Date':12} {'A n':>4} {'A avg':>8} {'B n':>4} {'B avg':>8} "
      f"{'C n':>4} {'C avg':>8} {'saving':>8}")
print(f"  {'─'*80}")

dA = df3[mask3_cur]
dB = df3_tight
dC = df3_aligned_detail

all_dates = sorted(set(dA['timestamp'].dt.date) |
                   set(dB['timestamp'].dt.date) |
                   set(dC['timestamp'].dt.date))
for date in all_dates:
    a = dA[dA['timestamp'].dt.date == date]
    b = dB[dB['timestamp'].dt.date == date]
    c = dC[dC['timestamp'].dt.date == date]
    a_avg = a['ret_3c'].mean()    if len(a) else float('nan')
    b_avg = b['ret_3c'].mean()    if len(b) else float('nan')
    c_avg = c['ret_3c_1m'].mean() if len(c) else float('nan')
    sav   = c['entry_saving'].mean() if len(c) else float('nan')
    print(f"  {str(date):12} {len(a):4d} {a_avg:+8.1f} "
          f"{len(b):4d} {b_avg:+8.1f} "
          f"{len(c):4d} {c_avg:+8.1f} {sav:+8.1f}")
