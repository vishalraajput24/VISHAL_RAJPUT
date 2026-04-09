# VISHAL RAJPUT TRADE — v13.5

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
| `VRL_ENGINE.py` | **Minimal signal logic.** 3-path entry (FAST/CONFIRMED/EMA) + divergence-aware exit chain. |
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
| `test_vrl.py` | **58 automated tests** covering entry paths, exit chain, cooldown, PNL. |

---

## Entry — 3 Paths with Divergence Gate

**Divergence gate (hard prerequisite, both CE and PE):** the opposite side must be falling (4-candle 1m move < 0). If the other side is up or flat, **no entry fires** regardless of which path qualifies.

Once the gate passes, any one of three independent paths can fire:

| Path | Timeframe | Momentum | Candles | Also Required |
|------|-----------|----------|---------|---------------|
| **FAST** | 1-minute | ≥ 14 pts | 4 | Current candle green, RSI rising, RSI ≤ 72 |
| **CONFIRMED** | 3-minute | ≥ 20 pts | 3 | 3m candle green, 3m RSI rising, 3m RSI ≤ 72 |
| **EMA** | 1-minute | — | — | EMA9-EMA21 ≥ 3, RSI ≥ 50 & rising, green, gap widening — **informational only, does not fire on its own** |

Fire logic:
- `FAST` and `CONFIRMED` both true → log as **CONFIRMED **** (strongest)
- `FAST` only → log as **FAST**
- `CONFIRMED` only → log as **CONFIRMED ***
- `EMA` only → logged as `[EMAV]` hint, **no trade**

All firing paths enter **2 LOTS**.

### Pre-Entry Guards

| Guard | Rule |
|-------|------|
| **RSI Hard Cap** | RSI > 72 = BLOCKED (blowoff territory) |
| **Divergence Gate** | Other side 4-candle move must be negative |
| **Direction Cooldown** | Same dir after big win (peak ≥ 10): 10min; after loss/small (peak < 10): 5min; opposite dir: immediate |
| **Daily Limits** | Max trades, max losses per config |
| **Market Hours** | 9:15–15:10 IST |

---

## Exit — Priority Chain

Exits are evaluated top-down on every tick. **First match wins**, no fallthrough.

| # | Reason | Trigger | Notes |
|---|--------|---------|-------|
| 1 | `EMERGENCY_SL` | `running ≤ -20` | Absolute floor; protects from gap moves |
| 2 | `STALE_ENTRY` | `candles ≥ 5` AND `peak < 3` | Signal never materialized, cut it |
| 3 | `RSI_BLOWOFF` | `RSI > 80` | Top-ticking, exit the party |
| 4 | `DIVERGENCE_EXIT` | Other side reversed (2 green + RSI rising) AND `peak ≥ 6` | Market regime flipped, take profits |
| 5 | `SPIKE_ABSORBED` | Candle low touched -12 but close recovered AND other still falling | **HOLD** — absorb the wick, don't exit |
| 5b | `WEAK_SL` | Same wick, but other side **not** falling | No support from other side, exit |
| 6 | `CANDLE_SL` | `running ≤ -12` (close-based, not wick) | Real drop, not a spike |
| 7 | `TRAIL_FLOOR` | Profit floor trails peak | See below |

### Profit Floors (dynamic trail)

Once `peak ≥ trail_activate`, the floor trails as a fraction of peak:

- Normal: `floor = entry + peak × keep_normal`
- Warning (other side showing strength): `floor = entry + peak × keep_warning`
- `floor` is clamped to a minimum lock; it only moves up, never down.
- Exit fires when `option_ltp ≤ floor`.

All tunables live in `config.yaml` under `exit:` / `trail:`.

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
mode: paper          # paper | live

cooldown:
  after_win: 10      # minutes: same-dir cooldown after peak >= 10pts
  after_loss: 5      # minutes: same-dir cooldown after peak < 10pts

entry:
  ema_gap_min: 3            # EMA info path
  rsi_min: 50
  rsi_max: 72               # hard cap, all paths
  fast_momentum_pts: 14     # FAST path threshold
  fast_momentum_candles: 4
  confirmed_momentum_pts: 20    # CONFIRMED path threshold
  confirmed_momentum_candles: 3

exit:
  max_sl: 20         # EMERGENCY_SL
  candle_sl: 12      # CANDLE_SL (close-based)
  stale_candles: 5
  stale_peak: 3
  rsi_blowoff: 80
  spike_recovery: 3  # SPIKE_ABSORBED tolerance

trail:
  activate: 10       # peak points to start trailing
  keep_normal: 0.6
  keep_warning: 0.75
  min_lock: 2
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
~/kite_env/bin/python3 test_vrl.py          # expect 58/58

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

`test_vrl.py` — **58 tests**:
- Strike selection (DTE 0/1+, tolerance zones)
- Version check
- Entry paths: FAST fires, CONFIRMED fires, EMA info-only
- Divergence gate: blocked when other side up
- Entry SL calculation
- Exit chain priority: EMERGENCY_SL, STALE_ENTRY, RSI_BLOWOFF, DIVERGENCE_EXIT, SPIKE_ABSORBED hold, WEAK_SL, CANDLE_SL
- Dynamic trail floor
- **Cooldown: same direction blocked after big win (10min)**
- **Cooldown: opposite direction allowed immediately**
- **Cooldown: same direction blocked after loss (5min)**
- **Cooldown: same direction allowed after cooldown expires**
- **RSI hard cap: RSI 73 blocked, RSI 71 allowed**
- **PNL: split lot correctness (saved_entry_price survives reset)**
