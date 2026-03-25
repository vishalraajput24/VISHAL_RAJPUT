# ═══════════════════════════════════════════════════════════════
#  VRL_ENGINE.py — VISHAL RAJPUT TRADE v12.13
#  Signal logic. Entry checks, exit management, scoring.
#  v12.13: Expiry breakout mode (spot consolidation trigger),
#          fib pivot proximity, expiry-specific SL/trail.
# ═══════════════════════════════════════════════════════════════

import logging
from datetime import datetime
import VRL_DATA as D

logger = logging.getLogger("vrl_live")

def _min_sl_pts(option_price: float) -> float:
    if option_price >= 300:   return 15.0
    elif option_price >= 200: return 12.0
    elif option_price >= 100: return 10.0
    elif option_price >= 50:  return 8.0
    else:                     return 6.0

# ═══════════════════════════════════════════════════════════════
#  LAYER 1 — 3-MIN PERMISSION GATE
#  "Should we be trading at all right now?"
# ═══════════════════════════════════════════════════════════════

def _check_3min(token: int, option_type: str, profile: dict, dte: int = 99) -> tuple:
    """Returns (permitted, details, bonus_score)"""
    details = {
        "ema_aligned": False, "body_ok": False,
        "rsi_ok": False, "price_ok": False,
        "ema_spread_3m": 0.0, "rsi_val_3m": 0.0,
        "body_pct_3m": 0.0, "conditions_met": 0,
        "permitted": False, "bonus": 0,
        "mode": "EMA",  # v12.11: "EMA" or "MOMENTUM"
    }
    try:
        df = D.get_historical_data(token, "3minute", D.LOOKBACK_3M)
        df = D.add_indicators(df)
        if df.empty or len(df) < 5:
            details["permitted"] = True
            return True, details, 0

        last   = df.iloc[-2]
        o, h, l, c = last["open"], last["high"], last["low"], last["close"]
        ema9   = last.get("EMA_9",  c)
        ema21  = last.get("EMA_21", c)
        rsi    = last.get("RSI",   50.0)
        rng    = h - l
        body   = abs(c - o)
        body_pct = round(body / rng * 100, 1) if rng > 0 else 0

        # v12.11: Momentum fallback ONLY on DTE ≤ 1 AND candles < 25
        # Normal days (DTE 2+): Kite returns multi-day history, EMA works fine
        # DTE ≤ 1: weekly expiry tokens have zero yesterday data
        n_candles = len(df)
        use_momentum = (dte <= 1 and n_candles < 25)

        if use_momentum:
            details["mode"] = "MOMENTUM"
            lookback_idx = min(5, n_candles - 2)
            ref_close = df.iloc[-2 - lookback_idx]["close"]
            momentum  = round(c - ref_close, 2)
            spread    = momentum
            details["ema_aligned"] = (momentum > 0)
            avg_price = df.iloc[-min(6, n_candles):]["close"].mean()
            details["price_ok"] = (c >= avg_price)
            logger.info("[ENGINE] 3m MOMENTUM (" + str(n_candles) + "c DTE=" + str(dte) + ")"
                        + " ref=" + str(round(ref_close, 1))
                        + " now=" + str(round(c, 1))
                        + " mom=" + str(momentum))
        else:
            details["mode"] = "EMA"
            spread = round(ema9 - ema21, 2)
            details["ema_aligned"] = (ema9 > ema21)
            details["price_ok"]    = (c >= ema9)

        details["ema_spread_3m"] = spread
        details["rsi_val_3m"]    = round(rsi, 1)
        details["body_pct_3m"]   = body_pct

        rsi_lo = profile.get("rsi_low",  45)
        rsi_hi = profile.get("rsi_high", 72)

        details["body_ok"]     = body_pct >= 40
        details["rsi_ok"]      = rsi_lo <= rsi <= rsi_hi

        conditions_met = sum([details["ema_aligned"], details["body_ok"],
                              details["rsi_ok"],       details["price_ok"]])
        details["conditions_met"] = conditions_met
        permitted = conditions_met >= 3
        details["permitted"] = permitted
        bonus = 1 if (conditions_met == 4 and abs(spread) >= 8) else 0
        details["bonus"] = bonus

        logger.info("[ENGINE] 3m gate " + option_type
                    + " ema=" + str(details["ema_aligned"])
                    + " body=" + str(body_pct) + "%"
                    + " rsi=" + str(round(rsi, 1))
                    + " price=" + str(details["price_ok"])
                    + " spread=" + str(spread)
                    + " met=" + str(conditions_met) + "/4"
                    + " → " + ("PERMIT" + ("+BONUS" if bonus else "") if permitted else "BLOCK"))

        return permitted, details, bonus
    except Exception as e:
        logger.warning("[ENGINE] _check_3min error: " + str(e) + " — allowing through")
        details["permitted"] = True
        return True, details, 0

