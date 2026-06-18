# V12 "Vishal Rajput" — NIFTY 5-min Option Strategy (Step-by-Step Full Logic)

> **Status:** PAPER forward-validation only (owner-approved 2026-06-16). **NOT live.**
> Re-judge alongside the ~30-Jun FINAL PACKAGE review.
> **Engine file:** `v12_vishal.py` · **Indicator/signal lib:** `screener/orion_v2514_backtest.py`
> **Last synced:** 2026-06-18

This is the canonical write-up of the V12 engine that the bot runs as a *paper* process
every day. It signals off the **NIFTY near-month FUTURE** (5-min candles) and "trades" a
single **ATM NIFTY weekly OPTION** on paper (P&L in premium points). It never places a
broker order and never touches the live V11 state.

⚠️ V12 is **not a proven edge** — the in-sample +136 opt-pts headline failed its
chronological OOS split (entire edge in the recent half, 70% of net from 3 trades). It is
being forward-paper-tested precisely because the *exit shape* is robust but the headline is
not OOS-stable. See `screener/v12_final_report.md`.

---

## 0. The one-paragraph summary

> On every closed 5-min NIFTY-future bar (09:30–14:45), look for an **SMI cross out of an
> extreme** (the **E2** trigger). If found, run a **FLOW veto** — kill the signal if the move
> is "hollow" (no volume/effort behind it, or a price extreme not confirmed by the
> Accumulation/Distribution line). If it survives, buy **1 ATM weekly option** (CE on a bull
> cross, PE on a bear cross). Manage it purely on **premium points**: hard SL at entry−22;
> once it's up +8, arm a trailing stop at peak−6; force-close at 15:25. One position at a
> time; stop entering for the day after 5 losing trades.

---

## 1. Architecture / data flow

```
NIFTY near-month FUTURE (5m candles, Kite historical_data)
        │
        ▼
 add_indicators()  →  SMI(period 30), VWAP, ATR, vol_ma(20), SMA8/20/50, RSI, MACD
        │              + add_flow_features()  →  volx, approach_volx, close_pos, A/D line, 20-bar extremes
        ▼
 gen_signals(df, CFG)  →  E2 SMI-cross signals only  (E1 off, E3 removed, E4 off)
        │
        ▼
 flow_veto()  →  drop "hollow" triggers (L1 effort OR L2 A/D-divergence)
        │
        ▼
 resolve ATM strike from FUTURE spot  →  get_option_tokens()  →  ATM weekly CE/PE
        │
        ▼
 PAPER option position, P&L in premium points, managed SL-22 / arm+8 / trail peak-6
```

- **Signal source:** NIFTY future (the future is *only* the signal + ATM reference; the
  futures trade leg was **removed 2026-06-17** — this is an **option-only** engine).
- **Traded instrument:** 1 ATM NIFTY **weekly** option, paper fill at last 1-min premium.
- **Infra reused:** `VRL_MAIN` for strike/expiry/option-1min data; `orion_v2514_backtest`
  for the validated indicators + `gen_signals`.

Constants (`v12_vishal.py` L46–57):

| Name | Value | Meaning |
|------|-------|---------|
| `ENTRY_START / ENTRY_END` | 09:30 / 14:45 | entry window |
| `FORCE_CLOSE` | 15:25 | hard EOD close |
| `POLL_SEC` | 30 | loop cadence |
| `MAX_LOSSES` | 5 | losing trades/day → stop new entries |
| `STRIKE_STEP` | 100 | 100-pt strikes (50-step too illiquid) |
| `SMI_PERIOD` | 30 | 5m-tuned %K period (overrides the 15m default of 10) |
| `VOL_WIN` | 20 | volume MA window for the flow gate |
| `E2_OB / E2_OS` | 35 / −35 | SMI overbought / oversold bands |
| `OPT_SL / OPT_ARM / OPT_GAP` | 22 / 8 / 6 | premium SL / arm threshold / trail gap |

---

## 2. The indicators (math)

All in `orion_v2514_backtest.py`.

### SMI — Stochastic Momentum Index (the trigger engine)
`smi(df, k=30, d=3, sig=3)` (L88):
```
hh = rolling_max(high, k);  ll = rolling_min(low, k)
rel = close − (hh+ll)/2                # distance from the midpoint of the k-bar range
rng = hh − ll                          # the k-bar range
er  = EMA(EMA(rel, d), d)              # double-smoothed numerator
eg  = EMA(EMA(rng, d), d)              # double-smoothed range
SMI      = 100 * er / (eg/2)           # bounded ~[-100, +100]
SMI_sig  = EMA(SMI, sig)               # signal line
```
SMI > 0 = price in the upper half of its k-bar range (bullish momentum); < 0 = lower half.
V12 uses **k=30** (5m-tuned) instead of the library default 10, and bands **±35** instead of
the 15m default 63/−37.

