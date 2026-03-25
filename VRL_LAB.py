# ═══════════════════════════════════════════════════════════════
#  VRL_LAB.py — VISHAL RAJPUT TRADE v12.13
#  Independent lab data collector. Separate process.
#  Collects 1-min + 3-min option candles. EOD forward fill.
#  Zero connection to trade loop. Cannot affect money.
#  Merged from: VRL_LAB_MAIN + VRL_LAB_OPTIONS
# ═══════════════════════════════════════════════════════════════

import csv
import os
import threading
import time
import logging
from datetime import date, datetime, timedelta

import pandas as pd

import VRL_DATA as D

logger = logging.getLogger("vrl_lab")

# ─── SCHEMAS ──────────────────────────────────────────────────

FIELDNAMES_3M = [
    "timestamp", "strike", "type",
    "open", "high", "low", "close", "volume",
    "spot_ref", "atm_distance", "dte",
    "session_block", "iv_vs_open",
    "body_pct", "adx", "rsi", "ema9", "ema9_gap", "volume_ratio",
    "iv_pct", "delta", "gamma", "theta", "vega",
    "fwd_3c", "fwd_6c", "fwd_9c", "fwd_outcome",
]

FIELDNAMES_1M = [
    "timestamp", "strike", "type",
    "open", "high", "low", "close", "volume",
    "spot_ref", "atm_distance", "dte",
    "session_block",
    "body_pct", "rsi", "ema9", "ema9_gap", "volume_ratio",
    "iv_pct", "delta",
    "fwd_1c", "fwd_3c", "fwd_5c", "fwd_outcome",
]

# Signal scan log — every minute, both CE + PE, fired or not
FIELDNAMES_SCAN = [
    "timestamp", "session", "dte", "atm_strike", "spot",
    "direction", "entry_price",
    # 1-min
    "rsi_1m", "body_pct_1m", "vol_ratio_1m", "rsi_rising_1m",
    # 3-min
    "rsi_3m", "body_pct_3m", "ema_spread_3m", "conditions_3m", "mode_3m",
    # result
    "score", "fired", "reject_reason",
    # Greeks
    "iv_pct", "delta",
    # VIX
    "vix",
    # v12.11: Spot columns
    "spot_rsi_3m", "spot_ema_spread_3m", "spot_regime", "spot_gap",
]

# ─── SESSION STATE ────────────────────────────────────────────

_current_atm_strike = None
_current_atm_tokens = None
_current_expiry     = None
_session_open_iv    = {}

_lab_running  = False
_kite_ref     = None
_last_3min    = None
_last_1min    = None
_fwd_done     = False


# ─── PATHS ────────────────────────────────────────────────────

def _csv_path_3m(d: date) -> str:
    return os.path.join(D.OPTIONS_3MIN_DIR,
                        "nifty_option_3min_" + d.strftime("%Y%m%d") + ".csv")


def _csv_path_1m(d: date) -> str:
    return os.path.join(D.OPTIONS_1MIN_DIR,
                        "nifty_option_1min_" + d.strftime("%Y%m%d") + ".csv")


def _csv_path_scan(d: date) -> str:
    return os.path.join(D.OPTIONS_1MIN_DIR,
                        "nifty_signal_scan_" + d.strftime("%Y%m%d") + ".csv")


def _csv_path_spot() -> str:
    return os.path.join(D.SPOT_DIR, "nifty_spot_1min_" + today.strftime("%Y%m%d") + ".csv")


# ─── SPOT 1-MIN COLLECTOR ─────────────────────────────────────

FIELDNAMES_SPOT = ["timestamp", "open", "high", "low", "close", "volume"]

