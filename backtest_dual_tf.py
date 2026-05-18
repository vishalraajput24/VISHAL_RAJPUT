"""
Backtest: Dual-TF entry — 3-min alignment + 1-min EMA9_high breakout
Uses real 1-min option data from the production DB.

Run on server:
    python3 ~/VISHAL_RAJPUT/backtest_dual_tf.py
"""
import sqlite3, os, sys
import pandas as pd
import numpy as np

DB = os.path.expanduser("~/lab_data/vrl_data.db")
if not os.path.exists(DB):
    sys.exit(f"DB not found: {DB}")

con = sqlite3.connect(DB)

# ── Load 1-min option data ──────────────────────────────────────────
print("Loading 1-min option data...", flush=True)
df1 = pd.read_sql("""
    SELECT timestamp, token, strike, type, open, high, low, close, volume
    FROM option_1min
    WHERE time(timestamp) >= '09:15:00' AND time(timestamp) < '15:30:00'
    ORDER BY timestamp
""", con, parse_dates=['timestamp'])

print(f"1-min rows: {len(df1)} | dates: {df1['timestamp'].dt.date.nunique()}")

# ── Load 3-min option data ──────────────────────────────────────────
print("Loading 3-min option data...", flush=True)
df3 = pd.read_sql("""
    SELECT timestamp, token, strike, type, open, high, low, close, volume
    FROM option_3min
    WHERE time(timestamp) >= '09:15:00' AND time(timestamp) < '15:30:00'
    ORDER BY timestamp
""", con, parse_dates=['timestamp'])

print(f"3-min rows: {len(df3)} | dates: {df3['timestamp'].dt.date.nunique()}")
con.close()


# ── Add EMA9 bands + RSI to any OHLC df ────────────────────────────
def add_ema9(df, span=9):
    g = df.groupby(['token','type'])
    df['ema9_high'] = g['high'].transform(lambda x: x.ewm(span=span, adjust=False).mean())
    df['ema9_low']  = g['low'].transform(lambda x: x.ewm(span=span, adjust=False).mean())
    df['ema9_mid']  = (df['ema9_high'] + df['ema9_low']) / 2
    return df

def add_rsi(df, period=14):
    def _rsi(s):
        d = s.diff()
        gain = d.clip(lower=0).ewm(com=period-1, adjust=False).mean()
        loss = (-d.clip(upper=0)).ewm(com=period-1, adjust=False).mean()
        rs = gain / loss.replace(0, np.nan)
        return 100 - 100 / (1 + rs)
    df['rsi'] = df.groupby(['token','type'])['close'].transform(_rsi)
    return df

print("Computing indicators...", flush=True)
df1 = add_ema9(df1)
df3 = add_ema9(df3)
df3 = add_rsi(df3)

# 3-min EMA9_low slope
df3['ema9l_slope'] = df3.groupby(['token','type'])['ema9_low'].diff()

# ── 3-min forward return (3 candles = 9 min) ────────────────────────
df3 = df3.sort_values(['token','type','timestamp'])
df3['fwd_close'] = df3.groupby(['token','type'])['close'].shift(-3)
df3['ret_3c']    = df3['fwd_close'] - df3['close']

# ── Time filter ─────────────────────────────────────────────────────
t3 = df3['timestamp']
df3 = df3[
    (t3.dt.time >= pd.Timestamp('09:45').time()) &
    (t3.dt.time <  pd.Timestamp('15:00').time())
].copy().dropna(subset=['ret_3c', 'ema9l_slope', 'rsi'])

# ── 3-min alignment gates ───────────────────────────────────────────
def align_3m(d):
    return (
        (d['close'] > d['open'])        &  # green candle
        (d['close'] > d['ema9_low'])    &  # above support band
        (d['ema9l_slope'] >= 0)         &  # EMA9_low rising
        (d['rsi'] > 45) & (d['rsi'] < 75) # RSI in range
    )

# ── CURRENT strategy: enter at 3-min close ─────────────────────────
cur = df3[align_3m(df3)].copy()

# ── DUAL-TF: for each 3-min aligned candle, find the 1-min
#    candle within that 3-min window where 1-min close > 1-min EMA9_high
# ──────────────────────────────────────────────────────────────────────
print("Running dual-TF backtest...", flush=True)

# Tag each 1-min row with its parent 3-min bucket
df1['bucket'] = df1['timestamp'].dt.floor('3min')

