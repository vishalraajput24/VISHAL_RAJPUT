"""
Heikin Ashi EMA9_low Fresh Break Backtest
Computes HA candles from raw OHLC, then tests:
  - EMA9 of HA_low (smoother than raw low EMA)
  - Fresh break: prev_ha_close <= ema9_ha_low, curr_ha_close > ema9_ha_low
  - RSI 50-65 (raw RSI from DB)
  - Time window 09:45-13:45

Compares vs raw OHLC fresh break (baseline ₹+26,676).
Exit: ladder SL simulated on raw OHLC (real execution prices).
LOT = 65

Run: bash ~/VISHAL_RAJPUT/run_backtest.sh ha_ema9
"""
import sqlite3, os, sys
import pandas as pd
import numpy as np

DB = os.path.expanduser("~/lab_data/vrl_data.db")
if not os.path.exists(DB):
    sys.exit(f"DB not found: {DB}")

con = sqlite3.connect(DB)
print("Loading data...", flush=True)
df = pd.read_sql("""
    SELECT timestamp, strike, type, open, high, low, close, rsi, ema9_low
    FROM option_3min
    WHERE time(timestamp) >= '09:15:00' AND time(timestamp) < '15:30:00'
    ORDER BY strike, type, timestamp
""", con, parse_dates=['timestamp'])
con.close()

df = df.sort_values(['strike','type','timestamp']).reset_index(drop=True)
print(f"Rows: {len(df)}", flush=True)

# ── Compute Heikin Ashi candles per symbol ─────────────────────────
print("Computing Heikin Ashi candles...", flush=True)

def compute_ha(group):
    o = group['open'].values.astype(float)
    h = group['high'].values.astype(float)
    l = group['low'].values.astype(float)
    c = group['close'].values.astype(float)
    ha_c = (o + h + l + c) / 4.0
    ha_o = np.empty_like(ha_c)
    ha_o[0] = (o[0] + c[0]) / 2.0
    for i in range(1, len(ha_o)):
        ha_o[i] = (ha_o[i-1] + ha_c[i-1]) / 2.0
    ha_h = np.maximum(h, np.maximum(ha_o, ha_c))
    ha_l = np.minimum(l, np.minimum(ha_o, ha_c))
    return pd.DataFrame(
        {'ha_open': ha_o, 'ha_high': ha_h, 'ha_low': ha_l, 'ha_close': ha_c},
        index=group.index
    )

ha_parts = []
for _, grp in df.groupby(['strike','type'], sort=False):
    ha_parts.append(compute_ha(grp))

ha_df = pd.concat(ha_parts).sort_index()
df['ha_open']  = ha_df['ha_open']
df['ha_high']  = ha_df['ha_high']
df['ha_low']   = ha_df['ha_low']
df['ha_close'] = ha_df['ha_close']

# ── EMA9 of HA_low ─────────────────────────────────────────────────
print("Computing EMA9 of HA_low...", flush=True)
df['ema9_ha_low'] = df.groupby(['strike','type'])['ha_low'].transform(
    lambda x: x.ewm(span=9, adjust=False).mean()
)

g = df.groupby(['strike','type'])

df['ha_close_prev']    = g['ha_close'].transform(lambda x: x.shift(1))
df['ema9_ha_low_prev'] = g['ema9_ha_low'].transform(lambda x: x.shift(1))
df['close_prev']       = g['close'].transform(lambda x: x.shift(1))
df['ema9_low_prev']    = g['ema9_low'].transform(lambda x: x.shift(1))
df['rsi_prev']         = g['rsi'].transform(lambda x: x.shift(1))
df['fwd_3c']           = g['close'].transform(lambda x: x.shift(-3))
df['close_1c']         = g['close'].transform(lambda x: x.shift(-1))
df['close_2c']         = g['close'].transform(lambda x: x.shift(-2))
df['close_3c']         = g['close'].transform(lambda x: x.shift(-3))

