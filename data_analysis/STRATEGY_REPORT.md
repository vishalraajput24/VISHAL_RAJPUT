# VRL v16.7 — Strategy Analysis Report

> Generated: 2026-05-12 | Data: May 1–11 2026 (+ today if pushed)
> V7 = 15-min candle strategy | V8 = 3-min candle strategy

---

## Daily P&L Summary

| Date       | V7 Trades | V7 W/L | V7 Pts | V7 Net ₹  | V8 Trades | V8 W/L | V8 Pts | V8 Net ₹ | Day Total Pts |
| ---------- | --------- | ------ | ------ | --------- | --------- | ------ | ------ | -------- | ------------- |
|            | 1         | 0/1    | +0.0   | +0.0      | 0         | 0/0    | +0     | +0       | +0.0          |
| 2026-05-04 | 26        | 15/11  | -47.9  | -8220.45  | 0         | 0/0    | +0     | +0       | -47.9         |
| 2026-05-05 | 14        | 6/8    | -43.6  | -6487.33  | 0         | 0/0    | +0     | +0       | -43.6         |
| 2026-05-06 | 9         | 2/7    | -19.6  | -3433.09  | 0         | 0/0    | +0     | +0       | -19.6         |
| 2026-05-07 | 26        | 3/23   | -410.5 | -55806.08 | 1         | 1/0    | +12.0  | +1468.16 | -398.5        |
| 2026-05-08 | 14        | 2/12   | -30.0  | -5116.85  | 3         | 1/2    | +0.0   | -263.24  | -30.0         |
| 2026-05-11 | 11        | 6/5    | +48.3  | +5457.85  | 5         | 2/3    | +8.0   | +638.87  | +56.3         |

---

## V7 (15-min) Strategy — Core Metrics

| Metric | Value |
|--------|-------|
| Total Trades | 101 |
| Win Rate | **33.7%** (34W / 67L / 10BE) |
| Total PnL (pts) | **-503.3 pts** |
| Total Net (₹) | -73605.95 |
| Avg Win | +11.0 pts |
| Avg Loss | -13.1 pts |
| Profit Factor | 0.43 |
| Avg Peak PnL | 9.7 pts |
| Avg Capture % | -51.6% (of peak_pnl captured at exit) |
| Avg Candles Held | 8.1 |
| Avg Entry Body % | 60.5% |
| Initial SL Hits | 51 (50.5%) |
| Trail/EOD Exits | 41 (40.6%) |

### V7 Exit Reason Breakdown

| Exit Reason  | Count | Total Pts | Avg Pts |
| ------------ | ----- | --------- | ------- |
| EMERGENCY_SL | 50    | -838.9    | -16.8   |
| VISHAL_TRAIL | 41    | 332.4     | 8.1     |
| FORCE_EXIT   | 9     | 3.2       | 0.4     |
| UNKNOWN      | 1     | 0.0       | 0.0     |

### V7 Entry Mode Breakdown

| Entry Mode    | Count | Total Pts | Avg Pts |
| ------------- | ----- | --------- | ------- |
| CLOSE_FILL    | 97    | -496.4    | -5.1    |
| OPTION_B      | 2     | 11.3      | 5.7     |
| EMA9_BREAKOUT | 1     | -18.2     | -18.2   |
| UNKNOWN       | 1     | 0.0       | 0.0     |

### V7 Peak PnL Distribution

| Peak Tier | Count | %     | Bar             |
| --------- | ----- | ----- | --------------- |
| <12       | 67    | 66.3% | ██████████░░░░░ |
| 12-24     | 24    | 23.8% | ████░░░░░░░░░░░ |
| 24-36     | 5     | 5.0%  | █░░░░░░░░░░░░░░ |
| 36-50     | 5     | 5.0%  | █░░░░░░░░░░░░░░ |
| >50       | 0     | 0.0%  | ░░░░░░░░░░░░░░░ |

### V7 Win Rate by Hour

| Hour  | Trades | Win%   | Pts    | Win Rate Bar    |
| ----- | ------ | ------ | ------ | --------------- |
| 09:xx | 20     | 10.0%  | -342.8 | ██░░░░░░░░░░░░░ |
| 10:xx | 14     | 42.9%  | -49.6  | ██████░░░░░░░░░ |
| 11:xx | 13     | 23.1%  | -67.2  | ███░░░░░░░░░░░░ |
| 12:xx | 20     | 45.0%  | -18.6  | ███████░░░░░░░░ |
| 13:xx | 16     | 56.2%  | +26.6  | ████████░░░░░░░ |
| 14:xx | 16     | 25.0%  | -56.5  | ████░░░░░░░░░░░ |
| 15:xx | 1      | 100.0% | +5.0   | ███████████████ |

### V7 Cross-Leg Gate (PASS = other leg dying)

