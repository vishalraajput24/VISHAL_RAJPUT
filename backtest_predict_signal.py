"""
Signal prediction: P(tight signal) from previous candle features

Goal: Enter at candle OPEN if P(this candle will be tight) > threshold.
      Tight = BW 13-17, RSI 50-65, green, close>ema9l, slope>=0

At T3 open we know EVERYTHING about the previous candle — BW, RSI,
RSI trend, EMA9_low slope, candle color. Build a probability map
from those features and test the open-entry return.

Method: simple conditional probability (no ML — data too small for it)
  - Bin prev_bw and prev_rsi
  - Compute P(tight | prev_bw_bin, prev_rsi_bin)
  - Score each candle using multi-feature scoring
  - For each score threshold: show precision, n_entered, avg return from OPEN

Run: python3 ~/VISHAL_RAJPUT/backtest_predict_signal.py
"""
import sqlite3, os, sys
import pandas as pd
import numpy as np

DB = os.path.expanduser("~/lab_data/vrl_data.db")
if not os.path.exists(DB):
    sys.exit(f"DB not found: {DB}")

con = sqlite3.connect(DB)
print("Loading 3-min...", flush=True)
df = pd.read_sql("""
    SELECT timestamp, strike, type, open, high, low, close,
           rsi, ema9_high, ema9_low, fwd_3c
    FROM option_3min
    WHERE time(timestamp) >= '09:45:00' AND time(timestamp) < '15:00:00'
    ORDER BY strike, type, timestamp
""", con, parse_dates=['timestamp'])
con.close()

# ── Prepare ─────────────────────────────────────────────────────
df['fwd_3c'] = pd.to_numeric(df['fwd_3c'], errors='coerce')
df = df.sort_values(['strike','type','timestamp']).copy()
df['bw']          = df['ema9_high'] - df['ema9_low']
g                 = df.groupby(['strike','type'])
df['ema9l_slope'] = g['ema9_low'].transform(lambda x: x.diff())
df['rsi_prev']    = g['rsi'].transform(lambda x: x.shift(1))
v = df[df['fwd_3c'].notna() & (df['close']>0)].iloc[0]
if abs(float(v['fwd_3c'])) > float(v['close'])*0.5:
    df['ret_3c'] = df['fwd_3c'] - df['close']
    print("fwd is absolute — converted to return")
else:
    df['ret_3c'] = df['fwd_3c']
df.dropna(subset=['ret_3c','ema9l_slope','rsi','rsi_prev'], inplace=True)
df['rsi_rising'] = df['rsi'] > df['rsi_prev']
df['body']       = df['close'] - df['open']

DAYS = df['timestamp'].dt.date.nunique()

# ── Define tight signal (target) ─────────────────────────────────
base  = ((df['close']>df['open']) & (df['close']>df['ema9_low']) & (df['ema9l_slope']>=0))
tight = base & (df['bw']>=13) & (df['bw']<=17) & (df['rsi']>50) & (df['rsi']<65) & df['rsi_rising']
df['is_tight'] = tight.astype(int)

# ── Previous candle features ──────────────────────────────────────
df['prev_bw']          = g['bw'].transform(lambda x: x.shift(1))
df['prev_rsi']         = g['rsi'].transform(lambda x: x.shift(1))
df['prev_rsi_prev']    = g['rsi'].transform(lambda x: x.shift(2))
df['prev_ema9l_slope'] = g['ema9_low'].transform(lambda x: x.diff().shift(1))
df['prev_close']       = g['close'].transform(lambda x: x.shift(1))
df['prev_open']        = g['open'].transform(lambda x: x.shift(1))
df['prev_ema9l']       = g['ema9_low'].transform(lambda x: x.shift(1))
df['prev_bw2']         = g['bw'].transform(lambda x: x.shift(2))  # 2 candles back
df['prev_rsi_rising']  = (df['prev_rsi'] > df['prev_rsi_prev']).astype(int)
df['prev_green']       = (df['prev_close'] > df['prev_open']).astype(int)
df['prev_above_ema9l'] = (df['prev_close'] > df['prev_ema9l']).astype(int)
df['prev_slope_ok']    = (df['prev_ema9l_slope'] >= 0).astype(int)
# Ret-open: if we enter at THIS candle's open and hold 9 min (3 candles at 3min)
# = forward return from open = ret_3c + body
df['ret_from_open']    = df['ret_3c'] + df['body']

df2 = df.dropna(subset=['prev_bw','prev_rsi','prev_ema9l_slope']).copy()
print(f"Candles for prediction analysis: {len(df2)} | Tight signals: {df2['is_tight'].sum()} | Days: {DAYS}")

BAR = '━'*84

# ════════════════════════════════════════════════════════════════
#  PART 1: PROBABILITY HEATMAP — prev_bw bin × prev_rsi bin
# ════════════════════════════════════════════════════════════════
print(f"\n{BAR}")
print(f"  PART 1 — P(tight) heatmap: previous candle BW × RSI")
print(f"  (read: if prev candle had BW X and RSI Y, what % next candle is tight?)")
print(BAR)