def collect_spot_1min(kite):
    """
    Append last closed 1-min SPOT candle to rolling spot CSV.
    Required by backfill — _read_spot_open() depends on this file.
    Call every minute at HH:MM:30.
    """
    if not D.is_market_open():
        return
    try:
        now     = datetime.now()
        from_dt = now - timedelta(minutes=5)
        candles = kite.historical_data(
            instrument_token=D.NIFTY_SPOT_TOKEN,
            from_date=from_dt, to_date=now,
            interval="minute", continuous=False, oi=False,
        )
        if not candles or len(candles) < 2:
            return
        last   = candles[-2]
        ts_str = (last["date"].strftime("%Y-%m-%d %H:%M:%S")
                  if hasattr(last["date"], "strftime") else str(last["date"]))
        path   = _csv_path_spot()
        is_new = not os.path.isfile(path)
        # Deduplicate
        if not is_new:
            try:
                with open(path) as f:
                    last_written = None
                    for row in csv.DictReader(f):
                        last_written = row.get("timestamp","")
                if last_written == ts_str:
                    return
            except Exception:
                pass
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", newline="") as f:
            w = csv.DictWriter(f, fieldnames=FIELDNAMES_SPOT, extrasaction="ignore")
            if is_new:
                w.writeheader()
            w.writerow({
                "timestamp": ts_str,
                "open" : round(last["open"],  2),
                "high" : round(last["high"],  2),
                "low"  : round(last["low"],   2),
                "close": round(last["close"], 2),
                "volume": int(last["volume"]),
            })
            f.flush()
    except Exception as e:
        logger.debug("[LAB] Spot 1m error: " + str(e))


# ─── SIGNAL SCAN LOGGER ───────────────────────────────────────

def _log_signal_scan(kite, spot_ltp: float, now: datetime):
    """
    v12.11: Every 1-min candle: run check_entry on CE + PE and log ALL indicators.
    Fixed: correct field names, logs even when gate blocks, adds spot data.
    """
    if not _current_atm_tokens or not _current_expiry:
        return
    if not D.is_market_open():
        return

    try:
        from VRL_ENGINE import check_entry as _check_entry
    except Exception:
        return

    today   = date.today()
    dte     = D.calculate_dte(_current_expiry)
    profile = D.get_dte_profile(dte)
    session = D.get_session_block(now.hour, now.minute)
    vix     = D.get_vix()
    ts_str  = now.strftime("%Y-%m-%d %H:%M:%S")
    rows    = []

    # v12.11: Spot data — fetched once per scan, shared by CE+PE rows
    spot_3m   = D.get_spot_indicators("3minute")
    spot_gap  = D.get_spot_gap()

    for opt_type, info in _current_atm_tokens.items():
        token = info["token"]
        try:
            result = _check_entry(
                token       = token,
                option_type = opt_type,
                profile     = profile,
                spot_ltp    = spot_ltp,
                strike      = _current_atm_strike,
                expiry_date = _current_expiry,
                session     = session,
            )

            d1 = result.get("details_1m", {})
            d3 = result.get("details_3m", {})
            g  = result.get("greeks", {})

            # Determine reject reason
            if result.get("fired"):
                reject = ""
            elif d3.get("conditions_met", 0) < 3 and d3.get("conditions_met", 0) > 0:
                reject = "3M_GATE_" + str(d3.get("conditions_met", 0)) + "/4"
            elif d1.get("rsi_reject"):
                reject = "RSI_ZONE"
            elif not d1.get("body_ok") and d1.get("body_pct", 0) > 0:
                reject = "BODY"
            elif not d1.get("vol_ok") and d1.get("vol_ratio", 0) > 0:
                reject = "VOLUME"
            elif result.get("score", 0) > 0:
                reject = "SCORE_" + str(result.get("score", 0))
            else:
                reject = "3M_BLOCK"

            rows.append({
                "timestamp"      : ts_str,
                "session"        : session,
                "dte"            : dte,
                "atm_strike"     : _current_atm_strike,
                "spot"           : round(spot_ltp, 2),
                "direction"      : opt_type,
                "entry_price"    : result.get("entry_price", 0),
                # 1-min — use correct key names from details dict
                "rsi_1m"         : d1.get("rsi_val", 0),
                "body_pct_1m"    : d1.get("body_pct", 0),
                "vol_ratio_1m"   : d1.get("vol_ratio", 0),
                "rsi_rising_1m"  : int(d1.get("rsi_rising", False)),
                # 3-min — fixed: rsi_val_3m not rsi_val, ema_spread_3m not ema_spread
                "rsi_3m"         : d3.get("rsi_val_3m", 0),
                "body_pct_3m"    : d3.get("body_pct_3m", 0),
                "ema_spread_3m"  : d3.get("ema_spread_3m", 0),
                "conditions_3m"  : d3.get("conditions_met", 0),
                "mode_3m"        : d3.get("mode", ""),
                # result
                "score"          : result.get("score", 0),
                "fired"          : int(result.get("fired", False)),
                "reject_reason"  : reject,
                # Greeks
                "iv_pct"         : g.get("iv_pct", 0),
                "delta"          : g.get("delta", 0),
                # VIX
                "vix"            : round(vix, 2),
                # v12.11: Spot
                "spot_rsi_3m"       : spot_3m.get("rsi", 0),
                "spot_ema_spread_3m": spot_3m.get("spread", 0),
                "spot_regime"       : spot_3m.get("regime", ""),
                "spot_gap"          : round(spot_gap, 1),
            })

        except Exception as e:
            logger.debug("[LAB] scan log error " + opt_type + ": " + str(e))
            continue

    if rows:
        path   = _csv_path_scan(today)
        is_new = not os.path.isfile(path)
        try:
            with open(path, "a", newline="") as f:
                w = csv.DictWriter(f, fieldnames=FIELDNAMES_SCAN, extrasaction="ignore")
                if is_new:
                    w.writeheader()
                w.writerows(rows)
                f.flush()
        except Exception as e:
            logger.warning("[LAB] scan write error: " + str(e))


