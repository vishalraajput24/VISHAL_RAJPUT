# VISHAL RAJPUT TRADE — v13.0

**Algorithmic Options Trading Bot for Nifty 50**

Minimal strategy options trading system. 2-lot execution with profit floors and RSI-based lot splitting. Runs on Zerodha Kite API with Telegram command interface and live web dashboard.

> **Status:** Paper Trading | **Market:** NSE Nifty 50 Options | **Expiry:** Weekly

---

## Architecture

```
┌─────────────────────────────────────────────────┐
│               CRONTAB (Mon-Fri)                  │
│  8:00 AUTH → 9:00 WEB → 9:10 BOT → 9:18 HC     │
└─────────────────┬───────────────────────────────┘
                  │
     ┌────────────┼────────────┐
     │            │            │
┌────▼────┐ ┌────▼────┐ ┌─────▼─────┐
│VRL_AUTH │ │VRL_MAIN │ │  VRL_WEB  │
│  Token  │ │  Brain  │ │ Dashboard │
└────┬────┘ └────┬────┘ └─────┬─────┘
     │           │            │ reads
  ┌──▼──┐ ┌─────▼─────┐   ┌──▼──────────┐
  │KITE │ │VRL_ENGINE │   │ dashboard   │
  │ API │ │ Entry/Exit│   │ .json       │
  └─────┘ └─────┬─────┘   └─────────────┘
          ┌─────▼─────┐
          │VRL_TRADE  │  ┌────────────┐
          │  Orders   │  │VRL_COMMANDS│
          └───────────┘  │  Telegram  │
                         └────────────┘
     ┌──────────┐
     │ VRL_LAB  │ → ~/lab_data/ (1m/3m/5m/15m/60m/daily)
     │Data Coll.│
     └──────────┘
```

---

## File Map

| File | Purpose |
|------|---------|
| `config.yaml` | **Central config** — all tunable values. Change here, restart, done. |
| `VRL_CONFIG.py` | Config loader + validator. Typed accessors, fails fast. |
| `VRL_MAIN.py` | Master orchestrator. Strategy loop, 2-lot execution, state, alerts, dashboard. |
| `VRL_ENGINE.py` | **Minimal signal logic.** EMA gap + RSI entry. Profit floors + RSI split exit. |
| `VRL_DATA.py` | Foundation. WebSocket, indicators, Greeks, spot analysis, data cache, IST timezone. |
| `VRL_LAB.py` | Data collector. All timeframes, forward fill at EOD, daily summary. |
| `VRL_AUTH.py` | Kite authentication. Auto-login via TOTP. |
| `VRL_TRADE.py` | Order execution. Paper fills. Only file that touches orders. |
| `VRL_TRADE_LIVE.py` | Production orders. Auto-loaded when `mode: live` in config. |
| `VRL_WEB.py` | War Room dashboard. Reads JSON, zero calculations. |
| `VRL_COMMANDS.py` | Telegram command handlers. |
| `VRL_HEALTHCHECK.py` | Pre-market system verification. |
| `VRL_DEPLOY.py` | Telegram-triggered deployment. |
| `research_zones.py` | Demand/supply zone detector. |
| `research_ml.py` | ML training on scan logs. |
| `test_vrl.py` | 26 automated tests for v13.0. |

---

## Entry — v13.0 Minimal

**4 checks. Nothing else.**

| # | Check | Condition | Why |
|---|-------|-----------|-----|
| 1 | **EMA Gap** | EMA9 - EMA21 >= 3pts | Trend exists |
| 2 | **RSI** | RSI >= 50 AND rising | Momentum confirmed |
| 3 | **Green Candle** | Close > Open | Not entering on reversal |
| 4 | **Gap Widening** | Current gap > Previous gap | Trend accelerating, not fading |

All 4 pass → **ENTER 2 LOTS**

No regime gate. No ADX. No 3-min gate. No scoring system. No volume check. No body check. No spread gate.

---

## Exit — Profit Floors + RSI Split

### Phase 1: Hard SL
- SL = entry - 12pts
- Both lots exit together
- **Stale exit**: 3 candles held + peak < 3pts → exit (signal was wrong)

### Phase 2: Profit Floors (move SL up, never down)

| Peak PNL | SL moves to |
|----------|-------------|
| +10pts | entry + 2 |
| +20pts | entry + 12 |
| +30pts | entry + 22 |
| +40pts | entry + 32 |