def get_option_ema_spread(token: int, dte: int = 99) -> float:
    try:
        df = D.get_historical_data(token, "3minute", D.LOOKBACK_3M)
        df = D.add_indicators(df)
        if df.empty or len(df) < 4:
            return 0.0
        last = df.iloc[-2]
        # v12.11: Momentum fallback only on DTE ≤ 1 with thin candles
        if dte <= 1 and len(df) < 25:
            lookback_idx = min(5, len(df) - 2)
            ref_close = df.iloc[-2 - lookback_idx]["close"]
            return round(last["close"] - ref_close, 2)
        return round(last.get("EMA_9", last["close"]) - last.get("EMA_21", last["close"]), 2)
    except Exception as e:
        logger.warning("[ENGINE] EMA spread error: " + str(e))
        return 0.0

def pre_entry_checks(kite, token: int, state: dict,
                     option_ltp: float, profile: dict,
                     session: str = "") -> tuple:
    if state.get("daily_trades", 0) >= D.MAX_DAILY_TRADES:
        return False, "MAX_DAILY_TRADES reached"
    if state.get("daily_losses", 0) >= D.MAX_DAILY_LOSSES:
        return False, "MAX_DAILY_LOSSES reached"
    last_exit = state.get("last_exit_time")
    if last_exit:
        try:
            elapsed = (datetime.now() - datetime.fromisoformat(last_exit)).total_seconds() / 60
            if elapsed < D.REENTRY_COOLDOWN_MIN:
                return False, "Cooldown: " + str(round(D.REENTRY_COOLDOWN_MIN - elapsed, 1)) + "min"
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

def loss_streak_gate(state: dict, score: int) -> bool:
    losses = state.get("consecutive_losses", 0)
    if losses < 2:
        return True
    if score >= D.EXCELLENCE_BYPASS_SCORE:
        logger.info("[ENGINE] ⚡ Excellence bypass: streak=" + str(losses)
                    + " score=" + str(score) + " — entering")
        return True
    logger.info("[ENGINE] Streak gate blocked: streak=" + str(losses)
                + " score=" + str(score) + "<" + str(D.LOSS_STREAK_GATE_SCORE))
    return False

def _check_1min(token: int, option_type: str, profile: dict) -> tuple:
    details = {
        "body_ok": False, "body_pct": 0.0,
        "rsi_ok": False,  "rsi_val":  0.0,
        "rsi_rising": False,
        "vol_ok": False,  "vol_ratio": 0.0,
        "entry_price": 0.0,
        "rsi_reject": False,
        "rejection_breakout": False,
    }
    try:
        df = D.get_historical_data(token, "minute", D.LOOKBACK_1M)
        df = D.add_indicators(df)
        if df.empty or len(df) < 7:
            return False, details

        last = df.iloc[-2]
        prev = df.iloc[-3]
        o, h, l, c = last["open"], last["high"], last["low"], last["close"]
        rsi      = last.get("RSI", 50)
        prev_rsi = prev.get("RSI", 50)

        rng      = h - l
        body     = abs(c - o)
        body_pct = (body / rng * 100) if rng > 0 else 0

        vols     = [df.iloc[i]["volume"] for i in range(-7, -2) if df.iloc[i]["volume"] > 0]
        avg_vol  = sum(vols) / len(vols) if vols else 1
        vol_ratio= round(last["volume"] / avg_vol if avg_vol > 0 else 1.0, 2)

        rsi_rising = rsi > prev_rsi
        details["rsi_rising"]  = rsi_rising
        details["body_pct"]    = round(body_pct, 1)
        details["rsi_val"]     = round(rsi, 1)
        details["vol_ratio"]   = vol_ratio
        details["entry_price"] = round(c, 2)

        # v12.12: 1-min uses tighter RSI zone (45-65) — enter early with headroom
        rsi_1m_lo = profile.get("rsi_1m_low", D.RSI_1M_LOW)
        rsi_1m_hi = profile.get("rsi_1m_high", D.RSI_1M_HIGH)
        rsi_ok = (rsi_1m_lo <= rsi <= rsi_1m_hi)
        details["rsi_ok"] = rsi_ok
        if not (rsi_ok and rsi_rising):
            details["rsi_reject"] = True
            if rsi > rsi_1m_hi:
                logger.info("[ENGINE] 1m RSI " + str(round(rsi, 1))
                            + " > " + str(rsi_1m_hi) + " — move already done, wait for pullback")
            return False, details

        body_ok = (c > o) and (body_pct >= profile["body_pct_min"])
        details["body_ok"] = body_ok
        details["vol_ok"]  = vol_ratio >= profile["volume_ratio_min"]

        prev_upper_wick = prev["high"] - max(prev["open"], prev["close"])
        prev_body  = abs(prev["close"] - prev["open"])
        prev_range = prev["high"] - prev["low"]
        prev_wick_ratio = prev_upper_wick / prev_range if prev_range > 0 else 0
        rejection = prev_wick_ratio > 0.6 and (prev_body / prev_range) < 0.3
        breakout  = (c > prev["high"]) and (h > prev["high"])
        details["rejection_breakout"] = rejection and breakout

        return body_ok and rsi_ok, details
    except Exception as e:
        logger.error("[ENGINE] _check_1min error: " + str(e))
        return False, details