# ─── IO HELPERS ───────────────────────────────────────────────

def _load_timestamps(path: str) -> set:
    if not os.path.isfile(path):
        return set()
    existing = set()
    try:
        with open(path, "r") as f:
            for row in csv.DictReader(f):
                existing.add((row["timestamp"], row["strike"], row["type"]))
    except Exception as e:
        logger.warning("[LAB] Load ts error: " + str(e))
    return existing


def _append_rows(path: str, fieldnames: list, rows: list) -> int:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    is_new  = not os.path.isfile(path)
    written = 0
    try:
        with open(path, "a", newline="") as f:
            w = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
            if is_new:
                w.writeheader()
            for row in rows:
                w.writerow(row)
                written += 1
            f.flush()
    except Exception as e:
        logger.error("[LAB] Write error: " + str(e))
    return written


# ─── INDICATORS ───────────────────────────────────────────────

def _compute_indicators(df: pd.DataFrame, idx: int) -> dict:
    result = {"body_pct": 0, "adx": 0, "rsi": 50,
              "ema9": 0, "ema9_gap": 0, "volume_ratio": 1.0}
    try:
        row = df.iloc[idx]
        o, h, l, c = row["open"], row["high"], row["low"], row["close"]
        rng  = h - l
        body = abs(c - o)
        result["body_pct"]  = round((body / rng * 100) if rng > 0 else 0, 1)
        result["adx"]       = round(row.get("ADX", 0), 1)
        result["rsi"]       = round(row.get("RSI", 50), 1)
        result["ema9"]      = round(row.get("EMA_9", c), 2)
        result["ema9_gap"]  = round(abs(c - row.get("EMA_9", c)), 2)

        n     = len(df)
        pos   = idx if idx >= 0 else n + idx
        start = max(0, pos - 5)
        vols  = [df.iloc[i]["volume"] for i in range(start, pos) if df.iloc[i]["volume"] > 0]
        avg_v = sum(vols) / len(vols) if vols else 1
        result["volume_ratio"] = round(row["volume"] / avg_v if avg_v > 0 else 1, 2)
    except Exception as e:
        logger.warning("[LAB] Indicator error: " + str(e))
    return result


# ─── FETCH ────────────────────────────────────────────────────

def _fetch_candles_with_warmup(kite, token: int, from_dt: datetime,
                               to_dt: datetime, interval: str,
                               warmup_candles: int = 60) -> list:
    """
    Fetch candles with warmup history prepended.
    Warmup = yesterday's last N candles, gives RSI/EMA time to converge.
    Returns only today's candles but indicators are warmed up.
    """
    # Extend from_dt backwards to get warmup history
    minutes_per_candle = {"minute": 1, "3minute": 3}.get(interval, 1)
    extra_minutes = warmup_candles * minutes_per_candle * 2  # ×2 buffer for weekends/gaps
    warmup_from = from_dt - timedelta(minutes=extra_minutes + 60)

    try:
        all_candles = kite.historical_data(
            instrument_token = int(token),
            from_date        = warmup_from,
            to_date          = to_dt,
            interval         = interval,
            continuous       = False,
            oi               = False,
        )
        return all_candles if all_candles else []
    except Exception as e:
        logger.warning("[LAB] Warmup fetch failed, using regular fetch: " + str(e))
        return _fetch_candles(kite, token, from_dt, to_dt, interval)


