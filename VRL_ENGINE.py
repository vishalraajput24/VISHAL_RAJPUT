# ═══════════════════════════════════════════════════════════════
#  VRL_ENGINE.py — VISHAL RAJPUT TRADE v13.6
#  Minimal EMA9 strategy. Close-based entry + divergence gate.
#  Two paths: FAST (2 green 1m above EMA9) or CONFIRMED (1 green 3m above EMA9).
#  Exit: EMERGENCY_SL → STALE → DIVERGENCE → CANDLE_SL → static profit floors.
# ═══════════════════════════════════════════════════════════════

import logging
import os
from datetime import datetime
import pandas as pd
import VRL_DATA as D
import VRL_CONFIG as CFG

logger = logging.getLogger("vrl_live")

# ═══════════════════════════════════════════════════════════════
#  DASHBOARD UTILITY (legacy — used by /spot, kept for back-compat)
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

            # v13.6: single same-direction cooldown, opposite direction always allowed
            cd_cfg = CFG.get().get("cooldown", {})
            same_dir_cd = cd_cfg.get("same_direction", 5)

            if direction and last_dir and direction == last_dir:
                if elapsed < same_dir_cd:
                    remaining = round(same_dir_cd - elapsed, 1)
                    logger.info("[ENGINE] COOLDOWN " + str(remaining)
                                + "min remaining (same dir=" + direction + ")")
                    return False, "Cooldown: " + str(remaining) + "min (same dir)"
            # Opposite direction or first trade: no cooldown
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
#  v13.6 ENTRY — EMA9 close-based, two paths
# ═══════════════════════════════════════════════════════════════

def _new_result(option_type: str) -> dict:
    """Canonical v13.6 result dict. Legacy keys kept at default for dashboard
    back-compat (they stay 0/False so the new HTML ignores them cleanly)."""
    return {
        # v13.6 core fields
        "fired": False,
        "option_type": option_type,
        "entry_mode": "",
        "entry_price": 0,
        "ema9": 0,
        "rsi": 0,
        "rsi_prev": 0,
        "rsi_rising": False,
        "candle_green": False,
        "close_above_ema": False,
        "two_green_above": False,
        "other_below_ema": False,
        "other_rsi_dropping": False,
        "path_fast": False,
        "path_conf": False,
        "verdict": "NO DATA",
        # Legacy fields kept at defaults so old dashboard/log callers don't crash
        "ema21": 0, "ema_gap": 0,
        "ema_ok": False, "rsi_ok": False,
        "gap_widening": False, "higher_low": False,
        "momentum_pts": 0, "momentum_tf": "",
        "momentum_threshold": 0,
        "ema_would_fire": False, "path_a": False, "path_b": False,
        "spot_confirms": False, "spot_move": 0,
        "spike_ratio": 0,
        "other_falling": False, "other_move": 0,
    }


