---
description: Watch the LIVE trade end-to-end — audit Telegram/dashboard/state alignment + real m.Stock execution, track every bug, fix safely once flat
---

You are now in **live trade watch mode**. The bot is running with **real money** (`config.yaml`
→ `mode: live` since 2026-06-15) — the V11 engine places real m.Stock LIMIT entries / MARKET
exits. Your job: monitor the running bot, verify the same trade reads identically across all
surfaces AND that the real-order execution path behaves correctly, **track every bug**, and
**fix confirmed bugs — but only safely** (see Fix policy). Get full end-to-end tracking of each
live trade so nothing slips.

## Data sources (read-only)
- **Source of truth (engine state)**: `state/vrl_v11_state.json`
  Key fields: `in_trade`, `symbol`, `strike`, `direction`, `entry_price`, `qty`, `peak_pnl`,
  `peak_ltp`, `initial_sl`, `active_ratchet_tier`, `active_ratchet_sl`, `candles_held`,
  `_trades_today`, `_pnl_today_pts`, `pdh_prev`, `pdl_prev`, `entry_range_pos`.
- **Dashboard JSON** (what `_WEB_HTML` renders on :8080): `state/vrl_dashboard.json`
  Use the `position`, `ce`, `pe`, `today`, `market`, `account` blocks.
- **Telegram (sent messages)**: `tail -n 80 ~/logs/live/vrl_live.log | grep '\[TG\] sent'`
- **Live broker log (m.Stock orders)**: `grep -E '\[MSTOCK\]|\[TRADE\]|\[V11\] LIVE' ~/logs/live/vrl_live.log | tail -40`
- **Closed trades (final reconciliation)**: last row of `~/lab_data/vrl_trade_log.csv`.

## Loop behaviour
1. First, confirm we are actually live: `grep 'Mode:' ~/logs/live/vrl_live.log | tail -1` should say
   `Mode: LIVE`. If it says PAPER, note it (we expected LIVE) and keep watching anyway.
2. Read `in_trade` from `vrl_v11_state.json`.
3. **If `in_trade` is false** → no open position. Report "no live trade; waiting" + the PDH/PDL
   context line + whether we're inside the 09:45 entry blackout, then schedule a wake-up in ~60s
   (ScheduleWakeup) passing `/watch` back as the prompt. Keep waiting until it flips true.
4. **If `in_trade` is true** → run the **Alignment Audit** + **Live execution audit** (below).
   Then schedule a wake-up in ~30–45s and re-run. Keep auditing every cycle until `in_trade`
   flips back to false.
5. **On exit** (`in_trade` true → false): do a final reconciliation of the new `vrl_trade_log.csv`
   row against the last dashboard/TG exit values AND the m.Stock `[MSTOCK] EXIT FILLED` line.
   Then stop the loop (omit ScheduleWakeup) and print a summary + any bugs found.

Pacing: ~30–45s while in a trade (cache stays warm), ~60s while waiting. Stop scheduling once the
trade is closed and reconciled, or when the user says stop.

## Alignment Audit (every cycle while in_trade)
Compare the OPEN position across the surfaces and emit a table:

| Field | state.json | dashboard.json | telegram | MATCH? |
|-------|-----------|----------------|----------|--------|
| symbol / strike / direction | | | | |
| entry_price | | | | |
| current LTP | | | | |
| peak_pnl | | | | |
| trail-SL tier + SL price | | | | |
| P&L (pts) | | | | |
| P&L (₹) | | | | |
| status / candles_held | | | | |

Flag a mismatch when values disagree beyond rounding (>0.05 pt or any categorical difference).

### V11 trail-SL tier formula (verify `active_ratchet_tier` + `active_ratchet_sl` match `peak_pnl`)
- `peak_pnl < 9`  → **INITIAL**  : SL = `initial_sl` (ema9_low, capped at `entry − 10`)
- `peak_pnl ≥ 9`  → **PROTECT**  : SL = `max(initial_sl, entry − 2)`
- `peak_pnl ≥ 11` → **LOCK_4**   : SL = `max(initial_sl, entry + 4)`
- `peak_pnl ≥ 15` → **TRAIL_10** : SL = `max(initial_sl, entry + 9, peak_ltp − 10)`
Flag if the tier/SL the engine holds doesn't match what the formula says for the current `peak_pnl`.

## Live execution audit (every cycle while in_trade, + on entry/exit events)
We are placing REAL orders — verify the live path, not just simulated fills:

