# ═══════════════════════════════════════════════════════════════
#  VRL_DATA.py — VISHAL RAJPUT TRADE v13.7
#  Foundation layer. Settings, logging, market data, Greeks.
# ═══════════════════════════════════════════════════════════════

import os
import math
import time
import logging
import threading
from datetime import date, datetime, timedelta
from logging.handlers import TimedRotatingFileHandler
from zoneinfo import ZoneInfo

import pandas as pd
from kiteconnect import KiteTicker

import VRL_CONFIG as CFG

# Load config at import time — fails fast if config.yaml is missing/invalid
CFG.load()

VERSION  = "v16.3"
BOT_NAME = "VISHAL RAJPUT TRADE"

# ── Timezone ──
IST = ZoneInfo("Asia/Kolkata")

def now_ist() -> datetime:
    """Return current time in IST, timezone-aware."""
    return datetime.now(IST)

def _load_env_file(path: str):
    if not os.path.isfile(path):
        return
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            val = val.strip().strip('"').strip("'")
            os.environ.setdefault(key.strip(), val)

_load_env_file(os.path.expanduser("~/.env"))

PAPER_MODE       = CFG.is_paper()
KITE_API_KEY     = os.getenv("KITE_API_KEY", "")
KITE_API_SECRET  = os.getenv("KITE_API_SECRET", "")
TELEGRAM_TOKEN   = os.getenv("TG_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TG_GROUP_ID", "")

BASE_DIR         = os.path.expanduser("~")
REPO_DIR         = os.path.dirname(os.path.abspath(__file__))
LOGS_DIR         = os.path.join(BASE_DIR, "logs")
LIVE_LOG_DIR     = os.path.join(LOGS_DIR, "live")
LAB_LOG_DIR      = os.path.join(LOGS_DIR, "lab")
FLOW_LOG_DIR     = os.path.join(LOGS_DIR, "flow")
AUTH_LOG_DIR     = os.path.join(LOGS_DIR, "auth")
WEB_LOG_DIR      = os.path.join(LOGS_DIR, "web")
HEALTH_LOG_DIR   = os.path.join(LOGS_DIR, "health")
ZONES_LOG_DIR    = os.path.join(LOGS_DIR, "zones")
ML_LOG_DIR       = os.path.join(LOGS_DIR, "ml")
ERROR_LOG_DIR    = os.path.join(LOGS_DIR, "errors")
# STATE_DIR lives next to the code (inside the repo) so AUTH and MAIN
# always agree on the token location. BUG-015.
STATE_DIR        = os.path.join(REPO_DIR, "state")
LAB_DIR          = os.path.join(BASE_DIR, "lab_data")
BACKUP_DIR       = os.path.join(BASE_DIR, "backups")
OPTIONS_3MIN_DIR = os.path.join(LAB_DIR, "options_3min")
OPTIONS_1MIN_DIR = os.path.join(LAB_DIR, "options_1min")
SPOT_DIR         = os.path.join(LAB_DIR, "spot")
REPORTS_DIR      = os.path.join(LAB_DIR, "reports")
SESSIONS_DIR     = os.path.join(LAB_DIR, "sessions")

LIVE_LOG_FILE    = os.path.join(LIVE_LOG_DIR, "vrl_live.log")
LAB_LOG_FILE     = os.path.join(LAB_LOG_DIR,  "vrl_lab.log")
TRADE_LOG_PATH   = os.path.join(LAB_DIR,      "vrl_trade_log.csv")
STATE_FILE_PATH  = os.path.join(STATE_DIR,    "vrl_live_state.json")
PID_FILE_PATH    = os.path.join(STATE_DIR,    "vrl_live.pid")
TOKEN_FILE_PATH  = os.path.join(STATE_DIR,    "access_token.json")

# ── All constants now read from config.yaml via VRL_CONFIG ──
INSTRUMENT_NAME  = CFG.instrument_name()
EXCHANGE_NFO     = CFG.get()["instrument"].get("exchange_nfo", "NFO")
EXCHANGE_NSE     = CFG.get()["instrument"].get("exchange_nse", "NSE")
LOT_SIZE_BASE    = CFG.lot_size()
LOT_SIZE         = LOT_SIZE_BASE
STRIKE_STEP         = CFG.strike_cfg("step", 100)
STRIKE_STEP_EXPIRY  = CFG.strike_cfg("step_expiry", 50)
NIFTY_SPOT_TOKEN = CFG.spot_token()
INDIA_VIX_TOKEN  = CFG.vix_token()
RISK_FREE_RATE   = CFG.risk("risk_free_rate", 0.065)

MAX_DAILY_TRADES        = CFG.risk("max_daily_trades", 999)
MAX_DAILY_LOSSES        = CFG.risk("max_daily_losses", 999)
PROFIT_LOCK_PTS         = CFG.risk("profit_lock_pts", 150)
PROFIT_LOCK_TRAIL_TF    = "3minute"

# v16: RSI constants kept for shadow analysis
RSI_1M_LOW         = 30
RSI_1M_HIGH_NORMAL = 60
RSI_1M_HIGH_STRONG = 70
RSI_1M_HIGH        = RSI_1M_HIGH_NORMAL

LOOKBACK_1M = CFG.lookback("1m")
LOOKBACK_3M = CFG.lookback("3m")
LOOKBACK_5M = CFG.lookback("5m")

TRADE_START_HOUR  = CFG.market_hours("trade_start_hour", 9)
TRADE_START_MIN   = CFG.market_hours("trade_start_min", 15)
ENTRY_CUTOFF_HOUR = CFG.market_hours("entry_cutoff_hour", 15)
ENTRY_CUTOFF_MIN  = CFG.market_hours("entry_cutoff_min", 10)
MARKET_OPEN_HOUR  = CFG.market_hours("open_hour", 9)
MARKET_OPEN_MIN   = CFG.market_hours("open_min", 15)
MARKET_CLOSE_HOUR = CFG.market_hours("close_hour", 15)
MARKET_CLOSE_MIN  = CFG.market_hours("close_min", 30)

WS_RECONNECT_DELAY = CFG.ws_reconnect_delay()
TICK_STALE_SECS    = CFG.ws_tick_stale_secs()

STATE_PERSIST_FIELDS = [
    # Position
    "in_trade", "symbol", "token", "direction", "strike", "expiry",
    "entry_price", "entry_time", "qty", "lot_count",
    # Exit state (v15.0: band-based)
    "peak_pnl", "trough_pnl", "candles_held",
    # v15.0 entry context + band trail
    "entry_mode", "entry_ema9_high", "entry_ema9_low",
    "entry_band_position", "entry_body_pct",
    "current_ema9_high", "current_ema9_low", "last_band_check_ts",
    "score_at_entry", "other_token",
    # v15.2 entry context (straddle + VWAP)
    "entry_straddle_delta", "entry_straddle_threshold", "entry_straddle_period",
    "entry_atm_strike", "entry_band_width",
    "entry_spot_vwap", "entry_spot_vs_vwap", "entry_vwap_bonus",
    "entry_straddle_info",
    # v16.0 Batch 7 band slope + context tag
    "entry_bands_state", "entry_context_tag",
    "ema9_high_slope_5c", "ema9_low_slope_5c",
    "current_bands_state", "current_ema9_high_slope", "current_ema9_low_slope",
    "_last_context_ts",
    # v15.2.5 velocity stall tracking
    "peak_history", "last_peak_candle_ts", "current_velocity",
    # v15.2.5 BUG-J sentinel: one-shot peak_history backfill on startup
    "_peak_history_backfilled",
    # v15.2.5 BUG-V sentinel: daily lab cleanup date guard
    "_last_cleanup_date",
    # v15.2.5 pre-entry alert toggle + rate-limit history
    "pre_entry_alerts_enabled", "alert_history",
    # v16.0 ratchet state
    "active_ratchet_tier", "active_ratchet_sl",
    # v15.1 BE+2 lock (legacy)
    "be2_active", "be2_level",
    # Last exit memory
    "last_exit_time", "last_exit_direction", "last_exit_peak",
    "last_exit_reason",
    # Daily
    "daily_trades", "daily_losses", "daily_pnl",
    "consecutive_losses", "profit_locked",
    # Bot control
    "paused", "prev_close",
    # v15.2.5 BUG-A: persist _exit_failed so a crash mid-manual-resolution
    # doesn't silently clear the block on restart
    "_exit_failed",
    # Legacy compat (kept for VRL_TRADE SL-M + restart resume)
    "phase1_sl", "exit_phase", "lot1_active", "lot2_active", "lots_split",
]

def get_session_block(hour: int, minute: int) -> str:
    mins = hour * 60 + minute
    if   mins < 10 * 60: return "OPEN"
    elif mins < 12 * 60: return "MORNING"
    elif mins < 14 * 60: return "AFTERNOON"
    else:                return "LATE"

def ensure_dirs():
    for d in [LIVE_LOG_DIR, LAB_LOG_DIR, FLOW_LOG_DIR, STATE_DIR,
              OPTIONS_3MIN_DIR, OPTIONS_1MIN_DIR, SPOT_DIR,
              REPORTS_DIR, SESSIONS_DIR, BACKUP_DIR,
              AUTH_LOG_DIR, WEB_LOG_DIR, HEALTH_LOG_DIR,
              ZONES_LOG_DIR, ML_LOG_DIR, ERROR_LOG_DIR]:
        os.makedirs(d, exist_ok=True)


class _ErrorMirrorHandler(logging.Handler):
    """Copies ERROR+ messages to ~/logs/errors/YYYY-MM-DD.log"""
    def __init__(self):
        super().__init__(level=logging.ERROR)
        self.setFormatter(logging.Formatter(
            "%(asctime)s | %(name)-12s | %(levelname)-8s | %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S"))

    def emit(self, record):
        try:
            os.makedirs(ERROR_LOG_DIR, exist_ok=True)
            today = date.today().strftime("%Y-%m-%d")
            path = os.path.join(ERROR_LOG_DIR, today + ".log")
            with open(path, "a") as f:
                f.write(self.format(record) + "\n")
        except Exception:
            pass


def _dated_log_path(log_dir: str) -> str:
    """Returns log path like ~/logs/live/2026-04-01.log"""
    today = date.today().strftime("%Y-%m-%d")
    return os.path.join(log_dir, today + ".log")


def setup_logger(name: str, log_file: str, level=logging.DEBUG) -> logging.Logger:
    os.makedirs(os.path.dirname(log_file), exist_ok=True)
    lg = logging.getLogger(name)
    if lg.handlers:
        return lg
    lg.setLevel(level)
    fmt = logging.Formatter("%(asctime)s | %(levelname)-8s | %(message)s",
                            datefmt="%Y-%m-%d %H:%M:%S")
    fh = TimedRotatingFileHandler(log_file, when="midnight", backupCount=30)
    fh.suffix = "%Y-%m-%d"
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(fmt)
    lg.addHandler(fh)
    ch = logging.StreamHandler()
    ch.setLevel(logging.INFO)
    ch.setFormatter(fmt)
    lg.addHandler(ch)
    # Mirror errors to central error log
    lg.addHandler(_ErrorMirrorHandler())
    return lg


def setup_dated_logger(name: str, log_dir: str, level=logging.DEBUG) -> logging.Logger:
    """Create a logger that writes to ~/logs/<category>/YYYY-MM-DD.log"""
    os.makedirs(log_dir, exist_ok=True)
    lg = logging.getLogger(name)
    if lg.handlers:
        return lg
    lg.setLevel(level)
    fmt = logging.Formatter("%(asctime)s | %(levelname)-8s | %(message)s",
                            datefmt="%Y-%m-%d %H:%M:%S")
    log_file = _dated_log_path(log_dir)
    fh = TimedRotatingFileHandler(log_file, when="midnight", backupCount=30)
    fh.suffix = "%Y-%m-%d"
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(fmt)
    lg.addHandler(fh)
    ch = logging.StreamHandler()
    ch.setLevel(logging.INFO)
    ch.setFormatter(fmt)
    lg.addHandler(ch)
    # Mirror errors to central error log
    lg.addHandler(_ErrorMirrorHandler())
    return lg


