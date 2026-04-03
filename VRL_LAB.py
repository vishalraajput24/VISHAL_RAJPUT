# ═══════════════════════════════════════════════════════════════
#  VRL_LAB.py — VISHAL RAJPUT TRADE v13.1
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
import VRL_DB as DB

logger = logging.getLogger("vrl_lab")

# ─── SCHEMAS ──────────────────────────────────────────────────

FIELDNAMES_3M = [
    "timestamp", "strike", "type",
    "open", "high", "low", "close", "volume",
    "spot_ref", "atm_distance", "dte",
    "session_block", "iv_vs_open",
    "body_pct", "adx", "rsi", "ema9", "ema21", "ema_spread", "ema9_gap", "volume_ratio",
    "iv_pct", "delta", "gamma", "theta", "vega",
    "fwd_3c", "fwd_6c", "fwd_9c", "fwd_outcome",
]

FIELDNAMES_1M = [
    "timestamp", "strike", "type",
    "open", "high", "low", "close", "volume",
    "spot_ref", "atm_distance", "dte",
    "session_block",
    "body_pct", "rsi", "ema9", "ema9_gap", "adx",
    "volume_ratio", "iv_pct", "delta",
    "fwd_1c", "fwd_3c", "fwd_5c", "fwd_outcome",
]

# Signal scan log — every minute, both CE + PE, fired or not
FIELDNAMES_SCAN = [
    "timestamp", "session", "dte", "atm_strike", "spot",
    "direction", "entry_price",
    # 1-min
    "rsi_1m", "body_pct_1m", "vol_ratio_1m", "rsi_rising_1m",
    "spread_1m",
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
    # v12.15: Market context
    "bias", "hourly_rsi", "straddle_decay_pct",
    "near_fib_level", "fib_distance",
    # v12.15: Blocked trade analysis (forward fill at EOD)
    "fwd_3c", "fwd_5c", "fwd_10c", "fwd_outcome",
]

# ─── SESSION STATE ────────────────────────────────────────────

_current_atm_strike = None
_current_atm_tokens = None
_current_expiry     = None
_session_open_iv    = {}
_lab_lock           = threading.Lock()   # protects the globals above

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
    from datetime import date as _d
    return os.path.join(D.SPOT_DIR, "nifty_spot_1min_" + _d.today().strftime("%Y%m%d") + ".csv")


# ─── 5-MIN + 15-MIN SCHEMAS ────────────────────────────────

FIELDNAMES_5M = [
    "timestamp", "strike", "type",
    "open", "high", "low", "close", "volume",
    "spot_ref", "dte", "session_block",
    "body_pct", "rsi", "ema9", "ema21", "ema_spread", "adx",
    "volume_ratio", "iv_pct", "delta",
]

FIELDNAMES_15M = [
    "timestamp", "strike", "type",
    "open", "high", "low", "close", "volume",
    "spot_ref", "dte", "session_block",
    "body_pct", "rsi", "ema9", "ema21", "ema_spread",
    "macd_hist", "adx",
    "volume_ratio", "iv_pct", "delta",
]

FIELDNAMES_SPOT_5M = [
    "timestamp", "open", "high", "low", "close", "volume",
    "ema9", "ema21", "ema_spread", "rsi", "adx",
]

FIELDNAMES_SPOT_15M = [
    "timestamp", "open", "high", "low", "close", "volume",
    "ema9", "ema21", "ema_spread", "rsi", "adx",
]


def _csv_path_5m(d):
    return os.path.join(D.OPTIONS_1MIN_DIR,
                        "nifty_option_5min_" + d.strftime("%Y%m%d") + ".csv")

def _csv_path_15m(d):
    return os.path.join(D.OPTIONS_1MIN_DIR,
                        "nifty_option_15min_" + d.strftime("%Y%m%d") + ".csv")

# Hourly + Daily spot schemas
FIELDNAMES_SPOT_60M = [
    "timestamp", "open", "high", "low", "close", "volume",
    "ema9", "ema21", "ema_spread", "rsi", "adx",
]

FIELDNAMES_SPOT_DAILY = [
    "date", "open", "high", "low", "close", "volume",
    "ema21", "rsi", "adx",
]

def _csv_path_spot_60m():
    from datetime import date as _d
    return os.path.join(D.SPOT_DIR, "nifty_spot_60min_" + _d.today().strftime("%Y%m%d") + ".csv")

def _csv_path_spot_daily():
    return os.path.join(D.SPOT_DIR, "nifty_spot_daily.csv")

def _csv_path_spot_5m():
    from datetime import date as _d
    return os.path.join(D.SPOT_DIR, "nifty_spot_5min_" + _d.today().strftime("%Y%m%d") + ".csv")

def _csv_path_spot_15m():
    from datetime import date as _d
    return os.path.join(D.SPOT_DIR, "nifty_spot_15min_" + _d.today().strftime("%Y%m%d") + ".csv")


# ─── SPOT 1-MIN COLLECTOR ─────────────────────────────────────

FIELDNAMES_SPOT = ["timestamp", "open", "high", "low", "close", "volume", "ema9", "ema21", "ema_spread", "rsi", "adx"]

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
        from_dt = now - timedelta(minutes=60)
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
            # Compute indicators on warmup data
            _spot_ema9 = _spot_ema21 = _spot_rsi = _spot_adx = 0
            try:
                _sdf = pd.DataFrame(candles)
                _sdf.rename(columns={"date": "timestamp"}, inplace=True)
                _sdf.set_index("timestamp", inplace=True)
                _sdf = D.add_indicators(_sdf)
                if len(_sdf) >= 2:
                    _slast = _sdf.iloc[-2]
                    _sc = float(_slast["close"])
                    _spot_ema9 = round(float(_slast.get("EMA_9", _sc)), 2)
                    _spot_ema21 = round(float(_slast.get("EMA_21", _sc)), 2)
                    _spot_rsi = round(float(_slast.get("RSI", 50)), 1)
                # ADX
                if len(_sdf) >= 16:
                    import numpy as _np
                    _up = _sdf["high"].diff()
                    _dn = -_sdf["low"].diff()
                    _pdm = _np.where((_up > _dn) & (_up > 0), _up, 0.0)
                    _ndm = _np.where((_dn > _up) & (_dn > 0), _dn, 0.0)
                    _tr = pd.concat([_sdf["high"]-_sdf["low"],
                                     (_sdf["high"]-_sdf["close"].shift(1)).abs(),
                                     (_sdf["low"]-_sdf["close"].shift(1)).abs()], axis=1).max(axis=1)
                    _atr_s = _tr.ewm(alpha=1/14, adjust=False).mean()
                    _pdi = 100 * pd.Series(_pdm, index=_sdf.index).ewm(alpha=1/14, adjust=False).mean() / _atr_s
                    _ndi = 100 * pd.Series(_ndm, index=_sdf.index).ewm(alpha=1/14, adjust=False).mean() / _atr_s
                    _adx_s = ((_pdi-_ndi).abs() / (_pdi+_ndi+1e-9) * 100).ewm(alpha=1/14, adjust=False).mean()
                    _spot_adx = round(float(_adx_s.iloc[-2]), 1)
            except Exception:
                pass
            _spot_row = {
                "timestamp": ts_str,
                "open" : round(last["open"],  2),
                "high" : round(last["high"],  2),
                "low"  : round(last["low"],   2),
                "close": round(last["close"], 2),
                "volume": int(last["volume"]),
                "ema9": _spot_ema9,
                "ema21": _spot_ema21,
                "ema_spread": round(_spot_ema9 - _spot_ema21, 2) if _spot_ema9 and _spot_ema21 else 0,
                "rsi": _spot_rsi,
                "adx": _spot_adx,
            }
            w.writerow(_spot_row)
            f.flush()
        # Dual write: SQLite
        try:
            DB.insert_spot_1min(_spot_row)
        except Exception:
            pass
    except Exception as e:
        logger.debug("[LAB] Spot 1m error: " + str(e))


