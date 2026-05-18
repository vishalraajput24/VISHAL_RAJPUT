"""
V9 Gate Value Analysis — starting from bare EMA9_low break
Does each gate actually add value, or is the base signal already good?

Layers:
  0. close > EMA9_low (bare signal)
  1. + green candle
  2. + RSI > 50
  3. + RSI rising
  4. + slope2 (EMA rising 2 candles)
  5. + BW 13-17
  6. V9 full (all gates)

Also checks: just EMA9_low break with NO other filter — raw edge?

Run: bash ~/VISHAL_RAJPUT/run_backtest.sh v9_gate_value
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

df['fwd_3c']     = g['close'].transform(lambda x: x.shift(-3))
df['rsi_prev']   = g['rsi'].transform(lambda x: x.shift(1))
df['ema9l_prev'] = g['ema9_low'].transform(lambda x: x.shift(1))
df['ema9l_prev2']= g['ema9_low'].transform(lambda x: x.shift(2))
df['bw']         = df['ema9_high'] - df['ema9_low']
df['slope1']     = df['ema9_low'] - df['ema9l_prev']
df['slope2']     = df['ema9l_prev'] - df['ema9l_prev2']
df['rsi_delta']  = df['rsi'] - df['rsi_prev']
df['close_prev'] = g['close'].transform(lambda x: x.shift(1))
df['open_prev']  = g['open'].transform(lambda x: x.shift(1))

df = df.dropna(subset=['fwd_3c','rsi_prev','ema9l_prev','bw'])

v = df[df['fwd_3c'].notna() & (df['close']>0)].iloc[0]
use_abs = abs(float(v['fwd_3c'])) > float(v['close'])*0.5
df['ret'] = df['fwd_3c'] - df['close'] if use_abs else df['fwd_3c']
df = df.dropna(subset=['ret'])

df['green']       = df['close'] > df['open']
df['above_ema9l'] = df['close'] > df['ema9_low']
df['rsi_rising']  = df['rsi'] > df['rsi_prev']
df['prev_above']  = df['close_prev'] > df['ema9l_prev']  # prev candle also above
df['fresh_break'] = df['above_ema9l'] & ~df['prev_above']  # just crossed above

DAYS = df['timestamp'].dt.date.nunique()
BAR  = '━' * 74
MIN_N = 20

print(f"Total candles: {len(df)} | Days: {DAYS}\n")

def st(r):
    if len(r) < MIN_N: return None
    return dict(n=len(r), avg=r.mean(), win=(r>0).mean()*100,
                esl=(r<-12).mean()*100, big=(r>12).mean()*100)

def row(label, r):
    s = st(r)
    if s is None:
        return f"  {label:50} {'<min':>5}"
    return (f"  {label:50} {s['n']:5d} {s['avg']:+7.1f}  "
            f"{s['win']:5.1f}%  {s['esl']:5.1f}%  {s['big']:5.1f}%")

hdr = f"\n  {'Filter':50} {'n':>5} {'avg':>8} {'win%':>6} {'ESL%':>6} {'big%':>6}"
sep = f"  {'─'*76}"

# ── PART 1: Raw EMA9_low break — what is the bare signal? ─────────
print(f"{BAR}")
print("  PART 1 — BARE EMA9_LOW BREAK (no other filters)")
print(BAR); print(hdr); print(sep)

all_c   = df
above   = df[df['above_ema9l']]
below   = df[~df['above_ema9l']]
fresh   = df[df['fresh_break']]          # first candle above (just crossed)
cont    = df[df['above_ema9l'] & df['prev_above']]  # continuing above

print(row("ALL candles (baseline universe)", all_c['ret']))
print(row("close > EMA9_low (any)", above['ret']))
print(row("close <= EMA9_low (below)", below['ret']))
print(row("FRESH break (prev was below, now above)", fresh['ret']))
print(row("Continuing above (prev also above)", cont['ret']))

# ── PART 2: Adding gates one by one ───────────────────────────────
print(f"\n{BAR}")
print("  PART 2 — GATE STACK (each gate adds on previous)")
print(BAR); print(hdr); print(sep)

l0 = df[df['above_ema9l']]
print(row("G0: close > EMA9_low", l0['ret']))

l1 = l0[l0['green']]
print(row("G1: + green candle", l1['ret']))

l2 = l1[l1['rsi'] > 50]
print(row("G2: + RSI > 50", l2['ret']))

l3 = l2[l2['rsi_rising']]
print(row("G3: + RSI rising", l3['ret']))

l4 = l3[(l3['slope1']>=0) & (l3['slope2']>=0)]
print(row("G4: + slope2 (EMA rising 2c)", l4['ret']))

l5 = l4[(l4['bw']>=13) & (l4['bw']<=17)]
print(row("G5: + BW 13-17  [V9 FULL]", l5['ret']))

# ── PART 3: What does each gate actually REMOVE? ──────────────────
print(f"\n{BAR}")
print("  PART 3 — WHAT EACH GATE REMOVES (the rejected trades)")
print(BAR); print(hdr); print(sep)

print(row("Rejected by green (red candles above ema9l)",
    l0[~l0['green']]['ret']))
print(row("Rejected by RSI>50 (RSI<=50 but green+above)",
    l1[l1['rsi']<=50]['ret']))
print(row("Rejected by RSI rising (RSI falling but above+green+rsi>50)",
    l2[~l2['rsi_rising']]['ret']))
print(row("Rejected by slope2 (EMA flat/falling)",
    l3[~((l3['slope1']>=0)&(l3['slope2']>=0))]['ret']))
print(row("Rejected by BW 13-17 (wrong band width)",
    l4[~((l4['bw']>=13)&(l4['bw']<=17))]['ret']))

# ── PART 4: Fresh break vs EMA9_low with each gate ────────────────
print(f"\n{BAR}")
print("  PART 4 — FRESH BREAK ONLY (first candle crossing EMA9_low)")
print("  (this is the purest 'EMA9_low just broke' signal)")
print(BAR); print(hdr); print(sep)

f0 = df[df['fresh_break']]
print(row("Fresh break only", f0['ret']))

f1 = f0[f0['green']]
print(row("+ green", f1['ret']))

f2 = f1[f1['rsi']>50]
print(row("+ RSI>50", f2['ret']))

f3 = f2[f2['rsi_rising']]
print(row("+ RSI rising", f3['ret']))

f4 = f3[(f3['slope1']>=0)&(f3['slope2']>=0)]
print(row("+ slope2", f4['ret']))

f5 = f4[(f4['bw']>=13)&(f4['bw']<=17)]
print(row("+ BW 13-17", f5['ret']))

f6 = f0[(f0['rsi']>50) & (f0['rsi']<65)]
print(row("Fresh break + RSI 50-65 only", f6['ret']))

f7 = f0[f0['rsi']>50]
print(row("Fresh break + RSI>50 only", f7['ret']))

f8 = f0[(f0['bw']>=13)&(f0['bw']<=17)]
print(row("Fresh break + BW 13-17 only", f8['ret']))

# ── PART 5: RSI ranges on bare EMA9_low break ─────────────────────
print(f"\n{BAR}")
print("  PART 5 — RSI RANGES ON BARE EMA9_LOW BREAK (no other gates)")
print(BAR); print(hdr); print(sep)

b = df[df['above_ema9l']]
print(row("EMA9l break, any RSI", b['ret']))
for lo, hi in [(0,40),(40,50),(50,60),(55,65),(60,65),(50,65),(40,65),(65,99)]:
    label = f"EMA9l break + RSI {lo}-{hi if hi<99 else 'all'}"
    sub = b[(b['rsi']>lo) & (b['rsi']<hi)]
    print(row(label, sub['ret']))

# ── PART 6: BW ranges on bare EMA9_low break ──────────────────────
print(f"\n{BAR}")
print("  PART 6 — BW RANGES ON BARE EMA9_LOW BREAK (no other gates)")
print(BAR); print(hdr); print(sep)

print(row("EMA9l break, any BW", b['ret']))
for lo, hi in [(0,10),(10,13),(13,15),(13,17),(15,17),(17,25),(25,99)]:
    label = f"EMA9l break + BW {lo}-{hi if hi<99 else 'all'}"
    sub = b[(b['bw']>=lo) & (b['bw']<hi)] if hi<99 else b[b['bw']>=lo]
    print(row(label, sub['ret']))

# ── PART 7: Simplest possible positive edge ────────────────────────
print(f"\n{BAR}")
print("  PART 7 — SIMPLEST COMBO WITH POSITIVE EDGE (2-gate max)")
print(BAR); print(hdr); print(sep)

combos = {
    'EMA9l break only':
        df[df['above_ema9l']],
    'EMA9l + RSI 50-65':
        df[df['above_ema9l'] & (df['rsi']>50) & (df['rsi']<65)],
    'EMA9l + RSI 60-65':
        df[df['above_ema9l'] & (df['rsi']>60) & (df['rsi']<65)],
    'EMA9l + BW 13-17':
        df[df['above_ema9l'] & (df['bw']>=13) & (df['bw']<=17)],
    'EMA9l + BW 13-15':
        df[df['above_ema9l'] & (df['bw']>=13) & (df['bw']<=15)],
    'EMA9l + green':
        df[df['above_ema9l'] & df['green']],
    'EMA9l + fresh break':
        df[df['fresh_break']],
    'Fresh break + BW 13-17':
        df[df['fresh_break'] & (df['bw']>=13) & (df['bw']<=17)],
    'Fresh break + RSI 50-65':
        df[df['fresh_break'] & (df['rsi']>50) & (df['rsi']<65)],
    'Fresh break + RSI 50-65 + BW 13-17':
        df[df['fresh_break'] & (df['rsi']>50) & (df['rsi']<65) & (df['bw']>=13) & (df['bw']<=17)],
    'V9 full (all 5 gates)':
        df[df['green'] & df['above_ema9l'] & (df['slope1']>=0) & (df['slope2']>=0) &
           (df['bw']>=13) & (df['bw']<=17) & (df['rsi']>50) & (df['rsi']<65) & df['rsi_rising']],
}

rows = []
for name, sub in combos.items():
    s = st(sub['ret'])
    if s: rows.append((name, s))

# sort by avg
rows.sort(key=lambda x: x[1]['avg'], reverse=True)
for name, s in rows:
    print(f"  {name:50} {s['n']:5d} {s['avg']:+7.1f}  "
          f"{s['win']:5.1f}%  {s['esl']:5.1f}%  {s['big']:5.1f}%")

print(f"\n  Note: {DAYS} days. MIN_N={MIN_N}. ret = 9-min forward return.")
