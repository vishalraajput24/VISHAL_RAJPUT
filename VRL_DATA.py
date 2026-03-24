# ═══════════════════════════════════════════════════════════════
#  VRL_DATA.py — VISHAL RAJPUT TRADE v12.13
#  Foundation layer. Settings, logging, market data, Greeks.
#  v12.13: Fib pivot points, expiry breakout mode,
#          spot consolidation detection, expiry-specific rules.
# ═══════════════════════════════════════════════════════════════

import os
import math
import time
import logging
import threading
from datetime import date, datetime, timedelta
from logging.handlers import RotatingFileHandler

import pandas as pd
from kiteconnect import KiteTicker

VERSION  = "v12.13"
BOT_NAME = "VISHAL RAJPUT TRADE"

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

PAPER_MODE       = True
KITE_API_KEY     = os.getenv("KITE_API_KEY", "")
KITE_API_SECRET  = os.getenv("KITE_API_SECRET", "")
TELEGRAM_TOKEN   = os.getenv("TG_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TG_GROUP_ID", "")

BASE_DIR         = os.path.expanduser("~")
LOGS_DIR         = os.path.join(BASE_DIR, "logs")
LIVE_LOG_DIR     = os.path.join(LOGS_DIR, "live")
LAB_LOG_DIR      = os.path.join(LOGS_DIR, "lab")
FLOW_LOG_DIR     = os.path.join(LOGS_DIR, "flow")
STATE_DIR        = os.path.join(BASE_DIR, "state")
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

INSTRUMENT_NAME  = "NIFTY"
EXCHANGE_NFO     = "NFO"
EXCHANGE_NSE     = "NSE"
LOT_SIZE_BASE    = 65
LOT_SIZE         = LOT_SIZE_BASE
STRIKE_STEP         = 100    # v12.10: 100-step for indicator stability
STRIKE_STEP_EXPIRY  = 50     # DTE=0: tighter step for liquid premiums
NIFTY_SPOT_TOKEN = 256265
INDIA_VIX_TOKEN  = 264969
RISK_FREE_RATE   = 0.065

MAX_DAILY_TRADES        = 999
MAX_DAILY_LOSSES        = 999
PROFIT_LOCK_PTS         = 150
PROFIT_LOCK_TRAIL_TF    = "3minute"
LOSS_STREAK_GATE_SCORE  = 5
EXCELLENCE_BYPASS_SCORE = 6

# v12.8: 1-min spread gates
SPREAD_1M_MIN_CE = 8    # CE needs +8pts — fights premium decay
SPREAD_1M_MIN_PE = 4    # PE needs +4pts — velocity advantage

# v12.12: Separate RSI zones per timeframe
RSI_1M_LOW  = 45   # 1-min entry zone lower
RSI_1M_HIGH = 65   # 1-min entry zone upper (20pt window — enter early)
RSI_3M_LOW  = 42   # 3-min permission zone lower
RSI_3M_HIGH = 72   # 3-min permission zone upper (30pt window — wider trend)

# v12.12: ATR-based SL
ATR_SL_MULTIPLIER = 2.0
ATR_SL_MAX        = 25     # Hard cap — never more than 25pts
ATR_SL_CANDLES    = 5      # Last N candles for ATR calculation

# v12.12: Trail drawdown
TRAIL_DRAWDOWN_PCT     = 25   # Exit when drawdown > 25% of peak
TRAIL_EMA_CANDLES_FAIL = 2    # Need 2 consecutive closes below EMA to exit

# v12.13: Expiry breakout mode (DTE=0)
EXPIRY_CONSOL_CANDLES  = 5    # Min candles for consolidation detection
EXPIRY_CONSOL_RANGE    = 15   # Max range (pts) to qualify as consolidation
EXPIRY_BREAKOUT_MIN    = 10   # Min pts beyond consolidation for breakout
EXPIRY_SL_MAX          = 20   # Hard cap SL on expiry (not 25)
EXPIRY_TRAIL_PCT       = 20   # Tighter trail on expiry (not 25%)
EXPIRY_START_HOUR      = 9    # Expiry entry window start
EXPIRY_START_MIN       = 45
EXPIRY_CUTOFF_HOUR     = 15   # Expiry entry window end
EXPIRY_CUTOFF_MIN      = 0
EXPIRY_FIB_PROXIMITY   = 20   # Within 20pts of fib level = near zone