def score_entry(det_1m: dict, greeks: dict, profile: dict,
                ema_spread: float, spread_1m: float = 0.0,
                option_type: str = "") -> tuple:
    """Returns (score: int, breakdown: dict)"""
    breakdown = {}
    score = 0

    if det_1m.get("body_ok"):
        score += 1
        breakdown["body"] = 1
        if det_1m.get("body_pct", 0) >= 50:
            score += 1
            breakdown["body_bonus"] = 1
        else:
            breakdown["body_bonus"] = 0
    else:
        breakdown["body"] = 0
        breakdown["body_bonus"] = 0

    if det_1m.get("rsi_ok") and det_1m.get("rsi_rising"):
        score += 1
        breakdown["rsi"] = 1
    else:
        breakdown["rsi"] = 0

    if det_1m.get("vol_ok"):
        score += 1
        breakdown["volume"] = 1
    else:
        breakdown["volume"] = 0

    delta = greeks.get("delta", 0)
    if profile.get("delta_min", 0) <= abs(delta) <= profile.get("delta_max", 1):
        score += 1
        breakdown["delta"] = 1
    else:
        breakdown["delta"] = 0

    # Double alignment bonus: 3-min strong + 1-min aligned same direction
    spread_3m_strong = abs(ema_spread) >= 5
    # Both CE and PE: we enter when the OPTION is trending UP (EMA9 > EMA21 = positive spread)
    spread_1m_aligned = spread_1m > 0

    if spread_3m_strong and spread_1m_aligned:
        score += 1
        breakdown["double_align"] = 1
        logger.debug("[ENGINE] Double align bonus: 3m=" + str(round(ema_spread,1))
                     + " 1m=" + str(round(spread_1m,1)))
    else:
        breakdown["double_align"] = 0

    breakdown["total"] = score
    return score, breakdown

