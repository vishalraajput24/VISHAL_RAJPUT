# ═══════════════════════════════════════════════════════════════
#  VRL_ENGINE.py — VISHAL RAJPUT TRADE v13.9
#  FAST: 2 green above EMA9 + breakout confirm + RSI↑ + spot aligned + slope.
#  CONFIRMED: 3m momentum backup. Time-aware RSI cap. Stop hunt recovery.
#  Static profit floors + dynamic trail. Entry cutoff 15:10.
# ═══════════════════════════════════════════════════════════════

import logging
import os
from datetime import datetime
import pandas as pd
import VRL_DATA as D
import VRL_CONFIG as CFG

logger = logging.getLogger("vrl_live")

# ═══════════════════════════════════════════════════════════════
#  DASHBOARD UTILITY
# ═══════════════════════════════════════════════════════════════

def get_option_ema_spread(token: int, dte: int = 99) -> float:
    try:
        df = D.get_historical_data(token, "3minute", D.LOOKBACK_3M)
        df = D.add_indicators(df)
        if df.empty or len(df) < 4:
            return 0.0
        last = df.iloc[-2]
        if dte <= 1 and len(df) < 25:
            lookback_idx = min(5, len(df) - 2)
            ref_close = df.iloc[-2 - lookback_idx]["close"]
            return round(last["close"] - ref_close, 2)
        return round(last.get("EMA_9", last["close"]) - last.get("EMA_21", last["close"]), 2)
    except Exception as e:
        logger.warning("[ENGINE] EMA spread error: " + str(e))
        return 0.0

# ═══════════════════════════════════════════════════════════════
#  TIME-AWARE RSI CAP (v13.8 Change 2)
# ═══════════════════════════════════════════════════════════════

def _get_rsi_cap(aggressive: bool = False) -> tuple:
    """Return (rsi_cap, session_name) based on time of day.
    Morning trending (9:15-10:15): 78. Midday chop (10:15-14:00): 72.
    Afternoon balanced (14:00-15:10): 75. Aggressive mode adds +3."""
    now = datetime.now()
    if now.hour < 10 or (now.hour == 10 and now.minute <= 15):
        cap, session = 78, "MORNING"
    elif now.hour < 14:
        cap, session = 72, "MIDDAY"
    else:
        cap, session = 75, "AFTERNOON"
    if aggressive:
        cap += 3
    return cap, session


# ═══════════════════════════════════════════════════════════════
#  PRE-ENTRY GUARDS (v13.8: stop hunt recovery — Change 5)
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
            last_reason = state.get("last_exit_reason", "")
            last_exit_price = float(state.get("last_exit_price", 0))

            if direction and direction == last_dir:
                # v13.8 Change 5: Stop hunt recovery — skip cooloff if CANDLE_SL
                # exit and price recovered 5+ pts within 1+ minute
                if (last_reason == "CANDLE_SL"
                        and option_ltp > last_exit_price + 5
                        and elapsed >= 1):
                    logger.info("[COOLOFF] Stop hunt recovery detected — "
                                + direction + " re-entry allowed"
                                + " (exit=" + str(last_exit_price)
                                + " now=" + str(option_ltp) + ")")
                    # Skip cooloff, allow entry to continue
                else:
                    # Standard 5-minute same-direction cooloff
                    cooldown = 5
                    if elapsed < cooldown:
                        remaining = round(cooldown - elapsed, 1)
                        logger.info("[ENGINE] COOLDOWN " + str(remaining)
                                    + "min remaining (same dir=" + direction + ")")
                        return False, "Cooldown: " + str(remaining) + "min (same dir)"
            elif direction and direction != last_dir and last_dir:
                # Opposite direction: enter immediately
                pass
            else:
                if elapsed < D.REENTRY_COOLDOWN_MIN:
                    return False, "Cooldown: " + str(round(D.REENTRY_COOLDOWN_MIN - elapsed, 1)) + "min"
        except Exception:
            pass

    if state.get("in_trade"):                return False, "Already in trade"
    if not D.is_entry_fire_window():         return False, "Before entry window"
    if not D.is_market_open():               return False, "Market closed"
    if not D.is_tick_live(D.NIFTY_SPOT_TOKEN): return False, "Spot tick stale"
    if option_ltp <= 0:                      return False, "Option LTP zero"
    if state.get("paused"):                  return False, "Bot paused"
    if not D.PAPER_MODE and kite is not None:
        try:
            from VRL_TRADE import get_margin_available
            avail = get_margin_available(kite)
            if avail > 0 and avail < option_ltp * D.LOT_SIZE * 1.2:
                return False, "Insufficient margin"
        except Exception:
            pass
    return True, ""


