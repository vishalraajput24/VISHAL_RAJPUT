# ═══════════════════════════════════════════════════════════════
#  VRL_MAIN.py — VISHAL RAJPUT TRADE v15.2
#  Master orchestration. Dual EMA9 Band Breakout strategy.
#  Entry: close > EMA9-high (fresh) + green + body ≥ 30% + band width ≥ 8
#  Exit: 5-rule chain. BE+2 lock after peak ≥ 5. Primary stop = EMA9-low close break.
# ═══════════════════════════════════════════════════════════════

import csv
import json
import logging
import os
import requests
import signal
import sys
import threading
import time
import zipfile
from copy import deepcopy
from datetime import date, datetime, timedelta

# ── Bootstrap dirs first ────────────────────────────────────────
import VRL_DATA as D
D.ensure_dirs()

from VRL_DATA   import setup_logger
from VRL_AUTH   import get_kite
from VRL_ENGINE import (
    check_entry, manage_exit, pre_entry_checks,
    loss_streak_gate, check_profit_lock,
    compute_entry_sl,
    get_option_ema_spread,
)
# VRL_TRADE handles both paper and live mode
import VRL_CONFIG as CFG
from VRL_TRADE import place_entry, place_exit

from VRL_LAB    import start_lab
import VRL_CHARGES as CHARGES

# ── Loggers ─────────────────────────────────────────────────────
logger     = setup_logger("vrl_live", D.LIVE_LOG_FILE)
lab_logger = setup_logger("vrl_lab",  D.LAB_LOG_FILE)

# ── Telegram base ───────────────────────────────────────────────
_TG_BASE = "https://api.telegram.org/bot"

# Global kite instance for REST fallback
_kite = None

# ═══════════════════════════════════════════════════════════════
#  STATE (re‑entry fields removed)
# ═══════════════════════════════════════════════════════════════

_state_lock = threading.Lock()

DEFAULT_STATE = {
    # ── Position ───────────────────────────────────────────
    "in_trade"           : False,
    "symbol"             : "",
    "token"              : None,
    "direction"          : "",
    "strike"             : 0,
    "expiry"             : "",
    "entry_price"        : 0.0,
    "entry_time"         : "",
    "qty"                : D.get_lot_size(),
    "lot_count"          : 2,
    # ── Exit state (v15.0: band-based trailing, no fixed floors) ──
    "peak_pnl"           : 0.0,
    "trough_pnl"         : 0.0,
    "candles_held"       : 0,
    "force_exit"         : False,
    "_exit_failed"       : False,
    # ── v15.0 entry context (captured at entry, displayed at exit) ──
    "entry_mode"         : "",
    "entry_ema9_high"    : 0.0,
    "entry_ema9_low"     : 0.0,
    "entry_band_position": "",
    "entry_body_pct"     : 0.0,
    "current_ema9_high"  : 0.0,
    "current_ema9_low"   : 0.0,
    "last_band_check_ts" : "",
    # ── v15.2 entry context (straddle + VWAP) ──
    "entry_straddle_delta"     : 0.0,
    "entry_straddle_threshold" : 0.0,
    "entry_straddle_period"    : "",
    "entry_atm_strike"         : 0,
    "entry_band_width"         : 0.0,
    "entry_spot_vwap"          : 0.0,
    "entry_spot_vs_vwap"       : 0.0,
    "entry_vwap_bonus"         : "",
    "entry_straddle_info"      : "",
    # v15.2.5 velocity stall tracking (per 3-min candle)
    "peak_history"       : [],
    "last_peak_candle_ts": "",
    "current_velocity"   : 0.0,
    # BUG-J: one-shot flag so backfill runs only once per trade
    "_peak_history_backfilled": False,
    # BUG-V: date sentinel for "run lab cleanup once per trading day"
    "_last_cleanup_date" : "",
    # v15.2.5 pre-entry alerts (learning mode)
    "pre_entry_alerts_enabled": True,
    "alert_history"      : {},   # key -> ISO timestamp
    # v16.0 ratchet state
    "active_ratchet_tier": "",
    "active_ratchet_sl"  : 0.0,
    # v15.1 BE+2 lock (legacy, kept for state compat)
    "be2_active"         : False,
    "be2_level"          : 0.0,
    "score_at_entry"     : 0,
    "other_token"        : 0,
    # ── Last exit memory (cooldown) ────────────────────────
    "last_exit_time"     : "",
    "last_exit_direction": "",
    "last_exit_peak"     : 0.0,
    "last_exit_reason"   : "",
    # ── Daily counters ─────────────────────────────────────
    "daily_trades"       : 0,
    "daily_losses"       : 0,
    "daily_pnl"          : 0.0,
    "consecutive_losses" : 0,
    "profit_locked"      : False,
    # ── Bot control ────────────────────────────────────────
    "paused"             : False,
    "_circuit_breaker"   : False,
    "_error_count"       : 0,
    # ── Daily reset flags ──────────────────────────────────
    "_eod_reported"      : False,
    "_eod_exited"        : False,
    "_bias_done"         : False,
    "_straddle_done"     : False,
    "_hourly_rsi_ts"     : 0,
    "_vix_warned"        : False,
    "_straddle_alerted"  : False,
    # ── Loop bookkeeping ───────────────────────────────────
    "_last_1min_candle"  : "",
    "_last_dash_scan_min": "",
    "_last_warmup_log"   : "",
    "_last_scan"         : {},
    "_relock_skip_count" : 0,
    "prev_close"         : 0.0,
    # ── Exchange order tracking (live mode — legacy compat) ──
    "_sl_order_id"       : "",
    "_sl_trigger_at_exchange": 0,
    "phase1_sl"          : 0.0,   # legacy: VRL_TRADE may still use for SL-M
    "exit_phase"         : 1,     # legacy
    "lot1_active"        : True,  # legacy (always True in v15.0)
    "lot2_active"        : True,  # legacy (always True in v15.0)
    "lots_split"         : False, # legacy (always False in v15.0)
    "current_floor"      : 0.0,   # legacy (used for dashboard trail display)
    "current_rsi"        : 0.0,   # legacy
    "_candle_low"        : 0.0,   # legacy
    "_last_milestone"    : 0,     # legacy
    "_static_floor_sl"   : 0.0,   # legacy
}

state   = deepcopy(DEFAULT_STATE)
_running = True

# ═══════════════════════════════════════════════════════════════
#  STRIKE LOCKING — stable scanning, no flickering
# ═══════════════════════════════════════════════════════════════

_locked_ce_strike = None
_locked_pe_strike = None
_locked_at_spot   = None
_locked_tokens    = {}
_LOCK_SHIFT_THRESHOLD = 150  # relock if spot moves 150+ pts
_last_dash_args = {}  # cached dashboard args for post-exit refresh

def _lock_strikes(spot, dte, kite=None, expiry=None):
    """Lock CE/PE strikes and subscribe tokens. Only called on relock."""
    global _locked_ce_strike, _locked_pe_strike, _locked_at_spot, _locked_tokens
    _locked_ce_strike = D.resolve_strike_for_direction(spot, "CE", dte)
    _locked_pe_strike = D.resolve_strike_for_direction(spot, "PE", dte)
    _locked_at_spot = spot
    _locked_tokens = {}

    if kite and expiry:
        for _dt, _strike in [("CE", _locked_ce_strike), ("PE", _locked_pe_strike)]:
            _tk = D.get_option_tokens(kite, _strike, expiry)
            if _tk.get(_dt):
                _locked_tokens[_dt] = _tk[_dt]

        # Subscribe tokens permanently — no unsub/resub flicker
        _sub_tokens = [v["token"] for v in _locked_tokens.values() if v.get("token")]
        if _sub_tokens:
            D.subscribe_tokens(_sub_tokens)

    logger.info("[MAIN] Strikes LOCKED: CE=" + str(_locked_ce_strike)
                + " PE=" + str(_locked_pe_strike)
                + " at spot=" + str(round(spot, 1)))

def _reset_strike_lock():
    """Reset lock after trade exit or session start."""
    global _locked_ce_strike, _locked_pe_strike, _locked_at_spot, _locked_tokens
    _locked_ce_strike = None
    _locked_pe_strike = None
    _locked_at_spot = None
    _locked_tokens = {}

# ═══════════════════════════════════════════════════════════════
#  STATE PERSISTENCE
# ═══════════════════════════════════════════════════════════════

def _save_state():
    try:
        with _state_lock:
            subset = {k: state.get(k) for k in D.STATE_PERSIST_FIELDS}
        tmp    = D.STATE_FILE_PATH + ".tmp"
        with open(tmp, "w") as f:
            json.dump(subset, f, indent=2, default=str)
        os.replace(tmp, D.STATE_FILE_PATH)
    except Exception as e:
        logger.error("[MAIN] State save error: " + str(e))

def _load_state():
    if not os.path.isfile(D.STATE_FILE_PATH):
        return
    try:
        with open(D.STATE_FILE_PATH) as f:
            saved = json.load(f)
        with _state_lock:
            for k, v in saved.items():
                if k in state:
                    state[k] = v
        logger.info("[MAIN] State loaded from disk")
        if state.get("in_trade"):
            logger.info("[MAIN] ⚠ Was in trade on last shutdown — "
                        + str(state.get("symbol")) + " monitoring resumed")
            _tg_send(
                "🔄 <b>Bot restarted mid-trade</b>\n"
                "Symbol : " + str(state.get("symbol")) + "\n"
                "Phase  : " + str(state.get("exit_phase")) + "\n"
                "Resuming exit monitoring."
            )
    except Exception as e:
        logger.error("[MAIN] State load error: " + str(e))


def _reconcile_positions(kite):
    """
    Startup position reconciliation — compare saved state with broker.
    If bot crashed mid-trade and position is gone at broker, reset state.
    If broker has position but state says no trade, alert for manual resolution.
    v13.1: Verified — runs on live startup, alerts on mismatch, never auto-closes.
    """
    if kite is None or D.PAPER_MODE:
        return
    try:
        positions = kite.positions()
        net = positions.get("net", [])
        # Find NFO positions with non-zero quantity
        nfo_positions = [p for p in net
                         if p.get("exchange") == "NFO"
                         and p.get("quantity", 0) != 0
                         and "NIFTY" in p.get("tradingsymbol", "")]

        saved_in_trade = state.get("in_trade", False)
        saved_symbol = state.get("symbol", "")

        if saved_in_trade and not nfo_positions:
            logger.warning("[RECONCILE] State says in_trade but NO broker position for "
                           + saved_symbol + " — resetting state")
            _tg_send(
                "⚠️ <b>POSITION MISMATCH</b>\n"
                "State : in_trade (" + saved_symbol + ")\n"
                "Broker: NO position found\n"
                "Action: State reset. Position was likely squared off manually."
            )
            with _state_lock:
                state["in_trade"] = False
                state["symbol"] = ""
                state["token"] = None

        elif not saved_in_trade and nfo_positions:
            symbols = [p["tradingsymbol"] for p in nfo_positions]
            logger.warning("[RECONCILE] State says NOT in_trade but broker has positions: "
                           + str(symbols))
            _tg_send(
                "⚠️ <b>POSITION MISMATCH</b>\n"
                "State : NOT in trade\n"
                "Broker: " + ", ".join(symbols) + "\n"
                "Action: Manual resolution needed. Bot will NOT auto-exit."
            )

        elif saved_in_trade and nfo_positions:
            broker_syms = [p["tradingsymbol"] for p in nfo_positions]
            if saved_symbol not in broker_syms:
                logger.warning("[RECONCILE] Symbol mismatch: state=" + saved_symbol
                               + " broker=" + str(broker_syms))
                _tg_send(
                    "⚠️ <b>SYMBOL MISMATCH</b>\n"
                    "State : " + saved_symbol + "\n"
                    "Broker: " + ", ".join(broker_syms) + "\n"
                    "Manual resolution needed."
                )
            else:
                logger.info("[RECONCILE] Position confirmed: " + saved_symbol)
        else:
            logger.info("[RECONCILE] Clean — no positions, no saved trade")

    except Exception as e:
        logger.error("[RECONCILE] Position check failed: " + str(e)
                     + " — continuing with saved state")


def _reset_daily(today_str: str):
    with _state_lock:
        state["daily_trades"]          = 0
        state["daily_losses"]          = 0
        state["daily_pnl"]             = 0.0
        state["profit_locked"]         = False
        state["_eod_reported"]         = False
        state["_eod_exited"]           = False
        state["aggressive_mode"]       = False
        state["paused"]                = False
        state["_bias_done"]            = False
        state["_straddle_done"]        = False
        state["_hourly_rsi_ts"]        = 0
        state["_vix_warned"]           = False
        state["_straddle_alerted"]     = False
    D.clear_token_cache()
    D.reset_daily_warnings()
    _reset_strike_lock()
    logger.info("[MAIN] _eod_exited reset for new day")
    with _state_lock:
        state["_shadow_exit_eod_sent"] = False
    # v15.2 Part 4: reset shadow state on new trading day
    try:
        import VRL_SHADOW
        VRL_SHADOW.reset_day()
    except Exception:
        pass
    # v15.2.5: clear pre-entry alert rate-limit history at daily rollover
    with _state_lock:
        state["alert_history"] = {}
    # DB maintenance
    try:
        import VRL_DB as _DB
        _DB.cleanup_old_db_data()
        from datetime import date as _d
        if _d.today().weekday() == 6:  # Sunday
            _DB.vacuum_db()
    except Exception:
        pass
    logger.info("[MAIN] Daily reset")
    _save_state()

# ═══════════════════════════════════════════════════════════════
#  PID FILE
# ═══════════════════════════════════════════════════════════════

def _write_pid():
    try:
        with open(D.PID_FILE_PATH, "w") as f:
            f.write(str(os.getpid()))
    except Exception:
        pass

def _remove_pid():
    try:
        if os.path.isfile(D.PID_FILE_PATH):
            os.remove(D.PID_FILE_PATH)
    except Exception:
        pass

# ═══════════════════════════════════════════════════════════════
#  TRADE LOG
# ═══════════════════════════════════════════════════════════════

# v15.2.5 BUG-N7: live columns only (matches _TRADE_FIELDS in VRL_DB.py).
# Dead v13 fields (mode, score, iv_at_entry, regime, spread_1m, spread_3m,
# delta_at_entry, straddle_decay, signal_price, bonus_*, momentum_pts,
# rsi_rising, spot_confirms, spot_move) removed.
TRADE_FIELDNAMES = [
    "date", "entry_time", "exit_time", "symbol", "direction", "strike",
    "entry_price", "exit_price", "pnl_pts", "pnl_rs",
    "gross_pnl_rs", "net_pnl_rs",
    "peak_pnl", "trough_pnl", "exit_reason", "exit_phase",
    "dte", "candles_held", "session", "sl_pts",
    "bias", "vix_at_entry", "hourly_rsi", "entry_mode",
    "brokerage", "stt", "exchange_charges", "gst", "stamp_duty",
    "total_charges", "num_exit_orders", "qty_exited",
    "entry_slippage", "exit_slippage", "lot_id",
    # v15.2 entry/exit context
    "entry_ema9_high", "entry_ema9_low",
    "exit_ema9_high", "exit_ema9_low",
    "entry_band_position", "exit_band_position",
    "entry_body_pct",
    "entry_straddle_delta", "entry_straddle_threshold",
    "entry_straddle_period", "entry_straddle_info",
    "entry_atm_strike", "entry_band_width",
    "entry_spot_vwap", "entry_spot_vs_vwap", "entry_vwap_bonus",
]

