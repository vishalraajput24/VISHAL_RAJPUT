# VISHAL RAJPUT TRADE — v12.15.1

**Algorithmic Options Trading Bot for Nifty 50**

Paper-mode rule-based options trading system targeting consistent daily gains through a multi-layer signal architecture. Runs on Zerodha Kite API with Telegram command interface and a live web dashboard.

> **Status:** Paper Trading Only | **Market:** NSE Nifty 50 Options | **Expiry:** Weekly (Tuesday)

---

## Architecture Overview

```
┌─────────────────────────────────────────────────────────┐
│                    CRONTAB SCHEDULER                     │
│  8:00 AUTH → 9:00 WEB → 9:08 ZONES → 9:10 BOT → 9:18 HC│
└─────────────────┬───────────────────────────────────────┘
                  │
     ┌────────────┼────────────┐
     │            │            │
┌────▼────┐ ┌────▼────┐ ┌─────▼─────┐
│VRL_AUTH │ │VRL_MAIN │ │  VRL_WEB  │
│  Token  │ │  Brain  │ │ Dashboard │
│ Manager │ │  Loop   │ │  Renderer │
└────┬────┘ └────┬────┘ └─────┬─────┘
     │           │            │
     │    ┌──────┼──────┐     │ reads
     │    │      │      │     │
  ┌──▼──┐ ▼   ┌──▼──┐  ▼  ┌──▼──────────┐
  │KITE │ │   │VRL_ │  │  │ dashboard   │
  │ API │ │   │LAB  │  │  │ .json       │
  └─────┘ │   │Data │  │  │ trade_log   │
          │   │Coll.│  │  │ (read only) │
    ┌─────▼─┐ └──┬──┘  │  └─────────────┘
    │VRL_   │    │     │
    │ENGINE │    ▼     │
    │Signal │  CSV     │
    │Logic  │  Store   │
    └───┬───┘  ~/lab_data/
        │
   ┌────▼────┐  ┌────────────┐
   │VRL_TRADE│  │VRL_COMMANDS│
   │  Order  │  │  Telegram  │
   │Execution│  │  Handlers  │
   └────┬────┘  └────────────┘
        │
   ┌────▼────┐
   │TELEGRAM │
   │  War    │
   │  Room   │
   └─────────┘
```

---

## File Map

| File | Lines | Purpose |
|------|-------|---------|
| `config.yaml` | ~310 | **Central configuration**. All tunable values — strategy, risk, trail, DTE profiles, market hours. Change here, restart bot, done. |
| `VRL_CONFIG.py` | ~290 | Config loader + validator. Typed accessors, fails fast on missing/invalid config. Immutable at runtime. |
| `VRL_MAIN.py` | ~1,850 | Master orchestrator. Strategy loop, trade execution, state management, alerts, dashboard writer, position reconciliation |
| `VRL_COMMANDS.py` | ~1,230 | Telegram command handlers. `/edge`, `/status`, `/files`, all 22 commands |
| `VRL_ENGINE.py` | ~1,050 | Signal brain. 3-min gate, 1-min entry, scoring, exit management (3-phase), expiry breakout, DTE 0 spike mode |
| `VRL_DATA.py` | ~1,450 | Foundation. Settings from config, WebSocket, indicators, Greeks (Newton-Raphson IV), spot analysis, fib pivots, data cache, timezone (IST), lab cleanup |
| `VRL_LAB.py` | ~1,600 | Data collector. 1m/3m/5m/15m/60m/daily spot + option candles with ADX, forward fill, daily summary, weekend guard |
| `VRL_AUTH.py` | ~180 | Kite authentication. Auto-login via TOTP, stale token protection, Telegram alerts |
| `VRL_TRADE.py` | ~180 | Order machine. Paper fills, margin checks, specific Kite exception handling. Only file that touches Kite orders |
| `VRL_TRADE_LIVE.py` | ~430 | Production order execution. Slippage logging, LIMIT orders, position verify. Auto-loaded when `mode: live` in config |
| `VRL_WEB.py` | ~550 | War Room dashboard. Dumb renderer — reads JSON, zero calculations. Signal monitor + market data + file browser |
| `VRL_HEALTHCHECK.py` | ~420 | Pre-market system verification. 20+ checks, Telegram report at 9:18 AM |
| `VRL_DEPLOY.py` | ~280 | Telegram-triggered deployment. Git pull + restart via /deploy command |
| `research_zones.py` | ~520 | Demand/supply zone detector. 60-day historical scan, multi-timeframe, Telegram alert |
| `research_ml.py` | ~270 | ML training. Decision Tree + Gradient Boosting on scan logs, walk-forward validation |
| `test_vrl.py` | ~390 | Automated test suite. 49 tests covering strike selection, RSI, regime, scoring, exits |