def check_entry(token: int, option_type: str, profile: dict,
                spot_ltp: float, strike: int, expiry_date,
                session: str) -> dict:
    result = {
        "fired": False, "mode": "", "score": 0,
        "details_1m": {}, "details_3m": {}, "greeks": {},
        "entry_price": 0.0, "ema_spread": 0.0,
        "regime": "UNKNOWN",
        "spread_1m": 0.0,
        "score_breakdown": {},
        "prediction": {},
    }
    try:
        dte         = D.calculate_dte(expiry_date)
        session_min = D.SESSION_SCORE_MIN.get(session, 5)
        if dte == 0 and option_type == "CE":
            session_min = max(session_min, 6)
        if session_min >= 999:
            return result

        # ── LAYER 1: 3-MIN PERMISSION GATE ──────────────────
        permitted, det_3m, bonus_3m = _check_3min(token, option_type, profile, dte)
        result["details_3m"] = det_3m

        if not permitted:
            logger.info("[ENGINE] " + option_type
                        + " 3m gate BLOCKED: met=" + str(det_3m.get("conditions_met")) + "/4")
            return result

        # ── LAYER 2: REGIME ──────────────────────────────────
        spread_3m  = det_3m.get("ema_spread_3m", 0.0)
        abs_spread = abs(spread_3m)
        if abs_spread >= 12:   result["regime"] = "TRENDING_STRONG"
        elif abs_spread >= 5:  result["regime"] = "TRENDING"
        elif abs_spread >= 2:  result["regime"] = "NEUTRAL"
        else:                  result["regime"] = "CHOPPY"

        # v12.11: Spot regime backup when option is in momentum mode
        if det_3m.get("mode") == "MOMENTUM" and result["regime"] in ("NEUTRAL", "CHOPPY"):
            spot_regime = D.get_spot_regime("3minute")
            if spot_regime in ("TRENDING", "TRENDING_STRONG"):
                result["regime"] = spot_regime
                logger.info("[ENGINE] Spot regime override: " + spot_regime
                            + " (option was " + result["regime"] + ")")

        # v12.13: CE uses spot regime backup when option shows NEUTRAL/CHOPPY
        if option_type == "CE" and result["regime"] in ("NEUTRAL", "CHOPPY"):
            spot_regime = D.get_spot_regime("3minute")
            if spot_regime in ("TRENDING", "TRENDING_STRONG"):
                logger.info("[ENGINE] CE spot override: " + spot_regime + " (option was " + result["regime"] + ")")
                result["regime"] = spot_regime
            else:
                logger.info("[ENGINE] CE blocked — regime=" + result["regime"]
                            + " spot=" + spot_regime + " — both weak")
                return result

        # ── LAYER 3: SESSION ADJUSTMENT ──────────────────────
        if spread_3m > 5 and option_type == "PE":
            session_min += 1
        elif spread_3m < -5 and option_type == "CE":
            session_min += 1

        # ── LAYER 4A: 1-MIN SPREAD GATE ─────────────────────
        ema_spread = get_option_ema_spread(token, dte)
        result["ema_spread"] = ema_spread

        spread_1m = 0.0
        try:
            df1s = D.get_historical_data(token, "minute", D.LOOKBACK_1M)
            df1s = D.add_indicators(df1s)
            if not df1s.empty and len(df1s) >= 3:
                l1s = df1s.iloc[-2]
                # v12.11: Momentum fallback only DTE ≤ 1
                if dte <= 1 and len(df1s) < 25:
                    lb = min(5, len(df1s) - 2)
                    spread_1m = round(float(l1s["close"]) - float(df1s.iloc[-2 - lb]["close"]), 2)
                else:
                    spread_1m = round(
                        float(l1s.get("EMA_9",  l1s["close"])) -
                        float(l1s.get("EMA_21", l1s["close"])), 2)
        except Exception:
            pass
        result["spread_1m"] = spread_1m

        # Hard gate: CE needs +8pts, PE needs -6pts
        # Both CE and PE: option must be trending UP (spread positive)
        # CE needs stronger momentum (fighting premium decay)
        if option_type == "CE" and spread_1m < D.SPREAD_1M_MIN_CE:
            logger.info("[ENGINE] CE 1m spread BLOCKED: " + str(round(spread_1m,1))
                        + " need +" + str(D.SPREAD_1M_MIN_CE))
            return result
        if option_type == "PE" and spread_1m < D.SPREAD_1M_MIN_PE:
            logger.info("[ENGINE] PE 1m spread BLOCKED: " + str(round(spread_1m,1))
                        + " need +" + str(D.SPREAD_1M_MIN_PE))
            return result

        # ── LAYER 4B: 1-MIN SIGNAL ───────────────────────────
        ok_1m, det_1m = _check_1min(token, option_type, profile)
        result["details_1m"] = det_1m
        result["entry_price"] = det_1m.get("entry_price", 0.0)

        if det_1m.get("rsi_reject"):
            return result
        if not ok_1m:
            return result

        greeks = D.get_full_greeks(det_1m.get("entry_price", 0.0),
                                   spot_ltp, strike, expiry_date, option_type)
        result["greeks"] = greeks

        # ── LAYER 5: SCORING ─────────────────────────────────
        score, breakdown = score_entry(det_1m, greeks, profile,
                                       ema_spread, spread_1m, option_type)
        if bonus_3m:
            score += bonus_3m
            breakdown["gate_bonus"] = 1
            logger.info("[ENGINE] 3m gate bonus +1 (all 4 + spread≥8)")
        else:
            breakdown["gate_bonus"] = 0

        result["score"]           = score
        result["score_breakdown"] = breakdown
        result["prediction"]      = D.predict_trade(result["regime"], session, score)

        if score >= session_min:
            result["fired"] = True
            result["mode"]  = "CONVICTION"
        else:
            logger.info("[ENGINE] " + option_type + " score=" + str(score)
                        + "<" + str(session_min) + " — blocked")

        logger.info(
            "[ENGINE] " + option_type
            + " 3m=" + str(det_3m.get("conditions_met")) + "/4"
            + " regime=" + result["regime"]
            + " 1mspread=" + str(round(spread_1m,1))
            + " body=" + str(det_1m.get("body_pct"))
            + " rsi=" + str(det_1m.get("rsi_val"))
            + " rsi↑=" + str(det_1m.get("rsi_rising"))
            + " vol=" + str(det_1m.get("vol_ratio"))
            + " score=" + str(score) + "/" + str(session_min)
            + " → " + (result["mode"] if result["fired"] else "SKIP")
        )
        return result
    except Exception as e:
        logger.error("[ENGINE] check_entry error: " + str(e))
        return result

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

