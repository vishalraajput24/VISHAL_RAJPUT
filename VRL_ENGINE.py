# ═══════════════════════════════════════════════════════════════
#  VRL_ENGINE.py — VISHAL RAJPUT TRADE v16.7 (Vishal Clean)
#  Entry: 3 gates only.
#    1. GREEN candle (close > open)
#    2. Close > EMA9_low
#    3. Body % >= 40
#  Exit chain (first match wins):
#    1. EMERGENCY_SL    (PNL <= -10)
#    2. FLAT_2X         (EMA9_low slope flat 0..+3 for 2 consecutive candles)
#    3. EOD_EXIT        (15:20)
#    4. VISHAL_TRAIL    (peak-driven SL ladder below)
#  SL ladder (peak-driven ratchet, never moves down):
#    peak < 8         → SL = entry - 10        (INITIAL)
#    peak >=  8       → SL = entry +  3        (LOCK_3)
#    peak >= 12       → SL = entry +  5        (LOCK_5)
#    peak >= 15       → SL = entry +  8        (LOCK_8)
#    peak >= 20       → SL = entry + 15        (LOCK_15)
#    peak >= 21       → SL = entry + (peak-5)  (LOCK_DYN — 1pt added per peak pt)
#  Re-entry: after exit, watch next 2 candles; GREEN candle closing above
#  the original entry candle's close → re-enter same direction. Else fresh
#  setup only.
# ═══════════════════════════════════════════════════════════════

import logging
import time
from datetime import datetime, timedelta, time as _dtime
import pandas as pd
import VRL_DATA as D
import VRL_CONFIG as CFG

logger = logging.getLogger("vrl_live")


def get_margin_available(kite) -> float:
    """Return available cash margin. Returns -1.0 on error.
    (Inlined from VRL_TRADE so pre_entry_checks can call it without
    a lazy cross-module import.)"""
    try:
        margins = kite.margins(segment="equity")
        return float(margins.get("net", 0))
    except Exception as e:
        logger.error("[TRADE] Margin fetch error: " + str(e))
        return -1.0


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
    if not D.PAPER_MODE and kite is not None:
        try:
            avail = get_margin_available(kite)
            if avail < option_ltp * D.get_lot_size() * 1.2:
                return False, "Insufficient margin"
        except Exception:
            return False, "Margin check failed"
    return True, ""

def _evaluate_entry_gates_pure(opt_3m, option_type: str, spot_ltp: float, now,
                               market_open: bool, state: dict,
                               atm_strike: int, silent: bool = False) -> dict:
    # ── Vishal Clean Entry (v16.7) — 3 hard gates ──
    #   1. GREEN candle (close > open)
    #   2. Close > EMA9_low
    #   3. Body % >= 40
    # Time window (warmup/cutoff) is an operational rail, not a strategy
    # gate — kept so the bot never trades during indicator warmup or
    # within 5 min of EOD. Slope and band-width are tracked for display
    # + the in-trade FLAT_2X exit, but never block entry.
    result = {
        "fired": False, "entry_price": 0, "entry_mode": "", "ema9_high": 0, "ema9_low": 0,
        "close": 0, "open": 0, "high": 0, "low": 0, "candle_green": False, "body_pct": 0,
        "band_width": 0, "reject_reason": "", "band_position": "",
        "ema9_low_slope": 0.0,
        "band_width_slope": 0.0, "margin_above": 0,
    }
    try:
        body_min = CFG.entry_ema9_band("body_pct_min", 40)
        warmup_until = CFG.entry_ema9_band("warmup_until", "09:35")
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
        # Slope + band-width still computed for display + in-trade
        # FLAT_2X exit — they DO NOT block entry.
        ema9_low_slope = round(ema9_low - float(prev.get("ema9_low", 0)), 2)
        band_width = round(ema9_high - ema9_low, 2)
        _prev_band_width = round(
            float(prev.get("ema9_high", 0)) - float(prev.get("ema9_low", 0)), 2)
        band_width_slope = round(band_width - _prev_band_width, 2)

        if close > ema9_high:
            _band_pos = "ABOVE"
        elif close < ema9_low:
            _band_pos = "BELOW"
        else:
            _band_pos = "IN"
        _candle_range = high - low
        _body_pct = round((abs(close - open_) / _candle_range * 100)
                          if _candle_range > 0 else 0, 1)
        _is_green = (close > open_)
        _margin = round(close - ema9_low, 2)

        result.update({
            "entry_price": round(close, 2), "ema9_high": round(ema9_high, 2),
            "ema9_low": round(ema9_low, 2), "close": round(close, 2), "open": round(open_, 2),
            "high": round(high, 2), "low": round(low, 2),
            "band_width": band_width,
            "band_width_slope": band_width_slope,
            "ema9_low_slope": ema9_low_slope,
            "candle_green": _is_green,
            "band_position": _band_pos,
            "body_pct": _body_pct,
            "margin_above": _margin,
        })

        # ── Operational rail: time window ──
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

        # ── GATE 1: GREEN candle ──
        if not _is_green:
            result["reject_reason"] = "red_candle"
            return result

        # ── GATE 2: Close > EMA9_low ──
        if close <= ema9_low:
            result["reject_reason"] = "close_below_ema9_low"
            return result

        # ── GATE 3: Body % >= body_pct_min ──
        if _body_pct < body_min:
            result["reject_reason"] = f"weak_body_{int(_body_pct)}pct"
            return result

        # ── All 3 gates passed ──
        result["fired"] = True
        result["entry_mode"] = "EMA9_BREAKOUT"
        result["ema9h_confirmed"] = (close > ema9_high)
        if not silent:
            logger.info(f"[ENGINE] {option_type} FIRED close={round(close,1)} "
                        f"ema9l={round(ema9_low,1)} body={int(_body_pct)}% "
                        f"(slope={ema9_low_slope} band={band_width} display-only)")
        return result

    except Exception as e:
        logger.error("[ENGINE] Entry error: " + str(e))
        # CRITICAL: reset fired=False so an exception in the success-path
        # log line doesn't leave the result with fired=True (cost a -10
        # trade on 2026-04-27 — body_pct→_body_pct rename leftover).
        result["fired"] = False
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
    return _evaluate_entry_gates_pure(
        opt_3m=opt_3m, option_type=option_type, spot_ltp=spot_ltp, now=now,
        market_open=market_open, state=state, atm_strike=atm_strike,
        silent=silent)