### VWAP, ATR, A/D — supporting
- **VWAP** (L108): daily cumulative `Σ(typical_price·vol)/Σvol`. Used only as an **E1
  confluence vote** (E1 never opens a trade in V12).
- **ATR(14)** (L97): Wilder ATR, logged as `atr_entry` for context only.
- **A/D line** (`add_flow_features`, L141): intraday Accumulation/Distribution —
  `Σ( MFM · volume )` where `MFM = ((close−low) − (high−close))/(high−low)`. Resets daily.
  Used by the L2 flow veto.

---

## 3. ENTRY — step by step

Runs **once per newly-closed 5-min bar** (`bar_ts != last_bar_ts`) inside the main loop
(`v12_vishal.py` L276–337).

**Step 1 — Gate the bar.** Skip unless `09:30 ≤ now ≤ 14:45`, not already `in_trade`, and
`losses < 5`. Require ≥51 closed bars loaded (SMI/MA warmup).

**Step 2 — Generate signals.** `gen_signals(df, CFG)` with `E1=False, E2=True (ob=35,
os=−35, confirm="none"), E3=False, E4=False`. Keep only signals whose bar index == the bar
that just closed.

The **E2 cross** rule (`orion_v2514_backtest.py` L220–226):
```
cross_up (→ CE):  prev SMI ≤ −35  AND  SMI > −35  AND  SMI > SMI_sig
cross_dn (→ PE):  prev SMI ≥ +35  AND  SMI < +35  AND  SMI < SMI_sig
```
i.e. SMI is **crossing back out of an extreme** in the trade's direction, *and* sits on the
correct side of its signal line. `confirm="none"` → no 15m/1h regime filter on V12.

**Step 3 — Pick direction & confluence.** If multiple engines fired (only E2 is live now,
but the code is general), pick the direction with the most engine votes; `conf = #engines
agreeing`. `e1_agree` = is price on the trade's side of VWAP? (CE: close>vwap). These are
**logged for analysis / conviction only — they do not gate the entry.**

**Step 4 — FLOW VETO** (`flow_veto`, L159). The signal is **dropped** if the move is hollow:
- **L1 — effort-vs-result:**
  - `quiet` = this bar's `volx` (= volume/vol_ma) ≤ 45th-pct **AND** the 5-bar pre-approach
    volume `approach_volx` ≤ 50th-pct → the breakout had no volume behind it.
  - `rej` = rejection wick against the trade: CE with `close_pos ≤ 0.40` (closed in the
    bottom 40% of the bar range), or PE with `close_pos ≥ 0.60`.
  - `L1 = quiet OR rej`.
- **L2 — A/D divergence:** CE makes a new 20-bar **price** high but the **A/D line** does NOT
  make a new 20-bar high (distribution into the high) → veto. Symmetric for PE.
- `veto = L1 OR L2`. Thresholds are **self-calibrating percentiles** of the loaded window —
  no fixed ADX/levels. On veto: Telegram `⚪ FLOW-SKIP`, no trade.

> Why: the flow study (`screener/v12_flow_divergence_study.py`) found chop-day losers were
> E2 triggers that faded into no-effort balance edges. E2 + this veto = exp **+15.7** vs
> **+1.29** baseline (in-sample), skipping all 8 of 06-17's losers.

**Step 5 — Resolve the ATM option.** From the live future spot `fut_now`, 100-pt strike:
- **CE floors** to the 100 below spot (`(spot//100)*100` → strike ≤ spot → slightly ITM call)
- **PE ceils** to the 100 above spot (`ceil` → strike ≥ spot → slightly ITM put)

`get_option_tokens(kite, strike, expiry)` → the weekly CE/PE token+symbol. Read the latest
1-min premium (`get_option_1min`). Abort if no token / premium ≤ 0.

**Step 6 — Open the paper position.** Set state: `entry_prem = prem`, `prem_sl = prem − 22`,
`peak_prem=0`, `armed_prem=False`. Save state, fire `🟢 V12 VR ENTRY` Telegram.

---

## 4. EXIT — step by step (premium-point ladder)

Checked **every poll (~30s), priority over entry** (`v12_vishal.py` L240–267):

```
fav = current_prem − entry_prem               # favourable excursion
peak_prem = max(peak_prem, fav)               # best seen
mae_prem  = min(mae_prem, fav)                 # worst seen (for analysis)

if not armed and peak_prem ≥ +8:              # ARM
    armed = True
if armed:
    prem_sl = max(prem_sl, entry_prem + peak_prem − 6)   # trail peak−6 (ratchets up only)

EXIT if:
  prem ≤ prem_sl  →  reason "SL"    (if never armed)  /  "TRAIL"  (if armed)
  now ≥ 15:25     →  reason "FORCE_CLOSE"
```

