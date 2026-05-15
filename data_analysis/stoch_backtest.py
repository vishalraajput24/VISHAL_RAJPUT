"""
stoch_backtest.py
=================
Backtest Stochastics and StochRSI parameter values for the V8 3-min NIFTY options entry strategy.

IMPORTANT NOTE ON fwd_3c / fwd_6c:
  These columns are ABSOLUTE future prices (the close price 3 or 6 candles ahead),
  NOT price changes. P&L = fwd_3c - close (price gain from entering at current close).
  win_rate_3c = fraction where fwd_3c > close (confirmed by fwd_outcome column).

Approach:
  - Load all nifty_option_3min_*.csv files (multi_day + live_20260513)
  - Apply V8 base gates G1-G5 as the base filter
  - For each (strike, type) group, compute rolling Stoch / StochRSI indicators
    (rolling windows computed ONLY within each instrument series -- no bleed)
  - For each parameter combo, count signals and compute forward-return metrics
  - Print results table sorted by avg_fwd_3c descending
  - Identify and explain the winner
"""

import glob
import warnings
from pathlib import Path

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")

# ---------------------------------------------------------------------------
# 1. Load data
# ---------------------------------------------------------------------------
DATA_DIR = Path("/home/user/VISHAL_RAJPUT/data_analysis")

files = sorted(glob.glob(str(DATA_DIR / "multi_day" / "nifty_option_3min_*.csv")))
live_file = DATA_DIR / "live_20260513" / "nifty_option_3min_20260513.csv"
if live_file.exists():
    files.append(str(live_file))

print(f"Loading {len(files)} file(s):")
for f in files:
    print(f"  {Path(f).name}")

dfs = [pd.read_csv(f, parse_dates=["timestamp"]) for f in files]
raw = pd.concat(dfs, ignore_index=True)
raw.sort_values(["strike", "type", "timestamp"], inplace=True)
raw.reset_index(drop=True, inplace=True)

print(f"\nTotal rows loaded : {len(raw):,}")
print(f"Date range        : {raw['timestamp'].min().date()} -> {raw['timestamp'].max().date()}")
print(f"Strike/type pairs : {raw.groupby(['strike','type']).ngroups}")
print(f"fwd_3c non-null   : {raw['fwd_3c'].notna().sum():,}")

# fwd_3c/fwd_6c are ABSOLUTE future prices; convert to P&L at load time
raw["pnl_3c"] = raw["fwd_3c"] - raw["close"]   # gain from entry at current close
raw["pnl_6c"] = raw["fwd_6c"] - raw["close"]

print(f"\nNOTE: fwd_3c/fwd_6c are absolute prices. P&L = fwd_Nc - close.")
print(f"  Overall pnl_3c range: {raw['pnl_3c'].min():.1f} .. {raw['pnl_3c'].max():.1f} pts")

# ---------------------------------------------------------------------------
# 2. Compute per-group rolling indicators -- all in one pass
#    (rolling windows strictly per (strike, type) series to avoid cross-bleed)
# ---------------------------------------------------------------------------

STOCH_K_PERIODS   = [3, 4, 5, 6, 7, 8, 9, 10, 14]
STOCH_RSI_PERIODS = [5, 8, 9, 14]
SMOOTH_K = 3   # slow %K smoothing window
D_PERIOD = 3   # %D signal window


def stoch_series(src: pd.Series, lo: pd.Series, hi: pd.Series,
                 smooth: int = 3, d: int = 3):
    """Compute slow-%K and %D from (src, lo, hi).
    Handles divide-by-zero (range == 0) by setting raw %K = 50.
    Returns (slow_k, d_line) as pd.Series with same index as src."""
    rng = hi - lo
    raw_k = np.where(rng == 0, 50.0, (src.values - lo.values) / rng.values * 100)
    raw_k_s = pd.Series(raw_k, index=src.index, dtype=float)
    slow_k  = raw_k_s.rolling(smooth, min_periods=smooth).mean()
    d_line  = slow_k.rolling(d, min_periods=d).mean()
    return slow_k, d_line


group_frames = []
print("\nComputing indicators per (strike, type) group...")

