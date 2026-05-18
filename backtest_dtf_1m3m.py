"""
Dual-Timeframe: 1-min EMA9_high + 3-min EMA9_low Fresh Break

Signal (both must be true at same timestamp):
  - 3-min: fresh break above EMA9_low
           (prev_close <= EMA9_low AND curr_close > EMA9_low)
  - 1-min: close > EMA9_high (price above fast resistance band)
  - RSI 50-65 on 3-min candle

Logic: 3-min confirms support break (trend), 1-min EMA9_high
confirms momentum is strong enough to push above the upper band.
Joined on (strike, type, timestamp) — 3-min candle close aligns
with 1-min candle close at every 3rd minute.

Ladder exit on 1-min OHLC (finer resolution = better simulation).
Expiry-aware: next Tuesday groupby.
LOT = 65

3-min baseline: RAW + RSI 50-65 = ₹+32,338 / 92 signals / 20 days

Run: bash ~/VISHAL_RAJPUT/run_backtest.sh dtf_1m3m
"""
import sqlite3, os, sys
from datetime import timedelta
import pandas as pd
import numpy as np

DB = os.path.expanduser("~/lab_data/vrl_data.db")
if not os.path.exists(DB):
    sys.exit(f"DB not found: {DB}")

con = sqlite3.connect(DB)

# ── Load 3-min data ────────────────────────────────────────────────
print("Loading 3-min data...", flush=True)

_cur = con.cursor()
_cur.execute("PRAGMA table_info(option_3min)")
_has_expiry = any(r[1] == 'expiry' for r in _cur.fetchall())

if _has_expiry:
    _sql3 = """
        SELECT timestamp, strike, type, open, high, low, close, rsi, ema9_low, expiry
        FROM option_3min
        WHERE time(timestamp) >= '09:15:00' AND time(timestamp) < '15:30:00'
        ORDER BY strike, type, expiry, timestamp
    """
else:
    _sql3 = """
        SELECT timestamp, strike, type, open, high, low, close, rsi, ema9_low
        FROM option_3min
        WHERE time(timestamp) >= '09:15:00' AND time(timestamp) < '15:30:00'
        ORDER BY strike, type, timestamp
    """

df3 = pd.read_sql(_sql3, con, parse_dates=['timestamp'])

# ── Load 1-min data ────────────────────────────────────────────────
print("Loading 1-min data...", flush=True)
df1 = pd.read_sql("""
    SELECT timestamp, strike, type, open, high, low, close
    FROM option_1min
    WHERE time(timestamp) >= '09:15:00' AND time(timestamp) < '15:30:00'
    ORDER BY strike, type, timestamp
""", con, parse_dates=['timestamp'])
con.close()

# ── Expiry ─────────────────────────────────────────────────────────
def _next_tue(d):
    return d + timedelta(days=(1 - d.weekday()) % 7)

if not _has_expiry:
    print("Computing expiry inline (Tuesday weekly)...")
    df3['expiry'] = df3['timestamp'].dt.date.apply(_next_tue).astype(str)

df1['expiry'] = df1['timestamp'].dt.date.apply(_next_tue).astype(str)

df3 = df3.sort_values(['strike','type','expiry','timestamp']).reset_index(drop=True)
df1 = df1.sort_values(['strike','type','expiry','timestamp']).reset_index(drop=True)

print(f"3-min rows: {len(df3)} | 1-min rows: {len(df1)}", flush=True)

# ── Compute EMA9_high on 1-min per contract ────────────────────────
print("Computing EMA9_high on 1-min...", flush=True)
df1['ema9_high_1m'] = df1.groupby(['strike','type','expiry'])['high'].transform(
    lambda x: x.ewm(span=9, adjust=False).mean()
)

# Keep only the columns needed for the join
df1_join = df1[['timestamp','strike','type','expiry',
                'open','high','low','close','ema9_high_1m']].copy()
df1_join = df1_join.rename(columns={
    'open':  'open_1m',
    'high':  'high_1m',
    'low':   'low_1m',
    'close': 'close_1m',
})

# ── 3-min: fresh break detection ──────────────────────────────────
print("Computing 3-min signals...", flush=True)
g3 = df3.groupby(['strike','type','expiry'])
df3['close_prev']    = g3['close'].transform(lambda x: x.shift(1))
df3['ema9_low_prev'] = g3['ema9_low'].transform(lambda x: x.shift(1))
df3['rsi_prev']      = g3['rsi'].transform(lambda x: x.shift(1))
df3['fwd_3c']        = g3['close'].transform(lambda x: x.shift(-3))
df3['close_1c']      = g3['close'].transform(lambda x: x.shift(-1))
df3['close_2c']      = g3['close'].transform(lambda x: x.shift(-2))
df3['close_3c']      = g3['close'].transform(lambda x: x.shift(-3))

