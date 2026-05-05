# ═══════════════════════════════════════════════════════════════
#  VRL_CONFIG.py — VISHAL RAJPUT TRADE v16.7
#  Central config loader. Loads config.yaml, validates required
#  v16.7 sections, exposes typed accessors.
#  Immutable at runtime — restart to reload.
# ═══════════════════════════════════════════════════════════════

import json
import logging
import os
import re
import time
from datetime import date

import pyotp
import requests
import yaml
from kiteconnect import KiteConnect

logger = logging.getLogger("vrl_live")

_CONFIG_PATH = os.environ.get(
    "VRL_CONFIG_PATH",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.yaml")
)

_cfg = None
_CONFIG_VERSION = "14.0"


class ConfigError(Exception):
    """Raised when config.yaml is missing or invalid."""
    pass


def _deep_get(d: dict, *keys, default=None):
    """Nested dict lookup."""
    for k in keys:
        if not isinstance(d, dict):
            return default
        d = d.get(k, default)
        if d is default:
            return default
    return d


def load(path: str = None) -> dict:
    """Load and validate config.yaml. Called once at startup."""
    global _cfg
    p = path or _CONFIG_PATH
    if not os.path.isfile(p):
        raise ConfigError("Config file not found: " + p)
    with open(p) as f:
        raw = yaml.safe_load(f)
    if not isinstance(raw, dict):
        raise ConfigError("Config file is empty or not a valid YAML dict")
    _validate(raw)
    _cfg = raw
    return _cfg


def _validate(cfg: dict):
    """Validate v15.2 required sections (nested entry: / exit: format)."""
    required = ["mode", "instrument", "lots", "entry", "exit",
                "strike", "risk", "market_hours"]
    for sec in required:
        if sec not in cfg:
            raise ConfigError("Missing required config section: " + sec)
    if cfg["mode"] not in ("paper", "live"):
        raise ConfigError("mode must be 'paper' or 'live', got: " + str(cfg["mode"]))
    inst = cfg["instrument"]
    for k in ("name", "lot_size", "spot_token"):
        if k not in inst:
            raise ConfigError("instrument." + k + " is required")
    if not isinstance(inst["lot_size"], int) or inst["lot_size"] <= 0:
        raise ConfigError("instrument.lot_size must be a positive integer")
    lots = cfg["lots"]
    for k in ("count", "size"):
        if k not in lots:
            raise ConfigError("lots." + k + " is required")
        if not isinstance(lots[k], int) or lots[k] <= 0:
            raise ConfigError("lots." + k + " must be a positive integer")
    # v16.7 validation: 3-gate entry (V6) + Vishal Clean filters required so
    # tuning changes land instead of silently falling back to defaults.
    # band_width_min / ema9_slope_lookback are optional display-only now
    # but kept in config for the dashboard's reject-reason translator.
    eb = (cfg.get("entry") or {}).get("ema9_band") or {}
    for k in ("body_pct_min", "warmup_until", "cutoff_after"):
        if k not in eb:
            raise ConfigError("entry.ema9_band." + k + " is required")
    # warmup_until / cutoff_after must be HH:MM. A typo like "9:35"
    # would slip past the engine's split-based parser in silent
    # reject-reason mode; catch it at config load.
    for _tk in ("warmup_until", "cutoff_after"):
        _ts = str(eb[_tk])
        try:
            _th, _tm = _ts.split(":")
            _th_i, _tm_i = int(_th), int(_tm)
            if not (0 <= _th_i < 24 and 0 <= _tm_i < 60):
                raise ValueError("out of range")
        except Exception as _te:
            raise ConfigError("entry.ema9_band." + _tk + " must be HH:MM "
                              "(24h), got: " + _ts + " (" + str(_te) + ")")
    xb = (cfg.get("exit") or {}).get("ema9_band") or {}
    for k in ("emergency_sl_pts", "eod_exit_time"):
        if k not in xb:
            raise ConfigError("exit.ema9_band." + k + " is required")
    # emergency_sl_pts must be a negative number — the engine's exit check
    # is `if pnl <= emergency_sl_pts`, so a non-negative value would fire
    # immediately on entry and blow up every trade.
    _esp = xb["emergency_sl_pts"]
    if not isinstance(_esp, (int, float)) or _esp >= 0:
        raise ConfigError("exit.ema9_band.emergency_sl_pts must be a "
                          "negative number, got: " + str(_esp))
    # eod_exit_time must be HH:MM with valid hour/minute — a typo would
    # crash the exit chain parser at runtime.
    _eod = str(xb["eod_exit_time"])
    try:
        _eh, _em = _eod.split(":")
        _eh_i, _em_i = int(_eh), int(_em)
        if not (0 <= _eh_i < 24 and 0 <= _em_i < 60):
            raise ValueError("out of range")
    except Exception as _e:
        raise ConfigError("exit.ema9_band.eod_exit_time must be HH:MM "
                          "(24h), got: " + _eod + " (" + str(_e) + ")")


