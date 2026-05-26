#!/usr/bin/env python3
"""
VRL_COLLECTOR.py — EOD options data snapshot (v2 — weekly expiry structure)

Saves per weekly Tuesday expiry:
  lab_data/collector/expiry_YYYYMMDD/
    3min/YYYY-MM-DD.parquet   ← ATM±300 strikes, all day
    1min/YYYY-MM-DD.parquet   ← ATM±5 strikes only (Shadow-DTF backtest)
  lab_data/collector/spot/YYYY-MM-DD.parquet
  lab_data/collector/meta/YYYY-MM-DD.json

Cron (runs at 15:35 every weekday):
    35 15 * * 1-5 cd ~/VISHAL_RAJPUT && /home/vishalraajput24/kite_env/bin/python3 VRL_COLLECTOR.py >> ~/logs/collector.log 2>&1
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

STRIKE_RANGE     = 300  # ATM ± this for 3-min full capture
STRIKE_STEP      = 50   # NIFTY strike spacing
SHADOW_STEPS     = 5    # ATM ± this for 1-min Shadow-DTF capture (11 strikes)


def _log(msg):
    print(datetime.now().strftime("%Y-%m-%d %H:%M:%S") + " | " + msg, flush=True)


def _last_trading_date(df: pd.DataFrame):
    try:
        return df.index.date.max()
    except Exception:
        return date.today()


def _get_session_df(df: pd.DataFrame, trading_date) -> pd.DataFrame:
    if df.empty:
        return df
    try:
        return df[df.index.date == trading_date]
    except Exception:
        return df


def _next_tuesday(from_date: date) -> date:
    """Return the nearest upcoming Tuesday (or today if today is Tuesday)."""
    days_ahead = (1 - from_date.weekday()) % 7  # Tuesday = weekday 1
    return from_date + timedelta(days=days_ahead)


def collect():
    _log("=== VRL_COLLECTOR v2 start ===")

    # ── Authenticate ─────────────────────────────────────────────
    try:
        kite = CFG.get_kite()
        D.init(kite)
        _log("Auth OK")
    except Exception as e:
        _log("AUTH FAILED: " + str(e))
        sys.exit(1)

    # ── Spot — determine trading date ────────────────────────────
    try:
        spot_df_raw      = D.get_historical_data(D.NIFTY_SPOT_TOKEN, "minute", 400)
        trading_date     = _last_trading_date(spot_df_raw)
        spot_df_session  = _get_session_df(spot_df_raw, trading_date)
        spot             = float(spot_df_session["close"].iloc[-1]) if not spot_df_session.empty else 0
        atm              = int(round(spot / STRIKE_STEP) * STRIKE_STEP)
        _log(f"Trading date: {trading_date}  Spot={spot:.1f}  ATM={atm}")
    except Exception as e:
        _log("Spot fetch error: " + str(e))
        trading_date = date.today()
        spot, atm = 0, 23500

    today_str = trading_date.isoformat()

    # ── Nearest Tuesday expiry ───────────────────────────────────
    try:
        expiry = D.get_nearest_expiry(kite)
        # Confirm it's a Tuesday; if not, find next Tuesday
        if expiry.weekday() != 1:
            expiry = _next_tuesday(expiry)
            _log(f"Expiry adjusted to nearest Tuesday: {expiry}")
        else:
            _log(f"Expiry: {expiry} (Tuesday ✓)")
    except Exception as e:
        _log("Expiry error: " + str(e))
        sys.exit(1)

    expiry_str = str(expiry).replace("-", "")

    # ── Directory structure ───────────────────────────────────────
    # Per-expiry week: expiry_YYYYMMDD/3min/ and expiry_YYYYMMDD/1min/
    collector_dir  = os.path.join(D.LAB_DIR, "collector")
    expiry_dir     = os.path.join(collector_dir, "expiry_" + expiry_str)
    dir_3min       = os.path.join(expiry_dir, "3min")
    dir_1min       = os.path.join(expiry_dir, "1min")
    spot_dir       = os.path.join(collector_dir, "spot")
    meta_dir       = os.path.join(collector_dir, "meta")
    for d in (dir_3min, dir_1min, spot_dir, meta_dir):
        os.makedirs(d, exist_ok=True)

    # ── Strike ranges ─────────────────────────────────────────────
    n_steps_full   = STRIKE_RANGE // STRIKE_STEP
    strikes_full   = [atm + i * STRIKE_STEP for i in range(-n_steps_full, n_steps_full + 1)]
    strikes_shadow = [atm + i * STRIKE_STEP for i in range(-SHADOW_STEPS, SHADOW_STEPS + 1)]
    _log(f"3-min strikes: {strikes_full[0]}→{strikes_full[-1]} ({len(strikes_full)} strikes)")
    _log(f"1-min strikes: {strikes_shadow[0]}→{strikes_shadow[-1]} ({len(strikes_shadow)} strikes, Shadow-DTF)")

    # ── Helper: fetch + filter + indicator one series ─────────────
    def _fetch(token, interval, candles=120):
        df = D.get_historical_data(token, interval, candles)
        df = _get_session_df(df, trading_date)
        if not df.empty:
            df = D.add_indicators(df)
        return df

    # ── 3-min: ATM±300 (full strike range) ───────────────────────
    _log("Fetching 3-min data...")
    rows_3m = []
    failed_3m = []
    for strike in strikes_full:
        try:
            tokens = D.get_option_tokens(kite, strike, expiry)
        except Exception as e:
            failed_3m.append(strike)
            continue
        for opt_type in ("CE", "PE"):
            if opt_type not in tokens:
                failed_3m.append((strike, opt_type))
                continue
            tok = tokens[opt_type]["token"]
            sym = tokens[opt_type]["symbol"]
            try:
                df = _fetch(tok, "3minute", 120)
                if df.empty:
                    continue
                df["strike"]   = strike
                df["opt_type"] = opt_type
                df["symbol"]   = sym
                df["token"]    = tok
                rows_3m.append(df)
                time.sleep(0.05)
            except Exception as e:
                failed_3m.append((strike, opt_type))

    if rows_3m:
        df_3m = pd.concat(rows_3m).sort_index()
        path_3m = os.path.join(dir_3min, today_str + ".parquet")
        df_3m.to_parquet(path_3m)
        _log(f"3-min saved: {len(df_3m)} rows, {len(rows_3m)} series → {path_3m}")
    else:
        _log("WARNING: no 3-min data collected")
        df_3m = pd.DataFrame()

    # ── 1-min: ATM±5 only (Shadow-DTF) ───────────────────────────
    _log("Fetching 1-min data (Shadow-DTF strikes)...")
    rows_1m = []
    failed_1m = []
    for strike in strikes_shadow:
        try:
            tokens = D.get_option_tokens(kite, strike, expiry)
        except Exception as e:
            failed_1m.append(strike)
            continue
        for opt_type in ("CE", "PE"):
            if opt_type not in tokens:
                failed_1m.append((strike, opt_type))
                continue
            tok = tokens[opt_type]["token"]
            sym = tokens[opt_type]["symbol"]
            try:
                df = _fetch(tok, "minute", 400)
                if df.empty:
                    continue
                df["strike"]   = strike
                df["opt_type"] = opt_type
                df["symbol"]   = sym
                df["token"]    = tok
                rows_1m.append(df)
                time.sleep(0.05)
            except Exception as e:
                failed_1m.append((strike, opt_type))

    if rows_1m:
        df_1m = pd.concat(rows_1m).sort_index()
        path_1m = os.path.join(dir_1min, today_str + ".parquet")
        df_1m.to_parquet(path_1m)
        _log(f"1-min saved: {len(df_1m)} rows, {len(rows_1m)} series → {path_1m}")
    else:
        _log("WARNING: no 1-min data collected")

    # ── Spot 1-min → master spot folder ──────────────────────────
    try:
        if not spot_df_session.empty:
            spot_path = os.path.join(spot_dir, today_str + ".parquet")
            spot_df_session.to_parquet(spot_path)
            _log(f"spot saved: {len(spot_df_session)} rows → {spot_path}")
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
        "date"            : today_str,
        "collected_at"    : datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "expiry"          : str(expiry),
        "expiry_weekday"  : expiry.strftime("%A"),
        "atm"             : atm,
        "spot"            : round(spot, 2),
        "vix"             : round(vix, 2),
        "strikes_3min"    : strikes_full,
        "strikes_1min"    : strikes_shadow,
        "series_3min_ok"  : len(rows_3m),
        "series_1min_ok"  : len(rows_1m),
        "failed_3min"     : len(failed_3m),
        "failed_1min"     : len(failed_1m),
    }
    meta_path = os.path.join(meta_dir, today_str + ".json")
    with open(meta_path, "w") as f:
        json.dump(meta, f, indent=2)
    _log("meta written → " + meta_path)

    if failed_3m:
        _log(f"3-min failed ({len(failed_3m)}): {failed_3m[:5]}{'...' if len(failed_3m)>5 else ''}")
    if failed_1m:
        _log(f"1-min failed ({len(failed_1m)}): {failed_1m}")

    _log("=== VRL_COLLECTOR done ===")
    return meta


if __name__ == "__main__":
    collect()
