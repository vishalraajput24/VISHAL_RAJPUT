# VISHAL RAJPUT TRADE — v12.14

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
   ┌────▼────┐
   │VRL_TRADE│
   │  Order  │
   │Execution│
   └────┬────┘
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
| `VRL_MAIN.py` | ~2500 | Master orchestrator. Strategy loop, Telegram commands, trade execution, state management, dashboard snapshot writer |
| `VRL_ENGINE.py` | ~820 | Signal brain. 3-min gate, 1-min entry, scoring, exit management (3-phase), expiry breakout mode |
| `VRL_DATA.py` | ~1050 | Foundation. Settings, WebSocket, indicators, Greeks (Newton-Raphson IV), spot analysis, fib pivots, warning system |
| `VRL_LAB.py` | ~1400 | Data collector. 1m/3m/5m/15m/60m/daily spot + option candles, scan log, forward fill, daily summary |
| `VRL_AUTH.py` | ~160 | Kite authentication. Auto-login via TOTP, stale token protection, Telegram alerts |
| `VRL_TRADE.py` | ~210 | Order machine. Paper/live fills, order verification, margin checks. Only file that touches Kite orders |
| `VRL_WEB.py` | ~300 | War Room dashboard. Dumb renderer — reads JSON, zero calculations. Signal monitor + market data + file browser |
| `VRL_HEALTHCHECK.py` | ~380 | Pre-market system verification. 20+ checks, Telegram report at 9:18 AM |
| `VRL_DEPLOY.py` | ~280 | Telegram-triggered deployment. Git pull + restart via /deploy command |
| `research_zones.py` | ~350 | Demand/supply zone detector. 60-day historical scan, multi-timeframe, Telegram alert |

---

## Signal Architecture — 3-Layer Stack

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

Bonus: All 4 pass + spread ≥ 8pts → +1 score bonus.

### Layer 2: Soldier (1-Min Spread Gate)
> "Is there enough momentum to enter?"

| Gate | CE Requirement | PE Requirement |
|------|---------------|----------------|
| 1-min EMA Spread | ≥ +6 pts | ≥ +4 pts |

Both CE and PE require the **option price** to be trending UP (EMA9 > EMA21). We're buying options — the premium must be rising.

### Layer 3: Sniper (1-Min Entry Trigger)
> "Is this the exact right candle to enter?"

| Condition | Requirement |
|-----------|-------------|
| Body | Green candle, body ≥ 40% of range |
| RSI | Between 45-65 AND rising (sweet zone — room to run) |
| Volume | ≥ 1.0x average (institutional participation) |

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

## Exit System — 3-Phase Trail

### Phase 1: Stop Loss
- ATR-based SL (2x ATR, capped at 25pts normal / 20pts expiry)
- Minimum SL floor based on premium level
- **Stale Entry Cut**: 5 candles held + peak < 5pts → exit early (saves full SL)

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

## Expiry Breakout Mode (DTE = 0)

On expiry day, regular 3-min option gate is too slow. Special mode activates:

1. Detect spot consolidation (5+ candles, range < 15pts)
2. Wait for breakout (spot moves > 10pts beyond consolidation)
3. Breakout UP → CE entry, breakout DOWN → PE entry
4. Simplified scoring: breakout magnitude + delta + gamma + volume + fib proximity
5. Tighter SL (20pts cap) and trail (20% drawdown)

---

## Warning System (v12.14)

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
| 1-min | `options_1min/nifty_option_1min_*.csv` | OHLCV + body + RSI + EMA9 + volume_ratio + IV + delta |
| 3-min | `options_3min/nifty_option_3min_*.csv` | OHLCV + body + ADX + RSI + EMA9 + IV + delta + gamma + theta + vega + forward fill |
| 5-min | `options_1min/nifty_option_5min_*.csv` | OHLCV + body + RSI + EMA9/21 + spread + IV + delta |
| 15-min | `options_1min/nifty_option_15min_*.csv` | OHLCV + body + RSI + EMA9/21 + MACD + ADX + IV + delta |

### Signal Scan Log (~/lab_data/options_1min/)
`nifty_signal_scan_YYYYMMDD.csv` — **38 columns**

Every 1-min candle, both CE and PE: all indicator values, gate status, score, fired/blocked, reject reason, Greeks, VIX, spot data, bias, hourly RSI, fib proximity. Forward filled at EOD with 3/5/10 candle future prices.

### Trade Log (~/lab_data/)
`vrl_trade_log.csv` — **29 columns**

Every trade: entry/exit prices, PNL, peak, trough (worst drawdown), exit phase, exit reason, score, session, strike, SL distance, spreads, delta, bias, VIX, hourly RSI.

### Daily Summary (~/lab_data/reports/)
`vrl_daily_summary.csv` — **40 columns**

