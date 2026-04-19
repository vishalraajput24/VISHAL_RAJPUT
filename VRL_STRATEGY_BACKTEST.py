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

def _make_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="VRL v16.0.1 strategy backtest — Part 1 (data loader)")
    p.add_argument("--start-date", default=DEFAULT_START,
                   help="ISO date YYYY-MM-DD inclusive (default: " + DEFAULT_START + ")")
    p.add_argument("--end-date", default=DEFAULT_END,
                   help="ISO date YYYY-MM-DD inclusive (default: " + DEFAULT_END + ")")
    p.add_argument("--verbose", action="store_true",
                   help="Enable INFO-level logging")
    return p


def main():
    args = _make_arg_parser().parse_args()
    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    print("═══ BATCH 9 BACKTEST — PART 1 (data loader only) ═══")
    print("Date range requested: " + args.start_date + " to " + args.end_date)

    data = load_historical_data(args.start_date, args.end_date)
    meta = data["meta"]

    actual = meta["date_range_actual"]
    print("Actual data range:    " + str(actual[0]) + " to " + str(actual[1]))
    print("")
    print("Spot 1-min candles:   " + str(meta["rows_spot"]))
    print("Option 3-min candles: " + str(meta["rows_opt_3m"]))
    print("Option 1-min candles: " + str(meta["rows_opt_1m"]))
    print("Unique strikes (3m):  " + str(meta["unique_strikes_3m"]))
    print("Live trades loaded:   " + str(meta["rows_trades"]))
    print("")
    print("Part 1 complete. Ready for Part 2 (event loop).")


if __name__ == "__main__":
    main()
