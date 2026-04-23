# ═══════════════════════════════════════════════════════════════
#  VRL_ENGINE.py — VISHAL RAJPUT TRADE v16.5 FINAL
#  Vishal Close Trail: 60% → 85% → 80% → VISHAL_LOCK (+40)
#  Exit on candle close, bulletproof margin check
# ═══════════════════════════════════════════════════════════════

import logging
import threading
import time
from datetime import datetime, timedelta
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
    return _evaluate_entry_gates_pure(
        opt_3m=opt_3m, option_type=option_type, spot_ltp=spot_ltp, now=now,
        market_open=market_open, state=state, straddle_delta=None,
        spot_vwap=None, spot_for_vwap=spot_ltp, atm_strike=atm_strike,
        silent=silent, other_opt_3m=None)

def compute_entry_sl(entry_price: float, hard_sl: int = 12) -> float:
    return round(entry_price - hard_sl, 2)

def compute_trail_sl(entry_price: float, peak_pnl: float,
                     direction: str = "") -> tuple:
    """Vishal Close Trail: 60% → 85% → 80% → VISHAL_LOCK (+40)."""
    if peak_pnl >= 45:
        sl = entry_price + 40  # Hard lock, minimal giveback
        tier = "VISHAL_LOCK"
    elif peak_pnl >= 40:
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
        last_close = option_ltp
        if opt_3m_full is not None and len(opt_3m_full) >= 2:
            last_close = opt_3m_full.iloc[-2]["close"]
        else:
            for _ in range(7):
                time.sleep(5)
                opt_3m_full = D.get_option_3min(state.get("token"), lookback=10)
                if opt_3m_full is not None and len(opt_3m_full) >= 2:
                    last_close = opt_3m_full.iloc[-2]["close"]
                    break
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


# ═══════════════════════════════════════════════════════════════
# === CHARGES (merged from VRL_CHARGES) ===
# ═══════════════════════════════════════════════════════════════
#  Brokerage & charges calculator. Pure math, no API calls.
#  Zerodha F&O charges as of April 2026.
#
#  BUG-K: lot_size is no longer a module-load constant.
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
    """BUG-K: lot_size defaults to live VRL_DATA.LOT_SIZE when None,
    so the broker's current lot value flows through on every call
    instead of being frozen at module import."""
    if lot_size is None:
        lot_size = _live_lot_size()
    return calculate_charges(entry_price, exit_price, lot_size, num_exit_orders=1)


# ═══════════════════════════════════════════════════════════════
# === ALERTS (merged from VRL_ALERTS) ===
# ═══════════════════════════════════════════════════════════════
#  Pre-entry awareness alerts for learning mode. Educational only,
#  never triggers trades. Four signal families:
#
#    A. REVERSAL_BUILDING   🔔   bounce from below-band with body+RSI
#    B. APPROACHING_BREAKOUT ⏰   close within N pts of ema9_high + RSI↑
#    C. READY_TO_FIRE        ⚡   all entry gates pass except exactly one
#    D. BLOCKED_SETUP        ⚠️   valid breakout blocked by a hard gate
#
#  Rate-limited per (strike, side, signal_type) and globally per hour.
#  Toggleable at runtime via /alerts_on and /alerts_off.
#  Never sends during warmup (first 15 min of session) to avoid noise.

# BUG-L v15.2.5 Batch 5: dedicated lock for alert_history mutations.
# Today VRL_MAIN already copies alert_history in/out under _state_lock,
# so the state dict handed to detect_pre_entry_signals() is a private
# snapshot. This lock makes the helpers safe ANYWAY — if a future
# caller passes a shared state dict without holding _state_lock (e.g.
# a /status handler or a diagnostic script), _record() and
# _rate_limited() won't tear the history dict.
_alert_lock = threading.Lock()


# Alert keys are strings like "PE_24150_A" so state.alert_history is
# JSON-friendly and round-trips through vrl_live_state.json.
_EMOJI = {"A": "🔔", "B": "⏰", "C": "⚡", "D": "⚠️"}
_LABEL = {
    "A": "REVERSAL BUILDING",
    "B": "APPROACHING BREAKOUT",
    "C": "READY TO FIRE",
    "D": "BLOCKED",
}