df = df.dropna(subset=[
    'ha_close_prev','ema9_ha_low_prev',
    'close_prev','ema9_low_prev','fwd_3c','rsi_prev'
])
df['ret'] = df['fwd_3c'] - df['close']
df['hhmm'] = df['timestamp'].dt.hour * 60 + df['timestamp'].dt.minute

DAYS = df['timestamp'].dt.date.nunique()
LOT  = 65
BAR  = '━' * 82

print(f"Days: {DAYS} | Candles: {len(df)}\n")

# ── Signal masks ───────────────────────────────────────────────────
raw_fresh = (df['close'] > df['ema9_low'])      & (df['close_prev']    <= df['ema9_low_prev'])
ha_fresh  = (df['ha_close'] > df['ema9_ha_low'])& (df['ha_close_prev'] <= df['ema9_ha_low_prev'])
rsi_ok    = (df['rsi'] > 50) & (df['rsi'] < 65)
in_win    = (df['hhmm'] >= 9*60+45) & (df['hhmm'] < 13*60+45)

def fake_rate(sigs):
    fv = sigs.dropna(subset=['close_1c','close_2c','close_3c'])
    return ((fv['close_1c'] < fv['ema9_low']) |
            (fv['close_2c'] < fv['ema9_low']) |
            (fv['close_3c'] < fv['ema9_low'])).mean() * 100

# ── Ladder SL ─────────────────────────────────────────────────────
def ladder_sl(entry, peak):
    if peak >= 50: return entry + 50
    if peak >= 40: return entry + 36
    if peak >= 36: return entry + 30
    if peak >= 30: return entry + 20
    if peak >= 24: return entry + 12
    if peak >= 12: return entry + 4
    return entry - 12

def sim_ladder(entry, candles, max_c=30):
    peak = 0.0
    for o, h, l, c in candles[:max_c]:
        if h - entry > peak:
            peak = h - entry
        sl = ladder_sl(entry, peak)
        if l <= sl:
            return max(sl, l) - entry, peak
    if candles:
        return candles[min(len(candles), max_c)-1][3] - entry, peak
    return 0.0, 0.0

print("Building raw OHLC candle lookup...", flush=True)
sym_candles = {}
for (strike, typ), grp in df.groupby(['strike','type']):
    sym_candles[(strike,typ)] = grp[['timestamp','open','high','low','close']].reset_index(drop=True)

def run_ladder(signals):
    results = []
    for _, row in signals.iterrows():
        key   = (row['strike'], row['type'])
        ts    = row['timestamp']
        entry = row['close']
        if key not in sym_candles:
            continue
        sym    = sym_candles[key]
        future = sym[sym['timestamp'] > ts]
        clist  = list(future[['open','high','low','close']].itertuples(index=False, name=None))
        if not clist:
            continue
        ret, peak = sim_ladder(entry, clist)
        results.append({'date': ts.date(), 'ret': ret, 'peak': peak})
    if not results:
        return pd.DataFrame(columns=['date','ret','peak'])
    return pd.DataFrame(results)

# ── Run all signal sets ────────────────────────────────────────────
sets = [
    ("RAW no filter",      df[raw_fresh]),
    ("RAW + RSI 50-65",    df[raw_fresh & rsi_ok]),
    ("RAW + RSI + window", df[raw_fresh & rsi_ok & in_win]),
    ("HA  no filter",      df[ha_fresh]),
    ("HA  + RSI 50-65",    df[ha_fresh & rsi_ok]),
    ("HA  + RSI + window", df[ha_fresh & rsi_ok & in_win]),
]

print(f"\n{BAR}")
print("  Running ladder simulations on all 6 signal sets...")
print(BAR)

pnls = {}
for label, sigs in sets:
    if len(sigs) < 5:
        print(f"  {label}: <5 signals, skip")
        pnls[label] = pd.DataFrame(columns=['date','ret','peak'])
        continue
    print(f"  {label} ({len(sigs)} signals)...", flush=True)
    pnls[label] = run_ladder(sigs)

