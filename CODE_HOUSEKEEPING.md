# VRL Codebase — Housekeeping & Full Code Map

> Generated 2026-06-19 for owner self-review. Purpose: go through **every file**, understand
> what it does, and decide KEEP / ARCHIVE / REMOVE yourself. Nothing here is deleted — this is
> the map. Tick each row, then we act on a follow-up PR.
>
> **The single most important rule before deleting anything:** check the
> [Live dependency graph](#live-dependency-graph) below. Several files whose *names* sound dead
> (`vishal_fno_screener.py`, `smi_paper_flow.py`) are still **imported as libraries** by things
> that run in cron. Deleting them breaks the live engines silently.

## Legend

| Tag | Meaning | Action |
|-----|---------|--------|
| 🟢 **LIVE** | Runs via `vrl-main.service` or a crontab line | **Never delete.** Core. |
| 🔵 **DEP** | Not run directly, but **imported** by a 🟢 LIVE file | **Never delete** — breaks a live engine. |
| 🛠️ **TOOL** | On-demand audit/utility you run by hand | Keep — low cost, high value when needed. |
| 🟡 **PENDING** | Active R&D; decision tied to a future review date | Keep until the review, then archive. |
| 🗄️ **ARCHIVE** | Study is **finished**, conclusion already saved (memory/CLAUDE.md) | Safe to move to `screener/studies_archive/` or `git rm`. Code adds no ongoing value. |
| 🔴 **REMOVE** | Dead / superseded / proven-dud, nothing imports it | Safe to `git rm`. |

---

## Live dependency graph

What actually runs, and what each runner pulls in. **Anything in this graph is untouchable.**

```
vrl-main.service ─────────────► VRL_MAIN.py                 (the bot; also `--collector` @15:35 cron)

cron 09:00  fno_collector.py ──► fno_strategy.py            (gate/regime "single source of truth")
                              └► vishal_fno_screener.py     (FNO_UNIVERSE, TRACKER_COLS)  ⚠️ "retired" name, LIVE dep
cron 16:00  portfolio_monitor.py ─► vishal_fno_screener.py  (get_kite, load_instruments)  ⚠️ same
cron 09:25  v12_vishal.py ─────► VRL_MAIN.py (infra) + orion_v2514_backtest.py (indicators)
cron 09:25  paper_wide.py ─────► VRL_MAIN.py (reuses real gate fns)
cron 09:47+ smi_focus35.py ────► smi_paper.py (S: exits/fill) + smi_paper_flow.py (F: flow-gate) + orion_v2514_backtest.py
cron 15:50  v12_veto_audit.py   (parses ~/logs/v12_vishal.log; standalone)
cron 09:13  split_atm_collector.py        (data collector, standalone)
cron 09:14  tick_delta_collector.py       (data collector, standalone)
cron Sun    multibagger_screener.py / dep_update_check.py
cron 10:30  weekly_report.py + analysis/push_daily_report.sh → analysis/daily_report.py
```

> Crons also fire three scripts that live in **`~/lab_data/`, not this repo** (so out of scope here):
> `final_package_tracker.py` (15:45), `wick_exhaustion_study.py --track` (16:10), `tar_expiry_study.py` (16:12).

---

## Tier 1 — 🟢 LIVE (never delete)

| File | What it does |
|------|--------------|
| `VRL_MAIN.py` | **The bot.** ~9k lines: config loader, Kite+m.Stock brokers, V11 Golden 1-min strategy loop, entry/exit ladder, Telegram handler, web dashboard (port 8080). Architecture fully documented in `CLAUDE.md`. `--collector` mode also runs at 15:35 to archive the day's candles. |
| `config.yaml` | Runtime config — `mode: paper/live`, instrument, lots, EMA bands, thresholds, market hours. |
| `screener/fno_collector.py` | 09:00 cron `--morning`: builds the F&O universe / OHLCV cache the stock engines read. |
| `screener/weekly_report.py` | 10:30 Mon–Sat: updates the multibagger weekly tracker performance. |
| `screener/multibagger_screener.py` | Sun 02:30: NSE-500 + Screener.in long-term multibagger scan. |
| `screener/portfolio_monitor.py` | 16:00: price/technical layer over the multibagger holdings. |
| `analysis/push_daily_report.sh` + `analysis/daily_report.py` | 10:30: builds & pushes the daily report (output in `analysis/daily/`). |
| `split_atm_collector.py` | 09:13: 1-min ITM CE/PE premium collector (feeds split-ATM backtests). |
| `tick_delta_collector.py` | 09:14: per-minute order-flow / tick-delta collector → `~/lab_data/tick_delta_log.csv`. DATA ONLY. |
| `paper_wide.py` | 09:25: independent **wide-window** (09:30–15:15) paper engine; A/B vs live's narrow 10:00–14:30 window. Reuses VRL_MAIN's real gates. |
| `v12_vishal.py` | 09:25: **the priority V12 engine** (5-min, option-only paper). Canonical V12 — all V12 work happens here. |
| `screener/smi_focus35.py` | 09:47 + every 15m: the **only** stock-F&O engine — 35 stocks each on its own tuned gate. |
| `v12_veto_audit.py` | 15:50: replays V12's flow-vetoed signals against real option candles (daily audit). |
| `dep_update_check.py` | Sun 03:00: `pip list --outdated` notifier (check-only). |

## Tier 2 — 🔵 DEP (imported by a LIVE file — never delete)

| File | Imported by | Role |
|------|-------------|------|
| `screener/fno_strategy.py` | `fno_collector.py` | Gate/regime/structure "single source of truth". |
| `screener/vishal_fno_screener.py` | `fno_collector.py`, `portfolio_monitor.py` | ⚠️ **Name says "retired" (the daily-pick *strategy* is), but the file is a live library** — exports `FNO_UNIVERSE`, `TRACKER_COLS`, `get_kite`, `load_instruments`. If you want to truly retire it, first move those constants/helpers into a small `fno_universe.py` and repoint the two importers. |
| `screener/orion_v2514_backtest.py` | `v12_vishal.py`, `smi_focus35.py` | Validated SMI/indicator math + engine harness (`OB.smi`, etc). |
| `screener/smi_paper.py` | `smi_focus35.py` | `S` = shared exits / fill model / `main()` orchestration. (Tracked, **locally modified** — needs committing.) |
| `screener/smi_paper_flow.py` | `smi_focus35.py` | `F` = the V12 flow-gate (`flow_veto`, `add_flow_features`). Standalone engine was removed 06-18; kept as library only. |

## Tier 3 — 🛠️ TOOL (on-demand, keep)

| File | What it does |
|------|--------------|
| `watch_trade.py` | Live alignment watcher — state vs dashboard vs Telegram, every 2s. Documented in CLAUDE.md. |
| `trace_trade.py` | Post-trade reconciler (SL tier, pnl math, entry_mode). Documented in CLAUDE.md. |
| `sl_replay_study.py` | **Canonical** SL-ladder replay — every ladder change (`PROTECT`/`LOCK_4`/`TRAIL`/`LOCK_25`) was validated here. Keep as the regression harness for any future ladder edit. |
| `screener/smi_force_close.py` | Manual flat-out of open FOCUS35 paper trades. |
| `screener/smi_backtest.py` | Canonical SMI E2 backtest + parameter tuner (the engine's origin study; still the re-tune harness). |

## Tier 4 — 🟡 PENDING (active R&D — keep until its review date)

| File | Decision tied to | Note |
|------|------------------|------|
| `v11_vac_shadow.py` | ~06-30 | VAC anti-chase shadow tracker; owner chose shadow-log over gate, re-judge end of month. |
| `v11_tick_imbalance_study.py` | ~06-26 | Studies tick-delta predictiveness; waiting on collector data to accumulate. |
| `screener/v12_flow_divergence_study.py` | shipped, ref | The study that **defined** the live V12 veto (keep E2+L1+L2, drop E3+L3). Keep as the reference/re-validate tool. |
| `screener/focus35_signal_lab.py` | ~06-25 | Horse-races orthogonal signals for FOCUS35 (exploratory). |
| `screener/focus35_exit_study.py` | ~06-25 | Exit-tuning for FOCUS35. |
| `screener/focus35_sl_regime_study.py` | ~06-25 | ATR-SL / regime tests (both ~rejected). |
| `screener/focus35_perstock_study.py` | ~06-25 | Per-stock OOS cull check. |
| `screener/focus35_cross_sectional_study.py` | ~06-25 | Basket/cross-sectional concept. |
| `screener/focus35_optionrider_study.py` | ~06-25 | "Manage on stock, fat tail lives in the option premium" concept. |
| `screener/smi_rnd_study.py` (+ `smi_rnd_report.md`) | ~06-25 | PE NIFTY-bear gate + exit R&D package. |
| `screener/gex_zones_poc.py` | unproven | Gamma-flip TREND/CHOP separator POC. Keep if you still want to test it; else 🗄️. |
| `screener/dz_zones.py` | this session | S/D-zone viewer. Useful as a manual tool; the **veto built on it was a dud** (see below). |

## Tier 5 — 🗄️ ARCHIVE (finished; conclusion already saved — safe to move/remove)

These are completed one-off studies. Their conclusions are captured in `CLAUDE.md` / auto-memory,
so the code itself is no longer load-bearing. Recommend `git rm` or move to `screener/studies_archive/`.
None are imported by anything live.

**V12 R&D pile (conclusions in memory: `v12_side_selection`, `v12_flow_divergence`, `v12_option_model`):**
`v12_5m_freq.py` · `v12_5m_freq2.py` · `v12_5m_study.py` · `v12_5m_tune.py` · `v12_asym_study.py` ·
`v12_audit_points.py` · `v12_capture_opt.py` · `v12_combo_study.py` · `v12_dynamic5m_study.py` ·
`v12_dynamic_study.py` · `v12_factor_study.py` · `v12_final_5m.py` · `v12_final_study.py` ·
`v12_momentum_study.py` · `v12_option_chain_backtest.py` · `v12_option_optimizer.py` ·
`v12_pe_hunt.py` · `v12_veto_ce_study.py` · `v12_stock_accuracy.py` · `v12_icici_wide.py`

> **Exception — keep these tuners as 🛠️ TOOLs** (they regenerate the live `FOCUS` dict / gates, you'll
> re-run them at the next tune): `v12_batch_tune.py`, `v12_focus_tune.py`, `v12_one_stock_tune.py`,
> `v12_focus_expand.py`.

**SMI / stock R&D (conclusions saved):**
`smi_pe_tuning.py` · `smi_single_filter.py` · `smi_trend_study.py` · `multibagger_smi_study.py` ·
`split_atm_backtest.py` (interim done) · `screener/orion_strategy_backtest.py` (6-stock orion;
superseded by `orion_v2514_backtest.py`, not imported).

**Study output reports (.md) — keep the conclusions, archive alongside their study:**
`orion_v2514_report.md` · `smi_backtest_report.md` · `v12_final_report.md` · `v12_final_strategy.md`
(and `smi_rnd_report.md` stays with its 🟡 study until 06-25).

## Tier 6 — 🔴 REMOVE (dead / proven dud)

| File | Why |
|------|-----|
| `screener/dz_veto_backtest.py` | Built & run this session. **Verdict: the S/D-overhead veto loses on all 104 trades / both timeframes** (removes net-positive trades, zero separating power). Conclusion recorded; the script has no reuse value. |
| `v11_flow_veto_replay.py` | Showed the V12-flow-veto→V11 port is a dead end offline (conclusion in `project_vishal_anti_chase`). |
| `__pycache__/` | Build artifact — should be `.gitignore`d, never committed. |

---

## VRL_MAIN.py internal map (the ~9k-line file)

Not split into modules — it's one file aliased as its own namespace
(`D = CFG = LEVELS = CHARGES = MSTOCK = sys.modules[__name__]`). Major regions, top→bottom:

1. **Config & constants** — loads `config.yaml`, all `V11_*` locked strategy values (gates, ladder, windows).
2. **Broker layer** — Kite (data) + m.Stock (`ms_place_buy/sell`, `ms_get_funds` with the 3× retry / stale-balance guard).
3. **State** — `_v11_state` dict + `_V11_PERSIST_FIELDS` (restart persistence) + `_v11_lock` (RLock).
4. **V11 scanner / strategy loop** — 3s scanner keeps `_v11_live` warm; fires entry when MOMENTUM + OPP DECAY pass.
5. **Entry/exit** — `_v11_execute_paper_entry/exit` (paper + wired live m.Stock path), `_v11_compute_trail_sl` (the ladder), `_v11_check_exit` (tick-based, runs before candle gate — BUG-01).
6. **Strike selection** — `resolve_strike_for_direction` / `_lock_strikes` (ITM-100 CE-floor/PE-ceil).
7. **Telegram** — `TGListener` thread + commands (`/forceexit` etc).
8. **Web** — `_WEB_HTML` string (dashboard source of truth) + `_start_web_server` + `_write_dashboard`.

> The legacy V7 engine (~1,140 lines) was already removed 2026-06-13. There is **no known dead code
> block left inside VRL_MAIN.py** — it's a single live path. Housekeeping opportunity here is *not*
> deletion but optional **splitting** into modules (brokers / strategy / web) if the single file gets
> unwieldy — that's a refactor, not a cleanup, and risky on a live bot. Recommend leaving as-is.

---

## Recommended action order (when you're ready)

1. **Add `.gitignore`** for `__pycache__/`, `*.pyc`, `state/*.json` (runtime), `static/VRL_DASHBOARD.html` (generated).
2. **Commit the in-flight tracked changes** — `screener/smi_paper.py`, `v12_vishal.py`, `screener/nse500_symbols.txt` are modified but uncommitted.
3. **Decide the untracked Tier-4/5 files** — most are untracked *and* uncommitted; archiving = `git rm`-equivalent (just delete, they were never in git) or move to `screener/studies_archive/`.
4. **Tier 6** — delete now (`dz_veto_backtest.py`, `v11_flow_veto_replay.py`).
5. **Before touching Tier 2** — repoint importers first (esp. `vishal_fno_screener.py`).

> The big realisation from building the map: the repo's mess is **R&D study scripts**, not dead
> production code. The live system is a tight set — `VRL_MAIN.py` + the 5 cron engines + their 5
> library deps. Everything else is a finished experiment whose answer already lives in `CLAUDE.md`
> and memory. Clearing Tier 5/6 removes ~30 files with zero risk to anything that runs.
