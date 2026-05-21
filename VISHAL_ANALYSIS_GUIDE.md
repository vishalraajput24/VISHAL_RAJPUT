# Vishal Analysis Guide
## Sherlock Holmes Method — How We Analyse VRL Shadow Trades

**Principle**: A good detective does not guess. He observes, eliminates the impossible, and follows the evidence.
Every trade leaves a clue. Every SL-HIT has a reason. Find it.

---

## Step 1 — Collect the Evidence

Before analysing anything, pull the raw facts from the log.

```bash
# All shadow signals and exits today
grep "SHADOW-P1.*SIGNAL\|SHADOW-P1.*SL-HIT\|SHADOW-P1.*EOD\|SHADOW-P2.*SIGNAL" ~/logs/live/vrl_live.log

# All ATM relocks (market chaos indicator)
grep "Strikes LOCKED" ~/logs/live/vrl_live.log

# All warnings/errors (ignoring routine rejects)
grep "WARNING\|ERROR" ~/logs/live/vrl_live.log | grep -v "REJECT\|SKIP\|band_narrow\|below_band\|dtime\|LOGPATH"

# Analysis flags (EXTENDED, WEAK_ADX, etc.)
grep "ANALYSIS" ~/logs/live/vrl_live.log

# V9 scan status
grep "REJECT-V8" ~/logs/live/vrl_live.log | tail -5
```

Save today's analysis at: `~/VISHAL_RAJPUT/analysis/YYYY-MM-DD_analysis.md`

---

## Step 2 — Read the Market Context

Before looking at individual trades, understand what the market was doing.

**Key questions:**
1. How many ATM relocks happened, and when? (≥2 in first 15 min = volatile open)
2. What was the DTE? (0 = expiry day, 1 = next day, 4+ = normal week)
3. Did V9 trade? If not, why? (BW gate? both-sides cooldown?)
4. What time did the market "settle" — stop thrashing and find direction?

**Rule**: Signals before market settles are high risk. If you see multiple relocks in the first 20 minutes, expect the first 1–2 signals to be poor quality. The setup that fires AFTER the chaos ends is usually the best of the day.

---

## Step 3 — Inspect Each Signal (The Evidence Checklist)

For every P1 signal, read:

| Field | Sweet Zone | Danger Zone | What it tells you |
|-------|-----------|-------------|-------------------|
| `ema9h_gap` | 0.8–2.5 | > 5.0 | How far price broke above EMA9H. Extended = move already done |
| `vwap_gap` | 0 to +5 | > +15 or < -2 | Price vs session average. Too far above = overextended |
| `RSI` | 50–65 | < 50 or > 68 | Momentum quality. Barely passing (48–50) = weak signal |
| `time` | 09:40+ | 09:15–09:35 | First 25 min = chaos zone. Wait for market to settle |
| `relock_count` | 0 | ≥ 2 in first 15 min | Multiple relocks = direction unknown, EMA9H unreliable |
| `DTE` | 2–5 | 0 (expiry day) | DTE0 = violent moves, SL too tight |

For every P2 signal, read:

| Field | Sweet Zone | Danger Zone |
|-------|-----------|-------------|
| `below_vwap` | < -5 pts | -2 to 0 (AT vwap, not below) |
| `ema9h_gap` | +0.5 to +2 | > 4 |
| `RSI` | 55–68 | < 55 |

---

## Step 4 — The Three Questions

For every losing trade, ask:
1. **Was this signal valid?** Check ema9h_gap, RSI, VWAP gap, time of day. If any parameter was in the danger zone → weak signal, expected result.
2. **Was this signal avoidable?** Check: was there a recent SL-HIT? Was sl_cooldown working? Was relock_cooldown active? Should a gate have blocked it?
3. **Is this a bug or a market behaviour?** If the same signal fires twice in 33 seconds → bug. If the signal was perfectly valid but market reversed → normal loss, collect data.

For every winning trade, ask:
1. **What made this work?** Which parameters were in optimal range?
2. **Was the peak utilised?** Did trail ladder lock in profit, or did we exit at INITIAL SL?
3. **Could we have held longer?** Was peak 30+ but we exited at +10? Or was +10 the max?

---

## Step 5 — The Day Verdict

After inspecting all signals:

### Score each signal
- ✅ Textbook: All parameters in sweet zone + positive PnL
- 🟡 Marginal: Some parameters borderline, or profitable but suboptimal
- ❌ Bad entry: Parameters in danger zone, OR bug-caused entry

### Compute adjusted PnL
Remove bug-caused entries and recompute. This shows your "clean" PnL — what the system would earn without bugs. If clean PnL is positive but actual is negative → bugs are the problem. If clean PnL is also negative → strategy has an issue.

### Identify one actionable finding
Every analysis should end with ONE clear finding: a pattern, a gate improvement, or a bug. More than one finding per session = scatter. Pick the most impactful and act on it.

