# ═══════════════════════════════════════════════════════════════
#  VRL_ENGINE.py — VISHAL RAJPUT TRADE v16.0
#  EMA9 Band Breakout + Ratchet Exit + 1-min EMA9 Break.
#
#  ENTRY (7 hard gates, all must pass + VWAP bonus shown but never blocks):
#    1. Time window 9:30 – 15:10 IST
#    2. Cooldown 5min same direction
#    3. Fresh breakout: close > ema9_high AND prev_close ≤ prev_ema9_high
#    4. Green candle
#    5. Body ≥ 30% of range
#    6. Band width ≥ 8 pts (chop filter)
#    7. Tiered straddle Δ (v15.2):
#         Open  9:30-10:30  Δ ≥ +1
#         Mid  10:30-14:00  Δ ≥ +5
#         Close 14:00-15:10 Δ ≥ +3
#    BONUS: VWAP confluence (display only, never blocks)
#
#  EXIT (6-rule priority chain, v16.0):
#    1. EMERGENCY_SL    pnl ≤ -20
#    2. EOD_EXIT        15:30 IST
#    3. STALE_ENTRY     5 candles + peak < 3
#    4. VELOCITY_STALL  2 consecutive no-growth windows
#    5. EMA1M_BREAK     1-min red + close < 1m EMA9 + pnl ≥ 5
#    6. PROFIT_RATCHET  5-tier lock based on peak
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
#    1. Time window: 9:30 ≤ now < 15:10
#    2. Cooldown: 5 min same direction
#    3. Fresh breakout: close > ema9_high AND prev_close ≤ prev_ema9_high
#    4. Green candle: close > open
#    5. Body ≥ 30% of candle range
# ═══════════════════════════════════════════════════════════════