def _fetch_candles(kite, token: int, from_dt: datetime,
                   to_dt: datetime, interval: str = "3minute") -> list:
    try:
        return kite.historical_data(
            instrument_token = int(token),
            from_date        = from_dt,
            to_date          = to_dt,
            interval         = interval,
            continuous       = False,
            oi               = False,
        )
    except Exception as e:
        logger.error("[LAB] Fetch error token=" + str(token) + " " + str(e))
        return []


# ─── RESET ────────────────────────────────────────────────────

def reset_session():
    global _current_atm_strike, _current_atm_tokens, _current_expiry, _session_open_iv
    _current_atm_strike = None
    _current_atm_tokens = None
    _current_expiry     = None
    _session_open_iv    = {}
    logger.info("[LAB] Session reset")


# ─── LIVE COLLECTION — 3-MIN ──────────────────────────────────

def collect_option_3min(kite, spot_ltp: float):
    """
    Collect last CLOSED 3-min option candle for ATM CE + PE.
    Uses candles[-2] (last closed), not candles[-1] (still forming).
    Call at HH:MM:30 — 30s after each 3-min boundary.
    """
    global _current_atm_strike, _current_atm_tokens, _current_expiry, _session_open_iv

    now = datetime.now()
    cur_mins   = now.hour * 60 + now.minute
    start_mins = D.MARKET_OPEN_HOUR * 60 + D.MARKET_OPEN_MIN
    end_mins   = D.MARKET_CLOSE_HOUR * 60 + D.MARKET_CLOSE_MIN
    if not (start_mins <= cur_mins <= end_mins):
        return

    today = date.today()

    if _current_expiry is None:
        _current_expiry = D.get_nearest_expiry(kite)
        if not _current_expiry:
            logger.error("[LAB] Cannot resolve expiry")
            return

    dte        = D.calculate_dte(_current_expiry)
    step       = D.get_active_strike_step(dte)
    new_strike = D.resolve_atm_strike(spot_ltp, step)

    if (_current_atm_strike is None
            or abs(new_strike - _current_atm_strike) >= step):
        if _current_atm_strike and new_strike != _current_atm_strike:
            logger.info("[LAB] ATM shift " + str(_current_atm_strike)
                        + "→" + str(new_strike))
        _current_atm_strike = new_strike
        _current_atm_tokens = D.get_option_tokens(kite, new_strike, _current_expiry)
        if not _current_atm_tokens:
            logger.error("[LAB] Token resolve failed strike=" + str(new_strike))
            return

    from_dt  = min(now - timedelta(minutes=180), now - timedelta(days=3))
    to_dt    = now
    today_ts = _load_timestamps(_csv_path_3m(today))
    session  = D.get_session_block(now.hour, now.minute)
    all_rows = []

    for opt_type, info in _current_atm_tokens.items():
        token   = info["token"]
        candles = _fetch_candles_with_warmup(kite, token, from_dt, to_dt, "3minute", 30)
        if not candles or len(candles) < 2:
            continue

        last = candles[-2]   # last CLOSED candle

        try:
            df = pd.DataFrame(candles)
            df.rename(columns={"date": "timestamp"}, inplace=True)
            df.set_index("timestamp", inplace=True)
            df = D.add_indicators(df)
            indic = _compute_indicators(df, -2)
        except Exception:
            indic = {}

        greeks = D.get_full_greeks(
            last["close"], spot_ltp, _current_atm_strike,
            _current_expiry, opt_type
        )

        if opt_type not in _session_open_iv and greeks.get("iv_pct", 0) > 0:
            _session_open_iv[opt_type] = greeks["iv_pct"]
        open_iv    = _session_open_iv.get(opt_type, greeks.get("iv_pct", 0))
        iv_vs_open = round(greeks.get("iv_pct", 0) - open_iv, 2)

        ts_str = (last["date"].strftime("%Y-%m-%d %H:%M:%S")
                  if hasattr(last["date"], "strftime") else str(last["date"]))

        key = (ts_str, str(_current_atm_strike), opt_type)
        if key in today_ts:
            continue

        all_rows.append({
            "timestamp"    : ts_str,
            "strike"       : _current_atm_strike,
            "type"         : opt_type,
            "open"         : round(last["open"],  2),
            "high"         : round(last["high"],  2),
            "low"          : round(last["low"],   2),
            "close"        : round(last["close"], 2),
            "volume"       : int(last["volume"]),
            "spot_ref"     : round(spot_ltp, 2),
            "atm_distance" : round(abs(spot_ltp - _current_atm_strike), 0),
            "dte"          : dte,
            "session_block": session,
            "iv_vs_open"   : iv_vs_open,
            "body_pct"     : indic.get("body_pct", 0),
            "adx"          : indic.get("adx", 0),
            "rsi"          : indic.get("rsi", 50),
            "ema9"         : indic.get("ema9", 0),
            "ema9_gap"     : indic.get("ema9_gap", 0),
            "volume_ratio" : indic.get("volume_ratio", 1),
            "iv_pct"       : greeks.get("iv_pct", 0),
            "delta"        : greeks.get("delta",  0),
            "gamma"        : greeks.get("gamma",  0),
            "theta"        : greeks.get("theta",  0),
            "vega"         : greeks.get("vega",   0),
            "fwd_3c": "", "fwd_6c": "", "fwd_9c": "", "fwd_outcome": "",
        })
        today_ts.add(key)
        time.sleep(0.35)

    if all_rows:
        all_rows.sort(key=lambda r: (r["timestamp"], r["type"]))
        n = _append_rows(_csv_path_3m(today), FIELDNAMES_3M, all_rows)
        logger.debug("[LAB] 3m wrote=" + str(n) + " @" + now.strftime("%H:%M"))


