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

---

## Prevention Rules

1. **Never use `logger.debug` in catch blocks** — use `logger.warning` minimum
2. **Never fall back to unlocked ATM** — force relock or skip
3. **Always capture state values BEFORE state reset** — entry, strike, peak, candles
4. **Always pass strike explicitly** — never rely on regex parsing of NIFTY symbols
5. **Test with fresh restart** — many bugs only appear after bot restart
6. **Check function signatures after refactoring** — v12→v13 broke signal scans for 5 days
7. **Auth token expires after 24h** — bot must self-heal, not depend on cron timing
8. **Dual-write errors must be visible** — SQLite failures should log at WARNING level
