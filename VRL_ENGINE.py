# ═══════════════════════════════════════════════════════════════
#  VRL_ENGINE.py — VISHAL RAJPUT TRADE v16.7 (Vishal Clean — V7)
#  Timeframe: 15-minute option candles (current single-strategy)
#  Entry: 2 gates (option-side only).
#    1. 15-min candle close > EMA9_low (option)
#    2. RSI >= 40 AND rising (RSI[fired] > RSI[prior])
#  Exit chain (TICK-based throughout):
#    1. EMERGENCY_SL: -12 pts (hard, immediate on tick)
#    2. EOD_EXIT: 15:20
#    3. VISHAL_TRAIL (peak ratchet):
#         peak <  12: SL = entry - 12  (INITIAL)
#         peak >= 12: SL = entry        (LOCK_BE)
#         peak >= 24: SL = entry + 12   (LOCK_12)
#         peak >= 30: SL = entry + 20   (LOCK_20)
#         peak >= 36: SL = entry + 24   (LOCK_24)
#         peak >= 40: SL = entry + 36   (LOCK_36)
#         peak >= 48: SL = entry + 36   (12-step continues)
#         peak >= 50: SL = entry + 50   (LOCK_50)
#         peak >= 60+: max(12-step, 50) — keeps ratcheting
#  Cooldown: 0 (removed — fresh entries always available).
# ═══════════════════════════════════════════════════════════════

import logging
import time
from datetime import datetime, timedelta, time as _dtime
import pandas as pd
import VRL_DATA as D
import VRL_CONFIG as CFG

logger = logging.getLogger("vrl_live")


def get_margin_available(kite) -> float:
    try:
        margins = kite.margins(segment="equity")
        return float(margins.get("net", 0))
    except Exception as e:
        logger.error("[TRADE] Margin fetch error: " + str(e))
        return -1.0


def get_option_ema_spread(token: int, dte: int = 99) -> float:
    try:
        df = D.get_historical_data(token, "3minute", D.LOOKBACK_3M)
        df = D.add_indicators(df)
        if df.empty or len(df) < 4:
            return 0.0
        last = df.iloc[-2]
        return round(last.get("EMA_9", last["close"]) - last.get("EMA_21", last["close"]), 2)
    except Exception as e:
        logger.warning("[ENGINE] EMA spread error: " + str(e))
        return 0.0

def pre_entry_checks(kite, token: int, state: dict, option_ltp: float, profile: dict,
                     session: str = "", direction: str = "") -> tuple:
    last_exit = state.get("last_exit_time")
    if last_exit:
        try:
            elapsed = (datetime.now() - datetime.fromisoformat(last_exit)).total_seconds() / 60
            cd_min = CFG.entry_ema9_band("cooldown_minutes", 5)
            # Both-sides cooldown: block CE and PE for cd_min after any exit.
            if elapsed < cd_min:
                return False, "Cooldown: " + str(round(cd_min - elapsed, 1)) + "min"
        except:
            pass
    if state.get("in_trade"):                return False, "Already in trade"
    if not D.is_market_open():               return False, "Market closed"
    if not D.is_tick_live(D.NIFTY_SPOT_TOKEN): return False, "Spot tick stale"
    if option_ltp <= 0:                      return False, "Option LTP zero"
    if state.get("paused"):                  return False, "Bot paused"
    if not D.PAPER_MODE and kite is not None:
        try:
            avail = get_margin_available(kite)
            if avail < option_ltp * D.get_lot_size() * 1.2:
                return False, "Insufficient margin"
        except Exception:
            return False, "Margin check failed"
    return True, ""