df3 = df3.dropna(subset=['close_prev','ema9_low_prev','rsi_prev','fwd_3c'])
df3['ret'] = df3['fwd_3c'] - df3['close']

fresh_3m = (df3['close'] > df3['ema9_low']) & (df3['close_prev'] <= df3['ema9_low_prev'])
rsi_ok   = (df3['rsi'] > 50) & (df3['rsi'] < 65)

# ── Join 1-min EMA9_high into 3-min at matching timestamps ────────
print("Joining 1-min EMA9_high to 3-min signals...", flush=True)
df3 = df3.merge(
    df1_join[['timestamp','strike','type','expiry','close_1m','ema9_high_1m',
              'open_1m','high_1m','low_1m']],
    on=['timestamp','strike','type','expiry'],
    how='left'
)

above_ema9h_1m = df3['close_1m'] > df3['ema9_high_1m']

DAYS = df3['timestamp'].dt.date.nunique()
LOT  = 65
BAR  = '━' * 80

print(f"Days: {DAYS} | 3-min candles: {len(df3)}\n")

def fake_rate(sigs):
    fv = sigs.dropna(subset=['close_1c','close_2c','close_3c'])
    return ((fv['close_1c'] < fv['ema9_low']) |
            (fv['close_2c'] < fv['ema9_low']) |
            (fv['close_3c'] < fv['ema9_low'])).mean() * 100

# ── Ladder on 1-min OHLC ──────────────────────────────────────────
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
sym1_candles = {}
for (strike, typ, exp), grp in df1.groupby(['strike','type','expiry']):
    sym1_candles[(strike, typ, exp)] = grp[['timestamp','open','high','low','close']].reset_index(drop=True)

def run_ladder(signals):
    results = []
    for _, row in signals.iterrows():
        key   = (row['strike'], row['type'], row['expiry'])
        ts    = row['timestamp']
        entry = row['close']
        if key not in sym1_candles:
            continue
        sym    = sym1_candles[key]
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
    ("3M fresh + RSI [baseline]",
     df3[fresh_3m & rsi_ok]),
    ("3M fresh + RSI + 1M>EMA9H",
     df3[fresh_3m & rsi_ok & above_ema9h_1m]),
    ("3M fresh + 1M>EMA9H (no RSI)",
     df3[fresh_3m & above_ema9h_1m]),
    ("1M>EMA9H only (no 3M filter)",
     df3[above_ema9h_1m & rsi_ok]),
]

print(f"\n{BAR}")
print("  Running ladder simulations...")
print(BAR)
pnls = {}
for label, sigs in sets:
    sigs = sigs.dropna(subset=['close'])
    if len(sigs) < 5:
        print(f"  {label}: <5 signals")
        pnls[label] = pd.DataFrame(columns=['date','ret','peak'])
        continue
    print(f"  {label} ({len(sigs)} signals)...", flush=True)
    pnls[label] = run_ladder(sigs)

# ── PART 1: Summary ────────────────────────────────────────────────
print(f"\n{BAR}")
print("  PART 1 — DUAL-TF SUMMARY: 3-min EMA9_low + 1-min EMA9_high")
print(BAR)
print(f"\n  {'Strategy':36} {'n':>5} {'/day':>5} {'fake%':>7} {'avg':>7} "
      f"{'win%':>6} {'ESL%':>6} {'big%':>6} {'total ₹':>10} {'max_dd ₹':>10}")
print(f"  {'─'*100}")

for label, sigs in sets:
    sigs = sigs.dropna(subset=['close'])
    pnl  = pnls[label]
    if pnl.empty:
        continue
    r   = pnl['ret']
    cum = r.cumsum()
    dd  = (cum - cum.cummax()).min() * LOT
    fr  = fake_rate(sigs)
    print(f"  {label:36} {len(sigs):5d} {len(sigs)/DAYS:5.1f} {fr:6.1f}%  "
          f"{r.mean():+6.1f}  {(r>0).mean()*100:5.1f}%  "
          f"{(r<-12).mean()*100:5.1f}%  {(r>12).mean()*100:5.1f}%  "
          f"₹{r.sum()*LOT:+8.0f}  ₹{dd:+8.0f}")

# ── PART 2: Fake vs Real for dual-TF ──────────────────────────────
print(f"\n{BAR}")
print("  PART 2 — FAKE vs REAL: 3M fresh + RSI + 1M>EMA9H")
print(BAR)
print(f"\n  {'type':6} {'n':>5} {'avg':>8} {'win%':>7} {'ESL%':>7} {'big%':>7}")
print(f"  {'─'*45}")

sigs_dtf = df3[fresh_3m & rsi_ok & above_ema9h_1m].dropna(
    subset=['close_1c','close_2c','close_3c','ret'])
