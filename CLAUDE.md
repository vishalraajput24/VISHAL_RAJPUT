# VRL Trading Bot — Developer Reference

> Last resynced: 2026-06-09 (fix/v10-lot2-deadlock-and-state-persistence). Single-file bot: `VRL_MAIN.py` (~10,266 lines).
> Grep by symbol name — line numbers in this doc are approximate.

---

## 🤖 Agent Sync Protocol (Claude Code ↔ Google Antigravity)

**Both agents must read this file first before any code change.**

If you are Google Antigravity reading this:
1. Read this entire CLAUDE.md — it is the single source of truth for architecture, strategy values, and rules.
2. After any change you make to `VRL_MAIN.py`, `config.yaml`, `watch_trade.py`, or `trace_trade.py` — update the relevant section of this file in the same PR. Do not leave CLAUDE.md stale.
3. **Never change locked strategy values** (OPP DECAY [−5,−4], BREAKEVEN @+12, TRAIL_10 @+18, lot sizes) without the user's explicit instruction.
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
| **MOMENTUM** | 1-min option `close > ema9_high` | — |
| **OPP DECAY** | opposite leg: `close − ema9_low` in `[−5.0, −4.0]` | hardcoded in scanner |

- `V10_OPEN_BLACKOUT_END = dtime(9, 45)` — no entries before 09:45
- `V10_MIN_EMA9H_GAP = 3.5` — reference constant only, NOT a hard gate
- **Same-candle guard** (`_last_fired_candle_ts`) — no double-entry on same 1-min candle
- **Exit-candle cooldown** (`_last_exit_candle_ts`) — no re-entry on same candle as exit

### Execution — split-lot 50/50
Config: `lots_fixed: 2`, `lot_size: 65` → 130 qty total, split into two 65-qty lots.

| Lot | Fill | Price |
|-----|------|-------|
| **Lot 1** (65 qty) | Market | Last 1-min candle close |
| **Lot 2** (65 qty) | Limit | `(candle_open + candle_close) / 2` |

- Lot 2 auto-cancelled after **3 candles** if not filled → Telegram alert
- Average entry and qty updated in `_v10_state` when Lot 2 fills
- **Initial SL**: `ema9_low` of breakout candle (`_v10_state["initial_sl"]`). Fallback: `entry − 5.0` if ema9_low ≥ entry

### Exit ladder — `_v10_compute_trail_sl(entry_price, peak_pnl, initial_sl)`
Tick-based (~1s), runs BEFORE the candle gate (BUG-01):

```
peak < 12 pts  → INITIAL    : SL = initial_sl
peak ≥ 12 pts  → BREAKEVEN  : SL = max(initial_sl, entry)
peak ≥ 18 pts  → TRAIL_10   : SL = max(initial_sl, entry, peak_ltp − 10.0)
```

Exit reasons: `EMERGENCY_SL` · `BREAKEVEN` · `VISHAL_TRAIL` · `EOD_EXIT`

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
| `screener/` | Stock F&O + multibagger screeners (separate processes, not imported by VRL_MAIN) |
| `static/VRL_DASHBOARD.html` | **Generated artifact** — overwritten from `_WEB_HTML` on every restart. Never edit directly. |
| `state/vrl_v8_state.json` | **Primary V10 engine state** — `_v10_state` including all split-lot fields (filename uses legacy `v8` prefix — rename pending) |
| `state/vrl_live_state.json` | Legacy V7 state — still written by bot, not used by V10 strategy logic |
| `state/vrl_dashboard.json` | Dashboard snapshot — written every main-loop cycle |

### Stale artifacts in state/ (do not rely on)
- `vrl_shadow_state.json` — shadow scanner removed; file is stale
- `vrl_v10_state.json` — orphaned file; active state is in `vrl_v8_state.json` (rename pending)
- `bw_gap_study.csv` — BW/RSI study; gates removed in V10 Golden

### Dashboard source of truth
`_WEB_HTML = r"""..."""` string in `VRL_MAIN.py` (~line 8956). `_start_web_server()` overwrites
`static/VRL_DASHBOARD.html` from this string on every startup.
**Always edit `_WEB_HTML` — never the static file.**
Only `vrl-main.service` runs (port 8080). `vrl-web.service` was retired 2026-06-07.

**Service**: `sudo systemctl restart vrl-main.service`
**Logs**: `~/logs/live/vrl_live.log`
**Trade CSV**: `~/lab_data/vrl_trade_log.csv` (`entry_mode` = `V10_CE` / `V10_PE`; paper fills tagged `PAPER_*`; `spot_regime` = 3-min EMA regime at fire time — analysis only, not a gate)

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
`initial_sl`, `entry_regime`, `lot1_qty`, `lot1_entry`,
`lot2_qty`, `lot2_limit`, `lot2_entry`, `lot2_filled`, `lot2_cancelled`,
`peak_ltp`, `xleg_other_margin`, `spot_regime_at_entry`

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
- V10 SL tier formula (peak < 12 / ≥ 12 / ≥ 18)
- Telegram log: entry alert, SL upgrade alert, lot2 fill/cancel alert, exit alert
Mismatches appended to `~/lab_data/trade_audit_notes.md`.

### trace_trade.py
Post-trade reconciler. Reads state + dashboard + CSV and flags:
- Lot 2 consistency (filled vs cancelled mutual exclusion, avg entry)
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
- **Lot 2 cancel window = 3 candles**: if limit doesn't fill in 3 minutes, cancel and run Lot 1 only.
- **All strategy parameters are locked** — OPP DECAY [−5,−4], BREAKEVEN @+12, TRAIL_10 @+18 peak−10. Change only with explicit user confirmation.

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
