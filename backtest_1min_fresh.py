"""
Fresh EMA9_low Break — 1-min candle backtest
Same strategy as 3-min but on 1-min timeframe.

  - EMA9 of 1-min LOW prices (computed fresh, not in DB)
  - Fresh break: prev_close <= EMA9_low AND curr_close > EMA9_low
  - RSI 50-65 (from DB)
  - Ladder exit on 1-min OHLC

Expiry-aware: groupby(['strike','type','expiry']), expiry = next Tuesday.
LOT = 65

3-min baseline for comparison:
  RAW + RSI 50-65: 92 signals/20d, avg=+5.5, total=₹+32,338

Run: bash ~/VISHAL_RAJPUT/run_backtest.sh 1min_fresh
"""
import sqlite3, os, sys
from datetime import timedelta
import pandas as pd
import numpy as np

DB = os.path.expanduser("~/lab_data/vrl_data.db")
if not os.path.exists(DB):
    sys.exit(f"DB not found: {DB}")

con = sqlite3.connect(DB)
print("Loading 1-min data...", flush=True)
df = pd.read_sql("""
    SELECT timestamp, strike, type, open, high, low, close, rsi
    FROM option_1min
    WHERE time(timestamp) >= '09:15:00' AND time(timestamp) < '15:30:00'
    ORDER BY strike, type, timestamp
""", con, parse_dates=['timestamp'])
con.close()

df = df.sort_values(['strike','type','timestamp']).reset_index(drop=True)
print(f"Rows: {len(df)}", flush=True)

# ── Expiry = next Tuesday (NIFTY50 weekly, effective Sep 2025) ─────
def _next_tue(d):
    return d + timedelta(days=(1 - d.weekday()) % 7)
df['expiry'] = df['timestamp'].dt.date.apply(_next_tue).astype(str)

df = df.sort_values(['strike','type','expiry','timestamp']).reset_index(drop=True)

# ── EMA9 of low per contract ───────────────────────────────────────
print("Computing EMA9 of 1-min low per contract...", flush=True)
df['ema9_low'] = df.groupby(['strike','type','expiry'])['low'].transform(
    lambda x: x.ewm(span=9, adjust=False).mean()
)

g = df.groupby(['strike','type','expiry'])
df['close_prev']   = g['close'].transform(lambda x: x.shift(1))
df['ema9_low_prev']= g['ema9_low'].transform(lambda x: x.shift(1))
df['rsi_prev']     = g['rsi'].transform(lambda x: x.shift(1))
df['fwd_3c']       = g['close'].transform(lambda x: x.shift(-3))
df['close_1c']     = g['close'].transform(lambda x: x.shift(-1))
df['close_2c']     = g['close'].transform(lambda x: x.shift(-2))
df['close_3c']     = g['close'].transform(lambda x: x.shift(-3))

df = df.dropna(subset=['close_prev','ema9_low_prev','rsi_prev','fwd_3c'])
df = df[df['close'] > 0].copy()
df['ret']  = df['fwd_3c'] - df['close']
df['hhmm'] = df['timestamp'].dt.hour * 60 + df['timestamp'].dt.minute

DAYS = df['timestamp'].dt.date.nunique()
LOT  = 65
BAR  = '━' * 80

print(f"Days: {DAYS} | Candles: {len(df)}\n")

# ── Signal masks ───────────────────────────────────────────────────
fresh  = (df['close'] > df['ema9_low']) & (df['close_prev'] <= df['ema9_low_prev'])
rsi_ok = (df['rsi'] > 50) & (df['rsi'] < 65)
in_win = (df['hhmm'] >= 9*60+45) & (df['hhmm'] < 13*60+45)

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

print("Building 1-min candle lookup...", flush=True)
sym_candles = {}
for (strike, typ, exp), grp in df.groupby(['strike','type','expiry']):
    sym_candles[(strike, typ, exp)] = grp[['timestamp','open','high','low','close']].reset_index(drop=True)

