# Post-Mortem: May 7, 2026 — -410.5 pts V7 Day / -304 pts Re-Entry Cascade

> Written: 2026-05-13
> Severity: CRITICAL — single worst trading day in VRL history
> Status: Partially fixed. Remaining guard proposed below.

---

## What Happened

On May 7, 2026, V7 took **26 trades** resulting in only **3 wins** and a total loss of **-410.5 pts**
(approximately **-55,800 Rs net** after charges).

Of these, **11 consecutive trades** were the same:
- Symbol: NIFTY CE 24350
- Direction: CE (bullish)
- Entry time: 09:48–09:58 (10-minute window)
- Entry price: ~216.8 Rs each
- candles_held: 1 on every single trade
- peak_pnl: 0.0 on every single trade
- exit_reason: EMERGENCY_SL on every single trade
- Loss per trade: -12 pts (Rs1,560)
- **Total cascade loss: ~-304 pts in under 10 minutes**

---

## Root Cause

**Three conditions combined to create the cascade:**

### 1. Re-entry watcher re-armed after each EMERGENCY_SL
After every EMERGENCY_SL exit, the re-entry watcher was armed (`_reentry_armed = True`).
This allowed V8/V7 to re-enter the same direction on the very next 3-min candle.
The market was whipsawing violently — every new entry immediately reversed and hit SL.

### 2. xLeg = FAIL on all 11 trades (both legs alive simultaneously)
The CE leg triggered the entry gate (close > EMA9_low, RSI rising).
But the PE leg was ALSO alive (above its EMA9_low) at the same time.
This is the definition of xLeg = FAIL — the market is torn, no directional conviction.
The xLeg gate was **disabled** at the time (display-only logging).

Research finding (post-mortem analysis):
- Morning FAIL trades (09:30–10:00) = 0% WR across all days, avg -26.9 pts
- FAIL entries before 10:00 are the single most dangerous condition in the strategy

### 3. No cascading re-entry limit
There was no cap on how many times V7/V8 could re-enter the same direction
in a short window. The bot entered 11 times in 10 minutes into the same losing setup.

---

## Timeline

```
09:48:xx  — EMERGENCY_SL  CE 24350  peak=0  -12pts
09:49:xx  — EMERGENCY_SL  CE 24350  peak=0  -12pts
09:50:xx  — EMERGENCY_SL  CE 24350  peak=0  -12pts
09:51:xx  — EMERGENCY_SL  CE 24350  peak=0  -12pts
09:52:xx  — EMERGENCY_SL  CE 24350  peak=0  -12pts
09:53:xx  — EMERGENCY_SL  CE 24350  peak=0  -12pts
09:54:xx  — EMERGENCY_SL  CE 24350  peak=0  -12pts
09:55:xx  — EMERGENCY_SL  CE 24350  peak=0  -12pts
09:56:xx  — EMERGENCY_SL  CE 24350  peak=0  -12pts
09:57:xx  — EMERGENCY_SL  CE 24350  peak=0  -12pts
09:58:xx  — EMERGENCY_SL  CE 24350  peak=0  -12pts
                                    ────────────────
                                    TOTAL: -304 pts
                                    TIME:  ~10 minutes
```

---

## Why peak_pnl = 0 on Every Trade

Price **never moved in the trade's favour** after entry.
The moment the position opened, the market immediately reversed.
The option price dropped from entry before a single 3-min candle could complete.
This means the entry signal itself was firing INTO a reversal, not at the start of a move.

---

## Fixes Applied

| Fix | When | Effect |
|-----|------|--------|
| xLeg gate logging enabled | Pre-May 7 | Was display-only — data was collected but gate not enforced |
| `_reentry_armed = False` after FORCE_EXIT | 2026-05-12 | Prevents re-entry after manual force exit |
| V8 cooldown flag fixed (was writing to wrong dict) | 2026-05-12 | 1-candle cooldown now actually works for V8 |
| V7 warmup 09:35 → 09:45 | 2026-05-13 | First V7 entry now at 09:45 candle, not 09:30 |

---

## Proposed Guards (Not Yet Implemented — Pending User Approval)

### Guard 1: Block FAIL entries before 10:00
```
Research data: Morning FAIL (09:30–10:00) = 0% WR, avg -26.9 pts, consistent 2/2 days
This single rule would have blocked all 11 cascade trades on May 7.
Implementation: in check_entry_v8() and check_entry_v7(), if xleg=FAIL and hour < 10 → reject
```

### Guard 2: Block FAIL when xleg_other_margin > 10 pts
```
Research data: FAIL margin >10 pts = 9.1% WR, avg -22.5 pts, 3 days consistent
On May 7, margin was very high (both legs strongly alive)
Implementation: if xleg=FAIL and xleg_other_margin > 10 → reject
```

### Guard 3: Daily loss circuit breaker
```
After N consecutive EMERGENCY_SL exits (e.g., 3), pause entries for 30 minutes.
Prevents cascade re-entry into a broken market.
Implementation: counter in state dict, reset on non-SL exit or time elapsed.
```

### Guard 4: Block PASS entries 11:00–12:00
```
Research data: 11AM PASS = 0% WR, consistent 4/4 days
Separate from May 7 but part of the same time-pattern study.
Implementation: time window check in entry gate.
```

---

## Key Lesson

> **A single bad market condition + no cascade limit = account-level damage.**
> The bot entered 11 times in 10 minutes and lost every time.
> Each individual trade looked valid by the entry gates.
> The problem was not the gate — it was the absence of a meta-level circuit breaker
> that asks: "have I lost 3 times in a row on the same setup? Stop and wait."

---

## How to Detect This in Future

In the live log, look for:
```
[V8] EMERGENCY_SL ... peak=0.0 ... candles_held=1
```
Three consecutive lines like this = cascade in progress. Bot should auto-pause.

In the trade CSV:
```bash
grep "EMERGENCY_SL" vrl_trade_log.csv | awk -F',' '{print $2, $14, $13}' | head -20
# Shows: entry_time, exit_reason, peak_pnl
# Three consecutive peak=0 lines = cascade signal
```

---

*Post-mortem written by VRL development team based on data analysis of May 4–11 2026 trading data.*
*Research agent analysis: 89 trades, 5 days, 3-min option candles + spot data.*
