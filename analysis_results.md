# VRL Strategy Analysis

Run: 2026-05-16 19:04

```
=== VRL_ANALYSIS ===
Days available: 61  (2026-02-23 → 2026-05-16)
Days with options data: 22

━━━━ 1. TIME OF DAY (green candle → fwd_3c) ━━━━
       mean  count  median
slot                      
09:30  1.34   5612    0.05
14:30  1.33   5079    0.00
09:00  1.29   2864    0.00
14:00  1.05   4875   -0.05
12:00  0.78   5089    0.00
11:30  0.66   4621   -0.10
12:30  0.25   4769    0.00
11:00  0.12   4732    0.00
10:00  0.05   5291    0.00
10:30  0.05   4906   -0.05
13:00 -0.54   4811   -0.05
13:30 -0.54   5101   -0.05
15:00 -1.45   3836   -0.15

  ✅ BEST slots : ['09:30', '14:30', '09:00']
  ❌ WORST slots: ['13:00', '13:30', '15:00']

━━━━ 2. BAND WIDTH vs FORWARD RETURN ━━━━
            mean  count
bw_bucket              
0           0.00  29561
2           0.04  12000
4           0.70   8006
6           1.16   5120
8           0.85   2797
10          0.12   1471
12         -0.60    825
14          0.37    510
16          2.82    360
18          4.58    234
20          3.18    170
22         -0.34    111
24          4.67     74
26         10.01     59
28         18.20     57
30         19.80     47
32         12.68     45
34         16.29     24
36         12.09     20

━━━━ 3. RSI CONDITIONS ━━━━
RSI bucket → avg fwd_3c:
            mean  count
rsi_bucket             
0.0         0.38    411
10.0       -0.47   1538
20.0       -0.31   3544
30.0        0.51   6660
40.0        0.62  10558
50.0       -0.27  11524
60.0       -0.09   8103
70.0        0.29   4515
80.0        0.42   2678
90.0        1.46   2129

RSI rising ≥2 filter:
               mean  count
rsi_flat       0.20  27801
rsi_rising_2+  0.18  23859

━━━━ 4. STOCHRSI(5) OVERSOLD CROSS ━━━━
Oversold cross (k≤20 prev → k rising):
          mean  count  median
no_cross  0.06  38543   -0.05
os_cross  0.72  11019    0.00

StochRSI > 50:
              mean  count
srsi_below50  0.33  23304
srsi_above50  0.10  26258

━━━━ 5. COMBINED GATE STACK ━━━━
  baseline    : n=61586  win%= 43.3%  avg=+0.35  median=+0.00
  current     : n= 1231  win%= 42.6%  avg=+1.68  median=+0.00
  proposed    : n=   29  win%= 48.3%  avg=+2.32  median=-0.60

━━━━ 6. TIMEFRAME COMPARISON ━━━━
(green candle → fwd_1c and fwd_3c, filtered to CE/PE options + spot)

TF                  n   win_1c   avg_1c   win_3c   avg_3c
------------------------------------------------------------
3min_opts       61586    39.5%   +0.166    43.3%   +0.349
5min_opts       39433    41.3%   +0.354    42.1%   +0.012
15min_opts      14442    38.4%   -0.618    41.1%   -0.583
1min_spot        9855    50.8%   +0.215    49.4%   +0.013

  ✅ Best avg_3c: 3min_opts (+0.349 pts/candle)

━━━━ 7. MINIMISED STRATEGY BACKTEST ━━━━

Strategy                n    win%      avg   median   total_pts
-----------------------------------------------------------------
  baseline        : n=61586  win%= 43.3%  avg=+0.35  median=+0.00  total=+21492pts
  current         : n=  941  win%= 44.2%  avg=+2.35  median=+0.00  total=+2212pts
  mini_G2_G6      : n= 5053  win%= 41.8%  avg=+0.36  median=-0.05  total=+1812pts
  mini_G2_G3_G6   : n=  169  win%= 47.9%  avg=-0.17  median=-1.35  total=-29pts

  Trade frequency (entries per 75-candle session):
  baseline        : ~2799.4 entries/session-day
  current         : ~42.8 entries/session-day
  mini_G2_G6      : ~229.7 entries/session-day
  mini_G2_G3_G6   : ~7.7 entries/session-day

━━━━ 8. EMA SPAN SWEEP (which EMA fits best for 3-min) ━━━━
  Filter: G1 + G2(close>ema_low) + G2B(slope ≥0 last 2 candles)

       EMA       n    win%      avg   median
  --------------------------------------------
  EMA-5   : n=26948  win%= 41.2%  avg=+0.145  median=-0.050
  EMA-7   : n=26937  win%= 41.1%  avg=+0.134  median=-0.050
  EMA-9   : n=26835  win%= 41.1%  avg=+0.126  median=-0.050
  EMA-11  : n=26920  win%= 41.3%  avg=+0.145  median=-0.050
  EMA-14  : n=27141  win%= 41.5%  avg=+0.168  median=-0.050
  EMA-21  : n=27541  win%= 42.1%  avg=+0.227  median=-0.050 ✅

  ✅ Best EMA span: 21  (avg=+0.227)

━━━━ 9. RSI THRESHOLD SWEEP ━━━━
  Base: G1 + G2 + G3(bw>=10). Testing rsi_min and rsi_rise_min.

  RSI threshold (rsi_rise >= 2):
     threshold       n    win%      avg
  --------------------------------------
  rsi>45    + rise>=2: n= 1295  win%= 43.7%  avg=+2.349
  rsi>48    + rise>=2: n= 1258  win%= 43.2%  avg=+1.926
  rsi>50    + rise>=2: n= 1230  win%= 42.6%  avg=+1.656
  rsi>52    + rise>=2: n= 1214  win%= 42.3%  avg=+1.483
  rsi>53    + rise>=2: n= 1205  win%= 42.1%  avg=+1.351
  rsi>55    + rise>=2: n= 1154  win%= 40.4%  avg=+0.590
  rsi>57    + rise>=2: n= 1118  win%= 38.9%  avg=+0.044
  rsi>60    + rise>=2: n= 1029  win%= 38.7%  avg=+0.038

  RSI rise requirement (rsi > 50):
    rise_min       n    win%      avg
  --------------------------------------
  rise>=0    : n= 1612  win%= 42.1%  avg=+0.919
  rise>=1    : n= 1380  win%= 42.3%  avg=+1.158
  rise>=2    : n= 1230  win%= 42.6%  avg=+1.656
  rise>=3    : n= 1090  win%= 42.9%  avg=+1.841
  rise>=5    : n=  862  win%= 41.6%  avg=+0.719

━━━━ 10. BAND WIDTH RANGE GRID ━━━━
  Find optimal BW min+max (base: G1 + G2)

   min_bw   max_bw       n    win%      avg
  ------------------------------------------------
        4   no cap  n=18400  win%= 46.5%  avg=+0.930 ✅
        4      <12  n=15790  win%= 46.1%  avg=+0.617
        4      <16  n=17109  win%= 46.1%  avg=+0.576
        4      <20  n=17698  win%= 46.1%  avg=+0.681
        4      <24  n=17979  win%= 46.2%  avg=+0.698
        6   no cap  n=11338  win%= 46.7%  avg=+1.233 ✅
        6      <12  n= 8728  win%= 46.0%  avg=+0.759
        6      <16  n=10047  win%= 46.0%  avg=+0.669
        6      <20  n=10636  win%= 46.1%  avg=+0.839
        6      <24  n=10917  win%= 46.1%  avg=+0.864
        8   no cap  n= 6686  win%= 46.4%  avg=+1.451 ✅
        8      <12  n= 4076  win%= 44.8%  avg=+0.574
        8      <16  n= 5395  win%= 44.9%  avg=+0.453
        8      <20  n= 5984  win%= 45.3%  avg=+0.775
        8      <24  n= 6265  win%= 45.4%  avg=+0.821
       10   no cap  n= 4054  win%= 47.2%  avg=+1.881 ✅
       10      <12  n= 1444  win%= 44.4%  avg=+0.184
       10      <16  n= 2763  win%= 44.8%  avg=+0.133
       10      <20  n= 3352  win%= 45.4%  avg=+0.766
       10      <24  n= 3633  win%= 45.6%  avg=+0.845
       12   no cap  n= 2610  win%= 48.8%  avg=+2.820 ✅
       12      <16  n= 1319  win%= 45.3%  avg=+0.078
       12      <20  n= 1908  win%= 46.2%  avg=+1.206
       12      <24  n= 2189  win%= 46.4%  avg=+1.281

  ✅ Best BW range: bw≥12 no cap  (avg=+2.820)

━━━━ 11. TRENDING vs SIDEWAYS ━━━━
  Day classification: spot daily (high-low)/open%
  trending=range>0.8%  sideways=range<0.5%  neutral=in between
  Days: trending=48  neutral=6  sideways=0

       Class   all_n   all_avg   curr_n   curr_avg  curr_win%
  --------------------------------------------------------------
  trending  : all=54497  avg=+0.419   curr= 1210  avg=+1.736   win%=42.6%
  neutral   : all= 7089  avg=-0.187   curr=   20  avg=-3.158   win%=40.0%

  → 'trending' days = strategy works. 'sideways' days = strategy should pause.

━━━━ 12. PREMIUM BOX BREAKOUT (per-series, after 10:30) ━━━━
  Rolling box on each CE/PE series independently.
  Signal: after 10:30 + green + close > box_high + box_width >= min_width

  A. Box size sweep (min_width=0 — pure breakout signal quality):
     N   window       n    win%      avg   median
  --------------------------------------------------
  N=5   ( 15min): n=14882  win%= 39.4%  avg=+0.222  median=+0.000
  N=8   ( 24min): n=11156  win%= 39.4%  avg=+0.623  median=+0.000
  N=10  ( 30min): n= 9826  win%= 39.9%  avg=+0.864  median=+0.000
  N=12  ( 36min): n= 8780  win%= 39.9%  avg=+1.014  median=+0.000
  N=15  ( 45min): n= 7831  win%= 39.8%  avg=+1.073  median=+0.000

  ✅ Best box size: N=15 (45 min)

  B. Box width minimum sweep (N=15, after 10:30):
     Wider box = market had more range = more liquidity = better move?
   min_width       n    win%      avg   median
  --------------------------------------------------
  width≥10  : n= 4714  win%= 42.1%  avg=+1.403  median=+0.000
  width≥20  : n= 3426  win%= 41.8%  avg=+1.496  median=+0.000
  width≥30  : n= 2429  win%= 40.8%  avg=+1.296  median=-0.400
  width≥40  : n= 1714  win%= 39.8%  avg=+0.743  median=-0.700
  width≥50  : n= 1184  win%= 38.0%  avg=+0.120  median=-1.450

  ✅ Best min box width: 20 pts

  C. Miss rate — big moves (fwd_3c > 15 pts) caught vs missed:
     Box filter: N=15, width≥20
  Big moves (>15 pts): 2846 total
  Caught by box signal: 559  (19.6%)
  Missed by box filter: 2287  (80.4%)
  All after-10:30 green candles:  47811
  Box signals fired:              3426  (7.2% of candles)

  D. Gate combinations (N=15, width≥20):
  combo                            n    win%      avg   median
  ----------------------------------------------------------
  box only                  : n= 3426  win%= 41.8%  avg=+1.496  median=+0.000
  box + ema rising          : n= 3286  win%= 41.8%  avg=+1.495  median=+0.000
  box + G3(bw≥10)           : n=  810  win%= 37.8%  avg=+0.986  median=-2.025
  box + G3(bw≥12)           : n=  511  win%= 38.9%  avg=+1.889  median=-2.500
  box + G5(rsi>50)          : n= 2245  win%= 41.1%  avg=+1.292  median=-1.250
  box + G5(rsi>45)          : n= 2245  win%= 41.1%  avg=+1.292  median=-1.250
  box+G3(≥10)+G5(>45)       : n=  581  win%= 38.0%  avg=+1.552  median=-3.000
  box+G3(≥12)+G5(>45)       : n=  353  win%= 40.2%  avg=+3.223  median=-3.050
  box+ema+G3+G5(>45)        : n=  532  win%= 37.0%  avg=+1.038  median=-3.500

  → Each CE/PE series has its own independent box.
    No cross-leg confirmation needed — if PE breaks its box, that IS the signal.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
DONE.
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
```
