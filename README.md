# VISHAL RAJPUT TRADE — v16.0

Algorithmic Nifty 50 options trading bot. Paper mode. Zerodha Kite API.

## Strategy: EMA9 Band Breakout (3-min option candles)

### Entry (6 hard gates, all must pass)
1. **Time window** 09:30–15:10 IST
2. **Cooldown** 5 min same direction after exit
3. **Fresh breakout** close > EMA9-high (3-candle lookback)
4. **Green candle** close > open
5. **Body ≥ 30%** of candle range
6. **Band width ≥ 8 pts** (chop filter)

### Display classifiers (never block)
- **Straddle Δ** — STRONG / NEUTRAL / WEAK / NA
- **VWAP confluence** — spot vs session VWAP

### Exit chain (6 rules, priority order)
1. `EMERGENCY_SL` — pnl ≤ −20 pts
2. `EOD_EXIT` — 15:30 IST
3. `STALE_ENTRY` — 5 candles + peak < 3
4. `VELOCITY_STALL` — 2 consecutive windows no peak growth
5. `EMA1M_BREAK` — 1-min red + close < 1m EMA9 + pnl ≥ 5
6. `PROFIT_RATCHET` — 5-tier lock (peak ≥10→+2, ≥15→+7, ≥25→+15, ≥35→+25, ≥45→+40)

### Shadow mode (data collection)
- **1-min A/B strategy** — silent comparison of 1-min vs 3-min EMA9
- Per-tick CSV + per-trade summary + EOD Telegram comparison

## Architecture
| File | Role |
|---|---|
| VRL_MAIN.py | Orchestration, state, Telegram, dashboard |
| VRL_ENGINE.py | Entry gates + exit chain + shadow pure functions |
| VRL_DATA.py | Market data, WebSocket, indicators, Greeks |
| VRL_CONFIG.py | YAML config accessors |
| VRL_DB.py | SQLite schema + insert helpers |
| VRL_LAB.py | Data collection (1m/3m/5m/15m candles + scans) |
| VRL_TRADE.py | Order execution (paper + live modes) |
| VRL_COMMANDS.py | Telegram command handlers |
| VRL_SHADOW.py | Silent 1-min A/B strategy |
| VRL_ALERTS.py | Pre-entry learning alerts (4 types) |
| VRL_WEB.py | Dashboard web server |
| VRL_CHARGES.py | Brokerage calculator |
| VRL_AUTH.py | Kite TOTP authentication |

## Deployment
```bash
sudo systemctl restart vrl-main   # trading bot
sudo systemctl restart vrl-web    # dashboard server
```

## Commands
```
/status   — trade status + velocity + stops
/pnl      — P&L with charges
/trades   — today's trade list
/download — full day zip
/validate — 10 system checks
/health   — system health
/alerts_on / /alerts_off — learning alerts toggle
/pause / /resume — entry control
/forceexit — emergency exit
```

## Config
All strategy parameters in `config.yaml`. No code edit needed to tune:
- Entry band thresholds, cooldown, warmup/cutoff times
- Exit SL levels, velocity stall params, BE+2 threshold
- Straddle display lookback, VWAP toggle
- Pre-entry alert rate limits

## Paper mode
Current mode: **PAPER**. All orders simulated at LTP.
Switch to live: set `mode: live` in config.yaml + restart.
