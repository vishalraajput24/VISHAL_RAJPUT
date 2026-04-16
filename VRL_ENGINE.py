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
            if avail > 0 and avail < option_ltp * D.get_lot_size() * 1.2:
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

        # ── GATE 3 (v15.2.4): Fresh breakout above EMA9-high ──
        # Relaxed from the original strict 1-candle check so the bot can
        # recover after a restart that happened mid-move. Fires when:
        #   close > ema9_high  AND  some candle in the last N bars had
        #   close <= its ema9_high (i.e. we WERE below the band recently).
        # Controlled by config: entry.ema9_band.fresh_breakout_lookback
        # (default 3). Setting it to 1 restores the strict v15.2 rule.
        fb_lookback = int(CFG.entry_ema9_band("fresh_breakout_lookback", 3) or 3)
        if fb_lookback < 1:
            fb_lookback = 1

        # Walk back from iloc[-3] up to `fb_lookback` candles looking for
        # "was below its own ema9_high".
        was_below_in_lookback = False
        for _k in range(3, 3 + fb_lookback):
            if len(opt_3m) < _k:
                break
            _bar = opt_3m.iloc[-_k]
            _bar_close  = float(_bar.get("close", 0))
            _bar_ema9h  = float(_bar.get("ema9_high", 0))
            if _bar_ema9h > 0 and _bar_close <= _bar_ema9h:
                was_below_in_lookback = True
                break

        is_fresh_breakout = (close > ema9_high) and was_below_in_lookback

        if not is_fresh_breakout:
            # v15.2.4: precise block-reason categorization. Lets us tell
            # stale post-restart state from pure chop in the log.
            if close <= ema9_high and prev_close > prev_ema9_high:
                reason_code = "just_crossed_down"
            elif close <= ema9_high:
                reason_code = "below_band"
            elif close > ema9_high and not was_below_in_lookback:
                # Above band for > fb_lookback candles — this is the
                # "bot restarted mid-move" case.
                reason_code = "already_above_band"
            else:
                # Logic says this branch shouldn't run (covered by the
                # fresh_breakout=True path). Log it so we can investigate.
                reason_code = "fresh_cross_up_but_missed_fire"

            result["reject_reason"] = (reason_code
                                       + "_close=" + str(round(close, 1))
                                       + "_ema9h=" + str(round(ema9_high, 1))
                                       + "_lookback=" + str(fb_lookback) + "c")
            if not silent:
                logger.info("[ENGINE] " + option_type
                            + " NO_BREAKOUT [" + reason_code + "]"
                            + " close=" + str(round(close, 1))
                            + " ema9h=" + str(round(ema9_high, 1))
                            + " prev_close=" + str(round(prev_close, 1))
                            + " prev_ema9h=" + str(round(prev_ema9_high, 1))
                            + " lookback=" + str(fb_lookback) + "c"
                            + " was_below=" + str(was_below_in_lookback))
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

        # ── GATE 7 (v15.2.5 Fix 5): straddle = DISPLAY ONLY, never blocks ──
        # Earlier versions hard-rejected on straddle_bleed / straddle_data_unavailable.
        # April 16 evidence: PE 24300 09:51 fresh breakout was blocked by the
        # NA bug and would have been +22pts. Even fully fixed, the delta can
        # underread real breakouts in fast moves. Policy decision: treat
        # straddle as contextual telemetry like VWAP. Classify into
        # STRONG (Δ≥+5) / NEUTRAL (0≤Δ<+5) / WEAK (Δ<0) / NA (no data),
        # log + annotate result, but NEVER reject.
        if CFG.straddle_filter("enabled", True):
            atm_strike = D.resolve_atm_strike(spot_ltp) if spot_ltp else 0
            result["atm_strike_used"] = atm_strike
            lookback_min = int(CFG.straddle_filter("lookback_minutes", 15))

            # Period label kept for the Telegram display + DB column, even
            # though no tier-specific threshold applies any more.
            now_for_period = datetime.now()
            mod = now_for_period.hour * 60 + now_for_period.minute
            if 585 <= mod < 630:
                period = "OPENING"
            elif 630 <= mod < 840:
                period = "MIDDAY"
            else:
                period = "CLOSING"
            result["straddle_period"]    = period
            result["straddle_threshold"] = 0   # deprecated; kept for back-compat

            sd = None
            try:
                sd = D.get_straddle_delta(
                    atm_strike, lookback_minutes=lookback_min)
            except Exception as e:
                logger.warning("[ENGINE] straddle delta error: " + str(e))

            result["straddle_delta"]     = sd if sd is not None else 0
            result["straddle_available"] = sd is not None

            if sd is None:
                result["straddle_info"] = "NA"
                logger.info("[ENGINE] " + option_type
                            + " STRADDLE_NA atm=" + str(atm_strike)
                            + " (" + period + ") — informational, proceeding")
            elif sd >= 5:
                result["straddle_info"] = "STRONG"
                logger.info("[ENGINE] " + option_type + " STRADDLE_STRONG "
                            + "Δ" + "{:+.1f}".format(sd) + " (" + period + ")")
            elif sd >= 0:
                result["straddle_info"] = "NEUTRAL"
                logger.info("[ENGINE] " + option_type + " STRADDLE_NEUTRAL "
                            + "Δ" + "{:+.1f}".format(sd) + " (" + period + ")")
            else:
                result["straddle_info"] = "WEAK"
                logger.info("[ENGINE] " + option_type + " STRADDLE_WEAK "
                            + "Δ" + "{:+.1f}".format(sd) + " (" + period + ")")
            # NO return here — straddle is display-only in v15.2.5 Fix 5.

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


