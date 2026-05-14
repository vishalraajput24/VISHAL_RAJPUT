"""
TEMP BACKTEST SCRIPT — EMA Band Strategy
Compares OLD gates (RSI + slope) vs NEW gates (pure EMA bands).

NEW LOGIC:
  Entry:  close > ema9_low  (broke above support band)
  XLeg:   other_close < other_ema9_high  (other side rejected at resistance)

Run: python3 backtest_ema_bands.py [YYYY-MM-DD]
     (default: all dates found in options_3min folder)

DELETE after use.
"""

import os, sys, glob, json
from datetime import datetime, date
import pandas as pd
import numpy as np

LAB_DIR        = os.path.expanduser("~/lab_data")
OPTIONS_3M_DIR = os.path.join(LAB_DIR, "options_3min")
TRADE_LOG      = os.path.join(LAB_DIR, "vrl_trade_log.csv")

# ── helpers ────────────────────────────────────────────────────────────────────

def add_indicators(df):
    if df.empty or len(df) < 3:
        return df
    df = df.copy()
    df["EMA_9"]     = df["close"].ewm(span=9,  adjust=False).mean()
    df["EMA_21"]    = df["close"].ewm(span=21, adjust=False).mean()
    delta    = df["close"].diff()
    gain     = delta.clip(lower=0)
    loss     = (-delta).clip(lower=0)
    avg_gain = gain.ewm(com=13, adjust=False).mean()
    avg_loss = loss.ewm(com=13, adjust=False).mean()
    rs       = avg_gain / avg_loss.replace(0, float("nan"))
    df["RSI"]      = (100 - (100 / (1 + rs))).fillna(50)
    df["ema9_high"] = df["high"].ewm(span=9, adjust=False).mean().round(2)
    df["ema9_low"]  = df["low"].ewm(span=9,  adjust=False).mean().round(2)
    return df


def load_day_data(date_str):
    """Load CE and PE candle data for a given date. Returns {token: df}."""
    compact = date_str.replace("-", "")
    pattern = os.path.join(OPTIONS_3M_DIR, f"*{compact}*.csv")
    files   = glob.glob(pattern)
    if not files:
        return {}
    dfs = {}
    for f in files:
        try:
            df = pd.read_csv(f, index_col=0, parse_dates=True)
            df.columns = [c.lower() for c in df.columns]
            if "close" not in df.columns:
                continue
            df = add_indicators(df)
            token = int(os.path.basename(f).split("_")[0]) if os.path.basename(f)[0].isdigit() else None
            if token:
                dfs[token] = df
        except Exception as e:
            pass
    return dfs


def check_old_gates(row, prev_row, prev2_row):
    """OLD logic: green + body20 + close>ema9l + 2-candle slope + RSI>=38 + RSI rise>=2."""
    close  = row["close"];  open_ = row["open"]
    ema9l  = row["ema9_low"]; rsi = row["RSI"]
    if close <= open_:                               return False, "red_candle"
    body   = abs(close - open_) / max(row["high"] - row["low"], 0.01)
    if body < 0.20:                                  return False, "body<20pct"
    if close <= ema9l:                               return False, "close<ema9l"
    slope1 = ema9l - float(prev_row["ema9_low"])
    slope2 = float(prev_row["ema9_low"]) - float(prev2_row["ema9_low"])
    if slope1 < 0 or slope2 < 0:                    return False, f"slope_fall_{round(slope1,2)},{round(slope2,2)}"
    if rsi < 38:                                     return False, f"rsi<38({round(rsi,1)})"
    rsi_rise = rsi - float(prev_row["RSI"])
    if rsi_rise < 2.0:                               return False, f"rsi_rise<2({round(rsi_rise,2)})"
    return True, "ok"


def check_new_gates(row, other_row):
    """NEW logic: green + body20 + close>ema9_low + other_close<other_ema9_high."""
    close  = row["close"];  open_ = row["open"]
    ema9l  = row["ema9_low"]
    if close <= open_:                               return False, "red_candle"
    body   = abs(close - open_) / max(row["high"] - row["low"], 0.01)
    if body < 0.20:                                  return False, "body<20pct"
    if close <= ema9l:                               return False, "close<ema9l"
    # Cross-leg: other side must be rejected at/below its ema9_high
    other_close  = other_row["close"]
    other_ema9h  = other_row["ema9_high"]
    if other_ema9h <= 0:                             return False, "other_ema9h=0"
    if other_close >= other_ema9h:                   return False, f"other_above_ema9h({round(other_close,1)}>={round(other_ema9h,1)})"
    return True, "ok"


# ── main ───────────────────────────────────────────────────────────────────────