def check_entry(token: int, option_type: str, spot_ltp: float = 0,
                dte: int = 99, expiry_date=None, kite=None,
                other_token: int = 0, silent: bool = False) -> dict:
    """v13.6: EMA9 close-based entry.

    FAST path (1-min):
      • Last 2 closed candles both green (close > open)
      • Both candles close above EMA9
      • RSI rising (curr > prev)
      • Other side: last 1m candle close BELOW EMA9 AND RSI dropping

    CONFIRMED path (3-min):
      • Last closed 3m candle is green
      • Closes above EMA9
      • Other side: last 3m candle is red OR closes below EMA9

    FAST takes priority when both fire.
    """
    result = _new_result(option_type)
    try:
        cfg = CFG.get().get("entry", {})
        rsi_max = cfg.get("rsi_max", 72)

        # ═══ 1-MIN DATA ═══
        df_1m = D.get_historical_data(token, "minute", 50)
        df_1m = D.add_indicators(df_1m)
        if df_1m.empty or len(df_1m) < 4:
            return result

        curr_1m = df_1m.iloc[-2]
        prev_1m = df_1m.iloc[-3]

        entry_price = float(curr_1m["close"])
        curr_open = float(curr_1m["open"])
        prev_close = float(prev_1m["close"])
        prev_open = float(prev_1m["open"])
        rsi = float(curr_1m.get("RSI", 50))
        rsi_prev = float(prev_1m.get("RSI", 50))
        rsi_rising = rsi > rsi_prev
        ema9 = float(curr_1m.get("EMA_9", 0))
        prev_ema9 = float(prev_1m.get("EMA_9", 0))

        curr_green = entry_price > curr_open
        prev_green = prev_close > prev_open
        curr_above_ema = ema9 > 0 and entry_price > ema9
        prev_above_ema = prev_ema9 > 0 and prev_close > prev_ema9
        two_green_above = curr_green and prev_green and curr_above_ema and prev_above_ema

        result.update({
            "entry_price": round(entry_price, 2),
            "ema9": round(ema9, 2),
            "rsi": round(rsi, 1),
            "rsi_prev": round(rsi_prev, 1),
            "rsi_rising": rsi_rising,
            "candle_green": curr_green,
            "close_above_ema": curr_above_ema,
            "two_green_above": two_green_above,
        })

        # RSI HARD CAP — blocks every path
        if rsi > rsi_max:
            result["verdict"] = "RSI cap " + str(round(rsi, 1)) + " > " + str(rsi_max)
            if not silent:
                logger.info("[ENGINE] " + option_type + " RSI_CAP " + str(round(rsi, 1)))
            return result

        # ═══ OTHER SIDE ANALYSIS (divergence gate) ═══
        other_below_ema_1m = False
        other_rsi_dropping = False
        other_red_3m = False
        other_below_ema_3m = False
        if other_token:
            try:
                other_df_1m = D.get_historical_data(other_token, "minute", 10)
                other_df_1m = D.add_indicators(other_df_1m)
                if not other_df_1m.empty and len(other_df_1m) >= 3:
                    o_curr = other_df_1m.iloc[-2]
                    o_prev = other_df_1m.iloc[-3]
                    o_close = float(o_curr["close"])
                    o_ema9 = float(o_curr.get("EMA_9", 0))
                    o_rsi = float(o_curr.get("RSI", 50))
                    o_rsi_prev = float(o_prev.get("RSI", 50))
                    other_below_ema_1m = o_ema9 > 0 and o_close < o_ema9
                    other_rsi_dropping = o_rsi < o_rsi_prev
            except Exception:
                pass

            try:
                other_df_3m = D.get_historical_data(other_token, "3minute", 10)
                other_df_3m = D.add_indicators(other_df_3m)
                if not other_df_3m.empty and len(other_df_3m) >= 2:
                    o3 = other_df_3m.iloc[-2]
                    o3_close = float(o3["close"])
                    o3_open = float(o3["open"])
                    o3_ema9 = float(o3.get("EMA_9", 0))
                    other_red_3m = o3_close < o3_open
                    other_below_ema_3m = o3_ema9 > 0 and o3_close < o3_ema9
            except Exception:
                pass

        result["other_below_ema"] = other_below_ema_1m
        result["other_rsi_dropping"] = other_rsi_dropping

        # ═══ FAST PATH (1-min) ═══
        path_fast = (two_green_above and rsi_rising
                     and other_below_ema_1m and other_rsi_dropping)
        result["path_fast"] = path_fast
        result["path_a"] = path_fast  # legacy alias for dashboard/tests

        # ═══ CONFIRMED PATH (3-min) ═══
        path_conf = False
        try:
            df_3m = D.get_historical_data(token, "3minute", 30)
            df_3m = D.add_indicators(df_3m)
            if not df_3m.empty and len(df_3m) >= 2:
                curr_3m = df_3m.iloc[-2]
                c3_close = float(curr_3m["close"])
                c3_open = float(curr_3m["open"])
                c3_ema9 = float(curr_3m.get("EMA_9", 0))
                c3_rsi = float(curr_3m.get("RSI", 50))
                c3_green = c3_close > c3_open
                c3_above_ema = c3_ema9 > 0 and c3_close > c3_ema9
                other_bad_3m = other_red_3m or other_below_ema_3m
                path_conf = (c3_green and c3_above_ema
                             and other_bad_3m and c3_rsi <= rsi_max)
        except Exception:
            pass
        result["path_conf"] = path_conf
        result["path_b"] = path_conf  # legacy alias

        # ═══ FIRE LOGIC ═══
        if path_fast:
            result["fired"] = True
            result["entry_mode"] = "FAST"
            result["verdict"] = "FIRED [FAST]"
            if not silent:
                logger.info("[ENGINE] " + option_type
                            + " FAST close=" + str(round(entry_price, 1))
                            + " ema9=" + str(round(ema9, 1))
                            + " rsi=" + str(round(rsi, 1)) + "↑"
                            + " other_below=✓ other_rsi↓=✓")
        elif path_conf:
            result["fired"] = True
            result["entry_mode"] = "CONFIRMED"
            result["verdict"] = "FIRED [CONFIRMED]"
            if not silent:
                logger.info("[ENGINE] " + option_type
                            + " CONFIRMED 3m close=" + str(round(entry_price, 1))
                            + " ema9=" + str(round(ema9, 1))
                            + " rsi=" + str(round(rsi, 1))
                            + " other_red/below=✓")
        else:
            # Build verdict with reason
            reasons = []
            if not curr_above_ema:
                reasons.append("close<ema9")
            elif not two_green_above:
                reasons.append("need 2 green above")
            if not rsi_rising:
                reasons.append("RSI↓")
            if other_token and not other_below_ema_1m:
                reasons.append("other above ema9")
            if other_token and not other_rsi_dropping:
                reasons.append("other RSI↑")
            result["verdict"] = "WAIT · " + ", ".join(reasons) if reasons else "WAIT"
            if not silent:
                logger.info("[ENGINE] " + option_type
                            + " WAIT close=" + str(round(entry_price, 1))
                            + " ema9=" + str(round(ema9, 1))
                            + " rsi=" + str(round(rsi, 1))
                            + (" ↑" if rsi_rising else " ↓")
                            + (" 2G✓" if two_green_above else " 2GX")
                            + (" other_below✓" if other_below_ema_1m else " other_above"))
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
#  v13.6 EXIT — simplified priority chain
#  1. EMERGENCY_SL   (-20 running)
#  2. STALE_ENTRY    (5 candles + peak < 3)
#  3. DIVERGENCE_EXIT (other 2 green + RSI rising, peak >= 6)
#  4. CANDLE_SL      (running <= -12, close-based)
#  5. PROFIT_FLOOR   (static: peak >=10→entry+2, >=20→+12, >=30→+22, >=40→+32)
# ═══════════════════════════════════════════════════════════════

