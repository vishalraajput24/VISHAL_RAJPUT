# ═══════════════════════════════════════════════════════════════
#  VRL_ENGINE.py — VISHAL RAJPUT TRADE v14.0
#  REBUILD: 3-min primary entry. 1-min noise scrapped.
#  Entry: RSI 40-55 + ADX≥15 + body≥20% + TRENDING regime + cooldown + cutoff
#  Exit: priority chain (emergency/stale/divergence/candle_sl/floors/trail) — UNCHANGED
# ═══════════════════════════════════════════════════════════════

import logging
import os
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
        if dte <= 1 and len(df) < 25:
            lookback_idx = min(5, len(df) - 2)
            ref_close = df.iloc[-2 - lookback_idx]["close"]
            return round(last["close"] - ref_close, 2)
        return round(last.get("EMA_9", last["close"]) - last.get("EMA_21", last["close"]), 2)
    except Exception as e:
        logger.warning("[ENGINE] EMA spread error: " + str(e))
        return 0.0


# ═══════════════════════════════════════════════════════════════
#  3-MIN INDICATOR HELPERS (v14.0)
# ═══════════════════════════════════════════════════════════════

def _compute_adx(df: pd.DataFrame, period: int = 14) -> float:
    """Compute ADX from OHLC DataFrame. Returns last closed candle's ADX."""
    try:
        import numpy as _np
        if df is None or df.empty or len(df) < period + 2:
            return 0.0
        _up = df["high"].diff()
        _dn = -df["low"].diff()
        _pdm = _np.where((_up > _dn) & (_up > 0), _up, 0.0)
        _ndm = _np.where((_dn > _up) & (_dn > 0), _dn, 0.0)
        _tr = pd.concat([df["high"] - df["low"],
                         (df["high"] - df["close"].shift(1)).abs(),
                         (df["low"] - df["close"].shift(1)).abs()], axis=1).max(axis=1)
        _atr = _tr.ewm(alpha=1.0 / period, adjust=False).mean()
        _pdi = 100 * pd.Series(_pdm, index=df.index).ewm(alpha=1.0 / period, adjust=False).mean() / _atr
        _ndi = 100 * pd.Series(_ndm, index=df.index).ewm(alpha=1.0 / period, adjust=False).mean() / _atr
        _adx = ((_pdi - _ndi).abs() / (_pdi + _ndi + 1e-9) * 100).ewm(alpha=1.0 / period, adjust=False).mean()
        return round(float(_adx.iloc[-2]), 1)
    except Exception:
        return 0.0


def _candle_body_pct(row) -> float:
    """Body as % of full candle range. Doji ≈ 0%, full marubozu = 100%."""
    try:
        h = float(row["high"])
        l = float(row["low"])
        o = float(row["open"])
        c = float(row["close"])
        rng = h - l
        if rng <= 0:
            return 0.0
        return round(abs(c - o) / rng * 100, 1)
    except Exception:
        return 0.0


# ═══════════════════════════════════════════════════════════════
#  PRE-ENTRY GUARDS (unchanged from v13.x)
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
            cd_cfg = CFG.get().get("cooldown", {})
            same_dir_cd = cd_cfg.get("same_direction", 5)
            if direction and last_dir and direction == last_dir:
                if elapsed < same_dir_cd:
                    remaining = round(same_dir_cd - elapsed, 1)
                    return False, "Cooldown: " + str(remaining) + "min (same dir)"
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
#  v14.0 ENTRY — 3-MIN PRIMARY
#
#  Six gates (all must pass):
#    1. 3-min RSI in [40, 55]
#    2. 3-min ADX ≥ 15
#    3. 3-min candle body ≥ 20% of range
#    4. Spot regime ∈ {TRENDING, TRENDING_STRONG}
#    5. Cooldown OK (5min same direction)
#    6. Time < 15:10 IST
#
#  15-min RSI is a CONFIDENCE LABEL only (HIGH / NORMAL), never a gate.
# ═══════════════════════════════════════════════════════════════

