# ORION Strategy Backtest — Stock Signal Level

Generated : 2026-06-09 17:52
Lookback  : 58 days  |  Universe: HDFCBANK, ICICIBANK, RELIANCE, SBIN, INFY, TCS
Hard SL   : ±10.0 pts  |  Trail arms at: +15.0 pts

## Overall  —  179/371 (48.2% win rate)
Avg win : 14.70 pts
Avg loss: -8.76 pts
Expectancy: 2.56 pts/trade

## By Engine

| Engine | Signals | Win% | Avg Win | Avg Loss | Expectancy |
|--------|---------|------|---------|----------|------------|
| E1 | 365 | 48.8% | +14.77 | -8.77 | 2.71 |
| E2 | 6 | 16.7% | +1.65 | -8.42 | -6.74 |
| E4 | 0 | — | — | — | — |

## By Stock

| Stock | Signals | Win% | Expectancy |
|-------|---------|------|------------|
| HDFCBANK     |      79 | 60.8% | 3.24 |
| ICICIBANK    |      54 | 50.0% | 1.09 |
| RELIANCE     |      60 | 40.0% | -0.20 |
| SBIN         |      73 | 45.2% | 2.57 |
| INFY         |      58 | 48.3% | 4.65 |
| TCS          |      47 | 40.4% | 4.04 |

## Exit Reasons

| Reason | Count | Win% | Avg PnL |
|--------|-------|------|---------|
| EOD             |     6 | 66.7% | 2.33 |
| FORCE_CLOSE     |   190 | 80.0% | 10.64 |
| SL              |   151 | 0.0% | -10.00 |
| TRAIL_VWAP      |    24 | 95.8% | 17.67 |

## Best 5 Trades
engine symbol       date direction  entry_price  exit_price  pnl_pts exit_reason
    E1   SBIN 2026-05-08        PE       1087.4       992.1     95.3 FORCE_CLOSE
    E1   SBIN 2026-05-08        PE       1081.2       992.1     89.1 FORCE_CLOSE
    E1    TCS 2026-05-18        CE       2266.7      2351.7     85.0 FORCE_CLOSE
    E1    TCS 2026-05-18        CE       2278.2      2351.7     73.5 FORCE_CLOSE
    E1    TCS 2026-05-14        CE       2238.3      2299.4     61.1 FORCE_CLOSE

## Worst 5 Trades
engine   symbol       date direction  entry_price  exit_price  pnl_pts exit_reason
    E1 HDFCBANK 2026-04-13        PE        789.8       799.8    -10.0          SL
    E1 HDFCBANK 2026-04-15        PE        808.8       818.8    -10.0          SL
    E1 HDFCBANK 2026-04-22        CE        805.8       795.8    -10.0          SL
    E1 HDFCBANK 2026-04-24        PE        782.7       792.7    -10.0          SL
    E1 HDFCBANK 2026-04-24        PE        781.9       791.9    -10.0          SL

---
Hard SL: ±10.0 pts · Trail: +15.0 pts · E3 skipped (cluster undefined)