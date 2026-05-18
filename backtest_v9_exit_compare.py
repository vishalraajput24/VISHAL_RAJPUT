"""
V9 Exit Method Comparison
Simulates 6 exit strategies on V9 signals using 3-min OHLC candles.

Methods:
  1. LADDER     — current trailing SL ladder (peak-based)
  2. FIXED_9    — exit after 9 min (3 candles)
  3. FIXED_18   — exit after 18 min (6 candles)
  4. FIXED_30   — exit after 30 min (10 candles)
  5. TP15_SL12  — exit at +15 profit OR -12 loss, whichever first
  6. EARLY_EXIT — check at 1-candle: if ret < -6 exit, else hold 9 min
  7. EARLY_LADR — check at 1-candle: if ret < -6 exit, else use ladder

Ladder definition (from CLAUDE.md):
  Peak <  12 → SL = entry - 12
  Peak >= 12 → SL = entry + 4
  Peak >= 24 → SL = entry + 12
  Peak >= 30 → SL = entry + 20
  Peak >= 36 → SL = entry + 30
  Peak >= 40 → SL = entry + 36
  Peak >= 50 → SL = entry + 50

Simulation assumption: candle OHLC, optimistic = high first then low
(actual would be tick-based; this is an approximation)

Run: bash ~/VISHAL_RAJPUT/run_backtest.sh v9_exit_compare
"""
import sqlite3, os, sys
import pandas as pd
import numpy as np

DB = os.path.expanduser("~/lab_data/vrl_data.db")
if not os.path.exists(DB):
    sys.exit(f"DB not found: {DB}")

con = sqlite3.connect(DB)
print("Loading 3-min data...", flush=True)
df = pd.read_sql("""
    SELECT timestamp, strike, type, open, high, low, close,
           rsi, ema9_high, ema9_low
    FROM option_3min
    WHERE time(timestamp) >= '09:18:00' AND time(timestamp) < '15:30:00'
    ORDER BY strike, type, timestamp
""", con, parse_dates=['timestamp'])
con.close()

df = df.sort_values(['strike','type','timestamp']).copy()
g = df.groupby(['strike','type'])

df['rsi_prev']   = g['rsi'].transform(lambda x: x.shift(1))
df['ema9l_prev'] = g['ema9_low'].transform(lambda x: x.shift(1))
df['ema9l_prev2']= g['ema9_low'].transform(lambda x: x.shift(2))
df['bw']         = df['ema9_high'] - df['ema9_low']
df['slope1']     = df['ema9_low'] - df['ema9l_prev']
df['slope2']     = df['ema9l_prev'] - df['ema9l_prev2']
df['rsi_delta']  = df['rsi'] - df['rsi_prev']
df['hhmm']       = df['timestamp'].dt.hour*60 + df['timestamp'].dt.minute

df = df.dropna(subset=['rsi_prev','ema9l_prev','bw'])

# Build per-symbol candle index for fast lookup
df['idx'] = df.groupby(['strike','type']).cumcount()
df = df.reset_index(drop=True)

DAYS = df['timestamp'].dt.date.nunique()
print(f"Rows: {len(df)} | Days: {DAYS}\n", flush=True)

# ── Ladder SL computation ─────────────────────────────────────────
def ladder_sl(entry, peak_profit):
    if peak_profit >= 50: return entry + 50
    if peak_profit >= 40: return entry + 36
    if peak_profit >= 36: return entry + 30
    if peak_profit >= 30: return entry + 20
    if peak_profit >= 24: return entry + 12
    if peak_profit >= 12: return entry + 4
    return entry - 12  # initial SL

# ── Simulate one trade given a sequence of candles ────────────────
def simulate(entry, candles, method, early_threshold=-6):
    """
    candles: list of (open, high, low, close) after entry candle
    Returns: (exit_price, exit_reason, candles_held)
    Assumption: within each candle, high comes before low (optimistic).
    For SL simulation, we use low to check SL hit.
    For TP simulation, we use high to check TP hit.
    """
    if not candles:
        return entry, 'NO_DATA', 0

    peak_profit = 0.0

    # FIXED TIME exits
    if method == 'FIXED_9':
        c = candles[2] if len(candles) >= 3 else candles[-1]
        return c[3], 'TIME_9', min(3, len(candles))  # close of candle 3
    if method == 'FIXED_18':
        c = candles[5] if len(candles) >= 6 else candles[-1]
        return c[3], 'TIME_18', min(6, len(candles))
    if method == 'FIXED_30':
        c = candles[9] if len(candles) >= 10 else candles[-1]
        return c[3], 'TIME_30', min(10, len(candles))

    # EARLY EXIT: check after candle 1
    if method in ('EARLY_EXIT', 'EARLY_LADR'):
        if candles:
            c1_close = candles[0][3]
            ret_1c = c1_close - entry
            if ret_1c < early_threshold:
                return c1_close, 'EARLY_CUT', 1
        # Else continue with ladder or fixed
        if method == 'EARLY_EXIT':
            # hold to 9min
            c = candles[2] if len(candles) >= 3 else candles[-1]
            return c[3], 'TIME_9', min(3, len(candles))
        # EARLY_LADR: fall through to ladder below
        start_idx = 1
    else:
        start_idx = 0

    # LADDER or TP15_SL12
    MAX_CANDLES = 20  # max hold = 60 min
    for i in range(start_idx, min(len(candles), MAX_CANDLES)):
        o, h, l, c = candles[i]

        if method == 'TP15_SL12':
            tp = entry + 15
            sl = entry - 12
            if h >= tp:
                return tp, 'TP', i+1
            if l <= sl:
                return sl, 'SL', i+1
            if i == 2:  # also exit at 9min if no TP/SL
                return c, 'TIME_9', i+1
            continue

        # LADDER or EARLY_LADR
        # Check if high extends peak
        high_profit = h - entry
        if high_profit > peak_profit:
            peak_profit = high_profit

        sl = ladder_sl(entry, peak_profit)

        # Check if low hits SL
        if l <= sl:
            exit_price = max(sl, l)  # could gap through
            return exit_price, f'SL_{peak_profit:.0f}pk', i+1

    # Max hold reached — exit at last close
    last = candles[min(len(candles), MAX_CANDLES)-1]
    return last[3], 'MAX_HOLD', min(len(candles), MAX_CANDLES)

