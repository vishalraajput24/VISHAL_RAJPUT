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

def check_entry(token: int, option_type: str, spot_ltp: float = 0,
                dte: int = 99, expiry_date=None, kite=None) -> dict:
    """v13.3: Momentum entry only. EMA is info."""
    result = {
        "fired": False, "option_type": option_type,
        "entry_price": 0, "ema9": 0, "ema21": 0,
        "ema_gap": 0, "rsi": 0, "rsi_prev": 0,
        "ema_ok": False, "rsi_ok": False,
        "candle_green": False, "gap_widening": False,
        "higher_low": False, "entry_mode": "", "momentum_pts": 0,
        "ema_would_fire": False, "path_a": False, "path_b": False,
    }
    try:
        df = D.get_historical_data(token, "minute", 20)
        df = D.add_indicators(df)
        if df.empty or len(df) < 5:
            return result
        curr = df.iloc[-2]
        prev = df.iloc[-3]
        entry_price = float(curr["close"])
        ema9 = float(curr.get("EMA_9", 0))
        ema21 = float(curr.get("EMA_21", 0))
        ema_gap = round(ema9 - ema21, 2)
        rsi = float(curr.get("RSI", 50))
        rsi_prev = float(prev.get("RSI", 50))
        result.update({"entry_price": round(entry_price, 2),
            "ema9": round(ema9, 2), "ema21": round(ema21, 2),
            "ema_gap": ema_gap, "rsi": round(rsi, 1), "rsi_prev": round(rsi_prev, 1)})

        import VRL_CONFIG as CFG
        cfg = CFG.get().get("entry", {})
        rsi_max = cfg.get("rsi_max", 72)
        # DTE-based momentum threshold
        if dte == 0:
            mom_pts_min = cfg.get("momentum_pts_dte0", 15)
        elif dte == 1:
            mom_pts_min = cfg.get("momentum_pts_dte1", 12)
        else:
            mom_pts_min = cfg.get("momentum_pts_dte2plus", 10)
        result["momentum_threshold"] = mom_pts_min
        mom_candles = cfg.get("momentum_candles", 3)
        mom_rsi_min = cfg.get("momentum_rsi_min", 45)
        ema_min = cfg.get("ema_gap_min", 3)

        if rsi > rsi_max:
            logger.info("[ENGINE] " + option_type + " RSI_CAP " + str(round(rsi, 1)))
            return result

        candle_green = float(curr["close"]) > float(curr["open"])
        higher_low = float(curr["low"]) > float(prev["low"])
        result["candle_green"] = candle_green
        result["higher_low"] = higher_low

        # EMA info (never fires alone)
        rsi_ok = rsi >= 50 and rsi > rsi_prev
        prev_e9 = float(prev.get("EMA_9", 0))
        prev_e21 = float(prev.get("EMA_21", 0))
        prev_gap = round(prev_e9 - prev_e21, 2)
        gap_widening = ema_gap > prev_gap
        ema_ok = ema_gap >= ema_min
        result["ema_ok"] = ema_ok
        result["rsi_ok"] = rsi_ok
        result["gap_widening"] = gap_widening
        path_ema = ema_ok and rsi_ok and candle_green and gap_widening and higher_low
        result["ema_would_fire"] = path_ema
        result["path_a"] = path_ema

        # MOMENTUM — the only entry trigger
        mom_pts = 0
        path_mom = False
        if len(df) >= mom_candles + 2:
            ref = float(df.iloc[-2 - mom_candles]["close"])
            mom_pts = round(entry_price - ref, 2)
            result["momentum_pts"] = mom_pts
            # Spike quality: how much of the move came from last candle
            last_candle_move = round(float(curr["close"]) - float(prev["close"]), 2)
            result["last_candle_move"] = last_candle_move
            spike_ratio = round(last_candle_move / mom_pts, 2) if mom_pts > 0 else 0
            result["spike_ratio"] = spike_ratio
            path_mom = (mom_pts >= mom_pts_min and rsi >= mom_rsi_min
                        and candle_green and higher_low)
        result["path_b"] = path_mom

        if path_mom:
            result["fired"] = True
            if path_ema:
                result["entry_mode"] = "CONFIRMED"
                logger.info("[ENGINE] " + option_type + " ENTRY [CONFIRMED ★★]"
                    + " mom=" + str(mom_pts) + "pts/" + str(mom_candles) + "c"
                    + (" spike" if spike_ratio > 0.6 else " steady")
                    + " ema=" + str(ema_gap) + " rsi=" + str(round(rsi, 1))
                    + " HL=Y entry=" + str(entry_price))
            else:
                result["entry_mode"] = "MOMENTUM"
                logger.info("[ENGINE] " + option_type + " ENTRY [MOMENTUM]"
                    + " mom=" + str(mom_pts) + "pts/" + str(mom_candles) + "c"
                    + (" spike" if spike_ratio > 0.6 else " steady")
                    + " rsi=" + str(round(rsi, 1))
                    + " HL=Y entry=" + str(entry_price))
        else:
            reasons = []
            if mom_pts < mom_pts_min: reasons.append("mom=" + str(mom_pts) + "/" + str(mom_pts_min) + "❌")
            else: reasons.append("mom=" + str(mom_pts) + "✅")
            if rsi < mom_rsi_min: reasons.append("rsi=" + str(round(rsi, 1)) + "❌")
            else: reasons.append("rsi=" + str(round(rsi, 1)) + "✅")
            if not candle_green: reasons.append("RED❌")
            if not higher_low: reasons.append("LOW↓❌")
            if path_ema: reasons.append("[EMA✅]")
            logger.info("[ENGINE] " + option_type + " " + " ".join(reasons))
        return result
    except Exception as e:
        logger.error("[ENGINE] check_entry error: " + str(e))
        return result



