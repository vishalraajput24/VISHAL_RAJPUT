#!/usr/bin/env python3
"""
VRL_COLLECTOR.py — EOD options data snapshot
Fetches full-day 3-min OHLC for ATM±300 strikes (CE+PE),
NIFTY spot 1-min, and VIX. Saves as Parquet for backtesting.

Cron (add via: crontab -e):
    35 15 * * 1-5 cd ~/VISHAL_RAJPUT && python VRL_COLLECTOR.py >> ~/logs/collector.log 2>&1
"""

import json
import os
import sys
import time
from datetime import date, datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import pandas as pd

import VRL_CONFIG as CFG
import VRL_DATA as D

STRIKE_RANGE = 300   # ATM ± this many points
STRIKE_STEP  = 50    # NIFTY strike spacing

def _log(msg):
    print(datetime.now().strftime("%Y-%m-%d %H:%M:%S") + " | " + msg, flush=True)


def _get_today_df(df: pd.DataFrame) -> pd.DataFrame:
    """Filter dataframe to today's date only."""
    if df.empty:
        return df
    today_str = date.today().isoformat()
    try:
        return df[df.index.date == date.today()]
    except Exception:
        return df


def collect():
    today     = date.today()
    today_str = today.isoformat()
    save_dir  = os.path.join(D.LAB_DIR, "collector", today_str)
    os.makedirs(save_dir, exist_ok=True)

    _log("=== VRL_COLLECTOR start: " + today_str + " ===")

    # ── Authenticate ────────────────────────────────────────────
    try:
        kite = CFG.get_kite()
        D.init(kite)
        _log("Auth OK")
    except Exception as e:
        _log("AUTH FAILED: " + str(e))
        sys.exit(1)

    # ── Spot price (use historical to get exact last close) ──────
    try:
        spot_df_raw = D.get_historical_data(D.NIFTY_SPOT_TOKEN, "minute", 400)
        spot_df_today = _get_today_df(spot_df_raw)
        if not spot_df_today.empty:
            spot = float(spot_df_today["close"].iloc[-1])
        else:
            spot = D.get_nifty_spot() or 0
        atm = int(round(spot / STRIKE_STEP) * STRIKE_STEP)
        _log(f"Spot={spot:.1f}  ATM={atm}")
    except Exception as e:
        _log("Spot fetch error: " + str(e))
        spot, atm = 0, 23500

    # ── Nearest expiry ───────────────────────────────────────────
    try:
        expiry = D.get_nearest_expiry(kite)
        _log("Expiry: " + str(expiry))
    except Exception as e:
        _log("Expiry error: " + str(e))
        sys.exit(1)

    # ── Strike range ─────────────────────────────────────────────
    n_steps = STRIKE_RANGE // STRIKE_STEP
    strikes = [atm + i * STRIKE_STEP for i in range(-n_steps, n_steps + 1)]
    _log(f"Strikes: {strikes[0]} → {strikes[-1]} ({len(strikes)} strikes)")

    # ── Collect options 3-min OHLC ───────────────────────────────
    option_rows = []
    failed = []
    for strike in strikes:
        try:
            tokens = D.get_option_tokens(kite, strike, expiry)
        except Exception as e:
            _log(f"  token lookup failed strike={strike}: {e}")
            failed.append(strike)
            continue

        for opt_type in ("CE", "PE"):
            if opt_type not in tokens:
                failed.append((strike, opt_type))
                continue
            token  = tokens[opt_type]["token"]
            symbol = tokens[opt_type]["symbol"]
            try:
                df = D.get_historical_data(token, "3minute", 120)
                df = _get_today_df(df)
                if df.empty:
                    _log(f"  {opt_type} {strike}: empty")
                    continue
                df = D.add_indicators(df)
                df["strike"]   = strike
                df["opt_type"] = opt_type
                df["symbol"]   = symbol
                df["token"]    = token
                option_rows.append(df)
                time.sleep(0.05)   # ~20 req/s — well under Kite limit
            except Exception as e:
                _log(f"  {opt_type} {strike} error: {e}")
                failed.append((strike, opt_type))

    if option_rows:
        options_df = pd.concat(option_rows).sort_index()
        out_path = os.path.join(save_dir, "options_3min.parquet")
        options_df.to_parquet(out_path)
        _log(f"options_3min.parquet: {len(options_df)} rows, {len(option_rows)} series → {out_path}")
    else:
        _log("WARNING: no options data collected")

    # ── NIFTY spot 1-min ─────────────────────────────────────────
    try:
        spot_out = _get_today_df(spot_df_raw)
        if not spot_out.empty:
            spot_path = os.path.join(save_dir, "spot_1min.parquet")
            spot_out.to_parquet(spot_path)
            _log(f"spot_1min.parquet: {len(spot_out)} rows → {spot_path}")
        else:
            _log("WARNING: no spot data for today")
    except Exception as e:
        _log("Spot save error: " + str(e))

    # ── VIX ──────────────────────────────────────────────────────
    try:
        vix = D.get_vix()
        _log(f"VIX: {vix}")
    except Exception as e:
        vix = 0
        _log("VIX error: " + str(e))

    # ── Meta ─────────────────────────────────────────────────────
    meta = {
        "date"        : today_str,
        "collected_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "atm"         : atm,
        "spot"        : round(spot, 2),
        "vix"         : round(vix, 2),
        "expiry"      : str(expiry),
        "strikes"     : strikes,
        "series_ok"   : len(option_rows),
        "series_failed": len(failed),
    }
    meta_path = os.path.join(save_dir, "meta.json")
    with open(meta_path, "w") as f:
        json.dump(meta, f, indent=2)
    _log("meta.json written")

    if failed:
        _log("Failed series: " + str(failed))

    _log("=== VRL_COLLECTOR done ===")
    return meta


if __name__ == "__main__":
    collect()
