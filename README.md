# VISHAL RAJPUT TRADE — v12.15

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
| `VRL_MAIN.py` | ~1,770 | Master orchestrator. Strategy loop, trade execution, state management, alerts, dashboard writer |
| `VRL_COMMANDS.py` | ~1,230 | Telegram command handlers. `/edge`, `/status`, `/files`, all 22 commands |
| `VRL_ENGINE.py` | ~870 | Signal brain. 3-min gate, 1-min entry, scoring, exit management (3-phase), expiry breakout mode |
| `VRL_DATA.py` | ~1,360 | Foundation. Settings, WebSocket, indicators, Greeks (Newton-Raphson IV), spot analysis, fib pivots, direction-aware strike selection |
| `VRL_LAB.py` | ~1,580 | Data collector. 1m/3m/5m/15m/60m/daily spot + option candles with ADX, forward fill, daily summary |
| `VRL_AUTH.py` | ~180 | Kite authentication. Auto-login via TOTP, stale token protection, Telegram alerts |
| `VRL_TRADE.py` | ~160 | Order machine. Paper fills, margin checks. Only file that touches Kite orders |
| `VRL_TRADE_LIVE.py` | ~430 | Production order execution. Slippage logging, LIMIT orders, position verify. For go-live |
| `VRL_WEB.py` | ~550 | War Room dashboard. Dumb renderer — reads JSON, zero calculations. Signal monitor + market data + file browser |
| `VRL_HEALTHCHECK.py` | ~420 | Pre-market system verification. 20+ checks, Telegram report at 9:18 AM |
| `VRL_DEPLOY.py` | ~280 | Telegram-triggered deployment. Git pull + restart via /deploy command |
| `research_zones.py` | ~520 | Demand/supply zone detector. 60-day historical scan, multi-timeframe, Telegram alert |

---

## Signal Architecture — v12.15

### Layer 1: Boss (3-Min Permission Gate)
> "Should we even be looking for a trade right now?"

Checks the **option's** 3-minute chart for trend health:

| Condition | Check | Must Pass |
|-----------|-------|-----------|
| **E** — EMA Aligned | EMA9 > EMA21 (option trending up) | 3 of 4 |
| **B** — Body | Candle body ≥ 40% of range | 3 of 4 |
| **R** — RSI | RSI between 42-72 (not exhausted) | 3 of 4 |
| **P** — Price | Close ≥ EMA9 (above fast average) | 3 of 4 |

**3 of 4 conditions must pass** for the gate to open. If blocked, no further checks run.

**Fail-closed**: Errors or insufficient data → BLOCK (not permit).

Bonus: All 4 pass + spread ≥ 8pts → +1 score bonus.

### Layer 1.5: Regime Filter (v12.15)
> "Is the market trending enough to trade?"

| Regime | Action |
|--------|--------|
| TRENDING_STRONG | Entry allowed |
| TRENDING | Entry allowed |
| NEUTRAL | CE blocked, PE allowed |
| CHOPPY | Both blocked |

### Layer 2: Soldier (1-Min Spread Gate)
> "Is there enough momentum to enter?"

| Gate | CE Requirement | PE Requirement |
|------|---------------|----------------|
| 1-min EMA Spread | ≥ +6 pts | ≥ +4 pts |
| Spread Acceleration | Must be increasing vs previous candle (v12.15) |

### Layer 3: Sniper (1-Min Entry Trigger)
> "Is this the exact right candle to enter?"

| Condition | Requirement |
|-----------|-------------|
| Body | Green candle, body ≥ 40% of range |
| RSI | Between **48-60** AND rising (v12.15: tightened from 45-65) |
| RSI vs 3m | **1m RSI must be BELOW 3m RSI** — dip within trend, not chasing (v12.15) |
| Volume | ≥ **1.5x** average (v12.15: raised from 1.0x) |

### Scoring System (0-7 points)

| Points | Source |
|--------|--------|
| +1 | Body ≥ 40% |
| +1 | Body bonus ≥ 50% |
| +1 | RSI in zone + rising |
| +1 | Volume OK |
| +1 | Delta in range (0.35-0.65) |
| +1 | Double alignment (3m strong + 1m aligned) |
| +1 | Gate bonus (all 4 conditions + spread ≥ 8) |