# ═══════════════════════════════════════════════════════════════
#  v15.2.5 MULTI-CANDIDATE STRIKE SCANNER
#  Scans ATM-50, ATM, ATM+50 (×CE+PE = 6 candidates) every call.
#  Picks the best fired candidate by score:
#      (straddle_delta, body_pct, -abs(strike - center_atm))
#  Higher straddle expansion wins; tiebreaker is body strength, then
#  closeness to the true ATM.
#  Opt-in via config (entry.ema9_band.multi_candidate.enabled).
# ═══════════════════════════════════════════════════════════════

def scan_all_candidates(kite, spot_ltp: float, atm_strike: int,
                        expiry, dte: int = 0,
                        state: dict = None) -> dict:
    """Scan 3 neighboring strikes (ATM-50, ATM, ATM+50) × (CE, PE).
    Returns the best fired candidate dict (with keys: strike, side,
    token, symbol, result) or None if nothing fired. Never raises —
    any per-strike error is logged and skipped.

    Callers should use this INSTEAD of a single check_entry() call
    when `entry.ema9_band.multi_candidate.enabled=true`. When disabled
    (default), the caller's existing locked-strike path remains in use.
    """
    if state is None:
        state = {}
    if not atm_strike or expiry is None:
        return None

    step = int(CFG.entry_ema9_band("multi_candidate_strike_range", 50) or 50)
    strikes = [atm_strike - step, atm_strike, atm_strike + step]
    fired = []
    for strike in strikes:
        try:
            tokens = D.get_option_tokens(kite, int(strike), expiry) or {}
        except Exception as e:
            logger.debug("[ENGINE] multi_candidate token resolve "
                         + str(strike) + " err: " + str(e))
            continue
        for side in ("CE", "PE"):
            info = tokens.get(side)
            if not info:
                continue
            tok = int(info.get("token") or 0)
            if not tok:
                continue
            try:
                r = check_entry(
                    tok, side, spot_ltp, dte, expiry, kite,
                    silent=True, state=state)
            except Exception as e:
                logger.debug("[ENGINE] multi_candidate check_entry "
                             + str(strike) + side + " err: " + str(e))
                continue
            if r.get("fired"):
                fired.append({
                    "strike": int(strike),
                    "side":   side,
                    "token":  tok,
                    "symbol": info.get("symbol", ""),
                    "result": r,
                })

    if not fired:
        return None

    def _score(c):
        r = c["result"]
        sd = float(r.get("straddle_delta") or 0)
        body = float(r.get("body_pct") or 0)
        dist = -abs(c["strike"] - atm_strike)
        return (sd, body, dist)

    fired.sort(key=_score, reverse=True)
    best = fired[0]
    logger.info("[ENGINE] MULTI_CANDIDATE fired=" + str(len(fired))
                + " chose=" + str(best["strike"]) + best["side"]
                + " Δ=" + str(best["result"].get("straddle_delta"))
                + " body=" + str(best["result"].get("body_pct")) + "%")
    return best


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
#  v15.2.5 EXIT — Single dynamic system with velocity stall
#
#  Priority order (first match wins, no fallthrough):
#    1. EMERGENCY_SL    → pnl ≤ -20
#    2. EOD_EXIT        → time ≥ 15:30
#    3. STALE_ENTRY     → 5 candles held AND peak < 3
#    4. VELOCITY_STALL  → 2 consecutive candles peak didn't grow (NEW v15.2.5)
#    5. EMA9_LOW_BREAK  → last closed 3m candle close < ema9_low
#    6. BREAKEVEN_LOCK  → peak >= 10 AND ltp ≤ entry+2  (moved below band-break)
# ═══════════════════════════════════════════════════════════════