---

## Central Configuration — `config.yaml`

**One file controls the entire bot.** Every tunable value lives in `config.yaml` — no code changes needed for strategy tuning.

```yaml
mode: paper          # paper | live — auto-switches trade module

instrument:
  name: NIFTY
  lot_size: 65

strategy:
  rsi:
    1m_low: 30
    1m_high_normal: 50
    1m_high_strong: 58
    1m_high_dte0: 70
  adx:
    choppy_bypass: 15
    multi_tf_bonus: 25
  spread:
    ce_min: 2
    pe_min: 2

risk:
  sl_max: 25
  sl_dte0: 15
  max_hold_dte0: 5

trail:
  profit_floors:
    10: 5       # peak 10 → lock 5pts
    20: 12
    30: 20
    50: 60      # peak 50 → lock 60% of peak
  adaptive_ema:
    low:  { timeframe: 5minute, candles: 2 }
    mid:  { timeframe: 3minute, candles: 2 }
    high: { timeframe: minute,  candles: 1 }
```

### Common Scenarios

| Change | What to Edit | Restart? |
|--------|-------------|----------|
| Tune RSI zone | `strategy.rsi.1m_low` / `1m_high_normal` | Yes |
| Tighter SL on expiry | `risk.sl_dte0` | Yes |
| Go live | `mode: live` | Yes |
| Add BankNifty | Duplicate as `config_banknifty.yaml`, change `instrument` | New instance |
| Change lot size | `instrument.lot_size` | Yes |

### Validation

Config validates on load — missing keys = bot refuses to start with a clear error. Secrets (API keys, tokens) stay in `~/.env`, never in config.

---

## Production Improvements (v12.15.1)

| Feature | Description |
|---------|-------------|
| **Auto Paper/Live Switch** | `mode: live` in config auto-imports `VRL_TRADE_LIVE.py`. No manual file copying. |
| **Position Reconciliation** | On startup, compares saved state with broker positions. Alerts on mismatch via Telegram. |
| **Specific Exception Handling** | `TokenException`, `OrderException`, `NetworkException` instead of broad `except` in trade execution. |
| **Data Caching** | 30-second TTL cache on `get_historical_data()` — eliminates duplicate API calls within same scan cycle. |
| **Timezone Awareness** | All timestamps use `Asia/Kolkata` (IST) via `zoneinfo`. Critical for cron jobs and market hour checks. |
| **Lab Data Retention** | Auto-deletes lab CSVs older than 30 days on startup. Configurable via `lab.retention_days`. |

---

## Signal Architecture — v12.15.1

### Layer 1: Boss (3-Min Permission Gate)
> "Should we even be looking for a trade right now?"

Checks the **option's** 3-minute chart for trend health:

| Condition | Check | Must Pass |
|-----------|-------|-----------|
| **E** — EMA Aligned | EMA9 > EMA21 (option trending up) | 2 of 4 |
| **B** — Body | Candle body ≥ 40% of range | 2 of 4 |
| **R** — RSI | RSI between 42-72 (not exhausted) | 2 of 4 |
| **P** — Price | Close ≥ EMA9 (above fast average) | 2 of 4 |

**2 of 4 conditions must pass** for the gate to open. If blocked, no further checks run.

**DTE 0: Skips 3-min gate entirely** — uses spot direction instead. Data showed 33 blocked DTE 0 entries were ALL winners (+57pts avg).

**Fail-closed**: Errors or insufficient data → BLOCK (not permit).

Bonus: All 4 pass + spread ≥ 8pts → +1 score bonus.

### Layer 1.5: Regime Filter
> "Is the market trending enough to trade?"

| Regime | DTE 1+ | DTE 0 |
|--------|--------|-------|
| TRENDING_STRONG | Entry allowed | Entry allowed |
| TRENDING | Entry allowed | Entry allowed |
| NEUTRAL | Blocked | Blocked |
| CHOPPY | Blocked | Allowed if ADX ≥ 15 |

### Layer 2: Soldier (1-Min Spread Gate)
> "Is there enough momentum to enter?"

