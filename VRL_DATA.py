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

VERSION  = "v13.10"
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
LOSS_STREAK_GATE_SCORE  = CFG.scoring("loss_streak_gate", 5)
EXCELLENCE_BYPASS_SCORE = CFG.scoring("excellence_bypass", 6)

SPREAD_1M_MIN_CE      = CFG.spread("ce_min", 2)
SPREAD_1M_MIN_PE      = CFG.spread("pe_min", 2)
SPREAD_1M_MIN_CE_DTE0 = CFG.spread("ce_min_dte0", 1)
SPREAD_1M_MIN_PE_DTE0 = CFG.spread("pe_min_dte0", 1)

RSI_1M_LOW         = CFG.rsi("1m_low", 30)
RSI_1M_HIGH_NORMAL = CFG.rsi("1m_high_normal", 60)
RSI_1M_HIGH_STRONG = CFG.rsi("1m_high_strong", 70)
RSI_1M_HIGH        = RSI_1M_HIGH_NORMAL
RSI_3M_LOW         = CFG.rsi("3m_low", 42)
RSI_3M_HIGH        = CFG.rsi("3m_high", 72)

ATR_SL_MULTIPLIER = CFG.risk("sl_multiplier", 2.0)
ATR_SL_MAX        = CFG.risk("sl_max", 25)
ATR_SL_CANDLES    = CFG.risk("sl_candles", 5)

TRAIL_DRAWDOWN_PCT     = CFG.trail("drawdown_pct", 25)
TRAIL_EMA_CANDLES_FAIL = CFG.trail("ema_candles_fail", 2)

EXPIRY_CONSOL_CANDLES  = CFG.expiry_cfg("consol_candles", 5)
EXPIRY_CONSOL_RANGE    = CFG.expiry_cfg("consol_range", 15)
EXPIRY_BREAKOUT_MIN    = CFG.expiry_cfg("breakout_min", 10)
EXPIRY_SL_MAX          = CFG.risk("sl_dte0", 15)
EXPIRY_TRAIL_PCT       = CFG.expiry_cfg("trail_pct", 20)
EXPIRY_START_HOUR      = CFG.expiry_cfg("start_hour", 9)
EXPIRY_START_MIN       = CFG.expiry_cfg("start_min", 45)
EXPIRY_CUTOFF_HOUR     = CFG.expiry_cfg("cutoff_hour", 15)
EXPIRY_CUTOFF_MIN      = CFG.expiry_cfg("cutoff_min", 0)
EXPIRY_FIB_PROXIMITY   = CFG.expiry_cfg("fib_proximity", 20)

SCALP_MODE_ENABLED = False

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

REENTRY_COOLDOWN_MIN = CFG.risk("reentry_cooldown_min", 5)

SESSION_SCORE_MIN = CFG.session_score_min()

WS_RECONNECT_DELAY = CFG.ws_reconnect_delay()
TICK_STALE_SECS    = CFG.ws_tick_stale_secs()

STATE_PERSIST_FIELDS = [
    "in_trade", "symbol", "token", "direction",
    "entry_price", "entry_time", "exit_phase",
    "phase1_sl", "phase2_sl",
    "qty", "trail_tightened", "profit_locked",
    "consecutive_losses", "daily_trades",
    "daily_losses", "daily_pnl", "peak_pnl",
    "mode", "iv_at_entry", "score_at_entry",
    "regime_at_entry", "last_exit_time",
    "last_exit_direction", "last_exit_peak",
    "prev_close",
    "candles_held",
    "_last_trail_candle",
]

# ── Prediction table + DTE profiles — read from config.yaml ──
def _build_prediction_table() -> dict:
    pred = CFG.get().get("prediction_table", {})
    table = {}
    for regime, sessions in pred.items():
        for session, value in sessions.items():
            table[(regime, session)] = value
    return table

PREDICTION_TABLE = _build_prediction_table()
DTE_PROFILES     = CFG.get()["dte_profiles"]

def get_dte_profile(dte: int) -> dict:
    return CFG.dte_profile(dte)

def get_session_block(hour: int, minute: int) -> str:
    mins = hour * 60 + minute
    if   mins < 10 * 60: return "OPEN"
    elif mins < 12 * 60: return "MORNING"
    elif mins < 14 * 60: return "AFTERNOON"
    else:                return "LATE"