- **Initial hard SL:** `entry − 22` premium points (wide, defined-risk; ~12% of a typical
  move). Non-binding by design — the study found wide SL + early arm + tight trail is the
  robust exit *shape* (param plateau 100% net-positive).
- **Arm at +8:** once the option is up 8 points, flip from "hard SL" to "protect profit".
- **Trail peak−6:** the stop ratchets to 6 points below the best premium seen — locks gains
  on a runner, never loosens.
- **Force close 15:25** regardless.

On exit: increment `trades` (and `losses` if pnl<0), append a row to
`~/lab_data/v12_vishal_log.csv`, fire `🔴 V12 VR OPT EXIT`, reset the per-trade fields
(keeping day counters).

Exit reasons: `SL` · `TRAIL` · `FORCE_CLOSE`.

---

## 5. TODAY'S EXAMPLE — 2026-06-18 (the real paper trade)

Pulled live from `~/lab_data/v12_vishal_log.csv`:

```
date       engine dir strike symbol            conf e1  entry     exit      leg entry exit  pnl  reason peak  mae  atr armed arm_time  hold
2026-06-18 E2     CE  24000  NIFTY2662324000CE 1    No  12:05:00  12:31:13  OPT 165.6 183.3 +17.8 TRAIL 26.9 -2.6 21.0 True  12:25:11  26.2m
```

Walking it through the logic above:

1. **Trigger (Step 2):** at the 12:05 5-min bar close, SMI crossed **up out of −35** with
   `SMI > SMI_sig` → an **E2 CE** signal. `conf=1` (only E2), `e1_agree=False` (future was
   not above VWAP — a confluence *miss*, but V12 does not gate on it).
2. **Flow veto (Step 4):** the move had enough effort / no A-D divergence → **not vetoed**,
   so the entry proceeded.
3. **Strike (Step 5):** future spot ≈ 24000-ish → CE floors to **24000** → bought the
   `NIFTY2662324000CE` weekly at premium **165.6**. Initial SL = 165.6 − 22 = **143.6**.
4. **Manage (Step 4 exits):** premium rose; `peak_prem` reached **+26.9** (prem ≈ 192.5).
   - It armed at **12:25:11** when peak first crossed **+8**.
   - The trail sat at `peak − 6` = 26.9 − 6 ≈ **+20.9** above entry at the high.
   - Premium pulled back and tagged the trailing stop at **183.3** → exit **12:31:13**,
     reason **TRAIL**, **+17.8** premium points. MAE was only −2.6 (never threatened the SL).
5. **Day:** that was the only trade → `opt_day = +17.75`, `trades=1`, `losses=0`.

So today V12 captured **+17.8 option points** on a single E2 CE that armed and trailed out —
a textbook "arm at +8, give back 6 from the peak" outcome (peak +26.9 → kept +17.8).

---

## 6. Operational facts

| Thing | Value |
|-------|-------|
| Run | cron ~09:25 Mon–Fri; self-exits after 15:30 (`v12_vishal.py` L225) |
| State | `state/v12_vishal_state.json` |
| Trade log | `~/lab_data/v12_vishal_log.csv` |
| Config | `screener/orion_v2514_best_cfg.json` (E1/E3/E4 forced off in `load_cfg`) |
| Telegram brand | "V12 Vishal Rajput" |
| Mode | **PAPER ONLY** — no broker order, never touches V11 live state |

## 7. What is LOCKED vs still being studied

- **Locked spec (do not change without owner):** E2-only trigger · flow veto (L1 effort + L2
  A/D-divergence) · SMI period 30 · bands ±35 · option-only · SL−22 / arm+8 / trail−6 ·
  window 09:30–14:45 · 100-pt ATM strikes (CE floor / PE ceil) · max 5 losses/day.
- **Removed:** E1 (poison on options — 9% win in H1) opens nothing; **E3** removed 2026-06-17
  (exp −1.03, made 6 of 8 of that day's losers); E4 off; the **futures trade leg** removed
  2026-06-17 (option-focus).
- **Open question (why it's paper):** the +136 in-sample headline is not OOS-stable. The
  exit *shape* is robust (param plateau), the *level* is not. Collecting ≥2 weeks of
  out-of-sample fills before any go/no-go vs live V11.

---

### Source map (read these for ground truth)
- `v12_vishal.py` — the engine (entry/exit/flow loop)
- `screener/orion_v2514_backtest.py` — `smi`, `add_indicators`, `gen_signals` (E2 cross), confirm/regime
- `screener/v12_final_report.md` — the OOS stress-test & verdict
- `screener/v12_flow_divergence_study.py` — derivation of the L1/L2 flow veto
