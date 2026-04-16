#!/usr/bin/env python3
"""
═══════════════════════════════════════════════════════════════
 VRL_DB_AUDIT.py — v15.2.5 DB health / mapping audit

 Opens ~/lab_data/vrl_data.db and validates:
   1. Every column the code INSERTs exists in the live table.
   2. Flags any column in the schema that the code never writes to
      (zombie DEFAULT columns).
   3. For trades + signal_scans, samples the most recent N rows
      per day and reports any column that's 0/'' on 100% of rows.
      That catches the v15.2 "entry_straddle_delta always zero"
      class of bug end-to-end.
   4. Confirms ema9_high / ema9_low are non-zero on 95%+ of recent
      option_3min rows.

 Usage:
     python3 ~/VISHAL_RAJPUT/VRL_DB_AUDIT.py
     python3 ~/VISHAL_RAJPUT/VRL_DB_AUDIT.py --fix    # run migrations again
     python3 ~/VISHAL_RAJPUT/VRL_DB_AUDIT.py --days 7 # sample horizon
═══════════════════════════════════════════════════════════════
"""
import argparse
import os
import sqlite3
import sys
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

DB_PATH = os.path.expanduser("~/lab_data/vrl_data.db")

# ── Expected CODE INSERT fields per table ──────────────────────
# These are imported from VRL_DB so the audit stays in lock-step
# with the live INSERT path. Any future code edit that adds a
# column automatically becomes part of the check.
try:
    import VRL_DB as _DB
except Exception as e:
    print("[FATAL] cannot import VRL_DB:", e)
    sys.exit(2)

CODE_FIELDS = {
    "spot_1min":    _DB._SPOT_FIELDS,
    "spot_5min":    _DB._SPOT_FIELDS,
    "spot_15min":   _DB._SPOT_FIELDS,
    "spot_60min":   _DB._SPOT_FIELDS,
    "spot_daily":   _DB._SPOT_DAILY_FIELDS,
    "option_1min":  _DB._OPT_1M_FIELDS,
    "option_3min":  _DB._OPT_3M_FIELDS,
    "option_5min":  _DB._OPT_5M_FIELDS,
    "option_15min": _DB._OPT_15M_FIELDS,
    "signal_scans": _DB._SCAN_FIELDS,
    "trades":       _DB._TRADE_FIELDS,
}

# Columns the code writes but where 0 / '' is a legitimate value
# (so missing all day isn't necessarily a bug). Excluded from the
# "always-zero" check to avoid false positives.
EXPECTED_SPARSE = {
    "trades": {
        "bonus_vwap", "bonus_fib_level", "bonus_fib_dist",
        "bonus_vol_spike", "bonus_vol_ratio", "bonus_pdh_break",
        "entry_slippage", "exit_slippage", "signal_price",
        "trough_pnl", "momentum_pts", "rsi_rising",
        "spot_confirms", "spot_move", "straddle_decay",
        "exit_phase", "score",
        # v13 dead fields — still in _TRADE_FIELDS but v15.x state never
        # populates them. Audited 2026-04-16 across 40 rows, confirmed
        # always zero/empty. Kept in schema for CSV back-compat.
        "iv_at_entry", "spread_1m", "spread_3m", "delta_at_entry",
        "regime", "sl_pts",
        # Exit-side band is 0 when trade exited without band ever populating
        "exit_ema9_high", "exit_ema9_low", "exit_band_position",
    },
    "signal_scans": {
        "fwd_3c", "fwd_5c", "fwd_10c", "fwd_outcome",
        "near_fib_level", "fib_distance",
        "score", "iv_pct", "delta",
        "rsi_rising_1m", "body_pct_1m", "vol_ratio_1m", "spread_1m",
        "rsi_1m",
        "straddle_decay_pct",
    },
    "option_3min": {
        "iv_vs_open", "ema_spread", "fwd_3c", "fwd_6c", "fwd_9c",
        "fwd_outcome", "iv_pct", "delta", "gamma", "theta", "vega",
        "volume_ratio",
    },
}


def _pragma_cols(conn, tbl):
    rows = conn.execute("PRAGMA table_info(" + tbl + ")").fetchall()
    return [r[1] for r in rows]  # [1] = name


def _count(conn, sql, params=()):
    r = conn.execute(sql, params).fetchone()
    return int(r[0] if r else 0)


