"""
V9 3-min ESL Deep Analysis
Not about adding filters — about understanding WHY ESL hits happen.
Questions:
  1. What is the loss distribution? (are ESLs -12 or -30?)
  2. What BW/RSI/slope state exists at ESL entries vs wins?
  3. Does time-of-day predict ESL probability?
  4. After an ESL, what happens next candle? (market state)
  5. Is ESL clustered on specific days or spread evenly?
  6. Candle body direction vs outcome (fake breakout profile)
  7. What % of ESL trades had a prior ESL within 3 candles? (clustering)
  8. Does the EMA band position at entry predict outcome?
  9. What is the win/ESL split by RSI bucket within V9 gates?
 10. Consecutive ESL streaks — how long, how often?

Run: bash ~/VISHAL_RAJPUT/run_backtest.sh v9_esl_deep
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
df['fwd_1c']     = g['close'].transform(lambda x: x.shift(-1))
df['fwd_2c']     = g['close'].transform(lambda x: x.shift(-2))
df['fwd_5c']     = g['close'].transform(lambda x: x.shift(-5))
df['fwd_10c']    = g['close'].transform(lambda x: x.shift(-10))
df['rsi_prev']   = g['rsi'].transform(lambda x: x.shift(1))
df['rsi_prev2']  = g['rsi'].transform(lambda x: x.shift(2))
df['ema9l_prev'] = g['ema9_low'].transform(lambda x: x.shift(1))
df['ema9l_prev2']= g['ema9_low'].transform(lambda x: x.shift(2))
df['ema9l_prev3']= g['ema9_low'].transform(lambda x: x.shift(3))
df['bw']         = df['ema9_high'] - df['ema9_low']
df['bw_prev']    = g['bw'].transform(lambda x: x.shift(1))
df['close_prev'] = g['close'].transform(lambda x: x.shift(1))
df['open_prev']  = g['open'].transform(lambda x: x.shift(1))
df['high_prev']  = g['high'].transform(lambda x: x.shift(1))
df['slope1']     = df['ema9_low'] - df['ema9l_prev']
df['slope2']     = df['ema9l_prev'] - df['ema9l_prev2']
df['slope3']     = df['ema9l_prev2'] - df['ema9l_prev3']
df['band_mid']   = (df['ema9_high'] + df['ema9_low']) / 2
df['close_in_band'] = (df['close'] - df['ema9_low']) / df['bw'].replace(0,np.nan)

df = df.dropna(subset=['fwd_3c','rsi_prev','ema9l_prev','bw'])

v = df[df['fwd_3c'].notna() & (df['close']>0)].iloc[0]
use_abs = abs(float(v['fwd_3c'])) > float(v['close'])*0.5
df['ret']  = df['fwd_3c'] - df['close'] if use_abs else df['fwd_3c']
df['ret1'] = df['fwd_1c'] - df['close'] if use_abs else df['fwd_1c']
df['ret2'] = df['fwd_2c'] - df['close'] if use_abs else df['fwd_2c']
df['ret5'] = df['fwd_5c'] - df['close'] if use_abs else df['fwd_5c']
df['ret10']= df['fwd_10c']- df['close'] if use_abs else df['fwd_10c']

df = df.dropna(subset=['ret'])

# V9 signal filter
v9 = df[
    (df['close'] > df['open']) &
    (df['close'] > df['ema9_low']) &
    (df['slope1'] >= 0) & (df['slope2'] >= 0) &
    (df['bw'] >= 13) & (df['bw'] <= 17) &
    (df['rsi'] > 50) & (df['rsi'] < 65) &
    (df['rsi'] > df['rsi_prev'])
].copy()

v9['outcome'] = pd.cut(v9['ret'],
    bins=[-999,-24,-18,-12,-6,0,6,12,18,24,999],
    labels=['<-24','-24:-18','-18:-12','-12:-6','-6:0','0:6','6:12','12:18','18:24','>24'])
v9['is_esl']  = v9['ret'] < -12
v9['is_win']  = v9['ret'] > 0
v9['is_big']  = v9['ret'] > 12
v9['hour']    = v9['timestamp'].dt.hour
v9['hhmm']    = v9['hour']*60 + v9['timestamp'].dt.minute
v9['date']    = v9['timestamp'].dt.date
v9['body']    = v9['close'] - v9['open']
v9['body_pct']= v9['body'] / v9['open'] * 100
v9['rsi_delta']= v9['rsi'] - v9['rsi_prev']
v9['prev_green']= v9['close_prev'] > v9['open_prev']
v9['above_prev_high'] = v9['close'] > v9['high_prev']

DAYS = v9['date'].nunique()
BAR  = '━' * 76
n_v9 = len(v9)
n_esl = v9['is_esl'].sum()
n_win = v9['is_win'].sum()

print(f"V9 signals: {n_v9} | Days: {DAYS}")
print(f"ESL (ret<-12): {n_esl} ({n_esl/n_v9*100:.1f}%)")
print(f"Win (ret>0):   {n_win} ({n_win/n_v9*100:.1f}%)")
print(f"Big win (>12): {v9['is_big'].sum()} ({v9['is_big'].mean()*100:.1f}%)\n")

# ── PART 1: Return distribution ───────────────────────────────────
print(f"{BAR}")
print("  PART 1 — RETURN DISTRIBUTION (where exactly do losses fall?)")
print(BAR)
print(f"\n  {'Bucket':12} {'n':>6} {'%':>7}  {'cumulative %':>14}")
print(f"  {'─'*45}")
cum = 0
for bucket, cnt in v9['outcome'].value_counts(sort=False).items():
    pct = cnt/n_v9*100
    cum += pct
    bar = '█' * int(pct/2)
    print(f"  {str(bucket):12} {cnt:6d} {pct:6.1f}%  {cum:6.1f}%  {bar}")

print(f"\n  ESL depth (among ESL trades only):")
esl_trades = v9[v9['is_esl']]['ret']
print(f"  median loss: {esl_trades.median():+.1f}")
print(f"  worst loss:  {esl_trades.min():+.1f}")
print(f"  % below -20: {(esl_trades<-20).mean()*100:.1f}%")
print(f"  % below -30: {(esl_trades<-30).mean()*100:.1f}%")
print(f"  % below -50: {(esl_trades<-50).mean()*100:.1f}%")

# ── PART 2: ESL vs WIN profile ────────────────────────────────────
print(f"\n{BAR}")
print("  PART 2 — ESL vs WIN ENTRY PROFILE (what's different at entry?)")
print(BAR)

wins = v9[v9['is_win']]
esls = v9[v9['is_esl']]

metrics = {
    'RSI':            ('rsi',          wins['rsi'].mean(),          esls['rsi'].mean()),
    'RSI delta':      ('rsi_delta',    wins['rsi_delta'].mean(),    esls['rsi_delta'].mean()),
    'BW':             ('bw',           wins['bw'].mean(),           esls['bw'].mean()),
    'slope1':         ('slope1',       wins['slope1'].mean(),       esls['slope1'].mean()),
    'slope2':         ('slope2',       wins['slope2'].mean(),       esls['slope2'].mean()),
    'body':           ('body',         wins['body'].mean(),         esls['body'].mean()),
    'body%':          ('body_pct',     wins['body_pct'].mean(),     esls['body_pct'].mean()),
    'close_in_band':  ('close_in_band',wins['close_in_band'].mean(),esls['close_in_band'].mean()),
    'hour':           ('hour',         wins['hour'].mean(),         esls['hour'].mean()),
}
print(f"\n  {'Metric':20} {'WIN avg':>10} {'ESL avg':>10} {'diff':>8}")
print(f"  {'─'*52}")
for name, (col, w, e) in metrics.items():
    diff = e - w
    flag = " ◀" if abs(diff) > (abs(w)+abs(e))/2 * 0.15 else ""
    print(f"  {name:20} {w:10.2f} {e:10.2f} {diff:+8.2f}{flag}")

# ── PART 3: RSI bucket breakdown ──────────────────────────────────
print(f"\n{BAR}")
print("  PART 3 — RSI BUCKET: win% and ESL% within V9 gates")
print(BAR)
print(f"\n  {'RSI bucket':15} {'n':>5} {'avg':>8} {'win%':>7} {'ESL%':>7} {'big%':>7}")
print(f"  {'─'*55}")
for lo, hi in [(50,53),(53,56),(56,59),(59,62),(62,65)]:
    sub = v9[(v9['rsi']>lo) & (v9['rsi']<=hi)]
    if len(sub) < 5: continue
    r = sub['ret']
    print(f"  RSI {lo:2d}-{hi:2d}       {len(sub):5d} {r.mean():+7.1f}  "
          f"{(r>0).mean()*100:6.1f}%  {(r<-12).mean()*100:6.1f}%  {(r>12).mean()*100:6.1f}%")

# ── PART 4: BW bucket breakdown ───────────────────────────────────
print(f"\n{BAR}")
print("  PART 4 — BW BUCKET: win% and ESL% within V9 gates")
print(BAR)
print(f"\n  {'BW bucket':12} {'n':>5} {'avg':>8} {'win%':>7} {'ESL%':>7} {'big%':>7}")
print(f"  {'─'*52}")
for lo, hi in [(13,14),(14,15),(15,16),(16,17),(17,18)]:
    sub = v9[(v9['bw']>=lo) & (v9['bw']<hi)]
    if len(sub) < 5: continue
    r = sub['ret']
    print(f"  BW {lo:.0f}-{hi:.0f}       {len(sub):5d} {r.mean():+7.1f}  "
          f"{(r>0).mean()*100:6.1f}%  {(r<-12).mean()*100:6.1f}%  {(r>12).mean()*100:6.1f}%")

# ── PART 5: Time of day ───────────────────────────────────────────
print(f"\n{BAR}")
print("  PART 5 — TIME OF DAY: ESL% by 30-min window")
print(BAR)
print(f"\n  {'Time window':15} {'n':>5} {'avg':>8} {'win%':>7} {'ESL%':>7}")
print(f"  {'─'*48}")
time_windows = [
    ('09:15-09:45', 9*60+15, 9*60+45),
    ('09:45-10:15', 9*60+45, 10*60+15),
    ('10:15-10:45', 10*60+15, 10*60+45),
    ('10:45-11:15', 10*60+45, 11*60+15),
    ('11:15-11:45', 11*60+15, 11*60+45),
    ('11:45-12:15', 11*60+45, 12*60+15),
    ('12:15-12:45', 12*60+15, 12*60+45),
    ('12:45-13:15', 12*60+45, 13*60+15),
    ('13:15-13:45', 13*60+15, 13*60+45),
    ('13:45-14:15', 13*60+45, 14*60+15),
    ('14:15-14:45', 14*60+15, 14*60+45),
    ('14:45-15:30', 14*60+45, 15*60+30),
]
for label, lo, hi in time_windows:
    sub = v9[(v9['hhmm']>=lo) & (v9['hhmm']<hi)]
    if len(sub) < 3: continue
    r = sub['ret']
    print(f"  {label:15} {len(sub):5d} {r.mean():+7.1f}  "
          f"{(r>0).mean()*100:6.1f}%  {(r<-12).mean()*100:6.1f}%")

# ── PART 6: ESL clustering — same day ────────────────────────────
print(f"\n{BAR}")
print("  PART 6 — ESL CLUSTERING BY DAY")
print(BAR)
print(f"\n  {'Date':12} {'signals':>8} {'ESL n':>7} {'ESL%':>7} {'avg':>8} {'win%':>7}")
print(f"  {'─'*60}")
day_esl = []
for d in sorted(v9['date'].unique()):
    sub = v9[v9['date']==d]
    r = sub['ret']
    esl_pct = (r<-12).mean()*100
    day_esl.append(esl_pct)
    print(f"  {str(d):12} {len(sub):8d} {(r<-12).sum():7d} {esl_pct:6.1f}%  "
          f"{r.mean():+7.1f}  {(r>0).mean()*100:6.1f}%")
print(f"\n  Days with ESL% > 40%: {sum(1 for x in day_esl if x>40)}")
print(f"  Days with ESL% = 0%:  {sum(1 for x in day_esl if x==0)}")
print(f"  Avg ESL% across days: {np.mean(day_esl):.1f}%")

# ── PART 7: Slope3 deep dive ──────────────────────────────────────
print(f"\n{BAR}")
print("  PART 7 — SLOPE DEEP DIVE (3-candle history)")
print(BAR)
print(f"\n  {'Slope pattern':30} {'n':>5} {'avg':>8} {'win%':>7} {'ESL%':>7}")
print(f"  {'─'*58}")
patterns = {
    'slope1>0, 2>0, 3>0 (all up)':  (v9['slope1']>0)  & (v9['slope2']>0)  & (v9['slope3']>0),
    'slope1>0, 2>0, 3<=0 (accel)':  (v9['slope1']>0)  & (v9['slope2']>0)  & (v9['slope3']<=0),
    'slope1>0, 2<=0, 3>0 (bounce)': (v9['slope1']>0)  & (v9['slope2']<=0) & (v9['slope3']>0),
    'slope1>0, 2<=0, 3<=0 (spike)': (v9['slope1']>0)  & (v9['slope2']<=0) & (v9['slope3']<=0),
    'slope1=0, 2>0 (flat top)':     (v9['slope1']==0) & (v9['slope2']>0),
    'slope sum > 3 (strong)':        (v9['slope1']+v9['slope2']+v9['slope3'])>3,
    'slope sum 1-3 (moderate)':      (v9['slope1']+v9['slope2']+v9['slope3']).between(1,3),
    'slope sum <= 0 (weak)':         (v9['slope1']+v9['slope2']+v9['slope3'])<=0,
}
for name, mask in patterns.items():
    sub = v9[mask]
    if len(sub) < 5: continue
    r = sub['ret']
    print(f"  {name:30} {len(sub):5d} {r.mean():+7.1f}  "
          f"{(r>0).mean()*100:6.1f}%  {(r<-12).mean()*100:6.1f}%")

# ── PART 8: Band position at entry ───────────────────────────────
print(f"\n{BAR}")
print("  PART 8 — BAND POSITION AT ENTRY")
print("  (0=at ema9_low, 1=at ema9_high, >1=above band)")
print(BAR)
print(f"\n  {'Position':20} {'n':>5} {'avg':>8} {'win%':>7} {'ESL%':>7}")
print(f"  {'─'*52}")
bins = [(0,0.3,'0.0-0.3 (low)'),(0.3,0.7,'0.3-0.7 (mid)'),(0.7,1.0,'0.7-1.0 (high)'),(1.0,1.5,'1.0-1.5 (above band)'),(1.5,9,'1.5+ (far above)')]
for lo, hi, label in bins:
    sub = v9[(v9['close_in_band']>=lo) & (v9['close_in_band']<hi)]
    if len(sub) < 5: continue
    r = sub['ret']
    print(f"  {label:20} {len(sub):5d} {r.mean():+7.1f}  "
          f"{(r>0).mean()*100:6.1f}%  {(r<-12).mean()*100:6.1f}%")

# ── PART 9: What happens 1-2 candles after ESL entry ─────────────
print(f"\n{BAR}")
print("  PART 9 — SPEED OF LOSS (how fast do ESL trades fall?)")
print(BAR)
esl_rows = v9[v9['is_esl']].copy()
print(f"\n  Among {len(esl_rows)} ESL trades:")
print(f"\n  {'Metric':35} {'value':>10}")
print(f"  {'─'*48}")
r1 = esl_rows['ret1'].dropna()
r2 = esl_rows['ret2'].dropna()
r3 = esl_rows['ret'].dropna()
if len(r1)>0:
    print(f"  avg after 1 candle (3min):         {r1.mean():+9.1f}")
    print(f"  % already negative after 1c:       {(r1<0).mean()*100:9.1f}%")
    print(f"  % already < -6 after 1c:           {(r1<-6).mean()*100:9.1f}%")
if len(r2)>0:
    print(f"  avg after 2 candles (6min):        {r2.mean():+9.1f}")
    print(f"  % already < -12 after 2c:          {(r2<-12).mean()*100:9.1f}%")
print(f"  avg at 3c close (ESL confirmed):   {r3.mean():+9.1f}")

# Compare with wins
win_rows = v9[v9['is_win']].copy()
r1w = win_rows['ret1'].dropna()
r2w = win_rows['ret2'].dropna()
if len(r1w)>0:
    print(f"\n  Among {len(win_rows)} WIN trades:")
    print(f"  avg after 1 candle (3min):         {r1w.mean():+9.1f}")
    print(f"  % already positive after 1c:       {(r1w>0).mean()*100:9.1f}%")

# ── PART 10: Fake breakout fingerprint ───────────────────────────
print(f"\n{BAR}")
print("  PART 10 — FAKE BREAKOUT FINGERPRINT")
print("  (ESL = close > ema9l but then reverses — what does entry look like?)")
print(BAR)

print(f"\n  {'Feature':35} {'ALL':>8} {'WIN':>8} {'ESL':>8} {'diff':>8}")
print(f"  {'─'*65}")

features = [
    ('RSI',               v9['rsi'],              wins['rsi'],              esls['rsi']),
    ('BW',                v9['bw'],               wins['bw'],               esls['bw']),
    ('slope1',            v9['slope1'],            wins['slope1'],           esls['slope1']),
    ('body',              v9['body'],              wins['body'],             esls['body']),
    ('body%',             v9['body_pct'],          wins['body_pct'],         esls['body_pct']),
    ('close_in_band',     v9['close_in_band'],     wins['close_in_band'],    esls['close_in_band']),
    ('rsi_delta',         v9['rsi_delta'],         wins['rsi_delta'],        esls['rsi_delta']),
    ('prev_green %',      v9['prev_green']*100,    wins['prev_green']*100,   esls['prev_green']*100),
    ('above_prev_high %', v9['above_prev_high']*100, wins['above_prev_high']*100, esls['above_prev_high']*100),
]
for fname, all_col, w_col, e_col in features:
    a, w, e = all_col.mean(), w_col.mean(), e_col.mean()
    diff = e - w
    flag = " ◀" if abs(diff) > max(abs(w),abs(e)) * 0.12 else ""
    print(f"  {fname:35} {a:8.2f} {w:8.2f} {e:8.2f} {diff:+8.2f}{flag}")

print(f"\n  Note: {DAYS} days, {n_v9} V9 signals. ESL = ret < -12 in 9min.")
