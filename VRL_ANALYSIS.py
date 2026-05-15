#!/usr/bin/env python3
"""
VRL_ANALYSIS.py — Strategy analysis on collected parquet data.
Finds which indicators, timeframes, and conditions win.

Usage:
    python3 VRL_ANALYSIS.py
"""

import os, sys, json, warnings
from datetime import date, datetime
from collections import defaultdict

warnings.filterwarnings("ignore")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import numpy as np
import pandas as pd

import VRL_DATA as D

COLLECTOR_DIR = os.path.join(D.LAB_DIR, "collector")
TRADE_LOG     = os.path.join(D.LAB_DIR, "vrl_trade_log.csv")

# ── helpers ─────────────────────────────────────────────────────

def _log(msg): print(msg, flush=True)


def _load_day(day_str: str):
    d = os.path.join(COLLECTOR_DIR, day_str)
    opts  = pd.read_parquet(os.path.join(d, "options_3min.parquet")) if os.path.isfile(os.path.join(d, "options_3min.parquet")) else pd.DataFrame()
    spot  = pd.read_parquet(os.path.join(d, "spot_1min.parquet"))    if os.path.isfile(os.path.join(d, "spot_1min.parquet"))    else pd.DataFrame()
    meta  = json.load(open(os.path.join(d, "meta.json"))) if os.path.isfile(os.path.join(d, "meta.json")) else {}
    return opts, spot, meta


def _candle_hour(ts):
    try: return pd.Timestamp(ts).hour
    except: return -1


def _add_indicators(df):
    if df.empty or len(df) < 5: return df
    df = df.copy()
    df["ema9h"] = df["close"].ewm(span=9).mean()
    df["ema9l"] = df["low"].ewm(span=9).mean()
    df["bw"]    = df["ema9h"] - df["ema9l"]
    # RSI-14
    delta = df["close"].diff()
    gain  = delta.clip(lower=0).rolling(14).mean()
    loss  = (-delta.clip(upper=0)).rolling(14).mean()
    df["rsi"] = 100 - (100 / (1 + gain / loss.replace(0, 1e-9)))
    # StochRSI(5)
    rmin = df["rsi"].rolling(5).min()
    rmax = df["rsi"].rolling(5).max()
    srsi = (df["rsi"] - rmin) / (rmax - rmin + 1e-9) * 100
    df["srsi_k"]    = srsi.rolling(3).mean()
    df["srsi_k_lag"] = df["srsi_k"].shift(1)
    # forward returns
    df["fwd_1c"] = df["close"].shift(-1) - df["close"]
    df["fwd_3c"] = df["close"].shift(-3) - df["close"]
    df["green"]  = df["close"] > df["open"]
    return df


# ══════════════════════════════════════════════════════════════════
# SECTION 1 — TIMEFRAME ANALYSIS
# Which hour of day produces best forward returns after a green candle
# ══════════════════════════════════════════════════════════════════

def analyze_time_of_day(all_opts):
    _log("\n━━━━ 1. TIME OF DAY (green candle → fwd_3c) ━━━━")
    rows = []
    for df in all_opts:
        for ts, row in df[df["green"] == True].iterrows():
            h = pd.Timestamp(ts).hour
            m = pd.Timestamp(ts).minute
            slot = f"{h:02d}:{(m//30)*30:02d}"
            rows.append({"slot": slot, "fwd_3c": row.get("fwd_3c", np.nan)})
    if not rows:
        _log("  no data"); return
    r = pd.DataFrame(rows).dropna()
    g = r.groupby("slot")["fwd_3c"].agg(["mean","count","median"]).round(2)
    g = g[g["count"] >= 30].sort_values("mean", ascending=False)
    _log(g.to_string())
    best = g.head(3).index.tolist()
    worst = g.tail(3).index.tolist()
    _log(f"\n  ✅ BEST slots : {best}")
    _log(f"  ❌ WORST slots: {worst}")