# ── Extract V9 signals ────────────────────────────────────────────
signals = df[
    (df['close'] > df['open']) &
    (df['close'] > df['ema9_low']) &
    (df['slope1'] >= 0) & (df['slope2'] >= 0) &
    (df['bw'] >= 13) & (df['bw'] <= 17) &
    (df['rsi'] > 50) & (df['rsi'] < 65) &
    (df['rsi'] > df['rsi_prev'])
].copy()

# Also build filtered set
signals_flt = signals[
    (~((signals['hhmm'] < 9*60+45))) &
    (~((signals['hhmm'] >= 13*60+45) & (signals['hhmm'] < 14*60+15))) &
    (signals['bw'] <= 15) &
    (signals['rsi_delta'] <= 3.0)
].copy()

print(f"V9 signals: {len(signals)} | Filtered: {len(signals_flt)}\n", flush=True)

METHODS = ['LADDER','FIXED_9','FIXED_18','FIXED_30','TP15_SL12','EARLY_EXIT','EARLY_LADR']

# ── Run simulation ────────────────────────────────────────────────
def run_simulation(sig_df, label):
    results = {m: [] for m in METHODS}

    # Build lookup: for each row, get subsequent candles by same strike/type
    sym_candles = {}
    for (strike, typ), grp in df.groupby(['strike','type']):
        key = (strike, typ)
        sym_candles[key] = grp[['timestamp','open','high','low','close']].reset_index(drop=True)

    total = len(sig_df)
    for i, (_, row) in enumerate(sig_df.iterrows()):
        if i % 100 == 0:
            print(f"  [{label}] {i}/{total}...", flush=True)

        key = (row['strike'], row['type'])
        entry = row['close']
        ts    = row['timestamp']

        if key not in sym_candles:
            continue

        sym = sym_candles[key]
        # candles AFTER entry
        future = sym[sym['timestamp'] > ts]
        candle_list = list(future[['open','high','low','close']].itertuples(index=False, name=None))

        if not candle_list:
            continue

        for method in METHODS:
            ep, reason, n_held = simulate(entry, candle_list, method)
            ret = ep - entry
            results[method].append({
                'date':   ts.date(),
                'entry':  entry,
                'exit':   ep,
                'ret':    ret,
                'reason': reason,
                'n_held': n_held,
            })

    return {m: pd.DataFrame(v) for m, v in results.items() if v}

print("Simulating V9 current gates...", flush=True)
res_v9  = run_simulation(signals,     'V9')
print("Simulating V9 filtered gates...", flush=True)
res_flt = run_simulation(signals_flt, 'FLT')

# ── Print results ─────────────────────────────────────────────────
BAR = '━' * 80

