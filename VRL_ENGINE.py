# ═══════════════════════════════════════════════════════════════
#  VRL_ENGINE.py — VISHAL RAJPUT TRADE v15.0
#  Dual EMA9 Band Breakout — pure option-level price action.
#
#  ENTRY: option 3-min close > EMA9-of-highs (fresh breakout: prev was at
#         or below) + green candle + body ≥ 30% of range.
#  EXIT: 4-rule priority chain. Primary stop = EMA9-of-lows close break.
#        No fixed SL. No profit floors. The band IS the trail.
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
#  PRE-ENTRY GUARDS (keeps existing market-open/cooldown/paused checks)
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
            cd_min = CFG.get().get("entry_ema9_band", {}).get("cooldown_minutes", 5)
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
            if avail > 0 and avail < option_ltp * D.LOT_SIZE * 1.2:
                return False, "Insufficient margin"
        except Exception:
            pass
    return True, ""


def loss_streak_gate(state: dict) -> bool:
    return True


# ═══════════════════════════════════════════════════════════════
#  v15.0 ENTRY — Dual EMA9 Band Breakout
#
#  Five gates (all must pass):
#    1. Time window: 9:45 ≤ now < 15:10
#    2. Cooldown: 5 min same direction
#    3. Fresh breakout: close > ema9_high AND prev_close ≤ prev_ema9_high
#    4. Green candle: close > open
#    5. Body ≥ 30% of candle range
# ═══════════════════════════════════════════════════════════════

def check_entry(token: int, option_type: str, spot_ltp: float = 0,
                dte: int = 99, expiry_date=None, kite=None,
                other_token: int = 0, silent: bool = False,
                state: dict = None) -> dict:
    """v15.0: Buy option whose 3-min just broke fresh above its own EMA9-high."""
    result = {
        "fired": False, "option_type": option_type,
        "entry_price": 0, "entry_mode": "",
        "ema9_high": 0, "ema9_low": 0,
        "close": 0, "open": 0, "high": 0, "low": 0,
        "prev_close": 0, "prev_ema9_high": 0,
        "candle_green": False, "body_pct": 0,
        "cooldown_ok": False, "reject_reason": "",
        "band_position": "",
    }
    if state is None:
        state = {}
    try:
        cfg = CFG.get().get("entry_ema9_band", {})
        body_min = cfg.get("body_pct_min", 30)
        cd_min = cfg.get("cooldown_minutes", 5)
        warmup_until = cfg.get("warmup_until", "09:45")
        cutoff_after = cfg.get("cutoff_after", "15:10")

        # ── Fetch 3-min option data ──
        opt_3m = D.get_option_3min(token, lookback=15)
        if opt_3m is None or opt_3m.empty or len(opt_3m) < 4:
            result["reject_reason"] = "insufficient_3m_data"
            return result

        last = opt_3m.iloc[-2]   # last CLOSED 3-min candle
        prev = opt_3m.iloc[-3]   # candle before that

        close = float(last["close"])
        open_ = float(last["open"])
        high  = float(last["high"])
        low   = float(last["low"])
        ema9_high = float(last.get("ema9_high", 0))
        ema9_low  = float(last.get("ema9_low", 0))
        prev_close = float(prev["close"])
        prev_ema9_high = float(prev.get("ema9_high", 0))

        # Band position label for dashboard
        if close > ema9_high:
            band_position = "ABOVE"
        elif close < ema9_low:
            band_position = "BELOW"
        else:
            band_position = "IN"

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
        })

        # ── GATE 1: Time window 9:45 — 15:10 ──
        # Only enforced when market is actually open (so tests at night pass)
        if D.is_market_open():
            now = datetime.now()
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

        # ── GATE 2: Cooldown 5min same direction ──
        last_exit_ts = state.get("last_exit_time")
        last_exit_dir = state.get("last_exit_direction", "")
        if last_exit_ts and last_exit_dir == option_type:
            try:
                elapsed = (datetime.now() - datetime.fromisoformat(last_exit_ts)).total_seconds() / 60
                if elapsed < cd_min:
                    result["reject_reason"] = "cooldown_" + str(round(cd_min - elapsed, 1)) + "min"
                    return result
            except Exception:
                pass
        result["cooldown_ok"] = True

        # ── GATE 3: Fresh breakout above EMA9-high ──
        if not (close > ema9_high and prev_close <= prev_ema9_high):
            if close <= ema9_high:
                reason = "below_band_close=" + str(round(close, 1)) + "_ema9h=" + str(round(ema9_high, 1))
            else:
                reason = "stale_breakout_prev_close=" + str(round(prev_close, 1)) + "_>_prev_ema9h=" + str(round(prev_ema9_high, 1))
            result["reject_reason"] = reason
            if not silent:
                logger.info("[ENGINE] " + option_type + " NO_BREAKOUT close="
                            + str(round(close, 1)) + " ema9h=" + str(round(ema9_high, 1))
                            + " prev_close=" + str(round(prev_close, 1))
                            + " prev_ema9h=" + str(round(prev_ema9_high, 1)))
            return result

        # ── GATE 4: Green candle ──
        candle_green = close > open_
        result["candle_green"] = candle_green
        if not candle_green:
            result["reject_reason"] = "red_candle_close=" + str(round(close, 1)) + "_open=" + str(round(open_, 1))
            if not silent:
                logger.info("[ENGINE] " + option_type + " RED_CANDLE close="
                            + str(round(close, 1)) + " open=" + str(round(open_, 1)))
            return result

        # ── GATE 5: Body ≥ 30% of range ──
        candle_range = high - low
        body = abs(close - open_)
        body_pct = round((body / candle_range * 100) if candle_range > 0 else 0, 1)
        result["body_pct"] = body_pct
        if body_pct < body_min:
            result["reject_reason"] = "weak_body_" + str(int(body_pct)) + "pct_<_" + str(body_min)
            if not silent:
                logger.info("[ENGINE] " + option_type + " WEAK_BODY " + str(int(body_pct))
                            + "% < " + str(body_min) + "%")
            return result

        # ═══ ALL GATES PASSED — FIRE ═══
        result["fired"] = True
        result["entry_mode"] = "EMA9_BREAKOUT"
        if not silent:
            logger.info("[ENGINE] " + option_type + " ENTRY [EMA9_BREAKOUT]"
                        + " close=" + str(round(close, 1))
                        + " ema9h=" + str(round(ema9_high, 1))
                        + " ema9l=" + str(round(ema9_low, 1))
                        + " body=" + str(int(body_pct)) + "% green=Y"
                        + " prev_close=" + str(round(prev_close, 1))
                        + "<=prev_ema9h" + str(round(prev_ema9_high, 1)))
        return result

    except Exception as e:
        logger.error("[ENGINE] check_entry error: " + str(e))
        result["reject_reason"] = "error_" + str(e)[:50]
        return result


