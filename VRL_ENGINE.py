# ═══════════════════════════════════════════════════════════════
#  VRL_ENGINE.py — VISHAL RAJPUT TRADE v15.2
#  EMA9 Band Breakout + Tiered Straddle Filter + VWAP Display.
#
#  ENTRY (7 hard gates, all must pass + VWAP bonus shown but never blocks):
#    1. Time window 9:45 – 15:10 IST
#    2. Cooldown 5min same direction
#    3. Fresh breakout: close > ema9_high AND prev_close ≤ prev_ema9_high
#    4. Green candle
#    5. Body ≥ 30% of range
#    6. Band width ≥ 8 pts (chop filter)
#    7. Tiered straddle Δ (v15.2):
#         Open  9:45-10:30  Δ ≥ +1
#         Mid  10:30-14:00  Δ ≥ +5
#         Close 14:00-15:10 Δ ≥ +3
#    BONUS: VWAP confluence (display only, never blocks)
#
#  EXIT (5-rule priority chain):
#    1. EMERGENCY_SL    pnl ≤ -20
#    2. EOD_EXIT        15:30 IST
#    3. STALE_ENTRY     5 candles + peak < 3
#    4. BREAKEVEN_LOCK  after peak ≥ 10, lock at entry+2 (v15.2)
#    5. EMA9_LOW_BREAK  last 3m close < ema9_low (primary trail)
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
    """v15.2: Buy option whose 3-min just broke fresh above its own EMA9-high,
    confirmed by a tiered straddle expansion gate. VWAP shown for context."""
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
        # v15.2 tiered straddle filter
        "straddle_delta": None, "straddle_threshold": 0,
        "straddle_period": "", "atm_strike_used": 0,
        # v15.2 VWAP bonus (display only)
        "spot_vwap": 0.0, "spot_vs_vwap": 0.0, "vwap_bonus": "",
    }
    if state is None:
        state = {}
    try:
        body_min       = CFG.entry_ema9_band("body_pct_min", 30)
        cd_min         = CFG.entry_ema9_band("cooldown_minutes", 5)   # back-compat alias
        warmup_until   = CFG.entry_ema9_band("warmup_until", "09:45")
        cutoff_after   = CFG.entry_ema9_band("cutoff_after", "15:10")
        min_band_width = CFG.entry_ema9_band("min_band_width_pts", 8)

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

        # v15.2: band width (for chop filter + dashboard display)
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

        # ── GATE 6: Narrow band chop filter ──
        if band_width < min_band_width:
            result["reject_reason"] = "narrow_band_" + str(round(band_width, 1)) + "pts"
            if not silent:
                logger.info("[ENGINE] " + option_type + " NARROW_BAND width="
                            + str(round(band_width, 1)) + " < " + str(min_band_width) + " (chop)")
            return result

        # ── GATE 7 (v15.2): Tiered straddle expansion filter ──
        # Block entries during chop where straddle isn't actually expanding,
        # while staying loose at the open and tight midday.
        if CFG.straddle_filter("enabled", True):
            atm_strike = D.resolve_atm_strike(spot_ltp) if spot_ltp else 0
            result["atm_strike_used"] = atm_strike
            lookback_min = int(CFG.straddle_filter("lookback_minutes", 15))
            # Time tier — period + threshold (mins-of-day)
            now_for_period = datetime.now()
            mod = now_for_period.hour * 60 + now_for_period.minute
            tiers = CFG.straddle_thresholds() or {}

            def _tier_minutes(key, default):
                t = (tiers.get(key) or {}).get(default)
                if not t:
                    return None
                hh, mm = str(t).split(":")
                return int(hh) * 60 + int(mm)

            open_s  = _tier_minutes("opening", "start") or 585   # 09:45
            open_e  = _tier_minutes("opening", "end")   or 630   # 10:30
            mid_s   = _tier_minutes("midday",  "start") or 630
            mid_e   = _tier_minutes("midday",  "end")   or 840   # 14:00
            close_s = _tier_minutes("closing", "start") or 840
            close_e = _tier_minutes("closing", "end")   or 910   # 15:10

            if open_s <= mod < open_e:
                threshold = (tiers.get("opening") or {}).get("min_delta", 1)
                period = "OPENING"
            elif mid_s <= mod < mid_e:
                threshold = (tiers.get("midday")  or {}).get("min_delta", 5)
                period = "MIDDAY"
            else:
                threshold = (tiers.get("closing") or {}).get("min_delta", 3)
                period = "CLOSING"

            result["straddle_threshold"] = threshold
            result["straddle_period"]    = period

            straddle_delta = None
            try:
                straddle_delta = D.get_straddle_delta(
                    atm_strike, lookback_minutes=lookback_min)
            except Exception as e:
                logger.warning("[ENGINE] straddle delta error: " + str(e))
            result["straddle_delta"] = straddle_delta

            if straddle_delta is None:
                result["reject_reason"] = "straddle_data_unavailable"
                if not silent:
                    logger.info("[ENGINE] " + option_type
                                + " STRADDLE_NA period=" + period
                                + " strike=" + str(atm_strike))
                return result

            # Strict spec-format: straddle_bleed_{:+.1f}_need_{}_in_{PERIOD}
            _sd_str = ("{:+.1f}".format(straddle_delta))
            if straddle_delta < threshold:
                result["reject_reason"] = ("straddle_bleed_" + _sd_str
                                            + "_need_" + str(threshold)
                                            + "_in_" + period)
                if not silent:
                    logger.info("[ENGINE] " + option_type
                                + " STRADDLE_BLEED \u0394" + _sd_str
                                + " < +" + str(threshold) + " (" + period + ")")
                return result

            if not silent:
                logger.info("[ENGINE] " + option_type
                            + " STRADDLE_OK \u0394" + _sd_str
                            + " >= +" + str(threshold) + " (" + period + ")")

        # ── BONUS (v15.2): VWAP confluence — display only, never blocks ──
        # This block must NEVER set reject_reason or return — it only logs
        # and populates display fields.
        try:
            if CFG.vwap_bonus("enabled", True):
                vwap_val = D.get_spot_vwap()
                _spot = D.get_spot_ltp() or spot_ltp
                if vwap_val and _spot:
                    diff = round(_spot - vwap_val, 2)
                    result["spot_vwap"]    = round(vwap_val, 1)
                    result["spot_vs_vwap"] = round(diff, 1)
                    if option_type == "CE":
                        result["vwap_bonus"] = "CONFLUENCE" if diff > 0 else "AGAINST"
                    else:  # PE
                        result["vwap_bonus"] = "CONFLUENCE" if diff < 0 else "AGAINST"
                    logger.info("[ENGINE] VWAP_INFO spot=" + str(round(_spot, 1))
                                + " vwap=" + "{:.1f}".format(vwap_val)
                                + " diff=" + "{:+.1f}".format(diff)
                                + " " + result["vwap_bonus"])
        except Exception as e:
            logger.debug("[ENGINE] vwap bonus error: " + str(e))

        # ═══ ALL HARD GATES PASSED — FIRE ═══
        result["fired"] = True
        result["entry_mode"] = "EMA9_BREAKOUT"
        if not silent:
            logger.info("[ENGINE] " + option_type + " ENTRY [EMA9_BREAKOUT]"
                        + " close=" + str(round(close, 1))
                        + " ema9h=" + str(round(ema9_high, 1))
                        + " ema9l=" + str(round(ema9_low, 1))
                        + " width=" + str(round(band_width, 1))
                        + " body=" + str(int(body_pct)) + "% green=Y"
                        + " straddleΔ=" + str(result.get("straddle_delta"))
                        + " (" + str(result.get("straddle_period", "-")) + ")"
                        + " vwap=" + str(result.get("vwap_bonus", "-")))
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

    emergency_sl       = CFG.exit_ema9_band("emergency_sl_pts", -20)
    stale_candles      = CFG.exit_ema9_band("stale_candles", 5)
    stale_peak_max     = CFG.exit_ema9_band("stale_peak_max", 3)
    eod_time           = CFG.exit_ema9_band("eod_exit_time", "15:30")
    be2_peak_threshold = CFG.exit_ema9_band("breakeven_lock_peak_threshold", 10)
    be2_offset         = CFG.exit_ema9_band("breakeven_lock_offset", 2)

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

    # ── RULE 4: BREAKEVEN_LOCK — once peak crosses threshold, lock entry+offset ──
    # Prevents profit giveback on trades that peaked meaningfully but band lags.
    if peak >= be2_peak_threshold:
        be2_level = round(entry + be2_offset, 2)
        state["be2_active"] = True
        state["be2_level"] = be2_level
        if option_ltp <= be2_level:
            logger.info("[ENGINE] BREAKEVEN_LOCK hit: peak=" + str(round(peak, 1))
                        + " ltp=" + str(round(option_ltp, 2))
                        + " <= lock=" + str(be2_level))
            return [{"lot_id": "ALL", "reason": "BREAKEVEN_LOCK", "price": be2_level}]
    else:
        state["be2_active"] = False

    # ── RULE 5: EMA9_LOW_BREAK — the dynamic trailing stop ──
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