def manage_exit(state: dict, option_ltp: float, profile: dict,
                other_token: int = 0) -> list:
    """v15.2.5: Single dynamic exit system with velocity stall.
    Band IS the stop; velocity catches momentum death before price reversal."""
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
    vs_enabled         = bool(CFG.exit_ema9_band("velocity_stall_enabled", True))
    vs_consec          = int(CFG.exit_ema9_band("velocity_stall_consecutive", 2))
    vs_min_peak        = float(CFG.exit_ema9_band("velocity_stall_min_peak", 3))

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

    # ── Fetch 3-min bars once (shared by peak_history update + EMA9_LOW_BREAK) ──
    token = state.get("token")
    last_candle_ts = ""
    last_close = None
    last_ema9_low = None
    last_ema9_high = None
    # v15.2.5 BUG-J: on first manage_exit call after a restart-with-open-trade,
    # peak_history can be empty (pre-v15.2.5 state.json or a state.alert_history
    # clobber). VELOCITY_STALL needs 5+ slots to fire, so without backfill
    # we'd be blind to momentum death for 5 × 3-min = 15 minutes AFTER a
    # restart — exactly the window we most need the guard. Seed the history
    # from today's closed 3-min bars once per trade.
    opt_3m_full = None
    try:
        opt_3m_full = D.get_option_3min(token, lookback=10)
        if opt_3m_full is not None and not opt_3m_full.empty and len(opt_3m_full) >= 2:
            last = opt_3m_full.iloc[-2]
            last_close     = float(last["close"])
            last_ema9_low  = float(last.get("ema9_low", 0))
            last_ema9_high = float(last.get("ema9_high", 0))
            last_candle_ts = str(last.name) if hasattr(last, "name") else str(last.get("timestamp", ""))
            # Keep current band in state so dashboard + /status can read it.
            state["current_ema9_high"] = round(last_ema9_high, 2)
            state["current_ema9_low"]  = round(last_ema9_low, 2)
            state["current_floor"]     = round(last_ema9_low, 2)  # legacy compat
    except Exception as e:
        logger.warning("[ENGINE] band fetch error: " + str(e))

    # ── v15.2.5 BUG-J: one-shot peak_history backfill on startup-with-trade ──
    if (not state.get("_peak_history_backfilled")
            and not state.get("peak_history")
            and opt_3m_full is not None
            and not opt_3m_full.empty
            and entry > 0):
        try:
            # Build peak per closed 3-min candle since entry. Use each
            # bar's HIGH (max intra-bar excursion) — close-only would
            # understate the peak the live run would have seen and
            # create false all-zero history on losing trades.
            closed = opt_3m_full.iloc[:-1]            # drop live in-progress bar
            recent = closed.tail(6)
            running = 0.0
            seeded  = []
            for _i, _r in recent.iterrows():
                _h = float(_r.get("high", _r.get("close", 0) or 0))
                _p = max(_h - float(entry), 0.0)
                if _p > running:
                    running = _p
                seeded.append(round(running, 2))

            # Skip seeding when the trade NEVER reached the min-peak
            # threshold that VELOCITY_STALL cares about — an all-zeros
            # or all-sub-threshold history would otherwise trigger
            # spurious VELOCITY_STALL exits on a losing-trade restart.
            # STALE_ENTRY (5 candles + peak<3) already covers the
            # never-in-profit case; no need for velocity logic there.
            vs_min_peak_for_seed = float(CFG.exit_ema9_band("velocity_stall_min_peak", 3))
            if seeded and max(seeded) >= vs_min_peak_for_seed:
                state["peak_history"] = seeded
                last_seed_ts = str(recent.index[-1]) if len(recent) else ""
                state["last_peak_candle_ts"] = last_seed_ts
                state["_peak_history_backfilled"] = True
                logger.info("[ENGINE] peak_history backfilled from "
                            + str(len(seeded)) + " bars: " + str(seeded)
                            + " (entry=" + str(round(float(entry), 2)) + ")")
            elif seeded:
                # Still mark sentinel so we don't re-attempt every tick,
                # but leave peak_history empty — VELOCITY_STALL stays dormant.
                state["_peak_history_backfilled"] = True
                logger.info("[ENGINE] peak_history backfill SKIPPED — max peak "
                            + str(max(seeded) if seeded else 0)
                            + " < vs_min_peak " + str(vs_min_peak_for_seed)
                            + " (trade never reached stall threshold)")
        except Exception as _bf:
            logger.warning("[ENGINE] peak_history backfill error: " + str(_bf))

    # ── v15.2.5: Update peak_history once per NEW 3-min candle ──
    # Dedupe by candle timestamp so we don't append on every tick.
    if last_candle_ts and state.get("last_peak_candle_ts") != last_candle_ts:
        ph = list(state.get("peak_history") or [])
        ph.append(round(peak, 2))
        ph = ph[-6:]                               # keep last 6 candles
        state["peak_history"] = ph
        state["last_peak_candle_ts"] = last_candle_ts
    ph = list(state.get("peak_history") or [])
    # Dashboard/status velocity = 3-candle rolling average (smooth)
    if len(ph) >= 4:
        state["current_velocity"] = round((ph[-1] - ph[-4]) / 3.0, 2)
    elif len(ph) >= 2:
        state["current_velocity"] = round(ph[-1] - ph[-2], 2)
    else:
        state["current_velocity"] = 0.0

    # ── RULE 4 (v15.2.5): VELOCITY_STALL — 3-candle-avg velocity ≤ 0 for 2 windows ──
    # velocity = (ph[-1] - ph[-4]) / 3        # pts per candle over last 3 candles
    # prev_velocity = (ph[-2] - ph[-5]) / 3   # same but one candle earlier
    # If BOTH <= 0 and peak >= vs_min_peak → momentum died, exit.
    # Needs len(ph) >= 5 so both velocity windows are defined.
    if vs_enabled and len(ph) >= 5 and peak >= vs_min_peak:
        velocity      = (ph[-1] - ph[-4]) / 3.0
        prev_velocity = (ph[-2] - ph[-5]) / 3.0
        state["current_velocity"] = round(velocity, 2)
        if velocity <= 0 and prev_velocity <= 0:
            logger.info("[ENGINE] VELOCITY_STALL peak_hist="
                        + str(ph[-5:])
                        + " v=" + "{:+.2f}".format(velocity)
                        + " prev_v=" + "{:+.2f}".format(prev_velocity)
                        + " peak=" + str(round(peak, 1))
                        + " → exit")
            return [{"lot_id": "ALL", "reason": "VELOCITY_STALL", "price": option_ltp}]

    # ── RULE 5: EMA9_LOW_BREAK — the dynamic trailing stop ──
    # Uses the bars we already fetched above. Only act once per closed candle.
    if last_close is not None and last_ema9_low is not None and last_candle_ts:
        if state.get("last_band_check_ts") != last_candle_ts:
            state["last_band_check_ts"] = last_candle_ts
            if last_ema9_low > 0 and last_close < last_ema9_low:
                logger.info("[ENGINE] EMA9_LOW_BREAK close=" + str(round(last_close, 1))
                            + " < ema9l=" + str(round(last_ema9_low, 1)))
                return [{"lot_id": "ALL", "reason": "EMA9_LOW_BREAK", "price": option_ltp}]

    # ── RULE 6: BREAKEVEN_LOCK — peak ≥ 10, lock at entry+offset ──
    # Runs AFTER band-break and velocity so those fire first on momentum death.
    if peak >= be2_peak_threshold:
        be2_level = round(entry + be2_offset, 2)
        state["be2_active"] = True
        state["be2_level"]  = be2_level
        if option_ltp <= be2_level:
            logger.info("[ENGINE] BREAKEVEN_LOCK hit: peak=" + str(round(peak, 1))
                        + " ltp=" + str(round(option_ltp, 2))
                        + " <= lock=" + str(be2_level))
            return [{"lot_id": "ALL", "reason": "BREAKEVEN_LOCK", "price": be2_level}]
    else:
        state["be2_active"] = False

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

    v15.2.2: defensive logging — every early-return now leaves a trace so
    silent failures (no shadow CSVs, no SHADOW log lines) are diagnosable
    without re-deploying. All logs at INFO so they survive the standard
    logger level filter.
    """
    _t = datetime.now().strftime("%H:%M:%S")
    logger.info("[SHADOW_1MIN] scan_called spot=" + str(spot_ltp) + " at " + _t)
    try:
        if not spot_ltp or float(spot_ltp) <= 0:
            logger.info("[SHADOW_1MIN] skip: bad spot_ltp=" + str(spot_ltp))
            return
        atm = D.resolve_atm_strike(float(spot_ltp))
        if not atm:
            logger.info("[SHADOW_1MIN] skip: no atm_strike for spot=" + str(spot_ltp))
            return
        expiry = None
        try:
            expiry = D.get_nearest_expiry()
        except Exception as _ee:
            logger.info("[SHADOW_1MIN] skip: expiry resolve raised: " + str(_ee))
            return
        if expiry is None:
            logger.info("[SHADOW_1MIN] skip: expiry is None (kite not initialised?)")
            return
        logger.info("[SHADOW_1MIN] dispatch tick atm=" + str(atm)
                    + " expiry=" + str(expiry))
        _SHADOW.tick(None, float(spot_ltp), int(atm), expiry)
    except Exception as e:
        logger.warning("[SHADOW_1MIN] scan error: " + str(e), exc_info=True)
