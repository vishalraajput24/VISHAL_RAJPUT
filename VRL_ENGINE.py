# ═══════════════════════════════════════════════════════════════
#  VRL_ENGINE.py — VISHAL RAJPUT TRADE v16.4
#  EMA9 Band Breakout + Balanced Trail (60%→70%→80%)
#  Simplified Entry: Time, Close>EMA9L, Green, Body≥30%
# ═══════════════════════════════════════════════════════════════

import logging
from datetime import datetime
import pandas as pd
import VRL_DATA as D
import VRL_CONFIG as CFG

logger = logging.getLogger("vrl_live")


# ═══════════════════════════════════════════════════════════════
#  DASHBOARD UTILITY (kept for /spot back-compat)
# ═══════════════════════════════════════════════════════════════

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


# ═══════════════════════════════════════════════════════════════
#  PRE-ENTRY GUARDS
# ═══════════════════════════════════════════════════════════════

def pre_entry_checks(kite, token: int, state: dict,
                     option_ltp: float, profile: dict,
                     session: str = "", direction: str = "") -> tuple:
    if state.get("daily_trades", 0) >= D.MAX_DAILY_TRADES:
        return False, "MAX_DAILY_TRADES reached"
    if state.get("daily_losses", 0) >= D.MAX_DAILY_LOSSES:
        return False, "MAX_DAILY_LOSSES reached"

    last_exit = state.get("last_exit_time")
    if last_exit:
        try:
            elapsed = (datetime.now() - datetime.fromisoformat(last_exit)).total_seconds() / 60
            last_dir = state.get("last_exit_direction", "")
            cd_min = CFG.entry_ema9_band("cooldown_minutes", 5)
            if direction and last_dir and direction == last_dir:
                if elapsed < cd_min:
                    remaining = round(cd_min - elapsed, 1)
                    return False, "Cooldown: " + str(remaining) + "min (same dir)"
        except Exception:
            pass

    if state.get("in_trade"):                return False, "Already in trade"
    if not D.is_market_open():               return False, "Market closed"
    if not D.is_tick_live(D.NIFTY_SPOT_TOKEN): return False, "Spot tick stale"
    if option_ltp <= 0:                      return False, "Option LTP zero"
    if state.get("paused"):                  return False, "Bot paused"
    if not D.PAPER_MODE and kite is not None:
        try:
            from VRL_TRADE import get_margin_available
            avail = get_margin_available(kite)
            if avail > 0 and avail < option_ltp * D.get_lot_size() * 1.2:
                return False, "Insufficient margin"
        except Exception:
            pass
    return True, ""


def loss_streak_gate(state: dict) -> bool:
    return True


# ═══════════════════════════════════════════════════════════════
#  PURE ENTRY GATE EVALUATOR
# ═══════════════════════════════════════════════════════════════