def audit_log_paths() -> dict:
    """v15.2.5 BUG-DL3: one-shot report of which log directories exist.

    Called once from VRL_MAIN startup. Does NOT create anything —
    just inspects disk state so the operator can see at a glance
    whether a category has ever been populated. Returns a dict
    mapping category name to {path, exists, file_count}. Also
    logs INFO lines for present dirs and WARNING lines for missing
    ones.
    """
    categories = {
        "live":   LIVE_LOG_DIR,
        "lab":    LAB_LOG_DIR,
        "auth":   AUTH_LOG_DIR,
        "web":    WEB_LOG_DIR,
        "health": HEALTH_LOG_DIR,
        "zones":  ZONES_LOG_DIR,
        "ml":     ML_LOG_DIR,
        "errors": ERROR_LOG_DIR,
        "flow":   FLOW_LOG_DIR,
    }
    result = {}
    for cat, path in categories.items():
        exists = os.path.isdir(path)
        try:
            n_files = (
                len([f for f in os.listdir(path)
                     if os.path.isfile(os.path.join(path, f))])
                if exists else 0)
        except Exception:
            n_files = -1
        result[cat] = {"path": path, "exists": exists, "file_count": n_files}
        if exists:
            logger.info("[LOGPATH] " + cat + ": " + path
                        + " (" + str(n_files) + " files)")
        else:
            logger.warning("[LOGPATH] " + cat + ": " + path
                           + " MISSING — no logs will zip under this "
                           "category until it's created")
    return result


def collect_logs_for_date(target_date: str = None) -> list:
    """
    Collect all log/data files for a given date (YYYY-MM-DD format).
    Returns list of (filepath, arcname) tuples for zipping.
    If target_date is None, uses today.
    """
    if target_date is None:
        target_date = date.today().strftime("%Y-%m-%d")
    date_compact = target_date.replace("-", "")  # 20260401

    files = []

    # Log directories — look for date-stamped files
    log_dirs = {
        "live": LIVE_LOG_DIR,
        "lab": LAB_LOG_DIR,
        "auth": AUTH_LOG_DIR,
        "web": WEB_LOG_DIR,
        "health": HEALTH_LOG_DIR,
        "zones": ZONES_LOG_DIR,
        "ml": ML_LOG_DIR,
        "errors": ERROR_LOG_DIR,
        "flow": FLOW_LOG_DIR,
    }
    for category, dirpath in log_dirs.items():
        if not os.path.isdir(dirpath):
            continue
        for fname in os.listdir(dirpath):
            fpath = os.path.join(dirpath, fname)
            if not os.path.isfile(fpath):
                continue
            # Match: YYYY-MM-DD.log, vrl_live.log.YYYY-MM-DD, or *_YYYYMMDD.*
            if (target_date in fname or date_compact in fname
                    or fname == "vrl_live.log" or fname == "vrl_lab.log"):
                arcname = "logs/" + category + "/" + fname
                files.append((fpath, arcname))

    # Trade log
    if os.path.isfile(TRADE_LOG_PATH):
        files.append((TRADE_LOG_PATH, "data/vrl_trade_log.csv"))

    # Lab data — option candles, spot, scans
    data_patterns = [
        (OPTIONS_3MIN_DIR, "nifty_option_3min_" + date_compact + ".csv", "data/options_3min/"),
        (OPTIONS_1MIN_DIR, "nifty_option_1min_" + date_compact + ".csv", "data/options_1min/"),
        (OPTIONS_1MIN_DIR, "nifty_option_5min_" + date_compact + ".csv", "data/options_1min/"),
        (OPTIONS_1MIN_DIR, "nifty_option_15min_" + date_compact + ".csv", "data/options_1min/"),
        (OPTIONS_1MIN_DIR, "nifty_signal_scan_" + date_compact + ".csv", "data/scans/"),
        (SPOT_DIR, "nifty_spot_1min_" + date_compact + ".csv", "data/spot/"),
        (SPOT_DIR, "nifty_spot_5min_" + date_compact + ".csv", "data/spot/"),
        (SPOT_DIR, "nifty_spot_15min_" + date_compact + ".csv", "data/spot/"),
        (SPOT_DIR, "nifty_spot_60min_" + date_compact + ".csv", "data/spot/"),
    ]
    for dirpath, fname, arc_prefix in data_patterns:
        fpath = os.path.join(dirpath, fname)
        if os.path.isfile(fpath):
            files.append((fpath, arc_prefix + fname))

    # Reports
    if os.path.isdir(REPORTS_DIR):
        for fname in os.listdir(REPORTS_DIR):
            if date_compact in fname or target_date in fname:
                fpath = os.path.join(REPORTS_DIR, fname)
                if os.path.isfile(fpath):
                    files.append((fpath, "data/reports/" + fname))

    # State snapshot
    if os.path.isfile(STATE_FILE_PATH):
        files.append((STATE_FILE_PATH, "state/vrl_live_state.json"))

    # Config snapshot
    config_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.yaml")
    if os.path.isfile(config_path):
        files.append((config_path, "state/config.yaml"))

    # Zones
    zones_path = os.path.join(STATE_DIR, "vrl_zones.json")
    if os.path.isfile(zones_path):
        files.append((zones_path, "state/vrl_zones.json"))

    return files


def create_daily_zip(target_date: str = None) -> str:
    """
    Create a zip of all logs + data for a date.
    Returns the zip file path, or empty string on failure.
    """
    if target_date is None:
        target_date = date.today().strftime("%Y-%m-%d")

    files = collect_logs_for_date(target_date)
    if not files:
        return ""

    zip_name = "vrl_" + target_date + ".zip"
    zip_path = os.path.join(STATE_DIR, zip_name)

    try:
        import zipfile
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for fpath, arcname in files:
                try:
                    zf.write(fpath, arcname)
                except Exception:
                    pass
        return zip_path
    except Exception as e:
        logger.error("[DATA] Daily zip failed: " + str(e))
        return ""

logger = logging.getLogger("vrl_live")

_kite             = None
_account_info     = {}
_token_cache      = {}
_token_cache_lock = threading.Lock()
_nfo_instruments       = None
_nfo_instruments_lock  = threading.Lock()
_nfo_instruments_date  = None
_ticker           = None
_ticks            = {}
_tick_lock        = threading.Lock()
_subscribed       = set()
_subscribed_lock  = threading.Lock()
_ws_connected     = False

# ── BUG-N1 v15.2.5: auth-rejection backoff ───────────────────
# When Kite's nightly 03:30 session invalidation kills the token,
# every historical_data / quote / LTP call raises "Incorrect
# api_key or access_token". Without a guard, the bot retries
# every 1-2 seconds for hours, flooding the log with 13K+ warnings.
# This flag stops all retries until VRL_AUTH refreshes the token
# and calls notify_auth_refreshed().
_auth_rejected = False
_auth_rejected_lock = threading.Lock()


def _is_auth_rejected() -> bool:
    with _auth_rejected_lock:
        return _auth_rejected


def _set_auth_rejected():
    global _auth_rejected
    with _auth_rejected_lock:
        if not _auth_rejected:
            logger.warning("[DATA] Auth token rejected — pausing retries "
                           "until re-auth via VRL_AUTH.")
        _auth_rejected = True


# ── BUG-N3 v15.2.5: cross-module "trade was taken" signal ────
# VRL_MAIN sets this after a successful entry; VRL_LAB reads it
# when building the next signal_scans row and writes trade_taken=1.
_trade_taken_lock = threading.Lock()
_trade_taken_direction = ""    # "" = no trade pending, "CE" or "PE"
_trade_taken_ts        = ""    # ISO timestamp of the entry


def mark_trade_taken(direction: str, ts: str = ""):
    """Called by VRL_MAIN after a successful entry."""
    global _trade_taken_direction, _trade_taken_ts
    with _trade_taken_lock:
        _trade_taken_direction = direction
        _trade_taken_ts = ts or datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def consume_trade_taken(direction: str) -> bool:
    """Called by VRL_LAB when building a fired scan row. Returns True
    and resets the flag if a trade was taken for this direction."""
    global _trade_taken_direction, _trade_taken_ts
    with _trade_taken_lock:
        if _trade_taken_direction == direction:
            _trade_taken_direction = ""
            _trade_taken_ts = ""
            return True
    return False


# ── BUG-N12 v15.2.5: active trade token for LAB persistence ──
# VRL_MAIN sets this on entry; VRL_LAB reads it to ensure the
# traded strike's candles are always written regardless of ATM drift.
_active_trade_lock = threading.Lock()
_active_trade = None   # None or {"token_ce": int, "token_pe": int, "strike": int, "direction": str}


def set_active_trade(strike: int, direction: str, token_ce: int = 0,
                     token_pe: int = 0):
    """Called by VRL_MAIN on successful entry."""
    global _active_trade
    with _active_trade_lock:
        _active_trade = {
            "strike": int(strike), "direction": str(direction),
            "token_ce": int(token_ce), "token_pe": int(token_pe),
        }
    logger.info("[DATA] Active trade set: strike=" + str(strike)
                + " dir=" + direction)


def clear_active_trade():
    """Called by VRL_MAIN on trade exit."""
    global _active_trade
    with _active_trade_lock:
        _active_trade = None


def get_active_trade() -> dict:
    """Called by VRL_LAB to get the active trade's tokens. Returns
    None if no trade is open."""
    with _active_trade_lock:
        return dict(_active_trade) if _active_trade else None


def notify_auth_refreshed():
    """Called by VRL_AUTH on successful login / token refresh.
    Resets the auth-rejection flag so historical_data and WS
    resume normal operation."""
    global _auth_rejected
    with _auth_rejected_lock:
        if _auth_rejected:
            logger.info("[DATA] Auth refreshed — resuming historical_data "
                        "and WS operations.")
        _auth_rejected = False

def init(kite_instance):
    global _kite
    _kite = kite_instance


def fetch_account_info(kite=None):
    """Fetch profile + margins once at startup and cache."""
    global _account_info
    k = kite or _kite
    if k is None:
        return _account_info
    try:
        profile = k.profile()
        margins = k.margins(segment="equity")
        avail = margins.get("available", {})
        used = margins.get("utilised", {})
        _account_info = {
            "name": profile.get("user_name", ""),
            "user_id": profile.get("user_id", ""),
            "email": profile.get("email", ""),
            "broker": "Zerodha",
            "available_margin": round(float(avail.get("live_balance", 0)), 2),
            "used_margin": round(float(used.get("debits", 0)), 2),
            "total_balance": round(float(margins.get("net", 0)), 2),
        }
        logger.info("[DATA] Account: " + _account_info["name"]
                     + " bal=" + str(_account_info["total_balance"]))
    except Exception as e:
        logger.warning("[DATA] Account fetch: " + str(e))
    return _account_info


def get_account_info():
    return _account_info


def refresh_margin(kite=None):
    """Refresh just margin numbers — call after each trade."""
    global _account_info
    k = kite or _kite
    if k is None:
        return
    try:
        margins = k.margins(segment="equity")
        avail = margins.get("available", {})
        used = margins.get("utilised", {})
        _account_info["available_margin"] = round(float(avail.get("live_balance", 0)), 2)
        _account_info["used_margin"] = round(float(used.get("debits", 0)), 2)
        _account_info["total_balance"] = round(float(margins.get("net", 0)), 2)
    except Exception:
        pass

def _on_ticks(ws, ticks):
    with _tick_lock:
        for tick in ticks:
            token = tick.get("instrument_token")
            ltp   = tick.get("last_price", 0)
            if token and ltp:
                _ticks[token] = {"ltp": float(ltp), "ts": time.time()}

def _on_connect(ws, response):
    global _ws_connected
    _ws_connected = True
    logger.info("[WS] Connected")
    with _subscribed_lock:
        if _subscribed:
            ws.subscribe(list(_subscribed))
            ws.set_mode(ws.MODE_FULL, list(_subscribed))