def check_1min_peek(token: int, option_type: str, spot_ltp: float = 0,
                    silent: bool = False, state: dict = None) -> dict:
    """1-min early peek — same 3 gates as 3-min, but evaluated on the
    just-closed 1-min candle so a fresh breakout fires 1-2 minutes
    sooner than waiting for the 3-min boundary. EMA9_low is read from
    the latest 3-min row (still the strategy's reference band).

    Marked entry_mode='FAST' for the trade log + alerts."""
    if state is None: state = {}
    result = {
        "fired": False, "entry_price": 0, "entry_mode": "", "ema9_high": 0, "ema9_low": 0,
        "close": 0, "open": 0, "high": 0, "low": 0, "candle_green": False, "body_pct": 0,
        "band_width": 0, "reject_reason": "", "band_position": "",
        "ema9_low_slope": 0.0,
        "band_width_slope": 0.0, "margin_above": 0,
    }
    try:
        opt_3m = D.get_option_3min(token, lookback=15)
        opt_1m = D.get_option_1min(token, lookback=15)
        market_open = D.is_market_open()
        now = datetime.now()

        body_min = CFG.entry_ema9_band("body_pct_min", 40)
        warmup_until = CFG.entry_ema9_band("warmup_until", "09:35")
        cutoff_after = CFG.entry_ema9_band("cutoff_after", "15:10")

        if (opt_3m is None or opt_3m.empty or len(opt_3m) < 4
                or opt_1m is None or opt_1m.empty or len(opt_1m) < 4):
            result["reject_reason"] = "insufficient_1m_or_3m"
            return result

        last_3m = opt_3m.iloc[-2]
        prev_3m = opt_3m.iloc[-3]
        ema9_high = float(last_3m.get("ema9_high", 0))
        ema9_low  = float(last_3m.get("ema9_low", 0))
        ema9_low_slope = round(ema9_low - float(prev_3m.get("ema9_low", 0)), 2)
        band_width = round(ema9_high - ema9_low, 2)
        _prev_band_width = round(
            float(prev_3m.get("ema9_high", 0)) - float(prev_3m.get("ema9_low", 0)), 2)
        band_width_slope = round(band_width - _prev_band_width, 2)

        last_1m = opt_1m.iloc[-2]
        close = float(last_1m["close"]); open_ = float(last_1m["open"])
        high = float(last_1m["high"]); low = float(last_1m["low"])
        _candle_range = high - low
        _body_pct = round((abs(close - open_) / _candle_range * 100)
                          if _candle_range > 0 else 0, 1)
        _is_green = (close > open_)

        result.update({
            "entry_price": round(close, 2), "ema9_high": round(ema9_high, 2),
            "ema9_low": round(ema9_low, 2), "close": round(close, 2), "open": round(open_, 2),
            "high": round(high, 2), "low": round(low, 2),
            "band_width": band_width, "band_width_slope": band_width_slope,
            "ema9_low_slope": ema9_low_slope, "candle_green": _is_green,
            "body_pct": _body_pct,
            "band_position": "ABOVE" if close > ema9_high else ("BELOW" if close < ema9_low else "IN"),
            "margin_above": round(close - ema9_low, 2),
        })

        if market_open:
            mins = now.hour * 60 + now.minute
            warmup_mins = int(warmup_until.split(":")[0])*60 + int(warmup_until.split(":")[1])
            cutoff_mins = int(cutoff_after.split(":")[0])*60 + int(cutoff_after.split(":")[1])
            if mins < warmup_mins:
                result["reject_reason"] = "before_" + warmup_until; return result
            if mins >= cutoff_mins:
                result["reject_reason"] = "after_" + cutoff_after; return result

        # Same 3 gates as 3-min path: GREEN, close>EMA9_low, body>=min
        if not _is_green:
            result["reject_reason"] = "red_candle_1m"; return result
        if close <= ema9_low:
            result["reject_reason"] = "close_below_ema9_low_1m"; return result
        if _body_pct < body_min:
            result["reject_reason"] = f"weak_body_1m_{int(_body_pct)}pct"; return result

        result["fired"] = True
        result["entry_mode"] = "FAST"
        if not silent:
            logger.info(f"[ENGINE] {option_type} FAST(1m) FIRED close={round(close,1)} "
                        f"ema9l={round(ema9_low,1)} body={int(_body_pct)}%")
        return result
    except Exception as e:
        logger.error("[ENGINE] 1-min peek error: " + str(e))
        result["fired"] = False
        result["reject_reason"] = "error_1m_" + str(e)[:50]
        return result