def _evaluate_entry_gates_pure(opt_3m, option_type: str, spot_ltp: float,
                                now, market_open: bool, state: dict,
                                straddle_delta, spot_vwap, spot_for_vwap: float,
                                atm_strike: int, silent: bool = False,
                                other_opt_3m=None) -> dict:
    result = {
        "fired": False, "option_type": option_type,
        "entry_price": 0, "entry_mode": "",
        "ema9_high": 0, "ema9_low": 0,
        "close": 0, "open": 0, "high": 0, "low": 0,
        "prev_close": 0, "prev_ema9_high": 0,
        "candle_green": False, "body_pct": 0,
        "band_width": 0,
        "cooldown_ok": False, "reject_reason": "",
        "band_position": "",
        "straddle_delta": None, "straddle_threshold": 0,
        "straddle_period": "", "atm_strike_used": 0,
        "spot_vwap": 0.0, "spot_vs_vwap": 0.0, "vwap_bonus": "",
        "ema9_high_slope_5c": 0.0, "ema9_low_slope_5c": 0.0,
        "bands_state": "", "context_tag": "",
        "backbone_status": "N/A",
    }
    if state is None:
        state = {}
    try:
        body_min       = CFG.entry_ema9_band("body_pct_min", 30)
        warmup_until   = CFG.entry_ema9_band("warmup_until", "09:30")
        cutoff_after   = CFG.entry_ema9_band("cutoff_after", "15:10")

        if opt_3m is None or opt_3m.empty or len(opt_3m) < 4:
            result["reject_reason"] = "insufficient_3m_data"
            return result

        last = opt_3m.iloc[-2]
        prev = opt_3m.iloc[-3]

        close = float(last["close"])
        open_ = float(last["open"])
        high  = float(last["high"])
        low   = float(last["low"])
        ema9_high = float(last.get("ema9_high", 0))
        ema9_low  = float(last.get("ema9_low", 0))
        prev_close = float(prev["close"])
        prev_ema9_high = float(prev.get("ema9_high", 0))

        if close > ema9_high:
            band_position = "ABOVE"
        elif close < ema9_low:
            band_position = "BELOW"
        else:
            band_position = "IN"

        band_width = round(ema9_high - ema9_low, 2)

        result.update({
            "entry_price": round(close, 2),
            "ema9_high": round(ema9_high, 2),
            "ema9_low": round(ema9_low, 2),
            "close": round(close, 2),
            "open": round(open_, 2),
            "high": round(high, 2),
            "low": round(low, 2),
            "prev_close": round(prev_close, 2),
            "prev_ema9_high": round(prev_ema9_high, 2),
            "band_position": band_position,
            "band_width": band_width,
        })

        # ── GATE 1: Time window ──
        if market_open:
            mins = now.hour * 60 + now.minute
            warmup_h, warmup_m = warmup_until.split(":")
            cutoff_h, cutoff_m = cutoff_after.split(":")
            warmup_mins = int(warmup_h) * 60 + int(warmup_m)
            cutoff_mins = int(cutoff_h) * 60 + int(cutoff_m)
            if mins < warmup_mins:
                result["reject_reason"] = "before_" + warmup_until + "_warmup"
                return result
            if mins >= cutoff_mins:
                result["reject_reason"] = "after_" + cutoff_after + "_cutoff"
                return result

        result["cooldown_ok"] = True

        # ── GATE 2: Fresh breakout (DISABLED – always pass) ──
        is_fresh_breakout = True

        # ── GATE 3: Green candle ──
        candle_green = close > open_
        result["candle_green"] = candle_green
        if not candle_green:
            result["reject_reason"] = "red_candle_close=" + str(round(close, 1)) + "_open=" + str(round(open_, 1))
            if not silent:
                logger.info("[ENGINE] " + option_type + " RED_CANDLE")
            return result

        # ── GATE 4: Body ≥ 30% ──
        candle_range = high - low
        body = abs(close - open_)
        body_pct = round((body / candle_range * 100) if candle_range > 0 else 0, 1)
        result["body_pct"] = body_pct
        if body_pct < body_min:
            result["reject_reason"] = "weak_body_" + str(int(body_pct)) + "pct_<_" + str(body_min)
            if not silent:
                logger.info("[ENGINE] " + option_type + " WEAK_BODY")
            return result

        # ── GATE 5 (REMOVED) Floor test – disabled ──
        # ── GATE 6 (REMOVED) Close near high – disabled ──
        # ── GATE 7 (REMOVED) Anti‑chop – disabled ──

        # ── BACKBONE check (display only) ──
        result["backbone_status"] = "N/A"
        if other_opt_3m is not None and len(other_opt_3m) >= 3:
            try:
                _ob = other_opt_3m.iloc[-2]
                _o_close = float(_ob.get("close", 0))
                _o_open  = float(_ob.get("open", 0))
                _o_ema9h = float(_ob.get("ema9_high", 0))
                _o_red = _o_close < _o_open
                _o_below = _o_close < _o_ema9h
                _backbone_ok = _o_red and _o_below
                other_side = "PE" if option_type == "CE" else "CE"
                result["backbone_other_close"] = round(_o_close, 2)
                result["backbone_other_ema9h"] = round(_o_ema9h, 2)
                result["backbone_other_red"]   = bool(_o_red)
                if _backbone_ok:
                    result["backbone_status"] = "CONFIRMED"
            except Exception as _be:
                logger.debug("[ENGINE] backbone check error: " + str(_be))

        # ── Straddle DISPLAY ONLY ──
        if CFG.straddle_filter("enabled", True):
            result["atm_strike_used"] = atm_strike
            mod = now.hour * 60 + now.minute
            if 585 <= mod < 630:
                period = "OPENING"
            elif 630 <= mod < 840:
                period = "MIDDAY"
            else:
                period = "CLOSING"
            result["straddle_period"]    = period
            result["straddle_threshold"] = 0

            sd = straddle_delta
            result["straddle_delta"]     = sd if sd is not None else 0
            result["straddle_available"] = sd is not None

            if sd is None:
                result["straddle_info"] = "NA"
            elif sd >= 5:
                result["straddle_info"] = "STRONG"
            elif sd >= 0:
                result["straddle_info"] = "NEUTRAL"
            else:
                result["straddle_info"] = "WEAK"

        # ── VWAP confluence (display only) ──
        try:
            if CFG.vwap_bonus("enabled", True):
                vwap_val = spot_vwap
                _spot = spot_for_vwap or spot_ltp
                if vwap_val and _spot:
                    diff = round(_spot - vwap_val, 2)
                    result["spot_vwap"]    = round(vwap_val, 1)
                    result["spot_vs_vwap"] = round(diff, 1)
                    if option_type == "CE":
                        result["vwap_bonus"] = "CONFLUENCE" if diff > 0 else "AGAINST"
                    else:
                        result["vwap_bonus"] = "CONFLUENCE" if diff < 0 else "AGAINST"
        except Exception as e:
            logger.debug("[ENGINE] vwap bonus error: " + str(e))

        # ═══ ALL HARD GATES PASSED – FIRE ═══
        result["fired"] = True
        result["entry_mode"] = "EMA9_BREAKOUT"
        result["ema9h_confirmed"] = bool(close > ema9_high)
        if not silent:
            logger.info("[ENGINE] " + option_type + " ENTRY [EMA9_BREAKOUT]"
                        + " close=" + str(round(close, 1))
                        + " body=" + str(int(body_pct)) + "%")
        return result

    except Exception as e:
        logger.error("[ENGINE] _evaluate_entry_gates_pure error: " + str(e))
        result["reject_reason"] = "error_" + str(e)[:50]
        return result