# ── Accessors ────────────────────────────────────────────────

def get() -> dict:
    if _cfg is None:
        raise ConfigError("Config not loaded. Call VRL_CONFIG.load() first.")
    return _cfg


def mode() -> str:
    return get()["mode"]


def is_paper() -> bool:
    return mode() == "paper"


def is_live() -> bool:
    return mode() == "live"


# ── Instrument ──

def instrument_name() -> str:
    return get()["instrument"]["name"]


def lot_size() -> int:
    return get()["instrument"]["lot_size"]


def spot_token() -> int:
    return get()["instrument"]["spot_token"]


def vix_token() -> int:
    return get()["instrument"].get("vix_token", 264969)


# ── Strategy v15.2 (nested entry: / exit: / filters: paths) ──

def entry_ema9_band(key: str, default=None):
    """Read entry.ema9_band.<key>. Special-case cooldown_minutes so callers
    that still ask for the old name pick up the new `cooldown_minutes_same_dir`."""
    eb = (get().get("entry") or {}).get("ema9_band") or {}
    if key == "cooldown_minutes":
        if "cooldown_minutes_same_dir" in eb:
            return eb["cooldown_minutes_same_dir"]
    if key in eb:
        return eb[key]
    return default


def exit_ema9_band(key: str, default=None):
    xb = (get().get("exit") or {}).get("ema9_band") or {}
    return xb.get(key, default)


def straddle_filter(key: str, default=None):
    """v16: straddle is display-only. Reads entry.filters.straddle_display."""
    sf = ((get().get("entry") or {}).get("filters") or {}).get("straddle_display") or {}
    return sf.get(key, default)


def vwap_bonus(key: str, default=None):
    vb = ((get().get("entry") or {}).get("filters") or {}).get("vwap_bonus") or {}
    return vb.get(key, default)


def cooldown(key: str, default=None):
    return _deep_get(get(), "cooldown", key, default=default)


# ── Risk ──

def risk(key: str, default=None):
    return _deep_get(get(), "risk", key, default=default)


# ── Market Hours ──

def market_hours(key: str, default=None):
    return _deep_get(get(), "market_hours", key, default=default)


# ── Lab (untouched) ──

def lab(key: str, default=None):
    return _deep_get(get(), "lab", key, default=default)


# ── Websocket ──

def ws_reconnect_delay() -> int:
    return _deep_get(get(), "websocket", "reconnect_delay", default=5)


def ws_tick_stale_secs() -> int:
    return _deep_get(get(), "websocket", "tick_stale_secs", default=8)


def ws_max_reconnect_delay() -> int:
    return _deep_get(get(), "websocket", "max_reconnect_delay", default=300)


# ── Web ──

def web_port() -> int:
    return _deep_get(get(), "web", "port", default=8080)


def web_auth() -> bool:
    return _deep_get(get(), "web", "auth", default=False)


# ── Strike ──

def strike_cfg(key: str, default=None):
    return _deep_get(get(), "strike", key, default=default)


# ── Lookback ──

def lookback(tf: str) -> int:
    defaults = {"1m": 50, "3m": 60, "5m": 10}
    return defaults.get(tf, 50)