def _evaluate_entry_gates_pure(opt_3m, option_type: str, spot_ltp: float,
                                now, market_open: bool, state: dict,
                                straddle_delta, spot_vwap, spot_for_vwap: float,
                                atm_strike: int, silent: bool = False) -> dict:
    """Pure entry-gate evaluator. All I/O lives in the check_entry wrapper.

    Parameters:
        opt_3m:         pandas DataFrame with columns open/high/low/close/volume
                        and indicator columns ema9_high, ema9_low (rolling 3-min
                        option candles, most recent LIVE bar at iloc[-1], last
                        CLOSED bar at iloc[-2]).
        option_type:    "CE" or "PE".
        spot_ltp:       current spot LTP (float, may be 0).
        now:            datetime-like for time-window + cooldown math.
        market_open:    bool — if False the time window gate is skipped (tests).
        state:          dict with last_exit_time / last_exit_direction.
        straddle_delta: pre-fetched straddle delta or None.
        spot_vwap:      pre-fetched session VWAP value (display only).
        spot_for_vwap:  pre-fetched spot LTP used for VWAP diff.
        atm_strike:     pre-computed ATM strike for display.
        silent:         suppress INFO logs when True.

    Returns: same dict shape as check_entry (fired/reject_reason/metrics).
    Behaviour identical to the inline body it replaces (extracted 1:1).
    """
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
        "straddle_delta": None, "straddle_threshold": 0,
        "straddle_period": "", "atm_strike_used": 0,
        "spot_vwap": 0.0, "spot_vs_vwap": 0.0, "vwap_bonus": "",
        "ema9_high_slope_5c": 0.0, "ema9_low_slope_5c": 0.0,
        "bands_state": "", "context_tag": "",
    }
    if state is None:
        state = {}
    try:
        body_min       = CFG.entry_ema9_band("body_pct_min", 30)
        cd_min         = CFG.entry_ema9_band("cooldown_minutes", 5)
        warmup_until   = CFG.entry_ema9_band("warmup_until", "09:30")
        cutoff_after   = CFG.entry_ema9_band("cutoff_after", "15:10")
        min_band_width = CFG.entry_ema9_band("min_band_width_pts", 8)

        if opt_3m is None or opt_3m.empty or len(opt_3m) < 4:
            result["reject_reason"] = "insufficient_3m_data"
            return result

        last = opt_3m.iloc[-2]
        prev = opt_3m.iloc[-3]

        close = float(last["close"])
        open_ = float(last["open"])
        high  = float(last["high"])
        low   = float(last["low"])
        ema9_high = float(last.get("ema9_high", 0))
        ema9_low  = float(last.get("ema9_low", 0))
        prev_close = float(prev["close"])
        prev_ema9_high = float(prev.get("ema9_high", 0))

        if close > ema9_high:
            band_position = "ABOVE"
        elif close < ema9_low:
            band_position = "BELOW"
        else:
            band_position = "IN"

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

        # ── GATE 1: Time window 9:30 — 15:10 ──
        if market_open:
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
                elapsed = (now - datetime.fromisoformat(last_exit_ts)).total_seconds() / 60
                if elapsed < cd_min:
                    result["reject_reason"] = "cooldown_" + str(round(cd_min - elapsed, 1)) + "min"
                    return result
            except Exception:
                pass
        result["cooldown_ok"] = True

        # ── GATE 3: Fresh breakout above EMA9-high ──
        fb_lookback = int(CFG.entry_ema9_band("fresh_breakout_lookback", 3) or 3)
        if fb_lookback < 1:
            fb_lookback = 1

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
            if close <= ema9_high and prev_close > prev_ema9_high:
                reason_code = "just_crossed_down"
            elif close <= ema9_high:
                reason_code = "below_band"
            elif close > ema9_high and not was_below_in_lookback:
                reason_code = "already_above_band"
            else:
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

        # ── GATE 7: straddle DISPLAY ONLY ──
        if CFG.straddle_filter("enabled", True):
            result["atm_strike_used"] = atm_strike
            mod = now.hour * 60 + now.minute
            if 585 <= mod < 630:
                period = "OPENING"
            elif 630 <= mod < 840:
                period = "MIDDAY"
            else:
                period = "CLOSING"
            result["straddle_period"]    = period
            result["straddle_threshold"] = 0

            sd = straddle_delta
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

        # ── VWAP confluence (display only) ──
        try:
            if CFG.vwap_bonus("enabled", True):
                vwap_val = spot_vwap
                _spot = spot_for_vwap or spot_ltp
                if vwap_val and _spot:
                    diff = round(_spot - vwap_val, 2)
                    result["spot_vwap"]    = round(vwap_val, 1)
                    result["spot_vs_vwap"] = round(diff, 1)
                    if option_type == "CE":
                        result["vwap_bonus"] = "CONFLUENCE" if diff > 0 else "AGAINST"
                    else:
                        result["vwap_bonus"] = "CONFLUENCE" if diff < 0 else "AGAINST"
                    logger.info("[ENGINE] VWAP_INFO spot=" + str(round(_spot, 1))
                                + " vwap=" + "{:.1f}".format(vwap_val)
                                + " diff=" + "{:+.1f}".format(diff)
                                + " " + result["vwap_bonus"])
        except Exception as e:
            logger.debug("[ENGINE] vwap bonus error: " + str(e))

        # ═══ band slope + context_tag (display only) ═══
        try:
            if opt_3m is not None and len(opt_3m) >= 6:
                _closed = opt_3m.iloc[:-1].tail(6)
                _eh_then = float(_closed.iloc[0].get("ema9_high", 0))
                _eh_now  = float(_closed.iloc[-1].get("ema9_high", 0))
                _el_then = float(_closed.iloc[0].get("ema9_low", 0))
                _el_now  = float(_closed.iloc[-1].get("ema9_low", 0))
                result["ema9_high_slope_5c"] = round(_eh_now - _eh_then, 1)
                result["ema9_low_slope_5c"]  = round(_el_now - _el_then, 1)
            else:
                result["ema9_high_slope_5c"] = 0.0
                result["ema9_low_slope_5c"]  = 0.0
        except Exception:
            result["ema9_high_slope_5c"] = 0.0
            result["ema9_low_slope_5c"]  = 0.0

        _ehs = result["ema9_high_slope_5c"]
        _els = result["ema9_low_slope_5c"]
        if _ehs >= 20 and _els >= 20:
            result["bands_state"] = "RISING"
        elif _ehs <= 3 and _els <= 3:
            result["bands_state"] = "FLAT"
        else:
            result["bands_state"] = "WEAK"

        _straddle_strong = (result.get("straddle_info") == "STRONG")
        _vwap_confluence = (result.get("vwap_bonus") == "CONFLUENCE")
        _bands_rising    = (result["bands_state"] == "RISING")
        if _straddle_strong and _vwap_confluence and _bands_rising:
            result["context_tag"] = "TRIPLE_CONFLUENCE"
        elif (result.get("straddle_info") == "WEAK"
              and result["bands_state"] == "FLAT"):
            result["context_tag"] = "MIXED_SIGNALS"
        else:
            result["context_tag"] = "NORMAL"

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
        logger.error("[ENGINE] _evaluate_entry_gates_pure error: " + str(e))
        result["reject_reason"] = "error_" + str(e)[:50]
        return result


