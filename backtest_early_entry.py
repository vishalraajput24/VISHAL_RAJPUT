"""
Backtest: Early entry — three concrete approaches

The question: can we enter BEFORE the 3-min candle closes and get a cheaper,
profitable entry? Tests three angles:

  A. Candle body analysis
     Max possible early-entry benefit = avg body of tight signal candles
     (if you could enter at 3-min OPEN, how many extra pts would you gain?)

  B. First 1-min of new bucket after previous tight signal
     Previous 3-min closes as tight (BW 13-17, RSI 50-65)
     → enter on 1st green 1-min of the NEXT bucket (partial confirmation)
     Compare quality vs waiting for NEXT 3-min to close

  C. Consecutive tight signal entry at candle OPEN
     When tight signal at T3 → tight signal also at T3+3min (back-to-back)
     → enter at OPEN price of T3+3min candle vs entering at its CLOSE
     Shows: do consecutive signals give sustained momentum worth entering early?

Run: python3 ~/VISHAL_RAJPUT/backtest_early_entry.py
"""
import sqlite3, os, sys
import pandas as pd
import numpy as np

DB = os.path.expanduser("~/lab_data/vrl_data.db")
if not os.path.exists(DB):
    sys.exit(f"DB not found: {DB}")

con = sqlite3.connect(DB)
print("Loading 3-min...", flush=True)
df3 = pd.read_sql("""
    SELECT timestamp, strike, type, open, high, low, close,
           rsi, ema9_high, ema9_low, fwd_3c
    FROM option_3min
    WHERE time(timestamp) >= '09:45:00' AND time(timestamp) < '15:00:00'
    ORDER BY strike, type, timestamp
""", con, parse_dates=['timestamp'])

print("Loading 1-min...", flush=True)
df1 = pd.read_sql("""
    SELECT timestamp, strike, type, open, high, low, close, rsi
    FROM option_1min
    WHERE time(timestamp) >= '09:45:00' AND time(timestamp) < '15:10:00'
    ORDER BY strike, type, timestamp
""", con, parse_dates=['timestamp'])
con.close()

# ── Prepare 3-min ─────────────────────────────────────────────────
df3['fwd_3c'] = pd.to_numeric(df3['fwd_3c'], errors='coerce')
df3 = df3.sort_values(['strike','type','timestamp']).copy()
df3['bw']          = df3['ema9_high'] - df3['ema9_low']
g3                 = df3.groupby(['strike','type'])
df3['ema9l_slope'] = g3['ema9_low'].transform(lambda x: x.diff())
df3['rsi_prev']    = g3['rsi'].transform(lambda x: x.shift(1))
# Shift open/close of NEXT candle (needed for consecutive analysis)
df3['next_open']   = g3['open'].transform(lambda x: x.shift(-1))
df3['next_close']  = g3['close'].transform(lambda x: x.shift(-1))
df3['next_ts']     = g3['timestamp'].transform(lambda x: x.shift(-1))
v = df3[df3['fwd_3c'].notna() & (df3['close']>0)].iloc[0]
if abs(float(v['fwd_3c'])) > float(v['close'])*0.5:
    df3['ret_3c'] = df3['fwd_3c'] - df3['close']
    print("3-min fwd is absolute — converted to return")
else:
    df3['ret_3c'] = df3['fwd_3c']
df3.dropna(subset=['ret_3c','ema9l_slope','rsi','rsi_prev'], inplace=True)
df3['rsi_rising'] = df3['rsi'] > df3['rsi_prev']
df3['body']        = df3['close'] - df3['open']
DAYS = df3['timestamp'].dt.date.nunique()

# ── Prepare 1-min ─────────────────────────────────────────────────
df1 = df1.sort_values(['strike','type','timestamp']).copy()
g1  = df1.groupby(['strike','type'])
df1['ema9_high']   = g1['high'].transform(lambda x: x.ewm(span=9,adjust=False).mean())
df1['ema9_low']    = g1['low'].transform(lambda x:  x.ewm(span=9,adjust=False).mean())
df1['ema9l_slope'] = g1['ema9_low'].transform(lambda x: x.diff())
df1['rsi_prev']    = g1['rsi'].transform(lambda x: x.shift(1))
df1['ret_3c']      = g1['close'].transform(lambda x: x.shift(-3) - x)
df1['ret_9c']      = g1['close'].transform(lambda x: x.shift(-9) - x)
for n, col in [(3,'ret_3c'),(9,'ret_9c')]:
    ts_n = g1['timestamp'].transform(lambda x: x.shift(-n))
    df1.loc[ts_n.isna()|(ts_n.dt.date!=df1['timestamp'].dt.date), col] = np.nan