def compute_entry_sl(entry_price: float, profile: dict, mode: str,
                     token: int = None, dte: int = 99) -> float:
    """v12.12: ATR-based SL. v12.13: Expiry cap 20pts."""
    if mode in ("CONVICTION", "EXPIRY_BREAKOUT") and token:
        sl_pts = D.calculate_atr_sl(token, profile, entry_price)
    else:
        sl_pts = profile.get("conv_sl_pts", 20) if mode == "CONVICTION" else profile.get("scalp_sl_pts", 8)
        sl_pts = sl_pts or 8
    actual = max(sl_pts, _min_sl_pts(entry_price))
    # v12.13: Tighter cap on expiry day
    sl_cap = D.EXPIRY_SL_MAX if dte == 0 else D.ATR_SL_MAX
    actual = min(actual, sl_cap)
    if actual != sl_pts:
        logger.info("[ENGINE] SL adjusted " + str(round(sl_pts,1)) + "→" + str(actual)
                    + "pts (@" + str(entry_price) + ")"
                    + (" EXPIRY_CAP" if dte == 0 else ""))
    return round(entry_price - actual, 2)

def manage_exit(state: dict, option_ltp: float, profile: dict) -> tuple:
    if not state.get("in_trade"):
        return False, "", 0.0

    entry       = state.get("entry_price", 0)
    phase       = state.get("exit_phase", 1)
    mode        = state.get("mode", "CONVICTION")
    running_pnl = round(option_ltp - entry, 2)

    if running_pnl > state.get("peak_pnl", 0):
        state["peak_pnl"] = running_pnl

    if phase == 1:
        sl = state.get("phase1_sl", 0)
        if sl > 0 and option_ltp <= sl:
            return True, "PHASE1_SL", option_ltp

        # Stale entry: 5 candles held, peak < 5pts — cut early
        candles_held = state.get("candles_held", 0)
        peak_pnl     = state.get("peak_pnl", 0)
        if candles_held >= 5 and peak_pnl < 5.0:
            logger.info("[ENGINE] STALE_ENTRY: " + str(candles_held)
                        + " candles peak=" + str(peak_pnl) + "pts < 5")
            return True, "STALE_ENTRY", option_ltp

        if mode == "CONVICTION":
            be_pts = profile.get("conv_breakeven_pts", 20)
            if running_pnl >= be_pts:
                state["exit_phase"] = 2
                state["phase2_sl"]  = round(entry + 2.0, 2)
                logger.info("[ENGINE] Phase 1→2 @" + str(running_pnl)
                            + "pts SL=" + str(round(entry + 2.0, 2)))

    if state.get("exit_phase") == 2:
        if option_ltp <= state.get("phase2_sl", entry + 2.0):
            return True, "BREAKEVEN_SL", option_ltp
        be_pts = profile.get("conv_breakeven_pts", 20)

        # Ratchet SL up every 5pts of profit
        new_sl = round(entry + 2.0 + max(0, running_pnl - 10), 2)
        if new_sl > state.get("phase2_sl", entry + 2.0):
            old_sl = state["phase2_sl"]
            state["phase2_sl"] = new_sl
            logger.info("[ENGINE] Phase2 SL ratcheted "
                        + str(old_sl) + "→" + str(new_sl))

        if running_pnl >= be_pts * 1.2:
            state["exit_phase"] = 3
            logger.info("[ENGINE] Phase 2→3 trail @" + str(running_pnl) + "pts")

    if state.get("exit_phase") == 3:
        return _conviction_trail(state, option_ltp, profile)

    return False, "", 0.0

