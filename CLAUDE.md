# VRL Trading Bot — Developer Reference

## Project Overview
Paper trading bot for NIFTY options (Zerodha Kite). Two parallel strategies:
- **V7**: 15-min candle strategy — currently in `V7_SHADOW_MODE = True` (signals computed, no trades)
- **V8**: 3-min candle strategy — **LIVE paper trading** (active)

**Service**: `sudo systemctl restart vrl-main.service`
**Logs**: `~/logs/live/vrl_live.log`
**Trade CSV**: `~/lab_data/vrl_trade_log.csv`
**V8 State**: `VISHAL_RAJPUT/state/vrl_v8_state.json`

---

## Key Files
| File | Purpose |
|------|---------|
| `VRL_MAIN.py` | Main strategy loop, entry/exit execution, Telegram handler |
| `VRL_ENGINE.py` | Gate logic: `check_entry_v8()`, `check_v8_continuation_reentry()` |
| `VRL_DATA.py` | Paths, WebSocket ticks, indicator calculations |
| `VRL_CONFIG.py` | Runtime config (lots, thresholds) |

---

## V8 Architecture

### Entry Gates (check_entry_v8 in VRL_ENGINE.py)
| Gate | Check |
|------|-------|
| G1 | Candle must be green (close > open) |
| G1B | Body ≥ 20% of high-low range |
| G2 | Close > EMA9_low |
| G2B | EMA9_low slope ≥ 0 on BOTH last 2 candles (2-candle slope) |
| G2C | `band_width = ema9_high - ema9_low >= 10` (choppy market filter) |
| G2D | `close >= (ema9_high + ema9_low) / 2` (close in upper half of EMA band) |
| G2E | `other_close <= other_band_mid` (other side in LOWER half of its own band = falling) |
| G3A | RSI ≥ 38 |
| G3B | RSI rise ≥ 2.0 pts vs previous candle |
| xLeg | Other side dying: `close < ema9l - 0.5` (0.5pt margin prevents rounding false positives) |

**G2C + G2D data basis** (9 days, 1404 candles with fwd data):
- Baseline (close > ema9_low only): avg_fwd = +9.2 pts, win% = 39.8%, n=910
- G2C + G2D together: avg_fwd = +20.4 pts, win% = 42.8%, n=381 (58% fewer entries)
- Band width 8-10 = -3.0 avg return (BAD). Band 12-16 = +18.1 avg (BEST).
- Close in lower half of band: +2.5 avg. Upper half: +11.9 avg.

### Exit Ladder (_v8_compute_trail_sl)
```
Peak < 12      → INITIAL: SL = entry - 12 (Emergency SL)
Peak ≥ 12      → LOCK_4:  SL = entry + 4
Peak ≥ 24      → LOCK_12: SL = entry + 12
Peak ≥ 30      → LOCK_20: SL = entry + 20
Peak ≥ 36      → LOCK_30: SL = entry + 30
Peak ≥ 40      → LOCK_36: SL = entry + 36
Peak ≥ 50      → LOCK_50: SL = entry + 50
```

### State Persistence
`_V8_PERSIST_FIELDS` in VRL_MAIN.py controls what's saved to disk on restart.
Any new state key that must survive restarts MUST be added to both:
1. `_v8_state = { ... }` initial dict (so `_load_v8_state` can restore it)
2. `_V8_PERSIST_FIELDS` list (so `_save_v8_state` writes it)

---

## Bugs Found & Fixed (chronological)

### BUG-01: V8 exits fired on candle close, not tick-based
**Symptom**: Peak always showed 0.0 on every exit. EMERGENCY_SL fired at end of minute.
**Root cause**: `_v8_check_exit()` was inside the `_is_new_1min_candle()` gate — only ran once per minute.
**Fix**: Moved `_v8_check_exit()` to run unconditionally every 1-second loop cycle, BEFORE the candle gate.
**Location**: VRL_MAIN.py ~line 2412
**Confirmed**: Next trade showed Peak +4.2 correctly.

---

### BUG-02: xLeg false positive from rounding
**Symptom**: Log showed "PE dying (210.3 < ema9l 210.3)" — same displayed value, both sides treated as dying.
**Root cause**: Comparison `o_close < o_ema9l` allows 0pt margin. Actual values were 210.28 vs 210.32, both display as 210.3.
**Fix**: Changed all 3 xLeg comparison sites to require `o_close < o_ema9l - 0.5`.
**Locations** (VRL_ENGINE.py):
- Fresh entry display: `result["xleg_other_dying"] = (o_ema9l > 0 and o_close < o_ema9l - 0.5)`
- Re-entry gate G4: `if o_ema9l > 0 and o_close >= o_ema9l - 0.5:`
- evaluate_cross_leg: `other_dying = other_close < other_ema9l - 0.5`

---

### BUG-03: EMA slope gate checked only 1 candle (too shallow)
**Symptom**: Bot entered on a spike followed by falling EMA — 1-candle slope appeared flat but trend was reversing.
**Root cause**: Only checked `ema9_low(t) - ema9_low(t-1) ≥ 0`. One candle insufficient to confirm trend.
**Fix**: Now requires BOTH `slope1 = ema9_low(t) - ema9_low(t-1) ≥ 0` AND `slope2 = ema9_low(t-1) - ema9_low(t-2) ≥ 0`.
**Location**: VRL_ENGINE.py Gate 2B block. Uses `opt_3m.iloc[-3]` and `opt_3m.iloc[-4]`.

