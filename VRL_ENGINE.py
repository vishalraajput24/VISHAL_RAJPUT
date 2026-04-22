# ═══════════════════════════════════════════════════════════════
#  VRL_ENGINE.py — VISHAL RAJPUT TRADE v16.5
#  Vishal Close Trail: exit on candle close, 85% trail for peaks ≥25
# ═══════════════════════════════════════════════════════════════

import logging
from datetime import datetime
import pandas as pd
import VRL_DATA as D
import VRL_CONFIG as CFG

logger = logging.getLogger("vrl_live")

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
            if direction and last_dir and direction == last_dir and elapsed < cd_min:
                return False, "Cooldown: " + str(round(cd_min - elapsed, 1)) + "min"
        except:
            pass
    if state.get("in_trade"):                return False, "Already in trade"
    if not D.is_market_open():               return False, "Market closed"
    if not D.is_tick_live(D.NIFTY_SPOT_TOKEN): return False, "Spot tick stale"
    if option_ltp <= 0:                      return False, "Option LTP zero"
    if state.get("paused"):                  return False, "Bot paused"
    return True, ""

def loss_streak_gate(state: dict) -> bool:
    return True

def _evaluate_entry_gates_pure(opt_3m, option_type: str, spot_ltp: float, now, market_open: bool,
                               state: dict, straddle_delta, spot_vwap, spot_for_vwap: float,
                               atm_strike: int, silent: bool = False, other_opt_3m=None) -> dict:
    result = {
        "fired": False, "entry_price": 0, "entry_mode": "", "ema9_high": 0, "ema9_low": 0,
        "close": 0, "open": 0, "high": 0, "low": 0, "candle_green": False, "body_pct": 0,
        "band_width": 0, "reject_reason": "", "band_position": "", "straddle_delta": None,
        "backbone_status": "N/A",
    }
    try:
        body_min = CFG.entry_ema9_band("body_pct_min", 30)
        warmup_until = CFG.entry_ema9_band("warmup_until", "09:30")
        cutoff_after = CFG.entry_ema9_band("cutoff_after", "15:10")

        if opt_3m is None or opt_3m.empty or len(opt_3m) < 4:
            result["reject_reason"] = "insufficient_3m_data"
            return result

        last = opt_3m.iloc[-2]
        prev = opt_3m.iloc[-3]
        close = float(last["close"]); open_ = float(last["open"])
        high = float(last["high"]); low = float(last["low"])
        ema9_high = float(last.get("ema9_high", 0))
        ema9_low  = float(last.get("ema9_low", 0))
        prev_close = float(prev["close"])
        prev_ema9_high = float(prev.get("ema9_high", 0))

        result.update({
            "entry_price": round(close, 2), "ema9_high": round(ema9_high, 2),
            "ema9_low": round(ema9_low, 2), "close": round(close, 2), "open": round(open_, 2),
            "high": round(high, 2), "low": round(low, 2),
            "band_width": round(ema9_high - ema9_low, 2),
        })

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

        # ── GATE: Close > EMA9-low ──
        if close <= ema9_low:
            result["reject_reason"] = "close_below_ema9_low"
            return result

        # ── GATE: Minimum gap of 3 pts above EMA9-low ──
        if close - ema9_low < 3:
            result["reject_reason"] = "weak_breakout_gap_lt_3"
            return result

        # ── GATE: Green candle ──
        if close <= open_:
            result["reject_reason"] = "red_candle"
            return result
        result["candle_green"] = True

        # ── GATE: Body ≥ 30% ──
        candle_range = high - low
        body = abs(close - open_)
        body_pct = round((body / candle_range * 100) if candle_range > 0 else 0, 1)
        result["body_pct"] = body_pct
        if body_pct < body_min:
            result["reject_reason"] = f"weak_body_{int(body_pct)}pct"
            return result

        # ── GATE: Floor test (low within 3 pts of ema9_low) ──
        floor_gap = low - ema9_low
        if floor_gap > 3:
            result["reject_reason"] = f"no_floor_test_gap_{round(floor_gap,1)}"
            return result

        # ── GATE: Fresh breakout (previous close ≤ previous ema9_high) ──
        if prev_close > prev_ema9_high:
            result["reject_reason"] = "not_fresh_breakout"
            return result

        # ── All gates passed ──
        result["fired"] = True
        result["entry_mode"] = "EMA9_BREAKOUT"
        result["ema9h_confirmed"] = (close > ema9_high)
        if not silent:
            logger.info(f"[ENGINE] {option_type} FIRED close={round(close,1)} > ema9l={round(ema9_low,1)} gap={round(close-ema9_low,1)} body={int(body_pct)}%")
        return result

    except Exception as e:
        logger.error("[ENGINE] Entry error: " + str(e))
        result["reject_reason"] = "error_" + str(e)[:50]
        return result