| Gate | CE (DTE 1+) | PE (DTE 1+) | CE (DTE 0) | PE (DTE 0) |
|------|-------------|-------------|------------|------------|
| 1-min EMA Spread | ≥ +2 pts | ≥ +2 pts | ≥ +1 pt | ≥ +1 pt |

Spread deceleration check **removed** in v12.15.1 — was blocking valid entries.

### Layer 3: Sniper (1-Min Entry Trigger)
> "Is this the exact right candle to enter?"

| Condition | DTE 1+ | DTE 0 |
|-----------|--------|-------|
| Body | Green candle, body ≥ 40% | Same |
| RSI | 30-50 (58 if ADX ≥ 30) AND rising | 30-70 AND rising |
| RSI vs 3m | 1m RSI must be below 3m RSI | Same (bypassed on spike) |
| Volume | ≥ 1.5x average | Same |

**DTE 0 Momentum Spike**: Premium moves ≥10% in 2 candles → skip RSI zone check, only need body ≥ 40% + vol ≥ 1.0x. Mode = `DTE0_SPIKE`.

### Scoring System (0-8 points)

| Points | Source |
|--------|--------|
| +1 | Body ≥ 40% |
| +1 | Body bonus ≥ 50% |
| +1 | RSI in zone + rising |
| +1 | Volume OK |
| +1 | Delta in range (0.35-0.65) |
| +1 | Double alignment (3m strong + 1m aligned) |
| +1 | Gate bonus (all 4 conditions + spread ≥ 8) |
| +1 | Multi-TF ADX bonus (3m + 5m + 15m all ≥ 25) |

**Modifiers:**
- **Zone modifier**: ±1 based on demand/supply zone proximity (within 30pts = ±1, within 60pts = -1 conflicting only)
- **Bias score**: Against daily bias → need score ≥ 6 (CE in BEAR, PE in BULL)

**Entry fires at score ≥ 5** (≥ 6 after loss streak or against bias).

---

## Direction-Aware Strike Selection

### The Problem
Old: Both CE and PE used same ATM strike. CE gets ITM but PE gets OTM on the same strike. OTM PE has zero intrinsic value — dies on any pause.

### The Fix

| DTE | Step | CE Strike | PE Strike |
|-----|------|-----------|-----------|
| 0 | 50 | ATM or 1 step below spot (ITM) | ATM or 1 step above spot (ITM) |
| 1+ | 100 | ATM or 1 step below spot (ITM) | ATM or 1 step above spot (ITM) |

**Example:** Spot 22,930
- DTE 3: CE = 22,900 (ATM ≤ spot), PE = 23,000 (1-step ITM)
- DTE 0: CE = 22,900 (1-step ITM), PE = 23,000 (1-step ITM)

### Premium Filter
| Check | DTE 1+ | DTE 0 |
|-------|--------|-------|
| Min premium | ≥ ₹100 | ≥ ₹50 |
| Max premium | ≤ ₹400 | ≤ ₹400 |

---

## Exit System — 3-Phase Trail

### Phase 1: Stop Loss
- ATR-based SL (2x ATR, capped at 25pts normal / **15pts expiry**)
- Minimum SL floor based on premium level
- **Stale Entry Cut**: 3 candles held + peak < 5pts → exit early
- **DTE 0 Max Hold**: 5 candles → forced exit regardless

### Phase 2: Breakeven Lock
- Triggers at breakeven_pts (profile-dependent, ~8-15pts)
- SL moves to entry + 2pts
- SL ratchets up every 5pts of additional profit

### Phase 3: Adaptive EMA Trail

| Running PNL | Timeframe | Candles Below EMA9 | Label |
|-------------|-----------|-------------------|-------|
| < 15pts | 5-min | 2 consecutive | CONSERVATIVE |
| 15-25pts | 3-min | 2 consecutive | MODERATE |
| > 25pts | 1-min | 1 candle | AGGRESSIVE |

### Hard Profit Floors (checked before drawdown)

| Peak PNL | Floor (exit if running drops to) |
|----------|----------------------------------|
| ≥ 10pts | Lock 5pts |
| ≥ 20pts | Lock 12pts |
| ≥ 30pts | Lock 20pts |
| ≥ 50pts | Lock 60% of peak |

### Other Exit Triggers
- **RSI Exhaustion**: RSI ≥ 76 with sufficient profit → immediate exit
- **Gamma Rider**: RSI was overbought, dropped below 65 with profit → exit
- **Drawdown exit**: Only fires when running is below profit floor

