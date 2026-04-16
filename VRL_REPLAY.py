#!/usr/bin/env python3
"""
═══════════════════════════════════════════════════════════════
 VRL_REPLAY.py — v15.2.5 record-replay verification

 Reads the most recent N trades from ~/lab_data/vrl_data.db and
 replays the entry result dict against the current VRL_ENGINE
 classification logic. For each trade it prints:

   - what the DB says actually happened at entry
     (straddle_delta, ema9_high, body_pct, vwap_bonus, mode)
   - what VRL_ENGINE would classify the same snapshot as TODAY
     (straddle_info STRONG/NEUTRAL/WEAK/NA, band width, etc)
   - mismatches between the two

 This is the Option-A verification: tests don't mock anything,
 they use real rows you've already collected.

 Usage:
     python3 ~/VISHAL_RAJPUT/VRL_REPLAY.py             # last 10 trades
     python3 ~/VISHAL_RAJPUT/VRL_REPLAY.py --n 30
     python3 ~/VISHAL_RAJPUT/VRL_REPLAY.py --date 2026-04-16
═══════════════════════════════════════════════════════════════
"""
import argparse
import os
import sqlite3
import sys
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

DB_PATH = os.path.expanduser("~/lab_data/vrl_data.db")


def _classify_straddle(sd):
    """Mirror the live engine logic exactly (VRL_ENGINE Gate 7 v15.2.5 Fix 5)."""
    if sd is None:
        return "NA"
    try:
        sd = float(sd)
    except Exception:
        return "NA"
    if sd >= 5:    return "STRONG"
    if sd >= 0:    return "NEUTRAL"
    return "WEAK"


def _be2_would_fire(peak, entry, exit_price):
    """Would BE+2 have fired at peak >= 10 and price dropped to entry+2?
    Just a sanity check — caller already saw the exit reason."""
    if peak is None or entry is None or exit_price is None:
        return None
    if float(peak) < 10:
        return False
    return float(exit_price) <= float(entry) + 2.0


def _velocity_would_have_fired(peak_pnl):
    """We don't have peak_history per-candle in the trade row, so we
    can't fully replay VELOCITY_STALL. This is informational only."""
    return "n/a (needs peak_history per-candle — not in trades table)"


def replay(n: int = 10, day: str = None):
    if not os.path.isfile(DB_PATH):
        print("[FATAL] DB not found:", DB_PATH)
        sys.exit(2)

    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row

    if day:
        rows = conn.execute(
            "SELECT * FROM trades WHERE date = ? ORDER BY entry_time", (day,)
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM trades ORDER BY date DESC, entry_time DESC LIMIT ?",
            (n,)).fetchall()

    if not rows:
        print("no trades in window")
        conn.close()
        return 0

    print("VRL REPLAY  |  " + str(len(rows)) + " trades"
          + (" on " + day if day else "") + "  |  " + DB_PATH)
    print("=" * 96)

    mismatches = 0
    for r in rows:
        d   = r["date"]
        et  = r["entry_time"]
        xt  = r["exit_time"]
        sym = r["symbol"] or ""
        dir_= r["direction"] or ""
        ep  = r["entry_price"]
        xp  = r["exit_price"]
        pnl = r["pnl_pts"]
        peak= r["peak_pnl"]
        ch  = r["candles_held"] or 0
        why = r["exit_reason"] or ""
        mode= r["entry_mode"] or ""

        sd  = r["entry_straddle_delta"]
        si  = r["entry_straddle_info"] if "entry_straddle_info" in r.keys() else ""
        spd = r["entry_straddle_period"] or ""
        bw  = r["entry_band_width"] or 0
        bp  = r["entry_body_pct"] or 0
        atm = r["entry_atm_strike"] or 0
        eh  = r["entry_ema9_high"] or 0
        el  = r["entry_ema9_low"] or 0
        vwb = r["entry_vwap_bonus"] if "entry_vwap_bonus" in r.keys() else ""

        print()
        print(d + " " + et + " → " + xt + "  " + dir_ + " " + sym)
        print("  entry " + str(ep) + " → exit " + str(xp)
              + "  pnl " + ("+" if (pnl or 0) >= 0 else "")
              + str(pnl) + "  peak " + str(peak)
              + "  candles " + str(ch) + "  reason " + why
              + "  mode " + mode)

        # ── Record vs classifier ─────────────────────────────────
        now_label = _classify_straddle(sd)
        tag = "MATCH" if si and si == now_label else (
              "NULL " if not si else "DRIFT")
        if tag == "DRIFT":
            mismatches += 1
        print("  STRADDLE:  sd=" + str(sd)
              + "  stored_info='" + str(si) + "'"
              + "  → engine would now classify as " + now_label
              + "  [" + tag + "]")

        # ── Metadata completeness ────────────────────────────────
        missing = []
        if atm == 0: missing.append("entry_atm_strike")
        if bw == 0:  missing.append("entry_band_width")
        if bp == 0:  missing.append("entry_body_pct")
        if eh == 0:  missing.append("entry_ema9_high")
        if el == 0:  missing.append("entry_ema9_low")
        if not vwb:  missing.append("entry_vwap_bonus")
        if not spd:  missing.append("entry_straddle_period")
        if not si:   missing.append("entry_straddle_info")
        if missing:
            mismatches += 1
            print("  METADATA:  " + str(len(missing))
                  + " v15.2 cols unpopulated: " + ", ".join(missing))
        else:
            print("  METADATA:  all 8 v15.2 entry cols populated ✓")

        # ── Would the new VELOCITY_STALL rule have helped? ───────
        if (peak or 0) >= 10 and why in ("BREAKEVEN_LOCK", "TRAIL_FLOOR"):
            print("  WHAT-IF :  peak " + str(peak)
                  + " + exit " + why + " — VELOCITY_STALL could have"
                  + " caught momentum death earlier (needs replay of"
                  + " per-candle peak_history to verify)")

    conn.close()
    print()
    print("=" * 96)
    print("replay summary: " + str(len(rows)) + " trades checked, "
          + str(mismatches) + " row(s) with metadata drift or classification"
          + " mismatch vs engine.")
    print("If metadata drift > 0: run VRL_DB_AUDIT.py --fix (re-applies "
          "ALTER TABLE migrations) and redeploy.")
    return 0 if mismatches == 0 else 1


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--n",    type=int, default=10, help="last N trades")
    ap.add_argument("--date", type=str, default=None, help="YYYY-MM-DD")
    args = ap.parse_args()
    sys.exit(replay(n=args.n, day=args.date))