def _conviction_trail(state: dict, option_ltp: float, profile: dict) -> tuple:
    token     = state.get("token")
    entry     = state.get("entry_price", 0)
    tightened = state.get("trail_tightened", False)
    peak      = state.get("peak_pnl", 0)
    running   = round(option_ltp - entry, 2)

    # v12.12: Percentage-based drawdown (replaces flat 5/8pts)
    # v12.13: Use tighter 20% on expiry (DTE=0), 25% otherwise
    dte_at_entry = state.get("dte_at_entry", 99)
    drawdown_pct = D.EXPIRY_TRAIL_PCT if dte_at_entry == 0 else D.TRAIL_DRAWDOWN_PCT
    drawdown = peak - running
    max_drawdown = peak * (drawdown_pct / 100.0) if peak > 0 else 999

    if peak >= 15 and drawdown > max_drawdown:
        logger.info("[TRAIL] DRAWDOWN_EXIT: peak=" + str(peak)
                    + " drop=" + str(round(drawdown, 1))
                    + " limit=" + str(round(max_drawdown, 1))
                    + " (" + str(drawdown_pct) + "%"
                    + (" EXPIRY" if dte_at_entry == 0 else "") + ")")
        return True, "DRAWDOWN_EXIT", option_ltp

    # RSI checks
    rsi_1m = 0.0
    try:
        df1 = D.get_historical_data(token, "minute", 20)
        df1 = D.add_indicators(df1)
        if not df1.empty:
            rsi_1m = round(df1.iloc[-1].get("RSI", 0), 1)
    except Exception as e:
        logger.warning("[TRAIL] RSI fetch: " + str(e))

    rsi_exhaust_pnl = profile.get("rsi_exhaustion_pnl") or 15
    if rsi_1m >= 76 and running >= rsi_exhaust_pnl:
        logger.info("[TRAIL] RSI_EXHAUSTION: RSI=" + str(rsi_1m)
                    + " pnl=+" + str(running))
        state["_rsi_was_overbought"] = True
        return True, "RSI_EXHAUSTION", option_ltp

    if rsi_1m >= 76:
        state["_rsi_was_overbought"] = True

    gamma_rsi_drop = profile.get("gamma_rider_rsi_drop") or 65
    gamma_min_pnl  = profile.get("gamma_rider_min_pnl")  or 10
    if (state.get("_rsi_was_overbought")
            and rsi_1m < gamma_rsi_drop
            and running >= gamma_min_pnl):
        logger.info("[TRAIL] GAMMA_RIDER: RSI dropped to " + str(rsi_1m)
                    + " pnl=+" + str(running))
        state["_rsi_was_overbought"] = False
        return True, "GAMMA_RIDER", option_ltp

    # Trail tightening logic (unchanged)
    if not tightened:
        if rsi_1m > (profile.get("conv_rsi_tighten") or 76):
            state["trail_tightened"] = True
            tightened = True
            logger.info("[TRAIL] RSI=" + str(rsi_1m) + " trail tightened")

    if state.get("profit_locked") and not tightened:
        state["trail_tightened"] = True
        tightened = True

    if not tightened:
        try:
            df_s = D.get_historical_data(token, "3minute", D.LOOKBACK_3M)
            df_s = D.add_indicators(df_s)
            if len(df_s) >= 5:
                s_now   = df_s.iloc[-2].get("EMA_9",0) - df_s.iloc[-2].get("EMA_21",0)
                s_prev  = df_s.iloc[-3].get("EMA_9",0) - df_s.iloc[-3].get("EMA_21",0)
                s_prev2 = df_s.iloc[-4].get("EMA_9",0) - df_s.iloc[-4].get("EMA_21",0)
                if (abs(s_now) < abs(s_prev)) and (abs(s_prev) < abs(s_prev2)) and s_now > 0:
                    state["trail_tightened"] = True
                    tightened = True
                    logger.info("[TRAIL] EMA spread narrowing 2c → trail tightened")
        except Exception as e:
            logger.warning("[TRAIL] EMA spread check: " + str(e))

    tf    = profile.get("conv_tighten_tf", "3minute") if tightened else profile.get("conv_trail_tf", "5minute")
    label = "TIGHT" if tightened else "WIDE"

    peak_min = profile.get("peak_drawdown_min") or 0
    if peak_min and peak >= peak_min:
        floor = peak * (1 - (profile.get("peak_drawdown_pct") or 40) / 100)
        if running <= floor:
            return True, "PEAK_DRAWDOWN_" + label, option_ltp

    # v12.12: 2-candle EMA close rule
    # Need 2 consecutive closes below EMA9 to exit (not just 1)
    # One close below could be a wick/spike. Two = real reversal.
    try:
        df = D.get_historical_data(token, tf, D.LOOKBACK_5M)
        df = D.add_indicators(df)
        if df.empty or len(df) < 3:
            return False, "", 0.0
        last     = df.iloc[-2]
        prev     = df.iloc[-3]
        ema9     = last.get("EMA_9", option_ltp)
        ema9_p   = prev.get("EMA_9", option_ltp)
        close    = last["close"]
        close_p  = prev["close"]
        floor    = round(last["low"] - 12.0, 2)

        # Both current AND previous candle must close below EMA9
        below_now  = close < ema9
        below_prev = close_p < ema9_p
        if below_now and below_prev:
            logger.info("[TRAIL] 2-candle EMA exit: " + str(round(close, 1))
                        + "<" + str(round(ema9, 1)) + " AND prev "
                        + str(round(close_p, 1)) + "<" + str(round(ema9_p, 1)))
            return True, "TRAIL_" + label, option_ltp
        elif below_now:
            logger.debug("[TRAIL] 1 candle below EMA — waiting for confirmation")
        if option_ltp < floor:
            return True, "FLOOR_" + label, option_ltp
        return False, "", 0.0
    except Exception as e:
        logger.error("[TRAIL] EMA trail error: " + str(e))
        return False, "", 0.0