# === AUTH (merged from VRL_AUTH) ===
# Zerodha Kite authentication. Auto-login via TOTP.
# VRL_DATA is imported lazily inside each function because VRL_DATA
# imports VRL_CONFIG at top-level (CFG.load() is called there), so a
# top-level `import VRL_DATA` here would create a circular import.

def _read_token() -> dict:
    import VRL_DATA as D
    try:
        if os.path.isfile(D.TOKEN_FILE_PATH):
            with open(D.TOKEN_FILE_PATH) as f:
                return json.load(f)
    except json.JSONDecodeError as _je:
        # Corrupt token file (truncated mid-write, disk full, etc.) —
        # log explicitly so restart-loop rate-limit risks are visible
        # instead of silently triggering a fresh login every startup.
        logger.warning("[AUTH] Token file corrupted (" + str(_je)
                       + ") — triggering fresh login")
    except Exception as _re:
        logger.warning("[AUTH] Token read error: " + str(_re))
    return {}


def _write_token(data: dict):
    import VRL_DATA as D
    os.makedirs(D.STATE_DIR, exist_ok=True)
    tmp = D.TOKEN_FILE_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f)
    os.replace(tmp, D.TOKEN_FILE_PATH)


def _auto_login(kite) -> str:
    import VRL_DATA as D
    user_id     = os.getenv("ZERODHA_USER_ID", "")
    password    = os.getenv("ZERODHA_PASSWORD", "")
    totp_secret = os.getenv("TOTP_SECRET", "")
    api_secret  = D.KITE_API_SECRET
    # Fail fast with a named error if any credential is missing, instead
    # of letting the POST at line 274 send empty strings and Zerodha
    # reject with a generic "Invalid credentials" response.
    _missing = [n for n, v in (
        ("ZERODHA_USER_ID", user_id),
        ("ZERODHA_PASSWORD", password),
        ("TOTP_SECRET", totp_secret),
        ("KITE_API_SECRET", api_secret),
    ) if not v]
    if _missing:
        raise RuntimeError("[AUTH] Missing env vars: " + ", ".join(_missing))
    session     = requests.Session()

    logger.info("[AUTH] Step 1: Password login")
    r          = session.post("https://kite.zerodha.com/api/login",
                              data={"user_id": user_id, "password": password}, timeout=15)
    request_id = r.json()["data"]["request_id"]
    logger.info("[AUTH] Step 1 OK")

    logger.info("[AUTH] Step 2: TOTP")
    totp = pyotp.TOTP(totp_secret).now()
    session.post("https://kite.zerodha.com/api/twofa",
                 data={"user_id": user_id, "request_id": request_id,
                       "twofa_value": totp, "twofa_type": "totp"}, timeout=15)
    logger.info("[AUTH] Step 2 OK")
    time.sleep(2)

    logger.info("[AUTH] Step 3: Fetching request_token")
    login_url     = kite.login_url()
    request_token = ""

    r = session.get(login_url, timeout=10, allow_redirects=False)
    finish_url = r.headers.get("Location", "")
    logger.info("[AUTH] Step 3a: finish_url=" + finish_url[:60])

    try:
        r2  = session.get(finish_url, timeout=10, allow_redirects=False)
        loc = r2.headers.get("Location", "")
        m   = re.search(r"request_token=([A-Za-z0-9]+)", loc)
        if m:
            request_token = m.group(1)
    except Exception as e:
        m = re.search(r"request_token=([A-Za-z0-9]+)", str(e))
        if m:
            request_token = m.group(1)

    if not request_token:
        raise RuntimeError("[AUTH] request_token not found after finish step")

    logger.info("[AUTH] Step 3 OK — " + request_token[:8] + "...")

    logger.info("[AUTH] Step 4: Generating session")
    sess         = kite.generate_session(request_token, api_secret=api_secret)
    access_token = sess["access_token"]
    logger.info("[AUTH] Done ✓")
    return access_token


def _notify_auth_refreshed():
    """Reset VRL_DATA's auth-rejection flag so historical_data and
    WebSocket retries resume after a successful login / refresh."""
    try:
        import VRL_DATA as _D_auth
        _D_auth.notify_auth_refreshed()
    except Exception:
        pass