def check_entry(token: int, option_type: str, spot_ltp: float = 0,
                dte: int = 99, expiry_date=None, kite=None,
                other_token: int = 0, silent: bool = False,
                state: dict = None) -> dict:
    """v16.0 thin wrapper: fetches live data and delegates to the pure
    gate evaluator. All entry-gate logic lives in _evaluate_entry_gates_pure.
    """
    if state is None:
        state = {}
    # Fetch 3-min option data up front.
    opt_3m = D.get_option_3min(token, lookback=15)
    market_open = D.is_market_open()
    now = datetime.now()
    atm_strike = D.resolve_atm_strike(spot_ltp) if spot_ltp else 0

    # Straddle delta (display only — pre-fetch here so the pure function
    # never does I/O).
    straddle_delta = None
    if CFG.straddle_filter("enabled", True) and atm_strike:
        try:
            lookback_min = int(CFG.straddle_filter("lookback_minutes", 15))
            straddle_delta = D.get_straddle_delta(
                atm_strike, lookback_minutes=lookback_min)
        except Exception as e:
            logger.warning("[ENGINE] straddle delta error: " + str(e))

    # VWAP (display only) — pre-fetch.
    spot_vwap = None
    spot_for_vwap = 0.0
    if CFG.vwap_bonus("enabled", True):
        try:
            spot_vwap = D.get_spot_vwap()
            spot_for_vwap = D.get_spot_ltp() or spot_ltp
        except Exception as e:
            logger.debug("[ENGINE] vwap fetch: " + str(e))

    return _evaluate_entry_gates_pure(
        opt_3m=opt_3m, option_type=option_type, spot_ltp=spot_ltp,
        now=now, market_open=market_open, state=state,
        straddle_delta=straddle_delta, spot_vwap=spot_vwap,
        spot_for_vwap=spot_for_vwap, atm_strike=atm_strike,
        silent=silent)


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


# ═══════════════════════════════════════════════════════════════
#  v15.2.5 Batch 3 BUG-R1 — Shadow-mode pure functions.
#  These NEVER mutate state, NEVER call exit functions, NEVER
#  touch production SL fields. They compute hypothetical values
#  for the shadow CSV logger to record. Analysis only.
# ═══════════════════════════════════════════════════════════════

def is_setup_building(token: int, direction: str) -> bool:
    """BUG-S2: Returns True if the locked strike is close to firing.
    Used by VRL_MAIN to defer ATM relock when a setup is 75%+ formed.
    Pure function — no state mutation.

    Criteria (all must hold):
      - close > ema9_high (breakout valid)
      - green candle (close > open)
      - body_pct >= 25 (near the 30% threshold)
      - band_width >= 6.0 (near the 8pt threshold — 75%)
    """
    try:
        df = D.get_option_3min(token, lookback=10)
        if df is None or df.empty or len(df) < 3:
            return False
        last = df.iloc[-2]
        close    = float(last["close"])
        open_    = float(last["open"])
        high     = float(last["high"])
        low      = float(last["low"])
        ema9_high = float(last.get("ema9_high", 0))
        ema9_low  = float(last.get("ema9_low", 0))
        band_width = ema9_high - ema9_low
        candle_range = high - low
        body_pct = (abs(close - open_) / candle_range * 100) if candle_range > 0 else 0

        breakout  = close > ema9_high
        green     = close > open_
        body_near = body_pct >= 25
        band_near = band_width >= 6.0

        return bool(breakout and green and body_near and band_near)
    except Exception:
        return False