for (strike, opt_type), grp in raw.groupby(["strike", "type"], sort=False):
    grp = grp.sort_values("timestamp").copy()

    # G5 helper: rsi of previous candle within this instrument series only
    grp["rsi_prev"] = grp["rsi"].shift(1)

    # ---- Stochastics on price (close/high/low) ----
    for k in STOCH_K_PERIODS:
        lo_roll = grp["low"].rolling(k,  min_periods=k).min()
        hi_roll = grp["high"].rolling(k, min_periods=k).max()
        sk, dk  = stoch_series(grp["close"], lo_roll, hi_roll, SMOOTH_K, D_PERIOD)
        grp[f"st_sk_{k}"] = sk
        grp[f"st_dk_{k}"] = dk

    # ---- StochRSI: stochastic formula applied to the RSI column ----
    for p in STOCH_RSI_PERIODS:
        rsi_lo = grp["rsi"].rolling(p, min_periods=p).min()
        rsi_hi = grp["rsi"].rolling(p, min_periods=p).max()
        sk, dk  = stoch_series(grp["rsi"], rsi_lo, rsi_hi, SMOOTH_K, D_PERIOD)
        grp[f"sr_sk_{p}"] = sk
        grp[f"sr_dk_{p}"] = dk

    group_frames.append(grp)

df = pd.concat(group_frames, ignore_index=True)
print(f"Done. DataFrame shape: {df.shape}")

# ---------------------------------------------------------------------------
# 3. Apply base V8 gates G1-G5 (G4 skipped -- cross-leg needs joining)
# ---------------------------------------------------------------------------
bw = df["ema9_high"] - df["ema9_low"]

g1 = df["body_pct"] > 0                                  # green candle
g2 = df["close"] > df["ema9_low"]                        # close > lower EMA band
g3 = bw >= 10                                             # band_width >= 10 (real momentum)
g5 = (df["rsi"] > 50) & (df["rsi"] > df["rsi_prev"])     # RSI > 50 AND rising vs prev candle

base_mask = g1 & g2 & g3 & g5 & df["fwd_3c"].notna()

df_base = df[base_mask].copy()
N_base      = len(df_base)
base_avg_3c = df_base["pnl_3c"].mean()
base_avg_6c = df_base["pnl_6c"].mean()
base_wr_3c  = (df_base["pnl_3c"] > 0).mean() * 100
base_wr_6c  = (df_base["pnl_6c"] > 0).mean() * 100

print(f"\n{'='*70}")
print("BASELINE  (G1+G2+G3+G5, no Stoch/StochRSI gate)")
print("  [avg_fwd = mean(fwd_Nc - close), win_rate = % where fwd_Nc > close]")
print(f"{'='*70}")
print(f"  n_signals      : {N_base}")
print(f"  avg_fwd_3c     : {base_avg_3c:+.2f} pts")
print(f"  avg_fwd_6c     : {base_avg_6c:+.2f} pts")
print(f"  win_rate_3c    : {base_wr_3c:.1f}%")
print(f"  win_rate_6c    : {base_wr_6c:.1f}%")
print(f"{'='*70}")

# ---------------------------------------------------------------------------
# 4. Evaluate every parameter combo
# ---------------------------------------------------------------------------
results = []

# ---- Stochastics ----
print(f"\n{'='*70}")
print("STOCHASTICS GRID  (smooth_k=3, d=3)")
print(f"{'='*70}")

for k in STOCH_K_PERIODS:
    sk_col = f"st_sk_{k}"
    dk_col = f"st_dk_{k}"

    # Entry condition: slow_%K > %D  AND  slow_%K > 50
    gate = (df_base[sk_col] > df_base[dk_col]) & (df_base[sk_col] > 50)
    sub  = df_base[gate & df_base[sk_col].notna() & df_base[dk_col].notna()]
    n    = len(sub)

    if n == 0:
        row = dict(indicator="Stoch", param=k, n_signals=0,
                   avg_fwd_3c=np.nan, avg_fwd_6c=np.nan,
                   win_rate_3c=np.nan, win_rate_6c=np.nan,
                   avg_fwd_3c_baseline=base_avg_3c)
    else:
        row = dict(
            indicator="Stoch", param=k, n_signals=n,
            avg_fwd_3c=sub["pnl_3c"].mean(),
            avg_fwd_6c=sub["pnl_6c"].mean(),
            win_rate_3c=(sub["pnl_3c"] > 0).mean() * 100,
            win_rate_6c=(sub["pnl_6c"] > 0).mean() * 100,
            avg_fwd_3c_baseline=base_avg_3c,
        )

    results.append(row)
    avg3 = f"{row['avg_fwd_3c']:+.2f}" if not np.isnan(row["avg_fwd_3c"]) else "  N/A"
    avg6 = f"{row['avg_fwd_6c']:+.2f}" if not np.isnan(row["avg_fwd_6c"]) else "  N/A"
    wr3  = f"{row['win_rate_3c']:.1f}%" if not np.isnan(row["win_rate_3c"]) else "  N/A"
    wr6  = f"{row['win_rate_6c']:.1f}%" if not np.isnan(row["win_rate_6c"]) else "  N/A"
    print(f"  Stoch k={k:2d} | n={n:4d} | avg3c={avg3:>7} | avg6c={avg6:>7} | wr3c={wr3:>6} | wr6c={wr6:>6}")

