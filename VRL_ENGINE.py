# ═══════════════════════════════════════════════════════════════
#  VRL_ENGINE.py — VISHAL RAJPUT TRADE v13.5
#  Minimal signal logic. EMA gap + RSI entry. 2-lot exit.
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
        # Momentum fallback only on DTE <= 1 with thin candles
        if dte <= 1 and len(df) < 25:
            lookback_idx = min(5, len(df) - 2)
            ref_close = df.iloc[-2 - lookback_idx]["close"]
            return round(last["close"] - ref_close, 2)
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
            last_peak = state.get("last_exit_peak", 0)

            # Direction-aware cooldown
            import VRL_CONFIG as CFG
            cooldown_after_win = CFG.get().get("cooldown", {}).get("after_win", 10)
            cooldown_after_loss = CFG.get().get("cooldown", {}).get("after_loss", 5)

            if direction and direction == last_dir:
                # Same direction re-entry: longer cooldown after big win
                cooldown = cooldown_after_win if last_peak >= 10 else cooldown_after_loss
                if elapsed < cooldown:
                    remaining = round(cooldown - elapsed, 1)
                    logger.info("[ENGINE] COOLDOWN " + str(remaining) + "min remaining"
                                + " (same dir=" + direction + " peak=" + str(round(last_peak, 1)) + ")")
                    return False, "Cooldown: " + str(remaining) + "min (same dir)"
            elif direction and direction != last_dir and last_dir:
                # Opposite direction: enter immediately — no cooldown
                pass
            else:
                # Fallback: use default cooldown
                if elapsed < D.REENTRY_COOLDOWN_MIN:
                    return False, "Cooldown: " + str(round(D.REENTRY_COOLDOWN_MIN - elapsed, 1)) + "min"
        except Exception:
            pass
    if state.get("in_trade"):                return False, "Already in trade"
    if not D.is_entry_fire_window():         return False, "Before 9:45"
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
    """v13.0: Always permit — streak management handled upstream."""
    return True

# ═══════════════════════════════════════════════════════════════
#  v13.0 ENTRY — 1-MIN EMA GAP + RSI ONLY
# ═══════════════════════════════════════════════════════════════


# v13.3 ENTRY -- LIMIT at breakout level. EMA as 2nd path.