if len(sigs_dtf) > 5:
    fake_mask = ((sigs_dtf['close_1c'] < sigs_dtf['ema9_low']) |
                 (sigs_dtf['close_2c'] < sigs_dtf['ema9_low']) |
                 (sigs_dtf['close_3c'] < sigs_dtf['ema9_low']))
    for lbl, sub in [('REAL', sigs_dtf[~fake_mask]), ('FAKE', sigs_dtf[fake_mask])]:
        r = sub['ret']
        if len(r) < 3:
            continue
        print(f"  {lbl:6} {len(r):5d} {r.mean():+7.1f}  "
              f"{(r>0).mean()*100:6.1f}%  {(r<-12).mean()*100:6.1f}%  "
              f"{(r>12).mean()*100:6.1f}%")

# ── PART 3: Day-by-day ─────────────────────────────────────────────
print(f"\n{BAR}")
print("  PART 3 — DAY-BY-DAY: dual-TF vs 3M baseline")
print(BAR)

pnl_dtf  = pnls["3M fresh + RSI + 1M>EMA9H"]
pnl_base = pnls["3M fresh + RSI [baseline]"]

print(f"\n  {'Date':12} {'DTF n':>6} {'DTF ₹':>9}  {'BASE n':>6} {'BASE ₹':>9}  {'saved':>7}")
print(f"  {'─'*60}")

cum_dtf = cum_base = 0
for d in sorted(df3['timestamp'].dt.date.unique()):
    d_dtf  = pnl_dtf[pnl_dtf['date']==d]['ret']   if not pnl_dtf.empty  else pd.Series(dtype=float)
    d_base = pnl_base[pnl_base['date']==d]['ret']  if not pnl_base.empty else pd.Series(dtype=float)
    if len(d_dtf)==0 and len(d_base)==0:
        continue
    inr_dtf  = d_dtf.sum()  * LOT
    inr_base = d_base.sum() * LOT
    cum_dtf  += inr_dtf
    cum_base += inr_base
    diff = inr_dtf - inr_base
    print(f"  {str(d):12} {len(d_dtf):6d} ₹{inr_dtf:+7.0f}  "
          f"{len(d_base):6d} ₹{inr_base:+7.0f}  ₹{diff:+6.0f}")

print(f"  {'─'*60}")
r_dtf  = pnl_dtf['ret']  if not pnl_dtf.empty  else pd.Series(dtype=float)
r_base = pnl_base['ret'] if not pnl_base.empty else pd.Series(dtype=float)
print(f"  {'TOTAL':12} {len(r_dtf):6d} ₹{r_dtf.sum()*LOT:+7.0f}  "
      f"{len(r_base):6d} ₹{r_base.sum()*LOT:+7.0f}  "
      f"₹{(r_dtf.sum()-r_base.sum())*LOT:+6.0f}")

# ── PART 4: How many 3M signals pass the 1M filter ─────────────────
print(f"\n{BAR}")
print("  PART 4 — FILTER EFFECT: how 1M>EMA9H screens 3M signals")
print(BAR)

base_n = len(df3[fresh_3m & rsi_ok])
dtf_n  = len(df3[fresh_3m & rsi_ok & above_ema9h_1m])
drop_n = base_n - dtf_n
print(f"\n  3M fresh + RSI signals:     {base_n}")
print(f"  Pass 1M>EMA9H filter:       {dtf_n}  ({dtf_n/base_n*100:.1f}% kept)")
print(f"  Blocked by 1M filter:       {drop_n}  ({drop_n/base_n*100:.1f}% removed)")

# What are the blocked signals like?
blocked = df3[fresh_3m & rsi_ok & ~above_ema9h_1m].dropna(subset=['ret'])
if len(blocked) > 3:
    r_bl = blocked['ret']
    print(f"\n  Blocked signals raw 3C return: avg={r_bl.mean():+.1f}  "
          f"win={(r_bl>0).mean()*100:.1f}%  n={len(r_bl)}")
    pnl_blocked = run_ladder(blocked)
    if not pnl_blocked.empty:
        rb = pnl_blocked['ret']
        print(f"  Blocked signals ladder P&L:   avg={rb.mean():+.1f}  "
              f"win={(rb>0).mean()*100:.1f}%  total=₹{rb.sum()*LOT:+,.0f}")
        print(f"  (If avg < 0: filter correctly removes bad trades)")
        print(f"  (If avg > 0: filter incorrectly removes good trades)")

print(f"\n  Note: {DAYS} days. LOT=65. Ladder on 1-min OHLC (max 30 candles = 30 min).")
print(f"  3-min EMA9_low from DB. 1-min EMA9_high computed fresh.")
print(f"  Join: 3-min candle close timestamp = 1-min candle close timestamp.")
