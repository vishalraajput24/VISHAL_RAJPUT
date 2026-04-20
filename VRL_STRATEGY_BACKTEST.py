#!/usr/bin/env python3
# ═══════════════════════════════════════════════════════════════
#  VRL_STRATEGY_BACKTEST.py — v16.0.1 strategy replay
#
#  PART 1: data loader skeleton
#  - Loads spot_1min, option_3min, option_1min, trades from DB
#  - Filters by --start-date / --end-date inclusive
#  - Sorts by timestamp ascending
#  - Reports row counts + actual date range
#
#  Read-only. No writes, no service restarts, no Kite calls.
#  Production code untouched.
#
#  Usage:
#    ~/kite_env/bin/python3 VRL_STRATEGY_BACKTEST.py \
#        --start-date 2026-04-10 --end-date 2026-04-17
# ═══════════════════════════════════════════════════════════════

import argparse
import logging
import os
import sqlite3
import sys
from datetime import date, datetime

import numpy as np
import pandas as pd

DB_PATH = os.path.expanduser("~/lab_data/vrl_data.db")
DEFAULT_START = "2026-04-10"
DEFAULT_END   = "2026-04-17"

logger = logging.getLogger("VRL_BACKTEST")


# ═══════════════════════════════════════════════════════════════
#  DATA LOADER
# ═══════════════════════════════════════════════════════════════

def _load_table(conn: sqlite3.Connection, table: str, columns: str,
                start_date: str, end_date: str,
                date_column: str = "timestamp") -> pd.DataFrame:
    """Load a table filtered by date column, sorted ascending."""
    if date_column == "timestamp":
        sql = ("SELECT " + columns + " FROM " + table
               + " WHERE date(timestamp) BETWEEN ? AND ? "
               + "ORDER BY timestamp ASC")
    else:
        sql = ("SELECT " + columns + " FROM " + table
               + " WHERE " + date_column + " BETWEEN ? AND ? "
               + "ORDER BY " + date_column + " ASC")
    try:
        df = pd.read_sql_query(sql, conn, params=(start_date, end_date))
    except Exception as e:
        logger.warning("[LOAD] " + table + " query error: " + str(e))
        return pd.DataFrame()
    return df


def _table_columns(conn: sqlite3.Connection, table: str) -> set:
    """Return set of column names for a table (PRAGMA table_info)."""
    try:
        rows = conn.execute("PRAGMA table_info(" + table + ")").fetchall()
        return {r[1] for r in rows}
    except Exception:
        return set()


def _to_datetime(df: pd.DataFrame, col: str = "timestamp") -> pd.DataFrame:
    """Coerce timestamp column to datetime in place."""
    if df.empty or col not in df.columns:
        return df
    df[col] = pd.to_datetime(df[col], errors="coerce")
    df = df.dropna(subset=[col])
    return df


def load_historical_data(start_date: str, end_date: str) -> dict:
    """Read the four tables we need for the backtest.

    Returns a dict:
        spot_1min:    DataFrame of spot 1-min OHLC + volume
        option_3min:  DataFrame of option 3-min bars (with EMA9 bands if present)
        option_1min:  DataFrame of option 1-min bars
        trades_live:  DataFrame of historical trades (cross-validation)
        meta:         counts + actual date range observed in the data
    """
    if not os.path.isfile(DB_PATH):
        raise SystemExit("ERROR: DB not found at " + DB_PATH)

    conn = sqlite3.connect(DB_PATH)

    # spot_1min — used to drive ATM determination + intraday timeline
    spot = _load_table(
        conn, "spot_1min",
        "timestamp, open, high, low, close, volume",
        start_date, end_date)
    spot = _to_datetime(spot)

    # option_3min — primary candles for entry/exit logic
    opt3_cols_avail = _table_columns(conn, "option_3min")
    opt3_select = ["timestamp", "strike", "type", "open", "high",
                   "low", "close", "volume"]
    if "ema9_high" in opt3_cols_avail:
        opt3_select.append("ema9_high")
    if "ema9_low" in opt3_cols_avail:
        opt3_select.append("ema9_low")
    opt_3m = _load_table(conn, "option_3min", ",".join(opt3_select),
                         start_date, end_date)
    opt_3m = _to_datetime(opt_3m)

    # option_1min — used for the EMA1M_BREAK exit rule
    opt_1m = _load_table(
        conn, "option_1min",
        "timestamp, strike, type, open, high, low, close, volume",
        start_date, end_date)
    opt_1m = _to_datetime(opt_1m)

    # trades — historical executions for cross-validation
    trades_cols = ("date, entry_time, exit_time, direction, strike, "
                   "entry_price, exit_price, pnl_pts, pnl_rs, "
                   "peak_pnl, exit_reason")
    trades = _load_table(conn, "trades", trades_cols,
                         start_date, end_date, date_column="date")

    conn.close()

    # Meta summary
    actual_start = ""
    actual_end = ""
    timeline_sources = [spot, opt_3m, opt_1m]
    nonempty_ts = []
    for src in timeline_sources:
        if not src.empty and "timestamp" in src.columns:
            nonempty_ts.append(src["timestamp"].min())
            nonempty_ts.append(src["timestamp"].max())
    if nonempty_ts:
        actual_start = str(min(nonempty_ts))
        actual_end = str(max(nonempty_ts))

    unique_strikes_3m = 0
    if not opt_3m.empty and "strike" in opt_3m.columns:
        unique_strikes_3m = opt_3m["strike"].nunique()

    meta = {
        "rows_spot": len(spot),
        "rows_opt_3m": len(opt_3m),
        "rows_opt_1m": len(opt_1m),
        "rows_trades": len(trades),
        "unique_strikes_3m": int(unique_strikes_3m),
        "date_range_actual": (actual_start, actual_end),
    }

    return {
        "spot_1min": spot,
        "option_3min": opt_3m,
        "option_1min": opt_1m,
        "trades_live": trades,
        "meta": meta,
    }