def evaluate_cross_leg(self_dir: str, opt_3m_other) -> dict:
    """Cross-leg divergence signal — LOG ONLY for 1-week evaluation.

    Theory: a real bull move kills PE (PE_close < PE_ema9_low).
            A real bear move kills CE (CE_close < CE_ema9_low).
            If the OTHER leg is still holding above its own EMA9_low
            while THIS leg breaks out, the move is chop / fake.

    PASS = other leg dying (other_close < other_ema9_low) → real trend.
    FAIL = other leg holding (other_close >= other_ema9_low) → chop.
    NA   = no usable data on the other leg.

    NEVER blocks entry. Tracked in trade log so we can compute
    accuracy after a week and decide whether to promote it to a
    hard gate. Threshold target: PASS-win-rate > 55%.
    """
    out = {
        "xleg_signal":       "NA",
        "xleg_other_close":  0.0,
        "xleg_other_ema9l":  0.0,
        "xleg_other_dying":  False,
        "xleg_other_margin": 0.0,
    }
    try:
        if opt_3m_other is None or opt_3m_other.empty or len(opt_3m_other) < 2:
            return out
        last = opt_3m_other.iloc[-2]
        other_close = float(last["close"])
        other_ema9l = float(last.get("ema9_low", 0))
        # Guard against junk EMA9_low (token mid-warmup) — if 0, NA.
        if other_ema9l <= 0:
            return out
        other_dying = other_close < other_ema9l
        out["xleg_other_close"]  = round(other_close, 2)
        out["xleg_other_ema9l"]  = round(other_ema9l, 2)
        out["xleg_other_dying"]  = bool(other_dying)
        out["xleg_other_margin"] = round(other_close - other_ema9l, 2)
        out["xleg_signal"]       = "PASS" if other_dying else "FAIL"
    except Exception as e:
        logger.debug("[XLEG] eval err: " + str(e))
    return out


def compute_entry_sl(entry_price: float, hard_sl: int = 10) -> float:
    return round(entry_price - hard_sl, 2)

