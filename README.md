# VISHAL RAJPUT TRADE — v13.7

**Algorithmic Options Trading Bot for Nifty 50**

Minimal strategy options trading system. 2-lot execution with dynamic trail and divergence-aware exits. Runs on Zerodha Kite API with Telegram command interface and live web dashboard.

> **Status:** Paper Trading | **Market:** NSE Nifty 50 Options | **Expiry:** Weekly

---

## Architecture

```
┌─────────────────────────────────────────────────┐
│          CRONTAB (Mon-Fri): AUTH @ 8AM           │
│      Everything else runs under systemd          │
└─────────────────┬───────────────────────────────┘
                  │
     ┌────────────┼────────────┐
     │            │            │
┌────▼────┐ ┌────▼────┐ ┌─────▼─────┐
│VRL_AUTH │ │VRL_MAIN │ │  VRL_WEB  │
│(cron)   │ │(systemd)│ │ (systemd) │
└────┬────┘ └────┬────┘ └─────┬─────┘
     │           │            │ reads
  ┌──▼──┐ ┌─────▼─────┐   ┌──▼──────────┐
  │KITE │ │VRL_ENGINE │   │ SQLite /    │
  │ API │ │ Entry/Exit│   │ dashboard   │
  └─────┘ └─────┬─────┘   └─────────────┘
          ┌─────▼─────┐
          │VRL_TRADE  │  ┌────────────┐
          │  Orders   │  │VRL_COMMANDS│
          └─────┬─────┘  │  Telegram  │
                │        └────────────┘
          ┌─────▼──────┐
          │VRL_VALIDATE│  20 post-trade alignment checks
          └────────────┘
     ┌──────────┐
     │ VRL_LAB  │ → ~/lab_data/ (1m/3m/5m/15m/60m/daily)
     │Data Coll.│    (systemd)
     └──────────┘
```

---

## File Map

| File | Purpose |
|------|---------|
| `config.yaml` | **Central config** — all tunable values. Change here, restart service, done. |
| `VRL_CONFIG.py` | Config loader + validator. Typed accessors, fails fast. |
| `VRL_MAIN.py` | Master orchestrator. Strategy loop, 2-lot execution, state, alerts, dashboard. |
| `VRL_ENGINE.py` | **Dual-TF momentum + divergence.** FAST 1m / CONFIRMED 3m entry. RSI cap 75. Static profit floors + dynamic trail exit. Entry cutoff 15:10. |
| `VRL_DATA.py` | Foundation. WebSocket, indicators, Greeks, spot analysis, data cache, IST timezone. |
| `VRL_DB.py` | **SQLite database layer.** WAL mode, dual-write with CSV. Query helpers. |
| `VRL_LAB.py` | Data collector. All timeframes, forward fill at EOD, daily summary. Writes CSV + SQLite. |
| `VRL_AUTH.py` | Kite authentication. Auto-login via TOTP. Runs from crontab at 8:00. |
| `VRL_TRADE.py` | Order execution. Paper + Live mode in one file. Only file that touches orders. |
| `VRL_VALIDATE.py` | **20 live market alignment checks.** Runs on every entry + exit, silent on PASS, alerts on FAIL. |
| `VRL_WEB.py` | War Room API server. Serves `static/VRL_DASHBOARD.html` + JSON APIs. |
| `static/VRL_DASHBOARD.html` | **Production dashboard.** Glassmorphism dark theme, RSI progress bars. |
| `VRL_COMMANDS.py` | Telegram command handlers. |
| `VRL_CHARGES.py` | Brokerage calculator (STT, exchange, GST, stamp duty). |
| `VRL_DEPLOY.py` | Telegram-triggered deployment (uses `systemctl restart vrl-main`). |
| `VRL_BUGS.md` | Bug sheet — all bugs found and fixed, prevention rules. |
| `test_vrl.py` | **70 automated tests** covering entry paths, RSI cap, profit floors, exit chain, cooldown, entry cutoff, EOD handler, phantom clear. |

---

## Entry — Dual-TF Momentum + Divergence Gate

**Divergence gate (hard prerequisite):** the opposite side must be falling (4-candle 1m move < 0). If the other side is up or flat, **no entry fires**.

| Path | Timeframe | Momentum | Candles | Also Required |
|------|-----------|----------|---------|---------------|
| **FAST** | 1-minute | ≥ 14 pts | 4 | Green candle, RSI rising, RSI ≤ 75 |
| **CONFIRMED** | 3-minute | ≥ 20 pts | 3 | 3m green, 3m RSI rising, 3m RSI ≤ 75 |

Fire logic: FAST takes priority. Both fire → **CONFIRMED ****. All entries = **2 LOTS** (130 qty).

### Pre-Entry Guards

