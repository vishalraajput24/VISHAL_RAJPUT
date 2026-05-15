# VRL Trading Bot — Developer Reference

## Project Overview
Paper trading bot for NIFTY options (Zerodha Kite). Two parallel strategies:
- **V7**: 15-min candle strategy — currently in `V7_SHADOW_MODE = True` (signals computed, no trades)
- **V8**: 3-min candle strategy — **LIVE paper trading** (active)

**Current version**: `v17` (merged to main 2026-05-14 via PR #7)
**Next**: re-entry disabled (fresh-setup-only), pending merge

**Service**: `sudo systemctl restart vrl-main.service`
**Logs**: `~/logs/live/vrl_live.log`
**Trade CSV**: `~/lab_data/vrl_trade_log.csv`
**V8 State**: `VISHAL_RAJPUT/state/vrl_v8_state.json`

### Deploy after any main merge
```bash
cd ~/VISHAL_RAJPUT && git checkout main && git pull && sudo systemctl restart vrl-main.service
```

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
| G2 | Close > EMA9_low (broke above support band) |
| G2B | EMA9_low slope ≥ 0 for last 2 candles (support band rising, not fake breakout) |
| G3 | `band_width = ema9_high - ema9_low >= 10` (real momentum, not choppy) |
| G4 | `other_close <= other_band_mid` (other side in lower half of its band = falling) |
| G5 | RSI > 50 AND RSI rising ≥ 2 pts vs previous candle |

**Data basis** (9 days, 1404 candles):
- Baseline (close > ema9_low only): avg_fwd = +9.2 pts, win% = 39.8%, n=910
- G3 (bw>=10) alone: avg_fwd = +16.3 pts, win% = 41.7%
- Band width 8-10 = -3.0 avg return (BAD). Band 12-16 = +18.1 avg (BEST).
- G4 ensures directional divergence — both sides rising = sideways = skip.

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
Normal. Opening 1-2 minutes often have both CE+PE failing gates (insufficient candles, choppy open). Cooldown blocks for **1 min** (was 3 min), then clears.

### EMERGENCY_SL cluster (3+ in a row)
Sign of choppy market or wrong direction bias. No automatic protection yet — consider adding: "3 consecutive EMERGENCY_SL → pause 30 min."

### Duplicate rows in CSV
Should be eliminated by BUG-07 fix. If seen again: check if `_v8_execute_paper_exit` is being called from a new code path not under `_v8_lock`.

---

### BUG-08: V8 permanently silent during both-sides cooldown
**Symptom**: No V8 log lines for 30+ minutes. Bot appeared dead.
**Root cause 1**: `silent=_v8_in_both_cooldown` → zero log output while cooldown was active.
**Root cause 2**: `_v8_both_rejected_ts` refreshed every 60s even while cooldown was active → never expired.
**Fix 1**: Changed to `silent=False` — V8 always logs.
**Fix 2**: Timestamp only armed when cooldown is NOT already active:
```python
if _v8_ce_gate_rejected and _v8_pe_gate_rejected:
    if not _v8_in_both_cooldown:
        _v8_state["_v8_both_rejected_ts"] = time.time()
```
**Location**: VRL_MAIN.py main loop cooldown block.

---

### BUG-09: Gate4 silent exception swallowing data errors
**Symptom**: Cross-leg divergence gate bypassed without any log when other-side data was missing.
**Root cause**: Bare `except: pass` — any exception silently skipped Gate4.
**Fix**: `except Exception as _g4e: logger.warning(f"... {_g4e} — gate4 skipped")`
**Location**: VRL_ENGINE.py Gate4 block.

---

### BUG-10: `_other_token` missing from `_v8_state` init dict
**Symptom**: After restart, `_other_token` never restored — `_load_v8_state` uses `if k in _v8_state` guard.
**Fix**: Added `"_other_token": 0` and `"_reentry_exit_price": 0.0` to initial `_v8_state` dict.
**Location**: VRL_MAIN.py `_v8_state` initial dict (~line 188).

---

### BUG-11: `_cmd_forceexit` read token/entry_price outside lock
**Symptom**: Potential race — TG thread read `_v8_state` values between main-loop writes.
**Fix**: Moved both reads inside `with _v8_lock:` block.
**Location**: VRL_MAIN.py `_cmd_forceexit`.

---

### BUG-12: Emergency SL default -10 instead of -12 (3 places)
**Symptom**: If config failed to load, fallback SL was -10 (looser than intended -12).
**Fix**: Changed `-10` → `-12` in VRL_MAIN.py (lines ~1431, ~1555) and VRL_ENGINE.py (line ~576).

---

### BUG-13: STRIKE_STEP used wrong config key
**Symptom**: `CFG.strike_cfg("step", 100)` — key `"step"` doesn't exist; correct keys are `"step_normal"` / `"step_dte0"`.
**Fix**: `CFG.strike_cfg("step_normal", 50)` and `CFG.strike_cfg("step_dte0", 50)`.
**Location**: VRL_DATA.py STRIKE_STEP / STRIKE_STEP_EXPIRY.

---

### BUG-14: ENTRY_CUTOFF_MIN default wrong (10 → 0)
**Symptom**: `CFG.market_hours("entry_cutoff_min", 10)` default 10 → cutoff 15:10 if config fails (should be 15:00).
**Fix**: Changed fallback default to `0`.
**Location**: VRL_DATA.py.

---

### BUG-15: Market close boundary off-by-one second
**Symptom**: `now <= end` allowed 15:30:00 exactly as "market open".
**Fix**: Changed to `now < end` in `is_market_open()` and `is_trading_window()`.
**Location**: VRL_DATA.py.

---

## Design Decisions (Locked)
- **Re-entry disabled** (2026-05-15): After analyzing 11:32 losing re-entry (price reversed 5.5 pts below exit within 33s), decided re-entry adds risk without edge. `_v8_execute_paper_exit` always sets `_reentry_armed = False`. Fresh setup only after every exit.
- **Both-sides cooldown = 1 min** (2026-05-15): Reduced from 3 min. Faster recovery when market picks a direction.
- **Gate 2B** (2026-05-15): EMA9_low slope must be ≥ 0 for last 2 candles. Blocks fake breakouts on falling support.

## Pending / Collect Data
- Post-emergency-SL opposite-side cooldown (2-3 candles block after ESL)
- xLeg dying leg: require dying leg's own EMA also falling 2+ candles
- Max trades/day limit (suggest: 10)
- Max consecutive EMERGENCY_SL limit (suggest: 3 → pause 30 min)
- Daily loss limit (suggest: -50 pts → stop entries)
- Time blackout windows (11:00–11:30, 13:00–14:30) — collect data first
- EOD data collector `VRL_COLLECTOR.py` (cron 15:35): ATM±300 strikes, NIFTY spot 1-min, VIX — save as Parquet
- Trade enrichment tool `VRL_ANALYSIS.py`: `--date`, `--esl`, `--gates`, `--deep` flags

---

## GitHub / Branch Rules
- **main** is protected — direct push blocked, PRs required
- Keep only 1 open PR at a time
- After merge: `git checkout main && git pull` locally to stay in sync
