# VRL Bot — Session Handover

## What This Is
NIFTY options paper trading bot (Zerodha Kite). Runs as systemd service `vrl-main`.
- **Service**: `sudo /bin/systemctl restart vrl-main`
- **Logs**: `~/logs/live/vrl_live.log`
- **Trade CSV**: `~/lab_data/vrl_trade_log.csv`
- **State**: `~/state/vrl_live_state.json`
- **Code**: `~/VISHAL_RAJPUT/` (main branch, PR required to merge)

---

## Active Strategies

### V9 (LIVE paper trading — 3-min candles)
The only strategy that actually places paper trades. Gates in `VRL_ENGINE.py → check_entry_v8()`:
| Gate | Check |
|------|-------|
| G1 | Candle green (close > open) |
| G2 | Close > EMA9_low |
| G2B | EMA9_low slope ≥ 0 for last 2 candles |
| G3 | BW 13–17 (band width sweet spot) |
| G4 | Other side close ≤ band_mid (divergence) |
| G5 | RSI 50–65 and rising |

Exit ladder: peak<12 → ESL(-12) | peak≥12 → lock+4 | peak≥18 → lock+10 | peak≥24 → lock+12 ...

### V7 (SHADOW only — 15-min candles)
`V7_SHADOW_MODE = True` in VRL_MAIN.py — signals logged, no trades, no Telegram.
**User confirmed V7 is not worth pursuing.** Leave in shadow forever or remove later.

### Shadow DTF (1-min + 3-min aligned — SHADOW only, data collection)
**New strategy being tested.** No real trades — Telegram alerts only, marked "SHADOW MODE".

**Logic (current — simplified for data collection):**
1. **PRIMARY trigger**: Last completed 1-min candle close > EMA9_high + RSI rising (45–75)
2. **FILTER**: Last completed 3-min candle close > EMA9_low + RSI rising (45–75) — that's it, no green/slope/BW
3. Fires DURING the 3-min candle (earlier than V9 which waits for 3-min close)
4. Tracks peak until next 1-min bucket, sends BUCKET-END result to Telegram
5. **Logs gap for both timeframes**: `gap1m = close - EMA9H`, `gap3m = close - EMA9L` — collect to correlate gap size with outcome

**State**: `_v8_shadow_dt["CE"]` and `_v8_shadow_dt["PE"]` — tracked independently per direction.

**Telegram messages:**
- Signal fires: `🔵 SHADOW DTF SIGNAL — CE/PE STRIKE`
- Bucket ends: `🔵 SHADOW DTF — BUCKET END` with peak pts and ✅/⚠️/❌

---

## What Was Done This Session (2026-05-19 — continued)

### 1. Fixed 15-min monitor crontab (broken path)
**Problem**: Cron was running `python3 vrl_monitor.py` from home dir but file is in `VISHAL_RAJPUT/`.
**Fix**: Updated crontab to use full path `/home/vishalraajput24/VISHAL_RAJPUT/vrl_monitor.py`.

### 2. Fixed WebSocket showing "Unknown" in monitor
**Problem**: `get_ws_status()` only read last 100 lines — WS connects once at startup, far earlier.
**Fix**: Now scans entire log in reverse for last `[WS]` event. Also treats `Subscribed` as Connected.
**File**: `~/VISHAL_RAJPUT/vrl_monitor.py`

### 3. Added Telegram BUCKET-END message for Shadow DTF
Previously only signal was sent to Telegram; bucket-end (result) was only logged.
Now both signal and result sent. Peak icons: ✅ ≥12 | ⚠️ 4–12 | ❌ <4.
**File**: `VRL_MAIN.py`

### 4. Fixed Shadow DTF only tracking one direction (break bug)
**Problem**: Two `break` statements in the CE/PE loop — once CE fired, PE was never evaluated.
**Fix**: Moved to per-direction state dicts `_v8_shadow_dt["CE"]` and `_v8_shadow_dt["PE"]`.
Each direction now tracked independently with own bucket_ts, entry, peak, live_entry.