One row per day: trade stats, scan stats (blocks by reason), market context (VIX, regime, gap, bias), straddle data.

---

## Demand/Supply Zone Detection

`research_zones.py` scans 60 days of historical spot data:

1. **Consolidation**: 3-8 candles with range < 80pts
2. **Impulse**: Following candle has body > 50%, body > 30pts
3. **Zone**: The consolidation range becomes a demand (bullish impulse) or supply (bearish impulse) zone
4. **Strength**: Impulse size, freshness (untested), multi-timeframe alignment
5. **Decay**: Each revisit weakens zone. After 3 tests → zone dead

Scans on 3-min, 15-min, and 60-min timeframes. Zones that align across timeframes marked as 🔥MTF (strongest).

Output: CSV + JSON (for dashboard) + Telegram alert with nearest zones.

---

## Fib Pivot Points

Calculated daily from previous session's High/Low/Close:

| Level | Formula |
|-------|---------|
| Pivot | (H + L + C) / 3 |
| R1 / S1 | Pivot ± 0.382 × Range |
| R2 / S2 | Pivot ± 0.618 × Range |
| R3 / S3 | Pivot ± 1.000 × Range |

Displayed on War Room dashboard with distance from current spot and nearest level highlight.

---

## Greeks — Newton-Raphson IV

Back-calculates real IV from option LTP (not VIX proxy):

```
VIX = 24% but actual option IV might be 37% on expiry day
```

Uses Black-Scholes model with Newton-Raphson iteration (max 100 iterations, 0.01 tolerance) to solve for implied volatility from market price. Then calculates Delta, Gamma, Theta, Vega from the solved IV.

---

## Dashboard — War Room

**http://SERVER_IP:8080**

Dumb renderer. Reads `~/state/vrl_dashboard.json` written by VRL_MAIN.py every scan cycle. Zero calculations in web server. Zero Kite dependency.

### Tabs

| Tab | Content |
|-----|---------|
| ⚡ SIGNALS | CE vs PE side-by-side: 3m gate (E/B/R/P dots), 1m spread bar, body/RSI/vol, score, verdict |
| 📊 MARKET | Spot EMA/RSI/regime, fib pivots with distances, hourly RSI, straddle, gap, session |
| 📒 TRADES | Trade cards: peak↑ trough↓, exit reason, session, phase |
| 📁 FILES | Browse all data folders, download CSVs to phone |

### Position Display (when in trade)
Entry, LTP, PNL, peak, trough, SL, SL distance, phase, trail status, RSI overbought status — all with visual progress bar.

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
| `/status` | Trade status + PNL |
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
| `/deploy` | Git pull + restart |

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

## Key Principles

1. **Data is the edge** — collect first, analyze later, change strategy only when data proves it
2. **VWAP applies to option chart, NOT spot** — spot has no volume
3. **Nifty expiry is Tuesday** — detected via Kite API, never day-of-week assumption
4. **Newton-Raphson IV** — real IV from LTP, not VIX proxy
5. **No strategy changes without data** — 5 clean days of paper trading before any gate modification
6. **Dashboard is dumb** — reads what bot writes, calculates nothing
7. **Single source of truth** — bot brain writes state, everything else reads
8. **Going live is Vishal's decision alone** — never suggested by the system

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

## Server Setup

```
Platform    : Google Cloud Platform (GCP)
Instance    : dvrn50cpbot (asia-south1-a)
OS          : Ubuntu 24
Python      : 3.x with kiteconnect, pandas, numpy, pyotp
Code        : ~/VISHAL_RAJPUT/ (git repo)
Data        : ~/lab_data/ (permanent storage)
State       : ~/state/ (live state + dashboard JSON)
Logs        : ~/logs/ (live + lab)
Research    : ~/research/ (zone CSVs)
Dashboard   : http://EXTERNAL_IP:8080
```

---

## Version History

| Version | Date | Changes |
|---------|------|---------|
| v12.14 | Mar 2026 | Warning system, data richness (29-col trade log, 38-col scan log), trough PNL, War Room dashboard, demand/supply zones, AUTH fix, files page, spot enrichment (ADX all TFs), hourly+daily collection |
| v12.13 | Mar 2026 | Expiry breakout mode, fib pivots, spot consolidation detection |
| v12.12 | Mar 2026 | ATR-based SL, 2-candle EMA exit, separate RSI zones per timeframe |
| v12.11 | Mar 2026 | Momentum fallback for DTE≤1, spot regime backup, scan log |
| v12.10 | Mar 2026 | 100-step ATM strikes for indicator stability |
| v12.9 | Mar 2026 | Circuit breaker, error count fix |

---

*Built by Vishal Rajput. Strategy untouched — data decides all future changes.*
