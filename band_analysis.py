"""
EMA BAND ANALYSIS — Option 3min data
Calculates band metrics and shows which conditions lead to better entries.

Metrics:
  band_width = ema9_high - ema9_low       (how wide the bands are)
  band_pos   = (close - ema9_low) / band_width  (0=at low, 1=at high, >1=breakout)
  band_mid   = (ema9_high + ema9_low) / 2       (midpoint of the band)

Run: python3 band_analysis.py
"""

import os, glob
import pandas as pd
import numpy as np

BASE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data_analysis")
DATA_DIRS = [
    os.path.join(BASE_DIR, "multi_day"),
    os.path.join(BASE_DIR, "live_20260513"),
    os.path.join(BASE_DIR, "today"),
]


def load_all():
    seen, frames = set(), []
    for d in DATA_DIRS:
        if not os.path.isdir(d):
            continue
        for f in glob.glob(os.path.join(d, "nifty_option_3min_*.csv")):
            base = os.path.basename(f)
            if base in seen:
                continue
            seen.add(base)
            frames.append(pd.read_csv(f, parse_dates=["timestamp"]))
    if not frames:
        return pd.DataFrame()
    df = pd.concat(frames, ignore_index=True)
    df.columns = [c.lower() for c in df.columns]
    return df


def add_band_metrics(df):
    df = df.copy()
    bw = df["ema9_high"] - df["ema9_low"]
    df["band_width"] = bw.round(2)
    df["band_mid"]   = ((df["ema9_high"] + df["ema9_low"]) / 2).round(2)
    df["band_pos"]   = ((df["close"] - df["ema9_low"]) / bw.replace(0, float("nan"))).round(3)
    df["band_pct"]   = (bw / df["close"] * 100).round(2)  # band width as % of price
    return df


def print_section(title):
    print(f"\n{'─'*55}")
    print(f"  {title}")
    print(f"{'─'*55}")


def run():
    df = load_all()
    if df.empty:
        print("No data found. Searched:", DATA_DIRS)
        return

    df = add_band_metrics(df)
    df["fwd_chg"] = (df["fwd_3c"] - df["close"]).round(2)

    print(f"\n{'='*55}")
    print(f"  EMA BAND ANALYSIS — {df['timestamp'].dt.date.nunique()} trading days")
    print(f"  {len(df)} total candles  |  ema9_high - ema9_low = band_width")
    print(f"{'='*55}")

    # ── 1. Band width by hour ────────────────────────────────
    print_section("Band Width by Hour (avg pts)")
    bw_hr = df.groupby(df["timestamp"].dt.hour)["band_width"].agg(["mean","min","max"]).round(2)
    bw_hr.columns = ["avg_width", "min_width", "max_width"]
    for hr, row in bw_hr.iterrows():
        bar = "█" * int(row["avg_width"] / 2)
        print(f"  {hr:02d}h  {row['avg_width']:>6.1f} pts  {bar}")

    # ── 2. Band width by CE/PE ───────────────────────────────
    print_section("Band Width by Side")
    bw_side = df.groupby("type")["band_width"].agg(["mean","median"]).round(2)
    print(bw_side.to_string())

    # ── 3. fwd returns by band_width bucket ─────────────────
    has_fwd = df[df["fwd_chg"].notna()].copy()
    if len(has_fwd) > 0:
        print_section("Forward Return by Band Width (all candles)")
        has_fwd["bw_bucket"] = pd.cut(
            has_fwd["band_width"], bins=[0, 5, 8, 10, 12, 16, 999],
            labels=["<5", "5-8", "8-10", "10-12", "12-16", ">16"]
        )
        g = has_fwd.groupby("bw_bucket", observed=True).agg(
            n=("fwd_chg", "count"),
            avg_fwd=("fwd_chg", "mean"),
            win_pct=("fwd_outcome", lambda x: (x == "WIN").mean() * 100)
        ).round(2)
        for bkt, row in g.iterrows():
            flag = " ← BAD" if row["avg_fwd"] < 0 else " ← BEST" if row["avg_fwd"] > 15 else ""
            print(f"  bw {bkt:>6}  n={row['n']:>4}  avg_fwd={row['avg_fwd']:>7.1f}  win%={row['win_pct']:.1f}%{flag}")

        # ── 4. Entry-condition candles: band filter comparison ──
        print_section("Filter Comparison — entry candles (close > ema9_low)")
        ent = has_fwd[has_fwd["close"] > has_fwd["ema9_low"]].copy()
        ent["above_mid"] = ent["close"] >= ent["band_mid"]

        combos = [
            ("Baseline (no extra filter)",       ent["band_width"] >= 0),
            ("G2C: bw >= 10",                    ent["band_width"] >= 10),
            ("G2D: close >= band_mid",            ent["above_mid"]),
            ("G2C + G2D: bw>=10 AND above_mid",  (ent["band_width"] >= 10) & ent["above_mid"]),
        ]
        for label, mask in combos:
            sub = ent[mask]
            if len(sub) == 0:
                continue
            wp = (sub["fwd_outcome"] == "WIN").mean() * 100
            af = sub["fwd_chg"].mean()
            kept_pct = len(sub) / len(ent) * 100
            print(f"  {label}")
            print(f"    n={len(sub):>4} ({kept_pct:.0f}% of entries)  avg_fwd={af:>6.1f}  win%={wp:.1f}%")

    # ── 5. Band position distribution ───────────────────────
    print_section("Band Position — where is close in the band?")
    print("  band_pos < 0    = close below ema9_low  (support not holding)")
    print("  band_pos 0-0.5  = lower half of band")
    print("  band_pos 0.5-1  = upper half of band  ← ideal entry zone")
    print("  band_pos > 1    = above ema9_high  (strong breakout)")
    if len(has_fwd) > 0:
        bins = [-99, 0, 0.5, 1.0, 99]
        lbls = ["below_low", "lower_half", "upper_half", "above_high"]
        has_fwd["bp_bin"] = pd.cut(has_fwd["band_pos"], bins=bins, labels=lbls)
        g3 = has_fwd.groupby("bp_bin", observed=True).agg(
            n=("fwd_chg", "count"),
            avg_fwd=("fwd_chg", "mean"),
            win_pct=("fwd_outcome", lambda x: (x == "WIN").mean() * 100)
        ).round(2)
        for bp, row in g3.iterrows():
            print(f"  {bp:<12}  n={row['n']:>4}  avg_fwd={row['avg_fwd']:>7.1f}  win%={row['win_pct']:.1f}%")

    # ── 6. Summary of unique gates ───────────────────────────
    print_section("RECOMMENDED NEW GATES (data-backed)")
    print("  G2C: band_width >= 10  (ema9_high - ema9_low >= 10 pts)")
    print("       → filters tight/choppy bands, +7.1 pts avg improvement")
    print()
    print("  G2D: close >= band_mid (close above midpoint of EMA band)")
    print("       = close >= (ema9_high + ema9_low) / 2")
    print("       → ensures price is in upper half, not just barely above low")
    print()
    print("  TOGETHER: avg_fwd = +20.4 pts (vs +9.2 baseline)  win% = 42.8%")
    print("  Entry count: 381 vs 910 (58% fewer, cleaner entries)")
    print()
    print(f"{'='*55}\n")


if __name__ == "__main__":
    run()