**Entry (real LIMIT via `ms_place_buy`):**
- TG entry alert must read **`Entry (Lmt)`** (not `Mkt`) in live mode.
- `state.entry_price` must equal the **broker fill price** from `[MSTOCK] ENTRY FILLED: price=...`
  — NOT the 1-min candle close. Flag any divergence (that's the whole point of the live wiring).
- Note the real `slippage=...pts` from the same log line (CSV still logs 0 — known follow-up, not a bug).
- **Entry MISS**: if you see `[V11] LIVE entry not filled` / TG `⚠️ entry MISSED` (LIMIT cancelled
  after 8s, rejection), verify `in_trade` did NOT flip true and the candle was stamped
  (`_last_fired_candle_ts`) so the scanner didn't re-fire the same bar. This is expected behaviour
  on fast moves — record it, don't flag as a bug unless in_trade went true anyway.
- **Partial fill**: `[TRADE] Partial fill REJECTED` must result in NO position. Flag if in_trade is true.

**Exit (real MARKET via `ms_place_sell`):**
- `[MSTOCK] EXIT FILLED: price=...` — the CSV `exit_price` should match this broker fill.
- Exit retries: `[TRADE] Exit attempt N failed — retry` are normal recovery; note them.
- 🚨 **CRITICAL — `[V11] LIVE EXIT FAILED — position STILL OPEN`** + TG `🚨 MANUAL ACTION`: this means
  a real position is open at the broker with no local close. Verify `in_trade` STAYED true and NO
  CSV row was written (the design keeps it open so the ladder/EOD retries). **Surface this LOUDLY
  at the top of your report every cycle until it resolves** — this is the one event that needs a human.

## PDH/PDL context (every cycle while in_trade, and in the waiting report)
Always report where spot sits relative to the previous day's range — owner wants this visible on
every watch cycle (chop/dead-zone awareness):
- Read `pdh_prev`, `pdl_prev`, `entry_range_pos` from `state/vrl_v11_state.json` (logged at entry).
  If missing/zero, compute from the newest `~/lab_data/spot/nifty_spot_1min_<YYYYMMDD>.csv` BEFORE
  today: PDH = max(high), PDL = min(low).
- Also compute the LIVE position using current spot (dashboard `market.spot` or spot CSV last row):
  `range_pos = (spot − PDL) / (PDH − PDL)` → 0 = at PDL, 1 = at PDH.
- Report one line, e.g.: `PDH/PDL: 23459.7 / 23151.5 — entry at 0.42 (inside MID), now 0.51`
  Label zones: `> 1` ABOVE PDH (breakout zone) · `0.67–1` upper third · `0.33–0.67` inside MID
  (dead zone — historically worst for PE entries) · `< 0.33` lower third · `< 0` BELOW PDL.
- Context only — never flag it as a mismatch; just surface it.

## Bug tracking (append-only log, every cycle)
For every mismatch or anomaly, append a dated entry to `~/lab_data/trade_audit_notes.md`:
```
## <ISO timestamp> — <symbol> <strike> <dir>  [LIVE]
- BUG: <field/behaviour> — state=<x> dashboard=<y> telegram=<z> broker=<b>  (expected <which is right + why>)
- Repro: which file/field/log line, which cycle
- Severity: CRITICAL (real-money / open position) | HIGH (wrong SL/PnL) | LOW (cosmetic)
```
Create the file with a header if it doesn't exist. Never delete prior entries.

## Fix policy — "track now, fix safely"
The owner wants bugs FIXED, not just logged. But a real position may be open, so timing matters:

- **While `in_trade` is TRUE (live position open): DO NOT edit code, config, or state, and DO NOT
  restart the service.** A restart/edit mid-trade can disrupt a real-money position. Track + log only.
- **The ONE exception** is `LIVE EXIT FAILED` (open position stuck at broker): do not edit code —
  instead surface it loudly and tell the owner to `/forceexit` or close manually at m.Stock now.
- **Once flat** (`in_trade` false, between trades or after EOD): fix confirmed bugs via the normal
  PR workflow from CLAUDE.md — `git checkout -b fix/...` → edit → `py_compile` → PR → squash-merge →
  `git checkout main && git pull`. Only `sudo systemctl restart vrl-main.service` to deploy when
  the bot is flat (no open position), and re-verify mode is still LIVE after restart.
- For a bug you can't fix safely or that needs a judgement call, log it and ask the owner before acting.

## Hard rules
- Read-only on `state/*.json` always (never hand-edit engine state).
- No `systemctl restart` while a live position is open — ever.
- All code/config fixes ship via PR (no direct main pushes), and only deploy when flat.
- If `vrl-main.service` is not active or the state file is stale (>120s old `ts`), surface it as a
  CRITICAL observation (in live mode a dead service = an unmanaged real position) and keep watching.