def compute_ratchet_sl(entry_price: float, peak_pnl: float,
                       direction: str) -> tuple:
    """Returns (sl_price, tier_label). sl_price=0 if no tier crossed.
    Pure function — no side effects."""
    if peak_pnl >= 45:
        lock, tier = 40, "T5"
    elif peak_pnl >= 35:
        lock, tier = 25, "T4"
    elif peak_pnl >= 25:
        lock, tier = 15, "T3"
    elif peak_pnl >= 15:
        lock, tier = 7, "T2"
    elif peak_pnl >= 10:
        lock, tier = 2, "T1"
    else:
        return 0.0, "None"
    return round(entry_price + lock, 2), tier


def _compute_1min_ema9_break_pure(df_1min, running_pnl: float,
                                   min_pnl_guard: float = 5.0) -> tuple:
    """Pure: evaluate the 1-min EMA9 break rule on a pre-fetched DataFrame.

    df_1min must have indicator columns (add_indicators already applied).
    Returns (would_break, close_price, ema9_1m). Same contract as
    compute_1min_ema9_break — the thin wrapper just fetches + calls this.
    """
    try:
        if df_1min is None or df_1min.empty or len(df_1min) < 10:
            return False, 0.0, 0.0
        last = df_1min.iloc[-2]
        ema9 = float(last.get("EMA_9", last["close"]))
        close = float(last["close"])
        is_red = close < float(last["open"])
        below_ema = close < ema9
        pnl_ok = running_pnl >= min_pnl_guard
        return bool(is_red and below_ema and pnl_ok), round(close, 2), round(ema9, 2)
    except Exception as e:
        logger.debug("[ENGINE] 1m EMA9 break calc error: " + str(e))
        return False, 0.0, 0.0


def compute_1min_ema9_break(option_token: int, running_pnl: float,
                             min_pnl_guard: float = 5.0) -> tuple:
    """Thin wrapper: fetches 1-min data and delegates to the pure evaluator.
    Returns (would_break, close_price, ema9_1m). Returns (False, 0, 0) on error."""
    try:
        df = D.get_historical_data(int(option_token), "minute", 15)
        if df is None or df.empty or len(df) < 10:
            return False, 0.0, 0.0
        df = D.add_indicators(df)
        return _compute_1min_ema9_break_pure(df, running_pnl, min_pnl_guard)
    except Exception as e:
        logger.debug("[ENGINE] 1m EMA9 break calc error: " + str(e))
        return False, 0.0, 0.0


def check_profit_lock(state: dict, daily_pnl: float) -> bool:
    if state.get("profit_locked"):
        return False
    if daily_pnl >= D.PROFIT_LOCK_PTS:
        state["profit_locked"] = True
        logger.info("[ENGINE] Profit lock at " + str(round(daily_pnl, 1)) + "pts")
        return True
    return False


# ═══════════════════════════════════════════════════════════════
#  v16.0 EXIT CHAIN — 6 rules, priority order
#
#    1. EMERGENCY_SL     → pnl ≤ -20
#    2. EOD_EXIT         → time ≥ 15:30
#    3. STALE_ENTRY      → 5 candles held AND peak < 3
#    4. VELOCITY_STALL   → 2 consecutive 3-candle windows no growth
#    5. EMA1M_BREAK      → 1-min red + close < 1m EMA9 + pnl ≥ 5
#    6. PROFIT_RATCHET   → 5-tier lock based on peak
#
#  EMA9_LOW_BREAK removed — too slow (−84% capture in backtest).
#  BREAKEVEN_LOCK removed — redundant with Ratchet T1 (both +2 at peak≥10).
# ═══════════════════════════════════════════════════════════════