# ═══════════════════════════════════════════════════════════════
#  MAIN STUB
# ═══════════════════════════════════════════════════════════════

# Known Nifty weekly expiries. Normally Tuesday; shifts to prior working
# day if Tuesday is a holiday. For Apr 2026: Apr 14 Tue was a holiday so
# that week's expiry moved to Apr 13 Mon.
_DEFAULT_EXPIRY_CAL = [
    "2026-04-07",  # Tue
    "2026-04-13",  # Mon (shift from Apr 14 holiday)
    "2026-04-21",  # Tue
    "2026-04-28",  # Tue
    "2026-05-05",  # Tue
]


def _compute_dte(current_dt, expiry_cal) -> int:
    """Return calendar days to the NEXT expiry on or after current_dt.date().
    Returns 999 if no expiry found ahead."""
    from datetime import date as _date
    cd = current_dt.date() if hasattr(current_dt, "date") else current_dt
    for s in expiry_cal:
        try:
            exp = datetime.strptime(s, "%Y-%m-%d").date()
        except Exception:
            continue
        if exp >= cd:
            return (exp - cd).days
    return 999


def _compute_fib_levels(df, lookback: int = 20) -> dict:
    """Compute fib retracement levels from recent option premium swing."""
    if df is None or len(df) < 5:
        return None
    recent = df.tail(lookback)
    sh = float(recent["high"].max())
    sl = float(recent["low"].min())
    sr = sh - sl
    if sr < 2:
        return None
    return {
        "swing_high": sh,
        "swing_low": sl,
        "swing_range": sr,
        "fib_236": round(sh - sr * 0.236, 2),
        "fib_382": round(sh - sr * 0.382, 2),
        "fib_500": round(sh - sr * 0.500, 2),
        "fib_618": round(sh - sr * 0.618, 2),
    }


def _make_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="VRL v16.0.1 strategy backtest")
    p.add_argument("--start-date", default=DEFAULT_START,
                   help="ISO date YYYY-MM-DD inclusive (default: " + DEFAULT_START + ")")
    p.add_argument("--end-date", default=DEFAULT_END,
                   help="ISO date YYYY-MM-DD inclusive (default: " + DEFAULT_END + ")")
    p.add_argument("--verbose", action="store_true",
                   help="Enable INFO-level logging")
    # Batch 9C sweep parameters
    p.add_argument("--body-min", type=float, default=30.0,
                   help="Body gate minimum %% of range (default 30)")
    p.add_argument("--band-min", type=float, default=8.0,
                   help="EMA9 band width minimum pts (default 8)")
    p.add_argument("--fresh-lookback", type=int, default=3,
                   help="Gate 3 fresh-breakout lookback (default 3)")
    p.add_argument("--entry-signal", choices=["ema9_low", "ema9_high"],
                   default="ema9_low",
                   help="Which band close must break (default ema9_low, v16.2)")
    p.add_argument("--stale-candles", type=int, default=5,
                   help="Stale exit candle count (default 5)")
    p.add_argument("--dte-filter", choices=["off", "on"], default="off",
                   help="Skip trading days with DTE <= 1 (default off)")
    p.add_argument("--ema-filter", choices=["off", "on"], default="off",
                   help="Reject entries where bands_state=FLAT (default off)")
    p.add_argument("--fib-filter", choices=["off", "breakout", "pullback", "proximity"],
                   default="off",
                   help="Fibonacci retracement filter mode (default off)")
    p.add_argument("--timeframe", choices=["3min", "1min"], default="3min",
                   help="Strategy timeframe: 3min (default, current production) or 1min")
    p.add_argument("--expiry-date", default="2026-04-21",
                   help="(legacy single-date fallback) Nearest expiry date")
    p.add_argument("--expiry-dates", default=",".join(_DEFAULT_EXPIRY_CAL),
                   help="Comma-separated weekly expiry dates (YYYY-MM-DD,...). "
                        "Default covers Apr-May 2026 with Apr 13 shift for holiday.")
    p.add_argument("--classify-csv", default=None,
                   help="Read an existing trades CSV and print NORMAL vs DRIFT KILL breakdown, then exit")
    p.add_argument("--tag", default="",
                   help="Optional suffix for output CSV filenames")
    return p