def _evaluate_entry_gates_pure(opt_3m, option_type: str, spot_ltp: float, now,
                               market_open: bool, state: dict,
                               atm_strike: int, silent: bool = False,
                               spot_3m=None) -> dict:
    # ── V7 entry: 2 gates (option-side only, 15-min candles) ──
    #   1. 15-min close > EMA9_low
    #   2. RSI >= 40 AND rising (RSI[fired] > RSI[prior])
    # NOTE: param name `opt_3m` retained for back-compat — V7 callers
    # pass 15-min candles in the same DataFrame format (timeframe-agnostic).
    result = {
        "fired": False, "entry_price": 0, "entry_mode": "", "ema9_high": 0, "ema9_low": 0,
        "close": 0, "open": 0, "high": 0, "low": 0, "candle_green": False, "body_pct": 0,
        "band_width": 0, "reject_reason": "", "band_position": "",
        "ema9_low_slope": 0.0,
        "band_width_slope": 0.0, "margin_above": 0,
        "spot_close": 0.0, "spot_ema9_low": 0.0, "spot_bias": "",
        "rsi": 0.0, "rsi_prev": 0.0, "rsi_rising": False,
    }
    try:
        warmup_until = CFG.entry_ema9_band("warmup_until", "09:35")
        cutoff_after = CFG.entry_ema9_band("cutoff_after", "15:00")

        # V7 needs at least 16 candles for RSI(14) to converge + prior + live.
        if opt_3m is None or opt_3m.empty or len(opt_3m) < 16:
            result["reject_reason"] = "insufficient_15m_data"
            return result

        last = opt_3m.iloc[-2]   # last CLOSED 15-min candle (fired)
        prev = opt_3m.iloc[-3]   # candle before fired

        # ── CRITICAL: Same-candle guard ──
        # Prevent re-firing on the same closed 15-min candle if we already
        # fired (or attempted to fire) on it. Without this, with cooldown=0
        # and a multi-minute scan loop, the same closed candle can trigger
        # 5-7 entries before the next candle closes — exactly what blew up
        # 2026-05-07 09:49-09:58 (-287 pts on 10 same-candle re-fires).
        try:
            fired_ts = str(last.name)
            result["fired_candle_ts"] = fired_ts
            last_fired_ts = state.get("_last_fired_candle_ts", "") if state else ""
            if last_fired_ts and last_fired_ts == fired_ts:
                result["reject_reason"] = "same_candle_already_fired"
                if not silent:
                    logger.info(f"[REJECT] {option_type} same_candle_guard "
                                f"already_fired_on={fired_ts}")
                return result
        except Exception as _ge:
            logger.warning("[ENGINE] same-candle guard error: " + str(_ge))

        close = float(last["close"]); open_ = float(last["open"])
        high = float(last["high"]); low = float(last["low"])
        ema9_high = float(last.get("ema9_high", 0))
        ema9_low  = float(last.get("ema9_low", 0))
        rsi_now   = float(last.get("RSI", 0))
        rsi_prev  = float(prev.get("RSI", 0))
        ema9_low_slope = round(ema9_low - float(prev.get("ema9_low", 0)), 2)
        band_width = round(ema9_high - ema9_low, 2)

        _band_pos = "ABOVE" if close > ema9_high else ("BELOW" if close < ema9_low else "IN")
        _candle_range = high - low
        _body_pct = round((abs(close - open_) / _candle_range * 100)
                          if _candle_range > 0 else 0, 1)
        _is_green = (close > open_)
        _margin = round(close - ema9_low, 2)
        _rsi_rising = (rsi_now > rsi_prev)

        result.update({
            "entry_price": round(close, 2), "ema9_high": round(ema9_high, 2),
            "ema9_low": round(ema9_low, 2), "close": round(close, 2), "open": round(open_, 2),
            "high": round(high, 2), "low": round(low, 2),
            "band_width": band_width, "ema9_low_slope": ema9_low_slope,
            "candle_green": _is_green, "band_position": _band_pos,
            "body_pct": _body_pct, "margin_above": _margin,
            "rsi": round(rsi_now, 1), "rsi_prev": round(rsi_prev, 1),
            "rsi_rising": _rsi_rising,
        })

        # ── Operational rail: time window ──
        if market_open:
            mins = now.hour * 60 + now.minute
            warmup_mins = int(warmup_until.split(":")[0])*60 + int(warmup_until.split(":")[1])
            cutoff_mins = int(cutoff_after.split(":")[0])*60 + int(cutoff_after.split(":")[1])
            if mins < warmup_mins:
                result["reject_reason"] = "before_" + warmup_until
                return result
            if mins >= cutoff_mins:
                result["reject_reason"] = "after_" + cutoff_after
                return result

        # ── GATE 1: 15-min candle close > EMA9_low ──
        if close <= ema9_low:
            result["reject_reason"] = "close_below_ema9_low"
            if not silent:
                logger.info(f"[REJECT] {option_type} gate1_close_below_band "
                            f"close={round(close,1)} ema9l={round(ema9_low,1)}")
            return result

        # ── GATE 2: RSI >= 40 AND rising ──
        if rsi_now < 40:
            result["reject_reason"] = f"rsi_below_40_{round(rsi_now,1)}"
            if not silent:
                logger.info(f"[REJECT] {option_type} gate2_rsi_below_40 "
                            f"rsi={round(rsi_now,1)}")
            return result
        if not _rsi_rising:
            result["reject_reason"] = f"rsi_not_rising_{round(rsi_now,1)}_vs_{round(rsi_prev,1)}"
            if not silent:
                logger.info(f"[REJECT] {option_type} gate2_rsi_not_rising "
                            f"rsi_now={round(rsi_now,1)} rsi_prev={round(rsi_prev,1)}")
            return result

        # ── Spot bias (DISPLAY ONLY — no longer a gate) ──
        try:
            if spot_3m is not None and not spot_3m.empty and len(spot_3m) >= 2:
                _spot_last = spot_3m.iloc[-2]
                _spot_close = float(_spot_last["close"])
                _spot_ema9l = float(_spot_last.get("ema9_low", 0))
                result["spot_close"]    = round(_spot_close, 2)
                result["spot_ema9_low"] = round(_spot_ema9l, 2)
                if _spot_ema9l > 0:
                    if option_type == "CE":
                        result["spot_bias"] = "BULLISH" if _spot_close > _spot_ema9l else "BEARISH"
                    else:
                        result["spot_bias"] = "BEARISH" if _spot_close < _spot_ema9l else "BULLISH"
        except Exception:
            pass

        # ── All 2 gates passed ──
        result["fired"] = True
        result["entry_mode"] = "EMA9_BREAKOUT"
        if not silent:
            logger.info(f"[ENGINE] {option_type} FIRED close={round(close,1)} "
                        f"ema9l={round(ema9_low,1)} "
                        f"rsi={round(rsi_now,1)} (prev={round(rsi_prev,1)}, rising) "
                        f"spot_bias={result.get('spot_bias','?')} "
                        f"(2-gate V7, 15-min)")
        return result

    except Exception as e:
        logger.error("[ENGINE] Entry error: " + str(e))
        result["fired"] = False
        result["reject_reason"] = "error_" + str(e)[:50]
        return result

