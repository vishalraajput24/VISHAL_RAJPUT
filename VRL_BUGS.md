# VRL Bug Sheet — All Bugs Found & Fixed

> Reference for future debugging. Never encounter the same bug twice.

---

## BUG-001: PNL shows ₹0 for split lot exits
- **Found:** April 2, 2026
- **Root cause:** When lot1 exits first (trade_done=True), state resets `entry_price=0`. Lot2 exit reads `entry_price=0` → PNL=0.
- **Fix:** Capture `saved_entry_price` BEFORE the exit loop. Pass to `_execute_exit_v13`.
- **File:** VRL_MAIN.py

## BUG-002: Dashboard shows "undefined" for EMA/RSI
- **Found:** April 2, 2026
- **Root cause:** Fallback signal block returned old v12 fields (`gate_3m`, `score`). Dashboard JS expects v13 fields (`ema_gap`, `rsi`).
- **Fix:** Fallback returns `ema9:0, ema21:0, ema_gap:0, rsi:0` instead of v12 fields.
- **File:** VRL_MAIN.py `_write_dashboard()`

## BUG-003: Dashboard signals show 0 during trade
- **Found:** April 2, 2026
- **Root cause:** Dashboard scan passed `{}` as `all_results` during trade. No live data.
- **Fix v1:** Added scan inside milestone block — but only ran on milestones.
- **Fix v2:** Moved scan to run EVERY cycle during trade, before exits.
- **Fix v3:** Used active trade token instead of locked tokens (which may differ after relock).
- **File:** VRL_MAIN.py strategy loop

## BUG-004: Market tab JS crash on `mk.regime`
- **Found:** April 2, 2026
- **Root cause:** `mk.regime.includes('TREND')` crashes when `regime` is undefined/null.
- **Fix:** `(mk.regime||'').includes('TREND')`
- **File:** VRL_WEB.py / VRL_DASHBOARD.html

## BUG-005: Signal scans not writing since v13.0
- **Found:** April 6, 2026
- **Root cause:** `_log_signal_scan` called `check_entry` with old v12 params (`profile=`, `strike=`, `session=`). v13's `check_entry` signature is `(token, option_type, spot_ltp, dte, expiry_date, kite)`. TypeError silently caught by `except Exception` with `logger.debug`.
- **Fix:** Updated call to v13 signature. Changed `logger.debug` to `logger.warning` so failures are visible.
- **Lesson:** NEVER use `logger.debug` in catch blocks for critical functions. Use `logger.warning` minimum.
- **File:** VRL_LAB.py `_log_signal_scan()`

## BUG-006: Signal scans skip when ATM tokens not resolved
- **Found:** April 6, 2026
- **Root cause:** `_current_atm_tokens` only set during 3-min collection. If 3-min collection fails, tokens stay None and scans are skipped forever.
- **Fix:** `_log_signal_scan` now self-resolves ATM tokens if None.
- **File:** VRL_LAB.py

## BUG-007: WebSocket dies over weekend, bot blind Monday
- **Found:** April 6, 2026
- **Root cause:** Kite API token expires after 24h. AUTH cron only ran Mon-Fri. Weekend = no auth = dead WebSocket by Monday.
- **Fix:** Added `check_and_reconnect()` — if spot tick stale 5+ min during market hours, auto re-auth + restart WebSocket. Max 1 attempt per 5 min.
- **File:** VRL_DATA.py, VRL_MAIN.py

## BUG-008: Wrong strike in Telegram alerts (CE 22700 vs CE 22600)
- **Found:** April 6, 2026
- **Root cause 1:** `state["strike"]` was set from fresh ATM calculation at entry time, not from the locked strike used for actual order placement. Spot at 22644 → ATM=22700, but locked strike=22600.
- **Root cause 2:** `_short_sym()` regex couldn't parse NIFTY symbol correctly — `NIFTY\d+` consumed strike digits too.
- **Root cause 3:** Exit alerts called `_short_sym(symbol, direction, 0)` with strike=0.
- **Fix:** `state["strike"]` uses `entry_result["_strike"]` (locked strike). All `_short_sym` calls pass correct strike. Exit captures `_exit_strike` before state reset.
- **File:** VRL_MAIN.py

## BUG-009: Wrong strike entry (bot enters 22700 instead of locked 22600)
- **Found:** April 6, 2026
- **Root cause:** When `_locked_tokens` was empty (after restart or API failure), bot fell back to fresh ATM tokens instead of relocking.
- **Fix:** Force `_lock_strikes()` when locked tokens empty. Never use unlocked ATM. If relock fails, skip cycle.
- **File:** VRL_MAIN.py

