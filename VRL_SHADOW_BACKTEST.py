#!/usr/bin/env python3
"""
VRL_SHADOW_BACKTEST.py — Shadow-DTF (1-min + 3-min) strategy backtest

Replays collected parquet data through Shadow-DTF gate logic:
  1-min gate: close > EMA9_HIGH  +  RSI 48-70 rising
  3-min gate: close > EMA9_LOW   +  RSI 48-70 rising
  Both must pass simultaneously → ENTRY

Exit ladder (same as V9 live):
  Peak <  12 → SL = entry - 12  (emergency)
  Peak >= 12 → SL = entry + 4
  Peak >= 24 → SL = entry + 12
  Peak >= 30 → SL = entry + 20
  Peak >= 36 → SL = entry + 30
  Peak >= 40 → SL = entry + 36
  Peak >= 50 → SL = entry + 50

Usage:
    python VRL_SHADOW_BACKTEST.py                        # all expiry weeks found
    python VRL_SHADOW_BACKTEST.py --expiry 20260519      # specific expiry
    python VRL_SHADOW_BACKTEST.py --from 2026-04-15      # date range
    python VRL_SHADOW_BACKTEST.py --verbose              # per-trade detail
    python VRL_SHADOW_BACKTEST.py --compare              # vs V9 on same days
"""

import argparse
import glob
import os
import sys
from datetime import date, datetime, timedelta

import pandas as pd

# ── Config ────────────────────────────────────────────────────────────────────
RSI_LO   = 48.0
RSI_HI   = 70.0
BW_MIN   = 13.0   # band width gate (matches V9 live)
BW_MAX   = 16.0
COOLDOWN = 3      # minutes between signals on same side
WARMUP   = "09:45"  # no entries before this time

LAB_DIR = os.path.join(os.path.expanduser("~"), "lab_data")


# ── Exit ladder ───────────────────────────────────────────────────────────────
def _compute_sl(entry: float, peak_pts: float) -> float:
    if peak_pts >= 50: return entry + 50
    if peak_pts >= 40: return entry + 36
    if peak_pts >= 36: return entry + 30
    if peak_pts >= 30: return entry + 20
    if peak_pts >= 24: return entry + 12
    if peak_pts >= 12: return entry + 4
    return entry - 12


# ── Indicator helpers (applied to loaded dataframes) ─────────────────────────
def _add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """Add EMA9_high, EMA9_low, RSI if not present."""
    if df.empty:
        return df
    if "RSI" not in df.columns:
        delta = df["close"].diff()
        gain  = delta.clip(lower=0).rolling(14).mean()
        loss  = (-delta.clip(upper=0)).rolling(14).mean()
        rs    = gain / loss.replace(0, 1e-9)
        df    = df.copy()
        df["RSI"] = 100 - 100 / (1 + rs)
    if "ema9_high" not in df.columns:
        df = df.copy()
        df["ema9_high"] = df["high"].ewm(span=9, adjust=False).mean()
        df["ema9_low"]  = df["low"].ewm(span=9, adjust=False).mean()
    return df


# ── Load parquet helpers ──────────────────────────────────────────────────────
def _load_parquet(path: str, strike: int, opt_type: str) -> pd.DataFrame:
    """Load one parquet file and return rows for given strike+opt_type."""
    try:
        df = pd.read_parquet(path)
        df = df[(df["strike"] == strike) & (df["opt_type"] == opt_type)].copy()
        if df.index.tz is not None:
            df.index = df.index.tz_localize(None)
        df = df.sort_index()
        df = _add_indicators(df)
        return df
    except Exception as e:
        return pd.DataFrame()


def _resample_to_3min(df_1m: pd.DataFrame) -> pd.DataFrame:
    """Resample 1-min OHLCV to 3-min (fallback when 3-min file missing)."""
    if df_1m.empty:
        return df_1m
    df = df_1m[["open", "high", "low", "close", "volume"]].resample("3min").agg({
        "open": "first", "high": "max", "low": "min",
        "close": "last",  "volume": "sum"
    }).dropna()
    return _add_indicators(df)