def _classify_trades_csv(csv_path: str) -> None:
    """Read a trades CSV and print NORMAL vs DRIFT KILL breakdown."""
    import csv as _csv
    path = os.path.expanduser(csv_path)
    if not os.path.isfile(path):
        print("ERROR: CSV not found: " + path)
        return

    normal = []
    drift = []
    with open(path) as f:
        reader = _csv.DictReader(f)
        for row in reader:
            try:
                pnl = float(row.get("pnl_pts", 0) or 0)
                entry = float(row.get("entry_price", 0) or 0)
                exit_px = float(row.get("exit_price", 0) or 0)
            except Exception:
                continue
            # Drift kill: big loss + premium collapsed below half of entry
            if pnl < -50 and entry > 0 and exit_px < entry * 0.5:
                drift.append(row)
            else:
                normal.append(row)

    total = len(normal) + len(drift)
    print("=" * 55)
    print("TRADE CLASSIFICATION: " + csv_path)
    print("=" * 55)
    print("Total trades:  " + str(total))

    def _stats(rows):
        n = len(rows)
        if n == 0:
            return 0, 0, 0.0, 0.0, 0
        pnls = [float(r["pnl_pts"]) for r in rows]
        wins = sum(1 for p in pnls if p > 0)
        avg = sum(pnls) / n
        net_rs = sum(float(r.get("pnl_rupees_net", 0) or 0) for r in rows)
        wr = round(wins / n * 100, 1)
        return n, wins, round(avg, 2), round(net_rs, 0), wr

    n_n, w_n, avg_n, net_n, wr_n = _stats(normal)
    n_d, w_d, avg_d, net_d, wr_d = _stats(drift)

    print("")
    print("NORMAL trades:      " + str(n_n) + "  avg pnl: "
          + "{:+.2f}".format(avg_n) + " pts  win rate: " + str(wr_n) + "%"
          + "  net: Rs" + "{:,}".format(int(net_n)))
    print("DRIFT KILL trades:  " + str(n_d) + "  avg pnl: "
          + "{:+.2f}".format(avg_d) + " pts  win rate: " + str(wr_d) + "%"
          + "  net: Rs" + "{:,}".format(int(net_d)))
    print("")
    print("=== CLEAN BASELINE (excluding drift kills) ===")
    print("Trades:       " + str(n_n))
    print("Wins:         " + str(w_n))
    print("Win rate:     " + str(wr_n) + "%")
    print("Expectancy:   " + "{:+.2f}".format(avg_n) + " pts/trade")
    print("Net PnL:      Rs" + "{:,}".format(int(net_n)))
    print("=" * 55)


def _apply_config_overrides(args) -> dict:
    """Monkey-patch VRL_CONFIG's cached YAML so the pure functions pick up
    sweep parameters at read time. Returns a dict with the original values
    so the caller can restore later (we don't bother — single-shot process)."""
    _expiry_cal = [s.strip() for s in (args.expiry_dates or "").split(",") if s.strip()]
    if not _expiry_cal:
        _expiry_cal = list(_DEFAULT_EXPIRY_CAL)
    overrides = {
        "body_min": args.body_min,
        "band_min": args.band_min,
        "stale_candles": args.stale_candles,
        "dte_filter": args.dte_filter == "on",
        "ema_filter": args.ema_filter == "on",
        "fib_filter": args.fib_filter,
        "fresh_lookback": args.fresh_lookback,
        "entry_signal": args.entry_signal,
        "expiry_date": args.expiry_date,     # legacy single-date
        "expiry_cal": _expiry_cal,           # new calendar
    }
    try:
        import VRL_CONFIG as CFG  # type: ignore
        # Patch 1: mutate the cached YAML dict (for code paths that read it)
        try:
            cfg = CFG.get()
        except Exception:
            cfg = getattr(CFG, "_cfg", None) or {}
        if isinstance(cfg, dict):
            entry = cfg.setdefault("entry", {}).setdefault("ema9_band", {})
            entry["body_pct_min"] = args.body_min
            entry["min_band_width_pts"] = args.band_min
            exit_ = cfg.setdefault("exit", {}).setdefault("ema9_band", {})
            exit_["stale_candles"] = args.stale_candles

        # Patch 2: intercept the lookup functions themselves. This is the
        # one that actually works — _evaluate_exit_chain_pure calls
        # CFG.exit_ema9_band(key, default) and we replace that call.
        _orig_entry = getattr(CFG, "entry_ema9_band", None)
        _orig_exit  = getattr(CFG, "exit_ema9_band", None)
        if _orig_entry:
            def _patched_entry(key, default=None, _orig=_orig_entry):
                if key == "body_pct_min":
                    return args.body_min
                if key == "min_band_width_pts":
                    return args.band_min
                return _orig(key, default)
            CFG.entry_ema9_band = _patched_entry
        if _orig_exit:
            def _patched_exit(key, default=None, _orig=_orig_exit):
                if key == "stale_candles":
                    return args.stale_candles
                return _orig(key, default)
            CFG.exit_ema9_band = _patched_exit
    except Exception as e:
        print("WARNING: CFG override failed (" + str(e) + ") — "
              + "default CFG values will apply to pure gates; "
              + "post-filter still enforces overrides.")
    return overrides