# ─── LIVE COLLECTION — 1-MIN ──────────────────────────────────

def collect_option_1min(kite, spot_ltp: float):
    """
    Collect last CLOSED 1-min option candle for ATM CE + PE.
    Call every minute at HH:MM:30.
    Depends on 3-min collector having initialised tokens first.
    """
    global _current_atm_strike, _current_atm_tokens, _current_expiry

    now = datetime.now()
    cur_mins   = now.hour * 60 + now.minute
    start_mins = D.MARKET_OPEN_HOUR * 60 + D.MARKET_OPEN_MIN
    end_mins   = D.MARKET_CLOSE_HOUR * 60 + D.MARKET_CLOSE_MIN
    if not (start_mins <= cur_mins <= end_mins):
        return

    if not _current_atm_tokens or not _current_expiry:
        return   # 3-min must init first

    today    = date.today()
    dte      = D.calculate_dte(_current_expiry)
    session  = D.get_session_block(now.hour, now.minute)
    from_dt  = min(now - timedelta(minutes=50), now - timedelta(days=3))
    to_dt    = now
    today_ts = _load_timestamps(_csv_path_1m(today))
    all_rows = []

    for opt_type, info in _current_atm_tokens.items():
        token   = info["token"]
        candles = _fetch_candles_with_warmup(kite, token, from_dt, to_dt, "minute", 30)
        if not candles or len(candles) < 2:
            continue

        last = candles[-2]   # last CLOSED 1-min candle

        try:
            df = pd.DataFrame(candles)
            df.rename(columns={"date": "timestamp"}, inplace=True)
            df.set_index("timestamp", inplace=True)
            df = D.add_indicators(df)
            # Use iloc[-2] which is warmed up now (has warmup history before it)
            indic = _compute_indicators(df, -2)
        except Exception:
            indic = {}

        greeks = D.get_full_greeks(
            last["close"], spot_ltp, _current_atm_strike,
            _current_expiry, opt_type
        )

        ts_str = (last["date"].strftime("%Y-%m-%d %H:%M:%S")
                  if hasattr(last["date"], "strftime") else str(last["date"]))

        key = (ts_str, str(_current_atm_strike), opt_type)
        if key in today_ts:
            continue

        all_rows.append({
            "timestamp"    : ts_str,
            "strike"       : _current_atm_strike,
            "type"         : opt_type,
            "open"         : round(last["open"],  2),
            "high"         : round(last["high"],  2),
            "low"          : round(last["low"],   2),
            "close"        : round(last["close"], 2),
            "volume"       : int(last["volume"]),
            "spot_ref"     : round(spot_ltp, 2),
            "atm_distance" : round(abs(spot_ltp - _current_atm_strike), 0),
            "dte"          : dte,
            "session_block": session,
            "body_pct"     : indic.get("body_pct", 0),
            "rsi"          : indic.get("rsi", 50),
            "ema9"         : indic.get("ema9", 0),
            "ema9_gap"     : indic.get("ema9_gap", 0),
            "volume_ratio" : indic.get("volume_ratio", 1),
            "iv_pct"       : greeks.get("iv_pct", 0),
            "delta"        : greeks.get("delta",  0),
            "fwd_1c": "", "fwd_3c": "", "fwd_5c": "", "fwd_outcome": "",
        })
        today_ts.add(key)
        time.sleep(0.25)

    if all_rows:
        all_rows.sort(key=lambda r: (r["timestamp"], r["type"]))
        n = _append_rows(_csv_path_1m(today), FIELDNAMES_1M, all_rows)
        logger.debug("[LAB] 1m wrote=" + str(n) + " @" + now.strftime("%H:%M"))


