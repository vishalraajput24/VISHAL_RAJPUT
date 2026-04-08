# ═══════════════════════════════════════════════════════════════
#  VRL_MAIN.py — VISHAL RAJPUT TRADE v13.3
#  Master orchestration. Minimal strategy: EMA gap + RSI.
#  2-lot execution with profit floors + RSI split.
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
from datetime import date, datetime

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
    "in_trade"           : False,
    "symbol"             : "",
    "token"              : None,
    "direction"          : "",
    "entry_price"        : 0.0,
    "entry_time"         : "",
    "exit_phase"         : 1,
    "phase1_sl"          : 0.0,
    "phase2_sl"          : 0.0,
    "qty"                : D.LOT_SIZE,
    "trail_tightened"    : False,
    "profit_locked"      : False,
    "consecutive_losses" : 0,
    "daily_trades"       : 0,
    "daily_losses"       : 0,
    "daily_pnl"          : 0.0,
    "peak_pnl"           : 0.0,
    "mode"               : "",
    "iv_at_entry"        : 0.0,
    "score_at_entry"     : 0,
    "regime_at_entry"    : "",
    "dte_at_entry"       : 0,
    "last_exit_time"     : "",
    "last_exit_direction": "",
    "last_exit_peak"     : 0.0,
    "_last_trail_candle" : "",
    "strike"             : 0,
    "expiry"             : "",
    "paused"             : False,
    "force_exit"         : False,
    "candles_held"       : 0,
    "lot1_active"        : True,
    "lot2_active"        : True,
    "lots_split"         : False,
    "lot_count"          : 2,
    "lot2_trail_sl"      : 0.0,
    "lot1_exit_price"    : 0.0,
    "lot1_exit_pnl"      : 0.0,
    "lot1_exit_reason"   : "",
    "lot1_exit_time"     : "",
    "lot2_exit_price"    : 0.0,
    "lot2_exit_pnl"      : 0.0,
    "lot2_exit_reason"   : "",
    "entry_mode"         : "",
    "momentum_pts"       : 0.0,
    "_sl_order_id"       : "",
    "_sl_trigger_at_exchange": 0,
    "_last_milestone"    : 0,
    "current_rsi"        : 0.0,
    "current_floor"      : 0.0,
    "floor_10_alerted"   : False,
    "floor_20_alerted"   : False,
    "floor_30_alerted"   : False,
    "split_alerted"      : False,
    "_last_1min_candle"  : "",
    "_eod_reported"      : False,
    "_last_candle_held_min": "",
    "_rsi_was_overbought": False,
    "_last_scan"         : {},
    "_exit_failed"       : False,
    "_circuit_breaker"   : False,
    "_error_count"       : 0,
    "_last_milestone"    : 0,
    "trough_pnl"         : 0.0,
    "session_at_entry"   : "",
    "spread_1m_at_entry" : 0.0,
    "spread_3m_at_entry" : 0.0,
    "delta_at_entry"     : 0.0,
    "sl_pts_at_entry"    : 0.0,
    "_bias_done"         : False,
    "_straddle_done"     : False,
    "_hourly_rsi_ts"     : 0,
    "_vix_warned"        : False,
    "_straddle_alerted"  : False,
    "prev_close"         : 0.0,
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
        persist_fields = D.STATE_PERSIST_FIELDS + ["_rsi_was_overbought"]
        with _state_lock:
            subset = {k: state.get(k) for k in persist_fields}
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
        state["paused"]                = False
        state["_bias_done"]            = False
        state["_straddle_done"]        = False
        state["_hourly_rsi_ts"]        = 0
        state["_vix_warned"]           = False
        state["_straddle_alerted"]     = False
    D.clear_token_cache()
    D.reset_daily_warnings()
    _reset_strike_lock()
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