def check_entry(token: int, option_type: str, spot_ltp: float = 0, dte: int = 99,
                expiry_date=None, kite=None, other_token: int = 0, silent: bool = False,
                state: dict = None) -> dict:
    if state is None: state = {}
    # V7: 15-minute option candles (timeframe-agnostic — keeps same DataFrame
    # schema with EMA_9/EMA_21/RSI/ema9_high/ema9_low via add_indicators).
    opt_15m = None
    try:
        opt_15m = D.add_indicators(
            D.get_historical_data(token, "15minute", 30))
    except Exception as _oe:
        logger.warning("[ENGINE] option 15-min fetch failed: " + str(_oe))
    # Spot 15-min for display-only bias on the alert.
    spot_3m = None
    try:
        spot_3m = D.add_indicators(
            D.get_historical_data(D.NIFTY_SPOT_TOKEN, "15minute", 30))
    except Exception as _se:
        logger.warning("[ENGINE] spot 15-min fetch failed: " + str(_se))
    market_open = D.is_market_open()
    now = datetime.now()
    atm_strike = D.resolve_atm_strike(spot_ltp) if spot_ltp else 0
    return _evaluate_entry_gates_pure(
        opt_3m=opt_15m, option_type=option_type, spot_ltp=spot_ltp, now=now,
        market_open=market_open, state=state, atm_strike=atm_strike,
        silent=silent, spot_3m=spot_3m)


