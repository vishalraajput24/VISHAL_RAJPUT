#!/usr/bin/env python3
"""
VRL V8 — Stochastic oscillator entry gate backtest.

Tests various Stoch / StochRSI conditions as additional entry filters
on top of the existing V8 base gates (G1, G2, G3, G5).

Data: multi_day/nifty_option_3min_*.csv + live_20260513/nifty_option_3min_20260513.csv

Sections:
  1. Baseline (no stoch gate)
  2. Best from previous run: StochRSI(14) >50 and StochRSI(5) >50
  3. Oversold-cross results sorted by avg_fwd_3c
  4. Threshold sweep (Stoch k=5, StochRSI p=14 vs thresholds 20/30/40/50)
  5. Clear winner recommendation
"""

import os
import glob
import pandas as pd
import numpy as np

# ── Paths ──────────────────────────────────────────────────────
SCRIPT_DIR  = os.path.dirname(os.path.abspath(__file__))
MULTI_DIR   = os.path.join(SCRIPT_DIR, "multi_day")
LIVE_DIR    = os.path.join(SCRIPT_DIR, "live_20260513")

# ── Indicator helpers ──────────────────────────────────────────

def compute_stoch(df, k_period, d_period=3, smooth_k=3):
    """
    Classic Slow Stochastic.
    Returns columns: slow_%K, slow_%D  (added in-place to df copy).
    """
    low_min  = df["low"].rolling(k_period).min()
    high_max = df["high"].rolling(k_period).max()
    fast_k   = 100.0 * (df["close"] - low_min) / (high_max - low_min + 1e-9)
    slow_k   = fast_k.rolling(smooth_k).mean()
    slow_d   = slow_k.rolling(d_period).mean()
    return slow_k, slow_d


def compute_rsi(series, period=14):
    delta  = series.diff()
    gain   = delta.clip(lower=0)
    loss   = (-delta).clip(lower=0)
    avg_g  = gain.ewm(alpha=1.0/period, min_periods=period, adjust=False).mean()
    avg_l  = loss.ewm(alpha=1.0/period, min_periods=period, adjust=False).mean()
    rs     = avg_g / (avg_l + 1e-9)
    return 100.0 - (100.0 / (1.0 + rs))


def compute_stoch_rsi(df, rsi_period=14, stoch_period=14, smooth_k=3, d_period=3):
    """
    StochRSI: stochastic applied to RSI values.
    Returns smooth_k, d columns.
    """
    rsi      = compute_rsi(df["close"], rsi_period)
    low_min  = rsi.rolling(stoch_period).min()
    high_max = rsi.rolling(stoch_period).max()
    raw_k    = 100.0 * (rsi - low_min) / (high_max - low_min + 1e-9)
    sk       = raw_k.rolling(smooth_k).mean()
    d        = sk.rolling(d_period).mean()
    return sk, d


# ── Data loading ───────────────────────────────────────────────

def load_all_data():
    pattern_multi = os.path.join(MULTI_DIR, "nifty_option_3min_*.csv")
    pattern_live  = os.path.join(LIVE_DIR,  "nifty_option_3min_20260513.csv")

    files = sorted(glob.glob(pattern_multi)) + sorted(glob.glob(pattern_live))
    if not files:
        raise FileNotFoundError(f"No data files found in {MULTI_DIR} or {LIVE_DIR}")

    dfs = []
    for f in files:
        tmp = pd.read_csv(f, parse_dates=["timestamp"])
        dfs.append(tmp)
    df = pd.concat(dfs, ignore_index=True)
    df = df.sort_values("timestamp").reset_index(drop=True)

    # Drop rows that have no forward return (end-of-day stubs)
    df = df.dropna(subset=["fwd_3c", "fwd_6c"])
    return df, files


# ── Base gate filter (G1+G2+G3+G5) ───────────────────────────

def apply_base_gates(df):
    """
    G1: close > open (green candle)
    G2: close > ema9_low
    G3: ema9_high - ema9_low >= 10
    G5: rsi > 50 AND rsi > rsi.shift(1)
    """
    g1 = df["close"] > df["open"]
    g2 = df["close"] > df["ema9_low"]
    g3 = (df["ema9_high"] - df["ema9_low"]) >= 10
    g5a = df["rsi"] > 50
    g5b = df["rsi"] > df["rsi"].shift(1)
    return g1 & g2 & g3 & g5a & g5b


# ── Metrics helper ─────────────────────────────────────────────

def metrics(mask, df, label):
    sub = df[mask]
    n   = len(sub)
    if n == 0:
        return {
            "label": label, "n_signals": 0,
            "avg_fwd_3c": float("nan"), "avg_fwd_6c": float("nan"),
            "win_rate_3c": float("nan"), "win_rate_6c": float("nan"),
        }
    pnl_3  = sub["fwd_3c"] - sub["close"]
    pnl_6  = sub["fwd_6c"] - sub["close"]
    return {
        "label":       label,
        "n_signals":   n,
        "avg_fwd_3c":  round(pnl_3.mean(), 2),
        "avg_fwd_6c":  round(pnl_6.mean(), 2),
        "win_rate_3c": round((pnl_3 > 0).mean() * 100, 1),
        "win_rate_6c": round((pnl_6 > 0).mean() * 100, 1),
    }