| Guard | Rule |
|-------|------|
| **RSI Hard Cap** | RSI > 75 = BLOCKED (near-miss logged for analysis) |
| **Divergence Gate** | Other side 4-candle move must be negative |
| **Entry Cutoff** | No new entries after 15:10 IST |
| **Direction Cooldown** | 5min same direction, opposite immediate |
| **Daily Limits** | Max trades, max losses per config |
| **Market Hours** | 9:15–15:10 IST |

---

## Exit — Priority Chain

Exits are evaluated top-down on every tick. **First match wins**, no fallthrough.

| # | Reason | Trigger | Notes |
|---|--------|---------|-------|
| 1 | `EMERGENCY_SL` | `running ≤ -20` | Absolute floor |
| 2 | `STALE_ENTRY` | `candles ≥ 5` AND `peak < 3` | Signal never materialized |
| 3 | `RSI_BLOWOFF` | `RSI > 80` | Top-ticking exit |
| 4 | `DIVERGENCE_EXIT` | Other reversed + `peak ≥ 6` | Regime flipped |
| 5 | `SPIKE_ABSORBED` | Low -12 but close recovered, other falling | **HOLD** |
| 5b | `WEAK_SL` | Same wick, other **not** falling | Exit |
| 6 | `CANDLE_SL` | `running ≤ -12` (close-based) | Real drop |
| 6b | `PROFIT_FLOOR` | Static floor SL breached | See below |
| 7 | `TRAIL_FLOOR` | Dynamic trail at higher peaks | See below |
| 8 | `MARKET_CLOSE` | 15:30 IST | EOD auto-exit |

### Static Profit Floors (v13.7)

| Peak PNL | SL ratchets to | Effect |
|----------|---------------|--------|
| +5 | entry - 6 | Damage control (tighter than -12) |
| +10 | entry + 2 | Breakeven+ |
| +20 | entry + 12 | Lock profit |
| +30 | entry + 22 | Scale profit |
| +40 | entry + 32 | Compound profit |
| +50 | entry + 42 | Extended runners |

Floors persist to `state["phase1_sl"]` immediately on crossing. SL only ratchets **up**, never down.

### Dynamic Trail (higher peaks)

Once `peak ≥ trail_activate` (15 FAST / 20 CONFIRMED), the floor trails as `entry + peak × keep_pct`. Dynamic trail activates above the static floors and provides tighter protection on large runners.

---

## Strike Selection

| DTE | Step | Method |
|-----|------|--------|
| 0 | 50 | CE rounds down, PE rounds up (ITM) |
| 1+ | 100 | CE rounds down, PE rounds up (ITM) |

**Strike locking**: Strikes locked until spot moves 150+ pts. Prevents flickering.

**Premium filter**: ₹100-400 (₹50+ on DTE 0)

---

## Central Config — `config.yaml`

```yaml
mode: paper

cooldown:
  same_direction: 5       # opposite always immediate

entry:
  rsi_max: 75             # v13.7: raised from 72
  fast_momentum_pts: 14
  fast_momentum_candles: 4
  confirmed_momentum_pts: 20
  confirmed_momentum_candles: 3

exit:
  candle_close_sl: 12     # close-based SL
  max_sl: 20              # EMERGENCY_SL
  stale_candles: 5
  stale_peak_min: 3

profit_floors:            # v13.7: static, persisted to state
  - { peak: 5,  lock: -6 }
  - { peak: 10, lock: 2 }
  - { peak: 20, lock: 12 }
  - { peak: 30, lock: 22 }
  - { peak: 40, lock: 32 }
  - { peak: 50, lock: 42 }
```

Change any value, `sudo systemctl restart vrl-main`. No code changes needed.

---

## Telegram Commands

| Command | Action |
|---------|--------|
| `/status` | Trade status + P&L |
| `/pnl` | Today's P&L summary with charges |
| `/trades` | Today's trade list |
| `/spot` | Spot trend + gap |
| `/pivot` | Fib pivot levels |
| `/download` | Today's data zip (or `/download 2026-04-01`) |
| `/health` | System health check |
| `/validate` | 10 system alignment checks |
| `/pause` | Block new entries |
| `/resume` | Re-enable entries |
| `/forceexit` | Emergency exit all lots |
| `/restart` | Restart bot via systemd |
| `/token` | Manage subscriber access tokens |

---

## Dashboard

`http://SERVER_IP:8080` — Production glassmorphism dashboard (`static/VRL_DASHBOARD.html`)

**Signals Tab** — CE/PE side by side:
- EMA9, EMA21, EMA Gap (with color + icons)
- RSI (with rising indicator)
- FAST / CONFIRMED / EMA path status
- Divergence gate status (other side falling?)
- Verdict: FIRED / READY / waiting reason
- Cooldown timer when active

