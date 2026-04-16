#!/usr/bin/env python3
"""
═══════════════════════════════════════════════════════════════
 VRL_BACKFILL_BANDS.py — VISHAL RAJPUT TRADE v15.2

 BUG 5 fix: backfill ema9_high / ema9_low for any option_3min rows
 in the database where the bands are still 0. Recomputes the
 9-period EWM of high and 9-period EWM of low per (strike, type),
 grouped by date so the cross-day boundaries don't bleed.

 Safe to re-run. Idempotent: only updates rows where ema9_high=0
 OR ema9_low=0. Does not touch trades or signal_scans.

 Usage:
     python3 VRL_BACKFILL_BANDS.py            # backfill ALL dates
     python3 VRL_BACKFILL_BANDS.py 2026-04-15 # backfill one date
═══════════════════════════════════════════════════════════════
"""
import os
import sys
import sqlite3
from collections import defaultdict

DB_PATH = os.path.expanduser("~/lab_data/vrl_data.db")


def _ewm9(values):
    """Plain Python 9-period EWM (alpha=2/(9+1)=0.2). Matches pandas.ewm(span=9, adjust=False)."""
    if not values:
        return []
    alpha = 2.0 / (9 + 1)
    out = [values[0]]
    for v in values[1:]:
        out.append(alpha * v + (1 - alpha) * out[-1])
    return out


def main(target_date=None):
    if not os.path.isfile(DB_PATH):
        print("DB not found: " + DB_PATH)
        return 1

    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row

    where = ("WHERE (ema9_high IS NULL OR ema9_high = 0 "
             "OR ema9_low IS NULL OR ema9_low = 0)")
    params = []
    if target_date:
        where += " AND date(timestamp) = ?"
        params.append(target_date)

    rows = conn.execute(
        "SELECT rowid, timestamp, strike, type, high, low, ema9_high, ema9_low "
        "FROM option_3min " + where + " "
        "ORDER BY date(timestamp), strike, type, timestamp",
        params,
    ).fetchall()

    if not rows:
        print("Nothing to backfill" + (" for " + target_date if target_date else ""))
        conn.close()
        return 0

    # Group by (date, strike, type) so each option track stays clean
    groups = defaultdict(list)
    for r in rows:
        date_key = (r["timestamp"] or "")[:10]
        key = (date_key, r["strike"], r["type"])
        groups[key].append({
            "rowid": r["rowid"],
            "high":  float(r["high"] or 0),
            "low":   float(r["low"]  or 0),
        })

    # We need the ENTIRE ordered series per (date, strike, type) — including
    # rows that already have non-zero bands — so the EWM warms up correctly.
    # Re-fetch the full track per group.
    total_updated = 0
    cur = conn.cursor()
    for (d, strike, typ), _items in groups.items():
        full = conn.execute(
            "SELECT rowid, timestamp, high, low, ema9_high, ema9_low "
            "FROM option_3min "
            "WHERE date(timestamp) = ? AND strike = ? AND type = ? "
            "ORDER BY timestamp",
            (d, strike, typ),
        ).fetchall()
        if not full:
            continue
        highs = [float(r["high"] or 0) for r in full]
        lows  = [float(r["low"]  or 0) for r in full]
        eh = _ewm9(highs)
        el = _ewm9(lows)
        # Update only zero rows
        for i, r in enumerate(full):
            curr_eh = float(r["ema9_high"] or 0)
            curr_el = float(r["ema9_low"]  or 0)
            if curr_eh == 0 or curr_el == 0:
                cur.execute(
                    "UPDATE option_3min SET ema9_high = ?, ema9_low = ? WHERE rowid = ?",
                    (round(eh[i], 2), round(el[i], 2), r["rowid"]),
                )
                total_updated += 1

    conn.commit()
    conn.close()
    print("Backfilled " + str(total_updated) + " option_3min rows"
          + (" for " + target_date if target_date else ""))
    return 0


if __name__ == "__main__":
    arg = sys.argv[1] if len(sys.argv) > 1 else None
    sys.exit(main(arg))