def audit(days: int = 3, fix: bool = False):
    if not os.path.isfile(DB_PATH):
        print("[FATAL] DB not found:", DB_PATH)
        sys.exit(2)

    # Optional: rerun migrations in-place before the read-only audit.
    # init_db() is idempotent (CREATE TABLE IF NOT EXISTS + ALTER TABLE
    # wrapped in try/except).
    if fix:
        print("Running VRL_DB.init_db() to re-apply idempotent migrations ...")
        _DB.init_db()
        print("  done.\n")

    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row

    print("VRL DB AUDIT  |  " + DB_PATH)
    print("=" * 74)

    any_drift = False

    # ── 1. Schema coverage per table ─────────────────────────────
    print("\n1. SCHEMA vs CODE INSERT FIELDS")
    for tbl, expected in CODE_FIELDS.items():
        live_cols = set(_pragma_cols(conn, tbl))
        missing = [c for c in expected if c not in live_cols]
        if missing:
            any_drift = True
            print("  [MISSING] " + tbl + " lacks " + str(len(missing))
                  + " cols the code writes: " + ", ".join(missing[:5])
                  + (" ..." if len(missing) > 5 else ""))
            print("            → fix: python3 VRL_DB_AUDIT.py --fix")
        else:
            # zombie cols = in live schema but not in CODE_FIELDS
            zombies = sorted(live_cols - set(expected))
            tag = ("  OK       " if not zombies
                   else "  NOTE     ")
            extra = (" (zombies: " + ", ".join(zombies[:3])
                     + ("..." if len(zombies) > 3 else "") + ")"
                     if zombies else "")
            print(tag + tbl + "  " + str(len(expected))
                  + " code cols ⊆ " + str(len(live_cols))
                  + " live cols" + extra)

    # ── 2. Always-zero / always-empty columns on recent trades + scans ──
    cutoff = (date.today() - timedelta(days=days)).isoformat()
    print("\n2. ALWAYS-ZERO COLUMNS on last " + str(days) + " days of trades + scans")
    print("   (a column flagged here means every sampled row had 0/'' for it —")
    print("    legitimate bug post-restart, expected for pre-fix historical rows)")
    for tbl, date_col in (("trades", "date"),
                          ("signal_scans", "date(timestamp)")):
        n = _count(conn,
                   "SELECT COUNT(*) FROM " + tbl + " WHERE "
                   + date_col + " >= ?",
                   (cutoff,))
        if n == 0:
            print("   " + tbl + ": 0 rows in sample window — skip")
            continue
        expected_cols = [c for c in CODE_FIELDS.get(tbl, [])
                         if c not in EXPECTED_SPARSE.get(tbl, set())]
        zero_cols = []
        empty_cols = []
        for col in expected_cols:
            cc = conn.execute("PRAGMA table_info(" + tbl + ")").fetchall()
            types = {r[1]: (r[2] or "").upper() for r in cc}
            t = types.get(col, "")
            if "REAL" in t or "INT" in t or "NUM" in t:
                bad = _count(conn,
                             "SELECT COUNT(*) FROM " + tbl
                             + " WHERE " + date_col + " >= ? AND ("
                             + col + " IS NULL OR " + col + " = 0)",
                             (cutoff,))
                if bad == n:
                    zero_cols.append(col)
            else:  # TEXT
                bad = _count(conn,
                             "SELECT COUNT(*) FROM " + tbl
                             + " WHERE " + date_col + " >= ? AND ("
                             + col + " IS NULL OR " + col + " = '')",
                             (cutoff,))
                if bad == n:
                    empty_cols.append(col)
        if zero_cols or empty_cols:
            any_drift = True
            print("   [ALL-ZERO] " + tbl + " (" + str(n) + " rows)")
            if zero_cols:
                print("     numeric cols always 0:  " + ", ".join(zero_cols))
            if empty_cols:
                print("     text cols always empty: " + ", ".join(empty_cols))
        else:
            print("   OK        " + tbl + "  " + str(n)
                  + " rows, every non-sparse col has live values somewhere")

    # ── 3. EMA9 band health on option_3min ───────────────────────
    print("\n3. EMA9 BAND COLUMNS on option_3min (last " + str(days) + " days)")
    n_opt = _count(conn,
                   "SELECT COUNT(*) FROM option_3min "
                   "WHERE date(timestamp) >= ?", (cutoff,))
    if n_opt == 0:
        print("   no rows in window")
    else:
        n_zero = _count(conn,
                        "SELECT COUNT(*) FROM option_3min "
                        "WHERE date(timestamp) >= ? "
                        "AND (ema9_high = 0 OR ema9_low = 0)",
                        (cutoff,))
        pct_good = round((n_opt - n_zero) / n_opt * 100, 1)
        verdict = "OK" if pct_good >= 95 else "DRIFT"
        if pct_good < 95:
            any_drift = True
        print("   " + verdict + "  " + str(n_opt - n_zero) + "/"
              + str(n_opt) + " rows have non-zero bands ("
              + str(pct_good) + "%)")
        if n_zero and pct_good < 95:
            print("       → fix: python3 VRL_BACKFILL_BANDS.py")

    # ── 4. signal_scans v14 label leak check ─────────────────────
    print("\n4. signal_scans reject_reason label sanity (v14 leak?)")
    if _count(conn, "SELECT COUNT(*) FROM signal_scans "
              "WHERE date(timestamp) >= ?", (cutoff,)):
        n_v14 = _count(conn,
                       "SELECT COUNT(*) FROM signal_scans "
                       "WHERE date(timestamp) >= ? AND reject_reason LIKE ?",
                       (cutoff, "EMA_%_RSI_%"))
        if n_v14 > 0:
            any_drift = True
            print("   [LEAK] " + str(n_v14) + " scans in window still have "
                  "v14 'EMA_X_RSI_Y' labels — pre-fix data, expected until "
                  "tomorrow's session")
        else:
            print("   OK   no v14 labels in window")

    conn.close()

    print()
    print("=" * 74)
    print("result:", ("⚠️  drift — see items above" if any_drift
                      else "✓ DB matches code expectations"))
    return 0 if not any_drift else 1


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=3,
                    help="sample window in days (default 3)")
    ap.add_argument("--fix", action="store_true",
                    help="run VRL_DB.init_db() to re-apply migrations before audit")
    args = ap.parse_args()
    sys.exit(audit(days=args.days, fix=args.fix))
