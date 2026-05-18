"""
Backtest: BW 13-17 + RSI 55-65 vs current gates (BW>=11, RSI 45-75)

Current V8 gates applied on 3-min:
  G1: Green candle
  G2: Close > EMA9_low
  G2B: EMA9_low slope >= 0
  G3: BW >= 11  (sweep: also test 13-17 tight range)
  G5: RSI 45-75 AND rising  (sweep: also test 55-65 tight range)

Run: python3 ~/VISHAL_RAJPUT/backtest_bw_rsi_filter.py
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
    WHERE time(timestamp) >= '09:45:00' AND time(timestamp) < '15:00:00'
    ORDER BY strike, type, timestamp
""", con, parse_dates=['timestamp'])
con.close()

print(f"Rows: {len(df)} | Days: {df['timestamp'].dt.date.nunique()}")

# ── Indicators ─────────────────────────────────────────────────
df['fwd_3c'] = pd.to_numeric(df['fwd_3c'], errors='coerce')
df = df.sort_values(['strike','type','timestamp'])
df['ema9l_slope'] = df.groupby(['strike','type'])['ema9_low'].diff()
df['rsi_prev']    = df.groupby(['strike','type'])['rsi'].shift(1)
df['bw']          = df['ema9_high'] - df['ema9_low']

# fwd_3c: absolute price → return
valid = df[df['fwd_3c'].notna() & (df['close'] > 0)].iloc[0]
if abs(float(valid['fwd_3c'])) > float(valid['close']) * 0.5:
    df['ret_3c'] = df['fwd_3c'] - df['close']
    print("fwd is absolute price — converted to return")
else:
    df['ret_3c'] = df['fwd_3c']

df = df.dropna(subset=['ret_3c','ema9l_slope','rsi','rsi_prev'])
df['rsi_rising'] = df['rsi'] > df['rsi_prev']

# ── Base gates (G1+G2+G2B always on) ───────────────────────────
base = (
    (df['close'] > df['open'])       &   # G1
    (df['close'] > df['ema9_low'])   &   # G2
    (df['ema9l_slope'] >= 0)             # G2B
)

# ── Define filter sets to compare ──────────────────────────────
filters = {
    "CURRENT  (BW>=11, RSI 45-75)": {
        "bw_min": 11, "bw_max": 999,
        "rsi_lo": 45, "rsi_hi": 75,
    },
    "TIGHT    (BW 13-17, RSI 55-65)": {
        "bw_min": 13, "bw_max": 17,
        "rsi_lo": 55, "rsi_hi": 65,
    },
    "BW tight only (BW 13-17, RSI 45-75)": {
        "bw_min": 13, "bw_max": 17,
        "rsi_lo": 45, "rsi_hi": 75,
    },
    "RSI tight only (BW>=11, RSI 55-65)": {
        "bw_min": 11, "bw_max": 999,
        "rsi_lo": 55, "rsi_hi": 65,
    },
}

def run_filter(f):
    mask = (
        base &
        (df['bw']  >= f['bw_min']) & (df['bw']  <= f['bw_max']) &
        (df['rsi'] >  f['rsi_lo']) & (df['rsi'] <  f['rsi_hi']) &
        df['rsi_rising']
    )
    d = df[mask]
    if len(d) < 10:
        return None
    n   = len(d)
    avg = d['ret_3c'].mean()
    med = d['ret_3c'].median()
    win = (d['ret_3c'] > 0).mean() * 100
    esl = (d['ret_3c'] < -12).mean() * 100
    big = (d['ret_3c'] > 20).mean() * 100
    score = avg - (esl - 40) * 0.5
    return dict(n=n, avg=avg, med=med, win=win, esl=esl, big=big, score=score, data=d)

# ── Summary table ───────────────────────────────────────────────
print(f"\n{'━'*80}")
print(f"  FILTER COMPARISON — 3-min V8 strategy ({df['timestamp'].dt.date.nunique()} days)")
print(f"{'━'*80}")
print(f"  {'Filter':42} {'n':>5} {'avg':>7} {'win%':>7} {'ESL%':>7} {'big%':>7} {'score':>7}")
print(f"  {'─'*78}")

results = {}
for name, f in filters.items():
    r = run_filter(f)
    if r:
        results[name] = r
        print(f"  {name:42} {r['n']:5d} {r['avg']:+7.1f} {r['win']:7.1f} "
              f"{r['esl']:7.1f} {r['big']:7.1f} {r['score']:+7.1f}")

# ── BW sweep within RSI 55-65 ───────────────────────────────────
print(f"\n{'━'*80}")
print(f"  BW SWEEP within RSI 55-65 (finding best BW range)")
print(f"{'━'*80}")
print(f"  {'BW range':20} {'n':>5} {'avg':>7} {'win%':>7} {'ESL%':>7} {'big%':>7} {'score':>7}")
print(f"  {'─'*78}")

