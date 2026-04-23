# VRL TASK HANDOFF — READ THIS FIRST

## Repo & Branch
- Repo: vishalraajput24/VISHAL_RAJPUT
- Branch: claude/fix-bugs-refactor-code-rppRh
- Working dir: /home/user/VISHAL_RAJPUT

## What This System Does
Nifty 50 options trading bot. EMA9 Band Breakout strategy on 3-min candles.
- Entry: close > EMA9-low + green candle + body ≥ 40% + gap ≥ 3pts + floor test + fresh breakout
- Exit: Emergency -10pts → EOD 15:20 → Vishal Trail (60%→85%→80%→LOCK+40)
- 2 lots fixed. Scans CE and PE. No entry before 9:35.

## Already DONE (committed)
- VRL_CONFIG.py: VRL_AUTH merged into it ✅
- VRL_DATA.py: minor import fix ✅
- VRL_DB.py: VRL_VALIDATE merged into it ✅

## THE 20-PART TASK PLAN
Do ONE part at a time. Write code, commit, then STOP and tell user "Part X done. Say go for Part Y."
Never do two parts in one message. Never write long explanations. Just do the code change and commit.

### Part 1 — VRL_ENGINE.py (DO THIS NEXT)
Merge 3 files into one. Write the complete new VRL_ENGINE.py containing:
1. ENGINE content (keep all existing functions)
2. CHARGES content (from VRL_CHARGES.py — all constants + calculate_charges + calculate_lot_charges)
3. ALERTS content (from VRL_ALERTS.py — all functions)
4. Add get_margin_available() inline (copy from VRL_TRADE.py lines 46-53)
5. Remove the lazy `from VRL_TRADE import get_margin_available` in pre_entry_checks (line 51 of current ENGINE)
6. Remove loss_streak_gate() function entirely
7. Remove is_setup_building() function entirely
8. Single combined import block at top: logging, time, datetime, threading, pandas, VRL_DATA, VRL_CONFIG
Commit message: "Part 1: merge CHARGES+ALERTS into ENGINE, fix margin import, remove dead functions"

### Part 2 — VRL_CONFIG.py cleanup
Remove BUG-xxx comment blocks. Remove duplicate auth retry code left from VRL_AUTH merge.
Commit: "Part 2: clean VRL_CONFIG - remove BUG comments and duplicate code"

### Part 3 — VRL_DATA.py (A): Remove 10 dead functions
Delete these completely (they are never called anywhere):
- now_ist()
- setup_dated_logger()
- get_atm_straddle()
- get_spot_5min()
- calculate_atr()
- calculate_atr_sl()
- compute_spot_regime()
- get_spot_regime()
- detect_spot_breakout()
- is_expiry_window()
Commit: "Part 3: VRL_DATA remove 10 dead functions"

### Part 4 — VRL_DATA.py (B): Remove 9 more dead functions
Delete these (dead when _compute_bonus removed):
- get_straddle_decay()
- check_vix_warning()
- is_entry_fire_window()
- detect_spot_consolidation()
- calculate_option_vwap()
- calculate_option_fib_pivots()
- detect_volume_spike()
- get_option_prev_day_hl()
- get_spot_vwap()
Commit: "Part 4: VRL_DATA remove 9 more dead functions"

### Part 5 — VRL_DATA.py (C): Enhanced regime detection
Enhance get_spot_indicators() to return 4 extra fields:
- regime_direction: "BULLISH" if EMA9>EMA21 else "BEARISH"
- regime_momentum: "STRENGTHENING" if spread growing vs 3 candles ago, "WEAKENING" if shrinking, "HOLDING" if stable
- adx_status: "CONFIRMED" if ADX>25, "BUILDING" if 15-25, "WEAK" if <15
- regime_summary: one string combining all e.g. "TRENDING|BULLISH|STRENGTHENING|CONFIRMED"
Add a function: send_regime_alert(spot_3m, tg_send_fn) — sends Telegram message with regime data.
This is MONITOR ONLY — no trade blocking.
Commit: "Part 5: enhanced 4-layer regime detection, monitor only"