---

### BUG-04: RSI drift entries (Fix A)
**Symptom**: Entries firing when RSI was barely moving (drift, not momentum).
**Fix**: Added Gate 3B — RSI must rise ≥ 2.0 pts vs previous candle.
**Location**: VRL_ENGINE.py after Gate 3A. Key: `_rsi_rise = round(_rsi_now - _rsi_prev, 2)`

---

### BUG-05: Both-sides cooldown not blocking entries (Fix B) — 3 separate bugs
**Symptom**: PE trade fired at 14:42 despite both CE+PE being rejected every minute from 14:27–14:41.

**Root cause 1**: Default `_v8_both_rejected_ts = 0`. The condition `time.time() - 0 < 180` = False (current unix time >> 180). So cooldown was always False on first use.
**Fix**: Changed condition to `(_v8_both_rej_ts > 0 and time.time() - _v8_both_rej_ts < 180)`.

**Root cause 2**: `_v8_both_rejected_ts` not in `_V8_PERSIST_FIELDS` or initial `_v8_state` dict. Lost on every restart.
**Fix**: Added to both `_v8_state` initial dict and `_V8_PERSIST_FIELDS`.

**Root cause 3**: When one side passed gates but was blocked by cooldown, its `_pe_gate_rejected` flag was NOT set True. So "both rejected" check at end of loop was False → timestamp stopped refreshing → cooldown expired after 3 min even while both sides were still failing.
**Fix** (THEN REVERTED — see BUG-06): Marking cooldown-blocked sides as "failed" was wrong.

---

### BUG-06: Infinite cooldown blocking all trades (caused by BUG-05 fix)
**Symptom**: Zero trades all session. Log showed `both_sides_cooldown age=60s` every minute indefinitely.
**Root cause**: The BUG-05 fix marked "cooldown-blocked" sides as gate-rejected for timestamp refresh. This caused:
- CE always gate-rejected (close < ema9l)
- PE passes gates but blocked by cooldown → marked "failed"
- Both "failed" every minute → timestamp refreshed every minute → cooldown NEVER expired
**Fix**: Removed the "mark cooldown-blocked as failed" logic. Cooldown now expires naturally after 3 min. When PE starts passing gates cleanly while cooldown is active, the cooldown drains and PE fires — that IS the directional signal.
**Current behavior**:
- Both gate-rejected → refresh cooldown (market confused, extend block)
- One passes, one fails → cooldown expires after 3 min → passing side fires

---

### BUG-07: Duplicate trades from thread race condition
**Symptom**: Trades #486/#487 identical (11:45:35, same CE 23500, same entry/exit). Trades #480/#481 at same timestamp.
**Root cause 1 — Duplicate exit**: `_v8_execute_paper_exit` had two separate `with _v8_lock:` blocks. Between Block 1 (read state + guard) and Block 2 (set `in_trade=False`), the lock was released. TG thread (FORCE_EXIT command) and main loop could both pass the guard simultaneously → both write CSV row.
**Fix**: Collapsed to single lock block — read all values, clear `in_trade=False`, update counters, all atomically. CSV write uses captured locals outside the lock.

**Root cause 2 — Duplicate entry**: `_v8_execute_paper_entry` had no guard. Never checked `in_trade` before executing. If called from two paths simultaneously, both ran.
**Fix**: Added `if _v8_state.get("in_trade"): logger.warning(...); return` under lock at top of entry function.
**Location**: VRL_MAIN.py `_v8_execute_paper_entry` (line ~240) and `_v8_execute_paper_exit` (line ~294).

---

## Threading Model
- **Main loop**: Single thread, runs every ~1 second
- **TG listener**: Separate `TGListener` daemon thread (handles Telegram commands)
- **_v8_lock**: Protects all `_v8_state` reads/writes
- **_state_lock**: Protects V7 `state` dict

**Rule**: Any function callable from BOTH main loop and TG thread must hold `_v8_lock` for the entire critical section (check + act atomically). Never check under lock, release, then act.

---

## Known Patterns / Watch For

### "same_candle_guard" blocking for many minutes
Normal for V7 (15-min candles). After firing on candle C, blocks until candle C+1 closes (up to 15 min). NOT a bug.

### Both-sides cooldown armed at market open
Normal. Opening 1-2 minutes often have both CE+PE failing gates (insufficient candles, choppy open). Cooldown blocks for 3 min, then clears.

### EMERGENCY_SL cluster (3+ in a row)
Sign of choppy market or wrong direction bias. No automatic protection yet — consider adding: "3 consecutive EMERGENCY_SL → pause 30 min."

### Duplicate rows in CSV
Should be eliminated by BUG-07 fix. If seen again: check if `_v8_execute_paper_exit` is being called from a new code path not under `_v8_lock`.

---

## Pending / Collect Data
- Post-emergency-SL opposite-side cooldown (2-3 candles block after ESL)
- xLeg dying leg: require dying leg's own EMA also falling 2+ candles
- Max trades/day limit (suggest: 10)
- Max consecutive EMERGENCY_SL limit (suggest: 3 → pause 30 min)
- Daily loss limit (suggest: -50 pts → stop entries)