df1.dropna(subset=['ema9l_slope','rsi','rsi_prev'], inplace=True)
df1['rsi_rising'] = df1['rsi'] > df1['rsi_prev']
df1['bucket_ts']  = df1['timestamp'].apply(
    lambda t: t.replace(minute=(t.minute//3)*3, second=0, microsecond=0))

# ── Gates ─────────────────────────────────────────────────────────
base3 = ((df3['close']>df3['open']) & (df3['close']>df3['ema9_low']) & (df3['ema9l_slope']>=0))
base1 = ((df1['close']>df1['open']) & (df1['close']>df1['ema9_low']) & (df1['ema9l_slope']>=0))

mask3_cur   = base3 & (df3['bw']>=11) & (df3['rsi']>45) & (df3['rsi']<75) & df3['rsi_rising']
mask3_tight = base3 & (df3['bw']>=13) & (df3['bw']<=17) & (df3['rsi']>50) & (df3['rsi']<65) & df3['rsi_rising']

df3_cur   = df3[mask3_cur].copy()
df3_tight = df3[mask3_tight].copy()

def sts(s):
    s = s.dropna()
    if len(s)<5: return None
    return dict(n=len(s), avg=s.mean(), win=(s>0).mean()*100,
                esl=(s<-12).mean()*100, big=(s>20).mean()*100,
                score=s.mean()-((s<-12).mean()*100-40)*0.5)

def prow(lbl, d, col='ret_3c', w=48):
    r = sts(d[col])
    if r: print(f"  {lbl:{w}} {r['n']:5d} {r['avg']:+7.1f} {r['win']:7.1f} {r['esl']:7.1f} {r['big']:7.1f} {r['score']:+7.1f}")
    else: print(f"  {lbl:{w}} (n={len(d)} — too few)")

BAR = '━'*88
HDR = f"  {'':48} {'n':>5} {'avg':>7} {'win%':>7} {'ESL%':>7} {'big%':>7} {'score':>7}"

# ════════════════════════════════════════════════════════════════
#  ANALYSIS A: CANDLE BODY — theoretical max early-entry benefit
# ════════════════════════════════════════════════════════════════
print(f"\n{BAR}")
print(f"  ANALYSIS A — CANDLE BODY  (max possible savings if enter at 3-min OPEN)")
print(BAR)

print(f"\n  CURRENT signals (BW>=11, RSI 45-75), n={len(df3_cur)}")
print(f"    avg body (open→close):  {df3_cur['body'].mean():+.2f} pts")
print(f"    median body:            {df3_cur['body'].median():+.2f} pts")
print(f"    % candles body > 3 pts: {(df3_cur['body']>3).mean()*100:.1f}%")
print(f"    avg ret_3c:             {df3_cur['ret_3c'].mean():+.2f}")
print(f"    theoretical open-entry: {df3_cur['ret_3c'].mean() + df3_cur['body'].mean():+.2f}  (+body savings)")

print(f"\n  TIGHT signals (BW 13-17, RSI 50-65), n={len(df3_tight)}")
print(f"    avg body (open→close):  {df3_tight['body'].mean():+.2f} pts")
print(f"    median body:            {df3_tight['body'].median():+.2f} pts")
print(f"    % candles body > 3 pts: {(df3_tight['body']>3).mean()*100:.1f}%")
print(f"    avg ret_3c:             {df3_tight['ret_3c'].mean():+.2f}")
print(f"    theoretical open-entry: {df3_tight['ret_3c'].mean() + df3_tight['body'].mean():+.2f}  (+body savings)")

# body quartiles
print(f"\n  Tight signal candle body percentiles:")
for p in [10,25,50,75,90]:
    print(f"    p{p:2d}: {df3_tight['body'].quantile(p/100):+.1f} pts")

# ════════════════════════════════════════════════════════════════
#  ANALYSIS B: FIRST 1-MIN OF NEXT BUCKET AFTER TIGHT SIGNAL
#  Previous 3-min tight → enter on 1st qualifying 1-min of next bucket
# ════════════════════════════════════════════════════════════════
print(f"\n{BAR}")
print(f"  ANALYSIS B — FIRST 1-MIN of next bucket after previous tight signal")
print(f"  (enter 0-2 min after tight signal confirms, no waiting for next 3-min close)")
print(BAR)
print(HDR)
print(f"  {'─'*86}")

# Next bucket = timestamp + 3min
df3_tight_nb = df3_tight.copy()
df3_tight_nb['next_bkt'] = df3_tight_nb['timestamp'] + pd.Timedelta(minutes=3)

def first_1m_in_bucket(mask_1m):
    return (df1[mask_1m].sort_values('timestamp')
            .groupby(['strike','type','bucket_ts']).first().reset_index()
            [['strike','type','bucket_ts','close','ret_3c','ret_9c','timestamp']]
            .rename(columns={'close':'c1m','ret_3c':'r3c_1m',
                              'ret_9c':'r9c_1m','timestamp':'ts_1m'}))

# 1-min filter configs for next-bucket entry
configs_1m = [
    ("1m: any green+G2+G2B (no RSI)",         base1),
    ("1m: green+G2+G2B + RSI 45-75 rising",   base1 & (df1['rsi']>45) & (df1['rsi']<75) & df1['rsi_rising']),
    ("1m: green+G2+G2B + RSI 50-65 rising",   base1 & (df1['rsi']>50) & (df1['rsi']<65) & df1['rsi_rising']),
    ("1m: green+G2+G2B + RSI 50-70 rising",   base1 & (df1['rsi']>50) & (df1['rsi']<70) & df1['rsi_rising']),
]

for name, m1 in configs_1m:
    bkts   = first_1m_in_bucket(m1)
    merged = df3_tight_nb.merge(bkts, left_on=['strike','type','next_bkt'],
                                 right_on=['strike','type','bucket_ts'], how='left')
    hit = merged.dropna(subset=['r3c_1m']).copy()
    hit['saving_vs_3m'] = hit['next_close'] - hit['c1m']  # 3-min next close - 1m entry
    ret  = sts(hit['r3c_1m'])
    ret9 = sts(hit['r9c_1m'])
    if not ret: print(f"  {name:48} too few"); continue
    sav = hit['saving_vs_3m'].dropna().mean()
    sav_pct = (hit['saving_vs_3m']>0).mean()*100
    r9avg = f"{ret9['avg']:+.1f}" if ret9 else "n/a"
    retain = len(hit)/len(df3_tight)*100
    print(f"  {name:48} {ret['n']:5d} {ret['avg']:+7.1f} {ret['win']:7.1f} "
          f"{ret['esl']:7.1f} {ret['big']:7.1f} {ret['score']:+7.1f}")
    print(f"  {'':48} retain={retain:.0f}%  sav={sav:+.1f}pts  cheap={sav_pct:.0f}%  9m={r9avg}")

# Compare: what does the NEXT 3-min candle give if it passes tight gates?
mask3_consec = mask3_tight.copy()
df3_tight_nb['next_tight'] = False
# Mark consecutive: check if next_ts also passes tight filter
consec_join = df3_tight[['strike','type','timestamp']].copy()
consec_join['is_tight'] = True
merged_c = df3_tight_nb.merge(
    consec_join.rename(columns={'timestamp':'next_bkt','is_tight':'nxt_is_tight'}),
    on=['strike','type','next_bkt'], how='left')
has_next_tight = merged_c['nxt_is_tight'].fillna(False)

# Forward return of the NEXT 3-min candle (ret_3c)
# Use shift(-1) next candle's return for this analysis via df3 join
df3_next = df3[['strike','type','timestamp','ret_3c','open','close']].copy()
df3_next = df3_next.rename(columns={'timestamp':'next_bkt','ret_3c':'next_ret3c',
                                     'open':'next_open2','close':'next_close2'})
df3_tight_nb2 = df3_tight_nb.merge(df3_next, on=['strike','type','next_bkt'], how='left')
df3_tight_nb2['next_passes_tight'] = has_next_tight.values[:len(df3_tight_nb2)]

print(f"\n  Reference: what if you just wait for the NEXT 3-min candle (regardless of gates)?")
next_ret = sts(df3_tight_nb2['next_ret3c'])
if next_ret:
    print(f"    All next candles: n={next_ret['n']} avg={next_ret['avg']:+.1f} "
          f"win={next_ret['win']:.1f}% ESL={next_ret['esl']:.1f}%")
tight_next = df3_tight_nb2[df3_tight_nb2['next_passes_tight']==True]
next_tight_ret = sts(tight_next['next_ret3c'])
if next_tight_ret:
    print(f"    Next candle also TIGHT: n={next_tight_ret['n']} "
          f"avg={next_tight_ret['avg']:+.1f} win={next_tight_ret['win']:.1f}% "
          f"ESL={next_tight_ret['esl']:.1f}% "
          f"({len(tight_next)/len(df3_tight)*100:.0f}% occur)")

# ════════════════════════════════════════════════════════════════
#  ANALYSIS C: CONSECUTIVE TIGHT SIGNALS — OPEN vs CLOSE ENTRY
# ════════════════════════════════════════════════════════════════
print(f"\n{BAR}")
print(f"  ANALYSIS C — CONSECUTIVE tight signals: enter at OPEN vs CLOSE of 2nd signal")
print(BAR)

# Build consecutive signal pairs: tight at T3 AND tight at T3+3min
df3_t_idx = df3_tight[['strike','type','timestamp']].copy()
df3_t_idx['is_tight'] = True
df3_consec = df3_tight.copy()
df3_consec['prev_ts'] = df3_consec['timestamp'] - pd.Timedelta(minutes=3)
df3_consec = df3_consec.merge(
    df3_t_idx.rename(columns={'timestamp':'prev_ts','is_tight':'prev_tight'}),
    on=['strike','type','prev_ts'], how='left')
df3_consec['prev_tight'] = df3_consec['prev_tight'].fillna(False)

isolated  = df3_consec[~df3_consec['prev_tight']]  # no tight candle before
consecuti = df3_consec[df3_consec['prev_tight']]    # previous candle was also tight

print(f"\n  Tight signals breakdown:")
print(f"    Total tight:      {len(df3_tight):4d}")
print(f"    Isolated (no prev tight):   {len(isolated):4d}  ({len(isolated)/len(df3_tight)*100:.0f}%)")
print(f"    Consecutive (prev was tight): {len(consecuti):4d}  ({len(consecuti)/len(df3_tight)*100:.0f}%)")

print(f"\n  Quality by type:")
print(HDR)
print(f"  {'─'*86}")
prow("All tight signals",           df3_tight)
prow("Isolated tight (1st in run)", isolated)
prow("Consecutive tight (2nd+ in run)", consecuti)

# For consecutive signals: open-entry vs close-entry
if len(consecuti) >= 5:
    # "open entry" return = ret_3c + body  (body = close - open, so if enter at open not close)
    consecuti = consecuti.copy()
    consecuti['ret_from_open'] = consecuti['ret_3c'] + consecuti['body']
    r_close = sts(consecuti['ret_3c'])
    r_open  = sts(consecuti['ret_from_open'])
    print(f"\n  Consecutive signals — OPEN vs CLOSE entry:")
    print(f"    Enter at CLOSE (current V8):  avg {r_close['avg']:+.1f}  win {r_close['win']:.1f}%  ESL {r_close['esl']:.1f}%")
    print(f"    Enter at OPEN  (early entry): avg {r_open['avg']:+.1f}   win {r_open['win']:.1f}%  ESL {r_open['esl']:.1f}%")
    print(f"    Avg body saved: {consecuti['body'].mean():+.1f} pts  "
          f"(body>0 in {(consecuti['body']>0).mean()*100:.0f}% cases)")
    # body distribution for consecutive
    print(f"    Body percentiles (p25/p50/p75): "
          f"{consecuti['body'].quantile(.25):+.1f} / "
          f"{consecuti['body'].quantile(.5):+.1f} / "
          f"{consecuti['body'].quantile(.75):+.1f}")

if len(isolated) >= 5:
    isolated = isolated.copy()
    isolated['ret_from_open'] = isolated['ret_3c'] + isolated['body']
    r_close = sts(isolated['ret_3c'])
    r_open  = sts(isolated['ret_from_open'])
    print(f"\n  Isolated signals — OPEN vs CLOSE entry:")
    print(f"    Enter at CLOSE (current V8):  avg {r_close['avg']:+.1f}  win {r_close['win']:.1f}%  ESL {r_close['esl']:.1f}%")
    print(f"    Enter at OPEN  (early entry): avg {r_open['avg']:+.1f}   win {r_open['win']:.1f}%  ESL {r_open['esl']:.1f}%")

# ════════════════════════════════════════════════════════════════
#  ANALYSIS D: 1-min ENTRY WITHIN CONSECUTIVE SIGNAL BUCKET
#  After a tight signal, enter on 1st qualifying 1-min of the NEXT bucket
#  BUT only when the next 3-min ALSO turned out to be a tight signal
#  (shows the realistic ceiling for next-bucket 1-min entry)
# ════════════════════════════════════════════════════════════════
if len(consecuti) >= 5:
    print(f"\n{BAR}")
    print(f"  ANALYSIS D — 1-min entry within a KNOWN consecutive tight signal")
    print(f"  (upper bound: assuming we know next 3-min will also be tight)")
    print(BAR)
    print(HDR)
    print(f"  {'─'*86}")

    # consecuti = tight candles that were preceded by a tight candle
    # These are exactly the "second" candles in consecutive pairs
    # Compare: entering at 1st 1-min of this same bucket vs entering at 3-min close
    # The 1-min candles in the SAME bucket as a consecutive tight signal
    consec_bkts = consecuti[['strike','type','timestamp']].copy()
    consec_bkts = consec_bkts.rename(columns={'timestamp':'bucket_ts'})

    for name, m1 in configs_1m:
        bkts   = first_1m_in_bucket(m1)
        merged = consecuti.merge(bkts, left_on=['strike','type','timestamp'],
                                  right_on=['strike','type','bucket_ts'], how='left')
        hit = merged.dropna(subset=['r3c_1m']).copy()
        if len(hit) < 5:
            print(f"  {name:48} too few ({len(hit)})")
            continue
        hit['saving'] = hit['close'] - hit['c1m']
        ret  = sts(hit['r3c_1m'])
        ret9 = sts(hit['r9c_1m'])
        sav  = hit['saving'].dropna().mean()
        sav_pct = (hit['saving']>0).mean()*100
        r9avg = f"{ret9['avg']:+.1f}" if ret9 else "n/a"
        retain = len(hit)/len(consecuti)*100
        print(f"  {name:48} {ret['n']:5d} {ret['avg']:+7.1f} {ret['win']:7.1f} "
              f"{ret['esl']:7.1f} {ret['big']:7.1f} {ret['score']:+7.1f}")
        print(f"  {'':48} retain={retain:.0f}%  sav={sav:+.1f}pts  cheap={sav_pct:.0f}%  9m={r9avg}")

    # Baseline: consecutive tight entered at 3m close
    print(f"\n  Baseline — consecutive tight entered at 3-min CLOSE:")
    prow("Consecutive tight (3m close entry)", consecuti, w=48)

# ════════════════════════════════════════════════════════════════
#  SUMMARY
# ════════════════════════════════════════════════════════════════
print(f"\n{BAR}")
print(f"  FINAL SUMMARY — all strategies ({DAYS} days)")
print(BAR)
print(HDR)
print(f"  {'─'*86}")
prow("A. CURRENT  (BW>=11, RSI 45-75)",                df3_cur, w=48)
prow("B. TIGHT    (BW 13-17, RSI 50-65)",              df3_tight, w=48)
if len(isolated)  >= 5: prow("B1. TIGHT isolated (1st in run)",    isolated, w=48)
if len(consecuti) >= 5: prow("B2. TIGHT consecutive (2nd+ in run)",consecuti, w=48)

print(f"\n  Key insight — max open-entry gains:")
print(f"    Tight all:         enter at open = +{df3_tight['ret_3c'].mean()+df3_tight['body'].mean():.1f} "
      f"(vs close +{df3_tight['ret_3c'].mean():.1f}, body={df3_tight['body'].mean():.1f})")
if len(consecuti) >= 5:
    print(f"    Tight consecutive: enter at open = "
          f"+{consecuti['ret_3c'].mean()+consecuti['body'].mean():.1f} "
          f"(vs close +{consecuti['ret_3c'].mean():.1f})")