def _log_signal_scan(kite, spot_ltp: float, now: datetime):
    """
    v12.11: Every 1-min candle: run check_entry on CE + PE and log ALL indicators.
    Logs to nifty_signal_scan_YYYYMMDD.csv with forward fill columns.
    Critical for strategy validation — DO NOT REMOVE.
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

            if result.get("fired"):
                reject = ""
            elif result.get("regime") in ("CHOPPY", "UNKNOWN"):
                reject = "REGIME_" + result.get("regime", "")
            elif d3.get("conditions_met", 0) < 2 and d3.get("conditions_met", 0) > 0:
                reject = "3M_GATE_" + str(d3.get("conditions_met", 0)) + "/4"
            elif d1.get("rsi_reject"):
                reject = d1.get("rsi_reject_reason", "RSI_ZONE")
            elif not d1.get("body_ok") and d1.get("body_pct", 0) > 0:
                reject = "BODY"
            elif not d1.get("vol_ok") and d1.get("vol_ratio", 0) > 0:
                reject = "VOLUME"
            elif result.get("score", 0) > 0:
                reject = "SCORE_" + str(result.get("score", 0))
            else:
                reject = "BLOCKED"

            rows.append({
                "timestamp"      : ts_str,
                "session"        : session,
                "dte"            : dte,
                "atm_strike"     : _current_atm_strike,
                "spot"           : round(spot_ltp, 2),
                "direction"      : opt_type,
                "entry_price"    : result.get("entry_price", 0),
                "rsi_1m"         : d1.get("rsi_val", 0),
                "body_pct_1m"    : d1.get("body_pct", 0),
                "vol_ratio_1m"   : d1.get("vol_ratio", 0),
                "rsi_rising_1m"  : int(d1.get("rsi_rising", False)),
                "rsi_3m"         : d3.get("rsi_val_3m", 0),
                "body_pct_3m"    : d3.get("body_pct_3m", 0),
                "ema_spread_3m"  : d3.get("ema_spread_3m", 0),
                "conditions_3m"  : d3.get("conditions_met", 0),
                "mode_3m"        : d3.get("mode", ""),
                "score"          : result.get("score", 0),
                "fired"          : int(result.get("fired", False)),
                "reject_reason"  : reject,
                "iv_pct"         : g.get("iv_pct", 0),
                "delta"          : g.get("delta", 0),
                "vix"            : round(vix, 2),
                "spot_rsi_3m"       : spot_3m.get("rsi", 0),
                "spot_ema_spread_3m": spot_3m.get("spread", 0),
                "spot_regime"       : spot_3m.get("regime", ""),
                "spot_gap"          : round(spot_gap, 1),
                "spread_1m"         : result.get("spread_1m", 0),
                "bias"              : D.get_daily_bias() if hasattr(D, "get_daily_bias") else "",
                "hourly_rsi"        : D.get_hourly_rsi() if hasattr(D, "get_hourly_rsi") else 0,
                "straddle_decay_pct": 0.0,
                "near_fib_level"    : "",
                "fib_distance"      : 0,
                "fwd_3c": "", "fwd_5c": "", "fwd_10c": "", "fwd_outcome": "",
            })

            try:
                fib = D.get_nearest_fib_level(spot_ltp)
                rows[-1]["near_fib_level"] = fib.get("level", "")
                rows[-1]["fib_distance"] = fib.get("distance", 0)
            except Exception:
                pass

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
        # Dual write: SQLite
        try:
            DB.insert_scan_many(rows)
        except Exception:
            pass


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
    with _lab_lock:
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

    # Lock protects reads/writes to _current_atm_* globals
    today = date.today()

    with _lab_lock:
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
            # Add ema21 + ema_spread
            _row3 = df.iloc[-2]
            indic["ema21"] = round(float(_row3.get("EMA_21", _row3["close"])), 2)
            indic["ema_spread"] = round(float(_row3.get("EMA_9", _row3["close"])) - float(_row3.get("EMA_21", _row3["close"])), 2)
            # Inline ADX calculation (D.add_indicators doesn't compute ADX)
            try:
                import numpy as _np
                _up3 = df["high"].diff()
                _dn3 = -df["low"].diff()
                _pdm3 = _np.where((_up3 > _dn3) & (_up3 > 0), _up3, 0.0)
                _ndm3 = _np.where((_dn3 > _up3) & (_dn3 > 0), _dn3, 0.0)
                _tr3 = pd.concat([df["high"]-df["low"],
                                  (df["high"]-df["close"].shift(1)).abs(),
                                  (df["low"]-df["close"].shift(1)).abs()], axis=1).max(axis=1)
                _atr3 = _tr3.ewm(alpha=1/14, adjust=False).mean()
                _pdi3 = 100 * pd.Series(_pdm3, index=df.index).ewm(alpha=1/14, adjust=False).mean() / _atr3
                _ndi3 = 100 * pd.Series(_ndm3, index=df.index).ewm(alpha=1/14, adjust=False).mean() / _atr3
                _adx3 = ((_pdi3-_ndi3).abs() / (_pdi3+_ndi3+1e-9) * 100).ewm(alpha=1/14, adjust=False).mean()
                indic["adx"] = round(float(_adx3.iloc[-2]), 1)
            except Exception:
                indic["adx"] = 0
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
            "ema21"        : indic.get("ema21", 0),
            "ema_spread"   : indic.get("ema_spread", 0),
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
        try:
            DB.insert_option_3min_many(all_rows)
        except Exception:
            pass
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
            # ADX for 1m
            try:
                import numpy as _np
                _up1 = df["high"].diff()
                _dn1 = -df["low"].diff()
                _pdm1 = _np.where((_up1 > _dn1) & (_up1 > 0), _up1, 0.0)
                _ndm1 = _np.where((_dn1 > _up1) & (_dn1 > 0), _dn1, 0.0)
                _tr1 = pd.concat([df["high"]-df["low"],
                                  (df["high"]-df["close"].shift(1)).abs(),
                                  (df["low"]-df["close"].shift(1)).abs()], axis=1).max(axis=1)
                _atr1 = _tr1.ewm(alpha=1/14, adjust=False).mean()
                _pdi1 = 100 * pd.Series(_pdm1, index=df.index).ewm(alpha=1/14, adjust=False).mean() / _atr1
                _ndi1 = 100 * pd.Series(_ndm1, index=df.index).ewm(alpha=1/14, adjust=False).mean() / _atr1
                _adx1 = ((_pdi1-_ndi1).abs() / (_pdi1+_ndi1+1e-9) * 100).ewm(alpha=1/14, adjust=False).mean()
                indic["adx"] = round(float(_adx1.iloc[-2]), 1)
            except Exception:
                indic["adx"] = 0
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
            "adx"          : indic.get("adx", 0),
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
        try:
            DB.insert_option_1min_many(all_rows)
        except Exception:
            pass
        logger.debug("[LAB] 1m wrote=" + str(n) + " @" + now.strftime("%H:%M"))


# ─── BACKFILL — 3-MIN ─────────────────────────────────────────


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

    with _lab_lock:
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

    # Dual write: update SQLite forward fill columns
    try:
        update_fn = DB.update_option_1min_fwd if timeframe == "1min" else DB.update_option_3min_fwd
        for row in rows:
            if row.get(fwd_keys[-1]):
                ts = row.get("timestamp", "")
                ot = row.get("type", "")
                if timeframe == "1min":
                    update_fn(ts, ot, row.get("fwd_1c"), row.get("fwd_3c"), row.get("fwd_5c"), row.get("fwd_outcome"))
                else:
                    update_fn(ts, ot, row.get("fwd_3c"), row.get("fwd_6c"), row.get("fwd_9c"), row.get("fwd_outcome"))
    except Exception:
        pass


# ─── LAB SCHEDULER ────────────────────────────────────────────


# ─── SCAN FORWARD FILL (v12.15) ──────────────────────────────

def fill_forward_scan(kite, target_date: date = None):
    """
    v12.15: For each scan row, fill what the option price was
    3/5/10 candles later. Answers: "What would have happened
    if we entered here?"
    Only fills rows where fired=0 (blocked entries) — these are
    the what-if analysis rows.
    """
    if target_date is None:
        target_date = date.today()

    path = _csv_path_scan(target_date)
    if not os.path.isfile(path):
        return

    logger.info("[LAB] Scan forward fill for " + str(target_date))

    try:
        with open(path) as f:
            rows = list(csv.DictReader(f))
    except Exception as e:
        logger.error("[LAB] Scan fwd read: " + str(e))
        return

    if not rows:
        return

    # Resolve tokens for all strikes in the scan
    # Get expiry for token lookup
    try:
        _expiry = D.get_nearest_expiry(kite)
    except Exception:
        _expiry = None
    if not _expiry:
        logger.warning("[LAB] Scan fwd fill: no expiry found")
        return

    _token_cache_fwd = {}

    changed = 0
    for row in rows:
        # Skip already filled
        if row.get("fwd_3c"):
            continue
        opt_type = row.get("direction", "")
        strike = int(float(row.get("atm_strike", 0)))
        if strike <= 0 or not opt_type:
            continue

        # Resolve token from strike + expiry (cached per strike)
        cache_key = str(strike) + "_" + opt_type
        if cache_key in _token_cache_fwd:
            token = _token_cache_fwd[cache_key]
        else:
            try:
                tokens = D.get_option_tokens(kite, strike, _expiry)
                token = tokens.get(opt_type, {}).get("token")
                _token_cache_fwd[cache_key] = token
            except Exception:
                token = None

        if not token:
            continue

        try:
            ts = datetime.fromisoformat(row["timestamp"])
            # Strip timezone for comparison with Kite candles
            if ts.tzinfo is not None:
                ts = ts.replace(tzinfo=None)
            entry = float(row.get("entry_price", 0))
            if entry <= 0:
                continue

            # v12.15.1: Fetch forward prices at 3, 5, 10 CANDLES (not minutes)
            # Use 1-min candles from entry time, look ahead N candles
            fwd_from = ts - timedelta(minutes=1)
            fwd_to = ts + timedelta(minutes=15)  # 15min window covers 10 candles
            candles = _fetch_candles(kite, token, fwd_from, fwd_to, "minute")
            time.sleep(0.3)

            if not candles or len(candles) < 3:
                continue

            # Find the candle at entry time (closest to ts)
            entry_idx = 0
            for i, c in enumerate(candles):
                c_time = c["date"] if isinstance(c["date"], datetime) else datetime.fromisoformat(str(c["date"]))
                if hasattr(c_time, 'tzinfo') and c_time.tzinfo is not None:
                    c_time = c_time.replace(tzinfo=None)
                if c_time <= ts:
                    entry_idx = i

            # Forward prices at 3, 5, 10 candles after entry
            prices = []
            for n_candles in [3, 5, 10]:
                idx = entry_idx + n_candles
                if idx < len(candles):
                    prices.append(round(float(candles[idx]["close"]), 2))
                else:
                    prices.append(None)

            if all(p is not None for p in prices):
                row["fwd_3c"]  = prices[0]
                row["fwd_5c"]  = prices[1]
                row["fwd_10c"] = prices[2]
                max_move = max(p - entry for p in prices)
                min_move = min(p - entry for p in prices)
                if max_move >= 10:
                    row["fwd_outcome"] = "WIN"
                elif min_move <= -8:
                    row["fwd_outcome"] = "LOSS"
                else:
                    row["fwd_outcome"] = "NEUTRAL"
                changed += 1
        except Exception as e:
            logger.debug("[LAB] Scan fwd row: " + str(e))

    try:
        with open(path, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=FIELDNAMES_SCAN, extrasaction="ignore")
            w.writeheader()
            w.writerows(rows)
            f.flush()
        logger.info("[LAB] Scan fwd fill done: " + str(changed) + " rows")
    except Exception as e:
        logger.error("[LAB] Scan fwd write: " + str(e))

    # Dual write: update SQLite scan forward fill
    try:
        for row in rows:
            if row.get("fwd_3c"):
                DB.update_scan_fwd(
                    row.get("timestamp", ""), row.get("direction", ""),
                    row.get("fwd_3c"), row.get("fwd_5c"), row.get("fwd_10c"),
                    row.get("fwd_outcome"))
    except Exception:
        pass


# ─── DAILY SUMMARY CSV (v12.15) ──────────────────────────────

FIELDNAMES_DAILY = [
    "date", "day_of_week",
    # Trade stats
    "total_trades", "wins", "losses", "pnl_pts", "pnl_rs",
    "best_trade_pts", "worst_trade_pts",
    "avg_peak", "avg_trough", "avg_candles_held",
    # Scan stats
    "total_scans", "total_fired",
    "blocks_3m_gate", "blocks_spread", "blocks_rsi",
    "blocks_body", "blocks_volume", "blocks_score",
    # Market context
    "bias", "vix_open", "vix_close", "vix_high",
    "spot_open", "spot_close", "spot_high", "spot_low", "spot_range",
    "gap_pts",
    "dte",
    # Warning data
    "straddle_open", "straddle_close", "straddle_decay_pct",
    "hourly_rsi_high", "hourly_rsi_low",
    # Regime distribution
    "regime_trending_pct", "regime_choppy_pct",
]


def generate_daily_summary(target_date: date = None):
    """
    v12.15: Generate one-row-per-day summary CSV.
    Called at EOD from VRL_MAIN.
    """
    if target_date is None:
        target_date = date.today()

    summary_path = os.path.join(D.REPORTS_DIR, "vrl_daily_summary.csv")
    os.makedirs(os.path.dirname(summary_path), exist_ok=True)

    row = {"date": target_date.isoformat(),
           "day_of_week": target_date.strftime("%A")}

    # ── Trade stats ──
    trade_log = os.path.join(D.LAB_DIR, "vrl_trade_log.csv")
    today_str = target_date.isoformat()
    trades = []
    if os.path.isfile(trade_log):
        try:
            with open(trade_log) as f:
                for r in csv.DictReader(f):
                    if r.get("date", "").strip() == today_str:
                        trades.append(r)
        except Exception:
            pass

    if trades:
        pnls = [float(t.get("pnl_pts", 0)) for t in trades]
        peaks = [float(t.get("peak_pnl", 0)) for t in trades]
        troughs = [float(t.get("trough_pnl", 0)) for t in trades]
        candles = [int(t.get("candles_held", 0)) for t in trades]
        row["total_trades"]     = len(trades)
        row["wins"]             = sum(1 for p in pnls if p > 0)
        row["losses"]           = sum(1 for p in pnls if p < 0)
        row["pnl_pts"]          = round(sum(pnls), 2)
        row["pnl_rs"]           = round(sum(pnls) * D.LOT_SIZE, 0)
        row["best_trade_pts"]   = round(max(pnls), 2)
        row["worst_trade_pts"]  = round(min(pnls), 2)
        row["avg_peak"]         = round(sum(peaks) / len(peaks), 1) if peaks else 0
        row["avg_trough"]       = round(sum(troughs) / len(troughs), 1) if troughs else 0
        row["avg_candles_held"] = round(sum(candles) / len(candles), 1) if candles else 0
    else:
        for k in ["total_trades", "wins", "losses", "pnl_pts", "pnl_rs",
                   "best_trade_pts", "worst_trade_pts", "avg_peak",
                   "avg_trough", "avg_candles_held"]:
            row[k] = 0

    # ── Scan stats ──
    scan_path = _csv_path_scan(target_date)
    scans = []
    if os.path.isfile(scan_path):
        try:
            with open(scan_path) as f:
                scans = list(csv.DictReader(f))
        except Exception:
            pass

    if scans:
        row["total_scans"]    = len(scans)
        row["total_fired"]    = sum(1 for s in scans if s.get("fired") == "1")
        reasons = [s.get("reject_reason", "") for s in scans if s.get("fired") != "1"]
        row["blocks_3m_gate"] = sum(1 for r in reasons if "3M" in r)
        row["blocks_spread"]  = sum(1 for r in reasons if "SPREAD" in r.upper() or "1M_SPREAD" in r.upper())
        row["blocks_rsi"]     = sum(1 for r in reasons if "RSI" in r)
        row["blocks_body"]    = sum(1 for r in reasons if "BODY" in r)
        row["blocks_volume"]  = sum(1 for r in reasons if "VOLUME" in r.upper() or "VOL" in r.upper())
        row["blocks_score"]   = sum(1 for r in reasons if "SCORE" in r)
        # Regime distribution
        regimes = [s.get("spot_regime", "") for s in scans if s.get("spot_regime")]
        if regimes:
            row["regime_trending_pct"] = round(sum(1 for r in regimes if "TREND" in r) / len(regimes) * 100, 0)
            row["regime_choppy_pct"]   = round(sum(1 for r in regimes if "CHOPPY" in r or "NEUTRAL" in r) / len(regimes) * 100, 0)
    else:
        for k in ["total_scans", "total_fired", "blocks_3m_gate",
                   "blocks_spread", "blocks_rsi", "blocks_body",
                   "blocks_volume", "blocks_score",
                   "regime_trending_pct", "regime_choppy_pct"]:
            row[k] = 0

    # ── Market context ──
    try:
        row["bias"] = D.get_daily_bias() if hasattr(D, "get_daily_bias") else ""
    except Exception:
        row["bias"] = ""

    try:
        row["vix_open"]  = round(D.get_vix(), 1)
        row["vix_close"] = round(D.get_vix(), 1)
        row["vix_high"]  = round(D.get_vix(), 1)
    except Exception:
        row["vix_open"] = row["vix_close"] = row["vix_high"] = 0

    # Spot from spot CSV
    spot_path = os.path.join(D.SPOT_DIR, "nifty_spot_1min_" + target_date.strftime("%Y%m%d") + ".csv")
    if os.path.isfile(spot_path):
        try:
            with open(spot_path) as f:
                spot_rows = list(csv.DictReader(f))
            if spot_rows:
                closes = [float(r.get("close", 0)) for r in spot_rows if float(r.get("close", 0)) > 0]
                highs  = [float(r.get("high", 0)) for r in spot_rows if float(r.get("high", 0)) > 0]
                lows   = [float(r.get("low", 0)) for r in spot_rows if float(r.get("low", 0)) > 0]
                if closes:
                    row["spot_open"]  = round(closes[0], 1)
                    row["spot_close"] = round(closes[-1], 1)
                if highs:
                    row["spot_high"] = round(max(highs), 1)
                if lows:
                    row["spot_low"]  = round(min(lows), 1)
                if highs and lows:
                    row["spot_range"] = round(max(highs) - min(lows), 1)
        except Exception:
            pass

    try:
        row["gap_pts"] = round(D.get_spot_gap(), 1) if hasattr(D, "get_spot_gap") else 0
    except Exception:
        row["gap_pts"] = 0

    try:
        exp = D.get_nearest_expiry()
        row["dte"] = D.calculate_dte(exp) if exp else 0
    except Exception:
        row["dte"] = 0

    # Straddle
    try:
        row["straddle_open"]      = round(getattr(D, "_straddle_open", 0), 1)
        row["straddle_close"]     = 0
        row["straddle_decay_pct"] = 0
    except Exception:
        pass

    # Hourly RSI
    try:
        row["hourly_rsi_high"] = round(D.get_hourly_rsi(), 1) if hasattr(D, "get_hourly_rsi") else 0
        row["hourly_rsi_low"]  = round(D.get_hourly_rsi(), 1) if hasattr(D, "get_hourly_rsi") else 0
    except Exception:
        row["hourly_rsi_high"] = row["hourly_rsi_low"] = 0

    # ── Write ──
    is_new = not os.path.isfile(summary_path)
    try:
        with open(summary_path, "a", newline="") as f:
            w = csv.DictWriter(f, fieldnames=FIELDNAMES_DAILY, extrasaction="ignore")
            if is_new:
                w.writeheader()
            w.writerow(row)
            f.flush()
        logger.info("[LAB] Daily summary written for " + str(target_date))
    except Exception as e:
        logger.error("[LAB] Daily summary write: " + str(e))



# ─── 5-MIN OPTION COLLECTOR ──────────────────────────────────

def collect_option_5min(kite, spot_ltp: float):
    """Collect last closed 5-min option candle for ATM CE + PE."""
    global _current_atm_strike, _current_atm_tokens, _current_expiry
    if not _current_atm_tokens or not _current_expiry:
        return
    now = datetime.now()
    if not D.is_market_open():
        return
    today = date.today()
    dte = D.calculate_dte(_current_expiry)
    session = D.get_session_block(now.hour, now.minute)
    from_dt = now - timedelta(days=3)
    to_dt = now
    all_rows = []
    for opt_type, info in _current_atm_tokens.items():
        token = info["token"]
        try:
            candles = _fetch_candles_with_warmup(kite, token, from_dt, to_dt, "5minute", 30)
            if not candles or len(candles) < 2:
                continue
            last = candles[-2]
            df = pd.DataFrame(candles)
            df.rename(columns={"date": "timestamp"}, inplace=True)
            df.set_index("timestamp", inplace=True)
            df = D.add_indicators(df)
            row = df.iloc[-2]
            c = float(row["close"])
            o = float(row["open"])
            h = float(row["high"])
            l_val = float(row["low"])
            rng = h - l_val
            e9 = float(row.get("EMA_9", c))
            e21 = float(row.get("EMA_21", c))
            # ADX for 5m
            adx_val_5m = 0
            try:
                import numpy as _np
                _up5 = df["high"].diff()
                _dn5 = -df["low"].diff()
                _pdm5 = _np.where((_up5 > _dn5) & (_up5 > 0), _up5, 0.0)
                _ndm5 = _np.where((_dn5 > _up5) & (_dn5 > 0), _dn5, 0.0)
                _tr5 = pd.concat([df["high"]-df["low"],
                                  (df["high"]-df["close"].shift(1)).abs(),
                                  (df["low"]-df["close"].shift(1)).abs()], axis=1).max(axis=1)
                _atr5 = _tr5.ewm(alpha=1/14, adjust=False).mean()
                _pdi5 = 100 * pd.Series(_pdm5, index=df.index).ewm(alpha=1/14, adjust=False).mean() / _atr5
                _ndi5 = 100 * pd.Series(_ndm5, index=df.index).ewm(alpha=1/14, adjust=False).mean() / _atr5
                _adx5 = ((_pdi5-_ndi5).abs() / (_pdi5+_ndi5+1e-9) * 100).ewm(alpha=1/14, adjust=False).mean()
                adx_val_5m = round(float(_adx5.iloc[-2]), 1)
            except Exception:
                pass
            vols = [df.iloc[i]["volume"] for i in range(-7, -2) if i >= -len(df) and df.iloc[i]["volume"] > 0]
            avg_v = sum(vols) / len(vols) if vols else 1
            greeks = D.get_full_greeks(c, spot_ltp, _current_atm_strike, _current_expiry, opt_type)
            ts_str = (last["date"].strftime("%Y-%m-%d %H:%M:%S")
                      if hasattr(last["date"], "strftime") else str(last["date"]))
            all_rows.append({
                "timestamp": ts_str, "strike": _current_atm_strike, "type": opt_type,
                "open": round(o, 2), "high": round(h, 2), "low": round(l_val, 2), "close": round(c, 2),
                "volume": int(last["volume"]), "spot_ref": round(spot_ltp, 2),
                "dte": dte, "session_block": session,
                "body_pct": round(abs(c - o) / rng * 100, 1) if rng > 0 else 0,
                "rsi": round(float(row.get("RSI", 50)), 1),
                "ema9": round(e9, 2), "ema21": round(e21, 2),
                "ema_spread": round(e9 - e21, 2), "adx": adx_val_5m,
                "volume_ratio": round(last["volume"] / avg_v if avg_v > 0 else 1, 2),
                "iv_pct": greeks.get("iv_pct", 0), "delta": greeks.get("delta", 0),
            })
        except Exception as e:
            logger.debug("[LAB] 5m error " + opt_type + ": " + str(e))
        time.sleep(0.35)
    if all_rows:
        _append_rows(_csv_path_5m(today), FIELDNAMES_5M, all_rows)
        try:
            DB.insert_option_5min_many(all_rows)
        except Exception:
            pass
        logger.debug("[LAB] 5m wrote=" + str(len(all_rows)))


def collect_option_15min(kite, spot_ltp: float):
    """Collect last closed 15-min option candle for ATM CE + PE."""
    global _current_atm_strike, _current_atm_tokens, _current_expiry
    if not _current_atm_tokens or not _current_expiry:
        return
    now = datetime.now()
    if not D.is_market_open():
        return
    today = date.today()
    dte = D.calculate_dte(_current_expiry)
    session = D.get_session_block(now.hour, now.minute)
    from_dt = now - timedelta(days=10)
    to_dt = now
    all_rows = []
    for opt_type, info in _current_atm_tokens.items():
        token = info["token"]
        try:
            candles = _fetch_candles_with_warmup(kite, token, from_dt, to_dt, "15minute", 30)
            if not candles or len(candles) < 2:
                continue
            last = candles[-2]
            df = pd.DataFrame(candles)
            df.rename(columns={"date": "timestamp"}, inplace=True)
            df.set_index("timestamp", inplace=True)
            df = D.add_indicators(df)
            row = df.iloc[-2]
            c = float(row["close"])
            o = float(row["open"])
            h = float(row["high"])
            l_val = float(row["low"])
            rng = h - l_val
            e9 = float(row.get("EMA_9", c))
            e21 = float(row.get("EMA_21", c))
            # ADX calc
            adx_val = 0
            try:
                import numpy as _np
                up = df["high"].diff()
                dn = -df["low"].diff()
                pdm = _np.where((up > dn) & (up > 0), up, 0.0)
                ndm = _np.where((dn > up) & (dn > 0), dn, 0.0)
                tr = pd.concat([df["high"]-df["low"],
                                (df["high"]-df["close"].shift(1)).abs(),
                                (df["low"]-df["close"].shift(1)).abs()], axis=1).max(axis=1)
                atr_s = tr.ewm(alpha=1/14, adjust=False).mean()
                pdi = 100 * pd.Series(pdm, index=df.index).ewm(alpha=1/14, adjust=False).mean() / atr_s
                ndi = 100 * pd.Series(ndm, index=df.index).ewm(alpha=1/14, adjust=False).mean() / atr_s
                adx_s = ((pdi-ndi).abs() / (pdi+ndi+1e-9) * 100).ewm(alpha=1/14, adjust=False).mean()
                adx_val = round(float(adx_s.iloc[-2]), 1)
            except Exception:
                pass
            # MACD
            macd_hist = 0
            try:
                ema12 = df["close"].ewm(span=12, adjust=False).mean()
                ema26 = df["close"].ewm(span=26, adjust=False).mean()
                macd_line = ema12 - ema26
                macd_sig = macd_line.ewm(span=9, adjust=False).mean()
                macd_hist = round(float((macd_line - macd_sig).iloc[-2]), 2)
            except Exception:
                pass
            vols = [df.iloc[i]["volume"] for i in range(-5, -2) if i >= -len(df) and df.iloc[i]["volume"] > 0]
            avg_v = sum(vols) / len(vols) if vols else 1
            greeks = D.get_full_greeks(c, spot_ltp, _current_atm_strike, _current_expiry, opt_type)
            ts_str = (last["date"].strftime("%Y-%m-%d %H:%M:%S")
                      if hasattr(last["date"], "strftime") else str(last["date"]))
            all_rows.append({
                "timestamp": ts_str, "strike": _current_atm_strike, "type": opt_type,
                "open": round(o, 2), "high": round(h, 2), "low": round(l_val, 2), "close": round(c, 2),
                "volume": int(last["volume"]), "spot_ref": round(spot_ltp, 2),
                "dte": dte, "session_block": session,
                "body_pct": round(abs(c - o) / rng * 100, 1) if rng > 0 else 0,
                "rsi": round(float(row.get("RSI", 50)), 1),
                "ema9": round(e9, 2), "ema21": round(e21, 2),
                "ema_spread": round(e9 - e21, 2),
                "macd_hist": macd_hist, "adx": adx_val,
                "volume_ratio": round(last["volume"] / avg_v if avg_v > 0 else 1, 2),
                "iv_pct": greeks.get("iv_pct", 0), "delta": greeks.get("delta", 0),
            })
        except Exception as e:
            logger.debug("[LAB] 15m error " + opt_type + ": " + str(e))
        time.sleep(0.35)
    if all_rows:
        _append_rows(_csv_path_15m(today), FIELDNAMES_15M, all_rows)
        try:
            DB.insert_option_15min_many(all_rows)
        except Exception:
            pass
        logger.debug("[LAB] 15m wrote=" + str(len(all_rows)))


def collect_spot_5min(kite):
    """Collect last closed 5-min spot candle."""
    if not D.is_market_open():
        return
    try:
        now = datetime.now()
        candles = kite.historical_data(
            instrument_token=D.NIFTY_SPOT_TOKEN,
            from_date=now - timedelta(days=3), to_date=now,
            interval="5minute", continuous=False, oi=False)
        if not candles or len(candles) < 15:
            return
        df = pd.DataFrame(candles)
        df.rename(columns={"date": "timestamp"}, inplace=True)
        df.set_index("timestamp", inplace=True)
        df = D.add_indicators(df)
        last = df.iloc[-2]
        c = float(last["close"])
        e9 = float(last.get("EMA_9", c))
        e21 = float(last.get("EMA_21", c))
        # ADX
        adx_val = 0
        try:
            import numpy as _np
            _up5 = df["high"].diff()
            _dn5 = -df["low"].diff()
            _pdm5 = _np.where((_up5 > _dn5) & (_up5 > 0), _up5, 0.0)
            _ndm5 = _np.where((_dn5 > _up5) & (_dn5 > 0), _dn5, 0.0)
            _tr5 = pd.concat([df["high"]-df["low"],
                              (df["high"]-df["close"].shift(1)).abs(),
                              (df["low"]-df["close"].shift(1)).abs()], axis=1).max(axis=1)
            _atr5 = _tr5.ewm(alpha=1/14, adjust=False).mean()
            _pdi5 = 100 * pd.Series(_pdm5, index=df.index).ewm(alpha=1/14, adjust=False).mean() / _atr5
            _ndi5 = 100 * pd.Series(_ndm5, index=df.index).ewm(alpha=1/14, adjust=False).mean() / _atr5
            _adx5 = ((_pdi5-_ndi5).abs() / (_pdi5+_ndi5+1e-9) * 100).ewm(alpha=1/14, adjust=False).mean()
            adx_val = round(float(_adx5.iloc[-2]), 1)
        except Exception:
            pass
        ts_str = str(df.index[-2])[:19]
        path = _csv_path_spot_5m()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        is_new = not os.path.isfile(path)
        import csv as _csv
        with open(path, "a", newline="") as f:
            w = _csv.DictWriter(f, fieldnames=FIELDNAMES_SPOT_5M, extrasaction="ignore")
            if is_new:
                w.writeheader()
            _s5_row = {
                "timestamp": ts_str,
                "open": round(float(last["open"]), 2),
                "high": round(float(last["high"]), 2),
                "low": round(float(last["low"]), 2),
                "close": round(c, 2),
                "volume": int(last["volume"]),
                "ema9": round(e9, 2), "ema21": round(e21, 2),
                "ema_spread": round(e9 - e21, 2),
                "rsi": round(float(last.get("RSI", 50)), 1),
                "adx": adx_val,
            }
            w.writerow(_s5_row)
            f.flush()
        try:
            DB.insert_spot_5min(_s5_row)
        except Exception:
            pass
    except Exception as e:
        logger.debug("[LAB] Spot 5m: " + str(e))


def collect_spot_15min(kite):
    """Collect last closed 15-min spot candle."""
    if not D.is_market_open():
        return
    try:
        now = datetime.now()
        candles = kite.historical_data(
            instrument_token=D.NIFTY_SPOT_TOKEN,
            from_date=now - timedelta(days=10), to_date=now,
            interval="15minute", continuous=False, oi=False)
        if not candles or len(candles) < 20:
            return
        df = pd.DataFrame(candles)
        df.rename(columns={"date": "timestamp"}, inplace=True)
        df.set_index("timestamp", inplace=True)
        df = D.add_indicators(df)
        last = df.iloc[-2]
        c = float(last["close"])
        e9 = float(last.get("EMA_9", c))
        e21 = float(last.get("EMA_21", c))
        # ADX
        adx_val = 0
        try:
            import numpy as _np
            up = df["high"].diff()
            dn = -df["low"].diff()
            pdm = _np.where((up > dn) & (up > 0), up, 0.0)
            ndm = _np.where((dn > up) & (dn > 0), dn, 0.0)
            tr = pd.concat([df["high"]-df["low"],
                            (df["high"]-df["close"].shift(1)).abs(),
                            (df["low"]-df["close"].shift(1)).abs()], axis=1).max(axis=1)
            atr_s = tr.ewm(alpha=1/14, adjust=False).mean()
            pdi = 100 * pd.Series(pdm, index=df.index).ewm(alpha=1/14, adjust=False).mean() / atr_s
            ndi = 100 * pd.Series(ndm, index=df.index).ewm(alpha=1/14, adjust=False).mean() / atr_s
            adx_s = ((pdi-ndi).abs() / (pdi+ndi+1e-9) * 100).ewm(alpha=1/14, adjust=False).mean()
            adx_val = round(float(adx_s.iloc[-2]), 1)
        except Exception:
            pass
        ts_str = str(df.index[-2])[:19]
        path = _csv_path_spot_15m()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        is_new = not os.path.isfile(path)
        import csv as _csv
        with open(path, "a", newline="") as f:
            w = _csv.DictWriter(f, fieldnames=FIELDNAMES_SPOT_15M, extrasaction="ignore")
            if is_new:
                w.writeheader()
            _s15_row = {
                "timestamp": ts_str,
                "open": round(float(last["open"]), 2),
                "high": round(float(last["high"]), 2),
                "low": round(float(last["low"]), 2),
                "close": round(c, 2),
                "volume": int(last["volume"]),
                "ema9": round(e9, 2), "ema21": round(e21, 2),
                "ema_spread": round(e9 - e21, 2),
                "rsi": round(float(last.get("RSI", 50)), 1),
                "adx": adx_val,
            }
            w.writerow(_s15_row)
            f.flush()
        try:
            DB.insert_spot_15min(_s15_row)
        except Exception:
            pass
    except Exception as e:
        logger.debug("[LAB] Spot 15m: " + str(e))



# ─── HOURLY (60-MIN) SPOT COLLECTOR ──────────────────────────

def collect_spot_60min(kite):
    """Collect last closed 60-min spot candle with EMA + RSI + ADX."""
    if not D.is_market_open():
        return
    try:
        now = datetime.now()
        candles = kite.historical_data(
            instrument_token=D.NIFTY_SPOT_TOKEN,
            from_date=now - timedelta(days=30), to_date=now,
            interval="60minute", continuous=False, oi=False)
        if not candles or len(candles) < 20:
            return
        df = pd.DataFrame(candles)
        df.rename(columns={"date": "timestamp"}, inplace=True)
        df.set_index("timestamp", inplace=True)
        df = D.add_indicators(df)
        last = df.iloc[-2]
        c = float(last["close"])
        e9 = float(last.get("EMA_9", c))
        e21 = float(last.get("EMA_21", c))
        # ADX
        adx_val = 0
        try:
            import numpy as _np
            _up = df["high"].diff()
            _dn = -df["low"].diff()
            _pdm = _np.where((_up > _dn) & (_up > 0), _up, 0.0)
            _ndm = _np.where((_dn > _up) & (_dn > 0), _dn, 0.0)
            _tr = pd.concat([df["high"]-df["low"],
                             (df["high"]-df["close"].shift(1)).abs(),
                             (df["low"]-df["close"].shift(1)).abs()], axis=1).max(axis=1)
            _atr = _tr.ewm(alpha=1/14, adjust=False).mean()
            _pdi = 100 * pd.Series(_pdm, index=df.index).ewm(alpha=1/14, adjust=False).mean() / _atr
            _ndi = 100 * pd.Series(_ndm, index=df.index).ewm(alpha=1/14, adjust=False).mean() / _atr
            _adxs = ((_pdi-_ndi).abs() / (_pdi+_ndi+1e-9) * 100).ewm(alpha=1/14, adjust=False).mean()
            adx_val = round(float(_adxs.iloc[-2]), 1)
        except Exception:
            pass
        ts_str = str(df.index[-2])[:19]
        path = _csv_path_spot_60m()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        is_new = not os.path.isfile(path)
        import csv as _csv
        with open(path, "a", newline="") as f:
            w = _csv.DictWriter(f, fieldnames=FIELDNAMES_SPOT_60M, extrasaction="ignore")
            if is_new:
                w.writeheader()
            _s60_row = {
                "timestamp": ts_str,
                "open": round(float(last["open"]), 2),
                "high": round(float(last["high"]), 2),
                "low": round(float(last["low"]), 2),
                "close": round(c, 2),
                "volume": int(last["volume"]),
                "ema9": round(e9, 2), "ema21": round(e21, 2),
                "ema_spread": round(e9 - e21, 2),
                "rsi": round(float(last.get("RSI", 50)), 1),
                "adx": adx_val,
            }
            w.writerow(_s60_row)
            f.flush()
        try:
            DB.insert_spot_60min(_s60_row)
        except Exception:
            pass
        logger.debug("[LAB] Spot 60m wrote @" + ts_str[-5:])
    except Exception as e:
        logger.debug("[LAB] Spot 60m: " + str(e))


# ─── DAILY SPOT COLLECTOR ────────────────────────────────────

def collect_spot_daily(kite):
    """Collect daily spot candle with EMA21 + RSI + ADX. Runs once at EOD."""
    try:
        now = datetime.now()
        candles = kite.historical_data(
            instrument_token=D.NIFTY_SPOT_TOKEN,
            from_date=now - timedelta(days=90), to_date=now,
            interval="day", continuous=False, oi=False)
        if not candles or len(candles) < 25:
            return
        df = pd.DataFrame(candles)
        for col in ("open", "high", "low", "close"):
            df[col] = df[col].astype(float)
        df["volume"] = df["volume"].astype(int)
        # EMA21
        df["ema21"] = df["close"].ewm(span=21, adjust=False).mean()
        # RSI
        delta = df["close"].diff()
        gain = delta.clip(lower=0).ewm(alpha=1/14, adjust=False).mean()
        loss = (-delta.clip(upper=0)).ewm(alpha=1/14, adjust=False).mean()
        df["rsi"] = 100 - 100 / (1 + gain / (loss + 1e-9))
        # ADX
        adx_val = 0
        try:
            import numpy as _np
            _up = df["high"].diff()
            _dn = -df["low"].diff()
            _pdm = _np.where((_up > _dn) & (_up > 0), _up, 0.0)
            _ndm = _np.where((_dn > _up) & (_dn > 0), _dn, 0.0)
            _tr = pd.concat([df["high"]-df["low"],
                             (df["high"]-df["close"].shift(1)).abs(),
                             (df["low"]-df["close"].shift(1)).abs()], axis=1).max(axis=1)
            _atr = _tr.ewm(alpha=1/14, adjust=False).mean()
            _pdi = 100 * pd.Series(_pdm, index=df.index).ewm(alpha=1/14, adjust=False).mean() / _atr
            _ndi = 100 * pd.Series(_ndm, index=df.index).ewm(alpha=1/14, adjust=False).mean() / _atr
            _adxs = ((_pdi-_ndi).abs() / (_pdi+_ndi+1e-9) * 100).ewm(alpha=1/14, adjust=False).mean()
            df["adx"] = _adxs
        except Exception:
            df["adx"] = 0
        # Write last row (today or yesterday)
        last = df.iloc[-1]
        dt_str = str(candles[-1]["date"])[:10]
        path = _csv_path_spot_daily()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        # Check if already written for this date
        existing_dates = set()
        if os.path.isfile(path):
            import csv as _csv2
            with open(path) as f:
                for r in _csv2.DictReader(f):
                    existing_dates.add(r.get("date", ""))
        if dt_str in existing_dates:
            return
        import csv as _csv
        is_new = not os.path.isfile(path)
        with open(path, "a", newline="") as f:
            w = _csv.DictWriter(f, fieldnames=FIELDNAMES_SPOT_DAILY, extrasaction="ignore")
            if is_new:
                w.writeheader()
            _sd_row = {
                "date": dt_str,
                "open": round(float(last["open"]), 2),
                "high": round(float(last["high"]), 2),
                "low": round(float(last["low"]), 2),
                "close": round(float(last["close"]), 2),
                "volume": int(last["volume"]),
                "ema21": round(float(last["ema21"]), 2),
                "rsi": round(float(last["rsi"]), 1),
                "adx": round(float(last["adx"]), 1),
            }
            w.writerow(_sd_row)
            f.flush()
        try:
            DB.insert_spot_daily(_sd_row)
        except Exception:
            pass
        logger.info("[LAB] Daily spot wrote " + dt_str)
    except Exception as e:
        logger.debug("[LAB] Spot daily: " + str(e))


def start_lab(kite):
    """
    Entry point. Call after kite auth in VRL_MAIN.py.
    Backfills history then starts live collection loop.
    Runs as daemon thread — dies when main exits.
    """
    global _kite_ref, _lab_running
    _kite_ref    = kite
    _lab_running = True

    # Initialize SQLite database
    try:
        DB.init_db()
        logger.info("[LAB] SQLite database initialized")
    except Exception as e:
        logger.warning("[LAB] SQLite init error: " + str(e))

    def _start():
        logger.info("[LAB] Starting — skipping backfill, direct to collection")
        _lab_loop()

    thread = threading.Thread(target=_start, name="LabCollector", daemon=True)
    thread.start()
    logger.info("[LAB] Collection thread started")



def _lab_loop():
    global _last_3min, _last_1min, _fwd_done

    _last_daily_reset = None   # Fix: prevent triple reset

    while _lab_running:
        try:
            now   = datetime.now()
            today = date.today()

            # v12.16: Weekend guard — no collection on Sat/Sun
            if today.weekday() >= 5:
                time.sleep(60)
                continue

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

                # Auto-cleanup: old logs (>7 days) + stale zips
                import glob as _cg
                _cleaned = 0
                for _old_log in _cg.glob(os.path.expanduser("~/logs/live/vrl_live.log.*")):
                    if os.path.getmtime(_old_log) < time.time() - 7 * 86400:
                        os.remove(_old_log)
                        _cleaned += 1
                for _old_zip in _cg.glob(os.path.expanduser("~/state/today_*.zip")):
                    os.remove(_old_zip)
                    _cleaned += 1
                if _cleaned:
                    logger.info("[LAB] Cleanup: deleted " + str(_cleaned) + " old files")

            # ── Fetch spot LTP once per loop iteration ────────
            _loop_spot_ltp = D.get_ltp(D.NIFTY_SPOT_TOKEN)
            if _loop_spot_ltp <= 0:
                try:
                    _q = _kite_ref.ltp("NSE:NIFTY 50")
                    _loop_spot_ltp = float(list(_q.values())[0]["last_price"])
                except Exception:
                    pass

            # ── 1-min collection at HH:MM:30 ──────────────────
            one_min_key = (today, now.hour, now.minute)
            if one_min_key != _last_1min and now.second >= 30:
                _last_1min  = one_min_key
                spot_ltp    = _loop_spot_ltp
                if spot_ltp > 0:
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
                spot_ltp   = _loop_spot_ltp
                if spot_ltp > 0:
                    try:
                        collect_option_3min(_kite_ref, spot_ltp)
                    except Exception as e:
                        logger.error("[LAB] 3m error: " + str(e))
                elif spot_ltp <= 0 and D.is_market_open():
                    logger.debug("[LAB] 3m skip — spot LTP not available yet")

            # ── 5-min collection at boundary + 30s ────────────
            five_min    = (now.minute // 5) * 5
            five_min_key = (today, now.hour, five_min)
            if (not hasattr(_lab_loop, '_last_5min') or
                    getattr(_lab_loop, '_last_5min', None) != five_min_key) and now.second >= 30:
                _lab_loop._last_5min = five_min_key
                spot_ltp = _loop_spot_ltp
                if spot_ltp > 0:
                    try:
                        collect_option_5min(_kite_ref, spot_ltp)
                    except Exception as e:
                        logger.debug("[LAB] 5m error: " + str(e))
                    try:
                        collect_spot_5min(_kite_ref)
                    except Exception as e:
                        logger.debug("[LAB] spot 5m: " + str(e))

            # ── 60-min collection at hour boundary + 40s ─────
            # v12.16: Fire at 10:00-15:00 (market hours only), with dedup
            sixty_min_key = (today, now.hour)
            if (now.minute == 0 and now.second >= 40 and now.second < 55
                    and 10 <= now.hour <= 15
                    and (not hasattr(_lab_loop, '_last_60min')
                         or getattr(_lab_loop, '_last_60min', None) != sixty_min_key)):
                _lab_loop._last_60min = sixty_min_key
                spot_ltp = _loop_spot_ltp
                if spot_ltp > 0:
                    try:
                        collect_spot_60min(_kite_ref)
                        logger.info("[LAB] 60m spot collected")
                    except Exception as e:
                        logger.debug("[LAB] spot 60m: " + str(e))

            # ── 15-min collection at boundary + 35s ───────────
            fifteen_min    = (now.minute // 15) * 15
            fifteen_min_key = (today, now.hour, fifteen_min)
            if (not hasattr(_lab_loop, '_last_15min') or
                    getattr(_lab_loop, '_last_15min', None) != fifteen_min_key) and now.second >= 35:
                _lab_loop._last_15min = fifteen_min_key
                spot_ltp = _loop_spot_ltp
                if spot_ltp > 0:
                    try:
                        collect_option_15min(_kite_ref, spot_ltp)
                    except Exception as e:
                        logger.debug("[LAB] 15m error: " + str(e))
                    try:
                        collect_spot_15min(_kite_ref)
                    except Exception as e:
                        logger.debug("[LAB] spot 15m: " + str(e))

            # ── Daily spot at 15:30 ───────────────────────────
            # v12.16: dedup guard, fire once at 15:30
            _daily_spot_key = (today, "daily_spot")
            if (now.hour == 15 and now.minute == 30 and now.second < 30
                    and (not hasattr(_lab_loop, '_last_daily_spot')
                         or getattr(_lab_loop, '_last_daily_spot', None) != _daily_spot_key)):
                _lab_loop._last_daily_spot = _daily_spot_key
                try:
                    collect_spot_daily(_kite_ref)
                    logger.info("[LAB] daily spot collected")
                except Exception as e:
                    logger.debug("[LAB] daily spot: " + str(e))

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
                try:
                    fill_forward_scan(_kite_ref, today)
                except Exception as e:
                    logger.error("[LAB] Scan forward fill error: " + str(e))

        except Exception as e:
            logger.error("[LAB] Loop error: " + str(e))

        time.sleep(1)
