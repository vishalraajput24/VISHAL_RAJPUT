# ═══════════════════════════════════════════════════════════════
#  VRL_ENGINE.py — VISHAL RAJPUT TRADE v13.0
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
    """v13.0 minimal entry — 1-min EMA gap + RSI only."""
    result = {
        "fired": False, "option_type": option_type,
        "entry_price": 0, "ema9": 0, "ema21": 0,
        "ema_gap": 0, "rsi": 0, "rsi_prev": 0,
        "ema_ok": False, "rsi_ok": False,
    }
    try:
        df = D.get_historical_data(token, "minute", 20)
        df = D.add_indicators(df)
        if df.empty or len(df) < 3:
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
        ema_min = CFG.get().get("entry", {}).get("ema_gap_min", 3)
        rsi_min = CFG.get().get("entry", {}).get("rsi_min", 50)

        # CHECK 1: EMA gap trending
        ema_ok = ema_gap >= ema_min

        # CHECK 2: RSI rising above minimum
        rsi_ok = rsi >= rsi_min and rsi > rsi_prev

        # CHECK 3: Green candle — last closed candle must be bullish
        # Prevents entering on a red candle where momentum just died
        candle_green = float(curr["close"]) > float(curr["open"])

        # CHECK 4: EMA gap WIDENING — gap must be growing, not shrinking
        # Prevents entering at the top when EMA gap already peaked
        prev_ema9 = float(prev.get("EMA_9", 0))
        prev_ema21 = float(prev.get("EMA_21", 0))
        prev_gap = round(prev_ema9 - prev_ema21, 2)
        gap_widening = ema_gap > prev_gap

        result["ema_ok"] = ema_ok
        result["rsi_ok"] = rsi_ok
        result["candle_green"] = candle_green
        result["gap_widening"] = gap_widening

        all_pass = ema_ok and rsi_ok and candle_green and gap_widening

        if all_pass:
            result["fired"] = True
            logger.info("[ENGINE] " + option_type + " ENTRY SIGNAL"
                + " ema_gap=" + str(ema_gap) + "(w:" + str(prev_gap) + ")"
                + " rsi=" + str(round(rsi,1)) + ">" + str(round(rsi_prev,1))
                + " green=Y entry=" + str(entry_price))
        else:
            reasons = []
            if not ema_ok: reasons.append("ema=" + str(ema_gap) + "❌")
            else: reasons.append("ema=" + str(ema_gap) + "✅")
            if not rsi_ok: reasons.append("rsi=" + str(round(rsi,1)) + "❌")
            else: reasons.append("rsi=" + str(round(rsi,1)) + "✅")
            if not candle_green: reasons.append("RED❌")
            if not gap_widening: reasons.append("gap_shrink❌")
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
    """v13.0 exit — profit floors + RSI split + ATR trail.
    Returns list of exit dicts: [{"lots": "ALL" or 1, "lot_id": "LOT1"/"LOT2"/"ALL", "reason": str, "price": float}]
    Returns [] if no exit.
    """
    if not state.get("in_trade"):
        return []

    entry = state.get("entry_price", 0)
    running = round(option_ltp - entry, 2)
    peak = state.get("peak_pnl", 0)
    candles = state.get("candles_held", 0)
    lots_split = state.get("lots_split", False)
    lot1_active = state.get("lot1_active", True)
    lot2_active = state.get("lot2_active", True)

    if running > peak:
        state["peak_pnl"] = running
        peak = running
    if running < state.get("trough_pnl", 0):
        state["trough_pnl"] = running

    # Get RSI
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
    stale_candles = CFG.get().get("exit", {}).get("stale_candles", 3)
    stale_peak = CFG.get().get("exit", {}).get("stale_peak_min", 3)
    rsi_split = CFG.get().get("rsi_exit", {}).get("split_at", 70)
    rsi_spike = CFG.get().get("rsi_exit", {}).get("sell_spike", 75)
    rsi_blowoff = CFG.get().get("rsi_exit", {}).get("blowoff", 80)

    exits = []

    # STALE EXIT
    if candles >= stale_candles and peak < stale_peak:
        logger.info("[ENGINE] STALE_ENTRY: " + str(candles) + " candles, peak=" + str(peak))
        return [{"lots": "ALL", "lot_id": "ALL", "reason": "STALE_ENTRY", "price": option_ltp}]

    # HARD SL
    if running <= -hard_sl:
        logger.info("[ENGINE] HARD_SL: running=" + str(running))
        return [{"lots": "ALL", "lot_id": "ALL", "reason": "HARD_SL", "price": option_ltp}]

    # PROFIT FLOOR calculation
    floors = CFG.get().get("profit_floors", [])
    floor_sl = entry - hard_sl
    for f in sorted(floors, key=lambda x: x["peak"]):
        if peak >= f["peak"]:
            floor_sl = entry + f["lock"]
    state["current_floor"] = round(floor_sl, 2)

    # RSI BLOWOFF > 80
    if rsi > rsi_blowoff and (lot1_active or lot2_active):
        logger.info("[ENGINE] RSI_BLOWOFF: rsi=" + str(round(rsi,1)))
        return [{"lots": "ALL", "lot_id": "ALL", "reason": "RSI_BLOWOFF", "price": option_ltp}]

    # RSI 75-80: sell lot 1
    if rsi >= rsi_spike and rsi <= rsi_blowoff and lot1_active and lots_split:
        logger.info("[ENGINE] RSI_SPIKE: lot1 exit, rsi=" + str(round(rsi,1)))
        exits.append({"lots": 1, "lot_id": "LOT1", "reason": "RSI_SPIKE", "price": option_ltp})
        state["lot1_active"] = False

    # RSI >= 70: split lots
    if rsi >= rsi_split and not lots_split:
        state["lots_split"] = True
        logger.info("[ENGINE] RSI " + str(round(rsi,1)) + " — lots SPLIT")

    # LOT 2 ATR TRAIL (after split)
    if lots_split and lot2_active:
        try:
            import pandas as pd
            df2 = D.get_historical_data(token, "minute", 20)
            if not df2.empty and len(df2) >= 15:
                highs = df2["high"].astype(float)
                lows = df2["low"].astype(float)
                closes = df2["close"].astype(float).shift(1)
                tr = pd.concat([highs-lows, (highs-closes).abs(), (lows-closes).abs()], axis=1).max(axis=1)
                atr = float(tr.rolling(14).mean().iloc[-1])
                atr_mult = CFG.get().get("trail", {}).get("atr_multiplier", 1.5)
                atr_trail = option_ltp - (atr * atr_mult)
                atr_trail = max(atr_trail, floor_sl)
                prev_trail = state.get("lot2_trail_sl", floor_sl)
                if atr_trail > prev_trail:
                    state["lot2_trail_sl"] = round(atr_trail, 2)
                if option_ltp <= state.get("lot2_trail_sl", floor_sl):
                    logger.info("[ENGINE] ATR_TRAIL: lot2 exit at " + str(round(option_ltp,2)))
                    exits.append({"lots": 1, "lot_id": "LOT2", "reason": "ATR_TRAIL", "price": option_ltp})
                    state["lot2_active"] = False
        except Exception:
            pass

    # PROFIT FLOOR HIT
    if not lots_split:
        if option_ltp <= floor_sl and peak >= 10:
            logger.info("[ENGINE] PROFIT_FLOOR: both lots at " + str(round(floor_sl,2)))
            return [{"lots": "ALL", "lot_id": "ALL", "reason": "PROFIT_FLOOR", "price": option_ltp}]
    else:
        if lot1_active and option_ltp <= floor_sl:
            logger.info("[ENGINE] FLOOR_SL: lot1 at " + str(round(floor_sl,2)))
            exits.append({"lots": 1, "lot_id": "LOT1", "reason": "FLOOR_SL", "price": option_ltp})
            state["lot1_active"] = False

    return exits


