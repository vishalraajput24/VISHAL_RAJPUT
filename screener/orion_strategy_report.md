# ORION Strategy Backtest — Stock Signal Level

Generated : 2026-06-09 18:25
Lookback  : 58 days  |  Universe: HDFCBANK, ICICIBANK, RELIANCE, SBIN, INFY, TCS
Hard SL   : ±10.0 pts  |  Trail arms at: +15.0 pts

## Overall  —  46/95 (48.4% win rate)
Avg win : 15.90 pts
Avg loss: -8.73 pts
Expectancy: 3.19 pts/trade

## By Engine

| Engine | Signals | Win% | Avg Win | Avg Loss | Expectancy |
|--------|---------|------|---------|----------|------------|
| E1 | 89 | 50.6% | +16.21 | -8.76 | 3.86 |
| E2 | 6 | 16.7% | +1.65 | -8.42 | -6.74 |
| E4 | 0 | — | — | — | — |

## By Stock

| Stock | Signals | Win% | Expectancy |
|-------|---------|------|------------|
| HDFCBANK     |      17 | 64.7% | 2.99 |
| ICICIBANK    |      13 | 69.2% | 1.27 |
| RELIANCE     |      18 | 50.0% | 3.34 |
| SBIN         |      17 | 23.5% | 1.23 |
| INFY         |      13 | 46.2% | 5.05 |
| TCS          |      17 | 41.2% | 5.25 |

## Exit Reasons

| Reason | Count | Win% | Avg PnL |
|--------|-------|------|---------|
| EOD             |     3 | 100.0% | 6.03 |
| FORCE_CLOSE     |    45 | 75.6% | 11.38 |
| SL              |    38 | 0.0% | -10.00 |
| TRAIL_VWAP      |     9 | 100.0% | 17.04 |

## Best 5 Trades
engine   symbol       date direction  entry_price  exit_price  pnl_pts exit_reason
    E1     SBIN 2026-05-08        PE       1081.2       992.1     89.1 FORCE_CLOSE
    E1      TCS 2026-05-18        CE       2278.2      2351.7     73.5 FORCE_CLOSE
    E1      TCS 2026-05-14        CE       2238.3      2299.4     61.1 FORCE_CLOSE
    E1     INFY 2026-04-21        PE       1313.5      1278.6     34.9 FORCE_CLOSE
    E1 RELIANCE 2026-04-30        CE       1407.1      1438.5     31.4  TRAIL_VWAP

## Worst 5 Trades
engine   symbol       date direction  entry_price  exit_price  pnl_pts exit_reason
    E1 HDFCBANK 2026-04-24        PE       781.90      791.90    -10.0          SL
    E1 HDFCBANK 2026-04-30        PE       765.00      775.00    -10.0          SL
    E1 HDFCBANK 2026-05-14        PE       752.30      762.30    -10.0          SL
    E1 HDFCBANK 2026-05-21        CE       766.05      756.05    -10.0          SL
    E2 HDFCBANK 2026-05-20        PE       757.20      767.20    -10.0          SL

---
Hard SL: ±10.0 pts · Trail: +15.0 pts · E3 skipped (cluster undefined)