# ─── BACKFILL — 3-MIN ─────────────────────────────────────────

def backfill_history(kite):
    """Backfill up to 60 days of 3-min option data."""
    logger.info("[LAB] Backfill starting")
    today   = date.today()
    filled  = skipped = failed = 0

    for delta_days in range(1, 61):
        target = today - timedelta(days=delta_days)
        if target.weekday() >= 5:
            continue
        if os.path.isfile(_csv_path_3m(target)):
            skipped += 1
            continue

        expiry = D.get_nearest_expiry(kite, reference_date=target)
        if not expiry:
            failed += 1
            continue

        spot_open = _read_spot_open(target)
        if spot_open is None:
            logger.warning("[LAB] No spot open for " + str(target) + " — skip")
            failed += 1
            continue

        dte     = (expiry - target).days
        step    = D.get_active_strike_step(dte)
        strike  = D.resolve_atm_strike(spot_open, step)
        tokens  = D.get_option_tokens(kite, strike, expiry)
        if not tokens:
            failed += 1
            continue

        from_dt = datetime.combine(target, datetime.min.time()).replace(
            hour=D.MARKET_OPEN_HOUR, minute=D.MARKET_OPEN_MIN)
        to_dt   = datetime.combine(target, datetime.min.time()).replace(
            hour=D.MARKET_CLOSE_HOUR, minute=D.MARKET_CLOSE_MIN)

        spot_map = _read_spot_1min_map(target)
        all_rows = []

        for opt_type, info in tokens.items():
            candles = _fetch_candles(kite, info["token"], from_dt, to_dt, "3minute")
            if not candles:
                continue

            try:
                df = pd.DataFrame(candles)
                df.rename(columns={"date": "timestamp"}, inplace=True)
                df.set_index("timestamp", inplace=True)
                df = D.add_indicators(df)
            except Exception:
                df = None

            first_iv = 0.0

            for i, c in enumerate(candles):
                indic  = _compute_indicators(df, i) if df is not None else {}
                ts_str = (c["date"].strftime("%Y-%m-%d %H:%M:%S")
                          if hasattr(c["date"], "strftime") else str(c["date"]))

                spot_at_candle = spot_map.get(ts_str[:16], spot_open)

                greeks = D.get_full_greeks(
                    c["close"], spot_at_candle, strike, expiry, opt_type
                )

                if i == 0:
                    first_iv = greeks.get("iv_pct", 0)
                iv_vs_open = round(greeks.get("iv_pct", 0) - first_iv, 2)

                all_rows.append({
                    "timestamp"    : ts_str,
                    "strike"       : strike,
                    "type"         : opt_type,
                    "open"         : round(c["open"],  2),
                    "high"         : round(c["high"],  2),
                    "low"          : round(c["low"],   2),
                    "close"        : round(c["close"], 2),
                    "volume"       : int(c["volume"]),
                    "spot_ref"     : round(spot_at_candle, 2),
                    "atm_distance" : round(abs(spot_at_candle - strike), 0),
                    "dte"          : dte,
                    "session_block": D.get_session_block(
                        c["date"].hour   if hasattr(c["date"], "hour")   else 9,
                        c["date"].minute if hasattr(c["date"], "minute") else 15,
                    ),
                    "iv_vs_open"   : iv_vs_open,
                    "body_pct"     : indic.get("body_pct", 0),
                    "adx"          : indic.get("adx",      0),
                    "rsi"          : indic.get("rsi",      50),
                    "ema9"         : indic.get("ema9",     0),
                    "ema9_gap"     : indic.get("ema9_gap", 0),
                    "volume_ratio" : indic.get("volume_ratio", 1),
                    "iv_pct"       : greeks.get("iv_pct", 0),
                    "delta"        : greeks.get("delta",  0),
                    "gamma"        : greeks.get("gamma",  0),
                    "theta"        : greeks.get("theta",  0),
                    "vega"         : greeks.get("vega",   0),
                    "fwd_3c": "", "fwd_6c": "", "fwd_9c": "", "fwd_outcome": "",
                })

            time.sleep(0.4)

        if all_rows:
            all_rows.sort(key=lambda r: (r["timestamp"], r["type"]))
            n = _append_rows(_csv_path_3m(target), FIELDNAMES_3M, all_rows)
            logger.info("[LAB] Backfill " + str(target) + " wrote=" + str(n))
            filled += 1
        else:
            failed += 1

        time.sleep(0.5)

    logger.info("[LAB] Backfill done: filled=" + str(filled)
                + " skipped=" + str(skipped) + " failed=" + str(failed))
    return filled, skipped, failed