---

## Telegram Alerts

### Milestone Alerts (+10, +20, +30pts)
Rich alerts with: entry → current price, P&L in pts + rupees, peak, held time, current SL level (Phase/Breakeven/Trail), distance to SL.

### Entry Alerts
One-liner summary at top: `PE 22900 ₹232 Score 7/5 TRENDING`
Bias, session, full score breakdown, greeks, exit plan.

### Exit Alerts
One-liner: `WIN +17.9pts ₹1,164`
Capture % of peak, held time, phase, trade quality assessment, daily W/L summary.

---

## Expiry Breakout Mode (DTE = 0)

On expiry day, regular 3-min option gate is skipped. Special modes activate:

### Standard DTE 0 Entry
- Skips `_check_3min` entirely — uses spot direction (ADX + spread)
- RSI ceiling raised to 70 (explosive moves)
- CHOPPY regime allowed if ADX ≥ 15
- Spread threshold: 1pt (vs 2pts normal)
- Premium min: ₹50 (vs ₹100)
- SL cap: 15pts (vs 25pts)
- Max hold: 5 candles

### DTE 0 Spike Mode
- Premium moves ≥10% in 2 candles → bypasses RSI zone check
- Only needs body ≥ 40% + volume ≥ 1.0x
- Catches fast gamma spikes

### Expiry Breakout Mode
1. Detect spot consolidation (5+ candles, range < 15pts)
2. Wait for breakout (spot moves > 10pts beyond consolidation)
3. Breakout UP → CE entry, breakout DOWN → PE entry
4. Simplified scoring: breakout magnitude + delta + gamma + volume + fib proximity
5. Tighter SL (15pts cap) and trail (20% drawdown)

---

## Warning System

All warnings are **Telegram alerts only — zero blocking**. Data collection for future gate decisions.

| Warning | Trigger | Time |
|---------|---------|------|
| Daily Bias | EMA21 + ADX on daily candles → BULL/BEAR/SIDEWAYS | 9:20 AM |
| Straddle Decay | ATM CE+PE premium sum drops > 5% → sellers day | After 9:30 |
| VIX Alert | VIX > 22 (elevated) or > 28 (danger) | Continuous |
| Hourly RSI | RSI > 70 (CE risky) or < 30 (PE risky) | Every hour |

---

## Data Collection

### Spot Data (~/lab_data/spot/)
| Timeframe | File Pattern | Fields |
|-----------|-------------|--------|
| 1-min | `nifty_spot_1min_YYYYMMDD.csv` | OHLCV + EMA9 + EMA21 + RSI + ADX |
| 5-min | `nifty_spot_5min_YYYYMMDD.csv` | OHLCV + EMA9 + EMA21 + RSI + ADX |
| 15-min | `nifty_spot_15min_YYYYMMDD.csv` | OHLCV + EMA9 + EMA21 + RSI + ADX |
| 60-min | `nifty_spot_60min_YYYYMMDD.csv` | OHLCV + EMA9 + EMA21 + RSI + ADX |
| Daily | `nifty_spot_daily.csv` | OHLCV + EMA21 + RSI + ADX |

**Weekend guard**: No data collection on Saturday/Sunday. Prevents junk files.

**60-min**: Fires at 10:00-15:00 (market hours only) with dedup guard.

**Daily**: Fires at 15:30 with dedup guard.

### Option Data (~/lab_data/)
| Timeframe | File Pattern | Fields |
|-----------|-------------|--------|
| 1-min | `options_1min/nifty_option_1min_*.csv` | OHLCV + body + RSI + EMA9 + ADX + volume_ratio + IV + delta |
| 3-min | `options_3min/nifty_option_3min_*.csv` | OHLCV + body + ADX + RSI + EMA9 + IV + delta + gamma + theta + vega + forward fill |
| 5-min | `options_1min/nifty_option_5min_*.csv` | OHLCV + body + RSI + EMA9/21 + spread + ADX + IV + delta |
| 15-min | `options_1min/nifty_option_15min_*.csv` | OHLCV + body + RSI + EMA9/21 + MACD + ADX + IV + delta |

### Trade Log (~/lab_data/)
`vrl_trade_log.csv` — **29 columns**

Every trade: entry/exit prices, PNL, peak, trough (worst drawdown), exit phase, exit reason, score, session, strike, SL distance, spreads, delta, bias, VIX, hourly RSI.

