# VRL Trading Bot — Developer Reference

> Last resynced: 2026-06-18 (OPP DECAY band tightened [−8,−6]→[−9,−7] dte≥2; keep FOCUS35, removed frozen/loose/flow stock-F&O engines). Single-file bot: `VRL_MAIN.py` (~10,000 lines).
> Grep by symbol name — line numbers in this doc are approximate.

---

## 🤖 Agent Sync Protocol (Claude Code ↔ Google Antigravity)

**Both agents must read this file first before any code change.**

If you are Google Antigravity reading this:
1. Read this entire CLAUDE.md — it is the single source of truth for architecture, strategy values, and rules.
2. After any change you make to `VRL_MAIN.py`, `config.yaml`, `watch_trade.py`, or `trace_trade.py` — update the relevant section of this file in the same PR. Do not leave CLAUDE.md stale.
3. **Never change locked strategy values** (OPP DECAY [−9,−7] dte≥2, initial SL cap entry−10, PROTECT @+9 → entry−2, LOCK_4 @+11 → entry+4, TRAIL_10 @+15 → max(entry+9, peak−10), 1 lot single market fill, ITM-100 strikes CE-floor/PE-ceil) without the user's explicit instruction.
4. Follow the PR workflow (branch → commit → `gh pr create` → `gh pr merge --squash`) — no direct pushes to main.
5. If you add a new `_v11_state` key that must survive restart, add it to BOTH the initial `_v11_state` dict AND `_V11_PERSIST_FIELDS`.
6. Update the `> Last resynced:` date at the top of this file whenever you resync it.

Claude Code follows the same rules. Both agents stay in sync through this file and git history.

---

## Project Overview
NIFTY weekly-options bot. Zerodha **Kite** for market data, **m.Stock** for live order placement.

- **Market-data source is config-driven** (2026-06-19): `config.yaml` → `data_provider: kite | upstox` → `D.DATA_PROVIDER`. Default **kite**. When `upstox`, the **entire** Kite data layer routes through `upstox_data.py` — REST (`get_historical_data`, `get_spot_ltp`/`get_vix`, `get_lot_size`, `get_nearest_expiry`, `get_option_tokens`) **and the live tick feed** (`start_websocket`/`subscribe_tokens`/`unsubscribe_tokens`/`get_ltp`/`check_and_reconnect` via the Upstox v3 protobuf WebSocket). Under `upstox` the bot needs **no Kite session at all** (startup skips `get_kite()`/`D.init(None)`; VWAP/Kite-margin dashboard bits degrade silently). Moving to Upstox because Kite data API costs ~₹500/mo; Upstox data is free. **Orders ALWAYS go to m.Stock — Kite/Upstox are data-only, never order routes.** Parity (06-19): spot/VIX/index identical; option 1-min candles differ ~0.2pt avg (vendor tick aggregation) — within entry noise. ⚠️ Still PAPER-validate a few sessions before flipping live.

- **Mode** is config-driven: `config.yaml` → `mode: paper | live` → `D.PAPER_MODE = CFG.is_paper()`
  - **paper**: fills simulated, logged as `PAPER_*`, zero slippage
  - **live**: ✅ **WIRED 2026-06-15 (owner-approved).** The V11 path now calls the m.Stock order primitives behind `not D.PAPER_MODE`:
    - **Entry** (`_v11_execute_paper_entry`): calls `place_entry(_kite, symbol, token, direction, qty, entry_price)` → `ms_place_buy` **LIMIT at ref + buffer (1%, min 2pts), 8s cancel**. On non-fill/rejection the entry is **aborted** (no `in_trade`), a TG "entry MISSED" alert fires, and the candle is stamped so the 3s scanner won't hammer it. On fill, the **broker fill price** (not the candle close) becomes `entry_price` for all SL/PnL math.
    - **Exit** (`_v11_execute_paper_exit`): calls `place_exit(_kite, …)` → `ms_place_sell` **MARKET** (with built-in retry/backoff) **before** clearing state. If the exit ultimately fails the position is **still open at the broker**, so state is NOT cleared and NO CSV row is written (`in_trade` stays True) — a critical TG "MANUAL ACTION" alert fires and the exit ladder/EOD retries on the next tick. On success the **broker fill price** is recorded.
    - Both broker calls run **outside `_v11_lock`** (they block up to ~8s; holding the RLock that long would freeze the exit/TG/web threads). The **paper path is byte-for-byte unchanged.**
    - ⚠️ **Live ≠ paper track record**: paper fills at candle close (zero slippage); live entry is a LIMIT that can **miss** fast breakouts paper caught (owner chose LIMIT+buffer over MARKET, 2026-06-15). Flip `config.yaml` → `mode: live` + restart to activate. CSV `entry_slippage`/`exit_slippage` still hardcode 0 (real slippage not yet threaded through — follow-up).
    - The old V7 real-order path (`_execute_entry`/`_execute_exit_v13`) was removed 2026-06-13 (dead — gated on the legacy `state` dict V11 never sets). The m.Stock *read* calls (`ms_get_funds`, `ms_get_banner_line`) ARE live and feed the dashboard. ⚠️ m.Stock's `fund_summary` gateway intermittently returns a **502 HTML bot-protection page** (`validate.perfdrive.com`) instead of JSON (~50% during incidents) — this used to flip the dashboard to the bogus Kite-margin fallback (negative number) for 5 min per failure. `ms_get_funds` now **retries 3× (1.5s gap) per refresh** and on total failure **serves the last-good balance flagged `stale`** (dashboard name shows `(m.Stock stale)`) instead of dropping to Kite, with a 20s failure-cooldown before re-trying (2026-06-19, PR pending).