def _on_close(ws, code, reason):
    global _ws_connected, _ticker
    _ws_connected = False
    reason_str = str(reason or "")
    logger.warning("[WS] Closed: " + str(code) + " " + reason_str)
    # BUG-N2: if Kite returned 403, the token is dead. Don't let
    # KiteTicker's auto-reconnect hammer a dead endpoint every minute.
    # Set the auth-rejection flag so historical_data also pauses.
    # notify_auth_refreshed() (from VRL_AUTH at 08:00) re-enables both.
    if "403" in reason_str or "Forbidden" in reason_str:
        logger.warning("[WS] 403 Forbidden — auth required, stopping WS "
                       "reconnect. Will resume after next VRL_AUTH refresh.")
        _set_auth_rejected()
        try:
            if _ticker:
                _ticker.close()
        except Exception:
            pass

def _on_error(ws, code, reason):
    reason_str = str(reason or "")
    logger.error("[WS] Error: " + str(code) + " " + reason_str)
    if "403" in reason_str or "Forbidden" in reason_str:
        _set_auth_rejected()

def _on_reconnect(ws, attempts):
    logger.info("[WS] Reconnecting attempt " + str(attempts))

def start_websocket():
    global _ticker
    if _kite is None:
        raise RuntimeError("Call init(kite) before start_websocket()")
    _ticker = KiteTicker(KITE_API_KEY, _kite.access_token)
    _ticker.on_ticks     = _on_ticks
    _ticker.on_connect   = _on_connect
    _ticker.on_close     = _on_close
    _ticker.on_error     = _on_error
    _ticker.on_reconnect = _on_reconnect
    _ticker.connect(threaded=True, disable_ssl_verification=False)
    logger.info("[WS] Ticker started")

def subscribe_tokens(tokens: list):
    global _subscribed
    with _subscribed_lock:
        new = set(int(t) for t in tokens if t)
        _subscribed.update(new)
        if _ticker and _ws_connected:
            _ticker.subscribe(list(new))
            _ticker.set_mode(_ticker.MODE_FULL, list(new))
    logger.info("[WS] Subscribed: " + str(new))

def unsubscribe_tokens(tokens: list):
    global _subscribed
    with _subscribed_lock:
        rem = set(int(t) for t in tokens if t)
        _subscribed -= rem
        if _ticker and _ws_connected:
            try:
                _ticker.unsubscribe(list(rem))
            except Exception:
                pass
    logger.info("[WS] Unsubscribed: " + str(rem))

def get_ltp(token) -> float:
    if token is None:
        return 0.0
    with _tick_lock:
        entry = _ticks.get(int(token))
    if not entry:
        return 0.0
    age = time.time() - entry["ts"]
    if age > TICK_STALE_SECS:
        if is_market_open():
            logger.warning("[DATA] Stale tick token=" + str(token)
                           + " age=" + str(round(age, 1)) + "s")
        return 0.0
    return entry["ltp"]


def get_spot_ltp() -> float:
    """v15.2: convenience helper — spot LTP via WebSocket tick cache."""
    return get_ltp(NIFTY_SPOT_TOKEN)

def is_tick_live(token) -> bool:
    with _tick_lock:
        entry = _ticks.get(int(token) if token else 0)
    if not entry:
        return False
    return (time.time() - entry["ts"]) < TICK_STALE_SECS


_last_reconnect_attempt = 0
_ws_autoheal_callback = None  # v13.10: optional Telegram alert hook

def set_autoheal_callback(fn):
    """Register a callback invoked on WS auto-heal events (e.g. Telegram alert)."""
    global _ws_autoheal_callback
    _ws_autoheal_callback = fn

def check_and_reconnect():
    """
    v13.10 (BUG-029): Auto-heal stale WebSocket. If spot tick is 3+ min stale during
    market hours, re-authenticate Kite and restart WebSocket. Rate limited to 1 per 10min.
    Called from strategy loop every cycle.
    """
    global _last_reconnect_attempt, _kite, _ticker
    if not is_market_open():
        return
    # BUG-N2: if auth is known-rejected, auto-heal can't help — VRL_AUTH
    # must refresh the token first. Skip to avoid pointless reconnect.
    if _is_auth_rejected():
        return
    # Check if spot tick is stale (v13.10: tightened from 5min to 3min)
    with _tick_lock:
        spot_entry = _ticks.get(NIFTY_SPOT_TOKEN)
    if spot_entry and (time.time() - spot_entry["ts"]) < 180:
        return  # tick is fresh (< 3 min), no action needed
    # v13.10: rate limit 1 auto-heal per 10 minutes to prevent loops
    if time.time() - _last_reconnect_attempt < 600:
        return
    _last_reconnect_attempt = time.time()
    logger.warning("[DATA] Spot tick stale 3+ min — attempting re-auth + WS reconnect")
    try:
        if _ws_autoheal_callback:
            _ws_autoheal_callback("\u26a0\ufe0f WebSocket auto-healing after stale tick (3min+)")
    except Exception:
        pass
    try:
        from VRL_AUTH import get_kite
        new_kite = get_kite()
        if not new_kite:
            logger.error("[DATA] Re-auth returned None")
            return
        _kite = new_kite
        # Stop old ticker safely
        try:
            if _ticker:
                _ticker.close()
                time.sleep(1)
        except Exception:
            pass
        _ticker = None
        time.sleep(2)
        # Start fresh ticker with new token
        try:
            start_websocket()
        except Exception as _ws_err:
            logger.error("[DATA] WS restart failed: " + str(_ws_err))
            return
        # Wait for connection before subscribing
        time.sleep(3)
        with _subscribed_lock:
            if _subscribed and _ticker and _ws_connected:
                try:
                    _ticker.subscribe(list(_subscribed))
                    _ticker.set_mode(_ticker.MODE_FULL, list(_subscribed))
                except Exception:
                    pass
        logger.info("[DATA] Re-auth + WS reconnect successful")
    except Exception as e:
        logger.error("[DATA] Re-auth failed: " + str(e))

def get_vix() -> float:
    ltp = get_ltp(INDIA_VIX_TOKEN)
    if ltp > 0:
        return ltp
    # BUG-N1: skip REST fallback if auth is rejected
    if _is_auth_rejected():
        return 0.0
    if _kite is not None:
        try:
            quote = _kite.quote(["NSE:INDIA VIX"])
            vix   = quote.get("NSE:INDIA VIX", {}).get("last_price", 0)
            if vix and vix > 0:
                return float(vix)
        except Exception as e:
            err_str = str(e).lower()
            if "incorrect api_key" in err_str or "access_token" in err_str:
                _set_auth_rejected()
            else:
                logger.debug("[DATA] VIX quote fallback failed: " + str(e))
    return 0.0

# ═══════════════════════════════════════════════════════════════
#  TRADING HOLIDAYS (NSE 2026) — add new ones here
#  Format: "YYYY-MM-DD" strings. Check local date, not UTC.
# ═══════════════════════════════════════════════════════════════
TRADING_HOLIDAYS = {
    "2026-01-26",  # Republic Day
    "2026-02-19",  # Mahashivratri
    "2026-03-05",  # Holi
    "2026-03-27",  # Eid-ul-Fitr
    "2026-04-03",  # Good Friday
    "2026-04-14",  # Ambedkar Jayanti / Dr B R Ambedkar Jayanti
    "2026-05-01",  # Maharashtra Day
    "2026-05-27",  # Bakri Eid
    "2026-08-15",  # Independence Day
    "2026-08-27",  # Ganesh Chaturthi
    "2026-10-02",  # Gandhi Jayanti
    "2026-10-21",  # Diwali Laxmi Pujan (Muhurat special session)
    "2026-11-05",  # Guru Nanak Jayanti
    "2026-12-25",  # Christmas
}

def is_trading_day(now: datetime = None) -> bool:
    """True only on weekdays that are NOT NSE holidays."""
    if now is None:
        now = now_ist()
    if now.weekday() >= 5:
        return False
    return now.strftime("%Y-%m-%d") not in TRADING_HOLIDAYS


def is_market_open() -> bool:
    now = now_ist()
    if not is_trading_day(now):
        return False
    start = now.replace(hour=MARKET_OPEN_HOUR,  minute=MARKET_OPEN_MIN,  second=0, microsecond=0)
    end   = now.replace(hour=MARKET_CLOSE_HOUR, minute=MARKET_CLOSE_MIN, second=0, microsecond=0)
    return start <= now <= end

def is_trading_window(now: datetime = None) -> bool:
    if now is None:
        now = now_ist()
    if not is_market_open():
        return False
    start = now.replace(hour=TRADE_START_HOUR, minute=TRADE_START_MIN, second=0, microsecond=0)
    end   = now.replace(hour=ENTRY_CUTOFF_HOUR, minute=ENTRY_CUTOFF_MIN, second=0, microsecond=0)
    return start <= now <= end

def _get_nfo_instruments(kite=None):
    """Fetch NFO instruments once per day, cached."""
    global _nfo_instruments, _nfo_instruments_date
    from datetime import date as _d
    today = _d.today()
    with _nfo_instruments_lock:
        if _nfo_instruments is not None and _nfo_instruments_date == today:
            return _nfo_instruments
    k = kite or _kite
    if k is None:
        return []
    instruments = k.instruments("NFO")
    with _nfo_instruments_lock:
        _nfo_instruments = instruments
        _nfo_instruments_date = today
    return instruments

# BUG-N8 v15.2.5: per-date cache for lot_size.
# get_lot_size() called 28× per day via D.get_lot_size() — each call
# invokes _get_nfo_instruments(kite) which returns thousands of rows.
# Cache the result per date so only the FIRST call per day hits Kite.
_lot_size_cache = {}  # {"2026-04-17": 65}
_lot_size_cache_lock = threading.Lock()

def get_lot_size(kite=None) -> int:
    k = kite or _kite
    if k is None:
        return LOT_SIZE_BASE
    today_iso = date.today().isoformat()
    with _lot_size_cache_lock:
        if today_iso in _lot_size_cache:
            return _lot_size_cache[today_iso]
    # Cache miss — fetch from broker (first call of the day).
    try:
        instruments = _get_nfo_instruments(k)
        for inst in instruments:
            if (inst.get("name") == "NIFTY"
                    and inst.get("instrument_type") == "CE"
                    and inst.get("lot_size", 0) > 0):
                lot = int(inst["lot_size"])
                logger.info("[DATA] Lot size from broker: " + str(lot)
                            + " (cached for " + today_iso + ")")
                with _lot_size_cache_lock:
                    _lot_size_cache[today_iso] = lot
                    # Evict stale dates (keep only today).
                    for k_date in list(_lot_size_cache.keys()):
                        if k_date != today_iso:
                            del _lot_size_cache[k_date]
                return lot
    except Exception as e:
        logger.warning("[DATA] Lot size fetch failed: " + str(e))
    return LOT_SIZE_BASE

# ── Historical data cache — avoids duplicate API calls within same minute ──
_hist_cache = {}
_hist_cache_lock = threading.Lock()
_HIST_CACHE_TTL = 30   # seconds
_HIST_CACHE_MAX = 256  # hard cap on entries — prevents unbounded growth

def _hist_cache_key(token: int, interval: str, lookback: int) -> str:
    return str(token) + "|" + interval + "|" + str(lookback)

def _hist_cache_get(key: str):
    with _hist_cache_lock:
        entry = _hist_cache.get(key)
        if entry and (time.time() - entry["ts"]) < _HIST_CACHE_TTL:
            return entry["df"].copy()
    return None

def _hist_cache_put(key: str, df):
    with _hist_cache_lock:
        _hist_cache[key] = {"df": df.copy(), "ts": time.time()}
        # Evict by age first
        now = time.time()
        stale = [k for k, v in _hist_cache.items() if now - v["ts"] > _HIST_CACHE_TTL * 2]
        for k in stale:
            del _hist_cache[k]
        # Hard cap: drop oldest entries if still over max
        if len(_hist_cache) > _HIST_CACHE_MAX:
            ordered = sorted(_hist_cache.items(), key=lambda kv: kv[1]["ts"])
            for k, _v in ordered[:len(_hist_cache) - _HIST_CACHE_MAX]:
                del _hist_cache[k]