def check_entry_v8(token: int, option_type: str, spot_ltp: float = 0,
                   silent: bool = False, state: dict = None,
                   other_token: int = 0) -> dict:
    """V8 — 3-min fresh-break entry (parallel strategy).

    Gates:
      1. GREEN candle (option close > open)
      2. FRESH BREAK (≥ 2 of last 3 prior closes ≤ ema9_low)
      3. (optional re-entry path) cross-leg confirmation handled in
         MAIN's re-entry watcher, not here.
    Same-candle guard prevents same-candle re-fires (state-driven).
    """
    if state is None:
        state = {}
    result = {
        "fired": False, "strategy": "V8", "entry_price": 0, "entry_mode": "",
        "ema9_high": 0, "ema9_low": 0, "close": 0, "open": 0,
        "high": 0, "low": 0, "candle_green": False, "body_pct": 0,
        "band_width": 0, "reject_reason": "", "fired_candle_ts": "",
        "fresh_break_count": 0,
        # cross-leg attached when re-entry watcher fires
        "xleg_other_close": 0.0, "xleg_other_ema9l": 0.0, "xleg_other_dying": False,
    }
    try:
        opt_3m = D.add_indicators(D.get_historical_data(token, "3minute", 20))
        if opt_3m is None or opt_3m.empty or len(opt_3m) < 5:
            result["reject_reason"] = "insufficient_3m_data"
            return result

        last = opt_3m.iloc[-2]
        # ── Same-candle guard ──
        fired_ts = str(last.name)
        result["fired_candle_ts"] = fired_ts
        if state.get("_last_fired_candle_ts", "") == fired_ts:
            result["reject_reason"] = "same_candle_already_fired"
            if not silent:
                logger.info(f"[REJECT-V8] {option_type} same_candle_guard ts={fired_ts}")
            return result

        # ── EMERGENCY_SL 1-candle cooldown ──
        if state.get("_sl_cooldown_skip_next"):
            state["_sl_cooldown_skip_next"] = False
            result["reject_reason"] = "sl_cooldown_skip"
            if not silent:
                logger.info(f"[REJECT-V8] {option_type} sl_cooldown_skip — "
                            f"skipping first candle after EMERGENCY_SL")
            return result

        close = float(last["close"]); open_ = float(last["open"])
        high = float(last["high"]); low = float(last["low"])
        ema9_high = float(last.get("ema9_high", 0))
        ema9_low  = float(last.get("ema9_low", 0))
        _candle_range = high - low
        _body_pct = round((abs(close - open_) / _candle_range * 100)
                          if _candle_range > 0 else 0, 1)
        _is_green = (close > open_)
        result.update({
            "entry_price": round(close, 2),
            "ema9_high": round(ema9_high, 2),
            "ema9_low": round(ema9_low, 2),
            "close": round(close, 2), "open": round(open_, 2),
            "high": round(high, 2), "low": round(low, 2),
            "band_width": round(ema9_high - ema9_low, 2),
            "candle_green": _is_green, "body_pct": _body_pct,
        })

        # ── Time window (rail, same as V7) ──
        now = datetime.now()
        warmup_until = CFG.entry_ema9_band("warmup_until", "09:35")
        cutoff_after = CFG.entry_ema9_band("cutoff_after", "15:00")
        if D.is_market_open():
            mins = now.hour * 60 + now.minute
            wm = int(warmup_until.split(":")[0])*60 + int(warmup_until.split(":")[1])
            cm = int(cutoff_after.split(":")[0])*60 + int(cutoff_after.split(":")[1])
            if mins < wm:
                result["reject_reason"] = "before_" + warmup_until
                return result
            if mins >= cm:
                result["reject_reason"] = "after_" + cutoff_after
                return result

        # ── GATE 1: GREEN candle ──
        if not _is_green:
            result["reject_reason"] = "red_candle"
            if not silent:
                logger.info(f"[REJECT-V8] {option_type} gate1_red_candle "
                            f"close={round(close,1)} open={round(open_,1)}")
            return result

        # ── GATE 2: close must be above EMA9 band ──
        if close <= ema9_low:
            result["reject_reason"] = "close_below_ema9_low"
            return result

        # ── GATE 3: RSI momentum (3-min) ──
        # 3A: RSI >= 38 (minimum momentum floor, allows early recovery)
        # 3B: RSI not breaking down — drop vs prior candle must be < 2 pts
        #     (filters genuine fading; ignores 3-min tick noise of 0-1.9 pts)
        _rsi_now  = float(last.get("RSI", 0) or 0)
        _rsi_prev = float(opt_3m.iloc[-3].get("RSI", 0) or 0)
        _rsi_drop = round(_rsi_prev - _rsi_now, 2)
        result["rsi"] = round(_rsi_now, 1)
        result["rsi_prev"] = round(_rsi_prev, 1)
        if _rsi_now < 38:
            result["reject_reason"] = f"rsi_below_38_{round(_rsi_now, 1)}"
            if not silent:
                logger.info(f"[REJECT-V8] {option_type} gate3a_rsi_below_38 "
                            f"rsi={round(_rsi_now,1)}")
            return result
        if _rsi_drop >= 2:
            result["reject_reason"] = f"rsi_breaking_down_{round(_rsi_now,1)}_drop_{round(_rsi_drop,1)}"
            if not silent:
                logger.info(f"[REJECT-V8] {option_type} gate3b_rsi_breakdown "
                            f"rsi={round(_rsi_now,1)} prev={round(_rsi_prev,1)} "
                            f"drop={round(_rsi_drop,1)}")
            return result

        # ── Cross-leg snapshot (informational) ──
        if other_token:
            try:
                opt3m_other = D.add_indicators(
                    D.get_historical_data(other_token, "3minute", 10))
                if opt3m_other is not None and len(opt3m_other) >= 2:
                    o_last = opt3m_other.iloc[-2]
                    o_close = float(o_last["close"])
                    o_ema9l = float(o_last.get("ema9_low", 0))
                    result["xleg_other_close"] = round(o_close, 2)
                    result["xleg_other_ema9l"] = round(o_ema9l, 2)
                    result["xleg_other_dying"] = (o_ema9l > 0 and o_close < o_ema9l)
            except Exception:
                pass

        result["fired"] = True
        result["entry_mode"] = "CLOSE_FILL"
        if not silent:
            logger.info(f"[ENGINE-V8] {option_type} FIRED close={round(close,1)} "
                        f"ema9l={round(ema9_low,1)} "
                        f"rsi={round(_rsi_now,1)} (prev={round(_rsi_prev,1)}) "
                        f"(3-gate V8, 3-min)")
        return result

    except Exception as e:
        logger.error("[ENGINE-V8] Entry error: " + str(e))
        result["fired"] = False
        result["reject_reason"] = "error_" + str(e)[:50]
        return result