# ═══════════════════════════════════════════════════════════════
#  SL + PROFIT LOCK
# ═══════════════════════════════════════════════════════════════

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
#  v13.0 EXIT — PROFIT FLOORS + RSI SPLIT + ATR TRAIL (2-LOT)
# ═══════════════════════════════════════════════════════════════

def manage_exit(state: dict, option_ltp: float, profile: dict) -> list:
    """v13.3: Trailing floor. Both lots same path."""
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
        df = D.get_historical_data(token, "minute", 5)
        df = D.add_indicators(df)
        if not df.empty and len(df) >= 2:
            rsi = float(df.iloc[-2].get("RSI", 50))
    except Exception:
        pass
    state["current_rsi"] = round(rsi, 1)
    import VRL_CONFIG as CFG
    hard_sl = CFG.get().get("exit", {}).get("hard_sl", 12)
    stale_candles = CFG.get().get("exit", {}).get("stale_candles", 5)
    stale_peak = CFG.get().get("exit", {}).get("stale_peak_min", 3)
    rsi_blowoff = CFG.get().get("rsi_exit", {}).get("blowoff", 80)
    trail_cfg = CFG.get().get("profit_trail", {})
    trail_activate = trail_cfg.get("activate_at", 10)
    # STALE EXIT
    if candles >= stale_candles and peak < stale_peak:
        logger.info("[ENGINE] STALE_ENTRY: " + str(candles) + " candles, peak=" + str(peak))
        return [{"lots": "ALL", "lot_id": "ALL", "reason": "STALE_ENTRY", "price": option_ltp}]
    # HARD SL
    if running <= -hard_sl:
        logger.info("[ENGINE] HARD_SL: running=" + str(running))
        return [{"lots": "ALL", "lot_id": "ALL", "reason": "HARD_SL", "price": option_ltp}]
    # VARIABLE GIVEBACK by peak level
    if peak >= 30:
        giveback = trail_cfg.get("giveback_high", 6)
    elif peak >= 20:
        giveback = trail_cfg.get("giveback_mid", 7)
    else:
        giveback = trail_cfg.get("giveback_low", 8)
    state["current_giveback"] = giveback
    # TRAILING FLOOR
    if peak >= trail_activate:
        floor_sl = entry + (peak - giveback)
    else:
        floor_sl = entry - hard_sl
    state["current_floor"] = round(floor_sl, 2)
    # RSI BLOWOFF
    if rsi > rsi_blowoff:
        logger.info("[ENGINE] RSI_BLOWOFF: rsi=" + str(round(rsi, 1)))
        return [{"lots": "ALL", "lot_id": "ALL", "reason": "RSI_BLOWOFF", "price": option_ltp}]
    # TRAILING FLOOR HIT
    if peak >= trail_activate and option_ltp <= floor_sl:
        locked = round(peak - giveback, 1)
        logger.info("[ENGINE] TRAIL_FLOOR: peak=" + str(round(peak, 1))
                     + " locked=+" + str(locked) + " give=" + str(giveback)
                     + " floor=₹" + str(round(floor_sl, 2)))
        return [{"lots": "ALL", "lot_id": "ALL", "reason": "TRAIL_FLOOR", "price": option_ltp}]
    return []

