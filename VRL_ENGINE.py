# ═══════════════════════════════════════════════════════════════
#  VRL_ENGINE.py — VISHAL RAJPUT TRADE v13.3
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
                dte: int = 99, expiry_date=None, kite=None) -> dict:
    """v13.3: LIMIT entry. Momentum or EMA fires -> entry_level for LIMIT order.
    Removed: green candle check, higher_low check.
    Added: EMA as 2nd path (gap>=5), dynamic thresholds, entry_level."""
    result = {
        "fired": False, "option_type": option_type,
        "entry_price": 0, "entry_level": 0,
        "ema9": 0, "ema21": 0, "ema_gap": 0,
        "rsi": 0, "rsi_prev": 0,
        "ema_ok": False, "rsi_ok": False, "gap_widening": False,
        "entry_mode": "", "momentum_pts": 0,
        "momentum_threshold": 0,
        "ema_would_fire": False, "path_a": False, "path_b": False,
        "rsi_rising": False, "spot_confirms": False, "spot_move": 0,
        "spike_ratio": 0,
    }
    try:
        df = D.get_historical_data(token, "minute", 50)
        df = D.add_indicators(df)
        if df.empty or len(df) < 7:
            return result

        curr = df.iloc[-2]
        prev = df.iloc[-3]

        entry_price = float(curr["close"])
        ema9 = float(curr.get("EMA_9", 0))
        ema21 = float(curr.get("EMA_21", 0))
        ema_gap = round(ema9 - ema21, 2)
        rsi = float(curr.get("RSI", 50))
        rsi_prev = float(prev.get("RSI", 50))
        rsi_rising = rsi > rsi_prev

        result.update({
            "entry_price": round(entry_price, 2),
            "ema9": round(ema9, 2), "ema21": round(ema21, 2),
            "ema_gap": ema_gap, "rsi": round(rsi, 1),
            "rsi_prev": round(rsi_prev, 1), "rsi_rising": rsi_rising,
        })

        # Spot direction -- info only
        spot_confirms = False
        spot_move = 0
        try:
            _sdf = D.get_historical_data(D.NIFTY_SPOT_TOKEN, "minute", 10)
            if _sdf is not None and not _sdf.empty and len(_sdf) >= 5:
                _sn = float(_sdf.iloc[-2]["close"])
                _s3 = float(_sdf.iloc[-5]["close"])
                spot_move = round(_sn - _s3, 2)
                spot_confirms = (spot_move > 0) if option_type == "CE" else (spot_move < 0)
        except Exception:
            pass
        result["spot_move"] = spot_move
        result["spot_confirms"] = spot_confirms

        import VRL_CONFIG as CFG
        cfg = CFG.get().get("entry", {})
        rsi_max       = cfg.get("rsi_max", 72)
        rsi_max_ema   = cfg.get("rsi_max_ema", 68)
        mom_pts_std   = cfg.get("momentum_pts", 14)
        mom_pts_conf  = cfg.get("momentum_pts_confirmed", 10)
        mom_pts_spike = cfg.get("momentum_pts_spike", 16)
        mom_candles   = cfg.get("momentum_candles", 3)
        mom_rsi_min   = cfg.get("momentum_rsi_min", 45)
        ema_info_min  = cfg.get("ema_gap_min", 3)
        ema_fire_min  = cfg.get("ema_fire_min", 5)

        # -- EMA conditions --
        rsi_ok = rsi >= 50 and rsi_rising
        prev_e9 = float(prev.get("EMA_9", 0))
        prev_e21 = float(prev.get("EMA_21", 0))
        gap_widening = ema_gap > round(prev_e9 - prev_e21, 2)
        ema_info = ema_gap >= ema_info_min
        ema_strong = ema_gap >= ema_fire_min
        result["ema_ok"] = ema_info
        result["rsi_ok"] = rsi_ok
        result["gap_widening"] = gap_widening
        path_ema = ema_strong and rsi_ok and gap_widening
        result["ema_would_fire"] = path_ema
        result["path_a"] = path_ema

        # -- MOMENTUM calculation --
        mom_pts = 0
        spike_ratio = 0
        if len(df) >= mom_candles + 3:
            ref = float(df.iloc[-3 - mom_candles]["close"])
            mom_pts = round(float(prev["close"]) - ref, 2)
            result["momentum_pts"] = mom_pts
            prev_prev = df.iloc[-4]
            last_mom = round(float(prev["close"]) - float(prev_prev["close"]), 2)
            result["last_candle_move"] = last_mom
            if mom_pts > 0:
                spike_ratio = round(last_mom / mom_pts, 2)
            result["spike_ratio"] = spike_ratio

        # -- Dynamic threshold --
        if spike_ratio > 0.6:
            mom_threshold = mom_pts_spike
        elif path_ema:
            mom_threshold = mom_pts_conf
        else:
            mom_threshold = mom_pts_std
        result["momentum_threshold"] = mom_threshold

        # -- MOMENTUM FIRE (primary) --
        path_mom = (mom_pts >= mom_threshold
                    and rsi >= mom_rsi_min
                    and rsi <= rsi_max)
        result["path_b"] = path_mom

        if path_mom:
            result["fired"] = True
            result["entry_level"] = round(float(prev["close"]), 2)
            if path_ema:
                result["entry_mode"] = "CONFIRMED"
                logger.info("[ENGINE] " + option_type + " ENTRY [CONFIRMED]"
                    + " mom=" + str(mom_pts) + "/" + str(mom_threshold)
                    + " ema=" + str(ema_gap)
                    + " rsi=" + str(round(rsi, 1))
                    + (" spike" if spike_ratio > 0.6 else " steady")
                    + " LIMIT=" + str(round(float(prev["close"]), 2)))
            else:
                result["entry_mode"] = "MOMENTUM"
                logger.info("[ENGINE] " + option_type + " ENTRY [MOMENTUM]"
                    + " mom=" + str(mom_pts) + "/" + str(mom_threshold)
                    + " rsi=" + str(round(rsi, 1))
                    + (" spike" if spike_ratio > 0.6 else " steady")
                    + " LIMIT=" + str(round(float(prev["close"]), 2)))

        # -- EMA INDEPENDENT FIRE (secondary) --
        elif path_ema and rsi <= rsi_max_ema and not result["fired"]:
            result["fired"] = True
            result["entry_mode"] = "EMA"
            result["entry_level"] = round(entry_price, 2)
            logger.info("[ENGINE] " + option_type + " ENTRY [EMA]"
                + " gap=" + str(ema_gap) + " rsi=" + str(round(rsi, 1))
                + " LIMIT=" + str(round(entry_price, 2)))

        else:
            reasons = []
            if mom_pts < mom_threshold:
                reasons.append("mom=" + str(mom_pts) + "/" + str(mom_threshold) + "X")
            else:
                reasons.append("mom=" + str(mom_pts) + "V")
            if rsi > rsi_max:
                reasons.append("RSI_CAP=" + str(round(rsi, 1)))
            elif rsi < mom_rsi_min:
                reasons.append("rsi=" + str(round(rsi, 1)) + "X")
            else:
                reasons.append("rsi=" + str(round(rsi, 1)) + "V")
            reasons.append("RSI^" if rsi_rising else "RSIv")
            reasons.append("SPOTV" if spot_confirms else "SPOTX")
            if path_ema:
                reasons.append("[EMAV]")
            elif ema_info:
                reasons.append("[ema=" + str(ema_gap) + "]")
            logger.info("[ENGINE] " + option_type + " " + " ".join(reasons))

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