**Entry fires at score ≥ 5** (≥ 6 after loss streak).

---

## Direction-Aware Strike Selection (v12.15)

### The Problem
Old: Both CE and PE used same ATM strike. CE gets ITM but PE gets OTM on the same strike. OTM PE has zero intrinsic value — dies on any pause.

### The Fix

| DTE | Step | CE Strike | PE Strike |
|-----|------|-----------|-----------|
| 0 | 50 | ATM or 1 step below spot (ITM) | ATM or 1 step above spot (ITM) |
| 1+ | 100 | ATM or 1 step below spot (ITM) | ATM or 1 step above spot (ITM) |

**Example:** Spot 22,930
- DTE 3: CE = 22,900 (ATM ≤ spot ✅), PE = 23,000 (1-step ITM)
- DTE 0: CE = 22,900 (1-step ITM), PE = 23,000 (1-step ITM)

### Premium Filter
| Check | Rule | Why |
|-------|------|-----|
| Min premium | ≥ ₹100 | Below ₹100 = too OTM, gamma risk |
| Max premium | ≤ ₹400 | Above ₹400 = too deep ITM, low % return |
| Sweet spot | ₹150-300 | Best risk/reward |

---

## Exit System — 3-Phase Trail

### Phase 1: Stop Loss
- ATR-based SL (2x ATR, capped at 25pts normal / 20pts expiry)
- Minimum SL floor based on premium level
- **Stale Entry Cut**: 3 candles held + peak < 5pts → exit early (v12.15: was 5 candles)

### Phase 2: Breakeven Lock
- Triggers at breakeven_pts (profile-dependent, ~12-20pts)
- SL moves to entry + 2pts
- SL ratchets up every 5pts of additional profit

### Phase 3: EMA Trail
- **Wide trail** (default): 5-min EMA9 — needs 2 consecutive candle closes below EMA to exit
- **Tight trail** (triggered by RSI ≥ 76 or EMA spread narrowing): 3-min EMA9
- **RSI Exhaustion**: RSI ≥ 76 with profit → immediate exit (top captured)
- **Gamma Rider**: RSI was overbought, dropped below 65 with profit → exit (reversal caught)
- **Drawdown exit**: Peak drawdown > 25% (20% on expiry)

---

## Telegram Alerts (v12.15)

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

On expiry day, regular 3-min option gate is too slow. Special mode activates:

1. Detect spot consolidation (5+ candles, range < 15pts)
2. Wait for breakout (spot moves > 10pts beyond consolidation)
3. Breakout UP → CE entry, breakout DOWN → PE entry
4. Simplified scoring: breakout magnitude + delta + gamma + volume + fib proximity
5. Tighter SL (20pts cap) and trail (20% drawdown)

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

### Tabs

| Tab | Content |
|-----|---------|
| SIGNALS | CE vs PE side-by-side: 3m gate (E/B/R/P dots), 1m spread bar, body/RSI/vol, RSI vs 3m, spread accel, score, greeks, verdict |
| MARKET | Spot EMA/RSI/regime, multi-TF spot + option tables with ADX, fib pivots with distances, hourly RSI, straddle, gap, session |
| TRADES | Trade cards: peak/trough, exit reason, session, phase |
| FILES | Browse all data folders, download CSVs |

---

## Telegram Commands

| Command | Purpose |
|---------|---------|
| `/help` | All commands |
| `/edge` | War Room (CE/PE signals + spot + verdict) |
| `/spot` | Spot trend + gap + regime |
| `/regime` | Regime + detection mode |
| `/align` | Independent indicator alignment check |
| `/pivot` | Fib pivot levels + zones |
| `/status` | Trade status + PNL + SL level |
| `/pnl` | Today's P&L summary |
| `/trades` | Today's trade list with details |
| `/files` | Interactive file browser |
| `/download` | Today's data zip |
| `/source` | Download all source code |
| `/health` | System health check |
| `/livecheck` | Last 50 log lines |
| `/pause` | Block new entries |
| `/resume` | Re-enable entries |
| `/forceexit` | Emergency exit |
| `/restart` | Restart bot |
| `/deploy` | Git pull + restart (via VRL_DEPLOY.py) |

