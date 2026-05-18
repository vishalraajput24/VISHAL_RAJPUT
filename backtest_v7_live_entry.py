"""
V7 Live-Candle Entry Backtest
Instead of waiting for the 15-min candle to CLOSE above EMA9_low,
enter the moment price breaks above EMA9_low during the live candle.

Method:
  1. Build 15-min candles (resample from 3-min) to get EMA9_low per bucket
  2. For each 15-min bucket, scan 1-min candles inside it
  3. Find FIRST 1-min candle where close > prev_15min_EMA9_low
  4. RSI condition (1-min RSI) must also be satisfied
  5. Entry at that 1-min close; forward return = next 45 min

Compare:
  A) V7 close-entry (current): enter at 15-min candle close
  B) Live-entry: enter at first 1-min breakout above EMA9_low
  C) Live-entry + RSI 40+
  D) Live-entry + RSI 50+

Run: bash ~/VISHAL_RAJPUT/run_backtest.sh v7_live_entry
"""
import sqlite3, os, sys
import pandas as pd
import numpy as np

DB = os.path.expanduser("~/lab_data/vrl_data.db")
if not os.path.exists(DB):
    sys.exit(f"DB not found: {DB}")

con = sqlite3.connect(DB)
print("Loading data...", flush=True)

# ── Load 3-min data → resample to 15-min for EMA9_low ────────────
df3 = pd.read_sql("""
    SELECT timestamp, strike, type, open, high, low, close,
           rsi, ema9_high, ema9_low
    FROM option_3min
    WHERE time(timestamp) >= '09:15:00' AND time(timestamp) < '15:30:00'
    ORDER BY strike, type, timestamp
""", con, parse_dates=['timestamp'])

# ── Load 1-min data for intra-candle entry ────────────────────────
df1 = pd.read_sql("""
    SELECT timestamp, strike, type, open, high, low, close, rsi, ema9_low
    FROM option_1min
    WHERE time(timestamp) >= '09:15:00' AND time(timestamp) < '15:30:00'
    ORDER BY strike, type, timestamp
""", con, parse_dates=['timestamp'])
con.close()

print(f"3-min rows: {len(df3)} | 1-min rows: {len(df1)}", flush=True)

# ── Build 15-min EMA9_low from 3-min ─────────────────────────────
df3 = df3.set_index('timestamp')
groups_15 = []
for (strike, typ), grp in df3.groupby(['strike', 'type']):
    r = grp.resample('15min', closed='left', label='left')
    g15 = pd.DataFrame({
        'open':     r['open'].first(),
        'high':     r['high'].max(),
        'low':      r['low'].min(),
        'close':    r['close'].last(),
        'rsi':      r['rsi'].last(),
        'ema9_low': r['ema9_low'].last(),
    })
    g15['strike'] = strike
    g15['type']   = typ
    groups_15.append(g15.reset_index().rename(columns={'timestamp':'bucket_ts'}))

df15 = pd.concat(groups_15, ignore_index=True)
df15 = df15[df15['bucket_ts'].dt.time >= pd.Timestamp('09:30').time()]
df15 = df15[df15['bucket_ts'].dt.time <  pd.Timestamp('15:00').time()]
df15 = df15.sort_values(['strike','type','bucket_ts'])

# Previous 15-min EMA9_low (what we know at start of live candle)
g15g = df15.groupby(['strike','type'])
df15['prev_ema9l'] = g15g['ema9_low'].transform(lambda x: x.shift(1))
df15['prev_rsi']   = g15g['rsi'].transform(lambda x: x.shift(1))

# 15-min forward return (close 3 buckets later = 45 min)
df15['fwd_45'] = g15g['close'].transform(lambda x: x.shift(-3))
df15 = df15.dropna(subset=['prev_ema9l', 'fwd_45'])
df15['ret_close'] = df15['fwd_45'] - df15['close']  # close-entry return

DAYS = df15['bucket_ts'].dt.date.nunique()
BAR  = '━' * 72

print(f"15-min buckets: {len(df15)} | Days: {DAYS}\n")