### Part 6 — VRL_DB.py cleanup
Remove BUG-xxx comment blocks. Remove any duplicate validation logic.
Commit: "Part 6: clean VRL_DB - remove BUG comments and duplicates"

### Part 7 — VRL_LAB.py (A): Remove extra timeframes
Delete ALL of these functions + their schemas + path functions:
- collect_option_5min() + FIELDNAMES_5M + _csv_path_5m()
- collect_option_15min() + FIELDNAMES_15M + _csv_path_15m()
- collect_spot_5min() + FIELDNAMES_SPOT_5M + _csv_path_spot_5m()
- collect_spot_15min() + FIELDNAMES_SPOT_15M + _csv_path_spot_15m()
- collect_spot_60min() + FIELDNAMES_SPOT_60M + _csv_path_spot_60m()
- collect_spot_daily() + FIELDNAMES_SPOT_DAILY + _csv_path_spot_daily()
Keep: collect_option_1min, collect_option_3min, collect_spot_1min and their schemas.
Commit: "Part 7: LAB remove 5min/15min/60min/daily timeframes"

### Part 8 — VRL_LAB.py (B): Remove dead LAB functions
Delete:
- generate_daily_summary() (~160 lines)
- _log_signal_scan() (~130 lines)
- Remove dead columns from FIELDNAMES_SCAN: bias, hourly_rsi, straddle_delta, straddle_period, atm_strike_used, band_width, spot_vwap, spot_vs_vwap, vwap_bonus
Commit: "Part 8: LAB remove daily summary, signal scan log, dead schema columns"

### Part 9 — VRL_MAIN.py (A): Remove 4 dead functions
Delete these functions completely from VRL_MAIN:
- _compute_bonus() — VWAP bonus dead
- _execute_exit() — wrapper, keep _execute_exit_v13
- _warmup_signal() — duplicate of _warmup_info
- _alert_profit_lock() — profit lock being removed
Commit: "Part 9: MAIN remove 4 dead functions"

### Part 10 — VRL_MAIN.py (B): Remove profit lock + streak gate
Find and remove ALL profit lock logic: state["profit_locked"], check_profit_lock(), D.PROFIT_LOCK_PTS references.
Find and remove ALL streak gate logic: consecutive_losses gate, score threshold checks for entry.
Commit: "Part 10: MAIN remove profit lock and streak gate logic"

### Part 11 — VRL_MAIN.py (C): Clean state fields
Remove dead DEFAULT_STATE fields: phase1_sl, phase2_sl, exit_phase, trail_tightened, mode, iv_at_entry, score_at_entry, regime_at_entry, _rsi_was_overbought, _last_trail_candle, current_velocity, peak_history.
Remove all references to these fields throughout MAIN.
Commit: "Part 11: MAIN clean dead state fields"

### Part 12 — VRL_ENGINE.py: Update entry rules
In _evaluate_entry_gates_pure():
- Change body_min from 30 to 40 (update CFG default too)
In pre_entry_checks():
- Change warmup_until default from "09:30" to "09:35"
- Remove the MAX_DAILY_TRADES check
- Remove the MAX_DAILY_LOSSES check
Commit: "Part 12: ENGINE body 40%, warmup 9:35, remove trade/loss limits"

### Part 13 — VRL_MAIN.py (D): Fix ATM strike + consolidate Telegram send
Fix ATM strike display bug: strike is currently _lock_strikes result but Telegram shows wrong value.
Ensure state["strike"] is always set correctly at entry and used in /status.
Consolidate: merge _tg_send_sync into _tg_send (one send function only).
Remove BUG-xxx comment blocks from all of MAIN.
Commit: "Part 13: fix ATM strike display, consolidate tg_send, remove BUG comments"