def check_entry(token: int, option_type: str, spot_ltp: float = 0,
                dte: int = 99, expiry_date=None, kite=None,
                other_token: int = 0, silent: bool = False) -> dict:
    """v13.5: Dual timeframe momentum + divergence."""
    result = {
        "fired": False, "option_type": option_type,
        "entry_price": 0, "ema9": 0, "ema21": 0,
        "ema_gap": 0, "rsi": 0, "rsi_prev": 0,
        "ema_ok": False, "rsi_ok": False,
        "candle_green": False, "gap_widening": False,
        "higher_low": False, "entry_mode": "",
        "momentum_pts": 0, "momentum_tf": "",
        "ema_would_fire": False, "path_a": False, "path_b": False,
        "rsi_rising": False, "spot_confirms": False, "spot_move": 0,
        "spike_ratio": 0, "other_falling": False, "other_move": 0,
        "momentum_threshold": 0,
    }
    try:
        import VRL_CONFIG as CFG
        cfg = CFG.get().get("entry", {})
        rsi_max      = cfg.get("rsi_max", 72)
        fast_pts     = cfg.get("fast_momentum_pts", 14)
        fast_candles = cfg.get("fast_momentum_candles", 4)
        conf_pts     = cfg.get("confirmed_momentum_pts", 20)
        conf_candles = cfg.get("confirmed_momentum_candles", 3)

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

        result.update({
            "entry_price": round(entry_price, 2),
            "ema9": round(ema9, 2), "ema21": round(ema21, 2),
            "ema_gap": ema_gap, "rsi": round(rsi, 1),
            "rsi_prev": round(rsi_prev, 1), "rsi_rising": rsi_rising,
        })

        # RSI HARD CAP
        if rsi > rsi_max:
            if not silent: logger.info("[ENGINE] " + option_type + " RSI_CAP " + str(round(rsi, 1)))
            return result

        # ═══ OTHER SIDE CHECK (divergence) ═══
        other_falling = False
        other_move = 0
        if other_token:
            try:
                other_df = D.get_historical_data(other_token, "minute", 10)
                if other_df is not None and not other_df.empty and len(other_df) >= 6:
                    other_now = float(other_df.iloc[-2]["close"])
                    other_4ago = float(other_df.iloc[-6]["close"])
                    other_move = round(other_now - other_4ago, 2)
                    other_falling = other_move < 0
            except Exception:
                pass
        result["other_falling"] = other_falling
        result["other_move"] = other_move

        # If other side NOT falling - no entry from either timeframe
        if not other_falling and other_token:
            reasons = ["OTHER_UP X (" + str(other_move) + "pts)"]
            if not silent: logger.info("[ENGINE] " + option_type + " DIVERGENCE FAIL " + " ".join(reasons))
            if len(df_1m) >= fast_candles + 3:
                ref_1m = float(df_1m.iloc[-3 - fast_candles]["close"])
                mom_1m = round(float(prev_1m["close"]) - ref_1m, 2)
                result["momentum_pts"] = mom_1m
            result["candle_green"] = float(curr_1m["close"]) > float(curr_1m["open"])
            return result

        # ═══ EMA INFO (never fires alone) ═══
        rsi_ok = rsi >= 50 and rsi > rsi_prev
        prev_e9 = float(prev_1m.get("EMA_9", 0))
        prev_e21 = float(prev_1m.get("EMA_21", 0))
        prev_gap = round(prev_e9 - prev_e21, 2)
        gap_widening = ema_gap > prev_gap
        ema_ok = ema_gap >= cfg.get("ema_gap_min", 3)
        result["ema_ok"] = ema_ok
        result["rsi_ok"] = rsi_ok
        result["gap_widening"] = gap_widening
        _curr_green_1m = float(curr_1m["close"]) > float(curr_1m["open"])
        path_ema = ema_ok and rsi_ok and _curr_green_1m and gap_widening
        result["ema_would_fire"] = path_ema
        result["path_a"] = path_ema

        # ═══ 1-MIN MOMENTUM (FAST) ═══
        path_fast = False
        mom_1m = 0
        if len(df_1m) >= fast_candles + 3:
            ref_1m = float(df_1m.iloc[-3 - fast_candles]["close"])
            mom_1m = round(float(prev_1m["close"]) - ref_1m, 2)
            path_fast = (mom_1m >= fast_pts
                         and _curr_green_1m
                         and rsi_rising
                         and rsi <= rsi_max)

        # ═══ 3-MIN MOMENTUM (CONFIRMED) ═══
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
                             and rsi_3m <= rsi_max)
        except Exception:
            pass

        result["momentum_pts"] = max(mom_1m, mom_3m)
        result["candle_green"] = _curr_green_1m
        result["path_b"] = path_fast or path_conf

        # ═══ FIRE LOGIC ═══
        if path_fast and path_conf:
            result["fired"] = True
            result["entry_mode"] = "CONFIRMED"
            result["momentum_tf"] = "1m+3m"
            result["momentum_threshold"] = fast_pts
            if not silent: logger.info("[ENGINE] " + option_type
                + " ENTRY [CONFIRMED **] 1m=" + str(mom_1m)
                + " 3m=" + str(mom_3m)
                + " rsi=" + str(round(rsi, 1))
                + " other=" + str(other_move)
                + " entry=" + str(entry_price))
        elif path_fast:
            result["fired"] = True
            result["entry_mode"] = "FAST"
            result["momentum_tf"] = "1m"
            result["momentum_threshold"] = fast_pts
            if not silent: logger.info("[ENGINE] " + option_type
                + " ENTRY [FAST] 1m=" + str(mom_1m) + "/" + str(fast_pts)
                + " rsi=" + str(round(rsi, 1))
                + (" RSIup" if rsi_rising else " RSIdn")
                + " other=" + str(other_move)
                + " entry=" + str(entry_price))
        elif path_conf:
            result["fired"] = True
            result["entry_mode"] = "CONFIRMED"
            result["momentum_tf"] = "3m"
            result["momentum_threshold"] = conf_pts
            if not silent: logger.info("[ENGINE] " + option_type
                + " ENTRY [CONFIRMED *] 3m=" + str(mom_3m) + "/" + str(conf_pts)
                + " rsi=" + str(round(rsi, 1))
                + " other=" + str(other_move)
                + " entry=" + str(entry_price))
        else:
            reasons = []
            reasons.append("1m=" + str(mom_1m) + "/" + str(fast_pts)
                           + ("V" if mom_1m >= fast_pts else "X"))
            reasons.append("3m=" + str(mom_3m) + "/" + str(conf_pts)
                           + ("V" if mom_3m >= conf_pts else "X"))
            if not _curr_green_1m:
                reasons.append("REDX")
            if not rsi_rising:
                reasons.append("RSIdnX")
            reasons.append("other=" + str(other_move)
                           + ("V" if other_falling else "X"))
            if path_ema:
                reasons.append("[EMAV]")
            if not silent: logger.info("[ENGINE] " + option_type + " " + " ".join(reasons))

        return result
    except Exception as e:
        logger.error("[ENGINE] check_entry error: " + str(e))
        return result