# ── V7 Close-Entry Baseline ────────────────────────────────────────
v7_curr = df15[
    (df15['close'] > df15['prev_ema9l']) &
    (df15['rsi'] >= 40)
]
print(f"V7 close-entry baseline: n={len(v7_curr)} avg={v7_curr['ret_close'].mean():+.1f} "
      f"win={(v7_curr['ret_close']>0).mean()*100:.1f}% "
      f"ESL={(v7_curr['ret_close']<-12).mean()*100:.1f}%\n")

# ── 1-min Live Entry ──────────────────────────────────────────────
# For each 15-min bucket, find first 1-min candle where close > prev_ema9l
df1 = df1.sort_values(['strike','type','timestamp'])
df1['fwd_45_1m'] = df1.groupby(['strike','type'])['close'].transform(
    lambda x: x.shift(-45))  # 45 one-minute bars forward

results = []
total_buckets = len(df15)
for idx, (_, row) in enumerate(df15.iterrows()):
    if idx % 500 == 0:
        print(f"  Processing {idx}/{total_buckets}...", flush=True)

    bucket_start = row['bucket_ts']
    bucket_end   = bucket_start + pd.Timedelta(minutes=15)
    strike       = row['strike']
    typ          = row['type']
    prev_ema9l   = row['prev_ema9l']

    # 1-min candles inside this bucket
    mask = (
        (df1['strike'] == strike) &
        (df1['type']   == typ) &
        (df1['timestamp'] >= bucket_start) &
        (df1['timestamp'] <  bucket_end)
    )
    intra = df1[mask].copy()
    if intra.empty:
        continue

    # Forward return from 15-min close (for comparison)
    fwd_45 = row['fwd_45']
    close_15 = row['close']

    # Find first 1-min candle where close > prev EMA9_low
    breakout = intra[intra['close'] > prev_ema9l]
    if breakout.empty:
        entry_price = None
        entry_rsi   = None
        fwd_from_entry = None
    else:
        first = breakout.iloc[0]
        entry_price    = first['close']
        entry_rsi      = first.get('rsi', float('nan'))
        fwd_from_entry = first['fwd_45_1m']
        if pd.isna(fwd_from_entry):
            fwd_from_entry = None

    results.append({
        'date':          bucket_start.date(),
        'bucket_ts':     bucket_start,
        'strike':        strike,
        'type':          typ,
        'prev_ema9l':    prev_ema9l,
        'close_15':      close_15,
        'ret_close':     row['ret_close'],       # V7 close-entry return
        'entry_live':    entry_price,            # live-entry price
        'entry_rsi':     entry_rsi,
        'fwd_live':      fwd_from_entry,
        'had_breakout':  entry_price is not None,
    })

df_res = pd.DataFrame(results)
df_res['ret_live'] = df_res.apply(
    lambda r: r['fwd_live'] - r['entry_live']
    if pd.notna(r['fwd_live']) and pd.notna(r['entry_live']) else float('nan'),
    axis=1
)
df_res['savings'] = df_res['entry_live'] - df_res['close_15']  # negative = cheaper entry

# ── PART 1: Summary ───────────────────────────────────────────────
print(f"\n{BAR}")
print("  PART 1 — CLOSE-ENTRY vs LIVE-ENTRY COMPARISON")
print(BAR)

# Only buckets where 15-min close-entry would have fired (V7 current gates)
v7_fired = df_res[
    (df_res['close_15'] > df_res['prev_ema9l']) &
    df_res['entry_rsi'].notna()
]
v7_live  = v7_fired[v7_fired['had_breakout'] & df_res['ret_live'].notna()]

print(f"\n  V7 current gate fired buckets: {len(v7_fired)}")
print(f"  Of those, had 1-min breakout:  {v7_fired['had_breakout'].sum()}")

print(f"\n  {'Strategy':35} {'n':>6} {'avg':>8} {'win%':>7} {'ESL%':>7} {'avg_saving':>11}")
print(f"  {'─'*72}")

# A: V7 close-entry
r = v7_fired['ret_close'].dropna()
print(f"  {'A) V7 close (current)':35} {len(r):6d} {r.mean():+7.1f}  "
      f"{(r>0).mean()*100:6.1f}%  {(r<-12).mean()*100:6.1f}%  {'—':>10}")

