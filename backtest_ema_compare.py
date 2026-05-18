"""
EMA Low Break Comparison — 3-min timeframe
Tests fresh break above EMA_low for periods: 9, 21, 51, 200
Computes EMA of low prices for each period from raw OHLC.

Compares:
  - Signal count and frequency
  - Fake break rate (falls back below within 3 candles)
  - 9-min forward return (raw)
  - With RSI 50-65 filter
  - Exit ladder P&L simulation
  - Day-by-day cumulative

Run: bash ~/VISHAL_RAJPUT/run_backtest.sh ema_compare
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
    SELECT timestamp, strike, type, open, high, low, close, rsi,
           ema9_low
    FROM option_3min
    WHERE time(timestamp) >= '09:15:00' AND time(timestamp) < '15:30:00'
    ORDER BY strike, type, timestamp
""", con, parse_dates=['timestamp'])
con.close()

df = df.sort_values(['strike','type','timestamp']).copy()
print(f"Rows: {len(df)}", flush=True)

# ── Compute EMA of lows for each period ──────────────────────────
print("Computing EMAs...", flush=True)

EMA_PERIODS = [9, 21, 51, 200]

def compute_ema_low(group, period):
    return group['low'].ewm(span=period, adjust=False).mean()

for p in EMA_PERIODS:
    col = f'ema{p}_low'
    if col == 'ema9_low':
        continue  # already in DB
    df[col] = df.groupby(['strike','type'])['low'].transform(
        lambda x: x.ewm(span=p, adjust=False).mean()
    )

# Also compute EMA9_low fresh (verify against DB column)
df['ema9_low_calc'] = df.groupby(['strike','type'])['low'].transform(
    lambda x: x.ewm(span=9, adjust=False).mean()
)

g = df.groupby(['strike','type'])
df['rsi_prev']  = g['rsi'].transform(lambda x: x.shift(1))
df['close_prev']= g['close'].transform(lambda x: x.shift(1))

# Forward returns
df['fwd_3c']  = g['close'].transform(lambda x: x.shift(-3))
df['fwd_6c']  = g['close'].transform(lambda x: x.shift(-6))
df['close_1c']= g['close'].transform(lambda x: x.shift(-1))
df['close_2c']= g['close'].transform(lambda x: x.shift(-2))
df['close_3c']= g['close'].transform(lambda x: x.shift(-3))
df['low_1c']  = g['low'].transform(lambda x: x.shift(-1))

df = df.dropna(subset=['rsi_prev','fwd_3c'])
df['ret'] = df['fwd_3c'] - df['close']

# Compute prev EMA cols for fresh break detection
for p in EMA_PERIODS:
    col = f'ema{p}_low'
    df[f'prev_{col}'] = g[col].transform(lambda x: x.shift(1))

DAYS = df['timestamp'].dt.date.nunique()
LOT  = 65
BAR  = '━' * 78

print(f"Days: {DAYS} | Candles: {len(df)}\n")

# ── Ladder simulation ─────────────────────────────────────────────
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
        return candles[min(len(candles),max_c)-1][3] - entry, peak
    return 0.0, 0.0

# Pre-build symbol candle lookup
sym_candles = {}
for (strike, typ), grp in df.groupby(['strike','type']):
    sym_candles[(strike,typ)] = grp[['timestamp','open','high','low','close']].reset_index(drop=True)

def run_ladder_pnl(signals):
    results = []
    for _, row in signals.iterrows():
        key   = (row['strike'], row['type'])
        ts    = row['timestamp']
        entry = row['close']
        if key not in sym_candles: continue
        sym   = sym_candles[key]
        future= sym[sym['timestamp'] > ts]
        clist = list(future[['open','high','low','close']].itertuples(index=False,name=None))
        if not clist: continue
        ret, peak = sim_ladder(entry, clist)
        results.append({'date': ts.date(), 'ret': ret, 'peak': peak})
    return pd.DataFrame(results)

# ── Analyse each EMA period ───────────────────────────────────────
print(f"{BAR}")
print("  PART 1 — FRESH BREAK SUMMARY (all signals, no RSI filter)")
print(BAR)
print(f"\n  {'EMA':8} {'signals':>8} {'/day':>6} {'fake%':>7} {'avg':>8} "
      f"{'win%':>7} {'ESL%':>7} {'big%':>7}")
