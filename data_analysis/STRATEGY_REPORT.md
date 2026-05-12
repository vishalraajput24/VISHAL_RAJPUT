# VRL v16.7 — Strategy Analysis Report

> Generated: 2026-05-12 | Filtered from 2026-05-01 | Range: 2026-05-04 → 2026-05-12
> V7 = 15-min candle strategy | V8 = 3-min candle strategy

---

## Daily P&L Summary

| Date       | V7 Trades | V7 W/L | V7 Pts | V7 Net ₹  | V8 Trades | V8 W/L | V8 Pts | V8 Net ₹ | Day Total Pts |
| ---------- | --------- | ------ | ------ | --------- | --------- | ------ | ------ | -------- | ------------- |
| 2026-05-04 | 26        | 15/11  | -47.9  | -8220.45  | 0         | 0/0    | +0     | +0       | -47.9         |
| 2026-05-05 | 14        | 6/8    | -43.6  | -6487.33  | 0         | 0/0    | +0     | +0       | -43.6         |
| 2026-05-06 | 9         | 2/7    | -19.6  | -3433.09  | 0         | 0/0    | +0     | +0       | -19.6         |
| 2026-05-07 | 26        | 3/23   | -410.5 | -55806.08 | 1         | 1/0    | +12.0  | +1468.16 | -398.5        |
| 2026-05-08 | 14        | 2/12   | -30.0  | -5116.85  | 3         | 1/2    | +0.0   | -263.24  | -30.0         |
| 2026-05-11 | 11        | 6/5    | +48.3  | +5457.85  | 5         | 2/3    | +8.0   | +638.87  | +56.3         |
| 2026-05-12 | 13        | 4/9    | +21.2  | +1986.14  | 8         | 0/8    | -45.5  | -6390.05 | -24.3         |

---

## V7 (15-min) Strategy — Core Metrics

| Metric | Value |
|--------|-------|
| Total Trades | 113 |
| Win Rate | **33.6%** (38W / 75L / 12BE) |
| Total PnL (pts) | **-482.1 pts** |
| Total Net (₹) | -71619.81 |
| Avg Win | +11.5 pts |
| Avg Loss | -12.2 pts |
| Profit Factor | 0.47 |
| Avg Peak PnL | 9.9 pts |
| Avg Capture % | -45.9% (of peak_pnl captured at exit) |
| Avg Candles Held | 8.8 |
| Avg Entry Body % | 58.2% |
| Initial SL Hits | 52 (46.0%) |
| Trail/EOD Exits | 44 (38.9%) |

### V7 Exit Reason Breakdown

| Exit Reason  | Count | Total Pts | Avg Pts |
| ------------ | ----- | --------- | ------- |
| EMERGENCY_SL | 51    | -851.1    | -16.7   |
| VISHAL_TRAIL | 44    | 380.4     | 8.6     |
| FORCE_EXIT   | 18    | -11.4     | -0.6    |

### V7 Entry Mode Breakdown

| Entry Mode    | Count | Total Pts | Avg Pts |
| ------------- | ----- | --------- | ------- |
| CLOSE_FILL    | 110   | -475.2    | -4.3    |
| OPTION_B      | 2     | 11.3      | 5.7     |
| EMA9_BREAKOUT | 1     | -18.2     | -18.2   |

### V7 Peak PnL Distribution

| Peak Tier | Count | %     | Bar             |
| --------- | ----- | ----- | --------------- |
| <12       | 74    | 65.5% | ██████████░░░░░ |
| 12-24     | 27    | 23.9% | ████░░░░░░░░░░░ |
| 24-36     | 6     | 5.3%  | █░░░░░░░░░░░░░░ |
| 36-50     | 6     | 5.3%  | █░░░░░░░░░░░░░░ |
| >50       | 0     | 0.0%  | ░░░░░░░░░░░░░░░ |

### V7 Win Rate by Hour

| Hour  | Trades | Win%   | Pts    | Win Rate Bar    |
| ----- | ------ | ------ | ------ | --------------- |
| 09:xx | 21     | 9.5%   | -343.8 | █░░░░░░░░░░░░░░ |
| 10:xx | 17     | 35.3%  | -54.6  | █████░░░░░░░░░░ |
| 11:xx | 16     | 25.0%  | -79.6  | ████░░░░░░░░░░░ |
| 12:xx | 23     | 43.5%  | -27.1  | ███████░░░░░░░░ |
| 13:xx | 18     | 55.6%  | +38.5  | ████████░░░░░░░ |
| 14:xx | 17     | 29.4%  | -20.5  | ████░░░░░░░░░░░ |
| 15:xx | 1      | 100.0% | +5.0   | ███████████████ |

### V7 Cross-Leg Gate (PASS = other leg dying)

| xLeg Signal | Count | Win%  | Pts    | Avg Peak |
| ----------- | ----- | ----- | ------ | -------- |
| PASS        | 72    | 38.9% | -133.0 | 10.8     |
| FAIL        | 39    | 25.6% | -349.0 | 8.1      |
| UNKNOWN     | 2     | 0.0%  | +0.0   | 9.5      |

---

## V8 (3-min) Strategy — Core Metrics

