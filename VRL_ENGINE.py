# ═══════════════════════════════════════════════════════════════
#  VRL_ENGINE.py — VISHAL RAJPUT TRADE v16.6
#  Entry: Golden 4 rules. Exit: strict 3 rules (Emergency / EOD / Vishal Trail).
#  Smart Trail v2+: INITIAL → BREAKEVEN → TRAIL_60 → TRAIL_75 → VISHAL_MAX → TRAIL_90
#  Exit on candle close, bulletproof margin check.
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

def _evaluate_entry_gates_pure(opt_3m, option_type: str, spot_ltp: float, now, market_open: bool,
                               state: dict, straddle_delta, spot_vwap, spot_for_vwap: float,
                               atm_strike: int, silent: bool = False, other_opt_3m=None) -> dict:
    # ── Golden 4 entry rules (Vishal V16.6 final) ──
    #   1. Time window 09:35 ≤ now ≤ 15:10
    #   2. Close > EMA9_low AND (ema9_high - ema9_low) > band_width_min
    #   3. EMA9_low slope flat or rising (last - N candles ago ≥ 0)
    #   4. Body ≥ body_pct_min (default 40%) of candle range
    result = {
        "fired": False, "entry_price": 0, "entry_mode": "", "ema9_high": 0, "ema9_low": 0,
        "close": 0, "open": 0, "high": 0, "low": 0, "candle_green": False, "body_pct": 0,
        "band_width": 0, "reject_reason": "", "band_position": "", "straddle_delta": None,
        "backbone_status": "N/A", "ema9_low_slope": 0.0,
    }
    try:
        body_min = CFG.entry_ema9_band("body_pct_min", 40)
        warmup_until = CFG.entry_ema9_band("warmup_until", "09:35")
        cutoff_after = CFG.entry_ema9_band("cutoff_after", "15:10")
        band_width_min = CFG.entry_ema9_band("band_width_min", 7)
        slope_lookback = int(CFG.entry_ema9_band("ema9_slope_lookback", 3))

        need_rows = max(4, slope_lookback + 2)
        if opt_3m is None or opt_3m.empty or len(opt_3m) < need_rows:
            result["reject_reason"] = "insufficient_3m_data"
            return result

        last = opt_3m.iloc[-2]
        close = float(last["close"]); open_ = float(last["open"])
        high = float(last["high"]); low = float(last["low"])
        ema9_high = float(last.get("ema9_high", 0))
        ema9_low  = float(last.get("ema9_low", 0))
        # Slope: current ema9_low minus ema9_low N candles ago (points over N bars).
        _ema9_low_past = float(opt_3m.iloc[-2 - slope_lookback].get("ema9_low", 0))
        ema9_low_slope = round(ema9_low - _ema9_low_past, 2)
        band_width = round(ema9_high - ema9_low, 2)

        # Always compute band_position + body_pct up-front so the
        # dashboard has values even when an early gate rejects.
        if close > ema9_high:
            _band_pos = "ABOVE"
        elif close < ema9_low:
            _band_pos = "BELOW"
        else:
            _band_pos = "IN"
        _candle_range = high - low
        _body_pct = round((abs(close - open_) / _candle_range * 100)
                          if _candle_range > 0 else 0, 1)

        result.update({
            "entry_price": round(close, 2), "ema9_high": round(ema9_high, 2),
            "ema9_low": round(ema9_low, 2), "close": round(close, 2), "open": round(open_, 2),
            "high": round(high, 2), "low": round(low, 2),
            "band_width": band_width,
            "ema9_low_slope": ema9_low_slope,
            "candle_green": (close > open_),
            "band_position": _band_pos,
            "body_pct": _body_pct,
        })

        # ── GATE 1: Time window (09:35-15:10) ──
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

        # ── GATE 2: Close > EMA9_low AND band_width > threshold ──
        if close <= ema9_low:
            result["reject_reason"] = "close_below_ema9_low"
            return result
        if band_width <= band_width_min:
            result["reject_reason"] = f"tight_band_{band_width}"
            return result

        # ── GATE 3: EMA9_low slope flat or rising ──
        if ema9_low_slope < 0:
            result["reject_reason"] = f"ema9_low_falling_{ema9_low_slope}"
            return result

        # ── GATE 4: Body ≥ body_pct_min ──
        # body_pct already computed up-front in result.update() above.
        if _body_pct < body_min:
            result["reject_reason"] = f"weak_body_{int(_body_pct)}pct"
            return result

        # ── All 4 gates passed ──
        result["fired"] = True
        result["entry_mode"] = "EMA9_BREAKOUT"
        result["ema9h_confirmed"] = (close > ema9_high)
        if not silent:
            logger.info(f"[ENGINE] {option_type} FIRED close={round(close,1)} "
                        f"ema9l={round(ema9_low,1)} band={band_width} "
                        f"slope={ema9_low_slope} body={int(_body_pct)}%")
        return result

    except Exception as e:
        logger.error("[ENGINE] Entry error: " + str(e))
        # CRITICAL: also reset fired=False so a NameError or other
        # exception in the success-path log line doesn't leave the
        # result with fired=True, which would let the main scan loop
        # enter a trade based on a corrupted result. (Found 2026-04-27
        # 09:35:35: body_pct→_body_pct refactor leftover threw
        # NameError after gates passed; trade fired anyway, lost 10 pts.)
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
        market_open=market_open, state=state, straddle_delta=None,
        spot_vwap=None, spot_for_vwap=spot_ltp, atm_strike=atm_strike,
        silent=silent, other_opt_3m=None)

def compute_entry_sl(entry_price: float, hard_sl: int = 10) -> float:
    return round(entry_price - hard_sl, 2)

def compute_trail_sl(entry_price: float, peak_pnl: float,
                     direction: str = "") -> tuple:
    """Smart Trail v2+ (Vishal V16.6 final).
    Priority: don't lose money. A BREAKEVEN tier snaps SL to entry once
    peak reaches +5, so trades that show any real profit never drop
    back into the Emergency SL zone.

    Tiers (ascending peak):
      peak <5:    INITIAL      SL = entry - 10       (Emergency zone)
      peak 5-8:   BREAKEVEN    SL = entry            (zero-loss guarantee)
      peak 8-15:  TRAIL_60     SL = entry + peak*0.60
      peak 15-30: TRAIL_75     SL = entry + peak*0.75
      peak 30-45: VISHAL_MAX   SL = entry + peak*0.85
      peak 45+:   TRAIL_90     SL = entry + peak*0.90
    """
    if peak_pnl >= 45:
        sl = entry_price + peak_pnl * 0.90
        tier = "TRAIL_90"
    elif peak_pnl >= 30:
        sl = entry_price + peak_pnl * 0.85
        tier = "VISHAL_MAX"
    elif peak_pnl >= 15:
        sl = entry_price + peak_pnl * 0.75
        tier = "TRAIL_75"
    elif peak_pnl >= 8:
        sl = entry_price + peak_pnl * 0.60
        tier = "TRAIL_60"
    elif peak_pnl >= 5:
        sl = entry_price
        tier = "BREAKEVEN"
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
            return [{"lot_id": "ALL", "reason": "VISHAL_TRAIL", "price": trail_sl}]
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

