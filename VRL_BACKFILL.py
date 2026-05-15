#!/usr/bin/env python3
"""
VRL_BACKFILL.py — One-time historical data backfill
Fetches as much data as Kite provides:
  - NIFTY spot 1-min: last 60 trading days (always available)
  - Options 3-min: all currently listed NIFTY expiries (last ~3-4 weeks)

Output matches VRL_COLLECTOR format: ~/lab_data/collector/YYYY-MM-DD/

Usage:
    python3 VRL_BACKFILL.py
    python3 VRL_BACKFILL.py --days 30     # fewer days for spot
"""

import argparse
import json
import os
import sys
import time
from datetime import date, datetime, timedelta
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import pandas as pd

import VRL_CONFIG as CFG
import VRL_DATA as D

STRIKE_RANGE  = 300
STRIKE_STEP   = 50
BACKFILL_DAYS = 60


def _log(msg):
    print(datetime.now().strftime("%H:%M:%S") + " | " + msg, flush=True)


def _save_day(trading_date: date, spot_day: pd.DataFrame,
              options_day: pd.DataFrame, vix: float, atm: int):
    """Save one day's data to the collector directory."""
    day_str  = trading_date.isoformat()
    save_dir = os.path.join(D.LAB_DIR, "collector", day_str)
    os.makedirs(save_dir, exist_ok=True)

    saved = []
    if not spot_day.empty:
        spot_day.to_parquet(os.path.join(save_dir, "spot_1min.parquet"))
        saved.append(f"spot={len(spot_day)}rows")

    if not options_day.empty:
        options_day.to_parquet(os.path.join(save_dir, "options_3min.parquet"))
        saved.append(f"opts={len(options_day)}rows")

    # Write / merge meta
    meta_path = os.path.join(save_dir, "meta.json")
    meta = {}
    if os.path.exists(meta_path):
        with open(meta_path) as f:
            meta = json.load(f)
    meta.update({
        "date"        : day_str,
        "atm"         : atm,
        "vix"         : round(vix, 2) if vix else meta.get("vix", 0),
        "backfilled"  : True,
    })
    with open(meta_path, "w") as f:
        json.dump(meta, f, indent=2)

    _log(f"  {day_str}: {', '.join(saved) if saved else 'meta only'}")


def _trading_days(n: int):
    """Return the last n calendar days that are weekdays (Mon-Fri)."""
    days = []
    d = date.today()
    while len(days) < n:
        if d.weekday() < 5:   # Mon=0 … Fri=4
            days.append(d)
        d -= timedelta(days=1)
    return days   # newest first