def backtest_date(date_str, token_map):
    """
    token_map: dict of {token: df} for the day.
    Match CE/PE pairs by finding tokens where one is CE and one is PE
    for the same strike (inferred from price relationship).
    Returns list of signal dicts.
    """
    # Try to pair CE and PE tokens from the same expiry/strike
    # Use the trade log to find what tokens were used on this date
    tokens = list(token_map.keys())
    if len(tokens) < 2:
        return [], []

    old_signals = []
    new_signals = []

    # Try all CE/PE pairs
    for i, tok_a in enumerate(tokens):
        for tok_b in tokens[i+1:]:
            df_a = token_map[tok_a]
            df_b = token_map[tok_b]
            if df_a.empty or df_b.empty or len(df_a) < 4 or len(df_b) < 4:
                continue

            # Align on common timestamps (closed candles only — skip last)
            common = df_a.index.intersection(df_b.index)
            if len(common) < 4:
                continue

            df_a = df_a.loc[common]
            df_b = df_b.loc[common]

            # Scan each candle (skip first 3 for warmup)
            for idx in range(3, len(common) - 1):
                ts   = common[idx]
                row_a  = df_a.iloc[idx]
                row_b  = df_b.iloc[idx]
                prev_a = df_a.iloc[idx-1]; prev2_a = df_a.iloc[idx-2]
                prev_b = df_b.iloc[idx-1]; prev2_b = df_b.iloc[idx-2]

                # Only scan during trading hours
                hour = ts.hour
                if hour < 9 or (hour == 9 and ts.minute < 18) or hour >= 15:
                    continue

                # OLD gates: scan A as entry, B as xleg
                ok_old_a, _ = check_old_gates(row_a, prev_a, prev2_a)
                if ok_old_a:
                    old_signals.append({"ts": ts, "entry_tok": tok_a, "other_tok": tok_b,
                                        "entry_price": row_a["close"]})

                # OLD gates: scan B as entry, A as xleg
                ok_old_b, _ = check_old_gates(row_b, prev_b, prev2_b)
                if ok_old_b:
                    old_signals.append({"ts": ts, "entry_tok": tok_b, "other_tok": tok_a,
                                        "entry_price": row_b["close"]})

                # NEW gates: A entry, B xleg
                ok_new_a, _ = check_new_gates(row_a, row_b)
                if ok_new_a:
                    new_signals.append({"ts": ts, "entry_tok": tok_a, "other_tok": tok_b,
                                        "entry_price": row_a["close"]})

                # NEW gates: B entry, A xleg
                ok_new_b, _ = check_new_gates(row_b, row_a)
                if ok_new_b:
                    new_signals.append({"ts": ts, "entry_tok": tok_b, "other_tok": tok_a,
                                        "entry_price": row_b["close"]})

    return old_signals, new_signals


def run():
    target_date = sys.argv[1] if len(sys.argv) > 1 else None

    if target_date:
        dates = [target_date]
    else:
        files = glob.glob(os.path.join(OPTIONS_3M_DIR, "*.csv"))
        dates_set = set()
        for f in files:
            base = os.path.basename(f)
            # Try to extract date from filename
            for part in base.replace(".csv","").split("_"):
                if len(part) == 8 and part.isdigit():
                    d = f"{part[:4]}-{part[4:6]}-{part[6:]}"
                    dates_set.add(d)
        dates = sorted(dates_set)

    if not dates:
        print("No candle data found in", OPTIONS_3M_DIR)
        print("Run during/after market hours so data is cached.")
        return

    print(f"\n{'='*60}")
    print(f"EMA BAND BACKTEST — {len(dates)} trading day(s)")
    print(f"{'='*60}")
    print(f"\n{'Gate':<12} {'Signals':>8} {'Days':>6}")
    print(f"{'-'*30}")

    total_old = 0
    total_new = 0

    for d in dates:
        token_map = load_day_data(d)
        if not token_map:
            print(f"  {d}: no data")
            continue
        old_sigs, new_sigs = backtest_date(d, token_map)
        total_old += len(old_sigs)
        total_new += len(new_sigs)
        print(f"  {d}  OLD={len(old_sigs):>4}  NEW={len(new_sigs):>4}  "
              f"{'FEWER' if len(new_sigs) < len(old_sigs) else 'MORE' if len(new_sigs) > len(old_sigs) else 'SAME'}")

    print(f"\n{'─'*40}")
    print(f"  TOTAL OLD signals : {total_old}")
    print(f"  TOTAL NEW signals : {total_new}")
    if total_old > 0:
        pct = round((total_new / total_old) * 100, 1)
        print(f"  NEW is {pct}% of OLD signal count")
        if total_new < total_old:
            print(f"  → NEW filters out {total_old - total_new} signals ({round(100-pct,1)}% reduction)")
        else:
            print(f"  → NEW generates {total_new - total_old} MORE signals")

    print(f"\n{'='*60}")
    print("NOTE: This counts raw candle-level signals, not filtered trades.")
    print("Same-candle guard and cooldowns are NOT applied here.")
    print("A lower NEW count = fewer but cleaner entries.")
    print(f"{'='*60}\n")


if __name__ == "__main__":
    run()
