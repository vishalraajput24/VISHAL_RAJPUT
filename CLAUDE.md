# VRL Trading Bot — Developer Reference

> Doc resynced to code 2026-06-08. The whole bot lives in a **single file** `VRL_MAIN.py`
> (~11k lines). The old `VRL_ENGINE.py` / `VRL_DATA.py` / `VRL_CONFIG.py` no longer exist.
> Any older line numbers in this doc are indicative only — grep by symbol name.

## Project Overview
NIFTY weekly-options bot (Zerodha **Kite** for market data, **m.Stock** for live order placement).
- **Mode is config-driven**: `config.yaml` → `mode: paper | live`, read once into `D.PAPER_MODE = CFG.is_paper()`.
  - **paper** (current): entries/exits are simulated, fills logged as `PAPER_*`, slippage 0.
  - **live**: real orders go through m.Stock (`MSTOCK.ms_place_buy` / `ms_place_sell`) with a limit-price buffer.
- **V10 Golden Strategy** — 1-min engine, running live. Master switch `V10_LIVE = True`.
- No shadow scanners, no P3, no V2 trackers — single code path.

**Current strategy version**: `v20` (V10 Golden, 2026-06-08).

### Module-as-namespace pattern (important)
`VRL_MAIN.py` aliases itself so old call-sites keep working:
```python
D = CFG = LEVELS = CHARGES = MSTOCK = sys.modules[__name__]
```
So `CFG.is_paper()`, `D.PAPER_MODE`, `MSTOCK.get_mstock()`, etc. all resolve to functions in this same file.
When searching for "dead" code, count dotted refs (`D.foo`, `MSTOCK.foo`) too — a function can be live via these aliases.

## V10 Golden entry gates (tunable constants near top of VRL_MAIN.py)
Two HARD gates — both must pass:

| Gate | Constant / logic | Description |
|------|-----------------|-------------|
| **MOMENTUM** | `close > ema9h` (1-min option candle) | Breakout confirmation |
| **OPP DECAY** | `opp_close − opp_ema9l` in `[−5.0, −4.0]` | Opposite leg decaying into its band |

- `V10_OPEN_BLACKOUT_END = 09:45` — **no entries before 09:45** (opening chop).
- **Same-candle guard** (`_last_fired_candle_ts`) — prevents double-entry on the same 1-min candle.
- **Exit-candle cooldown** (`_last_exit_candle_ts`) — blocks re-entry on same candle as exit.
- `V10_MIN_EMA9H_GAP` — kept as reference constant but not a hard gate in the Golden scanner.

## Execution model

### Entry — `_v8_execute_paper_entry`
Guarded by `_v8_state["in_trade"]` under `_v8_lock`.

**Split-lot 50/50:**
- **Lot 1** (50% qty): filled at market (last 1-min candle close).
- **Lot 2** (50% qty): limit at candle midpoint `(open + close) / 2`.
- Lot 2 auto-cancelled after 3 candles if not filled. Telegram alert on cancel.
- Average entry price updated when Lot 2 fills; qty updated too.

**Initial SL**: `ema9_low` of the breakout candle (stored in `_v8_state["initial_sl"]`). If ema9_low ≥ entry, fallback = `entry − 5.0`.

### Exit ladder — `_v8_compute_trail_sl(entry_price, peak_pnl, initial_sl)`
Tick-based (runs every ~1s), BEFORE the candle gate (BUG-01):

```
peak < 12   → INITIAL    : SL = initial_sl (ema9_low at entry)
peak ≥ 12   → BREAKEVEN  : SL = max(initial_sl, entry)
peak ≥ 18   → TRAIL_10   : SL = max(initial_sl, entry, peak_ltp − 10.0)
```

Exit reasons: `EMERGENCY_SL` (INITIAL tier hit), `BREAKEVEN`, `VISHAL_TRAIL` (TRAIL_10 hit), `EOD_EXIT`.

### Per-day counters (`_v8_state`)
`_trades_today`, `_wins_today`, `_losses_today`, `_pnl_today_pts` — reset at session start. No hard trades/day cap.

## File / layout
| Path | Purpose |
|------|---------|
| `VRL_MAIN.py` | Everything: config, brokers, strategy loop, entry/exit, TG handler, web server |
| `config.yaml` | Runtime config — `mode`, instrument, lots, EMA bands, thresholds, market hours |
| `screener/` | Standalone stock F&O + multibagger screeners (separate processes, not imported by VRL_MAIN) |
| `static/VRL_DASHBOARD.html` | **Generated artifact** — regenerated from `_WEB_HTML` on every startup. NOT the source of truth |
| `state/vrl_v8_state.json` | V10 live state (`_v8_state`, includes split-lot fields) |
| `state/vrl_dashboard.json` | Dashboard snapshot (written every main-loop cycle) |
| `state/vrl_live_state.json` | Live runtime state |