def _cleanup_trade_log():
    """One-time cleanup: remove corrupted rows where date doesn't match YYYY-MM-DD."""
    path = D.TRADE_LOG_PATH
    if not os.path.isfile(path):
        return
    try:
        import re
        date_re = re.compile(r"^\d{4}-\d{2}-\d{2}$")
        with open(path, "r") as f:
            reader = csv.DictReader(f)
            if not reader.fieldnames:
                return
            good_rows = [r for r in reader if date_re.match(r.get("date", ""))]
        # Rewrite with correct header + good rows only
        with open(path, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=TRADE_FIELDNAMES, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(good_rows)
        logger.info("[MAIN] Trade log cleaned: " + str(len(good_rows)) + " valid rows kept")
    except Exception as e:
        logger.warning("[MAIN] Trade log cleanup error: " + str(e))

def _compute_exit_band_position(exit_price: float,
                                current_ema9_high, current_ema9_low) -> str:
    """v15.2.5: where was price vs the band when we exited? ABOVE / IN / BELOW."""
    try:
        px = float(exit_price or 0)
        eh = float(current_ema9_high or 0)
        el = float(current_ema9_low  or 0)
        if eh <= 0 and el <= 0:
            return ""
        if px > eh:
            return "ABOVE"
        if px < el:
            return "BELOW"
        return "IN"
    except Exception:
        return ""


def _log_trade(st: dict, exit_price: float, exit_reason: str,
               candles_held: int = 0, saved_entry: float = None,
               lot_id: str = "ALL", qty: int = 0):
    os.makedirs(os.path.dirname(D.TRADE_LOG_PATH), exist_ok=True)
    is_new  = not os.path.isfile(D.TRADE_LOG_PATH)
    entry   = saved_entry if saved_entry is not None else st.get("entry_price", 0)
    pnl_pts = round(exit_price - entry, 2)
    _lot_qty = qty if qty > 0 else D.get_lot_size()
    pnl_rs  = round(pnl_pts * _lot_qty, 2)

    # v15.2.5 BUG-N7: live columns only. Dead v13 fields purged.
    row = {
        "date"          : date.today().isoformat(),
        "entry_time"    : st.get("entry_time", ""),
        "exit_time"     : datetime.now().strftime("%H:%M:%S"),
        "symbol"        : st.get("symbol", ""),
        "direction"     : st.get("direction", ""),
        "strike"        : st.get("strike", 0),
        "entry_price"   : entry,
        "exit_price"    : round(exit_price, 2),
        "pnl_pts"       : pnl_pts,
        "pnl_rs"        : pnl_rs,
        "peak_pnl"      : round(st.get("peak_pnl", 0), 2),
        "trough_pnl"    : round(st.get("trough_pnl", 0), 2),
        "exit_reason"   : exit_reason,
        "exit_phase"    : st.get("exit_phase", 1),
        "dte"           : st.get("dte_at_entry", 0),
        "candles_held"  : candles_held,
        "session"       : st.get("session_at_entry", ""),
        "sl_pts"        : st.get("sl_pts_at_entry", 0),
        "bias"          : D.get_daily_bias(),
        "vix_at_entry"  : round(D.get_vix(), 1),
        "hourly_rsi"    : D.get_hourly_rsi(),
        "entry_mode"    : st.get("entry_mode", "EMA9_BREAKOUT"),
        "entry_slippage": st.get("entry_slippage", 0),
        "exit_slippage" : 0,
        "lot_id"        : lot_id,
        "qty_exited"    : _lot_qty,
        # v15.2.5 fix: persist v15.2 entry/exit context so the trades table
        # stops writing zeros / empty strings. All values are captured in
        # state at entry (see VRL_MAIN strategy loop) + refreshed via
        # manage_exit(); the exit-time band is whatever was last seen.
        "entry_ema9_high":     round(float(st.get("entry_ema9_high", 0) or 0), 2),
        "entry_ema9_low":      round(float(st.get("entry_ema9_low",  0) or 0), 2),
        "exit_ema9_high":      round(float(st.get("current_ema9_high", 0) or 0), 2),
        "exit_ema9_low":       round(float(st.get("current_ema9_low",  0) or 0), 2),
        "entry_band_position": st.get("entry_band_position", "") or "",
        "exit_band_position":  _compute_exit_band_position(
                                    exit_price,
                                    st.get("current_ema9_high", 0),
                                    st.get("current_ema9_low", 0)),
        "entry_body_pct":      round(float(st.get("entry_body_pct", 0) or 0), 1),
        "entry_straddle_delta":     round(float(st.get("entry_straddle_delta", 0) or 0), 2),
        "entry_straddle_threshold": round(float(st.get("entry_straddle_threshold", 0) or 0), 2),
        "entry_straddle_period":    st.get("entry_straddle_period", "") or "",
        "entry_atm_strike":    int(st.get("entry_atm_strike", 0) or 0),
        "entry_band_width":    round(float(st.get("entry_band_width", 0) or 0), 2),
        "entry_spot_vwap":     round(float(st.get("entry_spot_vwap", 0) or 0), 2),
        "entry_spot_vs_vwap":  round(float(st.get("entry_spot_vs_vwap", 0) or 0), 2),
        "entry_vwap_bonus":    st.get("entry_vwap_bonus", "") or "",
        # v15.2.5 Fix 5: straddle classification captured at entry
        "entry_straddle_info": st.get("entry_straddle_info", "") or "",
    }

    # Fix strike: use locked strike from state, fallback to ATM calculation
    if not row["strike"] or row["strike"] == 0:
        try:
            _spot = D.get_ltp(D.NIFTY_SPOT_TOKEN)
            if _spot > 0:
                _step = D.get_active_strike_step(st.get("dte_at_entry", 0))
                row["strike"] = D.resolve_atm_strike(_spot, _step)
        except Exception:
            pass

    # Calculate charges
    _num_exit_orders = 1  # per-lot logging = 1 exit order each
    _qty = _lot_qty
    try:
        _ch = CHARGES.calculate_charges(entry, exit_price, _qty, _num_exit_orders)
        row["brokerage"] = _ch["brokerage"]
        row["stt"] = _ch["stt"]
        row["exchange_charges"] = _ch["exchange"]
        row["gst"] = _ch["gst"]
        row["stamp_duty"] = _ch["stamp"]
        row["total_charges"] = _ch["total_charges"]
        row["gross_pnl_rs"] = _ch["gross_pnl"]
        row["net_pnl_rs"] = _ch["net_pnl"]
        row["pnl_rs"] = _ch["gross_pnl"]  # override to match charges calc qty
        row["num_exit_orders"] = _num_exit_orders
    except Exception:
        pass

    try:
        with open(D.TRADE_LOG_PATH, "a", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=TRADE_FIELDNAMES, extrasaction="ignore")
            if is_new:
                writer.writeheader()
            writer.writerow(row)
            f.flush()
    except Exception as e:
        logger.error("[MAIN] Trade log error: " + str(e))

    # Dual write: SQLite
    try:
        import VRL_DB as _DB
        _DB.insert_trade(row)
    except Exception as _dbe:
        logger.warning("[MAIN] DB insert_trade error: " + str(_dbe))

def _read_today_trades() -> list:
    today_str = date.today().isoformat()
    trades    = []
    if not os.path.isfile(D.TRADE_LOG_PATH):
        return trades
    try:
        with open(D.TRADE_LOG_PATH, "r") as f:
            for row in csv.DictReader(f):
                if row.get("date", "") == today_str:
                    trades.append(row)
    except Exception as e:
        logger.error("[MAIN] Read trades error: " + str(e))
    return trades

# ═══════════════════════════════════════════════════════════════
#  TELEGRAM — SEND HELPERS
# ═══════════════════════════════════════════════════════════════

# Dynamic public IP — resolved once at module load
_WEB_IP = ""
try:
    import subprocess as _sp
    _WEB_IP = _sp.check_output(["curl", "-s", "ifconfig.me"], timeout=5).decode().strip()
except Exception:
    _WEB_IP = "unknown"

def _now_str() -> str:
    return datetime.now().strftime("%H:%M:%S")

def _mode_tag() -> str:
    return "📄 PAPER" if D.PAPER_MODE else "💰 LIVE"

def _rs(pts: float) -> str:
    rupees = round(pts * D.get_lot_size(), 0)
    sign   = "+" if rupees >= 0 else ""
    return sign + "₹" + str(int(rupees))

def _short_sym(symbol: str, direction: str = "", strike: int = 0) -> str:
    """CE 22600 from direction+strike. Fallback to symbol suffix."""
    if direction and strike:
        return direction + " " + str(strike)
    if not symbol:
        return ""
    # Extract CE/PE from end of symbol
    if symbol.endswith("CE"):
        return "CE"
    elif symbol.endswith("PE"):
        return "PE"
    return symbol

from collections import deque as _deque
_tg_timestamps = _deque(maxlen=20)
_TG_FLOOD_LIMIT = 5
_TG_FLOOD_WINDOW = 10  # seconds

def _tg_safe(s) -> str:
    """BUG-031: Escape <, >, & in dynamic content for Telegram HTML mode.
    Apply only to user/API-supplied strings, NOT to template literals."""
    if s is None:
        return ""
    try:
        return (str(s).replace("&", "&amp;")
                     .replace("<", "&lt;")
                     .replace(">", "&gt;"))
    except Exception:
        return ""


def _tg_send_sync(text: str, parse_mode: str = "HTML", chat_id: str = None,
                  priority: str = "normal") -> bool:
    """Blocking send with flood control — max 5 msgs per 10s.

    BUG-U v15.2.5 Batch 6: `priority="critical"` bypasses flood
    control entirely. Use for CRITICAL alerts that MUST reach the
    operator during a burst (exit failure, shutdown-with-open-trade,
    DB corruption) even if the loop has already queued 5 messages
    in the last 10 seconds. Critical sends still count toward the
    sliding window so a second non-critical call right after won't
    get an immediate free pass."""
    if not D.TELEGRAM_TOKEN or not (chat_id or D.TELEGRAM_CHAT_ID):
        return False

    is_critical = (str(priority).lower() == "critical")

    # Flood control — prevent Telegram 429 rate limit.
    # CRITICAL messages bypass the wait (BUG-U) but still append to
    # the timestamp queue so bookkeeping stays accurate.
    now_ts = time.time()
    while _tg_timestamps and now_ts - _tg_timestamps[0] > _TG_FLOOD_WINDOW:
        _tg_timestamps.popleft()
    if not is_critical and len(_tg_timestamps) >= _TG_FLOOD_LIMIT:
        wait = _TG_FLOOD_WINDOW - (now_ts - _tg_timestamps[0])
        if wait > 0:
            time.sleep(min(wait, _TG_FLOOD_WINDOW))
    _tg_timestamps.append(time.time())

    cid = chat_id or D.TELEGRAM_CHAT_ID
    url = _TG_BASE + D.TELEGRAM_TOKEN + "/sendMessage"
    # BUG-031: sanitize unknown HTML tags in HTML mode to prevent parse errors.
    # Telegram allows <b>, <i>, <u>, <s>, <code>, <pre>, <a href>. Everything
    # else (like stray <html> from an error trace) causes a 400.
    _safe_text = text
    if parse_mode == "HTML":
        try:
            import re as _re
            _safe_text = _re.sub(
                r"<(?!/?(b|i|u|s|code|pre|a)(\s|>|/))",
                "&lt;", text)
        except Exception:
            _safe_text = text
    try:
        resp = requests.post(url, json={
            "chat_id"              : cid,
            "text"                 : _safe_text,
            "parse_mode"           : parse_mode,
            "disable_notification" : False,
        }, timeout=10)
        if not resp.ok:
            logger.warning("[TG] Send failed: " + resp.text[:200])
            return False
        return True
    except Exception as e:
        logger.error("[TG] send error: " + type(e).__name__)
        return False

def _tg_send(text: str, parse_mode: str = "HTML", chat_id: str = None,
             priority: str = "normal") -> bool:
    """Non-blocking send — fires in background thread so strategy loop never waits.
    BUG-U: `priority` passed through to _tg_send_sync for CRITICAL bypass."""
    t = threading.Thread(
        target=_tg_send_sync,
        args=(text, parse_mode, chat_id, priority),
        daemon=True
    )
    t.start()
    return True

def _tg_send_file(file_path: str, caption: str = "", chat_id: str = None) -> bool:
    if not D.TELEGRAM_TOKEN:
        return False
    cid = chat_id or D.TELEGRAM_CHAT_ID
    url = _TG_BASE + D.TELEGRAM_TOKEN + "/sendDocument"
    try:
        with open(file_path, "rb") as f:
            resp = requests.post(url, data={
                "chat_id": cid,
                "caption": caption[:1024],
            }, files={"document": f}, timeout=60)
        if not resp.ok:
            logger.warning("[TG] File send failed: " + resp.text[:200])
            return False
        return True
    except Exception as e:
        logger.error("[TG] send_file error: " + type(e).__name__)
        return False

def _tg_inline_keyboard(text: str, keyboard: list, chat_id: str = None) -> dict:
    if not D.TELEGRAM_TOKEN:
        return {}
    cid = chat_id or D.TELEGRAM_CHAT_ID
    url = _TG_BASE + D.TELEGRAM_TOKEN + "/sendMessage"
    try:
        resp = requests.post(url, data={
            "chat_id"      : cid,
            "text"         : text,
            "parse_mode"   : "HTML",
            "reply_markup" : json.dumps({"inline_keyboard": keyboard}),
        }, timeout=10)
        if resp.ok:
            return resp.json().get("result", {})
    except Exception as e:
        logger.error("[TG] keyboard error: " + type(e).__name__)
    return {}

def _tg_answer_callback(callback_query_id: str, text: str = ""):
    url = _TG_BASE + D.TELEGRAM_TOKEN + "/answerCallbackQuery"
    try:
        requests.post(url, json={
            "callback_query_id": callback_query_id,
            "text": text,
        }, timeout=5)
    except Exception:
        pass

# ═══════════════════════════════════════════════════════════════
#  TELEGRAM — TRADE ALERTS
# ═══════════════════════════════════════════════════════════════

def _alert_bot_started():
    _web_url = "http://" + _WEB_IP + ":8080" if _WEB_IP and _WEB_IP != "unknown" else "http://localhost:8080"
    _acct = D.get_account_info()
    _acct_line = ""
    if _acct.get("name"):
        _acct_line = ("Account: " + _acct["name"]
                      + " (" + _acct.get("user_id", "") + ")\n"
                      "Balance: ₹" + "{:,}".format(int(_acct.get("total_balance", 0))) + "\n")
    _tg_send(
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "🚀 <b>VISHAL RAJPUT TRADE " + D.VERSION + "</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "Time   : " + _now_str() + "\n"
        "Mode   : " + _mode_tag() + "\n"
        + _acct_line +
        "Web    : " + _web_url + "\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "🤖 <b>VISHAL RAJPUT TRADE v16.0</b>\n"
        + _mode_tag() + " | EMA9 Band Breakout (3-min option candles)\n"
        "ENTRY: close &gt; EMA9-high (fresh) + green + body 30% + Straddle tiered\n"
        "EXIT: Ratchet 5-tier | 1m EMA9 break | Velocity stall | Emergency -20 | EOD 15:30\n"
        "2 lots fixed | No entry 9:15-9:30 or after 15:10\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "/help for commands"
    )
    if not D.PAPER_MODE:
        _tg_send(
            "🔴🔴🔴🔴🔴🔴🔴🔴🔴🔴\n"
            "⚡ <b>LIVE MODE — REAL MONEY</b> ⚡\n"
            "🔴🔴🔴🔴🔴🔴🔴🔴🔴🔴\n"
            "Account: " + str(D.get_account_info().get("name", "")) + "\n"
            "Balance: ₹" + "{:,}".format(int(D.get_account_info().get("total_balance", 0))) + "\n"
            "Lots: 2 × " + str(D.get_lot_size()) + " = " + str(D.get_lot_size() * 2) + " qty\n"
            "Stop: dynamic EMA9-low close break (trail) | Emergency -20pts\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "Every order uses REAL money.\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
        )

def _alert_profit_lock(daily_pnl: float):
    _tg_send(
        "🔒 <b>PROFIT LOCK — +" + str(round(daily_pnl,1)) + "pts  " + _rs(daily_pnl) + "</b>\n"
        "New entries still open but protected mode on."
    )

def _alert_exit_critical(symbol: str, qty: int, reason: str = ""):
    """v15.2.5 BUG-A: richer CRITICAL alert — names the blocked trade,
    tells the operator exactly which Telegram command clears the lock
    once Kite shows the position is flat. All further exit attempts
    are suppressed until /reset_exit is received."""
    _reason_line = ("Reason : " + str(reason) + "\n") if reason else ""
    _tg_send(
        "🚨 <b>CRITICAL: EXIT FAILED</b>\n"
        "Symbol : " + symbol + "  Qty: " + str(qty) + "\n"
        + _reason_line +
        "Both LIMIT + MARKET exit attempts failed at the broker.\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "1. Open Kite app and close this position manually NOW.\n"
        "2. Once flat on the broker side, send <b>/reset_exit</b> here\n"
        "   to re-enable automatic exits.\n"
        "Until then, all exit attempts are blocked to prevent duplicate\n"
        "orders or incorrect state.",
        priority="critical",   # BUG-U: bypass flood control
    )

def _alert_error(message: str):
    _tg_send("⚠️ <b>ERROR</b>  " + _now_str() + "\n" + message)

# ═══════════════════════════════════════════════════════════════
#  EOD REPORT
# ═══════════════════════════════════════════════════════════════

def _generate_eod_report():
    trades = _read_today_trades()
    today  = date.today().strftime("%d %b %Y")

    if not trades:
        _tg_send("📊 <b>EOD REPORT — " + today + "</b>\nNo trades today.")
        return

    total_pts  = sum(float(t.get("pnl_pts", 0)) for t in trades)
    total_rs   = sum(float(t.get("pnl_rs",  0)) for t in trades)
    wins       = [t for t in trades if float(t.get("pnl_pts", 0)) > 0]
    losses     = [t for t in trades if float(t.get("pnl_pts", 0)) <= 0]
    n_trades   = len(trades)
    win_rate   = round(len(wins) / n_trades * 100, 0) if n_trades > 0 else 0
    best       = max((float(t.get("pnl_pts", 0)) for t in trades), default=0)
    worst      = min((float(t.get("pnl_pts", 0)) for t in trades), default=0)
    convictions = [t for t in trades if t.get("mode") == "CONVICTION"]

    sign = "+" if total_pts >= 0 else ""
    icon = "✅" if total_pts >= 0 else "❌"

    trade_lines = ""
    for i, t in enumerate(trades, 1):
        pts   = float(t.get("pnl_pts", 0))
        sign2 = "+" if pts >= 0 else ""
        trade_lines += (
            str(i) + ". " + t.get("direction", "") + " C"
            + "  " + sign2 + str(round(pts, 1)) + "pts"
            + "  [" + t.get("exit_reason", "")[:14] + "]\n"
        )

    # Calculate charges summary
    _total_charges = sum(float(t.get("total_charges", 0)) for t in trades)
    _total_gross   = sum(float(t.get("gross_pnl_rs", t.get("pnl_rs", 0))) for t in trades)
    _total_net     = round(_total_gross - _total_charges, 2)
    _total_brok    = sum(float(t.get("brokerage", 0)) for t in trades)

    _tg_send(
        icon + " <b>EOD REPORT — " + today + "</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "Gross PNL  : " + sign + str(round(total_pts, 1)) + "pts  "
        + sign + "₹" + "{:,}".format(int(_total_gross)) + "\n"
        "Trades     : " + str(n_trades) + "  "
        + "W=" + str(len(wins)) + " L=" + str(len(losses)) + "\n"
        "Win Rate   : " + str(win_rate) + "%\n"
        "Best       : +" + str(round(best, 1)) + "pts\n"
        "Worst      : " + str(round(worst, 1)) + "pts\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "💰 <b>P&L BREAKDOWN</b>\n"
        "Gross    : " + sign + "₹" + "{:,}".format(int(_total_gross)) + "\n"
        "Charges  : -₹" + "{:,}".format(int(_total_charges)) + "\n"
        "  Brokerage: ₹" + "{:,}".format(int(_total_brok)) + "\n"
        "  STT+Other: ₹" + "{:,}".format(int(_total_charges - _total_brok)) + "\n"
        "Net      : " + ("+" if _total_net >= 0 else "-") + "₹"
        + "{:,}".format(abs(int(_total_net))) + "\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "<b>TRADES</b>\n"
        + trade_lines
        + "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        + ("Mode: PAPER 📄" if D.PAPER_MODE else "Mode: LIVE 💰")
    )

# ═══════════════════════════════════════════════════════════════
#  ENTRY + EXIT EXECUTION
# ═══════════════════════════════════════════════════════════════

def _execute_entry(kite, option_info: dict, option_type: str,
                   entry_result: dict, profile: dict,
                   expiry, dte: int, session: str = "MORNING"):
    token       = option_info["token"]
    symbol      = option_info["symbol"]
    entry_price = entry_result.get("entry_level") or entry_result["entry_price"]

    import VRL_CONFIG as CFG
    lot_count = CFG.get().get("lots", {}).get("count", 2)
    total_qty = D.get_lot_size() * lot_count

    fill = place_entry(kite, symbol, token, option_type,
                       total_qty, entry_price)

    if not fill["ok"]:
        if fill.get("error") == "LIMIT_NOT_FILLED":
            _sym_skip = _short_sym(symbol, option_type, entry_result.get("_strike", 0))
            _tg_send(
                "⏭ <b>ENTRY SKIPPED</b>\n"
                + _sym_skip + " ₹" + str(round(entry_price, 1)) + "\n"
                "Price moved away — LIMIT not filled\n"
                "Protected from bad fill ✓"
            )
            logger.info("[MAIN] Entry skipped: LIMIT not filled for " + symbol)
        else:
            logger.error("[MAIN] Entry failed: " + fill["error"])
            _alert_error("Entry failed: " + fill["error"])
        return

    actual_price = fill["fill_price"]
    actual_qty   = fill["fill_qty"]
    _entry_slippage = fill.get("slippage", 0)
    hard_sl = CFG.get().get("exit", {}).get("hard_sl", 12)
    phase1_sl = compute_entry_sl(actual_price, hard_sl)

    # v14.0: extract the OTHER side token for manage_exit divergence check
    _other_token_entry = 0
    try:
        if option_type == "CE" and _pe_info_v15:
            _other_token_entry = _pe_info_v15.get("token", 0)
        elif option_type == "PE" and _ce_info_v15:
            _other_token_entry = _ce_info_v15.get("token", 0)
    except Exception:
        pass

    with _state_lock:
        state["in_trade"]           = True
        state["symbol"]             = symbol
        state["token"]              = token
        state["direction"]          = option_type
        state["entry_price"]        = actual_price
        state["entry_time"]         = datetime.now().strftime("%H:%M:%S")
        state["strike"]             = entry_result.get("_strike", D.resolve_atm_strike(
            D.get_ltp(D.NIFTY_SPOT_TOKEN), D.get_active_strike_step(dte)))
        state["expiry"]             = expiry.isoformat() if expiry else ""
        state["qty"]                = actual_qty
        state["lot_count"]          = lot_count
        state["lot1_active"]        = True
        state["lot2_active"]        = True
        state["lots_split"]         = False

        # BUG-N12: tell VRL_LAB which strike to keep writing regardless
        # of ATM drift. Resolve both sides so the opposite-side candles
        # are also persisted for hedge research.
        try:
            _trade_strike = state["strike"]
            _trade_dir    = option_type
            _tce = int(_ce_info_v15.get("token", 0)) if _ce_info_v15 else 0
            _tpe = int(_pe_info_v15.get("token", 0)) if _pe_info_v15 else 0
            # If we only have the traded side, fill the other from the
            # same token-resolution batch at this strike.
            if not _tce or not _tpe:
                _both = D.get_option_tokens(kite, int(_trade_strike), expiry) or {}
                if not _tce:
                    _tce = int((_both.get("CE") or {}).get("token", 0))
                if not _tpe:
                    _tpe = int((_both.get("PE") or {}).get("token", 0))
            D.set_active_trade(_trade_strike, _trade_dir, _tce, _tpe)
        except Exception as _ate:
            logger.debug("[MAIN] set_active_trade: " + str(_ate))
        # Exit state
        state["exit_phase"]         = 1
        state["phase1_sl"]          = phase1_sl
        state["_static_floor_sl"]   = 0
        state["current_floor"]      = phase1_sl
        state["peak_pnl"]           = 0.0
        state["trough_pnl"]         = 0.0
        state["candles_held"]       = 0
        state["_candle_low"]        = actual_price
        # v15.2.5 BUG-J: fresh trade starts with an empty peak_history so
        # the normal per-candle append path populates it cleanly. Reset
        # the one-shot backfill sentinel so the NEXT restart-with-trade
        # (if this position is still open) gets its own seed.
        state["peak_history"]        = []
        state["last_peak_candle_ts"] = ""
        state["current_velocity"]    = 0.0
        state["_peak_history_backfilled"] = False
        state["_last_milestone"]    = 0
        state["current_rsi"]        = 0
        # v15.0 entry context — band values at entry
        state["entry_mode"]         = entry_result.get("entry_mode", "EMA9_BREAKOUT")
        state["entry_ema9_high"]    = round(float(entry_result.get("ema9_high", 0)), 2)
        state["entry_ema9_low"]     = round(float(entry_result.get("ema9_low", 0)), 2)
        state["entry_band_position"] = entry_result.get("band_position", "ABOVE")
        state["entry_body_pct"]     = round(float(entry_result.get("body_pct", 0)), 1)
        state["current_ema9_high"]  = round(float(entry_result.get("ema9_high", 0)), 2)
        state["current_ema9_low"]   = round(float(entry_result.get("ema9_low", 0)), 2)
        state["last_band_check_ts"] = ""
        state["other_token"]        = _other_token_entry
        # v15.2: capture straddle + VWAP context for exit alert + dashboard
        _sd_at_entry = entry_result.get("straddle_delta")
        state["entry_straddle_delta"]     = float(_sd_at_entry) if _sd_at_entry is not None else 0.0
        state["entry_straddle_threshold"] = float(entry_result.get("straddle_threshold", 0) or 0)
        state["entry_straddle_period"]    = entry_result.get("straddle_period", "")
        state["entry_atm_strike"]         = int(entry_result.get("atm_strike_used", 0) or 0)
        state["entry_band_width"]         = float(entry_result.get("band_width", 0) or 0)
        state["entry_spot_vwap"]          = float(entry_result.get("spot_vwap", 0) or 0)
        state["entry_spot_vs_vwap"]       = float(entry_result.get("spot_vs_vwap", 0) or 0)
        state["entry_vwap_bonus"]         = entry_result.get("vwap_bonus", "")
        # v15.2.5 Fix 5: STRONG / NEUTRAL / WEAK / NA straddle classification
        state["entry_straddle_info"]      = entry_result.get("straddle_info", "")
        # Counters
        state["daily_trades"]      += 1

    _save_state()

    # v13.5: Place ONE exchange backup SL-M for full qty at entry - candle_close_sl
    try:
        from VRL_TRADE import place_sl_order
        _cfg_lots    = CFG.get().get("lots", {})
        _lot_size    = _cfg_lots.get("size", D.get_lot_size())
        _lot_count   = _cfg_lots.get("count", 2)
        _candle_sl   = CFG.get().get("exit", {}).get("candle_close_sl", 12)
        _sl_price    = round(actual_price - _candle_sl, 2)
        _full_qty    = _lot_size * _lot_count
        _sl_oid = place_sl_order(kite, symbol, _full_qty, _sl_price)
        with _state_lock:
            state["_sl_order_id"] = _sl_oid
            state["_sl_trigger_at_exchange"] = round(_sl_price, 1)
            state["_sl_order_id_lot1"] = ""
            state["_sl_order_id_lot2"] = ""
    except Exception as _se:
        logger.warning("[MAIN] SL-M place error: " + str(_se))

    # ── v15.2 Entry alert ──
    _slip_line = ""
    if _entry_slippage > 0:
        _slip_line = "Slip: +" + str(_entry_slippage) + "pts\n"

    _emode = entry_result.get("entry_mode", "EMA9_BREAKOUT")
    _close = round(float(entry_result.get("close", actual_price)), 1)
    _ema9h = round(float(entry_result.get("ema9_high", 0)), 1)
    _ema9l = round(float(entry_result.get("ema9_low", 0)), 1)
    _body  = int(round(float(entry_result.get("body_pct", 0)), 0))
    _dist_to_stop = round(_close - _ema9l, 1) if _ema9l > 0 else 0

    # v15.2.5 Fix 5: straddle is now DISPLAY ONLY. Line shows the
    # STRONG / NEUTRAL / WEAK / NA classification + period label, no threshold.
    _sd    = entry_result.get("straddle_delta")
    _sinfo = entry_result.get("straddle_info", "") or ""
    _spd   = entry_result.get("straddle_period", "-") or "-"
    _savail = entry_result.get("straddle_available", True)
    if not _savail or _sd is None:
        _straddle_line = "Straddle: DATA UNAVAILABLE [NA]\n"
    else:
        _straddle_line = ("Straddle: \u0394" + "{:+.1f}".format(float(_sd))
                          + " [" + _sinfo + "] (" + _spd + ")\n")

    # VWAP bonus line (display only, matches spec)
    _vwap_line = ""
    try:
        _vw   = entry_result.get("spot_vwap")
        _diff = entry_result.get("spot_vs_vwap")
        _vbon = entry_result.get("vwap_bonus", "")
        if _vw and _vw > 0:
            _spot_disp = round(float(_vw) + float(_diff or 0), 1)
            _vwap_line = ("VWAP: spot " + "{:.1f}".format(_spot_disp)
                          + " vs vwap " + "{:.1f}".format(float(_vw))
                          + " (" + "{:+.0f}".format(float(_diff or 0))
                          + ") [" + str(_vbon) + "]\n")
    except Exception:
        _vwap_line = ""

    _detail = ("Close " + str(_close) + " &gt; EMA9-high " + str(_ema9h) + "\n"
               + "Body " + str(_body) + "% green\n"
               + _straddle_line
               + _vwap_line
               + "Stop: EMA9-low " + str(_ema9l)
               + " (" + "{:.1f}".format(_dist_to_stop) + "pts away)\n"
               + "BE+2 lock: activates after peak +10\n")
    _tg_send(
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "<b>" + _short_sym(symbol, option_type, state.get("strike", 0))
        + " x " + str(lot_count) + " LOTS [" + _emode + "]</b>\n"
        + datetime.now().strftime("%H:%M") + "  \u20B9" + str(round(actual_price, 1)) + "\n"
        + _detail + _slip_line +
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    )

    logger.info(
        "[MAIN] ENTRY " + option_type + " " + symbol
        + " price=" + str(actual_price)
        + " ema_gap=" + str(entry_result.get("ema_gap", 0))
        + " rsi=" + str(entry_result.get("rsi", 0))
        + " SL=" + str(phase1_sl)
    )

    # First live trade ever — one-time alert
    if not D.PAPER_MODE:
        _first_flag = os.path.expanduser("~/state/.first_live_done")
        try:
            if os.path.isfile(_first_flag):
                with open(_first_flag) as _ff:
                    _first_ts = _ff.read().strip()
                if _first_ts and _first_ts.startswith(date.today().isoformat()):
                    _tg_send(
                        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                        "🚀 <b>FIRST LIVE TRADE EVER</b>\n"
                        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                        "Real money is moving now.\n"
                        "The journey begins.\n"
                        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
                    )
        except Exception:
            pass
    # ── Live validation: 10 entry checks (silent on PASS, alerts on FAIL) ──
    try:
        from VRL_VALIDATE import validate_entry
        with _state_lock:
            _vstate = dict(state)
        _failures = validate_entry(_vstate, entry_result, kite)
        if _failures:
            _fail_msg = "⚠️ <b>ENTRY VALIDATION</b>\n"
            for _f in _failures:
                _fail_msg += "❌ " + _f + "\n"
                logger.warning("[VALIDATE] " + _f)
            _tg_send(_fail_msg)
        else:
            logger.info("[VALIDATE] Entry: 10/10 checks passed ✅")
    except Exception as _ve:
        logger.warning("[VALIDATE] Entry validation error: " + str(_ve))


def _execute_exit_v13(kite, exit_info: dict, saved_entry_price: float = None):
    """v13.0: Execute a single exit (partial or full).
    saved_entry_price: pre-captured entry price to avoid stale state after partial exit resets.
    """
    if state.get("_exit_failed"):
        logger.warning("[MAIN] Exit suppressed — previous CRITICAL failure unresolved")
        return

    lot_id = exit_info.get("lot_id", "ALL")
    reason = exit_info.get("reason", "UNKNOWN")
    exit_price = exit_info.get("price", 0)

    with _state_lock:
        symbol    = state["symbol"]
        token     = state["token"]
        direction = state["direction"]
        entry     = saved_entry_price if saved_entry_price is not None else state["entry_price"]
        peak      = state.get("peak_pnl", 0)
        candles   = state.get("candles_held", 0)
        _exit_strike = state.get("strike", 0)
        # v15.0: entry confirmation = band position at entry
        _entry_eh = round(float(state.get("entry_ema9_high", 0)), 1)
        _entry_el = round(float(state.get("entry_ema9_low", 0)), 1)
        _entry_body = int(round(float(state.get("entry_body_pct", 0)), 0))
        _entry_mode_e = state.get("entry_mode", "EMA9_BREAKOUT")
        _entry_conf = (_entry_mode_e + " | entry close &gt; EMA9h "
                       + str(_entry_eh) + " | body " + str(_entry_body) + "%")

    # Determine qty — for ALL exit use full entry qty
    if lot_id == "ALL":
        exit_qty = state.get("qty", D.get_lot_size() * 2)
    else:
        exit_qty = D.get_lot_size()

    # v13.5: Cancel the single exchange SL-M order
    try:
        from VRL_TRADE import cancel_sl_order
        _sl_oid = state.get("_sl_order_id", "")
        if _sl_oid:
            cancel_sl_order(kite, _sl_oid)
    except Exception:
        pass

    fill = place_exit(kite, symbol, token, direction,
                      exit_qty, exit_price, reason)

    if not fill["ok"] and fill.get("error") == "EXIT_FAILED_MANUAL_REQUIRED":
        with _state_lock:
            state["_exit_failed"] = True
        _save_state()   # v15.2.5 BUG-A: persist the block across crashes
        _alert_exit_critical(symbol, exit_qty, reason=reason)
        return

    actual_exit = fill["fill_price"] if fill["ok"] else exit_price
    pnl = round(actual_exit - entry, 2)

    # Update lot state + track per-lot exit data
    with _state_lock:
        if lot_id == "ALL":
            state["lot1_active"] = False
            state["lot2_active"] = False
            state["lot1_exit_price"] = round(actual_exit, 2)
            state["lot1_exit_pnl"] = round(pnl, 2)
            state["lot1_exit_reason"] = reason
            state["lot1_exit_time"] = datetime.now().strftime("%H:%M")
            state["lot2_exit_price"] = round(actual_exit, 2)
            state["lot2_exit_pnl"] = round(pnl, 2)
            state["lot2_exit_reason"] = reason
        elif lot_id == "LOT1":
            state["lot1_active"] = False
            state["lot1_exit_price"] = round(actual_exit, 2)
            state["lot1_exit_pnl"] = round(pnl, 2)
            state["lot1_exit_reason"] = reason
            state["lot1_exit_time"] = datetime.now().strftime("%H:%M")
        elif lot_id == "LOT2":
            state["lot2_active"] = False
            state["lot2_exit_price"] = round(actual_exit, 2)
            state["lot2_exit_pnl"] = round(pnl, 2)
            state["lot2_exit_reason"] = reason

    # Check if trade is fully closed
    trade_done = not state.get("lot1_active") and not state.get("lot2_active")

    # BUG-031: daily_pnl tracks POINTS per trade (matches dashboard/CSV).
    # Previous bug: used pnl * (qty/lot_size) which doubled the value for 2-lot exits.
    pnl_lots = pnl  # points per trade — one value per closed trade, matches trade log

    # Log EVERY lot exit (not just trade_done)
    _log_trade(state, actual_exit, reason, candles, saved_entry=entry,
               lot_id=lot_id, qty=exit_qty)

    # ── v15.2.5 Batch 3 BUG-R3: shadow trade summary ──
    try:
        _log_shadow_trade_summary(state, entry, actual_exit, reason,
                                  peak, candles)
    except Exception as _sts:
        logger.debug("[SHADOW_EXIT] trade summary: " + str(_sts))

    # Telegram alert
    if trade_done:
        with _state_lock:
            state["daily_pnl"] = round(state.get("daily_pnl", 0) + pnl_lots, 2)
            if pnl < 0:
                state["daily_losses"]       = state.get("daily_losses", 0) + 1
                state["consecutive_losses"] = state.get("consecutive_losses", 0) + 1
            else:
                state["consecutive_losses"] = 0
            state["last_exit_time"] = datetime.now().isoformat()
            state["last_exit_direction"] = direction
            state["last_exit_peak"] = peak
            state["last_exit_reason"] = reason
            state["last_exit_price"] = round(actual_exit, 2)
            old_token = state["token"]
            # BUG-N12: clear the LAB pinned strike so it returns to
            # ATM-following mode.
            try:
                D.clear_active_trade()
            except Exception:
                pass
            state.update({
                "in_trade": False, "symbol": "", "token": None,
                "direction": "", "entry_price": 0.0, "entry_time": "",
                "exit_phase": 1, "phase1_sl": 0.0, "phase2_sl": 0.0,
                "_static_floor_sl": 0.0, "current_floor": 0.0,
                "peak_pnl": 0.0, "trough_pnl": 0.0,
                "candles_held": 0, "force_exit": False, "_exit_failed": False,
                "lot1_active": True, "lot2_active": True, "lots_split": False,
                "lot1_exit_price": 0.0, "lot1_exit_pnl": 0.0,
                "lot1_exit_reason": "", "lot1_exit_time": "",
                "lot2_exit_price": 0.0, "lot2_exit_pnl": 0.0,
                "lot2_exit_reason": "",
                "_sl_order_id": "", "_sl_trigger_at_exchange": 0,
                "entry_mode": "",
            })
        if old_token:
            D.unsubscribe_tokens([old_token])
        _reset_strike_lock()
        _day_pnl    = state.get("daily_pnl", 0)
        _day_trades = state.get("daily_trades", 0)
        _day_losses = state.get("daily_losses", 0)
        _day_wins   = _day_trades - _day_losses
        _sym_short  = _short_sym(symbol, direction, _exit_strike)
        _pnl_sign   = "+" if pnl >= 0 else ""
        _day_rs     = int(_day_pnl * D.get_lot_size())
        import VRL_CONFIG as _CFG_exit
        _cd_cfg     = _CFG_exit.get().get("cooldown", {})
        # Calculate charges for Telegram
        _num_eo = 2 if state.get("lots_split") else 1
        try:
            _ch = CHARGES.calculate_charges(entry, actual_exit,
                      exit_qty, _num_eo)
        except Exception:
            _ch = {"gross_pnl": pnl * (exit_qty / D.get_lot_size()) * D.get_lot_size(),
                   "total_charges": 0, "net_pnl": pnl * (exit_qty / D.get_lot_size()) * D.get_lot_size(),
                   "charges_pts": 0}
        # v15.2 exit alert — 5 data lines, spec-aligned (no confirm-at-entry line)
        _gross_sign = "+" if _ch["gross_pnl"] >= 0 else "-"
        _net_sign   = "+" if _ch["net_pnl"]   >= 0 else "-"
        # v15.2.5: if exit was VELOCITY_STALL, prepend the peak-history context
        # that explains WHY we bailed (momentum died before price reversed).
        _extra_line = ""
        if reason == "VELOCITY_STALL":
            _ph = state.get("peak_history") or []
            _vel = state.get("current_velocity", 0)
            _extra_line = ("Last 4 peaks: " + str(_ph[-4:] if _ph else [])
                           + " | velocity=" + "{:+.2f}".format(float(_vel))
                           + "\nVelocity died — exited before reversal\n")
        _tg_send(
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "<b>" + _sym_short + "</b>  " + "{:+.1f}".format(pnl) + "pts\n"
            + reason + " | Peak +" + "{:.1f}".format(peak) + " | " + str(candles) + "min\n"
            + _extra_line +
            "Entry " + str(round(entry, 1)) + " -> Exit " + str(round(actual_exit, 1)) + "\n"
            "Gross: " + _gross_sign + "\u20B9" + "{:,}".format(abs(int(_ch["gross_pnl"])))
            + " | Charges: -\u20B9" + "{:,}".format(int(_ch["total_charges"]))
            + " | Net: " + _net_sign + "\u20B9" + "{:,}".format(abs(int(_ch["net_pnl"]))) + "\n"
            "DAY: " + "{:+.1f}".format(_day_pnl) + "pts | "
            + str(_day_wins) + "W " + str(_day_losses) + "L\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
        )
    else:
        # Partial exit — update daily PNL for the exited lot
        with _state_lock:
            state["daily_pnl"] = round(state.get("daily_pnl", 0) + pnl, 2)
        remaining = "LOT2" if state.get("lot2_active") else "LOT1"
        _sym_short_p = _short_sym(symbol, direction, _exit_strike)
        try:
            _ch_p = CHARGES.calculate_lot_charges(entry, actual_exit, D.get_lot_size())
        except Exception:
            _ch_p = {"net_pnl": pnl * D.get_lot_size(), "total_charges": 0}
        _tg_send(
            "💰 <b>" + lot_id + " " + _sym_short_p + "</b> "
            + ("+" if pnl >= 0 else "") + str(round(pnl, 1)) + "pts\n"
            "₹" + str(round(entry, 1)) + " → ₹" + str(round(actual_exit, 1)) + " | " + reason + "\n"
            "Net ₹" + "{:,}".format(abs(int(_ch_p["net_pnl"])))
            + " (charges ₹" + str(int(_ch_p["total_charges"])) + ")\n"
            + remaining + " riding..."
        )

    _save_state()
    # Refresh margin after trade (skip in paper mode)
    if not D.PAPER_MODE:
        try:
            D.refresh_margin(kite)
        except Exception:
            pass
    # Force dashboard refresh after exit so it shows "NOT IN TRADE" immediately
    if trade_done:
        try:
            _da = _last_dash_args
            if _da:
                _write_dashboard(
                    _da.get("spot_ltp", D.get_ltp(D.NIFTY_SPOT_TOKEN)),
                    _da.get("atm_strike", 0), _da.get("dte", 0),
                    _da.get("vix_ltp", D.get_vix()),
                    _da.get("session", ""), _da.get("profile", {}),
                    {}, _da.get("expiry"), datetime.now())
            else:
                _write_dashboard(D.get_ltp(D.NIFTY_SPOT_TOKEN), 0, 0,
                                 D.get_vix(), "", {}, {}, None, datetime.now())
        except Exception:
            pass
    logger.info("[MAIN] EXIT " + lot_id + " " + symbol
                + " price=" + str(actual_exit) + " pnl=" + str(pnl)
                + "pts reason=" + reason)

    # ── Live validation: 10 exit checks (only on full close) ──
    if trade_done:
        try:
            from VRL_VALIDATE import validate_exit
            with _state_lock:
                _vstate = dict(state)
            _failures = validate_exit(
                _vstate, pnl, actual_exit, reason,
                entry, exit_qty, kite)
            if _failures:
                _fail_msg = "⚠️ <b>EXIT VALIDATION</b>\n"
                for _f in _failures:
                    _fail_msg += "❌ " + _f + "\n"
                    logger.warning("[VALIDATE] " + _f)
                _tg_send(_fail_msg)
            else:
                logger.info("[VALIDATE] Exit: 10/10 checks passed ✅")
        except Exception as _ve:
            logger.warning("[VALIDATE] Exit validation error: " + str(_ve))


def _execute_exit(kite, option_ltp: float, reason: str,
                  saved_entry_price: float = None):
    """Legacy wrapper — exits all lots.

    BUG-D fix: now forwards `saved_entry_price` so callers (FORCE_EXIT,
    other legacy paths) can capture the entry price BEFORE any state
    mutation and guarantee correct PNL even if state["entry_price"]
    has been touched between capture and exit. Without this, the
    FORCE_EXIT path at line ~2100 captured _entry_px locally but the
    thin wrapper dropped it, forcing _execute_exit_v13 to re-read
    state — defeating the whole point of the capture.
    """
    _execute_exit_v13(kite, {"lots": "ALL", "lot_id": "ALL",
                             "reason": reason, "price": option_ltp},
                      saved_entry_price=saved_entry_price)

# ═══════════════════════════════════════════════════════════════
#  CANDLE BOUNDARY
# ═══════════════════════════════════════════════════════════════

def _is_new_1min_candle(now: datetime) -> bool:
    # BUG-R v15.2.5 Batch 6: bumped from 30 → 35 seconds. Kite's
    # historical_data endpoint occasionally reports the closed 1-min
    # candle without the final trade(s) until ~32–34 seconds past the
    # boundary, which caused rare stale-close reads. 35s gives a
    # 5-second broker-side safety margin. Window still stays open for
    # the remaining 24 seconds so a brief loop hiccup can't skip a
    # minute.
    key = now.strftime("%Y%m%d%H%M")
    with _state_lock:
        if state.get("_last_1min_candle") != key and now.second >= 35:
            state["_last_1min_candle"] = key
            return True
    return False


def _compute_bonus(token: int) -> dict:
    """Compute all bonus indicators for a token. Info only — never blocks trades."""
    bonus = {}
    try:
        vwap = D.calculate_option_vwap(token)
        bonus["vwap"] = vwap.get("vwap", 0)
        bonus["above_vwap"] = vwap.get("above_vwap", False)
        bonus["vwap_dist"] = vwap.get("distance", 0)
    except Exception:
        bonus["vwap"] = 0; bonus["above_vwap"] = False; bonus["vwap_dist"] = 0
    try:
        fib = D.calculate_option_fib_pivots(token)
        bonus["fib_nearest"] = fib.get("nearest_level", "")
        bonus["fib_distance"] = fib.get("nearest_distance", 0)
        bonus["fib_pivot"] = fib.get("pivot", 0)
        bonus["fib_R1"] = fib.get("R1", 0)
        bonus["fib_R2"] = fib.get("R2", 0)
        bonus["fib_R3"] = fib.get("R3", 0)
        bonus["fib_S1"] = fib.get("S1", 0)
        bonus["fib_S2"] = fib.get("S2", 0)
        bonus["fib_S3"] = fib.get("S3", 0)
    except Exception:
        bonus["fib_nearest"] = ""; bonus["fib_distance"] = 0
        bonus["fib_pivot"] = 0
    try:
        vol = D.detect_volume_spike(token)
        bonus["vol_spike"] = vol.get("spike", False)
        bonus["vol_ratio"] = vol.get("ratio", 0)
    except Exception:
        bonus["vol_spike"] = False; bonus["vol_ratio"] = 0
    try:
        pdh = D.get_option_prev_day_hl(token)
        bonus["pdh_break"] = pdh.get("above_prev_high", False)
        bonus["pdl_break"] = pdh.get("below_prev_low", False)
        bonus["prev_high"] = pdh.get("prev_high", 0)
        bonus["prev_low"] = pdh.get("prev_low", 0)
    except Exception:
        bonus["pdh_break"] = False; bonus["pdl_break"] = False
        bonus["prev_high"] = 0; bonus["prev_low"] = 0
    return bonus


# ═══════════════════════════════════════════════════════════════
#  STRATEGY LOOP
# ═══════════════════════════════════════════════════════════════


# ═══════════════════════════════════════════════════════════════
#  DASHBOARD SNAPSHOT — written every cycle for VRL_WEB.py
#  VRL_WEB.py reads this file. Zero calculation in web server.
# ═══════════════════════════════════════════════════════════════

# ═══════════════════════════════════════════════════════════════
#  v15.2.5 Batch 3 BUG-R2 — Shadow exit tick CSV logger
#  Pure logging — NEVER mutates state, NEVER calls exit functions,
#  NEVER touches production SL fields.
# ═══════════════════════════════════════════════════════════════

_SHADOW_EXIT_DIR = os.path.join(os.path.expanduser("~"), "lab_data", "shadow_exits")
os.makedirs(_SHADOW_EXIT_DIR, exist_ok=True)

_SHADOW_EXIT_FIELDS = [
    "timestamp", "symbol", "direction", "entry_price", "current_ltp",
    "running_pnl", "peak_pnl", "candles_held",
    "ema9_low_3m", "ratchet_sl", "ratchet_tier",
    "ema1m_break", "ema1m_close", "ema1m_ema9",
    "would_exit_ratchet", "would_exit_ema1m", "would_exit_ema9_low",
    "actual_sl", "actual_exit_reason_pending",
]


def _log_shadow_exit_tick(st: dict, option_ltp: float, now: datetime):
    """BUG-R2: one row per strategy-loop tick while in_trade. Read-only on st."""
    if not st.get("in_trade") or option_ltp <= 0:
        return
    entry   = float(st.get("entry_price", 0) or 0)
    pnl     = round(option_ltp - entry, 2)
    peak    = float(st.get("peak_pnl", 0) or 0)
    candles = int(st.get("candles_held", 0) or 0)
    token   = st.get("token")
    direction = st.get("direction", "")
    ema9l_3m = float(st.get("current_ema9_low", 0) or 0)

    # Shadow ratchet (pure function — no state mutation)
    from VRL_ENGINE import compute_ratchet_sl, compute_1min_ema9_break
    ratchet_sl, ratchet_tier = compute_ratchet_sl(entry, peak, direction)

    # 1-min EMA9 break (pure function — fetches historical_data)
    ema1m_break, ema1m_close, ema1m_ema9 = (False, 0.0, 0.0)
    if token:
        ema1m_break, ema1m_close, ema1m_ema9 = compute_1min_ema9_break(
            int(token), pnl)

    # Would-exit flags (hypothetical — never executed)
    would_ratchet = 1 if (ratchet_sl > 0 and option_ltp <= ratchet_sl) else 0
    would_ema1m   = 1 if ema1m_break else 0
    would_ema9low = 1 if (ema9l_3m > 0 and option_ltp <= ema9l_3m) else 0

    # Actual SL the bot is using right now (ratchet takes priority)
    actual_sl = float(st.get("active_ratchet_sl", 0) or 0)
    if actual_sl <= 0:
        actual_sl = float(st.get("current_ema9_low", 0) or 0)

    row = {
        "timestamp":              now.strftime("%Y-%m-%d %H:%M:%S"),
        "symbol":                 st.get("symbol", ""),
        "direction":              direction,
        "entry_price":            round(entry, 2),
        "current_ltp":            round(option_ltp, 2),
        "running_pnl":            pnl,
        "peak_pnl":               round(peak, 2),
        "candles_held":           candles,
        "ema9_low_3m":            round(ema9l_3m, 2),
        "ratchet_sl":             ratchet_sl,
        "ratchet_tier":           ratchet_tier,
        "ema1m_break":            1 if ema1m_break else 0,
        "ema1m_close":            ema1m_close,
        "ema1m_ema9":             ema1m_ema9,
        "would_exit_ratchet":     would_ratchet,
        "would_exit_ema1m":       would_ema1m,
        "would_exit_ema9_low":    would_ema9low,
        "actual_sl":              round(actual_sl, 2),
        "actual_exit_reason_pending": "",
    }
    _csv_path = os.path.join(_SHADOW_EXIT_DIR,
                             "shadow_" + now.strftime("%Y-%m-%d") + ".csv")
    is_new = not os.path.isfile(_csv_path)
    try:
        with open(_csv_path, "a", newline="") as f:
            w = csv.DictWriter(f, fieldnames=_SHADOW_EXIT_FIELDS,
                               extrasaction="ignore")
            if is_new:
                w.writeheader()
            w.writerow(row)
    except Exception as _we:
        logger.debug("[SHADOW_EXIT] CSV write: " + str(_we))


def _send_shadow_exit_eod():
    """BUG-R4: one Telegram at 15:35 summarizing shadow vs actual exits."""
    today_str = date.today().strftime("%Y-%m-%d")
    trades_path = os.path.join(_SHADOW_EXIT_DIR,
                               "shadow_trades_" + today_str + ".csv")
    if not os.path.isfile(trades_path):
        _tg_send("[SHADOW EXIT] No shadow data today.")
        return
    rows = []
    try:
        with open(trades_path) as f:
            rows = list(csv.DictReader(f))
    except Exception:
        _tg_send("[SHADOW EXIT] CSV read error.")
        return
    if not rows:
        _tg_send("[SHADOW EXIT] No shadow data today.")
        return
    n = len(rows)
    lot = D.get_lot_size()
    actual_sum = sum(float(r.get("actual_pnl", 0) or 0) for r in rows)
    ratchet_sum = 0.0
    ema1m_sum   = 0.0
    r_count = 0
    e_count = 0
    for r in rows:
        rp = r.get("first_ratchet_pnl_if_exited_here")
        ep = r.get("first_ema1m_break_pnl_if_exited_here")
        ap = float(r.get("actual_pnl", 0) or 0)
        if rp not in (None, ""):
            ratchet_sum += float(rp); r_count += 1
        else:
            ratchet_sum += ap
        if ep not in (None, ""):
            ema1m_sum += float(ep); e_count += 1
        else:
            ema1m_sum += ap
    best = max(actual_sum, ratchet_sum, ema1m_sum)
    saved = round(best - actual_sum, 1)
    _tg_send(
        "📊 <b>SHADOW EXIT REPORT — " + today_str + "</b>\n"
        "Trades: " + str(n) + "\n"
        "Actual:  " + "{:+.1f}".format(actual_sum) + "pts  (₹"
        + "{:+,.0f}".format(actual_sum * lot * 2) + ")\n"
        "Ratchet: " + "{:+.1f}".format(ratchet_sum) + "pts  (₹"
        + "{:+,.0f}".format(ratchet_sum * lot * 2) + ")  fired " + str(r_count) + "x\n"
        "EMA1m:   " + "{:+.1f}".format(ema1m_sum) + "pts  (₹"
        + "{:+,.0f}".format(ema1m_sum * lot * 2) + ")  fired " + str(e_count) + "x\n"
        "Best:    " + "{:+.1f}".format(best) + "pts\n"
        "Saved:   " + "{:+.1f}".format(saved) + "pts vs actual"
    )


_SHADOW_TRADE_FIELDS = [
    "date", "entry_time", "exit_time", "symbol", "direction",
    "entry_price", "actual_exit_price", "actual_exit_reason", "actual_pnl",
    "peak_pnl",
    "first_ratchet_hit_time", "first_ratchet_tier", "first_ratchet_price",
    "first_ratchet_pnl_if_exited_here",
    "first_ema1m_break_time", "first_ema1m_break_price",
    "first_ema1m_break_pnl_if_exited_here",
    "rule_winner", "rule_winner_pnl", "rule_winner_saved_pts",
]


def _log_shadow_trade_summary(st, entry, exit_price, reason, peak, candles):
    """BUG-R3: after a real exit, read today's shadow tick CSV and compute
    what ratchet + ema1m would have done. Pure read + write — no state mutation."""
    today_str = date.today().strftime("%Y-%m-%d")
    tick_path = os.path.join(_SHADOW_EXIT_DIR, "shadow_" + today_str + ".csv")
    if not os.path.isfile(tick_path):
        return
    actual_pnl = round(float(exit_price) - float(entry), 2)
    entry_time = st.get("entry_time", "")
    exit_time  = datetime.now().strftime("%H:%M:%S")
    symbol     = st.get("symbol", "")
    direction  = st.get("direction", "")

    # Scan tick CSV for first ratchet + ema1m triggers
    first_ratchet = None
    first_ema1m   = None
    try:
        with open(tick_path, "r") as f:
            for row in csv.DictReader(f):
                if row.get("symbol") != symbol:
                    continue
                if not first_ratchet and row.get("would_exit_ratchet") == "1":
                    first_ratchet = row
                if not first_ema1m and row.get("would_exit_ema1m") == "1":
                    first_ema1m = row
                if first_ratchet and first_ema1m:
                    break
    except Exception:
        pass

    def _pnl_at(row_or_none):
        if not row_or_none:
            return None, "", 0.0
        ltp = float(row_or_none.get("current_ltp", 0) or 0)
        ts  = row_or_none.get("timestamp", "")
        p   = round(ltp - float(entry), 2)
        return p, ts, ltp

    r_pnl, r_ts, r_price = _pnl_at(first_ratchet)
    e_pnl, e_ts, e_price = _pnl_at(first_ema1m)
    r_tier = first_ratchet.get("ratchet_tier", "") if first_ratchet else ""

    # Determine rule winner (earliest trigger)
    candidates = []
    if r_pnl is not None:
        candidates.append(("ratchet", r_pnl, r_ts))
    if e_pnl is not None:
        candidates.append(("ema1m", e_pnl, e_ts))
    if not candidates:
        winner, winner_pnl = "actual", actual_pnl
    else:
        candidates.sort(key=lambda x: x[2])
        winner, winner_pnl = candidates[0][0], candidates[0][1]
    saved = round(winner_pnl - actual_pnl, 2)

    out = {
        "date": today_str, "entry_time": entry_time, "exit_time": exit_time,
        "symbol": symbol, "direction": direction,
        "entry_price": round(float(entry), 2),
        "actual_exit_price": round(float(exit_price), 2),
        "actual_exit_reason": reason, "actual_pnl": actual_pnl,
        "peak_pnl": round(float(peak), 2),
        "first_ratchet_hit_time": r_ts, "first_ratchet_tier": r_tier,
        "first_ratchet_price": round(r_price, 2) if r_price else "",
        "first_ratchet_pnl_if_exited_here": r_pnl if r_pnl is not None else "",
        "first_ema1m_break_time": e_ts,
        "first_ema1m_break_price": round(e_price, 2) if e_price else "",
        "first_ema1m_break_pnl_if_exited_here": e_pnl if e_pnl is not None else "",
        "rule_winner": winner, "rule_winner_pnl": winner_pnl,
        "rule_winner_saved_pts": saved,
    }
    trades_path = os.path.join(_SHADOW_EXIT_DIR,
                               "shadow_trades_" + today_str + ".csv")
    is_new = not os.path.isfile(trades_path)
    try:
        with open(trades_path, "a", newline="") as f:
            w = csv.DictWriter(f, fieldnames=_SHADOW_TRADE_FIELDS,
                               extrasaction="ignore")
            if is_new:
                w.writeheader()
            w.writerow(out)
        logger.info("[SHADOW_EXIT] trade summary: " + winner + " saved "
                    + str(saved) + "pts vs actual " + str(actual_pnl))
    except Exception as _we:
        logger.debug("[SHADOW_EXIT] trade CSV: " + str(_we))


def _update_dashboard_ltp():
    """Quick update — just LTP values in dashboard JSON. No API calls."""
    try:
        dash_path = os.path.join(D.STATE_DIR, 'vrl_dashboard.json')
        if not os.path.isfile(dash_path):
            return
        with open(dash_path) as f:
            dash = json.load(f)

        # Update spot + VIX
        spot = D.get_ltp(D.NIFTY_SPOT_TOKEN)
        if spot > 0:
            dash.setdefault("market", {})["spot"] = round(spot, 2)
        vix = D.get_vix()
        if vix > 0:
            dash.setdefault("market", {})["vix"] = round(vix, 1)

        # Update option LTPs
        for side in ("CE", "PE"):
            sig = dash.get(side.lower(), {})
            oi = _locked_tokens.get(side) if _locked_tokens else None
            if oi:
                ltp = D.get_ltp(oi["token"])
                if ltp > 0:
                    sig["ltp"] = round(ltp, 2)

        # Update position if in trade
        with _state_lock:
            _tk = state.get("token")
            _ep = state.get("entry_price", 0)
            _it = state.get("in_trade", False)
        if _it and _tk:
            opt_ltp = D.get_ltp(_tk)
            if opt_ltp > 0:
                pos = dash.get("position", {})
                pos["ltp"] = round(opt_ltp, 2)
                pos["pnl"] = round(opt_ltp - _ep, 1)
                # Update lot PNLs
                running = round(opt_ltp - _ep, 1)
                l1 = pos.get("lot1", {})
                l2 = pos.get("lot2", {})
                if l1.get("status") == "active":
                    l1["pnl"] = running
                if l2.get("status") in ("active", "riding"):
                    l2["pnl"] = running

        dash["ts"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        tmp = dash_path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(dash, f, default=str)
        os.replace(tmp, dash_path)
    except Exception:
        pass


# ═══════════════════════════════════════════════════════════════
#  WARMUP HELPER (v14.0: data-driven, pre-warmed from previous day)
#  Queries actual 3-min spot history. Kite API returns multi-day
#  candles so at 9:16 we already have yesterday's full session.
#  If len(3m spot df) >= 14, we're warm → entries unblocked at 9:31.
# ═══════════════════════════════════════════════════════════════

def _warmup_info(now, dte):
    """Returns (is_warm, candles_done, candles_needed, eta_hhmm).
    v14.0: reads actual historical candle count, not wall-clock time."""
    needed = 14
    done = 0
    try:
        df = D.get_historical_data(D.NIFTY_SPOT_TOKEN, "3minute", 30)
        if df is not None and not df.empty:
            done = min(needed, len(df))
    except Exception:
        pass
    is_warm = done >= needed
    # ETA: if not warm, estimate when natural 3-min accumulation will finish
    if is_warm:
        eta = "ready"
    else:
        remaining_candles = needed - done
        remaining_min = remaining_candles * 3
        target = now + timedelta(minutes=remaining_min)
        eta = target.strftime("%H:%M")
    return is_warm, int(done), needed, eta


def _warmup_signal(opt_type, strike, progress, needed, eta):
    """Build a signal block with WARMUP status for the dashboard."""
    return {
        "status": "WARMUP",
        "warmup_progress": progress,
        "warmup_needed": needed,
        "warmup_eta": eta,
        "message": "Indicators warming up — trades blocked until stable",
        "strike": strike,
        "ltp": 0,
        "ema9": 0, "ema21": 0, "ema_gap": 0, "rsi": 0, "rsi_prev": 0,
        "ema_ok": False, "rsi_ok": False,
        "candle_green": False, "gap_widening": False,
        "fired": False, "verdict": "WARMUP " + str(progress) + "/" + str(needed),
        "path_a": False, "path_b": False,
        "momentum_pts": 0, "momentum_threshold": 0, "momentum_tf": "",
        "rsi_rising": False, "spot_confirms": False, "spot_move": 0,
        "other_falling": False, "other_move": 0,
        "two_green_above": False, "other_below_ema": False,
        "breakout_confirmed": False, "spot_slope": 0,
        "rsi_cap_active": 0, "spot_aligned": False,
        "entry_mode": "",
        "vwap": 0, "above_vwap": False, "vwap_dist": 0,
        "fib_nearest": "", "fib_distance": 0, "fib_pivot": 0,
        "fib_R1": 0, "fib_R2": 0, "fib_R3": 0,
        "fib_S1": 0, "fib_S2": 0, "fib_S3": 0,
        "vol_spike": False, "vol_ratio": 0,
        "pdh_break": False, "pdl_break": False,
        "prev_high": 0, "prev_low": 0,
    }


def _write_dashboard(spot_ltp, atm_strike, dte, vix_ltp, session,
                     profile, all_results, expiry, now,
                     dir_strikes=None):
    """Write everything the dashboard needs to a single JSON file."""
    if dir_strikes is None:
        dir_strikes = {}
    try:
        with _state_lock:
            st = dict(state)

        # ── Market context ──
        spot_3m = D.get_spot_indicators("3minute")
        spot_gap = D.get_spot_gap()
        fib_info = D.get_nearest_fib_level(spot_ltp) if spot_ltp > 0 else {}

        hourly_rsi = 0
        try:
            hourly_rsi = D.get_hourly_rsi() if hasattr(D, "get_hourly_rsi") else 0
        except Exception:
            pass

        bias = ""
        try:
            bias = D.get_daily_bias() if hasattr(D, "get_daily_bias") else ""
            if not bias:
                bias = ""
        except Exception:
            bias = ""

        straddle_open = getattr(D, "_straddle_open", 0)
        straddle_captured = getattr(D, "_straddle_captured", False)

        # ── Build CE/PE signal blocks (v15.2: Bands + Straddle Δ + VWAP) ──
        def _build_signal(opt_type, result):
            if not result:
                return {
                    "close": 0, "ema9_high": 0, "ema9_low": 0,
                    "band_position": "", "body_pct": 0,
                    "candle_green": False, "fired": False,
                    "verdict": "NO DATA", "ltp": 0, "entry_mode": "",
                    "strike": dir_strikes.get(opt_type, atm_strike),
                    "straddle_delta": None, "straddle_threshold": 0,
                    "straddle_period": "",
                    "spot_vwap": 0, "spot_vs_vwap": 0, "vwap_bonus": "",
                }
            _fired = result.get("fired", False)
            _mode = result.get("entry_mode", "")
            _close = float(result.get("close", result.get("entry_price", 0)))
            _eh = float(result.get("ema9_high", 0))
            _el = float(result.get("ema9_low", 0))
            _body = float(result.get("body_pct", 0))
            _green = result.get("candle_green", False)
            _pos = result.get("band_position", "")
            _reject = result.get("reject_reason", "")

            # Verdict
            _width = float(result.get("band_width", 0))
            if _fired:
                verdict = "READY TO FIRE"
            elif _reject:
                verdict = _reject
            elif _width > 0 and _width < 8:
                verdict = "narrow_band " + str(round(_width, 1)) + "pts (chop)"
            elif _pos == "ABOVE" and _green and _body >= 30:
                verdict = "READY"
            elif _pos == "ABOVE":
                verdict = "above band, waiting body/green"
            elif _pos == "IN":
                verdict = "inside band"
            else:
                verdict = "below band"

            return {
                # v15.x primary fields
                "close": round(_close, 2),
                "ema9_high": round(_eh, 2),
                "ema9_low": round(_el, 2),
                "band_width": round(_eh - _el, 2),
                "gap_from_ema9h": round(_close - _eh, 2),
                "band_position": _pos,
                "body_pct": round(_body, 1),
                "candle_green": _green,
                "reject_reason": _reject,
                "fired": _fired,
                "verdict": verdict,
                "entry_mode": _mode,
                "ltp": round(result.get("entry_price", 0), 2),
                "strike": result.get("_strike", dir_strikes.get(opt_type, atm_strike)),
                "bonus": result.get("bonus", {}),
                # v15.2 straddle filter context
                "straddle_delta":     result.get("straddle_delta"),
                "straddle_threshold": result.get("straddle_threshold", 0),
                "straddle_period":    result.get("straddle_period", ""),
                "atm_strike_used":    result.get("atm_strike_used", 0),
                # v15.2 VWAP bonus (display only)
                "spot_vwap":     round(float(result.get("spot_vwap", 0) or 0), 2),
                "spot_vs_vwap":  round(float(result.get("spot_vs_vwap", 0) or 0), 2),
                "vwap_bonus":    result.get("vwap_bonus", ""),
                # Legacy compat
                "rsi": 0, "ema9": round(_eh, 2), "ema21": 0,
            }

        # BUG-030: compute warmup state and use WARMUP placeholder during warmup
        _is_warm, _w_done, _w_need, _w_eta = _warmup_info(now, dte)
        if not _is_warm and D.is_market_open():
            ce_signal = _warmup_signal("CE", dir_strikes.get("CE", atm_strike), _w_done, _w_need, _w_eta)
            pe_signal = _warmup_signal("PE", dir_strikes.get("PE", atm_strike), _w_done, _w_need, _w_eta)
        else:
            ce_signal = _build_signal("CE", all_results.get("CE"))
            pe_signal = _build_signal("PE", all_results.get("PE"))

        # Flatten bonus dict into signal for dashboard consumption
        for _sig in (ce_signal, pe_signal):
            _b = _sig.pop("bonus", {})
            _sig["vwap"] = _b.get("vwap", 0)
            _sig["above_vwap"] = _b.get("above_vwap", False)
            _sig["vwap_dist"] = _b.get("vwap_dist", 0)
            _sig["fib_nearest"] = _b.get("fib_nearest", "")
            _sig["fib_distance"] = _b.get("fib_distance", 0)
            _sig["fib_pivot"] = _b.get("fib_pivot", 0)
            _sig["fib_R1"] = _b.get("fib_R1", 0)
            _sig["fib_R2"] = _b.get("fib_R2", 0)
            _sig["fib_R3"] = _b.get("fib_R3", 0)
            _sig["fib_S1"] = _b.get("fib_S1", 0)
            _sig["fib_S2"] = _b.get("fib_S2", 0)
            _sig["fib_S3"] = _b.get("fib_S3", 0)
            _sig["vol_spike"] = _b.get("vol_spike", False)
            _sig["vol_ratio"] = _b.get("vol_ratio", 0)
            _sig["pdh_break"] = _b.get("pdh_break", False)
            _sig["pdl_break"] = _b.get("pdl_break", False)
            _sig["prev_high"] = _b.get("prev_high", 0)
            _sig["prev_low"] = _b.get("prev_low", 0)

        # ── Fix LTP=0 when gate blocks early ──
        try:
            _tokens = D.get_option_tokens(None, atm_strike, expiry)
            for _sig, _side in [(ce_signal, "CE"), (pe_signal, "PE")]:
                if _sig.get("ltp", 0) == 0 and _side in _tokens:
                    _ltp = D.get_ltp(_tokens[_side]["token"])
                    if _ltp <= 0:
                        try:
                            _sym = _tokens[_side]["symbol"]
                            _q = D._kite.ltp("NFO:" + _sym)
                            _ltp = float(list(_q.values())[0]["last_price"])
                        except Exception:
                            pass
                    if _ltp > 0:
                        _sig["ltp"] = round(_ltp, 2)
        except Exception:
            pass

        # ── Position block with independent lot tracking ──
        position = {}
        if st.get("in_trade"):
            opt_ltp = D.get_ltp(st.get("token", 0))
            entry = st.get("entry_price", 0)
            running = round(opt_ltp - entry, 1) if opt_ltp > 0 else 0

            # v16.0: stop = ratchet SL if armed, else EMA9-low band
            _ratchet_sl = float(st.get("active_ratchet_sl", 0) or 0)
            if _ratchet_sl > 0:
                _stop_price = round(_ratchet_sl, 2)
                _stop_type = "RATCHET_" + str(st.get("active_ratchet_tier", ""))
            else:
                _stop_price = round(st.get("current_ema9_low", 0), 2)
                if _stop_price <= 0:
                    _stop_price = round(entry - 12, 2)
                _stop_type = "EMA9_LOW"

            lot1 = {"status": "active", "pnl": running,
                    "sl": _stop_price, "sl_type": _stop_type}
            lot2 = {"status": "active", "pnl": running,
                    "sl": _stop_price, "sl_type": _stop_type}

            position = {
                "in_trade": True,
                "symbol": st.get("symbol", ""),
                "direction": st.get("direction", ""),
                "entry": entry,
                "entry_time": st.get("entry_time", ""),
                "ltp": round(opt_ltp, 2) if opt_ltp > 0 else 0,
                "pnl": running,
                "peak": round(st.get("peak_pnl", 0), 1),
                "trough": round(st.get("trough_pnl", 0), 1),
                "candles": st.get("candles_held", 0),
                "strike": st.get("strike", 0),
                "entry_mode": st.get("entry_mode", ""),
                # v16.0 band context (display only)
                "current_ema9_high": round(st.get("current_ema9_high", 0), 2),
                "current_ema9_low":  round(st.get("current_ema9_low", 0), 2),
                "stop": _stop_price,
                "stop_dist": round(opt_ltp - _stop_price, 2)
                              if opt_ltp > 0 and _stop_price > 0 else 0,
                # v16.0 ratchet state
                "active_ratchet_tier": st.get("active_ratchet_tier", ""),
                "active_ratchet_sl":   round(float(st.get("active_ratchet_sl", 0) or 0), 2),
                # v15.2 entry context (replayed at exit on dashboard)
                "entry_straddle_delta":     st.get("entry_straddle_delta", 0),
                "entry_straddle_threshold": st.get("entry_straddle_threshold", 0),
                "entry_straddle_period":    st.get("entry_straddle_period", ""),
                "entry_band_width":         st.get("entry_band_width", 0),
                "entry_spot_vwap":          st.get("entry_spot_vwap", 0),
                "entry_spot_vs_vwap":       st.get("entry_spot_vs_vwap", 0),
                "entry_vwap_bonus":         st.get("entry_vwap_bonus", ""),
                # v15.2.5 velocity stall telemetry (sparkline + number)
                "peak_history":             (st.get("peak_history") or [])[-4:],
                "current_velocity":         round(float(st.get("current_velocity", 0) or 0), 2),
                # Legacy compat
                "lots_split": False,
                "current_rsi": round(st.get("current_rsi", 0), 1),
                "current_floor": round(st.get("current_ema9_low", 0), 2),
                "lot1": lot1,
                "lot2": lot2,
            }
        else:
            position = {"in_trade": False}

        # ── Today summary from trade log (single source of truth) ──
        _today_trades = _read_today_trades()
        _today_pnl_pts = 0.0
        _today_pnl_rs = 0.0
        _today_wins = 0
        _today_losses = 0
        for _tt in _today_trades:
            try:
                _p = float(_tt.get("pnl_pts", 0))
                _r = float(_tt.get("pnl_rs", 0))
                _today_pnl_pts += _p
                _today_pnl_rs += _r
                if _p > 0:
                    _today_wins += 1
                else:
                    _today_losses += 1
            except Exception:
                pass
        today_block = {
            "pnl": round(_today_pnl_pts, 1),
            "pnl_rs": round(_today_pnl_rs, 0),
            "trades": len(_today_trades),
            "wins": _today_wins,
            "losses": _today_losses,
            "streak": st.get("consecutive_losses", 0),
            "paused": st.get("paused", False),
            "profit_locked": st.get("profit_locked", False),
        }

        # ── Today charges from trade log ──
        try:
            _t_charges = sum(float(t.get("total_charges", 0)) for t in _today_trades)
            _t_gross = sum(float(t.get("gross_pnl_rs", t.get("pnl_rs", 0))) for t in _today_trades)
            _t_net = sum(float(t.get("net_pnl_rs", 0)) for t in _today_trades)
            today_block["total_charges"] = round(_t_charges, 2)
            today_block["gross_pnl_rs"] = round(_t_gross, 2)
            today_block["net_pnl_rs"] = round(_t_net, 2)
        except Exception:
            today_block["total_charges"] = 0
            today_block["gross_pnl_rs"] = 0
            today_block["net_pnl_rs"] = 0

        # ── Rolling stats ──
        rolling_block = {"last10_wr": 0, "last20_wr": 0, "last10_pts": 0, "streak": 0}
        try:
            import VRL_DB as _DB_dash
            _l10 = _DB_dash.query("SELECT pnl_pts FROM trades ORDER BY date DESC, entry_time DESC LIMIT 10")
            _l20 = _DB_dash.query("SELECT pnl_pts FROM trades ORDER BY date DESC, entry_time DESC LIMIT 20")
            _w10 = len([t for t in _l10 if float(t.get("pnl_pts", 0)) > 0])
            _w20 = len([t for t in _l20 if float(t.get("pnl_pts", 0)) > 0])
            _pts10 = sum(float(t.get("pnl_pts", 0)) for t in _l10)
            rolling_block["last10_wr"] = round(_w10 / len(_l10) * 100) if _l10 else 0
            rolling_block["last20_wr"] = round(_w20 / len(_l20) * 100) if _l20 else 0
            rolling_block["last10_pts"] = round(_pts10, 1)
            # Streak
            _streak = 0
            for _t in _l10:
                if float(_t.get("pnl_pts", 0)) > 0:
                    _streak += 1
                else:
                    break
            if _streak == 0:
                for _t in _l10:
                    if float(_t.get("pnl_pts", 0)) <= 0:
                        _streak -= 1
                    else:
                        break
            rolling_block["streak"] = _streak
        except Exception:
            pass

        # ── Straddle ──
        straddle_block = {
            "open": round(straddle_open, 1) if straddle_captured else 0,
            "captured": straddle_captured,
        }

        # ── v15.2 period (OPENING / MIDDAY / CLOSING) ──
        _mod = now.hour * 60 + now.minute
        if 585 <= _mod < 630:
            _period = "OPENING"
        elif 630 <= _mod < 840:
            _period = "MIDDAY"
        else:
            _period = "CLOSING"

        # ── Full snapshot ──
        dashboard = {
            "ts": now.strftime("%Y-%m-%d %H:%M:%S"),
            "version": D.VERSION,
            "mode": "PAPER" if D.PAPER_MODE else "LIVE",
            "period": _period,
            "market": {
                "spot": round(spot_ltp, 1),
                "atm": atm_strike,
                "locked_ce": _locked_ce_strike,
                "locked_pe": _locked_pe_strike,
                "locked_at_spot": round(_locked_at_spot, 1) if _locked_at_spot else 0,
                "dte": dte,
                "vix": round(vix_ltp, 1),
                "session": session,
                "regime": spot_3m.get("regime", ""),
                "bias": bias,
                "gap": round(spot_gap, 1),
                "spot_ema9": spot_3m.get("ema9", 0),
                "spot_ema21": spot_3m.get("ema21", 0),
                "spot_spread": spot_3m.get("spread", 0),
                "spot_rsi": spot_3m.get("rsi", 0),
                "spot_adx_3m": spot_3m.get("adx", 0),
                "hourly_rsi": round(hourly_rsi, 1),
                "fib_nearest": fib_info.get("level", ""),
                "fib_price": fib_info.get("price", 0),
                "fib_distance": round(fib_info.get("distance", 0), 1),
                "fib_pivots": D.get_fib_pivots() if hasattr(D, "get_fib_pivots") else {},
                "expiry": expiry.isoformat() if expiry else "",
                "market_open": D.is_market_open(),
                "indicators_warm": _is_warm,
                "warmup_progress": _w_done,
                "warmup_needed": _w_need,
                "warmup_eta": _w_eta,
            },
            "ce": ce_signal,
            "pe": pe_signal,
            "position": position,
            "today": today_block,
            "straddle": straddle_block,
            "account": {
                "name": D.get_account_info().get("name", ""),
                "user_id": D.get_account_info().get("user_id", ""),
                "balance": D.get_account_info().get("total_balance", 0),
                "available": D.get_account_info().get("available_margin", 0),
                "used": D.get_account_info().get("used_margin", 0),
            },
            "rolling": rolling_block,
        }

        # v15.2 Part 4: shadow 1-min head-to-head line
        try:
            import VRL_SHADOW
            dashboard["shadow"] = VRL_SHADOW.day_summary()
        except Exception:
            dashboard["shadow"] = {"trades": 0, "wins": 0, "losses": 0,
                                    "pnl": 0.0, "wr": 0, "avg_peak": 0.0,
                                    "peaks_over_10": 0}

        # Atomic write
        tmp = os.path.join(D.STATE_DIR, 'vrl_dashboard.json') + ".tmp"
        with open(tmp, "w") as f:
            json.dump(dashboard, f, indent=2, default=str)
        os.replace(tmp, os.path.join(D.STATE_DIR, 'vrl_dashboard.json'))

    except Exception as e:
        logger.debug("[DASH] Snapshot write: " + str(e))


def _strategy_loop(kite):
    global _running
    today_str = date.today().isoformat()
    logger.info("[MAIN] Strategy loop started")
    # Ensure state dir exists
    os.makedirs(os.path.expanduser("~/state"), exist_ok=True)
    # One-time trade log cleanup
    _cleanup_trade_log()
    # Compute bias
    try:
        D.compute_daily_bias(kite)
        logger.info("[MAIN] Daily bias: " + str(D.get_daily_bias()))
    except Exception as _be:
        logger.debug("[MAIN] Bias: " + str(_be))
    # Compute hourly RSI
    try:
        D.check_hourly_rsi(kite)
        logger.info("[MAIN] Hourly RSI: " + str(D.get_hourly_rsi()))
    except Exception as _he:
        logger.debug("[MAIN] H.RSI: " + str(_he))
    with _state_lock:
        state["_last_1min_candle"] = ""

    # Gap open detection — force strike relock if spot gapped 200+ pts
    try:
        _startup_spot = D.get_ltp(D.NIFTY_SPOT_TOKEN)
        _prev_close = state.get("prev_close", 0)
        if _prev_close > 0 and _startup_spot > 0:
            _gap = abs(_startup_spot - _prev_close)
            _gap_threshold = CFG.get().get("strike", {}).get("gap_relock_threshold", 200)
            if _gap > _gap_threshold:
                logger.info("[MAIN] GAP " + str(round(_gap)) + "pts — forcing strike relock at open")
                _tg_send("🔔 <b>GAP OPEN</b> " + str(round(_gap)) + "pts — strikes will relock")
                _reset_strike_lock()
    except Exception:
        pass

    expiry = D.get_nearest_expiry(kite)

    # Capture straddle if after 9:30 (must be AFTER expiry is resolved)
    try:
        _now = datetime.now()
        if expiry and _now.hour >= 9 and _now.minute >= 30:
            _ss = D.get_active_strike_step(D.calculate_dte(expiry))
            _sa = D.resolve_atm_strike(D.get_ltp(D.NIFTY_SPOT_TOKEN), _ss)
            if _sa > 0:
                D.capture_straddle(kite, _sa, expiry)
                logger.info("[MAIN] Straddle captured at startup")
    except Exception as _se:
        logger.debug("[MAIN] Straddle: " + str(_se))
    if expiry:
        logger.info("[MAIN] Expiry on startup: " + str(expiry))
    else:
        logger.warning("[MAIN] Expiry not resolved on startup — will retry in loop")

    while _running:
        try:
            now   = datetime.now()
            today = date.today()

            if today.isoformat() != today_str:
                today_str = today.isoformat()
                _reset_daily(today_str)
                expiry = D.get_nearest_expiry(kite)
                if not expiry:
                    for _retry in range(5):
                        _wait = 2 ** (_retry + 1)
                        logger.warning('[MAIN] Expiry resolve failed, retry '
                                       + str(_retry + 1) + ' in ' + str(_wait) + 's')
                        time.sleep(_wait)
                        expiry = D.get_nearest_expiry(kite)
                        if expiry:
                            break
                if not expiry:
                    logger.critical('[MAIN] Cannot resolve expiry after 5 retries')
                    _tg_send('\U0001f6a8 <b>CRITICAL: Expiry resolution failed. Bot paused.</b>\nUse /resume after market opens.')
                    with _state_lock:
                        state['paused'] = True
                    time.sleep(60)
                    continue
            dte     = D.calculate_dte(expiry) if expiry else 0
            profile = {"conv_sl_pts": 12}
            session = D.get_session_block(now.hour, now.minute)
            spot_ltp = D.get_ltp(D.NIFTY_SPOT_TOKEN)

            # v13.1: Auto-heal stale WebSocket (re-auth + reconnect)
            D.check_and_reconnect()

            # BUG-030: Log warmup progress once per minute during warmup
            try:
                _wm_warm, _wm_done, _wm_need, _wm_eta = _warmup_info(now, dte)
                if D.is_market_open() and not _wm_warm:
                    _wm_key = now.strftime("%H:%M")
                    if state.get("_last_warmup_log") != _wm_key:
                        state["_last_warmup_log"] = _wm_key
                        logger.info("[MAIN] Warmup progress: " + str(_wm_done)
                                    + "/" + str(_wm_need)
                                    + " candles (ETA: " + _wm_eta + ")")
            except Exception:
                pass

            # v12.15: Warning system
            try:
                _wmsg, _wupd = D.run_warnings(
                    kite, state, expiry, dte, spot_ltp, now)
                if _wupd:
                    with _state_lock:
                        state.update(_wupd)
                for _wm in _wmsg:
                    _tg_send(_wm)
            except Exception as _we:
                logger.warning("[MAIN] Warnings: " + str(_we))

            # v12.9 FIX: _error_count reset moved AFTER successful scan
            # (was here at top = circuit breaker never fired)

            with _state_lock:
                _pnl_snapshot = state.get("daily_pnl", 0)
            if check_profit_lock(state, _pnl_snapshot):
                _alert_profit_lock(_pnl_snapshot)
                _save_state()

            # v13.8 Change 3: Straddle decay aggressive mode
            _strad_open = getattr(D, "_straddle_open", 0)
            _strad_capt = getattr(D, "_straddle_captured", False)
            if (_strad_capt and _strad_open > 0
                    and not state.get("aggressive_mode")
                    and now.minute % 5 == 0 and now.second < 5):
                try:
                    _strad_curr = D.get_straddle_sum(kite, _locked_ce_strike, expiry) if hasattr(D, "get_straddle_sum") else 0
                    if _strad_curr > 0:
                        _decay_pct = (_strad_open - _strad_curr) / _strad_open
                        if _decay_pct >= 0.20:
                            with _state_lock:
                                state["aggressive_mode"] = True
                            _save_state()
                            logger.info("[MAIN] Aggressive mode ON — straddle decay "
                                        + str(round(_decay_pct * 100, 1)) + "%")
                            _tg_send("⚡ Aggressive mode activated\n"
                                     "Straddle decay " + str(round(_decay_pct * 100, 0))
                                     + "% — directional day confirmed")
                except Exception:
                    pass

            with _state_lock:
                _eod_done = state.get("_eod_reported")
            # v13.3: Save prev_close continuously from 15:25 onward — avoids
            # missing the 30-second EOD window if the loop is slow.
            # BUG-H Batch 4: surface which source (WS vs REST) actually saved
            # the value so operators can diagnose WebSocket stale-tick issues
            # from the log without needing a broker replay.
            if now.hour == 15 and now.minute >= 25:
                try:
                    _saved_via = ""
                    _safe_spot = D.get_ltp(D.NIFTY_SPOT_TOKEN)
                    if _safe_spot > 0:
                        _saved_via = "WS"
                    elif kite is not None:
                        try:
                            q = kite.ltp(["NSE:NIFTY 50"])
                            _safe_spot = float(list(q.values())[0]["last_price"])
                            _saved_via = "REST"
                        except Exception as _re25:
                            logger.debug("[MAIN] 15:25+ REST fallback failed: "
                                         + str(_re25))
                    if _safe_spot > 0:
                        with _state_lock:
                            # Only log once per source transition to avoid
                            # spamming the 5-minute-long save window.
                            prev_src = state.get("_prev_close_src", "")
                            if prev_src != _saved_via:
                                logger.info("[MAIN] prev_close source: " + _saved_via
                                            + " @ " + now.strftime("%H:%M:%S")
                                            + " (spot=" + str(round(_safe_spot, 1)) + ")")
                                state["_prev_close_src"] = _saved_via
                            state["prev_close"] = round(_safe_spot, 1)
                except Exception:
                    pass

            if (now.hour == 15 and now.minute == 35
                    and not _eod_done
                    and now.second < 30):
                with _state_lock:
                    state["_eod_reported"] = True
                    # Save prev_close for gap detection next day
                    _eod_spot = D.get_ltp(D.NIFTY_SPOT_TOKEN)
                    if _eod_spot <= 0 and kite is not None:
                        try:
                            q = kite.ltp(["NSE:NIFTY 50"])
                            _eod_spot = float(list(q.values())[0]["last_price"])
                            logger.info("[MAIN] EOD spot via REST: " + str(_eod_spot))
                        except Exception as _re:
                            logger.warning("[MAIN] EOD spot REST fallback failed: " + str(_re))
                    if _eod_spot > 0:
                        state["prev_close"] = round(_eod_spot, 1)
                        logger.info("[MAIN] prev_close saved: " + str(state["prev_close"]))
                    else:
                        # BUG-H Batch 4: Telegram on total EOD save failure.
                        # Without prev_close, tomorrow's gap-relock guard
                        # can't fire — operators need to know tonight so
                        # they can manually set state.prev_close or force
                        # a relock at the 9:15 open.
                        logger.warning("[MAIN] prev_close NOT saved — both WS and REST returned 0")
                        try:
                            _tg_send(
                                "⚠️ <b>EOD prev_close SAVE FAILED</b>\n"
                                "Both WebSocket and REST ltp() returned 0 at 15:35.\n"
                                "Tomorrow's gap-relock guard will be disabled.\n"
                                "Manual fix option: set state.prev_close via restart"
                                " + /status, or force relock after 9:15 open.",
                                priority="critical",   # BUG-U: bypass flood
                            )
                        except Exception:
                            pass
                _save_state()
                try:
                    _generate_eod_report()
                except Exception as e:
                    logger.error("[MAIN] EOD report error: " + str(e))
                try:
                    from VRL_LAB import generate_daily_summary
                    generate_daily_summary()
                except Exception as e:
                    logger.warning("[MAIN] Daily summary: " + str(e))
                # ── v15.2 Part 4: emit ONE shadow-vs-live EOD summary ──
                try:
                    import VRL_SHADOW
                    _live_trades_today = _read_today_trades()
                    _live_n = len(_live_trades_today)
                    _live_w = sum(1 for t in _live_trades_today
                                  if float(t.get("pnl_pts", 0)) > 0)
                    _live_pnl = round(sum(float(t.get("pnl_pts", 0))
                                           for t in _live_trades_today), 1)
                    _live_wr = round(_live_w / _live_n * 100) if _live_n else 0
                    VRL_SHADOW.emit_eod_summary(
                        _tg_send,
                        live_stats={"trades": _live_n, "wins": _live_w,
                                    "pnl": _live_pnl, "wr": _live_wr})
                except Exception as _se:
                    logger.warning("[SHADOW] EOD summary: " + str(_se))

            # ── v15.2.5 Batch 3 BUG-R4: EOD shadow exit Telegram ──
            # One-shot after the existing EOD report, gated by same
            # _eod_reported flag (fires at 15:35).
            try:
                if state.get("_eod_reported") and not state.get("_shadow_exit_eod_sent"):
                    state["_shadow_exit_eod_sent"] = True
                    _send_shadow_exit_eod()
            except Exception as _sre:
                logger.debug("[SHADOW_EXIT] EOD telegram: " + str(_sre))

            # ── BUG-V v15.2.5 Batch 6: daily lab cleanup at 15:45+ IST ──
            # Previously cleanup ran only at bot startup. A process that
            # stays up for a week accumulates 7 days of option_3min /
            # signal_scans growth with no trim. Run once per trading day
            # after market close (15:45+) so it can't compete with live
            # trading I/O. Gated by state._last_cleanup_date so we don't
            # repeat on every loop after 15:45.
            try:
                if now.hour == 15 and now.minute >= 45:
                    _today_iso = date.today().isoformat()
                    _need_cleanup = False
                    with _state_lock:
                        if state.get("_last_cleanup_date") != _today_iso:
                            state["_last_cleanup_date"] = _today_iso
                            _need_cleanup = True
                    if _need_cleanup:
                        logger.info("[MAIN] Running daily lab cleanup at "
                                    + now.strftime("%H:%M"))
                        try:
                            D.cleanup_old_lab_data()
                        except Exception as _ce:
                            logger.warning("[MAIN] Lab cleanup error: "
                                           + str(_ce))
                        try:
                            import VRL_DB as _DB_clean
                            _DB_clean.cleanup_old_db_data()
                        except Exception as _dce:
                            logger.warning("[MAIN] DB cleanup error: "
                                           + str(_dce))
                        _save_state()
            except Exception as _ce_outer:
                logger.debug("[MAIN] Daily cleanup dispatch: "
                             + str(_ce_outer))

            with _state_lock:
                _force = state.get("force_exit")
                _in_trade = state.get("in_trade")
                _token = state.get("token")
                _symbol = state.get("symbol", "")
                _entry_px = state.get("entry_price", 0)
            if _force and _in_trade:
                option_ltp = D.get_ltp(_token)
                # BUG-027: use floor SL as minimum if LTP is stale/zero
                _floor_sl = state.get("_static_floor_sl", state.get("phase1_sl", 0))
                _exit_px = option_ltp if option_ltp > 0 else max(_entry_px, _floor_sl)
                # BUG-D fix: thread the pre-captured entry through so PNL is
                # computed against the REAL entry — not a race-stale state read.
                _execute_exit(kite, _exit_px, "FORCE_EXIT",
                              saved_entry_price=_entry_px)
                time.sleep(1)
                continue

            # BUG-N5 v15.2.5: unconditionally mark EOD at 15:30+ BEFORE
            # the in_trade block. Old code only set _eod_exited inside
            # the in_trade force-exit path, so days with no trade at
            # close left the flag at None / False in state.json.
            if (now.hour > 15 or (now.hour == 15 and now.minute >= 30)):
                if not state.get("_eod_exited"):
                    with _state_lock:
                        state["_eod_exited"] = True
                    logger.info("[MAIN] _eod_exited=True at "
                                + now.strftime("%H:%M:%S")
                                + " (no trade open → flag-only)")

            if _in_trade:
                option_ltp = D.get_ltp(_token)
                if option_ltp <= 0 and kite is not None:
                    try:
                        q = kite.ltp(["NFO:" + _symbol])
                        option_ltp = float(q["NFO:" + _symbol]["last_price"])
                        logger.info("[MAIN] Option LTP via REST: " + str(option_ltp))
                    except Exception as e:
                        logger.warning("[MAIN] REST option LTP failed: " + str(e))
                if option_ltp > 0:
                    # Update RSI for dashboard display (independent of manage_exit)
                    try:
                        _dash_df = D.get_historical_data(_token, "minute", 5)
                        _dash_df = D.add_indicators(_dash_df)
                        if not _dash_df.empty and len(_dash_df) >= 2:
                            _dash_rsi = round(float(_dash_df.iloc[-2].get("RSI", 0)), 1)
                            with _state_lock:
                                state["current_rsi"] = _dash_rsi
                    except Exception:
                        pass

                    with _state_lock:
                        cur_1m = now.strftime("%H:%M")
                        if cur_1m != state.get("_last_candle_held_min", ""):
                            state["_last_candle_held_min"] = cur_1m
                            state["candles_held"] = state.get("candles_held", 0) + 1
                            # v13.5: reset candle_low on new 1-min candle boundary
                            state["_candle_low"] = option_ltp
                            # v13.5: 3-min CONFIRMED upgrade during FAST trade
                            if state.get("entry_mode") == "FAST" and not state.get("_3m_confirmed"):
                                try:
                                    _upg_token = state.get("token")
                                    _df3u = D.get_historical_data(_upg_token, "3minute", 10)
                                    if _df3u is not None and len(_df3u) >= 6:
                                        _prev3u = _df3u.iloc[-3]
                                        _ref3u = float(_df3u.iloc[-6]["close"])
                                        _mom3u = round(float(_prev3u["close"]) - _ref3u, 2)
                                        _cfg_up = CFG.get().get("entry", {})
                                        _conf_thr = _cfg_up.get("confirmed_momentum_pts", 20)
                                        if _mom3u >= _conf_thr:
                                            with _state_lock:
                                                state["entry_mode"] = "CONFIRMED"
                                                state["_3m_confirmed"] = True
                                            logger.info("[MAIN] 3-min CONFIRMED upgrade: mom_3m=" + str(_mom3u))
                                            _tg_send("★★ 3-min CONFIRMED — "
                                                + state.get("direction", "") + " +" + str(_mom3u) + "pts (3m)\n"
                                                + "Trail widened: keep 65% from +20 🔥")
                                except Exception as _ue:
                                    logger.debug("[MAIN] 3m upgrade: " + str(_ue))

                    # ── v15.2.5 Batch 3 BUG-R2: shadow exit CSV logger ──
                    # Runs BEFORE manage_exit so it captures pre-exit state.
                    # Pure logging — never mutates state or calls exit fns.
                    try:
                        _log_shadow_exit_tick(state, option_ltp, now)
                    except Exception as _se:
                        if not getattr(_log_shadow_exit_tick, "_warned", False):
                            logger.warning("[SHADOW_EXIT] tick logger error: "
                                           + str(_se))
                            _log_shadow_exit_tick._warned = True

                    # v13.0: manage_exit returns list of exit dicts
                    _mex_other_tok = state.get("other_token", 0)
                    exit_list = manage_exit(state, option_ltp, profile, other_token=_mex_other_tok)

                    # v15.0: Peak milestone alerts + exchange SL-M band update
                    if state.get("in_trade"):
                        _peak = state.get("peak_pnl", 0)
                        _last_ms = state.get("_last_milestone", 0)
                        _cur_el = round(float(state.get("current_ema9_low", 0)), 1)
                        _entry_px = state.get("entry_price", 0)
                        # Fire milestone alert once per threshold crossed
                        for _m in [5, 10, 15, 20, 30, 40, 50]:
                            if _peak >= _m and _last_ms < _m:
                                with _state_lock:
                                    state["_last_milestone"] = _m
                                _dist = round(option_ltp - _cur_el, 1) if _cur_el > 0 else 0
                                _tg_send("📈 <b>+" + str(_m) + "pts</b>"
                                         + " | Trail: EMA9-low ₹" + str(_cur_el)
                                         + " (" + str(_dist) + "pts away)")
                                break
                        # Update exchange SL-M trigger to current EMA9-low
                        try:
                            from VRL_TRADE import modify_sl_order
                            _new_trigger = _cur_el
                            _sid = state.get("_sl_order_id", "")
                            _cur = state.get("_sl_trigger_at_exchange", 0)
                            if _sid and _new_trigger > _cur:
                                if modify_sl_order(kite, _sid, _new_trigger):
                                    with _state_lock:
                                        state["_sl_trigger_at_exchange"] = _new_trigger
                        except Exception:
                            pass

                    # Live mode: force exit at 15:25 (before broker auto square-off at 15:30)
                    # Paper mode: exit at 15:28 as before
                    _eod_cutoff = 25 if not D.PAPER_MODE else 28
                    if now.hour == 15 and now.minute >= _eod_cutoff:
                        if not D.PAPER_MODE and now.minute < 28:
                            logger.warning("[MAIN] 15:25 SAFETY — forcing exit before broker square-off")
                            _tg_send("⚠️ <b>15:25 SAFETY EXIT</b>\nClosing before broker auto square-off")
                        exit_list = [{"lots": "ALL", "lot_id": "ALL",
                                      "reason": "EOD_SAFETY" if not D.PAPER_MODE else "MARKET_CLOSE",
                                      "price": option_ltp}]

                    # BUG-028: catch-all EOD exit at 15:30+ if cutoff above was missed
                    if (now.hour > 15 or (now.hour == 15 and now.minute >= 30)):
                        if not state.get("_eod_exited"):
                            logger.warning("[MAIN] 15:30 catch-all — forcing exit on open trade")
                            _tg_send("⚠️ <b>15:30 MARKET CLOSE</b>\nForcing exit on open position")
                            exit_list = [{"lots": "ALL", "lot_id": "ALL",
                                          "reason": "MARKET_CLOSE", "price": option_ltp}]
                            state["_eod_exited"] = True

                    # Dashboard signal scan — only on new 1-min candle
                    _scan_min = now.strftime("%H:%M")
                    if _scan_min != state.get("_last_dash_scan_min", "") and now.second >= 31:
                        state["_last_dash_scan_min"] = _scan_min
                        try:
                            _trade_scan = {}
                            _trade_dir = state.get("direction", "")
                            _trade_token = state.get("token")
                            _trade_strike = state.get("strike", 0)
                            for _dt in ("CE", "PE"):
                                if _dt == _trade_dir and _trade_token:
                                    _sr = check_entry(_trade_token, _dt, spot_ltp, dte, expiry, kite, silent=True)
                                    _sr["_strike"] = _trade_strike
                                else:
                                    _oi = _locked_tokens.get(_dt) if _locked_tokens else None
                                    if _oi:
                                        _sr = check_entry(_oi["token"], _dt, spot_ltp, dte, expiry, kite, silent=True)
                                        _sr["_strike"] = _locked_ce_strike if _dt == "CE" else _locked_pe_strike
                                    else:
                                        _sr = None
                                if _sr:
                                    _trade_scan[_dt] = _sr
                            _write_dashboard(spot_ltp, state.get("strike", 0),
                                             dte, D.get_vix(), session,
                                             profile, _trade_scan, expiry, now,
                                             dir_strikes={"CE": _locked_ce_strike, "PE": _locked_pe_strike})
                        except Exception:
                            pass

                    # Capture entry_price BEFORE any exit resets it
                    _saved_entry = state.get("entry_price", 0)
                    for _exit in exit_list:
                        _execute_exit_v13(kite, _exit, saved_entry_price=_saved_entry)

                    if not exit_list and state.get("in_trade"):
                        entry    = state.get("entry_price", 0)
                        pnl      = round(option_ltp - entry, 1)
                        last_ms  = state.get("_last_milestone", 0)
                        milestone= (int(pnl) // 10) * 10
                        if milestone > last_ms and milestone > 0 and state.get("lots_split"):
                            with _state_lock:
                                state["_last_milestone"] = milestone
                                _ms_trail_sl = state.get("lot2_trail_sl", 0)
                            _ms_sl_str = str(round(_ms_trail_sl, 1)) if _ms_trail_sl > 0 else "—"
                            _tg_send(
                                "📈 +" + str(milestone) + "pts | SL ₹" + _ms_sl_str + " (ATR)"
                            )
                        _save_state()

                # Quick LTP update every ~5 seconds (in-trade)
                if now.second % 5 < 2:
                    _update_dashboard_ltp()

                time.sleep(0.5)
                continue

            # ── NO RE‑ENTRY WATCHING — removed ──────────────────

            # v12.15: Feed spot buffer for consolidation detection
            if spot_ltp > 0:
                D.update_spot_buffer({
                    "timestamp": now.isoformat(),
                    "open": spot_ltp, "high": spot_ltp,
                    "low": spot_ltp, "close": spot_ltp,
                })

            if (not state.get("paused")
                    and D.is_trading_window(now)
                    and _is_new_1min_candle(now)
                    and spot_ltp > 0
                    and expiry is not None):

                # v12.15: Feed proper 1-min candle to spot buffer
                try:
                    _spot_df = D.get_historical_data(D.NIFTY_SPOT_TOKEN, "minute", 5)
                    if not _spot_df.empty and len(_spot_df) >= 2:
                        _last_spot = _spot_df.iloc[-2]
                        D.update_spot_buffer({
                            "timestamp": str(_spot_df.index[-2]),
                            "open": float(_last_spot["open"]),
                            "high": float(_last_spot["high"]),
                            "low": float(_last_spot["low"]),
                            "close": float(_last_spot["close"]),
                        })
                except Exception:
                    pass

                step       = D.get_active_strike_step(dte)
                atm_strike = D.resolve_atm_strike(spot_ltp, step)

                # ── STRIKE LOCKING — stable scanning ──────────────
                # Lock strikes until spot moves 150+ pts or trade exits
                # v13.3: True ATM-50 — relock whenever the nearest 50 changes.
                # The old 150pt threshold was fine for 100pt strikes but skips
                # 2-3 strikes when the step is 50.
                _relock = False
                _is_initial_lock = False
                _spot_move = 0.0
                _old_ce = None
                _old_pe = None
                if _locked_at_spot is None or _locked_ce_strike is None:
                    _relock = True
                    _is_initial_lock = True
                else:
                    _target_atm = int(round(spot_ltp / 50) * 50)
                    if _target_atm != _locked_ce_strike:
                        _relock = True
                        _spot_move = round(spot_ltp - _locked_at_spot, 1)
                        _old_ce = _locked_ce_strike
                        _old_pe = _locked_pe_strike
                        logger.info("[MAIN] ATM drift: locked=" + str(_locked_ce_strike)
                                    + " target=" + str(_target_atm)
                                    + " spot=" + str(round(spot_ltp, 1)) + " — RELOCKING")

                # BUG-S2 v15.2.5: defer relock when a setup is building
                # on the locked strike. Replaces the dead v13 momentum_pts
                # check (always 0 in v15.2 — never fired). New check uses
                # is_setup_building() which looks at close>ema9h, green,
                # body≥25, band≥6 — all the gates at ≥75% threshold.
                # Hard override: spot drift > 75pts forces relock regardless
                # (premium too far OTM to be reliable at that distance).
                _spot_drift = abs(spot_ltp - _locked_at_spot) if _locked_at_spot else 0
                _setup_building = False
                if _relock and not _is_initial_lock and _spot_drift <= 75:
                    try:
                        _building_ce = False
                        _building_pe = False
                        if _locked_tokens:
                            _ce_info = _locked_tokens.get("CE")
                            _pe_info = _locked_tokens.get("PE")
                            if _ce_info:
                                from VRL_ENGINE import is_setup_building
                                _building_ce = is_setup_building(
                                    int(_ce_info["token"]), "CE")
                            if _pe_info:
                                from VRL_ENGINE import is_setup_building
                                _building_pe = is_setup_building(
                                    int(_pe_info["token"]), "PE")
                        _setup_building = _building_ce or _building_pe
                    except Exception as _sbe:
                        logger.debug("[MAIN] setup_building check: " + str(_sbe))

                if _relock and _setup_building and _spot_drift <= 75:
                    _skip_count = state.get("_relock_skip_count", 0) + 1
                    state["_relock_skip_count"] = _skip_count
                    if _skip_count <= 2:
                        _which = ("CE" if _building_ce else "PE")
                        logger.info("[MAIN] ATM drift but setup BUILDING on "
                                    + _which + " " + str(_locked_ce_strike)
                                    + " — deferring relock ("
                                    + str(_skip_count) + "/2)"
                                    + " spot_drift=" + str(round(_spot_drift, 1)))
                        _relock = False
                    else:
                        logger.info("[MAIN] Relock FORCED after "
                                    + str(_skip_count)
                                    + " setup-building skips")
                        state["_relock_skip_count"] = 0
                elif _relock and _spot_drift > 75:
                    logger.info("[MAIN] Relock FORCED — spot drift "
                                + str(round(_spot_drift, 1))
                                + "pts > 75pt hard override")
                    state["_relock_skip_count"] = 0
                if _relock:
                    _lock_strikes(spot_ltp, dte, kite, expiry)
                    state["_relock_skip_count"] = 0  # BUG-021: reset on successful relock
                    # Telegram alert on relock (not initial lock)
                    if False:  # v13.3: relock alerts silenced — dashboard shows current strike
                        _tg_send(
                            "\U0001f512 <b>RELOCK</b>: CE "
                            + str(_old_ce) + " → " + str(_locked_ce_strike)
                            + " | PE " + str(_old_pe) + " → " + str(_locked_pe_strike)
                            + "\n(spot moved " + ("+" if _spot_move > 0 else "")
                            + str(_spot_move) + "pts)"
                        )

                # Use locked strikes — no recalculation per cycle
                dir_strikes = {"CE": _locked_ce_strike, "PE": _locked_pe_strike}
                dir_tokens = dict(_locked_tokens)

                # If locked tokens empty, force relock — never use unlocked ATM
                if not dir_tokens:
                    logger.warning("[MAIN] Locked tokens empty — forcing relock")
                    _lock_strikes(spot_ltp, dte, kite, expiry)
                    dir_tokens = dict(_locked_tokens)
                    dir_strikes = {"CE": _locked_ce_strike, "PE": _locked_pe_strike}
                    if not dir_tokens:
                        logger.warning("[MAIN] Relock failed — skipping cycle")
                        time.sleep(2)
                        continue

                # v13.1: Same entry logic for ALL DTEs (including DTE=0)
                # ── v13.5 SCAN — dual-TF momentum + divergence ─────
                all_results = {}
                best_result = None
                best_type = None
                best_opt_info = None

                # v13.3: Scan once per minute
                # v13.3: 3-min scan boundary (entry timeframe is 3-min)
                _now_scan = datetime.now()
                # v13.5: 1-min scan boundary (check_entry handles both 1m + 3m)
                _scan_key = _now_scan.strftime("%Y%m%d%H%M")
                _should_scan = _scan_key != state.get("_last_scan_key", "")
                if not _should_scan:
                    time.sleep(1)
                    continue
                with _state_lock:
                    state["_last_scan_key"] = _scan_key
                    state["_last_scan_minute"] = _now_scan.strftime("%H:%M")

                if not D.is_tick_live(D.INDIA_VIX_TOKEN):
                    D.subscribe_tokens([D.INDIA_VIX_TOKEN])

                # v13.5: extract both tokens for divergence check
                _ce_info_v15 = _locked_tokens.get("CE") if _locked_tokens else None
                _pe_info_v15 = _locked_tokens.get("PE") if _locked_tokens else None
                _ce_tok_v15 = _ce_info_v15.get("token", 0) if _ce_info_v15 else 0
                _pe_tok_v15 = _pe_info_v15.get("token", 0) if _pe_info_v15 else 0

                for opt_type in ("CE", "PE"):
                    opt_info = dir_tokens.get(opt_type)
                    if not opt_info:
                        continue

                    _other_tok = _pe_tok_v15 if opt_type == "CE" else _ce_tok_v15
                    result = check_entry(
                        token=opt_info["token"],
                        option_type=opt_type,
                        spot_ltp=spot_ltp,
                        dte=dte,
                        expiry_date=expiry,
                        kite=kite,
                        other_token=_other_tok,
                        state=state,
                    )
                    result["_strike"] = dir_strikes.get(opt_type, atm_strike)
                    # Bonus indicators — info only, never block
                    try:
                        result["bonus"] = _compute_bonus(opt_info["token"])
                    except Exception:
                        result["bonus"] = {}
                    all_results[opt_type] = result

                    if not result["fired"]:
                        continue

                    # Pre-entry checks (cooldown, margin, etc)
                    option_ltp_now = D.get_ltp(opt_info["token"])
                    if option_ltp_now <= 0:
                        try:
                            q = kite.ltp(["NFO:" + opt_info["symbol"]])
                            option_ltp_now = float(list(q.values())[0]["last_price"])
                        except Exception:
                            pass
                    ok, reason = pre_entry_checks(
                        kite, opt_info["token"], state,
                        option_ltp_now, profile, session,
                        direction=opt_type)
                    if not ok:
                        logger.info("[MAIN] Entry blocked (" + opt_type + "): " + reason)
                        continue

                    best_result = result
                    best_type = opt_type
                    best_opt_info = opt_info
                    break  # First to pass → enters (no scoring comparison)

                try:
                    vix_ltp = D.get_vix()
                except Exception:
                    vix_ltp = 0.0

                # Save scan state
                ce_res = all_results.get("CE", {})
                pe_res = all_results.get("PE", {})
                with _state_lock:
                    state["_last_scan"] = {
                        "time": now.strftime("%H:%M:%S"),
                        "session": session,
                        "vix": round(vix_ltp, 2),
                        "dte": dte,
                        "atm": atm_strike,
                        "fired": best_type or "No",
                        "fired_type": best_type or "—",
                        "ce": ce_res,
                        "pe": pe_res,
                    }

                # ── v15.2.5: pre-entry awareness alerts (learning mode) ──
                # Non-blocking. Only runs during the trading window (outer
                # if-gate guarantees that). Rate-limited inside VRL_ALERTS.
                try:
                    import VRL_ALERTS
                    with _state_lock:
                        _alert_state = {
                            "pre_entry_alerts_enabled":
                                state.get("pre_entry_alerts_enabled", True),
                            "alert_history":
                                dict(state.get("alert_history") or {}),
                        }
                    _signals = VRL_ALERTS.detect_pre_entry_signals(
                        all_results, _alert_state, dfs=None)
                    # Persist updated history + send
                    if _signals:
                        with _state_lock:
                            state["alert_history"] = _alert_state.get(
                                "alert_history", {})
                        for _sig in _signals:
                            _tg_send(_sig["msg"])
                except Exception as _ae:
                    logger.debug("[ALERTS] dispatch error: " + str(_ae))

                # ── v15.2 Part 4: silent 1-min shadow strategy ────────
                # Runs after live scan on every 1-min boundary. Independent
                # state, independent cooldown, never touches live state,
                # never places orders, never alerts during the day.
                # v15.2.2: heartbeat log proves the call site is reached
                # before we delegate, and exc_info=True surfaces any error.
                try:
                    logger.info("[SHADOW_1MIN] call_site reached at "
                                + now.strftime("%H:%M:%S")
                                + " spot=" + str(spot_ltp))
                    from VRL_ENGINE import shadow_scan_1min
                    shadow_scan_1min(spot_ltp)
                except Exception as _shade:
                    logger.warning("[SHADOW_1MIN] call_site error: "
                                   + str(_shade), exc_info=True)

                # Write dashboard + cache args for post-exit refresh
                global _last_dash_args
                _last_dash_args = {
                    "spot_ltp": spot_ltp, "atm_strike": atm_strike,
                    "dte": dte, "vix_ltp": vix_ltp,
                    "session": session, "profile": profile,
                    "expiry": expiry,
                }
                try:
                    _write_dashboard(spot_ltp, atm_strike, dte, vix_ltp, session,
                                     profile, all_results, expiry, now,
                                     dir_strikes=dir_strikes)
                except Exception as _de:
                    logger.debug("[DASH] " + str(_de))

                if best_result and best_opt_info:
                    _execute_entry(kite, best_opt_info, best_type,
                                   best_result, profile, expiry, dte, session)
                    # BUG-N3: mark that this scan became a real trade.
                    # VRL_LAB's next scan row will read the flag via
                    # D.consume_trade_taken() and set trade_taken=1.
                    if state.get("in_trade"):
                        D.mark_trade_taken(best_type)

            # Quick LTP update between candle scans
            if now.second % 10 < 2:
                _update_dashboard_ltp()

            # v12.9: Reset error count only after a successful loop iteration
            if state.get("_error_count", 0) > 0:
                with _state_lock:
                    state["_error_count"] = 0

        except Exception as e:
            logger.error("[MAIN] Loop error: " + str(e))
            with _state_lock:
                state["_error_count"] = state.get("_error_count", 0) + 1
                _cb_threshold = 3 if not D.PAPER_MODE else 5
                if state["_error_count"] >= _cb_threshold and not state.get("_circuit_breaker"):
                    if state.get('in_trade'):
                        try:
                            cb_ltp = D.get_ltp(state.get('token', 0))
                            if cb_ltp > 0:
                                _execute_exit(kite, cb_ltp, 'CIRCUIT_BREAKER_EXIT')
                                logger.warning('[MAIN] Circuit breaker: emergency exit executed')
                            else:
                                logger.critical('[MAIN] Circuit breaker: LTP=0, manual exit required')
                        except Exception as cb_e:
                            logger.critical('[MAIN] Circuit breaker exit failed: ' + str(cb_e))
                    state["_circuit_breaker"] = True
                    state["paused"]            = True
                    logger.critical("[MAIN] ⚡ CIRCUIT BREAKER — "
                                    + str(state["_error_count"]) + " errors")
                    _tg_send("⚡ <b>CIRCUIT BREAKER</b>\n"
                             + str(state["_error_count"]) + " consecutive errors.\n"
                             + "Bot paused. /resume to restart.\n"
                             + "Error: " + str(e)[:100])
            time.sleep(2)

        time.sleep(1)


# ═══════════════════════════════════════════════════════════════
#  TELEGRAM COMMANDS — extracted to VRL_COMMANDS.py
# ═══════════════════════════════════════════════════════════════
import VRL_COMMANDS

# ── Telegram listener state ───────────────────────────────────
_tg_offset         = 0
_tg_last_update_id = 0
_tg_running        = False

def _tg_get_updates(offset: int) -> list:
    url = _TG_BASE + D.TELEGRAM_TOKEN + "/getUpdates"
    try:
        resp = requests.get(url, params={"offset": offset, "timeout": 20}, timeout=30)
        if resp.ok:
            return resp.json().get("result", [])
    except Exception as e:
        logger.warning("[CTRL] getUpdates error: " + type(e).__name__)
    return []

def _tg_authorized(message: dict) -> bool:
    return str(message.get("chat", {}).get("id", "")) == str(D.TELEGRAM_CHAT_ID)

def _tg_handle_message(message: dict):
    if not _tg_authorized(message):
        return
    text = message.get("text", "").strip()
    if not text.startswith("/"):
        return
    parts   = text.split()
    raw_cmd = parts[0].split("@")[0].lower()
    args    = parts[1:] if len(parts) > 1 else []
    handler = VRL_COMMANDS._DISPATCH.get(raw_cmd)
    if handler:
        handler(args)
    else:
        _WATCHDOG = ("/deploy","/serverstatus","/serverlog","/gitlog")
        if raw_cmd not in _WATCHDOG:
            _tg_send("Unknown command: " + raw_cmd + "\nType /help")

def _tg_handle_callback(callback: dict):
    # Auth check — same as _tg_handle_message
    msg = callback.get("message", {})
    if str(msg.get("chat", {}).get("id", "")) != str(D.TELEGRAM_CHAT_ID):
        return
    query_id = callback.get("id", "")
    data     = callback.get("data", "")
    if data.startswith("FB:"):
        VRL_COMMANDS._handle_file_browser_callback(data, query_id)
    elif data.startswith("DL:"):
        VRL_COMMANDS._handle_download_callback(data, query_id)
    else:
        _tg_answer_callback(query_id, "Unknown action")

def _tg_poll_loop():
    global _tg_offset, _tg_last_update_id
    logger.info("[CTRL] Telegram listener started " + D.VERSION)
    while _tg_running:
        updates = _tg_get_updates(_tg_offset)
        for upd in updates:
            uid          = upd["update_id"]
            _tg_offset   = uid + 1
            if uid <= _tg_last_update_id:
                continue
            _tg_last_update_id = uid
            try:
                if "message" in upd:
                    _tg_handle_message(upd["message"])
                elif "callback_query" in upd:
                    _tg_handle_callback(upd["callback_query"])
            except Exception as e:
                logger.error("[CTRL] Update error: " + str(e))
        time.sleep(1)

def _start_telegram_listener():
    global _tg_running, _tg_offset
    _tg_running = True

    try:
        url  = _TG_BASE + D.TELEGRAM_TOKEN + "/getUpdates"
        resp = requests.get(url, params={"offset": -1, "timeout": 1}, timeout=5)
        if resp.ok:
            updates = resp.json().get("result", [])
            if updates:
                _tg_offset = updates[-1]["update_id"] + 1
                logger.info("[CTRL] Discarded " + str(len(updates))
                            + " pending updates on startup")
    except Exception as e:
        logger.warning("[CTRL] Startup getUpdates skip: " + type(e).__name__)

    thread = threading.Thread(target=_tg_poll_loop, name="TGListener", daemon=True)
    thread.start()
    logger.info("[CTRL] Listener thread launched")

def _stop_telegram_listener():
    global _tg_running
    _tg_running = False

# ═══════════════════════════════════════════════════════════════
#  SHUTDOWN
# ═══════════════════════════════════════════════════════════════

def _shutdown(signum, frame):
    global _running
    logger.info("[MAIN] Shutdown signal received")
    _running = False
    _stop_telegram_listener()
    # BUG-028: warn if shutting down with open trade
    # fix(BUG-C-tail): also Telegram the operator. Uses _tg_send_sync
    # (blocking) rather than _tg_send (async) so the message actually
    # fires before sys.exit(0). BUG-U's CRITICAL flood-control bypass
    # is not implemented yet; direct sync send is the specified fallback.
    if state.get("in_trade"):
        _sym   = state.get("symbol", "?")
        _entry = round(state.get("entry_price", 0), 2)
        _pk    = round(state.get("peak_pnl", 0), 1)
        logger.warning("[MAIN] Shutdown with open trade — state preserved for resume"
                       " (symbol=" + _sym
                       + " entry=" + str(_entry)
                       + " peak=" + str(_pk) + ")")
        try:
            _tg_send_sync(
                "⚠️ VRL SHUTDOWN with open position: " + _sym
                + " entry=" + str(_entry)
                + " peak=" + str(_pk),
                priority="critical",   # BUG-U: bypass flood on shutdown
            )
        except Exception as _tge:
            # Shutdown path: network may already be down. Swallow —
            # the log line above is the fallback signal.
            logger.debug("[MAIN] Shutdown telegram send failed: " + str(_tge))
    _save_state()
    _remove_pid()
    logger.info("[MAIN] Clean shutdown")
    sys.exit(0)

# ═══════════════════════════════════════════════════════════════
#  ENTRY POINT
# ═══════════════════════════════════════════════════════════════

def main():
    global _kite
    logger.info("[MAIN] ═══ VISHAL RAJPUT TRADE " + D.VERSION + " STARTING ═══")
    logger.info("[MAIN] Mode: " + ("PAPER" if D.PAPER_MODE else "LIVE"))
    logger.info("[MAIN] Scalps: DISABLED (data-backed decision)")

    _write_pid()
    signal.signal(signal.SIGINT,  _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    # BUG-029 Task 2: Explicit token freshness check on startup
    # get_kite() already auto-refreshes but we want loud logging + Telegram alert
    try:
        import json as _j
        from datetime import date as _dt_date
        _tok_path = D.TOKEN_FILE_PATH
        _tok_data = {}
        if os.path.isfile(_tok_path):
            with open(_tok_path) as _tf:
                _tok_data = _j.load(_tf)
        _tok_date = _tok_data.get("date", "")
        _today = _dt_date.today().isoformat()
        if _tok_date != _today:
            logger.warning("[MAIN] Token is from " + str(_tok_date or "MISSING")
                           + ", not today (" + _today + ") — forcing fresh auth")
            _tg_send("\u26a0\ufe0f Stale token detected on startup, auto-refreshing\n"
                     "Old: " + str(_tok_date or "MISSING") + " → New: " + _today)
        else:
            logger.info("[MAIN] Token freshness check: OK (" + _today + ")")
    except Exception as _te:
        logger.warning("[MAIN] Token freshness check error: " + str(_te))

    kite = get_kite()
    _kite = kite
    D.init(kite)

    # ── Token health ping (prediction #5 prevention) ────────────
    # One Telegram at startup confirming token is alive, Kite API
    # responds, and spot LTP resolves. If any layer fails, the
    # operator knows BEFORE 09:15 that the session is compromised.
    # This covers the 08:00→09:10 gap where AUTH succeeded but
    # Zerodha maintenance killed the token afterward.
    try:
        _health_ok = True
        _health_lines = []
        # 1. Profile check (proves token is valid)
        try:
            _prof = kite.profile()
            _health_lines.append("Token: ✅ " + str(_prof.get("user_name", "?")))
        except Exception as _he:
            _health_lines.append("Token: ❌ " + str(_he)[:60])
            _health_ok = False
        # 2. Spot quote (proves API data flow)
        try:
            _sq = kite.ltp(["NSE:NIFTY 50"])
            _sp = float(list(_sq.values())[0]["last_price"])
            _health_lines.append("Spot: ✅ " + str(round(_sp, 1)))
        except Exception as _he:
            _health_lines.append("Spot: ❌ " + str(_he)[:60])
            _health_ok = False
        # 3. WebSocket (proves tick feed)
        import time as _time_h
        _time_h.sleep(3)
        _ws_ltp = D.get_ltp(D.NIFTY_SPOT_TOKEN)
        if _ws_ltp > 0:
            _health_lines.append("WS: ✅ tick=" + str(round(_ws_ltp, 1)))
        else:
            _health_lines.append("WS: ⚠️ no tick yet (may need 30s to connect)")
        _icon = "✅" if _health_ok else "⚠️"
        _tg_send(
            _icon + " <b>TOKEN HEALTH CHECK</b>\n"
            + "\n".join(_health_lines) + "\n"
            "Time: " + datetime.now().strftime("%H:%M:%S IST")
        )
        logger.info("[MAIN] Token health: " + (" | ".join(_health_lines)))
    except Exception as _the:
        logger.warning("[MAIN] Token health check error: " + str(_the))

    # v13.10: Register WS auto-heal Telegram callback
    try:
        D.set_autoheal_callback(_tg_send)
    except Exception:
        pass

    # Fetch account info at startup
    try:
        D.fetch_account_info(kite)
    except Exception:
        pass

    live_lot_size = D.get_lot_size(kite)
    D.LOT_SIZE    = live_lot_size
    logger.info("[MAIN] Lot size from broker: " + str(live_lot_size))

    _load_state()
    _reconcile_positions(kite)

    # BUG-028: Clear phantom trade state if bot starts outside market hours
    if state.get("in_trade") and not D.is_market_open():
        logger.warning("[MAIN] Startup with in_trade=True but market is CLOSED — clearing phantom state")
        _tg_send("⚠️ Phantom trade detected on startup — state cleared\n"
                 "Symbol: " + state.get("symbol", "?") + "\n"
                 "Entry: " + str(state.get("entry_price", 0)) + "\n"
                 "Peak: " + str(state.get("peak_pnl", 0)))
        with _state_lock:
            state["in_trade"] = False
            state["symbol"] = ""
            state["token"] = None
            state["direction"] = ""
            state["entry_price"] = 0.0
            state["entry_time"] = ""
            state["exit_phase"] = 1
            state["phase1_sl"] = 0.0
            state["peak_pnl"] = 0.0
            state["candles_held"] = 0
            state["lot1_active"] = True
            state["lot2_active"] = True
            state["lots_split"] = False
            state["_static_floor_sl"] = 0
            state["current_floor"] = 0.0
        _save_state()
        logger.info("[MAIN] Phantom trade state cleared ✓")

    # Run daily lab data cleanup
    try:
        D.cleanup_old_lab_data()
    except Exception as e:
        logger.warning("[MAIN] Lab cleanup failed: " + str(e))

    # BUG-DL3 v15.2.5: log directory audit — one INFO line per category
    # present, one WARNING per missing dir. Helps the operator see at a
    # glance what `/download` will actually find on disk.
    try:
        D.audit_log_paths()
    except Exception as _ae:
        logger.debug("[MAIN] audit_log_paths error: " + str(_ae))

    # Wire Telegram commands module
    VRL_COMMANDS.setup(
        state_ref=state, lock_ref=_state_lock,
        tg_send_fn=_tg_send, tg_send_file_fn=_tg_send_file,
        tg_inline_keyboard_fn=_tg_inline_keyboard,
        tg_answer_callback_fn=_tg_answer_callback,
        save_state_fn=_save_state, read_today_trades_fn=_read_today_trades,
        remove_pid_fn=_remove_pid, now_str_fn=_now_str, rs_fn=_rs,
        kite_ref=kite,
    )

    try:
        import csv as _csv
        today_iso = date.today().isoformat()
        trades_today = []

        for log_path in [D.TRADE_LOG_PATH,
                         os.path.join(D.LAB_DIR, "vrl_trade_log.csv")]:
            if not os.path.isfile(log_path):
                continue
            try:
                with open(log_path) as f:
                    raw_rows = list(_csv.DictReader(f))

                found = []
                for r in raw_rows:
                    if r.get("date", "").strip() == today_iso:
                        found.append(r)
                    elif r.get("trade_id", "").strip() == today_iso:
                        found.append({
                            **r,
                            "date"   : r.get("trade_id", ""),
                            "pnl_pts": r.get("pnl_points", r.get("pnl_pts", "0")),
                        })
                if found:
                    trades_today = found
                    break
            except Exception:
                continue

        if trades_today:
            def _get_pnl(row):
                for k in ["pnl_pts", "pnl_points", "pnl_rs", "pnl"]:
                    if k in row:
                        try: return float(row[k])
                        except (TypeError, ValueError): pass
                return 0.0

            wins   = [t for t in trades_today if _get_pnl(t) > 0]
            losses = [t for t in trades_today if _get_pnl(t) < 0]
            pnl    = sum(_get_pnl(t) for t in trades_today)

            with _state_lock:
                state["daily_trades"]       = len(trades_today)
                state["daily_losses"]       = len(losses)
                state["daily_pnl"]          = round(pnl, 2)
                # v12.7 fix: count streak from tail — not total losses
                # e.g. W L L W → streak=0, not 2
                streak = 0
                for t in reversed(trades_today):
                    if _get_pnl(t) < 0:
                        streak += 1
                    else:
                        break
                state["consecutive_losses"] = streak

            logger.info("[MAIN] Restored: " + str(len(trades_today))
                        + " trades | " + str(len(losses)) + " losses | pnl="
                        + str(round(pnl,1)) + "pts")
        else:
            logger.info("[MAIN] No trades found for today — starting fresh")
    except Exception as e:
        logger.warning("[MAIN] Trade log restore failed: " + str(e))

    # BUG-020: Sync CSV trades into DB on startup (backfill after DB rebuild)
    try:
        import csv as _sync_csv
        import VRL_DB as _sync_db
        _sync_db.init_db()
        _today_iso = date.today().isoformat()
        _csv_path = D.TRADE_LOG_PATH
        _csv_trades = []
        if os.path.isfile(_csv_path):
            with open(_csv_path) as _sf:
                for _row in _sync_csv.DictReader(_sf):
                    _d = _row.get("date", "").strip()
                    if _d == _today_iso:
                        _csv_trades.append(_row)
        _db_trades = _sync_db.get_trades(_today_iso)
        _db_times = {t.get("entry_time", "").strip() for t in _db_trades}
        _inserted = 0
        for _ct in _csv_trades:
            _et = _ct.get("entry_time", "").strip()
            if _et and _et not in _db_times:
                _sync_db.insert_trade(_ct)
                _inserted += 1
        if _inserted > 0:
            logger.info("[SYNC] CSV→DB backfill: " + str(_inserted) + " rows inserted")
        else:
            logger.info("[SYNC] CSV/DB in sync for " + _today_iso
                        + " (CSV=" + str(len(_csv_trades)) + " DB=" + str(len(_db_trades)) + ")")
    except Exception as _se:
        logger.warning("[SYNC] CSV→DB backfill failed: " + str(_se))

    D.start_websocket()
    D.subscribe_tokens([D.NIFTY_SPOT_TOKEN, D.INDIA_VIX_TOKEN])
    time.sleep(2)

    # v14.0: Pre-warm 3-min indicators from previous trading day.
    # Kite historical_data returns multi-day candles, so at 9:15 we
    # already have yesterday's session (or Friday if Monday, auto-
    # skipping weekends/holidays). Entries unblock at 9:31 immediately.
    try:
        _pw = D.get_historical_data(D.NIFTY_SPOT_TOKEN, "3minute", 30)
        if _pw is not None and not _pw.empty:
            logger.info("[MAIN] Pre-warm: " + str(len(_pw))
                        + " 3-min spot candles loaded from history")
        else:
            logger.warning("[MAIN] Pre-warm: no historical 3-min data returned")
    except Exception as _pwe:
        logger.warning("[MAIN] Pre-warm failed: " + str(_pwe))

    # v12.11: Calculate spot gap on startup
    try:
        gap_info = D.calculate_spot_gap()
        if gap_info["gap_pts"] != 0:
            logger.info("[MAIN] Spot gap: " + str(gap_info["gap_pts"]) + "pts"
                        + " (" + str(gap_info["gap_pct"]) + "%)")
    except Exception as e:
        logger.warning("[MAIN] Gap calculation failed: " + str(e))

    # v12.15: Calculate fib pivot points
    try:
        pivots = D.calculate_fib_pivots()
        if pivots:
            logger.info("[MAIN] Fib pivots loaded: P=" + str(pivots.get("pivot", 0))
                        + " R1=" + str(pivots.get("R1", 0))
                        + " S1=" + str(pivots.get("S1", 0)))
    except Exception as e:
        logger.warning("[MAIN] Fib pivot calc failed: " + str(e))

    start_lab(kite)
    _start_telegram_listener()
    _alert_bot_started()

    logger.info("[MAIN] All systems ready. Strategy loop starting.")
    _strategy_loop(kite)

if __name__ == "__main__":
    main()