def compute_entry_sl(entry_price: float, hard_sl: int = 12) -> float:
    """v13.0: Simple fixed SL."""
    return round(entry_price - hard_sl, 2)

def check_profit_lock(state: dict, daily_pnl: float) -> bool:
    if state.get("profit_locked"):
        return False
    if daily_pnl >= D.PROFIT_LOCK_PTS:
        state["profit_locked"] = True
        if state.get("in_trade") and state.get("exit_phase") == 3:
            state["trail_tightened"] = True
        logger.info("[ENGINE] Profit lock at " + str(round(daily_pnl, 1)) + "pts")
        return True
    return False

# ═══════════════════════════════════════════════════════════════
#  v13.3 EXIT — SCOUT (LOT1 SL-6) + SOLDIER (LOT2 SL-12) + TRAIL
# ═══════════════════════════════════════════════════════════════

def manage_exit(state: dict, option_ltp: float, profile: dict,
                other_token: int = 0) -> list:
    """v13.5: Logic-based exits. Candle close SL. Dynamic trail."""
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

    import VRL_CONFIG as CFG
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

    # 1. EMERGENCY: running below -20
    if running <= -max_sl:
        logger.info("[ENGINE] EMERGENCY_SL: running=" + str(running))
        return [{"lot_id": "ALL", "reason": "EMERGENCY_SL", "price": option_ltp}]

    # 2. STALE: 5 candles, peak < 3
    if candles >= stale_candles and peak < stale_peak:
        logger.info("[ENGINE] STALE_ENTRY: " + str(candles) + "c peak=" + str(peak))
        return [{"lot_id": "ALL", "reason": "STALE_ENTRY", "price": option_ltp}]

    # 3. RSI BLOWOFF > 80
    if rsi > rsi_blowoff:
        logger.info("[ENGINE] RSI_BLOWOFF: rsi=" + str(round(rsi, 1)))
        return [{"lot_id": "ALL", "reason": "RSI_BLOWOFF", "price": option_ltp}]

    # 4. OTHER SIDE REVERSED: 2 green + RSI rising, peak >= 6
    if other_reversing and peak >= 6:
        logger.info("[ENGINE] DIVERGENCE_EXIT: other reversed, peak=" + str(round(peak, 1)))
        return [{"lot_id": "ALL", "reason": "DIVERGENCE_EXIT", "price": option_ltp}]

    # 5. SPIKE CHECK: low touched -12 but close recovered
    low_touch = candle_low - entry
    if low_touch <= -candle_sl and running > -spike_recovery:
        if other_falling:
            logger.info("[ENGINE] SPIKE_ABSORBED: low=" + str(round(low_touch, 1))
                         + " close=" + str(round(running, 1)) + " other still falling")
        else:
            logger.info("[ENGINE] WEAK_SL: spike + other not falling")
            return [{"lot_id": "ALL", "reason": "WEAK_SL", "price": option_ltp}]

    # 6. CANDLE CLOSE SL: real drop, not spike
    if running <= -candle_sl:
        logger.info("[ENGINE] CANDLE_SL: running=" + str(running))
        return [{"lot_id": "ALL", "reason": "CANDLE_SL", "price": option_ltp}]

    # 6b. STATIC PROFIT FLOORS (BUG-027: persist floor SL to state)
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
            # Also persist to phase1_sl so FORCE_EXIT and /status see it
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

    # 7. DYNAMIC TRAIL (activates at higher peaks than static floors)
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