def check_v8_continuation_reentry(token: int, option_type: str,
                                   other_token: int, state: dict = None) -> dict:
    """V8 cross-leg continuation re-entry path.
    Called only when V8 just exited and re-entry watcher is armed.

    Conditions:
      - Same-side option: GREEN + close > ema9_low
      - Other-side option: RED + close < ema9_low (cross-leg dying)

    Returns dict with fired=True if all conditions met, else with reject_reason.
    """
    if state is None: state = {}
    result = {
        "fired": False, "strategy": "V8", "entry_mode": "REENTRY_XLEG",
        "reject_reason": "", "entry_price": 0,
        "fired_candle_ts": "", "ema9_low": 0, "close": 0, "open": 0,
    }
    try:
        opt_3m = D.add_indicators(D.get_historical_data(token, "3minute", 10))
        if opt_3m is None or opt_3m.empty or len(opt_3m) < 2:
            result["reject_reason"] = "insufficient_3m_data"; return result
        last = opt_3m.iloc[-2]
        fired_ts = str(last.name)
        result["fired_candle_ts"] = fired_ts
        # Same-candle guard
        if state.get("_last_fired_candle_ts", "") == fired_ts:
            result["reject_reason"] = "same_candle_already_fired"; return result

        close = float(last["close"]); open_ = float(last["open"])
        ema9_low = float(last.get("ema9_low", 0))
        result["entry_price"] = round(close, 2)
        result["close"] = round(close, 2); result["open"] = round(open_, 2)
        result["ema9_low"] = round(ema9_low, 2)

        if close <= open_:
            result["reject_reason"] = "self_red_candle"; return result
        if close <= ema9_low:
            result["reject_reason"] = "self_close_below_ema9l"; return result

        # Cross-leg confirm: other side RED + close < ema9_low
        opt3_other = D.add_indicators(D.get_historical_data(other_token, "3minute", 10))
        if opt3_other is None or opt3_other.empty or len(opt3_other) < 2:
            result["reject_reason"] = "xleg_no_data"; return result
        o_last = opt3_other.iloc[-2]
        o_open = float(o_last["open"]); o_close = float(o_last["close"])
        o_ema9l = float(o_last.get("ema9_low", 0))
        result["xleg_other_close"] = round(o_close, 2)
        result["xleg_other_ema9l"] = round(o_ema9l, 2)
        if o_close >= o_open:
            result["reject_reason"] = "xleg_not_red"; return result
        if o_ema9l > 0 and o_close >= o_ema9l:
            result["reject_reason"] = "xleg_not_below_band"; return result
        result["xleg_other_dying"] = True

        result["fired"] = True
        logger.info(f"[ENGINE-V8] {option_type} REENTRY-XLEG FIRED "
                    f"self_close={round(close,1)} other_close={round(o_close,1)} "
                    f"other_ema9l={round(o_ema9l,1)} (cross-leg confirmed)")
        return result
    except Exception as e:
        logger.error("[ENGINE-V8] Re-entry error: " + str(e))
        result["reject_reason"] = "error_" + str(e)[:50]
        return result