def check_entry(token: int, option_type: str, spot_ltp: float = 0,
                dte: int = 99, expiry_date=None, kite=None,
                other_token: int = 0, silent: bool = False,
                state: dict = None) -> dict:
    if state is None:
        state = {}
    opt_3m = D.get_option_3min(token, lookback=15)
    market_open = D.is_market_open()
    now = datetime.now()
    atm_strike = D.resolve_atm_strike(spot_ltp) if spot_ltp else 0

    straddle_delta = None
    if CFG.straddle_filter("enabled", True) and atm_strike:
        try:
            lookback_min = int(CFG.straddle_filter("lookback_minutes", 15))
            straddle_delta = D.get_straddle_delta(atm_strike, lookback_minutes=lookback_min)
        except Exception as e:
            logger.warning("[ENGINE] straddle delta error: " + str(e))

    spot_vwap = None
    spot_for_vwap = 0.0
    if CFG.vwap_bonus("enabled", True):
        try:
            spot_vwap = D.get_spot_vwap()
            spot_for_vwap = D.get_spot_ltp() or spot_ltp
        except Exception as e:
            logger.debug("[ENGINE] vwap fetch: " + str(e))

    other_opt_3m = None
    try:
        if option_type == "CE":
            _other_tok = state.get("locked_pe_token") or other_token
        else:
            _other_tok = state.get("locked_ce_token") or other_token
        if _other_tok:
            other_opt_3m = D.get_option_3min(int(_other_tok), lookback=5)
    except Exception as _oe:
        logger.debug("[ENGINE] backbone fetch: " + str(_oe))

    return _evaluate_entry_gates_pure(
        opt_3m=opt_3m, option_type=option_type, spot_ltp=spot_ltp,
        now=now, market_open=market_open, state=state,
        straddle_delta=straddle_delta, spot_vwap=spot_vwap,
        spot_for_vwap=spot_for_vwap, atm_strike=atm_strike,
        silent=silent, other_opt_3m=other_opt_3m)


def compute_entry_sl(entry_price: float, hard_sl: int = 12) -> float:
    return round(entry_price - hard_sl, 2)


def is_setup_building(token: int, direction: str) -> bool:
    return False