def compute_trail_sl(entry_price: float, peak_pnl: float,
                     direction: str = "") -> tuple:
    """Vishal Clean SL ladder (v16.7) — peak-driven ratchet, never moves down.

    Tiers (ascending peak):
      peak <  8:  INITIAL    SL = entry - 10            (Emergency zone)
      peak >= 8:  LOCK_3     SL = entry +  3
      peak >=12:  LOCK_5     SL = entry +  5
      peak >=15:  LOCK_8     SL = entry +  8
      peak >=20:  LOCK_15    SL = entry + 15            (wick-capture sweet spot)
      peak >=21:  LOCK_DYN   SL = entry + (peak - 5)    (1pt added per peak pt)

    Examples:
      peak 21 → SL +16    peak 25 → SL +20
      peak 30 → SL +25    peak 50 → SL +45
    """
    if peak_pnl >= 21:
        sl = entry_price + (peak_pnl - 5)
        tier = "LOCK_DYN"
    elif peak_pnl >= 20:
        sl = entry_price + 15
        tier = "LOCK_15"
    elif peak_pnl >= 15:
        sl = entry_price + 8
        tier = "LOCK_8"
    elif peak_pnl >= 12:
        sl = entry_price + 5
        tier = "LOCK_5"
    elif peak_pnl >= 8:
        sl = entry_price + 3
        tier = "LOCK_3"
    else:
        sl = entry_price - 10
        tier = "INITIAL"
    return round(sl, 2), tier

