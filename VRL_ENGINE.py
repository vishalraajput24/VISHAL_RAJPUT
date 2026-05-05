# ═══════════════════════════════════════════════════════════════
#  VRL_ENGINE.py — VISHAL RAJPUT TRADE v16.7 (Vishal Clean — V6)
#  Entry: 3 simple gates.
#    1. GREEN candle (option close > open)
#    2. FRESH BREAK — option just crossed its EMA9_low
#         (current close > ema9_low AND ≥ 2 of last 3 prior
#          closes were ≤ ema9_low)
#    3. Spot bias confirms direction
#         CE: spot_close > spot_ema9_low
#         PE: spot_close < spot_ema9_low
#  Exit chain (first match wins):
#    1. EMERGENCY_SL  (single floor: -10 pts, no time-segmenting)
#    2. EOD_EXIT      (15:20)
#    3. VISHAL_TRAIL  (simple 4-tier peak ladder)
#         peak <  8: SL = entry - 10  (INITIAL)
#         peak >= 8: SL = entry        (LOCK_BE — breakeven)
#         peak >= 15: SL = entry + 5  (LOCK_5)
#         peak >= 20: SL = peak - 5   (LOCK_DYN)
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
    # ── V6 entry: 3 simple gates ──
    #   1. GREEN candle (option close > open)
    #   2. FRESH BREAK — option just crossed its EMA9_low
    #        (current close > ema9_low AND ≥ 2 of last 3 prior closes
    #         were ≤ ema9_low — i.e. option was below band recently)
    #   3. Spot bias agrees (CE: spot > spot_ema9_low | PE: spot < spot_ema9_low)
    result = {
        "fired": False, "entry_price": 0, "entry_mode": "", "ema9_high": 0, "ema9_low": 0,
        "close": 0, "open": 0, "high": 0, "low": 0, "candle_green": False, "body_pct": 0,
        "band_width": 0, "reject_reason": "", "band_position": "",
        "ema9_low_slope": 0.0,
        "band_width_slope": 0.0, "margin_above": 0,
        "spot_close": 0.0, "spot_ema9_low": 0.0, "spot_bias": "",
        "fresh_break_count": 0,
    }
    try:
        warmup_until = CFG.entry_ema9_band("warmup_until", "09:35")
        cutoff_after = CFG.entry_ema9_band("cutoff_after", "15:00")

        # V6 needs at least 5 candles (3 priors + 1 fired + 1 in-progress).
        if opt_3m is None or opt_3m.empty or len(opt_3m) < 5:
            result["reject_reason"] = "insufficient_3m_data"
            return result

        last = opt_3m.iloc[-2]
        prev = opt_3m.iloc[-3]
        close = float(last["close"]); open_ = float(last["open"])
        high = float(last["high"]); low = float(last["low"])
        ema9_high = float(last.get("ema9_high", 0))
        ema9_low  = float(last.get("ema9_low", 0))
        ema9_low_slope = round(ema9_low - float(prev.get("ema9_low", 0)), 2)
        band_width = round(ema9_high - ema9_low, 2)

        _band_pos = "ABOVE" if close > ema9_high else ("BELOW" if close < ema9_low else "IN")
        _candle_range = high - low
        _body_pct = round((abs(close - open_) / _candle_range * 100)
                          if _candle_range > 0 else 0, 1)
        _is_green = (close > open_)
        _margin = round(close - ema9_low, 2)

        result.update({
            "entry_price": round(close, 2), "ema9_high": round(ema9_high, 2),
            "ema9_low": round(ema9_low, 2), "close": round(close, 2), "open": round(open_, 2),
            "high": round(high, 2), "low": round(low, 2),
            "band_width": band_width, "ema9_low_slope": ema9_low_slope,
            "candle_green": _is_green, "band_position": _band_pos,
            "body_pct": _body_pct, "margin_above": _margin,
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

        # ── GATE 1: GREEN candle ──
        if not _is_green:
            result["reject_reason"] = "red_candle"
            return result

        # ── GATE 2: FRESH BREAK above EMA9_low ──
        # Current close must be above ema9_low, AND option must have been
        # at-or-below ema9_low in at least 2 of the last 3 prior candles.
        if close <= ema9_low:
            result["reject_reason"] = "close_below_ema9_low"
            return result
        try:
            pre3 = opt_3m.iloc[-5:-2]   # candles -5,-4,-3 (3 priors before fired)
            if len(pre3) < 3:
                result["reject_reason"] = "insufficient_history"
                return result
            below = int((pre3["close"] <= pre3["ema9_low"]).sum())
            result["fresh_break_count"] = below
            if below < 2:
                result["reject_reason"] = f"not_fresh_{below}of3_below"
                return result
        except Exception as _fe:
            result["reject_reason"] = "fresh_break_error_" + str(_fe)[:40]
            return result

        # ── GATE 3: Spot bias confirms direction ──
        try:
            if spot_3m is None or spot_3m.empty or len(spot_3m) < 2:
                result["reject_reason"] = "spot_data_unavailable"
                return result
            _spot_last = spot_3m.iloc[-2]
            _spot_close = float(_spot_last["close"])
            _spot_ema9l = float(_spot_last.get("ema9_low", 0))
            result["spot_close"]    = round(_spot_close, 2)
            result["spot_ema9_low"] = round(_spot_ema9l, 2)
            if _spot_ema9l <= 0:
                result["reject_reason"] = "spot_ema9_low_invalid"
                return result
            if option_type == "CE":
                if _spot_close <= _spot_ema9l:
                    result["spot_bias"] = "BEARISH"
                    result["reject_reason"] = "spot_against_CE"
                    return result
                result["spot_bias"] = "BULLISH"
            else:  # PE
                if _spot_close >= _spot_ema9l:
                    result["spot_bias"] = "BULLISH"
                    result["reject_reason"] = "spot_against_PE"
                    return result
                result["spot_bias"] = "BEARISH"
        except Exception as _se:
            result["reject_reason"] = "spot_gate_error_" + str(_se)[:40]
            return result

        # ── All 3 gates passed ──
        result["fired"] = True
        result["entry_mode"] = "EMA9_BREAKOUT"
        if not silent:
            logger.info(f"[ENGINE] {option_type} FIRED close={round(close,1)} "
                        f"ema9l={round(ema9_low,1)} fresh={result['fresh_break_count']}/3 "
                        f"spot={round(result['spot_close'],1)} "
                        f"vs spot_ema9l={round(result['spot_ema9_low'],1)} "
                        f"(3-gate V6)")
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
    opt_3m = D.get_option_3min(token, lookback=15)
    # Spot 3-min with EMA9 bands for Gate 5 bias confirmation.
    spot_3m = None
    try:
        spot_3m = D.add_indicators(
            D.get_historical_data(D.NIFTY_SPOT_TOKEN, "3minute", 30))
    except Exception as _se:
        logger.warning("[ENGINE] spot 3-min fetch failed: " + str(_se))
    market_open = D.is_market_open()
    now = datetime.now()
    atm_strike = D.resolve_atm_strike(spot_ltp) if spot_ltp else 0
    return _evaluate_entry_gates_pure(
        opt_3m=opt_3m, option_type=option_type, spot_ltp=spot_ltp, now=now,
        market_open=market_open, state=state, atm_strike=atm_strike,
        silent=silent, spot_3m=spot_3m)


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
    # V6 simple 4-tier ladder (no time-segmenting):
    #   peak <  8 → entry - 10 (INITIAL)
    #   peak >= 8 → entry      (LOCK_BE — breakeven)
    #   peak >= 15 → entry + 5 (LOCK_5)
    #   peak >= 20 → peak - 5  (LOCK_DYN)
    if peak_pnl >= 20:
        sl = entry_price + (peak_pnl - 5); tier = "LOCK_DYN"
    elif peak_pnl >= 15:
        sl = entry_price + 5;              tier = "LOCK_5"
    elif peak_pnl >= 8:
        sl = entry_price;                  tier = "LOCK_BE"
    else:
        sl = entry_price - 10;             tier = "INITIAL"
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
    if trail_sl > 0:
        if opt_3m_full is None or len(opt_3m_full) < 2:
            for _ in range(2):
                time.sleep(5)
                opt_3m_full = D.get_option_3min(state.get("token"), lookback=10)
                if opt_3m_full is not None and len(opt_3m_full) >= 2:
                    break
            else:
                return []
        _candle = opt_3m_full.iloc[-2]
        _et_str = state.get("entry_time") or ""
        _use = True
        if _et_str:
            try:
                _h, _m, _s = (int(p) for p in _et_str.split(":"))
                _et_t = _dtime(_h, _m, _s)
                _candle_close_t = (_candle.name + timedelta(minutes=3)).time()
                if _candle_close_t <= _et_t:
                    _use = False
            except Exception:
                pass
        if _use and _candle["close"] <= trail_sl:
            try:
                _trig_t = (_candle.name + timedelta(minutes=3)).strftime("%H:%M")
            except Exception:
                _trig_t = ""
            return [{
                "lot_id": "ALL",
                "reason": "VISHAL_TRAIL",
                "price": trail_sl,
                "trigger_close": round(float(_candle["close"]), 2),
                "trigger_time": _trig_t,
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
