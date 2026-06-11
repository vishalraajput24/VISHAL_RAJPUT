# VRL Trading Bot — Developer Reference

> Last resynced: 2026-06-11 (feat/watch-smi-fno). Single-file bot: `VRL_MAIN.py` (~10,000 lines).
> Grep by symbol name — line numbers in this doc are approximate.

---

## 🤖 Agent Sync Protocol (Claude Code ↔ Google Antigravity)

**Both agents must read this file first before any code change.**

If you are Google Antigravity reading this:
1. Read this entire CLAUDE.md — it is the single source of truth for architecture, strategy values, and rules.
2. After any change you make to `VRL_MAIN.py`, `config.yaml`, `watch_trade.py`, or `trace_trade.py` — update the relevant section of this file in the same PR. Do not leave CLAUDE.md stale.
3. **Never change locked strategy values** (OPP DECAY [−8,−4], initial SL cap entry−10, PROTECT @+9 → entry−2, LOCK_4 @+11 → entry+4, TRAIL_10 @+18, 1 lot single market fill) without the user's explicit instruction.
4. Follow the PR workflow (branch → commit → `gh pr create` → `gh pr merge --squash`) — no direct pushes to main.
5. If you add a new `_v10_state` key that must survive restart, add it to BOTH the initial `_v10_state` dict AND `_V10_PERSIST_FIELDS`. (Note: internal code still uses `_v8_*` prefix — full rename pending a dedicated PR.)
6. Update the `> Last resynced:` date at the top of this file whenever you resync it.

Claude Code follows the same rules. Both agents stay in sync through this file and git history.

---

## Project Overview
NIFTY weekly-options bot. Zerodha **Kite** for market data, **m.Stock** for live order placement.

- **Mode** is config-driven: `config.yaml` → `mode: paper | live` → `D.PAPER_MODE = CFG.is_paper()`
  - **paper**: fills simulated, logged as `PAPER_*`, zero slippage
  - **live**: real orders via m.Stock (`MSTOCK.ms_place_buy` / `ms_place_sell`) with limit-price buffer
- **Strategy**: V10 Golden — 1-min engine. `V10_LIVE = True`. Version string: `v20`.
- No shadow scanners, no P3, no V2 trackers — single code path.

### Module-as-namespace pattern
`VRL_MAIN.py` aliases itself:
```python
D = CFG = LEVELS = CHARGES = MSTOCK = sys.modules[__name__]
```
`CFG.is_paper()`, `D.PAPER_MODE`, `MSTOCK.ms_place_buy()` etc. all resolve to functions in this file.
When searching for "dead" code, count dotted refs (`D.foo`, `MSTOCK.foo`) — a function can be live via aliases.

---

## V10 Golden Strategy — LOCKED VALUES

### Entry gates (both must pass)
| Gate | Condition | Constant |
|------|-----------|----------|
| **MOMENTUM** | 1-min option `close >= ema9_high + 3.5` | `V10_MIN_EMA9H_GAP = 3.5` (hard gate) |
| **OPP DECAY** | opposite leg: `close − ema9_low` in `[−8.0, −4.0]`; **11:30–14:30 deep band `[−8.0, −6.0]`** | `V10_DECAY_DEEP_START/END`, `V10_DECAY_HIGH_DEEP/NORM` |

- **Midday deep-decay window (paper test, 2026-06-12)**: 11:30–14:30 entries need opposite-leg decay ≤ −6 (shallow-decay midday entries ran 2W/9L −34 pts over 06-10/06-11; study: `~/lab_data/xleg_context_study.py`). Owner will review accuracy after the paper day before any live decision.

