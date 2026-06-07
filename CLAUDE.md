# VRL Trading Bot — Developer Reference

> Doc resynced to code 2026-06-07. The whole bot now lives in a **single file** `VRL_MAIN.py`
> (~11.8k lines). The old `VRL_ENGINE.py` / `VRL_DATA.py` / `VRL_CONFIG.py` no longer exist —
> their logic was folded into `VRL_MAIN.py`. Any older line numbers in this doc are indicative only.

## Project Overview
NIFTY weekly-options bot (Zerodha **Kite** for market data, **m.Stock** for live order placement).
- **Mode is config-driven**: `config.yaml` → `mode: paper | live`, read once into `D.PAPER_MODE = CFG.is_paper()`.
  - **paper** (current): entries/exits are simulated, fills logged as `PAPER_*`, slippage 0.
  - **live**: real orders go through m.Stock (`MSTOCK.ms_place_buy` / `ms_place_sell`) with a limit-price buffer.
- **V10 (LIVE strategy)** — P1/P2 **1-min** engine, running since 2026-06-02 (PR #111). Master switch `V10_LIVE = True`.
- **P3** — shadow-only extreme-VWAP reversal probe (`V10_P3_VWAP_EXTREME = 75`), no live trades, data collection only.

**Current strategy version**: `v20` (V10 cutover, PR #111).

### Module-as-namespace pattern (important)
`VRL_MAIN.py` aliases itself so old call-sites keep working:
```python
D = CFG = LEVELS = CHARGES = MSTOCK = sys.modules[__name__]
```
So `CFG.is_paper()`, `D.PAPER_MODE`, `MSTOCK.get_mstock()`, etc. all resolve to functions in this same file.
When searching for "dead" code, count dotted refs (`D.foo`, `MSTOCK.foo`) too — a function can be live via these aliases.

## V10 entry gates — tunable constants (top of VRL_MAIN.py, ~line 4800)
All HARD gates (every one must pass); the near-VWAP *distance* gate is OFF.
- `V10_MIN_EMA9H_GAP = 3.5` — momentum-breakout floor (single source of truth; used in pre-filter **and** gate B).
- `V10_RSI_MIN = 55`, `V10_RSI_MAX = 80` — RSI must be in [55,80] **and rising** vs prev candle.
- `V10_BW_MIN = 5.0` — band-width floor (BW = ema9h − ema9l).
- `V10_NEAR_VWAP_MAX = 0` — near-VWAP DISTANCE gate **disabled** (set >0 to re-enable). P1/P2 still use VWAP only to define above/below.
- `V10_OPEN_BLACKOUT_END = 09:45` — **no entries before 09:45** (opening chop).
- **XLEG_CONFIRMED** (cross-leg dying, `evaluate_cross_leg`) + **LTP on correct side of VWAP** at fire.
- `V10_P3_VWAP_EXTREME = 75` — P3 shadow reversal threshold.

Warning-only (logged + dashboard, does NOT block): **trend-align** (CE+bull / PE+bear). **ADX is not gated** (weak-ADX won historically).

### Tiers
- **P1** = setup above VWAP (long-side breakout). **P2** = setup below VWAP. Both fire live through `_v8_execute_paper_entry`.
- **P3** = `|future − VWAP| >= 75` reversal CE/PE — shadow log only.

## Execution model
- **Entry**: `_v8_execute_paper_entry(direction, strike, symbol, token, ...)` — guarded by `_v8_state["in_trade"]` under `_v8_lock`.
  - paper → returns synthetic fill. live → `MSTOCK.ms_place_buy` with limit = ref + max(2.0, 1% of ref).
- **Exit ladder**: `_v8_compute_trail_sl(entry_price, peak_pnl)` — tick-based, hard SL −12:
  ```
  peak < 12  → INITIAL : SL = entry − 12
  peak ≥ 12  → LOCK_4  : entry + 4
  peak ≥ 18  → LOCK_10 : entry + 10
  peak ≥ 24  → LOCK_12 : entry + 12
  peak ≥ 30  → LOCK_20 : entry + 20
  peak ≥ 36  → LOCK_30 : entry + 30
  peak ≥ 40  → LOCK_36 : entry + 36
  peak ≥ 50  → LOCK_50 : entry + 50
  ```
- Exit check runs every ~1s loop cycle (tick-based), BEFORE the candle gate (see BUG-01).
- Per-day counters live in `_v8_state`: `_trades_today`, `_wins_today`, `_losses_today`, `_pnl_today_pts` (reset at session start). There is **no hard trades/day cap constant** in code today — counters are for tracking/dashboard.

## Live V10 performance (first 2 days, 25 trades — small sample)
- All V10: 48% win, **−0.88 pts/trade**, −22 total.
- **CE: 67% win, +0.89/trade. PE: 38% win, −1.88/trade** — PE is the entire loss. Matches the screener's 33-trade finding (CALLs 67% vs PUTs 24%).
- Every loser is a clean −12 EMERGENCY_SL; winners avg +11.17. 5/13 losers never reached +2 (entry-quality issue).
- **Open levers** (measure-first, sample too small to lock): gate the PE side harder; break the symmetric −12 stop (fast-fail dead trades or let winners run); flag "peak<2" entries.

## File / layout
| Path | Purpose |
|------|---------|
| `VRL_MAIN.py` | Everything: config accessors, Kite/m.Stock brokers, strategy loop, entry/exit, TG handler, web server, exit ladder |
| `config.yaml` | Runtime config — `mode`, instrument, lots, EMA bands, thresholds, market hours |
| `screener/` | Standalone stock F&O + multibagger screeners (separate processes, not imported by VRL_MAIN) |
| `static/VRL_DASHBOARD.html` | **Generated artifact** — regenerated from `_WEB_HTML` on every startup. NOT the source of truth (see below) |
| `state/vrl_v8_state.json` | V10 live state (`_v8_state`) |
| `state/vrl_shadow_state.json` | Shadow (P1/P2/P3) signal state |
| `state/vrl_live_state.json`, `state/vrl_dashboard.json` | Live runtime + dashboard snapshot |

### Dashboard source of truth
The web UI is the `_WEB_HTML = r"""..."""` string literal in `VRL_MAIN.py` (~line 10409). The HTTP handler
(`_WebHandler`, a daemon thread inside vrl-main on port 8080) serves it, and `_start_web_server()` **overwrites
`static/VRL_DASHBOARD.html` from `_WEB_HTML` on every startup**. So **edit `_WEB_HTML`, never the static file** —
a static-file edit is wiped on the next restart. Only `vrl-main.service` runs; `vrl-web.service` was retired
2026-06-07 (it ran the deleted `VRL_WEB.py`).

**Service**: `sudo systemctl restart vrl-main.service`
**Logs**: `~/logs/live/vrl_live.log`
**Trade CSV**: `~/lab_data/vrl_trade_log.csv` (header has `entry_mode` = `V10_P1` / `V10_P2`; paper fills tagged `PAPER_*`)

### Deploy after any main merge
```bash
cd ~/VISHAL_RAJPUT && git checkout main && git pull && sudo systemctl restart vrl-main.service
```

## Stock F&O screener (screener/, separate from VRL_MAIN)
Key scripts: `fno_strategy.py`, `vishal_fno_screener.py`, `multibagger_screener.py`, `portfolio_monitor.py`, `weekly_report.py`.
Config: `fno_strategy_config.json`.
- **CALLs enabled** (`require_regime_align=false`) — 33-trade study: CALLs 67% win vs PUTs 24%.
- `max_pcr=1.0` (block PCR>1.0), `naked_sl_pct=25`.
- Delivery-% spike signal (institutional confirmation, PR #172); dashboard score badge / signal chips / win-rate bar (PR #173).

## State persistence
`_V8_PERSIST_FIELDS` controls what survives restart. Any new state key that must persist MUST be added to BOTH:
1. the initial `_v8_state = { ... }` dict (so `_load_v8_state` restores it), and
2. `_V8_PERSIST_FIELDS` (so `_save_v8_state` writes it).

## Threading model
- **Main loop** — single thread, ~1s cycle.
- **TG listener** — `TGListener` daemon thread (Telegram commands).
- **Web server** — `ThreadingHTTPServer` + `_WebHandler` daemon (dashboard / login).
- **`_v8_lock`** protects all `_v8_state` reads/writes. **`_state_lock`** protects the legacy `state` dict.
- **Rule**: any function callable from BOTH main loop and TG/web thread must hold `_v8_lock` for the whole check-and-act critical section. Never check under lock, release, then act.

---

## Lessons learned / bug history (historical — pre-consolidation file/line refs no longer valid)
These describe *why* current safeguards exist. The code moved into `VRL_MAIN.py`; grep by symbol, not line number.

- **BUG-01**: Exits must run every ~1s tick, not on candle close. `_v8_check_exit()` runs unconditionally before the candle gate. (Symptom: Peak always 0.0, EMERGENCY_SL at minute end.)
- **BUG-02**: xLeg false positives from rounding — all cross-leg "dying" comparisons require a `−0.5` margin (`o_close < o_ema9l − 0.5`).
- **BUG-03**: EMA-slope gate checks 2 candles, not 1 (both slope1 and slope2 ≥ 0) to reject fake breakouts on falling support.
- **BUG-04**: RSI must *rise* ≥ 2.0 vs prev candle (blocks drift entries).
- **BUG-05/06**: Both-sides cooldown — only arm/refresh the timestamp when NOT already in cooldown; never mark a cooldown-blocked side as "gate-rejected" (that caused infinite cooldown). Cooldown = 1 min.
- **BUG-07**: Duplicate trades from thread race — entry and exit each do their full read-guard-write in ONE `with _v8_lock:` block. Entry returns early if `in_trade`.
- **BUG-08**: V10 must always log (no `silent=` during cooldown), and cooldown timestamp must expire.
- **BUG-09**: Cross-leg gate logs exceptions (`except Exception as e`), never bare `except: pass`.
- **BUG-10/11**: All restored state keys present in the initial `_v8_state` dict; TG force-exit reads token/entry under `_v8_lock`.
- **BUG-12**: Emergency-SL fallback is −12 (not −10) everywhere.
- **BUG-13/14/15**: Config-default fixes — strike step keys `step_normal`/`step_dte0`; entry-cutoff default 0 (→15:00); market-close boundary `now < end`.
- **BUG-16**: `V10_MIN_EMA9H_GAP` is the single source of truth — pre-filter and gate B both use the constant (no hardcoded `2.0`).

### Locked design decisions
- **Re-entry disabled** (2026-05-15): every exit sets `_reentry_armed = False`; fresh setup only.
- **Both-sides cooldown = 1 min** (2026-05-15).
- **Gate 2B** (2026-05-15): EMA9_low slope ≥ 0 for last 2 candles.

### Shadow ANALYSIS flags (logged after each signal, no trade impact)
- `XLEG_CONFIRMED` — cross-leg dead all 5 last candles → strong directional confirmation. ✅
- `XLEG_AMBIGUOUS` — cross-leg not consistently below EMA9H → confirmed loss predictor.
- `EXTENDED_GAP(X)` — ema9h_gap > 5 → caution, NOT a kill (strong trend can override).
- `WEAK_ADX(X)` — low directional conviction. `TINY_GAP` — ema9h_gap < 0.8.

### Pending / collect data
- **PE-side gate** — PE is the live money-loser; gate it to CE's quality bar (corroborated by screener).
- **Break symmetric −12 SL** — fast-fail trades that never reach +2, or let trailers run past +12.
- **P2 max ema9h_gap cap (≤5)** — large gaps lost on P2.
- **VWAP overextension flag** (gap > 25), **P2 min VWAP gap** (require genuine buildup below −5).
- Post-EMERGENCY-SL opposite-side cooldown; max consecutive ESL → pause; daily loss limit.
- Gap-open mornings (RSI/VWAP both block) — collect more days before deciding special handling.

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
