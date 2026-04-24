#!/home/user/kite_env/bin/python3
# ═══════════════════════════════════════════════════════════════
#  VRL_ALERTS.py — VISHAL RAJPUT TRADE v16.0 (Part 5)
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
# ═══════════════════════════════════════════════════════════════

import logging
import threading
from datetime import datetime, timedelta

import VRL_DATA as D
import VRL_CONFIG as CFG

logger = logging.getLogger("vrl_live")

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