def _evaluate_exit_chain_pure(state: dict, option_ltp: float, opt_3m_full, now, market_open: bool) -> list:
    if not state.get("in_trade"): return []
    entry = state.get("entry_price", 0)
    pnl = round(option_ltp - entry, 2)
    peak = max(state.get("peak_pnl", 0), pnl)
    state["peak_pnl"] = peak
    if pnl <= CFG.exit_ema9_band("emergency_sl_pts", -10):
        return [{"lot_id": "ALL", "reason": "EMERGENCY_SL", "price": option_ltp}]
    if market_open:
        # Robust HH:MM parse — tolerates single-digit hours or minutes.
        _eod_str = CFG.exit_ema9_band("eod_exit_time", "15:20")
        try:
            _eh, _em = _eod_str.split(":")
            eod_mins = int(_eh) * 60 + int(_em)
        except Exception:
            eod_mins = 15 * 60 + 20  # safe default
        if now.hour*60 + now.minute >= eod_mins:
            return [{"lot_id": "ALL", "reason": "EOD_EXIT", "price": option_ltp}]

    # ── FLAT_2X bookkeeping (no exit yet — see priority below) ──
    # Update the flat-streak counter on every new closed candle so
    # state["_flat_candle_streak"] is current. Whether the streak
    # actually FIRES the exit depends on whether VISHAL_TRAIL has
    # priority on this same candle (see ordering below).
    #
    # CRITICAL pre-entry guard: only count candles whose close-time
    # is strictly AFTER entry_time. Without this, the very first
    # manage_exit() call after a fresh entry counts the just-closed
    # candle that BEGAN before the entry tick — its slope is from
    # pre-entry data and has nothing to do with the new trade. Worse,
    # pandas reports candle.name = bucket-start, so a 14:27-14:30
    # candle with close at 14:30 looks "post-entry" if entry was 14:30:30
    # but is actually fully pre-entry. Same guard the trail uses.
    # Bug seen 2026-04-27 PE 24100 re-entry: peak +14.4 → exit at
    # entry price because pre-entry 14:30 candle's slope contributed
    # to streak, FLAT_2X fired at Rs 89.8 (=entry) BEFORE trail at
    # LOCK_5 (Rs 94.8) could trigger.
    flat_max = float(CFG.exit_ema9_band("flat_slope_max", 3))
    _flat_2x_pending = None  # populated if streak just hit 2
    if opt_3m_full is not None and len(opt_3m_full) >= 3:
        try:
            _last_fl = opt_3m_full.iloc[-2]
            _prev_fl = opt_3m_full.iloc[-3]
            _last_close_t = (_last_fl.name + timedelta(minutes=3)).strftime("%Y-%m-%d %H:%M")
            # Pre-entry guard: skip if candle's close-time <= entry_time.
            _post_entry = True
            _et_str = state.get("entry_time") or ""
            if _et_str:
                try:
                    _h, _m, _s = (int(p) for p in _et_str.split(":"))
                    _et_t = _dtime(_h, _m, _s)
                    _candle_close_t_fl = (_last_fl.name + timedelta(minutes=3)).time()
                    if _candle_close_t_fl <= _et_t:
                        _post_entry = False
                except Exception:
                    pass  # fail open — count as post-entry
            if _post_entry and state.get("_last_flat_check_ts", "") != _last_close_t:
                _slope = round(float(_last_fl.get("ema9_low", 0))
                               - float(_prev_fl.get("ema9_low", 0)), 2)
                _is_flat = _slope <= flat_max
                _streak = int(state.get("_flat_candle_streak", 0) or 0)
                if _is_flat:
                    _streak += 1
                else:
                    _streak = 0
                state["_flat_candle_streak"] = _streak
                state["_last_flat_check_ts"] = _last_close_t
                state["_last_ema9_low_slope"] = _slope
                if _streak >= 2:
                    try:
                        _trig_t = (_last_fl.name + timedelta(minutes=3)).strftime("%H:%M")
                    except Exception:
                        _trig_t = ""
                    _flat_2x_pending = {
                        "lot_id": "ALL",
                        "reason": "FLAT_2X",
                        "price": float(_last_fl["close"]),
                        "trigger_close": round(float(_last_fl["close"]), 2),
                        "trigger_time": _trig_t,
                        "trigger_slope": _slope,
                    }
        except Exception as _fe:
            logger.debug("[ENGINE] flat-2x check: " + str(_fe))

    # ── VISHAL_TRAIL — checked BEFORE FLAT_2X ──────────────────
    # Reasoning: once the SL ladder has armed (peak >= 8 → LOCK_3+),
    # the trail SL represents a profit floor we already promised to
    # honor. If the same candle that triggers FLAT_2X is also at or
    # below the trail SL, we MUST exit at the trail (better outcome)
    # rather than at the FLAT_2X candle close (lower).
    # Bug fix: 2026-04-27 CE 24100 trade — peak +9.6 (LOCK_3 SL +3),
    # candle closed below trail but FLAT_2X fired first → captured
    # +1 instead of locking the +3 the ladder promised.
    trail_sl, trail_tier = compute_trail_sl(entry, peak)
    state["active_ratchet_tier"] = trail_tier
    state["active_ratchet_sl"] = trail_sl
    if trail_sl > 0:
        # Fetch data if not provided, capped at 10s total wait so the
        # strategy loop doesn't stall out of emergency-SL monitoring.
        # On miss we return [] — the next manage_exit call retries.
        if opt_3m_full is None or len(opt_3m_full) < 2:
            for _ in range(2):
                time.sleep(5)
                opt_3m_full = D.get_option_3min(state.get("token"), lookback=10)
                if opt_3m_full is not None and len(opt_3m_full) >= 2:
                    break
            else:
                return []  # no data — hold the trade, retry next tick
        # CRITICAL: use the last closed 3-min candle only if its close
        # time is strictly AFTER entry_time. Otherwise iloc[-2] points
        # at a pre-entry candle (the live 15:03-15:06 bar is still
        # forming at iloc[-1], so iloc[-2] is the 15:00-15:03 bar —
        # pre-entry data). Using that stale close fired the trail the
        # moment SL rose above it, costing the trade 50+ pts on fast
        # entries. `<=` rejects the exact-boundary entry (<1% case).
        _candle = opt_3m_full.iloc[-2]
        _et_str = state.get("entry_time") or ""
        _use = True
        if _et_str:
            try:
                _h, _m, _s = (int(p) for p in _et_str.split(":"))
                _et_t = _dtime(_h, _m, _s)
                _candle_close_t = (_candle.name + timedelta(minutes=3)).time()
                if _candle_close_t <= _et_t:
                    _use = False   # whole candle was pre-entry — hold
            except Exception:
                pass  # fail open — use candle as before
        if _use and _candle["close"] <= trail_sl:
            # Include the triggering candle data so the exit alert can
            # show "10:27 candle closed Rs93.85 (below SL Rs97.88)"
            # — eliminates the "price still above SL" confusion when
            # market bounces back AFTER the bot's decision.
            try:
                _trig_t = (_candle.name + timedelta(minutes=3)).strftime("%H:%M")
            except Exception:
                _trig_t = ""
            return [{
                "lot_id": "ALL",
                "reason": "VISHAL_TRAIL",
                "price": trail_sl,
                "trigger_close": round(float(_candle["close"]), 2),
                "trigger_time": _trig_t,
                "trigger_sl": round(trail_sl, 2),
            }]

    # ── FLAT_2X — fires only if VISHAL_TRAIL didn't ────────────
    # Soft signal: trend has stalled (slope <= +3 for 2 candles).
    # Take whatever profit (or small loss) is on the table now
    # rather than wait for the trail to drag it back further.
    if _flat_2x_pending is not None:
        # Reset streak so a stale flat doesn't leak into the next trade.
        state["_flat_candle_streak"] = 0
        return [_flat_2x_pending]
    return []