def print_summary(res, title):
    print(f"\n{BAR}")
    print(f"  {title}")
    print(BAR)
    print(f"\n  {'Method':15} {'n':>5} {'avg':>8} {'win%':>7} {'ESL%':>7} {'big%':>7} {'avg_hold':>9}")
    print(f"  {'─'*65}")
    rows = []
    for m in METHODS:
        if m not in res or res[m].empty:
            continue
        r = res[m]['ret']
        h = res[m]['n_held']
        row_data = dict(
            method=m, n=len(r),
            avg=r.mean(), win=(r>0).mean()*100,
            esl=(r<-12).mean()*100, big=(r>12).mean()*100,
            hold=h.mean()
        )
        rows.append(row_data)

    # Sort by avg descending
    rows.sort(key=lambda x: x['avg'], reverse=True)
    for rd in rows:
        print(f"  {rd['method']:15} {rd['n']:5d} {rd['avg']:+7.1f}  "
              f"{rd['win']:6.1f}%  {rd['esl']:6.1f}%  {rd['big']:6.1f}%  "
              f"{rd['hold']:8.1f}c")

    # Reason breakdown for ladder
    if 'LADDER' in res and not res['LADDER'].empty:
        print(f"\n  LADDER exit reasons:")
        rc = res['LADDER']['reason'].value_counts()
        for reason, cnt in rc.items():
            pct = cnt/len(res['LADDER'])*100
            r_sub = res['LADDER'][res['LADDER']['reason']==reason]['ret']
            print(f"    {reason:20} {cnt:4d} ({pct:5.1f}%)  avg={r_sub.mean():+.1f}")

    if 'EARLY_LADR' in res and not res['EARLY_LADR'].empty:
        print(f"\n  EARLY_LADR exit reasons:")
        rc = res['EARLY_LADR']['reason'].value_counts()
        for reason, cnt in rc.items():
            pct = cnt/len(res['EARLY_LADR'])*100
            r_sub = res['EARLY_LADR'][res['EARLY_LADR']['reason']==reason]['ret']
            print(f"    {reason:20} {cnt:4d} ({pct:5.1f}%)  avg={r_sub.mean():+.1f}")

print_summary(res_v9,  "V9 CURRENT GATES — ALL 6 EXIT METHODS")
print_summary(res_flt, "V9 FILTERED (good_window + BW<=15 + delta<=3) — ALL 6 EXIT METHODS")

# ── Head to head: Ladder vs best other ───────────────────────────
print(f"\n{BAR}")
print("  HEAD TO HEAD: LADDER vs EARLY_LADR — DAY BY DAY (filtered signals)")
print(BAR)
print(f"\n  {'Date':12} {'LAD n':>6} {'LAD avg':>8} {'LAD ESL':>8}  "
      f"{'E_LAD n':>7} {'E_LAD avg':>9} {'E_LAD ESL':>10}")
print(f"  {'─'*72}")

lad = res_flt.get('LADDER', pd.DataFrame())
elad= res_flt.get('EARLY_LADR', pd.DataFrame())
if not lad.empty and not elad.empty:
    for d in sorted(lad['date'].unique()):
        l = lad[lad['date']==d]['ret']
        e = elad[elad['date']==d]['ret']
        if len(l)==0 and len(e)==0: continue
        la = l.mean() if len(l)>0 else float('nan')
        ea = e.mean() if len(e)>0 else float('nan')
        le = (l<-12).mean()*100 if len(l)>0 else float('nan')
        ee = (e<-12).mean()*100 if len(e)>0 else float('nan')
        print(f"  {str(d):12} {len(l):6d} {la:+7.1f}  {le:7.1f}%  "
              f"{len(e):7d} {ea:+8.1f}  {ee:9.1f}%")

# ── Return distribution comparison ───────────────────────────────
print(f"\n{BAR}")
print("  RETURN DISTRIBUTION: LADDER vs EARLY_LADR vs FIXED_9 (filtered)")
print(BAR)
bins  = [(-999,-30),(-30,-20),(-20,-12),(-12,-6),(-6,0),(0,6),(6,12),(12,20),(20,30),(30,999)]
blabs = ['<-30','-30:-20','-20:-12','-12:-6','-6:0','0:6','6:12','12:20','20:30','>30']

methods_show = ['LADDER','EARLY_LADR','FIXED_9']
print(f"\n  {'Bucket':12}", end='')
for m in methods_show:
    print(f"  {m:>12}", end='')
print()
print(f"  {'─'*55}")

for (lo,hi), lab in zip(bins, blabs):
    print(f"  {lab:12}", end='')
    for m in methods_show:
        if m not in res_flt or res_flt[m].empty:
            print(f"  {'—':>12}", end='')
            continue
        r = res_flt[m]['ret']
        n = ((r>lo) & (r<=hi)).sum()
        pct = n/len(r)*100
        print(f"  {pct:>10.1f}%", end='')
    print()

# ── Cumulative P&L ────────────────────────────────────────────────
print(f"\n{BAR}")
print("  CUMULATIVE P&L (1 lot each trade, filtered signals)")
print(BAR)
print(f"\n  {'Method':15} {'total_pts':>10} {'per_trade':>10} {'max_dd':>10} {'sharpe_proxy':>13}")
print(f"  {'─'*58}")
for m in METHODS:
    if m not in res_flt or res_flt[m].empty: continue
    r = res_flt[m]['ret']
    cumret = r.cumsum()
    total  = r.sum()
    per_t  = r.mean()
    # max drawdown on cumulative
    roll_max = cumret.cummax()
    dd = (cumret - roll_max).min()
    # simple sharpe proxy
    sharpe = r.mean() / r.std() * np.sqrt(len(r)) if r.std()>0 else 0
    print(f"  {m:15} {total:+9.1f}  {per_t:+9.1f}  {dd:+9.1f}  {sharpe:+12.2f}")

print(f"\n  Note: {DAYS} days. Ladder simulated on 3-min OHLC (optimistic: high before low).")
print(f"  Real ladder uses tick data — actual results may differ slightly.")