def run_ladder(signals):
    results = []
    for _, row in signals.iterrows():
        key   = (row['strike'], row['type'], row['expiry'])
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

# ── Signal sets ────────────────────────────────────────────────────
sets = [
    ("1M RAW no filter",      df[fresh]),
    ("1M RAW + RSI 50-65",    df[fresh & rsi_ok]),
    ("1M RAW + RSI + window", df[fresh & rsi_ok & in_win]),
]

print(f"\n{BAR}")
print("  Running ladder simulations...")
print(BAR)

pnls = {}
for label, sigs in sets:
    if len(sigs) < 5:
        print(f"  {label}: <5 signals, skip")
        pnls[label] = pd.DataFrame(columns=['date','ret','peak'])
        continue
    print(f"  {label} ({len(sigs)} signals)...", flush=True)
    pnls[label] = run_ladder(sigs)

# ── PART 1: Summary ────────────────────────────────────────────────
print(f"\n{BAR}")
print("  PART 1 — 1-MIN FRESH BREAK SUMMARY")
print(BAR)
print(f"\n  {'Strategy':28} {'n':>6} {'/day':>5} {'fake%':>7} {'avg':>7} "
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
    print(f"  {label:28} {len(sigs):6d} {len(sigs)/DAYS:5.1f} {fr:6.1f}%  "
          f"{r.mean():+6.1f}  {(r>0).mean()*100:5.1f}%  "
          f"{(r<-12).mean()*100:5.1f}%  {(r>12).mean()*100:5.1f}%  "
          f"₹{r.sum()*LOT:+8.0f}  ₹{dd:+8.0f}")

# 3-min baseline for reference
print(f"\n  {'─'*96}")
print(f"  {'3M RAW + RSI 50-65 [BASELINE]':28} {'92':>6} {'4.6':>5} {'38.0%':>7} "
      f"{'  +5.5':>7} {'53.8%':>6} {' 0.0%':>6} {'15.4%':>6} {'₹ +32338':>10} {'₹  -5720':>10}")

# ── PART 2: Fake vs Real ───────────────────────────────────────────
print(f"\n{BAR}")
print("  PART 2 — FAKE vs REAL (1-min RAW + RSI 50-65)")
print(BAR)
print(f"\n  {'type':6} {'n':>5} {'avg':>8} {'win%':>7} {'ESL%':>7} {'big%':>7}")
print(f"  {'─'*45}")

sigs_rsi = df[fresh & rsi_ok].dropna(subset=['close_1c','close_2c','close_3c','ret'])
if len(sigs_rsi) > 0:
    fake_mask = ((sigs_rsi['close_1c'] < sigs_rsi['ema9_low']) |
                 (sigs_rsi['close_2c'] < sigs_rsi['ema9_low']) |
                 (sigs_rsi['close_3c'] < sigs_rsi['ema9_low']))
    for lbl, sub in [('REAL', sigs_rsi[~fake_mask]), ('FAKE', sigs_rsi[fake_mask])]:
        r = sub['ret']
        if len(r) < 3:
            continue
        print(f"  {lbl:6} {len(r):5d} {r.mean():+7.1f}  "
              f"{(r>0).mean()*100:6.1f}%  {(r<-12).mean()*100:6.1f}%  "
              f"{(r>12).mean()*100:6.1f}%")

# ── PART 3: Day-by-day ─────────────────────────────────────────────
print(f"\n{BAR}")
print("  PART 3 — DAY-BY-DAY: 1-min vs 3-min baseline (RSI 50-65)")
print(BAR)
print(f"\n  {'Date':12} {'1M n':>6} {'1M ₹':>9}  {'3M n':>5} {'3M ₹':>9}")
print(f"  {'─'*48}")

pnl_1m = pnls.get("1M RAW + RSI 50-65", pd.DataFrame())

# 3-min reference values from last clean run
ref_3m = {
    '2026-04-20': (-1560, 2), '2026-04-21': (0, 0),
    '2026-04-22': (-1560, 2), '2026-04-23': (2990, 4),
    '2026-04-24': (4030, 4),  '2026-04-27': (-3120, 4),
    '2026-04-28': (-260, 3),  '2026-04-29': (-2340, 5),
    '2026-04-30': (-1300, 3), '2026-05-04': (1560, 4),
    '2026-05-05': (-1820, 5), '2026-05-06': (260, 3),
    '2026-05-07': (1944, 5),  '2026-05-08': (-3640, 6),
    '2026-05-11': (-1560, 2), '2026-05-12': (21083, 6),
    '2026-05-13': (1820, 3),  '2026-05-14': (780, 1),
    '2026-05-15': (-1043, 5), '2026-05-18': (715, 2),
}

cum_1m = 0
for d in sorted(df['timestamp'].dt.date.unique()):
    ds = str(d)
    day_1m = pnl_1m[pnl_1m['date']==d]['ret'] if not pnl_1m.empty else pd.Series(dtype=float)
    inr_1m = day_1m.sum() * LOT
    cum_1m += inr_1m
    ref = ref_3m.get(ds, (0, 0))
    print(f"  {ds:12} {len(day_1m):6d} ₹{inr_1m:+7.0f}  {ref[1]:5d} ₹{ref[0]:+7.0f}")

print(f"  {'─'*48}")
r_1m = pnl_1m['ret'] if not pnl_1m.empty else pd.Series(dtype=float)
print(f"  {'TOTAL':12} {len(r_1m):6d} ₹{r_1m.sum()*LOT:+7.0f}  {'92':>5} ₹{'+32338':>7}")

# ── PART 4: Signal time distribution ──────────────────────────────
print(f"\n{BAR}")
print("  PART 4 — SIGNAL TIME DISTRIBUTION (1-min RSI 50-65)")
print(BAR)
print(f"\n  {'Time':10} {'n':>5} {'avg':>8} {'win%':>7} {'ESL%':>7} {'total ₹':>10}")
print(f"  {'─'*52}")

sigs_1m = df[fresh & rsi_ok].copy()
if not pnl_1m.empty and len(sigs_1m) > 0:
    sigs_1m_idx = sigs_1m.copy()
    pnl_map = run_ladder(sigs_1m_idx)
    if not pnl_map.empty:
        sigs_1m_idx = sigs_1m_idx.reset_index(drop=True)
        pnl_map = pnl_map.reset_index(drop=True)
        sigs_1m_idx['pnl_ret'] = pnl_map['ret']
        for (hlo, hhi), label in [
            ((9*60+15, 9*60+45), '09:15-09:45'),
            ((9*60+45, 10*60+30), '09:45-10:30'),
            ((10*60+30, 11*60+30), '10:30-11:30'),
            ((11*60+30, 12*60+30), '11:30-12:30'),
            ((12*60+30, 13*60+30), '12:30-13:30'),
            ((13*60+30, 14*60+30), '13:30-14:30'),
            ((14*60+30, 15*60+30), '14:30-15:30'),
        ]:
            sub = sigs_1m_idx[(sigs_1m_idx['hhmm'] >= hlo) & (sigs_1m_idx['hhmm'] < hhi)]
            r = sub['pnl_ret'].dropna()
            if len(r) < 3:
                continue
            print(f"  {label:10} {len(r):5d} {r.mean():+7.1f}  "
                  f"{(r>0).mean()*100:6.1f}%  {(r<-12).mean()*100:6.1f}%  "
                  f"₹{r.sum()*LOT:+8.0f}")

print(f"\n  Note: {DAYS} days. LOT=65. Ladder on 1-min OHLC (max 30 candles = 30 min hold).")
print(f"  EMA9 of 1-min LOW, expiry-aware (Tuesday weekly).")
print(f"  Fake rate = close fell back below EMA9_low within next 3 1-min candles.")