def _evaluate_exit_chain_pure(state: dict, option_ltp: float,
                               opt_3m_full, now,
                               ema1m_break_result,
                               market_open: bool) -> list:
    """Pure exit-chain evaluator. Mutates `state` exactly as manage_exit did
    (peak_pnl, trough_pnl, peak_history, current_velocity, current_ema9_high/low,
    active_ratchet_tier/sl, _peak_history_backfilled, last_peak_candle_ts).

    Parameters:
        state:              position state dict (mutated in place).
        option_ltp:         current option LTP (float).
        opt_3m_full:        pre-fetched 3-min DataFrame with ema9_high/low, or None.
        now:                datetime — EOD comparison.
        ema1m_break_result: tuple (would_break, close, ema9_1m) pre-computed.
        market_open:        bool — gates EOD check exactly like the wrapper.

    Returns: list of exit dicts — same shape as manage_exit's output.
    """
    if not state.get("in_trade"):
        return []

    entry = state.get("entry_price", 0)
    pnl = round(option_ltp - entry, 2)
    peak = max(state.get("peak_pnl", 0), pnl)
    state["peak_pnl"] = peak
    if pnl < state.get("trough_pnl", 0):
        state["trough_pnl"] = pnl

    candles = state.get("candles_held", 0)

    emergency_sl  = CFG.exit_ema9_band("emergency_sl_pts", -20)
    stale_candles = CFG.exit_ema9_band("stale_candles", 5)
    stale_peak_max= CFG.exit_ema9_band("stale_peak_max", 3)
    eod_time      = CFG.exit_ema9_band("eod_exit_time", "15:30")
    vs_enabled    = bool(CFG.exit_ema9_band("velocity_stall_enabled", True))
    vs_min_peak   = float(CFG.exit_ema9_band("velocity_stall_min_peak", 3))

    # ── RULE 1: EMERGENCY catastrophic ──
    if pnl <= emergency_sl:
        logger.info("[ENGINE] EMERGENCY_SL pnl=" + str(pnl))
        return [{"lot_id": "ALL", "reason": "EMERGENCY_SL", "price": option_ltp}]

    # ── RULE 2: EOD auto-exit at 15:30 ──
    if market_open:
        eod_h, eod_m = eod_time.split(":")
        eod_mins = int(eod_h) * 60 + int(eod_m)
        if now.hour * 60 + now.minute >= eod_mins:
            logger.info("[ENGINE] EOD_EXIT at " + now.strftime("%H:%M"))
            return [{"lot_id": "ALL", "reason": "EOD_EXIT", "price": option_ltp}]

    # ── RULE 3: STALE entry — 5 candles, peak < 3 ──
    if candles >= stale_candles and peak < stale_peak_max:
        logger.info("[ENGINE] STALE_ENTRY " + str(candles) + "c peak=" + str(peak))
        return [{"lot_id": "ALL", "reason": "STALE_ENTRY", "price": option_ltp}]

    # ── band + peak_history bookkeeping ──
    last_candle_ts = ""
    if opt_3m_full is not None and not opt_3m_full.empty and len(opt_3m_full) >= 2:
        last = opt_3m_full.iloc[-2]
        last_candle_ts = str(last.name) if hasattr(last, "name") else str(last.get("timestamp", ""))
        state["current_ema9_high"] = round(float(last.get("ema9_high", 0)), 2)
        state["current_ema9_low"]  = round(float(last.get("ema9_low", 0)), 2)

    # ── BUG-J: one-shot peak_history backfill on startup-with-trade ──
    if (not state.get("_peak_history_backfilled")
            and not state.get("peak_history")
            and opt_3m_full is not None
            and not opt_3m_full.empty
            and entry > 0):
        try:
            closed = opt_3m_full.iloc[:-1]
            recent = closed.tail(6)
            running = 0.0
            seeded  = []
            for _i, _r in recent.iterrows():
                _h = float(_r.get("high", _r.get("close", 0) or 0))
                _p = max(_h - float(entry), 0.0)
                if _p > running:
                    running = _p
                seeded.append(round(running, 2))
            vs_min_peak_for_seed = float(CFG.exit_ema9_band("velocity_stall_min_peak", 3))
            if seeded and max(seeded) >= vs_min_peak_for_seed:
                state["peak_history"] = seeded
                last_seed_ts = str(recent.index[-1]) if len(recent) else ""
                state["last_peak_candle_ts"] = last_seed_ts
                state["_peak_history_backfilled"] = True
                logger.info("[ENGINE] peak_history backfilled from "
                            + str(len(seeded)) + " bars: " + str(seeded))
            elif seeded:
                state["_peak_history_backfilled"] = True
        except Exception as _bf:
            logger.warning("[ENGINE] peak_history backfill error: " + str(_bf))

    # ── Update peak_history once per NEW 3-min candle ──
    if last_candle_ts and state.get("last_peak_candle_ts") != last_candle_ts:
        ph = list(state.get("peak_history") or [])
        ph.append(round(peak, 2))
        ph = ph[-6:]
        state["peak_history"] = ph
        state["last_peak_candle_ts"] = last_candle_ts
    ph = list(state.get("peak_history") or [])
    if len(ph) >= 4:
        state["current_velocity"] = round((ph[-1] - ph[-4]) / 3.0, 2)
    elif len(ph) >= 2:
        state["current_velocity"] = round(ph[-1] - ph[-2], 2)
    else:
        state["current_velocity"] = 0.0

    # ── RULE 4: VELOCITY_STALL — 3-candle-avg velocity ≤ 0 for 2 windows ──
    if vs_enabled and len(ph) >= 5 and peak >= vs_min_peak:
        velocity      = (ph[-1] - ph[-4]) / 3.0
        prev_velocity = (ph[-2] - ph[-5]) / 3.0
        state["current_velocity"] = round(velocity, 2)
        if velocity <= 0 and prev_velocity <= 0:
            logger.info("[ENGINE] VELOCITY_STALL peak_hist="
                        + str(ph[-5:])
                        + " v=" + "{:+.2f}".format(velocity)
                        + " prev_v=" + "{:+.2f}".format(prev_velocity)
                        + " peak=" + str(round(peak, 1)) + " → exit")
            return [{"lot_id": "ALL", "reason": "VELOCITY_STALL", "price": option_ltp}]

    # ── RULE 5: EMA1M_BREAK — 1-min red + close < 1m EMA9 + in profit ──
    running_pnl = round(option_ltp - entry, 2)
    would_break, ema1m_close, ema1m_ema9 = ema1m_break_result
    if would_break:
        logger.info("[ENGINE] EMA1M_BREAK close=" + str(ema1m_close)
                    + " ema9_1m=" + str(ema1m_ema9)
                    + " pnl=" + str(running_pnl))
        return [{"lot_id": "ALL", "reason": "EMA1M_BREAK", "price": option_ltp}]

    # ── RULE 6: PROFIT_RATCHET — 5-tier lock based on peak ──
    ratchet_sl, ratchet_tier = compute_ratchet_sl(entry, peak,
                                                   state.get("direction", ""))
    state["active_ratchet_tier"] = ratchet_tier
    state["active_ratchet_sl"]   = ratchet_sl
    if ratchet_sl > 0 and option_ltp <= ratchet_sl:
        logger.info("[ENGINE] PROFIT_RATCHET tier=" + ratchet_tier
                    + " peak=" + str(round(peak, 1))
                    + " lock=+" + str(round(ratchet_sl - entry, 1))
                    + " sl=" + str(ratchet_sl))
        return [{"lot_id": "ALL", "reason": "PROFIT_RATCHET", "price": ratchet_sl}]

    return []


def manage_exit(state: dict, option_ltp: float, profile: dict,
                other_token: int = 0) -> list:
    """v16.0 thin wrapper: fetches 3-min bars + 1-min EMA9 break result,
    then delegates to the pure exit-chain evaluator. All exit-chain logic
    lives in _evaluate_exit_chain_pure."""
    if not state.get("in_trade"):
        return []

    token = state.get("token")
    opt_3m_full = None
    try:
        opt_3m_full = D.get_option_3min(token, lookback=10)
    except Exception as e:
        logger.warning("[ENGINE] band fetch error: " + str(e))

    entry = state.get("entry_price", 0)
    running_pnl = round(option_ltp - entry, 2)
    ema1m_break_result = compute_1min_ema9_break(
        option_token=int(token) if token else 0,
        running_pnl=running_pnl, min_pnl_guard=5.0)

    market_open = D.is_market_open()
    now = datetime.now()

    return _evaluate_exit_chain_pure(
        state=state, option_ltp=option_ltp,
        opt_3m_full=opt_3m_full, now=now,
        ema1m_break_result=ema1m_break_result,
        market_open=market_open)


# Shadow 1-min API — REMOVED in v16.0 Batch 7 (BUG-Q9).