def manage_exit(state: dict, option_ltp: float, profile: dict) -> list:
    """v13.3: Scout (LOT1 SL-6) + Soldier (LOT2 SL-12) + peak-giveback trail."""
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
    import VRL_CONFIG as CFG
    cfg_lots      = CFG.get().get("lots", {})
    lot1_sl_pts   = cfg_lots.get("lot1_sl", 6)
    lot2_sl_pts   = cfg_lots.get("lot2_sl", 12)
    stale_candles = CFG.get().get("exit", {}).get("stale_candles", 5)
    stale_peak    = CFG.get().get("exit", {}).get("stale_peak_min", 3)
    rsi_blowoff   = CFG.get().get("rsi_exit", {}).get("blowoff", 80)
    trail_cfg      = CFG.get().get("profit_trail", {})
    trail_activate = trail_cfg.get("activate_at", 8)

    lot1_active = state.get("lot1_active", True)
    lot2_active = state.get("lot2_active", True)

    if candles >= stale_candles and peak < stale_peak:
        logger.info("[ENGINE] STALE_ENTRY: " + str(candles) + " candles, peak=" + str(peak))
        return [{"lot_id": "ALL", "reason": "STALE_ENTRY", "price": option_ltp}]

    if rsi > rsi_blowoff:
        logger.info("[ENGINE] RSI_BLOWOFF: rsi=" + str(round(rsi, 1)))
        return [{"lot_id": "ALL", "reason": "RSI_BLOWOFF", "price": option_ltp}]

    if peak >= 30:
        giveback = trail_cfg.get("giveback_high", 6)
    elif peak >= 20:
        giveback = trail_cfg.get("giveback_mid", 7)
    else:
        giveback = trail_cfg.get("giveback_low", 8)
    state["current_giveback"] = giveback

    if peak >= trail_activate:
        floor_sl = entry + (peak - giveback)
    else:
        floor_sl = 0
    state["current_floor"] = round(floor_sl, 2) if peak >= trail_activate else 0

    exits = []

    if lot1_active:
        lot1_trigger = entry - lot1_sl_pts
        if peak >= trail_activate:
            lot1_trigger = max(lot1_trigger, floor_sl)
        if option_ltp <= lot1_trigger:
            reason = "TRAIL_FLOOR" if peak >= trail_activate else "SCOUT_SL"
            logger.info("[ENGINE] LOT1 " + reason
                + ": running=" + str(running)
                + " trigger=₹" + str(round(lot1_trigger, 2)))
            exits.append({"lot_id": "LOT1", "reason": reason,
                          "price": option_ltp, "qty": D.LOT_SIZE})

    if lot2_active:
        lot2_trigger = entry - lot2_sl_pts
        if peak >= trail_activate:
            lot2_trigger = max(lot2_trigger, floor_sl)
        if option_ltp <= lot2_trigger:
            reason = "TRAIL_FLOOR" if peak >= trail_activate else "HARD_SL"
            logger.info("[ENGINE] LOT2 " + reason
                + ": running=" + str(running)
                + " trigger=₹" + str(round(lot2_trigger, 2)))
            exits.append({"lot_id": "LOT2", "reason": reason,
                          "price": option_ltp, "qty": D.LOT_SIZE})

    return exits