- **Strategy**: V11 Golden — 1-min engine. `V11_LIVE = True`. Version string: `v21`.
- No shadow scanners, no P3, no V2 trackers — single code path. **Legacy V7 engine removed 2026-06-13** (~1,140 lines: `_execute_entry`, `_execute_exit_v13`, `check_entry`, `_evaluate_entry_gates_pure`, `_evaluate_exit_chain_pure`, `manage_exit`, `compute_trail_sl`, `pre_entry_checks`, `evaluate_cross_leg`, `evaluate_filters`/`log_entry` shadow-logging, + the two dead `if _in_trade:` loop blocks). File ~10,150 → ~9,010 lines.

### Module-as-namespace pattern
`VRL_MAIN.py` aliases itself:
```python
D = CFG = LEVELS = CHARGES = MSTOCK = sys.modules[__name__]
```
`CFG.is_paper()`, `D.PAPER_MODE`, `MSTOCK.ms_place_buy()` etc. all resolve to functions in this file.
When searching for "dead" code, count dotted refs (`D.foo`, `MSTOCK.foo`) — a function can be live via aliases.

---

## V11 Golden Strategy — LOCKED VALUES

### Entry gates (both must pass)
| Gate | Condition | Constant |
|------|-----------|----------|
| **MOMENTUM** | 1-min option `close >= ema9_high + 3.5` (dte≥2) | `V11_MIN_EMA9H_GAP = 3.5` (hard gate) |
| **OPP DECAY** | opposite leg: `close − ema9_low` in `[−9.0, −7.0]` (dte≥2) | `V11_DECAY_LOW = -9.0` / `V11_DECAY_HIGH = -7.0` |

- **Per-DTE %-gate for dte 0/1 (owner-approved 2026-06-16, LIVE)**: near-expiry ATM premium collapses (~50 @dte0, ~113 @dte1) so the absolute +3.5/[−9,−7] gate over-fires on cheap premium. For **dte 0 and dte 1** the gate is normalized to **% of premium** via `_v11_gate_check(dte, …)` + `V11_PCT_GATE_DTE`:
  - **dte 0**: MOMENTUM `close ≥ ema9_high + 2.3%·close`, OPP DECAY `(opp_margin/opp_close) ∈ [−4.8%, −2.7%]`
  - **dte 1**: MOMENTUM `+3.0%·close`, same decay band `[−4.8%, −2.7%]`
  - **dte ≥ 2**: **unchanged** — the locked absolute gate above.
  - Calibrated by the expiry-aligned per-DTE sweep (`~/lab_data/perdte_pct_gate_study.py`, 21 days / 5 weekly expiries): decay floor −4.8% stable across DTE; momentum % rises away from expiry. dte0 flipped −180%→+75.5% (n=8, 75% WR), dte1 −58.5%→+39.1% (n=17, 65% WR). **In-sample** — owner shipped for live validation; revisit at the ~06-26 FINAL PACKAGE review. The full per-DTE table (incl. dte 4/5/6 = NO-TRADE: no positive gate exists) lives in `perdte_pct_gate_study.py` `PERDTE_GATES`; only dte 0/1 are wired live (the bot trades NIFTY weeklies = ~always dte 0/1).

