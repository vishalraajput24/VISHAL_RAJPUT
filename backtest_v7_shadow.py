"""
V7 Shadow Signal Analysis
Extracts [V7-SHADOW] log entries and matches with option_3min DB
to compute forward returns — showing signal quality.

Run: bash ~/VISHAL_RAJPUT/run_backtest.sh v7_shadow
     (or: python3 ~/VISHAL_RAJPUT/backtest_v7_shadow.py)
"""
import os, sys, re, sqlite3
from datetime import datetime, timedelta
import pandas as pd

LOG_FILE = os.path.expanduser("~/logs/live/vrl_live.log")
DB       = os.path.expanduser("~/lab_data/vrl_data.db")

if not os.path.exists(LOG_FILE):
    sys.exit(f"Log not found: {LOG_FILE}")
if not os.path.exists(DB):
    sys.exit(f"DB not found: {DB}")

# ── Parse V7-SHADOW log entries ──────────────────────────────────
# Format: 2026-05-18 10:23:45,123 INFO ... [V7-SHADOW] CE 23500 close=210.5 rsi=58.3 ...
pattern = re.compile(
    r'(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}).*\[V7-SHADOW\]\s+'
    r'(CE|PE)\s+(\d+)\s+close=([\d.]+)\s+rsi=([\d.]+)'
)

signals = []
with open(LOG_FILE) as f:
    for line in f:
        m = pattern.search(line)
        if m:
            ts_str, direction, strike, close, rsi = m.groups()
            signals.append({
                'timestamp': pd.Timestamp(ts_str),
                'direction': direction,
                'strike':    int(strike),
                'close':     float(close),
                'rsi':       float(rsi),
            })

if not signals:
    sys.exit("No [V7-SHADOW] entries found in log. Bot may not have fired any V7 signals yet.")

df_sig = pd.DataFrame(signals)
print(f"Found {len(df_sig)} V7-SHADOW signals across {df_sig['timestamp'].dt.date.nunique()} days\n")

# ── Load option_3min around signal timestamps ─────────────────────
con = sqlite3.connect(DB)

# Get all 3-min data for the strikes/dates in signals
dates = df_sig['timestamp'].dt.date.unique()
strikes = df_sig['strike'].unique().tolist()
placeholders = ','.join('?' for _ in strikes)

df_opt = pd.read_sql(f"""
    SELECT timestamp, strike, type, open, high, low, close as opt_close, fwd_3c
    FROM option_3min
    WHERE strike IN ({placeholders})
    AND date(timestamp) IN ({','.join("'" + str(d) + "'" for d in dates)})
    ORDER BY strike, type, timestamp
""", con, params=strikes, parse_dates=['timestamp'])
con.close()

if df_opt.empty:
    sys.exit("No matching option_3min data found in DB for these signals.")

df_opt['fwd_3c'] = pd.to_numeric(df_opt['fwd_3c'], errors='coerce')

