#!/home/user/kite_env/bin/python3
"""
import_csv_to_db.py — Import all existing CSV data into SQLite.
Run once: ~/kite_env/bin/python3 import_csv_to_db.py
"""

import csv
import glob
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import VRL_DB as DB

BASE = os.path.expanduser("~")
LAB = os.path.join(BASE, "lab_data")
SPOT_DIR = os.path.join(LAB, "spot")
OPT1_DIR = os.path.join(LAB, "options_1min")
OPT3_DIR = os.path.join(LAB, "options_3min")


def _read_csv(path):
    """Read CSV file, return list of dicts."""
    if not os.path.isfile(path):
        return []
    try:
        with open(path) as f:
            return list(csv.DictReader(f))
    except Exception as e:
        print(f"  ERROR reading {path}: {e}")
        return []


def _import_files(pattern, table, fields, insert_fn):
    """Import all files matching glob pattern into a table."""
    files = sorted(glob.glob(pattern))
    total = 0
    for fp in files:
        rows = _read_csv(fp)
        if not rows:
            continue
        insert_fn(rows)
        total += len(rows)
        print(f"  {os.path.basename(fp)}: {len(rows)} rows")
    print(f"  TOTAL {table}: {total} rows from {len(files)} files\n")
    return total


def main():
    print("=" * 60)
    print("  CSV → SQLite Import")
    print("  Database: " + DB.DB_PATH)
    print("=" * 60 + "\n")

    DB.init_db()

    # ── SPOT 1-MIN ──
    print("── SPOT 1-MIN ──")
    _import_files(
        os.path.join(SPOT_DIR, "nifty_spot_1min_*.csv"),
        "spot_1min", DB._SPOT_FIELDS, DB._insert_many_fn("spot_1min", DB._SPOT_FIELDS))

    # ── SPOT 5-MIN ──
    print("── SPOT 5-MIN ──")
    _import_files(
        os.path.join(SPOT_DIR, "nifty_spot_5min_*.csv"),
        "spot_5min", DB._SPOT_FIELDS, DB._insert_many_fn("spot_5min", DB._SPOT_FIELDS))

    # ── SPOT 15-MIN ──
    print("── SPOT 15-MIN ──")
    _import_files(
        os.path.join(SPOT_DIR, "nifty_spot_15min_*.csv"),
        "spot_15min", DB._SPOT_FIELDS, DB._insert_many_fn("spot_15min", DB._SPOT_FIELDS))

    # ── SPOT 60-MIN ──
    print("── SPOT 60-MIN ──")
    _import_files(
        os.path.join(SPOT_DIR, "nifty_spot_60min_*.csv"),
        "spot_60min", DB._SPOT_FIELDS, DB._insert_many_fn("spot_60min", DB._SPOT_FIELDS))

    # ── SPOT DAILY ──
    print("── SPOT DAILY ──")
    daily_path = os.path.join(SPOT_DIR, "nifty_spot_daily.csv")
    rows = _read_csv(daily_path)
    if rows:
        DB._insert_many("spot_daily", rows, DB._SPOT_DAILY_FIELDS)
        print(f"  nifty_spot_daily.csv: {len(rows)} rows\n")

    # ── OPTION 1-MIN ──
    print("── OPTION 1-MIN ──")
    _import_files(
        os.path.join(OPT1_DIR, "nifty_option_1min_*.csv"),
        "option_1min", DB._OPT_1M_FIELDS, DB._insert_many_fn("option_1min", DB._OPT_1M_FIELDS))

    # ── OPTION 3-MIN ──
    print("── OPTION 3-MIN ──")
    _import_files(
        os.path.join(OPT3_DIR, "nifty_option_3min_*.csv"),
        "option_3min", DB._OPT_3M_FIELDS, DB._insert_many_fn("option_3min", DB._OPT_3M_FIELDS))

    # ── OPTION 5-MIN ──
    print("── OPTION 5-MIN ──")
    _import_files(
        os.path.join(OPT1_DIR, "nifty_option_5min_*.csv"),
        "option_5min", DB._OPT_5M_FIELDS, DB._insert_many_fn("option_5min", DB._OPT_5M_FIELDS))

    # ── OPTION 15-MIN ──
    print("── OPTION 15-MIN ──")
    _import_files(
        os.path.join(OPT1_DIR, "nifty_option_15min_*.csv"),
        "option_15min", DB._OPT_15M_FIELDS, DB._insert_many_fn("option_15min", DB._OPT_15M_FIELDS))

    # ── SIGNAL SCANS ──
    print("── SIGNAL SCANS ──")
    _import_files(
        os.path.join(OPT1_DIR, "nifty_signal_scan_*.csv"),
        "signal_scans", DB._SCAN_FIELDS, DB._insert_many_fn("signal_scans", DB._SCAN_FIELDS))

    # ── TRADES ──
    print("── TRADES ──")
    trade_path = os.path.join(LAB, "vrl_trade_log.csv")
    rows = _read_csv(trade_path)
    if rows:
        DB._insert_many("trades", rows, DB._TRADE_FIELDS)
        print(f"  vrl_trade_log.csv: {len(rows)} rows\n")

    # ── SUMMARY ──
    print("=" * 60)
    print("  IMPORT COMPLETE")
    print("=" * 60)
    tables = DB.query("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
    for t in tables:
        cnt = DB.query(f"SELECT count(*) as n FROM {t['name']}")
        print(f"  {t['name']}: {cnt[0]['n']} rows")

    db_size = os.path.getsize(DB.DB_PATH)
    print(f"\n  Database size: {db_size / (1024*1024):.1f} MB")
    DB.close()


if __name__ == "__main__":
    main()