def check_entry(token: int, option_type: str, spot_ltp: float = 0, dte: int = 99,
                expiry_date=None, kite=None, other_token: int = 0, silent: bool = False,
                state: dict = None) -> dict:
    if state is None: state = {}
    opt_3m = D.get_option_3min(token, lookback=15)
    market_open = D.is_market_open()
    now = datetime.now()
    atm_strike = D.resolve_atm_strike(spot_ltp) if spot_ltp else 0
    straddle_delta = None
    if CFG.straddle_filter("enabled", True) and atm_strike:
        try:
            straddle_delta = D.get_straddle_delta(atm_strike, lookback_minutes=int(CFG.straddle_filter("lookback_minutes", 15)))
        except: pass
    spot_vwap = D.get_spot_vwap() if CFG.vwap_bonus("enabled", True) else None
    spot_for_vwap = D.get_spot_ltp() or spot_ltp
    return _evaluate_entry_gates_pure(
        opt_3m=opt_3m, option_type=option_type, spot_ltp=spot_ltp, now=now,
        market_open=market_open, state=state, straddle_delta=straddle_delta,
        spot_vwap=spot_vwap, spot_for_vwap=spot_for_vwap, atm_strike=atm_strike,
        silent=silent, other_opt_3m=None)

def compute_entry_sl(entry_price: float, hard_sl: int = 12) -> float:
    return round(entry_price - hard_sl, 2)

def is_setup_building(token: int, direction: str) -> bool:
    return False

def compute_trail_sl(entry_price: float, peak_pnl: float,
                     direction: str = "") -> tuple:
    """Vishal Close Trail: 60% → 85% → 80%, exit on candle close."""
    if peak_pnl >= 40:
        sl = entry_price + peak_pnl * 0.80
        tier = "TRAIL_80"
    elif peak_pnl >= 25:
        sl = entry_price + peak_pnl * 0.85
        tier = "VISHAL_MAX"
    elif peak_pnl >= 10:
        sl = entry_price + peak_pnl * 0.60
        tier = "TRAIL_60"
    else:
        sl = entry_price - 10
        tier = "INITIAL"
    return round(sl, 2), tier

def check_profit_lock(state: dict, daily_pnl: float) -> bool:
    if state.get("profit_locked"): return False
    if daily_pnl >= D.PROFIT_LOCK_PTS:
        state["profit_locked"] = True
        logger.info("[ENGINE] Profit lock at " + str(round(daily_pnl,1)) + "pts")
        return True
    return False

def _evaluate_exit_chain_pure(state: dict, option_ltp: float, opt_3m_full, now, market_open: bool) -> list:
    if not state.get("in_trade"): return []
    entry = state.get("entry_price", 0)
    pnl = round(option_ltp - entry, 2)
    peak = max(state.get("peak_pnl", 0), pnl)
    state["peak_pnl"] = peak
    if pnl <= CFG.exit_ema9_band("emergency_sl_pts", -10):
        return [{"lot_id": "ALL", "reason": "EMERGENCY_SL", "price": option_ltp}]
    if market_open:
        eod_mins = int(CFG.exit_ema9_band("eod_exit_time", "15:20").replace(":", "")) // 100 * 60 + int(CFG.exit_ema9_band("eod_exit_time", "15:20")[-2:])
        if now.hour*60 + now.minute >= eod_mins:
            return [{"lot_id": "ALL", "reason": "EOD_EXIT", "price": option_ltp}]
    trail_sl, trail_tier = compute_trail_sl(entry, peak)
    state["active_ratchet_tier"] = trail_tier
    state["active_ratchet_sl"] = trail_sl
    if trail_sl > 0:
        last_close = opt_3m_full.iloc[-2]["close"] if opt_3m_full is not None and len(opt_3m_full) >= 2 else option_ltp
        if last_close <= trail_sl:
            return [{"lot_id": "ALL", "reason": "VISHAL_TRAIL", "price": trail_sl}]
    return []

def manage_exit(state: dict, option_ltp: float, profile: dict, other_token: int = 0) -> list:
    if not state.get("in_trade"): return []
    opt_3m_full = None
    try:
        opt_3m_full = D.get_option_3min(state.get("token"), lookback=10)
    except: pass
    return _evaluate_exit_chain_pure(state, option_ltp, opt_3m_full, datetime.now(), D.is_market_open())