### Phase 3: RSI Split (lots managed separately)

| RSI Level | Action |
|-----------|--------|
| RSI >= 70 | **SPLIT** — Lot 1 on floor SL, Lot 2 on ATR trail |
| RSI 75-80 | **LOT 1 SELLS** (capture spike) |
| RSI > 80 | **SELL ALL** (blowoff top) |

**Lot 2 ATR Trail**: `trail_sl = price - (ATR × 1.5)`. Trail only moves up. Never below profit floor.

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

entry:
  ema_gap_min: 3     # EMA9 - EMA21 minimum
  rsi_min: 50        # RSI floor

exit:
  hard_sl: 12        # initial SL points

profit_floors:
  - { peak: 10, lock: 2 }
  - { peak: 20, lock: 12 }
  - { peak: 30, lock: 22 }
  - { peak: 40, lock: 32 }

rsi_exit:
  split_at: 70       # split lots
  sell_spike: 75     # sell lot 1
  blowoff: 80        # sell everything
```

Change any value, restart bot. No code changes needed.

---

## Telegram Commands

| Command | Action |
|---------|--------|
| `/status` | Trade status + P&L |
| `/pnl` | Today's P&L summary |
| `/trades` | Today's trade list |
| `/spot` | Spot trend + gap |
| `/pivot` | Fib pivot levels |
| `/download` | Today's data zip (or `/download 2026-04-01`) |
| `/health` | System health check |
| `/pause` | Block new entries |
| `/resume` | Re-enable entries |
| `/forceexit` | Emergency exit all lots |

---

## Dashboard

`http://SERVER_IP:8080`

Shows for each CE/PE:
- EMA9, EMA21, EMA Gap (with ✅/❌)
- RSI + RSI prev (with ✅/❌)
- Green candle status
- Gap trend (widening/shrinking)
- Verdict: FIRED / READY / waiting reason

Position tab shows: lot 1/lot 2 status, floor SL, ATR trail, peak, RSI.

---

## Data Collection

All timeframes collected for future ML:

| Data | Frequency |
|------|-----------|
| Signal scan | Every minute (CE + PE) |
| Option 1min/3min/5min/15min | Per candle close |
| Spot 1min/5min/15min/60min/daily | Per candle close |
| Forward fill | EOD at 15:35 |
| Daily summary | EOD |

**Lab data retention**: Auto-deletes CSVs older than 30 days.

---

## Infrastructure

| Feature | Detail |
|---------|--------|
| **Auto Paper/Live** | `mode: live` in config → imports VRL_TRADE_LIVE |
| **Position Reconciliation** | Startup check: saved state vs broker positions |
| **Data Cache** | 30s TTL on historical API calls |
| **Timezone** | IST (Asia/Kolkata) everywhere |
| **Central Logs** | `~/logs/` with date-wise files + error mirror |
| **Log Download** | `/download` on Telegram or `/api/logs/download` on web |
| **Strike Locking** | Locked until spot moves 150+ pts |
| **Circuit Breaker** | 5 consecutive errors → pause + alert |

---

## Setup

```bash
# Requirements
pip install kiteconnect pandas pyotp pyyaml requests

# Environment (~/.env)
KITE_API_KEY=xxx
KITE_API_SECRET=xxx
KITE_TOTP_KEY=xxx
TG_TOKEN=xxx
TG_GROUP_ID=xxx

# Run
cd ~/VISHAL_RAJPUT
python test_vrl.py     # verify 26/26
python VRL_MAIN.py     # start bot
```

### Crontab
```
PYTHONPATH=/home/vishalraajput24/VISHAL_RAJPUT
0  8 * * 1-5  /home/.../kite_env/bin/python3 VRL_AUTH.py
0  9 * * 1-5  pkill -f VRL_WEB.py; .../python3 VRL_WEB.py &
10 9 * * 1-5  pkill -f VRL_MAIN.py; .../python3 VRL_MAIN.py &
18 9 * * 1-5  .../python3 VRL_HEALTHCHECK.py
```

---

## Test Suite

`test_vrl.py` — **26 tests**:
- Strike selection (DTE 0/1+, tolerance zones)
- Version check
- EMA gap + RSI entry (fire, block on gap, block on RSI)
- Entry SL calculation
- Hard SL exit
- Stale entry cut
- Profit floors
- RSI blowoff exit
- RSI lot split