def get_kite():
    import VRL_DATA as D
    kite      = KiteConnect(api_key=D.KITE_API_KEY)
    saved     = _read_token()
    today_str = date.today().isoformat()

    # Delete tokens older than 1 day (never serve yesterday's token)
    if saved.get("date") and saved.get("date") < today_str:
        logger.warning("[AUTH] Stale token from " + saved.get("date") + " — ignoring")
        saved = {}

    if saved.get("date") == today_str and saved.get("access_token"):
        logger.info("[AUTH] Trying saved token")
        kite.set_access_token(saved["access_token"])
        try:
            kite.profile()
            logger.info("[AUTH] Token valid ✓")
            _notify_auth_refreshed()
            return kite
        except Exception:
            logger.warning("[AUTH] Saved token expired")

    for attempt in range(3):
        try:
            token = _auto_login(kite)
            kite.set_access_token(token)
            _write_token({"date": today_str, "access_token": token})
            logger.info("[AUTH] Auto-login successful ✓")
            _notify_auth_refreshed()
            return kite
        except Exception as e:
            logger.error("[AUTH] Attempt " + str(attempt + 1) + " failed: " + str(e))
            if attempt < 2:
                time.sleep(3)

    raise RuntimeError("[AUTH] All login attempts failed")


def force_fresh_login():
    """v12.15: Force fresh login, ignoring cached token. For cron use."""
    import VRL_DATA as D
    kite      = KiteConnect(api_key=D.KITE_API_KEY)
    today_str = date.today().isoformat()
    for attempt in range(3):
        try:
            token = _auto_login(kite)
            kite.set_access_token(token)
            _write_token({"date": today_str, "access_token": token})
            print("[AUTH] Fresh login OK ✓ token cached for " + today_str)
            return kite
        except Exception as e:
            print("[AUTH] Attempt " + str(attempt + 1) + " failed: " + str(e))
            if attempt < 2:
                time.sleep(5)
    print("[AUTH] All fresh login attempts failed")
    return None


def _tg_alert(msg):
    """Send auth alert to Telegram."""
    import VRL_DATA as D
    try:
        url = "https://api.telegram.org/bot" + D.TELEGRAM_TOKEN + "/sendMessage"
        requests.post(url, json={
            "chat_id": D.TELEGRAM_CHAT_ID,
            "text": msg, "parse_mode": "HTML"
        }, timeout=10)
    except Exception:
        pass


if __name__ == "__main__":
    # v12.15: Standalone execution for cron job at 8 AM
    import VRL_DATA as D
    print("[AUTH] Cron login starting — " + date.today().isoformat())
    # Skip on weekends / NSE holidays — no Telegram alert on holidays
    if hasattr(D, "is_trading_day") and not D.is_trading_day():
        print("[AUTH] " + date.today().isoformat()
              + " is not a trading day — skipping login")
        import sys as _sys
        _sys.exit(0)
    # Delete stale token if > 1 day old
    saved = _read_token()
    if saved.get("date") and saved.get("date") != date.today().isoformat():
        print("[AUTH] Stale token from " + saved.get("date") + " — deleting")
        try:
            os.remove(D.TOKEN_FILE_PATH)
        except Exception:
            pass
    result = force_fresh_login()
    if result:
        # Verify token actually works
        try:
            result.profile()
            _tg_alert("🔑 <b>AUTH OK</b> " + date.today().isoformat()
                      + "\nToken fresh + verified ✓")
            print("[AUTH] Cron login complete + verified ✓")
        except Exception as e:
            _tg_alert("⚠️ <b>AUTH WARNING</b>\nToken saved but profile check failed: "
                      + str(e)[:100])
            print("[AUTH] Token saved but verification failed: " + str(e))
    else:
        _tg_alert("🚨 <b>AUTH FAILED</b>\nCron login failed at 8:00 AM\n"
                  "Bot will retry at 9:10 startup\nCheck manually if repeated")
        print("[AUTH] ⚠️ Cron login FAILED")