def loss_streak_gate(state: dict) -> bool:
    """Always permit — streak management handled upstream."""
    return True


# ═══════════════════════════════════════════════════════════════
#  v13.8 ENTRY — FAST (2 green EMA9) + CONFIRMED (3m momentum)
# ═══════════════════════════════════════════════════════════════

def check_entry(token: int, option_type: str, spot_ltp: float = 0,
                dte: int = 99, expiry_date=None, kite=None,
                other_token: int = 0, silent: bool = False,
                state: dict = None) -> dict:
    """v13.8: FAST = 2 green above EMA9, CONFIRMED = 3m momentum backup.
    Time-aware RSI cap. Spot alignment. Straddle aggressive mode."""
    result = {
        "fired": False, "option_type": option_type,
        "entry_price": 0, "ema9": 0, "ema21": 0,
        "ema_gap": 0, "rsi": 0, "rsi_prev": 0,
        "ema_ok": False, "rsi_ok": False,
        "candle_green": False, "gap_widening": False,
        "entry_mode": "", "rsi_rising": False,
        "other_falling": False, "other_move": 0,
        "spot_aligned": False, "two_green_above": False,
        "other_below_ema": False, "rsi_cap_active": 0,
        "breakout_confirmed": False, "spot_slope": 0,
        # Legacy compat fields (dashboard reads these)
        "path_a": False, "path_b": False,
        "momentum_pts": 0, "momentum_tf": "",
        "momentum_threshold": 0, "spike_ratio": 0,
        "spot_confirms": False, "spot_move": 0,
        "higher_low": False, "ema_would_fire": False,
    }
    if state is None:
        state = {}
    try:
        cfg = CFG.get().get("entry", {})
        aggressive = state.get("aggressive_mode", False)
        rsi_cap, session_name = _get_rsi_cap(aggressive)
        result["rsi_cap_active"] = rsi_cap

        conf_pts = cfg.get("confirmed_momentum_pts", 20)
        conf_candles = cfg.get("confirmed_momentum_candles", 3)
        # v13.8 Change 3: aggressive mode lowers confirmed threshold
        if aggressive:
            conf_pts = min(conf_pts, 15)

        # ═══ 1-MIN DATA ═══
        df_1m = D.get_historical_data(token, "minute", 50)
        df_1m = D.add_indicators(df_1m)
        if df_1m.empty or len(df_1m) < 7:
            return result

        curr_1m = df_1m.iloc[-2]
        prev_1m = df_1m.iloc[-3]

        entry_price = float(curr_1m["close"])
        rsi = float(curr_1m.get("RSI", 50))
        rsi_prev = float(prev_1m.get("RSI", 50))
        rsi_rising = rsi > rsi_prev
        ema9 = float(curr_1m.get("EMA_9", 0))
        ema21 = float(curr_1m.get("EMA_21", 0))
        ema_gap = round(ema9 - ema21, 2)

        _curr_green = entry_price > float(curr_1m["open"])
        _prev_green = float(prev_1m["close"]) > float(prev_1m["open"])
        _curr_above = ema9 > 0 and entry_price > ema9
        _prev_ema9 = float(prev_1m.get("EMA_9", 0))
        _prev_above = _prev_ema9 > 0 and float(prev_1m["close"]) > _prev_ema9
        _two_green_above = _curr_green and _prev_green and _curr_above and _prev_above

        # v13.9 Change 1: Option breakout confirmation
        # Last candle close must exceed previous candle high — genuine breakout
        _prev_high = float(prev_1m["high"])
        _breakout_confirmed = entry_price > _prev_high

        result.update({
            "entry_price": round(entry_price, 2),
            "ema9": round(ema9, 2), "ema21": round(ema21, 2),
            "ema_gap": ema_gap, "rsi": round(rsi, 1),
            "rsi_prev": round(rsi_prev, 1), "rsi_rising": rsi_rising,
            "candle_green": _curr_green,
            "two_green_above": _two_green_above,
            "breakout_confirmed": _breakout_confirmed,
        })

        # Entry cutoff at 15:10 IST
        _now = datetime.now()
        if D.is_market_open() and _now.hour >= 15 and _now.minute >= 10:
            if not silent:
                logger.info("[ENGINE] " + option_type
                            + " Entry cutoff 15:10 — market close approaching")
            return result

        # RSI HARD CAP (v13.8: time-aware)
        if rsi > rsi_cap:
            if not silent:
                logger.info("[NEAR_MISS] " + option_type + " RSI cap block: rsi="
                            + str(round(rsi, 1)) + " cap=" + str(rsi_cap)
                            + " (" + session_name
                            + ("+AGG" if aggressive else "") + ")")
            return result

        # ═══ OTHER SIDE CHECK ═══
        other_falling = False
        other_below_ema = False
        other_move = 0
        if other_token:
            try:
                other_df = D.get_historical_data(other_token, "minute", 10)
                other_df = D.add_indicators(other_df)
                if other_df is not None and not other_df.empty and len(other_df) >= 6:
                    _o_curr = other_df.iloc[-2]
                    _o_close = float(_o_curr["close"])
                    _o_ema9 = float(_o_curr.get("EMA_9", 0))
                    _o_4ago = float(other_df.iloc[-6]["close"])
                    other_move = round(_o_close - _o_4ago, 2)
                    other_falling = other_move < 0
                    other_below_ema = _o_ema9 > 0 and _o_close < _o_ema9
            except Exception:
                pass
        result["other_falling"] = other_falling
        result["other_move"] = other_move
        result["other_below_ema"] = other_below_ema

        # Divergence gate: other side must be falling
        if not other_falling and other_token:
            result["candle_green"] = _curr_green
            if not silent:
                logger.info("[ENGINE] " + option_type
                            + " DIVERGENCE FAIL other=" + str(other_move) + "pts")
            return result

        # ═══ SPOT ALIGNMENT (v13.8 Change 4) ═══
        _spot_aligned = True
        try:
            _spot_df = D.get_historical_data(D.NIFTY_SPOT_TOKEN, "minute", 50)
            _spot_df = D.add_indicators(_spot_df)
            if not _spot_df.empty and len(_spot_df) >= 10:
                _sp_last = _spot_df.iloc[-2]
                _sp_close = float(_sp_last["close"])
                _sp_ema9 = float(_sp_last.get("EMA_9", _sp_close))
                _sp_above = _sp_close > _sp_ema9
                if option_type == "CE" and not _sp_above:
                    _spot_aligned = False
                    if not silent:
                        logger.info("[ENGINE] CE blocked: spot not aligned"
                                    + " close=" + str(round(_sp_close, 1))
                                    + " ema9=" + str(round(_sp_ema9, 1)))
                    return result
                if option_type == "PE" and _sp_above:
                    _spot_aligned = False
                    if not silent:
                        logger.info("[ENGINE] PE blocked: spot not aligned"
                                    + " close=" + str(round(_sp_close, 1))
                                    + " ema9=" + str(round(_sp_ema9, 1)))
                    return result
        except Exception:
            pass
        result["spot_aligned"] = _spot_aligned

        # v13.9 Change 2: Spot EMA slope — filter out sideways
        _spot_slope = 0
        try:
            if not _spot_df.empty and len(_spot_df) >= 8:
                _slope_now = float(_spot_df.iloc[-2].get("EMA_9", 0))
                _slope_5ago = float(_spot_df.iloc[-7].get("EMA_9", 0))
                _spot_slope = round(_slope_now - _slope_5ago, 1)
        except Exception:
            pass
        result["spot_slope"] = _spot_slope

        _slope_ok = True
        if option_type == "CE" and _spot_slope < 2:
            _slope_ok = False
            if not silent:
                logger.info("[ENGINE] CE spot_flat slope=" + str(_spot_slope))
            return result
        if option_type == "PE" and _spot_slope > -2:
            _slope_ok = False
            if not silent:
                logger.info("[ENGINE] PE spot_flat slope=" + str(_spot_slope))
            return result

        # ═══ FAST PATH (v13.9: + breakout confirm + spot slope) ═══
        path_fast = (_two_green_above
                     and _breakout_confirmed
                     and rsi_rising
                     and rsi <= rsi_cap
                     and other_below_ema)
        result["path_a"] = path_fast

        # ═══ CONFIRMED PATH (3-min momentum — kept from v13.7) ═══
        path_conf = False
        mom_3m = 0
        try:
            df_3m = D.get_historical_data(token, "3minute", 50)
            df_3m = D.add_indicators(df_3m)
            if not df_3m.empty and len(df_3m) >= conf_candles + 3:
                curr_3m = df_3m.iloc[-2]
                prev_3m = df_3m.iloc[-3]
                ref_3m = float(df_3m.iloc[-3 - conf_candles]["close"])
                mom_3m = round(float(prev_3m["close"]) - ref_3m, 2)
                curr_3m_green = float(curr_3m["close"]) > float(curr_3m["open"])
                rsi_3m = float(curr_3m.get("RSI", 50))
                rsi_3m_prev = float(prev_3m.get("RSI", 50))
                rsi_3m_rising = rsi_3m > rsi_3m_prev
                path_conf = (mom_3m >= conf_pts
                             and curr_3m_green
                             and rsi_3m_rising
                             and rsi_3m <= rsi_cap)
        except Exception:
            pass
        result["path_b"] = path_conf
        result["momentum_pts"] = mom_3m
        result["momentum_threshold"] = conf_pts

        # ═══ FIRE LOGIC ═══
        if path_fast:
            result["fired"] = True
            result["entry_mode"] = "FAST"
            if not silent:
                logger.info("[ENGINE] " + option_type + " ENTRY [FAST]"
                            + " 2green_above=✓ breakout=✓"
                            + " rsi=" + str(round(rsi, 1)) + "↑/" + str(rsi_cap)
                            + " slope=" + str(_spot_slope)
                            + " entry=" + str(entry_price))
        elif path_conf:
            result["fired"] = True
            result["entry_mode"] = "CONFIRMED"
            result["momentum_tf"] = "3m"
            if not silent:
                logger.info("[ENGINE] " + option_type + " ENTRY [CONFIRMED]"
                            + " 3m=" + str(mom_3m) + "/" + str(conf_pts)
                            + " rsi=" + str(round(rsi, 1))
                            + " spot_aligned=✓"
                            + " entry=" + str(entry_price))
        else:
            reasons = []
            if not _two_green_above:
                reasons.append("2G_EMA_X")
            elif not _breakout_confirmed:
                reasons.append("BREAKOUT_X")
            if not rsi_rising:
                reasons.append("RSI↓")
            if not other_below_ema and other_token:
                reasons.append("other_above_ema")
            if mom_3m < conf_pts:
                reasons.append("3m=" + str(mom_3m) + "/" + str(conf_pts))
            if not silent:
                logger.info("[ENGINE] " + option_type + " " + " ".join(reasons)
                            + " rsi=" + str(round(rsi, 1))
                            + "/" + str(rsi_cap)
                            + " spot=✓")

        return result
    except Exception as e:
        logger.error("[ENGINE] check_entry error: " + str(e))
        return result