# ── Match each signal to its option candle + compute fwd return ───
results = []
for _, sig in df_sig.iterrows():
    sig_ts   = sig['timestamp']
    strike   = sig['strike']
    direction = sig['direction']

    # Find the 15-min candle close that triggered the signal
    # Round signal timestamp down to nearest 15-min boundary
    mins = sig_ts.minute
    bucket_min = (mins // 15) * 15
    bucket_ts  = sig_ts.replace(minute=bucket_min, second=0, microsecond=0)

    # Match the 3-min candle at or just after the 15-min close
    # The 15-min close = end of the 15-min bucket = start of next bucket
    # Look for a 3-min candle within ±5 min of signal time
    mask = (
        (df_opt['strike'] == strike) &
        (df_opt['type'] == direction) &
        (df_opt['timestamp'] >= sig_ts - pd.Timedelta(minutes=3)) &
        (df_opt['timestamp'] <= sig_ts + pd.Timedelta(minutes=3))
    )
    match = df_opt[mask].sort_values('timestamp')
    if match.empty:
        continue

    row = match.iloc[0]
    fwd = pd.to_numeric(row['fwd_3c'], errors='coerce')
    entry_price = sig['close']  # V7 would enter at this close

    if pd.isna(fwd):
        fwd_ret = float('nan')
    elif abs(fwd) > entry_price * 0.5:
        fwd_ret = fwd - entry_price  # absolute → return
    else:
        fwd_ret = fwd

    results.append({
        'date':      sig_ts.date(),
        'time':      sig_ts.strftime('%H:%M'),
        'direction': direction,
        'strike':    strike,
        'entry':     entry_price,
        'rsi':       sig['rsi'],
        'fwd_ret':   fwd_ret,
        'win':       fwd_ret > 0 if not pd.isna(fwd_ret) else None,
        'esl':       fwd_ret < -12 if not pd.isna(fwd_ret) else None,
    })

if not results:
    sys.exit("Could not match any signals to DB candles. Check timestamp alignment.")

df_res = pd.DataFrame(results).dropna(subset=['fwd_ret'])

BAR = '━' * 72

# ── Summary ───────────────────────────────────────────────────────
print(BAR)
print("  V7 SHADOW SIGNAL QUALITY")
print(BAR)
print(f"\n  Total signals matched: {len(df_res)}")
print(f"  Days:                  {df_res['date'].nunique()}")
print(f"  CE signals:            {(df_res['direction']=='CE').sum()}")
print(f"  PE signals:            {(df_res['direction']=='PE').sum()}")

r = df_res['fwd_ret']
print(f"\n  avg return:  {r.mean():+.1f} pts")
print(f"  median:      {r.median():+.1f} pts")
print(f"  win%:        {(r>0).mean()*100:.1f}%")
print(f"  ESL%:        {(r<-12).mean()*100:.1f}%  (lost >12 pts)")
print(f"  best:        {r.max():+.1f}")
print(f"  worst:       {r.min():+.1f}")

# ── By direction ─────────────────────────────────────────────────
print(f"\n{BAR}")
print("  BY DIRECTION")
print(BAR)
for d in ['CE', 'PE']:
    sub = df_res[df_res['direction'] == d]['fwd_ret']
    if len(sub) < 2:
        continue
    print(f"\n  {d}: n={len(sub)}  avg={sub.mean():+.1f}  "
          f"win={( sub>0).mean()*100:.1f}%  ESL={(sub<-12).mean()*100:.1f}%")

# ── Day by day ───────────────────────────────────────────────────
print(f"\n{BAR}")
print("  DAY-BY-DAY")
print(BAR)
print(f"\n  {'Date':12} {'n':>4} {'avg':>8} {'win%':>7} {'ESL%':>7}  signals")
print(f"  {'─'*65}")
for d, grp in df_res.groupby('date'):
    r = grp['fwd_ret']
    sigs = '  '.join(f"{row['direction']}@{row['time']}({row['fwd_ret']:+.0f})"
                     for _, row in grp.iterrows())
    print(f"  {str(d):12} {len(grp):4d} {r.mean():+7.1f}  "
          f"{(r>0).mean()*100:6.1f}%  {(r<-12).mean()*100:6.1f}%  {sigs}")

# ── RSI distribution of signals ──────────────────────────────────
print(f"\n{BAR}")
print("  RSI AT SIGNAL TIME")
print(BAR)
rsi_bins = [(40,50),(50,55),(55,60),(60,65),(65,75)]
print(f"\n  {'RSI range':12} {'n':>5} {'avg ret':>9} {'win%':>7}")
print(f"  {'─'*40}")
for lo, hi in rsi_bins:
    sub = df_res[(df_res['rsi']>lo) & (df_res['rsi']<=hi)]['fwd_ret']
    if len(sub) == 0:
        continue
    print(f"  {lo}-{hi:2d}       {len(sub):5d} {sub.mean():+8.1f}  {(sub>0).mean()*100:6.1f}%")

print(f"\n  Note: V7 uses 15-min candles. Forward return here is")
print(f"  approximated from the nearest 3-min candle in the DB.")