print(f"  {'─'*68}")

results_all = {}

for p in EMA_PERIODS:
    ecol      = f'ema{p}_low'
    prev_ecol = f'prev_{ecol}'

    fresh = df[
        (df['close'] > df[ecol]) &
        (df['close_prev'] <= df[prev_ecol])
    ].copy()

    if len(fresh) < 10:
        print(f"  EMA{p:3d}    {'<10 signals':>8}")
        continue

    # Fake rate
    fr_valid = fresh.dropna(subset=['close_1c','close_2c','close_3c'])
    fake3 = ((fr_valid['close_1c'] < fr_valid[ecol]) |
             (fr_valid['close_2c'] < fr_valid[ecol]) |
             (fr_valid['close_3c'] < fr_valid[ecol])).mean() * 100

    r = fresh['ret'].dropna()
    results_all[p] = fresh
    print(f"  EMA{p:3d}  {len(fresh):8d} {len(fresh)/DAYS:6.1f} {fake3:6.1f}%  "
          f"{r.mean():+7.1f}  {(r>0).mean()*100:6.1f}%  "
          f"{(r<-12).mean()*100:6.1f}%  {(r>12).mean()*100:6.1f}%")

# ── With RSI 50-65 ────────────────────────────────────────────────
print(f"\n{BAR}")
print("  PART 2 — FRESH BREAK + RSI 50-65")
print(BAR)
print(f"\n  {'EMA':8} {'signals':>8} {'/day':>6} {'fake%':>7} {'avg':>8} "
      f"{'win%':>7} {'ESL%':>7} {'big%':>7}")
print(f"  {'─'*68}")

results_rsi = {}

for p in EMA_PERIODS:
    ecol      = f'ema{p}_low'
    prev_ecol = f'prev_{ecol}'

    fresh = df[
        (df['close'] > df[ecol]) &
        (df['close_prev'] <= df[prev_ecol]) &
        (df['rsi'] > 50) & (df['rsi'] < 65)
    ].copy()

    if len(fresh) < 10:
        print(f"  EMA{p:3d}    {'<10 signals':>8}")
        continue

    fr_valid = fresh.dropna(subset=['close_1c','close_2c','close_3c'])
    fake3 = ((fr_valid['close_1c'] < fr_valid[ecol]) |
             (fr_valid['close_2c'] < fr_valid[ecol]) |
             (fr_valid['close_3c'] < fr_valid[ecol])).mean() * 100

    r = fresh['ret'].dropna()
    results_rsi[p] = fresh
    print(f"  EMA{p:3d}  {len(fresh):8d} {len(fresh)/DAYS:6.1f} {fake3:6.1f}%  "
          f"{r.mean():+7.1f}  {(r>0).mean()*100:6.1f}%  "
          f"{(r<-12).mean()*100:6.1f}%  {(r>12).mean()*100:6.1f}%")

# ── Fake vs Real split ────────────────────────────────────────────
print(f"\n{BAR}")
print("  PART 3 — FAKE vs REAL BREAK (RSI 50-65 filter)")
print(BAR)
print(f"\n  {'EMA':8} {'type':8} {'n':>5} {'avg':>8} {'win%':>7} {'ESL%':>7}")
print(f"  {'─'*52}")

for p in EMA_PERIODS:
    if p not in results_rsi: continue
    ecol  = f'ema{p}_low'
    fresh = results_rsi[p].copy()
    fv    = fresh.dropna(subset=['close_1c','close_2c','close_3c'])
    fake_mask = ((fv['close_1c'] < fv[ecol]) |
                 (fv['close_2c'] < fv[ecol]) |
                 (fv['close_3c'] < fv[ecol]))

    reals = fv[~fake_mask]
    fakes = fv[fake_mask]
    for label, sub in [('REAL', reals), ('FAKE', fakes)]:
        r = sub['ret']
        if len(r) < 3: continue
        print(f"  EMA{p:3d}  {label:8} {len(r):5d} {r.mean():+7.1f}  "
              f"{(r>0).mean()*100:6.1f}%  {(r<-12).mean()*100:6.1f}%")
    print()