def get_historical_data(token: int, interval: str, lookback: int,
                        today_only: bool = False) -> pd.DataFrame:
    if _kite is None:
        return pd.DataFrame()
    # Check cache first — avoids duplicate API calls within same 30s window
    cache_key = _hist_cache_key(token, interval, lookback)
    cached = _hist_cache_get(cache_key)
    if cached is not None:
        return cached
    min_from = datetime.now() - timedelta(days=3)
    minutes_per_candle = {
        "minute": 1, "3minute": 3, "5minute": 5,
        "15minute": 15, "30minute": 30, "60minute": 60,
    }.get(interval, 1)
    total_minutes  = lookback * minutes_per_candle * 2.5
    candidate_from = datetime.now() - timedelta(minutes=int(total_minutes) + 60)
    from_dt = min(candidate_from, min_from)
    to_dt   = datetime.now()
    raw   = None
    # BUG-N1: skip entirely if auth is known-rejected (03:30 session kill).
    if _is_auth_rejected():
        return pd.DataFrame()
    for attempt in range(2):
        try:
            raw = _kite.historical_data(
                instrument_token=int(token), from_date=from_dt, to_date=to_dt,
                interval=interval, continuous=False, oi=False)
            break
        except Exception as e:
            err_str = str(e).lower()
            if "incorrect api_key" in err_str or "access_token" in err_str:
                _set_auth_rejected()
                return pd.DataFrame()
            logger.warning("[DATA] historical_data attempt " + str(attempt+1)
                           + " token=" + str(token) + ": " + str(e))
            if attempt < 1:
                time.sleep(1)
    if not raw:
        return pd.DataFrame()
    df = pd.DataFrame(raw)
    df.rename(columns={"date": "timestamp"}, inplace=True)
    df.set_index("timestamp", inplace=True)
    df = df[["open", "high", "low", "close", "volume"]].copy()
    df = df.apply(pd.to_numeric, errors="coerce")
    df.dropna(inplace=True)
    _hist_cache_put(cache_key, df)
    return df

def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty or len(df) < 3:
        return df
    df         = df.copy()
    df["EMA_9"]  = df["close"].ewm(span=9,  adjust=False).mean()
    df["EMA_21"] = df["close"].ewm(span=21, adjust=False).mean()
    delta    = df["close"].diff()
    gain     = delta.clip(lower=0)
    loss     = (-delta).clip(lower=0)
    avg_gain = gain.ewm(com=13, adjust=False).mean()
    avg_loss = loss.ewm(com=13, adjust=False).mean()
    rs       = avg_gain / avg_loss.replace(0, float("nan"))
    df["RSI"] = (100 - (100 / (1 + rs))).fillna(50)
    # v15.0: EMA9 bands of high and low — for option band-breakout strategy
    df["ema9_high"] = df["high"].ewm(span=9, adjust=False).mean().round(2)
    df["ema9_low"]  = df["low"].ewm(span=9, adjust=False).mean().round(2)
    return df


def get_option_3min(token: int, lookback: int = 10) -> pd.DataFrame:
    """v15.0: Fetch option 3-min OHLC + EMA9 bands. Returns DataFrame with
    columns: open, high, low, close, volume, EMA_9, EMA_21, RSI, ema9_high, ema9_low.
    The last row (iloc[-1]) is the live in-progress candle. iloc[-2] is the
    last CLOSED candle. iloc[-3] is the candle before that."""
    df = get_historical_data(token, "3minute", lookback)
    if df.empty:
        return df
    return add_indicators(df)


# ═══════════════════════════════════════════════════════════════
#  v15.2 STRADDLE EXPANSION HELPERS — used by Gate 7
#  Read live ATM CE+PE 3-min closes and compare current vs N min ago.
#  Returns None on missing data so the gate can reject explicitly.
# ═══════════════════════════════════════════════════════════════

def get_atm_straddle(timestamp=None, atm_strike: int = 0) -> float:
    """Return ATM_CE_close + ATM_PE_close at (or just before) `timestamp`.
    timestamp=None → live (use the most recent CLOSED 3-min candle of each leg).
    Returns None if either leg's data is missing."""
    if not atm_strike:
        return None
    expiry = None
    try:
        expiry = get_nearest_expiry(_kite)
    except Exception:
        expiry = None
    if expiry is None or _kite is None:
        return None
    tokens = get_option_tokens(_kite, atm_strike, expiry) or {}
    ce_tok = (tokens.get("CE") or {}).get("token")
    pe_tok = (tokens.get("PE") or {}).get("token")
    if not ce_tok or not pe_tok:
        return None
    try:
        ce_df = get_historical_data(int(ce_tok), "3minute", 30)
        pe_df = get_historical_data(int(pe_tok), "3minute", 30)
        if ce_df is None or pe_df is None or ce_df.empty or pe_df.empty:
            return None

        def _closest(df, ts):
            """Pick the last close at-or-before ts. v15.2.3: tz-safe.
            Kite returns a tz-AWARE (IST) DatetimeIndex; callers pass a
            tz-NAIVE wall-clock datetime. Normalize both sides to naive
            so pandas doesn't raise TypeError on the comparison."""
            if ts is None:
                idx = -2 if len(df) >= 2 else -1
                return float(df.iloc[idx]["close"])
            try:
                df_idx = df.index
                # Strip tz from the DataFrame index if present
                if getattr(df_idx, "tz", None) is not None:
                    df_idx = df_idx.tz_localize(None)
                # Strip tz from ts if present
                if hasattr(ts, "tzinfo") and ts.tzinfo is not None:
                    ts = ts.replace(tzinfo=None)
                mask_pos = [i for i, t in enumerate(df_idx) if t <= ts]
                if not mask_pos:
                    return None
                return float(df.iloc[mask_pos[-1]]["close"])
            except Exception as _e:
                logger.debug("[STRADDLE] _closest err: " + str(_e))
                return None

        ce_close = _closest(ce_df, timestamp)
        pe_close = _closest(pe_df, timestamp)
        if ce_close is None or pe_close is None:
            return None
        return round(ce_close + pe_close, 2)
    except Exception as e:
        logger.debug("[STRADDLE] get_atm_straddle err: " + str(e))
        return None