- `V10_OPEN_BLACKOUT_END = dtime(9, 45)` — no entries before 09:45
- **Same-candle guard** (`_last_fired_candle_ts`) — no double-entry on same 1-min candle
- **Exit-candle cooldown** (`_last_exit_candle_ts`) — no re-entry on same candle as exit
- **Same-side 3-min blocker** (`_last_exit_direction_v10` + `_last_exit_time_unix`) — after any exit, same direction blocked for 180s (any strike). Prevents post-trail chasing and rapid same-side re-entries
- ~~Exhausted-loss re-entry block~~ — **removed 2026-06-11 (owner instruction)**: live counterfactual showed it skipped only 1 of 10 losers while blocking recovery winners (incl. a +32). Replaced by the midday deep-decay window above.

### Execution — single lot
Config: `lots_fixed: 1`, `lot_size: 65` → 65 qty, single market fill at the last 1-min candle close.
(Split-lot 50/50 with a Lot 2 limit order was removed 2026-06-10 — user found Lot 2 added complexity with no edge; trades often hit SL before the limit mattered.)

- **Initial SL**: `ema9_low` of breakout candle (`_v10_state["initial_sl"]`), **capped at `entry − 10.0`** (max-risk cap, owner-approved 2026-06-11, validated by `sl_replay_study.py`: 0 winners clipped over 53 replays). Fallback: `entry − 5.0` if ema9_low ≥ entry

### Exit ladder — `_v10_compute_trail_sl(entry_price, peak_pnl, initial_sl)`
Tick-based (~1s), runs BEFORE the candle gate (BUG-01):

```
peak < 9 pts   → INITIAL    : SL = initial_sl  (ema9_low capped at entry − 10)
peak ≥ 9 pts   → PROTECT    : SL = max(initial_sl, entry − 2.0)
peak ≥ 11 pts  → LOCK_4     : SL = max(initial_sl, entry + 4.0)
peak ≥ 18 pts  → TRAIL_10   : SL = max(initial_sl, entry + 4.0, peak_ltp − 10.0)
```

Exit reasons: `EMERGENCY_SL` · `PROTECT_2` · `LOCK_4` · `VISHAL_TRAIL` · `EOD_EXIT` · `FORCE_EXIT` (TG `/forceexit`)
(LOCK_4 replaced BREAKEVEN on 2026-06-10. PROTECT tier + LOCK_4 trigger 12→11 added 2026-06-11, owner-approved,
validated by `sl_replay_study.py`: +31.5 pts over 54 replayed trades, 0 trades made worse.)