### Daily Summary (~/lab_data/reports/)
`vrl_daily_summary.csv` — **40 columns**

One row per day: trade stats, scan stats (blocks by reason), market context (VIX, regime, gap, bias), straddle data.

### Logs
- **Daily rotation**: `vrl_live.log` rotates at midnight, 7-day retention
- **Lab logs**: `vrl_lab.log` for data collection
- **Trade log cleanup**: Auto-removes corrupted rows at startup

---

## Demand/Supply Zone Detection

`research_zones.py` scans 60 days of historical spot data:

1. **Consolidation**: 3-8 candles with range < 80pts
2. **Impulse**: Following candle has body > 50%, body > 30pts
3. **Zone**: The consolidation range becomes a demand (bullish impulse) or supply (bearish impulse) zone
4. **Strength**: Impulse size, freshness (untested), multi-timeframe alignment
5. **Decay**: Each revisit weakens zone. After 3 tests → zone dead

Scans on 3-min, 15-min, and 60-min timeframes. Zones that align across timeframes marked as MTF (strongest).

**Zone modifier in scoring**: CE near DEMAND = +1, CE near SUPPLY = -1, PE near SUPPLY = +1, PE near DEMAND = -1. Within 30pts = full effect, within 60pts = conflicting only.

---

## Fib Pivot Points

Calculated daily from previous session's High/Low/Close:

| Level | Formula |
|-------|---------|
| Pivot | (H + L + C) / 3 |
| R1 / S1 | Pivot ± 0.382 × Range |
| R2 / S2 | Pivot ± 0.618 × Range |
| R3 / S3 | Pivot ± 1.000 × Range |

---

## Greeks — Newton-Raphson IV

Back-calculates real IV from option LTP (not VIX proxy). Uses Black-Scholes model with Newton-Raphson iteration (max 100 iterations, 0.01 tolerance) to solve for implied volatility from market price. Then calculates Delta, Gamma, Theta, Vega from the solved IV.

---

## Dashboard — War Room

**http://SERVER_IP:8080** (optional auth via `VRL_WEB_TOKEN` env var)

Dumb renderer. Reads `~/state/vrl_dashboard.json` written by VRL_MAIN.py every scan cycle. Zero calculations in web server.

---

## Test Suite

`test_vrl.py` — **49 automated tests** covering:
- Strike selection (DTE 0 step=50, DTE 1+ step=100, tolerance zones)
- RSI constants (adaptive thresholds, DTE 0 ceiling)
- Spot regime classification
- Premium filters
- Profit floors (peak 10/20/30/50)
- Stale entry cut
- Phase transitions and SL ratcheting
- 3-min gate threshold
- Multi-TF ADX bonus
- Score entry mechanics
- Regime blocks

Run: `~/kite_env/bin/python test_vrl.py`

---

## Setup

### Requirements
```bash
pip install kiteconnect pandas pyotp pyyaml requests
```

### Environment Variables (`~/.env`)
```
KITE_API_KEY=your_key
KITE_API_SECRET=your_secret
KITE_TOTP_KEY=your_totp
TG_TOKEN=your_telegram_bot_token
TG_GROUP_ID=your_chat_id
```

### Crontab (Mon-Fri)
```
PYTHONPATH=/home/vishalraajput24/VISHAL_RAJPUT
0  8 * * 1-5  cd ~/VISHAL_RAJPUT && ~/kite_env/bin/python3 VRL_AUTH.py
0  9 * * 1-5  cd ~/VISHAL_RAJPUT && nohup ~/kite_env/bin/python3 VRL_WEB.py &
8  9 * * 1-5  cd ~/VISHAL_RAJPUT && ~/kite_env/bin/python3 research_zones.py
10 9 * * 1-5  cd ~/VISHAL_RAJPUT && nohup ~/kite_env/bin/python3 VRL_MAIN.py &
18 9 * * 1-5  cd ~/VISHAL_RAJPUT && ~/kite_env/bin/python3 VRL_HEALTHCHECK.py
40 15 * * 1-5 cd ~/VISHAL_RAJPUT && ~/kite_env/bin/python3 research_ml.py
```

### Quick Start
```bash
cd ~/VISHAL_RAJPUT
~/kite_env/bin/python test_vrl.py     # verify 49/49
~/kite_env/bin/python VRL_MAIN.py     # start bot
```