## BUG-010: /token command crashes — args is list not string
- **Found:** April 4, 2026
- **Root cause:** Telegram dispatcher passes args as `list` (parts[1:]), but `_cmd_token` called `args.strip()` expecting a string.
- **Fix:** `isinstance(args, list)` check at start.
- **File:** VRL_COMMANDS.py

## BUG-011: /token usage text shows HTML tags
- **Found:** April 4, 2026
- **Root cause:** `<name>` and `<days>` in usage text parsed as HTML tags by Telegram.
- **Fix:** Changed to `[name]` and `[days]`.
- **File:** VRL_COMMANDS.py

## BUG-012: prev_close not saved at EOD (LTP=0)
- **Found:** April 6, 2026
- **Root cause:** WebSocket disconnects before 15:35, `get_ltp()` returns 0, `prev_close` stays 0.
- **Fix:** REST API fallback via `kite.ltp("NSE:NIFTY 50")`.
- **File:** VRL_MAIN.py

## BUG-013: gross_pnl_rs vs pnl_rs mismatch in trade log
- **Found:** April 6, 2026
- **Root cause:** `pnl_rs` used `_lot_qty` but `gross_pnl_rs` was set to `pnl_rs`. Charges calculator used different qty.
- **Fix:** Both now use `_ch["gross_pnl"]` from charges calculator.
- **File:** VRL_MAIN.py `_log_trade()`

## BUG-014: Trade log shows strike=0 for some trades
- **Found:** April 6, 2026
- **Root cause:** State `strike` not set properly, or trade logged after state reset.
- **Fix:** `_log_trade` falls back to ATM calculation if strike=0. Entry uses locked strike.
- **File:** VRL_MAIN.py

## BUG-015: WebSocket stale at market open — token path mismatch
- **Found:** April 10, 2026
- **Root cause:** VRL_AUTH wrote access_token.json to ~/state/ but VRL_MAIN read from ~/VISHAL_RAJPUT/state/. MAIN never saw fresh tokens at startup, WebSocket stayed stale until manual reconnect after market opened.
- **Fix:** VRL_AUTH now imports TOKEN_FILE_PATH from VRL_DATA.py — single source of truth for token location. Both files now write/read from ~/VISHAL_RAJPUT/state/access_token.json.
- **Lesson:** Never hardcode paths in two files. Always import from a central constant.
- **File:** VRL_AUTH.py, VRL_DATA.py

## BUG-016: Dashboard.json path mismatch (same root cause as BUG-015)
- **Found:** April 10, 2026
- **Root cause:** VRL_DATA.py BASE_DIR was set to os.path.expanduser("~") = /home/vishalraajput24/. So STATE_DIR resolved to ~/state/ (home), but VRL_WEB.py used its own BASE = repo dir, looking at ~/VISHAL_RAJPUT/state/. VRL_MAIN wrote dashboard.json to home/state, VRL_WEB read from repo/state, /api/dashboard returned empty, VWAP/Fib/Vol/PDH all missing on dashboard.
- **Fix:** Defined REPO_DIR = directory of VRL_DATA.py file. STATE_DIR now uses REPO_DIR instead of BASE_DIR. All state files now live inside the repo, consistent with VRL_WEB's expectations.
- **Lesson:** Same lesson as BUG-015 — never define paths in two files. Always use a single REPO_DIR constant. BASE_DIR (home) should only be used for things genuinely outside the repo (like ~/.kite_env).
- **File:** VRL_DATA.py

## BUG-017: DB corruption hidden at DEBUG level
- **Found:** April 10, 2026
- **Root cause:** Every DB exception handler in VRL_DB.py used `logger.debug`, so a malformed SQLite file failed silently every minute (twice per cycle, once for CE scan + once for PE scan). No Telegram alert, no WARNING in the live log — the only trace was DEBUG-level "Query error: database disk image is malformed" that never made it to production log files.
- **Fix:** Added `_report_db_error(context, exc)` helper in VRL_DB.py. First occurrence of each distinct error surfaces at WARNING, subsequent repeats throttle down to DEBUG to avoid log spam. Any error containing "malformed", "corrupt", or "not a database" also triggers a one-shot Telegram alert with recovery instructions. All 8 catch blocks in VRL_DB.py now route through this helper. Added a startup `PRAGMA quick_check` in `init_db()` so corruption is detected immediately on bot start instead of waiting for the first query.
- **Lesson:** Reinforces Prevention Rule #1 — NEVER use `logger.debug` in DB/critical catch blocks. If a failure mode can leave the bot running but broken, it MUST alert.
- **File:** VRL_DB.py