bw_ranges = [
    (11,999), (11,20), (12,20), (13,20),
    (13,17),  (13,16), (14,17), (14,16),
    (15,20),  (15,18),
]
for blo, bhi in bw_ranges:
    mask = (
        base &
        (df['bw']  >= blo) & (df['bw'] <= bhi) &
        (df['rsi'] > 55)   & (df['rsi'] < 65)  &
        df['rsi_rising']
    )
    d = df[mask]
    if len(d) < 10:
        continue
    n   = len(d)
    avg = d['ret_3c'].mean()
    win = (d['ret_3c'] > 0).mean() * 100
    esl = (d['ret_3c'] < -12).mean() * 100
    big = (d['ret_3c'] > 20).mean() * 100
    score = avg - (esl - 40) * 0.5
    label = f"BW {blo}-{'∞' if bhi==999 else bhi}"
    print(f"  {label:20} {n:5d} {avg:+7.1f} {win:7.1f} {esl:7.1f} {big:7.1f} {score:+7.1f}")

# ── RSI sweep within BW 13-17 ───────────────────────────────────
print(f"\n{'━'*80}")
print(f"  RSI SWEEP within BW 13-17 (finding best RSI range)")
print(f"{'━'*80}")
print(f"  {'RSI range':20} {'n':>5} {'avg':>7} {'win%':>7} {'ESL%':>7} {'big%':>7} {'score':>7}")
print(f"  {'─'*78}")

rsi_ranges = [
    (45,75), (50,75), (50,70), (50,65),
    (55,75), (55,70), (55,65), (55,60),
    (60,75), (60,70),
]
for rlo, rhi in rsi_ranges:
    mask = (
        base &
        (df['bw']  >= 13) & (df['bw'] <= 17) &
        (df['rsi'] > rlo) & (df['rsi'] < rhi) &
        df['rsi_rising']
    )
    d = df[mask]
    if len(d) < 10:
        continue
    n   = len(d)
    avg = d['ret_3c'].mean()
    win = (d['ret_3c'] > 0).mean() * 100
    esl = (d['ret_3c'] < -12).mean() * 100
    big = (d['ret_3c'] > 20).mean() * 100
    score = avg - (esl - 40) * 0.5
    label = f"RSI {rlo}-{rhi}"
    print(f"  {label:20} {n:5d} {avg:+7.1f} {win:7.1f} {esl:7.1f} {big:7.1f} {score:+7.1f}")

# ── Day-by-day: CURRENT vs TIGHT ────────────────────────────────
if 'CURRENT  (BW>=11, RSI 45-75)' in results and 'TIGHT    (BW 13-17, RSI 55-65)' in results:
    cur  = results['CURRENT  (BW>=11, RSI 45-75)']['data']
    tght = results['TIGHT    (BW 13-17, RSI 55-65)']['data']

    print(f"\n{'━'*72}")
    print(f"  DAY-BY-DAY: CURRENT vs TIGHT (BW 13-17, RSI 55-65)")
    print(f"{'━'*72}")
    print(f"  {'Date':12} {'Cur n':>6} {'Cur avg':>9} {'Cur win':>8} "
          f"{'Tgt n':>6} {'Tgt avg':>9} {'Tgt win':>8}")
    print(f"  {'─'*70}")

    all_dates = sorted(set(cur['timestamp'].dt.date) | set(tght['timestamp'].dt.date))
    for d in all_dates:
        c = cur[cur['timestamp'].dt.date == d]
        t = tght[tght['timestamp'].dt.date == d]
        c_avg = c['ret_3c'].mean() if len(c) else float('nan')
        t_avg = t['ret_3c'].mean() if len(t) else float('nan')
        c_win = (c['ret_3c']>0).mean()*100 if len(c) else float('nan')
        t_win = (t['ret_3c']>0).mean()*100 if len(t) else float('nan')
        print(f"  {str(d):12} {len(c):6d} {c_avg:+9.1f} {c_win:7.1f}% "
              f"{len(t):6d} {t_avg:+9.1f} {t_win:7.1f}%")

    print(f"\n  CURRENT  total n={len(cur):4d}  avg={cur['ret_3c'].mean():+.1f}  "
          f"win={( cur['ret_3c']>0).mean()*100:.1f}%  "
          f"ESL={(cur['ret_3c']<-12).mean()*100:.1f}%  "
          f"big={(cur['ret_3c']>20).mean()*100:.1f}%")
    print(f"  TIGHT    total n={len(tght):4d}  avg={tght['ret_3c'].mean():+.1f}  "
          f"win={(tght['ret_3c']>0).mean()*100:.1f}%  "
          f"ESL={(tght['ret_3c']<-12).mean()*100:.1f}%  "
          f"big={(tght['ret_3c']>20).mean()*100:.1f}%")