- **Decay band tightened to `[−9, −7]` (owner-approved 2026-06-18, dte≥2)**: from `[−8, −6]`. The decay-floor sweep over all logged trades + the 06-18 session showed the shallow half of `[−8,−6]` holds the losers — on dte≥2 band `[−8,−7]` = 78% WR / +8.73 per trade (n=9) vs `[−8,−6]` 44% WR / +1.52 (n=27); on 06-18 (dte5) the +25 winner sat at −7.23 (inside) while all 4 losers were −6.3..−6.9 (excluded). Deep floor extended −8→−9 (neutral on dte≥2, same 9 trades; admits a few neutral deeper trades all-dte). ⚠️ best bucket n=9 — shipped on owner's explicit call, re-confirm at the ~06-26/30 FINAL PACKAGE review. Prior `[−8, −6]` was owner-final 2026-06-12 (widened from a midday-only deep window; shallow `(−6,−4]` ran 2W/9L −34 pts; study `~/lab_data/xleg_context_study.py`). dte 0/1 still use the %-gate above (UNCHANGED).

- **Entry window 10:00–14:30 (owner 2026-06-15)**: `V11_OPEN_BLACKOUT_END = dtime(10, 0)` (was 09:45) + `market_hours.entry_cutoff` 14:30 (was 15:00). Disciplined window — conviction_sizing_study showed 09:00-10:00 bled −₹788/trade; muhurat_kuttaka_study (OOS) showed 14:30–15:15 toxic (−2.24 fwd5/40% WR). Exits/EOD unchanged (EOD still 15:15; a trade opened at 14:29 is still managed to exit — its token stays subscribed from lock time, exits run unconditionally per BUG-01).
- **Same-candle guard** (`_last_fired_candle_ts`) — no double-entry on same 1-min candle
- **Exit-candle cooldown** (`_last_exit_candle_ts`) — no re-entry on same candle as exit
- **Same-side 3-min blocker** (`_last_exit_direction_v10` + `_last_exit_time_unix`) — after any exit, same direction blocked for 180s (any strike). Prevents post-trail chasing and rapid same-side re-entries
- ~~Exhausted-loss re-entry block~~ — **removed 2026-06-11 (owner instruction)**: live counterfactual showed it skipped only 1 of 10 losers while blocking recovery winners (incl. a +32). Replaced by the midday deep-decay window above.