def compute_trail_sl(entry_price: float, peak_pnl: float,
                     direction: str = "") -> tuple:
    """Balanced trail: 60% → 70% → 80% as peak grows."""
    if peak_pnl >= 40:
        sl = entry_price + peak_pnl * 0.80
        tier = "TRAIL_80"
    elif peak_pnl >= 25:
        sl = entry_price + peak_pnl * 0.70
        tier = "TRAIL_70"
    elif peak_pnl >= 10:
        sl = entry_price + peak_pnl * 0.60
        tier = "TRAIL_60"
    else:
        sl = entry_price - 10
        tier = "INITIAL"
    return round(sl, 2), tier


def check_profit_lock(state: dict, daily_pnl: float) -> bool:
    if state.get("profit_locked"):
        return False
    if daily_pnl >= D.PROFIT_LOCK_PTS:
        state["profit_locked"] = True
        logger.info("[ENGINE] Profit lock at " + str(round(daily_pnl, 1)) + "pts")
        return True
    return False


# ═══════════════════════════════════════════════════════════════
#  EXIT CHAIN
# ═══════════════════════════════════════════════════════════════

def _evaluate_exit_chain_pure(state: dict, option_ltp: float,
                               opt_3m_full, now,
                               market_open: bool) -> list:
    if not state.get("in_trade"):
        return []

    entry = state.get("entry_price", 0)
    pnl = round(option_ltp - entry, 2)
    peak = max(state.get("peak_pnl", 0), pnl)
    state["peak_pnl"] = peak
    if pnl < state.get("trough_pnl", 0):
        state["trough_pnl"] = pnl

    emergency_sl  = CFG.exit_ema9_band("emergency_sl_pts", -10)
    eod_time      = CFG.exit_ema9_band("eod_exit_time", "15:20")

    if pnl <= emergency_sl:
        logger.info("[ENGINE] EMERGENCY_SL pnl=" + str(pnl))
        return [{"lot_id": "ALL", "reason": "EMERGENCY_SL", "price": option_ltp}]

    if market_open:
        eod_h, eod_m = eod_time.split(":")
        eod_mins = int(eod_h) * 60 + int(eod_m)
        if now.hour * 60 + now.minute >= eod_mins:
            logger.info("[ENGINE] EOD_EXIT at " + now.strftime("%H:%M"))
            return [{"lot_id": "ALL", "reason": "EOD_EXIT", "price": option_ltp}]

    # Update EMA9 bands for dashboard
    if opt_3m_full is not None and not opt_3m_full.empty and len(opt_3m_full) >= 2:
        last = opt_3m_full.iloc[-2]
        state["current_ema9_high"] = round(float(last.get("ema9_high", 0)), 2)
        state["current_ema9_low"]  = round(float(last.get("ema9_low", 0)), 2)

    # Update peak history
    last_candle_ts = ""
    if opt_3m_full is not None and not opt_3m_full.empty and len(opt_3m_full) >= 2:
        last = opt_3m_full.iloc[-2]
        last_candle_ts = str(last.name) if hasattr(last, "name") else str(last.get("timestamp", ""))
    if last_candle_ts and state.get("last_peak_candle_ts") != last_candle_ts:
        ph = list(state.get("peak_history") or [])
        ph.append(round(peak, 2))
        ph = ph[-6:]
        state["peak_history"] = ph
        state["last_peak_candle_ts"] = last_candle_ts

    # Compute trail SL
    trail_sl, trail_tier = compute_trail_sl(entry, peak, state.get("direction", ""))
    state["active_ratchet_tier"] = trail_tier
    state["active_ratchet_sl"]   = trail_sl
    if trail_sl > 0 and option_ltp <= trail_sl:
        logger.info("[ENGINE] VISHAL_TRAIL tier=" + trail_tier + " sl=" + str(trail_sl))
        return [{"lot_id": "ALL", "reason": "VISHAL_TRAIL", "price": trail_sl}]

    return []


def manage_exit(state: dict, option_ltp: float, profile: dict,
                other_token: int = 0) -> list:
    if not state.get("in_trade"):
        return []

    token = state.get("token")
    opt_3m_full = None
    try:
        opt_3m_full = D.get_option_3min(token, lookback=10)
    except Exception as e:
        logger.warning("[ENGINE] band fetch error: " + str(e))

    market_open = D.is_market_open()
    now = datetime.now()

    return _evaluate_exit_chain_pure(
        state=state, option_ltp=option_ltp,
        opt_3m_full=opt_3m_full, now=now,
        market_open=market_open)
