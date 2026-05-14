"""
TEMP BACKTEST SCRIPT — EMA Band Strategy
Compares OLD gates (RSI + slope) vs NEW gates (pure EMA bands).

NEW LOGIC:
  Entry:  close > ema9_low  (broke above support band)
  XLeg:   other_close < other_ema9_high  (other side rejected at resistance)

Run: python3 backtest_ema_bands.py [YYYY-MM-DD]
     (default: all dates found in data_analysis folders)

DELETE after use.
"""

import os, sys, glob
import pandas as pd

BASE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data_analysis")
DATA_DIRS = [
    os.path.join(BASE_DIR, "multi_day"),
    os.path.join(BASE_DIR, "live_20260513"),
    os.path.join(BASE_DIR, "today"),
]

# ── helpers ────────────────────────────────────────────────────────────────────

def find_option_files(target_date=None):
    seen = set()
    files = []
    for d in DATA_DIRS:
        if not os.path.isdir(d):
            continue
        for f in glob.glob(os.path.join(d, "nifty_option_3min_*.csv")):
            base = os.path.basename(f)
            if base in seen:
                continue
            seen.add(base)
            if target_date:
                compact = target_date.replace("-", "")
                if compact not in base:
                    continue
            files.append(f)
    return sorted(files)


def check_old_gates(row, prev_row, prev2_row):
    """OLD: green + body>=20% + close>ema9l + 2-candle slope + RSI>=38 + RSI rise>=2."""
    close, open_ = row["close"], row["open"]
    ema9l, rsi = row["ema9_low"], row["rsi"]

    if close <= open_:
        return False
    body = abs(close - open_) / max(row["high"] - row["low"], 0.01)
    if body < 0.20:
        return False
    if close <= ema9l:
        return False
    slope1 = ema9l - prev_row["ema9_low"]
    slope2 = prev_row["ema9_low"] - prev2_row["ema9_low"]
    if slope1 < 0 or slope2 < 0:
        return False
    if rsi < 38:
        return False
    if rsi - prev_row["rsi"] < 2.0:
        return False
    return True


def check_new_gates(row, other_row):
    """NEW: green + body>=20% + close>ema9_low + other_close<other_ema9_high."""
    close, open_ = row["close"], row["open"]
    ema9l = row["ema9_low"]

    if close <= open_:
        return False
    body = abs(close - open_) / max(row["high"] - row["low"], 0.01)
    if body < 0.20:
        return False
    if close <= ema9l:
        return False
    other_ema9h = other_row["ema9_high"]
    if other_ema9h <= 0:
        return False
    if other_row["close"] >= other_ema9h:
        return False
    return True


def check_hybrid_gates(row, prev_row, prev2_row, other_row):
    """HYBRID: green + body>=20% + close>ema9l + 2-candle slope + other_close<other_ema9_high.
    Keeps slope (removes bad breakouts), drops RSI, uses EMA band xLeg."""
    close, open_ = row["close"], row["open"]
    ema9l = row["ema9_low"]

    if close <= open_:
        return False
    body = abs(close - open_) / max(row["high"] - row["low"], 0.01)
    if body < 0.20:
        return False
    if close <= ema9l:
        return False
    slope1 = ema9l - prev_row["ema9_low"]
    slope2 = prev_row["ema9_low"] - prev2_row["ema9_low"]
    if slope1 < 0 or slope2 < 0:
        return False
    other_ema9h = other_row["ema9_high"]
    if other_ema9h <= 0:
        return False
    if other_row["close"] >= other_ema9h:
        return False
    return True