### Dashboard source of truth
The web UI is the `_WEB_HTML = r"""..."""` string literal in `VRL_MAIN.py` (~line 10409). The HTTP handler
(`_WebHandler`, a daemon thread inside vrl-main on port 8080) serves it, and `_start_web_server()` **overwrites
`static/VRL_DASHBOARD.html` from `_WEB_HTML` on every startup**. So **edit `_WEB_HTML`, never the static file** —
a static-file edit is wiped on the next restart. Only `vrl-main.service` runs; `vrl-web.service` was retired
2026-06-07 (it ran the deleted `VRL_WEB.py`).

**Service**: `sudo systemctl restart vrl-main.service`
**Logs**: `~/logs/live/vrl_live.log`
**Trade CSV**: `~/lab_data/vrl_trade_log.csv` (`entry_mode` = `V10_CE` / `V10_PE`; paper fills tagged `PAPER_*`)

### Deploy after any main merge
```bash
cd ~/VISHAL_RAJPUT && git checkout main && git pull && sudo systemctl restart vrl-main.service
```

## State persistence
`_V8_PERSIST_FIELDS` controls what survives restart. Any new state key that must persist MUST be added to BOTH:
1. the initial `_v8_state = { ... }` dict (so `_load_v8_state` restores it), and
2. `_V8_PERSIST_FIELDS` (so `_save_v8_state` writes it).

Split-lot fields already in `_V8_PERSIST_FIELDS`: `initial_sl`, `entry_regime`, `lot1_qty`, `lot1_entry`, `lot2_qty`, `lot2_limit`, `lot2_entry`, `lot2_filled`, `lot2_cancelled`, `peak_ltp`, `xleg_other_margin`.

## Threading model
- **Main loop** — single thread, ~1s cycle.
- **TG listener** — `TGListener` daemon thread (Telegram commands).
- **Web server** — `ThreadingHTTPServer` + `_WebHandler` daemon (dashboard / login).
- **`_v8_lock`** protects all `_v8_state` reads/writes. **`_state_lock`** protects the legacy `state` dict.
- **Rule**: any function callable from BOTH main loop and TG/web thread must hold `_v8_lock` for the whole check-and-act critical section. Never check under lock, release, then act.

## V10 Golden scanner (main loop, inside `_strategy_loop`)
```
_v10_scanner_last_ts  — throttle: scanner runs every 3s (replaces old _v8_shadow_dt["last_scan_ts"])
_v10_live             — dict {"CE": {...}, "PE": {...}} with latest gate snapshot (feeds dashboard)
_v10_live_lock        — threading.Lock() protecting _v10_live
```
Scanner fires `_v8_execute_paper_entry` when both MOMENTUM + OPP DECAY pass and no cooldowns are active.

## Stock F&O screener (screener/, separate from VRL_MAIN)
Key scripts: `fno_strategy.py`, `vishal_fno_screener.py`, `multibagger_screener.py`, `portfolio_monitor.py`, `weekly_report.py`.
Config: `fno_strategy_config.json`.
- **CALLs enabled** (`require_regime_align=false`) — 33-trade study: CALLs 67% win vs PUTs 24%.
- `max_pcr=1.0` (block PCR>1.0), `naked_sl_pct=25`.
- Delivery-% spike signal (institutional confirmation, PR #172); dashboard score badge / signal chips / win-rate bar (PR #173).

---

## Lessons learned / bug history
These describe *why* current safeguards exist. Grep by symbol name, not line number.

- **BUG-01**: Exits must run every ~1s tick, not on candle close. `_v8_check_exit()` runs unconditionally before the candle gate.
- **BUG-07**: Duplicate trades from thread race — entry and exit each do their full read-guard-write in ONE `with _v8_lock:` block. Entry returns early if `in_trade`.
- **BUG-10/11**: All restored state keys present in the initial `_v8_state` dict; TG force-exit reads token/entry under `_v8_lock`.

### Locked design decisions
- **Re-entry disabled**: every exit sets `_reentry_armed = False`; fresh setup only.
- **Lot 2 cancel window = 3 candles**: if the limit doesn't fill in 3 minutes, cancel and run Lot 1 only.

### Pending / open research
- **OPP DECAY range**: currently −5 to −4; measure miss-rate vs tighter range.
- **MOMENTUM threshold**: currently `close > ema9h`; consider requiring gap > some minimum.
- **PE-side performance**: historically weaker; evaluate whether extra filters help.
- **Trail sensitivity**: TRAIL_10 (peak − 10); evaluate TRAIL_8 or TRAIL_12.
- Post-SL cooldown; daily loss limit.

---

## GitHub / Branch rules
- **main** is protected — PRs required, keep ≤ 1 open PR at a time.
- **Every code change ships via PR** — no uncommitted changes at end of session:
  1. `git checkout -b <type>/<short-desc>`
  2. `git add <tracked production files only>` (not backtest/scratch scripts)
  3. `git commit` (type: short reason + what/why)
  4. `git push origin <branch>`
  5. `gh pr create` (title + bullet summary + test plan)
  6. `gh pr merge --squash --delete-branch`
  7. `git checkout main && git pull`
- `gh` CLI at `~/bin/gh` (authed as vishalraajput24). If not found: `export PATH="$HOME/bin:$PATH"`.
