#!/usr/bin/env python3
# ═══════════════════════════════════════════════════════════════
#  VRL_BACKFILL_OPTIONS.py — One-time 5-day option history backfill
#  BUG-R12: Ensures research module has enough candle data.
#
#  Usage (in kite venv):
#    python3 VRL_BACKFILL_OPTIONS.py
#
#  Run ONCE manually after Batch 8 code deploys.
# ═══════════════════════════════════════════════════════════════

import os
import sys
import time
from datetime import date, datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import VRL_DATA as D
import VRL_DB as DB


def _get_trading_days(n_days=5):
    """Return last N trading days (exclude weekends)."""
    days = []
    d = date.today() - timedelta(days=1)
    while len(days) < n_days:
        if d.weekday() < 5:
            days.append(d)
        d -= timedelta(days=1)
    return sorted(days)


def _get_spot_close_for_date(d):
    """Estimate spot close from DB or use a fallback."""
    import sqlite3
    db_path = os.path.expanduser("~/lab_data/vrl_data.db")
    try:
        conn = sqlite3.connect(db_path)
        row = conn.execute(
            "SELECT close FROM spot_daily WHERE date=? LIMIT 1",
            (d.isoformat(),)
        ).fetchone()
        conn.close()
        if row and float(row[0]) > 0:
            return float(row[0])
    except Exception:
        pass
    try:
        conn = sqlite3.connect(db_path)
        row = conn.execute(
            "SELECT close FROM spot_1min WHERE date(timestamp)=? "
            "ORDER BY timestamp DESC LIMIT 1",
            (d.isoformat(),)
        ).fetchone()
        conn.close()
        if row and float(row[0]) > 0:
            return float(row[0])
    except Exception:
        pass
    return 0.0


def run_backfill():
    print("=" * 50)
    print("VRL_BACKFILL_OPTIONS — 5-day option history backfill")
    print("=" * 50)

    from VRL_AUTH import get_kite
    kite = get_kite()
    if not kite:
        print("ERROR: Could not authenticate with Kite. Run manually.")
        sys.exit(1)
    D.set_kite(kite)

    expiry = D.get_nearest_expiry(kite)
    if not expiry:
        print("ERROR: Could not resolve nearest expiry.")
        sys.exit(1)
    print("Expiry:", expiry)

    trading_days = _get_trading_days(5)
    print("Trading days:", [str(d) for d in trading_days])
    print()

    total_inserted = 0
    total_dupes = 0

    for td in trading_days:
        spot_close = _get_spot_close_for_date(td)
        if spot_close <= 0:
            print("  " + str(td) + ": no spot close data, skipping")
            continue

        atm = D.resolve_atm_strike(spot_close)
        strikes = [atm - 100, atm - 50, atm, atm + 50, atm + 100]
        print("  " + str(td) + ": spot=" + str(round(spot_close, 1))
              + " ATM=" + str(atm) + " strikes=" + str(strikes))

        day_inserted = 0
        for strike in strikes:
            tokens = D.get_option_tokens(kite, strike, expiry)
            if not tokens:
                print("    Strike " + str(strike) + ": no tokens found")
                continue

            for side in ("CE", "PE"):
                info = tokens.get(side, {})
                token = info.get("token")
                if not token:
                    continue

                for tf, table_fn in [("3minute", DB.insert_option_3min_many),
                                     ("minute", DB.insert_option_1min_many)]:
                    from_dt = datetime.combine(td, datetime.min.time()).replace(
                        hour=9, minute=15)
                    to_dt = datetime.combine(td, datetime.min.time()).replace(
                        hour=15, minute=30)

                    try:
                        time.sleep(0.4)
                        raw = kite.historical_data(
                            instrument_token=int(token),
                            from_date=from_dt, to_date=to_dt,
                            interval=tf, continuous=False, oi=False)
                    except Exception as e:
                        print("    " + side + " " + str(strike) + " " + tf
                              + ": fetch error " + str(e)[:80])
                        continue

                    if not raw:
                        continue

                    rows = []
                    for r in raw:
                        ts = r.get("date")
                        if ts and hasattr(ts, "strftime"):
                            ts = ts.strftime("%Y-%m-%d %H:%M:%S")
                        rows.append({
                            "timestamp": str(ts),
                            "strike": strike,
                            "type": side,
                            "open": float(r.get("open", 0)),
                            "high": float(r.get("high", 0)),
                            "low": float(r.get("low", 0)),
                            "close": float(r.get("close", 0)),
                            "volume": float(r.get("volume", 0)),
                        })

                    if rows:
                        table_fn(rows)
                        day_inserted += len(rows)

        total_inserted += day_inserted
        print("    → " + str(day_inserted) + " candles inserted")

    print()
    print("=" * 50)
    print("BACKFILL COMPLETE")
    print("Total candles inserted:", total_inserted)
    print("Date range:", str(trading_days[0]), "→", str(trading_days[-1]))
    print("(INSERT OR IGNORE — duplicates silently skipped)")
    print("Ready for: python3 VRL_RESEARCH_BACKTEST.py --days 30")
    print("=" * 50)


if __name__ == "__main__":
    run_backfill()
