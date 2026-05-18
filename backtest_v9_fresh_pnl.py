"""
Fresh Break Strategy — Fake Breakout Analysis + Real P&L Simulation

Questions:
  1. How often does a fresh EMA9_low break reverse? (fake breakout rate)
  2. What defines a fake vs real break?
  3. Real cumulative P&L through exit ladder — day by day
  4. Compare: Fresh+RSI50-65 vs V9 full — actual money

Run: bash ~/VISHAL_RAJPUT/run_backtest.sh v9_fresh_pnl
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

df['rsi_prev']    = g['rsi'].transform(lambda x: x.shift(1))
df['ema9l_prev']  = g['ema9_low'].transform(lambda x: x.shift(1))
df['ema9l_prev2'] = g['ema9_low'].transform(lambda x: x.shift(2))
df['bw']          = df['ema9_high'] - df['ema9_low']
df['slope1']      = df['ema9_low'] - df['ema9l_prev']
df['slope2']      = df['ema9l_prev'] - df['ema9l_prev2']
df['close_prev']  = g['close'].transform(lambda x: x.shift(1))
df['open_prev']   = g['open'].transform(lambda x: x.shift(1))

# Next 3 candle closes for fake break detection
df['close_1c']  = g['close'].transform(lambda x: x.shift(-1))
df['close_2c']  = g['close'].transform(lambda x: x.shift(-2))
df['close_3c']  = g['close'].transform(lambda x: x.shift(-3))
df['low_1c']    = g['low'].transform(lambda x: x.shift(-1))
df['low_2c']    = g['low'].transform(lambda x: x.shift(-2))

df = df.dropna(subset=['rsi_prev','ema9l_prev','bw'])
df['above_ema9l'] = df['close'] > df['ema9_low']
df['prev_above']  = df['close_prev'] > df['ema9l_prev']
df['fresh_break'] = df['above_ema9l'] & ~df['prev_above']
df['green']       = df['close'] > df['open']
df['rsi_rising']  = df['rsi'] > df['rsi_prev']

DAYS = df['timestamp'].dt.date.nunique()
BAR  = '━' * 76

print(f"Days: {DAYS} | Candles: {len(df)}\n")

# Signal sets
fresh_rsi = df[df['fresh_break'] & (df['rsi']>50) & (df['rsi']<65)].copy()
v9_full   = df[
    df['green'] & df['above_ema9l'] &
    (df['slope1']>=0) & (df['slope2']>=0) &
    (df['bw']>=13) & (df['bw']<=17) &
    (df['rsi']>50) & (df['rsi']<65) & df['rsi_rising']
].copy()

print(f"Fresh+RSI50-65 signals: {len(fresh_rsi)} ({len(fresh_rsi)/DAYS:.1f}/day)")
print(f"V9 full signals:        {len(v9_full)} ({len(v9_full)/DAYS:.1f}/day)\n")

# ── PART 1: Fake breakout analysis ───────────────────────────────
print(f"{BAR}")
print("  PART 1 — FAKE BREAKOUT ANALYSIS")
print("  (how often does price fall back below EMA9_low after fresh break?)")
print(BAR)

fr = fresh_rsi.copy()
fr['fake_1c'] = fr['close_1c'] < fr['ema9_low']   # next candle closes below
fr['fake_2c'] = (fr['close_1c'] < fr['ema9_low']) | (fr['close_2c'] < fr['ema9_low'])
fr['fake_3c'] = fr['fake_2c'] | (fr['close_3c'] < fr['ema9_low'])
fr['low_touch']= fr['low_1c'] < fr['ema9_low']    # next candle TOUCHES below (intra)

real_fr = fr.dropna(subset=['close_1c','close_2c','close_3c'])
n = len(real_fr)

print(f"\n  Signals analysed: {n}")
print(f"\n  {'Fake breakout definition':45} {'n':>5} {'%':>7}")
print(f"  {'─'*58}")
print(f"  {'Price falls below EMA9_low by next close (1c)':45} "
      f"{real_fr['fake_1c'].sum():5d} {real_fr['fake_1c'].mean()*100:6.1f}%")
print(f"  {'Falls below within 2 candles (6min)':45} "
      f"{real_fr['fake_2c'].sum():5d} {real_fr['fake_2c'].mean()*100:6.1f}%")
print(f"  {'Falls below within 3 candles (9min)':45} "
      f"{real_fr['fake_3c'].sum():5d} {real_fr['fake_3c'].mean()*100:6.1f}%")
print(f"  {'Next candle low touches below EMA9_low':45} "
      f"{real_fr['low_touch'].sum():5d} {real_fr['low_touch'].mean()*100:6.1f}%")

# Fake vs real return
fakes = real_fr[real_fr['fake_3c']]
reals = real_fr[~real_fr['fake_3c']]
fwd3 = pd.to_numeric(df.loc[fr.index, 'close'].reindex(fr.index), errors='coerce')
fr_fwd = g['close'].transform(lambda x: x.shift(-3)).reindex(fr.index)

# Compute returns directly
v = df[df['close']>0].iloc[0]
def get_ret(sub):
    close_vals = sub['close']
    fwd_vals   = sub['close_3c']
    valid = fwd_vals.notna()
    r = fwd_vals[valid] - close_vals[valid]
    return r

r_fake = get_ret(fakes)
r_real = get_ret(reals)

print(f"\n  FAKE breaks (fall below EMA9_low within 3c): n={len(fakes)}")
if len(r_fake)>0:
    print(f"    avg={r_fake.mean():+.1f}  win={(r_fake>0).mean()*100:.1f}%  "
          f"ESL={(r_fake<-12).mean()*100:.1f}%")
print(f"\n  REAL breaks (stay above EMA9_low for 3c): n={len(reals)}")
if len(r_real)>0:
    print(f"    avg={r_real.mean():+.1f}  win={(r_real>0).mean()*100:.1f}%  "
          f"ESL={(r_real<-12).mean()*100:.1f}%")

# What distinguishes fake from real?
print(f"\n  FAKE vs REAL entry characteristics:")
print(f"  {'Feature':20} {'REAL avg':>10} {'FAKE avg':>10} {'diff':>8}")
print(f"  {'─'*52}")
for col, label in [('rsi','RSI'),('bw','BW'),('slope1','slope1'),
                   ('close','entry price'),('rsi_prev','RSI prev')]:
    if col in real_fr.columns:
        rv = reals[col].mean()
        fv = fakes[col].mean()
        print(f"  {label:20} {rv:10.2f} {fv:10.2f} {fv-rv:+8.2f}")

# ── PART 2: Ladder simulation ─────────────────────────────────────
def ladder_sl(entry, peak):
    if peak >= 50: return entry + 50
    if peak >= 40: return entry + 36
    if peak >= 36: return entry + 30
    if peak >= 30: return entry + 20
    if peak >= 24: return entry + 12
    if peak >= 12: return entry + 4
    return entry - 12

def simulate_ladder(entry, candles, max_c=30):
    peak = 0.0
    for i, (o, h, l, c) in enumerate(candles[:max_c]):
        profit = h - entry
        if profit > peak:
            peak = profit
        sl = ladder_sl(entry, peak)
        if l <= sl:
            return max(sl, l), peak, i+1, 'SL'
    if candles:
        last = candles[min(len(candles),max_c)-1]
        return last[3], peak, min(len(candles),max_c), 'MAX'
    return entry, 0, 0, 'NO_DATA'

def simulate_fixed9(entry, candles):
    if len(candles) >= 3:
        return candles[2][3], 3, 'FIXED9'
    elif candles:
        return candles[-1][3], len(candles), 'FIXED9'
    return entry, 0, 'NO_DATA'

def run_pnl(signals, label):
    sym_data = {}
    for (strike, typ), grp in df.groupby(['strike','type']):
        sym_data[(strike,typ)] = grp[['timestamp','open','high','low','close']].reset_index(drop=True)

    trades = []
    for _, row in signals.iterrows():
        key  = (row['strike'], row['type'])
        ts   = row['timestamp']
        entry= row['close']
        if key not in sym_data: continue
        sym  = sym_data[key]
        future = sym[sym['timestamp'] > ts]
        candles= list(future[['open','high','low','close']].itertuples(index=False,name=None))
        if not candles: continue

        ep_lad, peak, nc_lad, r_lad = simulate_ladder(entry, candles)
        ep_f9,  nc_f9, r_f9        = simulate_fixed9(entry, candles)

        trades.append({
            'date':     ts.date(),
            'ts':       ts,
            'entry':    entry,
            'ret_lad':  ep_lad - entry,
            'ret_f9':   ep_f9  - entry,
            'peak':     peak,
            'reason':   r_lad,
        })

    return pd.DataFrame(trades)

print(f"\n{BAR}")
print("  PART 2 — SIMULATING EXIT LADDER (1 lot = 50 units, entry at signal close)")
print(BAR)
print("\n  Running simulations...", flush=True)

tr_fresh = run_pnl(fresh_rsi, "Fresh+RSI50-65")
tr_v9    = run_pnl(v9_full,   "V9 full")

LOT = 50  # NIFTY lot size

def print_pnl(tr, label):
    if tr.empty: return
    rl = tr['ret_lad']
    rf = tr['ret_f9']
    print(f"\n  ── {label} ──")
    print(f"  Trades: {len(tr)} over {tr['date'].nunique()} days "
          f"({len(tr)/tr['date'].nunique():.1f}/day)")
    print(f"\n  {'Method':15} {'total pts':>10} {'per trade':>10} "
          f"{'win%':>7} {'ESL%':>7} {'total ₹(1lot)':>14}")
    print(f"  {'─'*68}")
    for name, r in [('LADDER', rl), ('FIXED_9', rf)]:
        total_pts = r.sum()
        per_trade = r.mean()
        win_pct   = (r>0).mean()*100
        esl_pct   = (r<-12).mean()*100
        total_inr = total_pts * LOT
        print(f"  {name:15} {total_pts:+9.1f}  {per_trade:+9.1f}  "
              f"{win_pct:6.1f}%  {esl_pct:6.1f}%  ₹{total_inr:+12.0f}")

print_pnl(tr_fresh, "FRESH BREAK + RSI 50-65")
print_pnl(tr_v9,    "V9 FULL (current)")

# ── PART 3: Day-by-day P&L ───────────────────────────────────────
print(f"\n{BAR}")
print("  PART 3 — DAY-BY-DAY P&L (LADDER exit, 1 lot)")
print(BAR)
print(f"\n  {'Date':12} {'FR sig':>6} {'FR pts':>8} {'FR ₹':>10}  "
      f"{'V9 sig':>6} {'V9 pts':>8} {'V9 ₹':>10}")
print(f"  {'─'*70}")

all_dates = sorted(set(list(tr_fresh['date'].unique()) + list(tr_v9['date'].unique())))
fr_cum = 0; v9_cum = 0
for d in all_dates:
    fr_day = tr_fresh[tr_fresh['date']==d]['ret_lad'] if not tr_fresh.empty else pd.Series(dtype=float)
    v9_day = tr_v9[tr_v9['date']==d]['ret_lad']     if not tr_v9.empty    else pd.Series(dtype=float)
    fr_pts = fr_day.sum(); v9_pts = v9_day.sum()
    fr_cum += fr_pts;     v9_cum += v9_pts
    print(f"  {str(d):12} {len(fr_day):6d} {fr_pts:+7.1f}  ₹{fr_pts*LOT:+8.0f}  "
          f"{len(v9_day):6d} {v9_pts:+7.1f}  ₹{v9_pts*LOT:+8.0f}")

print(f"  {'─'*70}")
print(f"  {'TOTAL':12} {len(tr_fresh):6d} {tr_fresh['ret_lad'].sum():+7.1f}  "
      f"₹{tr_fresh['ret_lad'].sum()*LOT:+8.0f}  "
      f"{len(tr_v9):6d} {tr_v9['ret_lad'].sum():+7.1f}  "
      f"₹{tr_v9['ret_lad'].sum()*LOT:+8.0f}")

# ── PART 4: Can we spot fake breaks before entry? ─────────────────
print(f"\n{BAR}")
print("  PART 4 — CAN WE FILTER FAKE BREAKS BEFORE ENTRY?")
print("  (features at entry that predict fake vs real)")
print(BAR)

real_fr2 = real_fr.dropna(subset=['close_3c'])
real_fr2 = real_fr2.copy()
real_fr2['ret_3c'] = real_fr2['close_3c'] - real_fr2['close']
real_fr2['is_fake'] = real_fr2['fake_3c']

print(f"\n  Testing pre-entry filters on {len(real_fr2)} fresh+RSI50-65 signals:")
print(f"\n  {'Filter':45} {'n':>5} {'fake%':>7} {'avg ret':>8} {'win%':>7}")
print(f"  {'─'*68}")

def frow(label, sub):
    if len(sub) < 5: return
    fk = sub['is_fake'].mean()*100
    r  = sub['ret_3c']
    print(f"  {label:45} {len(sub):5d} {fk:6.1f}%  {r.mean():+7.1f}  "
          f"{(r>0).mean()*100:6.1f}%")

frow("All fresh+RSI50-65", real_fr2)
frow("+ BW < 13 (narrow band)", real_fr2[real_fr2['bw']<13])
frow("+ BW 13-15", real_fr2[(real_fr2['bw']>=13)&(real_fr2['bw']<15)])
frow("+ BW 15-17", real_fr2[(real_fr2['bw']>=15)&(real_fr2['bw']<17)])
frow("+ BW >= 17 (wide band)", real_fr2[real_fr2['bw']>=17])
frow("+ RSI 50-55", real_fr2[(real_fr2['rsi']>50)&(real_fr2['rsi']<55)])
frow("+ RSI 55-60", real_fr2[(real_fr2['rsi']>55)&(real_fr2['rsi']<60)])
frow("+ RSI 60-65", real_fr2[(real_fr2['rsi']>60)&(real_fr2['rsi']<65)])
frow("+ slope1 > 0", real_fr2[real_fr2['slope1']>0])
frow("+ slope1 <= 0", real_fr2[real_fr2['slope1']<=0])
frow("+ green candle", real_fr2[real_fr2['green']])
frow("+ red candle", real_fr2[~real_fr2['green']])
frow("+ body > 3pts", real_fr2[(real_fr2['close']-real_fr2['open'])>3])
frow("+ body 1-3pts", real_fr2[(real_fr2['close']-real_fr2['open']).between(1,3)])

# ── PART 5: Max drawdown and streak analysis ──────────────────────
print(f"\n{BAR}")
print("  PART 5 — RISK PROFILE (max loss streak, drawdown)")
print(BAR)

for label, tr in [("Fresh+RSI50-65 LADDER", tr_fresh['ret_lad']),
                  ("V9 full LADDER",        tr_v9['ret_lad'])]:
    if len(tr) == 0: continue
    cum = tr.cumsum()
    dd  = (cum - cum.cummax()).min()

    # Max consecutive losses
    losses = (tr < 0).astype(int)
    max_streak = max((sum(1 for _ in g2) for k, g2 in
                      __import__('itertools').groupby(losses) if k), default=0)

    # Max single loss
    worst = tr.min()
    best  = tr.max()

    print(f"\n  {label}:")
    print(f"    Total trades:         {len(tr)}")
    print(f"    Max drawdown:         {dd*LOT:+.0f} pts  (₹{dd*LOT:.0f})")
    print(f"    Max consecutive loss: {max_streak}")
    print(f"    Worst single trade:   {worst:+.1f} pts  (₹{worst*LOT:.0f})")
    print(f"    Best single trade:    {best:+.1f} pts  (₹{best*LOT:.0f})")
    print(f"    Avg win:              {tr[tr>0].mean():+.1f} pts")
    print(f"    Avg loss:             {tr[tr<0].mean():+.1f} pts")
    print(f"    Win/Loss ratio:       {abs(tr[tr>0].mean()/tr[tr<0].mean()):.2f}x")

print(f"\n  Note: {DAYS} days. LOT=50. Ladder simulated on 3-min OHLC.")
print(f"  Actual P&L depends on lot count and slippage.")
