# VRL Shadow → Live Trading Roadmap
**Last updated**: 2026-05-22 | **Author**: Vishal Rajput + Claude

---

## Strategy Priority Decision

### When going live — P1 FIRST, P2 stays shadow

| | P1 | P2 |
|--|--|--|
| Signal frequency | High (10–15/day) | Low (2–5/day) |
| Max win seen | +30 pts (S10) | +4 pts |
| Typical BW | varies | 3–5 (too narrow) |
| Big move potential | ✅ Yes | ❌ No (low energy) |
| Live readiness | Phase 3 (gate lock) | Not yet |

**P2 stays in shadow until:** BW consistently > 6 on signals AND avg peak > 10 pts confirmed over 10 days.

---

## Phase 1 — Data Collection (NOW → ~2026-05-30)

**Goal:** Accumulate enough signal data to make gate decisions with confidence.

### What we are collecting
| Data | Why |
|------|-----|
| DELAY-ANALYSIS (+5s/+10s/+30s/+60s) | Classify spike vs real move |
| NIFTY spot delta at each snapshot | Confirm option move is spot-driven |
| gap_vwap per signal | Overextension detection |
| XLEG_CONFIRMED / AMBIGUOUS | Directional confirmation |
| SPOT_EMA_BULL / BEAR | Spot bias at entry |
| BW on P2 signals | Band energy check |
| WEAK_ADX | Directional conviction |

### Rules during Phase 1
- No new hard gates without **5+ data points** confirming the pattern
- Both P1 and P2 run in shadow — no live trades
- Run `python3 VRL_ANALYSIS_BUILDER.py` after market close each day
- Save analysis file daily (`analysis/YYYY-MM-DD_analysis.md`)

---

## Phase 2 — Gate Decisions (~2026-05-30 → ~2026-06-06)

Add gates **one at a time**, in this order. After each gate — check signal count doesn't collapse.

### Gate Queue (priority order)

| Priority | Gate | Current Evidence | Threshold to add |
|----------|------|-----------------|-----------------|
| 1 | Block if `No XLEG_CONFIRMED` | 0W / 6L today | 3 more days same pattern |
| 2 | Block if `gap_vwap > 8` on P1 CE | 0W / 7L today | 3 more days same pattern |
| 3 | Block if `SPOT_EMA_BULL + CE direction` | 0W / 7L today | 3 more days same pattern |
| 4 | Block if `WEAK_ADX < 14` | Mixed (S10 won with ADX 12.7) | DO NOT add — reference only |
| 5 | Early exit if DELAY +5s AND +10s both negative | Spike classifier — cut loss at -4/-5 instead of -12 | Need 15-20 samples (have 3 so far) |

### What NOT to gate (locked decisions)
- `VWAP gap` is reference only — not a hard gate (strong trends override it)
- `EXTENDED_GAP` alone is not a kill signal (S10 2026-05-21 gap=11.10 → +36)
- `DEAD_WINDOW` time blocks — rejected. Good strategy doesn't care about time.

---

## Phase 3 — Live Readiness (~2026-06-06 onwards)

### Checklist before going live with P1

- [ ] Gates locked (no changes for 5 days)
- [ ] 5 consecutive profitable shadow days after gates locked
- [ ] Win rate ≥ 40% on shadow signals
- [ ] Average winner > 2x average loser in pts
- [ ] V2 ratchet showing consistent edge over V1
- [ ] DELAY-ANALYSIS: < 30% SPIKE classification on winning trades
- [ ] Bot running stable — no crashes, no missed signals for 1 week

### Go-live parameters (P1 only)
- **Lots**: 1 lot to start
- **Max trades/day**: 5 (add config gate)
- **Daily loss limit**: -50 pts → stop entries for the day
- **Consecutive SL limit**: 3 in a row → pause 30 min
- **Review cadence**: Every Friday EOD — go/no-go for next week

### P2 go-live conditions (separate evaluation)
- BW on P2 signals consistently > 6
- Peak avg > 10 pts over 10 days
- Win rate ≥ 35% on shadow
- Not before 2026-07-01 earliest

---

## Winning Pattern (confirmed 2026-05-22)

The formula for a good P1 CE trade:
```
gap_vwap < 2          (fresh VWAP crossover, not overextended)
+ XLEG_CONFIRMED      (PE dying cleanly, directional divergence)
+ SPOT_EMA_BEAR       (spot compressed, CE has room to run)
= HIGH PROBABILITY WIN
```

Examples: S10 (+30), S15 (+12), S1 (+10) — all had this combination.

The formula for a guaranteed loss:
```
SPOT_EMA_BULL + CE direction   → 0W/7L (2026-05-22)
gap_vwap > 8                   → 0W/7L (2026-05-22)
No XLEG_CONFIRMED              → 0W/6L (2026-05-22)
```

---

## Data Log

| Date | Signals | P1 P&L | P2 P&L | V2 Edge | Notes |
|------|---------|--------|--------|---------|-------|
| 2026-05-21 | 17 | — | — | — | First shadow day, bugs fixed |
| 2026-05-22 | 19 | -36 | -20 | +20.7 | Extreme chop (22 relocks). XLEG gate live EOD. DELAY+spot live. gap_vwap<2 → 3W/0L. DELAY 3 samples |
| 2026-05-23 | — | — | — | — | |
| 2026-05-26 | — | — | — | — | |
| 2026-05-27 | — | — | — | — | |
| 2026-05-28 | — | — | — | — | |
| 2026-05-29 | — | — | — | — | |
| 2026-05-30 | — | — | — | — | → Gate decision review |

---

*Update this file every Friday. Add daily row after each session.*