TRADE_FIELDNAMES = [
    "date", "entry_time", "exit_time", "symbol", "direction",
    "mode", "entry_price", "exit_price", "pnl_pts", "pnl_rs",
    "peak_pnl", "trough_pnl", "exit_reason", "exit_phase",
    "score", "iv_at_entry", "regime", "dte", "candles_held",
    "session", "strike", "sl_pts",
    "spread_1m", "spread_3m", "delta_at_entry",
    "bias", "vix_at_entry", "hourly_rsi",
    "straddle_decay",
    "brokerage", "stt", "exchange_charges", "gst", "stamp_duty",
    "total_charges", "net_pnl_rs", "gross_pnl_rs", "num_exit_orders",
    "entry_slippage", "exit_slippage", "signal_price",
    "lot_id",
    "bonus_vwap", "bonus_fib_level", "bonus_fib_dist",
    "bonus_vol_spike", "bonus_vol_ratio", "bonus_pdh_break",
    "qty_exited",
    "entry_mode", "momentum_pts",
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

def _log_trade(st: dict, exit_price: float, exit_reason: str,
               candles_held: int = 0, saved_entry: float = None,
               lot_id: str = "ALL", qty: int = 0):
    os.makedirs(os.path.dirname(D.TRADE_LOG_PATH), exist_ok=True)
    is_new  = not os.path.isfile(D.TRADE_LOG_PATH)
    entry   = saved_entry if saved_entry is not None else st.get("entry_price", 0)
    pnl_pts = round(exit_price - entry, 2)
    _lot_qty = qty if qty > 0 else D.LOT_SIZE
    pnl_rs  = round(pnl_pts * _lot_qty, 2)

    row = {
        "date"          : date.today().isoformat(),
        "entry_time"    : st.get("entry_time", ""),
        "exit_time"     : datetime.now().strftime("%H:%M:%S"),
        "symbol"        : st.get("symbol", ""),
        "direction"     : st.get("direction", ""),
        "mode"          : st.get("mode", ""),
        "entry_price"   : entry,
        "exit_price"    : round(exit_price, 2),
        "pnl_pts"       : pnl_pts,
        "pnl_rs"        : pnl_rs,
        "peak_pnl"      : round(st.get("peak_pnl", 0), 2),
        "trough_pnl"    : round(st.get("trough_pnl", 0), 2),
        "exit_reason"   : exit_reason,
        "exit_phase"    : st.get("exit_phase", 1),
        "score"         : st.get("score_at_entry", 0),
        "iv_at_entry"   : st.get("iv_at_entry", 0),
        "regime"        : st.get("regime_at_entry", ""),
        "dte"           : st.get("dte_at_entry", 0),
        "candles_held"  : candles_held,
        "session"       : st.get("session_at_entry", ""),
        "strike"        : st.get("strike", 0),
        "sl_pts"        : st.get("sl_pts_at_entry", 0),
        "spread_1m"     : st.get("spread_1m_at_entry", 0),
        "spread_3m"     : st.get("spread_3m_at_entry", 0),
        "delta_at_entry": st.get("delta_at_entry", 0),
        "bias": D.get_daily_bias(),
        "vix_at_entry": round(D.get_vix(), 1),
        "hourly_rsi": D.get_hourly_rsi(),
        "straddle_decay": 0.0,
        "entry_slippage": st.get("entry_slippage", 0),
        "exit_slippage": 0,
        "signal_price": st.get("signal_price", 0),
        "lot_id": lot_id,
        "bonus_vwap": 1 if st.get("bonus_vwap") else 0,
        "bonus_fib_level": st.get("bonus_fib_level", ""),
        "bonus_fib_dist": round(st.get("bonus_fib_dist", 0), 2),
        "bonus_vol_spike": 1 if st.get("bonus_vol_spike") else 0,
        "bonus_vol_ratio": round(st.get("bonus_vol_ratio", 0), 2),
        "bonus_pdh_break": 1 if st.get("bonus_pdh_break") else 0,
        "qty_exited": _lot_qty,
        "entry_mode": st.get("entry_mode", "EMA"),
        "momentum_pts": round(st.get("momentum_pts", 0), 2),
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
    except Exception:
        pass

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
    rupees = round(pts * D.LOT_SIZE, 0)
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

def _tg_send_sync(text: str, parse_mode: str = "HTML", chat_id: str = None) -> bool:
    """Blocking send with flood control — max 5 msgs per 10s."""
    if not D.TELEGRAM_TOKEN or not (chat_id or D.TELEGRAM_CHAT_ID):
        return False

    # Flood control — prevent Telegram 429 rate limit
    now_ts = time.time()
    while _tg_timestamps and now_ts - _tg_timestamps[0] > _TG_FLOOD_WINDOW:
        _tg_timestamps.popleft()
    if len(_tg_timestamps) >= _TG_FLOOD_LIMIT:
        wait = _TG_FLOOD_WINDOW - (now_ts - _tg_timestamps[0])
        if wait > 0:
            time.sleep(min(wait, _TG_FLOOD_WINDOW))
    _tg_timestamps.append(time.time())

    cid = chat_id or D.TELEGRAM_CHAT_ID
    url = _TG_BASE + D.TELEGRAM_TOKEN + "/sendMessage"
    try:
        resp = requests.post(url, json={
            "chat_id"              : cid,
            "text"                 : text,
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

def _tg_send(text: str, parse_mode: str = "HTML", chat_id: str = None) -> bool:
    """Non-blocking send — fires in background thread so strategy loop never waits."""
    t = threading.Thread(
        target=_tg_send_sync,
        args=(text, parse_mode, chat_id),
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
        "Entry  : MOMENTUM +15pts/3c + RSI≥45 + Green + HL\n"
        "         EMA confirms → CONFIRMED ★★\n"
        "Exit   : Trail peak-6 after +10 | SL -12 | RSI 80\n"
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
            "Lots: 2 × " + str(D.LOT_SIZE) + " = " + str(D.LOT_SIZE * 2) + " qty\n"
            "SL: 12pts = ₹" + "{:,}".format(12 * D.LOT_SIZE * 2) + " max loss per trade\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "Every order uses REAL money.\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
        )

def _alert_profit_lock(daily_pnl: float):
    _tg_send(
        "🔒 <b>PROFIT LOCK — +" + str(round(daily_pnl,1)) + "pts  " + _rs(daily_pnl) + "</b>\n"
        "New entries still open but protected mode on."
    )

def _alert_exit_critical(symbol: str, qty: int):
    _tg_send(
        "🚨 <b>CRITICAL: EXIT FAILED</b>\n"
        "Symbol : " + symbol + "  Qty: " + str(qty) + "\n"
        "MANUAL EXIT REQUIRED NOW\n"
        "Open Kite app immediately."
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
    entry_price = entry_result["entry_price"]

    import VRL_CONFIG as CFG
    lot_count = CFG.get().get("lots", {}).get("count", 2)
    total_qty = D.LOT_SIZE * lot_count

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

    with _state_lock:
        state["in_trade"]           = True
        state["symbol"]             = symbol
        state["token"]              = token
        state["direction"]          = option_type
        state["entry_price"]        = actual_price
        state["entry_time"]         = datetime.now().strftime("%H:%M:%S")
        state["exit_phase"]         = 1
        state["phase1_sl"]          = phase1_sl
        state["qty"]                = actual_qty
        state["lot_count"]          = lot_count
        state["lot1_active"]        = True
        state["lot2_active"]        = True
        state["lots_split"]         = False
        state["lot2_trail_sl"]      = 0.0
        state["trail_tightened"]    = False
        state["peak_pnl"]           = 0.0
        state["mode"]               = "MINIMAL"
        state["score_at_entry"]     = 0
        state["iv_at_entry"]        = 0
        state["regime_at_entry"]    = ""
        state["dte_at_entry"]       = dte
        state["strike"]             = entry_result.get("_strike", D.resolve_atm_strike(
            D.get_ltp(D.NIFTY_SPOT_TOKEN), D.get_active_strike_step(dte)))
        state["expiry"]             = expiry.isoformat() if expiry else ""
        state["candles_held"]       = 0
        state["_last_trail_candle"] = ""
        state["_rsi_was_overbought"] = False
        state["daily_trades"]      += 1
        state["trough_pnl"]         = 0.0
        state["session_at_entry"]   = session
        state["spread_1m_at_entry"] = round(entry_result.get("ema_gap", 0), 2)
        state["spread_3m_at_entry"] = 0.0
        state["delta_at_entry"]     = 0.0
        state["sl_pts_at_entry"]    = hard_sl
        state["current_rsi"]        = round(entry_result.get("rsi", 0), 1)
        state["current_floor"]      = phase1_sl
        state["entry_slippage"]     = _entry_slippage
        state["signal_price"]       = entry_price
        state["entry_mode"]         = entry_result.get("entry_mode", "MOMENTUM")
        state["_last_milestone"]    = 0
        state["momentum_pts"]       = entry_result.get("momentum_pts", 0)
        state["spike_ratio"]        = entry_result.get("spike_ratio", 0)
        _bonus_data = entry_result.get("bonus", {})
        state["bonus_vwap"]         = _bonus_data.get("above_vwap", False)
        state["bonus_fib_level"]    = _bonus_data.get("fib_nearest", "")
        state["bonus_fib_dist"]     = _bonus_data.get("fib_distance", 0)
        state["bonus_vol_spike"]    = _bonus_data.get("vol_spike", False)
        state["bonus_vol_ratio"]    = _bonus_data.get("vol_ratio", 0)
        state["bonus_pdh_break"]    = _bonus_data.get("pdh_break", False)
        state["floor_10_alerted"]   = False
        state["floor_20_alerted"]   = False
        state["floor_30_alerted"]   = False
        state["split_alerted"]      = False

    _save_state()

    # Place exchange backup SL order
    try:
        from VRL_TRADE import place_sl_order
        _sl_price = phase1_sl
        _sl_oid = place_sl_order(kite, symbol, actual_qty, _sl_price)
        with _state_lock:
            state["_sl_order_id"] = _sl_oid
            state["_sl_trigger_at_exchange"] = round(_sl_price, 1)
    except Exception:
        pass

    # Entry alert
    _slip_line = ""
    if _entry_slippage > 0:
        _slip_line = "Slip: +" + str(_entry_slippage) + "pts\n"
    # Bonus indicators line (wrapped in try — never crash entry alert)
    _bonus_line = ""
    try:
        _eb = entry_result.get("bonus", {})
        _bt = []
        if _eb.get("above_vwap"):
            _bt.append("VWAP ✓")
        else:
            _bt.append("VWAP ✗")
        if _eb.get("fib_nearest"):
            _bt.append("Fib " + str(_eb["fib_nearest"]) + " " + str(_eb.get("fib_distance", 0)) + "pts")
        if _eb.get("vol_spike"):
            _bt.append("Vol " + str(_eb.get("vol_ratio", 0)) + "x 🔥")
        if _eb.get("pdh_break"):
            _bt.append("PDH ↑")
        if _eb.get("pdl_break"):
            _bt.append("PDL ↓")
        if _bt:
            _bonus_line = "Bonus: " + " | ".join(_bt) + "\n"
    except Exception:
        _bonus_line = ""
    _emode = entry_result.get("entry_mode", "MOMENTUM")
    _mom_pts = entry_result.get("momentum_pts", 0)
    _mom_thr = entry_result.get("momentum_threshold", 15)
    _sr = entry_result.get("spike_ratio", 0)
    _quality = "spike ⚡" if _sr > 0.6 else "steady"
    if _emode == "CONFIRMED":
        _detail = "Mom +" + str(_mom_pts) + "pts (DTE" + str(dte) + ":" + str(_mom_thr) + ") (" + _quality + ") + EMA " + str(round(entry_result.get("ema_gap", 0), 1)) + " 🔥\n"
    else:
        _detail = "Mom: +" + str(_mom_pts) + "pts/3c (DTE" + str(dte) + ":" + str(_mom_thr) + ") | " + _quality + " | RSI " + str(round(entry_result.get("rsi", 0), 0)) + " | HL ✓\n"
    _tg_send(
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "🎯 <b>" + _short_sym(symbol, option_type, state.get("strike", 0))
        + " × " + str(lot_count) + " LOTS [" + _emode + "]</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        + datetime.now().strftime("%H:%M") + "  ₹" + str(round(actual_price, 1)) + "\n"
        + _detail + _slip_line +
        "SL ₹" + str(round(phase1_sl, 1)) + " (-" + str(hard_sl) + "pts)\n"
        + _bonus_line +
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

    # Determine qty — for ALL exit use full entry qty
    if lot_id == "ALL":
        exit_qty = state.get("qty", D.LOT_SIZE * 2)
    else:
        exit_qty = D.LOT_SIZE

    # Cancel exchange backup SL before exit
    try:
        _sl_oid = state.get("_sl_order_id", "")
        if _sl_oid:
            from VRL_TRADE import cancel_sl_order
            cancel_sl_order(kite, _sl_oid)
    except Exception:
        pass

    fill = place_exit(kite, symbol, token, direction,
                      exit_qty, exit_price, reason)

    if not fill["ok"] and fill.get("error") == "EXIT_FAILED_MANUAL_REQUIRED":
        with _state_lock:
            state["_exit_failed"] = True
        _alert_exit_critical(symbol, exit_qty)
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

    pnl_lots = pnl * (exit_qty / D.LOT_SIZE)

    # Log EVERY lot exit (not just trade_done)
    _log_trade(state, actual_exit, reason, candles, saved_entry=entry,
               lot_id=lot_id, qty=exit_qty)

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
            old_token = state["token"]
            state.update({
                "in_trade": False, "symbol": "", "token": None,
                "direction": "", "entry_price": 0.0, "entry_time": "",
                "exit_phase": 1, "phase1_sl": 0.0, "phase2_sl": 0.0,
                "peak_pnl": 0.0, "trough_pnl": 0.0, "mode": "",
                "candles_held": 0, "force_exit": False, "_exit_failed": False,
                "lot1_active": True, "lot2_active": True, "lots_split": False,
                "lot2_trail_sl": 0.0,
                "lot1_exit_price": 0.0, "lot1_exit_pnl": 0.0,
                "lot1_exit_reason": "", "lot1_exit_time": "",
                "lot2_exit_price": 0.0, "lot2_exit_pnl": 0.0,
                "lot2_exit_reason": "",
                "_sl_order_id": "", "_sl_trigger_at_exchange": 0,
                "floor_10_alerted": False, "floor_20_alerted": False,
                "floor_30_alerted": False, "split_alerted": False,
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
        _day_rs     = int(_day_pnl * D.LOT_SIZE)
        import VRL_CONFIG as _CFG_exit
        _cd_cfg     = _CFG_exit.get().get("cooldown", {})
        # Calculate charges for Telegram
        _num_eo = 2 if state.get("lots_split") else 1
        try:
            _ch = CHARGES.calculate_charges(entry, actual_exit,
                      exit_qty, _num_eo)
        except Exception:
            _ch = {"gross_pnl": pnl * (exit_qty / D.LOT_SIZE) * D.LOT_SIZE,
                   "total_charges": 0, "net_pnl": pnl * (exit_qty / D.LOT_SIZE) * D.LOT_SIZE,
                   "charges_pts": 0}
        if pnl >= 0:
            _tg_send(
                "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                "✅ <b>" + _sym_short + "  +" + str(round(pnl, 1)) + "pts</b>\n"
                "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                "₹" + str(round(entry, 1)) + " → ₹" + str(round(actual_exit, 1))
                + " | " + reason + "\n"
                "Peak +" + str(round(peak, 1)) + " | " + str(candles) + "min\n"
                "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                "Gross  : +₹" + "{:,}".format(abs(int(_ch["gross_pnl"]))) + "\n"
                "Charges: -₹" + "{:,}".format(int(_ch["total_charges"]))
                + " (" + str(_ch["charges_pts"]) + "pts)\n"
                "Net    : " + ("+" if _ch["net_pnl"] >= 0 else "-") + "₹"
                + "{:,}".format(abs(int(_ch["net_pnl"]))) + "\n"
                "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                "DAY: " + ("+" if _day_pnl >= 0 else "") + str(round(_day_pnl, 1)) + "pts"
                + " | " + str(_day_wins) + "W " + str(_day_losses) + "L\n"
                "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
            )
        else:
            _cd_min = _cd_cfg.get("after_loss", 5)
            _fast_sl = "⚡ FAST SL — " + str(candles) + " candle exit\n" if reason == "HARD_SL" and candles <= 1 else ""
            _tg_send(
                "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                "❌ <b>" + _sym_short + "  " + str(round(pnl, 1)) + "pts</b>\n"
                "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                + reason + " | Peak +" + str(round(peak, 1)) + " | " + str(candles) + "min\n"
                + _fast_sl +
                "Gross  : -₹" + "{:,}".format(abs(int(_ch["gross_pnl"]))) + "\n"
                "Charges: -₹" + "{:,}".format(int(_ch["total_charges"])) + "\n"
                "Net    : -₹" + "{:,}".format(abs(int(_ch["net_pnl"]))) + "\n"
                "⏳ " + direction + " blocked " + str(_cd_min) + "min\n"
                "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                "DAY: " + ("+" if _day_pnl >= 0 else "") + str(round(_day_pnl, 1)) + "pts"
                + " | " + str(_day_wins) + "W " + str(_day_losses) + "L\n"
                "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
            )
    else:
        # Partial exit — update daily PNL for the exited lot
        with _state_lock:
            state["daily_pnl"] = round(state.get("daily_pnl", 0) + pnl, 2)
        remaining = "LOT2" if state.get("lot2_active") else "LOT1"
        _sym_short_p = _short_sym(symbol, direction, _exit_strike)
        try:
            _ch_p = CHARGES.calculate_lot_charges(entry, actual_exit, D.LOT_SIZE)
        except Exception:
            _ch_p = {"net_pnl": pnl * D.LOT_SIZE, "total_charges": 0}
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


def _execute_exit(kite, option_ltp: float, reason: str):
    """Legacy wrapper — exits all lots."""
    _execute_exit_v13(kite, {"lots": "ALL", "lot_id": "ALL",
                             "reason": reason, "price": option_ltp})

# ═══════════════════════════════════════════════════════════════
#  CANDLE BOUNDARY
# ═══════════════════════════════════════════════════════════════

def _is_new_1min_candle(now: datetime) -> bool:
    key = now.strftime("%Y%m%d%H%M")
    with _state_lock:
        if state.get("_last_1min_candle") != key and 31 <= now.second <= 36:
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

        # ── Build CE/PE signal blocks ──
        def _build_signal(opt_type, result):
            if not result:
                return {
                    "ema9": 0, "ema21": 0, "ema_gap": 0, "ema_ok": False,
                    "rsi": 0, "rsi_prev": 0, "rsi_ok": False,
                    "candle_green": False, "gap_widening": False,
                    "fired": False, "verdict": "NO DATA",
                    "ltp": 0,
                    "strike": dir_strikes.get(opt_type, atm_strike),
                }

            # v13.0: Simple verdict from EMA gap + RSI
            ema_gap = result.get("ema_gap", 0)
            rsi_val = result.get("rsi", 0)
            ema_ok = result.get("ema_ok", False)
            rsi_ok = result.get("rsi_ok", False)

            if result.get("fired"):
                _em = result.get("entry_mode", "EMA")
                verdict = "FIRED [" + _em + "]"
            elif not ema_ok and not rsi_ok:
                verdict = "EMA " + str(ema_gap) + " RSI " + str(rsi_val)
            elif not ema_ok:
                verdict = "EMA " + str(ema_gap) + " (need 3+)"
            elif not rsi_ok:
                if rsi_val < 50:
                    verdict = "RSI " + str(rsi_val) + " (need 50+)"
                else:
                    verdict = "RSI " + str(rsi_val) + " not rising"
            else:
                verdict = "READY"

            return {
                "ema9": result.get("ema9", 0),
                "ema21": result.get("ema21", 0),
                "ema_gap": round(ema_gap, 1),
                "ema_ok": ema_ok,
                "candle_green": result.get("candle_green", False),
                "gap_widening": result.get("gap_widening", False),
                "rsi": round(rsi_val, 1),
                "rsi_prev": result.get("rsi_prev", 0),
                "rsi_ok": rsi_ok,
                "fired": result.get("fired", False),
                "verdict": verdict,
                "ltp": round(result.get("entry_price", 0), 2),
                "strike": result.get("_strike", dir_strikes.get(opt_type, atm_strike)),
                "bonus": result.get("bonus", {}),
                "entry_mode": result.get("entry_mode", ""),
                "momentum_pts": result.get("momentum_pts", 0),
                "path_a": result.get("path_a", False),
                "path_b": result.get("path_b", False),
                "spike_ratio": result.get("spike_ratio", 0),
                "momentum_threshold": result.get("momentum_threshold", 15),
            }

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

            # LOT 1 — independent
            if st.get("lot1_active"):
                lot1 = {"status": "active", "pnl": running,
                         "sl": round(st.get("current_floor", 0), 2), "sl_type": "FLOOR"}
            else:
                lot1 = {"status": "exited", "pnl": round(st.get("lot1_exit_pnl", 0), 2),
                         "exit_price": st.get("lot1_exit_price", 0),
                         "exit_reason": st.get("lot1_exit_reason", ""),
                         "exit_time": st.get("lot1_exit_time", ""), "sl": 0, "sl_type": ""}

            # LOT 2 — independent
            if st.get("lot2_active"):
                _l2_split = st.get("lots_split", False)
                _l2_sl = st.get("lot2_trail_sl", 0) if _l2_split else st.get("current_floor", 0)
                lot2 = {"status": "riding" if _l2_split else "active", "pnl": running,
                         "sl": round(_l2_sl, 2), "sl_type": "ATR" if _l2_split else "FLOOR"}
            else:
                lot2 = {"status": "exited", "pnl": round(st.get("lot2_exit_pnl", 0), 2),
                         "exit_price": st.get("lot2_exit_price", 0),
                         "exit_reason": st.get("lot2_exit_reason", ""), "sl": 0, "sl_type": ""}

            position = {
                "in_trade": True,
                "symbol": st.get("symbol", ""),
                "direction": st.get("direction", ""),
                "entry": entry,
                "ltp": round(opt_ltp, 2) if opt_ltp > 0 else 0,
                "pnl": running,
                "peak": round(st.get("peak_pnl", 0), 1),
                "candles": st.get("candles_held", 0),
                "lots_split": st.get("lots_split", False),
                "current_rsi": round(st.get("current_rsi", 0), 1),
                "current_floor": round(st.get("current_floor", 0), 2),
                "current_giveback": st.get("current_giveback", 8),
                "strike": st.get("strike", 0),
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

        # ── Full snapshot ──
        dashboard = {
            "ts": now.strftime("%Y-%m-%d %H:%M:%S"),
            "version": D.VERSION,
            "mode": "PAPER" if D.PAPER_MODE else "LIVE",
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
                "indicators_warm": now.hour >= 10 or (now.hour == 9 and now.minute >= 45 and dte >= 2),
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
            profile = D.get_dte_profile(dte)
            session = D.get_session_block(now.hour, now.minute)
            spot_ltp = D.get_ltp(D.NIFTY_SPOT_TOKEN)

            # v13.1: Auto-heal stale WebSocket (re-auth + reconnect)
            D.check_and_reconnect()

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

            with _state_lock:
                _eod_done = state.get("_eod_reported")
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
                        logger.warning("[MAIN] prev_close NOT saved — both WS and REST returned 0")
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

            with _state_lock:
                _force = state.get("force_exit")
                _in_trade = state.get("in_trade")
                _token = state.get("token")
                _symbol = state.get("symbol", "")
                _entry_px = state.get("entry_price", 0)
            if _force and _in_trade:
                option_ltp = D.get_ltp(_token)
                _execute_exit(kite, option_ltp or _entry_px, "FORCE_EXIT")
                time.sleep(1)
                continue

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

                    # v13.0: manage_exit returns list of exit dicts
                    exit_list = manage_exit(state, option_ltp, profile)

                    # ── TRAILING FLOOR MILESTONES + EXCHANGE SL UPDATE ──
                    if state.get("in_trade"):
                        _peak = state.get("peak_pnl", 0)
                        _floor = state.get("current_floor", 0)
                        _last_ms = state.get("_last_milestone", 0)
                        _entry_px = state.get("entry_price", 0)
                        for _m in [10, 15, 20, 25, 30, 40, 50]:
                            if _peak >= _m and _last_ms < _m:
                                # Use same giveback logic as manage_exit
                                _gb = 6 if _m >= 30 else 7 if _m >= 20 else 8
                                _locked = _m - _gb
                                state["_last_milestone"] = _m
                                _tg_send("🟢 +" + str(_m) + "pts — SL → ₹"
                                         + str(round(_entry_px + _locked, 1))
                                         + " 🔒 (+" + str(_locked) + " locked, "
                                         + str(_gb) + "pt trail)")
                                break
                        # Update exchange SL when floor locks higher
                        _old_trigger = state.get("_sl_trigger_at_exchange", 0)
                        if _floor > _old_trigger and state.get("_sl_order_id"):
                            try:
                                from VRL_TRADE import modify_sl_order
                                if modify_sl_order(kite, state["_sl_order_id"], _floor):
                                    state["_sl_trigger_at_exchange"] = round(_floor, 1)
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
                                    _sr = check_entry(_trade_token, _dt, spot_ltp, dte, expiry, kite)
                                    _sr["_strike"] = _trade_strike
                                else:
                                    _oi = _locked_tokens.get(_dt) if _locked_tokens else None
                                    if _oi:
                                        _sr = check_entry(_oi["token"], _dt, spot_ltp, dte, expiry, kite)
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
                _relock = False
                if _locked_at_spot is None:
                    _relock = True
                elif abs(spot_ltp - _locked_at_spot) > _LOCK_SHIFT_THRESHOLD:
                    _relock = True
                    _spot_move = round(spot_ltp - _locked_at_spot, 1)
                    _old_ce = _locked_ce_strike
                    _old_pe = _locked_pe_strike
                    logger.info("[MAIN] Spot moved " + str(round(abs(_spot_move), 1))
                                + "pts from lock — RELOCKING")

                if _relock:
                    _lock_strikes(spot_ltp, dte, kite, expiry)
                    # Telegram alert on relock (not initial lock)
                    if '_spot_move' in dir():
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
                # ── MINIMAL SCAN — EMA gap + RSI only ─────
                all_results = {}
                best_result = None
                best_type = None
                best_opt_info = None

                if not D.is_tick_live(D.INDIA_VIX_TOKEN):
                    D.subscribe_tokens([D.INDIA_VIX_TOKEN])

                for opt_type in ("CE", "PE"):
                    opt_info = dir_tokens.get(opt_type)
                    if not opt_info:
                        continue

                    result = check_entry(
                        token=opt_info["token"],
                        option_type=opt_type,
                        spot_ltp=spot_ltp,
                        dte=dte,
                        expiry_date=expiry,
                        kite=kite,
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

    kite = get_kite()
    _kite = kite
    D.init(kite)

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

    # Run daily lab data cleanup
    try:
        D.cleanup_old_lab_data()
    except Exception as e:
        logger.warning("[MAIN] Lab cleanup failed: " + str(e))

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
                        except: pass
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

    D.start_websocket()
    D.subscribe_tokens([D.NIFTY_SPOT_TOKEN, D.INDIA_VIX_TOKEN])
    time.sleep(2)

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