def manage_exit(state: dict, option_ltp: float, profile: dict, other_token: int = 0) -> list:
    if not state.get("in_trade"): return []
    opt_3m_full = None
    try:
        opt_3m_full = D.get_option_3min(state.get("token"), lookback=10)
    except: pass
    return _evaluate_exit_chain_pure(state, option_ltp, opt_3m_full, datetime.now(), D.is_market_open())


# ═══════════════════════════════════════════════════════════════
# === CHARGES (merged from VRL_CHARGES) ===
# ═══════════════════════════════════════════════════════════════
#  Brokerage & charges calculator. Pure math, no API calls.
#  Zerodha F&O charges as of April 2026.
#
#  lot_size is no longer a module-load constant.
#  calculate_lot_charges() looks it up from VRL_DATA at CALL TIME
#  when the caller doesn't pass an explicit value. This lets a
#  mid-session lot-size change (Zerodha has historically adjusted
#  NIFTY lots) flow through without a code edit or restart.

BROKERAGE_PER_ORDER = 20.0
STT_SELL_PCT = 0.000625           # 0.0625% on sell side
EXCHANGE_NSE_PCT = 0.000530       # 0.053% NSE F&O transaction
SEBI_TURNOVER_PCT = 0.000001      # ₹1 per crore
STAMP_DUTY_BUY_PCT = 0.00003      # 0.003% on buy side
GST_PCT = 0.18                    # 18% on (brokerage + exchange)


def _live_lot_size() -> int:
    """Runtime lookup of the active NIFTY lot size. Re-read on every
    call so a mid-session broker adjustment surfaces without a
    restart. Falls back to the historical default 65 only if
    VRL_DATA is somehow unavailable (e.g. unit test that imports
    ENGINE in isolation)."""
    try:
        lot = int(getattr(D, "LOT_SIZE", 0) or 0)
        if lot > 0:
            return lot
    except Exception:
        pass
    return 65


def calculate_charges(entry_price: float, exit_price: float,
                      qty: int, num_exit_orders: int = 1) -> dict:
    buy_turnover = entry_price * qty
    sell_turnover = exit_price * qty
    total_turnover = buy_turnover + sell_turnover

    gross_pnl = round((exit_price - entry_price) * qty, 2)
    gross_pts = round(exit_price - entry_price, 2)

    num_orders = 1 + num_exit_orders
    brokerage = round(BROKERAGE_PER_ORDER * num_orders, 2)
    stt = round(sell_turnover * STT_SELL_PCT, 2)
    exchange = round(total_turnover * EXCHANGE_NSE_PCT, 2)
    sebi = round(total_turnover * SEBI_TURNOVER_PCT, 2)
    stamp = round(buy_turnover * STAMP_DUTY_BUY_PCT, 2)
    gst = round((brokerage + exchange) * GST_PCT, 2)

    total_charges = round(brokerage + stt + exchange + sebi + stamp + gst, 2)
    net_pnl = round(gross_pnl - total_charges, 2)
    charges_pts = round(total_charges / qty, 2) if qty > 0 else 0
    net_pts = round(gross_pts - charges_pts, 2)

    return {
        "gross_pnl": gross_pnl, "gross_pts": gross_pts,
        "brokerage": brokerage, "stt": stt, "exchange": exchange,
        "sebi": sebi, "stamp": stamp, "gst": gst,
        "total_charges": total_charges, "charges_pts": charges_pts,
        "net_pnl": net_pnl, "net_pts": net_pts,
        "turnover": total_turnover, "num_orders": num_orders,
    }


def calculate_lot_charges(entry_price: float, exit_price: float,
                          lot_size: int = None) -> dict:
    """lot_size defaults to live VRL_DATA.LOT_SIZE when None,
    so the broker's current lot value flows through on every call
    instead of being frozen at module import."""
    if lot_size is None:
        lot_size = _live_lot_size()
    return calculate_charges(entry_price, exit_price, lot_size, num_exit_orders=1)