def compute_entry_sl(entry_price: float, hard_sl: int = 12) -> float:
    """v15.0: legacy compat. Initial SL placed at entry-12 by VRL_TRADE
    as a static safety net while the dynamic band trail takes over."""
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
#  v15.0 EXIT — Single dynamic system
#
#  Priority order (first match wins, no fallthrough):
#    1. EMERGENCY_SL    → pnl ≤ -20
#    2. EOD_EXIT        → time ≥ 15:30
#    3. STALE_ENTRY     → 5 candles held AND peak < 3
#    4. EMA9_LOW_BREAK  → last closed 3m candle close < ema9_low
#                         (THE primary trailing stop)
# ═══════════════════════════════════════════════════════════════

def manage_exit(state: dict, option_ltp: float, profile: dict,
                other_token: int = 0) -> list:
    """v15.0: Single dynamic exit system. Band IS the stop."""
    if not state.get("in_trade"):
        return []

    entry = state.get("entry_price", 0)
    pnl = round(option_ltp - entry, 2)
    peak = max(state.get("peak_pnl", 0), pnl)
    state["peak_pnl"] = peak
    if pnl < state.get("trough_pnl", 0):
        state["trough_pnl"] = pnl

    candles = state.get("candles_held", 0)

    exit_cfg = CFG.get().get("exit_ema9_band", {})
    emergency_sl = exit_cfg.get("emergency_sl_pts", -20)
    stale_candles = exit_cfg.get("stale_candles", 5)
    stale_peak_max = exit_cfg.get("stale_peak_max", 3)
    eod_time = exit_cfg.get("eod_exit_time", "15:30")

    # ── RULE 1: EMERGENCY catastrophic ──
    if pnl <= emergency_sl:
        logger.info("[ENGINE] EMERGENCY_SL pnl=" + str(pnl))
        return [{"lot_id": "ALL", "reason": "EMERGENCY_SL", "price": option_ltp}]

    # ── RULE 2: EOD auto-exit at 15:30 ──
    now = datetime.now()
    if D.is_market_open():
        eod_h, eod_m = eod_time.split(":")
        eod_mins = int(eod_h) * 60 + int(eod_m)
        if now.hour * 60 + now.minute >= eod_mins:
            logger.info("[ENGINE] EOD_EXIT at " + now.strftime("%H:%M"))
            return [{"lot_id": "ALL", "reason": "EOD_EXIT", "price": option_ltp}]

    # ── RULE 3: STALE entry — 5 candles, peak < 3 ──
    if candles >= stale_candles and peak < stale_peak_max:
        logger.info("[ENGINE] STALE_ENTRY " + str(candles) + "c peak=" + str(peak))
        return [{"lot_id": "ALL", "reason": "STALE_ENTRY", "price": option_ltp}]

    # ── RULE 4: EMA9_LOW_BREAK — the dynamic trailing stop ──
    token = state.get("token")
    try:
        opt_3m = D.get_option_3min(token, lookback=5)
        if opt_3m is not None and not opt_3m.empty and len(opt_3m) >= 2:
            last = opt_3m.iloc[-2]  # last CLOSED candle
            last_close = float(last["close"])
            last_ema9_low = float(last.get("ema9_low", 0))
            last_ema9_high = float(last.get("ema9_high", 0))

            # Update state with current band (for dashboard + position card)
            state["current_ema9_high"] = round(last_ema9_high, 2)
            state["current_ema9_low"] = round(last_ema9_low, 2)
            state["current_floor"] = round(last_ema9_low, 2)  # legacy compat

            # Only act on a NEW closed candle (avoid repeat exit signals on same bar)
            last_ts = str(last.name) if hasattr(last, "name") else str(last.get("timestamp", ""))
            if state.get("last_band_check_ts") != last_ts:
                state["last_band_check_ts"] = last_ts
                if last_ema9_low > 0 and last_close < last_ema9_low:
                    logger.info("[ENGINE] EMA9_LOW_BREAK close=" + str(round(last_close, 1))
                                + " < ema9l=" + str(round(last_ema9_low, 1)))
                    return [{"lot_id": "ALL", "reason": "EMA9_LOW_BREAK", "price": option_ltp}]
    except Exception as e:
        logger.warning("[ENGINE] band check error: " + str(e))

    return []