# ── Single-day replay ─────────────────────────────────────────────────────────
def _run_day(day_str: str, path_1m: str, path_3m: str,
             strike: int, opt_type: str,
             rsi_lo=RSI_LO, rsi_hi=RSI_HI,
             bw_min=BW_MIN, bw_max=BW_MAX,
             verbose=False) -> list:
    """
    Replay Shadow-DTF for one day, one strike, one direction.
    Returns list of trade dicts.
    """
    df_1m = _load_parquet(path_1m, strike, opt_type)
    df_3m = _load_parquet(path_3m, strike, opt_type) if path_3m else pd.DataFrame()

    # Fallback: resample 1-min → 3-min if 3-min file missing
    if df_3m.empty and not df_1m.empty:
        df_3m = _resample_to_3min(df_1m)

    if df_1m.empty or df_3m.empty:
        return []

    warmup_dt  = datetime.strptime(day_str + " " + WARMUP, "%Y-%m-%d %H:%M")
    cutoff_dt  = datetime.strptime(day_str + " 15:15",      "%Y-%m-%d %H:%M")

    trades      = []
    in_trade    = False
    entry_price = 0.0
    peak_price  = 0.0
    peak_pts    = 0.0
    sl          = 0.0
    entry_time  = None
    last_entry_ts = {}   # opt_type → last entry datetime (cooldown)

    # Walk 1-min candles
    for ts, row in df_1m.iterrows():
        ts_dt = pd.Timestamp(ts).to_pydatetime()
        if ts_dt < warmup_dt or ts_dt > cutoff_dt:
            continue

        # ── If in trade: update SL, check exit ──
        if in_trade:
            ltp = float(row["close"])
            if ltp > peak_price:
                peak_price = ltp
                peak_pts   = round(peak_price - entry_price, 2)
            sl = _compute_sl(entry_price, peak_pts)
            if ltp <= sl:
                pnl    = round(ltp - entry_price, 2)
                reason = "ESL" if pnl < 0 else "TRAIL"
                trades.append({
                    "date": day_str, "direction": opt_type, "strike": strike,
                    "entry_time": entry_time.strftime("%H:%M:%S"),
                    "exit_time":  ts_dt.strftime("%H:%M:%S"),
                    "entry": round(entry_price, 2),
                    "exit":  round(ltp, 2),
                    "pnl":   pnl,
                    "peak":  round(peak_pts, 2),
                    "reason": reason,
                    "hold_min": int((ts_dt - entry_time).total_seconds() // 60),
                })
                if verbose:
                    print(f"  EXIT  {opt_type} {strike} {ts_dt.strftime('%H:%M')} "
                          f"exit={ltp:.1f} pnl={pnl:+.1f} peak={peak_pts:+.1f} {reason}")
                in_trade = False
            continue

        # ── EOD forced exit ──
        if ts_dt >= cutoff_dt and in_trade:
            ltp = float(row["close"])
            pnl = round(ltp - entry_price, 2)
            trades.append({
                "date": day_str, "direction": opt_type, "strike": strike,
                "entry_time": entry_time.strftime("%H:%M:%S"),
                "exit_time":  ts_dt.strftime("%H:%M:%S"),
                "entry": round(entry_price, 2),
                "exit":  round(ltp, 2),
                "pnl":   pnl,
                "peak":  round(peak_pts, 2),
                "reason": "EOD",
                "hold_min": int((ts_dt - entry_time).total_seconds() // 60),
            })
            in_trade = False
            continue

        # ── Cooldown check ──
        last_ts = last_entry_ts.get(opt_type)
        if last_ts and (ts_dt - last_ts).total_seconds() < COOLDOWN * 60:
            continue

        # ── 1-min gate ──
        ema9h_1m = float(row.get("ema9_high", 0) or 0)
        close_1m = float(row["close"])
        rsi_1m   = float(row.get("RSI", 0) or 0)

        # Need prev row for RSI rising check
        idx_loc = df_1m.index.get_loc(ts)
        if idx_loc < 1:
            continue
        rsi_1m_p = float(df_1m.iloc[idx_loc - 1].get("RSI", 0) or 0)

        if not (ema9h_1m > 0 and close_1m > ema9h_1m):
            continue
        if not (rsi_lo < rsi_1m < rsi_hi and rsi_1m > rsi_1m_p):
            continue

        # ── 3-min gate: find latest completed 3-min candle ≤ ts ──
        df_3m_past = df_3m[df_3m.index <= ts]
        if len(df_3m_past) < 2:
            continue
        last_3m  = df_3m_past.iloc[-1]
        prev_3m  = df_3m_past.iloc[-2]

        ema9l_3m = float(last_3m.get("ema9_low", 0) or 0)
        close_3m = float(last_3m["close"])
        rsi_3m   = float(last_3m.get("RSI", 0) or 0)
        rsi_3m_p = float(prev_3m.get("RSI", 0) or 0)
        bw_3m    = round(float(last_3m.get("ema9_high", 0) or 0) - ema9l_3m, 2)

        if not (close_3m > ema9l_3m):
            continue
        if not (rsi_lo < rsi_3m < rsi_hi and rsi_3m > rsi_3m_p):
            continue
        if bw_min > 0 and not (bw_min <= bw_3m <= bw_max):
            continue

        # ── FIRE ──
        entry_price = close_1m
        peak_price  = close_1m
        peak_pts    = 0.0
        sl          = entry_price - 12
        entry_time  = ts_dt
        last_entry_ts[opt_type] = ts_dt
        in_trade    = True

        if verbose:
            print(f"  ENTRY {opt_type} {strike} {ts_dt.strftime('%H:%M')} "
                  f"entry={entry_price:.1f} "
                  f"1m: close={close_1m:.1f}>ema9h={ema9h_1m:.1f} rsi={rsi_1m:.1f}↑  "
                  f"3m: close={close_3m:.1f}>ema9l={ema9l_3m:.1f} bw={bw_3m:.1f} rsi={rsi_3m:.1f}↑")

    # Force EOD exit if still in trade at end of day
    if in_trade and not df_1m.empty:
        last_row   = df_1m.iloc[-1]
        ltp        = float(last_row["close"])
        pnl        = round(ltp - entry_price, 2)
        last_ts_dt = pd.Timestamp(df_1m.index[-1]).to_pydatetime()
        trades.append({
            "date": day_str, "direction": opt_type, "strike": strike,
            "entry_time": entry_time.strftime("%H:%M:%S"),
            "exit_time":  last_ts_dt.strftime("%H:%M:%S"),
            "entry": round(entry_price, 2),
            "exit":  round(ltp, 2),
            "pnl":   pnl,
            "peak":  round(peak_pts, 2),
            "reason": "EOD",
            "hold_min": int((last_ts_dt - entry_time).total_seconds() // 60),
        })

    return trades


# ── Summary ───────────────────────────────────────────────────────────────────
def _summarise(trades: list, label: str = ""):
    if not trades:
        print(f"\n{label}  No trades found.")
        return {}
    df = pd.DataFrame(trades)
    n         = len(df)
    wins      = (df["pnl"] > 0).sum()
    losses    = (df["pnl"] <= 0).sum()
    wr        = round(wins / n * 100, 1)
    net       = round(df["pnl"].sum(), 1)
    avg       = round(df["pnl"].mean(), 2)
    avg_win   = round(df[df["pnl"] > 0]["pnl"].mean(), 2) if wins else 0
    avg_loss  = round(df[df["pnl"] <= 0]["pnl"].mean(), 2) if losses else 0
    avg_peak  = round(df["peak"].mean(), 2)
    esl_pct   = round((df["reason"] == "ESL").sum() / n * 100, 1)
    days      = df["date"].nunique()

    hdr = f"\n{'─'*60}\n{label or 'SHADOW-DTF BACKTEST RESULTS'}\n{'─'*60}"
    print(hdr)
    print(f"  Days       : {days}")
    print(f"  Signals    : {n}  ({round(n/days,1)}/day)")
    print(f"  Win rate   : {wins}W / {losses}L = {wr}%")
    print(f"  Net PnL    : {net:+.1f} pts")
    print(f"  Avg/trade  : {avg:+.2f} pts")
    print(f"  Avg win    : {avg_win:+.2f} pts")
    print(f"  Avg loss   : {avg_loss:+.2f} pts")
    print(f"  Avg peak   : {avg_peak:.2f} pts")
    print(f"  ESL rate   : {esl_pct}%")
    print(f"{'─'*60}")

    # Per-day table
    day_grp = df.groupby("date").agg(
        trades=("pnl", "count"),
        net=("pnl", "sum"),
        wins=("pnl", lambda x: (x > 0).sum()),
    ).reset_index()
    print(f"\n{'Date':<14} {'Trades':<8} {'Net':>8} {'W/L'}")
    for _, r in day_grp.iterrows():
        l = r["trades"] - r["wins"]
        bar = ("+" * int(max(0, r["net"]) // 5)) or ("-" * int(abs(min(0, r["net"])) // 5))
        print(f"  {r['date']:<12} {int(r['trades']):<8} {r['net']:>+7.1f}  "
              f"{int(r['wins'])}W/{int(l)}L  {bar}")

    return {
        "days": days, "trades": n, "wins": int(wins), "losses": int(losses),
        "win_rate": wr, "net_pnl": net, "avg_pnl": avg,
        "avg_win": avg_win, "avg_loss": avg_loss,
        "avg_peak": avg_peak, "esl_pct": esl_pct,
    }


# ── Main runner ───────────────────────────────────────────────────────────────
def run_backtest(expiry_filter=None, from_date=None, to_date=None,
                 rsi_lo=RSI_LO, rsi_hi=RSI_HI,
                 bw_min=BW_MIN, bw_max=BW_MAX,
                 verbose=False, compare=False):

    collector_dir = os.path.join(LAB_DIR, "collector")

    # Find all expiry folders (new structure: expiry_YYYYMMDD/1min/ + 3min/)
    expiry_dirs = sorted(glob.glob(os.path.join(collector_dir, "expiry_*")))
    if not expiry_dirs:
        print("No expiry folders found. Run VRL_COLLECTOR.py first.")
        sys.exit(1)

    if expiry_filter:
        expiry_dirs = [d for d in expiry_dirs if expiry_filter in d]
        if not expiry_dirs:
            print(f"No expiry folder matching '{expiry_filter}' found.")
            sys.exit(1)

    all_trades = []

    for expiry_dir in expiry_dirs:
        expiry_label = os.path.basename(expiry_dir).replace("expiry_", "")
        dir_1m = os.path.join(expiry_dir, "1min")
        dir_3m = os.path.join(expiry_dir, "3min")

        # Skip old-structure folders (no 1min subfolder)
        if not os.path.isdir(dir_1m):
            print(f"[SKIP] {expiry_label} — no 1min/ subfolder (old format, run collector to update)")
            continue

        files_1m = sorted(glob.glob(os.path.join(dir_1m, "*.parquet")))
        if not files_1m:
            print(f"[SKIP] {expiry_label} — no 1-min parquet files")
            continue

        print(f"\n{'='*60}")
        print(f"EXPIRY: {expiry_label}  ({len(files_1m)} days)")
        print(f"{'='*60}")

        week_trades = []
        for path_1m in files_1m:
            day_str = os.path.basename(path_1m).replace(".parquet", "")

            # Date range filter
            if from_date and day_str < from_date:
                continue
            if to_date and day_str > to_date:
                continue

            path_3m = os.path.join(dir_3m, day_str + ".parquet")
            if not os.path.isfile(path_3m):
                path_3m = None

            # Load meta for ATM
            meta_path = os.path.join(collector_dir, "meta", day_str + ".json")
            atm = 0
            try:
                import json
                with open(meta_path) as f:
                    meta = json.load(f)
                atm = meta.get("atm", 0)
                strikes_1m = meta.get("strikes_1min", [atm] if atm else [])
            except Exception:
                # Fallback: read strikes from parquet
                try:
                    df_tmp = pd.read_parquet(path_1m)
                    strikes_1m = sorted(df_tmp["strike"].unique().tolist())
                    atm = strikes_1m[len(strikes_1m)//2] if strikes_1m else 0
                except Exception:
                    strikes_1m = []

            if not strikes_1m:
                print(f"  [{day_str}] No strikes found, skip")
                continue

            if verbose:
                print(f"\n  [{day_str}] ATM={atm}  strikes={strikes_1m[0]}..{strikes_1m[-1]}")

            day_trades = []
            for strike in strikes_1m:
                for opt_type in ("CE", "PE"):
                    t = _run_day(day_str, path_1m, path_3m, strike, opt_type,
                                 rsi_lo=rsi_lo, rsi_hi=rsi_hi,
                                 bw_min=bw_min, bw_max=bw_max,
                                 verbose=verbose)
                    day_trades.extend(t)

            # Deduplicate: if same direction fires on multiple strikes same minute
            # keep only the one closest to ATM
            if day_trades and atm:
                df_day = pd.DataFrame(day_trades)
                df_day["atm_dist"] = abs(df_day["strike"] - atm)
                # Keep best (closest ATM) per direction per entry_time
                df_day = df_day.sort_values("atm_dist").drop_duplicates(
                    subset=["date", "direction", "entry_time"], keep="first"
                ).drop(columns="atm_dist").to_dict("records")
                day_trades = df_day

            if day_trades:
                n_day = len(day_trades)
                pnl_day = round(sum(t["pnl"] for t in day_trades), 1)
                wins_day = sum(1 for t in day_trades if t["pnl"] > 0)
                print(f"  [{day_str}]  {n_day} trades  {wins_day}W/{n_day-wins_day}L  "
                      f"pnl={pnl_day:+.1f}pts")
            else:
                print(f"  [{day_str}]  0 signals")

            week_trades.extend(day_trades)

        if week_trades:
            _summarise(week_trades, f"EXPIRY {expiry_label}")
        all_trades.extend(week_trades)

    # Overall summary
    if all_trades:
        _summarise(all_trades, "ALL EXPIRIES — SHADOW-DTF TOTAL")

        # Save results CSV
        out_path = os.path.join(LAB_DIR, "shadow_backtest_results.csv")
        pd.DataFrame(all_trades).to_csv(out_path, index=False)
        print(f"\nResults saved → {out_path}")

        # Compare vs V9 if requested
        if compare:
            _compare_vs_v9(all_trades)

    return all_trades


def _compare_vs_v9(shadow_trades: list):
    """Compare Shadow-DTF signals vs V9 live trades on same days."""
    v9_log = os.path.join(LAB_DIR, "vrl_trade_log.csv")
    if not os.path.isfile(v9_log):
        print("\n[COMPARE] vrl_trade_log.csv not found — skipping V9 comparison")
        return

    try:
        v9_df    = pd.read_csv(v9_log)
        sh_df    = pd.DataFrame(shadow_trades)
        sh_dates = set(sh_df["date"].unique())
        v9_days  = v9_df[v9_df["date"].isin(sh_dates)] if "date" in v9_df.columns else pd.DataFrame()

        if v9_days.empty:
            print("\n[COMPARE] No V9 trades on same days as shadow signals")
            return

        print(f"\n{'─'*60}\nSHADOW-DTF vs V9 (same days)\n{'─'*60}")
        sh_net = round(sh_df["pnl"].sum(), 1)
        sh_wr  = round((sh_df["pnl"] > 0).mean() * 100, 1)
        v9_net = round(pd.to_numeric(v9_days.get("pnl_pts", 0), errors="coerce").sum(), 1)
        v9_n   = len(v9_days)
        v9_wr  = round((pd.to_numeric(v9_days.get("pnl_pts", 0), errors="coerce") > 0).mean() * 100, 1)
        print(f"  Shadow-DTF : {len(sh_df)} signals  WR {sh_wr}%  Net {sh_net:+.1f}pts")
        print(f"  V9 live    : {v9_n} trades   WR {v9_wr}%  Net {v9_net:+.1f}pts")
        diff = round(sh_net - v9_net, 1)
        print(f"  Difference : {diff:+.1f}pts  ({'Shadow better ✓' if diff > 0 else 'V9 better' if diff < 0 else 'Equal'})")
    except Exception as e:
        print(f"\n[COMPARE] Error: {e}")


# ── CLI ───────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Shadow-DTF (1m+3m) backtest")
    ap.add_argument("--expiry",   help="Filter to expiry folder e.g. 20260520")
    ap.add_argument("--from",     dest="from_date", help="Start date YYYY-MM-DD")
    ap.add_argument("--to",       dest="to_date",   help="End date YYYY-MM-DD")
    ap.add_argument("--rsi-lo",   type=float, default=RSI_LO)
    ap.add_argument("--rsi-hi",   type=float, default=RSI_HI)
    ap.add_argument("--bw-min",   type=float, default=BW_MIN)
    ap.add_argument("--bw-max",   type=float, default=BW_MAX)
    ap.add_argument("--verbose",  action="store_true")
    ap.add_argument("--compare",  action="store_true", help="Compare vs V9 live trades")
    args = ap.parse_args()

    run_backtest(
        expiry_filter = args.expiry,
        from_date     = args.from_date,
        to_date       = args.to_date,
        rsi_lo        = args.rsi_lo,
        rsi_hi        = args.rsi_hi,
        bw_min        = args.bw_min,
        bw_max        = args.bw_max,
        verbose       = args.verbose,
        compare       = args.compare,
    )