def _cfg(key: str, default=None):
    return ((CFG.get().get("alerts") or {}).get("pre_entry") or {}).get(key, default)


def is_enabled(state: dict) -> bool:
    """Runtime toggle — state wins over config so /alerts_off persists."""
    # If state has explicit bool, respect it; else fall back to config default.
    if "pre_entry_alerts_enabled" in state:
        return bool(state["pre_entry_alerts_enabled"])
    return bool(_cfg("enabled", True))


def set_enabled(state: dict, flag: bool):
    state["pre_entry_alerts_enabled"] = bool(flag)


def _now_iso() -> str:
    return datetime.now().replace(microsecond=0).isoformat()


def _rate_limited(state: dict, key: str, window_min: int) -> bool:
    """Returns True if the specific (strike, side, type) key fired within
    the last `window_min` minutes — caller should skip sending.
    BUG-L: alert_history read protected by _alert_lock."""
    with _alert_lock:
        hist = state.get("alert_history") or {}
        last = hist.get(key)
    if not last:
        return False
    try:
        dt_last = datetime.fromisoformat(last)
    except Exception:
        return False
    return (datetime.now() - dt_last).total_seconds() / 60.0 < window_min


def _global_cap_exceeded(state: dict, cap_per_hour: int) -> bool:
    """BUG-L: snapshot alert_history under _alert_lock before iterating
    so we don't get RuntimeError: dictionary changed size during iteration
    if another thread is mutating it."""
    with _alert_lock:
        hist = dict(state.get("alert_history") or {})
    cutoff = datetime.now() - timedelta(hours=1)
    count = 0
    for _k, ts in hist.items():
        try:
            if datetime.fromisoformat(ts) > cutoff:
                count += 1
        except Exception:
            pass
    return count >= cap_per_hour


def _record(state: dict, key: str):
    """BUG-L: full read-modify-write of alert_history serialized under
    _alert_lock so two concurrent signals for different keys can't
    clobber each other's additions or each other's trim pass."""
    cutoff = datetime.now() - timedelta(hours=2)
    with _alert_lock:
        hist = dict(state.get("alert_history") or {})
        hist[key] = _now_iso()
        # Trim entries older than 2h to stop unbounded growth.
        fresh = {}
        for k, ts in hist.items():
            try:
                if datetime.fromisoformat(ts) > cutoff:
                    fresh[k] = ts
            except Exception:
                pass
        state["alert_history"] = fresh


def _key(strike: int, side: str, signal_type: str) -> str:
    return str(side) + "_" + str(int(strike or 0)) + "_" + str(signal_type)


# ── Signal detectors ─────────────────────────────────────────────
# Each detector inspects the current + prior 3-min bars of a side's
# option_3min DataFrame (fetched once by the caller for efficiency)
# plus the engine's check_entry() result dict. Returns either a dict
# {"type": "A"|"B"|"C"|"D", "msg": "..."} or None.

def _detect_reversal_building(side: str, strike: int, result: dict,
                               df) -> dict:
    """A — Two prior bars were weak (close ≤ ema9_low), current is a
    strong green bar (body ≥ 50%)."""
    if df is None or len(df) < 4:
        return None
    try:
        last = df.iloc[-2]
        p1 = df.iloc[-3]
        p2 = df.iloc[-4]
        close = float(last["close"]);   open_ = float(last["open"])
        ema9l = float(last.get("ema9_low", 0))
        if ema9l <= 0:
            return None
        body_range = float(last["high"]) - float(last["low"])
        body_pct = (abs(close - open_) / body_range * 100) if body_range > 0 else 0
        prev1_close  = float(p1["close"]);  prev1_ema9l = float(p1.get("ema9_low", 0))
        prev2_close  = float(p2["close"]);  prev2_ema9l = float(p2.get("ema9_low", 0))
        rsi_last = float(last.get("RSI", 50))
        rsi_prev = float(p1.get("RSI", 50))
        if not (close > open_):
            return None
        if body_pct < 50:
            return None
        if rsi_last <= rsi_prev:
            return None
        if not (prev1_close <= prev1_ema9l and prev2_close <= prev2_ema9l):
            return None
        msg = ("🔔 " + side + " " + str(strike) + " REVERSAL BUILDING\n"
               "Close ₹" + str(round(close, 1))
               + " | EMA9-low ₹" + str(round(ema9l, 1))
               + " | Green body " + str(int(body_pct)) + "%\n"
               "RSI rising " + str(round(rsi_prev, 1)) + " → " + str(round(rsi_last, 1)) + "\n"
               "Setup may fire next candle if momentum continues")
        return {"type": "A", "msg": msg}
    except Exception as e:
        logger.debug("[ALERTS] A detector err: " + str(e))
        return None