SCALP_MODE_ENABLED = False

LOOKBACK_1M = 50
LOOKBACK_3M = 60
LOOKBACK_5M = 10

TRADE_START_HOUR  = 9
TRADE_START_MIN   = 38
ENTRY_CUTOFF_HOUR = 15
ENTRY_CUTOFF_MIN  = 10
MARKET_OPEN_HOUR  = 9
MARKET_OPEN_MIN   = 15
MARKET_CLOSE_HOUR = 15
MARKET_CLOSE_MIN  = 30

REENTRY_COOLDOWN_MIN = 5

SESSION_SCORE_MIN = {
    "OPEN"     : 5,
    "MORNING"  : 5,
    "AFTERNOON": 5,
    "LATE"     : 6,
}

WS_RECONNECT_DELAY = 5
TICK_STALE_SECS    = 8

STATE_PERSIST_FIELDS = [
    "in_trade", "symbol", "token", "direction",
    "entry_price", "entry_time", "exit_phase",
    "phase1_sl", "phase2_sl",
    "qty", "trail_tightened", "profit_locked",
    "consecutive_losses", "daily_trades",
    "daily_losses", "daily_pnl", "peak_pnl",
    "mode", "iv_at_entry", "score_at_entry",
    "regime_at_entry", "last_exit_time",
    "candles_held",
    "_last_trail_candle",
]

# v12.8: Prediction table — avg peak pts by (regime, session)
# Source: 4-day research data (moves CSV analysis)
PREDICTION_TABLE = {
    ("TRENDING_STRONG", "OPEN")      : 35,
    ("TRENDING_STRONG", "MORNING")   : 32,
    ("TRENDING_STRONG", "AFTERNOON") : 28,
    ("TRENDING_STRONG", "LATE")      : 42,
    ("TRENDING",        "OPEN")      : 25,
    ("TRENDING",        "MORNING")   : 22,
    ("TRENDING",        "AFTERNOON") : 20,
    ("TRENDING",        "LATE")      : 30,
    ("NEUTRAL",         "OPEN")      : 8,
    ("NEUTRAL",         "MORNING")   : 7,
    ("NEUTRAL",         "AFTERNOON") : 7,
    ("NEUTRAL",         "LATE")      : 6,
    ("CHOPPY",          "OPEN")      : 4,
    ("CHOPPY",          "MORNING")   : 4,
    ("CHOPPY",          "AFTERNOON") : 4,
    ("CHOPPY",          "LATE")      : 4,
    ("UNKNOWN",         "OPEN")      : 8,
    ("UNKNOWN",         "MORNING")   : 7,
    ("UNKNOWN",         "AFTERNOON") : 7,
    ("UNKNOWN",         "LATE")      : 6,
}