def get_straddle_delta(atm_strike: int, lookback_minutes: int = 15) -> float:
    """Straddle expansion = straddle(now) - straddle(now - lookback_minutes).

    v15.2.3 changes:
    - Timezone-safe lookup (see `_closest` in get_atm_straddle).
    - Graceful fallback: if the strict lookback is unavailable (data gap,
      ATM just shifted, weekend boundary), try progressively shorter
      lookbacks [15, 12, 9, 6] before giving up.
    - INFO-level diagnostic log so 100%-NA failures are visible without
      redeploy (the old code returned None with no trace).
    """
    if not atm_strike:
        return None
    now_dt = now_ist().replace(tzinfo=None)
    current = get_atm_straddle(None, atm_strike)
    if current is None:
        logger.info("[STRADDLE] atm=" + str(atm_strike)
                    + " current=NA → delta=None")
        return None

    tried = []
    for lb in (int(lookback_minutes), 12, 9, 6):
        if lb <= 0 or lb in tried:
            continue
        tried.append(lb)
        prior_dt = now_dt - timedelta(minutes=lb)
        # Snap back to the 3-min boundary that line up with option_3min rows.
        prior_dt = prior_dt.replace(
            minute=(prior_dt.minute // 3) * 3, second=0, microsecond=0)
        prior = get_atm_straddle(prior_dt, atm_strike)
        if prior is not None:
            delta = round(current - prior, 2)
            logger.info("[STRADDLE] atm=" + str(atm_strike)
                        + " now=" + str(round(current, 1))
                        + " @-" + str(lb) + "min=" + str(round(prior, 1))
                        + " Δ=" + "{:+.1f}".format(delta)
                        + " prior_ts=" + prior_dt.strftime("%H:%M"))
            return delta

    logger.info("[STRADDLE] atm=" + str(atm_strike)
                + " current=" + str(round(current, 1))
                + " prior NA for lookbacks=" + str(tried) + " → delta=None")
    return None


# ═══════════════════════════════════════════════════════════════
#  v15.2 SPOT VWAP — display-only confluence indicator
#  Cumulative session VWAP from 5-min spot candles, market open → now.
# ═══════════════════════════════════════════════════════════════

def get_spot_5min(today: bool = True, end=None) -> list:
    """Return today's spot 5-min candles up to `end` (or now). List of dicts."""
    try:
        df = get_historical_data(NIFTY_SPOT_TOKEN, "5minute", 80)
    except Exception:
        return []
    if df is None or df.empty:
        return []
    df = df.copy()
    today_str = date.today().strftime("%Y-%m-%d")
    df["_date"] = df.index.map(
        lambda x: x.strftime("%Y-%m-%d") if hasattr(x, "strftime") else str(x)[:10])
    if today:
        df = df[df["_date"] == today_str]
    if end is not None:
        try:
            df = df[df.index <= end]
        except Exception:
            pass
    if df.empty:
        return []
    return [
        {"high":  float(r["high"]),
         "low":   float(r["low"]),
         "close": float(r["close"]),
         "volume": float(r["volume"]) if r["volume"] else 0.0}
        for _, r in df.iterrows()
    ]


def get_spot_vwap(end=None) -> float:
    """Cumulative session VWAP from 5-min spot candles. Returns None if no data."""
    rows = get_spot_5min(today=True, end=end)
    if not rows:
        return None
    cum_pv = 0.0
    cum_v  = 0.0
    for r in rows:
        typical = (r["high"] + r["low"] + r["close"]) / 3.0
        v = r["volume"] if r["volume"] > 0 else 1.0
        cum_pv += typical * v
        cum_v  += v
    return round(cum_pv / cum_v, 2) if cum_v > 0 else None


# ═══════════════════════════════════════════════════════════════
#  BONUS INDICATORS — information only, never block trades
# ═══════════════════════════════════════════════════════════════

def calculate_option_vwap(token: int) -> dict:
    """VWAP on option — today's intraday only."""
    result = {"vwap": 0.0, "above_vwap": False, "distance": 0.0}
    try:
        from datetime import date as _d
        df = get_historical_data(token, "minute", 200)
        if df.empty or len(df) < 5:
            return result
        # Filter to TODAY only — today_only param unreliable
        today_str = _d.today().strftime("%Y-%m-%d")
        df = df.copy()
        df["_date"] = df.index.map(
            lambda x: x.strftime("%Y-%m-%d") if hasattr(x, "strftime") else str(x)[:10])
        df = df[df["_date"] == today_str]
        if df.empty or len(df) < 3:
            return result
        typical_price = (df["high"].astype(float) + df["low"].astype(float) + df["close"].astype(float)) / 3
        vol = df["volume"].astype(float)
        cum_vol = vol.cumsum().replace(0, float("nan"))
        cum_tp_vol = (typical_price * vol).cumsum()
        vwap = cum_tp_vol / cum_vol
        vwap_val = round(float(vwap.iloc[-1]), 2)
        last_close = float(df["close"].iloc[-1])
        result["vwap"] = vwap_val
        result["above_vwap"] = last_close > vwap_val
        result["distance"] = round(last_close - vwap_val, 2)
    except Exception as e:
        logger.debug("[VWAP] " + str(e))
    return result


def calculate_option_fib_pivots(token: int) -> dict:
    """Fib pivot points from previous day's option H/L/C."""
    result = {"pivot": 0, "R1": 0, "R2": 0, "R3": 0,
              "S1": 0, "S2": 0, "S3": 0,
              "nearest_level": "", "nearest_distance": 0, "ok": False}
    try:
        from datetime import date as _date
        df = get_historical_data(token, "minute", 500)
        if df.empty or len(df) < 50:
            return result
        df = df.copy()
        df["_date"] = df.index.map(lambda x: str(x)[:10])
        today_str = _date.today().strftime("%Y-%m-%d")
        dates = sorted(df["_date"].unique())
        if today_str not in dates or dates.index(today_str) == 0:
            return result
        prev_date = dates[dates.index(today_str) - 1]
        prev = df[df["_date"] == prev_date]
        if prev.empty:
            return result
        H = float(prev["high"].max())
        L = float(prev["low"].min())
        C = float(prev["close"].iloc[-1])
        R = H - L
        pivot = round((H + L + C) / 3, 2)
        result.update({
            "pivot": pivot,
            "R1": round(pivot + 0.382 * R, 2), "R2": round(pivot + 0.618 * R, 2),
            "R3": round(pivot + 1.000 * R, 2),
            "S1": round(pivot - 0.382 * R, 2), "S2": round(pivot - 0.618 * R, 2),
            "S3": round(pivot - 1.000 * R, 2),
            "ok": True,
        })
        last_price = float(df["close"].iloc[-1])
        levels = [(k, v) for k, v in result.items()
                  if k in ("pivot", "R1", "R2", "R3", "S1", "S2", "S3")]
        if levels:
            nearest = min(levels, key=lambda x: abs(last_price - x[1]))
            result["nearest_level"] = nearest[0]
            result["nearest_distance"] = round(last_price - nearest[1], 2)
    except Exception as e:
        logger.debug("[OPT_FIB] " + str(e))
    return result


def detect_volume_spike(token: int, threshold: float = 3.0) -> dict:
    """Check if current candle volume is Nx average."""
    result = {"spike": False, "ratio": 0.0, "current_vol": 0, "avg_vol": 0}
    try:
        df = get_historical_data(token, "minute", 25)
        if df.empty or len(df) < 10:
            return result
        last = df.iloc[-2]
        avg_window = df.iloc[-22:-2]
        avg_vol = float(avg_window["volume"].mean()) if len(avg_window) > 0 else 1
        curr_vol = float(last["volume"])
        ratio = round(curr_vol / avg_vol, 2) if avg_vol > 0 else 0
        result["current_vol"] = int(curr_vol)
        result["avg_vol"] = int(avg_vol)
        result["ratio"] = ratio
        result["spike"] = ratio >= threshold
    except Exception as e:
        logger.debug("[VOLSPIKE] " + str(e))
    return result


def get_option_prev_day_hl(token: int) -> dict:
    """Get previous day high/low for option."""
    result = {"prev_high": 0, "prev_low": 0,
              "above_prev_high": False, "below_prev_low": False, "ok": False}
    try:
        from datetime import date as _date
        df = get_historical_data(token, "minute", 500)
        if df.empty or len(df) < 50:
            return result
        df = df.copy()
        df["_date"] = df.index.map(lambda x: str(x)[:10])
        today_str = _date.today().strftime("%Y-%m-%d")
        dates = sorted(df["_date"].unique())
        if today_str not in dates or dates.index(today_str) == 0:
            return result
        prev_date = dates[dates.index(today_str) - 1]
        prev = df[df["_date"] == prev_date]
        if prev.empty:
            return result
        ph = float(prev["high"].max())
        pl = float(prev["low"].min())
        last_price = float(df["close"].iloc[-1])
        result.update({
            "prev_high": round(ph, 2), "prev_low": round(pl, 2),
            "above_prev_high": last_price > ph,
            "below_prev_low": last_price < pl, "ok": True,
        })
    except Exception as e:
        logger.debug("[PDH] " + str(e))
    return result


def calculate_atr(token: int, interval: str = "minute",
                  n_candles: int = None) -> float:
    """v12.12: Calculate ATR (Average True Range) for SL sizing."""
    if n_candles is None:
        n_candles = ATR_SL_CANDLES
    try:
        df = get_historical_data(token, interval, n_candles + 10)
        if df.empty or len(df) < n_candles + 1:
            return 0.0
        # True Range = max(high-low, |high-prev_close|, |low-prev_close|)
        prev_close = df["close"].shift(1)
        df["TR"] = pd.concat([
            df["high"] - df["low"],
            (df["high"] - prev_close).abs(),
            (df["low"] - prev_close).abs()
        ], axis=1).max(axis=1)
        # ATR = average of last N candles' true range
        atr = float(df["TR"].iloc[-n_candles - 1:-1].mean())
        return round(atr, 2)
    except Exception as e:
        logger.warning("[DATA] ATR calc error: " + str(e))
        return 0.0


def calculate_atr_sl(token: int, profile: dict,
                     entry_price: float = 0.0) -> float:
    """
    v12.12: ATR-based SL with floor and hard cap.
    Returns SL in points.
    """
    atr = calculate_atr(token, "minute", ATR_SL_CANDLES)
    if atr <= 0:
        # Fallback to DTE-based fixed SL
        return float(profile.get("conv_sl_pts", 15))

    raw_sl = round(ATR_SL_MULTIPLIER * atr, 1)

    # Floor based on premium level
    if entry_price >= 300:   floor_sl = 15
    elif entry_price >= 200: floor_sl = 12
    elif entry_price >= 100: floor_sl = 10
    elif entry_price >= 50:  floor_sl = 8
    else:                    floor_sl = 6

    sl = max(raw_sl, floor_sl)
    sl = min(sl, ATR_SL_MAX)  # Hard cap 25pts

    logger.info("[DATA] ATR SL: atr=" + str(atr)
                + " raw=" + str(round(ATR_SL_MULTIPLIER * atr, 1))
                + " floor=" + str(floor_sl)
                + " final=" + str(sl))
    return sl

def get_active_strike_step(dte: int = None) -> int:
    """v13.3: True ATM — 50-step for ALL DTE."""
    return 50

def resolve_atm_strike(spot_ltp: float, step: int = None) -> int:
    if step is None:
        step = STRIKE_STEP
    return int(round(spot_ltp / step) * step)

# Premium filter — from config
STRIKE_PREMIUM_MIN      = CFG.strike_cfg("premium_min", 100)
STRIKE_PREMIUM_MIN_DTE0 = CFG.strike_cfg("premium_min_dte0", 50)
STRIKE_PREMIUM_MAX      = CFG.strike_cfg("premium_max", 400)

def resolve_strike_for_direction(spot: float, direction: str, dte: int) -> int:
    """
    v13.3: True ATM — round to nearest 50 for ALL DTE.
    Both CE and PE use the SAME ATM strike. Premium naturally balanced.
    """
    return int(round(spot / 50) * 50)

def get_nearest_expiry(kite=None, reference_date=None) -> date:
    if reference_date is None:
        reference_date = date.today()
    kite = kite or _kite
    if kite is None:
        raise RuntimeError("Kite not initialised")
    try:
        instruments = _get_nfo_instruments(kite)
        expiries    = set()
        for inst in instruments:
            if inst.get("name") == "NIFTY" and inst.get("instrument_type") == "CE":
                exp = inst.get("expiry")
                if exp and isinstance(exp, date):
                    expiries.add(exp)
        future = sorted(e for e in expiries if e >= reference_date)
        if not future:
            logger.error("[DATA] No future expiry found")
            return None
        return future[0]
    except Exception as e:
        logger.error("[DATA] get_nearest_expiry error: " + str(e))
        return None

def calculate_dte(expiry_date) -> int:
    if expiry_date is None:
        return 0
    return max((expiry_date - date.today()).days, 0)

def get_option_tokens(kite, strike: int, expiry_date) -> dict:
    kite = kite or _kite
    if kite is None:
        return {}
    key = (int(strike), expiry_date.isoformat() if expiry_date else "")
    with _token_cache_lock:
        if key in _token_cache:
            return dict(_token_cache[key])
    try:
        instruments = _get_nfo_instruments(kite)
        expiry_str  = expiry_date.isoformat() if expiry_date else ""
        result      = {}
        for inst in instruments:
            if (inst.get("name") == "NIFTY"
                    and int(inst.get("strike", 0)) == int(strike)
                    and str(inst.get("expiry", "")) == expiry_str
                    and inst.get("instrument_type") in ("CE", "PE")):
                opt_type = inst["instrument_type"]
                result[opt_type] = {
                    "token" : inst["instrument_token"],
                    "symbol": inst["tradingsymbol"],
                }
            if len(result) == 2:
                break
        if len(result) < 2:
            logger.warning("[DATA] Token resolve incomplete: strike=" + str(strike)
                           + " found=" + str(list(result.keys())))
        with _token_cache_lock:
            _token_cache[key] = result
        return dict(result)
    except Exception as e:
        logger.error("[DATA] get_option_tokens error: " + str(e))
        return {}

def clear_token_cache():
    with _token_cache_lock:
        _token_cache.clear()
    logger.info("[DATA] Token cache cleared")

def _norm_cdf(x: float) -> float:
    if x < -8.0: return 0.0
    if x > 8.0:  return 1.0
    a1,a2,a3,a4,a5,p = 0.319381530,-0.356563782,1.781477937,-1.821255978,1.330274429,0.2316419
    k    = 1.0 / (1.0 + p * abs(x))
    poly = k * (a1 + k * (a2 + k * (a3 + k * (a4 + k * a5))))
    cdf  = 1.0 - _norm_pdf(x) * poly
    return cdf if x >= 0 else 1.0 - cdf

def _norm_pdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)

def _bs_price(S, K, T, r, sigma, option_type: str) -> float:
    if T <= 0 or sigma <= 0 or S <= 0 or K <= 0:
        return 0.0
    try:
        sqrt_T = math.sqrt(T)
        d1 = (math.log(S/K) + (r + 0.5*sigma**2)*T) / (sigma*sqrt_T)
        d2 = d1 - sigma*sqrt_T
        if option_type == "CE":
            return S*_norm_cdf(d1) - K*math.exp(-r*T)*_norm_cdf(d2)
        else:
            return K*math.exp(-r*T)*_norm_cdf(-d2) - S*_norm_cdf(-d1)
    except Exception:
        return 0.0

def _bs_vega(S, K, T, r, sigma) -> float:
    if T <= 0 or sigma <= 0 or S <= 0:
        return 0.0
    try:
        sqrt_T = math.sqrt(T)
        d1 = (math.log(S/K) + (r + 0.5*sigma**2)*T) / (sigma*sqrt_T)
        return S * _norm_pdf(d1) * sqrt_T
    except Exception:
        return 0.0

def calculate_iv(market_price, S, K, T, r, option_type, max_iter=100, tol=0.01):
    if not market_price or market_price <= 0 or T <= 0 or S <= 0 or K <= 0:
        return 0.0
    sigma = 0.20
    for _ in range(max_iter):
        price = _bs_price(S, K, T, r, sigma, option_type)
        vega  = _bs_vega(S, K, T, r, sigma)
        diff  = price - market_price
        if abs(diff) < tol:
            return round(sigma, 6)
        if abs(vega) < 1e-10:
            break
        sigma -= diff / vega
        sigma = max(0.001, min(sigma, 10.0))
    lo, hi = 0.001, 5.0
    for _ in range(100):
        mid   = (lo + hi) / 2.0
        price = _bs_price(S, K, T, r, mid, option_type)
        if abs(price - market_price) < 0.01:
            return round(mid, 6)
        if price < market_price:
            lo = mid
        else:
            hi = mid
    return 0.0

def calculate_greeks(S, K, T, r, sigma, option_type):
    empty = {"delta":0.0,"gamma":0.0,"theta":0.0,"vega":0.0,"iv_pct":0.0,"price":0.0}
    if T <= 0 or sigma <= 0 or S <= 0 or K <= 0:
        return empty
    try:
        sqrt_T = math.sqrt(T)
        d1     = (math.log(S/K) + (r + 0.5*sigma**2)*T) / (sigma*sqrt_T)
        d2     = d1 - sigma*sqrt_T
        pdf_d1 = _norm_pdf(d1)
        exp_rT = math.exp(-r*T)
        delta  = _norm_cdf(d1) if option_type == "CE" else _norm_cdf(d1) - 1.0
        gamma  = pdf_d1 / (S * sigma * sqrt_T)
        if option_type == "CE":
            theta = ((-S*pdf_d1*sigma/(2*sqrt_T)) - r*K*exp_rT*_norm_cdf(d2)) / 365.0
        else:
            theta = ((-S*pdf_d1*sigma/(2*sqrt_T)) + r*K*exp_rT*_norm_cdf(-d2)) / 365.0
        vega  = S * pdf_d1 * sqrt_T / 100.0
        price = _bs_price(S, K, T, r, sigma, option_type)
        return {"delta":round(delta,4),"gamma":round(gamma,6),"theta":round(theta,4),
                "vega":round(vega,4),"iv_pct":round(sigma*100,2),"price":round(price,2)}
    except Exception as e:
        logger.error("[GREEKS] Calculation error: " + str(e))
        return empty

def get_full_greeks(market_price, spot_ltp, strike, expiry_date, option_type, r=None):
    if r is None:
        r = RISK_FREE_RATE
    empty = {"delta":0.0,"gamma":0.0,"theta":0.0,"vega":0.0,"iv_pct":0.0,"dte":0,"ok":False}
    try:
        dte   = max((expiry_date - date.today()).days, 0)
        T     = dte / 365.0 if dte > 0 else 0.5 / 365.0
        P     = float(market_price) if market_price else 0.0
        iv    = calculate_iv(P, float(spot_ltp), float(strike), T, r, option_type) if P > 0 else 0.0
        if iv <= 0:
            return {**empty, "dte": dte}
        greeks        = calculate_greeks(float(spot_ltp), float(strike), T, r, iv, option_type)
        greeks["dte"] = dte
        greeks["ok"]  = True
        return greeks
    except Exception as e:
        logger.error("[GREEKS] get_full_greeks error: " + str(e))
        return empty


# ═══════════════════════════════════════════════════════════════
#  SPOT INTELLIGENCE LAYER (v12.11)
#  Always reliable — spot has full multi-day history from Kite
#  Used for: gap detection, regime backup, direction, alignment
# ═══════════════════════════════════════════════════════════════

_spot_gap      = 0.0
_spot_prev_close = 0.0
_prev_spot_spread_3m = None   # for spread_prev in compute_spot_regime

def get_spot_indicators(interval: str = "3minute") -> dict:
    """
    Fetch spot EMA9, EMA21, RSI on any timeframe.
    Always has 100+ candles — never thin data.
    """
    result = {
        "ema9": 0.0, "ema21": 0.0, "spread": 0.0,
        "rsi": 0.0, "close": 0.0, "candles": 0,
        "regime": "UNKNOWN",
    }
    try:
        lookback = 60 if interval == "3minute" else 50
        df = get_historical_data(NIFTY_SPOT_TOKEN, interval, lookback)
        df = add_indicators(df)
        if df.empty or len(df) < 5:
            return result
        last = df.iloc[-2]
        ema9  = round(float(last.get("EMA_9",  last["close"])), 2)
        ema21 = round(float(last.get("EMA_21", last["close"])), 2)
        spread = round(ema9 - ema21, 2)
        rsi   = round(float(last.get("RSI", 50)), 1)
        result["ema9"]    = ema9
        result["ema21"]   = ema21
        result["spread"]  = spread
        result["rsi"]     = rsi
        result["close"]   = round(float(last["close"]), 2)
        result["candles"] = len(df)
        # Regime from spot — always accurate
        abs_sp = abs(spread)
        if abs_sp >= 12:   result["regime"] = "TRENDING_STRONG"
        elif abs_sp >= 5:  result["regime"] = "TRENDING"
        elif abs_sp >= 2:  result["regime"] = "NEUTRAL"
        else:              result["regime"] = "CHOPPY"
        # ADX inline
        try:
            import numpy as _np
            _up = df["high"].diff()
            _dn = -df["low"].diff()
            _pdm = _np.where((_up > _dn) & (_up > 0), _up, 0.0)
            _ndm = _np.where((_dn > _up) & (_dn > 0), _dn, 0.0)
            _tr = pd.concat([df["high"]-df["low"],
                             (df["high"]-df["close"].shift(1)).abs(),
                             (df["low"]-df["close"].shift(1)).abs()], axis=1).max(axis=1)
            _atr = _tr.ewm(alpha=1/14, adjust=False).mean()
            _pdi = 100 * pd.Series(_pdm, index=df.index).ewm(alpha=1/14, adjust=False).mean() / _atr
            _ndi = 100 * pd.Series(_ndm, index=df.index).ewm(alpha=1/14, adjust=False).mean() / _atr
            _adx = ((_pdi-_ndi).abs() / (_pdi+_ndi+1e-9) * 100).ewm(alpha=1/14, adjust=False).mean()
            result["adx"] = round(float(_adx.iloc[-2]), 1)
        except Exception:
            result["adx"] = 0
        # Track spread_prev for regime scoring
        if interval == "3minute":
            global _prev_spot_spread_3m
            result["spread_prev"] = _prev_spot_spread_3m if _prev_spot_spread_3m is not None else spread
            _prev_spot_spread_3m = spread
    except Exception as e:
        logger.warning("[SPOT] get_spot_indicators error: " + str(e))
    return result


def compute_spot_regime() -> str:
    """
    Price action regime — instant, zero lag. No ADX dependency.
    Uses higher highs/lower lows, range, breakout, momentum.
    """
    try:
        df = get_historical_data(NIFTY_SPOT_TOKEN, "3minute", 15)
        if df.empty or len(df) < 7:
            return "UNKNOWN"

        candles = []
        for i in range(-min(10, len(df)), 0):
            row = df.iloc[i]
            candles.append({
                "open": float(row["open"]), "high": float(row["high"]),
                "low": float(row["low"]), "close": float(row["close"]),
            })

        if len(candles) < 5:
            return "UNKNOWN"

        last5 = candles[-5:]
        last3 = candles[-3:]

        # 1. HIGHER HIGHS / LOWER LOWS — directional move
        hh = all(last3[i]["high"] > last3[i-1]["high"] for i in range(1, len(last3)))
        ll = all(last3[i]["low"] < last3[i-1]["low"] for i in range(1, len(last3)))
        trending = hh or ll

        # 2. RANGE — last 5 candles total range
        range_high = max(c["high"] for c in last5)
        range_low = min(c["low"] for c in last5)
        total_range = range_high - range_low

        # 3. BREAKOUT — current candle body vs average
        bodies = [abs(c["close"] - c["open"]) for c in candles]
        avg_body = sum(bodies) / len(bodies) if bodies else 1
        curr_body = abs(candles[-1]["close"] - candles[-1]["open"])
        breakout = curr_body > avg_body * 2

        # 4. MOMENTUM — are candles getting bigger?
        recent_bodies = [abs(c["close"] - c["open"]) for c in last3]
        accelerating = recent_bodies[-1] > recent_bodies[0]

        # REGIME DECISION — pure price action
        if breakout and trending:
            return "TRENDING_STRONG"
        elif trending or (breakout and total_range > 30):
            return "TRENDING"
        elif total_range < 30:
            return "NEUTRAL"
        else:
            return "CHOPPY"

    except Exception:
        return "UNKNOWN"


def calculate_spot_gap() -> dict:
    """
    Calculate gap: today's open vs previous trading session's close.

    BUG-G v15.2.5 Batch 4: lookback extended from 7 → 14 days so that
    long weekends + back-to-back holidays (e.g. Diwali + weekend combo)
    don't strand us with no previous session in the window. Also
    falls back to the DAILY candle endpoint when the 1-min fetch
    returns no prior-day data — daily candles survive any gap.

    Kite's 1-min historical_data returns only market-hours candles,
    so the last candle before today in the fetched window is always
    the previous session's close regardless of non-trading days — as
    long as the window actually reaches one.
    """
    global _spot_gap, _spot_prev_close
    result = {"gap_pts": 0.0, "gap_pct": 0.0, "prev_close": 0.0, "today_open": 0.0}
    try:
        if _kite is None:
            return result
        now     = datetime.now()
        # BUG-G: widened from 7 → 14 days to survive long weekends +
        # paired holidays. Extra data is cheap and skipped in the loop below.
        from_dt = now - timedelta(days=14)
        raw = _kite.historical_data(
            instrument_token=NIFTY_SPOT_TOKEN,
            from_date=from_dt, to_date=now,
            interval="minute", continuous=False, oi=False)

        today_str = date.today().strftime("%Y-%m-%d")
        prev_close = None
        prev_date  = None
        today_open = None

        # Primary path: 1-min grouped by date.
        if raw and len(raw) >= 50:
            dates_seen = {}
            for c in raw:
                d = str(c["date"])[:10]
                if d not in dates_seen:
                    dates_seen[d] = {"first_open": c["open"], "last_close": c["close"]}
                dates_seen[d]["last_close"] = c["close"]

            sorted_dates = sorted(dates_seen.keys())
            if today_str in sorted_dates:
                today_idx = sorted_dates.index(today_str)
                today_open = float(dates_seen[today_str]["first_open"])
                if today_idx > 0:
                    prev_date  = sorted_dates[today_idx - 1]
                    prev_close = float(dates_seen[prev_date]["last_close"])

        # BUG-G fallback: if 1-min path couldn't find a previous session,
        # ask the daily-candle endpoint directly. Daily candles survive
        # any combo of weekends + holidays because Kite stores one per
        # actual trading day, with the session's final close baked in.
        if prev_close is None:
            try:
                daily = _kite.historical_data(
                    instrument_token=NIFTY_SPOT_TOKEN,
                    from_date=(now - timedelta(days=30)),
                    to_date=now,
                    interval="day", continuous=False, oi=False)
            except Exception as _e:
                daily = None
            if daily:
                dsorted = sorted(daily, key=lambda r: str(r.get("date", "")))
                # Walk backwards past any same-day candle and pick the
                # first one strictly before today.
                for r in reversed(dsorted):
                    d = str(r.get("date", ""))[:10]
                    if d < today_str:
                        prev_close = float(r["close"])
                        prev_date  = d
                        if today_open is None:
                            today_open = float(r["close"])  # degrades to 0 gap, honest default
                        logger.info("[SPOT] Gap prev_close via DAILY fallback: "
                                    + str(prev_close) + " (" + prev_date + ")")
                        break

        if prev_close is None or today_open is None:
            logger.warning("[SPOT] Gap calc: no prev session found in 14d 1-min "
                           "OR 30d daily — returning zero-gap result")
            return result

        gap_pts = round(today_open - prev_close, 2)
        gap_pct = round(gap_pts / prev_close * 100, 2) if prev_close > 0 else 0.0
        _spot_gap        = gap_pts
        _spot_prev_close = prev_close
        result = {
            "gap_pts": gap_pts, "gap_pct": gap_pct,
            "prev_close": prev_close, "today_open": today_open,
        }
        logger.info("[SPOT] Gap: " + str(gap_pts) + "pts ("
                    + str(gap_pct) + "%) prev=" + str(prev_close)
                    + " (" + str(prev_date) + ") open=" + str(today_open))
    except Exception as e:
        logger.warning("[SPOT] Gap calculation error: " + str(e))
    return result


def get_spot_gap() -> float:
    return _spot_gap


def get_spot_regime(interval: str = "3minute") -> str:
    """Quick regime from spot."""
    return get_spot_indicators(interval).get("regime", "UNKNOWN")


# ═══════════════════════════════════════════════════════════════
#  FIB PIVOT POINTS (v12.15)
#  Calculated once at startup from previous session's H/L/C
#  Fibonacci ratios: 0.382, 0.618, 1.000
# ═══════════════════════════════════════════════════════════════

_fib_pivots = {}

def calculate_fib_pivots() -> dict:
    """
    Calculate Fibonacci pivot points from previous trading session.
    Uses 7-day spot fetch — works across weekends and holidays.
    Returns dict with Pivot, R1-R3, S1-S3.
    """
    global _fib_pivots
    result = {}
    try:
        if _kite is None:
            return result
        now     = datetime.now()
        from_dt = now - timedelta(days=7)
        raw = _kite.historical_data(
            instrument_token=NIFTY_SPOT_TOKEN,
            from_date=from_dt, to_date=now,
            interval="minute", continuous=False, oi=False)
        if not raw or len(raw) < 50:
            return result

        today_str = date.today().strftime("%Y-%m-%d")
        # Group by date
        by_date = {}
        for c in raw:
            d = str(c["date"])[:10]
            if d not in by_date:
                by_date[d] = {"high": 0, "low": 999999, "close": 0}
            by_date[d]["high"]  = max(by_date[d]["high"], float(c["high"]))
            by_date[d]["low"]   = min(by_date[d]["low"],  float(c["low"]))
            by_date[d]["close"] = float(c["close"])

        sorted_dates = sorted(by_date.keys())
        if today_str not in sorted_dates:
            return result
        idx = sorted_dates.index(today_str)
        if idx == 0:
            return result

        prev_date = sorted_dates[idx - 1]
        prev = by_date[prev_date]
        H = prev["high"]
        L = prev["low"]
        C = prev["close"]
        R = H - L

        pivot = round((H + L + C) / 3, 2)
        result = {
            "pivot":     pivot,
            "R1":        round(pivot + 0.382 * R, 2),
            "R2":        round(pivot + 0.618 * R, 2),
            "R3":        round(pivot + 1.000 * R, 2),
            "S1":        round(pivot - 0.382 * R, 2),
            "S2":        round(pivot - 0.618 * R, 2),
            "S3":        round(pivot - 1.000 * R, 2),
            "prev_high": H,
            "prev_low":  L,
            "prev_close":C,
            "prev_date": prev_date,
            "range":     round(R, 2),
        }
        _fib_pivots = result

        # Also compute today's developing pivots
        if today_str in by_date:
            td = by_date[today_str]
            result["today_high"] = td["high"]
            result["today_low"]  = td["low"]

        logger.info("[PIVOT] Fib pivots: P=" + str(pivot)
                    + " R1=" + str(result["R1"]) + " R2=" + str(result["R2"])
                    + " R3=" + str(result["R3"])
                    + " S1=" + str(result["S1"]) + " S2=" + str(result["S2"])
                    + " S3=" + str(result["S3"])
                    + " (" + prev_date + " H=" + str(H) + " L=" + str(L)
                    + " C=" + str(C) + ")")
    except Exception as e:
        logger.warning("[PIVOT] Fib calc error: " + str(e))
    return result


def get_fib_pivots() -> dict:
    return _fib_pivots


def get_nearest_fib_level(spot_price: float) -> dict:
    """Find nearest fib level to current spot. Returns {level_name, price, distance}."""
    if not _fib_pivots:
        return {"level": "—", "price": 0, "distance": 999}
    levels = [(k, v) for k, v in _fib_pivots.items()
              if k in ("pivot", "R1", "R2", "R3", "S1", "S2", "S3")]
    if not levels:
        return {"level": "—", "price": 0, "distance": 999}
    nearest = min(levels, key=lambda x: abs(spot_price - x[1]))
    return {
        "level": nearest[0],
        "price": nearest[1],
        "distance": round(spot_price - nearest[1], 2),
    }


# ═══════════════════════════════════════════════════════════════
#  SPOT CONSOLIDATION DETECTOR (v12.15)
#  Tracks last N 1-min candles for tight-range detection
#  Used by expiry breakout mode
# ═══════════════════════════════════════════════════════════════

_spot_1m_buffer = []   # List of (timestamp, open, high, low, close)
_spot_buffer_lock = threading.Lock()
_SPOT_BUFFER_MAX = 20  # Keep last 20 candles

def update_spot_buffer(candle: dict):
    """Called by strategy loop or lab to feed 1-min spot candles."""
    global _spot_1m_buffer
    with _spot_buffer_lock:
        _spot_1m_buffer.append(candle)
        if len(_spot_1m_buffer) > _SPOT_BUFFER_MAX:
            _spot_1m_buffer = _spot_1m_buffer[-_SPOT_BUFFER_MAX:]


def detect_spot_consolidation() -> dict:
    """
    Check if last N candles form a consolidation (range < threshold).
    Returns {consolidating, range, high, low, candles, near_fib}.
    """
    n = EXPIRY_CONSOL_CANDLES
    result = {
        "consolidating": False, "range": 0, "high": 0, "low": 0,
        "candles": 0, "near_fib": {}, "mid": 0,
    }
    with _spot_buffer_lock:
        if len(_spot_1m_buffer) < n:
            return result
        recent = list(_spot_1m_buffer[-n:])
    highs = [float(c.get("high", c.get("close", 0))) for c in recent]
    lows  = [float(c.get("low",  c.get("close", 0))) for c in recent]
    h = max(highs)
    l = min(lows)
    rng = round(h - l, 2)

    result["range"]   = rng
    result["high"]    = round(h, 2)
    result["low"]     = round(l, 2)
    result["candles"] = n
    result["mid"]     = round((h + l) / 2, 2)

    if rng <= EXPIRY_CONSOL_RANGE:
        result["consolidating"] = True
        result["near_fib"] = get_nearest_fib_level(result["mid"])

    return result


def detect_spot_breakout(current_spot: float) -> dict:
    """
    Check if current spot price has broken out of consolidation.
    Returns {breakout, direction, magnitude, consolidation}.
    """
    result = {
        "breakout": False, "direction": "", "magnitude": 0,
        "consolidation": {}, "near_fib": {},
    }
    consol = detect_spot_consolidation()
    if not consol["consolidating"]:
        return result

    result["consolidation"] = consol

    if current_spot > consol["high"] + EXPIRY_BREAKOUT_MIN:
        result["breakout"]  = True
        result["direction"] = "CE"
        result["magnitude"] = round(current_spot - consol["high"], 2)
        result["near_fib"]  = get_nearest_fib_level(current_spot)
    elif current_spot < consol["low"] - EXPIRY_BREAKOUT_MIN:
        result["breakout"]  = True
        result["direction"] = "PE"
        result["magnitude"] = round(consol["low"] - current_spot, 2)
        result["near_fib"]  = get_nearest_fib_level(current_spot)

    return result


def is_expiry_window(now: datetime = None) -> bool:
    """Check if current time is within expiry trading window."""
    if now is None:
        now = datetime.now()
    start = now.replace(hour=EXPIRY_START_HOUR, minute=EXPIRY_START_MIN, second=0)
    end   = now.replace(hour=EXPIRY_CUTOFF_HOUR, minute=EXPIRY_CUTOFF_MIN, second=0)
    return start <= now <= end


# ═══════════════════════════════════════════════════════════════
#  v12.15: WARNING SYSTEM (all warnings only — no blocking)
#  Bias 9:20 | Straddle 9:30 | VIX+Hourly RSI continuous
#  Entry fire: 9:30-15:10 | Scan from 9:15
# ═══════════════════════════════════════════════════════════════

VIX_WARN_LEVEL    = 22
VIX_DANGER_LEVEL  = 28
STRADDLE_WARN_PCT = 5.0
ENTRY_FIRE_HOUR   = 9
ENTRY_FIRE_MIN    = 31   # v13.3: 9:31 — catch morning moves

_straddle_open     = 0.0
_straddle_captured = False
_daily_bias        = "UNKNOWN"
_daily_bias_done   = False
_hourly_rsi        = 0.0
_hourly_rsi_ts     = 0
_straddle_check_ts = 0


def capture_straddle(kite, strike, expiry):
    global _straddle_open, _straddle_captured
    if _straddle_captured:
        return
    try:
        tokens = get_option_tokens(kite, strike, expiry)
        if not tokens:
            return
        ce_ltp = pe_ltp = 0.0
        for side in ("CE", "PE"):
            info = tokens.get(side)
            if not info:
                continue
            ltp = get_ltp(info["token"])
            if ltp <= 0 and kite:
                try:
                    q = kite.ltp(["NFO:" + info["symbol"]])
                    ltp = float(list(q.values())[0]["last_price"])
                except Exception:
                    pass
            if side == "CE":
                ce_ltp = ltp
            else:
                pe_ltp = ltp
        if ce_ltp > 0 and pe_ltp > 0:
            _straddle_open = round(ce_ltp + pe_ltp, 2)
            _straddle_captured = True
            logger.info("[STRADDLE] CE=" + str(round(ce_ltp, 1))
                        + " PE=" + str(round(pe_ltp, 1))
                        + " Sum=" + str(_straddle_open))
    except Exception as e:
        logger.warning("[STRADDLE] Capture: " + str(e))


def get_straddle_decay(kite, strike, expiry):
    global _straddle_check_ts
    import time as _t
    result = {"decay_pct": 0.0, "current": 0.0, "open": _straddle_open,
              "warning": False, "msg": ""}
    if not _straddle_captured or _straddle_open <= 0:
        return result
    if _t.time() - _straddle_check_ts < 300:
        return result
    _straddle_check_ts = _t.time()
    try:
        tokens = get_option_tokens(kite, strike, expiry)
        if not tokens:
            return result
        ce_ltp = pe_ltp = 0.0
        for side in ("CE", "PE"):
            info = tokens.get(side)
            if info:
                ltp = get_ltp(info["token"])
                if side == "CE":
                    ce_ltp = ltp
                else:
                    pe_ltp = ltp
        if ce_ltp > 0 and pe_ltp > 0:
            current = ce_ltp + pe_ltp
            decay = round((current - _straddle_open) / _straddle_open * 100, 1)
            result["current"] = round(current, 2)
            result["decay_pct"] = decay
            if decay <= -STRADDLE_WARN_PCT:
                result["warning"] = True
                result["msg"] = ("SELLERS DAY straddle " + str(decay)
                                 + "% (open " + str(int(_straddle_open))
                                 + " now " + str(int(current)) + ")")
    except Exception as e:
        logger.warning("[STRADDLE] Decay: " + str(e))
    return result


def compute_daily_bias(kite):
    global _daily_bias, _daily_bias_done
    result = {"bias": "UNKNOWN", "ema21": 0, "adx": 0, "spot": 0, "details": ""}
    try:
        if _kite is None:
            return result
        now = datetime.now()
        raw = _kite.historical_data(
            instrument_token=NIFTY_SPOT_TOKEN,
            from_date=now - timedelta(days=60), to_date=now,
            interval="day", continuous=False, oi=False)
        if not raw or len(raw) < 25:
            return result
        df = pd.DataFrame(raw)
        for col in ("close", "high", "low"):
            df[col] = df[col].astype(float)
        ema21 = df["close"].ewm(span=21, adjust=False).mean()
        last_ema = round(float(ema21.iloc[-1]), 2)
        last_c = float(df["close"].iloc[-1])
        import numpy as _np
        up = df["high"].diff()
        dn = -df["low"].diff()
        pdm = _np.where((up > dn) & (up > 0), up, 0.0)
        ndm = _np.where((dn > up) & (dn > 0), dn, 0.0)
        tr = pd.concat([df["high"]-df["low"],
                         (df["high"]-df["close"].shift(1)).abs(),
                         (df["low"]-df["close"].shift(1)).abs()], axis=1).max(axis=1)
        atr_s = tr.ewm(alpha=1/14, adjust=False).mean()
        pdi = 100 * pd.Series(pdm, index=df.index).ewm(alpha=1/14, adjust=False).mean() / atr_s
        ndi = 100 * pd.Series(ndm, index=df.index).ewm(alpha=1/14, adjust=False).mean() / atr_s
        adx_s = ((pdi-ndi).abs() / (pdi+ndi+1e-9) * 100).ewm(alpha=1/14, adjust=False).mean()
        adx_v = round(float(adx_s.iloc[-1]), 1)
        above = last_c > last_ema
        if adx_v < 18:
            bias, det = "SIDEWAYS", "ADX " + str(adx_v) + " < 18 no trend"
        elif above and adx_v >= 20:
            bias, det = "BULL", "Above EMA21 + ADX " + str(adx_v)
        elif not above and adx_v >= 20:
            bias, det = "BEAR", "Below EMA21 + ADX " + str(adx_v)
        else:
            bias, det = "NEUTRAL", "Mixed ADX " + str(adx_v)
        result = {"bias": bias, "ema21": last_ema, "adx": adx_v,
                  "spot": last_c, "details": det}
        _daily_bias = bias
        _daily_bias_done = True
        logger.info("[BIAS] " + bias + " EMA21=" + str(last_ema) + " ADX=" + str(adx_v))
    except Exception as e:
        logger.warning("[BIAS] " + str(e))
    return result


def get_daily_bias():
    return _daily_bias


def check_hourly_rsi(kite):
    global _hourly_rsi, _hourly_rsi_ts
    result = {"rsi": 0.0, "warning": False, "msg": ""}
    try:
        if _kite is None:
            return result
        now = datetime.now()
        raw = _kite.historical_data(
            instrument_token=NIFTY_SPOT_TOKEN,
            from_date=now - timedelta(days=10), to_date=now,
            interval="60minute", continuous=False, oi=False)
        if not raw or len(raw) < 20:
            return result
        df = pd.DataFrame(raw)
        df["close"] = df["close"].astype(float)
        delta = df["close"].diff()
        gain = delta.clip(lower=0).ewm(alpha=1/14, adjust=False).mean()
        loss = (-delta.clip(upper=0)).ewm(alpha=1/14, adjust=False).mean()
        rsi = 100 - 100 / (1 + gain / (loss + 1e-9))
        rv = round(float(rsi.iloc[-1]), 1)
        _hourly_rsi = rv
        _hourly_rsi_ts = int(now.timestamp())
        result["rsi"] = rv
        if rv >= 70:
            result["warning"] = True
            result["msg"] = "Hourly RSI " + str(rv) + " OVERBOUGHT — CE risky"
        elif rv <= 30:
            result["warning"] = True
            result["msg"] = "Hourly RSI " + str(rv) + " OVERSOLD — PE risky"
        logger.info("[HOURLY] RSI=" + str(rv))
    except Exception as e:
        logger.warning("[HOURLY] " + str(e))
    return result


def get_hourly_rsi():
    return _hourly_rsi


def check_vix_warning():
    if not is_market_open():
        return {"vix": 0, "warning": False, "level": "NORMAL", "msg": ""}
    vix = get_vix()
    result = {"vix": round(vix, 1), "warning": False, "level": "NORMAL", "msg": ""}
    if vix >= VIX_DANGER_LEVEL:
        result.update(warning=True, level="DANGER",
                      msg="VIX " + str(round(vix, 1)) + " DANGER — SLs hit by noise")
    elif vix >= VIX_WARN_LEVEL:
        result.update(warning=True, level="ELEVATED",
                      msg="VIX " + str(round(vix, 1)) + " ELEVATED — wider SLs needed")
    return result


def is_entry_fire_window(now=None):
    if now is None:
        now = datetime.now()
    start = now.replace(hour=ENTRY_FIRE_HOUR, minute=ENTRY_FIRE_MIN, second=0)
    end = now.replace(hour=ENTRY_CUTOFF_HOUR, minute=ENTRY_CUTOFF_MIN, second=0)
    return start <= now <= end


def run_warnings(kite, state, expiry, dte, spot_ltp, now):
    import time as _t
    msgs = []
    upd = {}
    # Skip all warnings on weekends and NSE holidays — no Telegram spam
    if not is_trading_day(now):
        return msgs, upd
    # 1. Daily bias 9:20
    if now.hour == 9 and 20 <= now.minute <= 22 and not state.get("_bias_done"):
        try:
            b = compute_daily_bias(kite)
            upd["_bias_done"] = True
            if b.get("bias") != "UNKNOWN":
                ic = {"BULL": "\U0001f402", "BEAR": "\U0001f43b",
                      "SIDEWAYS": "\u26a0\ufe0f", "NEUTRAL": "\u3030\ufe0f"}
                msgs.append(ic.get(b["bias"], "?") + " <b>DAILY BIAS: " + b["bias"] + "</b>\n"
                            + b.get("details", "") + "\n"
                            + "EMA21: " + str(b.get("ema21", 0)) + "  ADX: " + str(b.get("adx", 0)))
        except Exception as _e:
            logger.warning("[WARN] Bias: " + str(_e))
    # 2. Straddle capture 9:30
    if (now.hour == 9 and now.minute >= 30 and not state.get("_straddle_done")
            and spot_ltp > 0 and expiry is not None):
        try:
            _ss = get_active_strike_step(dte)
            _sa = resolve_atm_strike(spot_ltp, _ss)
            if _sa > 0:
                capture_straddle(kite, _sa, expiry)
                upd["_straddle_done"] = True
                if _straddle_captured:
                    msgs.append("\U0001f4ca <b>STRADDLE CAPTURED</b>\nATM CE+PE: \u20b9" + str(int(_straddle_open)))
        except Exception as _e:
            logger.warning("[WARN] Straddle: " + str(_e))
    # 3. Straddle decay (every 5min)
    if (_straddle_captured and not state.get("_straddle_alerted")
            and spot_ltp > 0 and expiry is not None):
        try:
            _ss2 = get_active_strike_step(dte)
            _sa2 = resolve_atm_strike(spot_ltp, _ss2)
            sd = get_straddle_decay(kite, _sa2, expiry)
            if sd.get("warning"):
                upd["_straddle_alerted"] = True
                msgs.append("\U0001f534 <b>" + sd["msg"] + "</b>")
        except Exception:
            pass
    # 4. Hourly RSI (every hour — only during market hours)
    if (is_market_open() and now.minute == 0 and now.second < 35
            and (_t.time() - state.get("_hourly_rsi_ts", 0)) > 3000):
        try:
            hr = check_hourly_rsi(kite)
            upd["_hourly_rsi_ts"] = _t.time()
            if hr.get("warning"):
                msgs.append("\u26a0\ufe0f <b>" + hr["msg"] + "</b>")
        except Exception as _e:
            logger.warning("[WARN] Hourly: " + str(_e))
    # 5. VIX (once)
    if not state.get("_vix_warned"):
        try:
            vw = check_vix_warning()
            if vw.get("warning"):
                upd["_vix_warned"] = True
                msgs.append("\u26a0\ufe0f <b>" + vw["msg"] + "</b>")
        except Exception:
            pass
    return msgs, upd


def reset_daily_warnings():
    global _straddle_open, _straddle_captured, _daily_bias, _daily_bias_done
    global _hourly_rsi, _hourly_rsi_ts, _straddle_check_ts
    _straddle_open = 0.0
    _straddle_captured = False
    _daily_bias = "UNKNOWN"
    _daily_bias_done = False
    _hourly_rsi = 0.0
    _hourly_rsi_ts = 0
    _straddle_check_ts = 0


# ═══════════════════════════════════════════════════════════════
#  LAB DATA RETENTION — delete CSVs older than N days
# ═══════════════════════════════════════════════════════════════

def cleanup_old_lab_data(retention_days: int = None):
    """Delete lab CSV files older than retention_days. Called daily."""
    if retention_days is None:
        retention_days = CFG.lab("retention_days", 30)
    cutoff = datetime.now() - timedelta(days=retention_days)
    dirs_to_clean = [OPTIONS_1MIN_DIR, OPTIONS_3MIN_DIR, SPOT_DIR, REPORTS_DIR]
    removed = 0
    for d in dirs_to_clean:
        if not os.path.isdir(d):
            continue
        for f in os.listdir(d):
            fp = os.path.join(d, f)
            if not os.path.isfile(fp) or not f.endswith(".csv"):
                continue
            try:
                mtime = datetime.fromtimestamp(os.path.getmtime(fp))
                if mtime < cutoff:
                    os.remove(fp)
                    removed += 1
            except Exception:
                pass
    if removed > 0:
        logger.info("[DATA] Lab cleanup: removed " + str(removed)
                    + " files older than " + str(retention_days) + " days")


# ═══════════════════════════════════════════════════════════════
#  BUG-R13: ensure_option_history() — single entry point for any
#  module that needs option candle history. Checks DB first, fetches
#  from Kite API if insufficient. Never raises on network errors.
# ═══════════════════════════════════════════════════════════════

def ensure_option_history(kite_inst, strike: int, expiry,
                          min_candles: int = 30,
                          timeframes: tuple = ("3minute",),
                          lookback_days: int = 5) -> dict:
    """Ensure DB has at least min_candles of history for given strike
    (both CE and PE) across each requested timeframe.

    Returns: {"strike": int, "ce_candles": int, "pe_candles": int,
              "fetched": bool, "error": str or None}
    """
    import sqlite3
    k = kite_inst or _kite
    result = {"strike": strike, "ce_candles": 0, "pe_candles": 0,
              "fetched": False, "api_calls": 0, "error": None}
    if k is None:
        result["error"] = "kite not initialised"
        return result

    db_path = os.path.expanduser("~/lab_data/vrl_data.db")
    cutoff = (datetime.now() - timedelta(days=lookback_days)).strftime("%Y-%m-%d")
    table_map = {"minute": "option_1min", "3minute": "option_3min",
                 "5minute": "option_5min", "15minute": "option_15min"}
    tokens = get_option_tokens(k, strike, expiry)
    if not tokens:
        result["error"] = "no tokens for strike " + str(strike)
        return result

    fetched_any = False
    for tf in timeframes:
        table = table_map.get(tf)
        if not table:
            continue
        for side in ("CE", "PE"):
            info = tokens.get(side, {})
            token = info.get("token")
            if not token:
                continue
            try:
                conn = sqlite3.connect(db_path)
                cnt = conn.execute(
                    "SELECT COUNT(*) FROM " + table
                    + " WHERE strike=? AND type=? AND date(timestamp)>=?",
                    (strike, side, cutoff)
                ).fetchone()[0]
                conn.close()
            except Exception:
                cnt = 0

            if side == "CE":
                result["ce_candles"] = max(result["ce_candles"], cnt)
            else:
                result["pe_candles"] = max(result["pe_candles"], cnt)

            if cnt >= min_candles:
                continue

            try:
                from_dt = datetime.now() - timedelta(days=lookback_days)
                to_dt = datetime.now()
                time.sleep(0.5)
                result["api_calls"] += 1
                raw = k.historical_data(
                    instrument_token=int(token),
                    from_date=from_dt, to_date=to_dt,
                    interval=tf, continuous=False, oi=False)
                if not raw:
                    continue
                import VRL_DB as DB
                rows = []
                for r in raw:
                    ts = r.get("date")
                    if ts and hasattr(ts, "strftime"):
                        ts = ts.strftime("%Y-%m-%d %H:%M:%S")
                    rows.append({
                        "timestamp": str(ts),
                        "strike": strike,
                        "type": side,
                        "open": float(r.get("open", 0)),
                        "high": float(r.get("high", 0)),
                        "low": float(r.get("low", 0)),
                        "close": float(r.get("close", 0)),
                        "volume": float(r.get("volume", 0)),
                    })
                if rows:
                    if tf == "3minute":
                        DB.insert_option_3min_many(rows)
                    elif tf == "minute":
                        DB.insert_option_1min_many(rows)
                    elif tf == "5minute":
                        DB.insert_option_5min_many(rows)
                    elif tf == "15minute":
                        DB.insert_option_15min_many(rows)
                    fetched_any = True
                    new_cnt = cnt + len(rows)
                    if side == "CE":
                        result["ce_candles"] = new_cnt
                    else:
                        result["pe_candles"] = new_cnt
                    logger.info("[PRELOAD] " + side + " " + str(strike)
                                + " " + tf + ": fetched " + str(len(rows))
                                + " candles (had " + str(cnt) + ")")
            except Exception as e:
                err = str(e)[:100]
                if "incorrect api_key" in err.lower() or "access_token" in err.lower():
                    _set_auth_rejected()
                    result["error"] = "auth rejected"
                    return result
                logger.warning("[PRELOAD] fetch error " + side + " "
                               + str(strike) + " " + tf + ": " + err)

    result["fetched"] = fetched_any
    return result
