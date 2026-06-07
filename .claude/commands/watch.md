---
description: Watch the live trade and audit Telegram/dashboard/state alignment — note bugs, never fix
---

You are now in **live trade watch mode**. Your job is to monitor the running bot and verify that
the same trade reads identically across all three surfaces. **NOTE bugs only — never edit code,
never restart the service, never touch state files.** A separate bot fixes them.

## Data sources (read-only)
- **Source of truth (engine state)**: `state/vrl_v8_state.json`
  Key fields: `in_trade`, `symbol`, `strike`, `direction`, `entry_price`, `peak_pnl`,
  `active_ratchet_tier`, `active_ratchet_sl`, `candles_held`, `_trades_today`, `_pnl_today_pts`.
- **Dashboard JSON** (what `_WEB_HTML` renders on :8080): `state/vrl_dashboard.json`
  Use the `position`, `ce`, `pe`, `today` blocks.
- **Telegram (sent messages)**: `tail -n 60 ~/logs/live/vrl_live.log | grep '\[TG\] sent'`
- **Closed trades (final reconciliation)**: last row of `~/lab_data/vrl_trade_log.csv`.

## Loop behaviour
1. Read `in_trade` from `vrl_v8_state.json`.
2. **If `in_trade` is false** → no open position. Report "no live trade; waiting", then schedule a
   wake-up in ~60s (ScheduleWakeup) passing `/watch` back as the prompt. Keep waiting until it flips true.
3. **If `in_trade` is true** → run the **Alignment Audit** (below). Then schedule a wake-up in ~30–45s
   and re-run. Keep auditing every cycle until `in_trade` flips back to false.
4. **On exit** (`in_trade` true → false): do a final reconciliation of the new `vrl_trade_log.csv` row
   against the last dashboard/TG exit values. Then stop the loop (omit ScheduleWakeup) and print a summary.

Pacing: use ~30–45s while in a trade (cache stays warm), ~60s while waiting. Stop scheduling once the
trade is closed and reconciled, or when the user says stop.

## Alignment Audit (every cycle while in_trade)
Compare the OPEN position across the three surfaces and emit a table:

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

## Bug logging (append-only, no fixes)
For every mismatch or anomaly, append a dated entry to `~/lab_data/trade_audit_notes.md`:
```
## <ISO timestamp> — <symbol> <strike> <dir>
- BUG: <field> — state=<x> dashboard=<y> telegram=<z>  (expected <which is right + why>)
- Repro: which file/field, which cycle
```
Create the file with a header if it doesn't exist. Never delete prior entries.

## Hard rules
- Read-only. No Edit/Write to code, config, or state JSON. No `systemctl`. No git.
- The only file you may write is `~/lab_data/trade_audit_notes.md` (append).
- If `vrl-main.service` is not active or the state file is stale (>120s old `ts`), note it as an
  observation and keep watching — do not act.