def _detect_approaching_breakout(side: str, strike: int, result: dict,
                                  df) -> dict:
    """B — Close within 3 pts below ema9_high, RSI rising 2 candles,
    at least 1 of last 2 green."""
    gap_max = float(_cfg("approaching_breakout_gap_pts", 3))
    if df is None or len(df) < 4:
        return None
    try:
        last = df.iloc[-2]
        p1 = df.iloc[-3]
        p2 = df.iloc[-4]
        close = float(last["close"])
        ema9h = float(last.get("ema9_high", 0))
        if ema9h <= 0:
            return None
        if close >= ema9h:
            return None  # B only fires BELOW the band (approaching, not crossed)
        if (ema9h - close) > gap_max:
            return None
        rsi_last = float(last.get("RSI", 50))
        rsi_p1   = float(p1.get("RSI", 50))
        rsi_p2   = float(p2.get("RSI", 50))
        if not (rsi_last > rsi_p1 and rsi_p1 > rsi_p2):
            return None
        greens = sum(1 for b in (last, p1) if float(b["close"]) > float(b["open"]))
        if greens < 1:
            return None
        msg = ("⏰ " + side + " " + str(strike) + " APPROACHING BREAKOUT\n"
               "Close ₹" + str(round(close, 1))
               + " vs EMA9-high ₹" + str(round(ema9h, 1))
               + " (" + str(round(ema9h - close, 1)) + "pts away)\n"
               "RSI " + str(round(rsi_p2, 1)) + " → " + str(round(rsi_p1, 1))
               + " → " + str(round(rsi_last, 1))
               + ", greens " + str(greens) + "/2\n"
               "Watch next 1–2 candles for cross above band")
        return {"type": "B", "msg": msg}
    except Exception as e:
        logger.debug("[ALERTS] B detector err: " + str(e))
        return None


def _detect_ready_to_fire(side: str, strike: int, result: dict,
                           df) -> dict:
    """C — Engine said NOT fired, but the reject_reason points at
    exactly one remaining gate. We look at the reject and classify:
    weak_body, cooldown, narrow_band, warmup/cutoff → one-gate-short.
    If straddle_bleed is the reason, that's covered by D (BLOCKED)."""
    if result.get("fired"):
        return None
    reason = str(result.get("reject_reason", "") or "")
    if not reason:
        return None
    close = float(result.get("close", 0) or 0)
    ema9h = float(result.get("ema9_high", 0) or 0)
    body  = float(result.get("body_pct", 0) or 0)
    sd    = result.get("straddle_delta")
    green = bool(result.get("candle_green", False))

    # Only fire C when the "setup outline" is healthy:
    # price is above the band AND candle is green AND straddle data OK.
    if not (close > ema9h and green):
        return None
    blocker = None
    if reason.startswith("weak_body"):
        blocker = "body " + str(int(body)) + "% < 30"
    elif reason.startswith("cooldown"):
        blocker = "cooldown active (" + reason + ")"
    elif reason.startswith("narrow_band"):
        blocker = reason.replace("_", " ")
    elif reason.startswith("before_") or reason.startswith("after_"):
        blocker = "time window (" + reason + ")"
    else:
        return None

    msg = ("⚡ " + side + " " + str(strike) + " READY TO FIRE\n"
           "Close ₹" + str(round(close, 1))
           + " > EMA9-high ₹" + str(round(ema9h, 1)) + " ✓\n"
           "Green ✓ | Body " + str(int(body)) + "% "
           + ("✓" if body >= 30 else "✗") + "\n"
           "Straddle Δ" + (str(sd) if sd is not None else "n/a")
           + (" ✓" if sd is not None else "") + "\n"
           "MISSING: " + str(blocker))
    return {"type": "C", "msg": msg}