def compute_entry_sl(entry_price: float, hard_sl: int = 12) -> float:
    """Simple fixed SL. Exit fires on candle close beyond this."""
    return round(entry_price - hard_sl, 2)


def check_profit_lock(state: dict, daily_pnl: float) -> bool:
    if state.get("profit_locked"):
        return False
    if daily_pnl >= D.PROFIT_LOCK_PTS:
        state["profit_locked"] = True
        logger.info("[ENGINE] Profit lock at " + str(round(daily_pnl, 1)) + "pts")
        return True
    return False


# ═══════════════════════════════════════════════════════════════
#  v13.8 EXIT — Priority chain + static profit floors + dynamic trail
# ═══════════════════════════════════════════════════════════════

def manage_exit(state: dict, option_ltp: float, profile: dict,
                other_token: int = 0) -> list:
    """v13.8: Priority chain. Static profit floors persist to state."""
    if not state.get("in_trade"):
        return []

    entry = state.get("entry_price", 0)
    running = round(option_ltp - entry, 2)
    peak = state.get("peak_pnl", 0)
    candles = state.get("candles_held", 0)

    if running > peak:
        state["peak_pnl"] = running
        peak = running
    if running < state.get("trough_pnl", 0):
        state["trough_pnl"] = running

    # RSI on 1-min
    token = state.get("token")
    rsi = 50
    try:
        df = D.get_historical_data(token, "minute", 25)
        df = D.add_indicators(df)
        if not df.empty and len(df) >= 2:
            rsi = float(df.iloc[-2].get("RSI", 50))
    except Exception:
        pass
    state["current_rsi"] = round(rsi, 1)

    # Candle low tracking for spike detection
    candle_low = state.get("_candle_low", option_ltp)
    if candle_low <= 0:
        candle_low = option_ltp
    if option_ltp < candle_low:
        state["_candle_low"] = option_ltp
        candle_low = option_ltp

    exit_cfg = CFG.get().get("exit", {})
    candle_sl       = exit_cfg.get("candle_close_sl", 12)
    max_sl          = exit_cfg.get("max_sl", 20)
    spike_recovery  = exit_cfg.get("spike_recovery", 8)
    stale_candles   = exit_cfg.get("stale_candles", 5)
    stale_peak      = exit_cfg.get("stale_peak_min", 3)
    rsi_blowoff     = CFG.get().get("rsi_exit", {}).get("blowoff", 80)

    trail_cfg = CFG.get().get("profit_trail", {})
    entry_mode = state.get("entry_mode", "FAST")
    min_lock = trail_cfg.get("min_lock", 2)

    if entry_mode == "CONFIRMED":
        trail_activate = trail_cfg.get("confirmed_activate_at", 20)
        keep_normal = trail_cfg.get("confirmed_keep_normal", 0.65)
        keep_warning = trail_cfg.get("confirmed_keep_warning", 0.80)
    else:
        trail_activate = trail_cfg.get("fast_activate_at", 15)
        keep_normal = trail_cfg.get("fast_keep_normal", 0.75)
        keep_warning = trail_cfg.get("fast_keep_warning", 0.85)

    # ═══ OTHER SIDE ANALYSIS ═══
    other_reversing = False
    other_warning = False
    other_falling = True
    if other_token:
        try:
            other_df = D.get_historical_data(other_token, "3minute", 10)
            if other_df is not None and not other_df.empty and len(other_df) >= 4:
                other_curr = other_df.iloc[-2]
                other_prev = other_df.iloc[-3]
                other_curr_green = float(other_curr["close"]) > float(other_curr["open"])
                other_prev_green = float(other_prev["close"]) > float(other_prev["open"])
                other_rsi = float(other_curr.get("RSI", 50))
                other_rsi_prev = float(other_prev.get("RSI", 50))
                other_rsi_rising = other_rsi > other_rsi_prev
                other_reversing = other_curr_green and other_prev_green and other_rsi_rising
                other_warning = other_curr_green and not other_prev_green
                other_falling = not other_curr_green
        except Exception:
            pass
    state["other_reversing"] = other_reversing
    state["other_warning"] = other_warning
    state["other_falling"] = other_falling

    # ═══ EXIT PRIORITY ORDER ═══

    # 1. EMERGENCY
    if running <= -max_sl:
        logger.info("[ENGINE] EMERGENCY_SL: running=" + str(running))
        return [{"lot_id": "ALL", "reason": "EMERGENCY_SL", "price": option_ltp}]

    # 2. STALE
    if candles >= stale_candles and peak < stale_peak:
        logger.info("[ENGINE] STALE_ENTRY: " + str(candles) + "c peak=" + str(peak))
        return [{"lot_id": "ALL", "reason": "STALE_ENTRY", "price": option_ltp}]

    # 3. RSI BLOWOFF > 80
    if rsi > rsi_blowoff:
        logger.info("[ENGINE] RSI_BLOWOFF: rsi=" + str(round(rsi, 1)))
        return [{"lot_id": "ALL", "reason": "RSI_BLOWOFF", "price": option_ltp}]

    # 4. DIVERGENCE EXIT
    if other_reversing and peak >= 6:
        logger.info("[ENGINE] DIVERGENCE_EXIT: other reversed, peak=" + str(round(peak, 1)))
        return [{"lot_id": "ALL", "reason": "DIVERGENCE_EXIT", "price": option_ltp}]

    # 5. SPIKE CHECK
    low_touch = candle_low - entry
    if low_touch <= -candle_sl and running > -spike_recovery:
        if other_falling:
            logger.info("[ENGINE] SPIKE_ABSORBED: low=" + str(round(low_touch, 1))
                         + " close=" + str(round(running, 1)) + " other still falling")
        else:
            logger.info("[ENGINE] WEAK_SL: spike + other not falling")
            return [{"lot_id": "ALL", "reason": "WEAK_SL", "price": option_ltp}]

    # 6. CANDLE CLOSE SL
    if running <= -candle_sl:
        logger.info("[ENGINE] CANDLE_SL: running=" + str(running))
        return [{"lot_id": "ALL", "reason": "CANDLE_SL", "price": option_ltp}]

    # 6b. STATIC PROFIT FLOORS (BUG-027: persist to state)
    _floors = CFG.get().get("profit_floors", [])
    _best_floor = None
    for _f in _floors:
        if peak >= _f.get("peak", 999):
            _best_floor = _f
    if _best_floor is not None:
        _lock = _best_floor.get("lock", 0)
        _floor_sl = round(entry + _lock, 2)
        _prev_floor = state.get("_static_floor_sl", 0)
        if _floor_sl > _prev_floor:
            state["_static_floor_sl"] = _floor_sl
            if _floor_sl > state.get("phase1_sl", 0):
                state["phase1_sl"] = _floor_sl
            logger.info("[FLOOR] Peak " + str(round(peak, 1))
                        + " crossed +" + str(_best_floor.get("peak", 0))
                        + ", SL ratcheted to " + str(_floor_sl)
                        + " (+" + str(_lock) + " locked)")
        _active_floor = max(_floor_sl, state.get("_static_floor_sl", 0))
        if option_ltp <= _active_floor:
            logger.info("[ENGINE] PROFIT_FLOOR: peak=" + str(round(peak, 1))
                        + " floor=" + str(_active_floor))
            return [{"lot_id": "ALL", "reason": "PROFIT_FLOOR", "price": option_ltp}]

    # 7. DYNAMIC TRAIL
    if peak >= trail_activate:
        keep = keep_warning if other_warning else keep_normal
        lock_pts = round(peak * keep, 1)
        lock_pts = max(lock_pts, min_lock)
        floor_sl = entry + lock_pts
        state["current_floor"] = round(floor_sl, 2)
        state["current_keep"] = keep
        state["current_lock"] = round(lock_pts, 1)

        if option_ltp <= floor_sl:
            logger.info("[ENGINE] TRAIL_FLOOR: peak=" + str(round(peak, 1))
                         + " keep=" + str(keep) + " lock=+" + str(lock_pts)
                         + " floor=" + str(round(floor_sl, 2)))
            return [{"lot_id": "ALL", "reason": "TRAIL_FLOOR", "price": option_ltp}]

    return []