# B: Live entry (no RSI filter beyond breakout)
r = v7_live['ret_live'].dropna()
s = v7_live['savings'].dropna()
print(f"  {'B) Live breakout (any RSI)':35} {len(r):6d} {r.mean():+7.1f}  "
      f"{(r>0).mean()*100:6.1f}%  {(r<-12).mean()*100:6.1f}%  {s.mean():+10.1f}")

# C: Live entry + RSI >= 40
sub = v7_live[v7_live['entry_rsi'] >= 40]
r = sub['ret_live'].dropna()
s = sub['savings'].dropna()
if len(r) >= 5:
    print(f"  {'C) Live + RSI>=40':35} {len(r):6d} {r.mean():+7.1f}  "
          f"{(r>0).mean()*100:6.1f}%  {(r<-12).mean()*100:6.1f}%  {s.mean():+10.1f}")

# D: Live entry + RSI >= 50
sub = v7_live[v7_live['entry_rsi'] >= 50]
r = sub['ret_live'].dropna()
s = sub['savings'].dropna()
if len(r) >= 5:
    print(f"  {'D) Live + RSI>=50':35} {len(r):6d} {r.mean():+7.1f}  "
          f"{(r>0).mean()*100:6.1f}%  {(r<-12).mean()*100:6.1f}%  {s.mean():+10.1f}")

# E: Live entry + RSI 50-65
sub = v7_live[(v7_live['entry_rsi'] > 50) & (v7_live['entry_rsi'] < 65)]
r = sub['ret_live'].dropna()
s = sub['savings'].dropna()
if len(r) >= 5:
    print(f"  {'E) Live + RSI 50-65':35} {len(r):6d} {r.mean():+7.1f}  "
          f"{(r>0).mean()*100:6.1f}%  {(r<-12).mean()*100:6.1f}%  {s.mean():+10.1f}")

# ── PART 2: Breakout savings distribution ─────────────────────────
print(f"\n{BAR}")
print("  PART 2 — ENTRY SAVINGS (live vs close, negative = cheaper)")
print(BAR)
s = v7_live['savings']
print(f"\n  avg saving:    {s.mean():+.1f} pts")
print(f"  median saving: {s.median():+.1f} pts")
print(f"  % cheaper:     {(s<0).mean()*100:.1f}%")
print(f"  % same/worse:  {(s>=0).mean()*100:.1f}%")

# ── PART 3: Buckets where 15-min did NOT fire but live did ────────
print(f"\n{BAR}")
print("  PART 3 — EARLY SIGNALS (live fires, 15-min close doesn't)")
print(BAR)
early = df_res[
    df_res['had_breakout'] &
    (df_res['close_15'] <= df_res['prev_ema9l']) &  # 15-min close DIDN'T break out
    df_res['ret_live'].notna()
]
r = early['ret_live'].dropna()
if len(r) >= 5:
    print(f"\n  Buckets where live fires but 15-min close doesn't: {len(early)}")
    print(f"  avg={r.mean():+.1f}  win={(r>0).mean()*100:.1f}%  ESL={(r<-12).mean()*100:.1f}%")
    print(f"  (These are signals the current V7 MISSES)")
else:
    print(f"\n  Too few early-only signals: n={len(r)}")

# ── PART 4: Day-by-day ────────────────────────────────────────────
print(f"\n{BAR}")
print("  PART 4 — DAY-BY-DAY (V7 close vs Live breakout + RSI>=40)")
print(BAR)
print(f"\n  {'Date':12} {'Close n':>8} {'Close avg':>10} {'Live n':>8} {'Live avg':>10}")
print(f"  {'─'*55}")

live_rsi40 = v7_live[v7_live['entry_rsi'] >= 40]
for d in sorted(df_res['date'].unique()):
    c = v7_fired[v7_fired['date']==d]['ret_close'].dropna()
    l = live_rsi40[live_rsi40['date']==d]['ret_live'].dropna()
    c_avg = c.mean() if len(c)>0 else float('nan')
    l_avg = l.mean() if len(l)>0 else float('nan')
    print(f"  {str(d):12} {len(c):8d} {c_avg:+9.1f}  {len(l):8d} {l_avg:+9.1f}")