# ═══════════════════════════════════════════════════════════════
#  v15.2 Part 4 — Shadow 1-min strategy public API
#  The full implementation lives in VRL_SHADOW (separate module so
#  shadow state, CSV writers, and EOD Telegram can evolve without
#  coupling to the live engine). VRL_ENGINE re-exports the canonical
#  symbols so callers can do:
#      from VRL_ENGINE import shadow_scan_1min, shadow_state
# ═══════════════════════════════════════════════════════════════

import VRL_SHADOW as _SHADOW

# Module-level shadow state — the SAME dict backing VRL_SHADOW's state.
# Shared by reference; never persisted to state.json; in-memory only.
shadow_state = _SHADOW.shadow_state


def shadow_scan_1min(spot_ltp):
    """v15.2 Part 4: silent 1-min EMA9 band breakout scan. Logs only,
    never trades, never touches live state. Call once per 1-min boundary
    from VRL_MAIN (after the live 3-min check_entry loop).

    Resolves ATM strike + nearest expiry internally using VRL_DATA helpers.
    Any exception is caught and logged — this must never kill the main loop.
    """
    try:
        if not spot_ltp or float(spot_ltp) <= 0:
            return
        atm = D.resolve_atm_strike(float(spot_ltp))
        expiry = None
        try:
            expiry = D.get_nearest_expiry()
        except Exception:
            expiry = None
        if not atm or expiry is None:
            # Can't scan without both — skip silently, next minute will retry.
            return
        _SHADOW.tick(None, float(spot_ltp), int(atm), expiry)
    except Exception as e:
        logger.warning("[SHADOW_1MIN] scan error: " + str(e))