bw_bins  = [(0,9),(9,11),(11,13),(13,15),(15,17),(17,19),(19,99)]
rsi_bins = [(30,45),(45,50),(50,55),(55,60),(60,65),(65,75),(75,99)]

bw_labels  = ['BW 0-9','9-11','11-13','13-15','15-17','17-19','19+']
rsi_labels = ['RSI<45','45-50','50-55','55-60','60-65','65-75','75+']

# Print header
print(f"\n  {'prev_BW':12}", end='')
for rl in rsi_labels:
    print(f" {rl:>9}", end='')
print()
print(f"  {'─'*82}")

for (blo,bhi), bl in zip(bw_bins, bw_labels):
    print(f"  {bl:12}", end='')
    for (rlo,rhi), _ in zip(rsi_bins, rsi_labels):
        sub = df2[(df2['prev_bw']>=blo) & (df2['prev_bw']<bhi) &
                  (df2['prev_rsi']>rlo) & (df2['prev_rsi']<=rhi)]
        if len(sub) < 5:
            print(f" {'  —':>9}", end='')
        else:
            p = sub['is_tight'].mean()*100
            print(f" {p:8.1f}%", end='')
    print()

# ════════════════════════════════════════════════════════════════
#  PART 2: MULTI-FEATURE SCORE
#  Score 0-8: each binary feature adds 1 point
#  Higher score = more likely to be a tight signal
# ════════════════════════════════════════════════════════════════
print(f"\n{BAR}")
print(f"  PART 2 — MULTI-FEATURE SCORE → P(tight) and open-entry return")
print(BAR)

# Features (each = 1 point):
# 1. prev_bw in 11-19 (in the band-width momentum zone)
# 2. prev_bw in 13-17 (exactly in tight zone — extra precision)
# 3. prev_rsi in 48-67 (RSI in momentum zone)
# 4. prev_rsi in 52-63 (RSI in tight zone)
# 5. prev_rsi_rising
# 6. prev_green (prev candle green)
# 7. prev_above_ema9l (prev close above EMA9_low)
# 8. prev_slope_ok (prev EMA9_low slope >= 0)

df2 = df2.copy()
df2['f1'] = ((df2['prev_bw']>=11) & (df2['prev_bw']<=19)).astype(int)
df2['f2'] = ((df2['prev_bw']>=13) & (df2['prev_bw']<=17)).astype(int)
df2['f3'] = ((df2['prev_rsi']>48) & (df2['prev_rsi']<67)).astype(int)
df2['f4'] = ((df2['prev_rsi']>52) & (df2['prev_rsi']<63)).astype(int)
df2['f5'] = df2['prev_rsi_rising']
df2['f6'] = df2['prev_green']
df2['f7'] = df2['prev_above_ema9l']
df2['f8'] = df2['prev_slope_ok']
df2['score'] = df2[['f1','f2','f3','f4','f5','f6','f7','f8']].sum(axis=1)

print(f"\n  {'Score':>6} {'n_all':>7} {'n_tight':>8} {'P(tight)':>9} "
      f"{'avg open-ret':>13} {'win%':>7} {'ESL%':>7} {'avg close-ret':>14}")
print(f"  {'─'*82}")

for sc in sorted(df2['score'].unique()):
    grp = df2[df2['score']==sc]
    tight_n = grp['is_tight'].sum()
    p_tight = grp['is_tight'].mean()*100
    r_open = grp['ret_from_open']
    r_close = grp['ret_3c']
    if len(grp) < 5:
        print(f"  {sc:6d} {len(grp):7d} {tight_n:8d} {p_tight:8.1f}%   (too few)")
        continue
    print(f"  {sc:6d} {len(grp):7d} {tight_n:8d} {p_tight:8.1f}%   "
          f"{r_open.mean():+12.1f}  {(r_open>0).mean()*100:6.1f}%  "
          f"{(r_open<-12).mean()*100:6.1f}%  {r_close.mean():+13.1f}")

# ════════════════════════════════════════════════════════════════
#  PART 3: THRESHOLD SWEEP — enter at open if score >= threshold
#  vs current strategy (wait for tight signal, enter at close)
# ════════════════════════════════════════════════════════════════
print(f"\n{BAR}")
print(f"  PART 3 — THRESHOLD SWEEP (enter at candle OPEN if score >= threshold)")
print(BAR)
print(f"\n  {'Threshold':>10} {'n_entered':>10} {'precision':>10} "
      f"{'avg open-ret':>13} {'win%':>7} {'ESL%':>7} {'score_metric':>13}")
print(f"  {'─'*82}")