def _read_spot_open(target_date: date):
    paths = [
        os.path.join(D.SPOT_DIR, "nifty_spot_1min.csv"),
        os.path.expanduser("~/nifty_spot_1min.csv"),
    ]
    target_str = target_date.strftime("%Y-%m-%d")
    for path in paths:
        if not os.path.isfile(path):
            continue
        try:
            with open(path) as f:
                for row in csv.DictReader(f):
                    ts = row.get("timestamp", row.get("date", ""))
                    if ts.startswith(target_str + " 09:15"):
                        return float(row.get("close", row.get("Close", 0)))
        except Exception as e:
            logger.warning("[LAB] Spot open read error: " + str(e))
    return None


def _read_spot_1min_map(target_date: date) -> dict:
    result     = {}
    paths      = [
        os.path.join(D.SPOT_DIR, "nifty_spot_1min.csv"),
        os.path.expanduser("~/nifty_spot_1min.csv"),
    ]
    target_str = target_date.strftime("%Y-%m-%d")
    for path in paths:
        if not os.path.isfile(path):
            continue
        try:
            with open(path) as f:
                for row in csv.DictReader(f):
                    ts = row.get("timestamp", row.get("date", ""))
                    if ts.startswith(target_str):
                        key = ts[:16]
                        try:
                            result[key] = float(row.get("close", row.get("Close", 0)))
                        except Exception:
                            pass
        except Exception as e:
            logger.warning("[LAB] Spot map error: " + str(e))
    return result


# ─── EOD FORWARD FILL ─────────────────────────────────────────