# ── Pretty-print table ─────────────────────────────────────────

def print_table(rows, title=None):
    if title:
        print(f"\n{'='*72}")
        print(f"  {title}")
        print(f"{'='*72}")
    if not rows:
        print("  (no rows)")
        return
    headers = ["Label", "N", "avg_fwd_3c", "avg_fwd_6c", "win%_3c", "win%_6c"]
    col_w   = [max(len(h), max(len(str(r[k])) for r in rows))
               for h, k in zip(headers,
                               ["label","n_signals","avg_fwd_3c","avg_fwd_6c",
                                "win_rate_3c","win_rate_6c"])]
    keys    = ["label","n_signals","avg_fwd_3c","avg_fwd_6c","win_rate_3c","win_rate_6c"]
    header_line = "  " + "  ".join(str(h).ljust(col_w[i]) for i, h in enumerate(headers))
    sep_line    = "  " + "  ".join("-"*w for w in col_w)
    print(header_line)
    print(sep_line)
    for r in rows:
        line = "  " + "  ".join(str(r[k]).ljust(col_w[i]) for i, k in enumerate(keys))
        print(line)


# ── Main ───────────────────────────────────────────────────────

def main():
    print("[stoch_backtest] Loading data...")
    df, files = load_all_data()
    print(f"[stoch_backtest] Loaded {len(df):,} rows from {len(files)} files")
    dates = df["timestamp"].dt.date.unique()
    print(f"[stoch_backtest] Dates: {sorted(dates)}")

    # Sort within each (strike, type, date) group so rolling works correctly
    df = df.sort_values(["strike", "type", "timestamp"]).reset_index(drop=True)

    base_mask = apply_base_gates(df)
    print(f"[stoch_backtest] Base gate signals: {base_mask.sum():,} / {len(df):,}\n")

    # ── 1. BASELINE ────────────────────────────────────────────
    baseline = metrics(base_mask, df, "Baseline (no stoch)")

    # ── 2. PREVIOUS BEST: Stoch >50 and StochRSI >50 ──────────
    #    We compute these here so we can display them as reference.
    prev_best_rows = []

    # Stoch k=5 >50
    results_stoch_50 = {}
    for k in [3,4,5,6,7,8,9,10,14]:
        sk, sd = compute_stoch(df, k_period=k)
        mask = base_mask & (sk > 50)
        results_stoch_50[k] = metrics(mask, df, f"Stoch({k})>50")

    # StochRSI >50
    results_srsi_50 = {}
    for p in [5,8,9,14]:
        sk, sd = compute_stoch_rsi(df, rsi_period=p, stoch_period=p)
        mask = base_mask & (sk > 50)
        results_srsi_50[p] = metrics(mask, df, f"StochRSI({p})>50")

    prev_best_rows = [
        results_stoch_50[5],
        results_srsi_50[14],
        results_srsi_50[5],
    ]

    # ── 3. OVERSOLD CROSS ─────────────────────────────────────
    #    Entry: indicator crosses up from oversold (prev <= 20, now > %D)

    oc_rows = []  # all oversold-cross results

    # Stoch oversold cross
    for k in [3,4,5,6,7,8,9,10,14]:
        sk, sd = compute_stoch(df, k_period=k)
        # slow_%K > %D  AND  slow_%K.shift(1) <= 20
        mask = base_mask & (sk > sd) & (sk.shift(1) <= 20)
        oc_rows.append(metrics(mask, df, f"Stoch_OsCross({k})"))

    # StochRSI oversold cross
    for p in [5,8,9,14]:
        sk, sd = compute_stoch_rsi(df, rsi_period=p, stoch_period=p)
        mask = base_mask & (sk > sd) & (sk.shift(1) <= 20)
        oc_rows.append(metrics(mask, df, f"StochRSI_OsCross({p})"))

    # Sort by avg_fwd_3c descending (NaN last)
    oc_rows_sorted = sorted(
        oc_rows,
        key=lambda r: r["avg_fwd_3c"] if not (r["avg_fwd_3c"] != r["avg_fwd_3c"]) else -999,
        reverse=True
    )

    # ── 4. THRESHOLD SWEEP ────────────────────────────────────
    thresh_rows = []

    # Stoch k=5, thresholds 20/30/40/50
    for t in [20, 30, 40, 50]:
        sk, sd = compute_stoch(df, k_period=5)
        mask = base_mask & (sk > t)
        thresh_rows.append(metrics(mask, df, f"Stoch5_thresh{t}"))

    # StochRSI p=14, thresholds 20/30/40/50
    for t in [20, 30, 40, 50]:
        sk, sd = compute_stoch_rsi(df, rsi_period=14, stoch_period=14)
        mask = base_mask & (sk > t)
        thresh_rows.append(metrics(mask, df, f"StochRSI14_thresh{t}"))

    # ── PRINT OUTPUT ──────────────────────────────────────────

    # Section 1: Baseline
    print_table([baseline], title="1. BASELINE (no stoch gate)")

    # Section 2: Previous best
    print_table(prev_best_rows, title="2. PREVIOUS BEST — Stoch/StochRSI > 50")

    # Also print full Stoch >50 sweep for reference
    all_50_rows = list(results_stoch_50.values()) + list(results_srsi_50.values())
    print_table(all_50_rows, title="   (Full >50 sweep for reference)")

    # Section 3: Oversold cross
    print_table(oc_rows_sorted, title="3. OVERSOLD CROSS results (sorted by avg_fwd_3c)")

    # Section 4: Threshold sweep
    print_table(thresh_rows, title="4. THRESHOLD SWEEP — Stoch(k=5) and StochRSI(p=14)")

    # ── 5. RECOMMENDATION ─────────────────────────────────────
    print(f"\n{'='*72}")
    print("  5. WINNER RECOMMENDATION")
    print(f"{'='*72}")

    baseline_3c  = baseline["avg_fwd_3c"]
    baseline_wr  = baseline["win_rate_3c"]
    baseline_n   = baseline["n_signals"]

    # Candidates: must have n_signals >= 20 (at least 5% of baseline or 20 abs)
    min_n = max(20, int(baseline_n * 0.05))

    all_candidates = (
        list(results_stoch_50.values()) +
        list(results_srsi_50.values()) +
        oc_rows +
        thresh_rows
    )
    candidates = [r for r in all_candidates if r["n_signals"] >= min_n]

    if not candidates:
        print("  No candidates with sufficient signal count.")
        return

    # Best by avg_fwd_3c
    best_3c = max(candidates, key=lambda r: r["avg_fwd_3c"])
    # Best by win_rate_3c
    best_wr = max(candidates, key=lambda r: r["win_rate_3c"])
    # Best by avg_fwd_6c
    best_6c = max(candidates, key=lambda r: r["avg_fwd_6c"])

    print(f"\n  Min signal threshold for candidacy: {min_n}")
    print(f"  Qualifying candidates: {len(candidates)}")
    print()
    print(f"  Baseline:              n={baseline_n:4d}  avg_fwd_3c={baseline_3c:+.2f}  win%={baseline_wr:.1f}%")
    print()
    print(f"  Best avg_fwd_3c:       [{best_3c['label']}]")
    print(f"    n={best_3c['n_signals']:4d}  avg_fwd_3c={best_3c['avg_fwd_3c']:+.2f}  "
          f"avg_fwd_6c={best_3c['avg_fwd_6c']:+.2f}  win%_3c={best_3c['win_rate_3c']:.1f}%")
    print(f"    vs baseline: fwd_3c {best_3c['avg_fwd_3c'] - baseline_3c:+.2f} pts  "
          f"win% {best_3c['win_rate_3c'] - baseline_wr:+.1f}pp  "
          f"signals kept {best_3c['n_signals']/baseline_n*100:.0f}%")
    print()
    print(f"  Best win_rate_3c:      [{best_wr['label']}]")
    print(f"    n={best_wr['n_signals']:4d}  avg_fwd_3c={best_wr['avg_fwd_3c']:+.2f}  "
          f"win%_3c={best_wr['win_rate_3c']:.1f}%")
    print()
    print(f"  Best avg_fwd_6c:       [{best_6c['label']}]")
    print(f"    n={best_6c['n_signals']:4d}  avg_fwd_6c={best_6c['avg_fwd_6c']:+.2f}")
    print()

    # Overall recommendation: prefer highest avg_fwd_3c with decent n
    winner = best_3c
    delta_3c = winner["avg_fwd_3c"] - baseline_3c
    delta_wr  = winner["win_rate_3c"] - baseline_wr
    signal_retention = winner["n_signals"] / baseline_n * 100

    print(f"  RECOMMENDATION: Add [{winner['label']}] gate to V8 entry")
    print(f"    avg_fwd_3c improvement : {delta_3c:+.2f} pts over baseline")
    print(f"    win_rate_3c improvement: {delta_wr:+.1f}pp over baseline")
    print(f"    Signal retention       : {signal_retention:.0f}% of baseline signals pass")

    if delta_3c < 1.0:
        print("    NOTE: improvement < 1 pt — marginal edge, collect more data before adding gate")
    elif delta_3c < 3.0:
        print("    NOTE: modest improvement — worth monitoring in shadow mode first")
    else:
        print("    NOTE: strong improvement — candidate for production gate")

    print()


if __name__ == "__main__":
    main()