---

## Step 6 — Write the Analysis File

**File naming**: `~/VISHAL_RAJPUT/analysis/YYYY-MM-DD_analysis.md`

**Standard sections**:
1. Market context (relocks, DTE, V9 status)
2. Signal-by-signal table + Sherlock read
3. Day summary (P1 total, adjusted total)
4. Key patterns confirmed/refuted
5. Active bugs observed
6. One actionable finding for next session

---

## The EMA9H Gap Taxonomy (Most Important Single Metric)

```
< 0.5 pts  → TINY_GAP     → price barely broke EMA9H, may be noise
0.5–0.8    → MARGINAL     → monitor closely, not ideal
0.8–2.5    → SWEET ZONE   → optimal entry, full confidence
2.5–5.0    → ELEVATED     → caution, partial move done, smaller peak expected
5.0–8.0    → EXTENDED     → high risk, mean-reversion likely, log and watch
> 8.0      → DANGER       → avoid, price overextended, ANALYSIS flag EXTENDED fires
```

**Key insight**: When ema9h_gap > 5, price has already run far above the support band. The EMA9H acts as a trailing floor. If price is 8 pts above it, the band needs to "catch up" before another leg. Entry here catches the exhaust phase, not the momentum phase.

---

## The P1 Exit Trail Ladder

```
Peak < 12       → INITIAL:  SL = entry - 12 (Emergency SL)
Peak ≥ 12       → LOCK+4:   SL = entry + 4  (locked in profit)
Peak ≥ 18       → LOCK+10:  SL = entry + 10
Peak ≥ 24       → LOCK+12:  SL = entry + 12
Peak ≥ 30       → LOCK+20:  SL = entry + 20
Peak ≥ 36       → LOCK+30:  SL = entry + 30
Peak ≥ 40       → LOCK+36:  SL = entry + 36
Peak ≥ 50       → LOCK+50:  SL = entry + 50
```

**When analysing an exit**: Always check what trail level was reached. An exit at INITIAL SL after peak=+2 means the signal never had momentum. An exit at LOCK+10 after peak=+23 means the signal had momentum but reversed before locking LOCK+20 — look at what the market did at that reversal point.

---

## Red Flags — Immediate Investigation Required

| Observation | What it means |
|-------------|---------------|
| `sl_cooldown age=Xs` NEVER appearing in log | BUG: sl_ts not being set on SL-HIT |
| `relock_cooldown` NEVER appearing | BUG: relock_ts not being set |
| `ANALYSIS` lines not appearing after signals | BUG: spot_3m or other variable undefined |
| Same direction fires twice within 60 seconds of a SL-HIT | BUG: sl_cooldown not blocking |
| Signal fires within 2 min of a relock | BUG: relock_cooldown not working |
| `SHADOW-P1 error: name 'X' is not defined` | Scope/variable bug — fix immediately |
| Trade #N and #N+1 identical timestamp | BUG: duplicate entry (threading issue) |

---

## Common Mistakes in Analysis

❌ **Don't blame the market for avoidable losses.**
If the signal was in the danger zone (ema9h_gap=6.45, RSI=49.3 marginal), the loss is the signal's fault. Fix the gate or add a flag.

❌ **Don't count bug-caused trades in strategy evaluation.**
If sl_cooldown was broken and the second trade fired because of a bug, remove it from your PnL analysis. The strategy didn't lose — the bug did.

❌ **Don't optimize on one day.**
One day's data is noise. Use patterns across 5+ similar market days. The ema9h_gap sweet zone was confirmed over 22 days of backtest — trust that, not one session.

✅ **Do ask: "If the bug was fixed, what would PnL have been?"**
This shows you whether the strategy has edge. Bugs hide true performance.

✅ **Do look for the first signal that fires AFTER market settles.**
After volatile opens, the first calm signal is usually the day's best. Find it, study it.

---

## Analysis by Market Regime

| Regime | What to expect | Adjust analysis |
|--------|---------------|-----------------|
| Trending day (1 direction) | P1 fires 1–2 signals in right direction, trails well | Look for VWAP compression after each pullback |
| Rangebound | Multiple small signals, poor peaks | High INITIAL SL rate normal. Look for reduced ema9h_gap |
| Volatile open | 2+ relocks, choppy first 20 min | Expect first 1–2 signals to be poor. Signal quality improves after 09:40 |
| DTE=0 (expiry) | Wild oscillations, fast premium decay | Extended gaps more common, trail ladder reached faster then reversed |
| High VIX (>18) | Larger swings, faster moves | INITIAL SL hit more often even on good setups |

---

*"When you have eliminated the impossible, whatever remains, however improbable, must be the truth."*
*— Applied to trading: when you have eliminated bugs, market noise, and bad entries, what remains is the true edge of the strategy.*