DTE_PROFILES = {
    "6+" : {
        "body_pct_min": 40,
        "rsi_low": 42, "rsi_high": 72,           # 3-min zone (30pt window)
        "rsi_1m_low": 45, "rsi_1m_high": 65,     # 1-min zone (20pt window)
        "max_gap_ema": 15, "volume_ratio_min": 1.0,
        "delta_min": 0.35, "delta_max": 0.65,
        "conv_sl_pts": 20, "conv_breakeven_pts": 15,
        "conv_trail_tf": "5minute", "conv_tighten_tf": "3minute",
        "conv_rsi_tighten": 76, "peak_drawdown_pct": 40, "peak_drawdown_min": 80,
        "rsi_exhaustion_min": 76, "rsi_exhaustion_pnl": 12,
        "gamma_rider_rsi_drop": 65, "gamma_rider_min_pnl": 8,
        "score_conv_min": 5, "conviction_allowed": True,
    },
    "3-5": {
        "body_pct_min": 40,
        "rsi_low": 42, "rsi_high": 72,
        "rsi_1m_low": 45, "rsi_1m_high": 65,
        "max_gap_ema": 13, "volume_ratio_min": 1.0,
        "delta_min": 0.35, "delta_max": 0.65,
        "conv_sl_pts": 18, "conv_breakeven_pts": 14,
        "conv_trail_tf": "5minute", "conv_tighten_tf": "3minute",
        "conv_rsi_tighten": 75, "peak_drawdown_pct": 40, "peak_drawdown_min": 80,
        "rsi_exhaustion_min": 76, "rsi_exhaustion_pnl": 12,
        "gamma_rider_rsi_drop": 65, "gamma_rider_min_pnl": 8,
        "score_conv_min": 5, "conviction_allowed": True,
    },
    "2" : {
        "body_pct_min": 40,
        "rsi_low": 42, "rsi_high": 72,
        "rsi_1m_low": 45, "rsi_1m_high": 65,
        "max_gap_ema": 12, "volume_ratio_min": 1.0,
        "delta_min": 0.35, "delta_max": 0.65,
        "conv_sl_pts": 15, "conv_breakeven_pts": 12,
        "conv_trail_tf": "3minute", "conv_tighten_tf": "1minute",
        "conv_rsi_tighten": 74, "peak_drawdown_pct": 38, "peak_drawdown_min": 70,
        "rsi_exhaustion_min": 75, "rsi_exhaustion_pnl": 10,
        "gamma_rider_rsi_drop": 64, "gamma_rider_min_pnl": 8,
        "score_conv_min": 5, "conviction_allowed": True,
    },
    "1" : {
        "body_pct_min": 40,
        "rsi_low": 42, "rsi_high": 72,
        "rsi_1m_low": 45, "rsi_1m_high": 65,
        "max_gap_ema": 12, "volume_ratio_min": 1.0,
        "delta_min": 0.35, "delta_max": 0.65,
        "conv_sl_pts": 12, "conv_breakeven_pts": 10,
        "conv_trail_tf": "3minute", "conv_tighten_tf": "1minute",
        "conv_rsi_tighten": 72, "peak_drawdown_pct": 35, "peak_drawdown_min": 60,
        "rsi_exhaustion_min": 74, "rsi_exhaustion_pnl": 8,
        "gamma_rider_rsi_drop": 63, "gamma_rider_min_pnl": 6,
        "score_conv_min": 5, "conviction_allowed": True,
    },
    "0" : {
        "body_pct_min": 40,
        "rsi_low": 42, "rsi_high": 72,
        "rsi_1m_low": 45, "rsi_1m_high": 65,
        "max_gap_ema": 15, "volume_ratio_min": 1.0,
        "delta_min": 0.30, "delta_max": 0.70,
        "conv_sl_pts": 10, "conv_breakeven_pts": 8,
        "conv_trail_tf": "1minute", "conv_tighten_tf": "1minute",
        "conv_rsi_tighten": 70, "peak_drawdown_pct": 30, "peak_drawdown_min": 40,
        "rsi_exhaustion_min": 72, "rsi_exhaustion_pnl": 6,
        "gamma_rider_rsi_drop": 60, "gamma_rider_min_pnl": 5,
        "score_conv_min": 5, "conviction_allowed": True,
    },
}

def get_dte_profile(dte: int) -> dict:
    if dte >= 6:   return DTE_PROFILES["6+"]
    elif dte >= 3: return DTE_PROFILES["3-5"]
    elif dte == 2: return DTE_PROFILES["2"]
    elif dte == 1: return DTE_PROFILES["1"]
    else:          return DTE_PROFILES["0"]

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
              REPORTS_DIR, SESSIONS_DIR, BACKUP_DIR]:
        os.makedirs(d, exist_ok=True)

def setup_logger(name: str, log_file: str, level=logging.DEBUG) -> logging.Logger:
    os.makedirs(os.path.dirname(log_file), exist_ok=True)
    lg = logging.getLogger(name)
    if lg.handlers:
        return lg
    lg.setLevel(level)
    fmt = logging.Formatter("%(asctime)s | %(levelname)-8s | %(message)s",
                            datefmt="%Y-%m-%d %H:%M:%S")
    fh = RotatingFileHandler(log_file, maxBytes=10*1024*1024, backupCount=5)
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(fmt)
    lg.addHandler(fh)
    ch = logging.StreamHandler()
    ch.setLevel(logging.INFO)
    ch.setFormatter(fmt)
    lg.addHandler(ch)
    return lg