## BUG-018: /status shows stale SL during profit floor
- **Found:** April 10, 2026
- **Root cause:** `/status` read `phase1_sl` (original hard SL) even after profit floors ratcheted the active SL upward. Peak could be +25pts but /status showed the original entry-12 SL.
- **Fix:** Compute active SL by iterating config `profit_floors` and taking the highest applicable `entry + lock` value. Display whichever is higher: the base SL or the floor SL.
- **File:** VRL_COMMANDS.py `_cmd_status()`

## BUG-019: /help and startup banner show v13.0 strategy text
- **Found:** April 10, 2026
- **Root cause:** Strategy text in `/help` footer and `_alert_bot_started()` banner was never updated from v13.0 (EMA gap≥3, RSI split 70/75, Widening). Showed wrong cooldown values and exit rules.
- **Fix:** Updated both locations to reflect v13.5 actual logic: FAST 1m +14/4c, CONFIRMED 3m +20/3c, divergence gate, candle close SL -12, profit floors, 5min same-dir cooldown.
- **File:** VRL_COMMANDS.py, VRL_MAIN.py

## BUG-020: DB/CSV/state PNL sync drift after DB rebuild
- **Found:** April 10, 2026
- **Root cause:** After BUG-017 DB rebuild, trades table was empty but CSV and state.json carried old data. Dashboard showed mismatched counts (DB=2 CSV=3) and PNL (state=-16 dashboard=-14.1).
- **Fix:** Added CSV→DB backfill on startup: reads today's CSV trades, inserts any missing into DB. Runs once after `_reconcile_positions()`, before WebSocket starts.
- **Lesson:** After any DB rebuild, always reconcile from the CSV source of truth.
- **File:** VRL_MAIN.py `main()`

## BUG-021: Relock SKIPPED loop traps bot on stale strikes
- **Found:** April 10, 2026
- **Root cause:** When ATM drifted but either side had momentum >10pts, relock was skipped indefinitely. Bot could stay on stale strikes for 5+ minutes while market moved away, missing entries on the correct strike.
- **Fix:** Added `_relock_skip_count` — max 3 consecutive skips. After 3 skips, force relock regardless of momentum. Counter resets on successful relock.
- **File:** VRL_MAIN.py (relock section in strategy loop)

## BUG-022: Dashboard position card shows ₹0 rupee value
- **Found:** April 10, 2026
- **Root cause:** Dashboard JS read `pos.lot1_active` / `pos.lot2_active` which don't exist at the top level of the position JSON. The lot active status lives inside `pos.lot1.status` and `pos.lot2.status`. So `al=0` → `prs=0`.
- **Fix:** Read lot active status from `pos.lot1.status==='active'` and `pos.lot2.status==='active'`. Fallback to 2 lots if both are undefined.
- **File:** static/VRL_DASHBOARD.html

## BUG-023: Dashboard lot cards show "SL ₹0"
- **Found:** April 10, 2026
- **Root cause:** Dashboard read `pos.sl` which isn't set in the position JSON. The SL lives inside `lot1.sl` / `lot2.sl`. Additionally, profit floor ratchets weren't computed for display.
- **Fix:** Read SL from lot objects, then iterate profit_floors to ratchet up based on `pos.peak`. Shares the same floor computation as BUG-018.
- **File:** static/VRL_DASHBOARD.html

## BUG-024: Dashboard RSI bar shows stale "split at 70" label
- **Found:** April 10, 2026
- **Root cause:** v13.0 lot-splitting at RSI 70 was removed in v13.5, but the dashboard label still said "RSI → split at 70" with the orange marker at 87.5% (70/80).
- **Fix:** Changed label to "RSI → cap 72", scaled bar as rsi/72, removed the orange split marker.
- **File:** static/VRL_DASHBOARD.html

## BUG-025: Day PNL stat excludes floating open position
- **Found:** April 10, 2026
- **Root cause:** Day P&L stat showed only `td.pnl` (realized), ignoring any open position's unrealized PNL. Users saw a red day total while a profitable trade was open.
- **Fix:** Display PNL as `realized + open_floating`. Added breakdown text "Realized X Open Y" below when a position is active.
- **File:** static/VRL_DASHBOARD.html

## BUG-026: Trade cards show MINIMAL badge for legacy trades
- **Found:** April 10, 2026
- **Root cause:** Trades from v13.0 era had `entry_mode="MINIMAL"` or `"EMA"`. Dashboard rendered these with the blue CONFIRMED styling, which was confusing alongside real FAST/CONFIRMED v13.5 badges.
- **Fix:** Map `MINIMAL` and `EMA` modes to a gray "LEGACY" badge. New trades always write `entry_mode` from `entry_result` in VRL_MAIN's state capture (already working since v13.5).
- **File:** static/VRL_DASHBOARD.html

