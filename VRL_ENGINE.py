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
    """v13.2 entry — EMA gap OR momentum. Two paths, same trade."""
    result = {
        "fired": False, "option_type": option_type,
        "entry_price": 0, "ema9": 0, "ema21": 0,
        "ema_gap": 0, "rsi": 0, "rsi_prev": 0,
        "ema_ok": False, "rsi_ok": False,
        "candle_green": False, "gap_widening": False,
        "entry_mode": "", "momentum_pts": 0,
        "path_a": False, "path_b": False,
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

        result.update({
            "entry_price": round(entry_price, 2),
            "ema9": round(ema9, 2), "ema21": round(ema21, 2),
            "ema_gap": ema_gap, "rsi": round(rsi, 1),
            "rsi_prev": round(rsi_prev, 1),
        })

        import VRL_CONFIG as CFG
        entry_cfg = CFG.get().get("entry", {})
        ema_min = entry_cfg.get("ema_gap_min", 3)
        rsi_min = entry_cfg.get("rsi_min", 50)
        rsi_max = entry_cfg.get("rsi_max", 72)

        # RSI HARD CAP — blocks BOTH paths
        if rsi > rsi_max:
            result["ema_ok"] = ema_gap >= ema_min
            result["rsi_ok"] = False
            logger.info("[ENGINE] RSI_BLOWOFF_ENTRY: rsi=" + str(round(rsi, 1))
                        + " > " + str(rsi_max) + " — BLOCKED")
            return result

        # ═══ PATH A: EMA GAP (existing — unchanged) ═══
        ema_ok = ema_gap >= ema_min
        rsi_ok = rsi >= rsi_min and rsi > rsi_prev
        candle_green = float(curr["close"]) > float(curr["open"])
        prev_ema9 = float(prev.get("EMA_9", 0))
        prev_ema21 = float(prev.get("EMA_21", 0))
        prev_gap = round(prev_ema9 - prev_ema21, 2)
        gap_widening = ema_gap > prev_gap

        result["ema_ok"] = ema_ok
        result["rsi_ok"] = rsi_ok
        result["candle_green"] = candle_green
        result["gap_widening"] = gap_widening

        path_a = ema_ok and rsi_ok and candle_green and gap_widening
        result["path_a"] = path_a

        # ═══ PATH B: MOMENTUM (catches V-reversals) ═══
        momentum_pts_min = entry_cfg.get("momentum_pts", 15)
        momentum_candles = entry_cfg.get("momentum_candles", 3)
        momentum_rsi_min = entry_cfg.get("momentum_rsi_min", 45)

        path_b = False
        momentum_pts = 0
        if len(df) >= momentum_candles + 2:
            ref_close = float(df.iloc[-2 - momentum_candles]["close"])
            momentum_pts = round(entry_price - ref_close, 2)
            result["momentum_pts"] = momentum_pts
            path_b = (momentum_pts >= momentum_pts_min
                      and rsi >= momentum_rsi_min
                      and candle_green)
        result["path_b"] = path_b

        # ═══ FIRE if EITHER path passes ═══
        if path_a and path_b:
            result["fired"] = True
            result["entry_mode"] = "BOTH"
            logger.info("[ENGINE] " + option_type + " ENTRY [BOTH]"
                + " ema=" + str(ema_gap) + " mom=+" + str(momentum_pts)
                + " rsi=" + str(round(rsi, 1)) + " entry=" + str(entry_price))
        elif path_a:
            result["fired"] = True
            result["entry_mode"] = "EMA"
            logger.info("[ENGINE] " + option_type + " ENTRY [EMA]"
                + " ema_gap=" + str(ema_gap) + "(w:" + str(prev_gap) + ")"
                + " rsi=" + str(round(rsi, 1)) + ">" + str(round(rsi_prev, 1))
                + " entry=" + str(entry_price))
        elif path_b:
            result["fired"] = True
            result["entry_mode"] = "MOMENTUM"
            logger.info("[ENGINE] " + option_type + " ENTRY [MOMENTUM]"
                + " +" + str(momentum_pts) + "pts/" + str(momentum_candles) + "c"
                + " rsi=" + str(round(rsi, 1)) + " entry=" + str(entry_price))
        else:
            reasons = []
            if not ema_ok: reasons.append("ema=" + str(ema_gap) + "❌")
            else: reasons.append("ema=" + str(ema_gap) + "✅")
            if not rsi_ok: reasons.append("rsi=" + str(round(rsi, 1)) + "❌")
            else: reasons.append("rsi=" + str(round(rsi, 1)) + "✅")
            if not candle_green: reasons.append("RED❌")
            if not gap_widening: reasons.append("gap_shrink❌")
            if momentum_pts > 10:
                reasons.append("mom=" + str(momentum_pts) + "pts")
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
    """v13.3 exit — both lots same path. No split, no ATR trail."""
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

    # STALE EXIT
    if candles >= stale_candles and peak < stale_peak:
        logger.info("[ENGINE] STALE_ENTRY: " + str(candles) + " candles, peak=" + str(peak))
        return [{"lots": "ALL", "lot_id": "ALL", "reason": "STALE_ENTRY", "price": option_ltp}]

    # HARD SL
    if running <= -hard_sl:
        logger.info("[ENGINE] HARD_SL: running=" + str(running))
        return [{"lots": "ALL", "lot_id": "ALL", "reason": "HARD_SL", "price": option_ltp}]

    # PROFIT FLOOR
    floors = CFG.get().get("profit_floors", [])
    floor_sl = entry - hard_sl
    for f in sorted(floors, key=lambda x: x["peak"]):
        if peak >= f["peak"]:
            floor_sl = entry + f["lock"]
    state["current_floor"] = round(floor_sl, 2)

    # RSI BLOWOFF > 80
    if rsi > rsi_blowoff:
        logger.info("[ENGINE] RSI_BLOWOFF: rsi=" + str(round(rsi, 1)))
        return [{"lots": "ALL", "lot_id": "ALL", "reason": "RSI_BLOWOFF", "price": option_ltp}]

    # PROFIT FLOOR HIT
    if option_ltp <= floor_sl and peak >= 10:
        logger.info("[ENGINE] PROFIT_FLOOR: at " + str(round(floor_sl, 2)) + " peak=" + str(round(peak, 1)))
        return [{"lots": "ALL", "lot_id": "ALL", "reason": "PROFIT_FLOOR", "price": option_ltp}]

    return []


# v13.1: DTE=0 uses same check_entry() as regular days. No special expiry mode.