# ══════════════════════════════════════════════════════════════════
# SECTION 2 — BAND WIDTH SWEET SPOT
# ══════════════════════════════════════════════════════════════════

def analyze_band_width(all_opts):
    _log("\n━━━━ 2. BAND WIDTH vs FORWARD RETURN ━━━━")
    rows = []
    for df in all_opts:
        for ts, row in df[df["green"] == True].iterrows():
            bw = row.get("bw", np.nan)
            f3 = row.get("fwd_3c", np.nan)
            if pd.isna(bw) or pd.isna(f3): continue
            bucket = int(bw // 2) * 2      # bucket to 2-pt width
            rows.append({"bw_bucket": bucket, "fwd_3c": f3})
    if not rows: _log("  no data"); return
    r = pd.DataFrame(rows)
    g = r.groupby("bw_bucket")["fwd_3c"].agg(["mean","count"]).round(2)
    g = g[g["count"] >= 20].sort_index()
    _log(g.to_string())


# ══════════════════════════════════════════════════════════════════
# SECTION 3 — RSI CONDITIONS
# ══════════════════════════════════════════════════════════════════

def analyze_rsi(all_opts):
    _log("\n━━━━ 3. RSI CONDITIONS ━━━━")
    rows = []
    for df in all_opts:
        df2 = df[df["green"] == True].copy()
        df2["rsi_prev"] = df2["rsi"].shift(1)
        df2["rsi_rise"] = df2["rsi"] - df2["rsi_prev"]
        df2["rsi_bucket"] = (df2["rsi"] // 10) * 10
        for ts, row in df2.iterrows():
            rows.append({"rsi": row.get("rsi", np.nan),
                         "rsi_rise": row.get("rsi_rise", np.nan),
                         "fwd_3c": row.get("fwd_3c", np.nan),
                         "rsi_bucket": row.get("rsi_bucket", np.nan)})
    if not rows: _log("  no data"); return
    r = pd.DataFrame(rows).dropna()
    g = r.groupby("rsi_bucket")["fwd_3c"].agg(["mean","count"]).round(2)
    g = g[g["count"] >= 20].sort_index()
    _log("RSI bucket → avg fwd_3c:")
    _log(g.to_string())
    # Rising vs flat RSI
    g2 = r.groupby(r["rsi_rise"] >= 2)["fwd_3c"].agg(["mean","count"]).round(2)
    g2.index = ["rsi_flat", "rsi_rising_2+"]
    _log("\nRSI rising ≥2 filter:")
    _log(g2.to_string())


# ══════════════════════════════════════════════════════════════════
# SECTION 4 — STOCHRSI OVERSOLD CROSS
# ══════════════════════════════════════════════════════════════════

def analyze_stochrsi(all_opts):
    _log("\n━━━━ 4. STOCHRSI(5) OVERSOLD CROSS ━━━━")
    rows = []
    for df in all_opts:
        df2 = df[df["green"] == True].copy()
        for ts, row in df2.iterrows():
            k     = row.get("srsi_k", np.nan)
            k_lag = row.get("srsi_k_lag", np.nan)
            f3    = row.get("fwd_3c", np.nan)
            if pd.isna(k) or pd.isna(k_lag) or pd.isna(f3): continue
            os_cross = (k_lag <= 20 and k > k_lag)
            above50  = (k > 50)
            rows.append({"os_cross": os_cross, "above50": above50, "fwd_3c": f3})
    if not rows: _log("  no data"); return
    r = pd.DataFrame(rows)
    g = r.groupby("os_cross")["fwd_3c"].agg(["mean","count","median"]).round(2)
    g.index = ["no_cross", "os_cross"]
    _log("Oversold cross (k≤20 prev → k rising):")
    _log(g.to_string())
    g2 = r.groupby("above50")["fwd_3c"].agg(["mean","count"]).round(2)
    g2.index = ["srsi_below50", "srsi_above50"]
    _log("\nStochRSI > 50:")
    _log(g2.to_string())


# ══════════════════════════════════════════════════════════════════
# SECTION 5 — COMBINED GATE STACK
# Test: G1+G2+G3(bw>=12)+G5(rsi>50,rise>=2)+G6(os_cross)
# vs current G1+G2+G3(bw>=10)+G5
# ══════════════════════════════════════════════════════════════════

def analyze_combined(all_opts):
    _log("\n━━━━ 5. COMBINED GATE STACK ━━━━")
    rows = []
    for df in all_opts:
        df2 = df.copy()
        df2["rsi_prev"] = df2["rsi"].shift(1)
        df2["rsi_rise"] = df2["rsi"] - df2["rsi_prev"]
        for ts, row in df2.iterrows():
            g1  = bool(row.get("green", False))
            bw  = float(row.get("bw", 0) or 0)
            rsi = float(row.get("rsi", 0) or 0)
            rr  = float(row.get("rsi_rise", 0) or 0)
            k   = float(row.get("srsi_k", 50) or 50)
            kl  = float(row.get("srsi_k_lag", 50) or 50)
            f3  = row.get("fwd_3c", np.nan)
            if not g1 or pd.isna(f3): continue
            baseline  = True                              # G1 only
            current   = bw >= 10 and rsi > 50 and rr >= 2
            proposed  = bw >= 12 and rsi > 50 and rr >= 2 and (kl <= 20 and k > kl)
            rows.append({"baseline": baseline,
                         "current":  current,
                         "proposed": proposed,
                         "fwd_3c":   f3})
    if not rows: _log("  no data"); return
    r = pd.DataFrame(rows)
    results = {}
    for col in ("baseline", "current", "proposed"):
        sub = r[r[col] == True]["fwd_3c"]
        win = (sub > 0).sum()
        results[col] = {
            "n":      len(sub),
            "win%":   round(win / len(sub) * 100, 1) if len(sub) else 0,
            "avg":    round(sub.mean(), 2) if len(sub) else 0,
            "median": round(sub.median(), 2) if len(sub) else 0,
        }
    for k, v in results.items():
        _log(f"  {k:12s}: n={v['n']:5d}  win%={v['win%']:5.1f}%  avg={v['avg']:+.2f}  median={v['median']:+.2f}")


# ══════════════════════════════════════════════════════════════════
# SECTION 6 — TIMEFRAME COMPARISON
# Resample 3-min data to 1-min(spot), 5-min, 15-min and compare
# win rate and avg forward return after a green candle
# ══════════════════════════════════════════════════════════════════

def _resample_to(df: pd.DataFrame, rule: str) -> pd.DataFrame:
    """Resample OHLCV dataframe to a coarser timeframe."""
    if df.empty: return df
    agg = {"open": "first", "high": "max", "low": "min",
           "close": "last", "volume": "sum"}
    r = df.resample(rule).agg({k: v for k, v in agg.items() if k in df.columns})
    return r.dropna(subset=["close"])


def analyze_timeframes(all_opts, all_spots):
    _log("\n━━━━ 6. TIMEFRAME COMPARISON ━━━━")
    _log("(green candle → fwd_1c and fwd_3c, filtered to CE/PE options + spot)")

    results = {}

    # Options at 3-min (native)
    for label, frames in [("3min_opts", all_opts)]:
        rows = []
        for df in frames:
            df2 = df[df["green"] == True].copy()
            for _, row in df2.iterrows():
                rows.append({"fwd_1c": row.get("fwd_1c", np.nan),
                             "fwd_3c": row.get("fwd_3c", np.nan)})
        r = pd.DataFrame(rows).dropna()
        results[label] = {
            "n"     : len(r),
            "win1c" : round((r["fwd_1c"] > 0).mean() * 100, 1),
            "avg1c" : round(r["fwd_1c"].mean(), 3),
            "win3c" : round((r["fwd_3c"] > 0).mean() * 100, 1),
            "avg3c" : round(r["fwd_3c"].mean(), 3),
        }

    # Options resampled to 5-min and 15-min
    for tf, rule in [("5min_opts", "5min"), ("15min_opts", "15min")]:
        rows = []
        for df in all_opts:
            for (strike, opt_type), grp in df.groupby(["strike", "opt_type"]):
                r5 = _resample_to(grp, rule)
                if r5.empty: continue
                r5 = _add_indicators(r5)
                for _, row in r5[r5["green"] == True].iterrows():
                    rows.append({"fwd_1c": row.get("fwd_1c", np.nan),
                                 "fwd_3c": row.get("fwd_3c", np.nan)})
        r = pd.DataFrame(rows).dropna()
        results[tf] = {
            "n"     : len(r),
            "win1c" : round((r["fwd_1c"] > 0).mean() * 100, 1),
            "avg1c" : round(r["fwd_1c"].mean(), 3),
            "win3c" : round((r["fwd_3c"] > 0).mean() * 100, 1),
            "avg3c" : round(r["fwd_3c"].mean(), 3),
        }

    # Spot 1-min
    spot_rows = []
    for df in all_spots:
        if df.empty: continue
        df2 = _add_indicators(df)
        for _, row in df2[df2["green"] == True].iterrows():
            spot_rows.append({"fwd_1c": row.get("fwd_1c", np.nan),
                              "fwd_3c": row.get("fwd_3c", np.nan)})
    rs = pd.DataFrame(spot_rows).dropna()
    results["1min_spot"] = {
        "n"     : len(rs),
        "win1c" : round((rs["fwd_1c"] > 0).mean() * 100, 1),
        "avg1c" : round(rs["fwd_1c"].mean(), 3),
        "win3c" : round((rs["fwd_3c"] > 0).mean() * 100, 1),
        "avg3c" : round(rs["fwd_3c"].mean(), 3),
    }

    _log(f"\n{'TF':<14} {'n':>6}  {'win_1c':>7}  {'avg_1c':>7}  {'win_3c':>7}  {'avg_3c':>7}")
    _log("-" * 60)
    for tf, v in results.items():
        _log(f"{tf:<14} {v['n']:>6}  {v['win1c']:>6.1f}%  {v['avg1c']:>+7.3f}  "
             f"{v['win3c']:>6.1f}%  {v['avg3c']:>+7.3f}")

    best = max(results.items(), key=lambda x: x[1]["avg3c"])
    _log(f"\n  ✅ Best avg_3c: {best[0]} ({best[1]['avg3c']:+.3f} pts/candle)")


# ══════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════

def main():
    _log("=== VRL_ANALYSIS ===")
    days = sorted([d for d in os.listdir(COLLECTOR_DIR)
                   if os.path.isdir(os.path.join(COLLECTOR_DIR, d))], reverse=True)
    _log(f"Days available: {len(days)}  ({days[-1]} → {days[0]})")

    all_opts  = []
    all_spots = []
    for day_str in days:
        try:
            opts, spot, meta = _load_day(day_str)
            if not spot.empty:
                all_spots.append(spot.sort_index())
            if opts.empty: continue
            parts = []
            for (strike, opt_type), grp in opts.groupby(["strike","opt_type"]):
                grp2 = _add_indicators(grp.sort_index())
                parts.append(grp2)
            if parts:
                all_opts.append(pd.concat(parts))
        except Exception as e:
            pass

    _log(f"Days with options data: {len(all_opts)}")
    if not all_opts:
        _log("No options data found — run VRL_BACKFILL.py first")
        return

    analyze_time_of_day(all_opts)
    analyze_band_width(all_opts)
    analyze_rsi(all_opts)
    analyze_stochrsi(all_opts)
    analyze_combined(all_opts)
    analyze_timeframes(all_opts, all_spots)

    _log("\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    _log("DONE.")
    _log("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")


if __name__ == "__main__":
    main()