- **EOD hard-close**: `config.yaml` → `exit.ema9_band.eod_exit_time` = **"15:15"** (changed from 15:20 on 2026-06-10). Checked tick-based inside `_v8_check_exit()`.
- **No-tick safeguards** (PR #210, 2026-06-10 incident — restart after 15:00 left the open trade blind, EOD never fired):
  1. Startup resubscribes the in-trade token + `_other_token` unconditionally (option tokens are otherwise only subscribed via `_lock_strikes()`, which is gated to the 09:15–15:00 trading window).
  2. If `ltp <= 0` when EOD time is reached, the trade is force-closed at average entry price (same fallback as `/forceexit`) instead of silently skipping the exit check.

### Per-day counters
`_v10_state`: `_trades_today`, `_wins_today`, `_losses_today`, `_pnl_today_pts` — reset at midnight. No hard daily cap.

---

## File layout

| Path | Purpose |
|------|---------|
| `VRL_MAIN.py` | Everything: config, brokers, strategy loop, entry/exit, TG handler, web server |
| `config.yaml` | Runtime config — `mode`, instrument, lots, EMA bands, thresholds, market hours |
| `trace_trade.py` | Post-trade audit script (standalone, no Claude dependency) |
| `watch_trade.py` | Live alignment watcher — polls state/dashboard/TG every 2s (standalone) |
| `sl_replay_study.py` | SL-ladder replay backtest — re-runs historical trades against `lab_data/options_1min` candles under candidate SL rules (standalone, read-only) |
| `screener/` | Stock F&O SMI paper engine + multibagger screeners (separate processes, not imported by VRL_MAIN) |
| `static/VRL_DASHBOARD.html` | **Generated artifact** — overwritten from `_WEB_HTML` on every restart. Never edit directly. |
| `state/vrl_v8_state.json` | **Primary V10 engine state** — `_v10_state` (filename uses legacy `v8` prefix — rename pending) |
| `state/vrl_live_state.json` | Legacy V7 state — still written by bot, not used by V10 strategy logic |
| `state/vrl_dashboard.json` | Dashboard snapshot — full rebuild (`_write_dashboard`) once per 1-min candle + after every exit (V10 and V7 paths); fast path `_update_dashboard_ltp` every 5–10s only refreshes ts/LTP/position, never the `today` block |

### Stock F&O — SMI paper engine (since 2026-06-12)
Old daily-pick screener strategy **retired 2026-06-11** (crons removed: `vishal_fno_screener.py`
15:40 + `fno_collector.py --tick`; files kept on disk for rollback; final book +₹18,709,
archived to `fno_tracker_archive.csv`). `fno_collector.py --morning` still runs (universe/OHLCV cache).

New engine: `screener/smi_paper.py` — cron every 15m bar close +2min (09:47–15:31 Mon–Fri),
log `~/logs/smi_paper.log`. **2-week paper validation — strategy constants FROZEN, no tuning
before ~2026-06-25.** Spec (evidence: `smi_backtest.py`, `smi_pe_tuning.py`, `smi_single_filter.py`):
- SMI RMA(Wilder) 14/3/3 on 15m + 1h. CE: cross up −40 same-bar, SMI>signal, 1h SMI>signal+5,
  1h SMI in (0,50). PE: cross down +45 entry within 6 bars (first confirm), SMI<signal,
  1h SMI<signal−5, 1h SMI in (0,50), stock below day VWAP. NIFTY 1h SMI bear = PE conviction tag.
- Entries on bars labelled 09:30–14:30; paper fill = 1 lot nearest-expiry ATM option at LTP.
- Exits (stock-price driven): SL 1% of entry, trail arms at +1.5% peak → exit close vs SMA8,
  force close at 15:15 bar. Backtest: ~66% win, ~+0.49%/trade stock-level (40 days, 119 stocks).
- State `smi_paper_state.json` · dashboard rows in `fno_tracker.csv` (`structure=SMI`) ·
  clean trade log `smi_paper_log.csv` for the review.

### Stale artifacts in state/ (do not rely on)
- `vrl_shadow_state.json` — shadow scanner removed; file is stale
- `vrl_v10_state.json` — orphaned file; active state is in `vrl_v8_state.json` (rename pending)
- `bw_gap_study.csv` — BW/RSI study; gates removed in V10 Golden
- `vrl_zones.json` — zones engine removed; `/api/zones` route deleted 2026-06-10

### Dashboard source of truth
`_WEB_HTML = r"""..."""` string in `VRL_MAIN.py` (~line 8956). `_start_web_server()` overwrites
`static/VRL_DASHBOARD.html` from this string on every startup.
**Always edit `_WEB_HTML` — never the static file.**
Only `vrl-main.service` runs (port 8080). `vrl-web.service` was retired 2026-06-07.

Tabs: **SIG** (V10 gates + position + MSTOCK account + rolling performance) · **F&O** (stock
options portfolio, lots/invested/P&L) · **TRD** (trade log) · **WKLY** (multibagger model
portfolio, 1 share each) · **FILES**. The MKT tab was retired 2026-06-10 — it showed V7-era
analytics (spot/option multi-TF tables, fib pivots, zones, straddle) that no V10 gate uses;
its MSTOCK + ROLLING sections moved to SIG. Removed with it (dead code): straddle capture /
`aggressive_mode` (set but never read; `get_straddle_sum` never existed), `_web_read_multitf`,
`_web_read_shadow`, and the `/api/multitf`, `/api/shadow`, `/api/zones` routes.
Note: `lab_data/spot/` + `lab_data/options_*` CSV collectors were NOT removed — they feed
backtests/analysis, only their dashboard reader is gone.

FILES tab folders = `_WEB_FOLDERS`: trade_log, spot, options_3min, options_1min, logs_live,
logs_errors. Dead dirs removed 2026-06-10 (created but never written to): `lab_data/reports`,
`lab_data/sessions`, `logs/zones`, `logs/ml`, `logs/flow` — their constants, ensure_dirs
entries, zip-inventory map entries, and the `/files` page links (research/state/logs) that
pointed at non-existent folder keys are all gone.

**Service**: `sudo systemctl restart vrl-main.service`
**Logs**: `~/logs/live/vrl_live.log`
**Trade CSV**: `~/lab_data/vrl_trade_log.csv` (`entry_mode` = `V10_CE` / `V10_PE`; paper fills tagged `PAPER_*`; `spot_regime` = 3-min EMA regime at fire time — analysis only, not a gate; `pdh_prev`/`pdl_prev`/`entry_range_pos` = prev-day high/low + spot position in that range at entry, added 2026-06-11 — analysis only, candidate gate after 2–3 weeks of data: PE entries mid-range ran 23% win rate)

### Deploy after any main merge
```bash
cd ~/VISHAL_RAJPUT && git checkout main && git pull && sudo systemctl restart vrl-main.service
```

---

## State persistence
`_V10_PERSIST_FIELDS` (code: `_V8_PERSIST_FIELDS`) controls what survives restart. Any new key MUST be added to BOTH:
1. The initial `_v10_state = { ... }` dict (code: `_v8_state`) so `_load_v10_state` restores it
2. `_V10_PERSIST_FIELDS` (code: `_V8_PERSIST_FIELDS`) so `_save_v10_state` writes it

Fields currently persisted:
`in_trade`, `symbol`, `token`, `direction`, `strike`, `entry_price`, `entry_time`, `qty`,
`peak_pnl`, `active_ratchet_tier`, `active_ratchet_sl`, `candles_held`, `_other_token`,
`_sl_cooldown_skip_next`, `_force_exit_ts`,
`_pnl_today_pts`, `_trades_today`, `_wins_today`, `_losses_today`,
`_v8_both_rejected_ts`, `_last_trade_date`, `_last_exit_candle_ts`,
`_last_exit_time_unix`, `_last_exit_direction_v10`,
`initial_sl`, `entry_regime`,
`peak_ltp`, `xleg_other_margin`, `spot_regime_at_entry`,
`entry_spot`, `entry_atm_dist`, `pdh_prev`, `pdl_prev`, `entry_range_pos`,
`neighbor_ltp_otm`, `neighbor_ltp_itm`, `max_otm_drift`,
`vix_at_entry`, `hourly_rsi_at_entry`, `bias_at_entry`, `session_at_entry`,
`first_profit_candle`, `first_profit_ltp`, `first_profit_ts`,
`breakout_candle`, `breakout_ltp`, `breakout_ts`

---

## Threading model
- **Main loop** — single thread, ~1s cycle
- **TG listener** — `TGListener` daemon thread (Telegram commands)
- **Web server** — `ThreadingHTTPServer` + `_WebHandler` daemon (port 8080)
- **`_v10_lock`** (code: `_v8_lock`) — `threading.RLock()` — protects all `_v10_state` reads/writes; RLock allows `_save_v8_state()` to re-enter from within exit-check block
- **`_state_lock`** — protects legacy `state` dict
- **Rule**: any function callable from both main loop and TG/web thread must hold `_v10_lock` for the full check-and-act section. Never check under lock, release, then act.

## V10 Golden scanner (inside `_strategy_loop`)
```
_v10_scanner_last_ts  — throttle: scanner runs every 3s
_v10_live             — dict {"CE": {...}, "PE": {...}} — gate snapshot fed to dashboard
_v10_live_lock        — threading.Lock() protecting _v10_live
```
Scanner runs every 3s **regardless of `in_trade`** so `_v10_live` stays warm with live EMA9 data for the dashboard. When `in_trade=True`, the inner guard sets `reject_reason="in_trade"` and `_ready_to_fire=False` — no entry fires, but `_v10_live` is updated.
Scanner fires `_v10_execute_paper_entry` (code: `_v8_execute_paper_entry`) when MOMENTUM + OPP DECAY both pass and no cooldowns active.
**Expiry** is determined by the broker (Kite instrument list) at startup — never calculate it manually.

---

## Audit tools (standalone, no Claude dependency)

### watch_trade.py
```bash
python3 watch_trade.py          # foreground
nohup python3 watch_trade.py &  # background
```
Polls every 2s (in trade) / 10s (idle). Cross-checks:
- `state/vrl_v8_state.json` (V10 engine state) vs `state/vrl_dashboard.json` (9 fields)
- V10 SL tier formula (peak < 9 / ≥ 9 / ≥ 11 / ≥ 18)
- Telegram log: entry alert, SL upgrade alert, exit alert
Mismatches appended to `~/lab_data/trade_audit_notes.md`.

Also watches the SMI stock F&O paper engine every 60s (PR #235):
- `screener/smi_paper_state.json` open trades — SL formula (stock entry ∓1%), trail armed at +1.5% peak, matching OPEN row in `fno_tracker.csv` (structure=SMI)
- Stale-state alarm if state file >22 min old during 09:47–15:31 (dead cron detector)
- Exit reconciliation vs `screener/smi_paper_log.csv` (pnl_rs math, exit reason ∈ SL-HIT/TRAIL-SMA8/EOD-CLOSE/EOD-LATE, tracker status)

### trace_trade.py
Post-trade reconciler. Reads state + dashboard + CSV and flags:
- SL tier vs peak_pnl formula
- CSV pnl_pts vs exit_price − entry_price
- entry_mode must be `V10_CE` or `V10_PE`

---

## Bug history — why safeguards exist

- **BUG-01**: Exits must run every ~1s tick. `_v10_check_exit()` runs unconditionally before the candle gate.
- **BUG-07**: Duplicate trades from thread race — entry and exit each hold `_v10_lock` for the full check-and-act. Entry returns early if `in_trade`.
- **BUG-10/11**: All restored state keys present in initial `_v10_state` dict; TG force-exit reads token/entry under `_v10_lock`.

### Locked design decisions
- **Re-entry disabled**: every exit sets `_reentry_armed = False`; fresh setup only.
- **No strike/streak re-entry blockers (2026-06-11)**: the exhausted-loss strike block was tried and removed same day — live counterfactual showed it kills recovery winners. 15+ broader variants (time/streak/daily-cap) all reduced net P&L. Big winners are themselves re-entries after clean SLs.
- **Single-lot execution (2026-06-10)**: 1 lot, market fill at candle close. Split-lot 50/50 (Lot 2 limit @ candle midpoint, 3-candle cancel) removed at user request.
- **All strategy parameters are locked** — OPP DECAY [−8,−4], initial SL cap entry−10, PROTECT @+9 entry−2, LOCK_4 @+11 entry+4, TRAIL_10 @+18 peak−10. Change only with explicit user confirmation (ladder values owner-approved 2026-06-11 via sl_replay_study.py).

---

## GitHub / Branch rules
- **main** is protected — PRs required, ≤ 1 open PR at a time
- **Every code change ships via PR** — no uncommitted changes at end of session:
  1. `git checkout -b <type>/<short-desc>`
  2. `git add <tracked production files only>`
  3. `git commit`
  4. `git push origin <branch>`
  5. `gh pr create` (title + bullet summary + test plan)
  6. `gh pr merge --squash --delete-branch`
  7. `git checkout main && git pull`
- `gh` CLI at `~/bin/gh`. If not found: `export PATH="$HOME/bin:$PATH"`