## BUG-027: Profit floor SL never persisted to state — peak +10.8 trade exited at 0pts
- **Found:** April 10, 2026
- **Root cause:** PE 24000 trade peaked at +10.8pts (above the +10 floor threshold) but the engine's manage_exit only had the dynamic trail which activates at peak ≥15 (FAST) or ≥20 (CONFIRMED). The static `profit_floors` config was never read by the engine — only used for display in VRL_COMMANDS. So no floor ratcheted the SL. Later, FORCE_EXIT used `option_ltp or entry_price` and with a stale LTP of 0, exited at entry price → PNL 0.
- **Fix:** Added static profit floor check in manage_exit between CANDLE_SL and DYNAMIC TRAIL. Reads `profit_floors` from config, finds highest applicable floor for current peak, persists to `state["_static_floor_sl"]` AND `state["phase1_sl"]`, logs "[FLOOR] Peak X crossed, SL ratcheted to Y". Also fixed FORCE_EXIT to use `max(entry_price, floor_sl)` as fallback when LTP is 0.
- **Lesson:** Config entries that are never read by the engine are worse than missing — they create a false sense of safety. Always verify config keys are actually consumed by the code path they're supposed to control.
- **File:** VRL_ENGINE.py manage_exit(), VRL_MAIN.py FORCE_EXIT

## BUG-028: Phantom trade state after market close
- **Found:** April 10, 2026
- **Root cause:** CE 24050 trade entered at 15:05:31 was still open in state when market closed at 15:30. The EOD cutoff handler fires at 15:28 (paper) but only if the strategy loop reaches that code within that exact minute. If the bot is slow, restarted, or the loop misses the window, the trade survives in state as in_trade=True forever. After restart, the bot resumed polling option LTP via REST every second to monitor a phantom position on a closed market.
- **Fix:** Three layers of protection: (1) Catch-all EOD handler at 15:30+ that force-exits any still-open position, gated by `_eod_exited` flag to run once per day. (2) Startup safety check that clears phantom trade state if bot starts outside market hours with in_trade=True, sends a Telegram alert with the phantom position details, and calls _save_state(). (3) Shutdown handler logs a warning when shutting down with an open trade so the operator knows state was preserved. Verified all 6 exit code paths call _save_state() after setting in_trade=False.
- **Lesson:** Every state mutation must be immediately persisted. Never assume the next loop iteration will save state — the process may be killed first. EOD handlers must have a catch-all at market close time, not just a narrow minute window.
- **File:** VRL_MAIN.py

## v13.7: Strategy enhancements (April 10, 2026)
- **RSI cap raised from 72 to 75** — PE momentum hit 42pts at RSI 78 but was blocked at 72. Cap 75 allows bigger moves; RSI_BLOWOFF exit at 80 still protects.
- **Peak protection floor at +5 → SL entry-6** — damage control: tightens SL from -12 to -6 once peak crosses +5. Prevents +10 peak trades from exiting at -12.
- **Extended runner floor at +50 → lock +42** — for expiry day runners that exceed +40.
- **Entry cutoff at 15:10 IST** — no new entries in last 20 minutes. Existing positions managed until EOD.
- **Near-miss logging** — logs `[NEAR_MISS]` when RSI cap blocks a would-fire signal, for future threshold analysis.
- **Relock skip reduced from 3 to 2** with new override: force relock if spot drifted >30pts from lock point regardless of momentum.
- **Full floor ladder**: +5→-6, +10→+2, +20→+12, +30→+22, +40→+32, +50→+42

## v13.8 FINAL: Strategy simplification + stop hunt recovery (April 11, 2026)
- **Change 1: FAST path simplified** — removed momentum points math. Entry now: 2 green candles closing above EMA9 with RSI rising + other side below EMA9. Binary checks, no thresholds.
- **Change 2: Time-aware RSI cap** — 78 morning (9:15-10:15), 72 midday (10:15-14:00), 75 afternoon (14:00-15:10). Adapts to market phase. Aggressive mode adds +3.
- **Change 3: Straddle aggressive mode** — when straddle decays 20%+, system activates aggressive mode: RSI cap +3, CONFIRMED threshold 20→15. Resets daily.
- **Change 4: Spot alignment** — CE requires spot above EMA9, PE requires spot below EMA9. Prevents option-only manipulation signals.
- **Change 5: Stop hunt recovery** — cooloff auto-skipped when previous exit was CANDLE_SL and price recovers 5+ pts within 1+ min. Allows re-entry after genuine stop hunts.
- **Also: last_exit_price now saved** on every exit for stop hunt recovery detection.
- **Expected impact**: Cleaner entries, fewer false signals, faster re-entry on genuine trends, adaptive to market rhythm.