### Strike selection — ITM-100 "intelligent" strikes (owner 2026-06-17, v22, PAPER 1-WEEK TRIAL)
`V11_STRIKE_STEP = 100`, `resolve_strike_for_direction`: **CE floors** to the 100 below spot
(→ strike ≤ spot → ITM call), **PE ceils** to the 100 above spot (→ strike ≥ spot → ITM put).
50-step half-strikes were too illiquid. CE and PE now sit on DIFFERENT strikes but BOTH ITM
(they straddle spot, so ITM depth always sums to 100 — at an exact round-100 spot both collapse
to true ATM). **Why ITM both sides:** the OPP DECAY gate must read a meaningful opposite leg.
The scanner already pairs `own=_locked_tokens["CE"]` with `opp=_locked_tokens["PE"]`, so locking
the two ITM legs means a **CE entry reads decay on the ITM PE** (and vice-versa) — not an OTM leg
that is always decaying anyway (owner's rationale). `_lock_strikes` neighbor pre-warm widened
±50 → ±100; relock hysteresis rewritten to 100-band-cross + 15-pt buffer (was `round(spot/50)`).
⚠️ **Trial caveats**: (1) depth asymmetry is inherent to a 100 grid (one leg near-ATM, the other
near +100 deep-ITM depending on where spot sits in the band); (2) the SL ladder below is in
ABSOLUTE premium points calibrated on ~ATM 50-step premium — deep-ITM premium is larger / moves
differently, NOT auto-rescaled; (3) the 06-15 split-ATM backtest found ITM LOSES on V11
(−92 pts/33tr) — this is a 1-week PAPER forward test to measure the real difference, revisit ~06-24.

### Execution — single lot
Config: `lots_fixed: 1`, `lot_size: 65` → 65 qty, single market fill at the last 1-min candle close.
(Split-lot 50/50 with a Lot 2 limit order was removed 2026-06-10 — user found Lot 2 added complexity with no edge; trades often hit SL before the limit mattered.)

- **Initial SL**: `ema9_low` of breakout candle (`_v11_state["initial_sl"]`), **capped at `entry − 10.0`** (max-risk cap, owner-approved 2026-06-11, validated by `sl_replay_study.py`: 0 winners clipped over 53 replays). Fallback: `entry − 5.0` if ema9_low ≥ entry

### Exit ladder — `_v11_compute_trail_sl(entry_price, peak_pnl, initial_sl)`
Tick-based (~1s), runs BEFORE the candle gate (BUG-01):

```
peak < 9 pts   → INITIAL    : SL = initial_sl  (ema9_low capped at entry − 10)
peak ≥ 9 pts   → PROTECT    : SL = max(initial_sl, entry − 2.0)
peak ≥ 11 pts  → LOCK_4     : SL = max(initial_sl, entry + 4.0)
peak ≥ 15 pts  → TRAIL_10   : SL = max(initial_sl, entry + 9.0, peak_ltp − 10.0)
peak ≥ 25 pts  → LOCK_25    : SL = max(initial_sl, entry + 25.0, peak_ltp − 5.0)
```

- **`LOCK_25` floor + tight trail (owner-approved 2026-06-15)**: `V11_TARGET_PTS = 25.0`. New top
  rung in `_v11_compute_trail_sl`: `peak ≥ 25 → SL = max(initial_sl, entry+25, peak_ltp−5)`. Once
  peak hits +25 it locks **entry+25 as a hard floor** (guaranteed +25 min) AND trails **peak−5**
  above it (tight trail to grab max points on the runner). Floor binds for peak +25..+30; above
  +30 the peak−5 trail takes over (peak +40 → SL +35, peak +50 → SL +45). Evolved same day:
  +25 hard-exit → +25 floor w/ peak−10 trail → owner tightened the runner trail to **peak−5** to
  capture more. `~/lab_data/target_replay.py` (92tr, peak-vs-outcome): the +25-floor variant was
  +163 vs +108.7 hard-exit vs +87.8 bare trail; peak−5 grabs ~+5 more per clean runner on top.
  Caveat: peak−5 is a tight trail — more shakeout risk on choppy pullbacks than peak−10 (not fully
  measurable from candle data; owner accepted the trade-off for max capture). (Opposite-leg re-entry block was
  studied the same day and **rejected** — no edge, kills reflexive-opposite winners like 06-10 CE
  +22.9; consistent with the 06-11 re-entry-blocker finding.)

Exit reasons: `EMERGENCY_SL` · `PROTECT_2` · `LOCK_4` · `VISHAL_TRAIL` · `LOCK_25` · `EOD_EXIT` · `FORCE_EXIT` (TG `/forceexit`)
(LOCK_4 replaced BREAKEVEN on 2026-06-10. PROTECT tier + LOCK_4 trigger 12→11 added 2026-06-11, owner-approved,
validated by `sl_replay_study.py`: +31.5 pts over 54 replayed trades, 0 trades made worse.)
**Merged top rung — owner-approved 2026-06-13:** the old separate `+18 → peak−10` tier was
folded into one `peak ≥ 15` rung: `max(entry+9, peak−10)`. The `+9` floor holds from peak +15..+19
(peak−10 only overtakes +9 at peak 19), then the trail takes over — so removing the +18 gate
changes nothing for big winners while locking +9 (not +4) on the +15..+18 mid-winners that the
old ladder left under-protected. Replay (`sl_replay_study.py`): +25.1 pts over 73 trades, 0 made worse.)