def backtest_file(filepath):
    df = pd.read_csv(filepath, parse_dates=["timestamp"])
    df.columns = [c.lower() for c in df.columns]

    old_signals, new_signals, hyb_signals = [], [], []

    for strike in sorted(df["strike"].unique()):
        sdf = df[df["strike"] == strike]
        ce = sdf[sdf["type"] == "CE"].sort_values("timestamp").reset_index(drop=True)
        pe = sdf[sdf["type"] == "PE"].sort_values("timestamp").reset_index(drop=True)

        if len(ce) < 4 or len(pe) < 4:
            continue

        common_ts = set(ce["timestamp"]).intersection(set(pe["timestamp"]))
        if len(common_ts) < 4:
            continue

        ce = ce[ce["timestamp"].isin(common_ts)].sort_values("timestamp").reset_index(drop=True)
        pe = pe[pe["timestamp"].isin(common_ts)].sort_values("timestamp").reset_index(drop=True)

        for idx in range(3, len(ce)):
            ts = ce.iloc[idx]["timestamp"]
            hour = ts.hour
            if hour < 9 or (hour == 9 and ts.minute < 18) or hour >= 15:
                continue

            row_ce, row_pe = ce.iloc[idx], pe.iloc[idx]
            prev_ce, prev2_ce = ce.iloc[idx - 1], ce.iloc[idx - 2]
            prev_pe, prev2_pe = pe.iloc[idx - 1], pe.iloc[idx - 2]

            if check_old_gates(row_ce, prev_ce, prev2_ce):
                old_signals.append({"ts": ts, "entry": "CE", "strike": strike, "price": row_ce["close"]})
            if check_old_gates(row_pe, prev_pe, prev2_pe):
                old_signals.append({"ts": ts, "entry": "PE", "strike": strike, "price": row_pe["close"]})

            if check_new_gates(row_ce, row_pe):
                new_signals.append({"ts": ts, "entry": "CE", "strike": strike, "price": row_ce["close"]})
            if check_new_gates(row_pe, row_ce):
                new_signals.append({"ts": ts, "entry": "PE", "strike": strike, "price": row_pe["close"]})

            if check_hybrid_gates(row_ce, prev_ce, prev2_ce, row_pe):
                hyb_signals.append({"ts": ts, "entry": "CE", "strike": strike, "price": row_ce["close"]})
            if check_hybrid_gates(row_pe, prev_pe, prev2_pe, row_ce):
                hyb_signals.append({"ts": ts, "entry": "PE", "strike": strike, "price": row_pe["close"]})

    return old_signals, new_signals, hyb_signals


# ── main ───────────────────────────────────────────────────────────────────────

def run():
    target_date = sys.argv[1] if len(sys.argv) > 1 else None
    files = find_option_files(target_date)

    if not files:
        print("No option 3min CSV files found.")
        print("Searched:", DATA_DIRS)
        return

    print(f"\n{'='*60}")
    print(f"EMA BAND BACKTEST — {len(files)} trading day(s)")
    print(f"{'='*60}")

    total_old, total_new, total_hyb = 0, 0, 0

    for f in files:
        date_str = os.path.basename(f).replace("nifty_option_3min_", "").replace(".csv", "")
        old_sigs, new_sigs, hyb_sigs = backtest_file(f)
        total_old += len(old_sigs)
        total_new += len(new_sigs)
        total_hyb += len(hyb_sigs)
        print(f"  {date_str}  OLD={len(old_sigs):>4}  HYBRID={len(hyb_sigs):>4}  PURE_EMA={len(new_sigs):>4}")

        if hyb_sigs:
            for s in hyb_sigs[:3]:
                print(f"    HYB -> {s['ts'].strftime('%H:%M')} {s['entry']} {s['strike']} @ {s['price']}")

    print(f"\n{'─'*50}")
    print(f"  TOTAL OLD    (slope+RSI+xleg_0.5) : {total_old}")
    print(f"  TOTAL HYBRID (slope+ema9h_xleg)   : {total_hyb}")
    print(f"  TOTAL PURE   (ema9l+ema9h_xleg)   : {total_new}")
    if total_old > 0:
        hyb_pct = round((total_hyb / total_old) * 100, 1)
        new_pct = round((total_new / total_old) * 100, 1)
        print(f"\n  HYBRID is {hyb_pct}% of OLD count  ({total_hyb - total_old:+d} signals)")
        print(f"  PURE   is {new_pct}% of OLD count  ({total_new - total_old:+d} signals)")

    print(f"\n{'='*60}")
    print("NOTE: Raw candle signals. No same-candle guard or cooldowns applied.")
    print("A lower NEW count = fewer but cleaner entries.")
    print(f"{'='*60}\n")


if __name__ == "__main__":
    run()
