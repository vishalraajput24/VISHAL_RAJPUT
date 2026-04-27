# ═══════════════════════════════════════════════════════════════
#  VRL_LAB.py — VISHAL RAJPUT TRADE v13.7
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
    "session_block",
    "body_pct", "adx", "rsi", "ema9", "ema21", "ema_spread", "ema9_gap", "volume_ratio",
    "ema9_high", "ema9_low",   # v15.0: dual EMA9 bands for band-breakout strategy
    "fwd_3c", "fwd_6c", "fwd_9c", "fwd_outcome",
]

FIELDNAMES_1M = [
    "timestamp", "strike", "type",
    "open", "high", "low", "close", "volume",
    "spot_ref", "atm_distance", "dte",
    "session_block",
    "body_pct", "rsi", "ema9", "ema9_gap", "adx",
    "volume_ratio",
    "fwd_1c", "fwd_3c", "fwd_5c", "fwd_outcome",
]

# Signal scan log — live columns only.
# Dead v13 fields removed in the schema migration. CSV matches the DB schema.
FIELDNAMES_SCAN = [
    "timestamp", "session", "dte", "atm_strike", "spot",
    "direction", "entry_price",
    # v15.2 indicator fields
    "ema9_high", "ema9_low", "band_position", "body_pct",
    "body_pct_3m", "ema_spread_3m", "mode_3m",
    # Market context
    "vix", "spot_rsi_3m", "spot_ema_spread_3m", "spot_regime",
    # Result
    "fired", "trade_taken", "reject_reason",
    # Forward fill (populated EOD)
    "fwd_3c", "fwd_5c", "fwd_10c", "fwd_outcome",
]

# ─── SESSION STATE ────────────────────────────────────────────

_current_atm_strike = None
_current_atm_tokens = None
_current_expiry     = None
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


# ─── SPOT 1-MIN COLLECTOR ─────────────────────────────────────

FIELDNAMES_SPOT = ["timestamp", "open", "high", "low", "close", "volume", "ema9", "ema21", "ema_spread", "rsi", "adx"]