## BUG-029: Stale WebSocket at market open due to token refresh without process restart
- **Date:** April 13, 2026
- **Root cause:** 8 AM cron runs VRL_AUTH.py successfully and writes a fresh access_token.json to disk. BUT vrl-main was already running from the previous session with yesterday's token cached in memory. It never re-reads the file. WebSocket keeps trying with the dead token → stale ticks for 2+ hours at market open until the user manually restarts vrl-main.
- **Fix (4 layers of protection):**
  1. **Crontab**: AUTH now chains `&& sudo /bin/systemctl restart vrl-main` so the process always restarts with the fresh token. Single passwordless sudoers rule already in place.
  2. **VRL_MAIN startup token check**: Before calling `get_kite()`, main() explicitly reads access_token.json, compares date to today, logs a loud warning + sends a Telegram alert if stale. `get_kite()` then auto-refreshes. Self-heals even if cron fails.
  3. **WS auto-heal tightened (VRL_DATA.check_and_reconnect)**: stale threshold 5min→3min, rate limit 5min→10min, new `set_autoheal_callback()` hook lets VRL_MAIN register a Telegram alert when auto-heal fires. Catches WebSocket death mid-session.
  4. **VRL_PRECHECK.py at 9:10 IST**: standalone script runs 4 checks (service alive, token fresh, Kite API responds, dashboard.json fresh). Fires a Telegram alert 5 min before market open so operator has time to intervene. Auto-heals service + token if broken.
- **Lesson:** Never assume child processes see file changes — must explicitly restart or hot-reload. Defense in depth: 4 independent layers means even if 3 fail, the 4th catches it.
- **File:** crontab, VRL_MAIN.py, VRL_DATA.py, VRL_PRECHECK.py (new)

## BUG-030: Dashboard appears dead during indicator warmup period
- **Date:** April 13, 2026
- **Root cause:** During the 9:15-10:00 IST warmup window (14 candles of 3-min data needed for reliable RSI/EMA), the strategy loop skips signal scanning and `_build_signal()` returned an empty dict. The dashboard rendered blank signal cards with no status indicator. User could not distinguish between "bot dead" and "bot correctly waiting for indicators to stabilize".
- **Fix (3 layers of visibility):**
  1. **Dashboard JSON**: Added `_warmup_info()` helper + `_warmup_signal()` placeholder in VRL_MAIN. During warmup, CE and PE signal blocks are populated with `status="WARMUP"`, `warmup_progress`, `warmup_needed`, `warmup_eta`, and a `message` field. Market block also gets `warmup_progress/needed/eta` for the header.
  2. **Dashboard HTML**: Signal render function detects `status==='WARMUP'` and renders a dedicated amber card with an hourglass icon, progress bar (`warmup_progress / warmup_needed`), ETA text ("Trading resumes HH:MM"), and a gentle `wmpulse` opacity animation to show it's alive. The WARMUP header tag also pulses (`wmtag`) with a tooltip.
  3. **Telegram `/status`**: Reads `vrl_dashboard.json` and if market_open + !indicators_warm, prepends a warmup block: `🟡 WARMUP (X/14 candles) | ETA: HH:MM | Trades blocked until indicators stable`.
  - Plus: `[MAIN] Warmup progress: X/14 candles (ETA: HH:MM)` logged once per minute to live log.
- **Lesson:** Silent waiting states in UI look identical to failure states. Always communicate what the system is doing, even when it's intentionally idle.
- **File:** VRL_MAIN.py, static/VRL_DASHBOARD.html, VRL_COMMANDS.py

---

## Prevention Rules

1. **Never use `logger.debug` in catch blocks** — use `logger.warning` minimum. For DB/IO errors that can repeat every cycle, throttle repeats via a per-error-signature `seen` set rather than downgrading the first sighting (see `_report_db_error` in VRL_DB.py).
2. **Never fall back to unlocked ATM** — force relock or skip
3. **Always capture state values BEFORE state reset** — entry, strike, peak, candles
4. **Always pass strike explicitly** — never rely on regex parsing of NIFTY symbols
5. **Test with fresh restart** — many bugs only appear after bot restart
6. **Check function signatures after refactoring** — v12→v13 broke signal scans for 5 days
7. **Auth token expires after 24h** — bot must self-heal, not depend on cron timing
8. **Dual-write errors must be visible** — SQLite failures should log at WARNING level