**Position Card** (when in trade):
- Gradient border (green = profit, red = loss)
- Big PNL with rupee value
- Entry mode badge (FAST / CONFIRMED / CONFIRMED**)
- Dynamic trail floor indicator
- Clean symbols: "CE 22500" not "NIFTY2640722500CE"

**Market Tab** — Spot data, multi-TF tables, fib pivots, zones, straddle
**Trades Tab** — Day summary + scrollable trade cards with win/loss colors

---

## Data Collection

All timeframes collected for future ML. **Dual write: CSV + SQLite.**

| Data | Frequency | SQLite Table |
|------|-----------|-------------|
| Signal scan | Every minute (CE + PE) | `signal_scans` |
| Option 1min/3min/5min/15min | Per candle close | `option_1min` / `option_3min` / etc. |
| Spot 1min/5min/15min/60min/daily | Per candle close | `spot_1min` / `spot_5min` / etc. |
| Trades | Per trade exit | `trades` |
| Forward fill | EOD at 15:35 | Updated in-place via SQL |
| Daily summary | EOD | CSV only |

**Database**: `~/lab_data/vrl_data.db` (SQLite, WAL mode)

**API Endpoints** (SQLite-powered):
| Endpoint | Example |
|----------|---------|
| `/api/db/trades?date=2026-04-02` | Today's trades |
| `/api/db/scans?date=2026-04-02&direction=CE` | CE scans for a date |
| `/api/db/spot?tf=5min&from=09:15&to=15:30` | Spot data range |
| `/api/db/stats?date=2026-04-02` | Trade stats (count, avg, wins) |

**Lab data retention**: Auto-deletes CSVs older than 30 days. SQLite persists forever.

---

## Infrastructure

All long-running processes run as **systemd services**. No `pkill`, no `nohup`, no backgrounded cron jobs.

| Service | Unit | Purpose |
|---------|------|---------|
| `vrl-main` | `vrl-main.service` | Strategy loop (VRL_MAIN.py) |
| `vrl-web` | `vrl-web.service` | Dashboard + API (VRL_WEB.py) |
| `vrl-lab` | `vrl-lab.service` | Data collector (VRL_LAB.py) |

Standard operations:

```bash
sudo systemctl status  vrl-main
sudo systemctl restart vrl-main
sudo systemctl stop    vrl-main
journalctl -u vrl-main -f       # live logs
```

Systemd handles auto-restart on crash, logs to journald, and survives reboots.

| Feature | Detail |
|---------|--------|
| **Auto Paper/Live** | `mode: live` in config → live order path in VRL_TRADE |
| **Position Reconciliation** | Startup check: saved state vs broker positions |
| **Data Cache** | 30s TTL on historical API calls |
| **Timezone** | IST (Asia/Kolkata) everywhere |
| **Central Logs** | `~/logs/` with date-wise files + error mirror |
| **Strike Locking** | Locked until spot moves 150+ pts |
| **Circuit Breaker** | 5 consecutive errors → pause + alert |
| **Validation** | VRL_VALIDATE runs 20 alignment checks after every entry/exit |

---

## Setup

```bash
# Requirements
pip install -r requirements.txt

# Environment (~/.env)
KITE_API_KEY=xxx
KITE_API_SECRET=xxx
KITE_TOTP_KEY=xxx
TG_TOKEN=xxx
TG_GROUP_ID=xxx

# Verify
cd ~/VISHAL_RAJPUT
~/kite_env/bin/python3 test_vrl.py          # expect 70/70

# Start
sudo systemctl enable --now vrl-main vrl-web vrl-lab
```

### Crontab

Only **AUTH** is scheduled via crontab (runs once a day before market open). Everything else is a systemd service.

```
PYTHONPATH=/home/vishalraajput24/VISHAL_RAJPUT
0 8 * * 1-5  /home/vishalraajput24/kite_env/bin/python3 /home/vishalraajput24/VISHAL_RAJPUT/VRL_AUTH.py
```

---

## Test Suite

`test_vrl.py` — **70 tests**:
- Strike selection (DTE 0/1+, tolerance zones)
- Version check (v13.7)
- Entry: FAST fires, CONFIRMED fires, flat blocked, low RSI blocked
- RSI hard cap: 76 blocked, 74 allowed (v13.7 cap = 75)
- Entry cutoff: 15:11 blocked
- Exit chain: EMERGENCY_SL, STALE_ENTRY, RSI_BLOWOFF, CANDLE_SL, PROFIT_FLOOR
- Static profit floors: +5→-6 (damage control), +50→+42 (extended)
- Floor persistence to state.phase1_sl (BUG-027)
- Force exit respects floor SL, not entry price
- EOD auto-exit handler, startup phantom clear
- Cooldown: same dir blocked, opposite allowed, expiry works
- PNL: split lot correctness
- Charges calculator, bonus indicators, DB operations