---

## Server Setup

```
Platform    : Google Cloud Platform (GCP)
Instance    : dvrn50cpbot (asia-south1-a)
OS          : Ubuntu 24
Python      : 3.x with kiteconnect, pandas, numpy, pyotp
Code        : ~/VISHAL_RAJPUT/ (git repo)
Data        : ~/lab_data/ (permanent storage)
State       : ~/state/ (live state + dashboard JSON)
Logs        : ~/logs/ (live + lab, daily rotation)
Research    : ~/research/ (zone CSVs)
Dashboard   : http://EXTERNAL_IP:8080
Restart     : vrl-restart (alias)
```

---

## DTE Profiles

| DTE | SL | Breakeven | Trail | Tighten | RSI Exhaust |
|-----|-----|-----------|-------|---------|-------------|
| 6+ | 20pts | 15pts | 5-min | 3-min | 76 / 12pts |
| 3-5 | 18pts | 14pts | 5-min | 3-min | 76 / 12pts |
| 2 | 15pts | 12pts | 3-min | 1-min | 75 / 10pts |
| 1 | 12pts | 10pts | 3-min | 1-min | 74 / 8pts |
| 0 | 10pts | 8pts | 1-min | 1-min | 72 / 6pts |

---

## Crontab Schedule (Mon-Fri)

```
8:00  VRL_AUTH.py        → Fresh Kite token + Telegram alert
9:00  VRL_WEB.py         → Dashboard server starts
9:08  research_zones.py  → Zone detection + Telegram alert
9:10  VRL_MAIN.py        → Bot starts (scan from 9:15, fire from 9:45)
9:18  VRL_HEALTHCHECK.py → System verification + Telegram report
```

Bot auto-generates at EOD:
- 15:35 — EOD trade report
- 15:35 — Forward fill (scan log + option CSVs)
- 15:36 — Daily spot candle
- 15:36 — Daily summary CSV

---

## Security (v12.15)

- Path traversal protection on Telegram file browser and web `/api/download`
- Auth check on Telegram callback queries (inline keyboards)
- Bot token excluded from log files
- Optional web auth via `VRL_WEB_TOKEN` env var
- 3-min gate fail-closed (errors block, not permit)
- File handle leaks fixed

---

## Version History

| Version | Date | Changes |
|---------|------|---------|
| v12.15 | Mar 2026 | **Strategy overhaul**: RSI 48-60, RSI 1m<3m (dip entry), spread acceleration, volume 1.5x, regime filter (TRENDING only), stale cut 3 candles. **Direction-aware strikes**: CE ITM/PE ITM, premium ₹100-400 filter, step=50 DTE≤3. **Code cleanup**: VRL_COMMANDS.py extracted (-40% MAIN), dead code removed (-313 lines), daily log rotation, trade log cleanup. **Dashboard**: 1-min data + greeks + regime on all paths, ADX on 1m/5m. **Alerts**: Rich milestones with trail SL, entry with bias/session, exit with capture %. **Security**: Path traversal fix, auth on callbacks, fail-closed gates, web auth token |
| v12.14 | Mar 2026 | Warning system, data richness (29-col trade log, 38-col scan log), trough PNL, War Room dashboard, demand/supply zones, AUTH fix, files page, spot enrichment (ADX all TFs), hourly+daily collection |
| v12.13 | Mar 2026 | Expiry breakout mode, fib pivots, spot consolidation detection |
| v12.12 | Mar 2026 | ATR-based SL, 2-candle EMA exit, separate RSI zones per timeframe |
| v12.11 | Mar 2026 | Momentum fallback for DTE≤1, spot regime backup, scan log |
| v12.10 | Mar 2026 | 100-step ATM strikes for indicator stability |
| v12.9 | Mar 2026 | Circuit breaker, error count fix |

---

*Built by Vishal Rajput. Strategy data-driven — every change backed by 33+ paper trades.*