def check_entry(token: int, option_type: str, spot_ltp: float = 0,
                dte: int = 99, expiry_date=None, kite=None,
                other_token: int = 0, silent: bool = False,
                state: dict = None) -> dict:
    """v14.0: 3-min RSI 40-55 + ADX≥15 + body≥20% + TRENDING regime."""
    result = {
        "fired": False, "option_type": option_type,
        "entry_price": 0, "entry_mode": "",
        "rsi_3m": 0, "adx_3m": 0, "body_pct_3m": 0,
        "rsi_15m": 0, "confidence_15m": "NORMAL",
        "regime": "", "cooldown_ok": False,
        "reject_reason": "",
        # Legacy compat fields (downstream code reads these — keep at defaults)
        "ema9": 0, "ema21": 0, "ema_gap": 0, "rsi": 0, "rsi_prev": 0,
        "ema_ok": False, "rsi_ok": False, "candle_green": False,
        "gap_widening": False, "rsi_rising": False,
        "other_falling": False, "other_move": 0,
        "spot_aligned": False, "two_green_above": False,
        "other_below_ema": False, "rsi_cap_active": 0,
        "breakout_confirmed": False, "spot_slope": 0,
        "path_a": False, "path_b": False,
        "momentum_pts": 0, "momentum_tf": "", "momentum_threshold": 0,
        "spike_ratio": 0, "spot_confirms": False, "spot_move": 0,
    }
    if state is None:
        state = {}
    try:
        cfg = CFG.get().get("entry_3min", {})
        rsi_min = cfg.get("rsi_min", 40)
        rsi_max = cfg.get("rsi_max", 55)
        adx_min = cfg.get("adx_min", 15)
        body_min = cfg.get("body_pct_min", 20)
        allowed_regimes = cfg.get("allowed_regimes", ["TRENDING", "TRENDING_STRONG"])

        # ── 1. Fetch 3-min option candle ──
        df_3m = D.get_historical_data(token, "3minute", 30)
        df_3m = D.add_indicators(df_3m)
        if df_3m is None or df_3m.empty or len(df_3m) < 16:
            result["reject_reason"] = "insufficient_3m_data"
            return result

        last_3m = df_3m.iloc[-2]  # last CLOSED 3-min candle
        entry_price = float(last_3m["close"])
        rsi_3m = float(last_3m.get("RSI", 0))
        adx_3m = _compute_adx(df_3m)
        body_pct = _candle_body_pct(last_3m)

        result["entry_price"] = round(entry_price, 2)
        result["rsi_3m"] = round(rsi_3m, 1)
        result["adx_3m"] = adx_3m
        result["body_pct_3m"] = body_pct
        # Legacy fields for dashboard back-compat
        result["rsi"] = round(rsi_3m, 1)
        result["ema9"] = round(float(last_3m.get("EMA_9", 0)), 2)
        result["ema21"] = round(float(last_3m.get("EMA_21", 0)), 2)
        result["candle_green"] = entry_price > float(last_3m["open"])

        # ── 2. RSI 40-55 zone ──
        if not (rsi_min <= rsi_3m <= rsi_max):
            result["reject_reason"] = "rsi_out_of_zone_" + str(round(rsi_3m, 0))
            if not silent:
                logger.info("[ENGINE] " + option_type + " rsi=" + str(round(rsi_3m, 1))
                            + " not in " + str(rsi_min) + "-" + str(rsi_max) + " zone")
            return result

        # ── 3. ADX ≥ 15 ──
        if adx_3m < adx_min:
            result["reject_reason"] = "adx_too_low_" + str(round(adx_3m, 0))
            if not silent:
                logger.info("[ENGINE] " + option_type + " adx=" + str(adx_3m)
                            + " < " + str(adx_min) + " (no trend)")
            return result

        # ── 4. Body ≥ 20% ──
        if body_pct < body_min:
            result["reject_reason"] = "weak_body_" + str(round(body_pct, 0))
            if not silent:
                logger.info("[ENGINE] " + option_type + " body=" + str(body_pct)
                            + "% < " + str(body_min) + " (doji/weak)")
            return result

        # ── 5. Spot regime ──
        try:
            regime = D.compute_spot_regime() if hasattr(D, "compute_spot_regime") else ""
        except Exception:
            regime = ""
        result["regime"] = regime
        if regime not in allowed_regimes:
            result["reject_reason"] = "regime_" + str(regime or "UNKNOWN")
            if not silent:
                logger.info("[ENGINE] " + option_type + " regime=" + str(regime)
                            + " not in " + str(allowed_regimes))
            return result

        # ── 6. Cooldown (5min same direction) ──
        last_exit_ts = state.get("last_exit_time")
        last_exit_dir = state.get("last_exit_direction", "")
        if last_exit_ts and last_exit_dir == option_type:
            try:
                elapsed = (datetime.now() - datetime.fromisoformat(last_exit_ts)).total_seconds() / 60
                if elapsed < 5:
                    result["reject_reason"] = "cooldown_" + str(round(5 - elapsed, 1)) + "min"
                    if not silent:
                        logger.info("[ENGINE] " + option_type + " cooldown "
                                    + str(round(5 - elapsed, 1)) + "min remaining")
                    return result
            except Exception:
                pass
        result["cooldown_ok"] = True

        # ── 7. Entry cutoff 15:10 IST (only when market is live) ──
        now = datetime.now()
        if D.is_market_open():
            if now.hour > 15 or (now.hour == 15 and now.minute >= 10):
                result["reject_reason"] = "after_15:10_cutoff"
                if not silent:
                    logger.info("[ENGINE] " + option_type + " entry cutoff 15:10")
                return result

        # ── 8. 15-min RSI confidence label (NOT a gate) ──
        try:
            df_15m = D.get_historical_data(token, "15minute", 10)
            df_15m = D.add_indicators(df_15m)
            if df_15m is not None and not df_15m.empty and len(df_15m) >= 2:
                rsi_15m = float(df_15m.iloc[-2].get("RSI", 0))
                result["rsi_15m"] = round(rsi_15m, 1)
                if option_type == "CE" and 30 <= rsi_15m <= 50:
                    result["confidence_15m"] = "HIGH"
                elif option_type == "PE" and 50 <= rsi_15m <= 70:
                    result["confidence_15m"] = "HIGH"
                else:
                    result["confidence_15m"] = "NORMAL"
        except Exception:
            result["confidence_15m"] = "UNKNOWN"

        # ═══ ALL CHECKS PASSED — FIRE ═══
        result["fired"] = True
        result["entry_mode"] = "3MIN"
        if not silent:
            logger.info("[ENGINE] " + option_type + " ENTRY [3MIN]"
                        + " rsi=" + str(round(rsi_3m, 1))
                        + " adx=" + str(adx_3m)
                        + " body=" + str(round(body_pct, 0)) + "%"
                        + " regime=" + str(regime)
                        + " conf_15m=" + result["confidence_15m"]
                        + " rsi15m=" + str(result["rsi_15m"])
                        + " entry=" + str(round(entry_price, 2)))
        return result

    except Exception as e:
        logger.error("[ENGINE] check_entry error: " + str(e))
        result["reject_reason"] = "error_" + str(e)[:50]
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
#  EXIT CHAIN (unchanged from v13.x — proven, kept verbatim)
#  EMERGENCY → STALE → BLOWOFF → DIVERGENCE → SPIKE → CANDLE_SL
#  → STATIC PROFIT FLOORS → DYNAMIC TRAIL
# ═══════════════════════════════════════════════════════════════

def manage_exit(state: dict, option_ltp: float, profile: dict,
                other_token: int = 0) -> list:
    """Priority chain. Static profit floors persist to state."""
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
    entry_mode = state.get("entry_mode", "3MIN")
    min_lock = trail_cfg.get("min_lock", 2)
    trail_activate = trail_cfg.get("activate_at", 15)
    keep_normal = trail_cfg.get("keep_normal", 0.75)
    keep_warning = trail_cfg.get("keep_warning", 0.85)

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

    # 6b. STATIC PROFIT FLOORS (persist to state)
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