# ── PART 1: Summary table ──────────────────────────────────────────
print(f"\n{BAR}")
print("  PART 1 — SUMMARY: RAW vs HEIKIN ASHI (EMA9_low fresh break)")
print(BAR)
print(f"\n  {'Strategy':28} {'n':>5} {'/day':>5} {'fake%':>7} {'avg':>7} "
      f"{'win%':>6} {'ESL%':>6} {'big%':>6} {'total ₹':>10} {'max_dd ₹':>10}")
print(f"  {'─'*96}")

for label, sigs in sets:
    pnl = pnls[label]
    if pnl.empty:
        continue
    r   = pnl['ret']
    cum = r.cumsum()
    dd  = (cum - cum.cummax()).min() * LOT
    fr  = fake_rate(sigs)
    print(f"  {label:28} {len(sigs):5d} {len(sigs)/DAYS:5.1f} {fr:6.1f}%  "
          f"{r.mean():+6.1f}  {(r>0).mean()*100:5.1f}%  "
          f"{(r<-12).mean()*100:5.1f}%  {(r>12).mean()*100:5.1f}%  "
          f"₹{r.sum()*LOT:+8.0f}  ₹{dd:+8.0f}")

# ── PART 2: Fake vs Real breakdown ────────────────────────────────
print(f"\n{BAR}")
print("  PART 2 — FAKE vs REAL BREAKDOWN (best filtered: RSI + window)")
print(BAR)
print(f"\n  {'Strategy':28} {'type':6} {'n':>5} {'avg':>8} {'win%':>7} {'ESL%':>7}")
print(f"  {'─'*65}")

for label, mask in [
    ("RAW + RSI + window", raw_fresh & rsi_ok & in_win),
    ("HA  + RSI + window", ha_fresh  & rsi_ok & in_win),
]:
    sigs = df[mask].dropna(subset=['close_1c','close_2c','close_3c','ret'])
    if len(sigs) < 5:
        continue
    fake_mask = ((sigs['close_1c'] < sigs['ema9_low']) |
                 (sigs['close_2c'] < sigs['ema9_low']) |
                 (sigs['close_3c'] < sigs['ema9_low']))
    for lbl, sub in [('REAL', sigs[~fake_mask]), ('FAKE', sigs[fake_mask])]:
        r = sub['ret']
        if len(r) < 3:
            continue
        print(f"  {label:28} {lbl:6} {len(r):5d} {r.mean():+7.1f}  "
              f"{(r>0).mean()*100:6.1f}%  {(r<-12).mean()*100:6.1f}%")
    print()

# ── PART 3: Day-by-day comparison ─────────────────────────────────
print(f"\n{BAR}")
print("  PART 3 — DAY-BY-DAY: RAW+RSI+window vs HA+RSI+window (ladder ₹)")
print(BAR)

raw_pnl = pnls["RAW + RSI + window"]
ha_pnl  = pnls["HA  + RSI + window"]

print(f"\n  {'Date':12} {'RAW n':>6} {'RAW ₹':>9}  {'HA n':>5} {'HA ₹':>9}  {'HA-RAW':>8}")
print(f"  {'─'*60}")

r_cum = h_cum = 0
for d in sorted(df['timestamp'].dt.date.unique()):
    r_day = raw_pnl[raw_pnl['date']==d]['ret'] if not raw_pnl.empty else pd.Series(dtype=float)
    h_day = ha_pnl[ha_pnl['date']==d]['ret']   if not ha_pnl.empty  else pd.Series(dtype=float)
    if len(r_day) == 0 and len(h_day) == 0:
        continue
    r_inr = r_day.sum() * LOT
    h_inr = h_day.sum() * LOT
    r_cum += r_inr
    h_cum += h_inr
    diff  = h_inr - r_inr
    print(f"  {str(d):12} {len(r_day):6d} ₹{r_inr:+7.0f}  {len(h_day):5d} ₹{h_inr:+7.0f}  ₹{diff:+6.0f}")

print(f"  {'─'*60}")
print(f"  {'TOTAL':12} {len(raw_pnl):6d} ₹{r_cum:+7.0f}  {len(ha_pnl):5d} ₹{h_cum:+7.0f}  ₹{h_cum-r_cum:+6.0f}")

