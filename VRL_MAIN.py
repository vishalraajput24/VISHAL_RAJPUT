# ═══════════════════════════════════════════════════════════════
#  VRL_MAIN.py — VISHAL RAJPUT TRADE v16.6
#  Master orchestration. EMA9 Band Breakout strategy.
#  Entry: close > EMA9-low (fresh) + green + body ≥ 40%
#  Exit: 3-rule chain — Emergency -10, EOD 15:20, Vishal Trail (60/85/80/LOCK+40 tiers).
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
from VRL_CONFIG import get_kite
from VRL_ENGINE import (
    check_entry, manage_exit, pre_entry_checks,
    compute_entry_sl,
    get_option_ema_spread,
)
# VRL_TRADE handles both paper and live mode
import VRL_CONFIG as CFG
try:
    from kiteconnect.exceptions import (
        TokenException, NetworkException, GeneralException,
        OrderException, InputException,
    )
except ImportError:
    TokenException = NetworkException = GeneralException = Exception
    OrderException = InputException = Exception

from VRL_LAB    import start_lab
import VRL_ENGINE as CHARGES

# ── Loggers ─────────────────────────────────────────────────────
logger     = setup_logger("vrl_live", D.LIVE_LOG_FILE)
lab_logger = setup_logger("vrl_lab",  D.LAB_LOG_FILE)
# Wall-clock at module import — used by /pulse to report bot uptime.
_BOT_START_TS = time.time()

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
    # ── Exit state ────────────────────────────────────────
    "peak_pnl"           : 0.0,
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
    "_last_cleanup_date" : "",
    # v16.0 ratchet state
    "active_ratchet_tier": "",
    "active_ratchet_sl"  : 0.0,
    "other_token"        : 0,
    # ── Last exit memory (cooldown) ────────────────────────
    "last_exit_time"     : "",
    "last_exit_direction": "",
    "last_exit_peak"     : 0.0,
    "last_exit_reason"   : "",
    # ── Daily counters ─────────────────────────────────────
    "daily_pnl"          : 0.0,
    # ── Bot control ────────────────────────────────────────
    "paused"             : False,
    # ── Daily reset flags ──────────────────────────────────
    "_eod_reported"      : False,
    "_eod_exited"        : False,
    "_bias_done"         : False,
    "_straddle_done"     : False,
    "_hourly_rsi_ts"     : 0,
    "_straddle_alerted"  : False,
    # ── Loop bookkeeping ───────────────────────────────────
    "_last_1min_candle"  : "",
    "_last_dash_scan_min": "",
    "_last_warmup_log"   : "",
    "_last_scan"         : {},
    "prev_close"         : 0.0,
    # ── Exchange order tracking (live mode — legacy compat) ──
    "_sl_order_id"       : "",
    "_sl_trigger_at_exchange": 0,
    "lot1_active"        : True,  # legacy (always True in v15.0)
    "lot2_active"        : True,  # legacy (always True in v15.0)
    "lots_split"         : False, # legacy (always False in v15.0)
    "current_floor"      : 0.0,   # legacy (used for dashboard trail display)
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
    """Lock CE/PE strikes (ATM + OTM) and subscribe 4 tokens."""
    global _locked_ce_strike, _locked_pe_strike, _locked_at_spot, _locked_tokens
    _locked_ce_strike = D.resolve_strike_for_direction(spot, "CE", dte)
    _locked_pe_strike = D.resolve_strike_for_direction(spot, "PE", dte)
    _locked_at_spot = spot
    _locked_tokens = {}

    if kite and expiry:
        # v16.3.2: subscribe ATM + OTM for each side (4 tokens total)
        # CE: ATM + ATM+50 (OTM call)
        # PE: ATM + ATM-50 (OTM put)
        _ce_otm_strike = _locked_ce_strike + 50
        _pe_otm_strike = _locked_pe_strike - 50

        for _dt, _strike in [("CE", _locked_ce_strike), ("PE", _locked_pe_strike)]:
            _tk = D.get_option_tokens(kite, _strike, expiry)
            if _tk.get(_dt):
                _locked_tokens[_dt] = _tk[_dt]

        # OTM tokens
        _ce_otm_tk = D.get_option_tokens(kite, _ce_otm_strike, expiry)
        if _ce_otm_tk.get("CE"):
            _locked_tokens["CE_OTM"] = _ce_otm_tk["CE"]
        _pe_otm_tk = D.get_option_tokens(kite, _pe_otm_strike, expiry)
        if _pe_otm_tk.get("PE"):
            _locked_tokens["PE_OTM"] = _pe_otm_tk["PE"]

        _sub_tokens = [v["token"] for v in _locked_tokens.values() if v.get("token")]
        if _sub_tokens:
            D.subscribe_tokens(_sub_tokens)

    logger.info("[MAIN] Strikes LOCKED: CE=" + str(_locked_ce_strike)
                + "+" + str(_locked_ce_strike + 50)
                + " PE=" + str(_locked_pe_strike)
                + "+" + str(_locked_pe_strike - 50)
                + " at spot=" + str(round(spot, 1)))
    if kite and expiry and _locked_ce_strike:
        try:
            _r11 = D.ensure_option_history(
                kite, _locked_ce_strike, expiry,
                min_candles=30, timeframes=("3minute",))
            if _r11.get("fetched"):
                logger.info("[PRELOAD] Strike lock " + str(_locked_ce_strike)
                            + " CE=" + str(_r11["ce_candles"])
                            + " PE=" + str(_r11["pe_candles"]))
        except Exception as _r11e:
            logger.debug("[PRELOAD] strike lock error: " + str(_r11e))

def _reset_strike_lock():
    """Reset lock after trade exit or session start.
    Unsubscribes every currently-locked token first so the WebSocket
    doesn't leak stale CE/PE/OTM subscriptions across relocks. Without
    this, a full trading day of ~20 relocks leaves 60+ dead tokens
    pinned against the Kite quota."""
    global _locked_ce_strike, _locked_pe_strike, _locked_at_spot, _locked_tokens
    try:
        _old = [v.get("token") for v in (_locked_tokens or {}).values()
                if isinstance(v, dict) and v.get("token")]
        if _old:
            D.unsubscribe_tokens(_old)
    except Exception as _ue:
        logger.debug("[MAIN] reset_strike_lock unsubscribe: " + str(_ue))
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
                "Resuming exit monitoring."
            )
            # Refresh band context immediately so the dashboard doesn't
            # show zeroed current_ema9_high/low until the next manage_exit
            # tick. Best-effort — if the fetch fails we keep the persisted
            # values and the next 3-min candle will overwrite them.
            try:
                _rt_tok = state.get("token")
                if _rt_tok:
                    _rt_df = D.get_option_3min(_rt_tok, lookback=10)
                    if _rt_df is not None and len(_rt_df) >= 2:
                        _rt_last = _rt_df.iloc[-2]
                        with _state_lock:
                            state["current_ema9_high"] = round(
                                float(_rt_last.get("ema9_high", 0)), 2)
                            state["current_ema9_low"] = round(
                                float(_rt_last.get("ema9_low", 0)), 2)
                            state["last_band_check_ts"] = datetime.now().isoformat()
            except Exception as _rte:
                logger.debug("[MAIN] restart band refresh: " + str(_rte))
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
        state["daily_pnl"]             = 0.0
        state["_eod_reported"]         = False
        state["_eod_exited"]           = False
        state["aggressive_mode"]       = False
        state["paused"]                = False
        state["_bias_done"]            = False
        state["_straddle_done"]        = False
        state["_hourly_rsi_ts"]        = 0
        state["_straddle_alerted"]     = False
        # Clear persisted scan dedup key so a crash-restart landing at
        # 09:30:45 (after the loop already scanned 09:30) doesn't treat
        # the current minute as already-scanned and silently skip the
        # first entry of the session.
        state["_last_scan_key"]        = ""
    D.clear_token_cache()
    D.reset_daily_warnings()
    _reset_strike_lock()
    logger.info("[MAIN] _eod_exited reset for new day")
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
    # Fetch 5 days of 3-min + 1-min candles for ATM±100 so GARCH is warm.
    try:
        from datetime import date as _dr10
        if state.get("_preload_done_today") != _dr10.today().isoformat():
            _spot_close = float(state.get("prev_close", 0) or 0)
            if _spot_close <= 0:
                _spot_close = D.get_ltp(D.NIFTY_SPOT_TOKEN)
            if _spot_close > 0:
                _atm_prov = D.resolve_atm_strike(_spot_close)
                _expiry_prov = D.get_nearest_expiry(_kite)
                if _atm_prov and _expiry_prov:
                    _r10_strikes = [_atm_prov + _off
                                    for _off in (-100, -50, 0, 50, 100)]
                    _r10_total = 0
                    for _r10_sk in _r10_strikes:
                        _r10_res = D.ensure_option_history(
                            _kite, _r10_sk, _expiry_prov,
                            min_candles=30, timeframes=("3minute", "minute"))
                        if _r10_res.get("fetched"):
                            _r10_total += (_r10_res.get("ce_candles", 0)
                                           + _r10_res.get("pe_candles", 0))
                    logger.info("[PRELOAD] Market-open 5-strike window ATM="
                                + str(_atm_prov) + " total_candles="
                                + str(_r10_total))
                    with _state_lock:
                        state["_preload_done_today"] = _dr10.today().isoformat()
                    _save_state()
    except Exception as _r10e:
        logger.warning("[PRELOAD] market-open error: " + str(_r10e))

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