def backfill(days: int = BACKFILL_DAYS):
    _log(f"=== VRL_BACKFILL start: last {days} trading days ===")

    # ── Auth ────────────────────────────────────────────────────
    try:
        kite = CFG.get_kite()
        D.init(kite)
        _log("Auth OK")
    except Exception as e:
        _log("AUTH FAILED: " + str(e))
        sys.exit(1)

    t_days = _trading_days(days)
    from_dt = datetime.combine(t_days[-1], datetime.min.time())   # oldest day
    to_dt   = datetime.now()

    # ── 1. NIFTY spot 1-min (full range in one call) ────────────
    _log(f"Fetching NIFTY spot 1-min: {t_days[-1]} → {t_days[0]} ...")
    try:
        raw = kite.historical_data(
            instrument_token=D.NIFTY_SPOT_TOKEN,
            from_date=from_dt, to_date=to_dt,
            interval="minute", continuous=False, oi=False)
        spot_df = pd.DataFrame(raw)
        spot_df.rename(columns={"date": "timestamp"}, inplace=True)
        spot_df.set_index("timestamp", inplace=True)
        spot_df = spot_df[["open", "high", "low", "close", "volume"]].apply(
            pd.to_numeric, errors="coerce").dropna()
        _log(f"  Spot: {len(spot_df)} rows fetched")
    except Exception as e:
        _log("Spot fetch error: " + str(e))
        spot_df = pd.DataFrame()

    # Compute per-day ATM from spot close
    day_atm = {}
    for d in t_days:
        day_spot = spot_df[spot_df.index.date == d] if not spot_df.empty else pd.DataFrame()
        if not day_spot.empty:
            s = float(day_spot["close"].iloc[-1])
            day_atm[d] = int(round(s / STRIKE_STEP) * STRIKE_STEP)

    # ── 2. Available NIFTY option expiries from instruments ──────
    _log("Fetching NFO instruments ...")
    try:
        instruments = kite.instruments("NFO")
        nifty_opts  = [i for i in instruments
                       if i.get("name") == "NIFTY"
                       and i.get("instrument_type") in ("CE", "PE")]
        expiries = sorted(set(i["expiry"] for i in nifty_opts if i.get("expiry")))
        _log(f"  Available NIFTY expiries: {[str(e) for e in expiries]}")
    except Exception as e:
        _log("Instruments error: " + str(e))
        nifty_opts, expiries = [], []

    if not expiries:
        _log("No available expiries — skipping options collection")
    else:
        # Build token lookup: (expiry, strike, opt_type) → token
        tok_lookup = {}
        for i in nifty_opts:
            key = (i["expiry"], int(i.get("strike", 0)), i["instrument_type"])
            tok_lookup[key] = {
                "token" : i["instrument_token"],
                "symbol": i["tradingsymbol"],
            }

        # For each expiry, determine which trading days used it as front-month.
        # Front-month = smallest expiry >= that date.
        _log("Collecting options 3-min OHLC per expiry ...")
        for expiry in expiries:
            exp_days = [d for d in t_days if d <= expiry]
            # Only days where this was the nearest expiry
            front_days = []
            for d in exp_days:
                nearest = min((e for e in expiries if e >= d), default=None)
                if nearest == expiry:
                    front_days.append(d)

            if not front_days:
                continue

            exp_from = datetime.combine(min(front_days), datetime.min.time())
            exp_to   = datetime.combine(max(front_days), datetime.max.time())

            # Collect unique ATMs across these days
            atms_needed = set(day_atm.get(d) for d in front_days if d in day_atm)
            atms_needed.discard(None)
            if not atms_needed:
                _log(f"  Expiry {expiry}: no ATM data for front days — skip")
                continue

            # Strike range: union of ATM±300 for all days
            strikes_needed = set()
            for atm in atms_needed:
                n = STRIKE_RANGE // STRIKE_STEP
                for i in range(-n, n + 1):
                    strikes_needed.add(atm + i * STRIKE_STEP)

            _log(f"  Expiry {expiry}: {len(front_days)} days, "
                 f"{len(strikes_needed)} strikes, ATMs={sorted(atms_needed)}")

            # Fetch 3-min for each strike×type
            series_rows = defaultdict(list)   # date → list of dfs
            for strike in sorted(strikes_needed):
                for opt_type in ("CE", "PE"):
                    key = (expiry, strike, opt_type)
                    if key not in tok_lookup:
                        continue
                    tok  = tok_lookup[key]["token"]
                    sym  = tok_lookup[key]["symbol"]
                    try:
                        raw = kite.historical_data(
                            instrument_token=tok,
                            from_date=exp_from, to_date=exp_to,
                            interval="3minute", continuous=False, oi=False)
                        if not raw:
                            continue
                        df = pd.DataFrame(raw)
                        df.rename(columns={"date": "timestamp"}, inplace=True)
                        df.set_index("timestamp", inplace=True)
                        df = df[["open", "high", "low", "close", "volume"]].apply(
                            pd.to_numeric, errors="coerce").dropna()
                        df = D.add_indicators(df)
                        df["strike"]   = strike
                        df["opt_type"] = opt_type
                        df["symbol"]   = sym
                        df["token"]    = tok
                        # Split into per-day buckets
                        for day_d in front_days:
                            day_slice = df[df.index.date == day_d]
                            if not day_slice.empty:
                                series_rows[day_d].append(day_slice)
                        time.sleep(0.05)
                    except Exception as e:
                        _log(f"    {sym} error: {e}")

            # Save per day
            for day_d in front_days:
                parts = series_rows.get(day_d, [])
                opts_day = pd.concat(parts).sort_index() if parts else pd.DataFrame()
                spot_day = spot_df[spot_df.index.date == day_d] if not spot_df.empty else pd.DataFrame()
                atm      = day_atm.get(day_d, 0)
                _save_day(day_d, spot_day, opts_day, vix=0, atm=atm)

    # ── 3. Spot-only days (no options available) ─────────────────
    # Save spot data for days that didn't get saved above
    _log("Saving spot-only days ...")
    saved_days = set()
    collector_root = os.path.join(D.LAB_DIR, "collector")
    if os.path.isdir(collector_root):
        for d in os.listdir(collector_root):
            try:
                saved_days.add(date.fromisoformat(d))
            except Exception:
                pass

    for day_d in t_days:
        if day_d in saved_days:
            continue
        spot_day = spot_df[spot_df.index.date == day_d] if not spot_df.empty else pd.DataFrame()
        atm      = day_atm.get(day_d, 0)
        _save_day(day_d, spot_day, pd.DataFrame(), vix=0, atm=atm)

    _log("=== VRL_BACKFILL done ===")
    _log(f"Data in: {collector_root}")
    _log("Options coverage limited to expiries still in Kite instruments list.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=BACKFILL_DAYS,
                        help="Number of trading days to backfill (default: 60)")
    args = parser.parse_args()
    backfill(args.days)