def collect_spot_1min(kite):
    """
    Append last closed 1-min SPOT candle to rolling spot CSV.
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
    global _current_atm_strike, _current_atm_tokens, _current_expiry
    with _lab_lock:
        _current_atm_strike = None
        _current_atm_tokens = None
        _current_expiry     = None
    logger.info("[LAB] Session reset")


# ─── LIVE COLLECTION — 3-MIN ──────────────────────────────────

def collect_option_3min(kite, spot_ltp: float):
    """
    Collect last CLOSED 3-min option candle for ATM CE + PE.
    Uses candles[-2] (last closed), not candles[-1] (still forming).
    Call at HH:MM:30 — 30s after each 3-min boundary.
    """
    global _current_atm_strike, _current_atm_tokens, _current_expiry

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
            # Add ema21 + ema_spread + v15.0 bands
            _row3 = df.iloc[-2]
            indic["ema21"] = round(float(_row3.get("EMA_21", _row3["close"])), 2)
            indic["ema_spread"] = round(float(_row3.get("EMA_9", _row3["close"])) - float(_row3.get("EMA_21", _row3["close"])), 2)
            indic["ema9_high"] = round(float(_row3.get("ema9_high", _row3["high"])), 2)
            indic["ema9_low"]  = round(float(_row3.get("ema9_low", _row3["low"])), 2)
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
            "adx"          : indic.get("adx", 0),
            "rsi"          : indic.get("rsi", 50),
            "ema9"         : indic.get("ema9", 0),
            "ema21"        : indic.get("ema21", 0),
            "ema_spread"   : indic.get("ema_spread", 0),
            "ema9_gap"     : indic.get("ema9_gap", 0),
            "volume_ratio" : indic.get("volume_ratio", 1),
            "ema9_high"    : indic.get("ema9_high", 0),
            "ema9_low"     : indic.get("ema9_low", 0),
            "fwd_3c": "", "fwd_6c": "", "fwd_9c": "", "fwd_outcome": "",
        })
        today_ts.add(key)
        time.sleep(0.35)

    if all_rows:
        all_rows.sort(key=lambda r: (r["timestamp"], r["type"]))
        n = _append_rows(_csv_path_3m(today), FIELDNAMES_3M, all_rows)
        try:
            DB.insert_option_3min_many(all_rows)
        except Exception as _dbe:
            logger.warning("[LAB] 3m DB insert failed (CSV still wrote): "
                           + str(_dbe))
        logger.debug("[LAB] 3m wrote=" + str(n) + " @" + now.strftime("%H:%M"))
    try:
        _at_n = _collect_active_trade_candles(
            kite, "3minute", today, now, today_ts)
        if _at_n:
            logger.debug("[LAB] 3m active-trade wrote=" + str(_at_n))
    except Exception as _ate:
        logger.debug("[LAB] 3m active-trade err: " + str(_ate))
    # Also collect for any post-exit observation strikes (10-min window
    # after trade exit) so the data trail continues past the exit point.
    try:
        _pe_n = _collect_post_exit_candles(
            kite, "3minute", today, now, today_ts)
        if _pe_n:
            logger.debug("[LAB] 3m post-exit wrote=" + str(_pe_n))
    except Exception as _pee:
        logger.debug("[LAB] 3m post-exit err: " + str(_pee))


# ── persist active-trade strike candles through ATM rotation ──

def _collect_active_trade_candles(kite, interval: str, today, now,
                                  already_written_keys: set = None):
    """If VRL_MAIN has an active trade at a strike different from the
    current ATM, fetch + write candles for the trade's CE + PE tokens
    so the data trail has zero gaps from entry to exit.

    interval: "3minute" or "minute".
    already_written_keys: set of (timestamp, strike, type) tuples
      already written by the current collection pass — used to dedup.
    """
    active = D.get_active_trade()
    if not active:
        return 0
    trade_strike = active.get("strike", 0)
    if not trade_strike or trade_strike == _current_atm_strike:
        return 0   # same strike, already covered by normal collection
    if _current_expiry is None:
        return 0
    from_dt = now - timedelta(minutes=180 if interval == "3minute" else 60)
    to_dt   = now
    dte     = D.calculate_dte(_current_expiry)
    session = D.get_session_block(now.hour, now.minute)
    spot_ltp = D.get_ltp(D.NIFTY_SPOT_TOKEN)
    n_written = 0
    for side, tok_key in [("CE", "token_ce"), ("PE", "token_pe")]:
        tok = active.get(tok_key, 0)
        if not tok:
            continue
        try:
            candles = _fetch_candles_with_warmup(
                kite, int(tok), from_dt, to_dt, interval, 30)
            if not candles or len(candles) < 2:
                continue
            last = candles[-2]
            ts_str = (last["date"].strftime("%Y-%m-%d %H:%M:%S")
                      if hasattr(last["date"], "strftime") else str(last["date"]))
            key = (ts_str, str(trade_strike), side)
            if already_written_keys and key in already_written_keys:
                continue
            row = {
                "timestamp"    : ts_str,
                "strike"       : trade_strike,
                "type"         : side,
                "open"         : round(last["open"], 2),
                "high"         : round(last["high"], 2),
                "low"          : round(last["low"],  2),
                "close"        : round(last["close"], 2),
                "volume"       : int(last["volume"]),
                "spot_ref"     : round(spot_ltp, 2) if spot_ltp else 0,
                "atm_distance" : round(abs((spot_ltp or 0) - trade_strike), 0),
                "dte"          : dte,
                "session_block": session,
            }
            if interval == "3minute":
                # Compute indicators for the active trade's candles
                _adf = pd.DataFrame(candles)
                _adf.rename(columns={"date": "timestamp"}, inplace=True)
                _adf.set_index("timestamp", inplace=True)
                _adf = D.add_indicators(_adf)
                _arow = _adf.iloc[-2]
                row.update({
                    "rsi"      : round(float(_arow.get("RSI", 50)), 1),
                    "ema9"     : round(float(_arow.get("EMA_9", last["close"])), 2),
                    "ema21"    : round(float(_arow.get("EMA_21", last["close"])), 2),
                    "ema9_high": round(float(_arow.get("ema9_high", last["high"])), 2),
                    "ema9_low" : round(float(_arow.get("ema9_low", last["low"])), 2),
                })
                try:
                    DB.insert_option_3min(row)
                    n_written += 1
                except Exception:
                    pass
            else:
                _adf = pd.DataFrame(candles)
                _adf.rename(columns={"date": "timestamp"}, inplace=True)
                _adf.set_index("timestamp", inplace=True)
                _adf = D.add_indicators(_adf)
                _arow = _adf.iloc[-2]
                row.update({
                    "rsi" : round(float(_arow.get("RSI", 50)), 1),
                    "ema9": round(float(_arow.get("EMA_9", last["close"])), 2),
                })
                try:
                    DB.insert_option_1min(row)
                    n_written += 1
                except Exception:
                    pass
        except Exception as _e:
            logger.debug("[LAB] active-trade candle " + side
                         + " " + interval + ": " + str(_e))
    return n_written


def _collect_post_exit_candles(kite, interval: str, today, now,
                               already_written_keys: set = None):
    """For each post-exit observation registered in VRL_DATA, fetch and
    persist the just-closed candle so the data trail continues past
    trade exit. Without this, lab CSV/DB cuts off at exit and we lose
    visibility into what happened to the option after the bot got out.

    interval: "3minute" or "minute".
    Same dedup approach as _collect_active_trade_candles: skip rows
    already written by the current pass.
    """
    try:
        observations = D.get_post_exit_observations()
    except Exception:
        return 0
    if not observations:
        return 0
    if _current_expiry is None:
        return 0
    from_dt = now - timedelta(minutes=180 if interval == "3minute" else 60)
    to_dt   = now
    dte     = D.calculate_dte(_current_expiry)
    session = D.get_session_block(now.hour, now.minute)
    spot_ltp = D.get_ltp(D.NIFTY_SPOT_TOKEN)
    n_written = 0
    for obs in observations:
        tok    = obs.get("token", 0)
        strike = obs.get("strike", 0)
        side   = obs.get("side", "")
        if not tok or not strike or not side:
            continue
        try:
            candles = _fetch_candles_with_warmup(
                kite, int(tok), from_dt, to_dt, interval, 30)
            if not candles or len(candles) < 2:
                continue
            last = candles[-2]
            ts_str = (last["date"].strftime("%Y-%m-%d %H:%M:%S")
                      if hasattr(last["date"], "strftime") else str(last["date"]))
            key = (ts_str, str(strike), side)
            if already_written_keys and key in already_written_keys:
                continue
            row = {
                "timestamp"    : ts_str,
                "strike"       : strike,
                "type"         : side,
                "open"         : round(last["open"], 2),
                "high"         : round(last["high"], 2),
                "low"          : round(last["low"],  2),
                "close"        : round(last["close"], 2),
                "volume"       : int(last["volume"]),
                "spot_ref"     : round(spot_ltp, 2) if spot_ltp else 0,
                "atm_distance" : round(abs((spot_ltp or 0) - strike), 0),
                "dte"          : dte,
                "session_block": session,
            }
            _adf = pd.DataFrame(candles)
            _adf.rename(columns={"date": "timestamp"}, inplace=True)
            _adf.set_index("timestamp", inplace=True)
            _adf = D.add_indicators(_adf)
            _arow = _adf.iloc[-2]
            if interval == "3minute":
                row.update({
                    "rsi"      : round(float(_arow.get("RSI", 50)), 1),
                    "ema9"     : round(float(_arow.get("EMA_9", last["close"])), 2),
                    "ema21"    : round(float(_arow.get("EMA_21", last["close"])), 2),
                    "ema9_high": round(float(_arow.get("ema9_high", last["high"])), 2),
                    "ema9_low" : round(float(_arow.get("ema9_low", last["low"])), 2),
                })
                try:
                    DB.insert_option_3min(row)
                    n_written += 1
                except Exception:
                    pass
            else:
                row.update({
                    "rsi" : round(float(_arow.get("RSI", 50)), 1),
                    "ema9": round(float(_arow.get("EMA_9", last["close"])), 2),
                })
                try:
                    DB.insert_option_1min(row)
                    n_written += 1
                except Exception:
                    pass
        except Exception as _e:
            logger.debug("[LAB] post-exit candle " + side
                         + " " + interval + ": " + str(_e))
    return n_written


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
            "fwd_1c": "", "fwd_3c": "", "fwd_5c": "", "fwd_outcome": "",
        })
        today_ts.add(key)
        time.sleep(0.25)

    if all_rows:
        all_rows.sort(key=lambda r: (r["timestamp"], r["type"]))
        n = _append_rows(_csv_path_1m(today), FIELDNAMES_1M, all_rows)
        try:
            DB.insert_option_1min_many(all_rows)
        except Exception as _dbe:
            logger.warning("[LAB] 1m DB insert failed (CSV still wrote): "
                           + str(_dbe))
        logger.debug("[LAB] 1m wrote=" + str(n) + " @" + now.strftime("%H:%M"))
    try:
        _at_n = _collect_active_trade_candles(kite, "minute", today, now)
        if _at_n:
            logger.debug("[LAB] 1m active-trade wrote=" + str(_at_n))
    except Exception as _ate:
        logger.debug("[LAB] 1m active-trade err: " + str(_ate))
    # Post-exit observation: keep writing 1-min candles for the just-
    # exited strike for 10 min after exit so the data trail is complete.
    try:
        _pe_n = _collect_post_exit_candles(kite, "minute", today, now)
        if _pe_n:
            logger.debug("[LAB] 1m post-exit wrote=" + str(_pe_n))
    except Exception as _pee:
        logger.debug("[LAB] 1m post-exit err: " + str(_pee))


# ─── BACKFILL — 3-MIN ─────────────────────────────────────────


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
    except Exception as _fwde:
        logger.warning("[LAB] fwd-fill DB update failed (" + timeframe
                       + ", CSV still correct): " + str(_fwde))


def _startup_backfill(kite):
    """Mid-day restart warmup.

    When the bot restarts after market open, today's option_3min /
    option_1min tables have a hole from 09:15 up to the restart
    moment — because LAB was down. The live engine doesn't care
    (check_entry fetches fresh history on every tick), but
    dashboards, audits and daily reports read from the DB and see
    a ragged session.

    Gate: only backfills when today's in-memory indicator buffer is
    effectively empty — i.e. today has <5 option_3min DB rows. If
    the restart was before 09:15 or after 15:30 we also skip.

    Backfill pulls the last ~60 candles per interval for the current
    ATM CE+PE, runs D.add_indicators() so ema9_high/low/RSI are
    populated, and inserts into the DB. Greeks/IV columns are left
    at defaults — a cold restart cannot reconstruct them after the
    fact; only live ticks give IV_vs_open + theta decay signal.
    """
    try:
        today = date.today()
        # Out-of-session restart → nothing useful to backfill
        now = datetime.now()
        mod = now.hour * 60 + now.minute
        if mod < 9 * 60 + 15 or mod > 15 * 60 + 30:
            logger.info("[LAB] Startup backfill skipped — outside session hours")
            return
        # Gate: today's buffer empty?
        try:
            rows = DB.query(
                "SELECT COUNT(*) AS n FROM option_3min "
                "WHERE date(timestamp) = ?", (today.isoformat(),))
            n_today = int(rows[0]["n"]) if rows else 0
        except Exception:
            n_today = 0
        if n_today >= 5:
            logger.info("[LAB] Startup backfill skipped — "
                        + str(n_today) + " option_3min rows already "
                        "present for " + today.isoformat())
            return

        # Resolve current ATM + tokens
        spot_ltp = D.get_ltp(D.NIFTY_SPOT_TOKEN)
        if spot_ltp <= 0:
            logger.warning("[LAB] Startup backfill aborted — no spot LTP yet")
            return
        expiry = D.get_nearest_expiry(kite)
        if expiry is None:
            logger.warning("[LAB] Startup backfill aborted — no expiry")
            return
        atm = D.resolve_atm_strike(spot_ltp)
        tokens = D.get_option_tokens(kite, atm, expiry) or {}
        if not tokens:
            logger.warning("[LAB] Startup backfill aborted — ATM tokens unresolved")
            return

        n_3m = 0
        n_1m = 0
        # Today 00:00 onwards — Kite returns only market-hours bars
        today_start = datetime.combine(today, datetime.min.time())
        for opt_type, info in tokens.items():
            token = int(info.get("token") or 0)
            if not token:
                continue
            # ── 3-min backfill ────────────────────────────────────
            c3 = _fetch_candles_with_warmup(
                kite, token, today_start, now, "3minute", 60)
            if c3:
                try:
                    df3 = pd.DataFrame(c3)
                    df3.rename(columns={"date": "timestamp"}, inplace=True)
                    df3.set_index("timestamp", inplace=True)
                    df3 = D.add_indicators(df3)
                    rows3 = []
                    for ts, r in df3.iterrows():
                        ts_str = (ts.strftime("%Y-%m-%d %H:%M:%S")
                                  if hasattr(ts, "strftime") else str(ts))
                        if ts_str[:10] != today.isoformat():
                            continue
                        rows3.append({
                            "timestamp"    : ts_str,
                            "strike"       : atm, "type": opt_type,
                            "open"         : round(float(r["open"]),  2),
                            "high"         : round(float(r["high"]),  2),
                            "low"          : round(float(r["low"]),   2),
                            "close"        : round(float(r["close"]), 2),
                            "volume"       : int(r.get("volume", 0) or 0),
                            "spot_ref"     : round(spot_ltp, 2),
                            "atm_distance" : round(abs(spot_ltp - atm), 0),
                            "dte"          : D.calculate_dte(expiry),
                            "session_block": D.get_session_block(
                                ts.hour if hasattr(ts, "hour") else 10,
                                ts.minute if hasattr(ts, "minute") else 0),
                            "body_pct"     : 0,
                            "rsi"          : round(float(r.get("RSI", 50)), 1),
                            "ema9"         : round(float(r.get("EMA_9", r["close"])), 2),
                            "ema21"        : round(float(r.get("EMA_21", r["close"])), 2),
                            "ema_spread"   : 0,
                            "ema9_high"    : round(float(r.get("ema9_high", r["high"])), 2),
                            "ema9_low"     : round(float(r.get("ema9_low",  r["low"])),  2),
                        })
                    if rows3:
                        try:
                            DB.insert_option_3min_many(rows3)
                            n_3m += len(rows3)
                        except Exception as _de:
                            logger.debug("[LAB] 3m backfill DB insert: " + str(_de))
                except Exception as _e3:
                    logger.debug("[LAB] 3m backfill parse err: " + str(_e3))

            # ── 1-min backfill ────────────────────────────────────
            c1 = _fetch_candles_with_warmup(
                kite, token, today_start, now, "minute", 60)
            if c1:
                try:
                    df1 = pd.DataFrame(c1)
                    df1.rename(columns={"date": "timestamp"}, inplace=True)
                    df1.set_index("timestamp", inplace=True)
                    df1 = D.add_indicators(df1)
                    rows1 = []
                    for ts, r in df1.iterrows():
                        ts_str = (ts.strftime("%Y-%m-%d %H:%M:%S")
                                  if hasattr(ts, "strftime") else str(ts))
                        if ts_str[:10] != today.isoformat():
                            continue
                        rows1.append({
                            "timestamp"    : ts_str,
                            "strike"       : atm, "type": opt_type,
                            "open"         : round(float(r["open"]),  2),
                            "high"         : round(float(r["high"]),  2),
                            "low"          : round(float(r["low"]),   2),
                            "close"        : round(float(r["close"]), 2),
                            "volume"       : int(r.get("volume", 0) or 0),
                            "spot_ref"     : round(spot_ltp, 2),
                            "atm_distance" : round(abs(spot_ltp - atm), 0),
                            "dte"          : D.calculate_dte(expiry),
                            "session_block": D.get_session_block(
                                ts.hour if hasattr(ts, "hour") else 10,
                                ts.minute if hasattr(ts, "minute") else 0),
                            "body_pct"     : 0,
                            "rsi"          : round(float(r.get("RSI", 50)), 1),
                            "ema9"         : round(float(r.get("EMA_9", r["close"])), 2),
                            "ema9_gap"     : 0,
                        })
                    if rows1:
                        try:
                            DB.insert_option_1min_many(rows1)
                            n_1m += len(rows1)
                        except Exception as _de:
                            logger.debug("[LAB] 1m backfill DB insert: " + str(_de))
                except Exception as _e1:
                    logger.debug("[LAB] 1m backfill parse err: " + str(_e1))

        logger.info("[LAB] Startup backfill: " + str(n_3m) + " 3m + "
                    + str(n_1m) + " 1m rows written for ATM=" + str(atm))
    except Exception as e:
        logger.warning("[LAB] Startup backfill top-level error: " + str(e))


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

    # v15.2.5 mid-day restart backfill, gated on empty buffer
    try:
        _startup_backfill(kite)
    except Exception as _be:
        logger.warning("[LAB] Startup backfill skipped on outer error: " + str(_be))

    def _start():
        logger.info("[LAB] Starting — collection loop")
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
                    # Spot 1-min rolling CSV (still consumed by reports tooling)
                    try:
                        collect_spot_1min(_kite_ref)
                    except Exception as e:
                        logger.debug("[LAB] spot 1m: " + str(e))
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

            # ── EOD forward fill — widened from exact 15:35:00-30
            # to 15:35–15:50 window. The old 30-second slot was missed if
            # the loop was slow, restarting, or busy with the 3-min
            # collection tick. Still gated by _fwd_done so it runs AT MOST
            # once per trading day.
            if (now.hour == 15 and 35 <= now.minute <= 50
                    and not _fwd_done):
                _fwd_done = True
                logger.info("[LAB] EOD forward fill starting at "
                            + now.strftime("%H:%M:%S"))
                _n_fwd = 0
                try:
                    fill_forward_columns(_kite_ref, today, "3min")
                    _n_fwd += 1
                    fill_forward_columns(_kite_ref, today, "1min")
                    _n_fwd += 1
                except Exception as e:
                    logger.error("[LAB] Forward fill error: " + str(e))
                logger.info("[LAB] Forward fill complete: "
                            + str(_n_fwd) + "/2 jobs for "
                            + today.isoformat())

        except Exception as e:
            logger.error("[LAB] Loop error: " + str(e))

        time.sleep(1)