# ---- StochRSI ----
print(f"\n{'='*70}")
print("STOCHRSI GRID  (smooth_k=3, d=3)")
print(f"{'='*70}")

for p in STOCH_RSI_PERIODS:
    sk_col = f"sr_sk_{p}"
    dk_col = f"sr_dk_{p}"

    gate = (df_base[sk_col] > df_base[dk_col]) & (df_base[sk_col] > 50)
    sub  = df_base[gate & df_base[sk_col].notna() & df_base[dk_col].notna()]
    n    = len(sub)

    if n == 0:
        row = dict(indicator="StochRSI", param=p, n_signals=0,
                   avg_fwd_3c=np.nan, avg_fwd_6c=np.nan,
                   win_rate_3c=np.nan, win_rate_6c=np.nan,
                   avg_fwd_3c_baseline=base_avg_3c)
    else:
        row = dict(
            indicator="StochRSI", param=p, n_signals=n,
            avg_fwd_3c=sub["pnl_3c"].mean(),
            avg_fwd_6c=sub["pnl_6c"].mean(),
            win_rate_3c=(sub["pnl_3c"] > 0).mean() * 100,
            win_rate_6c=(sub["pnl_6c"] > 0).mean() * 100,
            avg_fwd_3c_baseline=base_avg_3c,
        )

    results.append(row)
    avg3 = f"{row['avg_fwd_3c']:+.2f}" if not np.isnan(row["avg_fwd_3c"]) else "  N/A"
    avg6 = f"{row['avg_fwd_6c']:+.2f}" if not np.isnan(row["avg_fwd_6c"]) else "  N/A"
    wr3  = f"{row['win_rate_3c']:.1f}%" if not np.isnan(row["win_rate_3c"]) else "  N/A"
    wr6  = f"{row['win_rate_6c']:.1f}%" if not np.isnan(row["win_rate_6c"]) else "  N/A"
    print(f"  StochRSI p={p:2d} | n={n:4d} | avg3c={avg3:>7} | avg6c={avg6:>7} | wr3c={wr3:>6} | wr6c={wr6:>6}")

# ---------------------------------------------------------------------------
# 5. Full results table (sorted by avg_fwd_3c descending)
# ---------------------------------------------------------------------------
res_df = pd.DataFrame(results)
res_df.sort_values("avg_fwd_3c", ascending=False, inplace=True, na_position="last")
res_df.reset_index(drop=True, inplace=True)

SEP = "=" * 92
DIV = "-" * 92

print(f"\n{SEP}")
print("FULL RESULTS TABLE  --  sorted by avg_fwd_3c (pnl_3c = fwd_3c - close) descending")
print(SEP)
hdr = (f"{'Rank':<5} {'Indicator':<18} {'Param':>5}  {'n_signals':>9}  "
       f"{'avg_fwd_3c':>10}  {'avg_fwd_6c':>10}  {'wr_3c%':>8}  {'wr_6c%':>8}  {'vs_baseline_3c':>14}")
print(hdr)
print(DIV)

for rank, (_, row) in enumerate(res_df.iterrows(), start=1):
    n    = int(row["n_signals"])
    avg3 = f"{row['avg_fwd_3c']:+.2f}" if not pd.isna(row["avg_fwd_3c"]) else "      N/A"
    avg6 = f"{row['avg_fwd_6c']:+.2f}" if not pd.isna(row["avg_fwd_6c"]) else "      N/A"
    wr3  = f"{row['win_rate_3c']:.1f}%" if not pd.isna(row["win_rate_3c"]) else "    N/A"
    wr6  = f"{row['win_rate_6c']:.1f}%" if not pd.isna(row["win_rate_6c"]) else "    N/A"
    vs   = (f"{row['avg_fwd_3c'] - base_avg_3c:+.2f}"
            if not pd.isna(row["avg_fwd_3c"]) else "           N/A")
    label = f"{row['indicator']}(k={int(row['param'])})"
    print(f"{rank:<5} {label:<18}  {int(row['param']):>5}  {n:>9}  {avg3:>10}  {avg6:>10}  "
          f"{wr3:>8}  {wr6:>8}  {vs:>14}")