# ═══════════════════════════════════════════════════════════════
#  EXPIRY BREAKOUT MODE (v12.13)
#  Spot consolidation → breakout detection
#  No option RSI/EMA gate — spot is the trigger
#  Only active on DTE=0
# ═══════════════════════════════════════════════════════════════

def check_expiry_breakout(kite, spot_ltp: float, strike: int,
                          expiry_date, session: str) -> dict:
    """
    v12.13: Expiry-specific entry based on spot breakout.
    Returns same format as check_entry for compatibility.
    """
    result = {
        "fired": False, "mode": "", "score": 0,
        "details_1m": {}, "details_3m": {}, "greeks": {},
        "entry_price": 0.0, "ema_spread": 0.0,
        "regime": "UNKNOWN",
        "spread_1m": 0.0,
        "score_breakdown": {},
        "prediction": {},
        "breakout": {},
    }

    try:
        # Check expiry window
        from datetime import datetime
        now = datetime.now()
        if not D.is_expiry_window(now):
            return result

        # Detect spot breakout
        breakout = D.detect_spot_breakout(spot_ltp)
        result["breakout"] = breakout

        if not breakout["breakout"]:
            return result

        option_type = breakout["direction"]  # "CE" or "PE"
        magnitude   = breakout["magnitude"]

        logger.info("[EXPIRY] Breakout detected: " + option_type
                    + " magnitude=" + str(magnitude) + "pts"
                    + " consol_range=" + str(breakout["consolidation"]["range"])
                    + " fib=" + str(breakout["near_fib"].get("level", "—")))

        # Resolve strike — ATM or ATM-50 (slight ITM for higher delta)
        step = D.get_active_strike_step(0)  # 50-step on expiry
        atm  = D.resolve_atm_strike(spot_ltp, step)
        # For CE: ATM or ATM-50 (below spot). For PE: ATM or ATM+50 (above spot)
        if option_type == "CE":
            target_strike = atm - step  # Slight ITM CE
            if target_strike < atm - 100:
                target_strike = atm  # Fallback to ATM
        else:
            target_strike = atm + step  # Slight ITM PE
            if target_strike > atm + 100:
                target_strike = atm

        # Get token for the target strike
        tokens = D.get_option_tokens(kite, target_strike, expiry_date)
        if not tokens or option_type not in tokens:
            # Fallback to ATM
            tokens = D.get_option_tokens(kite, atm, expiry_date)
            target_strike = atm
            if not tokens or option_type not in tokens:
                logger.warning("[EXPIRY] No tokens for " + option_type + " " + str(target_strike))
                return result

        info  = tokens[option_type]
        token = info["token"]

        # Get option LTP
        option_ltp = D.get_ltp(token)
        if option_ltp <= 0:
            return result

        # Premium filter: ₹20-150 on expiry (wide range for paper testing)
        if option_ltp < 20 or option_ltp > 150:
            logger.info("[EXPIRY] Premium ₹" + str(round(option_ltp, 1))
                        + " outside 20-150 range — skip")
            return result

        # Quick option quality check — just volume, no RSI/EMA gate
        vol_ok = True
        try:
            df1 = D.get_historical_data(token, "minute", 10)
            if not df1.empty and len(df1) >= 3:
                last = df1.iloc[-2]
                vols = [df1.iloc[i]["volume"] for i in range(-5, -1) if i < len(df1) and df1.iloc[i]["volume"] > 0]
                avg_vol = sum(vols) / len(vols) if vols else 1
                vol_ratio = last["volume"] / avg_vol if avg_vol > 0 else 1
                vol_ok = vol_ratio >= 0.5  # Very relaxed — just needs some volume
                result["details_1m"] = {
                    "entry_price": round(float(last["close"]), 2),
                    "rsi_val": round(float(last.get("RSI", 50)), 1),
                    "body_pct": 0, "vol_ratio": round(vol_ratio, 2),
                    "rsi_rising": True, "rsi_ok": True, "body_ok": True,
                    "vol_ok": vol_ok, "rsi_reject": False,
                }
        except Exception:
            pass

        if not vol_ok:
            logger.info("[EXPIRY] Volume too thin — skip")
            return result

        # Get Greeks
        greeks = D.get_full_greeks(option_ltp, spot_ltp, target_strike,
                                    expiry_date, option_type)
        result["greeks"] = greeks

        # Fib proximity bonus
        fib_info = breakout.get("near_fib", {})
        fib_dist = abs(fib_info.get("distance", 999))
        fib_near = fib_dist <= D.EXPIRY_FIB_PROXIMITY

        # Score: simplified for expiry
        # Breakout magnitude + delta quality + volume + fib proximity
        score = 0
        breakdown = {}

        if magnitude >= 20:
            score += 2; breakdown["breakout_strong"] = 2
        elif magnitude >= 10:
            score += 1; breakdown["breakout"] = 1

        delta = abs(greeks.get("delta", 0))
        if 0.4 <= delta <= 0.65:
            score += 1; breakdown["delta"] = 1

        gamma = greeks.get("gamma", 0)
        if gamma >= 0.002:
            score += 1; breakdown["gamma"] = 1

        if vol_ok:
            score += 1; breakdown["volume"] = 1

        if fib_near:
            score += 1; breakdown["fib_proximity"] = 1
            logger.info("[EXPIRY] Near fib " + fib_info.get("level", "")
                        + " (" + str(fib_dist) + "pts)")

        result["score"]           = score
        result["score_breakdown"] = breakdown
        result["entry_price"]     = round(option_ltp, 2)
        result["regime"]          = "BREAKOUT"
        result["prediction"]      = D.predict_trade("TRENDING", session, score)

        # Fire if score >= 3 (relaxed for paper)
        if score >= 3:
            result["fired"]  = True
            result["mode"]   = "EXPIRY_BREAKOUT"
            result["symbol"] = info.get("symbol", "")
            result["token"]  = token
            result["direction"] = option_type
            result["strike"]    = target_strike

        logger.info(
            "[EXPIRY] " + option_type + " " + str(target_strike)
            + " ltp=₹" + str(round(option_ltp, 1))
            + " breakout=" + str(magnitude) + "pts"
            + " delta=" + str(round(delta, 3))
            + " gamma=" + str(round(gamma, 6))
            + " fib=" + (fib_info.get("level", "—") if fib_near else "no")
            + " score=" + str(score)
            + " → " + ("FIRE" if result["fired"] else "SKIP")
        )
        return result

    except Exception as e:
        logger.error("[EXPIRY] check_expiry_breakout error: " + str(e))
        return result