def evaluate_cross_leg(self_dir: str, opt_3m_other) -> dict:
    out = {
        "xleg_signal":       "NA",
        "xleg_other_close":  0.0,
        "xleg_other_ema9l":  0.0,
        "xleg_other_dying":  False,
        "xleg_other_margin": 0.0,
    }
    try:
        if opt_3m_other is None or opt_3m_other.empty or len(opt_3m_other) < 2:
            return out
        last = opt_3m_other.iloc[-2]
        other_close = float(last["close"])
        other_ema9l = float(last.get("ema9_low", 0))
        if other_ema9l <= 0:
            return out
        other_dying = other_close < other_ema9l
        out["xleg_other_close"]  = round(other_close, 2)
        out["xleg_other_ema9l"]  = round(other_ema9l, 2)
        out["xleg_other_dying"]  = bool(other_dying)
        out["xleg_other_margin"] = round(other_close - other_ema9l, 2)
        out["xleg_signal"]       = "PASS" if other_dying else "FAIL"
    except Exception as e:
        logger.debug("[XLEG] eval err: " + str(e))
    return out


def compute_entry_sl(entry_price: float, hard_sl: int = 10) -> float:
    return round(entry_price - hard_sl, 2)

def compute_trail_sl(entry_price: float, peak_pnl: float,
                     direction: str = "", now=None) -> tuple:
    # V7 ladder — discrete 12-step + specific tiers at 30/40/50.
    # All TICK-based. Hard SL at -12.
    if peak_pnl < 12:
        sl = entry_price - 12
        return round(sl, 2), "INITIAL"
    # 12-step base ladder: peak 12→0, 24→12, 36→24, 48→36, 60→48, ...
    base_lock = (int(peak_pnl // 12) - 1) * 12
    # User-specific overrides at 30, 40, 50
    if peak_pnl >= 50:
        spec_lock = 50
    elif peak_pnl >= 40:
        spec_lock = 36
    elif peak_pnl >= 30:
        spec_lock = 20
    else:
        spec_lock = 0
    lock = max(base_lock, spec_lock)
    sl = entry_price + lock
    if lock == 0:
        tier = "LOCK_BE"
    else:
        tier = f"LOCK_{lock}"
    return round(sl, 2), tier

def _evaluate_exit_chain_pure(state: dict, option_ltp: float, opt_3m_full, now, market_open: bool) -> list:
    if not state.get("in_trade"): return []
    entry = state.get("entry_price", 0)
    pnl = round(option_ltp - entry, 2)
    peak = max(state.get("peak_pnl", 0), pnl)
    state["peak_pnl"] = peak
    # ── V6 single-floor emergency SL: -10 pts ──
    _emergency_sl = CFG.exit_ema9_band("emergency_sl_pts", -10)
    if pnl <= _emergency_sl:
        return [{"lot_id": "ALL", "reason": "EMERGENCY_SL", "price": option_ltp}]
    if market_open:
        _eod_str = CFG.exit_ema9_band("eod_exit_time", "15:20")
        try:
            _eh, _em = _eod_str.split(":")
            eod_mins = int(_eh) * 60 + int(_em)
        except Exception:
            eod_mins = 15 * 60 + 20
        if now.hour*60 + now.minute >= eod_mins:
            return [{"lot_id": "ALL", "reason": "EOD_EXIT", "price": option_ltp}]

    trail_sl, trail_tier = compute_trail_sl(entry, peak, now=now)
    state["active_ratchet_tier"] = trail_tier
    state["active_ratchet_sl"] = trail_sl

    # ── V6.1+ TICK-BASED trail for LOCKED tiers (peak ≥ 8) ──
    # When option_ltp drops to/below the locked SL → exit immediately
    # at the SL price. INITIAL tier (peak < 8) is covered by the
    # emergency SL check above (entry-10 = same threshold), so no
    # separate close-based trail check is needed for it.
    if trail_tier != "INITIAL" and trail_sl > 0:
        if option_ltp <= trail_sl:
            return [{
                "lot_id": "ALL",
                "reason": "VISHAL_TRAIL",
                "price": trail_sl,
                "trigger_close": round(float(option_ltp), 2),
                "trigger_time": now.strftime("%H:%M:%S"),
                "trigger_sl": round(trail_sl, 2),
            }]

    return []

def manage_exit(state: dict, option_ltp: float, profile: dict, other_token: int = 0) -> list:
    if not state.get("in_trade"): return []
    opt_3m_full = None
    try:
        opt_3m_full = D.get_option_3min(state.get("token"), lookback=10)
    except Exception as _e:
        logger.warning("[ENGINE] manage_exit get_option_3min failed: " + str(_e))
    return _evaluate_exit_chain_pure(state, option_ltp, opt_3m_full, datetime.now(), D.is_market_open())


# ═══════════════════════════════════════════════════════════════
# === CHARGES (merged from VRL_CHARGES) ===
# ═══════════════════════════════════════════════════════════════

BROKERAGE_PER_ORDER = 20.0
STT_SELL_PCT = 0.000625
EXCHANGE_NSE_PCT = 0.000530
SEBI_TURNOVER_PCT = 0.000001
STAMP_DUTY_BUY_PCT = 0.00003
GST_PCT = 0.18

def _live_lot_size() -> int:
    try:
        lot = int(getattr(D, "LOT_SIZE", 0) or 0)
        if lot > 0:
            return lot
    except Exception:
        pass
    return 65

def calculate_charges(entry_price: float, exit_price: float,
                      qty: int, num_exit_orders: int = 1) -> dict:
    buy_turnover = entry_price * qty
    sell_turnover = exit_price * qty
    total_turnover = buy_turnover + sell_turnover
    gross_pnl = round((exit_price - entry_price) * qty, 2)
    gross_pts = round(exit_price - entry_price, 2)
    num_orders = 1 + num_exit_orders
    brokerage = round(BROKERAGE_PER_ORDER * num_orders, 2)
    stt = round(sell_turnover * STT_SELL_PCT, 2)
    exchange = round(total_turnover * EXCHANGE_NSE_PCT, 2)
    sebi = round(total_turnover * SEBI_TURNOVER_PCT, 2)
    stamp = round(buy_turnover * STAMP_DUTY_BUY_PCT, 2)
    gst = round((brokerage + exchange) * GST_PCT, 2)
    total_charges = round(brokerage + stt + exchange + sebi + stamp + gst, 2)
    net_pnl = round(gross_pnl - total_charges, 2)
    charges_pts = round(total_charges / qty, 2) if qty > 0 else 0
    net_pts = round(gross_pts - charges_pts, 2)
    return {
        "gross_pnl": gross_pnl, "gross_pts": gross_pts,
        "brokerage": brokerage, "stt": stt, "exchange": exchange,
        "sebi": sebi, "stamp": stamp, "gst": gst,
        "total_charges": total_charges, "charges_pts": charges_pts,
        "net_pnl": net_pnl, "net_pts": net_pts,
        "turnover": total_turnover, "num_orders": num_orders,
    }

def calculate_lot_charges(entry_price: float, exit_price: float,
                          lot_size: int = None) -> dict:
    if lot_size is None:
        lot_size = _live_lot_size()
    return calculate_charges(entry_price, exit_price, lot_size, num_exit_orders=1)