def predict_trade(regime: str, session: str, score: int) -> dict:
    """
    Predict trade outcome from regime + session + score.
    Returns conservative / target / stretch in pts and rupees.
    """
    avg_peak   = PREDICTION_TABLE.get((regime, session),
                 PREDICTION_TABLE.get(("TRENDING", "MORNING"), 22))
    score_mult = 1.0 + (score - 5) * 0.05
    adj_peak   = avg_peak * score_mult
    c = round(adj_peak * 0.40)
    t = round(adj_peak * 0.65)
    s = round(adj_peak * 0.90)
    return {
        "avg_peak"       : round(adj_peak, 1),
        "conservative"   : c, "conservative_rs": c * LOT_SIZE,
        "target"         : t, "target_rs"      : t * LOT_SIZE,
        "stretch"        : s, "stretch_rs"     : s * LOT_SIZE,
    }

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
    global _ws_connected
    _ws_connected = False
    logger.warning("[WS] Closed: " + str(code) + " " + str(reason))

def _on_error(ws, code, reason):
    logger.error("[WS] Error: " + str(code) + " " + str(reason))

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
    if _kite is not None:
        try:
            quote = _kite.quote(["NSE:INDIA VIX"])
            vix   = quote.get("NSE:INDIA VIX", {}).get("last_price", 0)
            if vix and vix > 0:
                return float(vix)
        except Exception as e:
            logger.debug("[DATA] VIX quote fallback failed: " + str(e))
    return 0.0

def is_market_open() -> bool:
    now = now_ist()
    if now.weekday() >= 5:
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

def get_lot_size(kite=None) -> int:
    k = kite or _kite
    if k is None:
        return LOT_SIZE_BASE
    try:
        instruments = _get_nfo_instruments(k)
        for inst in instruments:
            if (inst.get("name") == "NIFTY"
                    and inst.get("instrument_type") == "CE"
                    and inst.get("lot_size", 0) > 0):
                lot = int(inst["lot_size"])
                logger.info("[DATA] Lot size from broker: " + str(lot))
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
    for attempt in range(2):
        try:
            raw = _kite.historical_data(
                instrument_token=int(token), from_date=from_dt, to_date=to_dt,
                interval=interval, continuous=False, oi=False)
            break
        except Exception as e:
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
    return df


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
    Uses 7-day fetch — handles weekends + holidays automatically.
    Kite returns only market-hours candles — last candle before today
    is always the previous session's close, regardless of how many
    non-trading days are in between.
    """
    global _spot_gap, _spot_prev_close
    result = {"gap_pts": 0.0, "gap_pct": 0.0, "prev_close": 0.0, "today_open": 0.0}
    try:
        if _kite is None:
            return result
        now     = datetime.now()
        from_dt = now - timedelta(days=7)  # 7 days covers any holiday combo
        raw = _kite.historical_data(
            instrument_token=NIFTY_SPOT_TOKEN,
            from_date=from_dt, to_date=now,
            interval="minute", continuous=False, oi=False)
        if not raw or len(raw) < 50:
            return result

        # Group by date to find previous session
        today_str = date.today().strftime("%Y-%m-%d")
        dates_seen = {}
        for c in raw:
            d = str(c["date"])[:10]
            if d not in dates_seen:
                dates_seen[d] = {"first_open": c["open"], "last_close": c["close"]}
            dates_seen[d]["last_close"] = c["close"]

        sorted_dates = sorted(dates_seen.keys())
        if today_str not in sorted_dates:
            logger.warning("[SPOT] Today not in fetched data")
            return result

        today_idx = sorted_dates.index(today_str)
        if today_idx == 0:
            logger.warning("[SPOT] No previous session in fetched data")
            return result

        prev_date  = sorted_dates[today_idx - 1]
        prev_close = float(dates_seen[prev_date]["last_close"])
        today_open = float(dates_seen[today_str]["first_open"])
        gap_pts    = round(today_open - prev_close, 2)
        gap_pct    = round(gap_pts / prev_close * 100, 2) if prev_close > 0 else 0.0

        _spot_gap        = gap_pts
        _spot_prev_close = prev_close
        result = {
            "gap_pts": gap_pts, "gap_pct": gap_pct,
            "prev_close": prev_close, "today_open": today_open,
        }
        logger.info("[SPOT] Gap: " + str(gap_pts) + "pts ("
                    + str(gap_pct) + "%) prev=" + str(prev_close)
                    + " (" + prev_date + ") open=" + str(today_open))
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
#  Entry fire: 9:45-15:10 | Scan from 9:15
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