- **EOD hard-close**: `config.yaml` → `exit.ema9_band.eod_exit_time` = **"15:15"** (changed from 15:20 on 2026-06-10). Checked tick-based inside `_v11_check_exit()`.
- **No-tick safeguards** (PR #210, 2026-06-10 incident — restart after 15:00 left the open trade blind, EOD never fired):
  1. Startup resubscribes the in-trade token + `_other_token` unconditionally (option tokens are otherwise only subscribed via `_lock_strikes()`, which is gated to the 09:15–15:00 trading window).
  2. If `ltp <= 0` when EOD time is reached, the trade is force-closed at average entry price (same fallback as `/forceexit`) instead of silently skipping the exit check.

### Per-day counters
`_v11_state`: `_trades_today`, `_wins_today`, `_losses_today`, `_pnl_today_pts` — reset at midnight. No hard daily cap.

---

## File layout

| Path | Purpose |
|------|---------|
| `VRL_MAIN.py` | Everything: config, brokers, strategy loop, entry/exit, TG handler, web server |
| `config.yaml` | Runtime config — `mode`, instrument, lots, EMA bands, thresholds, market hours |
| `trace_trade.py` | Post-trade audit script (standalone, no Claude dependency) |
| `watch_trade.py` | Live alignment watcher — polls state/dashboard/TG every 2s (standalone) |
| `upstox_data.py` | **Upstox market-DATA backend** (migration, 2026-06-19). Drop-in for Kite *read* calls (historical/ltp/quote/instruments/expiry/strike/lot) when `config.yaml`→`data_provider: upstox`. Keeps `token` an int (= Upstox `exchange_token`) with an internal bridge `exchange_token→instrument_key` so VRL_MAIN state/CSV/`int(token)` casts are untouched. Full REST + live **protobuf WebSocket** (Upstox v3 market-data-feed via `upstox-python-sdk`'s `MarketDataStreamerV3`, mode `ltpc`; decoded ltp cached by canonical int token → `get_ltp` unchanged). Under `upstox` the bot runs with NO Kite session. Orders always m.Stock. PAPER-validate before live. |
| `upstox_auth.py` | Headless daily Upstox token refresh (`upstox-totp`): generates token → writes `UPSTOX_ACCESS_TOKEN` to `~/.env` → verifies via profile. Cron 06:00 Mon-Fri (token expires 03:30). No Upstox login password (flow = OTP→TOTP→PIN); `UPSTOX_PASSWORD` is unused, set to the PIN. |
| `upstox_parity.py` | READ-ONLY Kite↔Upstox parity harness (reuses VRL_MAIN's real Kite fns). Verdict 06-19: spot/index/VIX **bit-identical**, spot 1-min 99.7% exact; option 1-min differ ~0.2pt avg (max ~2pt, vendor tick aggregation). |
| `paper_wide.py` | INDEPENDENT wide-window (09:30–15:15) paper engine for data collection (owner 2026-06-15). Imports VRL_MAIN and reuses its REAL gate fns (`get_option_1min`/`_v11_compute_trail_sl`) → zero divergence. Own state (`state/paper_wide_state.json`) + log (`lab_data/paper_wide_log.csv`). SILENT all day; ONE EOD Telegram summary. Never touches live state / never places orders. Cron 09:25 Mon–Fri, self-exits after EOD. A/B vs live's narrow 10:00–14:30 window at ~06-25. |
| `screener/smi_focus35.py` | FOCUSED stock engine (owner 2026-06-17, "1 code for the keeper stocks, monitor 1 week"; renamed focus9→focus35 on 06-18 to match the count). One engine, each stock running its OWN tuned V12 gate from the per-stock tuning (`v12_one_stock_tune.py` / `v12_batch_tune.py`): per-stock **SMI period (k) · cross bands (±ob) · direction · flow on/off** in the `FOCUS` dict. **35 stocks** (started as 9, hence the old "focus9" name — original 9 + 6 liquid-expansion on 06-18 + 2 batch-2 on 06-18 + 18 batch-3 liquid-subset on 06-18). The 18 batch-3 names (win≥75% from a full-cache sweep, thin/penny option-books dropped) were then **DEEP-TUNED** by `screener/v12_focus_tune.py` — a wider 2430-config/stock grid that also sweeps SMI smoothing (`d`/`sig`), `sl`% and `trail`%, optimizing max avg%/trade subject to n≥12 & win≥70% (the original batch tuner froze those at 3/3, 1.0%, 1.5%). The FOCUS dict carries the per-stock overrides; the engine honors them (`smi_focus35` passes `d`/`sig` to `OB.smi`; `smi_paper` sets per-trade `sl_price` from `sl_pct` and reads `trail_arm`). Post-tune in-sample win/avg: MPHASIS 92%/+0.98 · TATACONSUM 92%/+0.64 · ZYDUSLIFE 80%/+0.64 · APOLLOHOSP 75%/+0.47 · DMART 85%/+0.59 · DLF 75%/+0.83 · JIOFIN 77%/+0.64 · LUPIN 86%/+0.27 · AUBANK 86%/+0.54 · LICHSGFIN 77%/+0.70 · INDUSINDBK 83%/+0.67 · TRENT 79%/+0.52 · DIVISLAB 92%/+0.67 · BPCL 75%/+0.37 · COALINDIA 79%/+0.80 · SIEMENS 77%/+0.93 · CHOLAFIN 77%/+0.58 · MCX 75%/+0.81. ⚠️ **Heavily in-sample / overfit by design** — best-of-2430 per stock; treat win-rates as a screen, not a guarantee. The deep tune mostly found edge in tighter SL (0.8%) and faster trail-arm (1.0–1.5%), not the frozen 1.0/1.5. Original 9 + WR: MARUTI k21/±45/both/flowON 87% · LT k30/±50/both/flowON 87% · TITAN k21/±50/CE/flowON 83% · INFY k30/±35/PE/flowOFF 83% · BHARTIARTL k40/±40/both/flowON 83% · RELIANCE k30/±50/both/flowON 79% · BAJFINANCE k30/±50/both/flowON 79% · TCS k21/±35/both/flowOFF 73% · SBIN k30/±45/both/flowON 71%. 06-18 liquid expansion (+6): KOTAKBANK k40/±45/both/flowON 93% · ASIANPAINT k21/±35/PE/flowOFF 81% · HINDUNILVR k30/±50/PE/flowOFF 79% · M&M k30/±40/PE/flowOFF 75% · ADANIPORTS k30/±40/both/flowON 75% · HDFCLIFE k30/±40/both/flowON 71%. 06-18 batch-2 (+2): HEROMOTOCO k30/±35/PE/flowOFF 83% · NESTLEIND k40/±35/PE/flowOFF 73%. All expansion picks pass the same batch-tune keeper bar (win≥70% & avg>0, in-sample 40d). Only these 35 ever fire; everything else returns None. Shared exits/fill (1% SL · trail +1.5% · 15:15 EOD · 1 lot ATM). Reuses `smi_paper` + `smi_paper_flow` + `orion_v2514_backtest`; own state/log/tracker (`smi_focus35_state.json` / `smi_focus35_log.csv` / `fno_tracker_focus.csv`, `structure=SMI_FOCUS`), TG "SMI FOCUS35", own cron, log `~/logs/smi_focus35.log`, green "FOCUS35" dashboard badge. **IN-SAMPLE TUNED — this is the ~1-week forward-validation run, NOT a proven edge; judge the week vs the in-sample win-rates above.** |
| `screener/smi_paper_flow.py` | **LIBRARY ONLY (standalone FLOW engine removed 2026-06-18).** Kept because `smi_focus35` imports it for the V12 flow-gate (`F.VOL_WIN`, `F.add_flow_features`, `F.flow_veto` — L1 effort-vs-result + L2 A/D-divergence veto, `v12_vishal.flow_veto` math). Its own cron/state/log/tracker (`fno_tracker_flow.csv` etc.) are gone — do not run it directly. |
| `sl_replay_study.py` | SL-ladder replay backtest — re-runs historical trades against `lab_data/options_1min` candles under candidate SL rules (standalone, read-only) |
| `screener/` | Stock F&O SMI paper engine + multibagger screeners (separate processes, not imported by VRL_MAIN) |
| `static/VRL_DASHBOARD.html` | **Generated artifact** — overwritten from `_WEB_HTML` on every restart. Never edit directly. |
| `state/vrl_v11_state.json` | **Primary V11 engine state** — `_v11_state` |
| `state/vrl_live_state.json` | Legacy V7 state — still written by bot, not used by V11 strategy logic |
| `state/vrl_dashboard.json` | Dashboard snapshot — full rebuild (`_write_dashboard`) once per 1-min candle + after every exit (V11 and V7 paths); fast path `_update_dashboard_ltp` every 5–10s only refreshes ts/LTP/position, never the `today` block |

### Stock F&O — SMI FOCUS35 is the only engine (owner 2026-06-18: "keep focus35, remove other stock F&O code")
Old daily-pick screener strategy **retired 2026-06-11** (crons removed: `vishal_fno_screener.py`
15:40 + `fno_collector.py --tick`; files kept on disk for rollback; final book +₹18,709,
archived to `fno_tracker_archive.csv`). `fno_collector.py --morning` still runs (universe/OHLCV cache).

**Removed 2026-06-18** — the three parallel SMI paper engines (frozen `smi_paper`, `smi_paper_loose`,
`smi_paper_flow`): their crons deleted, the LOOSE engine file + the dead `smi_loose_optimize.py`
study `git rm`'d, and all their data purged (`fno_tracker.csv` / `fno_tracker_loose.csv` /
`fno_tracker_flow.csv` + the matching `*_state.json` / `*_log.csv` + the `.bak_20260617` backups).
`smi_paper.py` and `smi_paper_flow.py` files are **kept ON PURPOSE** — `smi_focus35` imports them
as libraries (S = exits/fill/`main()` orchestration, F = the flow-gate). They no longer run standalone.

**`screener/smi_focus35.py` — the surviving engine.** 35 stocks, each running its OWN tuned V12
gate (per-stock SMI period `k` · cross bands `±ob` · direction · flow on/off · `d`/`sig`/`sl`/`trail`
overrides in the `FOCUS` dict). Cron every 15m bar close +2min (09:47–15:31 Mon–Fri), log
`~/logs/smi_focus35.log`. Reuses the shared SMI math / exits / fill model from `smi_paper`
(SMI RMA(Wilder) base; **exits**: SL 1% of entry — per-stock override via `sl_pct`; trail arms at
+1.5% peak — per-stock `trail_arm`; close vs SMA8; 15:15 force close; paper fill = 1 lot
nearest-expiry ATM option at LTP). Own state/log/tracker (`smi_focus35_state.json` /
`smi_focus35_log.csv` / `fno_tracker_focus.csv`, `structure=SMI_FOCUS`), TG "SMI FOCUS35",
green "FOCUS35" dashboard badge. **IN-SAMPLE TUNED — ~1-week forward-validation, NOT a proven edge.**

**Dashboard:** the F&O tab's `_web_read_fno()` now reads ONLY `fno_tracker_focus.csv`
(tag `engine=FOCUS`, green "FOCUS35" badge). The frozen/loose readers were removed with the engines.
SMI has NO fixed target (t1/t2 blank by design) — the card's progress bar/target fields stay empty.
`watch_trade.py` now watches the FOCUS35 state/tracker/log; `smi_force_close.py` flat-outs FOCUS35.

### Stale artifacts in state/ (do not rely on)
- `vrl_shadow_state.json` — shadow scanner removed; file is stale
- `bw_gap_study.csv` — BW/RSI study; gates removed in V11 Golden
- `vrl_zones.json` — zones engine removed; `/api/zones` route deleted 2026-06-10

### Dashboard source of truth
`_WEB_HTML = r"""..."""` string in `VRL_MAIN.py` (~line 8956). `_start_web_server()` overwrites
`static/VRL_DASHBOARD.html` from this string on every startup.
**Always edit `_WEB_HTML` — never the static file.**
Only `vrl-main.service` runs (port 8080). `vrl-web.service` was retired 2026-06-07.

Tabs: **SIG** (V11 gates + position + MSTOCK account + rolling performance) · **F&O** (stock
options portfolio, lots/invested/P&L) · **TRD** (trade log) · **WKLY** (multibagger model
portfolio, 1 share each) · **FILES**. The MKT tab was retired 2026-06-10 — it showed V7-era
analytics (spot/option multi-TF tables, fib pivots, zones, straddle) that no V11 gate uses;
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
**Trade CSV**: `~/lab_data/vrl_trade_log.csv` (`entry_mode` = `V11_CE` / `V11_PE`; paper fills tagged `PAPER_*`; `spot_regime` = 3-min EMA regime at fire time — analysis only, not a gate; `pdh_prev`/`pdl_prev`/`entry_range_pos` = prev-day high/low + spot position in that range at entry, added 2026-06-11 — analysis only, candidate gate after 2–3 weeks of data: PE entries mid-range ran 23% win rate)

### Deploy after any main merge
```bash
cd ~/VISHAL_RAJPUT && git checkout main && git pull && sudo systemctl restart vrl-main.service
```

---

## State persistence
`_V11_PERSIST_FIELDS` controls what survives restart. Any new key MUST be added to BOTH:
1. The initial `_v11_state = { ... }` dict so `_load_v11_state` restores it
2. `_V11_PERSIST_FIELDS` so `_save_v11_state` writes it

Fields currently persisted:
`in_trade`, `symbol`, `token`, `direction`, `strike`, `entry_price`, `entry_time`, `qty`,
`peak_pnl`, `active_ratchet_tier`, `active_ratchet_sl`, `candles_held`, `_other_token`,
`_sl_cooldown_skip_next`, `_force_exit_ts`,
`_pnl_today_pts`, `_trades_today`, `_wins_today`, `_losses_today`,
`_v11_both_rejected_ts`, `_last_trade_date`, `_last_exit_candle_ts`,
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
- **`_v11_lock`** — `threading.RLock()` — protects all `_v11_state` reads/writes; RLock allows `_save_v11_state()` to re-enter from within exit-check block
- **`_state_lock`** — protects legacy `state` dict
- **Rule**: any function callable from both main loop and TG/web thread must hold `_v11_lock` for the full check-and-act section. Never check under lock, release, then act.

## V11 Golden scanner (inside `_strategy_loop`)
```
_v11_scanner_last_ts  — throttle: scanner runs every 3s
_v11_live             — dict {"CE": {...}, "PE": {...}} — gate snapshot fed to dashboard
_v11_live_lock        — threading.Lock() protecting _v11_live
```
Scanner runs every 3s **regardless of `in_trade`** so `_v11_live` stays warm with live EMA9 data for the dashboard. When `in_trade=True`, the inner guard sets `reject_reason="in_trade"` and `_ready_to_fire=False` — no entry fires, but `_v11_live` is updated.
Scanner fires `_v11_execute_paper_entry` when MOMENTUM + OPP DECAY both pass and no cooldowns active.
**Expiry** is determined by the broker (Kite instrument list) at startup — never calculate it manually.

---

## Audit tools (standalone, no Claude dependency)

### watch_trade.py
```bash
python3 watch_trade.py          # foreground
nohup python3 watch_trade.py &  # background
```
Polls every 2s (in trade) / 10s (idle). Cross-checks:
- `state/vrl_v11_state.json` (V11 engine state) vs `state/vrl_dashboard.json` (9 fields)
- V11 SL tier formula (peak < 9 / ≥ 9 / ≥ 11 / ≥ 18)
- Telegram log: entry alert, SL upgrade alert, exit alert
Mismatches appended to `~/lab_data/trade_audit_notes.md`.

Also watches the SMI FOCUS35 stock F&O paper engine every 15m, matching its cron (repointed 06-18 from the removed frozen engine):
- `screener/smi_focus35_state.json` open trades — SL formula (stock entry ∓1%), trail armed at +1.5% peak, matching OPEN row in `fno_tracker_focus.csv` (structure=SMI_FOCUS)
- Stale-state alarm if state file >22 min old during 09:47–15:31 (dead cron detector)
- Exit reconciliation vs `screener/smi_focus35_log.csv` (pnl_rs math, exit reason ∈ SL-HIT/TRAIL-SMA8/EOD-CLOSE/EOD-LATE, tracker status)

### trace_trade.py
Post-trade reconciler. Reads state + dashboard + CSV and flags:
- SL tier vs peak_pnl formula
- CSV pnl_pts vs exit_price − entry_price
- entry_mode must be `V11_CE` or `V11_PE`

---

## Bug history — why safeguards exist

- **BUG-01**: Exits must run every ~1s tick. `_v11_check_exit()` runs unconditionally before the candle gate.
- **BUG-07**: Duplicate trades from thread race — entry and exit each hold `_v11_lock` for the full check-and-act. Entry returns early if `in_trade`.
- **BUG-10/11**: All restored state keys present in initial `_v11_state` dict; TG force-exit reads token/entry under `_v11_lock`.

### Locked design decisions
- **Re-entry disabled**: every exit sets `_reentry_armed = False`; fresh setup only.
- **No strike/streak re-entry blockers (2026-06-11)**: the exhausted-loss strike block was tried and removed same day — live counterfactual showed it kills recovery winners. 15+ broader variants (time/streak/daily-cap) all reduced net P&L. Big winners are themselves re-entries after clean SLs.
- **Single-lot execution (2026-06-10)**: 1 lot, market fill at candle close. Split-lot 50/50 (Lot 2 limit @ candle midpoint, 3-candle cancel) removed at user request.
- **All strategy parameters are locked** — OPP DECAY [−9,−7] dte≥2 (owner-approved 2026-06-18, tightened from [−8,−6]), initial SL cap entry−10, PROTECT @+9 entry−2, LOCK_4 @+11 entry+4, TRAIL_10 @+15 max(entry+9, peak−10) (owner-approved 2026-06-13, merged the old +18 tier), **LOCK_25 floor @ peak≥25 → SL max(entry+25, peak−10) (owner-approved 2026-06-15, target_replay.py +163 pts/92tr; keeps runners, evolved from an initial +25 hard-exit)**. Change only with explicit user confirmation (ladder values validated via sl_replay_study.py / target_replay.py).

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