def main():
    args = _make_arg_parser().parse_args()
    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    if args.classify_csv:
        _classify_trades_csv(args.classify_csv)
        return

    overrides = _apply_config_overrides(args)

    print("═══ BATCH 9 — STRATEGY BACKTEST ═══")
    print("Date range: " + args.start_date + " to " + args.end_date)
    print("Params: body_min=" + str(args.body_min)
          + " band_min=" + str(args.band_min)
          + " stale=" + str(args.stale_candles)
          + " DTE=" + args.dte_filter
          + " EMA=" + args.ema_filter)

    data = load_historical_data(args.start_date, args.end_date)
    meta = data["meta"]
    print("Spot 1-min:   " + str(meta["rows_spot"]))
    print("Option 3-min: " + str(meta["rows_opt_3m"]))
    print("Option 1-min: " + str(meta["rows_opt_1m"]))
    print("Strikes (3m): " + str(meta["unique_strikes_3m"]))
    print("Live trades:  " + str(meta["rows_trades"]))
    print()

    # ── Precompute indicators per (strike, type) ──
    from VRL_DATA import add_indicators
    opt_3m = data["option_3min"]
    opt_1m = data["option_1min"]
    spot   = data["spot_1min"]

    if opt_3m.empty or spot.empty:
        print("ERROR: insufficient data for backtest.")
        return

    cache_3m = {}
    for (strike, otype), grp in opt_3m.groupby(["strike", "type"]):
        g = grp.sort_values("timestamp").reset_index(drop=True)
        g = g.set_index("timestamp")
        g = add_indicators(g)
        cache_3m[(int(strike), otype)] = g.reset_index()

    cache_1m = {}
    if not opt_1m.empty:
        for (strike, otype), grp in opt_1m.groupby(["strike", "type"]):
            g = grp.sort_values("timestamp").reset_index(drop=True)
            g = g.set_index("timestamp")
            g = add_indicators(g)
            cache_1m[(int(strike), otype)] = g.reset_index()

    print("Indicator cache built: " + str(len(cache_3m)) + " groups (3m), "
          + str(len(cache_1m)) + " groups (1m)")

    # ── Build spot timeline at chosen granularity ──
    spot_ts = spot.set_index("timestamp").sort_index()
    _tf = args.timeframe
    if _tf == "1min":
        spot_timeline = spot_ts["close"].dropna()
        opt_cache = cache_1m
    else:
        spot_timeline = spot_ts["close"].resample("3min").last().dropna()
        opt_cache = cache_3m
    print("Timeframe: " + _tf + "  (option cache groups: " + str(len(opt_cache)) + ")")

    # ── Determine trading days ──
    trading_days = sorted(spot["timestamp"].dt.date.unique())
    print("Trading days: " + str([str(d) for d in trading_days]))
    print()

    # ── Import pure functions ──
    from VRL_ENGINE import (
        _evaluate_entry_gates_pure,
        _evaluate_exit_chain_pure,
        _compute_1min_ema9_break_pure,
        compute_ratchet_sl,
    )

    # ── Run simulation ──
    LOT_SIZE   = 65
    LOT_COUNT  = 2
    TOTAL_QTY  = LOT_SIZE * LOT_COUNT
    COOLDOWN   = 5  # minutes

    trades     = []
    rejections = []
    daily_stats = {}

    state = {
        "in_trade": False,
        "last_exit_time": "",
        "last_exit_direction": "",
    }

    for day in trading_days:
        day_str = str(day)
        locked_strike = None
        locked_spot   = None
        day_trades    = 0
        day_pnl       = 0.0

        day_spot = spot_timeline[spot_timeline.index.date == day]
        if day_spot.empty:
            continue

        for ts, spot_close in day_spot.items():
            atm = int(round(float(spot_close) / 50) * 50)
            now = ts.to_pydatetime() if hasattr(ts, "to_pydatetime") else ts
            mins = now.hour * 60 + now.minute

            # Time window check: 09:30 – 15:30
            if mins < 570 or mins > 930:
                continue

            # Strike lock logic
            if locked_strike is None:
                locked_strike = atm
                locked_spot = float(spot_close)
            else:
                drift = abs(float(spot_close) - locked_spot)
                if drift >= 75:
                    locked_strike = atm
                    locked_spot = float(spot_close)
                elif drift >= 50:
                    locked_strike = atm
                    locked_spot = float(spot_close)

            if not state["in_trade"]:
                # ── Entry evaluation ──
                if mins < 570 or mins >= 910:
                    continue  # outside 09:30 – 15:10

                for otype in ("CE", "PE"):
                    key = (locked_strike, otype)
                    if key not in opt_cache:
                        continue
                    df_full = opt_cache[key]
                    df_up = df_full[df_full["timestamp"] <= ts]
                    if len(df_up) < 4:
                        continue

                    # ── INLINE ENTRY GATES (parameterizable) ──
                    last = df_up.iloc[-1]
                    prev = df_up.iloc[-2]
                    close = float(last["close"])
                    open_ = float(last["open"])
                    high = float(last["high"])
                    low = float(last["low"])
                    ema9h = float(last.get("ema9_high", 0))
                    ema9l = float(last.get("ema9_low", 0))
                    prev_close = float(prev["close"])
                    prev_ema9h = float(prev.get("ema9_high", 0))

                    fired = True
                    reject = None

                    # Gate 2: Cooldown
                    if state.get("last_exit_time") and state.get("last_exit_direction") == otype:
                        try:
                            elapsed = (now - datetime.fromisoformat(state["last_exit_time"])).total_seconds() / 60
                            if elapsed < 5:
                                fired = False; reject = "cooldown_" + str(round(5 - elapsed, 1))
                        except Exception:
                            pass

                    # Gate 3: Fresh breakout — ema9_low (v16.2) or ema9_high (legacy)
                    if fired:
                        _fb_lb = int(overrides.get("fresh_lookback", 3) or 3)
                        _band_col = "ema9_low" if overrides.get("entry_signal") == "ema9_low" else "ema9_high"
                        _band_val = ema9l if _band_col == "ema9_low" else ema9h
                        was_below = False
                        for k in range(2, 2 + _fb_lb):
                            if len(df_up) <= k:
                                break
                            bar = df_up.iloc[-k-1]
                            _bar_band = float(bar.get(_band_col, 0))
                            _bar_close = float(bar.get("close", 0))
                            if _bar_band > 0 and _bar_close <= _bar_band:
                                was_below = True; break
                        if not (close > _band_val and was_below):
                            fired = False; reject = "fresh_breakout_lb" + str(_fb_lb)

                    # Gate 4: Green candle
                    if fired and close <= open_:
                        fired = False; reject = "green_candle"

                    # Gate 5: Body >= body_min (SWEEP)
                    cr = high - low
                    body_pct = (abs(close - open_) / cr * 100) if cr > 0 else 0
                    if fired and body_pct < overrides["body_min"]:
                        fired = False; reject = "body_" + str(int(body_pct))

                    # Gate 6: Band width >= band_min (SWEEP)
                    band_width = ema9h - ema9l
                    if fired and band_width < overrides["band_min"]:
                        fired = False; reject = "band_" + str(round(band_width, 1))

                    # DTE filter (optional) — uses weekly expiry calendar
                    if fired and overrides["dte_filter"]:
                        try:
                            dte = _compute_dte(now, overrides["expiry_cal"])
                            if dte <= 1:
                                fired = False; reject = "dte_" + str(dte)
                        except Exception:
                            pass

                    # EMA bands rising filter (optional)
                    if fired and overrides["ema_filter"]:
                        if len(df_up) >= 6:
                            eh_then = float(df_up.iloc[-6].get("ema9_high", 0))
                            el_then = float(df_up.iloc[-6].get("ema9_low", 0))
                            if (ema9h - eh_then) <= 3 and (ema9l - el_then) <= 3:
                                fired = False; reject = "ema_flat"

                    # Fibonacci retracement filter (optional)
                    fib_mode = overrides.get("fib_filter", "off")
                    if fired and fib_mode != "off":
                        fibs = _compute_fib_levels(df_up, lookback=20)
                        if fibs is not None:
                            if fib_mode == "breakout":
                                if close < fibs["fib_236"]:
                                    fired = False; reject = "fib_below_236"
                            elif fib_mode == "pullback":
                                near_382 = abs(close - fibs["fib_382"]) < 3
                                near_500 = abs(close - fibs["fib_500"]) < 3
                                if not (near_382 or near_500):
                                    fired = False; reject = "fib_not_at_pullback"
                            elif fib_mode == "proximity":
                                for lvl in (fibs["fib_382"], fibs["fib_500"], fibs["fib_618"]):
                                    if abs(close - lvl) < 3:
                                        fired = False
                                        reject = "fib_proximity_" + str(round(lvl, 1))
                                        break

                    if not fired:
                        rejections.append({
                            "timestamp": str(now),
                            "day": day_str,
                            "strike": locked_strike,
                            "type": otype,
                            "reason": reject or "",
                        })
                        continue

                    # Entry fires
                    entry_price = round(close, 2)
                    if entry_price <= 0:
                        continue
                    state.update({
                        "in_trade": True,
                        "entry_price": entry_price,
                        "direction": otype,
                        "strike": locked_strike,
                        "entry_time": str(now),
                        "token": None,
                        "peak_pnl": 0.0,
                        "trough_pnl": 0.0,
                        "candles_held": 0,
                        "peak_history": [],
                        "last_peak_candle_ts": "",
                        "current_velocity": 0.0,
                        "active_ratchet_tier": "",
                        "active_ratchet_sl": 0.0,
                        "_peak_history_backfilled": True,
                        "current_ema9_high": 0.0,
                        "current_ema9_low": 0.0,
                    })
                    break

            else:
                # ── Exit evaluation ──
                otype = state["direction"]
                key = (state["strike"], otype)
                if key not in opt_cache:
                    continue
                df_full = opt_cache[key]
                df_up = df_full[df_full["timestamp"] <= ts]
                if len(df_up) < 2:
                    continue

                dummy = df_up.iloc[-1:].copy()
                dummy.index = [df_up.index[-1] + 1]
                eval_3m = pd.concat([df_up, dummy])

                option_ltp = float(df_up.iloc[-1]["close"])
                # v16: candles_held = MINUTES elapsed since entry (matches
                # production increment per 1-min boundary). At each 3-min
                # exit check this advances by 3 (0 → 3 → 6 → 9 ...).
                try:
                    _ent_dt = datetime.fromisoformat(state["entry_time"])
                    state["candles_held"] = int((now - _ent_dt).total_seconds() / 60)
                except Exception:
                    state["candles_held"] = state.get("candles_held", 0) + 3

                # 1-min EMA9 break check
                ema1m_result = (False, 0.0, 0.0)
                key_1m = (state["strike"], otype)
                if key_1m in cache_1m:
                    df_1m_full = cache_1m[key_1m]
                    df_1m_up = df_1m_full[df_1m_full["timestamp"] <= ts]
                    if len(df_1m_up) >= 10:
                        dummy_1m = df_1m_up.iloc[-1:].copy()
                        dummy_1m.index = [df_1m_up.index[-1] + 1]
                        eval_1m = pd.concat([df_1m_up, dummy_1m])
                        running_pnl = round(option_ltp - state["entry_price"], 2)
                        ema1m_result = _compute_1min_ema9_break_pure(
                            eval_1m, running_pnl, min_pnl_guard=5.0)

                exit_list = _evaluate_exit_chain_pure(
                    state=state,
                    option_ltp=option_ltp,
                    opt_3m_full=eval_3m,
                    now=now,
                    ema1m_break_result=ema1m_result,
                    market_open=True,
                )

                if exit_list:
                    exit_info = exit_list[0]
                    exit_price = float(exit_info.get("price", option_ltp))
                    pnl_pts = round(exit_price - state["entry_price"], 2)
                    peak = state.get("peak_pnl", 0)
                    giveback = round(peak - pnl_pts, 2) if peak > 0 else 0
                    ratchet_tier = state.get("active_ratchet_tier", "None")

                    # Charges
                    try:
                        from VRL_CHARGES import calculate_charges
                        ch = calculate_charges(
                            state["entry_price"], exit_price, TOTAL_QTY, 1)
                        charges = ch.get("total_charges", 0)
                        net_rs = ch.get("net_pnl", pnl_pts * TOTAL_QTY)
                    except Exception:
                        charges = 50
                        net_rs = pnl_pts * TOTAL_QTY - charges

                    trade = {
                        "day": day_str,
                        "entry_time": state["entry_time"],
                        "exit_time": str(now),
                        "strike": state["strike"],
                        "type": otype,
                        "entry_price": round(state["entry_price"], 2),
                        "exit_price": round(exit_price, 2),
                        "pnl_pts": pnl_pts,
                        "pnl_rupees_gross": round(pnl_pts * TOTAL_QTY, 0),
                        "peak_pnl": round(peak, 2),
                        "giveback": giveback,
                        "exit_reason": exit_info.get("reason", "UNKNOWN"),
                        "ratchet_tier": ratchet_tier if ratchet_tier else "None",
                        "candles_held": state.get("candles_held", 0),
                        "charges": round(charges, 0),
                        "pnl_rupees_net": round(net_rs, 0),
                    }
                    trades.append(trade)
                    day_trades += 1
                    day_pnl += pnl_pts

                    state.update({
                        "in_trade": False,
                        "last_exit_time": str(now),
                        "last_exit_direction": otype,
                    })

        daily_stats[day_str] = {"trades": day_trades, "pnl": round(day_pnl, 2)}

    # ── Force EOD exit if still in position ──
    if state["in_trade"]:
        otype = state["direction"]
        key = (state["strike"], otype)
        if key in opt_cache:
            df_full = opt_cache[key]
            if not df_full.empty:
                last_close = float(df_full.iloc[-1]["close"])
                pnl_pts = round(last_close - state["entry_price"], 2)
                peak = state.get("peak_pnl", 0)
                trades.append({
                    "day": str(trading_days[-1]) if trading_days else "",
                    "entry_time": state["entry_time"],
                    "exit_time": "EOD_FORCED",
                    "strike": state["strike"],
                    "type": otype,
                    "entry_price": round(state["entry_price"], 2),
                    "exit_price": round(last_close, 2),
                    "pnl_pts": pnl_pts,
                    "pnl_rupees_gross": round(pnl_pts * TOTAL_QTY, 0),
                    "peak_pnl": round(peak, 2),
                    "giveback": round(peak - pnl_pts, 2) if peak > 0 else 0,
                    "exit_reason": "EOD_FORCED",
                    "ratchet_tier": state.get("active_ratchet_tier", "None"),
                    "candles_held": state.get("candles_held", 0),
                    "charges": 50,
                    "pnl_rupees_net": round(pnl_pts * TOTAL_QTY - 50, 0),
                })
                state["in_trade"] = False

    # ═══════════════════════════════════════════════════════════
    #  REPORT
    # ═══════════════════════════════════════════════════════════
    print("=" * 55)
    print("  BATCH 9 — STRATEGY BACKTEST REPORT")
    print("=" * 55)
    print("Run date:    " + date.today().isoformat())
    print("Sample:      " + str(len(trading_days)) + " trading days, "
          + str(len(trades)) + " simulated trades")
    print("Date range:  " + args.start_date + " to " + args.end_date)
    print()

    if not trades:
        print("NO TRADES generated. Check data + gate parameters.")
        print("Rejections logged: " + str(len(rejections)))
        if rejections:
            reasons = {}
            for r in rejections:
                rr = str(r.get("reason", ""))[:30]
                reasons[rr] = reasons.get(rr, 0) + 1
            print("Top rejection reasons:")
            for rr, cnt in sorted(reasons.items(), key=lambda x: -x[1])[:10]:
                print("  " + rr + ": " + str(cnt))
        return

    # ── Overall ──
    n = len(trades)
    winners = [t for t in trades if t["pnl_pts"] > 0]
    losers  = [t for t in trades if t["pnl_pts"] <= 0]
    w, l = len(winners), len(losers)
    wr = round(w / n * 100, 1) if n else 0
    total_gross = sum(t["pnl_rupees_gross"] for t in trades)
    total_net   = sum(t["pnl_rupees_net"] for t in trades)
    total_pts   = sum(t["pnl_pts"] for t in trades)
    avg_pts     = round(total_pts / n, 2) if n else 0
    best  = max(trades, key=lambda t: t["pnl_pts"])
    worst = min(trades, key=lambda t: t["pnl_pts"])

    print("=== OVERALL PERFORMANCE ===")
    print("Total trades:    " + str(n))
    print("Winners:         " + str(w) + " (" + str(wr) + "%)")
    print("Losers:          " + str(l))
    print("Gross PnL:       Rs" + "{:,}".format(int(total_gross)))
    print("Net PnL:         Rs" + "{:,}".format(int(total_net)))
    print("Expectancy:      " + "{:+.2f}".format(avg_pts) + " pts/trade")
    print("Best trade:      " + "{:+.1f}".format(best["pnl_pts"]) + " pts (" + best["day"] + ")")
    print("Worst trade:     " + "{:.1f}".format(worst["pnl_pts"]) + " pts (" + worst["day"] + ")")
    print()

    # ── Win/Loss ──
    avg_win  = round(np.mean([t["pnl_pts"] for t in winners]), 2) if winners else 0
    avg_loss = round(np.mean([t["pnl_pts"] for t in losers]), 2) if losers else 0
    wl_ratio = round(abs(avg_win / avg_loss), 2) if avg_loss != 0 else float("inf")
    print("=== WIN/LOSS DISTRIBUTION ===")
    print("Avg winner:      " + "{:+.2f}".format(avg_win) + " pts")
    print("Avg loser:       " + "{:.2f}".format(avg_loss) + " pts")
    print("Win/loss ratio:  " + str(wl_ratio))
    print()

    # ── Peak/Giveback ──
    avg_peak = round(np.mean([t["peak_pnl"] for t in trades]), 2)
    avg_exit = round(np.mean([t["pnl_pts"] for t in trades]), 2)
    avg_give = round(np.mean([t["giveback"] for t in trades]), 2)
    peak_10  = [t for t in trades if t["peak_pnl"] >= 10]
    p10_good = [t for t in peak_10 if t["pnl_pts"] >= 5]
    print("=== PEAK / GIVEBACK ===")
    print("Avg peak PnL:    " + "{:+.2f}".format(avg_peak) + " pts")
    print("Avg exit PnL:    " + "{:+.2f}".format(avg_exit) + " pts")
    print("Avg giveback:    " + "{:.2f}".format(avg_give) + " pts")
    if peak_10:
        print("Trades peak>10:  " + str(len(peak_10))
              + " (exited>5: " + str(len(p10_good))
              + ", exited<5: " + str(len(peak_10) - len(p10_good)) + ")")
    print()

    # ── Ratchet ──
    tier_buckets = {}
    for t in trades:
        tier = t.get("ratchet_tier", "None")
        if tier not in tier_buckets:
            tier_buckets[tier] = []
        tier_buckets[tier].append(t["pnl_pts"])
    print("=== RATCHET TIER BREAKDOWN ===")
    for tier in ["None", "T1", "T2", "T3", "T4", "T5"]:
        pts_list = tier_buckets.get(tier, [])
        if pts_list:
            avg = round(np.mean(pts_list), 2)
            print("  " + tier + ": " + str(len(pts_list)) + " trades, avg "
                  + "{:+.2f}".format(avg) + " pts")
    print()

    # ── Exit reason ──
    reason_buckets = {}
    for t in trades:
        r = t["exit_reason"]
        if r not in reason_buckets:
            reason_buckets[r] = []
        reason_buckets[r].append(t["pnl_pts"])
    print("=== EXIT REASON BREAKDOWN ===")
    for reason, pts_list in sorted(reason_buckets.items(),
                                    key=lambda x: -len(x[1])):
        avg = round(np.mean(pts_list), 2)
        print("  " + reason + ": " + str(len(pts_list)) + " trades, avg "
              + "{:+.2f}".format(avg) + " pts")
    print()

    # ── Daily ──
    print("=== DAILY BREAKDOWN ===")
    for day_str in sorted(daily_stats.keys()):
        ds = daily_stats[day_str]
        print("  " + day_str + ": " + str(ds["trades"]) + " trades, "
              + "{:+.1f}".format(ds["pnl"]) + " pts")
    print()

    # ── Gate rejections ──
    print("=== GATE REJECTION ANALYSIS ===")
    print("Total candles examined: " + str(len(rejections)))
    reason_counts = {}
    for r in rejections:
        rr = str(r.get("reason", ""))
        # Categorize by gate
        if "before_" in rr or "after_" in rr:
            gate = "time_window"
        elif "cooldown" in rr:
            gate = "cooldown"
        elif "below_band" in rr or "above_band" in rr or "crossed_down" in rr or "missed_fire" in rr:
            gate = "fresh_breakout"
        elif "red_candle" in rr:
            gate = "green_candle"
        elif "weak_body" in rr:
            gate = "body_30pct"
        elif "narrow_band" in rr:
            gate = "band_width_8pts"
        elif "insufficient" in rr:
            gate = "data_insufficient"
        else:
            gate = "other"
        reason_counts[gate] = reason_counts.get(gate, 0) + 1
    for gate, cnt in sorted(reason_counts.items(), key=lambda x: -x[1]):
        print("  " + gate + ": " + str(cnt))
    print()

    # ── Cross-validation ──
    live_trades = data["trades_live"]
    print("=== CROSS-VALIDATION VS LIVE TRADES ===")
    if live_trades.empty:
        print("  No live trades in DB for comparison.")
    else:
        print("  Live trades in range: " + str(len(live_trades)))
        print("  Backtest trades:      " + str(n))
        matches = 0
        for _, lt in live_trades.iterrows():
            lt_strike = int(lt.get("strike", 0))
            lt_dir = lt.get("direction", "")
            lt_date = str(lt.get("date", ""))
            for bt in trades:
                if (bt["strike"] == lt_strike
                        and bt["type"] == lt_dir
                        and bt["day"] == lt_date):
                    matches += 1
                    break
        print("  Date+strike+direction matches: " + str(matches))
    print()

    # ── Verdict ──
    print("=== VERDICT ===")
    if avg_pts > 0:
        monthly = round(avg_pts * (n / max(1, len(trading_days))) * 22, 1)
        monthly_rs = int(monthly * TOTAL_QTY)
        print("STRATEGY HAS POSITIVE EDGE (sample-limited)")
        print("Per-trade expectancy: " + "{:+.2f}".format(avg_pts) + " pts")
        print("Monthly projection:   " + "{:+.1f}".format(monthly) + " pts  Rs"
              + "{:,}".format(monthly_rs) + " @ " + str(LOT_COUNT) + " lots")
    else:
        print("STRATEGY HAS NEGATIVE EDGE (current form)")
        print("Per-trade expectancy: " + "{:.2f}".format(avg_pts) + " pts")
        print("Investigate: which gates/exits lose most? See breakdowns above.")
    print()
    print("WARNING: N=" + str(n) + " trades on " + str(len(trading_days))
          + " days. Statistical confidence is LOW.")
    print("Extend backfill to 30 days for reliable conclusions.")
    print("=" * 55)

    # ── Save CSV ──
    today_str = date.today().isoformat()
    csv_path = os.path.expanduser(
        "~/lab_data/strategy_backtest_" + today_str + ".csv")
    try:
        pd.DataFrame(trades).to_csv(csv_path, index=False)
        print("Trades CSV: " + csv_path)
    except Exception as e:
        print("CSV write error: " + str(e))

    rej_path = os.path.expanduser(
        "~/lab_data/strategy_backtest_rejections_" + today_str + ".csv")
    try:
        pd.DataFrame(rejections).to_csv(rej_path, index=False)
        print("Rejections CSV: " + rej_path)
    except Exception as e:
        print("Rejections CSV error: " + str(e))


if __name__ == "__main__":
    main()