logger = logging.getLogger("vrl_live")

_kite             = None
_token_cache      = {}
_token_cache_lock = threading.Lock()
_ticker           = None
_ticks            = {}
_tick_lock        = threading.Lock()
_subscribed       = set()
_subscribed_lock  = threading.Lock()
_ws_connected     = False

def init(kite_instance):
    global _kite
    _kite = kite_instance

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
    now = datetime.now()
    if now.weekday() >= 5:
        return False
    start = now.replace(hour=MARKET_OPEN_HOUR,  minute=MARKET_OPEN_MIN,  second=0)
    end   = now.replace(hour=MARKET_CLOSE_HOUR, minute=MARKET_CLOSE_MIN, second=0)
    return start <= now <= end

def is_trading_window(now: datetime = None) -> bool:
    if now is None:
        now = datetime.now()
    if not is_market_open():
        return False
    start = now.replace(hour=TRADE_START_HOUR, minute=TRADE_START_MIN, second=0)
    end   = now.replace(hour=ENTRY_CUTOFF_HOUR, minute=ENTRY_CUTOFF_MIN, second=0)
    return start <= now <= end

def get_lot_size(kite=None) -> int:
    k = kite or _kite
    if k is None:
        return LOT_SIZE_BASE
    try:
        instruments = k.instruments("NFO")
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

def get_historical_data(token: int, interval: str, lookback: int,
                        today_only: bool = False) -> pd.DataFrame:
    if _kite is None:
        return pd.DataFrame()
    # v12.11 FIX: Always go back at least 3 calendar days
    # Covers weekends (Sat+Sun) so Monday always gets Friday's data
    # Kite returns only market-hours candles — extra days cost nothing
    # This single fix solves: option EMA warmup, RSI warmup, gap detection
    min_from = datetime.now() - timedelta(days=3)
    minutes_per_candle = {
        "minute": 1, "3minute": 3, "5minute": 5,
        "15minute": 15, "30minute": 30, "60minute": 60,
    }.get(interval, 1)
    total_minutes  = lookback * minutes_per_candle * 2.5
    candidate_from = datetime.now() - timedelta(minutes=int(total_minutes) + 60)
    # Use whichever reaches further back
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


def calculate_atr(token: int, interval: str = "minute",
                  n_candles: int = None) -> float:
    """v12.12: Calculate ATR (Average True Range) for SL sizing."""
    if n_candles is None:
        n_candles = ATR_SL_CANDLES
    try:
        df = get_historical_data(token, interval, n_candles + 10)
        if df.empty or len(df) < n_candles + 1:
            return 0.0
        # True Range = high - low for each candle
        df["TR"] = df["high"] - df["low"]
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
    """v12.10: Returns 50-step on expiry day, 100-step otherwise."""
    if dte is not None and dte == 0:
        return STRIKE_STEP_EXPIRY
    return STRIKE_STEP

def resolve_atm_strike(spot_ltp: float, step: int = None) -> int:
    if step is None:
        step = STRIKE_STEP
    return int(round(spot_ltp / step) * step)

def get_nearest_expiry(kite=None, reference_date=None) -> date:
    if reference_date is None:
        reference_date = date.today()
    kite = kite or _kite
    if kite is None:
        raise RuntimeError("Kite not initialised")
    try:
        instruments = kite.instruments("NFO")
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
        instruments = kite.instruments("NFO")
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
    except Exception as e:
        logger.warning("[SPOT] get_spot_indicators error: " + str(e))
    return result


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
#  FIB PIVOT POINTS (v12.13)
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
#  SPOT CONSOLIDATION DETECTOR (v12.13)
#  Tracks last N 1-min candles for tight-range detection
#  Used by expiry breakout mode
# ═══════════════════════════════════════════════════════════════

_spot_1m_buffer = []   # List of (timestamp, open, high, low, close)
_SPOT_BUFFER_MAX = 20  # Keep last 20 candles

def update_spot_buffer(candle: dict):
    """Called by strategy loop or lab to feed 1-min spot candles."""
    global _spot_1m_buffer
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
    if len(_spot_1m_buffer) < n:
        return result

    recent = _spot_1m_buffer[-n:]
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
