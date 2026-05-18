"""
Backtest: Next-candle open entry vs current close entry
Signal fires on candle N close (gates pass) → enter at candle N+1 open

Run: python3 ~/VISHAL_RAJPUT/backtest_next_open.py
"""
import sqlite3, os, sys
import pandas as pd
import numpy as np

DB = os.path.expanduser("~/lab_data/vrl_data.db")
if not os.path.exists(DB):
    sys.exit(f"DB not found: {DB}")

con = sqlite3.connect(DB)
print("Loading 3-min option data...", flush=True)
df = pd.read_sql("""
    SELECT timestamp, strike, type, open, high, low, close,
           rsi, ema9_high, ema9_low, fwd_3c
    FROM option_3min
    WHERE time(timestamp) >= '09:15:00' AND time(timestamp) < '15:30:00'
    ORDER BY strike, type, timestamp
""", con, parse_dates=['timestamp'])
con.close()

print(f"Rows: {len(df)} | Days: {df['timestamp'].dt.date.nunique()}")

# ── Cast fwd_3c to numeric ─────────────────────────────────────────
df['fwd_3c'] = pd.to_numeric(df['fwd_3c'], errors='coerce')

# ── EMA9_low slope (1 candle) ──────────────────────────────────────
df = df.sort_values(['strike','type','timestamp'])
df['ema9l_slope'] = df.groupby(['strike','type'])['ema9_low'].diff()

# ── fwd_3c: absolute price → return ───────────────────────────────
valid = df[df['fwd_3c'].notna() & (df['close'] > 0)].iloc[0]
if abs(float(valid['fwd_3c'])) > float(valid['close']) * 0.5:
    df['ret_3c'] = df['fwd_3c'] - df['close']
else:
    df['ret_3c'] = df['fwd_3c']

# ── Next candle open (N+1 open within same series) ─────────────────
df['next_open'] = df.groupby(['strike','type'])['open'].shift(-1)
df['next_ts']   = df.groupby(['strike','type'])['timestamp'].shift(-1)

# Only valid if next candle is exactly 3 min later (same session)
df['gap_min'] = (df['next_ts'] - df['timestamp']).dt.total_seconds() / 60
df['next_open'] = df['next_open'].where(df['gap_min'] == 3, other=np.nan)

# Return from next-candle open entry
# fwd_3c is 3 candles from candle N's close
# From next_open entry, same fwd_close = close_N + ret_3c
df['fwd_close_abs'] = df['close'] + df['ret_3c']
df['ret_next_open'] = df['fwd_close_abs'] - df['next_open']

# ── Time filter ────────────────────────────────────────────────────
t = df['timestamp']
df = df[
    (t.dt.time >= pd.Timestamp('09:45').time()) &
    (t.dt.time <  pd.Timestamp('15:00').time())
].copy().dropna(subset=['ret_3c', 'ema9l_slope', 'rsi', 'ema9_high', 'ema9_low'])

# ── Gates ──────────────────────────────────────────────────────────
bw = df['ema9_high'] - df['ema9_low']

# Current gates (no G3)
gates_base = (
    (df['close'] > df['open'])      &  # G1 green
    (df['close'] > df['ema9_low'])  &  # G2 above band
    (df['ema9l_slope'] >= 0)        &  # G2B slope rising
    (df['rsi'] > 45) & (df['rsi'] < 75)  # G5 RSI
)

# With G3 BW>=11
gates_g3 = gates_base & (bw >= 11)

# ── Run comparison for both gate sets ─────────────────────────────
def compare(mask, label):
    d = df[mask].copy().dropna(subset=['ret_next_open'])
    n_cur  = mask.sum()
    n_next = len(d)

    cur_avg  = df[mask]['ret_3c'].mean()
    cur_win  = (df[mask]['ret_3c'] > 0).mean() * 100
    cur_esl  = (df[mask]['ret_3c'] < -12).mean() * 100
    cur_big  = (df[mask]['ret_3c'] > 20).mean() * 100

    nxt_avg  = d['ret_next_open'].mean()
    nxt_win  = (d['ret_next_open'] > 0).mean() * 100
    nxt_esl  = (d['ret_next_open'] < -12).mean() * 100
    nxt_big  = (d['ret_next_open'] > 20).mean() * 100

    body_avg = (df[mask]['close'] - df[mask]['open']).mean()

    print(f"\n{'━'*62}")
    print(f"  {label}")
    print(f"{'━'*62}")
    print(f"  {'':28s} {'CURRENT':>10} {'NEXT OPEN':>10} {'DIFF':>8}")
    print(f"  {'─'*60}")
    print(f"  {'Signals':28s} {n_cur:10d} {n_next:10d}")
    print(f"  {'Avg return (pts)':28s} {cur_avg:+10.1f} {nxt_avg:+10.1f} {nxt_avg-cur_avg:+8.1f}")
    print(f"  {'Win rate (>0)':28s} {cur_win:9.1f}% {nxt_win:9.1f}%  {nxt_win-cur_win:+7.1f}%")
    print(f"  {'ESL rate (<-12)':28s} {cur_esl:9.1f}% {nxt_esl:9.1f}%  {nxt_esl-cur_esl:+7.1f}%")
    print(f"  {'Big win (>20)':28s} {cur_big:9.1f}% {nxt_big:9.1f}%  {nxt_big-cur_big:+7.1f}%")
    print(f"  {'Avg candle body saved':28s} {body_avg:+10.1f} pts")

    print(f"\n  Day-by-day:")
    print(f"  {'Date':12} {'Cur n':>6} {'Cur avg':>9} {'Next n':>7} {'Next avg':>9} {'Body':>7}")
    for date in sorted(df[mask]['timestamp'].dt.date.unique()):
        c = df[mask][df[mask]['timestamp'].dt.date == date]
        nx = d[d['timestamp'].dt.date == date]
        if len(c) > 0:
            nx_avg = nx['ret_next_open'].mean() if len(nx) > 0 else float('nan')
            body   = (c['close'] - c['open']).mean()
            print(f"  {str(date):12} {len(c):6d} {c['ret_3c'].mean():+9.1f} "
                  f"{len(nx):7d} {nx_avg:+9.1f} {body:+7.1f}")

compare(gates_base, "WITHOUT G3  (G1+G2+G2B+RSI only)")
compare(gates_g3,   "WITH G3 BW>=11 (all gates)")