| xLeg Signal | Count | Win%  | Pts    | Avg Peak |
| ----------- | ----- | ----- | ------ | -------- |
| PASS        | 65    | 38.5% | -155.0 | 10.9     |
| FAIL        | 35    | 25.7% | -348.2 | 7.7      |
| UNKNOWN     | 1     | 0.0%  | +0.0   | 0.0      |

---

## V8 (3-min) Strategy — Core Metrics

| Metric | Value |
|--------|-------|
| Total Trades | 9 |
| Win Rate | **44.4%** (4W / 5L / 2BE) |
| Total PnL (pts) | **+20.0 pts** |
| Total Net (₹) | +1843.79 |
| Avg Win | +14.0 pts |
| Avg Loss | -7.2 pts |
| Profit Factor | 1.56 |
| Avg Peak PnL | 17.1 pts |

### V8 Entry Tier Breakdown

| Entry Tier | Count | Total Pts | Avg Pts |
| ---------- | ----- | --------- | ------- |
| V8_LOCK_12 | 3     | 36.0      | 12.0    |
| V8_INITIAL | 3     | -36.0     | -12.0   |
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
| Current  (SL=-12, BE@12) | 26   | 66     | 25.7% | -442.0    |
| EnhA     (SL=-10, BE@12) | 26   | 66     | 25.7% | -310.0    |
| EnhB     (SL=-12, BE@8)  | 32   | 58     | 31.7% | -327.0    |
| EnhC     (SL=-10, BE@8)  | 32   | 58     | 31.7% | -211.0    |

> **Best variant: `EnhC     (SL=-10, BE@8)`** → -211.0 pts
> vs Current: -442.0 pts
> Delta: +231.0 pts

### 2. Entry Body % Gate — Which Threshold is Best?

Current: no body% gate on V7 (V7 uses close>EMA9_low + RSI gate).
Testing: what if we require minimum body% at entry?

| Gate        | Qualifying | Win%  | Pts    | Filtered   | Filtered Win% |
| ----------- | ---------- | ----- | ------ | ---------- | ------------- |
| body >= 20% | 92 trades  | 32.6% | -534.5 | 8 skipped  | 50.0% skip WR |
| body >= 30% | 90 trades  | 33.3% | -512.2 | 10 skipped | 40.0% skip WR |
| body >= 40% | 83 trades  | 32.5% | -542.5 | 17 skipped | 41.2% skip WR |
| body >= 50% | 69 trades  | 27.5% | -555.6 | 31 skipped | 48.4% skip WR |
| body >= 60% | 61 trades  | 26.2% | -541.0 | 39 skipped | 46.2% skip WR |

> Best body gate: **≥ 30%** → -512.2 pts on 90 trades (33.3% WR)

### 3. Best Entry Window (Hour)

**Top 3 hours by P&L:**
- `13:xx` — 16 trades, 56.2% WR, +26.6 pts
- `12:xx` — 20 trades, 45.0% WR, -18.6 pts
- `10:xx` — 14 trades, 42.9% WR, -49.6 pts

**Worst 3 hours:**
- `09:xx` — 20 trades, 10.0% WR, -342.8 pts
- `11:xx` — 13 trades, 23.1% WR, -67.2 pts
- `14:xx` — 16 trades, 25.0% WR, -56.5 pts

### 4. Cross-Leg Gate Recommendation

| Signal | Count | Win% | Pts | Avg Peak |
|--------|-------|------|-----|----------|
| PASS (other leg dying) | 65 | 38.5% | -155.0 | 10.9 |
| FAIL (other leg live)  | 35 | 25.7% | -348.2 | 7.7 |

> PASS vs FAIL delta: Win% +12.8pp | Peak +3.2 pts
> ✅ xLeg PASS gate adds value — filter FAIL signals

---

## Summary — Top Recommended Enhancements for V7

**1. SL Tightening** — Switch to `EnhC     (SL=-10, BE@8)` for estimated **+231.0 pts** gain.
   > Action: Change `EMERGENCY_SL_PTS` in config or adjust initial SL in `compute_trail_sl()`.
**2. xLeg Gate** — Skip entries when xleg signal = FAIL.
   > PASS trades have 38.5% WR vs FAIL at 25.7% WR.
**3. Time Window Filter** — Avoid entries during hours: [9, 10, 11, 12, 14].
   > These hours show consistent losses. Adding a `cutoff_before/after` for bad hours could help.

---

## Data Coverage

| | V7 | V8 |
|-|----|-----|
| Trades | 101 | 9 |
| Total Pts | -503.3 | +20.0 |
| Win Rate | 33.7% | 44.4% |
| Profit Factor | 0.43 | 1.56 |

_V8 launched May 07 — limited sample size, monitor more days._

---
*Report generated by `data_analysis/strategy_analysis.py`*