def _detect_blocked_setup(side: str, strike: int, result: dict,
                           df) -> dict:
    """D — Fresh breakout + green + body≥30 but a HARD gate
    (straddle tier, cooldown, time window) blocked. The educational
    part: show WHICH filter did the blocking."""
    if result.get("fired"):
        return None
    reason = str(result.get("reject_reason", "") or "")
    if not reason:
        return None
    close = float(result.get("close", 0) or 0)
    ema9h = float(result.get("ema9_high", 0) or 0)
    body  = float(result.get("body_pct", 0) or 0)
    green = bool(result.get("candle_green", False))
    if not (close > ema9h and green and body >= 30):
        return None
    # Only alert when the HARD gate was the blocker. already_above_band
    # and below_band are breakout-quality fails, not filter blocks.
    hard_gates = ("straddle_bleed", "straddle_data_unavailable",
                  "cooldown_", "before_", "after_", "narrow_band")
    if not any(reason.startswith(p) for p in hard_gates):
        return None
    msg = ("⚠️ " + side + " " + str(strike) + " BLOCKED\n"
           "Breakout valid but gate blocked entry\n"
           "Close ₹" + str(round(close, 1))
           + " > EMA9-high ₹" + str(round(ema9h, 1)) + " ✓\n"
           "Green body " + str(int(body)) + "% ✓\n"
           "BLOCKED BY: " + reason)
    return {"type": "D", "msg": msg}


# ── Public entry point ──────────────────────────────────────────

def detect_pre_entry_signals(all_results: dict, state: dict,
                              dfs: dict = None) -> list:
    """Returns list of {"type", "key", "msg"} dicts ready for Telegram.
    `all_results` = {"CE": result_dict, "PE": result_dict} from
    check_entry(). `dfs` optionally maps side -> option_3min DataFrame
    (pre-fetched by the main loop). Rate-limit bookkeeping mutates
    state['alert_history']."""
    if not is_enabled(state):
        return []
    rate_min = int(_cfg("rate_limit_per_key_minutes", 15))
    cap      = int(_cfg("global_hourly_cap", 10))
    types_on = _cfg("signal_types", {}) or {}
    if _global_cap_exceeded(state, cap):
        return []

    out = []
    for side in ("CE", "PE"):
        r = (all_results or {}).get(side) or {}
        if not r:
            continue
        strike = int(r.get("_strike") or r.get("atm_strike_used") or 0)
        if not strike:
            continue
        df = (dfs or {}).get(side)

        detectors = [
            ("A", "reversal_building",     _detect_reversal_building),
            ("B", "approaching_breakout",  _detect_approaching_breakout),
            ("C", "ready_to_fire",         _detect_ready_to_fire),
            ("D", "blocked_setup",         _detect_blocked_setup),
        ]
        for code, cfg_key, fn in detectors:
            if not bool(types_on.get(cfg_key, True)):
                continue
            # BUG-P v15.2.5 Batch 6: isolate each detector. Individual
            # detectors already have internal try/except but a future
            # refactor might forget one, and an unhandled exception
            # would abort the outer loop — killing every later detector
            # for BOTH sides this tick. Wrap here too so the blast
            # radius is exactly one (detector, side) pair.
            try:
                sig = fn(side, strike, r, df)
            except Exception as _fe:
                logger.warning("[ALERTS] detector " + code + " (" + side
                               + " " + str(strike) + ") raised: "
                               + type(_fe).__name__ + " " + str(_fe))
                continue
            if not sig:
                continue
            key = _key(strike, side, code)
            if _rate_limited(state, key, rate_min):
                continue
            if _global_cap_exceeded(state, cap):
                break
            _record(state, key)
            out.append({"type": code, "key": key, "msg": sig["msg"]})
            logger.info("[ALERTS] " + _LABEL[code] + " " + side
                        + " " + str(strike) + " → queued")
    return out