# ── PART 4: HA candle properties ──────────────────────────────────
print(f"\n{BAR}")
print("  PART 4 — HA CANDLE PROPERTIES vs RAW (on fresh break signals)")
print(BAR)

raw_sigs = df[raw_fresh & rsi_ok & in_win].copy()
ha_sigs  = df[ha_fresh  & rsi_ok & in_win].copy()

if len(raw_sigs) > 0:
    raw_sigs['body'] = raw_sigs['close'] - raw_sigs['open']
    raw_sigs['ha_body'] = raw_sigs['ha_close'] - raw_sigs['ha_open']
    print(f"\n  RAW signals ({len(raw_sigs)}):")
    print(f"    avg raw body:  {raw_sigs['body'].mean():+.2f}")
    print(f"    avg HA  body:  {raw_sigs['ha_body'].mean():+.2f}")
    print(f"    HA green%:     {(raw_sigs['ha_close']>raw_sigs['ha_open']).mean()*100:.1f}%")

if len(ha_sigs) > 0:
    ha_sigs['body']    = ha_sigs['close'] - ha_sigs['open']
    ha_sigs['ha_body'] = ha_sigs['ha_close'] - ha_sigs['ha_open']
    print(f"\n  HA  signals ({len(ha_sigs)}):")
    print(f"    avg raw body:  {ha_sigs['body'].mean():+.2f}")
    print(f"    avg HA  body:  {ha_sigs['ha_body'].mean():+.2f}")
    print(f"    HA green%:     {(ha_sigs['ha_close']>ha_sigs['ha_open']).mean()*100:.1f}%")

# ── PART 5: Signal overlap ─────────────────────────────────────────
print(f"\n{BAR}")
print("  PART 5 — SIGNAL OVERLAP (do HA and RAW fire on the same candles?)")
print(BAR)

raw_idx = set(df[raw_fresh & rsi_ok & in_win].index)
ha_idx  = set(df[ha_fresh  & rsi_ok & in_win].index)
both    = raw_idx & ha_idx
raw_only= raw_idx - ha_idx
ha_only = ha_idx  - raw_idx

print(f"\n  RAW only:  {len(raw_only)} signals")
print(f"  HA  only:  {len(ha_only)} signals")
print(f"  Both:      {len(both)} signals")
print(f"  Total RAW: {len(raw_idx)} | Total HA: {len(ha_idx)}")

if len(both) > 0:
    both_df = df.loc[list(both)]
    pnl_both = run_ladder(both_df)
    if not pnl_both.empty:
        r = pnl_both['ret']
        print(f"\n  Shared signals P&L: avg={r.mean():+.1f}  win={( r>0).mean()*100:.1f}%  "
              f"ESL={(r<-12).mean()*100:.1f}%  total=₹{r.sum()*LOT:+,.0f}")

if len(ha_only) > 0:
    ha_only_df = df.loc[list(ha_only)]
    pnl_hao = run_ladder(ha_only_df)
    if not pnl_hao.empty:
        r = pnl_hao['ret']
        print(f"  HA-only  signals P&L: avg={r.mean():+.1f}  win={(r>0).mean()*100:.1f}%  "
              f"ESL={(r<-12).mean()*100:.1f}%  total=₹{r.sum()*LOT:+,.0f}")

if len(raw_only) > 0:
    raw_only_df = df.loc[list(raw_only)]
    pnl_ro = run_ladder(raw_only_df)
    if not pnl_ro.empty:
        r = pnl_ro['ret']
        print(f"  RAW-only signals P&L: avg={r.mean():+.1f}  win={(r>0).mean()*100:.1f}%  "
              f"ESL={(r<-12).mean()*100:.1f}%  total=₹{r.sum()*LOT:+,.0f}")

print(f"\n  Note: {DAYS} days. LOT=65. Ladder exit on raw OHLC (real execution prices).")
print(f"  HA signal uses HA candles for detection, entry at raw close price.")
print(f"  Fake rate = raw close fell back below raw EMA9_low within 3 candles.")