print(DIV)
# Baseline row
print(f"{'---':<5} {'[BASELINE]':<18}  {'---':>5}  {N_base:>9}  {base_avg_3c:>+10.2f}  {base_avg_6c:>+10.2f}  "
      f"{base_wr_3c:>7.1f}%  {base_wr_6c:>7.1f}%  {'0.00 (ref)':>14}")
print(SEP)

# avg_fwd_3c_baseline (spec requirement)
print(f"\n  avg_fwd_3c_baseline (G1-G5, no stoch gate): {base_avg_3c:+.2f} pts")

# ---------------------------------------------------------------------------
# 6. Winner selection and explanation
# ---------------------------------------------------------------------------
MIN_SIGNALS = 15   # statistical floor

valid = res_df[res_df["n_signals"] >= MIN_SIGNALS].copy()

print(f"\n[Winner selection: must have n_signals >= {MIN_SIGNALS}]")

if valid.empty:
    print("No combo meets the minimum signal threshold. Collect more data.")
else:
    winner = valid.iloc[0]   # top by avg_fwd_3c

    delta3  = winner["avg_fwd_3c"] - base_avg_3c
    delta6  = winner["avg_fwd_6c"] - base_avg_6c
    dwr3    = winner["win_rate_3c"] - base_wr_3c
    retain  = int(winner["n_signals"]) / N_base * 100

    print(f"\n{SEP}")
    print("WINNER")
    print(SEP)
    print(f"  Combo         : {winner['indicator']}  param={int(winner['param'])}")
    print(f"  n_signals     : {int(winner['n_signals'])}  ({retain:.0f}% of baseline signals retained)")
    print(f"  avg_fwd_3c    : {winner['avg_fwd_3c']:+.2f} pts  (baseline {base_avg_3c:+.2f},  delta {delta3:+.2f})")
    print(f"  avg_fwd_6c    : {winner['avg_fwd_6c']:+.2f} pts  (baseline {base_avg_6c:+.2f},  delta {delta6:+.2f})")
    print(f"  win_rate_3c   : {winner['win_rate_3c']:.1f}%  (baseline {base_wr_3c:.1f}%,  delta {dwr3:+.1f} pp)")
    print(f"  win_rate_6c   : {winner['win_rate_6c']:.1f}%  (baseline {base_wr_6c:.1f}%)")
    print(f"\n  WHY IT WINS:")
    print(f"  1. Highest avg_fwd_3c (P&L per signal) of all combos with >= {MIN_SIGNALS} signals.")
    if delta3 > 0:
        print(f"  2. avg_fwd_3c {delta3:+.2f} pts ABOVE baseline -- gate adds positive expected value.")
    else:
        print(f"  2. avg_fwd_3c {delta3:+.2f} pts vs baseline -- gate does not improve avg P&L.")
    if dwr3 > 0:
        print(f"  3. Win rate {dwr3:+.1f} pp higher than baseline -- fewer losing entries.")
    elif dwr3 < 0:
        print(f"  3. Win rate {dwr3:+.1f} pp lower than baseline -- but avg gain per winner compensates.")
    else:
        print(f"  3. Win rate unchanged vs baseline.")
    if retain < 70:
        print(f"  4. Retains {retain:.0f}% of signals -- tighter filter trades quality for quantity.")
    else:
        print(f"  4. Retains {retain:.0f}% of signals -- permissive gate, minimal signal reduction.")
    print(SEP)

    # Best win-rate (may differ)
    bwr_row = valid.loc[valid["win_rate_3c"].idxmax()]
    if not (bwr_row["indicator"] == winner["indicator"] and bwr_row["param"] == winner["param"]):
        print(f"\n  BEST WIN-RATE combo : {bwr_row['indicator']}(k={int(bwr_row['param'])}) "
              f"-> wr3c={bwr_row['win_rate_3c']:.1f}%  avg3c={bwr_row['avg_fwd_3c']:+.2f}  "
              f"n={int(bwr_row['n_signals'])}")

    # Champions per indicator type
    print(f"\n{SEP}")
    print(f"CHAMPION PER INDICATOR TYPE  (n >= {MIN_SIGNALS})")
    print(SEP)
    for ind in ["Stoch", "StochRSI"]:
        sub_v = valid[valid["indicator"] == ind]
        if sub_v.empty:
            print(f"  {ind:<12} : no combo with >= {MIN_SIGNALS} signals")
        else:
            champ = sub_v.iloc[0]
            print(f"  {ind:<12} : param={int(champ['param'])}  "
                  f"avg3c={champ['avg_fwd_3c']:+.2f} pts  "
                  f"wr3c={champ['win_rate_3c']:.1f}%  "
                  f"n={int(champ['n_signals'])}")
    print(SEP)

print("\nDone.")