### Part 14 — VRL_COMMANDS.py: Remove 14 dead commands
Keep ONLY these 12 command functions:
_cmd_help, _cmd_status, _cmd_trades, _cmd_account,
_cmd_pause, _cmd_resume, _cmd_forceexit, _cmd_restart,
_cmd_alerts_on, _cmd_alerts_off, _cmd_reset_exit,
_cmd_livecheck, _cmd_health, _cmd_download
Delete everything else: _cmd_pnl, _cmd_streak, _cmd_slippage, _cmd_spot, _cmd_pivot,
_cmd_edge, _cmd_greeks, _cmd_score, _cmd_regime, _cmd_align,
_cmd_files, _cmd_download_strategy, _cmd_validate, _cmd_source, _cmd_token,
_cmd_download_all, file browser functions, inline keyboard handlers.
Fix _cmd_validate import (currently: from VRL_VALIDATE — change to from VRL_DB).
Fix _cmd_status in-trade to use active_ratchet_tier/active_ratchet_sl not phase1_sl/phase2_sl.
Update _DISPATCH table to only have the 12 commands.
Update _cmd_help to show only 12 commands + correct strategy description.
Commit: "Part 14: COMMANDS keep 12 commands only, remove 14 dead commands"

### Part 15 — VRL_MAIN.py (E): Merge commands inline
Move all 12 command functions from VRL_COMMANDS.py into VRL_MAIN.py.
Remove: import VRL_COMMANDS and the VRL_COMMANDS.setup() call.
Remove VRL_COMMANDS.py dispatch call — wire commands directly in _tg_handle_message.
Commit: "Part 15: merge 12 Telegram commands inline into MAIN"

### Part 16 — VRL_MAIN.py (F): Merge VRL_TRADE inline
Move these functions from VRL_TRADE.py into VRL_MAIN.py:
- get_margin_available()
- verify_order_fill()
- place_entry()
- place_exit()
Delete: place_sl_order(), cancel_sl_order(), modify_sl_order() — never called
Remove: from VRL_TRADE import place_entry, place_exit
Commit: "Part 16: merge VRL_TRADE inline into MAIN, remove dead SL functions"

### Part 17 — VRL_MAIN.py (G): Fix all imports
Update import block at top of MAIN:
Remove: from VRL_AUTH import get_kite (now in VRL_CONFIG)
Remove: import VRL_CHARGES as CHARGES (now in VRL_ENGINE)
Remove: from VRL_TRADE import place_entry, place_exit (now inline)
Add: from VRL_CONFIG import get_kite
Verify all imports resolve correctly.
Commit: "Part 17: fix all imports in MAIN"

### Part 18 — Delete old files
Delete these 4 files (content already merged):
- VRL_CHARGES.py
- VRL_ALERTS.py
- VRL_COMMANDS.py
- VRL_TRADE.py
(VRL_AUTH.py and VRL_VALIDATE.py were already content-merged in earlier commits — delete them too)
Commit: "Part 18: delete 6 merged/dead source files"

### Part 19 — Syntax verification
Run: python3 -m py_compile on all 6 final files.
Fix any import errors or syntax issues found.
Commit: "Part 19: fix any syntax/import issues after consolidation"

### Part 20 — Final commit + summary
Run final line count. Confirm 6 files only. Push to branch.
Report: before/after line counts, files removed, key changes made.

## KEY DECISIONS (from user)
- Body min: 40% (was 30%)
- Warmup: 9:35 (was 9:30)
- MAX_DAILY_TRADES gate: REMOVED
- MAX_DAILY_LOSSES gate: REMOVED
- Profit Lock: REMOVED
- Streak Gate: REMOVED
- Regime detection: Monitor only via Telegram alert, no blocking
- Telegram commands kept (12 total): /help /status /trades /account /pause /resume /forceexit /restart /alerts_on /alerts_off /reset_exit /livecheck /health /download
- LAB: Keep only 1min + 3min for CE/PE + spot. Remove 5min/15min/60min/daily.
- Dead code: Remove completely, no trace left
- BUG-xxx comments: Remove all
- Target: 40% line reduction (13,394 → ~8,000)

## HOW TO DO EACH PART (IMPORTANT)
1. Read the relevant file(s)
2. Make the change using Edit or Write tool
3. Run: python3 -m py_compile <file> to check syntax
4. Git add + commit with the message specified
5. Git push
6. Tell user: "Part X done. Say go for Part Y."
STOP. Wait for user. Do NOT continue to next part automatically.