| Metric | Value |
|--------|-------|
| Total Trades | 17 |
| Win Rate | **23.5%** (4W / 13L / 4BE) |
| Total PnL (pts) | **-25.5 pts** |
| Total Net (₹) | -4546.26 |
| Avg Win | +14.0 pts |
| Avg Loss | -6.3 pts |
| Profit Factor | 0.69 |
| Avg Peak PnL | 9.5 pts |

### V8 Entry Tier Breakdown

| Entry Tier | Count | Total Pts | Avg Pts |
| ---------- | ----- | --------- | ------- |
| V8_INITIAL | 11    | -81.5     | -7.4    |
| V8_LOCK_12 | 3     | 36.0      | 12.0    |
| V8_LOCK_BE | 2     | 0.0       | 0.0     |
| V8_LOCK_20 | 1     | 20.0      | 20.0    |

---

## V7 Parameter Enhancement Analysis

> Based on historical data — simulations use recorded peak_pnl as upper bound.

### 1. SL Ladder Variants — Simulated P&L

Testing 4 variants on actual V7 trades:
- **Current**: Initial SL = -12, BE lock at peak ≥ 12
- **EnhA**: Tighten Initial SL to -10 (save 2pts on each losing trade)
- **EnhB**: Keep SL = -12 but lock BE earlier at peak ≥ 8
- **EnhC**: Tighten SL to -10 AND lock BE earlier at peak ≥ 8

| Variant                  | Wins | Losses | Win%  | Total Pts |
| ------------------------ | ---- | ------ | ----- | --------- |
| Current  (SL=-12, BE@12) | 29   | 74     | 25.7% | -480.4    |
| EnhA     (SL=-10, BE@12) | 29   | 74     | 25.7% | -332.4    |
| EnhB     (SL=-12, BE@8)  | 35   | 66     | 31.0% | -365.4    |
| EnhC     (SL=-10, BE@8)  | 35   | 66     | 31.0% | -233.4    |

> **Best variant: `EnhC     (SL=-10, BE@8)`** → -233.4 pts
> vs Current: -480.4 pts
> Delta: +247.0 pts

### 2. Entry Body % Gate — Which Threshold is Best?

Current: no body% gate on V7 (V7 uses close>EMA9_low + RSI gate).
Testing: what if we require minimum body% at entry?

| Gate        | Qualifying | Win%  | Pts    | Filtered   | Filtered Win% |
| ----------- | ---------- | ----- | ------ | ---------- | ------------- |
| body >= 20% | 103 trades | 33.0% | -501.1 | 10 skipped | 40.0% skip WR |
| body >= 30% | 99 trades  | 32.3% | -500.3 | 14 skipped | 42.9% skip WR |
| body >= 40% | 89 trades  | 32.6% | -525.6 | 24 skipped | 37.5% skip WR |
| body >= 50% | 74 trades  | 27.0% | -541.1 | 39 skipped | 46.2% skip WR |
| body >= 60% | 63 trades  | 25.4% | -551.8 | 50 skipped | 44.0% skip WR |

> Best body gate: **≥ 30%** → -500.3 pts on 99 trades (32.3% WR)

### 3. Best Entry Window (Hour)

**Top 3 hours by P&L:**
- `13:xx` — 18 trades, 55.6% WR, +38.5 pts
- `14:xx` — 17 trades, 29.4% WR, -20.5 pts
- `12:xx` — 23 trades, 43.5% WR, -27.1 pts

**Worst 3 hours:**
- `09:xx` — 21 trades, 9.5% WR, -343.8 pts
- `11:xx` — 16 trades, 25.0% WR, -79.6 pts
- `10:xx` — 17 trades, 35.3% WR, -54.6 pts

### 4. Cross-Leg Gate Recommendation

| Signal | Count | Win% | Pts | Avg Peak |
|--------|-------|------|-----|----------|
| PASS (other leg dying) | 72 | 38.9% | -133.0 | 10.8 |
| FAIL (other leg live)  | 39 | 25.6% | -349.0 | 8.1 |

> PASS vs FAIL delta: Win% +13.3pp | Peak +2.7 pts
> ✅ xLeg PASS gate adds value — filter FAIL signals

---

## Summary — Top Recommended Enhancements for V7

**1. SL Tightening** — Switch to `EnhC     (SL=-10, BE@8)` for estimated **+247.0 pts** gain.
   > Action: Change `EMERGENCY_SL_PTS` in config or adjust initial SL in `compute_trail_sl()`.
**2. xLeg Gate** — Skip entries when xleg signal = FAIL.
   > PASS trades have 38.9% WR vs FAIL at 25.6% WR.
**3. Time Window Filter** — Avoid entries during hours: [9, 10, 11, 12, 14].
   > These hours show consistent losses. Adding a `cutoff_before/after` for bad hours could help.

---

## Data Coverage

| | V7 | V8 |
|-|----|-----|
| Trades | 113 | 17 |
| Total Pts | -482.1 | -25.5 |
| Win Rate | 33.6% | 23.5% |
| Profit Factor | 0.47 | 0.69 |

_V8 launched May 07 — limited sample size, monitor more days._

---
*Report generated by `data_analysis/strategy_analysis.py`*