def fill_forward_columns(kite, target_date: date = None, timeframe: str = "3min"):
    """Fill fwd columns for 3-min or 1-min CSV at EOD."""
    if target_date is None:
        target_date = date.today()

    if timeframe == "1min":
        path       = _csv_path_1m(target_date)
        fieldnames = FIELDNAMES_1M
        fwd_keys   = ["fwd_1c", "fwd_3c", "fwd_5c"]
        fwd_mins   = [1, 3, 5]
        win_pts    = 10
        loss_pts   = -6
    else:
        path       = _csv_path_3m(target_date)
        fieldnames = FIELDNAMES_3M
        fwd_keys   = ["fwd_3c", "fwd_6c", "fwd_9c"]
        fwd_mins   = [9, 18, 27]
        win_pts    = 15
        loss_pts   = -8

    if not os.path.isfile(path):
        return

    logger.info("[LAB] Forward fill " + timeframe + " for " + str(target_date))

    try:
        with open(path) as f:
            rows = list(csv.DictReader(f))
    except Exception as e:
        logger.error("[LAB] Fwd fill read error: " + str(e))
        return

    tokens_by_type = {}
    if _current_atm_tokens:
        for opt_type, info in _current_atm_tokens.items():
            tokens_by_type[opt_type] = info["token"]

    changed  = 0
    interval = "minute" if timeframe == "1min" else "3minute"

    for row in rows:
        if row.get(fwd_keys[-1]):
            continue
        opt_type = row.get("type")
        token    = tokens_by_type.get(opt_type)
        if not token:
            continue

        try:
            ts     = datetime.fromisoformat(row["timestamp"])
            prices = []

            for mins in fwd_mins:
                fwd_t   = ts + timedelta(minutes=mins)
                candles = _fetch_candles(kite, token,
                                         fwd_t - timedelta(minutes=1),
                                         fwd_t + timedelta(minutes=2),
                                         interval)
                prices.append(round(candles[-1]["close"], 2) if candles else None)
                time.sleep(0.25)

            entry = float(row.get("close", 0))
            if all(p is not None for p in prices):
                for key, price in zip(fwd_keys, prices):
                    row[key] = price
                max_move = max(p - entry for p in prices)
                min_move = min(p - entry for p in prices)
                if max_move >= win_pts:
                    row["fwd_outcome"] = "WIN"
                elif min_move <= loss_pts:
                    row["fwd_outcome"] = "LOSS"
                else:
                    row["fwd_outcome"] = "NEUTRAL"
                changed += 1

        except Exception as e:
            logger.warning("[LAB] Fwd fill row error: " + str(e))

    try:
        with open(path, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
            w.writeheader()
            w.writerows(rows)
            f.flush()
        logger.info("[LAB] Fwd fill done: " + str(changed) + " rows")
    except Exception as e:
        logger.error("[LAB] Fwd fill write error: " + str(e))


# ─── LAB SCHEDULER ────────────────────────────────────────────

def start_lab(kite):
    """
    Entry point. Call after kite auth in VRL_MAIN.py.
    Backfills history then starts live collection loop.
    Runs as daemon thread — dies when main exits.
    """
    global _kite_ref, _lab_running
    _kite_ref    = kite
    _lab_running = True

    def _start():
        logger.info("[LAB] Starting — running backfill first")
        try:
            backfill_history(kite)
        except Exception as e:
            logger.error("[LAB] Backfill error: " + str(e))
        _lab_loop()

    thread = threading.Thread(target=_start, name="LabCollector", daemon=True)
    thread.start()
    logger.info("[LAB] Collection thread started")


def stop_lab():
    global _lab_running
    _lab_running = False


def _lab_loop():
    global _last_3min, _last_1min, _fwd_done

    _last_daily_reset = None   # Fix: prevent triple reset

    while _lab_running:
        try:
            now   = datetime.now()
            today = date.today()

            # ── Daily reset at 9:14 — only once per day ───────────
            reset_key = today.isoformat()
            if (now.hour == 9 and now.minute == 14
                    and now.second < 5
                    and _last_daily_reset != reset_key):
                _last_daily_reset = reset_key
                reset_session()
                _fwd_done  = False
                _last_3min = None
                _last_1min = None
                logger.info("[LAB] Daily reset")

            # ── 1-min collection at HH:MM:30 ──────────────────
            one_min_key = (today, now.hour, now.minute)
            if one_min_key != _last_1min and now.second >= 30:
                _last_1min  = one_min_key
                spot_ltp    = D.get_ltp(D.NIFTY_SPOT_TOKEN)
                if spot_ltp > 0 and D.is_tick_live(D.NIFTY_SPOT_TOKEN):
                    try:
                        collect_option_1min(_kite_ref, spot_ltp)
                    except Exception as e:
                        logger.error("[LAB] 1m error: " + str(e))
                    # Spot 1-min — required for backfill _read_spot_open()
                    try:
                        collect_spot_1min(_kite_ref)
                    except Exception as e:
                        logger.debug("[LAB] spot 1m: " + str(e))
                    # Signal scan — log every minute, fired or not
                    try:
                        _log_signal_scan(_kite_ref, spot_ltp, now)
                    except Exception as e:
                        logger.debug("[LAB] scan log: " + str(e))
                elif spot_ltp <= 0 and D.is_market_open():
                    logger.debug("[LAB] 1m skip — spot LTP not available yet")

            # ── 3-min collection at boundary + 30s ────────────
            candle_min    = (now.minute // 3) * 3
            three_min_key = (today, now.hour, candle_min)
            if three_min_key != _last_3min and now.second >= 30:
                _last_3min = three_min_key
                spot_ltp   = D.get_ltp(D.NIFTY_SPOT_TOKEN)
                if spot_ltp > 0 and D.is_tick_live(D.NIFTY_SPOT_TOKEN):
                    try:
                        collect_option_3min(_kite_ref, spot_ltp)
                    except Exception as e:
                        logger.error("[LAB] 3m error: " + str(e))
                elif spot_ltp <= 0 and D.is_market_open():
                    logger.debug("[LAB] 3m skip — spot LTP not available yet")

            # ── EOD forward fill at 15:35 ─────────────────────
            if (now.hour == 15 and now.minute == 35
                    and not _fwd_done and now.second < 30):
                _fwd_done = True
                logger.info("[LAB] EOD forward fill starting")
                try:
                    fill_forward_columns(_kite_ref, today, "3min")
                    fill_forward_columns(_kite_ref, today, "1min")
                except Exception as e:
                    logger.error("[LAB] Forward fill error: " + str(e))

        except Exception as e:
            logger.error("[LAB] Loop error: " + str(e))

        time.sleep(1)