# v15.2.5 live columns only (matches _TRADE_FIELDS in VRL_DB.py).
# Dead v13 fields previously tracked here — removed via schema migrations.
TRADE_FIELDNAMES = [
    "date", "entry_time", "exit_time", "symbol", "direction", "strike",
    "entry_price", "exit_price", "pnl_pts", "pnl_rs",
    "gross_pnl_rs", "net_pnl_rs",
    "peak_pnl", "exit_reason",
    "dte", "candles_held", "session", "sl_pts",
    "vix_at_entry", "entry_mode",
    "brokerage", "stt", "exchange_charges", "gst", "stamp_duty",
    "total_charges", "num_exit_orders", "qty_exited",
    "entry_slippage", "exit_slippage", "lot_id",
    "entry_ema9_high", "entry_ema9_low",
    "exit_ema9_high", "exit_ema9_low",
    "entry_band_position", "exit_band_position",
    "entry_body_pct",
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

    # v15.2.5 live columns only. Dead v13 fields purged.
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
        "exit_reason"   : exit_reason,
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
        # v15.2.5 Fix 5: straddle classification captured at entry
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
    """Escape <, >, & in dynamic content for Telegram HTML mode.
    Apply only to user/API-supplied strings, NOT to template literals."""
    if s is None:
        return ""
    try:
        return (str(s).replace("&", "&amp;")
                     .replace("<", "&lt;")
                     .replace(">", "&gt;"))
    except Exception:
        return ""


def _tg_send(text: str, parse_mode: str = "HTML", chat_id: str = None,
             priority: str = "normal") -> bool:
    """Non-blocking Telegram send with flood control.

    Runs the POST in a daemon thread so the strategy loop never waits.
    `priority="critical"` bypasses flood control so exit-failure / DB-
    corruption / shutdown-with-open-trade alerts always deliver even
    during a 5-in-10s burst. Critical sends still append to the
    sliding window so bookkeeping stays accurate.
    """
    def _worker():
        if not D.TELEGRAM_TOKEN or not (chat_id or D.TELEGRAM_CHAT_ID):
            return
        is_critical = (str(priority).lower() == "critical")
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
        # Sanitize unknown HTML tags in HTML mode; Telegram only allows
        # <b>, <i>, <u>, <s>, <code>, <pre>, <a href>.
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
        except Exception as e:
            logger.error("[TG] send error: " + type(e).__name__)

    threading.Thread(target=_worker, daemon=True).start()
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
        _acct_line = ("Account : " + _acct["name"] + "\n"
                      "Balance : Rs" + "{:,}".format(int(_acct.get("total_balance", 0))) + "\n")
    _tg_send(
        "<b>VISHAL RAJPUT TRADE " + D.VERSION + "</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "Time    : " + _now_str() + "\n"
        "Mode    : " + _mode_tag() + "\n"
        + _acct_line +
        "Web     : " + _web_url + "\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "<b>STRATEGY</b>  EMA9 Band Breakout v16.6 (Vishal Golden 4)\n"
        "Entry   : 09:35 - 15:10 IST  |  5-min same-direction cooldown\n"
        "Gates   : 4 — time window, close>ema9_low + band>7,\n"
        "          ema9_low slope flat/rising, body>=40%\n"
        "Size    : 2 lots fixed\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "<b>EXITS</b>  (first match wins)\n"
        "1. Emergency -10pts\n"
        "2. EOD 15:20\n"
        "3. Vishal Trail (see tiers)\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "<b>SMART TRAIL v2+</b>\n"
        "peak <5    SL = entry-10          (INITIAL)\n"
        "peak 5-8   SL = entry             (BREAKEVEN)\n"
        "peak 8-15  SL = entry+peak*0.60   (TRAIL_60)\n"
        "peak 15-30 SL = entry+peak*0.75   (TRAIL_75)\n"
        "peak 30-45 SL = entry+peak*0.85   (VISHAL_MAX)\n"
        "peak 45+   SL = entry+peak*0.90   (TRAIL_90)\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
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
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "Every order uses REAL money.\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
        )

def _alert_exit_critical(symbol: str, qty: int, reason: str = ""):
    """v15.2.5 richer CRITICAL alert — names the blocked trade,
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
        priority="critical",   # bypass flood control
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
        _tg_send(
            "<b>EOD REPORT " + today + "</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "No trades today.\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━"
        )
        return

    total_pts  = sum(float(t.get("pnl_pts", 0)) for t in trades)
    total_rs   = sum(float(t.get("gross_pnl_rs", t.get("pnl_rs", 0))) for t in trades)
    wins       = [t for t in trades if float(t.get("pnl_pts", 0)) > 0]
    losses     = [t for t in trades if float(t.get("pnl_pts", 0)) <= 0]
    n_trades   = len(trades)
    win_rate   = round(len(wins) / n_trades * 100, 0) if n_trades > 0 else 0
    best       = max((float(t.get("pnl_pts", 0)) for t in trades), default=0)
    worst      = min((float(t.get("pnl_pts", 0)) for t in trades), default=0)

    sign = "+" if total_pts >= 0 else ""

    trade_lines = ""
    for i, t in enumerate(trades[:5], 1):
        _pts    = float(t.get("pnl_pts", 0))
        _side   = t.get("direction", "")
        _strike = t.get("strike", 0)
        _reason = t.get("exit_reason", "")
        trade_lines += (
            str(i) + ". " + _side + " " + str(_strike) + "  "
            + "{:+.1f}".format(_pts) + "pts  " + _reason + "\n"
        )
    if len(trades) > 5:
        trade_lines += "+" + str(len(trades) - 5) + " more\n"

    _tg_send(
        "<b>EOD REPORT " + today + "</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        + ("🟢" if total_pts >= 0 else "🔴")
        + " <b>" + sign + "{:.1f}".format(total_pts) + " pts   "
        + ("+" if total_rs >= 0 else "-") + "Rs" + "{:,}".format(abs(int(total_rs)))
        + "</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "Trades   " + str(n_trades) + "   (" + str(len(wins)) + "W " + str(len(losses)) + "L)\n"
        "Win rate " + str(int(win_rate)) + "%\n"
        "Best     " + "{:+.1f}".format(best) + " pts\n"
        "Worst    " + "{:+.1f}".format(worst) + " pts\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        + trade_lines +
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━"
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
    # Read the same config key that the engine's emergency-SL check uses
    # (exit.ema9_band.emergency_sl_pts = -10) so the log line and the real
    # trigger stay in sync. Previously this fell back to a stale default of
    # 12, printing "SL=entry-12" while the engine exited at entry-10.
    hard_sl = abs(CFG.exit_ema9_band("emergency_sl_pts", -10))
    phase1_sl = compute_entry_sl(actual_price, hard_sl)

    # Extract the OTHER side token for manage_exit divergence check.
    # _locked_tokens is the module-global populated by _lock_strikes();
    # the previous _ce_info_v15/_pe_info_v15 names were locals of the
    # strategy loop and never reachable from this callee.
    _other_token_entry = 0
    try:
        _ce_locked = (_locked_tokens or {}).get("CE") or {}
        _pe_locked = (_locked_tokens or {}).get("PE") or {}
        if option_type == "CE" and _pe_locked:
            _other_token_entry = int(_pe_locked.get("token", 0) or 0)
        elif option_type == "PE" and _ce_locked:
            _other_token_entry = int(_ce_locked.get("token", 0) or 0)
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
        # of ATM drift. Resolve both sides so the opposite-side candles
        # are also persisted for hedge research.
        try:
            _trade_strike = state["strike"]
            _trade_dir    = option_type
            _tce = int((_ce_locked or {}).get("token", 0) or 0)
            _tpe = int((_pe_locked or {}).get("token", 0) or 0)
            # If the locked ATM tokens don't match the traded strike
            # (OTM candidate won), resolve fresh tokens for the exact
            # traded strike so hedge research has both legs of the
            # actual position.
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
        state["_static_floor_sl"]   = 0
        state["current_floor"]      = phase1_sl
        state["peak_pnl"]           = 0.0
        state["candles_held"]       = 0
        state["_candle_low"]        = actual_price
        # Fresh trade starts without
        # the normal per-candle append path populates it cleanly. Reset
        # the one-shot backfill sentinel so the NEXT restart-with-trade
        # (if this position is still open) gets its own seed.
        state["_last_milestone"]    = 0
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
        # v15.2.5 Fix 5: STRONG / NEUTRAL / WEAK / NA straddle classification
        # v16.0 Batch 7: band slope + context tag (display only)
        state["backbone_status"]          = entry_result.get("backbone_status", "N/A")

    _save_state()

    # ── v16.3.2 Entry alert ──
    _close = round(float(entry_result.get("close", actual_price)), 1)
    _ema9l = round(float(entry_result.get("ema9_low", 0)), 1)
    _body  = int(round(float(entry_result.get("body_pct", 0)), 0))
    _strike_label = entry_result.get("_strike_label", "ATM")
    _entry_score = entry_result.get("_entry_score", 0)

    _dir_emoji = "🟢" if option_type == "CE" else "🔴"
    _sym = _short_sym(symbol, option_type, entry_result.get("_strike", state.get("strike", 0)))
    _tm = datetime.now().strftime("%H:%M:%S")

    _slope = float(entry_result.get("ema9_low_slope", 0) or 0)
    _slope_tag = "+" if _slope >= 0 else ""
    _core = (
        "Entry   Rs" + "{:.2f}".format(actual_price) + "   @ " + _tm + "\n"
        "Close   " + "{:.1f}".format(_close) + "  &gt;  EMA9L " + "{:.1f}".format(_ema9l) + "\n"
        "Band    " + "{:.1f}".format(float(entry_result.get("band_width", 0))) + " pts\n"
        "Slope   " + _slope_tag + "{:.1f}".format(_slope) + " pts (3 bars)\n"
        "Body    " + str(_body) + "%\n"
    )

    # Read the emergency SL from the same config key the engine uses
    # (exit.ema9_band.emergency_sl_pts = -10) so the alert and the actual
    # trigger stay in sync if the operator changes the config.
    _sl_pts = abs(CFG.exit_ema9_band("emergency_sl_pts", -10))
    _initial_sl = round(actual_price - _sl_pts, 1)
    _stop_block = (
        "<b>STOP</b>\n"
        "Hard SL   -" + str(_sl_pts) + " pts (Rs"
        + "{:.1f}".format(_initial_sl) + ")\n"
        "Breakeven at +5, trail arms at +8 (Smart v2+)\n"
    )

    # Backbone display removed — check_entry() never passes other-side
    # candle data to the engine, so backbone_status was permanently "N/A"
    # and the entry alert carried a dead line. State key is still written
    # above for back-compat with the dashboard field.
    _backbone_block = ""

    _ctx_lines = []
    _ehs = int(float(entry_result.get("ema9_high_slope_5c", 0) or 0))
    _els = int(float(entry_result.get("ema9_low_slope_5c", 0) or 0))
    _bstate = entry_result.get("bands_state", "")
    if _bstate == "RISING":
        _ctx_lines.append("Bands     +" + str(_ehs) + " / +" + str(_els) + "  RISING OK")
    elif _bstate == "FLAT":
        _ctx_lines.append("Bands     +" + str(_ehs) + " / +" + str(_els) + "  FLAT WARN")
    elif _bstate:
        _ctx_lines.append("Bands     +" + str(_ehs) + " / +" + str(_els) + "  " + _bstate)

    _sinfo = entry_result.get("straddle_info", "") or ""
    _sd    = entry_result.get("straddle_delta")
    _savail = entry_result.get("straddle_available", True)
    if _savail and _sinfo and _sinfo != "NA" and _sd is not None:
        _ctx_lines.append("Straddle  \u0394" + "{:+.1f}".format(float(_sd)) + "  " + _sinfo)

    _ctag = entry_result.get("context_tag", "NORMAL")
    if _ctag == "TRIPLE_CONFLUENCE":
        _ctx_header = "<b>CONTEXT  \u2713 TRIPLE CONFLUENCE</b>\n"
    elif _ctag == "MIXED_SIGNALS":
        _ctx_header = "<b>CONTEXT  \u26A0 MIXED SIGNALS</b>\n"
    else:
        _ctx_header = "<b>CONTEXT</b>\n"

    _ctx_block = _ctx_header + "\n".join(_ctx_lines) + "\n"

    _slip_block = ""
    if _entry_slippage and abs(float(_entry_slippage)) > 0.05:
        _slip_block = "Slippage: " + "{:+.2f}".format(float(_entry_slippage)) + " pts\n"

    _tg_send(
        _dir_emoji + " <b>" + _sym + " " + _strike_label + " x "
        + str(lot_count) + " LOTS</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        + _core +
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        + _stop_block +
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        + _backbone_block
        + _slip_block +
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━"
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
        from VRL_DB import validate_entry
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
        # Snapshot the active trail tier BEFORE state.update() wipes it below,
        # so the exit alert reports the real tier (BREAKEVEN/TRAIL_60/TRAIL_75/VISHAL_MAX/TRAIL_90)
        # instead of always falling back to INITIAL.
        _tier_snapshot = state.get("active_ratchet_tier", "") or "INITIAL"
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

    fill = place_exit(kite, symbol, token, direction,
                      exit_qty, exit_price, reason)

    if not fill["ok"] and fill.get("error") == "EXIT_FAILED_MANUAL_REQUIRED":
        with _state_lock:
            state["_exit_failed"] = True
        _save_state()   # v15.2.5 persist the block across crashes
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
    # Previous bug: used pnl * (qty/lot_size) which doubled the value for 2-lot exits.
    pnl_lots = pnl  # points per trade — one value per closed trade, matches trade log

    # Log EVERY lot exit (not just trade_done)
    _log_trade(state, actual_exit, reason, candles, saved_entry=entry,
               lot_id=lot_id, qty=exit_qty)

    # Telegram alert
    if trade_done:
        with _state_lock:
            state["daily_pnl"] = round(state.get("daily_pnl", 0) + pnl_lots, 2)
            state["last_exit_time"] = datetime.now().isoformat()
            state["last_exit_direction"] = direction
            state["last_exit_peak"] = peak
            state["last_exit_reason"] = reason
            state["last_exit_price"] = round(actual_exit, 2)
            old_token = state["token"]
            try:
                D.clear_active_trade()
            except Exception:
                pass
            state.update({
                "in_trade": False, "symbol": "", "token": None,
                "direction": "", "strike": 0,
                "entry_price": 0.0, "entry_time": "",
                "_static_floor_sl": 0.0, "current_floor": 0.0,
                "peak_pnl": 0.0,
                "candles_held": 0, "force_exit": False, "_exit_failed": False,
                # Trail state — clear so next trade starts at INITIAL
                "active_ratchet_tier": "", "active_ratchet_sl": 0.0,
                "_last_milestone": 0,
                # Entry context (v15.0 band + body)
                "entry_mode": "",
                "entry_ema9_high": 0.0, "entry_ema9_low": 0.0,
                "entry_band_position": "", "entry_body_pct": 0.0,
                "current_ema9_high": 0.0, "current_ema9_low": 0.0,
                "last_band_check_ts": "",
                "other_token": 0,
                # Exchange SL-M tracking (live mode)
                "_sl_order_id": "", "_sl_trigger_at_exchange": 0,
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
        # v16.0 Batch 7 exit alert — reason-specific context line
        _dir_emoji = "🟢" if direction == "CE" else "🔴"
        _sym_exit  = _short_sym(symbol, direction, _exit_strike)
        _sign_pnl  = "+" if pnl >= 0 else ""
        _net_sign  = "+" if _ch["net_pnl"] >= 0 else "-"

        _reason_line = ""
        _tier = _tier_snapshot
        if reason == "VISHAL_TRAIL":
            _reason_line = "Trail " + _tier + " triggered\n"
        # capture percentage (v16.2 display)
        _capture_line = ""
        try:
            _peak_f = float(peak) if peak else 0
            if _peak_f > 0:
                _cap = int(round(pnl / _peak_f * 100))
                _capture_line = "Capture " + str(_cap) + "%\n"
        except Exception:
            pass

        _tg_send(
            _dir_emoji + " <b>EXIT " + _sym_exit + "</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "<b>" + reason + "</b>    " + _sign_pnl + "{:.1f}".format(pnl) + " pts\n"
            + _reason_line +
            "Entry   Rs" + "{:.1f}".format(entry) + "\n"
            "Exit    Rs" + "{:.1f}".format(actual_exit) + "\n"
            "Peak    +" + "{:.1f}".format(peak) + " pts\n"
            + _capture_line +
            "Hold    " + str(candles) + " min\n"
            "Trail   " + _tier + "\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "Gross   " + ("+" if _ch["gross_pnl"] >= 0 else "-")
            + "Rs" + "{:,}".format(abs(int(_ch["gross_pnl"]))) + "\n"
            "Charges -Rs" + "{:,}".format(int(_ch["total_charges"])) + "\n"
            "<b>Net     " + _net_sign + "Rs" + "{:,}".format(abs(int(_ch["net_pnl"]))) + "</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "DAY " + "{:+.1f}".format(_day_pnl) + " pts"
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
            from VRL_DB import validate_exit
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


# ═══════════════════════════════════════════════════════════════
#  CANDLE BOUNDARY
# ═══════════════════════════════════════════════════════════════

def _is_new_1min_candle(now: datetime) -> bool:
    key = now.strftime("%Y%m%d%H%M")
    with _state_lock:
        if state.get("_last_1min_candle") != key and now.second >= 35:
            state["_last_1min_candle"] = key
            return True
    return False


# ═══════════════════════════════════════════════════════════════
#  STRATEGY LOOP
# ═══════════════════════════════════════════════════════════════


# ═══════════════════════════════════════════════════════════════
#  DASHBOARD SNAPSHOT — written every cycle for VRL_WEB.py
#  VRL_WEB.py reads this file. Zero calculation in web server.
# ═══════════════════════════════════════════════════════════════

# ═══════════════════════════════════════════════════════════════
#  Shadow CSV logger — REMOVED in v16.0 Batch 7.
#  Historical shadow CSVs remain on disk in ~/lab_data/shadow_exits/.
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
            elif _pos == "ABOVE" and _green and _body >= 40:
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
                # v16.6 Golden 4 — slope of EMA9_low over last N candles
                "ema9_low_slope": round(float(result.get("ema9_low_slope", 0) or 0), 2),
                # v15.2 straddle filter context
                "straddle_delta":     result.get("straddle_delta"),
                "straddle_threshold": result.get("straddle_threshold", 0),
                "straddle_period":    result.get("straddle_period", ""),
                "atm_strike_used":    result.get("atm_strike_used", 0),
                # Legacy compat
                "rsi": 0, "ema9": round(_eh, 2), "ema21": 0,
            }

        # Warmup metadata is still consumed by the dashboard header
        # (market-context block). During warmup the per-side blocks now
        # fall through to _build_signal's "NO DATA" fallback.
        _is_warm, _w_done, _w_need, _w_eta = _warmup_info(now, dte)
        ce_signal = _build_signal("CE", all_results.get("CE"))
        pe_signal = _build_signal("PE", all_results.get("PE"))

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
                "candles": st.get("candles_held", 0),
                "strike": st.get("strike", 0),
                "entry_mode": st.get("entry_mode", ""),
                # v16.0 band context (display only)
                "current_ema9_high": round(st.get("current_ema9_high", 0), 2),
                "current_ema9_low":  round(st.get("current_ema9_low", 0), 2),
                "stop": _stop_price,
                "stop_dist": round(opt_ltp - _stop_price, 2)
                              if opt_ltp > 0 and _stop_price > 0 else 0,
                # v16.2 trail state (state key preserved for back-compat)
                "active_ratchet_tier": st.get("active_ratchet_tier", ""),
                "active_ratchet_sl":   round(float(st.get("active_ratchet_sl", 0) or 0), 2),
                "trail_tier":          st.get("active_ratchet_tier", ""),
                "trail_sl":            round(float(st.get("active_ratchet_sl", 0) or 0), 2),
                "backbone_status":     st.get("backbone_status", "CONFIRMED"),
                # v16.0 Batch 7 context display (bands + straddle + VWAP)
                # v15.2 entry context (replayed at exit on dashboard)
                # v15.2.5 velocity stall telemetry (sparkline + number)
                # Legacy compat
                "lots_split": False,
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
            "paused": st.get("paused", False),
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
                "spot_ema9": spot_3m.get("ema9", 0),
                "spot_ema21": spot_3m.get("ema21", 0),
                "spot_spread": spot_3m.get("spread", 0),
                "spot_rsi": spot_3m.get("rsi", 0),
                "spot_adx_3m": spot_3m.get("adx", 0),
                "hourly_rsi": round(hourly_rsi, 1),
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
                        logger.warning("[MAIN] prev_close NOT saved — both WS and REST returned 0")
                        try:
                            _tg_send(
                                "⚠️ <b>EOD prev_close SAVE FAILED</b>\n"
                                "Both WebSocket and REST ltp() returned 0 at 15:35.\n"
                                "Tomorrow's gap-relock guard will be disabled.\n"
                                "Manual fix option: set state.prev_close via restart"
                                " + /status, or force relock after 9:15 open.",
                                priority="critical",   # bypass flood
                            )
                        except Exception:
                            pass
                _save_state()
                try:
                    _generate_eod_report()
                except Exception as e:
                    logger.error("[MAIN] EOD report error: " + str(e))
            # ── daily lab cleanup at 15:45+ IST ──
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
                _floor_sl = state.get("_static_floor_sl", 0)
                _exit_px = option_ltp if option_ltp > 0 else max(_entry_px, _floor_sl)
                _execute_exit_v13(kite,
                                  {"lots": "ALL", "lot_id": "ALL",
                                   "reason": "FORCE_EXIT", "price": _exit_px},
                                  saved_entry_price=_entry_px)
                time.sleep(1)
                continue
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

                    _mex_other_tok = state.get("other_token", 0)
                    # Snapshot the previous tier AND SL BEFORE manage_exit
                    # mutates state so the upgrade-alert below can show
                    # both "old tier → new tier" and "old SL → new SL".
                    _prev_tier = state.get("active_ratchet_tier", "None") or "None"
                    _prev_sl   = float(state.get("active_ratchet_sl", 0) or 0)
                    exit_list = manage_exit(state, option_ltp, profile, other_token=_mex_other_tok)

                    # v16.2: trail tier upgrade alert
                    try:
                        _new_tier  = state.get("active_ratchet_tier", "None")
                        # Alert only on transitions to an ARMED tier (skip INITIAL).
                        _armed = _new_tier not in ("None", "", "INITIAL")
                        # Suppress the upgrade alert if an exit is firing on
                        # the same tick — the exit alert already carries the
                        # real tier via _tier_snapshot, so sending both is
                        # redundant and confusing.
                        _exit_imminent = bool(exit_list)
                        if (state.get("in_trade") and _new_tier != _prev_tier
                                and _new_tier and _armed and not _exit_imminent):
                            _r_sl   = float(state.get("active_ratchet_sl", 0) or 0)
                            _r_ent  = float(state.get("entry_price", 0) or 0)
                            _r_lock = round(_r_sl - _r_ent, 1)
                            _r_peak = float(state.get("peak_pnl", 0) or 0)
                            _r_ltp  = float(option_ltp or 0)
                            _r_room = round(_r_ltp - _r_sl, 1) if _r_ltp > 0 else 0
                            _r_emoji = "🟢" if state.get("direction") == "CE" else "🔴"
                            _r_sym = _short_sym(state.get("symbol", ""),
                                                 state.get("direction", ""),
                                                 state.get("strike", 0))
                            # Lock icon escalates with tier strength
                            _icon = "🔒"
                            if _new_tier == "BREAKEVEN":
                                _icon = "🛡️"
                            elif _new_tier == "TRAIL_60":
                                _icon = "🔒"
                            elif _new_tier == "TRAIL_75":
                                _icon = "🔒🔒"
                            elif _new_tier == "VISHAL_MAX":
                                _icon = "🔒🔒"
                            elif _new_tier == "TRAIL_90":
                                _icon = "🔒🔒🔒"
                            # Old→New SL line — shows the ratchet jump
                            # clearly so the operator can see "SL moved
                            # from Rs X (tier A) up to Rs Y (tier B)".
                            _sl_old_str = ("Rs" + "{:.1f}".format(_prev_sl)
                                           if _prev_sl > 0 else "entry-10")
                            _tg_send(
                                _icon + " <b>SL UPGRADED → " + _new_tier + "</b>\n"
                                "━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                                + _r_emoji + " " + _r_sym + "   Peak +"
                                + "{:.1f}".format(_r_peak) + "\n"
                                "Prev  " + _prev_tier + "   " + _sl_old_str + "\n"
                                "New   " + _new_tier + "   Rs"
                                + "{:.1f}".format(_r_sl) + "   ⬆️\n"
                                "Lock  +" + "{:.1f}".format(_r_lock) + " pts\n"
                                "Room  " + "{:.1f}".format(_r_room) + " pts"
                            )
                    except Exception as _re:
                        logger.debug("[MAIN] trail tier alert error: " + str(_re))

                    # v16.0 Batch 7: refresh bands_state once per new 3-min candle
                    try:
                        _ctx_tok = state.get("token")
                        if _ctx_tok:
                            _df_ctx = D.get_option_3min(_ctx_tok, lookback=10)
                    except Exception:
                        pass

                    # v15.0: Peak milestone alerts + exchange SL-M band update
                    if state.get("in_trade"):
                        _peak = state.get("peak_pnl", 0)
                        _last_ms = state.get("_last_milestone", 0)
                        _cur_el = round(float(state.get("current_ema9_low", 0)), 1)
                        _entry_px = state.get("entry_price", 0)
                        # Fire milestone alert once per threshold crossed.
                        # Thresholds now align with Smart Trail v2+ tier
                        # boundaries (5/8/15/30/45) plus a few mid steps
                        # so the operator sees peak progress + SL status
                        # at every major point.
                        for _m in [5, 8, 10, 15, 20, 25, 30, 40, 50]:
                            if _peak >= _m and _last_ms < _m:
                                with _state_lock:
                                    state["_last_milestone"] = _m
                                _r_tier = state.get("active_ratchet_tier", "") or "INITIAL"
                                _r_sl   = float(state.get("active_ratchet_sl", 0) or 0)
                                _cur_pnl = round(option_ltp - _entry_px, 1)
                                if _r_sl <= 0:
                                    _r_sl = round(_entry_px - 10, 1)
                                _lock = round(_r_sl - _entry_px, 1)
                                _room = round(option_ltp - _r_sl, 1)
                                # Icon by tier strength
                                _ms_icon = "📈"
                                if _r_tier == "BREAKEVEN":
                                    _ms_icon = "🛡️"
                                elif _r_tier in ("TRAIL_60", "TRAIL_75"):
                                    _ms_icon = "🔒"
                                elif _r_tier in ("VISHAL_MAX", "TRAIL_90"):
                                    _ms_icon = "🔒🔒"
                                _lock_str = (("+" if _lock >= 0 else "")
                                             + "{:.1f}".format(_lock))
                                _tg_send(
                                    _ms_icon + " <b>Peak +" + str(_m)
                                    + " pts</b>   " + _r_tier + "\n"
                                    "━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                                    "Peak  +" + "{:.1f}".format(_peak) + "\n"
                                    "Now   +" + "{:.1f}".format(_cur_pnl) + "\n"
                                    "SL    Rs" + "{:.1f}".format(_r_sl)
                                    + "   (" + _lock_str + " locked)\n"
                                    "Room  " + "{:.1f}".format(_room) + " pts"
                                )
                                break
                    # Live mode: force exit at 15:25 (before broker auto square-off at 15:30)
                    # Paper mode: exit at 15:28 as before
                    _eod_cutoff = 25 if not D.PAPER_MODE else 28
                    if now.hour == 15 and now.minute >= _eod_cutoff:
                        if not D.PAPER_MODE and now.minute < 28:
                            logger.warning("[MAIN] 15:25 SAFETY — forcing exit before broker square-off")
                            _tg_send("⚠️ <b>15:25 SAFETY EXIT</b>\nClosing before broker auto square-off")
                        exit_list = [{"lots": "ALL", "lot_id": "ALL",
                                      "reason": "EOD_EXIT" if not D.PAPER_MODE else "MARKET_CLOSE",
                                      "price": option_ltp}]
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

            if (not state.get("paused")
                    and D.is_trading_window(now)
                    and _is_new_1min_candle(now)
                    and spot_ltp > 0
                    and expiry is not None):

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

                if _relock:
                    _lock_strikes(spot_ltp, dte, kite, expiry)
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

                # v16.3.2: dual-strike evaluation (ATM + OTM per side)
                # CE candidates: ATM CE + ATM+50 CE (OTM)
                # PE candidates: ATM PE + ATM-50 PE (OTM)
                # Score = body_pct × (close - ema9_low). Best wins.
                _ce_info_v15 = _locked_tokens.get("CE") if _locked_tokens else None
                _pe_info_v15 = _locked_tokens.get("PE") if _locked_tokens else None
                _ce_tok_v15 = _ce_info_v15.get("token", 0) if _ce_info_v15 else 0
                _pe_tok_v15 = _pe_info_v15.get("token", 0) if _pe_info_v15 else 0

                _candidates = []
                for opt_type in ("CE", "PE"):
                    _other_tok = _pe_tok_v15 if opt_type == "CE" else _ce_tok_v15
                    _otm_key = opt_type + "_OTM"
                    _atm_info = dir_tokens.get(opt_type)
                    _otm_info = _locked_tokens.get(_otm_key)
                    # get_option_tokens() returns {token, symbol} only — no
                    # strike key — so we track the real strike per label
                    # here. Before this fix, both ATM and OTM candidates
                    # ended up tagged with the ATM strike, making the log
                    # and state["strike"] disagree with the traded symbol.
                    _atm_strike_v = dir_strikes.get(opt_type, atm_strike)
                    _otm_strike_v = (_atm_strike_v + 50) if opt_type == "CE" else (_atm_strike_v - 50)
                    _iter = [("ATM", _atm_info, _atm_strike_v),
                             ("OTM", _otm_info, _otm_strike_v)]
                    for _label, _oi, _strike_val in _iter:
                        if not _oi or not _strike_val:
                            continue
                        # Sanity: if the resolved symbol doesn't contain the
                        # expected strike, skip — prevents a stale token
                        # cache from crossing strikes.
                        _sym_chk = str(_oi.get("symbol", ""))
                        if _sym_chk and str(_strike_val) not in _sym_chk:
                            logger.warning("[MAIN] Strike/symbol mismatch skipped: "
                                           + _label + " " + opt_type + " strike="
                                           + str(_strike_val) + " sym=" + _sym_chk)
                            continue
                        result = check_entry(
                            token=_oi["token"],
                            option_type=opt_type,
                            spot_ltp=spot_ltp,
                            dte=dte,
                            expiry_date=expiry,
                            kite=kite,
                            other_token=_other_tok,
                            state=state,
                        )
                        result["_strike"] = _strike_val
                        result["_strike_label"] = _label
                        result["_symbol"] = _oi.get("symbol", "")
                        all_results[opt_type + "_" + _label] = result
                        if not result["fired"]:
                            continue
                        _body = float(result.get("body_pct", 0) or 0)
                        _gap = float(result.get("close", 0) or 0) - float(result.get("ema9_low", 0) or 0)
                        _score = round(_body * max(_gap, 0.1), 2)
                        _candidates.append({
                            "type": opt_type, "label": _label,
                            "info": _oi, "result": result,
                            "score": _score, "strike": _strike_val,
                        })

                # Pick best candidate by score
                if _candidates:
                    _candidates.sort(key=lambda c: c["score"], reverse=True)
                    _winner = _candidates[0]
                    opt_type = _winner["type"]
                    opt_info = _winner["info"]
                    result = _winner["result"]
                    _win_label = _winner["label"]
                    _win_score = _winner["score"]
                    result["_strike"] = _winner["strike"]
                    result["_strike_label"] = _win_label
                    result["_entry_score"] = _win_score
                    logger.info("[MAIN] BEST CANDIDATE: " + opt_type + " " + _win_label
                                + " strike=" + str(_winner["strike"])
                                + " score=" + str(_win_score)
                                + " (of " + str(len(_candidates)) + " fired)")

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
                    if ok:
                        best_result = result
                        best_type = opt_type
                        best_opt_info = opt_info
                    else:
                        logger.info("[MAIN] Entry blocked (" + opt_type
                                    + " " + _win_label + "): " + reason)

                # Populate all_results for dashboard (keep CE/PE keys for compat)
                for _k, _v in list(all_results.items()):
                    if _k.startswith("CE_ATM"):
                        all_results["CE"] = _v
                    elif _k.startswith("PE_ATM"):
                        all_results["PE"] = _v

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

                # ── v15.2 Part 4: silent 1-min shadow strategy ────────
                # Runs after live scan on every 1-min boundary. Independent
                # state, independent cooldown, never touches live state,
                # never places orders, never alerts during the day.
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
                    if state.get("in_trade"):
                        D.mark_trade_taken(best_type)

            # Quick LTP update between candle scans
            if now.second % 10 < 2:
                _update_dashboard_ltp()

        except Exception as e:
            logger.error("[MAIN] Loop error: " + str(e))
            time.sleep(2)

        time.sleep(1)


# ═══════════════════════════════════════════════════════════════
# === TELEGRAM COMMANDS (merged from VRL_COMMANDS) ===
# ═══════════════════════════════════════════════════════════════

# Dynamic public IP — resolved once at module load
_WEB_IP = ""
try:
    import subprocess as _sp
    _WEB_IP = _sp.check_output(["curl", "-s", "ifconfig.me"], timeout=5).decode().strip()
except Exception:
    _WEB_IP = "unknown"


def _send_today_download(target_date: str = None):
    """Full day zip — all logs + data + state for a date.
    /download             → today
    /download YYYY-MM-DD  → specific day
    """
    if target_date is None:
        target_date = date.today().strftime("%Y-%m-%d")

    files = D.collect_logs_for_date(target_date)
    if not files:
        _tg_send("No files found for " + target_date)
        return

    zip_path = D.create_daily_zip(target_date)
    if not zip_path or not os.path.isfile(zip_path):
        _tg_send("Failed to create zip for " + target_date)
        return

    try:
        size_mb = round(os.path.getsize(zip_path) / (1024 * 1024), 2)
        file_count = len(files)
        categories = {}
        for _, arcname in files:
            cat = arcname.split("/")[0]
            categories[cat] = categories.get(cat, 0) + 1
        cat_summary = " | ".join(k + ":" + str(v) for k, v in sorted(categories.items()))
        _TG_SIZE_LIMIT_MB = 45
        caption = ("📦 VRL Logs — " + target_date
                   + "\n" + str(file_count) + " files | "
                   + str(size_mb) + " MB"
                   + "\n" + cat_summary)

        if size_mb > _TG_SIZE_LIMIT_MB:
            _link_hint = "http://" + str(_WEB_IP) + ":8080"
            logger.warning("[DOWNLOAD] zip " + os.path.basename(zip_path)
                           + " is " + str(size_mb) + "MB > "
                           + str(_TG_SIZE_LIMIT_MB) + "MB Telegram cap — "
                           "skipping send, file preserved at " + zip_path)
            _tg_send(
                "⚠️ <b>DOWNLOAD TOO LARGE FOR TELEGRAM</b>\n"
                "Date : " + target_date + "\n"
                "Size : " + str(size_mb) + " MB (cap " + str(_TG_SIZE_LIMIT_MB) + " MB)\n"
                "Files: " + str(file_count) + "\n"
                + cat_summary + "\n"
                "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                "Local path: <code>" + zip_path + "</code>\n"
                "Fetch via SSH or browse " + _link_hint + "."
            )
            return

        _ok = False
        try:
            _ok = bool(_tg_send_file(zip_path, caption=caption))
        except Exception as _se:
            logger.error("[DOWNLOAD] Telegram file send raised: "
                         + type(_se).__name__ + " " + str(_se))
            _ok = False

        if _ok:
            logger.info("[DOWNLOAD] sent " + os.path.basename(zip_path)
                        + " (" + str(size_mb) + "MB, "
                        + str(file_count) + " entries)")
            try:
                os.remove(zip_path)
            except Exception:
                pass
        else:
            logger.warning("[DOWNLOAD] Telegram send failed — zip "
                           "preserved for SSH retrieval: " + zip_path)
            _tg_send(
                "⚠️ <b>DOWNLOAD DELIVERY FAILED</b>\n"
                "Date : " + target_date + "\n"
                "Size : " + str(size_mb) + " MB\n"
                "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                "File kept on disk for SSH pull:\n"
                "<code>" + zip_path + "</code>"
            )
    except Exception as e:
        _tg_send("Download error: " + str(e))


def _why_blocked(st: dict) -> str:
    if st.get("paused"):
        return "⏸ PAUSED"
    return "✅ Ready to enter"


def _cmd_pulse(args):
    """🩺 Doctor's pulse check — single-shot diagnostic dump.
    Reports everything needed to spot a bug at a glance: bot/data/
    market/today/position/engine/config/errors. Designed so the
    output can be forwarded for remote diagnosis."""
    try:
        import VRL_CONFIG as _CFG
        now = datetime.now()
        # Uptime
        _up_secs = int(time.time() - _BOT_START_TS)
        _up_h = _up_secs // 3600
        _up_m = (_up_secs % 3600) // 60
        _up_str = (str(_up_h) + "h " if _up_h else "") + str(_up_m) + "m"

        # Data layer health
        _spot = D.get_ltp(D.NIFTY_SPOT_TOKEN)
        _spot_live = D.is_tick_live(D.NIFTY_SPOT_TOKEN)
        with D._tick_lock:
            _se = D._ticks.get(int(D.NIFTY_SPOT_TOKEN))
        _tick_age = int(time.time() - _se["ts"]) if _se else -1
        _market = D.is_market_open()
        _acct = D.get_account_info() if hasattr(D, "get_account_info") else {}
        _user = _acct.get("name", "?")
        _lot = D.get_lot_size()

        # Today block
        try:
            _trades_today = _read_today_trades() if "_read_today_trades" in globals() else []
        except Exception:
            _trades_today = []
        _td_pnl = sum(float(t.get("pnl_pts", 0) or 0) for t in _trades_today)
        _td_wins = sum(1 for t in _trades_today if float(t.get("pnl_pts", 0) or 0) > 0)
        _td_loss = len(_trades_today) - _td_wins
        _last_t = _trades_today[-1] if _trades_today else None

        # Position snapshot
        _in_trade = state.get("in_trade", False)
        _pos_str = "—"
        if _in_trade:
            _ep = float(state.get("entry_price", 0) or 0)
            _ltp = D.get_ltp(state.get("token", 0))
            _pn = round(_ltp - _ep, 1) if _ltp else 0
            _pk = float(state.get("peak_pnl", 0) or 0)
            _tier = state.get("active_ratchet_tier", "INITIAL") or "INITIAL"
            _sl = float(state.get("active_ratchet_sl", 0) or 0)
            if _sl <= 0: _sl = round(_ep - 10, 2)
            _lock = round(_sl - _ep, 1)
            _room = round(_ltp - _sl, 1) if _ltp else 0
            _dir_emj = "🟢" if state.get("direction") == "CE" else "🔴"
            _sym = state.get("direction", "") + " " + str(state.get("strike", ""))
            _pos_str = (
                _dir_emj + " " + _sym + "  " + ("+" if _pn >= 0 else "") + str(_pn) + "pts\n"
                + "Entry Rs" + str(_ep) + " → " + str(round(_ltp, 2)) + " · Peak +" + str(_pk) + "\n"
                + "Tier: " + _tier + " @ Rs" + str(round(_sl, 2))
                + " (Lock " + ("+" if _lock >= 0 else "") + str(_lock) + " · Room "
                + ("+" if _room >= 0 else "") + str(_room) + ")"
            )

        # Engine state
        _ce_lck = _locked_ce_strike or "?"
        _pe_lck = _locked_pe_strike or "?"
        _last_scan = state.get("_last_scan_minute", "?")

        # Config snapshot
        _eb = _CFG.get().get("entry", {}).get("ema9_band", {}) or {}
        _xb = _CFG.get().get("exit", {}).get("ema9_band", {}) or {}
        _cd = _CFG.entry_ema9_band("cooldown_minutes", 5) if hasattr(_CFG, "entry_ema9_band") else 5

        # Recent errors (last 5 lines)
        _err_lines = []
        try:
            _err_path = os.path.join(D.ERROR_LOG_DIR, date.today().strftime("%Y-%m-%d") + ".log")
            if os.path.isfile(_err_path):
                with open(_err_path) as _f:
                    _err_lines = [ln.strip() for ln in _f.readlines()[-5:]]
        except Exception:
            pass

        # Status indicators — distinguish ✅ healthy / ❌ broken / 💤 idle
        def _ok(b): return "✅" if b else "❌"
        # Market: ✅ open · 💤 closed (not an error after-hours)
        if _market:
            _market_icon = "✅"; _market_str = "OPEN"
        else:
            _market_icon = "💤"; _market_str = "CLOSED (idle until 09:15 IST)"
        # Tick: only flag ❌ when market is OPEN and tick is stale
        if _spot > 0 and _spot_live:
            _tick_icon = "✅"; _tick_str = str(round(_spot, 2)) + "  (" + str(_tick_age) + "s ago)"
        elif not _market:
            _tick_icon = "💤"
            _tick_str = ("idle (last " + str(round(_se["ltp"], 2))
                         + " · " + str(_tick_age // 60) + "m ago)") if _se else "idle (no history)"
        else:
            _tick_icon = "❌"
            _tick_str = "STALE — " + (str(_tick_age) + "s ago" if _tick_age >= 0 else "never")

        msg = (
            "🩺 <b>PULSE CHECK</b> · " + now.strftime("%H:%M:%S") + " IST\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "<b>BOT</b>\n"
            + _ok(True) + " v" + D.VERSION.replace("v", "") + " · uptime " + _up_str + "\n"
            + _ok(True) + " " + ("PAPER" if D.PAPER_MODE else "LIVE")
            + " · " + str(_lot) + " × 2 lots\n"
            + _market_icon + " market " + _market_str + "\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "<b>DATA</b>\n"
            + _ok(_user != "?") + " token: " + str(_user) + "\n"
            + _tick_icon + " spot tick: " + _tick_str + "\n"
            + _ok(_lot > 0) + " lot size: " + str(_lot) + "\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "<b>TODAY</b>\n"
            + str(len(_trades_today)) + " trades · "
            + str(_td_wins) + "W " + str(_td_loss) + "L · "
            + ("+" if _td_pnl >= 0 else "") + "{:.1f}".format(_td_pnl) + " pts\n"
            + ("Last: " + str(_last_t.get("entry_time", "?")) + " "
               + str(_last_t.get("direction", "?")) + " "
               + str(_last_t.get("strike", "?")) + " "
               + ("+" if float(_last_t.get("pnl_pts", 0) or 0) >= 0 else "")
               + str(_last_t.get("pnl_pts", "?")) + " ("
               + str(_last_t.get("exit_reason", "?")) + ")\n" if _last_t else "")
            + "━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "<b>POSITION</b>\n"
            + _pos_str + "\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "<b>ENGINE</b>\n"
            + ("Locked: CE " + str(_ce_lck) + " · PE " + str(_pe_lck) + "\n"
               + "Last scan: " + str(_last_scan) + "\n"
               + "Bias: " + str(state.get("daily_bias", "?")) + "\n"
               if _market else "💤 awaiting market open\n")
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "<b>CONFIG</b>\n"
            "Body min: " + str(_eb.get("body_pct_min", "?")) + "%  "
            + "Band min: " + str(_eb.get("band_width_min", "?")) + "pts\n"
            "Slope lookback: " + str(_eb.get("ema9_slope_lookback", "?")) + "c  "
            + "SL: " + str(_xb.get("emergency_sl_pts", "?")) + "pts\n"
            "Time: " + str(_eb.get("warmup_until", "?")) + " - "
            + str(_eb.get("cutoff_after", "?")) + "  "
            + "EOD: " + str(_xb.get("eod_exit_time", "?")) + "\n"
            "Cooldown: " + str(_cd) + "min\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "<b>ERRORS</b> (today, last 5)\n"
            + (_ok(False) + " " + str(len(_err_lines)) + " errors\n<pre>"
               + "\n".join(ln[:100] for ln in _err_lines) + "</pre>"
               if _err_lines else _ok(True) + " None\n")
            + "━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "<b>SMART TRAIL v2+</b>\n"
            "INITIAL  (peak <5)   entry-10\n"
            "🛡️ BREAKEVEN(5-8)    entry\n"
            "🔒 TRAIL_60 (8-15)   entry+60%\n"
            "🔒🔒 TRAIL_75(15-30) entry+75%\n"
            "🔒🔒 VISHAL_MAX(30-45) entry+85%\n"
            "🔒🔒🔒 TRAIL_90(45+) entry+90%\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "<b>GOLDEN 4 ENTRY</b>\n"
            "1. Time " + str(_eb.get("warmup_until", "09:35")) + " - "
            + str(_eb.get("cutoff_after", "15:10")) + "\n"
            "2. Close > EMA9_low + Band > " + str(_eb.get("band_width_min", 7)) + "\n"
            "3. EMA9 slope flat or rising\n"
            "4. Body ≥ " + str(_eb.get("body_pct_min", 40)) + "%\n"
        )
        _tg_send(msg)
    except Exception as e:
        _tg_send("🩺 Pulse error: " + str(e))


def _cmd_help(args):
    _tg_send(
        "🤖 <b>VISHAL RAJPUT TRADE " + D.VERSION + "</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "<b>DIAGNOSTIC</b>\n"
        "/pulse     — 🩺 full health check (one-shot)\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "<b>TRADING</b>\n"
        "/status    — trade status + PNL\n"
        "/trades    — today's trade list\n"
        "/account   — balance + margin info\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "<b>DATA</b>\n"
        "/download  — full day zip (or /download YYYY-MM-DD)\n"
        "/livecheck — last 50 log lines\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "<b>CONTROL</b>\n"
        "/pause      — block new entries\n"
        "/resume     — re-enable entries\n"
        "/forceexit  — emergency exit all lots\n"
        "/restart    — restart bot\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "VISHAL RAJPUT TRADE v16.6 — EMA9 Band Breakout, "
        "7 entry gates, 3-rule exit chain "
        "(Emergency SL / EOD 15:20 / Vishal Trail), "
        + ("PAPER" if D.PAPER_MODE else "LIVE") + " 2 lots.\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "🌐 Dashboard: http://" + _WEB_IP + ":8080"
    )


def _cmd_status(args):
    global _kite
    with _state_lock:
        st = dict(state)

    if not st.get("in_trade"):
        last_scan = st.get("_last_scan", {})
        _warmup_line = ""
        try:
            import json as _j
            import os as _os
            _dash_path = _os.path.join(D.STATE_DIR, "vrl_dashboard.json")
            if _os.path.isfile(_dash_path):
                with open(_dash_path) as _df:
                    _d = _j.load(_df)
                _mk = _d.get("market", {})
                if _mk.get("market_open") and not _mk.get("indicators_warm", True):
                    _wp = _mk.get("warmup_progress", 0)
                    _wn = _mk.get("warmup_needed", 14)
                    _we = _mk.get("warmup_eta", "—")
                    _warmup_line = ("🟡 WARMUP (" + str(_wp) + "/" + str(_wn) + " candles)\n"
                                    "ETA       : " + _we + "\n"
                                    "Trades blocked until indicators stable\n"
                                    "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n")
        except Exception:
            pass
        _tg_send(
            "📊 <b>STATUS — NO TRADE</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            + _warmup_line +
            "PNL    : " + str(round(st.get("daily_pnl", 0), 1)) + "pts\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "Last scan : " + last_scan.get("time", "—") + "\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "Bot       : " + _why_blocked(st)
        )
        return

    ltp = 0.0
    try:
        ltp = D.get_ltp(st.get("token"))
        if ltp <= 0 and _kite is not None:
            symbol = st.get("symbol")
            if symbol:
                q = _kite.ltp(["NFO:" + symbol])
                ltp = float(q["NFO:" + symbol]["last_price"])
                logger.info("[STATUS] LTP via REST: " + str(ltp))
    except Exception as e:
        logger.warning("[STATUS] LTP fetch error: " + str(e))
        ltp = 0.0

    entry   = st.get("entry_price", 0)
    pnl     = round(ltp - entry, 1) if ltp > 0 else 0
    peak    = st.get("peak_pnl", 0)

    _tier = st.get("active_ratchet_tier", "None")
    _rsl  = float(st.get("active_ratchet_sl", 0) or 0)
    if _tier and _tier not in ("", "None", "INITIAL") and _rsl > 0:
        _stop_line = "Trail  : " + _tier + " @ Rs" + str(round(_rsl, 1))
        _stop_dist = round(ltp - _rsl, 1) if ltp > 0 else "—"
    else:
        _init_sl   = round(entry - 10, 1)
        _stop_line = "Trail  : INITIAL @ Rs" + str(_init_sl)
        _stop_dist = round(ltp - _init_sl, 1) if ltp > 0 else "—"

    _tg_send(
        "📊 <b>STATUS — IN TRADE</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "Time   : " + _now_str() + "\n"
        "Symbol : " + st.get("symbol", "") + "\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "Entry  : " + str(round(entry, 2)) + "\n"
        "LTP    : " + str(round(ltp, 2)) + "\n"
        "PNL    : " + ("+" if pnl >= 0 else "") + str(pnl) + "pts  " + _rs(pnl) + "\n"
        "Peak   : +" + str(round(peak, 1)) + "pts\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        + _stop_line + "  (" + str(_stop_dist) + "pts away)\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "Day PNL: " + str(round(st.get("daily_pnl", 0), 1)) + "pts"
    )


def _cmd_account(args):
    try:
        _acct = D.get_account_info()
        if _kite:
            D.refresh_margin(_kite)
            _acct = D.get_account_info()
    except Exception:
        _acct = D.get_account_info()

    if not _acct.get("name"):
        _tg_send("Account info not available. Bot may not have fetched it yet.")
        return

    _tg_send(
        "👤 <b>ACCOUNT</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "Name     : " + _acct.get("name", "") + "\n"
        "User ID  : " + _acct.get("user_id", "") + "\n"
        "Broker   : " + _acct.get("broker", "Zerodha") + "\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "Balance  : ₹" + "{:,}".format(int(_acct.get("total_balance", 0))) + "\n"
        "Available: ₹" + "{:,}".format(int(_acct.get("available_margin", 0))) + "\n"
        "Used     : ₹" + "{:,}".format(int(_acct.get("used_margin", 0)))
    )


def _cmd_download(args):
    """Full day zip. /download → today; /download YYYY-MM-DD → specific day."""
    target = None
    if isinstance(args, list):
        args = " ".join(args)
    if args and args.strip():
        arg = args.strip()
        if len(arg) == 8 and arg.isdigit():
            target = arg[:4] + "-" + arg[4:6] + "-" + arg[6:8]
        elif len(arg) == 10 and arg[4] == "-" and arg[7] == "-":
            target = arg
        else:
            _tg_send("Usage: /download or /download 2026-04-16")
            return
    _send_today_download(target)


def _cmd_pause(args):
    with _state_lock:
        state["paused"] = True
    _tg_send("⏸ Paused. No new entries.")
    logger.info("[CTRL] Paused")


def _cmd_resume(args):
    with _state_lock:
        state["paused"] = False
    _tg_send("▶️ Resumed.")
    logger.info("[CTRL] Resumed")


def _cmd_forceexit(args):
    with _state_lock:
        if not state.get("in_trade"):
            _tg_send("No open trade.")
            return
        state["force_exit"] = True
    _tg_send("🚨 Force exit triggered.")
    logger.warning("[CTRL] Force exit")


def _cmd_restart(args):
    _tg_send("🔄 Restarting...")
    logger.info("[CTRL] Restart requested")
    _remove_pid()
    time.sleep(2)
    os.execv(sys.executable, [sys.executable] + sys.argv)


def _cmd_livecheck(args):
    try:
        with open(D.LIVE_LOG_FILE, "r") as f:
            lines = f.readlines()
        last_50 = "".join(lines[-50:])
        if len(last_50) > 4000:
            last_50 = last_50[-4000:]
        import re as _re
        last_50 = _re.sub(r'(api_key|access_token|token|secret|password)\s*[=:]\s*\S+',
                          r'\1=***', last_50, flags=_re.IGNORECASE)
        _tg_send("<pre>" + last_50 + "</pre>")
    except Exception as e:
        _tg_send("Log error: " + str(e))


def _cmd_trades(args):
    """Today's trade list with details."""
    trades = _read_today_trades()
    if not trades:
        _tg_send("📒 No trades today.")
        return
    lines = ""
    total = 0.0
    for i, t in enumerate(trades, 1):
        pts = float(t.get("pnl_pts", 0))
        total += pts
        sign = "+" if pts >= 0 else ""
        icon = "✅" if pts >= 0 else "❌"
        peak = float(t.get("peak_pnl", 0))
        captured = round(pts / peak * 100) if peak > 0 else 0
        lines += (
            icon + " <b>Trade " + str(i) + "</b>  " + t.get("direction", "") + "\n"
            "  " + t.get("entry_time", "") + " → " + t.get("exit_time", "") + "\n"
            "  Entry: ₹" + str(t.get("entry_price", "")) + " → Exit: ₹" + str(t.get("exit_price", "")) + "\n"
            "  PNL: " + sign + str(round(pts, 1)) + "pts  " + _rs(pts) + "\n"
            "  Peak: +" + str(round(peak, 1)) + "pts  Captured: " + str(captured) + "%\n"
            "  Reason: " + t.get("exit_reason", "") + "\n"
        )
    sign = "+" if total >= 0 else ""
    _tg_send(
        "📒 <b>TODAY'S TRADES</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        + lines
        + "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "Net: " + sign + str(round(total, 1)) + "pts  " + _rs(total)
    )


_DISPATCH = {
    "/help"      : _cmd_help,
    "/pulse"     : _cmd_pulse,
    "/status"    : _cmd_status,
    "/trades"    : _cmd_trades,
    "/account"   : _cmd_account,
    "/pause"     : _cmd_pause,
    "/resume"    : _cmd_resume,
    "/forceexit" : _cmd_forceexit,
    "/restart"   : _cmd_restart,
    "/livecheck" : _cmd_livecheck,
    "/download"  : _cmd_download,
}


# ═══════════════════════════════════════════════════════════════
# === TRADE EXECUTION (merged from VRL_TRADE) ===
# ═══════════════════════════════════════════════════════════════

def _verify_timeout(kind: str, default: int) -> int:
    """Pull verify_order_fill timeouts from config.yaml
    trade.verify_timeout_{entry,exit}. Fall back to the historical
    hardcoded value if config lacks the key."""
    try:
        v = (CFG.get().get("trade") or {}).get("verify_timeout_" + kind)
        if v is not None:
            return int(v)
    except Exception:
        pass
    return default


def verify_order_fill(kite, order_id: str, timeout_secs: int = 10) -> tuple:
    """Poll order history until filled or timeout. Returns (fill_price, fill_qty)."""
    deadline = time.time() + timeout_secs
    while time.time() < deadline:
        try:
            history = kite.order_history(order_id)
            if not history:
                time.sleep(0.5)
                continue
            last = history[-1]
            status = last.get("status", "")
            if status == "COMPLETE":
                return float(last.get("average_price", 0)), int(last.get("filled_quantity", 0))
            elif status in ("REJECTED", "CANCELLED"):
                logger.error("[TRADE] Order " + order_id + " " + status
                             + " msg=" + str(last.get("status_message", "")))
                return 0.0, 0
        except Exception as e:
            logger.warning("[TRADE] verify_fill error: " + str(e))
        time.sleep(0.5)
    logger.error("[TRADE] Fill verification timeout: " + order_id)
    return 0.0, 0


def place_entry(kite, symbol: str, token: int,
                option_type: str, qty: int,
                entry_price_ref: float) -> dict:
    """Paper mode: simulated fill. Live mode: LIMIT entry at LTP + buffer."""
    if D.PAPER_MODE:
        logger.info("[TRADE] PAPER ENTRY: " + symbol
                    + " qty=" + str(qty)
                    + " ref=" + str(round(entry_price_ref, 2)))
        return {
            "ok": True, "fill_price": round(entry_price_ref, 2),
            "fill_qty": qty,
            "order_id": "PAPER_" + datetime.now().strftime("%H%M%S%f")[:12],
            "error": "", "slippage": 0,
        }

    _first_live_flag = os.path.expanduser("~/state/.first_live_done")
    if not os.path.isfile(_first_live_flag):
        logger.info("[TRADE] 🚀 FIRST LIVE ORDER EVER")

    buffer = max(2.0, round(entry_price_ref * 0.01, 1))
    limit_price = round(entry_price_ref + buffer, 1)

    logger.info("[TRADE] LIMIT ENTRY: ref=" + str(round(entry_price_ref, 2))
                + " buffer=" + str(buffer) + " limit=" + str(limit_price))

    try:
        order_id = kite.place_order(
            variety          = kite.VARIETY_REGULAR,
            exchange         = D.EXCHANGE_NFO,
            tradingsymbol    = symbol,
            transaction_type = kite.TRANSACTION_TYPE_BUY,
            quantity         = qty,
            order_type       = kite.ORDER_TYPE_LIMIT,
            price            = limit_price,
            product          = kite.PRODUCT_MIS,
        )
        logger.info("[TRADE] LIMIT ENTRY placed: " + str(order_id)
                    + " limit=" + str(limit_price))

        fill_price, fill_qty = verify_order_fill(
            kite, order_id, timeout_secs=_verify_timeout("entry", 8))

        if fill_qty == 0:
            try:
                kite.cancel_order(kite.VARIETY_REGULAR, order_id)
                logger.info("[TRADE] Entry cancelled — price moved away")
            except Exception:
                pass
            return {
                "ok": False, "fill_price": 0.0, "fill_qty": 0,
                "order_id": str(order_id),
                "error": "LIMIT_NOT_FILLED", "slippage": 0,
            }

        slippage = round(fill_price - entry_price_ref, 2)
        logger.info("[TRADE] ENTRY FILLED: price=" + str(fill_price)
                    + " slippage=" + str(slippage) + "pts")

        if not os.path.isfile(_first_live_flag):
            try:
                with open(_first_live_flag, "w") as _f:
                    _f.write(datetime.now().isoformat())
            except Exception:
                pass

        if fill_qty < qty:
            logger.warning("[TRADE] Partial fill accepted: "
                           + str(fill_qty) + "/" + str(qty))

        return {
            "ok": True, "fill_price": fill_price, "fill_qty": fill_qty,
            "order_id": str(order_id), "error": "", "slippage": slippage,
        }

    except TokenException as e:
        logger.error("[TRADE] Entry auth error: " + str(e))
        return {"ok": False, "fill_price": 0.0, "fill_qty": 0,
                "order_id": "", "error": "AUTH_EXPIRED: " + str(e), "slippage": 0}
    except OrderException as e:
        logger.error("[TRADE] Entry order rejected: " + str(e))
        return {"ok": False, "fill_price": 0.0, "fill_qty": 0,
                "order_id": "", "error": "ORDER_REJECTED: " + str(e), "slippage": 0}
    except NetworkException as e:
        logger.error("[TRADE] Entry network error: " + str(e))
        return {"ok": False, "fill_price": 0.0, "fill_qty": 0,
                "order_id": "", "error": "NETWORK: " + str(e), "slippage": 0}
    except Exception as e:
        logger.error("[TRADE] Entry unexpected: " + type(e).__name__ + " " + str(e))
        return {"ok": False, "fill_price": 0.0, "fill_qty": 0,
                "order_id": "", "error": str(e), "slippage": 0}


def place_exit(kite, symbol: str, token: int,
               option_type: str, qty: int,
               exit_price_ref: float, reason: str) -> dict:
    """Paper mode: simulated fill. Live mode: MARKET exit with retry."""
    if D.PAPER_MODE:
        logger.info("[TRADE] PAPER EXIT: " + symbol
                    + " qty=" + str(qty)
                    + " ref=" + str(round(exit_price_ref, 2))
                    + " reason=" + reason)
        return {
            "ok": True, "fill_price": round(exit_price_ref, 2),
            "fill_qty": qty,
            "order_id": "PAPER_" + datetime.now().strftime("%H%M%S%f")[:12],
            "error": "", "slippage": 0,
        }

    for attempt in range(2):
        try:
            order_id = kite.place_order(
                variety          = kite.VARIETY_REGULAR,
                exchange         = D.EXCHANGE_NFO,
                tradingsymbol    = symbol,
                transaction_type = kite.TRANSACTION_TYPE_SELL,
                quantity         = qty,
                order_type       = kite.ORDER_TYPE_MARKET,
                product          = kite.PRODUCT_MIS,
                market_protection = -1,
            )
            logger.info("[TRADE] MARKET EXIT placed attempt=" + str(attempt + 1)
                        + " order=" + str(order_id))

            fill_price, fill_qty = verify_order_fill(kite, order_id)

            if fill_qty > 0:
                slippage = round(exit_price_ref - fill_price, 2)
                return {
                    "ok": True, "fill_price": fill_price, "fill_qty": fill_qty,
                    "order_id": str(order_id), "error": "", "slippage": slippage,
                }

            logger.warning("[TRADE] Exit attempt " + str(attempt + 1) + " not filled")
            time.sleep(1)

        except TokenException as e:
            logger.error("[TRADE] Exit auth error attempt=" + str(attempt + 1) + ": " + str(e))
            time.sleep(1)
        except (OrderException, NetworkException) as e:
            logger.error("[TRADE] Exit order/network error attempt=" + str(attempt + 1) + ": " + str(e))
            time.sleep(1)
        except Exception as e:
            logger.error("[TRADE] Exit unexpected error attempt=" + str(attempt + 1)
                         + ": " + type(e).__name__ + " " + str(e))
            time.sleep(1)

    logger.critical("CRITICAL: Exit failed for " + symbol
                    + " qty=" + str(qty) + ". MANUAL ACTION REQUIRED.")
    return {"ok": False, "fill_price": 0.0, "fill_qty": 0,
            "order_id": "", "error": "EXIT_FAILED_MANUAL_REQUIRED", "slippage": 0}


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
    handler = _DISPATCH.get(raw_cmd)
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
    # Warn if shutting down with open trade
    if state.get("in_trade"):
        _sym   = state.get("symbol", "?")
        _entry = round(state.get("entry_price", 0), 2)
        _pk    = round(state.get("peak_pnl", 0), 1)
        logger.warning("[MAIN] Shutdown with open trade — state preserved for resume"
                       " (symbol=" + _sym
                       + " entry=" + str(_entry)
                       + " peak=" + str(_pk) + ")")
        try:
            _tg_send(
                "⚠️ VRL SHUTDOWN with open position: " + _sym
                + " entry=" + str(_entry)
                + " peak=" + str(_pk),
                priority="critical",
            )
            # Give the daemon thread a moment to deliver before sys.exit
            time.sleep(1.5)
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
        # Wait up to ~15s for the first tick before declaring no feed —
        # the WS subscription lands before this check but the first tick
        # can arrive 5-10s later. Outside market hours we don't expect
        # ticks at all, so report that explicitly instead of warning.
        import time as _time_h
        _ws_ltp = 0.0
        _market_open_now = D.is_market_open()
        if _market_open_now:
            for _ in range(15):
                _ws_ltp = D.get_ltp(D.NIFTY_SPOT_TOKEN)
                if _ws_ltp > 0:
                    break
                _time_h.sleep(1)
            if _ws_ltp > 0:
                _health_lines.append("WS: ✅ tick=" + str(round(_ws_ltp, 1)))
            else:
                _health_lines.append("WS: ⚠️ no tick after 15s (feed may be down)")
                _health_ok = False
        else:
            # Market closed — WS won't push ticks. Surface the last-known
            # tick age if we have one, otherwise say idle.
            with D._tick_lock:
                _entry = D._ticks.get(int(D.NIFTY_SPOT_TOKEN))
            if _entry:
                _age_min = int((_time_h.time() - _entry["ts"]) / 60)
                _health_lines.append(
                    "WS: 💤 market closed (last tick "
                    + str(_age_min) + "m ago at "
                    + str(round(_entry["ltp"], 1)) + ")"
                )
            else:
                _health_lines.append("WS: 💤 market closed (no ticks yet)")
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
    # present, one WARNING per missing dir. Helps the operator see at a
    # glance what `/download` will actually find on disk.
    try:
        D.audit_log_paths()
    except Exception as _ae:
        logger.debug("[MAIN] audit_log_paths error: " + str(_ae))

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
                state["daily_pnl"]          = round(pnl, 2)

            logger.info("[MAIN] Restored: " + str(len(trades_today))
                        + " trades | " + str(len(losses)) + " losses | pnl="
                        + str(round(pnl,1)) + "pts")
        else:
            logger.info("[MAIN] No trades found for today — starting fresh")
    except Exception as e:
        logger.warning("[MAIN] Trade log restore failed: " + str(e))
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

    start_lab(kite)
    _start_telegram_listener()
    _alert_bot_started()

    logger.info("[MAIN] All systems ready. Strategy loop starting.")
    _strategy_loop(kite)

if __name__ == "__main__":
    main()