### 5. Fixed Shadow DTF firing late (architectural fix)
**Problem**: Old logic waited for 3-min candle close THEN checked 1-min tick. Always fired after V9 or minutes late.
**Root cause**: Checking `_sh_ltp > ema9h_1m` (live tick) after 3-min close = stale, not early.
**Fix**: Flipped logic — 1-min completed candle close is now the PRIMARY trigger.
3-min completed candle is now just the FILTER/alignment check.
**Result**: Shadow fires DURING the 3-min candle, before V9 (which waits for 3-min close).

### 7. Added rejection logging to Shadow DTF
Both 1-min and 3-min rejections now logged with specific reason + values.
1-min throttled to every 15s to avoid spam. 3-min logged every time 1-min passes.
Example: `[SHADOW-DTF] REJECT CE 1m_ok but 3m_close_below_ema9l close=47.0 ema9l=52.3`

### 8. Simplified Shadow DTF — removed BW, green-candle, slope checks
**User decision**: Only collect gap data, don't filter on BW or candle direction yet.
- 1-min: close > EMA9_high + RSI rising (45–75) — removed green-candle check
- 3-min: close > EMA9_low + RSI rising (45–75) — removed green, slope, BW checks
- Gap logged on every signal: `gap1m = close - EMA9H`, `gap3m = close - EMA9L`
- Goal: accumulate gap values across signals, then correlate gap size with peak outcome

### 6. Permissions set to bypassPermissions
`~/.claude/settings.local.json` → `"defaultMode": "bypassPermissions"`.
Takes effect on next session restart (not mid-session).

---

## Today's Trading Summary (2026-05-19 — Expiry Day)
| # | Time | Dir | Entry | PnL | Reason |
|---|------|-----|-------|-----|--------|
| 1 | 09:43 | CE | 64.7 | -12 | ESL |
| 2 | 10:11 | CE | 59.85 | -12 | ESL |
| 3 | 10:27 | CE | 57.15 | -12 | ESL |
| 4 | 10:57 | CE | 57.35 | +4 | Trail |
| 5 | 11:00 | CE | 60.5 | -12 | ESL |
| 6 | 11:14 | **PE** | 66.6 | **+12** | Trail ✅ |
Net: **-32 pts** (expiry day sideways chop)

---

## Key Data Findings (from trade CSV analysis, 557 trades)

### ESL Rate by Hour
| Hour | ESL | Win | ESL Rate |
|------|-----|-----|----------|
| 9xx | 28 | 21 | **57%** ← worst |
| 10xx | 23 | 38 | 37% |
| 13xx | 22 | 51 | **30%** ← best |

### Consecutive ESL Runs
- Max ever: **15 consecutive ESLs**
- 35 runs of 2+ in history
- Pattern: once 2 ESLs hit in a row, very likely choppy/sideways market

### Pending decisions (user said collect data first, don't implement yet)
- 9:15–9:45 blackout (57% ESL rate in 9am hour)
- Pause after 2 consecutive ESLs (30 min)
- Both-sides cooldown currently 1 min

---

## Shadow DTF Results So Far (only 2026-05-19, 3 signals)
| Time | Dir | Peak | Notes |
|------|-----|------|-------|
| 09:15 | CE | +10.1 ✅ | bw=12.02 (below V9's 13 min) |
| 09:27 | CE | +3.8 ⚠️ | small move |
| 10:10 | CE | +17.6 ✅ | V9 entered 1min later → ESL -12 |
PE signal at 11:22 was too late (move was over). Fixed by today's architectural change.

---

## Important Files
| File | Purpose |
|------|---------|
| `VRL_MAIN.py` | Main loop, V9 entry/exit, Shadow DTF, Telegram |
| `VRL_ENGINE.py` | Gate logic `check_entry_v8()` |
| `VRL_DATA.py` | WebSocket, indicators, strike resolution |
| `VRL_CONFIG.py` | Runtime config (lots, thresholds) |
| `vrl_monitor.py` | 15-min Telegram status (cron `*/15 9-15 * * 1-5`) |
| `CLAUDE.md` | Full bug history, design decisions, architecture |

## Rules
- `main` branch is protected — PRs required, no direct push
- After any merge: `git checkout main && git pull && sudo /bin/systemctl restart vrl-main`
- Don't implement ESL circuit breaker or 9am blackout yet — user wants more data first
- V7 is dead — don't touch or re-enable
- Shadow DTF = data collection only, never enable live trading without user decision