# ── Ladder P&L ────────────────────────────────────────────────────
print(f"\n{BAR}")
print("  PART 4 — EXIT LADDER P&L (1 lot = 65 units, RSI 50-65 filter)")
print(BAR)
print("\n  Running ladder simulation...", flush=True)

pnl_results = {}
for p in EMA_PERIODS:
    if p not in results_rsi: continue
    print(f"  EMA{p}...", flush=True)
    tr = run_ladder_pnl(results_rsi[p])
    pnl_results[p] = tr

print(f"\n  {'EMA':8} {'trades':>7} {'total pts':>10} {'total ₹':>10} "
      f"{'per trade':>10} {'win%':>7} {'ESL%':>7} {'max_dd ₹':>10}")
print(f"  {'─'*75}")

for p in EMA_PERIODS:
    if p not in pnl_results or pnl_results[p].empty: continue
    tr  = pnl_results[p]
    r   = tr['ret']
    cum = r.cumsum()
    dd  = (cum - cum.cummax()).min() * LOT
    total_inr = r.sum() * LOT
    print(f"  EMA{p:3d}  {len(r):7d} {r.sum():+9.1f}  ₹{total_inr:+8.0f}  "
          f"{r.mean():+9.1f}  {(r>0).mean()*100:6.1f}%  "
          f"{(r<-12).mean()*100:6.1f}%  ₹{dd:+8.0f}")

# ── Day-by-day for each EMA ───────────────────────────────────────
print(f"\n{BAR}")
print("  PART 5 — DAY-BY-DAY P&L (ladder, 1 lot, RSI 50-65)")
print(BAR)

# Header
header = f"  {'Date':12}"
for p in EMA_PERIODS:
    header += f"  {'EMA'+str(p):>14}"
print(f"\n{header}")
print(f"  {'─'*75}")

all_dates = sorted(df['timestamp'].dt.date.unique())
cumulative = {p: 0 for p in EMA_PERIODS}

for d in all_dates:
    row_str = f"  {str(d):12}"
    any_data = False
    for p in EMA_PERIODS:
        if p not in pnl_results or pnl_results[p].empty:
            row_str += f"  {'—':>14}"
            continue
        day = pnl_results[p][pnl_results[p]['date']==d]['ret']
        pts = day.sum()
        n   = len(day)
        cumulative[p] += pts
        inr = pts * LOT
        row_str += f"  {n}t {pts:+5.1f} ₹{inr:+5.0f}"
        if n > 0: any_data = True
    if any_data:
        print(row_str)

print(f"  {'─'*75}")
total_row = f"  {'TOTAL':12}"
for p in EMA_PERIODS:
    if p not in pnl_results or pnl_results[p].empty:
        total_row += f"  {'—':>14}"
        continue
    r = pnl_results[p]['ret']
    total_row += f"  {len(r)}t {r.sum():+5.1f} ₹{r.sum()*LOT:+5.0f}"
print(total_row)

# ── Risk profile ──────────────────────────────────────────────────
print(f"\n{BAR}")
print("  PART 6 — RISK PROFILE COMPARISON")
print(BAR)
print(f"\n  {'EMA':8} {'avg win':>9} {'avg loss':>9} {'W/L ratio':>10} "
      f"{'max streak':>11} {'best ₹':>10} {'worst ₹':>10}")
print(f"  {'─'*72}")

for p in EMA_PERIODS:
    if p not in pnl_results or pnl_results[p].empty: continue
    r   = pnl_results[p]['ret']
    wins= r[r>0]; losses=r[r<0]
    if len(wins)==0 or len(losses)==0: continue
    wl  = abs(wins.mean()/losses.mean())
    import itertools
    streak = max((sum(1 for _ in g2) for k,g2 in
                  itertools.groupby((r<0).astype(int)) if k), default=0)
    print(f"  EMA{p:3d}  {wins.mean():+8.1f}  {losses.mean():+8.1f}  "
          f"{wl:9.2f}x  {streak:10d}  ₹{r.max()*LOT:+8.0f}  ₹{r.min()*LOT:+8.0f}")

print(f"\n  Note: {DAYS} days. LOT=65. Fresh break = prev close <= EMA_low, current > EMA_low.")
print(f"  EMA computed from 3-min LOW prices (EWM, adjust=False).")