# ═══════════════════════════════════════════════════════════════
#  EXPIRY BREAKOUT MODE (v12.15)
#  Spot consolidation → breakout detection
#  No option RSI/EMA gate — spot is the trigger
#  Only active on DTE=0
# ═══════════════════════════════════════════════════════════════

def check_expiry_breakout(kite, spot_ltp: float, strike: int,
                          expiry_date, session: str) -> dict:
    """
    v12.15: Expiry-specific entry based on spot breakout.
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
                vols = [df1.iloc[i]["volume"] for i in range(-5, -1) if abs(i) <= len(df1) and df1.iloc[i]["volume"] > 0]
                avg_vol = sum(vols) / len(vols) if vols else 1
                vol_ratio = last["volume"] / avg_vol if avg_vol > 0 else 1
                vol_ok = vol_ratio >= 0.5  # Very relaxed — just needs some volume
                result["details_1m"] = {
                    "entry_price": round(float(last["close"]), 2),
                    "rsi_val": round(float(last.get("RSI", 50)), 1),
                    "body_pct": 0, "vol_ratio": round(vol_ratio, 2),
                    "rsi_rising": True, "rsi_ok": True, "body_ok": True,
                    "vol_ok": vol_ok, "rsi_reject": False, "rsi_reject_reason": "",
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