# Merge 3-min alignment info onto 1-min rows
df3_align = df3[align_3m(df3)][['token','type','timestamp','ema9_low','ema9_high','ret_3c','rsi']].copy()
df3_align = df3_align.rename(columns={
    'timestamp':  'bucket',
    'ema9_low':   '3m_ema9_low',
    'ema9_high':  '3m_ema9_high',
    'ret_3c':     '3m_ret',
    'rsi':        '3m_rsi',
})

df1m = df1.merge(df3_align, on=['token','type','bucket'], how='inner')

# 1-min trigger: close > 1-min EMA9_high (breakout above band)
trigger = df1m[df1m['close'] > df1m['ema9_high']].copy()

# For each bucket, take the FIRST triggered 1-min candle
trigger = trigger.sort_values(['token','type','bucket','timestamp'])
first_trigger = trigger.groupby(['token','type','bucket']).first().reset_index()

# Entry price = 1-min close (where trigger fired)
first_trigger['entry_1m']  = first_trigger['close']
first_trigger['ret_1m']    = first_trigger['3m_ret'] + (first_trigger['3m_ema9_high'] - first_trigger['entry_1m'])
# More accurate: fwd = 3m_fwd_close - entry_1m
# But since we only have 3-min forward close, use: ret = fwd_close_3m - entry_1m
# 3m_ret = fwd_close_3m - 3m_close  →  fwd_close_3m = 3m_close + 3m_ret ... need 3m_close
# Let's merge 3m_close back
df3_close = df3[['token','type','timestamp','close']].rename(columns={'timestamp':'bucket','close':'3m_close'})
first_trigger = first_trigger.merge(df3_close, on=['token','type','bucket'], how='left')
first_trigger['fwd_close_3m'] = first_trigger['3m_close'] + first_trigger['3m_ret']
first_trigger['ret_1m']       = first_trigger['fwd_close_3m'] - first_trigger['entry_1m']
first_trigger['saving']       = first_trigger['3m_close'] - first_trigger['entry_1m']


# ── Results ─────────────────────────────────────────────────────────
def show(d, label, ret_col):
    n    = len(d)
    avg  = d[ret_col].mean()
    med  = d[ret_col].median()
    win  = (d[ret_col] > 0).mean() * 100
    esl  = (d[ret_col] < -12).mean() * 100
    big  = (d[ret_col] > 20).mean() * 100
    print(f"\n{'━'*58}")
    print(f"  {label}  (n={n})")
    print(f"{'━'*58}")
    print(f"  Avg return      : {avg:+.1f} pts")
    print(f"  Median return   : {med:+.1f} pts")
    print(f"  Win  rate (>0)  : {win:.1f}%")
    print(f"  ESL  rate (<-12): {esl:.1f}%")
    print(f"  Big win  (>20)  : {big:.1f}%")

show(cur,           "CURRENT  — 3-min close entry",          'ret_3c')
show(first_trigger, "DUAL-TF  — 1-min EMA9_high breakout",  'ret_1m')

print(f"\n  Avg pts saved vs 3-min close: {first_trigger['saving'].mean():+.1f}")
print(f"  % signals where 1-min trigger fires: "
      f"{len(first_trigger)/len(cur)*100:.1f}% of current signals")

print(f"\n  Saving distribution:")
for s in [3, 5, 10, 15, 20]:
    pct = (first_trigger['saving'] >= s).mean() * 100
    if pct > 0:
        avg_s = first_trigger.loc[first_trigger['saving'] >= s, 'saving'].mean()
        print(f"    saving >= {s:2d} pts: {pct:4.1f}% | avg saving {avg_s:.1f} pts")

print(f"\n  Day-by-day:")
print(f"  {'Date':12} {'Cur n':>6} {'Cur avg':>9} {'Dual n':>7} {'Dual avg':>9} {'Saving':>8}")
for date in sorted(cur['timestamp'].dt.date.unique()):
    c  = cur[cur['timestamp'].dt.date == date]
    d2 = first_trigger[first_trigger['bucket'].dt.date == date]
    if len(c) > 0 or len(d2) > 0:
        print(f"  {str(date):12} {len(c):6d} {c['ret_3c'].mean():+9.1f} "
              f"{len(d2):7d} {d2['ret_1m'].mean():+9.1f} {d2['saving'].mean():+8.1f}")