for thresh in range(0, 9):
    entered = df2[df2['score'] >= thresh]
    if len(entered) < 5:
        continue
    tight_in = entered['is_tight'].sum()
    precision = tight_in / len(entered) * 100
    r = entered['ret_from_open']
    sm = r.mean() - ((r<-12).mean()*100 - 40)*0.5
    print(f"  {thresh:>10} {len(entered):>10} {precision:9.1f}%   "
          f"{r.mean():+12.1f}  {(r>0).mean()*100:6.1f}%  "
          f"{(r<-12).mean()*100:6.1f}%  {sm:+12.1f}")

# Baseline for comparison
base_tight = df2[df2['is_tight']==1]
print(f"\n  {'BASELINE B (enter at CLOSE, tight only)':>50} "
      f"n={len(base_tight)} avg_close={base_tight['ret_3c'].mean():+.1f} "
      f"avg_open={base_tight['ret_from_open'].mean():+.1f}")

# ════════════════════════════════════════════════════════════════
#  PART 4: BEST THRESHOLD — detailed stats + day-by-day
# ════════════════════════════════════════════════════════════════
print(f"\n{BAR}")
print(f"  PART 4 — BEST THRESHOLD detailed: score >= 6 and score >= 7")
print(BAR)

for thresh in [6, 7, 8]:
    entered = df2[df2['score'] >= thresh].copy()
    if len(entered) < 5:
        continue
    tight_in  = entered['is_tight'].sum()
    missed    = len(base_tight) - (entered['is_tight']==1).sum()
    wasted    = len(entered) - tight_in

    print(f"\n  ── Score >= {thresh}: n_entered={len(entered)}, "
          f"precision={tight_in/len(entered)*100:.1f}%, "
          f"recall={tight_in/len(base_tight)*100:.1f}%")
    print(f"     Tight caught: {tight_in}/{len(base_tight)} | "
          f"Wasted entries (not tight): {wasted}")

    r_open  = entered['ret_from_open']
    r_close = entered[entered['is_tight']==1]['ret_3c']
    print(f"     Open-entry (all entered):   avg={r_open.mean():+.1f}  "
          f"win={( r_open>0).mean()*100:.1f}%  ESL={( r_open<-12).mean()*100:.1f}%")
    if len(r_close) >= 5:
        print(f"     Close-entry (tight only):   avg={r_close.mean():+.1f}  "
              f"win={(r_close>0).mean()*100:.1f}%  ESL={(r_close<-12).mean()*100:.1f}%")

    # Day-by-day
    print(f"\n     {'Date':12} {'B close n':>10} {'B close avg':>12} "
          f"{'open-entry n':>13} {'open-entry avg':>15}")
    all_dates = sorted(entered['timestamp'].dt.date.unique())
    for d in all_dates:
        bc = base_tight[base_tight['timestamp'].dt.date==d]
        oe = entered[entered['timestamp'].dt.date==d]
        bc_avg = bc['ret_3c'].mean()         if len(bc)>0 else float('nan')
        oe_avg = oe['ret_from_open'].mean()  if len(oe)>0 else float('nan')
        print(f"     {str(d):12} {len(bc):10d} {bc_avg:+11.1f}  "
              f"{len(oe):13d} {oe_avg:+14.1f}")

# ════════════════════════════════════════════════════════════════
#  PART 5: FEATURE IMPORTANCE — which features predict tight best?
# ════════════════════════════════════════════════════════════════
print(f"\n{BAR}")
print(f"  PART 5 — FEATURE IMPORTANCE: P(tight) given each feature alone")
print(BAR)

features = {
    'prev_bw in 11-19':   df2['f1']==1,
    'prev_bw in 13-17':   df2['f2']==1,
    'prev_rsi in 48-67':  df2['f3']==1,
    'prev_rsi in 52-63':  df2['f4']==1,
    'prev_rsi rising':    df2['f5']==1,
    'prev candle green':  df2['f6']==1,
    'prev close>ema9l':   df2['f7']==1,
    'prev slope>=0':      df2['f8']==1,
}
base_p = df2['is_tight'].mean()*100
print(f"\n  Base P(tight) = {base_p:.2f}%\n")
print(f"  {'Feature':28} {'P(tight|feat)':>14} {'lift':>8} {'n':>7}")
print(f"  {'─'*60}")
for fname, fmask in features.items():
    sub = df2[fmask]
    if len(sub) < 10: continue
    p = sub['is_tight'].mean()*100
    lift = p / base_p
    print(f"  {fname:28} {p:13.2f}%  {lift:7.2f}x  {len(sub):6d}")

# Top combo: all 8 features
combo = df2[df2['f1']==1][df2['f2']==1][df2['f3']==1][df2['f5']==1][df2['f6']==1][df2['f7']==1][df2['f8']==1]
print(f"\n  Best combo (f1+f2+f3+f5+f6+f7+f8): "
      f"P(tight)={combo['is_tight'].mean()*100:.1f}% n={len(combo)}")