def manage_exit(state: dict, option_ltp: float, profile: dict,
                other_token: int = 0) -> list:
    """v13.6: clean priority chain. No RSI blowoff, no spike logic, no dynamic trail."""
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

    exit_cfg = CFG.get().get("exit", {})
    hard_sl = exit_cfg.get("hard_sl", exit_cfg.get("candle_close_sl", 12))
    emergency_sl = exit_cfg.get("emergency_sl", exit_cfg.get("max_sl", 20))
    stale_candles = exit_cfg.get("stale_candles", 5)
    stale_peak = exit_cfg.get("stale_peak", exit_cfg.get("stale_peak_min", 3))
    divergence_peak = exit_cfg.get("divergence_peak", 6)

    # RSI snapshot (still surfaced for dashboard display, not used for exits)
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

    # ═══ OTHER SIDE REVERSAL CHECK ═══
    other_reversing = False
    if other_token:
        try:
            other_df = D.get_historical_data(other_token, "3minute", 10)
            other_df = D.add_indicators(other_df) if hasattr(D, "add_indicators") else other_df
            if other_df is not None and not other_df.empty and len(other_df) >= 4:
                o_curr = other_df.iloc[-2]
                o_prev = other_df.iloc[-3]
                o_curr_green = float(o_curr["close"]) > float(o_curr["open"])
                o_prev_green = float(o_prev["close"]) > float(o_prev["open"])
                o_rsi = float(o_curr.get("RSI", 50))
                o_rsi_prev = float(o_prev.get("RSI", 50))
                o_rsi_rising = o_rsi > o_rsi_prev
                other_reversing = o_curr_green and o_prev_green and o_rsi_rising
        except Exception:
            pass
    state["other_reversing"] = other_reversing

    # ═══ 1. EMERGENCY_SL ═══
    if running <= -emergency_sl:
        logger.info("[ENGINE] EMERGENCY_SL: running=" + str(running))
        return [{"lot_id": "ALL", "reason": "EMERGENCY_SL", "price": option_ltp}]

    # ═══ 2. STALE_ENTRY ═══
    if candles >= stale_candles and peak < stale_peak:
        logger.info("[ENGINE] STALE_ENTRY: " + str(candles) + "c peak=" + str(peak))
        return [{"lot_id": "ALL", "reason": "STALE_ENTRY", "price": option_ltp}]

    # ═══ 3. DIVERGENCE_EXIT ═══
    if other_reversing and peak >= divergence_peak:
        logger.info("[ENGINE] DIVERGENCE_EXIT: other reversed, peak=" + str(round(peak, 1)))
        return [{"lot_id": "ALL", "reason": "DIVERGENCE_EXIT", "price": option_ltp}]

    # ═══ 4. CANDLE_SL ═══
    if running <= -hard_sl:
        logger.info("[ENGINE] CANDLE_SL: running=" + str(running))
        return [{"lot_id": "ALL", "reason": "CANDLE_SL", "price": option_ltp}]

    # ═══ 5. PROFIT_FLOOR (static) ═══
    floors = CFG.get().get("profit_floors", [
        {"peak": 10, "lock": 2},
        {"peak": 20, "lock": 12},
        {"peak": 30, "lock": 22},
        {"peak": 40, "lock": 32},
    ])
    applicable = None
    for f in floors:
        if peak >= f.get("peak", 0):
            applicable = f
    if applicable is not None:
        lock_pts = applicable.get("lock", 0)
        floor_sl = entry + lock_pts
        state["current_floor"] = round(floor_sl, 2)
        state["current_lock"] = round(lock_pts, 1)
        if option_ltp <= floor_sl:
            logger.info("[ENGINE] PROFIT_FLOOR: peak=" + str(round(peak, 1))
                        + " lock=+" + str(lock_pts)
                        + " floor=" + str(round(floor_sl, 2)))
            return [{"lot_id": "ALL", "reason": "PROFIT_FLOOR", "price": option_ltp}]

    return []
