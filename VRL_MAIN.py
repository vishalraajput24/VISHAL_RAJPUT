# ═══════════════════════════════════════════════════════════════
#  VRL_MAIN.py — VISHAL RAJPUT TRADE v20 (Vishal Clean V7+V10)
#  MERGED: VRL_CONFIG + VRL_DATA + VRL_ENGINE + VRL_LEVELS + VRL_LAB
#  V7 (SHADOW): 15-min | 2-gate (close>ema9l, RSI>=40 rising) | signals only
#  V10 (LIVE):   1-min  | P1+P2 (XLEG_CONFIRMED, |gap_vwap|<5, ema9h_gap>=0.8, LTP>VWAP)
#  V10 Exit: Emergency -12 | INITIAL(-12) → LOCK_4(@12) → LOCK_12(@24) →
#           LOCK_20(@30) → LOCK_30(@36) → LOCK_36(@40) → LOCK_50(@50+)
# ═══════════════════════════════════════════════════════════════

import csv
import json
import logging
import os
import re
import requests
import signal
import sys
import threading
import time
import numpy as np
import pandas as pd
import pyotp
import yaml
from copy import deepcopy
from datetime import date, datetime, timedelta, time as dtime
from logging.handlers import TimedRotatingFileHandler

from kiteconnect import KiteConnect, KiteTicker

class _DBNoop:
    """Safe no-op proxy for removed VRL_DB — any method call does nothing."""
    def __getattr__(self, name):
        return lambda *a, **kw: None
DB        = _DBNoop()  # VRL_DB removed — all DB calls silently no-op
_DB       = _DBNoop()  # no-op stub — used by web API handlers
_VDB      = _DBNoop()  # no-op stub — trade insert DB removed
_SC       = _DBNoop()  # no-op stub — scan DB removed
_DB_clean = _DBNoop()  # no-op stub — DB cleanup removed
_DB_dash  = _DBNoop()  # no-op stub — dashboard DB removed

logger = logging.getLogger("vrl_live")
lab_logger = logging.getLogger("vrl_lab")


# ===============================================================
# ===============================================================

# ═══════════════════════════════════════════════════════════════
#  Central config loader. Loads config.yaml, validates required
#  sections, exposes typed accessors.
#  Immutable at runtime — restart to reload.
# ═══════════════════════════════════════════════════════════════




_CONFIG_PATH = os.environ.get(
    "VRL_CONFIG_PATH",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.yaml")
)

_cfg = None


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
    os.makedirs(D.STATE_DIR, exist_ok=True)
    tmp = D.TOKEN_FILE_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f)
    os.replace(tmp, D.TOKEN_FILE_PATH)


def _auto_login(kite) -> str:
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
            notify_auth_refreshed()
    except Exception:
        pass


def get_kite():
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
    try:
        url = "https://api.telegram.org/bot" + D.TELEGRAM_TOKEN + "/sendMessage"
        requests.post(url, json={
            "chat_id": D.TELEGRAM_CHAT_ID,
            "text": msg, "parse_mode": "HTML"
        }, timeout=10)
    except Exception:
        pass


# ===============================================================
# ===============================================================

# ═══════════════════════════════════════════════════════════════
#
#  Market data (ticks, quotes, historical) stays on Kite.
#  This module handles: BUY entry, SELL exit, cancel, order-fill verification.
#
#  Auth flow (TOTP path — recommended, fully automated):
#    login(client_id, password) → ugid
#    verify_totp(api_key, pyotp.TOTP(secret).now()) → access_token
#
#  Env vars required in ~/.env:
#    MSTOCK_CLIENT_ID      — your MStock login ID (e.g. MA2081433)
#    MSTOCK_PASSWORD       — your MStock password
#    MSTOCK_API_KEY        — API key from MStock developer portal
#    MSTOCK_TOTP_SECRET    — TOTP secret from MStock Security settings
#                            (enable Authenticator App → copy the secret key)
# ═══════════════════════════════════════════════════════════════

# ── Constants ────────────────────────────────────────────────────────────────
MSTOCK_TOKEN_FILE = os.path.expanduser("~/state/mstock_token.json")
MSTOCK_EXCHANGE   = "NFO"
MSTOCK_PRODUCT    = "MIS"     # intraday
MSTOCK_VALIDITY   = "DAY"
MSTOCK_TAG        = "VRL"

# Status strings from MStock order book (case-insensitive compare done at use)
_STATUS_COMPLETE  = "complete"
_STATUS_REJECTED  = "rejected"
_STATUS_CANCELLED = "cancelled"


# ── Token file helpers ───────────────────────────────────────────────────────

def _ms_read_token() -> dict:
    try:
        if os.path.isfile(MSTOCK_TOKEN_FILE):
            with open(MSTOCK_TOKEN_FILE) as f:
                return json.load(f)
    except Exception as e:
        logger.warning(f"[MSTOCK] Token read error: {e}")
    return {}


def _ms_write_token(data: dict):
    os.makedirs(os.path.dirname(MSTOCK_TOKEN_FILE), exist_ok=True)
    tmp = MSTOCK_TOKEN_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f)
    os.replace(tmp, MSTOCK_TOKEN_FILE)


# ── Auth ─────────────────────────────────────────────────────────────────────

def _do_login_totp(mc, client_id: str, password: str,
                   api_key: str, totp_secret: str) -> str:
    """
    Automated login via TOTP (Authenticator App) — fully unattended.
    """
    logger.info("[MSTOCK] Step 1: Login")
    resp = mc.login(client_id, password)
    data = resp.json()
    if data.get("status") != "success":
        raise RuntimeError(f"[MSTOCK] Login failed: {data}")
    logger.info("[MSTOCK] Step 1 OK")

    logger.info("[MSTOCK] Step 2: TOTP verify")
    totp_code = pyotp.TOTP(totp_secret).now()
    resp = mc.verify_totp(api_key, totp_code)
    data = resp.json()
    if data.get("status") != "success":
        raise RuntimeError(f"[MSTOCK] verify_totp failed: {data}")
    access_token = data["data"]["access_token"]
    logger.info("[MSTOCK] Step 2 OK — session ready")
    return access_token


def get_mstock():
    """
    Return an authenticated MConnect instance.
    Reads cached daily token first; does full login only if needed.
    """
    from tradingapi_a.mconnect import MConnect

    api_key     = os.getenv("MSTOCK_API_KEY", "")
    totp_secret = os.getenv("MSTOCK_TOTP_SECRET", "")
    client_id   = os.getenv("MSTOCK_CLIENT_ID", "")
    password    = os.getenv("MSTOCK_PASSWORD", "")

    missing = [n for n, v in [
        ("MSTOCK_API_KEY",     api_key),
        ("MSTOCK_TOTP_SECRET", totp_secret),
        ("MSTOCK_CLIENT_ID",   client_id),
        ("MSTOCK_PASSWORD",    password),
    ] if not v]
    if missing:
        raise RuntimeError(
            f"[MSTOCK] Missing env vars: {', '.join(missing)}\n"
            f"  → Enable Authenticator App on MStock (Profile → Security)\n"
            f"  → Copy the TOTP secret and add to ~/.env as MSTOCK_TOTP_SECRET=..."
        )

    mc        = MConnect()
    today_str = datetime.now().strftime("%Y-%m-%d")
    saved     = _ms_read_token()

    if saved.get("date") == today_str and saved.get("access_token"):
        logger.info("[MSTOCK] Using cached daily token")
        mc.set_access_token(saved["access_token"])
        mc.set_api_key(api_key)
        return mc

    logger.info("[MSTOCK] No valid token — doing fresh login (TOTP)")
    access_token = _do_login_totp(mc, client_id, password, api_key, totp_secret)
    _ms_write_token({"date": today_str, "access_token": access_token})
    mc.set_access_token(access_token)
    mc.set_api_key(api_key)
    return mc


# ── Order fill verification ──────────────────────────────────────────────────

def ms_verify_fill(mc, order_id: str, timeout_secs: int = 10) -> tuple:
    """
    Poll MStock order status until COMPLETE or REJECTED/CANCELLED.
    Returns (fill_price, fill_qty). Returns (0.0, 0) on failure/timeout.
    """
    deadline = time.time() + timeout_secs
    while time.time() < deadline:
        try:
            resp = mc.get_order_details(order_id, _segment="E")
            data = resp.json()
            if data.get("status") != "success":
                time.sleep(0.5)
                continue

            order = data.get("data")
            if isinstance(order, list):
                order = order[-1] if order else {}
            if not order:
                time.sleep(0.5)
                continue

            status = str(order.get("status", "")).lower()
            if status == _STATUS_COMPLETE:
                fill_price = float(order.get("average_price", 0) or 0)
                fill_qty   = int(order.get("filled_quantity", 0) or 0)
                return fill_price, fill_qty
            elif status in (_STATUS_REJECTED, _STATUS_CANCELLED):
                logger.error(f"[MSTOCK] Order {order_id} {status}: "
                             f"{order.get('status_message', '')}")
                return 0.0, 0
        except Exception as e:
            logger.warning(f"[MSTOCK] verify_fill error: {e}")
        time.sleep(0.5)

    logger.error(f"[MSTOCK] Fill verification timeout: {order_id}")
    return 0.0, 0


# ── Entry order ───────────────────────────────────────────────────────────────

def ms_place_buy(mc, symbol: str, qty: int, limit_price: float,
                 timeout_secs: int = 8,
                 exchange: str = MSTOCK_EXCHANGE,
                 product: str = MSTOCK_PRODUCT) -> dict:
    """Place a LIMIT BUY (entry) on MStock."""
    try:
        resp = mc.place_order(
            _variety           = "regular",
            _tradingsymbol     = symbol,
            _exchange          = exchange,
            _transaction_type  = "BUY",
            _order_type        = "LIMIT",
            _quantity          = str(qty),
            _product           = product,
            _validity          = MSTOCK_VALIDITY,
            _price             = str(round(limit_price, 1)),
            _trigger_price     = "0",
            _disclosed_quantity= "0",
            _tag               = MSTOCK_TAG,
        )
        data = resp.json()
        if data.get("status") != "success":
            err = str(data.get("message", data))
            logger.error(f"[MSTOCK] BUY rejected: {err}")
            return {"ok": False, "fill_price": 0.0, "fill_qty": 0,
                    "order_id": "", "error": f"ORDER_REJECTED: {err}", "slippage": 0}

        order_id = str(data["data"]["order_id"])
        logger.info(f"[MSTOCK] LIMIT BUY placed: {order_id} {symbol} {exchange}/{product} limit={limit_price}")

        fill_price, fill_qty = ms_verify_fill(mc, order_id, timeout_secs)

        if fill_qty == 0:
            try:
                mc.cancel_order(order_id)
                logger.info(f"[MSTOCK] Entry cancelled — price moved: {order_id}")
            except Exception:
                pass
            return {"ok": False, "fill_price": 0.0, "fill_qty": 0,
                    "order_id": order_id, "error": "LIMIT_NOT_FILLED", "slippage": 0}

        ref_price = limit_price
        slippage  = round(fill_price - ref_price, 2)
        logger.info(f"[MSTOCK] ENTRY FILLED: price={fill_price} slippage={slippage}pts")
        return {"ok": True, "fill_price": fill_price, "fill_qty": fill_qty,
                "order_id": order_id, "error": "", "slippage": slippage}

    except Exception as e:
        logger.error(f"[MSTOCK] ms_place_buy exception: {e}")
        return {"ok": False, "fill_price": 0.0, "fill_qty": 0,
                "order_id": "", "error": str(e), "slippage": 0}


# ── Exit order ────────────────────────────────────────────────────────────────

def ms_place_sell(mc, symbol: str, qty: int,
                  timeout_secs: int = 8,
                  exchange: str = MSTOCK_EXCHANGE,
                  product: str = MSTOCK_PRODUCT) -> dict:
    """Place a MARKET SELL (exit) on MStock."""
    try:
        resp = mc.place_order(
            _variety           = "regular",
            _tradingsymbol     = symbol,
            _exchange          = exchange,
            _transaction_type  = "SELL",
            _order_type        = "MARKET",
            _quantity          = str(qty),
            _product           = product,
            _validity          = MSTOCK_VALIDITY,
            _price             = "0",
            _trigger_price     = "0",
            _disclosed_quantity= "0",
            _tag               = MSTOCK_TAG,
        )
        data = resp.json()
        if data.get("status") != "success":
            err = str(data.get("message", data))
            logger.error(f"[MSTOCK] SELL rejected: {err}")
            return {"ok": False, "fill_price": 0.0, "fill_qty": 0,
                    "order_id": "", "error": f"ORDER_REJECTED: {err}", "slippage": 0}

        order_id = str(data["data"]["order_id"])
        logger.info(f"[MSTOCK] MARKET SELL placed: {order_id}")

        fill_price, fill_qty = ms_verify_fill(mc, order_id, timeout_secs)

        if fill_qty == 0:
            logger.error(f"[MSTOCK] EXIT NOT FILLED — manual action required: {order_id}")
            return {"ok": False, "fill_price": 0.0, "fill_qty": 0,
                    "order_id": order_id,
                    "error": "EXIT_FAILED_MANUAL_REQUIRED", "slippage": 0}

        logger.info(f"[MSTOCK] EXIT FILLED: price={fill_price}")
        return {"ok": True, "fill_price": fill_price, "fill_qty": fill_qty,
                "order_id": order_id, "error": "", "slippage": 0}

    except Exception as e:
        logger.error(f"[MSTOCK] ms_place_sell exception: {e}")
        return {"ok": False, "fill_price": 0.0, "fill_qty": 0,
                "order_id": "", "error": str(e), "slippage": 0}


# ── Stock F&O convenience wrappers ───────────────────────────────────────────

def ms_stock_buy(mc, symbol: str, qty: int, limit_price: float,
                 positional: bool = False,
                 timeout_secs: int = 10) -> dict:
    """Buy a stock F&O contract (option or future) on MStock NFO."""
    product = "NRML" if positional else "MIS"
    return ms_place_buy(mc, symbol, qty, limit_price,
                        timeout_secs=timeout_secs,
                        exchange="NFO", product=product)


def ms_stock_sell(mc, symbol: str, qty: int,
                  positional: bool = False,
                  timeout_secs: int = 10) -> dict:
    """Sell/exit a stock F&O contract on MStock NFO."""
    product = "NRML" if positional else "MIS"
    return ms_place_sell(mc, symbol, qty,
                         timeout_secs=timeout_secs,
                         exchange="NFO", product=product)


def ms_get_stock_positions(mc) -> list:
    """Return all open stock F&O positions from MStock (NFO only, non-NIFTY/BANKNIFTY)."""
    try:
        resp = mc.get_net_position()
        data = resp.json()
        net  = (data.get("data") or {}).get("net", []) if data.get("status") == "success" else []
        return [p for p in net
                if p.get("exchange") == "NFO"
                and p.get("quantity", 0) != 0
                and not str(p.get("tradingsymbol", "")).startswith("NIFTY")
                and not str(p.get("tradingsymbol", "")).startswith("BANKNIFTY")]
    except Exception as e:
        logger.error(f"[MSTOCK] ms_get_stock_positions error: {e}")
        return []


# ── Startup banner helper ────────────────────────────────────────────────────

def ms_get_banner_line() -> str:
    """Return a one-liner for the bot startup Telegram banner."""
    import base64
    client_id = os.getenv("MSTOCK_CLIENT_ID", "MStock")
    try:
        mc = get_mstock()

        # ── Name from JWT payload ──────────────────────────────────────
        name = ""
        try:
            saved = _ms_read_token()
            jwt   = saved.get("access_token", "")
            if jwt:
                payload_b64 = jwt.split(".")[1]
                padding = 4 - len(payload_b64) % 4
                if padding != 4:
                    payload_b64 += "=" * padding
                payload = json.loads(base64.urlsafe_b64decode(payload_b64))
                name = str(payload.get("CLIENTNAME", "")).strip().title()
        except Exception:
            pass

        # ── Fund summary (balance) ─────────────────────────────────────
        avail_str = ""
        used_str  = ""
        try:
            f  = mc.get_fund_summary()
            fd = f.json()
            if fd.get("status") == "success":
                rows = fd.get("data") or []
                row = next(
                    (r for r in rows if str(r.get("SEG", "")).upper() in ("A", "E", "EQUITY")),
                    rows[0] if rows else {}
                )
                avail = float(row.get("AVAILABLE_BALANCE") or row.get("NET") or 0)
                used  = float(row.get("AMOUNT_UTILIZED") or row.get("LIMIT_SOD") or 0)
                avail_str = " | Avail: ₹{:,.0f}".format(avail)
                used_str  = " | Used: ₹{:,.0f}".format(used)
        except Exception:
            pass

        label = name if name else client_id
        return f"MStock: {label}{avail_str}{used_str}"

    except Exception as e:
        logger.warning(f"[MSTOCK] banner_line error: {e}")
        return f"MStock: {client_id} (login pending)"


# ── Quick connection test ─────────────────────────────────────────────────────

def ms_test_connection() -> bool:
    """Call from auth script to confirm MStock is working. Checks fund summary."""
    try:
        mc   = get_mstock()
        resp = mc.get_fund_summary()
        data = resp.json()
        ok   = data.get("status") == "success"
        if ok:
            logger.info("[MSTOCK] Connection test OK")
        else:
            logger.warning(f"[MSTOCK] Connection test FAILED: {data}")
        return ok
    except Exception as e:
        logger.error(f"[MSTOCK] Connection test error: {e}")
        return False


# ===============================================================
# ===============================================================

# ═══════════════════════════════════════════════════════════════
#  Foundation layer. Settings, logging, market data, Greeks.
# ═══════════════════════════════════════════════════════════════





VERSION  = "v20"
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

# ── Module self-reference aliases (so D.xxx, CFG.xxx, LEVELS.xxx etc. all resolve
#    to this module after the merge of VRL_CONFIG/DATA/ENGINE/LEVELS/LAB) ──
import sys as _sys
D = _sys.modules[__name__]
CFG = _sys.modules[__name__]
LEVELS = _sys.modules[__name__]
CHARGES = _sys.modules[__name__]
MSTOCK = _sys.modules[__name__]
load()  # initialize CONFIG singleton before any CFG.xxx calls

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
# always agree on the token location..
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
STATE_FILE_PATH        = os.path.join(STATE_DIR, "vrl_live_state.json")
V8_STATE_FILE_PATH     = os.path.join(STATE_DIR, "vrl_v8_state.json")
SHADOW_STATE_FILE_PATH = os.path.join(STATE_DIR, "vrl_shadow_state.json")
PID_FILE_PATH          = os.path.join(STATE_DIR, "vrl_live.pid")
TOKEN_FILE_PATH     = os.path.join(STATE_DIR, "access_token.json")

# ── All constants now read from config.yaml via VRL_CONFIG ──
INSTRUMENT_NAME  = CFG.instrument_name()
EXCHANGE_NFO     = CFG.get()["instrument"].get("exchange_nfo", "NFO")
EXCHANGE_NSE     = CFG.get()["instrument"].get("exchange_nse", "NSE")
LOT_SIZE_BASE    = CFG.lot_size()
LOT_SIZE         = LOT_SIZE_BASE
STRIKE_STEP         = CFG.strike_cfg("step_normal", 50)
STRIKE_STEP_EXPIRY  = CFG.strike_cfg("step_dte0", 50)
NIFTY_SPOT_TOKEN = CFG.spot_token()
INDIA_VIX_TOKEN  = CFG.vix_token()

LOOKBACK_1M = CFG.lookback("1m")
LOOKBACK_3M = CFG.lookback("3m")
LOOKBACK_5M = CFG.lookback("5m")

TRADE_START_HOUR  = CFG.market_hours("trade_start_hour", 9)
TRADE_START_MIN   = CFG.market_hours("trade_start_min", 15)
ENTRY_CUTOFF_HOUR = CFG.market_hours("entry_cutoff_hour", 15)
ENTRY_CUTOFF_MIN  = CFG.market_hours("entry_cutoff_min", 0)
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
    # Exit state
    "peak_pnl", "candles_held",
    # v15.0 entry context + band trail
    "entry_mode", "entry_ema9_high", "entry_ema9_low",
    "entry_band_position", "entry_body_pct",
    "current_ema9_high", "current_ema9_low", "last_band_check_ts",
    "other_token",
    "_last_cleanup_date",
    # v16.0 ratchet state
    "active_ratchet_tier", "active_ratchet_sl",
    # Milestone + scan throttling — restored so restarts mid-trade don't
    # lose dedup state and re-fire milestone alerts / scan the same bar twice.
    "_last_milestone", "_last_candle_held_min", "_last_scan_key",
    # Last exit memory
    "last_exit_time", "last_exit_direction", "last_exit_peak",
    "last_exit_reason",
    # Daily
    "daily_pnl",
    # Bot control
    "paused", "prev_close",
    "_exit_failed",
    # Legacy compat (kept for VRL_TRADE SL-M + restart resume)
    "lot1_active", "lot2_active", "lots_split",
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


def audit_log_paths() -> dict:
    """v15.2.5 one-shot report of which log directories exist.

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
_ws_reconnect_attempts = 0
_ws_reconnect_delay = 1
# Cap on the exponential-backoff reconnect delay. Configurable via
# websocket.max_reconnect_delay so extended Kite outages don't hammer
# the server every 60s. Defaults to 300s (5 min) — comfortable for
# Kite's rate limits while still recovering quickly.
_ws_max_delay = CFG.ws_max_reconnect_delay()

# ── auth-rejection backoff ───────────────────
# When Kite's nightly 03:30 session invalidation kills the token,
# every historical_data / quote / LTP call raises "Incorrect
# api_key or access_token". Without a guard, the bot retries
# every 1-2 seconds for hours, flooding the log with 13K+ warnings.
# This flag stops all retries until VRL_CONFIG refreshes the token
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
                           "until re-auth via VRL_CONFIG.")
        _auth_rejected = True


# ── cross-module "trade was taken" signal ────
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


# ── active trade token for LAB persistence ──
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


# ── Post-exit observation registry ───────────────────────────────
# When a trade exits, VRL_MAIN registers the just-exited strike+token
# here for N minutes so VRL_LAB can keep persisting candles for it
# (otherwise CSV/DB cuts off at exit and we lose post-exit price data
# for analysis). The registry is auto-pruned on every read.
_post_exit_obs = []           # list of dicts: {strike, token, side, expire_at}
_post_exit_obs_lock = threading.Lock()


def register_post_exit_observation(token: int, strike: int, side: str,
                                   expire_at: float):
    """VRL_MAIN calls this on trade exit. expire_at is epoch seconds
    after which the observation is dropped."""
    global _post_exit_obs
    with _post_exit_obs_lock:
        _post_exit_obs.append({
            "token": int(token), "strike": int(strike),
            "side": str(side), "expire_at": float(expire_at),
        })


def get_post_exit_observations() -> list:
    """VRL_LAB calls this every collection cycle. Returns currently
    active (un-expired) observations as a list of dicts. Prunes
    expired entries as a side-effect."""
    global _post_exit_obs
    now = time.time()
    with _post_exit_obs_lock:
        _post_exit_obs = [o for o in _post_exit_obs if o["expire_at"] > now]
        return [dict(o) for o in _post_exit_obs]


def notify_auth_refreshed():
    """Called by VRL_CONFIG on successful login / token refresh.
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
    global _ws_connected, _ws_reconnect_attempts, _ws_reconnect_delay
    _ws_connected = True
    _ws_reconnect_attempts = 0
    _ws_reconnect_delay = 1
    logger.info("[WS] Connected")
    with _subscribed_lock:
        if _subscribed:
            ws.subscribe(list(_subscribed))
            ws.set_mode(ws.MODE_FULL, list(_subscribed))
    return
def _on_close(ws, code, reason):
    global _ws_connected, _ticker, _ws_reconnect_attempts, _ws_reconnect_delay
    _ws_connected = False
    reason_str = str(reason or "")
    logger.warning("[WS] Closed: " + str(code) + " " + reason_str)
    if "403" in reason_str or "Forbidden" in reason_str:
        logger.warning("[WS] 403 Forbidden — auth required")
        _set_auth_rejected()
        try:
            if _ticker: _ticker.close()
        except Exception as _ce:
            logger.debug("[WS] ticker.close() on 403 failed: " + str(_ce))
        return
    if _ws_reconnect_attempts < 10:
        delay = min(_ws_reconnect_delay * (2 ** _ws_reconnect_attempts), _ws_max_delay)
        _ws_reconnect_attempts += 1
        logger.info(f"[WS] Reconnecting in {delay}s (attempt {_ws_reconnect_attempts})")
        time.sleep(delay)
        try:
            start_websocket()
        except Exception as e:
            logger.error("[WS] Reconnect failed: " + str(e))
    else:
        logger.critical("[WS] Max reconnect attempts reached")
    return
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

def subscribe_tokens(tokens: list) -> set:
    """Subscribe to WS feed for the given tokens. Returns the set of
    tokens actually accepted (empty set on failure). Callers that need
    to track what actually got subscribed should use the return value
    rather than the input list — prior code assumed all inputs made
    it through, leaking tokens on partial failure."""
    global _subscribed
    with _subscribed_lock:
        new = set(int(t) for t in tokens if t)
        if _ticker and _ws_connected:
            try:
                _ticker.subscribe(list(new))
                _ticker.set_mode(_ticker.MODE_FULL, list(new))
            except Exception as _e:
                logger.warning("[WS] Subscribe failed: " + str(_e))
                return set()
        _subscribed.update(new)
    logger.info("[WS] Subscribed: " + str(new))
    return new

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
    v13.10 (Auto-heal stale WebSocket. If spot tick is 3+ min stale during
    market hours, re-authenticate Kite and restart WebSocket. Rate limited to 1 per 10min.
    Called from strategy loop every cycle.
    """
    global _last_reconnect_attempt, _kite, _ticker
    if not is_market_open():
        return
    if _is_auth_rejected():
        return
    # Check if spot tick is stale (v13.10: tightened from 5min to 3min)
    with _tick_lock:
        spot_entry = _ticks.get(NIFTY_SPOT_TOKEN)
    if spot_entry and (time.time() - spot_entry["ts"]) < 180:
        return  # tick is fresh (< 3 min), no action needed
    # If last tick was not from today, market never opened — holiday/weekend, don't alert
    from datetime import date as _date
    _last_tick_date = _date.fromtimestamp(spot_entry["ts"]) if spot_entry else None
    if _last_tick_date != _date.today():
        return
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
#  TRADING HOLIDAYS — static fallback list (used only before 9:35
#  when dynamic detection hasn't run yet). Dynamic tick-based
#  detection overrides this after 9:35 IST each day.
#  Keep updated as a safety net; dynamic detection is primary.
# ═══════════════════════════════════════════════════════════════
TRADING_HOLIDAYS = {
    "2026-01-26",  # Republic Day
    "2026-02-19",  # Mahashivratri
    "2026-03-05",  # Holi
    "2026-03-27",  # Eid-ul-Fitr
    "2026-04-03",  # Good Friday
    "2026-04-14",  # Ambedkar Jayanti
    "2026-05-01",  # Maharashtra Day
    "2026-05-27",  # Bakri Eid
    "2026-05-28",  # Buddha Purnima
    "2026-08-15",  # Independence Day
    "2026-08-27",  # Ganesh Chaturthi
    "2026-10-02",  # Gandhi Jayanti
    "2026-10-21",  # Diwali Laxmi Pujan
    "2026-11-05",  # Guru Nanak Jayanti
    "2026-12-25",  # Christmas
}

# ── Dynamic holiday detection state ──────────────────────────
# Avoids need to update TRADING_HOLIDAYS manually. Detects
# holidays AND special Sunday sessions (Budget day etc.) by
# checking whether Nifty spot ticks actually arrived today.
_dyn_holiday_cache: dict = {}   # {"YYYY-MM-DD": True/False/None}
_dyn_last_fail_ts: float = 0.0  # epoch time of last API failure — throttle retries to 1/5min

def _detect_market_active_today() -> bool | None:
    """
    Dynamically detect if market is actually trading today using
    Kite's quote API last_trade_time — this is NSE's own timestamp,
    not our receive time, so it correctly shows yesterday's date on
    holidays even if the WS sends a cached "last known" price on connect.

    Returns True  — NSE last_trade_time is today → market active.
    Returns False — last_trade_time is from a previous day → holiday.
    Returns None  — too early (before 9:20) or kite not ready yet.
    Result cached per calendar day — only 1 API call per day.
    """
    today = datetime.now().strftime("%Y-%m-%d")
    if today in _dyn_holiday_cache and _dyn_holiday_cache[today] is not None:
        return _dyn_holiday_cache[today]

    now = datetime.now()
    # Only check after 9:20 IST (5 min into session, enough time for first trade)
    check_after = now.replace(hour=9, minute=20, second=0, microsecond=0)
    if now < check_after:
        return None  # too early — caller uses static list as fallback

    # kite must be initialised
    if _kite is None:
        return None

    # Throttle retries: if last API call failed, wait 5 min before retrying
    global _dyn_last_fail_ts
    import time as _time
    if _dyn_last_fail_ts and (_time.time() - _dyn_last_fail_ts) < 300:
        return None  # cooldown — caller uses static list

    try:
        quote = _kite.quote(["NSE:NIFTY 50"])
        ltt = (quote.get("NSE:NIFTY 50") or {}).get("last_trade_time")
        if ltt is None:
            return None  # API error — don't decide
        # last_trade_time can be a datetime or string "YYYY-MM-DD HH:MM:SS"
        if hasattr(ltt, "strftime"):
            ltt_date = ltt.strftime("%Y-%m-%d")
        else:
            ltt_date = str(ltt)[:10]

        if ltt_date == today:
            _dyn_holiday_cache[today] = True
            logger.info(f"[DATA] Dynamic: market ACTIVE today — NSE last_trade_time={ltt_date}")
            return True
        else:
            _dyn_holiday_cache[today] = False
            logger.info(f"[DATA] Dynamic: market HOLIDAY today — NSE last_trade_time={ltt_date} (not today)")
            return False
    except Exception as _e:
        _dyn_last_fail_ts = _time.time()  # start cooldown
        logger.debug(f"[DATA] Dynamic holiday check failed: {_e} — using static list (retry in 5m)")
        return None  # fall back to static list on any API error


def is_trading_day(now: datetime = None) -> bool:
    """True only on weekdays. Uses dynamic detection after 9:35, static list before."""
    if now is None:
        now = datetime.now()
    if now.weekday() >= 5:
        return False  # always skip weekends
    # After 9:35: dynamic detection is authoritative
    active = _detect_market_active_today()
    if active is True:
        return True   # ticks confirmed — even a special Sunday session
    if active is False:
        return False  # no ticks → holiday
    # Before 9:35 or no tick data yet — fall back to static list
    return now.strftime("%Y-%m-%d") not in TRADING_HOLIDAYS


def is_market_open() -> bool:
    now = datetime.now()
    if not is_trading_day(now):
        return False
    start = now.replace(hour=MARKET_OPEN_HOUR,  minute=MARKET_OPEN_MIN,  second=0, microsecond=0)
    end   = now.replace(hour=MARKET_CLOSE_HOUR, minute=MARKET_CLOSE_MIN, second=0, microsecond=0)
    return start <= now < end

def is_trading_window(now: datetime = None) -> bool:
    if now is None:
        now = datetime.now()
    if not is_market_open():
        return False
    start = now.replace(hour=TRADE_START_HOUR, minute=TRADE_START_MIN, second=0, microsecond=0)
    end   = now.replace(hour=ENTRY_CUTOFF_HOUR, minute=ENTRY_CUTOFF_MIN, second=0, microsecond=0)
    return start <= now < end

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
    # Hold the lock across the entire miss→fetch→write flow so two
    # threads that both miss the cache don't both end up parsing the
    # 2000-row instrument dump.
    with _lot_size_cache_lock:
        if today_iso in _lot_size_cache:
            return _lot_size_cache[today_iso]
        try:
            instruments = _get_nfo_instruments(k)
            for inst in instruments:
                if (inst.get("name") == "NIFTY"
                        and inst.get("instrument_type") == "CE"
                        and inst.get("lot_size", 0) > 0):
                    lot = int(inst["lot_size"])
                    logger.info("[DATA] Lot size from broker: " + str(lot)
                                + " (cached for " + today_iso + ")")
                    _lot_size_cache[today_iso] = lot
                    # Evict stale dates (keep only today).
                    for k_date in list(_lot_size_cache.keys()):
                        if k_date != today_iso:
                            del _lot_size_cache[k_date]
                    return lot
        except Exception as e:
            logger.warning("[DATA] Lot size fetch failed: " + str(e))
    return LOT_SIZE_BASE

# ── Historical data cache — keyed by candle bucket so it self-invalidates
# when a new candle closes. A 30s fixed TTL used to cause two separate API
# hits per 3-min bucket; now we only refetch when the bucket flips.
_hist_cache = {}
_hist_cache_lock = threading.Lock()
_HIST_CACHE_MAX = 256  # hard cap on entries — prevents unbounded growth

_INTERVAL_SECS = {
    "minute":    60,
    "3minute":   180,
    "5minute":   300,
    "15minute":  900,
    "30minute":  1800,
    "60minute":  3600,
    "hour":      3600,
    "day":       86400,
}

def _candle_bucket(interval: str) -> int:
    """Current epoch floor-divided by the candle width. Bumps on each
    candle close, so callers naturally miss the cache when a new bar
    is available and hit otherwise."""
    secs = _INTERVAL_SECS.get(interval, 60)
    return int(time.time()) // secs

def _hist_cache_key(token: int, interval: str, lookback: int) -> str:
    return (str(token) + "|" + interval + "|" + str(lookback)
            + "|" + str(_candle_bucket(interval)))

def _hist_cache_get(key: str):
    with _hist_cache_lock:
        entry = _hist_cache.get(key)
        if entry:
            return entry["df"].copy()
    return None

def _hist_cache_put(key: str, df):
    with _hist_cache_lock:
        _hist_cache[key] = {"df": df.copy(), "ts": time.time()}
        # Hard cap: drop oldest entries if over max
        if len(_hist_cache) > _HIST_CACHE_MAX:
            ordered = sorted(_hist_cache.items(), key=lambda kv: kv[1]["ts"])
            for k, _v in ordered[:len(_hist_cache) - _HIST_CACHE_MAX]:
                del _hist_cache[k]

def get_historical_data(token: int, interval: str, lookback: int,
                        today_only: bool = False) -> pd.DataFrame:
    if _kite is None:
        return pd.DataFrame()
    # Check cache first — key includes the current candle bucket, so a
    # fresh fetch is triggered exactly once per candle close.
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


def get_option_1min(token: int, lookback: int = 10) -> pd.DataFrame:
    """v16.6: Fetch option 1-min OHLC + EMA9 bands. Used for the
    1-min "early peek" entry path so the bot can fire BEFORE the
    full 3-min candle closes when momentum confirms early.
    Same indexing convention as get_option_3min: iloc[-1] is the
    live forming candle, iloc[-2] is the last closed."""
    df = get_historical_data(token, "minute", lookback)
    if df.empty:
        return df
    return add_indicators(df)


# ═══════════════════════════════════════════════════════════════
#  v15.2 STRADDLE EXPANSION HELPERS — used by Gate 7
#  Read live ATM CE+PE 3-min closes and compare current vs N min ago.
#  Returns None on missing data so the gate can reject explicitly.
# ═══════════════════════════════════════════════════════════════

def get_active_strike_step(dte: int = None) -> int:
    """v13.3: True ATM — 50-step for ALL DTE."""
    return 50

def resolve_atm_strike(spot_ltp: float, step: int = None) -> int:
    # Guard against spot_ltp <= 0 (WS tick glitch, pre-open read before
    # first tick lands). Returning 0 cascaded into strike-0 lookups that
    # silently produced empty token dicts; return 0 but log a warning so
    # the caller has a clear breadcrumb.
    if not spot_ltp or spot_ltp <= 0:
        logger.warning("[DATA] resolve_atm_strike called with spot_ltp="
                       + str(spot_ltp) + " — returning 0")
        return 0
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
            # Do NOT cache incomplete results — if only CE was found
            # (e.g., pre-listing on gap-open), a cached {"CE": ...} would
            # keep returning the incomplete dict, causing the next trade
            # to manage the missing side with token=None.
            logger.warning("[DATA] Token resolve incomplete: strike=" + str(strike)
                           + " found=" + str(list(result.keys()))
                           + " — skipping cache, will retry")
            return dict(result)
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

# ═══════════════════════════════════════════════════════════════
#  SPOT INTELLIGENCE LAYER (v12.11)
#  Always reliable — spot has full multi-day history from Kite
#  Used for: gap detection, regime backup, direction, alignment
# ═══════════════════════════════════════════════════════════════

_prev_spot_spread_3m = None   # cached by get_spot_indicators for spread_prev

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




# ═══════════════════════════════════════════════════════════════
#  v12.15: WARNING SYSTEM (all warnings only — no blocking)
#  Bias 9:20 | Straddle 9:30 | VIX+Hourly RSI continuous
#  Entry fire: 9:30-15:10 | Scan from 9:15
# ═══════════════════════════════════════════════════════════════

_straddle_open     = 0.0
_straddle_captured = False
_daily_bias        = "UNKNOWN"
_daily_bias_done   = False
_hourly_rsi        = 0.0
_hourly_rsi_ts     = 0


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


def run_warnings(kite, state, expiry, dte, spot_ltp, now):
    import time as _t
    msgs = []
    upd = {}
    # Skip all warnings on weekends and NSE holidays — no Telegram spam
    if not is_trading_day(now):
        return msgs, upd
    # 1. Daily bias 9:20 \u2014 computed for internal use only (no Telegram)
    if now.hour == 9 and 20 <= now.minute <= 22 and not state.get("_bias_done"):
        try:
            compute_daily_bias(kite)
            upd["_bias_done"] = True
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
                pass  # straddle value captured internally, no Telegram alert
        except Exception as _e:
            logger.warning("[WARN] Straddle: " + str(_e))
    # 4. Hourly RSI (every hour — only during market hours)
    if (is_market_open() and now.minute == 0 and now.second < 35
            and (_t.time() - state.get("_hourly_rsi_ts", 0)) > 3000):
        try:
            hr = check_hourly_rsi(kite)
            upd["_hourly_rsi_ts"] = _t.time()
            # hourly RSI computed for internal use only \u2014 no Telegram alert
        except Exception as _e:
            logger.warning("[WARN] Hourly: " + str(_e))
    return msgs, upd


def reset_daily_warnings():
    global _straddle_open, _straddle_captured, _daily_bias, _daily_bias_done
    global _hourly_rsi, _hourly_rsi_ts
    _straddle_open = 0.0
    _straddle_captured = False
    _daily_bias = "UNKNOWN"
    _daily_bias_done = False
    _hourly_rsi = 0.0
    _hourly_rsi_ts = 0


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
#  ensure_option_history() — single entry point for any
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
    table_map = {"minute": "option_1min", "3minute": "option_3min"}
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


# ===============================================================
# ===============================================================

# ═══════════════════════════════════════════════════════════════
#  Timeframe: 15-minute option candles (current single-strategy)
#  Entry: 2 gates (option-side only).
#    1. 15-min candle close > EMA9_low (option)
#    2. RSI >= 40 AND rising (RSI[fired] > RSI[prior])
#  Exit chain (TICK-based throughout):
#    1. EMERGENCY_SL: -12 pts (hard, immediate on tick)
#    2. EOD_EXIT: 15:20
#    3. VISHAL_TRAIL (peak ratchet):
#         peak <  12: SL = entry - 12  (INITIAL)
#         peak >= 12: SL = entry        (LOCK_BE)
#         peak >= 24: SL = entry + 12   (LOCK_12)
#         peak >= 30: SL = entry + 20   (LOCK_20)
#         peak >= 36: SL = entry + 24   (LOCK_24)
#         peak >= 40: SL = entry + 36   (LOCK_36)
#         peak >= 48: SL = entry + 36   (12-step continues)
#         peak >= 50: SL = entry + 50   (LOCK_50)
#         peak >= 60+: max(12-step, 50) — keeps ratcheting
#  Cooldown: 0 (removed — fresh entries always available).
# ═══════════════════════════════════════════════════════════════




def get_margin_available(kite) -> float:
    try:
        margins = kite.margins(segment="equity")
        return float(margins.get("net", 0))
    except Exception as e:
        logger.error("[TRADE] Margin fetch error: " + str(e))
        return -1.0


def pre_entry_checks(kite, token: int, state: dict, option_ltp: float, profile: dict,
                     session: str = "", direction: str = "") -> tuple:
    last_exit = state.get("last_exit_time")
    if last_exit:
        try:
            elapsed = (datetime.now() - datetime.fromisoformat(last_exit)).total_seconds() / 60
            cd_min = CFG.entry_ema9_band("cooldown_minutes", 5)
            # Both-sides cooldown: block CE and PE for cd_min after any exit.
            if elapsed < cd_min:
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

def _evaluate_entry_gates_pure(opt_3m, option_type: str, spot_ltp: float, now,
                               market_open: bool, state: dict,
                               atm_strike: int, silent: bool = False,
                               spot_3m=None) -> dict:
    # ── V7 entry: 2 gates (option-side only, 15-min candles) ──
    #   1. 15-min close > EMA9_low
    #   2. RSI >= 40 AND rising (RSI[fired] > RSI[prior])
    # NOTE: param name `opt_3m` retained for back-compat — V7 callers
    # pass 15-min candles in the same DataFrame format (timeframe-agnostic).
    result = {
        "fired": False, "entry_price": 0, "entry_mode": "", "ema9_high": 0, "ema9_low": 0,
        "close": 0, "open": 0, "high": 0, "low": 0, "candle_green": False, "body_pct": 0,
        "band_width": 0, "reject_reason": "", "band_position": "",
        "ema9_low_slope": 0.0,
        "band_width_slope": 0.0, "margin_above": 0,
        "spot_close": 0.0, "spot_ema9_low": 0.0, "spot_bias": "",
        "rsi": 0.0, "rsi_prev": 0.0, "rsi_rising": False,
    }
    try:
        warmup_until = CFG.entry_ema9_band("warmup_until", "09:35")
        cutoff_after = CFG.entry_ema9_band("cutoff_after", "15:00")

        # V7 needs at least 16 candles for RSI(14) to converge + prior + live.
        if opt_3m is None or opt_3m.empty or len(opt_3m) < 16:
            result["reject_reason"] = "insufficient_15m_data"
            return result

        last = opt_3m.iloc[-2]   # last CLOSED 15-min candle (fired)
        prev = opt_3m.iloc[-3]   # candle before fired

        # ── CRITICAL: Same-candle guard ──
        # Prevent re-firing on the same closed 15-min candle if we already
        # fired (or attempted to fire) on it. Without this, with cooldown=0
        # and a multi-minute scan loop, the same closed candle can trigger
        # 5-7 entries before the next candle closes — exactly what blew up
        # 2026-05-07 09:49-09:58 (-287 pts on 10 same-candle re-fires).
        try:
            fired_ts = str(last.name)
            result["fired_candle_ts"] = fired_ts
            last_fired_ts = state.get("_last_fired_candle_ts", "") if state else ""
            if last_fired_ts and last_fired_ts == fired_ts:
                result["reject_reason"] = "same_candle_already_fired"
                if not silent:
                    logger.info(f"[REJECT] {option_type} same_candle_guard "
                                f"already_fired_on={fired_ts}")
                return result
            # Belt: candle must have closed AFTER our last exit
            # Catches same-candle re-fires even if string comparison drifts
            _exit_epoch = float(state.get("_reentry_exit_ts", 0) or 0) if state else 0
            if _exit_epoch > 0:
                _candle_close_epoch = (last.name + timedelta(minutes=15)).timestamp()
                if _candle_close_epoch <= _exit_epoch:
                    result["reject_reason"] = "pre_exit_candle"
                    if not silent:
                        logger.info(f"[REJECT] {option_type} pre_exit_candle "
                                    f"candle_close={_candle_close_epoch:.0f} exit={_exit_epoch:.0f}")
                    return result
        except Exception as _ge:
            logger.warning("[ENGINE] same-candle guard error: " + str(_ge))

        close = float(last["close"]); open_ = float(last["open"])
        high = float(last["high"]); low = float(last["low"])
        ema9_high = float(last.get("ema9_high", 0))
        ema9_low  = float(last.get("ema9_low", 0))
        rsi_now   = float(last.get("RSI", 0))
        rsi_prev  = float(prev.get("RSI", 0))
        ema9_low_slope = round(ema9_low - float(prev.get("ema9_low", 0)), 2)
        band_width = round(ema9_high - ema9_low, 2)

        _band_pos = "ABOVE" if close > ema9_high else ("BELOW" if close < ema9_low else "IN")
        _candle_range = high - low
        _body_pct = round((abs(close - open_) / _candle_range * 100)
                          if _candle_range > 0 else 0, 1)
        _is_green = (close > open_)
        _margin = round(close - ema9_low, 2)
        _rsi_rising = (rsi_now > rsi_prev + 0.5)  # require genuine rise, not rounding noise

        result.update({
            "entry_price": round(close, 2), "ema9_high": round(ema9_high, 2),
            "ema9_low": round(ema9_low, 2), "close": round(close, 2), "open": round(open_, 2),
            "high": round(high, 2), "low": round(low, 2),
            "band_width": band_width, "ema9_low_slope": ema9_low_slope,
            "candle_green": _is_green, "band_position": _band_pos,
            "body_pct": _body_pct, "margin_above": _margin,
            "rsi": round(rsi_now, 1), "rsi_prev": round(rsi_prev, 1),
            "rsi_rising": _rsi_rising,
        })

        # ── Operational rail: time window ──
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

        # ── GATE 1: 15-min candle close > EMA9_low ──
        if close <= ema9_low:
            result["reject_reason"] = "close_below_ema9_low"
            if not silent:
                logger.info(f"[REJECT] {option_type} gate1_close_below_band "
                            f"close={round(close,1)} ema9l={round(ema9_low,1)}")
            return result

        # ── GATE 2: RSI >= 40 AND rising ──
        if rsi_now < 40:
            result["reject_reason"] = f"rsi_below_40_{round(rsi_now,1)}"
            if not silent:
                logger.info(f"[REJECT] {option_type} gate2_rsi_below_40 "
                            f"rsi={round(rsi_now,1)}")
            return result
        if not _rsi_rising:
            result["reject_reason"] = f"rsi_not_rising_{round(rsi_now,1)}_vs_{round(rsi_prev,1)}"
            if not silent:
                logger.info(f"[REJECT] {option_type} gate2_rsi_not_rising "
                            f"rsi_now={round(rsi_now,1)} rsi_prev={round(rsi_prev,1)}")
            return result

        # ── Spot bias (DISPLAY ONLY — no longer a gate) ──
        try:
            if spot_3m is not None and not spot_3m.empty and len(spot_3m) >= 2:
                _spot_last = spot_3m.iloc[-2]
                _spot_close = float(_spot_last["close"])
                _spot_ema9l = float(_spot_last.get("ema9_low", 0))
                result["spot_close"]    = round(_spot_close, 2)
                result["spot_ema9_low"] = round(_spot_ema9l, 2)
                if _spot_ema9l > 0:
                    if option_type == "CE":
                        result["spot_bias"] = "BULLISH" if _spot_close > _spot_ema9l else "BEARISH"
                    else:
                        result["spot_bias"] = "BEARISH" if _spot_close < _spot_ema9l else "BULLISH"
        except Exception:
            pass

        # ── All 2 gates passed ──
        result["fired"] = True
        result["entry_mode"] = "EMA9_BREAKOUT"
        if not silent:
            logger.info(f"[ENGINE] {option_type} FIRED close={round(close,1)} "
                        f"ema9l={round(ema9_low,1)} "
                        f"rsi={round(rsi_now,1)} (prev={round(rsi_prev,1)}, rising) "
                        f"spot_bias={result.get('spot_bias','?')} "
                        f"(2-gate V7, 15-min)")
        return result

    except Exception as e:
        logger.error("[ENGINE] Entry error: " + str(e))
        result["fired"] = False
        result["reject_reason"] = "error_" + str(e)[:50]
        return result

def check_entry(token: int, option_type: str, spot_ltp: float = 0, dte: int = 99,
                expiry_date=None, kite=None, other_token: int = 0, silent: bool = False,
                state: dict = None) -> dict:
    if state is None: state = {}
    # V7: 15-minute option candles (timeframe-agnostic — keeps same DataFrame
    # schema with EMA_9/EMA_21/RSI/ema9_high/ema9_low via add_indicators).
    opt_15m = None
    try:
        opt_15m = D.add_indicators(
            D.get_historical_data(token, "15minute", 30))
    except Exception as _oe:
        logger.warning("[ENGINE] option 15-min fetch failed: " + str(_oe))
    # Spot 15-min for display-only bias on the alert.
    spot_3m = None
    try:
        spot_3m = D.add_indicators(
            D.get_historical_data(D.NIFTY_SPOT_TOKEN, "15minute", 30))
    except Exception as _se:
        logger.warning("[ENGINE] spot 15-min fetch failed: " + str(_se))
    market_open = D.is_market_open()
    now = datetime.now()
    atm_strike = D.resolve_atm_strike(spot_ltp) if spot_ltp else 0
    return _evaluate_entry_gates_pure(
        opt_3m=opt_15m, option_type=option_type, spot_ltp=spot_ltp, now=now,
        market_open=market_open, state=state, atm_strike=atm_strike,
        silent=silent, spot_3m=spot_3m)
def evaluate_cross_leg(self_dir: str, opt_3m_other) -> dict:
    out = {
        "xleg_signal":       "NA",
        "xleg_other_close":  0.0,
        "xleg_other_ema9l":  0.0,
        "xleg_other_dying":  False,
        "xleg_other_margin": 0.0,
    }
    try:
        if opt_3m_other is None or opt_3m_other.empty or len(opt_3m_other) < 2:
            return out
        last = opt_3m_other.iloc[-2]
        other_close = float(last["close"])
        other_ema9l = float(last.get("ema9_low", 0))
        if other_ema9l <= 0:
            return out
        other_dying = other_close < other_ema9l - 0.5
        out["xleg_other_close"]  = round(other_close, 2)
        out["xleg_other_ema9l"]  = round(other_ema9l, 2)
        out["xleg_other_dying"]  = bool(other_dying)
        out["xleg_other_margin"] = round(other_close - other_ema9l, 2)
        out["xleg_signal"]       = "PASS" if other_dying else "FAIL"
    except Exception as e:
        logger.debug("[XLEG] eval err: " + str(e))
    return out


def compute_entry_sl(entry_price: float, hard_sl: int = 10) -> float:
    return round(entry_price - hard_sl, 2)

def compute_trail_sl(entry_price: float, peak_pnl: float,
                     direction: str = "", now=None) -> tuple:
    # V7 ladder — discrete 12-step + specific tiers at 30/40/50.
    # All TICK-based. Hard SL at -12.
    if peak_pnl < 12:
        sl = entry_price - 12
        return round(sl, 2), "INITIAL"
    # 12-step base ladder: peak 12→0, 24→12, 36→24, 48→36, 60→48, ...
    base_lock = (int(peak_pnl // 12) - 1) * 12
    # User-specific overrides at 30, 40, 50
    if peak_pnl >= 50:
        spec_lock = 50
    elif peak_pnl >= 40:
        spec_lock = 36
    elif peak_pnl >= 30:
        spec_lock = 20
    else:
        spec_lock = 0
    lock = max(base_lock, spec_lock)
    sl = entry_price + lock
    if lock == 0:
        tier = "LOCK_BE"
    else:
        tier = f"LOCK_{lock}"
    return round(sl, 2), tier

def _evaluate_exit_chain_pure(state: dict, option_ltp: float, opt_3m_full, now, market_open: bool) -> list:
    if not state.get("in_trade"): return []
    entry = state.get("entry_price", 0)
    pnl = round(option_ltp - entry, 2)
    peak = max(state.get("peak_pnl", 0), pnl)
    state["peak_pnl"] = peak
    # ── Emergency SL: -12 pts (config: exit.ema9_band.emergency_sl_pts) ──
    _emergency_sl = CFG.exit_ema9_band("emergency_sl_pts", -12)
    if pnl <= _emergency_sl:
        return [{"lot_id": "ALL", "reason": "EMERGENCY_SL", "price": option_ltp}]
    if market_open:
        _eod_str = CFG.exit_ema9_band("eod_exit_time", "15:20")
        try:
            _eh, _em = _eod_str.split(":")
            eod_mins = int(_eh) * 60 + int(_em)
        except Exception:
            eod_mins = 15 * 60 + 20
        if now.hour*60 + now.minute >= eod_mins:
            return [{"lot_id": "ALL", "reason": "EOD_EXIT", "price": option_ltp}]

    trail_sl, trail_tier = compute_trail_sl(entry, peak, now=now)
    state["active_ratchet_tier"] = trail_tier
    state["active_ratchet_sl"] = trail_sl

    # ── V6.1+ TICK-BASED trail for LOCKED tiers (peak ≥ 8) ──
    # When option_ltp drops to/below the locked SL → exit immediately
    # at the SL price. INITIAL tier (peak < 8) is covered by the
    # emergency SL check above (entry-10 = same threshold), so no
    # separate close-based trail check is needed for it.
    if trail_tier != "INITIAL" and trail_sl > 0:
        if option_ltp <= trail_sl:
            return [{
                "lot_id": "ALL",
                "reason": "VISHAL_TRAIL",
                "price": trail_sl,
                "trigger_close": round(float(option_ltp), 2),
                "trigger_time": now.strftime("%H:%M:%S"),
                "trigger_sl": round(trail_sl, 2),
            }]

    return []

def manage_exit(state: dict, option_ltp: float, profile: dict, other_token: int = 0) -> list:
    if not state.get("in_trade"): return []
    opt_3m_full = None
    try:
        opt_3m_full = D.get_option_3min(state.get("token"), lookback=10)
    except Exception as _e:
        logger.warning("[ENGINE] manage_exit get_option_3min failed: " + str(_e))
    return _evaluate_exit_chain_pure(state, option_ltp, opt_3m_full, datetime.now(), D.is_market_open())


# ═══════════════════════════════════════════════════════════════
# === CHARGES (merged from VRL_CHARGES) ===
# ═══════════════════════════════════════════════════════════════

BROKERAGE_PER_ORDER = 0.0   # MStock: ₹0 brokerage on options (update if your plan differs)
STT_SELL_PCT = 0.000625
EXCHANGE_NSE_PCT = 0.000530
SEBI_TURNOVER_PCT = 0.000001
STAMP_DUTY_BUY_PCT = 0.00003
GST_PCT = 0.18

def _live_lot_size() -> int:
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
    if lot_size is None:
        lot_size = _live_lot_size()
    return calculate_charges(entry_price, exit_price, lot_size, num_exit_orders=1)


# ===============================================================
# ===============================================================

#!/usr/bin/env python3
"""
Institutional level computation (shadow data collection only).

PURE OBSERVATION — does NOT block any live trades.
Logs filter pass/fail per V10 entry into ~/lab_data/shadow_levels_data.csv.

After 2 weeks of data, analyze and decide which filters to promote to live.

Levels computed:
  Yesterday spot: PDH, PDL, PDC, Pivot, TC, BC, CPR_W
  Today opening : ORH, ORL (9:15-9:30)
  Yesterday opt : opt_PDH, opt_PDL, opt_PDC (per strike + opt_type)

Shadow filters (proposed):
  G7  : DTE != 0                    (avoid expiry day theta trap)
  G8  : Pivot alignment             (CE > pivot, PE < pivot)
  G9  : CPR width <= 70             (skip chop days)
  G10 : NOT Mon 9:45-10:30          (worst hour-of-week)
  G11 : VWAP alignment on 15-min    (CE: fut > VWAP+25, PE: fut < VWAP-25)
"""



# ── Telegram (imported lazily to avoid circular import) ──
_TG_BASE = "https://api.telegram.org/bot"



# ── Module-level cache (computed once per day at startup) ──
_levels_lock      = threading.Lock()
_daily_levels     = {}   # {'PDH', 'PDL', 'PDC', 'Pivot', 'TC', 'BC', 'CPR_W', 'ORH', 'ORL'}
_opt_levels       = {}   # {(strike, 'CE'/'PE'): {'opt_PDH','opt_PDL','opt_PDC'}}
_last_compute_day = None

# ── VWAP state (refreshed every 15-min candle) ──
_vwap_state = {
    "fut_close"   : 0.0,    # latest 15-min futures close
    "vwap"        : 0.0,    # latest 15-min VWAP value
    "gap"         : 0.0,    # fut_close - vwap
    "last_update" : None,   # datetime of last refresh
}
_VWAP_FUT_TOKEN = 15956226  # NIFTY26JUNFUT 2026-06-30 — update when rolling to next expiry
_VWAP_BUFFER    = 25        # pts — CE needs gap > +25, PE needs gap < -25

# ── Output CSV ──
LAB_DIR  = os.path.join(os.path.expanduser("~"), "lab_data")
CSV_PATH = os.path.join(LAB_DIR, "shadow_levels_data.csv")
CSV_HEADERS = [
    "date", "time", "direction", "strike", "entry_price", "spot",
    "PDH", "PDL", "PDC", "Pivot", "TC", "BC", "CPR_W",
    "ORH", "ORL", "opt_PDC",
    "above_pivot", "in_cpr",
    "g7_dte_ok", "g8_pivot_ok", "g9_cpr_ok", "g10_time_ok",
    "g11_vwap_ok", "vwap_fut_close", "vwap_value", "vwap_gap",
    "all_pass", "dte", "dow",
]


def _ensure_csv():
    os.makedirs(LAB_DIR, exist_ok=True)
    if not os.path.isfile(CSV_PATH):
        with open(CSV_PATH, "w", newline="") as f:
            csv.writer(f).writerow(CSV_HEADERS)


def compute_today(D, kite, expiry) -> dict:
    """
    Called once at startup (or first scan of the day).
    Computes yesterday-based levels (PDH/PDL/PDC/CPR/Pivot) from spot history.
    Stores in module-level _daily_levels.
    Returns the levels dict.
    """
    global _daily_levels, _last_compute_day, _opt_levels
    today = date.today()
    with _levels_lock:
        if _last_compute_day == today and _daily_levels:
            return _daily_levels

        try:
            # Yesterday spot 1-min — fetch generously to ensure we get prev session
            spot_df = D.get_historical_data(D.NIFTY_SPOT_TOKEN, "minute", 800)
            if spot_df is None or spot_df.empty:
                logger.warning("[LEVELS] No spot history — levels unavailable")
                return {}
            if spot_df.index.tz is not None:
                spot_df.index = spot_df.index.tz_localize(None)

            # Today + yesterday dates
            unique_dates = sorted(set(spot_df.index.date))
            today_d = today
            if today_d not in unique_dates and len(unique_dates) > 0:
                today_d = unique_dates[-1]
            yest = None
            for d_ in unique_dates[::-1]:
                if d_ < today_d:
                    yest = d_
                    break
            if not yest:
                logger.warning("[LEVELS] No yesterday data in spot history")
                return {}

            ydf = spot_df[spot_df.index.date == yest]
            ydf = ydf.between_time("09:15", "15:30")
            if ydf.empty:
                logger.warning(f"[LEVELS] Yesterday {yest} session empty")
                return {}

            PDH = float(ydf["high"].max())
            PDL = float(ydf["low"].min())
            PDC = float(ydf["close"].iloc[-1])
            Pivot = (PDH + PDL + PDC) / 3.0
            BC    = (PDH + PDL) / 2.0
            TC    = 2 * Pivot - BC
            CPR_W = abs(TC - BC)

            # Opening range (today 9:15-9:30) — may be empty if called before 9:30
            tdf = spot_df[spot_df.index.date == today_d]
            or_df = tdf.between_time("09:15", "09:29") if not tdf.empty else None
            ORH = float(or_df["high"].max()) if (or_df is not None and not or_df.empty) else 0.0
            ORL = float(or_df["low"].min())  if (or_df is not None and not or_df.empty) else 0.0

            _daily_levels = {
                "PDH": round(PDH, 2), "PDL": round(PDL, 2), "PDC": round(PDC, 2),
                "Pivot": round(Pivot, 2),
                "TC":    round(max(TC, BC), 2),
                "BC":    round(min(TC, BC), 2),
                "CPR_W": round(CPR_W, 2),
                "ORH":   round(ORH, 2),
                "ORL":   round(ORL, 2),
                "yest_date": str(yest),
                "today_date": str(today_d),
            }
            _last_compute_day = today
            logger.info(
                f"[LEVELS] PDH={_daily_levels['PDH']} PDL={_daily_levels['PDL']} "
                f"PDC={_daily_levels['PDC']} Pivot={_daily_levels['Pivot']} "
                f"CPR={_daily_levels['BC']}-{_daily_levels['TC']} (w={_daily_levels['CPR_W']}) "
                f"ORH={_daily_levels['ORH']} ORL={_daily_levels['ORL']}"
            )

        except Exception as e:
            logger.warning(f"[LEVELS] compute_today error: {e}")
            return {}

    return _daily_levels


def refresh_opening_range(D) -> dict:
    """Call after 9:30 to update ORH/ORL once opening range completes."""
    global _daily_levels
    if not _daily_levels:
        return {}
    try:
        spot_df = D.get_historical_data(D.NIFTY_SPOT_TOKEN, "minute", 60)
        if spot_df is None or spot_df.empty:
            return _daily_levels
        if spot_df.index.tz is not None:
            spot_df.index = spot_df.index.tz_localize(None)
        today_d = date.today()
        tdf = spot_df[spot_df.index.date == today_d].between_time("09:15", "09:29")
        if not tdf.empty:
            with _levels_lock:
                _daily_levels["ORH"] = round(float(tdf["high"].max()), 2)
                _daily_levels["ORL"] = round(float(tdf["low"].min()), 2)
            logger.info(f"[LEVELS] OR refreshed: ORH={_daily_levels['ORH']} ORL={_daily_levels['ORL']}")
    except Exception as e:
        logger.debug(f"[LEVELS] refresh_OR error: {e}")
    return _daily_levels


def update_vwap(kite) -> dict:
    """
    Fetch latest 15-min NIFTY futures bars, compute today's running VWAP.
    Call at startup and every 15-min candle boundary in main loop.
    Returns current _vwap_state dict.
    NEVER raises — silent on failure.
    """
    global _vwap_state
    try:
        import numpy as np
        from_dt = datetime.now() - timedelta(hours=7)
        data    = kite.historical_data(_VWAP_FUT_TOKEN, from_dt, datetime.now(), "15minute")
        if not data:
            return _vwap_state

        import pandas as pd
        df = pd.DataFrame(data)
        df["date"] = pd.to_datetime(df["date"])
        df = df.set_index("date")
        if df.index.tz is not None:
            df.index = df.index.tz_localize(None)

        today_d = date.today()
        df = df[df.index.date == today_d]
        df = df.between_time("09:15", "15:30")
        if df.empty:
            return _vwap_state

        # Running VWAP from 9:15
        df["typical"] = (df["high"] + df["low"] + df["close"]) / 3.0
        df["tv"]      = df["typical"] * df["volume"]
        cum_tv  = df["tv"].cumsum()
        cum_vol = df["volume"].cumsum()
        df["vwap"] = cum_tv / cum_vol.replace(0, np.nan)

        last = df.iloc[-1]
        fut_close = round(float(last["close"]), 2)
        vwap_val  = round(float(last["vwap"]), 2)
        gap       = round(fut_close - vwap_val, 2)

        with _levels_lock:
            _vwap_state["fut_close"]   = fut_close
            _vwap_state["vwap"]        = vwap_val
            _vwap_state["gap"]         = gap
            _vwap_state["last_update"] = datetime.now()

        logger.info(
            f"[VWAP] fut={fut_close}  vwap={vwap_val}  "
            f"gap={gap:+.1f}  "
            f"({'BULL' if gap > 0 else 'BEAR'} bias)"
        )
    except Exception as e:
        logger.debug(f"[VWAP] update error: {e}")
    return _vwap_state


def get_vwap_state() -> dict:
    """Return current VWAP state (for dashboard etc)."""
    return dict(_vwap_state)


def compute_opt_pdc(D, strike: int, opt_type: str, token: int) -> dict:
    """
    Yesterday's option PDC for a single strike+opt_type. Cached.
    Returns {'opt_PDH','opt_PDL','opt_PDC'} or {} on failure.
    """
    key = (int(strike), opt_type)
    with _levels_lock:
        if key in _opt_levels:
            return _opt_levels[key]
    try:
        df = D.get_historical_data(token, "minute", 800)
        if df is None or df.empty:
            return {}
        if df.index.tz is not None:
            df.index = df.index.tz_localize(None)
        dates = sorted(set(df.index.date))
        today_d = date.today()
        yest = None
        for d_ in dates[::-1]:
            if d_ < today_d:
                yest = d_; break
        if not yest: return {}
        ydf = df[df.index.date == yest].between_time("09:15", "15:30")
        if ydf.empty: return {}
        out = {
            "opt_PDH": round(float(ydf["high"].max()), 2),
            "opt_PDL": round(float(ydf["low"].min()), 2),
            "opt_PDC": round(float(ydf["close"].iloc[-1]), 2),
        }
        with _levels_lock:
            _opt_levels[key] = out
        return out
    except Exception as e:
        logger.debug(f"[LEVELS] opt_pdc {strike}{opt_type} error: {e}")
        return {}


def evaluate_filters(direction: str, spot_px: float, entry_time_dt: datetime,
                     dte: int, opt_pdc: float = 0.0) -> dict:
    """
    Returns dict of shadow filter pass/fail. PURE OBSERVATION — does not block.
    direction: 'CE' or 'PE'
    spot_px:   current NIFTY spot
    entry_time_dt: datetime of entry
    dte:       days to expiry (0 = today is expiry)
    opt_pdc:   yesterday close for this option (0 if unknown)
    """
    L = _daily_levels
    if not L:
        return {"g7_dte_ok": None, "g8_pivot_ok": None, "g9_cpr_ok": None,
                "g10_time_ok": None, "all_pass": None}

    # G7: DTE != 0 (skip expiry day)
    g7 = dte != 0

    # G8: pivot alignment
    pivot = L.get("Pivot", 0)
    if direction == "CE":
        g8 = spot_px > pivot
    elif direction == "PE":
        g8 = spot_px < pivot
    else:
        g8 = False

    # G9: CPR width <= 70
    g9 = L.get("CPR_W", 999) <= 70

    # G10: NOT (Monday AND 9:45 <= time <= 10:30)
    is_mon  = entry_time_dt.weekday() == 0
    in_morn = _dtime(9, 45) <= entry_time_dt.time() <= _dtime(10, 30)
    g10 = not (is_mon and in_morn)

    # G11: VWAP alignment — CE: fut > VWAP+25, PE: fut < VWAP-25
    gap = _vwap_state.get("gap", 0.0)
    vwap_ready = _vwap_state.get("vwap", 0.0) > 0
    if not vwap_ready:
        g11 = None   # VWAP not yet computed — unknown
    elif direction == "CE":
        g11 = gap > _VWAP_BUFFER
    elif direction == "PE":
        g11 = gap < -_VWAP_BUFFER
    else:
        g11 = False

    return {
        "g7_dte_ok"  : g7,
        "g8_pivot_ok": g8,
        "g9_cpr_ok"  : g9,
        "g10_time_ok": g10,
        "g11_vwap_ok": g11,
        "all_pass"   : g7 and g8 and g9 and g10 and (g11 is not False),
    }


def log_entry(direction: str, strike: int, entry_price: float, spot_px: float,
              entry_time_dt: datetime, dte: int, opt_pdc: float = 0.0):
    """
    Called every V10 entry. Computes filters + writes one CSV row.
    NEVER blocks the trade — pure data collection.
    """
    try:
        _ensure_csv()
        f = evaluate_filters(direction, spot_px, entry_time_dt, dte, opt_pdc)
        L = _daily_levels

        pivot = L.get("Pivot", 0)
        bc, tc = L.get("BC", 0), L.get("TC", 0)
        above_pivot = spot_px > pivot if pivot else None
        in_cpr      = bc <= spot_px <= tc if (bc and tc) else None

        row = [
            entry_time_dt.strftime("%Y-%m-%d"),
            entry_time_dt.strftime("%H:%M:%S"),
            direction, strike, round(entry_price, 2), round(spot_px, 2),
            L.get("PDH", 0), L.get("PDL", 0), L.get("PDC", 0),
            L.get("Pivot", 0), L.get("TC", 0), L.get("BC", 0), L.get("CPR_W", 0),
            L.get("ORH", 0), L.get("ORL", 0), round(opt_pdc, 2),
            above_pivot, in_cpr,
            f.get("g7_dte_ok"), f.get("g8_pivot_ok"),
            f.get("g9_cpr_ok"), f.get("g10_time_ok"),
            f.get("g11_vwap_ok"),
            round(_vwap_state.get("fut_close", 0), 2),
            round(_vwap_state.get("vwap", 0), 2),
            round(_vwap_state.get("gap", 0), 2),
            f.get("all_pass"), dte, entry_time_dt.strftime("%A"),
        ]
        with open(CSV_PATH, "a", newline="") as fh:
            csv.writer(fh).writerow(row)

        # Human-readable log line
        marks = lambda b: "✓" if b is True else "✗" if b is False else "?"
        logger.info(
            f"[SHADOW-LVL] {direction} {strike} entry={entry_price} spot={spot_px} | "
            f"G7={marks(f.get('g7_dte_ok'))}(dte={dte}) "
            f"G8={marks(f.get('g8_pivot_ok'))}(pivot={pivot}) "
            f"G9={marks(f.get('g9_cpr_ok'))}(cpr_w={L.get('CPR_W',0)}) "
            f"G10={marks(f.get('g10_time_ok'))} "
            f"G11={marks(f.get('g11_vwap_ok'))}(gap={_vwap_state.get('gap',0):+.1f}) → "
            f"all={marks(f.get('all_pass'))}"
        )

    except Exception as e:
        logger.warning(f"[SHADOW-LVL] log_entry error: {e}")


def get_levels() -> dict:
    """Return current daily levels (for dashboard etc)."""
    return dict(_daily_levels)


# ===============================================================
# ===============================================================

# ═══════════════════════════════════════════════════════════════
#  Independent lab data collector. Separate process.
#  Collects 1-min + 3-min option candles. EOD forward fill.
#  Zero connection to trade loop. Cannot affect money.
#  Merged from: VRL_LAB_MAIN + VRL_LAB_OPTIONS
# ═══════════════════════════════════════════════════════════════





# ─── SCHEMAS ──────────────────────────────────────────────────

FIELDNAMES_3M = [
    "timestamp", "strike", "type",
    "open", "high", "low", "close", "volume",
    "spot_ref", "atm_distance", "dte",
    "session_block",
    "body_pct", "adx", "rsi", "ema9", "ema21", "ema_spread", "ema9_gap", "volume_ratio",
    "ema9_high", "ema9_low",   # v15.0: dual EMA9 bands for band-breakout strategy
    "fwd_3c", "fwd_6c", "fwd_9c", "fwd_outcome",
]

FIELDNAMES_1M = [
    "timestamp", "strike", "type",
    "open", "high", "low", "close", "volume",
    "spot_ref", "atm_distance", "dte",
    "session_block",
    "body_pct", "rsi", "ema9", "ema9_gap", "adx",
    "volume_ratio",
    "fwd_1c", "fwd_3c", "fwd_5c", "fwd_outcome",
]

# Signal scan log — live columns only.
# Dead v13 fields removed in the schema migration. CSV matches the DB schema.
FIELDNAMES_SCAN = [
    "timestamp", "session", "dte", "atm_strike", "spot",
    "direction", "entry_price",
    # v15.2 indicator fields
    "ema9_high", "ema9_low", "band_position", "body_pct",
    "body_pct_3m", "ema_spread_3m", "mode_3m",
    # Market context
    "vix", "spot_rsi_3m", "spot_ema_spread_3m", "spot_regime",
    # Result
    "fired", "trade_taken", "reject_reason",
    # Forward fill (populated EOD)
    "fwd_3c", "fwd_5c", "fwd_10c", "fwd_outcome",
]

# ─── SESSION STATE ────────────────────────────────────────────

_current_atm_strike = None
_current_atm_tokens = None
_current_expiry     = None
_lab_lock           = threading.Lock()   # protects the globals above

_lab_running  = False
_kite_ref     = None
_last_3min    = None
_last_1min    = None
_fwd_done     = False


# ─── PATHS ────────────────────────────────────────────────────

def _csv_path_3m(d: date) -> str:
    return os.path.join(D.OPTIONS_3MIN_DIR,
                        "nifty_option_3min_" + d.strftime("%Y%m%d") + ".csv")


def _csv_path_1m(d: date) -> str:
    return os.path.join(D.OPTIONS_1MIN_DIR,
                        "nifty_option_1min_" + d.strftime("%Y%m%d") + ".csv")


def _csv_path_scan(d: date) -> str:
    return os.path.join(D.OPTIONS_1MIN_DIR,
                        "nifty_signal_scan_" + d.strftime("%Y%m%d") + ".csv")


def _csv_path_spot() -> str:
    from datetime import date as _d
    return os.path.join(D.SPOT_DIR, "nifty_spot_1min_" + _d.today().strftime("%Y%m%d") + ".csv")


# ─── SPOT 1-MIN COLLECTOR ─────────────────────────────────────

FIELDNAMES_SPOT = ["timestamp", "open", "high", "low", "close", "volume", "ema9", "ema21", "ema_spread", "rsi", "adx"]

def collect_spot_1min(kite):
    """
    Append last closed 1-min SPOT candle to rolling spot CSV.
    Call every minute at HH:MM:30.
    """
    if not D.is_market_open():
        return
    try:
        now     = datetime.now()
        from_dt = now - timedelta(minutes=60)
        candles = kite.historical_data(
            instrument_token=D.NIFTY_SPOT_TOKEN,
            from_date=from_dt, to_date=now,
            interval="minute", continuous=False, oi=False,
        )
        if not candles or len(candles) < 2:
            return
        last   = candles[-2]
        ts_str = (last["date"].strftime("%Y-%m-%d %H:%M:%S")
                  if hasattr(last["date"], "strftime") else str(last["date"]))
        path   = _csv_path_spot()
        is_new = not os.path.isfile(path)
        # Deduplicate
        if not is_new:
            try:
                with open(path) as f:
                    last_written = None
                    for row in csv.DictReader(f):
                        last_written = row.get("timestamp","")
                if last_written == ts_str:
                    return
            except Exception:
                pass
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", newline="") as f:
            w = csv.DictWriter(f, fieldnames=FIELDNAMES_SPOT, extrasaction="ignore")
            if is_new:
                w.writeheader()
            # Compute indicators on warmup data
            _spot_ema9 = _spot_ema21 = _spot_rsi = _spot_adx = 0
            try:
                _sdf = pd.DataFrame(candles)
                _sdf.rename(columns={"date": "timestamp"}, inplace=True)
                _sdf.set_index("timestamp", inplace=True)
                _sdf = D.add_indicators(_sdf)
                if len(_sdf) >= 2:
                    _slast = _sdf.iloc[-2]
                    _sc = float(_slast["close"])
                    _spot_ema9 = round(float(_slast.get("EMA_9", _sc)), 2)
                    _spot_ema21 = round(float(_slast.get("EMA_21", _sc)), 2)
                    _spot_rsi = round(float(_slast.get("RSI", 50)), 1)
                # ADX
                if len(_sdf) >= 16:
                    import numpy as _np
                    _up = _sdf["high"].diff()
                    _dn = -_sdf["low"].diff()
                    _pdm = _np.where((_up > _dn) & (_up > 0), _up, 0.0)
                    _ndm = _np.where((_dn > _up) & (_dn > 0), _dn, 0.0)
                    _tr = pd.concat([_sdf["high"]-_sdf["low"],
                                     (_sdf["high"]-_sdf["close"].shift(1)).abs(),
                                     (_sdf["low"]-_sdf["close"].shift(1)).abs()], axis=1).max(axis=1)
                    _atr_s = _tr.ewm(alpha=1/14, adjust=False).mean()
                    _pdi = 100 * pd.Series(_pdm, index=_sdf.index).ewm(alpha=1/14, adjust=False).mean() / _atr_s
                    _ndi = 100 * pd.Series(_ndm, index=_sdf.index).ewm(alpha=1/14, adjust=False).mean() / _atr_s
                    _adx_s = ((_pdi-_ndi).abs() / (_pdi+_ndi+1e-9) * 100).ewm(alpha=1/14, adjust=False).mean()
                    _spot_adx = round(float(_adx_s.iloc[-2]), 1)
            except Exception:
                pass
            _spot_row = {
                "timestamp": ts_str,
                "open" : round(last["open"],  2),
                "high" : round(last["high"],  2),
                "low"  : round(last["low"],   2),
                "close": round(last["close"], 2),
                "volume": int(last["volume"]),
                "ema9": _spot_ema9,
                "ema21": _spot_ema21,
                "ema_spread": round(_spot_ema9 - _spot_ema21, 2) if _spot_ema9 and _spot_ema21 else 0,
                "rsi": _spot_rsi,
                "adx": _spot_adx,
            }
            w.writerow(_spot_row)
            f.flush()
    except Exception as e:
        logger.debug("[LAB] Spot 1m error: " + str(e))


def _load_timestamps(path: str) -> set:
    if not os.path.isfile(path):
        return set()
    existing = set()
    try:
        with open(path, "r") as f:
            for row in csv.DictReader(f):
                existing.add((row["timestamp"], row["strike"], row["type"]))
    except Exception as e:
        logger.warning("[LAB] Load ts error: " + str(e))
    return existing


def _append_rows(path: str, fieldnames: list, rows: list) -> int:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    is_new  = not os.path.isfile(path)
    written = 0
    try:
        with open(path, "a", newline="") as f:
            w = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
            if is_new:
                w.writeheader()
            for row in rows:
                w.writerow(row)
                written += 1
            f.flush()
    except Exception as e:
        logger.error("[LAB] Write error: " + str(e))
    return written


# ─── INDICATORS ───────────────────────────────────────────────

def _compute_indicators(df: pd.DataFrame, idx: int) -> dict:
    result = {"body_pct": 0, "adx": 0, "rsi": 50,
              "ema9": 0, "ema9_gap": 0, "volume_ratio": 1.0}
    try:
        row = df.iloc[idx]
        o, h, l, c = row["open"], row["high"], row["low"], row["close"]
        rng  = h - l
        body = abs(c - o)
        result["body_pct"]  = round((body / rng * 100) if rng > 0 else 0, 1)
        result["adx"]       = round(row.get("ADX", 0), 1)
        result["rsi"]       = round(row.get("RSI", 50), 1)
        result["ema9"]      = round(row.get("EMA_9", c), 2)
        result["ema9_gap"]  = round(abs(c - row.get("EMA_9", c)), 2)

        n     = len(df)
        pos   = idx if idx >= 0 else n + idx
        start = max(0, pos - 5)
        vols  = [df.iloc[i]["volume"] for i in range(start, pos) if df.iloc[i]["volume"] > 0]
        avg_v = sum(vols) / len(vols) if vols else 1
        result["volume_ratio"] = round(row["volume"] / avg_v if avg_v > 0 else 1, 2)
    except Exception as e:
        logger.warning("[LAB] Indicator error: " + str(e))
    return result


# ─── FETCH ────────────────────────────────────────────────────

def _fetch_candles_with_warmup(kite, token: int, from_dt: datetime,
                               to_dt: datetime, interval: str,
                               warmup_candles: int = 60) -> list:
    """
    Fetch candles with warmup history prepended.
    Warmup = yesterday's last N candles, gives RSI/EMA time to converge.
    Returns only today's candles but indicators are warmed up.
    """
    # Extend from_dt backwards to get warmup history
    minutes_per_candle = {"minute": 1, "3minute": 3}.get(interval, 1)
    extra_minutes = warmup_candles * minutes_per_candle * 2  # ×2 buffer for weekends/gaps
    warmup_from = from_dt - timedelta(minutes=extra_minutes + 60)

    try:
        all_candles = kite.historical_data(
            instrument_token = int(token),
            from_date        = warmup_from,
            to_date          = to_dt,
            interval         = interval,
            continuous       = False,
            oi               = False,
        )
        return all_candles if all_candles else []
    except Exception as e:
        logger.warning("[LAB] Warmup fetch failed, using regular fetch: " + str(e))
        return _fetch_candles(kite, token, from_dt, to_dt, interval)


def _fetch_candles(kite, token: int, from_dt: datetime,
                   to_dt: datetime, interval: str = "3minute") -> list:
    try:
        return kite.historical_data(
            instrument_token = int(token),
            from_date        = from_dt,
            to_date          = to_dt,
            interval         = interval,
            continuous       = False,
            oi               = False,
        )
    except Exception as e:
        logger.error("[LAB] Fetch error token=" + str(token) + " " + str(e))
        return []


# ─── RESET ────────────────────────────────────────────────────

def reset_session():
    global _current_atm_strike, _current_atm_tokens, _current_expiry
    with _lab_lock:
        _current_atm_strike = None
        _current_atm_tokens = None
        _current_expiry     = None
    logger.info("[LAB] Session reset")


# ─── LIVE COLLECTION — 3-MIN ──────────────────────────────────

def collect_option_3min(kite, spot_ltp: float):
    """
    Collect last CLOSED 3-min option candle for ATM CE + PE.
    Uses candles[-2] (last closed), not candles[-1] (still forming).
    Call at HH:MM:30 — 30s after each 3-min boundary.
    """
    global _current_atm_strike, _current_atm_tokens, _current_expiry

    now = datetime.now()
    cur_mins   = now.hour * 60 + now.minute
    start_mins = D.MARKET_OPEN_HOUR * 60 + D.MARKET_OPEN_MIN
    end_mins   = D.MARKET_CLOSE_HOUR * 60 + D.MARKET_CLOSE_MIN
    if not (start_mins <= cur_mins <= end_mins):
        return

    # Lock protects reads/writes to _current_atm_* globals
    today = date.today()

    with _lab_lock:
        if _current_expiry is None:
            _current_expiry = D.get_nearest_expiry(kite)
            if not _current_expiry:
                logger.error("[LAB] Cannot resolve expiry")
                return

        dte        = D.calculate_dte(_current_expiry)
        step       = D.get_active_strike_step(dte)
        new_strike = D.resolve_atm_strike(spot_ltp, step)

        if (_current_atm_strike is None
                or abs(new_strike - _current_atm_strike) >= step):
            if _current_atm_strike and new_strike != _current_atm_strike:
                logger.info("[LAB] ATM shift " + str(_current_atm_strike)
                            + "→" + str(new_strike))
            _current_atm_strike = new_strike
            _current_atm_tokens = D.get_option_tokens(kite, new_strike, _current_expiry)
            if not _current_atm_tokens:
                logger.error("[LAB] Token resolve failed strike=" + str(new_strike))
                return

    from_dt  = min(now - timedelta(minutes=180), now - timedelta(days=3))
    to_dt    = now
    today_ts = _load_timestamps(_csv_path_3m(today))
    session  = D.get_session_block(now.hour, now.minute)
    all_rows = []

    for opt_type, info in _current_atm_tokens.items():
        token   = info["token"]
        candles = _fetch_candles_with_warmup(kite, token, from_dt, to_dt, "3minute", 30)
        if not candles or len(candles) < 2:
            continue

        last = candles[-2]   # last CLOSED candle

        try:
            df = pd.DataFrame(candles)
            df.rename(columns={"date": "timestamp"}, inplace=True)
            df.set_index("timestamp", inplace=True)
            df = D.add_indicators(df)
            indic = _compute_indicators(df, -2)
            # Add ema21 + ema_spread + v15.0 bands
            _row3 = df.iloc[-2]
            indic["ema21"] = round(float(_row3.get("EMA_21", _row3["close"])), 2)
            indic["ema_spread"] = round(float(_row3.get("EMA_9", _row3["close"])) - float(_row3.get("EMA_21", _row3["close"])), 2)
            indic["ema9_high"] = round(float(_row3.get("ema9_high", _row3["high"])), 2)
            indic["ema9_low"]  = round(float(_row3.get("ema9_low", _row3["low"])), 2)
            # Inline ADX calculation (D.add_indicators doesn't compute ADX)
            try:
                import numpy as _np
                _up3 = df["high"].diff()
                _dn3 = -df["low"].diff()
                _pdm3 = _np.where((_up3 > _dn3) & (_up3 > 0), _up3, 0.0)
                _ndm3 = _np.where((_dn3 > _up3) & (_dn3 > 0), _dn3, 0.0)
                _tr3 = pd.concat([df["high"]-df["low"],
                                  (df["high"]-df["close"].shift(1)).abs(),
                                  (df["low"]-df["close"].shift(1)).abs()], axis=1).max(axis=1)
                _atr3 = _tr3.ewm(alpha=1/14, adjust=False).mean()
                _pdi3 = 100 * pd.Series(_pdm3, index=df.index).ewm(alpha=1/14, adjust=False).mean() / _atr3
                _ndi3 = 100 * pd.Series(_ndm3, index=df.index).ewm(alpha=1/14, adjust=False).mean() / _atr3
                _adx3 = ((_pdi3-_ndi3).abs() / (_pdi3+_ndi3+1e-9) * 100).ewm(alpha=1/14, adjust=False).mean()
                indic["adx"] = round(float(_adx3.iloc[-2]), 1)
            except Exception:
                indic["adx"] = 0
        except Exception:
            indic = {}

        ts_str = (last["date"].strftime("%Y-%m-%d %H:%M:%S")
                  if hasattr(last["date"], "strftime") else str(last["date"]))

        key = (ts_str, str(_current_atm_strike), opt_type)
        if key in today_ts:
            continue

        all_rows.append({
            "timestamp"    : ts_str,
            "strike"       : _current_atm_strike,
            "type"         : opt_type,
            "open"         : round(last["open"],  2),
            "high"         : round(last["high"],  2),
            "low"          : round(last["low"],   2),
            "close"        : round(last["close"], 2),
            "volume"       : int(last["volume"]),
            "spot_ref"     : round(spot_ltp, 2),
            "atm_distance" : round(abs(spot_ltp - _current_atm_strike), 0),
            "dte"          : dte,
            "session_block": session,
            "body_pct"     : indic.get("body_pct", 0),
            "adx"          : indic.get("adx", 0),
            "rsi"          : indic.get("rsi", 50),
            "ema9"         : indic.get("ema9", 0),
            "ema21"        : indic.get("ema21", 0),
            "ema_spread"   : indic.get("ema_spread", 0),
            "ema9_gap"     : indic.get("ema9_gap", 0),
            "volume_ratio" : indic.get("volume_ratio", 1),
            "ema9_high"    : indic.get("ema9_high", 0),
            "ema9_low"     : indic.get("ema9_low", 0),
            "fwd_3c": "", "fwd_6c": "", "fwd_9c": "", "fwd_outcome": "",
        })
        today_ts.add(key)
        time.sleep(0.35)

    if all_rows:
        all_rows.sort(key=lambda r: (r["timestamp"], r["type"]))
        n = _append_rows(_csv_path_3m(today), FIELDNAMES_3M, all_rows)
        logger.debug("[LAB] 3m wrote=" + str(n) + " @" + now.strftime("%H:%M"))
    try:
        _at_n = _collect_active_trade_candles(
            kite, "3minute", today, now, today_ts)
        if _at_n:
            logger.debug("[LAB] 3m active-trade wrote=" + str(_at_n))
    except Exception as _ate:
        logger.debug("[LAB] 3m active-trade err: " + str(_ate))
    # Also collect for any post-exit observation strikes (10-min window
    # after trade exit) so the data trail continues past the exit point.
    try:
        _pe_n = _collect_post_exit_candles(
            kite, "3minute", today, now, today_ts)
        if _pe_n:
            logger.debug("[LAB] 3m post-exit wrote=" + str(_pe_n))
    except Exception as _pee:
        logger.debug("[LAB] 3m post-exit err: " + str(_pee))


# ── persist active-trade strike candles through ATM rotation ──

def _collect_active_trade_candles(kite, interval: str, today, now,
                                  already_written_keys: set = None):
    """If VRL_MAIN has an active trade at a strike different from the
    current ATM, fetch + write candles for the trade's CE + PE tokens
    so the data trail has zero gaps from entry to exit.

    interval: "3minute" or "minute".
    already_written_keys: set of (timestamp, strike, type) tuples
      already written by the current collection pass — used to dedup.
    """
    active = D.get_active_trade()
    if not active:
        return 0
    trade_strike = active.get("strike", 0)
    if not trade_strike or trade_strike == _current_atm_strike:
        return 0   # same strike, already covered by normal collection
    if _current_expiry is None:
        return 0
    from_dt = now - timedelta(minutes=180 if interval == "3minute" else 60)
    to_dt   = now
    dte     = D.calculate_dte(_current_expiry)
    session = D.get_session_block(now.hour, now.minute)
    spot_ltp = D.get_ltp(D.NIFTY_SPOT_TOKEN)
    n_written = 0
    for side, tok_key in [("CE", "token_ce"), ("PE", "token_pe")]:
        tok = active.get(tok_key, 0)
        if not tok:
            continue
        try:
            candles = _fetch_candles_with_warmup(
                kite, int(tok), from_dt, to_dt, interval, 30)
            if not candles or len(candles) < 2:
                continue
            last = candles[-2]
            ts_str = (last["date"].strftime("%Y-%m-%d %H:%M:%S")
                      if hasattr(last["date"], "strftime") else str(last["date"]))
            key = (ts_str, str(trade_strike), side)
            if already_written_keys and key in already_written_keys:
                continue
            row = {
                "timestamp"    : ts_str,
                "strike"       : trade_strike,
                "type"         : side,
                "open"         : round(last["open"], 2),
                "high"         : round(last["high"], 2),
                "low"          : round(last["low"],  2),
                "close"        : round(last["close"], 2),
                "volume"       : int(last["volume"]),
                "spot_ref"     : round(spot_ltp, 2) if spot_ltp else 0,
                "atm_distance" : round(abs((spot_ltp or 0) - trade_strike), 0),
                "dte"          : dte,
                "session_block": session,
            }
            if interval == "3minute":
                # Compute indicators for the active trade's candles
                _adf = pd.DataFrame(candles)
                _adf.rename(columns={"date": "timestamp"}, inplace=True)
                _adf.set_index("timestamp", inplace=True)
                _adf = D.add_indicators(_adf)
                _arow = _adf.iloc[-2]
                row.update({
                    "rsi"      : round(float(_arow.get("RSI", 50)), 1),
                    "ema9"     : round(float(_arow.get("EMA_9", last["close"])), 2),
                    "ema21"    : round(float(_arow.get("EMA_21", last["close"])), 2),
                    "ema9_high": round(float(_arow.get("ema9_high", last["high"])), 2),
                    "ema9_low" : round(float(_arow.get("ema9_low", last["low"])), 2),
                })
                n_written += 1
            else:
                _adf = pd.DataFrame(candles)
                _adf.rename(columns={"date": "timestamp"}, inplace=True)
                _adf.set_index("timestamp", inplace=True)
                _adf = D.add_indicators(_adf)
                _arow = _adf.iloc[-2]
                row.update({
                    "rsi" : round(float(_arow.get("RSI", 50)), 1),
                    "ema9": round(float(_arow.get("EMA_9", last["close"])), 2),
                })
                n_written += 1
        except Exception as _e:
            logger.debug("[LAB] active-trade candle " + side
                         + " " + interval + ": " + str(_e))
    return n_written


def _collect_post_exit_candles(kite, interval: str, today, now,
                               already_written_keys: set = None):
    """For each post-exit observation registered in VRL_DATA, fetch and
    persist the just-closed candle so the data trail continues past
    trade exit. Without this, lab CSV/DB cuts off at exit and we lose
    visibility into what happened to the option after the bot got out.

    interval: "3minute" or "minute".
    Same dedup approach as _collect_active_trade_candles: skip rows
    already written by the current pass.
    """
    try:
        observations = D.get_post_exit_observations()
    except Exception:
        return 0
    if not observations:
        return 0
    if _current_expiry is None:
        return 0
    from_dt = now - timedelta(minutes=180 if interval == "3minute" else 60)
    to_dt   = now
    dte     = D.calculate_dte(_current_expiry)
    session = D.get_session_block(now.hour, now.minute)
    spot_ltp = D.get_ltp(D.NIFTY_SPOT_TOKEN)
    n_written = 0
    for obs in observations:
        tok    = obs.get("token", 0)
        strike = obs.get("strike", 0)
        side   = obs.get("side", "")
        if not tok or not strike or not side:
            continue
        try:
            candles = _fetch_candles_with_warmup(
                kite, int(tok), from_dt, to_dt, interval, 30)
            if not candles or len(candles) < 2:
                continue
            last = candles[-2]
            ts_str = (last["date"].strftime("%Y-%m-%d %H:%M:%S")
                      if hasattr(last["date"], "strftime") else str(last["date"]))
            key = (ts_str, str(strike), side)
            if already_written_keys and key in already_written_keys:
                continue
            row = {
                "timestamp"    : ts_str,
                "strike"       : strike,
                "type"         : side,
                "open"         : round(last["open"], 2),
                "high"         : round(last["high"], 2),
                "low"          : round(last["low"],  2),
                "close"        : round(last["close"], 2),
                "volume"       : int(last["volume"]),
                "spot_ref"     : round(spot_ltp, 2) if spot_ltp else 0,
                "atm_distance" : round(abs((spot_ltp or 0) - strike), 0),
                "dte"          : dte,
                "session_block": session,
            }
            _adf = pd.DataFrame(candles)
            _adf.rename(columns={"date": "timestamp"}, inplace=True)
            _adf.set_index("timestamp", inplace=True)
            _adf = D.add_indicators(_adf)
            _arow = _adf.iloc[-2]
            if interval == "3minute":
                row.update({
                    "rsi"      : round(float(_arow.get("RSI", 50)), 1),
                    "ema9"     : round(float(_arow.get("EMA_9", last["close"])), 2),
                    "ema21"    : round(float(_arow.get("EMA_21", last["close"])), 2),
                    "ema9_high": round(float(_arow.get("ema9_high", last["high"])), 2),
                    "ema9_low" : round(float(_arow.get("ema9_low", last["low"])), 2),
                })
            else:
                row.update({
                    "rsi" : round(float(_arow.get("RSI", 50)), 1),
                    "ema9": round(float(_arow.get("EMA_9", last["close"])), 2),
                })
            n_written += 1
        except Exception as _e:
            logger.debug("[LAB] post-exit candle " + side
                         + " " + interval + ": " + str(_e))
    return n_written


# ─── LIVE COLLECTION — 1-MIN ──────────────────────────────────

def collect_option_1min(kite, spot_ltp: float):
    """
    Collect last CLOSED 1-min option candle for ATM CE + PE.
    Call every minute at HH:MM:30.
    Depends on 3-min collector having initialised tokens first.
    """
    global _current_atm_strike, _current_atm_tokens, _current_expiry

    now = datetime.now()
    cur_mins   = now.hour * 60 + now.minute
    start_mins = D.MARKET_OPEN_HOUR * 60 + D.MARKET_OPEN_MIN
    end_mins   = D.MARKET_CLOSE_HOUR * 60 + D.MARKET_CLOSE_MIN
    if not (start_mins <= cur_mins <= end_mins):
        return

    if not _current_atm_tokens or not _current_expiry:
        return   # 3-min must init first

    today    = date.today()
    dte      = D.calculate_dte(_current_expiry)
    session  = D.get_session_block(now.hour, now.minute)
    from_dt  = min(now - timedelta(minutes=50), now - timedelta(days=3))
    to_dt    = now
    today_ts = _load_timestamps(_csv_path_1m(today))
    all_rows = []

    for opt_type, info in _current_atm_tokens.items():
        token   = info["token"]
        candles = _fetch_candles_with_warmup(kite, token, from_dt, to_dt, "minute", 30)
        if not candles or len(candles) < 2:
            continue

        last = candles[-2]   # last CLOSED 1-min candle

        try:
            df = pd.DataFrame(candles)
            df.rename(columns={"date": "timestamp"}, inplace=True)
            df.set_index("timestamp", inplace=True)
            df = D.add_indicators(df)
            # Use iloc[-2] which is warmed up now (has warmup history before it)
            indic = _compute_indicators(df, -2)
            # ADX for 1m
            try:
                import numpy as _np
                _up1 = df["high"].diff()
                _dn1 = -df["low"].diff()
                _pdm1 = _np.where((_up1 > _dn1) & (_up1 > 0), _up1, 0.0)
                _ndm1 = _np.where((_dn1 > _up1) & (_dn1 > 0), _dn1, 0.0)
                _tr1 = pd.concat([df["high"]-df["low"],
                                  (df["high"]-df["close"].shift(1)).abs(),
                                  (df["low"]-df["close"].shift(1)).abs()], axis=1).max(axis=1)
                _atr1 = _tr1.ewm(alpha=1/14, adjust=False).mean()
                _pdi1 = 100 * pd.Series(_pdm1, index=df.index).ewm(alpha=1/14, adjust=False).mean() / _atr1
                _ndi1 = 100 * pd.Series(_ndm1, index=df.index).ewm(alpha=1/14, adjust=False).mean() / _atr1
                _adx1 = ((_pdi1-_ndi1).abs() / (_pdi1+_ndi1+1e-9) * 100).ewm(alpha=1/14, adjust=False).mean()
                indic["adx"] = round(float(_adx1.iloc[-2]), 1)
            except Exception:
                indic["adx"] = 0
        except Exception:
            indic = {}

        ts_str = (last["date"].strftime("%Y-%m-%d %H:%M:%S")
                  if hasattr(last["date"], "strftime") else str(last["date"]))

        key = (ts_str, str(_current_atm_strike), opt_type)
        if key in today_ts:
            continue

        all_rows.append({
            "timestamp"    : ts_str,
            "strike"       : _current_atm_strike,
            "type"         : opt_type,
            "open"         : round(last["open"],  2),
            "high"         : round(last["high"],  2),
            "low"          : round(last["low"],   2),
            "close"        : round(last["close"], 2),
            "volume"       : int(last["volume"]),
            "spot_ref"     : round(spot_ltp, 2),
            "atm_distance" : round(abs(spot_ltp - _current_atm_strike), 0),
            "dte"          : dte,
            "session_block": session,
            "body_pct"     : indic.get("body_pct", 0),
            "rsi"          : indic.get("rsi", 50),
            "ema9"         : indic.get("ema9", 0),
            "ema9_gap"     : indic.get("ema9_gap", 0),
            "adx"          : indic.get("adx", 0),
            "volume_ratio" : indic.get("volume_ratio", 1),
            "fwd_1c": "", "fwd_3c": "", "fwd_5c": "", "fwd_outcome": "",
        })
        today_ts.add(key)
        time.sleep(0.25)

    if all_rows:
        all_rows.sort(key=lambda r: (r["timestamp"], r["type"]))
        n = _append_rows(_csv_path_1m(today), FIELDNAMES_1M, all_rows)
        logger.debug("[LAB] 1m wrote=" + str(n) + " @" + now.strftime("%H:%M"))
    try:
        _at_n = _collect_active_trade_candles(kite, "minute", today, now)
        if _at_n:
            logger.debug("[LAB] 1m active-trade wrote=" + str(_at_n))
    except Exception as _ate:
        logger.debug("[LAB] 1m active-trade err: " + str(_ate))
    # Post-exit observation: keep writing 1-min candles for the just-
    # exited strike for 10 min after exit so the data trail is complete.
    try:
        _pe_n = _collect_post_exit_candles(kite, "minute", today, now)
        if _pe_n:
            logger.debug("[LAB] 1m post-exit wrote=" + str(_pe_n))
    except Exception as _pee:
        logger.debug("[LAB] 1m post-exit err: " + str(_pee))


# ─── BACKFILL — 3-MIN ─────────────────────────────────────────


def _read_spot_1min_map(target_date: date) -> dict:
    result     = {}
    paths      = [
        os.path.join(D.SPOT_DIR, "nifty_spot_1min.csv"),
        os.path.expanduser("~/nifty_spot_1min.csv"),
    ]
    target_str = target_date.strftime("%Y-%m-%d")
    for path in paths:
        if not os.path.isfile(path):
            continue
        try:
            with open(path) as f:
                for row in csv.DictReader(f):
                    ts = row.get("timestamp", row.get("date", ""))
                    if ts.startswith(target_str):
                        key = ts[:16]
                        try:
                            result[key] = float(row.get("close", row.get("Close", 0)))
                        except Exception:
                            pass
        except Exception as e:
            logger.warning("[LAB] Spot map error: " + str(e))
    return result


# ─── EOD FORWARD FILL ─────────────────────────────────────────

def fill_forward_columns(kite, target_date: date = None, timeframe: str = "3min"):
    """Fill fwd columns for 3-min or 1-min CSV at EOD."""
    if target_date is None:
        target_date = date.today()

    if timeframe == "1min":
        path       = _csv_path_1m(target_date)
        fieldnames = FIELDNAMES_1M
        fwd_keys   = ["fwd_1c", "fwd_3c", "fwd_5c"]
        fwd_mins   = [1, 3, 5]
        win_pts    = 10
        loss_pts   = -6
    else:
        path       = _csv_path_3m(target_date)
        fieldnames = FIELDNAMES_3M
        fwd_keys   = ["fwd_3c", "fwd_6c", "fwd_9c"]
        fwd_mins   = [9, 18, 27]
        win_pts    = 15
        loss_pts   = -8

    if not os.path.isfile(path):
        return

    logger.info("[LAB] Forward fill " + timeframe + " for " + str(target_date))

    try:
        with open(path) as f:
            rows = list(csv.DictReader(f))
    except Exception as e:
        logger.error("[LAB] Fwd fill read error: " + str(e))
        return

    with _lab_lock:
        tokens_by_type = {}
        if _current_atm_tokens:
            for opt_type, info in _current_atm_tokens.items():
                tokens_by_type[opt_type] = info["token"]

    changed  = 0
    interval = "minute" if timeframe == "1min" else "3minute"

    for row in rows:
        if row.get(fwd_keys[-1]):
            continue
        opt_type = row.get("type")
        token    = tokens_by_type.get(opt_type)
        if not token:
            continue

        try:
            ts     = datetime.fromisoformat(row["timestamp"])
            prices = []

            for mins in fwd_mins:
                fwd_t   = ts + timedelta(minutes=mins)
                candles = _fetch_candles(kite, token,
                                         fwd_t - timedelta(minutes=1),
                                         fwd_t + timedelta(minutes=2),
                                         interval)
                prices.append(round(candles[-1]["close"], 2) if candles else None)
                time.sleep(0.25)

            entry = float(row.get("close", 0))
            if all(p is not None for p in prices):
                for key, price in zip(fwd_keys, prices):
                    row[key] = price
                max_move = max(p - entry for p in prices)
                min_move = min(p - entry for p in prices)
                if max_move >= win_pts:
                    row["fwd_outcome"] = "WIN"
                elif min_move <= loss_pts:
                    row["fwd_outcome"] = "LOSS"
                else:
                    row["fwd_outcome"] = "NEUTRAL"
                changed += 1

        except Exception as e:
            logger.warning("[LAB] Fwd fill row error: " + str(e))

    try:
        with open(path, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
            w.writeheader()
            w.writerows(rows)
            f.flush()
        logger.info("[LAB] Fwd fill done: " + str(changed) + " rows")
    except Exception as e:
        logger.error("[LAB] Fwd fill write error: " + str(e))



def _startup_backfill(_kite):
    pass  # DB removed — nothing to backfill


def start_lab(kite):
    """
    Entry point. Call after kite auth in VRL_MAIN.py.
    Backfills history then starts live collection loop.
    Runs as daemon thread — dies when main exits.
    """
    global _kite_ref, _lab_running
    _kite_ref    = kite
    _lab_running = True

    # v15.2.5 mid-day restart backfill, gated on empty buffer
    try:
        _startup_backfill(kite)
    except Exception as _be:
        logger.warning("[LAB] Startup backfill skipped on outer error: " + str(_be))

    def _start():
        logger.info("[LAB] Starting — collection loop")
        _lab_loop()

    thread = threading.Thread(target=_start, name="LabCollector", daemon=True)
    thread.start()
    logger.info("[LAB] Collection thread started")


def _lab_loop():
    global _last_3min, _last_1min, _fwd_done

    _last_daily_reset = None   # Fix: prevent triple reset

    while _lab_running:
        try:
            now   = datetime.now()
            today = date.today()

            # v12.16: Weekend guard — no collection on Sat/Sun
            if today.weekday() >= 5:
                time.sleep(60)
                continue

            # ── Daily reset at 9:14 — only once per day ───────────
            reset_key = today.isoformat()
            if (now.hour == 9 and now.minute == 14
                    and now.second < 5
                    and _last_daily_reset != reset_key):
                _last_daily_reset = reset_key
                reset_session()
                _fwd_done  = False
                _last_3min = None
                _last_1min = None
                logger.info("[LAB] Daily reset")

                # Auto-cleanup: old logs (>7 days) + stale zips
                import glob as _cg
                _cleaned = 0
                for _old_log in _cg.glob(os.path.expanduser("~/logs/live/vrl_live.log.*")):
                    if os.path.getmtime(_old_log) < time.time() - 7 * 86400:
                        os.remove(_old_log)
                        _cleaned += 1
                for _old_zip in _cg.glob(os.path.expanduser("~/state/today_*.zip")):
                    os.remove(_old_zip)
                    _cleaned += 1
                if _cleaned:
                    logger.info("[LAB] Cleanup: deleted " + str(_cleaned) + " old files")

            # ── Fetch spot LTP once per loop iteration ────────
            _loop_spot_ltp = D.get_ltp(D.NIFTY_SPOT_TOKEN)
            if _loop_spot_ltp <= 0:
                try:
                    _q = _kite_ref.ltp("NSE:NIFTY 50")
                    _loop_spot_ltp = float(list(_q.values())[0]["last_price"])
                except Exception:
                    pass

            # ── 1-min collection at HH:MM:30 ──────────────────
            one_min_key = (today, now.hour, now.minute)
            if one_min_key != _last_1min and now.second >= 30:
                _last_1min  = one_min_key
                spot_ltp    = _loop_spot_ltp
                if spot_ltp > 0:
                    try:
                        collect_option_1min(_kite_ref, spot_ltp)
                    except Exception as e:
                        logger.error("[LAB] 1m error: " + str(e))
                    # Spot 1-min rolling CSV (still consumed by reports tooling)
                    try:
                        collect_spot_1min(_kite_ref)
                    except Exception as e:
                        logger.debug("[LAB] spot 1m: " + str(e))
                elif spot_ltp <= 0 and D.is_market_open():
                    logger.debug("[LAB] 1m skip — spot LTP not available yet")

            # ── 3-min collection at boundary + 30s ────────────
            candle_min    = (now.minute // 3) * 3
            three_min_key = (today, now.hour, candle_min)
            if three_min_key != _last_3min and now.second >= 30:
                _last_3min = three_min_key
                spot_ltp   = _loop_spot_ltp
                if spot_ltp > 0:
                    try:
                        collect_option_3min(_kite_ref, spot_ltp)
                    except Exception as e:
                        logger.error("[LAB] 3m error: " + str(e))
                elif spot_ltp <= 0 and D.is_market_open():
                    logger.debug("[LAB] 3m skip — spot LTP not available yet")

            # ── EOD forward fill — widened from exact 15:35:00-30
            # to 15:35–15:50 window. The old 30-second slot was missed if
            # the loop was slow, restarting, or busy with the 3-min
            # collection tick. Still gated by _fwd_done so it runs AT MOST
            # once per trading day.
            if (now.hour == 15 and 35 <= now.minute <= 50
                    and not _fwd_done):
                _fwd_done = True
                logger.info("[LAB] EOD forward fill starting at "
                            + now.strftime("%H:%M:%S"))
                _n_fwd = 0
                try:
                    fill_forward_columns(_kite_ref, today, "3min")
                    _n_fwd += 1
                    fill_forward_columns(_kite_ref, today, "1min")
                    _n_fwd += 1
                except Exception as e:
                    logger.error("[LAB] Forward fill error: " + str(e))
                logger.info("[LAB] Forward fill complete: "
                            + str(_n_fwd) + "/2 jobs for "
                            + today.isoformat())

        except Exception as e:
            logger.error("[LAB] Loop error: " + str(e))

        time.sleep(1)

from datetime import time as _dtime

# (self-reference aliases D/CFG/LEVELS/CHARGES defined near line 490)

# ── Bootstrap dirs ──
ensure_dirs()

# ── Load config at startup (was done by VRL_DATA importing VRL_CONFIG) ──
load()



# ===============================================================
#  ORIGINAL VRL_MAIN.py CODE
# ===============================================================

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

# Post-exit observation queue: tokens kept subscribed for 10 min after
# exit so VRL_LAB can record post-exit price action for analysis.
# Format: [(token, unsubscribe_at_timestamp_epoch), ...]
_post_exit_observation = []
_post_exit_lock = threading.Lock()
POST_EXIT_OBSERVATION_MINUTES = 10

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
    # V7 re-entry watcher — 2-candle window after exit
    "_reentry_armed"     : False,
    "_reentry_exit_ts"   : 0.0,
    "_reentry_attempts"  : 0,         # count of candles checked
    "_reentry_last_checked_epoch": 0.0,
    "_next_candle2_after": 0.0,
    "_reentry_direction" : "",
    "_reentry_token"     : 0,
    "_reentry_strike"    : 0,
    # Same-candle guard: timestamp of last fired candle (str). Engine
    # rejects re-entry when current candle == this, stops the
    # 2026-05-07 same-candle re-fire bug.
    "_last_fired_candle_ts": "",
    # V8 EMERGENCY_SL 1-candle cooldown: set True when SL fires,
    # entry scan skips the very next candle then clears the flag.
    "_sl_cooldown_skip_next": False,
    "_force_exit_ts"        : 0.0,
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
_last_health_log_ts = 0.0   # throttle for intraday [MAIN] Token health re-log

_v8_state = {
    "_last_fired_candle_ts": "",     # same-candle guard
    "_signals_today": 0,             # count for /pulse
    "_last_signal_time": "",
    # Paper position state (parallel to V7, independent).
    "in_trade": False,
    "symbol": "",
    "token": 0,
    "direction": "",
    "strike": 0,
    "entry_price": 0.0,
    "entry_time": "",
    "qty": 0,
    "peak_pnl": 0.0,
    "active_ratchet_tier": "",
    "active_ratchet_sl": 0.0,
    "candles_held": 0,
    "_last_minute": "",
    "_other_token": 0,          # other leg's token — needed for re-entry after restart
    "_reentry_exit_price": 0.0, # exit price of last trade — re-entry anti-chase gate
    # Re-entry watcher (cross-leg continuation, 2-candle window)
    "_reentry_armed": False,
    "_reentry_attempts": 0,
    "_reentry_last_checked_epoch": 0.0,
    "_reentry_direction": "",
    "_reentry_token": 0,
    "_reentry_strike": 0,
    "_reentry_other_token": 0,
    # Daily cumulative
    "_pnl_today_pts": 0.0,
    "_trades_today": 0,
    "_wins_today": 0,
    "_losses_today": 0,
    # 1-candle cooldown after EMERGENCY_SL (owned here, not in V7 state)
    "_sl_cooldown_skip_next": False,
    "_force_exit_ts"        : 0.0,
    # Exit candle guard: block re-entry on same 3-min candle we just exited from
    "_last_exit_candle_ts"  : "",
    # Both-sides rejection cooldown: unix timestamp of last scan where both CE+PE failed
    "_v8_both_rejected_ts": 0.0,
    # Date of last trade — used to detect new day and reset daily counters on restart
    "_last_trade_date": "",
    # Current expiry / DTE — synced from main loop every iteration so entry/exit always sees correct value
    "expiry": "",
    "dte": 0,
    # EMERGENCY_SL direction cooldown — only blocks the side that triggered the SL
    "_sl_cooldown_direction": "",
    # Strike management data collection (reset per trade, not persisted)
    "entry_spot": 0.0,
    "entry_atm_dist": 0,      # strike - true_ATM at entry (CE: + = ITM, - = OTM)
    "neighbor_ltp_otm": 0.0,  # LTP of 1-strike-OTM neighbor at entry
    "neighbor_ltp_itm": 0.0,  # LTP of 1-strike-ITM neighbor at entry
    "max_otm_drift": 0.0,     # max pts the position went OTM during trade
}
_v8_lock = threading.Lock()


def _v8_compute_trail_sl(entry_price: float, peak_pnl: float) -> tuple:
    """V8 SL ladder (3-min): LOCK_4 at +12, LOCK_10 at +18, then custom tiers up to LOCK_50."""
    if peak_pnl < 12:
        return round(entry_price - 12, 2), "INITIAL"
    if peak_pnl >= 50:
        lock, tier = 50, "LOCK_50"
    elif peak_pnl >= 40:
        lock, tier = 36, "LOCK_36"
    elif peak_pnl >= 36:
        lock, tier = 30, "LOCK_30"
    elif peak_pnl >= 30:
        lock, tier = 20, "LOCK_20"
    elif peak_pnl >= 24:
        lock, tier = 12, "LOCK_12"
    elif peak_pnl >= 18:
        lock, tier = 10, "LOCK_10"
    else:
        lock, tier = 4, "LOCK_4"
    return round(entry_price + lock, 2), tier


def _v8_execute_paper_entry(direction: str, strike: int, symbol: str, token: int,
                             entry_price: float, entry_result: dict,
                             other_token: int = 0,
                             spot_at_entry: float = 0.0,
                             neighbor_ltp_otm: float = 0.0,
                             neighbor_ltp_itm: float = 0.0):
    """Open a V8 paper position. Records in _v8_state, sends Telegram alert."""
    lot_count = CFG.get().get("lots", {}).get("count", 2)
    qty = lot_count * D.get_lot_size()
    now_dt  = datetime.now()
    now_str = now_dt.strftime("%H:%M:%S")
    is_reentry = (entry_result.get("entry_mode") == "REENTRY_XLEG")

    with _v8_lock:
        if _v8_state.get("in_trade"):
            logger.warning("[V10] Entry attempted while already in_trade — BLOCKED (duplicate guard)")
            return
        _v8_state["in_trade"]              = True
        _v8_state["symbol"]                = symbol
        _v8_state["token"]                 = token
        _v8_state["direction"]             = direction
        _v8_state["strike"]                = int(strike or 0)
        _v8_state["entry_price"]           = float(entry_price)
        _v8_state["entry_time"]            = now_str
        _v8_state["qty"]                   = qty
        _v8_state["peak_pnl"]              = 0.0
        _v8_state["active_ratchet_tier"]   = "INITIAL"
        _v8_state["active_ratchet_sl"]     = round(entry_price - 12, 2)
        _v8_state["candles_held"]          = 0
        _v8_state["_last_fired_candle_ts"] = entry_result.get("fired_candle_ts", "")
        _v8_state["_other_token"]          = int(other_token or 0)
        # Strike management data collection
        _v8_state["entry_spot"]        = float(spot_at_entry)
        _true_atm = int(round(spot_at_entry / 50) * 50) if spot_at_entry > 0 else int(strike)
        _v8_state["entry_atm_dist"]    = int(strike) - _true_atm
        _v8_state["neighbor_ltp_otm"]  = float(neighbor_ltp_otm)
        _v8_state["neighbor_ltp_itm"]  = float(neighbor_ltp_itm)
        _v8_state["max_otm_drift"]     = 0.0
        # Clear any pending re-entry state (fresh setup wins)
        _v8_state["_reentry_armed"]        = False
        _v8_state["_reentry_attempts"]     = 0

    logger.info("[V10] PAPER ENTRY: " + symbol + " qty=" + str(qty)
                + " entry=" + str(entry_price) + " mode="
                + str(entry_result.get("entry_mode", "")))

    _ce_pe = "🟢" if direction == "CE" else "🔴"
    _gap = round(entry_result.get("close", 0) - entry_result.get("ema9_high", entry_result.get("ema9_low", 0)), 1)
    _tg_send(
        _ce_pe + " <b>V10 " + direction + " " + str(strike) + "</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "Entry   ₹" + "{:.1f}".format(entry_price) + "  @ " + now_str + "\n"
        "SL      ₹" + "{:.1f}".format(entry_price - 12) + "  (−12 pts)\n"
        "Gap     " + "{:+.1f}".format(_gap) + "  |  BW " + "{:.1f}".format(
            entry_result.get("ema9_high", 0) - entry_result.get("ema9_low", 0)) + "\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "Trail  +12→lock4  +24→lock12  +30→lock20  +50→lock50",
        priority="critical"
    )
    _save_v8_state()


def _v8_execute_paper_exit(reason: str, exit_price: float):
    """Close V8 paper position. Logs trade to CSV, arms re-entry watcher."""
    with _v8_lock:
        if not _v8_state.get("in_trade"):
            return
        # Read all values FIRST (before clearing), then mark closed immediately.
        # Any concurrent call (TG thread vs main loop) now sees in_trade=False and returns —
        # eliminating the duplicate-exit race condition.
        entry_price = float(_v8_state.get("entry_price", 0))
        symbol      = _v8_state.get("symbol", "")
        direction   = _v8_state.get("direction", "")
        strike      = int(_v8_state.get("strike", 0) or 0)
        qty         = int(_v8_state.get("qty", 0) or 0)
        peak        = float(_v8_state.get("peak_pnl", 0))
        entry_time  = _v8_state.get("entry_time", "")
        candles     = int(_v8_state.get("candles_held", 0) or 0)
        tier        = _v8_state.get("active_ratchet_tier", "")
        token       = int(_v8_state.get("token", 0) or 0)
        other_tok   = int(_v8_state.get("_other_token", 0) or 0)
        dte_val        = int(_v8_state.get("dte", 0) or 0)
        entry_spot_val = float(_v8_state.get("entry_spot", 0))
        entry_atm_dist = int(_v8_state.get("entry_atm_dist", 0))
        neighbor_otm   = float(_v8_state.get("neighbor_ltp_otm", 0))
        neighbor_itm   = float(_v8_state.get("neighbor_ltp_itm", 0))
        max_otm_drift  = float(_v8_state.get("max_otm_drift", 0))
        pnl_pts_now = round(exit_price - entry_price, 2)
        # Clear position state
        _v8_state["in_trade"]            = False
        _v8_state["symbol"]              = ""
        _v8_state["token"]               = 0
        _v8_state["direction"]           = ""
        _v8_state["strike"]              = 0
        _v8_state["entry_price"]         = 0.0
        _v8_state["peak_pnl"]            = 0.0
        _v8_state["active_ratchet_tier"] = ""
        _v8_state["active_ratchet_sl"]   = 0.0
        _v8_state["candles_held"]        = 0
        # Update daily counters and arm re-entry under the same lock
        _v8_state["_pnl_today_pts"] = round(_v8_state.get("_pnl_today_pts", 0) + pnl_pts_now, 2)
        _v8_state["_trades_today"]  = _v8_state.get("_trades_today", 0) + 1
        if pnl_pts_now > 0:
            _v8_state["_wins_today"]   = _v8_state.get("_wins_today", 0) + 1
        elif pnl_pts_now < 0:
            _v8_state["_losses_today"] = _v8_state.get("_losses_today", 0) + 1
        if reason == "EMERGENCY_SL":
            _v8_state["_sl_cooldown_skip_next"] = True
            _v8_state["_sl_cooldown_direction"] = direction   # block SAME side only
        _v8_state["_reentry_armed"]              = False  # disabled — fresh setup only
        _v8_state["_reentry_attempts"]           = 0
        _v8_state["_reentry_last_checked_epoch"] = 0.0
        _v8_state["_reentry_direction"]          = direction
        _v8_state["_reentry_token"]              = token
        _v8_state["_reentry_strike"]             = strike
        _v8_state["_reentry_other_token"]        = other_tok
        _v8_state["_reentry_exit_price"]         = round(exit_price, 2)
        _v8_state["_last_trade_date"]            = date.today().isoformat()
        # Exit candle guard: record the 3-min bucket we're exiting in
        _now_exit = datetime.now()
        _exit_bucket_min = (_now_exit.minute // 3) * 3
        _v8_state["_last_exit_candle_ts"] = str(
            _now_exit.replace(minute=_exit_bucket_min, second=0, microsecond=0)
        )

    # --- Lock released: safe to read captured locals for logging ---
    pnl_pts   = round(exit_price - entry_price, 2)
    pnl_rs    = round(pnl_pts * qty, 2)
    exit_time = datetime.now().strftime("%H:%M:%S")
    exit_spot = round(D.get_ltp(D.NIFTY_SPOT_TOKEN), 1)

    # Charges (reuse engine's calc)
    charges = {}
    try:
        charges = calculate_charges(entry_price, exit_price, qty, num_exit_orders=1)
        net_pnl = charges["net_pnl"]
        total_charges = charges["total_charges"]
    except Exception:
        net_pnl = pnl_rs
        total_charges = 0.0

    # Log to CSV + DB (entry_mode tagged V8 so we can split V7/V8 in analysis)
    try:
        _v8_row = {
            "date": date.today().isoformat(),
            "entry_time": entry_time, "exit_time": exit_time,
            "symbol": symbol, "direction": direction, "strike": strike,
            "entry_price": entry_price, "exit_price": exit_price,
            "pnl_pts": pnl_pts, "pnl_rs": pnl_rs,
            "gross_pnl_rs": pnl_rs, "net_pnl_rs": net_pnl,
            "peak_pnl": peak, "exit_reason": reason,
            "dte": dte_val, "candles_held": candles, "session": "",
            "sl_pts": -12, "vix_at_entry": 0,
            "entry_mode": "V10_" + tier,
            "bias": "", "hourly_rsi": 0,
            "brokerage": charges.get("brokerage", 0) if isinstance(charges, dict) else 0,
            "stt": charges.get("stt", 0) if isinstance(charges, dict) else 0,
            "exchange_charges": charges.get("exchange", 0) if isinstance(charges, dict) else 0,
            "gst": charges.get("gst", 0) if isinstance(charges, dict) else 0,
            "stamp_duty": charges.get("stamp", 0) if isinstance(charges, dict) else 0,
            "total_charges": total_charges, "num_exit_orders": 1,
            "qty_exited": qty, "entry_slippage": 0, "exit_slippage": 0,
            "lot_id": "ALL",
            "entry_ema9_high": "", "entry_ema9_low": "",
            "exit_ema9_high": "", "exit_ema9_low": "",
            "entry_band_position": "", "exit_band_position": "",
            "entry_body_pct": "",
            "xleg_signal": "", "xleg_other_close": "", "xleg_other_ema9l": "",
            "xleg_other_dying": "", "xleg_other_margin": "",
            "spike_close": "", "spike_target": "", "spike_fill": "", "spike_wait_used": "",
            "entry_spot": entry_spot_val, "exit_spot": exit_spot,
            "entry_atm_dist": entry_atm_dist,
            "neighbor_ltp_otm": neighbor_otm, "neighbor_ltp_itm": neighbor_itm,
            "max_otm_drift": round(max_otm_drift, 1),
        }
        import csv as _csv
        log_path = D.TRADE_LOG_PATH
        with open(log_path, "a", newline="") as f:
            w = _csv.DictWriter(f, fieldnames=TRADE_FIELDNAMES, extrasaction="ignore")
            w.writerow(_v8_row)
    except Exception as _le:
        logger.warning("[V10] Trade log write error: " + str(_le))

    logger.info("[V10] PAPER EXIT: " + symbol + " qty=" + str(qty)
                + " ref=" + str(exit_price) + " reason=" + reason
                + " pnl=" + str(pnl_pts) + "pts")

    _emoji = "🟢" if pnl_pts >= 0 else "🔴"
    _tg_send(
        "⚡ <b>V10 EXIT " + direction + " " + str(strike) + "</b>\n"
        + reason + "    " + ("+" if pnl_pts >= 0 else "") + "{:.1f}".format(pnl_pts) + " pts\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "Entry  Rs" + "{:.1f}".format(entry_price) + "\n"
        "Exit   Rs" + "{:.1f}".format(exit_price) + "\n"
        "Peak   +" + "{:.1f}".format(peak) + " pts  Trail " + str(tier) + "\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "Gross  " + ("+" if pnl_rs >= 0 else "") + "Rs" + "{:.0f}".format(pnl_rs) + "\n"
        "Charges -Rs" + "{:.0f}".format(total_charges) + "\n"
        "Net    " + ("+" if net_pnl >= 0 else "") + "Rs" + "{:.0f}".format(net_pnl) + "\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "V10 DAY " + ("+" if _v8_state.get("_pnl_today_pts", 0) >= 0 else "")
        + "{:.1f}".format(_v8_state.get("_pnl_today_pts", 0)) + " pts ("
        + str(_v8_state.get("_wins_today", 0)) + "W "
        + str(_v8_state.get("_losses_today", 0)) + "L)",
        priority="critical"
    )
    _save_v8_state()


def _v8_check_exit():
    """Tick-based exit check for V8 position. Called every scan cycle."""
    with _v8_lock:
        if not _v8_state.get("in_trade"):
            return
        token       = int(_v8_state.get("token", 0) or 0)
        entry_price = float(_v8_state.get("entry_price", 0))
        peak        = float(_v8_state.get("peak_pnl", 0))
        direction   = _v8_state.get("direction", "")
        strike      = int(_v8_state.get("strike", 0) or 0)
        # Increment candles_held once per minute (V10 equivalent of V7 line 8189)
        _cur_min = datetime.now().strftime("%H:%M")
        if _cur_min != _v8_state.get("_last_candle_min", ""):
            _v8_state["_last_candle_min"] = _cur_min
            _v8_state["candles_held"] = _v8_state.get("candles_held", 0) + 1

    if not token: return
    ltp = D.get_ltp(token)
    if ltp <= 0: return
    pnl = round(ltp - entry_price, 2)

    # Update peak
    if pnl > peak:
        with _v8_lock:
            _v8_state["peak_pnl"] = pnl
        peak = pnl

    # Track max OTM drift (data collection for smart strike management)
    _spot_now = D.get_ltp(D.NIFTY_SPOT_TOKEN)
    if _spot_now > 0 and strike > 0:
        _otm = max(0.0, (strike - _spot_now) if direction == "CE" else (_spot_now - strike))
        with _v8_lock:
            if _otm > _v8_state.get("max_otm_drift", 0.0):
                _v8_state["max_otm_drift"] = _otm

    # Compute trail SL using V7 ladder (12-step + 30/40/50)
    trail_sl, trail_tier = _v8_compute_trail_sl(entry_price, peak)
    with _v8_lock:
        prev_tier = _v8_state.get("active_ratchet_tier", "")
        _v8_state["active_ratchet_tier"] = trail_tier
        _v8_state["active_ratchet_sl"]   = trail_sl

    # Tier upgrade alert (matches V7 style)
    if prev_tier and prev_tier != trail_tier and trail_tier != "INITIAL":
        _tg_send(
            "⚡ <b>V10 SL UPGRADED → " + trail_tier + "</b>\n"
            "Peak +{:.1f}".format(peak) + " pts\n"
            "Prev " + str(prev_tier) + "  →  New " + trail_tier
            + "  SL Rs" + "{:.1f}".format(trail_sl),
            priority="critical"
        )
        _save_v8_state()

    # Emergency SL (-12) — TICK based
    if pnl <= -12:
        _v8_execute_paper_exit("EMERGENCY_SL", round(entry_price - 12, 2))
        return

    # Trail SL — TICK based for locked tiers (peak ≥ 12)
    if trail_tier != "INITIAL" and ltp <= trail_sl:
        _v8_execute_paper_exit("VISHAL_TRAIL", float(trail_sl))
        return

    # EOD exit
    eod_str = CFG.exit_ema9_band("eod_exit_time", "15:20") if hasattr(CFG, "exit_ema9_band") else "15:20"
    try:
        _eh, _em = eod_str.split(":")
        eod_mins = int(_eh) * 60 + int(_em)
    except Exception:
        eod_mins = 15 * 60 + 20
    now_mins = datetime.now().hour * 60 + datetime.now().minute
    if now_mins >= eod_mins:
        _v8_execute_paper_exit("EOD_EXIT", float(ltp))

# ═══════════════════════════════════════════════════════════════
#  STRIKE LOCKING — stable scanning, no flickering
# ═══════════════════════════════════════════════════════════════

_locked_ce_strike = None
_locked_pe_strike = None
_locked_at_spot   = None
_locked_tokens    = {}
_LOCK_SHIFT_THRESHOLD = 150  # relock if spot moves 150+ pts
_last_dash_args = {}  # cached dashboard args for post-exit refresh
_v8_last_entry_scan_ts = 0.0  # throttle V8 entry scan to every 3s
spot_3m: dict = {}  # BUG-B fix: module-level cache; updated by _write_dashboard() each call
# Shadow: dual-TF early entry tracking (1 week data collection before going live)
_v8_shadow_dt = {
    "active": False,       # CE shadow signal active
    "direction": "",       # CE or PE (kept for live_entry comparison)
    "bucket_ts": "",       # completed 3-min candle timestamp
    "entry_price": 0.0,    "entry_time": "",
    "peak_price": 0.0,     "peak_pts": 0.0,
    "live_entry": 0.0,
    "last_scan_ts": 0.0,
    # per-direction tracking
    "relock_ts": 0.0,   # unix ts of last ATM relock — blocks P1 signals 2 min
    "CE": {"active": False, "bucket_ts": "", "entry_price": 0.0, "entry_time": "",
           "peak_price": 0.0, "peak_pts": 0.0, "live_entry": 0.0,
           "shadow_sl": 0.0, "shadow_level": "INITIAL",
           "today_entry": 0.0, "today_date": "",
           "entry_tok": 0, "entry_strike": 0,
           "sl_ts": 0.0,  # unix ts of last SL-HIT — blocks re-entry 1 min
           "last_exit_pnl": 0.0, "last_exit_reason": "", "last_exit_ts": 0.0},
    "PE": {"active": False, "bucket_ts": "", "entry_price": 0.0, "entry_time": "",
           "peak_price": 0.0, "peak_pts": 0.0, "live_entry": 0.0,
           "shadow_sl": 0.0, "shadow_level": "INITIAL",
           "today_entry": 0.0, "today_date": "",
           "entry_tok": 0, "entry_strike": 0,
           "sl_ts": 0.0,  # unix ts of last SL-HIT — blocks re-entry 1 min
           "last_exit_pnl": 0.0, "last_exit_reason": "", "last_exit_ts": 0.0},
}

# Shadow Part 2 — buildup tracker (close > EMA9H, close < VWAP, RSI > 55 rising)
_v8_shadow_p2 = {
    "last_scan_ts": 0.0,
    "CE": {"active": False, "bucket_ts": "", "entry_price": 0.0, "entry_time": "",
           "peak_price": 0.0, "peak_pts": 0.0,
           "shadow_sl": 0.0, "shadow_level": "INITIAL",
           "today_entry": 0.0, "today_date": "",
           "p1_entry": 0.0, "entry_tok": 0, "entry_strike": 0,
           "exit_ts": 0.0,
           "last_exit_pnl": 0.0, "last_exit_reason": "", "last_exit_ts": 0.0},
    "PE": {"active": False, "bucket_ts": "", "entry_price": 0.0, "entry_time": "",
           "peak_price": 0.0, "peak_pts": 0.0,
           "shadow_sl": 0.0, "shadow_level": "INITIAL",
           "today_entry": 0.0, "today_date": "",
           "p1_entry": 0.0, "entry_tok": 0, "entry_strike": 0,
           "exit_ts": 0.0,
           "last_exit_pnl": 0.0, "last_exit_reason": "", "last_exit_ts": 0.0},
}

# ── V2 shadow trackers (A/B test: same entry, new exit logic) ──
# P1-V2: dynamic trail every 5s (LTP-8) after peak≥15, hard exit at +40
# P2-V2: same ratchet ladder as P1-V2 (peak<15 standard, peak≥15 entry+15 then +1/5s), hard exit +40
_v8_shadow_dt_v2 = {
    "CE": {"active": False, "entry_price": 0.0, "entry_time": "",
           "peak_price": 0.0, "peak_pts": 0.0, "shadow_sl": 0.0, "entry_tok": 0,
           "dyn_trail_ts": 0.0},
    "PE": {"active": False, "entry_price": 0.0, "entry_time": "",
           "peak_price": 0.0, "peak_pts": 0.0, "shadow_sl": 0.0, "entry_tok": 0,
           "dyn_trail_ts": 0.0},
}
_v8_shadow_p2_v2 = {
    "CE": {"active": False, "entry_price": 0.0, "entry_time": "",
           "peak_price": 0.0, "peak_pts": 0.0, "shadow_sl": 0.0, "entry_tok": 0,
           "dyn_trail_ts": 0.0},
    "PE": {"active": False, "entry_price": 0.0, "entry_time": "",
           "peak_price": 0.0, "peak_pts": 0.0, "shadow_sl": 0.0, "entry_tok": 0,
           "dyn_trail_ts": 0.0},
}

# Shadow P3 — extreme VWAP reversal (|fut-vwap| >= V10_P3_VWAP_EXTREME, shadow-only, no live trades)
_v8_shadow_p3 = {
    "last_scan_ts": 0.0,
    "CE": {"active": False, "bucket_ts": "", "entry_price": 0.0, "entry_time": "",
           "peak_price": 0.0, "peak_pts": 0.0,
           "shadow_sl": 0.0, "shadow_level": "INITIAL",
           "entry_tok": 0, "entry_strike": 0, "vwap_gap_at_entry": 0.0,
           "last_exit_pnl": 0.0, "last_exit_reason": "", "last_exit_ts": 0.0},
    "PE": {"active": False, "bucket_ts": "", "entry_price": 0.0, "entry_time": "",
           "peak_price": 0.0, "peak_pts": 0.0,
           "shadow_sl": 0.0, "shadow_level": "INITIAL",
           "entry_tok": 0, "entry_strike": 0, "vwap_gap_at_entry": 0.0,
           "last_exit_pnl": 0.0, "last_exit_reason": "", "last_exit_ts": 0.0},
}

_bw_scan_last_bucket: str = ""  # BW-SCAN: tracks last logged 1-min bucket

# DELAY-ANALYSIS: snapshot LTP at +5s/+10s/+30s/+60s after each P1/P2 signal
# Pure research — shadow only, no TG, no trade impact
_delay_jobs: list = []   # list of pending snapshot dicts

def _shadow_trail_sl(entry: float, peak_pts: float):
    """Return (sl_price, level_name) for shadow signal trail ladder."""
    if   peak_pts >= 50: return round(entry + 50, 1), "LOCK+50"
    elif peak_pts >= 40: return round(entry + 36, 1), "LOCK+36"
    elif peak_pts >= 36: return round(entry + 30, 1), "LOCK+30"
    elif peak_pts >= 30: return round(entry + 20, 1), "LOCK+20"
    elif peak_pts >= 24: return round(entry + 12, 1), "LOCK+12"
    elif peak_pts >= 18: return round(entry + 10, 1), "LOCK+10"
    elif peak_pts >= 12: return round(entry +  4, 1), "LOCK+4"
    else:                return round(entry - 12, 1), "INITIAL"

# ── Shadow Analysis Tracker (pure logging, zero trade impact) ──
# Tracks last 2 peak_pts per direction for P1 and P2 to detect dead-market streaks
_shadow_analysis = {
    "CE": {"last_peaks": [], "last_peaks_p2": [], "cross_buf": []},
    "PE": {"last_peaks": [], "last_peaks_p2": [], "cross_buf": []},
}

# ── v10 ENTRY GATES (derived 2026-06-02 from 79 pooled shadow signals across 6 days) ──
# near-VWAP + non-tiny-gap flips P1/P2 from -0.6 to +3.7 pts/trade (47%->71% win).
# Tunable on purpose — will tighten as more live days of data arrive.
V10_MIN_EMA9H_GAP = 3.5   # momentum breakout floor (gap 3.5-5 = +5.3/trade; below loses)
V10_RSI_MIN       = 55    # RSI floor (48-55 = 26% win loser zone)
V10_RSI_MAX       = 80    # RSI cap (raised 70→80 2026-06-03: 70 blocked a strong CE breakout, RSI 76→88 ran +20-50; no data proved 70+ loses)
V10_BW_MIN        = 5.0   # band-width floor (BW<5 = no energy)
V10_NEAR_VWAP_MAX = 0     # near-VWAP DISTANCE gate OFF (0 = disabled; set >0 to re-enable)
V10_OPEN_BLACKOUT_END = dtime(9, 45)  # hard gate: no entries before 9:45 (opening chop)
V10_P3_VWAP_EXTREME = 75  # P3 shadow: fire reversal CE/PE when |fut-vwap| >= this (data collection only)
# CUTOVER FLAG: True = P1/P2 (v10, 1-min) place the live paper trades.
V10_LIVE = True
# Live gate snapshot for dashboard monitoring — updated every shadow scan, per side
_v10_live_lock = threading.Lock()
_v10_live = {"CE": {}, "PE": {}}
_shadow_lock = threading.Lock()  # protects _v8_shadow_dt / _v8_shadow_p2 snapshots


def _log_shadow_analysis(signal_label, direction, fire_time, entry_price,
                         vwap_gap, other_vwap_gap, spot_adx, last_peaks,
                         ema9h_gap=0.0, xleg_buf=None, dte=0,
                         fut_vwap_gap=0.0, spot_ema9=0.0, spot_ema21=0.0, bw=0.0):
    """Log all analysis flags at signal fire — no trade impact."""
    flags = []

    # 1. Time blackout 13:00–14:15
    _h, _m = fire_time.hour, fire_time.minute
    if (_h == 13) or (_h == 14 and _m < 15):
        flags.append(f"DEAD_WINDOW({fire_time.strftime('%H:%M')})")

    # 2. ADX weak trend
    if 0 < spot_adx < 18:
        flags.append(f"WEAK_ADX({spot_adx})")

    # 3. Last 2 peaks both < 5 pts
    if len(last_peaks) >= 2 and all(p < 5 for p in last_peaks[-2:]):
        flags.append(f"LOW_PEAK_STREAK(last2={last_peaks[-2:]}")

    # 4. VWAP gap compression — both sides < 10 pts
    if vwap_gap is not None and other_vwap_gap is not None:
        if abs(vwap_gap) < 10 and abs(other_vwap_gap) < 10:
            flags.append(f"VWAP_COMPRESSED(self={vwap_gap:.1f} other={other_vwap_gap:.1f})")

    # 5. EMA9H gap bounds
    if ema9h_gap > 5:
        flags.append(f"EXTENDED_GAP({ema9h_gap:.2f})")
    elif 0 < ema9h_gap < 0.5:
        flags.append(f"TINY_GAP({ema9h_gap:.2f})")

    _xleg_note = ""
    if xleg_buf is not None and len(xleg_buf) >= 3:
        _buf = xleg_buf[-5:]
        _rejected = sum(1 for v in _buf if not v)
        _total = len(_buf)
        _other = "PE" if direction == "CE" else "CE"
        if _rejected == _total:
            _xleg_note = f"XLEG_CONFIRMED({_other} all{_total} below_ema9l)"
        elif _rejected < _total // 2:
            flags.append(f"XLEG_AMBIGUOUS({_other} only {_rejected}/{_total} below_ema9l)")

    # 7. Futures VWAP bias vs signal direction (data collection — no trade impact)
    if fut_vwap_gap != 0.0:
        _fv_bias = "BULL" if fut_vwap_gap > 0 else "BEAR"
        if (direction == "CE" and fut_vwap_gap < -15) or (direction == "PE" and fut_vwap_gap > 15):
            flags.append(f"FUT_VWAP_MISMATCH({_fv_bias} gap={fut_vwap_gap:+.0f})")

    # 8. Spot EMA9 vs EMA21 alignment — always shown as context tag
    _ema_note = ""
    if spot_ema9 > 0 and spot_ema21 > 0:
        _ema_align = "BULL" if spot_ema9 > spot_ema21 else "BEAR"
        _ema_note = f"SPOT_EMA_{_ema_align}(ema9={spot_ema9:.0f} ema21={spot_ema21:.0f})"

    # ── EXCELLENT SCORE (0-100, LOG-ONLY — zero trade impact) ─────────────
    # Composite of the confirmed winning DNA. This is a HYPOTHESIS under test,
    # not a verdict: OI-wall proximity (~10 pts of edge) is NOT yet wired, so
    # false positives during the open chop are EXPECTED — measuring them is the
    # whole point of this shadow phase. Grades: A+>=80 A>=65 B>=50 C<50.
    _es = 0
    _es_parts = []
    _aligned = False
    if spot_ema9 > 0 and spot_ema21 > 0:
        _aligned = ((direction == "CE" and spot_ema9 > spot_ema21) or
                    (direction == "PE" and spot_ema9 < spot_ema21))
    if _aligned:
        _es += 28; _es_parts.append("trend")
    if 0 < bw <= 6:
        _es += 17; _es_parts.append("bw")
    if 0.8 <= ema9h_gap <= 2.5:
        _es += 17; _es_parts.append("gap+")
    elif 2.5 < ema9h_gap <= 5.0:
        _es += 10; _es_parts.append("gap")
    elif ema9h_gap > 5.0 and _aligned:
        _es += 5;  _es_parts.append("gapX")
    elif 0 < ema9h_gap < 0.8:
        _es += 8;  _es_parts.append("gapT")
    if "XLEG_CONFIRMED" in _xleg_note:
        _es += 18; _es_parts.append("xleg")
    _trend_est = (fire_time.hour * 60 + fire_time.minute) >= 630   # past 10:30 open chop
    _adx_ok = spot_adx >= 18
    if _trend_est and _adx_ok:
        _es += 10; _es_parts.append("trendOK")
    elif _trend_est or _adx_ok:
        _es += 5
    if (direction == "PE" and fut_vwap_gap < 0) or (direction == "CE" and fut_vwap_gap > 0):
        _es += 10; _es_parts.append("fut")
    _grade = "A+" if _es >= 80 else ("A" if _es >= 65 else ("B" if _es >= 50 else "C"))
    _es_tag = f"EXCELLENT={_es}({_grade})[{'+'.join(_es_parts)}]"

    _dte_tag = f"DTE={dte}"
    if flags:
        logger.info(f"[ANALYSIS] {signal_label} {direction} entry={entry_price:.1f} {_dte_tag} — "
                    f"FLAGS: {' | '.join(flags)}"
                    + (f" | {_xleg_note}" if _xleg_note else "")
                    + (f" | {_ema_note}" if _ema_note else "")
                    + f" | {_es_tag}")
    else:
        logger.info(f"[ANALYSIS] {signal_label} {direction} entry={entry_price:.1f} {_dte_tag} — "
                    f"clean (no flags)"
                    + (f" | {_xleg_note}" if _xleg_note else "")
                    + (f" | {_ema_note}" if _ema_note else "")
                    + f" | {_es_tag}")


def _lock_strikes(spot, dte, kite=None, expiry=None):
    """Lock ATM strikes and subscribe tokens.
    v16.7 final: ATM CE+PE for trading + ATM±50 CE+PE for pre-warm.
    Pre-warmed neighbors mean zero indicator-warmup gap when spot
    drifts past hysteresis buffer and relock fire.
    Multi-candidate scan (when enabled) uses the same neighbor tokens.
    """
    global _locked_ce_strike, _locked_pe_strike, _locked_at_spot, _locked_tokens
    _locked_ce_strike = D.resolve_strike_for_direction(spot, "CE", dte)
    _locked_pe_strike = D.resolve_strike_for_direction(spot, "PE", dte)
    _locked_at_spot = spot
    _locked_tokens = {}

    if kite and expiry:
        # Active legs (ATM)
        for _dt, _strike in [("CE", _locked_ce_strike), ("PE", _locked_pe_strike)]:
            _tk = D.get_option_tokens(kite, _strike, expiry)
            if _tk.get(_dt):
                _locked_tokens[_dt] = _tk[_dt]
                _locked_tokens[_dt]["strike"] = _strike  # ensure strike survives into V8 entry display

        # Pre-warm neighbors — ATM±50 CE+PE (always, regardless of multi flag)
        # Keys: CE_UP / CE_DN / PE_UP / PE_DN
        for _suffix, _delta in (("UP", +50), ("DN", -50)):
            _ce_n_strike = _locked_ce_strike + _delta
            _pe_n_strike = _locked_pe_strike + _delta
            _ce_n_tk = D.get_option_tokens(kite, _ce_n_strike, expiry)
            if _ce_n_tk.get("CE"):
                _locked_tokens["CE_" + _suffix] = _ce_n_tk["CE"]
            _pe_n_tk = D.get_option_tokens(kite, _pe_n_strike, expiry)
            if _pe_n_tk.get("PE"):
                _locked_tokens["PE_" + _suffix] = _pe_n_tk["PE"]

        _sub_tokens = [v["token"] for v in _locked_tokens.values() if v.get("token")]
        if _sub_tokens:
            D.subscribe_tokens(_sub_tokens)

    logger.info("[MAIN] Strikes LOCKED: ATM=" + str(_locked_ce_strike)
                + " (neighbors " + str(_locked_ce_strike - 50)
                + "/" + str(_locked_ce_strike + 50)
                + " pre-warmed) at spot=" + str(round(spot, 1)))
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
    pinned against the Kite quota.

    EXCEPTION: tokens currently in the post-exit observation queue,
    the active trade's own token, and the active trade's other_token
    are skipped — those need to stay subscribed so exit monitoring
    and cross‑leg checks keep working."""
    global _locked_ce_strike, _locked_pe_strike, _locked_at_spot, _locked_tokens
    try:
        # Collect tokens currently being held for post-exit observation
        with _post_exit_lock:
            _post_exit_tokens = {tok for tok, _ in _post_exit_observation}
        _old = [v.get("token") for v in (_locked_tokens or {}).values()
                if isinstance(v, dict) and v.get("token")]
        # ── PATCH: also keep tokens of the currently open trade alive ──
        # Without this, a mid‑trade strike relock would unsubscribe the
        # opposite leg and break cross‑leg divergence monitoring.
        with _state_lock:
            _trade_tok = int(state.get("token", 0) or 0)
            _other_tok = int(state.get("other_token", 0) or 0)
        _keep = _post_exit_tokens | {_trade_tok, _other_tok} - {0}
        _to_drop = [t for t in _old if int(t) not in _keep]
        if _to_drop:
            D.unsubscribe_tokens(_to_drop)
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


_V8_PERSIST_FIELDS = [
    "in_trade", "symbol", "token", "direction", "strike",
    "entry_price", "entry_time", "qty",
    "peak_pnl", "active_ratchet_tier", "active_ratchet_sl",
    "candles_held", "_other_token",
    "_sl_cooldown_skip_next", "_force_exit_ts",
    "_pnl_today_pts", "_trades_today", "_wins_today", "_losses_today",
    "_v8_both_rejected_ts", "_last_trade_date", "_last_exit_candle_ts",
]

def _save_v8_state():
    try:
        with _v8_lock:
            subset = {k: _v8_state.get(k) for k in _V8_PERSIST_FIELDS}
        tmp = D.V8_STATE_FILE_PATH + ".tmp"
        with open(tmp, "w") as f:
            json.dump(subset, f, indent=2, default=str)
        os.replace(tmp, D.V8_STATE_FILE_PATH)
    except Exception as e:
        logger.error("[V10] State save error: " + str(e))

def _load_v8_state():
    if not os.path.isfile(D.V8_STATE_FILE_PATH):
        return
    try:
        with open(D.V8_STATE_FILE_PATH) as f:
            saved = json.load(f)
        with _v8_lock:
            for k, v in saved.items():
                if k in _v8_state:
                    _v8_state[k] = v
        logger.info("[V10] State loaded from disk")
        # Reset daily counters if state file is from a previous day
        _today = date.today().isoformat()
        _last_date = str(saved.get("_last_trade_date", ""))
        if _last_date != _today:
            with _v8_lock:
                _v8_state["_pnl_today_pts"] = 0.0
                _v8_state["_trades_today"]  = 0
                _v8_state["_wins_today"]    = 0
                _v8_state["_losses_today"]  = 0
                _v8_state["_v8_both_rejected_ts"] = 0.0
                _v8_state["_sl_cooldown_skip_next"] = False  # clear stale cooldown on new day
            logger.info("[V10] New trading day — daily counters reset (last_date=" + _last_date + ")")
        if _v8_state.get("in_trade"):
            _sym  = str(_v8_state.get("symbol", ""))
            _ep   = float(_v8_state.get("entry_price", 0))
            _peak = float(_v8_state.get("peak_pnl", 0))
            _tier  = str(_v8_state.get("active_ratchet_tier", "INITIAL"))
            _sl    = float(_v8_state.get("active_ratchet_sl", 0) or 0)
            if _sl <= 0: _sl = round(_ep - 12, 2)
            _tok   = int(_v8_state.get("token", 0) or 0)
            _ltp   = D.get_ltp(_tok) if _tok else 0
            _pnl   = round(_ltp - _ep, 1) if _ltp else 0
            _room  = round(_ltp - _sl, 1) if _ltp else 0
            _dir   = str(_v8_state.get("direction", ""))
            _strk  = str(_v8_state.get("strike", ""))
            _qty   = int(_v8_state.get("qty", 0) or 0)
            _etime = str(_v8_state.get("entry_time", ""))
            _emj   = "🟢" if _dir == "CE" else "🔴"
            logger.info("[V10] Was in trade on last shutdown — " + _sym + " monitoring resumed")
            _tg_send(
                "⚡ <b>V10 restarted mid-trade</b>\n"
                + _emj + " " + _dir + " " + _strk + " · qty " + str(_qty) + "\n"
                "━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                "Entry  Rs" + "{:.2f}".format(_ep) + "  @ " + _etime + "\n"
                + ("LTP    Rs" + "{:.2f}".format(_ltp)
                   + "  (" + ("+" if _pnl >= 0 else "") + str(_pnl) + " pts)\n" if _ltp else "LTP    — (no tick yet)\n")
                + "Peak   +" + "{:.1f}".format(_peak) + " pts\n"
                "━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                "Tier   " + _tier + " · SL Rs" + "{:.2f}".format(_sl)
                + ("  (Room " + ("+" if _room >= 0 else "") + str(_room) + ")" if _ltp else "") + "\n"
                "✅ Exit monitoring resumed."
            )
    except Exception as e:
        logger.error("[V10] State load error: " + str(e))


def _save_shadow_state():
    """Persist _v8_shadow_dt and _v8_shadow_p2 to disk so active signals survive restarts."""
    try:
        with _shadow_lock:
            _p1_snap = {"CE": dict(_v8_shadow_dt["CE"]), "PE": dict(_v8_shadow_dt["PE"])}
            _p2_snap = {"CE": dict(_v8_shadow_p2["CE"]), "PE": dict(_v8_shadow_p2["PE"])}
            _p1v2_snap = {"CE": dict(_v8_shadow_dt_v2["CE"]), "PE": dict(_v8_shadow_dt_v2["PE"])}
            _p2v2_snap = {"CE": dict(_v8_shadow_p2_v2["CE"]), "PE": dict(_v8_shadow_p2_v2["PE"])}
            _p3_snap = {"CE": dict(_v8_shadow_p3["CE"]), "PE": dict(_v8_shadow_p3["PE"])}
        with _v10_live_lock:
            _live_snap = {k: dict(v) for k, v in _v10_live.items()}
        payload = {
            "p1":    _p1_snap,
            "p2":    _p2_snap,
            "p1_v2": _p1v2_snap,
            "p2_v2": _p2v2_snap,
            "p3":    _p3_snap,
            "saved_date": date.today().isoformat(),
            "live": _live_snap,
        }
        tmp = D.SHADOW_STATE_FILE_PATH + ".tmp"
        with open(tmp, "w") as f:
            json.dump(payload, f, indent=2, default=str)
        os.replace(tmp, D.SHADOW_STATE_FILE_PATH)
    except Exception as e:
        logger.error("[SHADOW] State save error: " + str(e))


def _load_shadow_state():
    """Restore shadow signal state from disk on startup."""
    global _v8_shadow_dt, _v8_shadow_p2, _v8_shadow_dt_v2, _v8_shadow_p2_v2, _v8_shadow_p3
    if not os.path.isfile(D.SHADOW_STATE_FILE_PATH):
        return
    try:
        with open(D.SHADOW_STATE_FILE_PATH) as f:
            saved = json.load(f)
        # Only restore if from today — stale state from yesterday is useless
        if saved.get("saved_date") != date.today().isoformat():
            logger.info("[SHADOW] State file is from previous day — skipping restore")
            return
        for _dir in ("CE", "PE"):
            if _dir in saved.get("p1", {}):
                _v8_shadow_dt[_dir].update(saved["p1"][_dir])
            if _dir in saved.get("p2", {}):
                _v8_shadow_p2[_dir].update(saved["p2"][_dir])
            if _dir in saved.get("p1_v2", {}):
                _v8_shadow_dt_v2[_dir].update(saved["p1_v2"][_dir])
            if _dir in saved.get("p2_v2", {}):
                _v8_shadow_p2_v2[_dir].update(saved["p2_v2"][_dir])
            if _dir in saved.get("p3", {}):
                _v8_shadow_p3[_dir].update(saved["p3"][_dir])
        _p1_ce = _v8_shadow_dt["CE"].get("active", False)
        _p1_pe = _v8_shadow_dt["PE"].get("active", False)
        _p2_ce = _v8_shadow_p2["CE"].get("active", False)
        _p2_pe = _v8_shadow_p2["PE"].get("active", False)
        active_list = []
        if _p1_ce: active_list.append(f"P1-CE@{_v8_shadow_dt['CE'].get('entry_price',0)}")
        if _p1_pe: active_list.append(f"P1-PE@{_v8_shadow_dt['PE'].get('entry_price',0)}")
        if _p2_ce: active_list.append(f"P2-CE@{_v8_shadow_p2['CE'].get('entry_price',0)}")
        if _p2_pe: active_list.append(f"P2-PE@{_v8_shadow_p2['PE'].get('entry_price',0)}")
        if active_list:
            logger.info("[SHADOW] Restored active signals: " + ", ".join(active_list))
            pass  # shadow restore TG removed
        else:
            logger.info("[SHADOW] State loaded — no active signals")
        # Restore _v10_live so LIVE GATES show last-known values after restart
        # (P1 scan is blocked while in_trade, so without this they show "— no data —")
        with _v10_live_lock:
            for _dir in ("CE", "PE"):
                _saved_live = saved.get("live", {}).get(_dir, {})
                if _saved_live and "gap" in _saved_live:
                    _v10_live[_dir].update(_saved_live)
    except Exception as e:
        logger.error("[SHADOW] State load error: " + str(e))


def _reconcile_positions(kite):
    """
    Startup position reconciliation — compare saved state with MStock broker.
    If bot crashed mid-trade and position is gone at broker, reset state.
    If broker has position but state says no trade, alert for manual resolution.
    v13.2: Uses MStock get_net_position() — orders placed on MStock, not Kite.
    """
    if kite is None or D.PAPER_MODE:
        return
    try:
        mc        = MSTOCK.get_mstock()
        resp      = mc.get_net_position()
        data      = resp.json()
        positions = data.get("data", {}) if data.get("status") == "success" else {}
        net       = positions.get("net", []) if isinstance(positions, dict) else []
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
    # V8 shadow daily counters
    _v8_state["_signals_today"]    = 0
    _v8_state["_last_signal_time"] = ""
    _v8_state["_last_fired_candle_ts"] = ""
    with _v8_lock:
        _v8_state["_sl_cooldown_skip_next"] = False  # BUG-FIX: clear stale ESL flag on new day
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
        state["_or_refreshed_today"]   = False  # reset OR refresh flag
        # Clear persisted scan dedup key so a crash-restart landing at
        # 09:30:45 (after the loop already scanned 09:30) doesn't treat
        # the current minute as already-scanned and silently skip the
        # first entry of the session.
        state["_last_scan_key"]        = ""
    D.clear_token_cache()
    D.reset_daily_warnings()
    _reset_strike_lock()
    logger.info("[MAIN] _eod_exited reset for new day")
    logger.info("[MAIN] Daily reset")
    # Force recompute of institutional levels for the new day
    try:
        LEVELS._last_compute_day = None
        LEVELS._daily_levels = {}
        LEVELS._opt_levels = {}
        LEVELS.compute_today(D, _kite, None)
    except Exception as _le:
        logger.debug(f"[LEVELS] daily recompute error: {_le}")

    # Reset VWAP for new day
    try:
        LEVELS._vwap_state = {"fut_close": 0.0, "vwap": 0.0,
                              "gap": 0.0, "last_update": None}
        state["_last_vwap_15m_slot"] = -1
        LEVELS.update_vwap(_kite)
    except Exception as _ve:
        logger.debug(f"[VWAP] daily reset error: {_ve}")
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

TRADE_FIELDNAMES = [
    "date", "entry_time", "exit_time", "symbol", "direction", "strike",
    "entry_price", "exit_price", "pnl_pts", "pnl_rs",
    "gross_pnl_rs", "net_pnl_rs",
    "peak_pnl", "exit_reason",
    "dte", "candles_held", "session", "sl_pts",
    "vix_at_entry", "entry_mode",
    "bias", "hourly_rsi",
    "brokerage", "stt", "exchange_charges", "gst", "stamp_duty",
    "total_charges", "num_exit_orders", "qty_exited",
    "entry_slippage", "exit_slippage", "lot_id",
    "entry_ema9_high", "entry_ema9_low",
    "exit_ema9_high", "exit_ema9_low",
    "entry_band_position", "exit_band_position",
    "entry_body_pct",
    # v16.7 Cross-leg divergence (LOG ONLY — 1-week eval)
    "xleg_signal", "xleg_other_close", "xleg_other_ema9l",
    "xleg_other_dying", "xleg_other_margin",
    # v16.7 Anti-spike pullback entry tracking
    "spike_close", "spike_target", "spike_fill", "spike_wait_used",
    # Strike management data collection (added for smart strike analysis)
    "entry_spot", "exit_spot", "entry_atm_dist",
    "neighbor_ltp_otm", "neighbor_ltp_itm", "max_otm_drift",
]

def _trade_csv_reader(f):
    """Return a DictReader that works whether or not the trade log has a header row.
    Peeks at the first 4 bytes: if it starts with 'date' the file has a header,
    otherwise inject TRADE_FIELDNAMES so no data row is silently consumed."""
    first = f.read(4)
    f.seek(0)
    if first.startswith("date"):
        return csv.DictReader(f)
    return csv.DictReader(f, fieldnames=TRADE_FIELDNAMES)


def _cleanup_trade_log():
    """One-time cleanup: remove corrupted rows where date doesn't match YYYY-MM-DD."""
    path = D.TRADE_LOG_PATH
    if not os.path.isfile(path):
        return
    try:
        import re
        date_re = re.compile(r"^\d{4}-\d{2}-\d{2}$")
        with open(path, "r") as f:
            reader = _trade_csv_reader(f)
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
        # v16.7 Cross-leg divergence
        "xleg_signal":         st.get("_xleg_signal", "NA") or "NA",
        "xleg_other_close":    round(float(st.get("_xleg_other_close", 0) or 0), 2),
        "xleg_other_ema9l":    round(float(st.get("_xleg_other_ema9l", 0) or 0), 2),
        "xleg_other_dying":    bool(st.get("_xleg_other_dying", False)),
        "xleg_other_margin":   round(float(st.get("_xleg_other_margin", 0) or 0), 2),
        # v16.7 Anti-spike pullback
        "spike_close":         round(float(st.get("_spike_close", 0) or 0), 2),
        "spike_target":        round(float(st.get("_spike_target", 0) or 0), 2),
        "spike_fill":          round(float(st.get("_spike_fill", 0) or 0), 2),
        "spike_wait_used":     round(float(st.get("_spike_wait_used", 0) or 0), 1),
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
    _num_exit_orders = 1
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

    # One-shot header migration: add missing columns or prepend header when absent
    if not is_new:
        try:
            with open(D.TRADE_LOG_PATH, "r", newline="") as _f_chk:
                _r_chk = csv.reader(_f_chk)
                _hdr = next(_r_chk, None) or []
            _has_header = "date" in _hdr
            _missing = [c for c in TRADE_FIELDNAMES if c not in _hdr] if _has_header else list(TRADE_FIELDNAMES)
            if _missing or not _has_header:
                logger.info("[MAIN] Trade-log header upgrade: has_header="
                            + str(_has_header) + " missing=" + str(_missing))
                with open(D.TRADE_LOG_PATH, "r", newline="") as _f_rd:
                    if _has_header:
                        _old_rows = list(csv.DictReader(_f_rd))
                    else:
                        _old_rows = list(csv.DictReader(_f_rd, fieldnames=TRADE_FIELDNAMES))
                with open(D.TRADE_LOG_PATH, "w", newline="") as _f_wr:
                    _w = csv.DictWriter(_f_wr, fieldnames=TRADE_FIELDNAMES,
                                        extrasaction="ignore")
                    _w.writeheader()
                    for _orow in _old_rows:
                        for _c in _missing:
                            _orow.setdefault(_c, "")
                        _w.writerow(_orow)
                logger.info("[MAIN] Trade-log header upgrade: rewrote "
                            + str(len(_old_rows)) + " rows with new schema")
        except Exception as _me:
            logger.warning("[MAIN] Trade-log header migration error: " + str(_me))

    try:
        with open(D.TRADE_LOG_PATH, "a", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=TRADE_FIELDNAMES, extrasaction="ignore")
            if is_new:
                writer.writeheader()
            writer.writerow(row)
            f.flush()
    except Exception as e:
        logger.error("[MAIN] Trade log error: " + str(e))

def _read_today_trades() -> list:
    today_str = date.today().isoformat()
    trades    = []
    if not os.path.isfile(D.TRADE_LOG_PATH):
        return trades
    try:
        with open(D.TRADE_LOG_PATH, "r") as f:
            for row in _trade_csv_reader(f):
                if row.get("date", "") == today_str:
                    trades.append(row)
    except Exception as e:
        logger.error("[MAIN] Read trades error: " + str(e))
    return trades

def _compute_rolling_stats(n: int = 20) -> dict:
    """Read last n closed trades and return win-rate/pts/streak stats."""
    trades = []
    if not os.path.isfile(D.TRADE_LOG_PATH):
        return {"last10_wr": 0, "last20_wr": 0, "last10_pts": 0, "streak": 0}
    try:
        with open(D.TRADE_LOG_PATH, "r") as f:
            for row in _trade_csv_reader(f):
                trades.append(row)
    except Exception:
        return {"last10_wr": 0, "last20_wr": 0, "last10_pts": 0, "streak": 0}
    trades = trades[-n:] if len(trades) > n else trades
    last20 = trades
    last10 = trades[-10:] if len(trades) >= 10 else trades
    def _wr(t):
        wins = sum(1 for r in t if float(r.get("pnl_pts", 0) or 0) > 0)
        return round(wins / len(t) * 100) if t else 0
    def _pts(t):
        return round(sum(float(r.get("pnl_pts", 0) or 0) for r in t), 1)
    # Streak: count consecutive wins (+) or losses (-) from the end
    streak = 0
    if last20:
        last_sign = 1 if float(last20[-1].get("pnl_pts", 0) or 0) > 0 else -1
        for r in reversed(last20):
            sign = 1 if float(r.get("pnl_pts", 0) or 0) > 0 else -1
            if sign == last_sign:
                streak += last_sign
            else:
                break
    return {
        "last10_wr": _wr(last10),
        "last20_wr": _wr(last20),
        "last10_pts": _pts(last10),
        "streak": streak,
    }

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
    if symbol.endswith("CE"):
        return "CE"
    elif symbol.endswith("PE"):
        return "PE"
    return symbol

from collections import deque as _deque
_tg_timestamps = _deque(maxlen=20)
_TG_FLOOD_LIMIT = 15   # was 5 — Telegram allows ~30/sec; 15/10s is safe
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
            else:
                logger.debug("[TG] sent ok — " + text[:60].replace("\n", " "))
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
    # ── Startup spam suppression — skip TG alert if restarted within 10 min ──
    _ts_file = os.path.join(os.path.expanduser("~"), "logs", "live", ".last_bot_start_ts")
    _now_ts = time.time()
    try:
        if os.path.exists(_ts_file):
            with open(_ts_file) as _tf:
                _last_ts = float(_tf.read().strip())
            if _now_ts - _last_ts < 600:  # 10 min cooldown
                logger.info(f"[MAIN] Startup TG alert suppressed — last restart {int(_now_ts - _last_ts)}s ago")
                with open(_ts_file, "w") as _tf:
                    _tf.write(str(_now_ts))
                return
    except Exception:
        pass
    try:
        os.makedirs(os.path.dirname(_ts_file), exist_ok=True)
        with open(_ts_file, "w") as _tf:
            _tf.write(str(_now_ts))
    except Exception:
        pass

    _web_url = "http://" + _WEB_IP + ":8080" if _WEB_IP and _WEB_IP != "unknown" else "http://localhost:8080"
    _acct = D.get_account_info()
    _acct_line = ""
    if _acct.get("name"):
        _acct_line = ("Account : " + _acct["name"] + "\n"
                      "Balance : Rs" + "{:,}".format(int(_acct.get("total_balance", 0))) + "\n")
    try:
        _ms_line = "Orders  : " + MSTOCK.ms_get_banner_line() + "\n"
    except Exception:
        _ms_line = ""
    _tg_send(
        "<b>VISHAL RAJPUT TRADE " + D.VERSION + "</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "Time    : " + _now_str() + "\n"
        "Mode    : " + _mode_tag() + "\n"
        + _acct_line
        + _ms_line +
        "Web     : " + _web_url + "\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "<b>STRATEGY</b>  Vishal Clean v20\n"
        ""
        "V10 LIVE   : 1-min  | P1+P2 | PAPER trading\n"
        "Entry   : " + CFG.entry_ema9_band("warmup_until_v8", "09:35") + " - " + CFG.entry_ema9_band("cutoff_after", "15:00") + " IST\n"
        "Size    : 2 lots fixed\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "<b>V10 GATES (P1)</b>\n"
        "1) ema9h_gap >= " + str(V10_MIN_EMA9H_GAP) + " pts (momentum break)\n"
        "2) RSI " + str(V10_RSI_MIN) + "-" + str(V10_RSI_MAX) + " AND rising\n"
        "3) Band width >= " + str(V10_BW_MIN) + " pts (energy)\n"
        "4) XLEG_CONFIRMED (cross-leg dying)\n"
        "5) LTP on correct side of VWAP at fire\n"
        "   near-VWAP gate OFF · trend-align = warning only\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "<b>V10 SL LADDER</b>\n"
        "peak < 12  → INITIAL  entry - 12\n"
        "peak >= 12 → LOCK_4   entry + 4\n"
        "peak >= 24 → LOCK_12  entry + 12\n"
        "peak >= 30 → LOCK_20  entry + 20\n"
        "peak >= 36 → LOCK_30  entry + 30\n"
        "peak >= 40 → LOCK_36  entry + 36\n"
        "peak >= 50 → LOCK_50  entry + 50\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "<b>EXITS</b>  Emergency -12 | EOD 15:20 | Trail\n"
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

def _wait_for_pullback(token: int, target_price: float, timeout_secs: int) -> tuple:
    """Anti-spike limit-pullback: poll LTP up to timeout_secs.
    Fill at current LTP the moment it touches target (close-buffer).
    Returns (fill_price, elapsed_secs) on fill, (None, elapsed_secs)
    on timeout. Aborts early if bot paused or market closes.

    Hard requirement: caller must not be in_trade (entry-path only).
    """
    if timeout_secs <= 0 or target_price <= 0 or token <= 0:
        return None, 0
    deadline = time.time() + timeout_secs
    start = time.time()
    while time.time() < deadline:
        if state.get("paused"):
            return None, round(time.time() - start, 1)
        if not D.is_market_open():
            return None, round(time.time() - start, 1)
        try:
            ltp = D.get_ltp(token)
        except Exception:
            ltp = 0
        if ltp and ltp > 0 and ltp <= target_price:
            return float(ltp), round(time.time() - start, 1)
        time.sleep(1)
    return None, float(timeout_secs)


def _execute_entry(kite, option_info: dict, option_type: str,
                   entry_result: dict, profile: dict,
                   expiry, dte: int, session: str = "MORNING"):
    token       = option_info["token"]
    symbol      = option_info["symbol"]
    entry_price = entry_result["entry_price"]

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
    hard_sl = abs(CFG.exit_ema9_band("emergency_sl_pts", -12))
    phase1_sl = compute_entry_sl(actual_price, hard_sl)

    # Extract the OTHER side token for manage_exit divergence check.
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
        # Same-candle guard: remember which closed candle this entry came
        # from so engine rejects re-entry on the same candle for any reason
        # (cooldown=0, fast scan loop, immediate emergency exit, etc.).
        _fts = entry_result.get("fired_candle_ts") if entry_result else None
        if _fts:
            state["_last_fired_candle_ts"] = str(_fts)
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
        try:
            _trade_strike = state["strike"]
            _trade_dir    = option_type
            _tce = int((_ce_locked or {}).get("token", 0) or 0)
            _tpe = int((_pe_locked or {}).get("token", 0) or 0)
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
        state["_last_milestone"]    = 0
        # v15.0 entry context — band values at entry
        state["entry_mode"]         = entry_result.get("entry_mode", "EMA9_BREAKOUT")
        state["entry_ema9_high"]    = round(float(entry_result.get("ema9_high", 0)), 2)
        state["entry_ema9_low"]     = round(float(entry_result.get("ema9_low", 0)), 2)
        state["entry_band_position"] = entry_result.get("band_position", "ABOVE")
        state["entry_body_pct"]     = round(float(entry_result.get("body_pct", 0)), 1)
        # Cross-leg divergence (LOG ONLY for 1-week eval; never blocks)
        state["_xleg_signal"]       = entry_result.get("xleg_signal", "NA")
        state["_xleg_other_close"]  = round(float(entry_result.get("xleg_other_close", 0) or 0), 2)
        state["_xleg_other_ema9l"]  = round(float(entry_result.get("xleg_other_ema9l", 0) or 0), 2)
        state["_xleg_other_dying"]  = bool(entry_result.get("xleg_other_dying", False))
        state["_xleg_other_margin"] = round(float(entry_result.get("xleg_other_margin", 0) or 0), 2)
        # Anti-spike pullback
        state["_spike_close"]       = round(float(entry_result.get("spike_close", 0) or 0), 2)
        state["_spike_target"]      = round(float(entry_result.get("spike_target", 0) or 0), 2)
        state["_spike_fill"]        = round(float(entry_result.get("spike_fill", 0) or 0), 2)
        state["_spike_wait_used"]   = round(float(entry_result.get("spike_wait_used", 0) or 0), 1)
        state["current_ema9_high"]  = round(float(entry_result.get("ema9_high", 0)), 2)
        state["current_ema9_low"]   = round(float(entry_result.get("ema9_low", 0)), 2)
        state["last_band_check_ts"] = ""
        state["other_token"]        = _other_token_entry

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
    _bw = float(entry_result.get("band_width", 0))
    _entry_mode_tag = entry_result.get("entry_mode", "EMA9_BREAKOUT")

    # Cross-leg divergence — display only, /xleg shows weekly accuracy
    _xls = entry_result.get("xleg_signal", "NA")
    _xl_other = "PE" if option_type == "CE" else "CE"
    _xl_margin = float(entry_result.get("xleg_other_margin", 0) or 0)
    if _xls == "PASS":
        _xl_line = ("X-Leg   ✓ " + _xl_other + " dying ("
                    + "{:+.1f}".format(_xl_margin) + " below own EMA9L)\n")
    elif _xls == "FAIL":
        _xl_line = ("X-Leg   ✗ " + _xl_other + " holding ("
                    + "{:+.1f}".format(_xl_margin) + " above own EMA9L)\n")
    else:
        _xl_line = "X-Leg   — no data\n"

    _rsi  = float(entry_result.get("rsi", 0) or 0)
    _rsi_prev = float(entry_result.get("rsi_prev", 0) or 0)
    _rsi_arrow = "↑" if entry_result.get("rsi_rising") else "↓"
    _core = (
        "Entry   Rs" + "{:.2f}".format(actual_price) + "   @ " + _tm + " (15-min)\n"
        "Mode    " + str(_entry_mode_tag) + "\n"
        "Close   " + "{:.1f}".format(_close) + "  &gt;  EMA9L " + "{:.1f}".format(_ema9l) + "\n"
        "RSI     " + "{:.1f}".format(_rsi) + " " + _rsi_arrow
        + " (prev " + "{:.1f}".format(_rsi_prev) + ")\n"
        + _xl_line +
        "Band    " + "{:.1f}".format(_bw) + " pts  (display)\n"
    )

    # V6 single emergency floor + simple trail ladder.
    _sl_pts = abs(CFG.exit_ema9_band("emergency_sl_pts", -12))
    _initial_sl = round(actual_price - _sl_pts, 1)
    _stop_block = (
        "<b>STOP</b>\n"
        "Hard SL   -" + str(_sl_pts) + " pts (Rs"
        + "{:.1f}".format(_initial_sl) + ")\n"
        "Trail (V10): ≥12→+4 | ≥24→+12 | ≥30→+20 | ≥36→+30 | ≥40→+36 | ≥50→+50\n"
    )

    _slip_block = ""
    if _entry_slippage and abs(float(_entry_slippage)) > 0.05:
        _slip_block = "Slippage: " + "{:+.2f}".format(float(_entry_slippage)) + " pts\n"

    _tg_send(
        "🕐 <b>V10 ENTRY " + ("FRESH" if _entry_mode_tag == "CLOSE_FILL" else str(_entry_mode_tag)) + "</b>\n"
        + _dir_emoji + " <b>" + _sym + " " + _strike_label + " x "
        + str(lot_count) + " LOTS</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        + _core +
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        + _stop_block
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
        # so the exit alert reports the real tier (LOCK_3/LOCK_5/LOCK_8/LOCK_15/LOCK_DYN)
        # instead of always falling back to INITIAL.
        _tier_snapshot = state.get("active_ratchet_tier", "") or "INITIAL"
        # v15.0: entry confirmation = band position at entry
        _entry_eh = round(float(state.get("entry_ema9_high", 0)), 1)
        _entry_el = round(float(state.get("entry_ema9_low", 0)), 1)
        _entry_body = int(round(float(state.get("entry_body_pct", 0)), 0))
        _entry_mode_e = state.get("entry_mode", "EMA9_BREAKOUT")
        _entry_conf = (_entry_mode_e + " | entry close &gt; EMA9h "
                       + str(_entry_eh) + " | body " + str(_entry_body) + "%")

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
    # ── Partial fill check: if broker filled less than requested, log + alert ──
    _filled_qty = fill.get("fill_qty", exit_qty)
    if fill["ok"] and _filled_qty < exit_qty:
        _unfilled = exit_qty - _filled_qty
        logger.warning(f"[TRADE] PARTIAL FILL: filled {_filled_qty}/{exit_qty}, "
                       f"unfilled {_unfilled} — manual close needed for remaining")
        _tg_send(f"⚠️ <b>PARTIAL FILL</b> {symbol}\n"
                 f"Filled: {_filled_qty}/{exit_qty}\n"
                 f"Remaining {_unfilled} units — <b>CLOSE MANUALLY</b>")
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

    trade_done = not state.get("lot1_active") and not state.get("lot2_active")
    pnl_lots = pnl

    _log_trade(state, actual_exit, reason, candles, saved_entry=entry,
               lot_id=lot_id, qty=exit_qty)

    if trade_done:
        with _state_lock:
            state["daily_pnl"] = round(state.get("daily_pnl", 0) + pnl_lots, 2)
            state["last_exit_time"] = datetime.now().isoformat()
            state["last_exit_direction"] = direction
            state["last_exit_peak"] = peak
            state["last_exit_reason"] = reason
            if reason == "EMERGENCY_SL":
                state["_sl_cooldown_skip_next"] = True
            state["last_exit_price"] = round(actual_exit, 2)
            old_token = state["token"]
            # Capture strike + direction BEFORE state.update() wipes them
            # so we can register the exited strike with VRL_DATA for
            # post-exit lab data capture.
            old_strike = state.get("strike", 0)
            old_dir    = state.get("direction", "")
            old_entry_close = float(state.get("entry_price", 0) or 0)
            try:
                D.clear_active_trade()
            except Exception:
                pass
            # ── PATCH: store exit timestamp as epoch seconds ──
            _exit_epoch = time.time()
            state.update({
                "in_trade": False, "symbol": "", "token": None,
                "direction": "", "strike": 0,
                "entry_price": 0.0, "entry_time": "",
                "_static_floor_sl": 0.0, "current_floor": 0.0,
                "peak_pnl": 0.0,
                "candles_held": 0, "force_exit": False, "_exit_failed": False,
                "active_ratchet_tier": "", "active_ratchet_sl": 0.0,
                "_last_milestone": 0,
                # Re-entry watcher (V7): 2-candle window after exit.
                # Each new 15-min candle close is a re-entry attempt.
                # If 2 consecutive attempts fail, window expires and we
                # rely on fresh-entry path only.
                "_reentry_armed":      (reason != "FORCE_EXIT"),
                "_reentry_exit_ts":    _exit_epoch,
                "_reentry_attempts":   0,
                "_reentry_last_checked_epoch": 0.0,
                "_reentry_direction":  str(old_dir or ""),
                "_reentry_token":      int(old_token or 0),
                "_reentry_strike":     int(old_strike or 0),
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
            })
        if old_token:
            import time as _time_post
            _expire_at = _time_post.time() + (POST_EXIT_OBSERVATION_MINUTES * 60)
            with _post_exit_lock:
                _post_exit_observation.append((int(old_token), _expire_at))
            try:
                if old_strike:
                    D.register_post_exit_observation(
                        token=int(old_token),
                        strike=int(old_strike),
                        side=str(old_dir or ""),
                        expire_at=_expire_at,
                    )
            except Exception as _re:
                logger.debug("[POST_EXIT] register err: " + str(_re))
            logger.info(
                "[POST_EXIT] Token " + str(old_token)
                + " (" + str(old_dir) + " " + str(old_strike) + ")"
                + " held " + str(POST_EXIT_OBSERVATION_MINUTES)
                + " min for post-exit observation"
            )
        _reset_strike_lock()
        _day_pnl    = state.get("daily_pnl", 0)
        _sym_short  = _short_sym(symbol, direction, _exit_strike)
        _pnl_sign   = "+" if pnl >= 0 else ""
        _day_rs     = int(_day_pnl * D.get_lot_size())
        _cd_cfg     = CFG.get().get("cooldown", {})
        _num_eo = 2 if state.get("lots_split") else 1
        try:
            _ch = CHARGES.calculate_charges(entry, actual_exit,
                      exit_qty, _num_eo)
        except Exception:
            _ch = {"gross_pnl": pnl * (exit_qty / D.get_lot_size()) * D.get_lot_size(),
                   "total_charges": 0, "net_pnl": pnl * (exit_qty / D.get_lot_size()) * D.get_lot_size(),
                   "charges_pts": 0}
        _dir_emoji = "🟢" if direction == "CE" else "🔴"
        _sym_exit  = _short_sym(symbol, direction, _exit_strike)
        _sign_pnl  = "+" if pnl >= 0 else ""
        _net_sign  = "+" if _ch["net_pnl"] >= 0 else "-"

        _reason_line = ""
        _tier = _tier_snapshot
        if reason == "VISHAL_TRAIL":
            _reason_line = "Trail " + _tier + " triggered\n"
            _trig_close = exit_info.get("trigger_close")
            _trig_time = exit_info.get("trigger_time", "")
            _trig_sl = exit_info.get("trigger_sl")
            if _trig_close is not None and _trig_sl is not None:
                _reason_line += ("Trigger " + (str(_trig_time) + " " if _trig_time else "")
                                + "close Rs" + "{:.1f}".format(_trig_close)
                                + " (≤ SL Rs" + "{:.1f}".format(_trig_sl) + ")\n")
        _capture_line = ""
        try:
            _peak_f = float(peak) if peak else 0
            if _peak_f >= 1.0:
                _cap = int(round(pnl / _peak_f * 100))
                _capture_line = "Capture " + str(_cap) + "%\n"
            elif _peak_f > 0:
                _capture_line = "Capture —\n"
        except Exception:
            pass

        _tg_send(
            _dir_emoji + " <b>V10 EXIT " + _sym_exit + "</b>\n"
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
    if not D.PAPER_MODE:
        try:
            D.refresh_margin(kite)
        except Exception:
            pass
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
            dash.setdefault("market", {})["spot"] = round(spot, 1)
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

        # Update V10 position if in trade (_v8_state — V7 state never has V10 trades)
        with _v8_lock:
            _v10_it = _v8_state.get("in_trade", False)
            _v10_tk = _v8_state.get("token", 0)
            _v10_ep = _v8_state.get("entry_price", 0)
            _v10_pk = _v8_state.get("peak_pnl", 0)
        if _v10_it and _v10_tk:
            opt_ltp = D.get_ltp(_v10_tk)
            if opt_ltp > 0:
                pos = dash.get("position", {})
                pos["ltp"] = round(opt_ltp, 2)
                pos["pnl"] = round(opt_ltp - _v10_ep, 1)
                pos["peak"] = round(_v10_pk, 1)
        elif not _v10_it and dash.get("position", {}).get("in_trade"):
            dash["position"] = {"in_trade": False}

        dash["ts"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        dash["version"] = D.VERSION
        dash.setdefault("market", {})["market_open"] = D.is_market_open()

        tmp = dash_path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(dash, f, default=str)
        os.replace(tmp, dash_path)
    except Exception:
        pass


def _warmup_info(now, dte):
    """Returns (is_warm, candles_done, candles_needed, eta_hhmm)."""
    needed = 14
    done = 0
    try:
        df = D.get_historical_data(D.NIFTY_SPOT_TOKEN, "3minute", 30)
        if df is not None and not df.empty:
            done = min(needed, len(df))
    except Exception:
        pass
    is_warm = done >= needed
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
    global spot_3m  # BUG-B fix: update module-level cache so strategy loop can read it
    if dir_strikes is None:
        dir_strikes = {}
    try:
        with _state_lock:
            st = dict(state)
        with _v8_lock:
            st_v10 = dict(_v8_state)

        spot_3m = {}
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

        def _build_signal(opt_type, result):
            _ltp_fallback = 0
            try:
                _tk = (dir_strikes or {}).get(opt_type, atm_strike)
                _ltp_fallback = D.get_ltp((_locked_tokens or {}).get(opt_type, {}).get("token", 0)) or 0
            except Exception:
                pass
            if not result:
                return {
                    "close": 0, "ema9_high": 0, "ema9_low": 0,
                    "band_width": 0, "bw_pct": 0, "body_pct": 0,
                    "fired": False,
                    "verdict": "MARKET CLOSED" if not D.is_market_open() else "WARMING UP",
                    "ltp": round(_ltp_fallback, 2),
                    "strike": dir_strikes.get(opt_type, atm_strike),
                    "g1_gap_ok": False, "g2_rsi_ok": False,
                    "g3_bw_ok": False,
                    "g4_other_falling": False, "g5_above_ema9l": False,
                    "rsi": 0, "rsi_prev": 0,
                    "ema9_low_slope": 0,
                }
            _fired = result.get("fired", False)
            _mode = result.get("entry_mode", "")
            _close = float(result.get("close", result.get("entry_price", 0)))
            _eh = float(result.get("ema9_high", 0))
            _el = float(result.get("ema9_low", 0))
            _bw = round(_eh - _el, 1)
            _body = float(result.get("body_pct", 0))
            _green = bool(result.get("candle_green", False))
            _reject = result.get("reject_reason", "")
            _rsi = round(float(result.get("rsi", 0) or 0), 1)
            _rsi_prev = round(float(result.get("rsi_prev", 0) or 0), 1)
            _slope = round(float(result.get("ema9_low_slope", 0) or 0), 2)

            # V10 gate pass/fail flags (must match the REAL v10 gates in the fire path)
            _gap = round(_close - _eh, 2) if _eh > 0 else 0.0
            _bw_pct = round(_bw / _close * 100, 2) if _close > 0 else 0.0
            _g1 = (_gap >= V10_MIN_EMA9H_GAP)                        # ema9h_gap >= 3.5
            _g2 = (V10_RSI_MIN < _rsi < V10_RSI_MAX and _rsi > _rsi_prev) if _rsi > 0 else False  # RSI 55-80 rising
            _g3 = (_bw >= V10_BW_MIN)                                 # BW >= 5
            _g4 = bool(result.get("g4_other_falling", result.get("xleg_other_dying", False)))  # xleg
            _g5 = (_close > _el) if (_el > 0 and _close > 0) else False  # close > ema9l (basic trend)

            if _fired:
                verdict = "✅ ALL GATES PASSED"
            elif _reject:
                verdict = _reject
            else:
                _fails = []
                if not _g1: _fails.append(f"G1:gap={_gap:.1f}(need>={V10_MIN_EMA9H_GAP})")
                if not _g2: _fails.append(f"G2:RSI={_rsi}(need {V10_RSI_MIN}-{V10_RSI_MAX}↑)")
                if not _g3: _fails.append(f"G3:BW={_bw:.1f}(need>={V10_BW_MIN})")
                if not _g4: _fails.append("G4:xleg_not_confirmed")
                if not _g5: _fails.append(f"G5:below_ema9l({round(_close,1)}<{round(_el,1)})")
                verdict = _fails[0] if _fails else "scanning"

            _ltp_out = round(result.get("entry_price", 0) or _ltp_fallback, 2)
            if _ltp_out == 0: _ltp_out = round(_ltp_fallback, 2)

            return {
                "close": round(_close, 2),
                "ema9_high": round(_eh, 2),
                "ema9_low": round(_el, 2),
                "band_width": _bw,
                "bw_pct": _bw_pct,
                "body_pct": round(_body, 1),
                "fired": _fired,
                "verdict": verdict,
                "ltp": _ltp_out,
                "strike": result.get("_strike", dir_strikes.get(opt_type, atm_strike)),
                "rsi": _rsi,
                "rsi_prev": _rsi_prev,
                "ema9_low_slope": _slope,
                "g1_gap_ok": _g1,
                "g2_rsi_ok": _g2,
                "g3_bw_ok": _g3,
                "g4_other_falling": _g4,
                "g5_above_ema9l": _g5,
                "g6_stochrsi": result.get("g6_stochrsi_os_cross"),
                "g6_k": result.get("g6_k_now", 0),
            }

        _is_warm, _w_done, _w_need, _w_eta = _warmup_info(now, dte)
        # Feed dashboard from the live v10 gate snapshot (updated every scan by P1/P2)
        def _v10_to_result(side):
            """Convert _v10_live snapshot to a result dict that _build_signal understands."""
            if not D.is_market_open():
                return None
            with _v10_live_lock:
                lv = dict(_v10_live.get(side, {}))
            if not lv or lv.get("gap") is None:
                return None
            _gap_val = float(lv.get("gap", 0))
            _rsi_val = float(lv.get("rsi", 0))
            _bw_val  = float(lv.get("bw", 0))
            _price   = float(lv.get("price", 0))
            # Reconstruct ema9_high/low from gap+bw so _build_signal's gate math works
            _ema9h = round(_price - _gap_val, 2) if _price > 0 else 0
            _ema9l = round(_ema9h - _bw_val, 2) if _ema9h > 0 else 0
            return {
                "close": _price, "entry_price": _price,
                "ema9_high": _ema9h, "ema9_low": _ema9l,
                "rsi": _rsi_val, "rsi_prev": _rsi_val - (1 if lv.get("rsi_rising") else -1),
                "candle_green": True, "body_pct": 0,
                "fired": bool(lv.get("ready")),
                "reject_reason": lv.get("reject", ""),
                "g4_other_falling": True,  # xleg checked in the real path
                "entry_mode": "V10_P1",
            }
        ce_signal = _build_signal("CE", _v10_to_result("CE"))
        pe_signal = _build_signal("PE", _v10_to_result("PE"))

        try:
            _tokens = D.get_option_tokens(None, atm_strike, expiry)
            for _sig, _side in [(ce_signal, "CE"), (pe_signal, "PE")]:
                # Always sync ltp + close from the same live tick (no drift)
                _live_tok = (_locked_tokens or {}).get(_side, _tokens.get(_side, {}))
                _ltp = D.get_ltp(_live_tok.get("token", 0)) if _live_tok else 0
                if _ltp and _ltp > 0:
                    _sig["ltp"] = round(_ltp, 2)
                    if D.is_market_open():
                        _sig["close"] = round(_ltp, 2)
                elif _sig.get("ltp", 0) == 0 and _side in _tokens:
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

        position = {}
        if st_v10.get("in_trade"):
            opt_ltp = D.get_ltp(st_v10.get("token", 0))
            entry = st_v10.get("entry_price", 0)
            running = round(opt_ltp - entry, 1) if opt_ltp > 0 else 0

            _ratchet_sl = float(st_v10.get("active_ratchet_sl", 0) or 0)
            if _ratchet_sl > 0:
                _stop_price = round(_ratchet_sl, 2)
                _stop_type = "RATCHET_" + str(st_v10.get("active_ratchet_tier", ""))
            else:
                _stop_price = round(entry - 12, 2)
                _stop_type = "INITIAL_SL"

            position = {
                "in_trade": True,
                "symbol": st_v10.get("symbol", ""),
                "direction": st_v10.get("direction", ""),
                "entry": entry,
                "entry_time": st_v10.get("entry_time", ""),
                "ltp": round(opt_ltp, 2) if opt_ltp > 0 else 0,
                "pnl": running,
                "peak": round(st_v10.get("peak_pnl", 0), 1),
                "candles": st_v10.get("candles_held", 0),
                "strike": st_v10.get("strike", 0),
                "sl": _stop_price,
                "active_ratchet_tier": st_v10.get("active_ratchet_tier", ""),
                "lot_size": CFG.lot_size(),
                "lot1_active": st_v10.get("lot1_active", True),
                "lot2_active": st_v10.get("lot2_active", True),
                "lots_split": st_v10.get("lots_split", False),
                "current_floor": round(float(st_v10.get("current_floor", 0) or 0), 2),
                "current_rsi": round(float(
                    (_v10_live.get(st_v10.get("direction", ""), {}) or {}).get("rsi", 0) or 0
                ), 1),
            }
        else:
            position = {"in_trade": False}

        _today_trades = _read_today_trades()
        _today_pnl_pts = 0.0
        _today_pnl_rs = 0.0
        _today_wins = 0
        _today_losses = 0
        for _tt in _today_trades:
            try:
                _p = float(_tt.get("pnl_pts", 0))
                _r = float(_tt.get("net_pnl_rs", 0) or _tt.get("pnl_rs", 0))
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

        try:
            rolling_block = _compute_rolling_stats(20)
        except Exception:
            rolling_block = {"last10_wr": 0, "last20_wr": 0, "last10_pts": 0, "streak": 0}

        straddle_block = {
            "open": round(straddle_open, 1) if straddle_captured else 0,
            "captured": straddle_captured,
        }

        dashboard = {
            "ts": now.strftime("%Y-%m-%d %H:%M:%S"),
            "version": D.VERSION,
            "mode": "PAPER" if D.PAPER_MODE else "LIVE",
            "market": {
                "spot": round(spot_ltp, 1),
                "atm": atm_strike,
                "locked_ce": _locked_ce_strike,
                "locked_pe": _locked_pe_strike,
                "dte": dte,
                "vix": round(vix_ltp, 1),
                "session": session,
                "regime": spot_3m.get("regime", ""),
                "bias": bias,
                "vwap": round(float(LEVELS._vwap_state.get("vwap", 0.0)), 2),
                "gap": round(float(LEVELS._vwap_state.get("gap", 0.0)), 1),
                "spot_ema9": spot_3m.get("ema9", 0),
                "spot_ema21": spot_3m.get("ema21", 0),
                "spot_spread": spot_3m.get("spread", 0),
                "spot_rsi": spot_3m.get("rsi", 0),
                "spot_adx_3m": round(float(spot_3m.get("adx", 0)), 1),
                "hourly_rsi": round(hourly_rsi, 1),
                "expiry": expiry.isoformat() if expiry else "",
                "market_open": D.is_market_open(),
                "indicators_warm": _is_warm,
            },
            "ce": ce_signal,
            "pe": pe_signal,
            "position": position,
            "today": today_block,
            "straddle": straddle_block,
            "account": {
                "name": D.get_account_info().get("name", ""),
                "balance": D.get_account_info().get("total_balance", 0),
                "used": D.get_account_info().get("used_margin", 0),
            },
            "rolling": rolling_block,
            "cooldown": {},
        }

        tmp = os.path.join(D.STATE_DIR, 'vrl_dashboard.json') + ".tmp"
        with open(tmp, "w") as f:
            json.dump(dashboard, f, indent=2, default=str)
        os.replace(tmp, os.path.join(D.STATE_DIR, 'vrl_dashboard.json'))

    except Exception as e:
        logger.debug("[DASH] Snapshot write: " + str(e))


def _strategy_loop(kite):
    global _running, _last_health_log_ts
    today_str = date.today().isoformat()
    logger.info("[MAIN] Strategy loop started")
    os.makedirs(os.path.expanduser("~/state"), exist_ok=True)
    _cleanup_trade_log()
    try:
        D.compute_daily_bias(kite)
        logger.info("[MAIN] Daily bias: " + str(D.get_daily_bias()))
    except Exception as _be:
        logger.debug("[MAIN] Bias: " + str(_be))
    try:
        D.check_hourly_rsi(kite)
        logger.info("[MAIN] Hourly RSI: " + str(D.get_hourly_rsi()))
    except Exception as _he:
        logger.debug("[MAIN] H.RSI: " + str(_he))
    with _state_lock:
        state["_last_1min_candle"] = ""

    try:
        _now_startup = datetime.now()
        _startup_mins = _now_startup.hour * 60 + _now_startup.minute
        # Only check gap at true market open window (09:00–09:34).
        # Mid-day restarts must NOT compare prev_close to current intraday spot.
        if 540 <= _startup_mins < 574:
            _startup_spot = D.get_ltp(D.NIFTY_SPOT_TOKEN)
            _prev_close = state.get("prev_close", 0)
            if _prev_close > 0 and _startup_spot > 0:
                _gap = abs(_startup_spot - _prev_close)
                _gap_threshold = CFG.get().get("strike", {}).get("gap_relock_threshold", 200)
                if _gap > _gap_threshold:
                    logger.info("[MAIN] GAP " + str(round(_gap)) + "pts — forcing strike relock at open")
                    _tg_send("🔔 <b>GAP OPEN</b> " + str(round(_gap)) + "pts — strikes will relock")
                    _reset_strike_lock()
        else:
            logger.info("[MAIN] Gap-open check skipped (mid-day restart at "
                        + _now_startup.strftime("%H:%M") + ")")
    except Exception:
        pass

    expiry = D.get_nearest_expiry(kite)

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

    _last_health_log_ts = time.time()   # startup health already logged; first loop re-log in 30 min
    while _running:
        # ── intraday Token-health refresh (every 30 min; startup check was one-shot) ──
        try:
            _hb_now = time.time()
            if _hb_now - _last_health_log_ts >= 1800:
                _last_health_log_ts = _hb_now
                _hb_spot = D.get_ltp(D.NIFTY_SPOT_TOKEN)
                with D._tick_lock:
                    _hb_e = D._ticks.get(int(D.NIFTY_SPOT_TOKEN))
                if D.is_market_open() and _hb_spot > 0:
                    _hb_ws = "WS: ✅ tick=" + str(round(_hb_spot, 1))
                elif _hb_e:
                    _hb_ws = ("WS: 💤 market closed (last tick "
                              + str(int((_hb_now - _hb_e["ts"]) / 60)) + "m ago at "
                              + str(round(_hb_e["ltp"], 1)) + ")")
                else:
                    _hb_ws = "WS: 💤 no ticks yet"
                logger.info("[MAIN] Token health: Token: ✅ | Spot: "
                            + (("✅ " + str(round(_hb_spot, 1))) if _hb_spot > 0 else "⚠️ —")
                            + " | " + _hb_ws)
        except Exception:
            pass
        try:
            now   = datetime.now()
            today = date.today()

            try:
                import time as _t_obs
                _now_epoch = _t_obs.time()
                _expired = []
                with _post_exit_lock:
                    _kept = []
                    for tok, expire_at in _post_exit_observation:
                        if _now_epoch >= expire_at:
                            _expired.append(tok)
                        else:
                            _kept.append((tok, expire_at))
                    _post_exit_observation[:] = _kept
                if _expired:
                    with _state_lock:
                        _active_token = state.get("token") or 0
                    _safe_to_drop = [t for t in _expired if t != _active_token]
                    try:
                        _lock_set = set()
                        if _locked_tokens:
                            for _v in _locked_tokens.values():
                                if isinstance(_v, dict):
                                    _tk = _v.get("token")
                                    if _tk:
                                        _lock_set.add(int(_tk))
                        with _state_lock:
                            for k in ("_locked_ce_token", "_locked_pe_token",
                                      "_locked_ce_token_2", "_locked_pe_token_2"):
                                _t = state.get(k)
                                if _t:
                                    _lock_set.add(int(_t))
                        _safe_to_drop = [t for t in _safe_to_drop if t not in _lock_set]
                    except Exception:
                        pass
                    if _safe_to_drop:
                        D.unsubscribe_tokens(_safe_to_drop)
                        logger.info(
                            "[POST_EXIT] Unsubscribed after observation: "
                            + str(_safe_to_drop)
                        )
            except Exception as _pe_err:
                logger.debug("[POST_EXIT] Cleanup error: " + str(_pe_err))

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
            # Keep _v8_state expiry/dte in sync — entry/exit functions read from here
            try:
                with _v8_lock:
                    _v8_state["expiry"] = expiry.isoformat() if expiry else ""
                    _v8_state["dte"]    = dte
            except Exception:
                pass
            profile = {"conv_sl_pts": 12}
            session = D.get_session_block(now.hour, now.minute)
            spot_ltp = D.get_ltp(D.NIFTY_SPOT_TOKEN)

            D.check_and_reconnect()

            # ── V8 tick-based exit: runs every 1-second scan cycle ──
            # Must be OUTSIDE the _is_new_1min_candle gate — exits need to
            # fire on every tick, not once per minute at candle close.
            _v8_check_exit()

            # ── V8 entry: scan every 10 seconds (outside 1-min gate) ──
            # BUG-16 fix: entry was gated to once-per-minute at :35s.
            # If candle turned green at :40s, bot missed it until next minute.
            # Now checks every 10s — same_candle_guard prevents double-entry.
            global _v8_last_entry_scan_ts
            _v8_force_exit_age = time.time() - float(_v8_state.get("_force_exit_ts", 0) or 0)
            _v8_in_force_cooldown = (_v8_force_exit_age < 180 and float(_v8_state.get("_force_exit_ts", 0) or 0) > 0)
            if (_v8_in_force_cooldown
                    and not _v8_state.get("in_trade")
                    and time.time() - _v8_last_entry_scan_ts >= 3):
                _v8_last_entry_scan_ts = time.time()
                logger.info(f"[REJECT-V8] force_exit_cooldown age={int(_v8_force_exit_age)}s — entries blocked 3 min after manual exit")
            # (legacy entry scan removed — V10 P1/P2 drives all entries now)


            # ── V2 EXIT + DYNAMIC TRAIL (A/B comparison — no real trades) ──
            # P1-V2: standard ladder below peak 15, then entry+15→+1/5s ratchet, hard exit +40
            # P2-V2: same ratchet as P1-V2 — standard ladder below peak 15, entry+15→+1/5s, hard exit +40
            # NOTE: uses is_market_open() so EOD exits at 15:15 fire correctly
            global _v8_shadow_dt_v2, _v8_shadow_p2_v2
            if D.is_market_open():
                # P1 V2
                for _v2_dir in ("CE", "PE"):
                    _v2d = _v8_shadow_dt_v2[_v2_dir]
                    if not _v2d.get("active"):
                        continue
                    _v2_tok   = int(_v2d.get("entry_tok", 0) or 0)
                    _v2_entry = float(_v2d.get("entry_price", 0))
                    _v2_sl    = float(_v2d.get("shadow_sl", round(_v2_entry - 12, 1)))
                    _v2_ltp   = D.get_ltp(_v2_tok) if _v2_tok else 0
                    if not _v2_ltp:
                        continue
                    # Update peak
                    _v2_pk_px  = max(float(_v2d.get("peak_price", _v2_entry)), _v2_ltp)
                    _v2_pk_pts = round(_v2_pk_px - _v2_entry, 1)
                    _v2d["peak_price"] = _v2_pk_px
                    _v2d["peak_pts"]   = _v2_pk_pts
                    # Check exits
                    _v2_reason = _v2_exit_px = None
                    if _v2_ltp >= _v2_entry + 40:
                        _v2_reason, _v2_exit_px = "TARGET+40", round(_v2_entry + 40, 1)
                    elif now.time() >= dtime(15, 15):
                        _v2_reason, _v2_exit_px = "EOD", (_v2_ltp if _v2_ltp > 0 else _v2_entry)
                    elif _v2_ltp <= _v2_sl:
                        _v2_reason, _v2_exit_px = "SL-HIT", _v2_sl
                    if _v2_reason:
                        _v2_pnl = round(_v2_exit_px - _v2_entry, 1)
                        logger.info(f"[SHADOW-P1-V2] {_v2_dir} {_v2_reason} "
                                    f"entry={_v2_entry} exit={_v2_exit_px:.1f} "
                                    f"pnl={_v2_pnl:+.1f} peak=+{_v2_pk_pts:.1f}")
                        _v2d.update({"active": False, "entry_price": 0.0, "entry_time": "",
                                     "peak_price": 0.0, "peak_pts": 0.0, "shadow_sl": 0.0})
                        _save_shadow_state()
                        continue
                    # Trail: ratchet after peak ≥ 15 — first tick locks entry+15, then +1 every 5s
                    # Tick 1 (SL < entry+15): jump to entry+15 (no +1, avoids immediate exit)
                    # Tick 2+: SL += 1 each 5s tick
                    if _v2_pk_pts >= 15 and time.time() - _v2d.get("dyn_trail_ts", 0) >= 5:
                        _v2d["dyn_trail_ts"] = time.time()
                        if _v2_sl < _v2_entry + 15:
                            _v2_new_sl = round(_v2_entry + 15, 1)
                        else:
                            _v2_new_sl = round(_v2_sl + 1, 1)
                        if _v2_new_sl > _v2_sl:
                            _v2d["shadow_sl"] = _v2_new_sl
                            logger.info(f"[SHADOW-P1-V2] {_v2_dir} dyn_trail "
                                        f"sl={_v2_new_sl:.1f} ltp={_v2_ltp:.1f} "
                                        f"peak=+{_v2_pk_pts:.1f}")
                    elif _v2_pk_pts < 15:
                        _v2_std_sl, _ = _shadow_trail_sl(_v2_entry, _v2_pk_pts)
                        if _v2_std_sl > _v2_sl:
                            _v2d["shadow_sl"] = _v2_std_sl
                # P2 V2 — same ratchet as P1-V2: standard ladder below peak 15,
                # then entry+15 on first tick, +1 every 5s after. Hard exit TARGET+40.
                for _v2p2_dir in ("CE", "PE"):
                    _v2p2d = _v8_shadow_p2_v2[_v2p2_dir]
                    if not _v2p2d.get("active"):
                        continue
                    _v2p2_tok   = int(_v2p2d.get("entry_tok", 0) or 0)
                    _v2p2_entry = float(_v2p2d.get("entry_price", 0))
                    _v2p2_sl    = float(_v2p2d.get("shadow_sl", round(_v2p2_entry - 12, 1)))
                    _v2p2_ltp   = D.get_ltp(_v2p2_tok) if _v2p2_tok else 0
                    if not _v2p2_ltp:
                        continue
                    # Update peak
                    _v2p2_pk_px  = max(float(_v2p2d.get("peak_price", _v2p2_entry)), _v2p2_ltp)
                    _v2p2_pk_pts = round(_v2p2_pk_px - _v2p2_entry, 1)
                    _v2p2d["peak_price"] = _v2p2_pk_px
                    _v2p2d["peak_pts"]   = _v2p2_pk_pts
                    # Check exits
                    _v2p2_reason = _v2p2_exit_px = None
                    if _v2p2_ltp >= _v2p2_entry + 40:
                        _v2p2_reason, _v2p2_exit_px = "TARGET+40", round(_v2p2_entry + 40, 1)
                    elif now.time() >= dtime(15, 15):
                        _v2p2_reason, _v2p2_exit_px = "EOD", (_v2p2_ltp if _v2p2_ltp > 0 else _v2p2_entry)
                    elif _v2p2_ltp <= _v2p2_sl:
                        _v2p2_reason, _v2p2_exit_px = "SL-HIT", _v2p2_sl
                    if _v2p2_reason:
                        _v2p2_pnl = round(_v2p2_exit_px - _v2p2_entry, 1)
                        logger.info(f"[SHADOW-P2-V2] {_v2p2_dir} {_v2p2_reason} "
                                    f"entry={_v2p2_entry} exit={_v2p2_exit_px:.1f} "
                                    f"pnl={_v2p2_pnl:+.1f} peak=+{_v2p2_pk_pts:.1f}")
                        _v2p2d.update({"active": False, "entry_price": 0.0, "entry_time": "",
                                       "peak_price": 0.0, "peak_pts": 0.0, "shadow_sl": 0.0,
                                       "dyn_trail_ts": 0.0})
                        _save_shadow_state()
                        continue
                    # Ratchet: peak ≥ 15 → first tick locks entry+15, then +1 every 5s
                    if _v2p2_pk_pts >= 15 and time.time() - _v2p2d.get("dyn_trail_ts", 0) >= 5:
                        _v2p2d["dyn_trail_ts"] = time.time()
                        if _v2p2_sl < _v2p2_entry + 15:
                            _v2p2_new_sl = round(_v2p2_entry + 15, 1)
                        else:
                            _v2p2_new_sl = round(_v2p2_sl + 1, 1)
                        if _v2p2_new_sl > _v2p2_sl:
                            _v2p2d["shadow_sl"] = _v2p2_new_sl
                            logger.info(f"[SHADOW-P2-V2] {_v2p2_dir} dyn_trail "
                                        f"sl={_v2p2_new_sl:.1f} ltp={_v2p2_ltp:.1f} "
                                        f"peak=+{_v2p2_pk_pts:.1f}")
                    elif _v2p2_pk_pts < 15:
                        _v2p2_std_sl, _ = _shadow_trail_sl(_v2p2_entry, _v2p2_pk_pts)
                        if _v2p2_std_sl > _v2p2_sl:
                            _v2p2d["shadow_sl"] = _v2p2_std_sl

            # ── BW-SCAN: log EMA9 band data every new 1-min candle ──
            # Fires once per minute (second >= 35). Shows 1m + 3m band for CE + PE.
            # Positions: ABOVE (close > ema9h) | INSIDE (ema9l <= close <= ema9h) | BELOW (close < ema9l)
            global _bw_scan_last_bucket
            _bw_now_key = now.strftime("%Y%m%d%H%M")
            if (D.is_trading_window(now) and _locked_tokens
                    and _bw_scan_last_bucket != _bw_now_key and now.second >= 35):
                _bw_scan_last_bucket = _bw_now_key
                try:
                    _bw_parts = []
                    for _bw_dir, _bw_info in [("CE", (_locked_tokens or {}).get("CE", {})),
                                               ("PE", (_locked_tokens or {}).get("PE", {}))]:
                        _bw_tok = int(_bw_info.get("token", 0) or 0)
                        if not _bw_tok:
                            continue
                        _bw_1m = D.get_option_1min(_bw_tok, 10)
                        _bw_3m = D.get_option_3min(_bw_tok, lookback=5)
                        _1m_str = _3m_str = "n/a"
                        if _bw_1m is not None and len(_bw_1m) >= 2:
                            _bwr = _bw_1m.iloc[-2]
                            _bwc  = float(_bwr["close"])
                            _bwel = float(_bwr.get("ema9_low", 0))
                            _bweh = float(_bwr.get("ema9_high", 0))
                            _bwbw = round(_bweh - _bwel, 1) if _bweh and _bwel else 0
                            _bwgp = round(_bwc - _bweh, 1) if _bweh else 0
                            _bwpos = "ABOVE" if _bwc > _bweh else ("INSIDE" if _bwc >= _bwel else "BELOW")
                            _1m_str = (f"c={_bwc:.1f} el={_bwel:.1f} eh={_bweh:.1f} "
                                       f"bw={_bwbw:.1f} gap={_bwgp:+.1f} [{_bwpos}]")
                        if _bw_3m is not None and len(_bw_3m) >= 2:
                            _bwr3 = _bw_3m.iloc[-2]
                            _bwc3  = float(_bwr3["close"])
                            _bwel3 = float(_bwr3.get("ema9_low", 0))
                            _bweh3 = float(_bwr3.get("ema9_high", 0))
                            _bwbw3 = round(_bweh3 - _bwel3, 1) if _bweh3 and _bwel3 else 0
                            _bwgp3 = round(_bwc3 - _bweh3, 1) if _bweh3 else 0
                            _bwpos3 = "ABOVE" if _bwc3 > _bweh3 else ("INSIDE" if _bwc3 >= _bwel3 else "BELOW")
                            _3m_str = (f"c={_bwc3:.1f} el={_bwel3:.1f} eh={_bweh3:.1f} "
                                       f"bw={_bwbw3:.1f} gap={_bwgp3:+.1f} [{_bwpos3}]")
                        _bw_parts.append(f"  {_bw_dir}  1m: {_1m_str}  ||  3m: {_3m_str}")
                    if _bw_parts:
                        logger.info(f"[BW-SCAN] {now.strftime('%H:%M')}\n" + "\n".join(_bw_parts))
                except Exception as _bwe:
                    logger.debug(f"[BW-SCAN] error: {_bwe}")

            # ── SHADOW: 1-min entry tracker (data collection, NO live trades) ──
            # Signal: 1-min close > EMA9_high + RSI 48-70 rising + close > 1-min VWAP
            # Both CE and PE tracked independently. Bucket = 1-min candle ts.
            global _v8_shadow_dt, _v8_shadow_p2

            # ── EOD/SL safety: close active signals even if _locked_tokens not yet set ──
            # Handles late restart case where strike lock hasn't happened yet
            # NOTE: uses is_market_open() (not is_trading_window) so EOD exit at 15:15 fires
            if D.is_market_open():
                for _sd_early, _sd_label_e in [(_v8_shadow_dt, "P1"), (_v8_shadow_p2, "P2")]:
                    for _sdir_e in ("CE", "PE"):
                        _sds_e = _sd_early[_sdir_e]
                        if not _sds_e.get("active"):
                            continue
                        _stok_e   = int(_sds_e.get("entry_tok", 0) or 0)
                        _sep_e    = float(_sds_e.get("entry_price", 0) or 0)
                        _ssl_e    = float(_sds_e.get("shadow_sl", round(_sep_e - 12, 1)) or round(_sep_e - 12, 1))
                        _sltp_e   = D.get_ltp(_stok_e) if _stok_e else 0
                        _speak_e  = float(_sds_e.get("peak_pts", 0) or 0)
                        _slvl_e   = _sds_e.get("shadow_level", "INITIAL")
                        _close_e  = None
                        if now.time() >= dtime(15, 15):
                            _close_e = ("EOD", _sltp_e if _sltp_e > 0 else _sep_e)
                        elif _sltp_e > 0 and _sltp_e <= _ssl_e:
                            _close_e = ("SL-HIT", _ssl_e)
                        if _close_e:
                            _reason_e, _exit_e = _close_e
                            _pnl_e = round(_exit_e - _sep_e, 1)
                            _icon_e = "✅" if _pnl_e >= 20 else ("🟡" if _pnl_e > 0 else "❌")
                            logger.info(f"[SHADOW-{_sd_label_e}] {_sdir_e} {_reason_e} "
                                        f"entry={_sep_e} exit={_exit_e:.1f} "
                                        f"pnl={_pnl_e:+.1f} peak=+{_speak_e:.1f} trail={_slvl_e}")
                            pass  # shadow TG alert removed (v10 live alerts only)
                            _upd_e = {
                                "active": False, "entry_price": 0.0, "entry_time": "",
                                "peak_price": 0.0, "peak_pts": 0.0,
                                "shadow_sl": 0.0, "shadow_level": "INITIAL",
                                "last_exit_pnl": _pnl_e, "last_exit_reason": _reason_e,
                                "last_exit_ts": time.time(),
                            }
                            # BUG-A fix: set sl_ts on P1 SL-HIT so cooldown blocks re-entry 60s
                            if _reason_e == "SL-HIT" and _sd_label_e == "P1":
                                _upd_e["sl_ts"] = time.time()
                            # P2 exit cooldown: set exit_ts on ANY P2 exit (SL-HIT, trail, EOD)
                            # Blocks P2 re-entry in same direction for 120s
                            if _sd_label_e == "P2":
                                _upd_e["exit_ts"] = time.time()
                            _sds_e.update(_upd_e)
                            _save_shadow_state()

            # ── ALWAYS update CE/PE live price for dashboard (even during a trade) ──
            if _locked_tokens and now.second % 5 == 0:
                for _lp_dir, _lp_info in [("CE", (_locked_tokens or {}).get("CE", {})),
                                           ("PE", (_locked_tokens or {}).get("PE", {}))]:
                    _lp_tok = int(_lp_info.get("token", 0) or 0)
                    if _lp_tok:
                        _lp_px = D.get_ltp(_lp_tok)
                        if _lp_px and _lp_dir in _v10_live:
                            with _v10_live_lock:
                                _v10_live[_lp_dir]["price"] = round(_lp_px, 1)
                if now.second % 15 == 0:
                    _save_shadow_state()

            if (not _v8_state.get("in_trade")
                    and D.is_trading_window(now)
                    and _locked_tokens
                    and time.time() - _v8_shadow_dt["last_scan_ts"] >= 3):
                _v8_shadow_dt["last_scan_ts"] = time.time()
                try:
                    for _sh_dir, _sh_info in [("CE", (_locked_tokens or {}).get("CE", {})),
                                               ("PE", (_locked_tokens or {}).get("PE", {}))]:
                        _sh_tok = int(_sh_info.get("token", 0) or 0)
                        if not _sh_tok:
                            continue

                        # ── 1-min PRIMARY: last completed 1-min candle ──
                        _sh_1m = get_option_1min(_sh_tok, 100)   # full session for VWAP (local, uses WS cache)
                        if _sh_1m is None or len(_sh_1m) < 4:
                            continue
                        _sh_1m_comp   = _sh_1m.iloc[-2]   # last completed 1-min candle
                        _sh_1m_bk_ts  = str(_sh_1m_comp.name)
                        _sh_1m_close  = float(_sh_1m_comp["close"])
                        _sh_1m_open   = float(_sh_1m_comp["open"])
                        _sh_ema9h_1m  = float(_sh_1m_comp.get("ema9_high", 0))
                        _sh_ema9l_1m  = float(_sh_1m_comp.get("ema9_low", 0))
                        _sh_rsi_1m    = float(_sh_1m_comp.get("RSI", 0) or 0)
                        _sh_rsi_1m_p  = float(_sh_1m.iloc[-3].get("RSI", 0) or 0)
                        _sh_ema9l_1m_prev = float(_sh_1m.iloc[-3].get("ema9_low", 0))

                        # 1-min session VWAP (cumulative from 9:15, resets daily)
                        _sh_1m_day = _sh_1m[_sh_1m.index.date == now.date()].copy()
                        if len(_sh_1m_day) < 3:
                            continue
                        _sh_1m_day["_typ"] = (_sh_1m_day["high"] + _sh_1m_day["low"] + _sh_1m_day["close"]) / 3.0
                        _sh_1m_day["_tv"]  = _sh_1m_day["_typ"] * _sh_1m_day["volume"]
                        _sh_cum_vol = _sh_1m_day["volume"].cumsum().replace(0, np.nan)
                        _sh_1m_vwap = float((_sh_1m_day["_tv"].cumsum() / _sh_cum_vol).iloc[-2])

                        _sh_ds = _v8_shadow_dt[_sh_dir]  # per-direction state

                        # Bucket change: update bucket_ts only — DO NOT reset active signal
                        # Signal tracks until SL hit or EOD, independent of candle boundaries
                        if _sh_ds["bucket_ts"] != _sh_1m_bk_ts:
                            _sh_ds["bucket_ts"] = _sh_1m_bk_ts

                        # Always update dashboard snapshot regardless of signal active state.
                        # Without this, _v10_live only gets a price-only update while active,
                        # leaving gap=None → _v10_to_result returns None → dashboard shows
                        # "WARMING UP" for the entire duration of the shadow signal.
                        _sh_1m_gap = round(_sh_1m_close - _sh_ema9h_1m, 2)
                        with _v10_live_lock:
                            _v10_live[_sh_dir] = {
                                "strike": int(_sh_info.get("strike", 0) or 0),
                                "price": round((D.get_ltp(_sh_tok) or _sh_1m_close), 1),
                                "gap": round(_sh_1m_gap, 2), "gap_ok": _sh_1m_gap >= V10_MIN_EMA9H_GAP,
                                "rsi": round(_sh_rsi_1m, 1), "rsi_rising": _sh_rsi_1m > _sh_rsi_1m_p,
                                "rsi_ok": (V10_RSI_MIN < _sh_rsi_1m < V10_RSI_MAX) and (_sh_rsi_1m > _sh_rsi_1m_p),
                                "bw": round(_sh_ema9h_1m - _sh_ema9l_1m, 1), "bw_ok": (_sh_ema9h_1m - _sh_ema9l_1m) >= V10_BW_MIN,
                                "ready": False, "reject": "in_trade" if _sh_ds.get("active") else "",
                            }

                        # If signal active — track LTP using ORIGINAL token (not current ATM)
                        if _sh_ds["active"]:
                            _sh_track_tok = int(_sh_ds.get("entry_tok", 0) or _sh_tok)
                            _sh_ltp_pk  = D.get_ltp(_sh_track_tok)
                            if not _sh_ltp_pk:
                                continue   # LTP unavailable, skip this cycle
                            _sh_cur_sl  = _sh_ds.get("shadow_sl", round(_sh_ds["entry_price"] - 12, 1))
                            _sh_entry   = _sh_ds["entry_price"]

                            def _sh_close_signal(reason, exit_px):
                                _fin_peak = _sh_ds["peak_pts"]
                                _fin_lvl  = _sh_ds.get("shadow_level", "INITIAL")
                                _fin_pnl  = round(exit_px - _sh_entry, 1)
                                _fin_icon = "✅" if _fin_pnl >= 20 else ("🟡" if _fin_pnl > 0 else "❌")
                                _fin_msg  = (
                                    f"🔵 SHADOW P1 {_sh_dir} — {reason}\n"
                                    f"Entry: {_sh_entry:.1f}  Exit: {exit_px:.1f}\n"
                                    f"PnL: {_fin_icon} {_fin_pnl:+.1f}  Peak: +{_fin_peak:.1f}\n"
                                    f"Trail reached: {_fin_lvl}\n"
                                )
                                _p2_e2 = _v8_shadow_p2[_sh_dir].get("today_entry", 0.0)
                                _p2_d2 = _v8_shadow_p2[_sh_dir].get("today_date", "")
                                if _p2_e2 > 0 and _p2_d2 == str(now.date()):
                                    _sv2 = round(_sh_entry - _p2_e2, 1)
                                    _fin_msg += f"P2 at {_p2_e2:.1f} → P1 saved {_sv2:+.1f}pts\n"
                                if _sh_ds["live_entry"] > 0:
                                    _sv3 = round(_sh_ds["live_entry"] - _sh_entry, 1)
                                    _fin_msg += f"vs V10: {_sh_ds['live_entry']:.1f}  diff={_sv3:+.1f}pts\n"
                                _fin_msg += f"<i>⚠️ Shadow only</i>"
                                logger.info(
                                    f"[SHADOW-P1] {_sh_dir} {reason} "
                                    f"entry={_sh_entry} exit={exit_px:.1f} "
                                    f"pnl={_fin_pnl:+.1f} peak=+{_fin_peak:.1f} trail={_fin_lvl}"
                                )
                                # BW+Gap study log
                                try:
                                    import csv as _csv_s1, os as _os_s1
                                    _sp1 = _os_s1.path.join(_os_s1.path.dirname(__file__), "state", "bw_gap_study.csv")
                                    _sp1_new = not _os_s1.path.isfile(_sp1)
                                    with open(_sp1, "a", newline="") as _sf1:
                                        _sw1 = _csv_s1.writer(_sf1)
                                        if _sp1_new:
                                            _sw1.writerow(["date", "entry_time", "path", "direction",
                                                           "bw", "gap", "rsi", "entry_price",
                                                           "peak_pts", "trail_level", "pnl_pts", "exit_reason"])
                                        _sw1.writerow([
                                            date.today().isoformat(),
                                            _sh_ds.get("entry_time", ""),
                                            "P1", _sh_dir,
                                            _sh_ds.get("study_bw", 0),
                                            _sh_ds.get("study_gap", 0),
                                            _sh_ds.get("study_rsi", 0),
                                            round(_sh_entry, 1),
                                            round(_fin_peak, 1),
                                            _fin_lvl,
                                            _fin_pnl,
                                            reason,
                                        ])
                                except Exception as _sel1:
                                    logger.warning(f"[STUDY] P1 log error: {_sel1}")
                                pass  # shadow TG P1 exit alert removed
                                # Track peak for analysis streak detection
                                _shadow_analysis[_sh_dir]["last_peaks"].append(_fin_peak)
                                _shadow_analysis[_sh_dir]["last_peaks"] = \
                                    _shadow_analysis[_sh_dir]["last_peaks"][-2:]
                                _sh_ds.update({
                                    "active": False, "entry_price": 0.0, "entry_time": "",
                                    "peak_price": 0.0, "peak_pts": 0.0, "live_entry": 0.0,
                                    "shadow_sl": 0.0, "shadow_level": "INITIAL",
                                    "bucket_ts": _sh_1m_bk_ts,  # block re-fire on same candle
                                    "sl_ts": time.time() if reason == "SL-HIT" else _sh_ds.get("sl_ts", 0.0),
                                    "last_exit_pnl": _fin_pnl, "last_exit_reason": reason,
                                    "last_exit_ts": time.time(),
                                })
                                _save_shadow_state()

                            # EOD check
                            if now.time() >= dtime(15, 15):
                                _sh_close_signal("EOD", _sh_ltp_pk)
                                continue

                            # SL hit check (LTP touches or goes below trail SL)
                            if _sh_ltp_pk <= _sh_cur_sl:
                                _sh_close_signal("SL-HIT", _sh_cur_sl)
                                continue

                            # Update peak + trail ladder
                            if _sh_ltp_pk > _sh_ds["peak_price"]:
                                _sh_ds["peak_price"] = _sh_ltp_pk
                                _sh_ds["peak_pts"]   = round(_sh_ltp_pk - _sh_entry, 1)
                                _new_sl, _new_lvl = _shadow_trail_sl(_sh_entry, _sh_ds["peak_pts"])
                                _old_lvl = _sh_ds.get("shadow_level", "INITIAL")
                                if _new_lvl != _old_lvl:
                                    _sh_ds["shadow_sl"]    = _new_sl
                                    _sh_ds["shadow_level"] = _new_lvl
                                    logger.info(
                                        f"[SHADOW-P1] {_sh_dir} trail ↑ {_new_lvl} "
                                        f"peak=+{_sh_ds['peak_pts']:.1f} sl_now={_new_sl:.1f}"
                                    )
                                    pass  # shadow TG trail alert removed
                                    _save_shadow_state()
                            continue

                        # ── 1-min: close > EMA9_high + RSI filter + above VWAP ──
                        _sh_1m_gap     = round(_sh_1m_close - _sh_ema9h_1m, 2)
                        _sh_vwap_gap   = round(_sh_1m_close - _sh_1m_vwap, 2)
                        _sh_1m_reject  = None
                        if not (_sh_ema9h_1m > 0 and _sh_1m_close > _sh_ema9h_1m):
                            _sh_1m_reject = f"1m_below_ema9h close={_sh_1m_close} ema9h={_sh_ema9h_1m} gap={_sh_1m_gap}"
                        elif _sh_1m_gap < V10_MIN_EMA9H_GAP:
                            _sh_1m_reject = f"1m_ema9h_gap_weak gap={_sh_1m_gap:.2f}(need>={V10_MIN_EMA9H_GAP})"
                        elif not (_sh_rsi_1m > _sh_rsi_1m_p):
                            _sh_1m_reject = f"1m_rsi_falling rsi={_sh_rsi_1m:.1f} prev={_sh_rsi_1m_p:.1f}"
                        elif not (V10_RSI_MIN < _sh_rsi_1m < V10_RSI_MAX):
                            _sh_1m_reject = f"1m_rsi_outofrange rsi={_sh_rsi_1m:.1f}(need {V10_RSI_MIN}-{V10_RSI_MAX})"
                        elif (_sh_ema9h_1m - _sh_ema9l_1m) < V10_BW_MIN:
                            _sh_1m_reject = f"1m_bw_weak bw={round(_sh_ema9h_1m-_sh_ema9l_1m,1)}(need>={V10_BW_MIN})"
                        elif not (_sh_1m_close > _sh_1m_vwap):
                            _sh_1m_reject = f"1m_below_vwap close={_sh_1m_close:.1f} vwap={_sh_1m_vwap:.1f} gap={_sh_vwap_gap}"
                        # ── live gate snapshot for dashboard (per side, every scan) ──
                        with _v10_live_lock:
                            _v10_live[_sh_dir] = {
                                "strike": int(_sh_info.get("strike", 0) or 0),
                                "price": round((D.get_ltp(_sh_tok) or _sh_1m_close), 1),
                                "gap": round(_sh_1m_gap, 2), "gap_ok": _sh_1m_gap >= V10_MIN_EMA9H_GAP,
                                "rsi": round(_sh_rsi_1m, 1), "rsi_rising": _sh_rsi_1m > _sh_rsi_1m_p,
                                "rsi_ok": (V10_RSI_MIN < _sh_rsi_1m < V10_RSI_MAX) and (_sh_rsi_1m > _sh_rsi_1m_p),
                                "bw": round(_sh_ema9h_1m - _sh_ema9l_1m, 1), "bw_ok": (_sh_ema9h_1m - _sh_ema9l_1m) >= V10_BW_MIN,
                                "ready": (_sh_1m_reject is None), "reject": (_sh_1m_reject.split()[0] if _sh_1m_reject else ""),
                            }
                        if now.second % 15 == 0:
                            _save_shadow_state()   # persist live gate snapshot for dashboard monitor
                        if _sh_1m_reject:
                            if now.second % 15 == 0:
                                logger.info(f"[SHADOW-DTF] REJECT {_sh_dir} {_sh_1m_reject}")
                            # Detailed RSI-block log — once per candle bucket
                            if 'rsi' in _sh_1m_reject and _sh_ds.get("_rsi_block_ts") != _sh_1m_bk_ts:
                                _sh_ds["_rsi_block_ts"] = _sh_1m_bk_ts
                                _sh_bw_rb   = round(_sh_ema9h_1m - _sh_ema9l_1m, 1)
                                _sh_str_rb  = int(_sh_info.get("strike", 0) or 0)
                                logger.info(
                                    f"[RSI-SHADOW] {_sh_dir} {_sh_str_rb} BLOCKED "
                                    f"entry={_sh_1m_close:.1f} ema9h_gap={_sh_1m_gap:+.2f} bw={_sh_bw_rb} "
                                    f"vwap={_sh_1m_vwap:.1f} gap_vwap={_sh_vwap_gap:+.2f} "
                                    f"rsi={_sh_rsi_1m:.1f} reason={_sh_1m_reject.split()[0]}"
                                )
                            continue

                        # ── Hard gate: opening blackout 9:15–9:45 ──
                        if now.time() < V10_OPEN_BLACKOUT_END:
                            if now.second % 15 == 0:
                                logger.info(f"[SHADOW-P1] REJECT {_sh_dir} open_blackout "
                                            f"(no entries before {V10_OPEN_BLACKOUT_END})")
                            continue

                        _sh_sl_age = time.time() - _sh_ds.get("sl_ts", 0)
                        if 0 < _sh_sl_age < 60:
                            if now.second % 15 == 0:
                                logger.info(f"[SHADOW-P1] REJECT {_sh_dir} sl_cooldown age={int(_sh_sl_age)}s")
                            continue

                        # ── FIRE ──
                        # Gate: XLEG_CONFIRMED — cross-leg must be below EMA9H all last 5 scans
                        _xleg_g_dir  = "PE" if _sh_dir == "CE" else "CE"
                        _xleg_g_buf  = _shadow_analysis[_xleg_g_dir].get("cross_buf", [])
                        _xleg_g_buf5 = _xleg_g_buf[-5:]
                        _xleg_g_ok   = len(_xleg_g_buf5) >= 3 and all(not v for v in _xleg_g_buf5)
                        if not _xleg_g_ok:
                            logger.info(
                                f"[SHADOW-P1] REJECT {_sh_dir} xleg_not_confirmed "
                                f"{_xleg_g_dir} buf={_xleg_g_buf5} n={len(_xleg_g_buf5)}"
                            )
                            continue
                        _sh_ltp    = D.get_ltp(_sh_tok)
                        # Gate: LTP must still be above VWAP at fire time
                        # Candle close may have been above VWAP but LTP can slip below by signal time
                        if _sh_ltp and _sh_ltp < _sh_1m_vwap:
                            logger.info(
                                f"[SHADOW-P1] REJECT {_sh_dir} ltp_slipped_below_vwap "
                                f"ltp={_sh_ltp:.1f} vwap={_sh_1m_vwap:.1f} "
                                f"slip={round(_sh_ltp - _sh_1m_vwap, 1)}"
                            )
                            continue
                        # ── v10 GATE A — near-VWAP DISTANCE gate (DISABLED when V10_NEAR_VWAP_MAX=0) ──
                        if V10_NEAR_VWAP_MAX > 0 and abs(_sh_vwap_gap) >= V10_NEAR_VWAP_MAX:
                            logger.info(f"[SHADOW-P1] REJECT {_sh_dir} v10_vwap_far "
                                        f"gap_vwap={_sh_vwap_gap:+.2f} (need |gap|<{V10_NEAR_VWAP_MAX})")
                            continue
                        # ── v10 GATE B — block tiny ema9h_gap (< V10_MIN_EMA9H_GAP) ──
                        if _sh_1m_gap < V10_MIN_EMA9H_GAP:
                            logger.info(f"[SHADOW-P1] REJECT {_sh_dir} v10_tiny_gap "
                                        f"ema9h_gap={_sh_1m_gap:+.2f} (need >={V10_MIN_EMA9H_GAP})")
                            continue
                        _sh_strike = int(_sh_info.get("strike", 0) or 0)
                        # ── v10 LIVE: P1 places the real paper trade via the proven v8 executor ──
                        if V10_LIVE and not _v8_state.get("in_trade"):
                            try:
                                _sh_spot_now   = D.get_ltp(D.NIFTY_SPOT_TOKEN)
                                _sh_otm_key    = "CE_UP" if _sh_dir == "CE" else "PE_DN"
                                _sh_itm_key    = "CE_DN" if _sh_dir == "CE" else "PE_UP"
                                _sh_nbr_otm    = D.get_ltp(dir_tokens.get(_sh_otm_key, {}).get("token", 0) or 0)
                                _sh_nbr_itm    = D.get_ltp(dir_tokens.get(_sh_itm_key, {}).get("token", 0) or 0)
                                _v8_execute_paper_entry(
                                    direction=_sh_dir, strike=_sh_strike,
                                    symbol=_sh_info.get("symbol", ""), token=_sh_tok,
                                    entry_price=_sh_ltp,
                                    entry_result={"entry_price": _sh_ltp, "entry_mode": "V10_P1",
                                                  "fired_candle_ts": _sh_1m_bk_ts,
                                                  "close": _sh_1m_close,
                                                  "ema9_low": _sh_ema9l_1m, "ema9_high": _sh_ema9h_1m,
                                                  "bw": round(_sh_ema9h_1m - _sh_ema9l_1m, 1),
                                                  "gap": round(_sh_1m_gap, 2),
                                                  "rsi": round(_sh_rsi_1m, 1)},
                                    other_token=0,
                                    spot_at_entry=_sh_spot_now,
                                    neighbor_ltp_otm=_sh_nbr_otm,
                                    neighbor_ltp_itm=_sh_nbr_itm)
                            except Exception as _v10p1e:
                                logger.warning(f"[V10-LIVE] P1 execute error: {_v10p1e}")
                        _sh_sl     = round(_sh_ltp - 12, 1)
                        _sh_ds.update({
                            "active": True, "bucket_ts": _sh_1m_bk_ts,
                            "entry_price": _sh_ltp, "entry_time": now.strftime("%H:%M:%S"),
                            "peak_price": _sh_ltp, "peak_pts": 0.0,
                            "shadow_sl": round(_sh_ltp - 12, 1), "shadow_level": "INITIAL",
                            "today_entry": _sh_ltp, "today_date": str(now.date()),
                            "entry_tok": _sh_tok, "entry_strike": _sh_strike,
                            "sl_ts": 0.0,  # clear stale cooldown/outcome from prior trade
                            "last_exit_pnl": 0.0, "last_exit_reason": "", "last_exit_ts": 0.0,
                            "study_bw": round(_sh_ema9h_1m - _sh_ema9l_1m, 1),
                            "study_gap": round(_sh_1m_gap, 2),
                            "study_rsi": round(_sh_rsi_1m, 1),
                        })
                        # V2 tracker: same entry, new exit (dynamic trail + hard exit +40)
                        _v8_shadow_dt_v2[_sh_dir].update({
                            "active": True, "entry_price": _sh_ltp,
                            "entry_time": now.strftime("%H:%M:%S"),
                            "peak_price": _sh_ltp, "peak_pts": 0.0,
                            "shadow_sl": round(_sh_ltp - 12, 1), "entry_tok": _sh_tok,
                        })
                        # Check if Part 2 fired earlier today → compute saved pts
                        _p2_ds      = _v8_shadow_p2[_sh_dir]
                        _p2_today   = _p2_ds.get("today_entry", 0.0)
                        _p2_date    = _p2_ds.get("today_date", "")
                        _p2_line    = ""
                        if _p2_today > 0 and _p2_date == str(now.date()):
                            _p2_saved = round(_sh_ltp - _p2_today, 1)
                            _p2_ds["p1_entry"] = _sh_ltp   # store P1 entry in P2 state
                            _p2_line = (f"P2 entered: {_p2_today:.1f} → "
                                        f"saved {_p2_saved:+.1f} pts\n")
                            logger.info(
                                f"[SHADOW-P1] {_sh_dir} P2 was at {_p2_today} "
                                f"P1 now {_sh_ltp} saved={_p2_saved:+.1f}pts"
                            )
                        _sh_bw = round(_sh_ema9h_1m - _sh_ema9l_1m, 1)
                        logger.info(
                            f"[SHADOW-P1] {_sh_dir} {_sh_strike} SIGNAL "
                            f"entry={_sh_ltp} sl={_sh_sl} "
                            f"ema9h_gap={_sh_1m_gap:+.2f} bw={_sh_bw} "
                            f"vwap={_sh_1m_vwap:.1f} gap_vwap={_sh_vwap_gap:+.2f} rsi={_sh_rsi_1m:.1f}↑"
                        )
                        # CROSS-TRADE: check if P2 is open in opposite direction
                        _cross_opp = "PE" if _sh_dir == "CE" else "CE"
                        _cross_p2  = _v8_shadow_p2[_cross_opp]
                        if _cross_p2.get("active") and _cross_p2.get("today_date") == str(now.date()):
                            logger.info(
                                f"[CROSS-TRADE] P1-{_sh_dir} just fired vs P2-{_cross_opp} already open "
                                f"p1_entry={_sh_ltp} p2_entry={_cross_p2['entry_price']:.1f} "
                                f"p2_peak={_cross_p2.get('peak_pts',0):.1f} strike={_sh_strike}"
                            )
                        # DELAY-ANALYSIS: track LTP + spot at +5s/+10s/+30s/+60s
                        _delay_jobs.append({
                            "label": f"P1-{_sh_dir}", "strike": _sh_strike,
                            "base": _sh_ltp, "tok": _sh_tok,
                            "spot_base": D.get_ltp(D.NIFTY_SPOT_TOKEN) or 0.0,
                            "fire_ts": time.time(),
                            "snaps":       {5: None, 10: None, 30: None, 60: None},
                            "spot_snaps":  {5: None, 10: None, 30: None, 60: None},
                        })
                        pass  # shadow TG P1 entry alert removed
                        _save_shadow_state()
                        # ── Analysis flags (no trade impact) ──
                        _other_sh_dir = "PE" if _sh_dir == "CE" else "CE"
                        _other_sh_vwap_gap = None
                        try:
                            _other_sh_info = (_locked_tokens or {}).get(_other_sh_dir, {})
                            _other_sh_tok2 = int(_other_sh_info.get("token", 0) or 0)
                            if _other_sh_tok2:
                                _other_sh_1m2 = D.get_option_1min(_other_sh_tok2, 5)
                                if _other_sh_1m2 is not None and len(_other_sh_1m2) >= 2:
                                    _osh_day2 = _other_sh_1m2[_other_sh_1m2.index.date == now.date()]
                                    if len(_osh_day2) >= 2:
                                        _osh_tv2 = ((_osh_day2["high"]+_osh_day2["low"]+_osh_day2["close"])/3)*_osh_day2["volume"]
                                        _osh_vwap2 = float((_osh_tv2.cumsum()/_osh_day2["volume"].cumsum().replace(0,float('nan'))).iloc[-2])
                                        _other_sh_vwap_gap = round(_osh_day2["close"].iloc[-2] - _osh_vwap2, 1)
                        except Exception:
                            pass
                        _xleg_sh_dir = "PE" if _sh_dir == "CE" else "CE"
                        _log_shadow_analysis(
                            "P1", _sh_dir, now, _sh_ltp,
                            _sh_vwap_gap, _other_sh_vwap_gap,
                            float(spot_3m.get("adx", 0)),
                            _shadow_analysis[_sh_dir]["last_peaks"],
                            ema9h_gap=_sh_1m_gap,
                            xleg_buf=_shadow_analysis[_xleg_sh_dir]["cross_buf"],
                            dte=dte,
                            fut_vwap_gap=float(LEVELS._vwap_state.get("gap", 0.0)),
                            spot_ema9=float(spot_3m.get("ema9", 0)),
                            spot_ema21=float(spot_3m.get("ema21", 0)),
                            bw=_sh_bw,
                        )
                        # PDH/PDL/Pivot/VWAP filter data for this P1 shadow signal
                        try:
                            LEVELS.log_entry(
                                direction=_sh_dir,
                                strike=int(_sh_strike or 0),
                                entry_price=float(_sh_ltp),
                                spot_px=float(D.get_ltp(D.NIFTY_SPOT_TOKEN) or 0),
                                entry_time_dt=now,
                                dte=dte,
                            )
                        except Exception as _lvl_sh_e:
                            logger.debug(f"[SHADOW-LVL] P1 hook error: {_lvl_sh_e}")
                except Exception as _she:
                    logger.warning(f"[SHADOW-P1] error: {_she}")

            # ── SHADOW Part 2: buildup tracker (close > EMA9H, close < VWAP, RSI > 55 rising) ──
            if (not _v8_state.get("in_trade")
                    and D.is_trading_window(now)
                    and _locked_tokens
                    and time.time() - _v8_shadow_p2["last_scan_ts"] >= 3):
                _v8_shadow_p2["last_scan_ts"] = time.time()
                try:
                    for _s2_dir, _s2_info in [("CE", (_locked_tokens or {}).get("CE", {})),
                                               ("PE", (_locked_tokens or {}).get("PE", {}))]:
                        _s2_tok = int(_s2_info.get("token", 0) or 0)
                        if not _s2_tok:
                            continue

                        # ── 1-min last completed candle ──
                        _s2_1m = D.get_option_1min(_s2_tok, 100)
                        if _s2_1m is None or len(_s2_1m) < 4:
                            continue
                        _s2_comp     = _s2_1m.iloc[-2]
                        _s2_bk_ts    = str(_s2_comp.name)
                        _s2_close    = float(_s2_comp["close"])
                        _s2_open     = float(_s2_comp["open"])
                        _s2_ema9h    = float(_s2_comp.get("ema9_high", 0))
                        _s2_ema9l    = float(_s2_comp.get("ema9_low", 0))
                        _s2_bw       = round(_s2_ema9h - _s2_ema9l, 2) if _s2_ema9h > 0 and _s2_ema9l > 0 else 0.0
                        _s2_rsi      = float(_s2_comp.get("RSI", 0) or 0)
                        _s2_rsi_p    = float(_s2_1m.iloc[-3].get("RSI", 0) or 0)
                        _s2_ema9l_prev = float(_s2_1m.iloc[-3].get("ema9_low", 0))

                        # 1-min session VWAP
                        _s2_day = _s2_1m[_s2_1m.index.date == now.date()].copy()
                        if len(_s2_day) < 3:
                            continue
                        _s2_day["_typ"] = (_s2_day["high"] + _s2_day["low"] + _s2_day["close"]) / 3.0
                        _s2_day["_tv"]  = _s2_day["_typ"] * _s2_day["volume"]
                        _s2_cum_vol = _s2_day["volume"].cumsum().replace(0, np.nan)
                        _s2_vwap    = float((_s2_day["_tv"].cumsum() / _s2_cum_vol).iloc[-2])

                        _s2_ds = _v8_shadow_p2[_s2_dir]

                        # Bucket change: update bucket_ts only — DO NOT reset active signal
                        if _s2_ds["bucket_ts"] != _s2_bk_ts:
                            _s2_ds["bucket_ts"] = _s2_bk_ts

                        # If signal active — track LTP using ORIGINAL token (not current ATM)
                        if _s2_ds["active"]:
                            _s2_track_tok = int(_s2_ds.get("entry_tok", 0) or _s2_tok)
                            _s2_ltp_pk  = D.get_ltp(_s2_track_tok)
                            if not _s2_ltp_pk:
                                continue
                            _s2_cur_sl  = _s2_ds.get("shadow_sl", round(_s2_ds["entry_price"] - 12, 1))
                            _s2_entry   = _s2_ds["entry_price"]

                            def _s2_close_signal(reason, exit_px):
                                _s2_fin_peak = _s2_ds["peak_pts"]
                                _s2_fin_lvl  = _s2_ds.get("shadow_level", "INITIAL")
                                _s2_fin_pnl  = round(exit_px - _s2_entry, 1)
                                _s2_fin_icon = "✅" if _s2_fin_pnl >= 20 else ("🟡" if _s2_fin_pnl > 0 else "❌")
                                _s2_fin_msg  = (
                                    f"🟡 SHADOW P2 {_s2_dir} — {reason}\n"
                                    f"Entry: {_s2_entry:.1f}  Exit: {exit_px:.1f}\n"
                                    f"PnL: {_s2_fin_icon} {_s2_fin_pnl:+.1f}  Peak: +{_s2_fin_peak:.1f}\n"
                                    f"Trail reached: {_s2_fin_lvl}\n"
                                )
                                _s2_p1e2 = _s2_ds.get("p1_entry", 0.0)
                                if _s2_p1e2 > 0:
                                    _s2_diff2 = round(_s2_p1e2 - _s2_entry, 1)
                                    _s2_fin_msg += f"P1 at {_s2_p1e2:.1f} → P2 was {_s2_diff2:+.1f}pts earlier\n"
                                else:
                                    _s2_fin_msg += f"P1 not fired — VWAP never broke\n"
                                _s2_fin_msg += f"<i>⚠️ Shadow only</i>"
                                logger.info(
                                    f"[SHADOW-P2] {_s2_dir} {reason} "
                                    f"entry={_s2_entry} exit={exit_px:.1f} "
                                    f"pnl={_s2_fin_pnl:+.1f} peak=+{_s2_fin_peak:.1f} trail={_s2_fin_lvl}"
                                )
                                # BW+Gap study log
                                try:
                                    import csv as _csv_s2, os as _os_s2
                                    _sp2 = _os_s2.path.join(_os_s2.path.dirname(__file__), "state", "bw_gap_study.csv")
                                    _sp2_new = not _os_s2.path.isfile(_sp2)
                                    with open(_sp2, "a", newline="") as _sf2:
                                        _sw2 = _csv_s2.writer(_sf2)
                                        if _sp2_new:
                                            _sw2.writerow(["date", "entry_time", "path", "direction",
                                                           "bw", "gap", "rsi", "entry_price",
                                                           "peak_pts", "trail_level", "pnl_pts", "exit_reason"])
                                        _sw2.writerow([
                                            date.today().isoformat(),
                                            _s2_ds.get("entry_time", ""),
                                            "P2", _s2_dir,
                                            _s2_ds.get("study_bw", 0),
                                            _s2_ds.get("study_gap", 0),
                                            _s2_ds.get("study_rsi", 0),
                                            round(_s2_entry, 1),
                                            round(_s2_fin_peak, 1),
                                            _s2_fin_lvl,
                                            _s2_fin_pnl,
                                            reason,
                                        ])
                                except Exception as _sel2:
                                    logger.warning(f"[STUDY] P2 log error: {_sel2}")
                                pass  # shadow TG P2 exit alert removed
                                # Track peak for analysis streak detection
                                _shadow_analysis[_s2_dir]["last_peaks_p2"].append(_s2_fin_peak)
                                _shadow_analysis[_s2_dir]["last_peaks_p2"] = \
                                    _shadow_analysis[_s2_dir]["last_peaks_p2"][-2:]
                                _s2_ds.update({
                                    "active": False, "entry_price": 0.0, "entry_time": "",
                                    "peak_price": 0.0, "peak_pts": 0.0,
                                    "shadow_sl": 0.0, "shadow_level": "INITIAL", "p1_entry": 0.0,
                                    "bucket_ts": _s2_bk_ts,  # block re-fire on same candle
                                    "last_exit_pnl": _s2_fin_pnl, "last_exit_reason": reason,
                                    "last_exit_ts": time.time(),
                                })
                                _save_shadow_state()

                            # EOD check
                            if now.time() >= dtime(15, 15):
                                _s2_close_signal("EOD", _s2_ltp_pk)
                                continue

                            # SL hit check
                            if _s2_ltp_pk <= _s2_cur_sl:
                                _s2_close_signal("SL-HIT", _s2_cur_sl)
                                continue

                            # Update peak + trail ladder
                            if _s2_ltp_pk > _s2_ds["peak_price"]:
                                _s2_ds["peak_price"] = _s2_ltp_pk
                                _s2_ds["peak_pts"]   = round(_s2_ltp_pk - _s2_entry, 1)
                                _s2_new_sl, _s2_new_lvl = _shadow_trail_sl(_s2_entry, _s2_ds["peak_pts"])
                                _s2_old_lvl = _s2_ds.get("shadow_level", "INITIAL")
                                if _s2_new_lvl != _s2_old_lvl:
                                    _s2_ds["shadow_sl"]    = _s2_new_sl
                                    _s2_ds["shadow_level"] = _s2_new_lvl
                                    logger.info(
                                        f"[SHADOW-P2] {_s2_dir} trail ↑ {_s2_new_lvl} "
                                        f"peak=+{_s2_ds['peak_pts']:.1f} sl_now={_s2_new_sl:.1f}"
                                    )
                                    pass  # shadow TG P2 trail alert removed
                                    _save_shadow_state()
                            continue

                        # ── Part 2 gates ──
                        _s2_ema9h_gap  = round(_s2_close - _s2_ema9h, 2)
                        _s2_vwap_gap   = round(_s2_close - _s2_vwap, 2)
                        _s2_reject     = None
                        if not (_s2_ema9h > 0 and _s2_close > _s2_ema9h):
                            _s2_reject = f"below_ema9h gap={_s2_ema9h_gap}"
                        elif _s2_ema9h_gap < V10_MIN_EMA9H_GAP:
                            _s2_reject = f"ema9h_gap_weak gap={_s2_ema9h_gap:.2f}(need>={V10_MIN_EMA9H_GAP})"
                        elif not (_s2_close <= _s2_vwap):
                            _s2_reject = f"already_above_vwap gap={_s2_vwap_gap:+.1f}"
                        elif not (_s2_rsi > _s2_rsi_p):
                            _s2_reject = f"rsi_falling rsi={_s2_rsi:.1f} prev={_s2_rsi_p:.1f}"
                        elif not (V10_RSI_MIN < _s2_rsi < V10_RSI_MAX):
                            _s2_reject = f"rsi_weak rsi={_s2_rsi:.1f}(need {V10_RSI_MIN}-{V10_RSI_MAX})"
                        elif _s2_bw < V10_BW_MIN:
                            _s2_reject = f"bw_weak bw={_s2_bw}(need>={V10_BW_MIN})"
                        if now.second % 15 == 0:
                            _s2_above_ema9l = (_s2_ema9l > 0 and _s2_close > _s2_ema9l)
                            _shadow_analysis[_s2_dir]["cross_buf"].append(_s2_above_ema9l)
                            _shadow_analysis[_s2_dir]["cross_buf"] = \
                                _shadow_analysis[_s2_dir]["cross_buf"][-5:]
                        if _s2_reject:
                            if now.second % 15 == 0:
                                logger.info(f"[SHADOW-P2] REJECT {_s2_dir} {_s2_reject}")
                            continue

                        # ── Hard gate: opening blackout 9:15–9:45 ──
                        if now.time() < V10_OPEN_BLACKOUT_END:
                            if now.second % 15 == 0:
                                logger.info(f"[SHADOW-P2] REJECT {_s2_dir} open_blackout "
                                            f"(no entries before {V10_OPEN_BLACKOUT_END})")
                            continue

                        # ── Exit cooldown: block P2 re-entry for 120s after any exit ──
                        _s2_exit_age = time.time() - _s2_ds.get("exit_ts", 0)
                        if 0 < _s2_exit_age < 120:
                            if now.second % 15 == 0:
                                logger.info(f"[SHADOW-P2] REJECT {_s2_dir} exit_cooldown age={int(_s2_exit_age)}s")
                            continue

                        _xleg_g2_dir  = "PE" if _s2_dir == "CE" else "CE"
                        _xleg_g2_buf  = _shadow_analysis[_xleg_g2_dir].get("cross_buf", [])
                        _xleg_g2_buf5 = _xleg_g2_buf[-5:]
                        _xleg_g2_ok   = len(_xleg_g2_buf5) >= 3 and all(not v for v in _xleg_g2_buf5)
                        if not _xleg_g2_ok:
                            logger.info(
                                f"[SHADOW-P2] REJECT {_s2_dir} xleg_not_confirmed "
                                f"{_xleg_g2_dir} buf={_xleg_g2_buf5} n={len(_xleg_g2_buf5)}"
                            )
                            continue
                        _s2_ltp    = D.get_ltp(_s2_tok)
                        # Gate: LTP must still be below VWAP at fire time
                        if _s2_ltp and _s2_ltp > _s2_vwap:
                            logger.info(
                                f"[SHADOW-P2] REJECT {_s2_dir} ltp_slipped_above_vwap "
                                f"ltp={_s2_ltp:.1f} vwap={_s2_vwap:.1f} "
                                f"gap={round(_s2_ltp - _s2_vwap, 1)}"
                            )
                            continue
                        # ── v10 GATE A — near-VWAP DISTANCE gate (DISABLED when V10_NEAR_VWAP_MAX=0) ──
                        if V10_NEAR_VWAP_MAX > 0 and abs(_s2_vwap_gap) >= V10_NEAR_VWAP_MAX:
                            logger.info(f"[SHADOW-P2] REJECT {_s2_dir} v10_vwap_far "
                                        f"gap_vwap={_s2_vwap_gap:+.2f} (need |gap|<{V10_NEAR_VWAP_MAX})")
                            continue
                        # ── v10 GATE B — block tiny ema9h_gap (< V10_MIN_EMA9H_GAP) ──
                        if _s2_ema9h_gap < V10_MIN_EMA9H_GAP:
                            logger.info(f"[SHADOW-P2] REJECT {_s2_dir} v10_tiny_gap "
                                        f"ema9h_gap={_s2_ema9h_gap:+.2f} (need >={V10_MIN_EMA9H_GAP})")
                            continue
                        _s2_strike = int(_s2_info.get("strike", 0) or 0)
                        # ── v10 LIVE: P2 places the real paper trade via the proven v8 executor ──
                        if V10_LIVE and not _v8_state.get("in_trade"):
                            try:
                                _s2_spot_now  = D.get_ltp(D.NIFTY_SPOT_TOKEN)
                                _s2_otm_key   = "CE_UP" if _s2_dir == "CE" else "PE_DN"
                                _s2_itm_key   = "CE_DN" if _s2_dir == "CE" else "PE_UP"
                                _s2_nbr_otm   = D.get_ltp(dir_tokens.get(_s2_otm_key, {}).get("token", 0) or 0)
                                _s2_nbr_itm   = D.get_ltp(dir_tokens.get(_s2_itm_key, {}).get("token", 0) or 0)
                                _v8_execute_paper_entry(
                                    direction=_s2_dir, strike=_s2_strike,
                                    symbol=_s2_info.get("symbol", ""), token=_s2_tok,
                                    entry_price=_s2_ltp,
                                    entry_result={"entry_price": _s2_ltp, "entry_mode": "V10_P2",
                                                  "fired_candle_ts": _s2_bk_ts,
                                                  "close": _s2_close,
                                                  "ema9_low": _s2_ema9l, "ema9_high": _s2_ema9h,
                                                  "bw": round(_s2_bw, 1),
                                                  "gap": round(_s2_ema9h_gap, 2),
                                                  "rsi": round(_s2_rsi, 1)},
                                    other_token=0,
                                    spot_at_entry=_s2_spot_now,
                                    neighbor_ltp_otm=_s2_nbr_otm,
                                    neighbor_ltp_itm=_s2_nbr_itm)
                            except Exception as _v10p2e:
                                logger.warning(f"[V10-LIVE] P2 execute error: {_v10p2e}")
                        _s2_sl_px  = round(_s2_ltp - 12, 1)
                        _s2_ds.update({
                            "active": True, "bucket_ts": _s2_bk_ts,
                            "entry_price": _s2_ltp, "entry_time": now.strftime("%H:%M:%S"),
                            "peak_price": _s2_ltp, "peak_pts": 0.0,
                            "shadow_sl": _s2_sl_px, "shadow_level": "INITIAL",
                            "today_entry": _s2_ltp, "today_date": str(now.date()),
                            "p1_entry": 0.0,
                            "entry_tok": _s2_tok, "entry_strike": _s2_strike,
                            "exit_ts": 0.0,  # clear stale cooldown/outcome from prior trade
                            "last_exit_pnl": 0.0, "last_exit_reason": "", "last_exit_ts": 0.0,
                            "study_bw": round(_s2_bw, 1),
                            "study_gap": round(_s2_ema9h_gap, 2),
                            "study_rsi": round(_s2_rsi, 1),
                        })
                        # V2 tracker: same entry, hard exit at +20
                        _v8_shadow_p2_v2[_s2_dir].update({
                            "active": True, "entry_price": _s2_ltp,
                            "entry_time": now.strftime("%H:%M:%S"),
                            "peak_price": _s2_ltp, "peak_pts": 0.0,
                            "shadow_sl": _s2_sl_px, "entry_tok": _s2_tok,
                        })
                        # Check if P1 already fired today (rare — price jumped above VWAP directly)
                        _s2_p1_today = _v8_shadow_dt[_s2_dir].get("today_entry", 0.0)
                        _s2_p1_date  = _v8_shadow_dt[_s2_dir].get("today_date", "")
                        _s2_p1_note  = ""
                        if _s2_p1_today > 0 and _s2_p1_date == str(now.date()):
                            _s2_diff = round(_s2_ltp - _s2_p1_today, 1)
                            _s2_p1_note = f"⚠️ P1 already fired: {_s2_p1_today:.1f} (P2 is {_s2_diff:+.1f})\n"
                        logger.info(
                            f"[SHADOW-P2] {_s2_dir} {_s2_strike} SIGNAL "
                            f"entry={_s2_ltp} sl={_s2_sl_px} "
                            f"ema9h_gap={_s2_ema9h_gap:+.2f} bw={_s2_bw:.1f} "
                            f"vwap={_s2_vwap:.1f} below_by={_s2_vwap_gap:.1f} rsi={_s2_rsi:.1f}↑"
                        )
                        # CROSS-TRADE: check if P1 is open in opposite direction
                        _cross_opp2 = "PE" if _s2_dir == "CE" else "CE"
                        _cross_p1   = _v8_shadow_dt[_cross_opp2]
                        if _cross_p1.get("active") and _cross_p1.get("today_date","") == str(now.date()):
                            logger.info(
                                f"[CROSS-TRADE] P2-{_s2_dir} just fired vs P1-{_cross_opp2} already open "
                                f"p2_entry={_s2_ltp} p1_entry={_cross_p1.get('entry_price',0):.1f} "
                                f"p1_peak={_cross_p1.get('peak_pts',0):.1f} strike={_s2_strike}"
                            )
                        # DELAY-ANALYSIS: track LTP + spot at +5s/+10s/+30s/+60s
                        _delay_jobs.append({
                            "label": f"P2-{_s2_dir}", "strike": _s2_strike,
                            "base": _s2_ltp, "tok": _s2_tok,
                            "spot_base": D.get_ltp(D.NIFTY_SPOT_TOKEN) or 0.0,
                            "fire_ts": time.time(),
                            "snaps":       {5: None, 10: None, 30: None, 60: None},
                            "spot_snaps":  {5: None, 10: None, 30: None, 60: None},
                        })
                        pass  # shadow TG P2 entry alert removed
                        _save_shadow_state()
                        # ── Analysis flags (no trade impact) ──
                        _other_s2_dir = "PE" if _s2_dir == "CE" else "CE"
                        _other_s2_vwap_gap = None
                        try:
                            _other_s2_info = (_locked_tokens or {}).get(_other_s2_dir, {})
                            _other_s2_tok2 = int(_other_s2_info.get("token", 0) or 0)
                            if _other_s2_tok2:
                                _other_s2_1m2 = D.get_option_1min(_other_s2_tok2, 5)
                                if _other_s2_1m2 is not None and len(_other_s2_1m2) >= 2:
                                    _os2_day = _other_s2_1m2[_other_s2_1m2.index.date == now.date()]
                                    if len(_os2_day) >= 2:
                                        _os2_tv = ((_os2_day["high"]+_os2_day["low"]+_os2_day["close"])/3)*_os2_day["volume"]
                                        _os2_vwap = float((_os2_tv.cumsum()/_os2_day["volume"].cumsum().replace(0,float('nan'))).iloc[-2])
                                        _other_s2_vwap_gap = round(_os2_day["close"].iloc[-2] - _os2_vwap, 1)
                        except Exception:
                            pass
                        _xleg_s2_dir = "PE" if _s2_dir == "CE" else "CE"
                        _log_shadow_analysis(
                            "P2", _s2_dir, now, _s2_ltp,
                            _s2_vwap_gap, _other_s2_vwap_gap,
                            float(spot_3m.get("adx", 0)),
                            _shadow_analysis[_s2_dir]["last_peaks_p2"],
                            ema9h_gap=_s2_ema9h_gap,
                            xleg_buf=_shadow_analysis[_xleg_s2_dir]["cross_buf"],
                            dte=dte,
                            fut_vwap_gap=float(LEVELS._vwap_state.get("gap", 0.0)),
                            spot_ema9=float(spot_3m.get("ema9", 0)),
                            spot_ema21=float(spot_3m.get("ema21", 0)),
                            bw=_s2_bw,
                        )
                        # PDH/PDL/Pivot/VWAP filter data for this P2 shadow signal
                        try:
                            LEVELS.log_entry(
                                direction=_s2_dir,
                                strike=int(_s2_strike or 0),
                                entry_price=float(_s2_ltp),
                                spot_px=float(D.get_ltp(D.NIFTY_SPOT_TOKEN) or 0),
                                entry_time_dt=now,
                                dte=dte,
                            )
                        except Exception as _lvl_s2_e:
                            logger.debug(f"[SHADOW-LVL] P2 hook error: {_lvl_s2_e}")
                except Exception as _s2e:
                    logger.warning(f"[SHADOW-P2] error: {_s2e}")

            # ── SHADOW P3: extreme VWAP reversal (|fut-vwap| >= V10_P3_VWAP_EXTREME) ──
            # CE when VWAP >> fut (price crashed far below VWAP, bouncing back)
            # PE when fut >> VWAP (price ran far above VWAP, fading back)
            # Shadow-only — no live trades, data collection only.
            _p3_fut_vwap_gap = float(LEVELS._vwap_state.get("gap", 0.0))  # fut - vwap
            if (not _v8_state.get("in_trade")
                    and D.is_trading_window(now)
                    and _locked_tokens
                    and abs(_p3_fut_vwap_gap) >= V10_P3_VWAP_EXTREME
                    and time.time() - _v8_shadow_p3["last_scan_ts"] >= 3):
                _v8_shadow_p3["last_scan_ts"] = time.time()
                try:
                    # P3-CE: VWAP far above fut (fut<vwap by 75+) → bounce CE
                    # P3-PE: fut far above VWAP (fut>vwap by 75+) → fade PE
                    _p3_dir = "CE" if _p3_fut_vwap_gap < 0 else "PE"
                    _p3_info = (_locked_tokens or {}).get(_p3_dir, {})
                    _p3_tok  = int(_p3_info.get("token", 0) or 0)
                    if _p3_tok:
                        _p3_1m = D.get_option_1min(_p3_tok, 100)
                        if _p3_1m is not None and len(_p3_1m) >= 4:
                            _p3_comp   = _p3_1m.iloc[-2]
                            _p3_bk_ts  = str(_p3_comp.name)
                            _p3_close  = float(_p3_comp["close"])
                            _p3_ema9h  = float(_p3_comp.get("ema9_high", 0))
                            _p3_ema9l  = float(_p3_comp.get("ema9_low", 0))
                            _p3_bw     = round(_p3_ema9h - _p3_ema9l, 2) if _p3_ema9h > 0 and _p3_ema9l > 0 else 0.0
                            _p3_rsi    = float(_p3_comp.get("RSI", 0) or 0)
                            _p3_rsi_p  = float(_p3_1m.iloc[-3].get("RSI", 0) or 0)
                            _p3_gap    = round(_p3_close - _p3_ema9h, 2)
                            _p3_ds     = _v8_shadow_p3[_p3_dir]

                            if _p3_ds["bucket_ts"] != _p3_bk_ts:
                                _p3_ds["bucket_ts"] = _p3_bk_ts

                            # Track active P3 signal
                            if _p3_ds["active"]:
                                _p3_track_tok = int(_p3_ds.get("entry_tok", 0) or _p3_tok)
                                _p3_ltp_pk = D.get_ltp(_p3_track_tok)
                                if _p3_ltp_pk:
                                    _p3_entry = _p3_ds["entry_price"]
                                    _p3_cur_sl = _p3_ds.get("shadow_sl", round(_p3_entry - 12, 1))

                                    def _p3_close_signal(reason, exit_px):
                                        _p3_fin_peak = _p3_ds["peak_pts"]
                                        _p3_fin_lvl  = _p3_ds.get("shadow_level", "INITIAL")
                                        _p3_fin_pnl  = round(exit_px - _p3_entry, 1)
                                        logger.info(
                                            f"[SHADOW-P3] {_p3_dir} {reason} "
                                            f"entry={_p3_entry} exit={exit_px:.1f} "
                                            f"pnl={_p3_fin_pnl:+.1f} peak=+{_p3_fin_peak:.1f} "
                                            f"trail={_p3_fin_lvl} "
                                            f"vwap_gap_at_entry={_p3_ds.get('vwap_gap_at_entry',0):+.0f}"
                                        )
                                        _p3_ds.update({
                                            "active": False, "entry_price": 0.0, "entry_time": "",
                                            "peak_price": 0.0, "peak_pts": 0.0,
                                            "shadow_sl": 0.0, "shadow_level": "INITIAL",
                                            "bucket_ts": _p3_bk_ts,
                                            "last_exit_pnl": _p3_fin_pnl, "last_exit_reason": reason,
                                            "last_exit_ts": time.time(),
                                        })
                                        _save_shadow_state()

                                    if now.time() >= dtime(15, 15):
                                        _p3_close_signal("EOD", _p3_ltp_pk)
                                    elif _p3_ltp_pk <= _p3_cur_sl:
                                        _p3_close_signal("SL-HIT", _p3_cur_sl)
                                    else:
                                        if _p3_ltp_pk > _p3_ds["peak_price"]:
                                            _p3_ds["peak_price"] = _p3_ltp_pk
                                            _p3_ds["peak_pts"]   = round(_p3_ltp_pk - _p3_entry, 1)
                                            _p3_new_sl, _p3_new_lvl = _shadow_trail_sl(_p3_entry, _p3_ds["peak_pts"])
                                            if _p3_new_lvl != _p3_ds.get("shadow_level", "INITIAL"):
                                                _p3_ds["shadow_sl"]    = _p3_new_sl
                                                _p3_ds["shadow_level"] = _p3_new_lvl
                                                logger.info(
                                                    f"[SHADOW-P3] {_p3_dir} trail ↑ {_p3_new_lvl} "
                                                    f"peak=+{_p3_ds['peak_pts']:.1f} sl_now={_p3_new_sl:.1f}"
                                                )
                                                _save_shadow_state()
                            else:
                                # ── P3 entry gates ──
                                _p3_reject = None
                                if not (_p3_ema9h > 0 and _p3_close > _p3_ema9h):
                                    _p3_reject = f"below_ema9h gap={_p3_gap:.2f}"
                                elif _p3_gap < V10_MIN_EMA9H_GAP:
                                    _p3_reject = f"ema9h_gap_weak gap={_p3_gap:.2f}(need>={V10_MIN_EMA9H_GAP})"
                                elif not (_p3_rsi > _p3_rsi_p):
                                    _p3_reject = f"rsi_falling rsi={_p3_rsi:.1f} prev={_p3_rsi_p:.1f}"
                                elif not (V10_RSI_MIN < _p3_rsi < V10_RSI_MAX):
                                    _p3_reject = f"rsi_outofrange rsi={_p3_rsi:.1f}"
                                elif _p3_bw < V10_BW_MIN:
                                    _p3_reject = f"bw_weak bw={_p3_bw:.1f}"

                                # XLEG_CONFIRMED: cross-leg must be below EMA9H all last 5 scans
                                _p3_xleg_dir = "PE" if _p3_dir == "CE" else "CE"
                                _p3_xleg_buf = _shadow_analysis[_p3_xleg_dir].get("cross_buf", [])
                                _p3_xleg_ok  = len(_p3_xleg_buf[-5:]) >= 3 and all(not v for v in _p3_xleg_buf[-5:])

                                if _p3_reject:
                                    if now.second % 15 == 0:
                                        logger.info(f"[SHADOW-P3] REJECT {_p3_dir} {_p3_reject} "
                                                    f"vwap_gap={_p3_fut_vwap_gap:+.0f}")
                                elif not _p3_xleg_ok:
                                    if now.second % 15 == 0:
                                        logger.info(f"[SHADOW-P3] REJECT {_p3_dir} xleg_not_confirmed "
                                                    f"{_p3_xleg_dir} buf={_p3_xleg_buf[-5:]} "
                                                    f"vwap_gap={_p3_fut_vwap_gap:+.0f}")
                                else:
                                    _p3_ltp = D.get_ltp(_p3_tok)
                                    if _p3_ltp:
                                        _p3_sl_px = round(_p3_ltp - 12, 1)
                                        _p3_strike = int(_p3_info.get("strike", 0) or 0)
                                        _p3_ds.update({
                                            "active": True, "bucket_ts": _p3_bk_ts,
                                            "entry_price": _p3_ltp, "entry_time": now.strftime("%H:%M:%S"),
                                            "peak_price": _p3_ltp, "peak_pts": 0.0,
                                            "shadow_sl": _p3_sl_px, "shadow_level": "INITIAL",
                                            "entry_tok": _p3_tok, "entry_strike": _p3_strike,
                                            "vwap_gap_at_entry": round(_p3_fut_vwap_gap, 1),
                                            "last_exit_pnl": 0.0, "last_exit_reason": "", "last_exit_ts": 0.0,
                                        })
                                        logger.info(
                                            f"[SHADOW-P3] {_p3_dir} {_p3_strike} SIGNAL "
                                            f"entry={_p3_ltp} sl={_p3_sl_px} "
                                            f"ema9h_gap={_p3_gap:+.2f} bw={_p3_bw:.1f} "
                                            f"rsi={_p3_rsi:.1f}↑ "
                                            f"fut_vwap_gap={_p3_fut_vwap_gap:+.0f}(extreme)"
                                        )
                                        _save_shadow_state()
                except Exception as _p3e:
                    logger.warning(f"[SHADOW-P3] error: {_p3e}")

            # Capture live entry price into per-direction shadow state
            _live_dir = _v8_state.get("direction", "")
            if (_live_dir in ("CE", "PE")
                    and _v8_state.get("in_trade")
                    and _v8_shadow_dt[_live_dir]["active"]
                    and _v8_shadow_dt[_live_dir]["live_entry"] == 0):
                _live_px = float(_v8_state.get("entry_price", 0))
                _v8_shadow_dt[_live_dir]["live_entry"] = _live_px
                _saved = round(_live_px - _v8_shadow_dt[_live_dir]["entry_price"], 1)
                logger.info(
                    f"[SHADOW-DTF] LIVE ENTRY {_live_dir} fired "
                    f"shadow={_v8_shadow_dt[_live_dir]['entry_price']} live={_live_px} "
                    f"saved={_saved:+.1f}pts ({'EARLIER ✓' if _saved > 0 else 'LATER or same'})"
                )

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

            # ── Refresh opening range at 9:30 (once) ──
            try:
                if (now.hour == 9 and now.minute >= 30
                        and not state.get("_or_refreshed_today")):
                    LEVELS.refresh_opening_range(D)
                    state["_or_refreshed_today"] = True
            except Exception:
                pass

            # ── Refresh VWAP every 15-min candle boundary ──
            try:
                _cur_15m = now.hour * 4 + now.minute // 15
                if _cur_15m != state.get("_last_vwap_15m_slot", -1):
                    LEVELS.update_vwap(kite)
                    state["_last_vwap_15m_slot"] = _cur_15m
            except Exception:
                pass

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
                                + " + /status, or force relock after 9:15 open.",
                                priority="critical",
                            )
                        except Exception:
                            pass
                _save_state()
                try:
                    _generate_eod_report()
                except Exception as e:
                    logger.error("[MAIN] EOD report error: " + str(e))
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
                            state["_candle_low"] = option_ltp

                    _mex_other_tok = state.get("other_token", 0)
                    _prev_tier = state.get("active_ratchet_tier", "None") or "None"
                    _prev_sl   = float(state.get("active_ratchet_sl", 0) or 0)
                    exit_list = manage_exit(state, option_ltp, profile, other_token=_mex_other_tok)

                    try:
                        _new_tier  = state.get("active_ratchet_tier", "None")
                        _armed = _new_tier not in ("None", "", "INITIAL")
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
                            _icon = "🔒"
                            if _new_tier == "LOCK_M5":
                                _icon = "⚠️"
                            elif _new_tier == "LOCK_3":
                                _icon = "🛡️"
                            elif _new_tier == "LOCK_5":
                                _icon = "🔒"
                            elif _new_tier == "LOCK_8":
                                _icon = "🔒🔒"
                            elif _new_tier == "LOCK_15":
                                _icon = "🔒🔒"
                            elif _new_tier == "LOCK_DYN":
                                _icon = "🔒🔒🔒"
                            _sl_old_str = ("Rs" + "{:.1f}".format(_prev_sl)
                                           if _prev_sl > 0 else "entry-10")
                            _tg_send(
                                _icon + " <b>V10 SL UPGRADED → " + _new_tier + "</b>\n"
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

                    if state.get("in_trade"):
                        _peak = state.get("peak_pnl", 0)
                        _last_ms = state.get("_last_milestone", 0)
                        _cur_el = round(float(state.get("current_ema9_low", 0)), 1)
                        _entry_px = state.get("entry_price", 0)
                        for _m in [5, 8, 10, 12, 15, 20, 25, 30, 40, 50]:
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
                                _ms_icon = "📈"
                                if _r_tier == "LOCK_M5":
                                    _ms_icon = "⚠️"
                                elif _r_tier == "LOCK_3":
                                    _ms_icon = "🛡️"
                                elif _r_tier in ("LOCK_5", "LOCK_8"):
                                    _ms_icon = "🔒"
                                elif _r_tier in ("LOCK_15", "LOCK_DYN"):
                                    _ms_icon = "🔒🔒"
                                _lock_str = (("+" if _lock >= 0 else "")
                                             + "{:.1f}".format(_lock))
                                _tg_send(
                                    _ms_icon + " <b>V10 Peak +" + str(_m)
                                    + " pts</b>   " + _r_tier + "\n"
                                    "━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                                    "Peak  +" + "{:.1f}".format(_peak) + "\n"
                                    "Now   +" + "{:.1f}".format(_cur_pnl) + "\n"
                                    "SL    Rs" + "{:.1f}".format(_r_sl)
                                    + "   (" + _lock_str + " locked)\n"
                                    "Room  " + "{:.1f}".format(_room) + " pts"
                                )
                                break
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

                if now.second % 5 < 2:
                    _update_dashboard_ltp()

                time.sleep(0.5)
                continue

            if (not state.get("paused")
                    and D.is_trading_window(now)
                    and _is_new_1min_candle(now)
                    and spot_ltp > 0
                    and expiry is not None):

                step       = D.get_active_strike_step(dte)
                atm_strike = D.resolve_atm_strike(spot_ltp, step)

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
                    _dist_from_lock = abs(spot_ltp - _locked_ce_strike)
                    if (_target_atm != _locked_ce_strike
                            and _dist_from_lock >= 40):
                        _relock = True
                        _spot_move = round(spot_ltp - _locked_at_spot, 1)
                        _old_ce = _locked_ce_strike
                        _old_pe = _locked_pe_strike
                        logger.info("[MAIN] ATM drift past hysteresis: locked="
                                    + str(_locked_ce_strike) + " target="
                                    + str(_target_atm) + " spot="
                                    + str(round(spot_ltp, 1))
                                    + " (dist=" + "{:.1f}".format(_dist_from_lock)
                                    + " > 40) — RELOCKING (neighbor pre-warmed)")

                if _relock:
                    _lock_strikes(spot_ltp, dte, kite, expiry)
                    if not _is_initial_lock:
                        _v8_shadow_dt["relock_ts"] = time.time()
                        _shadow_analysis["CE"]["cross_buf"] = []
                        _shadow_analysis["PE"]["cross_buf"] = []
                        logger.info("[SHADOW-P1] Relock — cross_buf cleared")

                dir_strikes = {"CE": _locked_ce_strike, "PE": _locked_pe_strike}
                dir_tokens = dict(_locked_tokens)

                if not dir_tokens:
                    logger.warning("[MAIN] Locked tokens empty — forcing relock")
                    _lock_strikes(spot_ltp, dte, kite, expiry)
                    dir_tokens = dict(_locked_tokens)
                    dir_strikes = {"CE": _locked_ce_strike, "PE": _locked_pe_strike}
                    if not dir_tokens:
                        logger.warning("[MAIN] Relock failed — skipping cycle")
                        time.sleep(2)
                        continue

                all_results = {}
                best_result = None
                best_type = None
                best_opt_info = None

                _now_scan = datetime.now()
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

                # ── RE-ENTRY WATCHER (V7: 2-candle window) ──
                _re_armed = bool(state.get("_reentry_armed", False))
                if _re_armed and not state.get("in_trade"):
                    _re_dir   = str(state.get("_reentry_direction", "") or "")
                    _re_token = int(state.get("_reentry_token", 0) or 0)
                    _re_strike = int(state.get("_reentry_strike", 0) or 0)
                    _re_exit_epoch = float(state.get("_reentry_exit_ts", 0) or 0)
                    _re_attempts = int(state.get("_reentry_attempts", 0) or 0)
                    _re_last_checked = float(state.get("_reentry_last_checked_epoch", 0) or 0)
                    if _re_dir and _re_token and _re_exit_epoch > 0:
                        try:
                            _re_15m = D.add_indicators(
                                D.get_historical_data(_re_token, "15minute", 30))
                            if _re_15m is not None and len(_re_15m) >= 16:
                                _re_last = _re_15m.iloc[-2]
                                _re_close_dt = _re_last.name + timedelta(minutes=15)
                                _re_close_epoch = _re_close_dt.timestamp()
                                # Only check this candle once: must be after exit AND newer than last check
                                if (_re_close_epoch > _re_exit_epoch
                                    and _re_close_epoch > _re_last_checked):
                                    with _state_lock:
                                        state["_reentry_attempts"] = _re_attempts + 1
                                        state["_reentry_last_checked_epoch"] = _re_close_epoch
                                    _re_attempts += 1
                                    _re_result = check_entry(
                                        token=_re_token, option_type=_re_dir,
                                        spot_ltp=spot_ltp, dte=dte,
                                        expiry_date=expiry, kite=kite,
                                        silent=False, state=state)
                                    _passed = _re_result.get("fired", False)
                                    # Body gate for V7 re-entry: require ≥ 20% body (no doji confirmation)
                                    if _passed:
                                        _re_body = float(_re_result.get("body_pct", 0) or 0)
                                        if _re_body < 20:
                                            _passed = False
                                            _re_result["fired"] = False
                                            _re_result["reject_reason"] = f"reentry_weak_body_{_re_body}pct"
                                            logger.info(f"[REENTRY-V10] {_re_dir} body={_re_body}% < 20% — rejected")
                                    if not _passed:
                                        _why = _re_result.get("reject_reason", "?")
                                        if _re_attempts >= 2:
                                            # 2-candle window exhausted → disarm
                                            _tg_send(
                                                "🚫 <b>RE-ENTRY DROPPED</b>\n"
                                                + _re_dir + " " + str(_re_strike)
                                                + " — 2/2 candles failed (V7)\n"
                                                "Last reason: " + str(_why) + "\n"
                                                "Waiting for fresh setup."
                                            )
                                            logger.info("[REENTRY] window exhausted (2/2): " + str(_why))
                                            with _state_lock:
                                                state["_reentry_armed"] = False
                                                state["_reentry_exit_ts"] = 0.0
                                                state["_reentry_direction"] = ""
                                                state["_reentry_token"] = 0
                                                state["_reentry_strike"] = 0
                                                state["_reentry_attempts"] = 0
                                                state["_reentry_last_checked_epoch"] = 0.0
                                        else:
                                            _tg_send(
                                                "⏳ <b>RE-ENTRY ATTEMPT 1/2 FAILED</b>\n"
                                                + _re_dir + " " + str(_re_strike)
                                                + " — Reason: " + str(_why) + "\n"
                                                "Waiting next 15-min candle (1 more attempt)."
                                            )
                                            logger.info(f"[REENTRY] attempt {_re_attempts}/2 failed: " + str(_why))
                                    else:
                                        _re_result["entry_mode"] = "REENTRY"
                                        _re_result["_strike"] = _re_strike
                                        _re_result["_strike_label"] = "REENTRY"
                                        _re_oi = {"token": _re_token, "symbol": ""}
                                        try:
                                            for _k, _v in (_locked_tokens or {}).items():
                                                if int(_v.get("token", 0) or 0) == _re_token:
                                                    _re_oi = {"token": _re_token,
                                                              "symbol": _v.get("symbol", "")}; break
                                            if not _re_oi.get("symbol") and kite and expiry and _re_strike:
                                                _re_tk = D.get_option_tokens(kite, _re_strike, expiry) or {}
                                                _re_si = _re_tk.get(_re_dir) or {}
                                                if _re_si.get("symbol"):
                                                    _re_oi["symbol"] = _re_si.get("symbol", "")
                                        except Exception:
                                            pass
                                        _re_result["_symbol"] = _re_oi.get("symbol", "")
                                        try:
                                            _other_dt_re = "PE" if _re_dir == "CE" else "CE"
                                            _other_oi_re = (_locked_tokens or {}).get(_other_dt_re) or {}
                                            _other_tok_re = int(_other_oi_re.get("token", 0) or 0)
                                            if _other_tok_re:
                                                _other_3m_re = D.get_option_3min(_other_tok_re, lookback=10)
                                                _xl_re_info = evaluate_cross_leg(_re_dir, _other_3m_re)
                                                _re_result.update(_xl_re_info)
                                        except Exception as _xre:
                                            logger.debug("[XLEG][REENTRY] " + str(_xre))
                                        _xl_gate_re = bool(CFG.entry_ema9_band("xleg_gate_enabled", True))
                                        _xl_sig_re = _re_result.get("xleg_signal", "NA")
                                        if _xl_gate_re and _xl_sig_re == "FAIL":
                                            _tg_send(
                                                "🚫 <b>RE-ENTRY BLOCKED — X-LEG FAIL</b>\n"
                                                + _re_dir + " " + str(_re_strike) + " confirmation candle was good but x-leg said no."
                                            )
                                            with _state_lock:
                                                state["_reentry_armed"] = False
                                                state["_reentry_exit_ts"] = 0.0
                                                state["_reentry_direction"] = ""
                                                state["_reentry_token"] = 0
                                                state["_reentry_strike"] = 0
                                            continue
                                        _saved_lex = state.get("last_exit_direction", "")
                                        with _state_lock:
                                            state["last_exit_direction"] = ""
                                        _re_ltp_now = D.get_ltp(_re_token)
                                        if _re_ltp_now <= 0:
                                            _re_ltp_now = float(_re_last["close"])
                                        ok, why = pre_entry_checks(
                                            kite, _re_token, state,
                                            _re_ltp_now, profile, session,
                                            direction=_re_dir)
                                        if not (ok and _re_oi.get("symbol")):
                                            with _state_lock:
                                                state["last_exit_direction"] = _saved_lex
                                                state["_reentry_armed"] = False
                                                state["_reentry_exit_ts"] = 0.0
                                            logger.info("[REENTRY] pre-entry blocked: " + str(why))
                                            continue
                                        _re_close = float(_re_result.get("close", 0) or 0)
                                        _tg_send(
                                            "🔄 <b>V10 RE-ENTRY CONFIRMED " + _re_dir + " "
                                            + str(_re_strike) + "</b>\n"
                                            "Confirmation candle " + _re_close_dt.strftime("%H:%M")
                                            + ": GREEN body "
                                            + str(int(_re_result.get("body_pct", 0))) + "%\n"
                                            "Filling at candle close Rs" + "{:.2f}".format(_re_close)
                                        )
                                        # V5 CLOSE FILL — re-entry at candle close, no wait
                                        _re_result["entry_price"] = _re_close
                                        _re_result["entry_mode"]  = "CLOSE_FILL"
                                        logger.info("[CLOSE_FILL] RE-ENTRY " + _re_dir
                                                    + " at candle close Rs" + str(_re_close))
                                        with _state_lock:
                                            state["_reentry_armed"] = False
                                            state["_reentry_exit_ts"] = 0.0
                                            state["_reentry_direction"] = ""
                                            state["_reentry_token"] = 0
                                            state["_reentry_strike"] = 0
                                        _execute_entry(kite, _re_oi, _re_dir,
                                                       _re_result, profile,
                                                       expiry, dte, session)
                                        if state.get("in_trade"):
                                            D.mark_trade_taken(_re_dir)
                                            time.sleep(0.5)
                                            continue
                        except Exception as _ree:
                            import traceback as _tb_re
                            logger.error("[REENTRY] check error: " + str(_ree)
                                         + "\n" + _tb_re.format_exc())

                # V7 15-min check_entry scan removed — V10 P1/P2 handles all entries
                # V10 entry is handled above in the 10-second scan (outside 1-min gate)

                try:
                    vix_ltp = D.get_vix()
                except Exception:
                    vix_ltp = 0.0

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
                    # V5 CLOSE FILL — enter at candle close (body high of green candle).
                    # No pullback wait, no midpoint, no Option-B. Instant fill at close.
                    _entry_close_x = float(best_result.get("close", 0) or 0)
                    best_result["entry_price"] = _entry_close_x
                    best_result["entry_mode"]  = "CLOSE_FILL"
                    logger.info("[CLOSE_FILL] " + best_type + " entry at candle close Rs"
                                + str(_entry_close_x))

                if best_result and best_opt_info:
                    _xl_signal = "NA"
                    try:
                        _other_dt = "PE" if best_type == "CE" else "CE"
                        _other_oi = (_locked_tokens or {}).get(_other_dt) or {}
                        _other_tok_xl = int(_other_oi.get("token", 0) or 0)
                        if _other_tok_xl:
                            _other_3m_xl = D.get_option_3min(_other_tok_xl, lookback=10)
                            _xl_info = evaluate_cross_leg(best_type, _other_3m_xl)
                            best_result.update(_xl_info)
                            _xl_signal = _xl_info.get("xleg_signal", "NA")
                            logger.info(
                                "[XLEG] " + best_type + " entry — other "
                                + _other_dt + " close=" + str(_xl_info.get("xleg_other_close"))
                                + " ema9l=" + str(_xl_info.get("xleg_other_ema9l"))
                                + " margin=" + "{:+.2f}".format(_xl_info.get("xleg_other_margin", 0))
                                + " → " + str(_xl_signal)
                            )
                    except Exception as _xe:
                        logger.debug("[XLEG] " + str(_xe))

                    _xl_gate = bool(CFG.entry_ema9_band("xleg_gate_enabled", True))
                    if _xl_gate and _xl_signal == "FAIL":
                        _xl_other_dt = "PE" if best_type == "CE" else "CE"
                        _xl_margin = float(best_result.get("xleg_other_margin", 0) or 0)
                        _tg_send(
                            "🚫 <b>X-LEG GATE — entry blocked</b>\n"
                            + best_type + " " + str(best_result.get("_strike", 0))
                            + "  | " + _xl_other_dt + " holding "
                            + ("+" if _xl_margin >= 0 else "")
                            + "{:.1f}".format(_xl_margin)
                            + " above own EMA9L\n"
                            "Backtest: blocking FAIL trades = +56 pts/5d"
                        )
                        logger.info("[XLEG-GATE] " + best_type
                            + " blocked (FAIL signal) — waiting fresh setup")
                        best_result = None
                        best_opt_info = None
                    _execute_entry(kite, best_opt_info, best_type,
                                   best_result, profile, expiry, dte, session)
                    if state.get("in_trade"):
                        D.mark_trade_taken(best_type)

            if now.second % 10 < 2:
                _update_dashboard_ltp()

            # ── Live status dump every 5s — readable by any external script ──
            if now.second % 5 == 0:
                try:
                    _ce_info = (_locked_tokens or {}).get("CE", {})
                    _pe_info = (_locked_tokens or {}).get("PE", {})
                    _ce_tok  = int(_ce_info.get("token", 0) or 0)
                    _pe_tok  = int(_pe_info.get("token", 0) or 0)
                    _status  = {
                        "ts": now.strftime("%Y-%m-%d %H:%M:%S"),
                        "spot": round(D.get_spot_ltp(), 1),
                        "atm_strike": int(_ce_info.get("strike", 0) or 0),
                        "in_trade": bool(_v8_state.get("in_trade")),
                        "direction": _v8_state.get("direction", ""),
                        "entry_price": _v8_state.get("entry_price", 0),
                        "peak_pts": _v8_state.get("last_exit_peak", 0),
                        "daily_trades": _v8_state.get("daily_trades", 0),
                        "daily_pnl": _v8_state.get("daily_pnl", 0.0),
                        "daily_losses": _v8_state.get("daily_losses", 0),
                        "consecutive_losses": _v8_state.get("consecutive_losses", 0),
                        "CE": {
                            "strike": int(_ce_info.get("strike", 0) or 0),
                            "ltp": round(D.get_ltp(_ce_tok), 2) if _ce_tok else 0,
                        },
                        "PE": {
                            "strike": int(_pe_info.get("strike", 0) or 0),
                            "ltp": round(D.get_ltp(_pe_tok), 2) if _pe_tok else 0,
                        },
                    }
                    # add 3m indicators if available
                    for _sd, _stok in [("CE", _ce_tok), ("PE", _pe_tok)]:
                        if _stok:
                            try:
                                _df = D.add_indicators(D.get_historical_data(_stok, "3minute", 10))
                                if _df is not None and len(_df) >= 3:
                                    _r = _df.iloc[-2]
                                    _el = float(_r.get("ema9_low", 0))
                                    _eh = float(_r.get("ema9_high", 0))
                                    _status[_sd]["3m"] = {
                                        "close": round(float(_r["close"]), 2),
                                        "open":  round(float(_r["open"]), 2),
                                        "ema9l": round(_el, 2),
                                        "ema9h": round(_eh, 2),
                                        "bw":    round(_eh - _el, 2),
                                        "rsi":   round(float(_r.get("RSI", 0) or 0), 1),
                                    }
                            except Exception:
                                pass
                    with open("/home/vishalraajput24/state/vrl_status.json", "w") as _sf:
                        json.dump(_status, _sf, indent=2, default=str)
                except Exception:
                    pass

        except Exception as e:
            import traceback as _tb
            _tb_str = _tb.format_exc()
            logger.error("[MAIN] Loop error: " + str(e) + "\n" + _tb_str)
            time.sleep(2)

        # ── DELAY-ANALYSIS: snapshot LTP at +5s/+10s/+30s/+60s after P1/P2 signals ──
        try:
            _done_jobs = []
            for _dj in _delay_jobs:
                _elapsed = time.time() - _dj["fire_ts"]
                _tok     = _dj["tok"]
                for _delay in (5, 10, 30, 60):
                    if _dj["snaps"][_delay] is None and _elapsed >= _delay:
                        _snap_ltp  = D.get_ltp(_tok)
                        _snap_spot = D.get_ltp(D.NIFTY_SPOT_TOKEN)
                        _dj["snaps"][_delay]      = _snap_ltp  if _snap_ltp  else 0.0
                        _dj["spot_snaps"][_delay] = _snap_spot if _snap_spot else 0.0
                # Timeout: discard jobs older than 2 min (prevent memory leak if LTP fails)
                if _elapsed > 120 and not all(v is not None for v in _dj["snaps"].values()):
                    _done_jobs.append(_dj); continue
                if all(v is not None for v in _dj["snaps"].values()):
                    _b   = _dj["base"]
                    _sb  = _dj.get("spot_base", 0.0)
                    _s5  = _dj["snaps"][5];  _sp5  = _dj["spot_snaps"][5]
                    _s10 = _dj["snaps"][10]; _sp10 = _dj["spot_snaps"][10]
                    _s30 = _dj["snaps"][30]; _sp30 = _dj["spot_snaps"][30]
                    _s60 = _dj["snaps"][60]; _sp60 = _dj["spot_snaps"][60]
                    logger.info(
                        f"[DELAY-ANALYSIS] {_dj['label']} {_dj['strike']} "
                        f"base={_b:.1f} spot_base={_sb:.0f} "
                        f"+5s=opt{_s5:.1f}({_s5-_b:+.1f})spot{_sp5:.0f}({_sp5-_sb:+.0f}) "
                        f"+10s=opt{_s10:.1f}({_s10-_b:+.1f})spot{_sp10:.0f}({_sp10-_sb:+.0f}) "
                        f"+30s=opt{_s30:.1f}({_s30-_b:+.1f})spot{_sp30:.0f}({_sp30-_sb:+.0f}) "
                        f"+60s=opt{_s60:.1f}({_s60-_b:+.1f})spot{_sp60:.0f}({_sp60-_sb:+.0f})"
                    )
                    _done_jobs.append(_dj)
            for _dj in _done_jobs:
                _delay_jobs.remove(_dj)
        except Exception as _dae:
            logger.debug("[DELAY-ANALYSIS] error: " + str(_dae))

        time.sleep(1)


# ═══════════════════════════════════════════════════════════════
# === TELEGRAM COMMANDS (merged from VRL_COMMANDS) ===
# ═══════════════════════════════════════════════════════════════

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
    """🩺 Doctor's pulse check — single-shot diagnostic dump."""
    try:
        now = datetime.now()
        _up_secs = int(time.time() - _BOT_START_TS)
        _up_h = _up_secs // 3600
        _up_m = (_up_secs % 3600) // 60
        _up_str = (str(_up_h) + "h " if _up_h else "") + str(_up_m) + "m"

        _spot = D.get_ltp(D.NIFTY_SPOT_TOKEN)
        _spot_live = D.is_tick_live(D.NIFTY_SPOT_TOKEN)
        with D._tick_lock:
            _se = D._ticks.get(int(D.NIFTY_SPOT_TOKEN))
        _tick_age = int(time.time() - _se["ts"]) if _se else -1
        _market = D.is_market_open()
        _acct = D.get_account_info() if hasattr(D, "get_account_info") else {}
        _user = _acct.get("name", "?")
        _lot = D.get_lot_size()

        try:
            _trades_today = _read_today_trades() if "_read_today_trades" in globals() else []
        except Exception:
            _trades_today = []
        _td_pnl = sum(float(t.get("pnl_pts", 0) or 0) for t in _trades_today)
        _td_wins = sum(1 for t in _trades_today if float(t.get("pnl_pts", 0) or 0) > 0)
        _td_loss = len(_trades_today) - _td_wins
        _last_t = _trades_today[-1] if _trades_today else None

        _v8_in_trade = _v8_state.get("in_trade", False)
        _v8_pos_str = ""
        if _v8_in_trade:
            _v8_ep  = float(_v8_state.get("entry_price", 0) or 0)
            _v8_tok = int(_v8_state.get("token", 0) or 0)
            _v8_ltp = D.get_ltp(_v8_tok) if _v8_tok else 0
            _v8_pn  = round(_v8_ltp - _v8_ep, 1) if _v8_ltp else 0
            _v8_pk  = float(_v8_state.get("peak_pnl", 0) or 0)
            _v8_tier = _v8_state.get("active_ratchet_tier", "INITIAL") or "INITIAL"
            _v8_sl  = float(_v8_state.get("active_ratchet_sl", 0) or 0)
            if _v8_sl <= 0: _v8_sl = round(_v8_ep - 12, 2)
            _v8_lock = round(_v8_sl - _v8_ep, 1)
            _v8_room = round(_v8_ltp - _v8_sl, 1) if _v8_ltp else 0
            _v8_dir_emj = "🟢" if _v8_state.get("direction") == "CE" else "🔴"
            _v8_sym = _v8_state.get("direction", "") + " " + str(_v8_state.get("strike", ""))
            _v8_pos_str = (
                "[V10] " + _v8_dir_emj + " " + _v8_sym + "  "
                + ("+" if _v8_pn >= 0 else "") + str(_v8_pn) + "pts\n"
                + "Entry Rs" + str(_v8_ep) + " → Rs" + str(round(_v8_ltp, 2))
                + " · Peak +" + str(_v8_pk) + "\n"
                + "Tier: " + _v8_tier + " @ Rs" + str(round(_v8_sl, 2))
                + " (Lock " + ("+" if _v8_lock >= 0 else "") + str(_v8_lock)
                + " · Room " + ("+" if _v8_room >= 0 else "") + str(_v8_room) + ")"
            )

        _ce_lck = _locked_ce_strike or "?"
        _pe_lck = _locked_pe_strike or "?"
        _last_scan = state.get("_last_scan_minute", "?")

        _cd = CFG.entry_ema9_band("cooldown_minutes", 5) if hasattr(CFG, "entry_ema9_band") else 5

        _err_lines = []
        try:
            _err_path = os.path.join(D.ERROR_LOG_DIR, date.today().strftime("%Y-%m-%d") + ".log")
            if os.path.isfile(_err_path):
                with open(_err_path) as _f:
                    _err_lines = [ln.strip() for ln in _f.readlines()[-5:]]
        except Exception:
            pass

        def _ok(b): return "✅" if b else "❌"
        if _market:
            _market_icon = "✅"; _market_str = "OPEN"
        else:
            _market_icon = "💤"; _market_str = "CLOSED (idle until 09:15 IST)"
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
            + "🕐 V10: " + str(len(_trades_today)) + " trades · "
            + str(_td_wins) + "W " + str(_td_loss) + "L · "
            + ("+" if _td_pnl >= 0 else "") + "{:.1f}".format(_td_pnl) + " pts\n"
            + "⚡ V10: "
            + str(_v8_state.get("_trades_today", 0)) + " trades · "
            + str(_v8_state.get("_wins_today", 0)) + "W "
            + str(_v8_state.get("_losses_today", 0)) + "L · "
            + ("+" if _v8_state.get("_pnl_today_pts", 0) >= 0 else "")
            + "{:.1f}".format(_v8_state.get("_pnl_today_pts", 0)) + " pts"
            + (" | V8 active: " + str(_v8_state.get("direction", "")) + " "
               + str(_v8_state.get("strike", ""))
               + " peak +" + "{:.1f}".format(_v8_state.get("peak_pnl", 0))
               if _v8_state.get("in_trade") else "")
            + "\n"
            + ("Last: " + str(_last_t.get("entry_time", "?")) + " "
               + str(_last_t.get("direction", "?")) + " "
               + str(_last_t.get("strike", "?")) + " "
               + ("+" if float(_last_t.get("pnl_pts", 0) or 0) >= 0 else "")
               + str(_last_t.get("pnl_pts", "?")) + " ("
               + str(_last_t.get("exit_reason", "?")) + ")\n" if _last_t else "")
            + "━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "<b>POSITION</b>\n"
            + (_v8_pos_str + "\n" if _v8_in_trade else "—\n")
            + "━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "<b>ENGINE</b>\n"
            + ("Locked: CE " + str(_ce_lck) + " · PE " + str(_pe_lck) + "\n"
               + "Last scan: " + str(_last_scan) + "\n"
               + "Bias: " + str(state.get("daily_bias", "?")) + "\n"
               if _market else "💤 awaiting market open\n")
            + "━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "<b>V10 CONFIG (1-min P1+P2)</b>\n"
            "EMA9H gap ≥ " + str(V10_MIN_EMA9H_GAP) + "  "
            + "RSI " + str(V10_RSI_MIN) + "–" + str(V10_RSI_MAX) + " rising  "
            + "BW ≥ " + str(V10_BW_MIN) + "\n"
            "XLEG_CONFIRMED + LTP on correct VWAP side\n"
            "VWAP dist gate: " + ("OFF" if V10_NEAR_VWAP_MAX == 0 else "≤" + str(V10_NEAR_VWAP_MAX)) + "\n"
            "Cooldown: " + str(_cd) + "min BOTH sides\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "<b>V10 SL LADDER</b>\n"
            "INITIAL  peak<12   entry-12\n"
            "LOCK_4   peak≥12   entry+4\n"
            "LOCK_12  peak≥24   entry+12\n"
            "LOCK_20  peak≥30   entry+20\n"
            "LOCK_30  peak≥36   entry+30\n"
            "LOCK_36  peak≥40   entry+36\n"
            "LOCK_50  peak≥50   entry+50\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "<b>ERRORS</b> (today, last 5)\n"
            + (_ok(False) + " " + str(len(_err_lines)) + " errors\n<pre>"
               + "\n".join(ln[:100] for ln in _err_lines) + "</pre>"
               if _err_lines else _ok(True) + " None\n")
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
        ""  # /xleg removed
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "<b>TRADING</b>\n"
        "/status    — trade status + PNL\n"
        "/trades    — today's trade list\n"
        "/account   — balance + margin info\n"
        "/vishal_stock_fno — F&O positions live P&L\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "<b>DATA</b>\n"
        "/download  — full day zip (or /download YYYY-MM-DD)\n"
        "/livecheck — last 50 log lines\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "<b>CONTROL</b>\n"
        "/pause      — block new entries\n"
        "/resume     — re-enable entries\n"
        "/forceexit  — emergency exit all lots\n"
        "/deploy     — git pull main + restart\n"
        "/restart    — restart bot (no pull)\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "VISHAL RAJPUT TRADE v20 — V10 live 1-min P1+P2, "
        "exit chain (Emergency SL / EOD 15:20 / Vishal Trail), "
        + ("PAPER" if D.PAPER_MODE else "LIVE") + " 2 lots.\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "🌐 Dashboard: http://" + _WEB_IP + ":8080"
    )


def _cmd_status(args):
    global _kite
    # V10 is the live strategy — read _v8_state, not V7 state
    with _v8_lock:
        st = dict(_v8_state)
    with _post_exit_lock:
        _post_n = len(_post_exit_observation)
    _post_exit_line = ""
    if _post_n:
        _post_exit_line = ("Post-exit watching: " + str(_post_n)
                           + " token(s)\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n")

    if not st.get("in_trade"):
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
        _day_pts = round(st.get("_pnl_today_pts", 0), 1)
        _day_w   = st.get("_wins_today", 0)
        _day_l   = st.get("_losses_today", 0)
        _tg_send(
            "📊 <b>STATUS — NO TRADE</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            + _warmup_line +
            _post_exit_line +
            "Day PNL: " + ("+" if _day_pts >= 0 else "") + str(_day_pts) + "pts  "
            + str(_day_w) + "W " + str(_day_l) + "L\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "Bot     : V10 scanning"
        )
        return

    qty   = int(st.get("qty", 0) or D.get_lot_size() * 2)
    token = int(st.get("token", 0) or 0)
    ltp   = 0.0
    try:
        ltp = D.get_ltp(token)
        if ltp <= 0 and _kite is not None:
            symbol = st.get("symbol")
            if symbol:
                q = _kite.ltp(["NFO:" + symbol])
                ltp = float(q["NFO:" + symbol]["last_price"])
                logger.info("[STATUS] LTP via REST: " + str(ltp))
    except Exception as e:
        logger.warning("[STATUS] LTP fetch error: " + str(e))
        ltp = 0.0

    entry = float(st.get("entry_price", 0))
    pnl   = round(ltp - entry, 1) if ltp > 0 else 0
    peak  = float(st.get("peak_pnl", 0))
    pnl_rs = round(pnl * qty, 0)
    pnl_rs_str = ("+" if pnl_rs >= 0 else "") + "₹" + "{:,.0f}".format(pnl_rs)

    _tier = st.get("active_ratchet_tier", "INITIAL")
    _rsl  = float(st.get("active_ratchet_sl", 0) or 0)
    if _tier and _tier not in ("", "None", "INITIAL") and _rsl > 0:
        _stop_line = "Trail  : " + _tier + " @ ₹" + str(round(_rsl, 1))
        _stop_dist = round(ltp - _rsl, 1) if ltp > 0 else "—"
    else:
        _init_sl   = round(entry - 12, 1)
        _stop_line = "Trail  : INITIAL @ ₹" + str(_init_sl)
        _stop_dist = round(ltp - _init_sl, 1) if ltp > 0 else "—"

    _day_pts = round(st.get("_pnl_today_pts", 0), 1)
    _day_w   = st.get("_wins_today", 0)
    _day_l   = st.get("_losses_today", 0)

    _tg_send(
        "📊 <b>STATUS — IN TRADE</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "Time   : " + _now_str() + "\n"
        "Symbol : " + st.get("symbol", "") + "\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "Entry  : ₹" + str(round(entry, 2)) + "\n"
        "LTP    : ₹" + str(round(ltp, 2)) + "\n"
        "PNL    : " + ("+" if pnl >= 0 else "") + str(pnl) + "pts  " + pnl_rs_str + "\n"
        "Peak   : +" + str(round(peak, 1)) + "pts\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        + _stop_line + "  (" + str(_stop_dist) + "pts away)\n"
        "Ladder : @+12→LOCK_4  @+18→LOCK_10  @+24→LOCK_12\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        + _post_exit_line +
        "Day PNL: " + ("+" if _day_pts >= 0 else "") + str(_day_pts) + "pts  "
        + str(_day_w) + "W " + str(_day_l) + "L"
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
    v7_open = False
    v8_open = False
    with _state_lock:
        if state.get("in_trade"):
            state["force_exit"] = True
            v7_open = True
    _v8_tok = 0
    _v8_entry_px = 0.0
    with _v8_lock:
        if _v8_state.get("in_trade"):
            v8_open = True
            _v8_tok = int(_v8_state.get("token", 0) or 0)
            _v8_entry_px = float(_v8_state.get("entry_price", 0) or 0)
            _v8_state["_force_exit_ts"] = time.time()  # BUG-C fix: arm 3-min re-entry cooldown

    # Close any active shadow P1/P2 signals
    _shadow_closed = []
    for _sd, _sd_label in [(_v8_shadow_dt, "P1"), (_v8_shadow_p2, "P2")]:
        for _sdir in ("CE", "PE"):
            _sds = _sd[_sdir]
            if _sds.get("active"):
                _stok = int(_sds.get("entry_tok", 0) or 0)
                _sep  = float(_sds.get("entry_price", 0) or 0)
                _sltp = D.get_ltp(_stok) if _stok else 0
                _exit_px = _sltp if _sltp > 0 else _sep
                _spnl = round(_exit_px - _sep, 1)
                _speak = round(_sds.get("peak_pts", 0), 1)
                _slvl = _sds.get("shadow_level", "INITIAL")
                pass  # shadow force-exit TG removed
                _sds.update({
                    "active": False, "entry_price": 0.0, "entry_time": "",
                    "peak_price": 0.0, "peak_pts": 0.0,
                    "shadow_sl": 0.0, "shadow_level": "INITIAL",
                    "last_exit_pnl": _spnl, "last_exit_reason": "FORCE-EXIT",
                    "last_exit_ts": time.time(),
                })
                _shadow_closed.append(f"{_sd_label}-{_sdir}")
                logger.warning(f"[CTRL] Force exit shadow {_sd_label} {_sdir} pnl={_spnl:+.1f}")
    if _shadow_closed:
        _save_shadow_state()

    if not v7_open and not v8_open and not _shadow_closed:
        _tg_send("No open trade.")
        return
    if v7_open or v8_open:
        _tg_send("🚨 Force exit triggered.")
        logger.warning("[CTRL] Force exit")
    if v8_open:
        _ltp = D.get_ltp(_v8_tok) if _v8_tok else 0
        if _ltp <= 0:
            _ltp = _v8_entry_px
        _v8_execute_paper_exit("FORCE_EXIT", round(_ltp, 2))


def _cmd_deploy(args):
    import subprocess
    _cwd = os.path.dirname(os.path.abspath(__file__))

    def _run(cmd):
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=30, cwd=_cwd)
        combined = (r.stdout + r.stderr).strip()
        return combined, r.returncode

    _tg_send("📦 Pulling latest from main...")

    # capture commit hash before pull
    before_sha, _ = _run(["git", "rev-parse", "--short", "HEAD"])

    fetch_out, rc = _run(["git", "fetch", "origin", "main"])
    if rc != 0:
        _tg_send("❌ Fetch failed:\n<pre>" + fetch_out[-600:] + "</pre>")
        return
    reset_out, rc = _run(["git", "reset", "--hard", "origin/main"])
    if rc != 0:
        _tg_send("❌ Reset failed:\n<pre>" + reset_out[-600:] + "</pre>")
        return

    after_sha, _ = _run(["git", "rev-parse", "--short", "HEAD"])

    if before_sha == after_sha:
        _tg_send("✅ Already up to date (no changes).\nSHA: " + after_sha + "\n🔄 Restarting...")
    else:
        commits, _ = _run(["git", "log", before_sha + ".." + after_sha,
                            "--oneline", "--no-decorate"])
        files, _   = _run(["git", "diff", "--name-only", before_sha, after_sha])
        _tg_send(
            "✅ <b>DEPLOY SUMMARY</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "<b>SHA</b>  " + before_sha + " → " + after_sha + "\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "<b>Changes</b>\n<pre>" + (commits[:600] if commits else "—") + "</pre>\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "<b>Files</b>\n<pre>" + (files[:300] if files else "—") + "</pre>\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "🔄 Restarting now..."
        )

    logger.info(f"[CTRL] Deploy: {before_sha} -> {after_sha}, restarting")
    _remove_pid()
    time.sleep(2)
    os.execv(sys.executable, [sys.executable] + sys.argv)


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


def _read_today_shadow_trades() -> list:
    """Parse shadow trade entries+exits from today's log file."""
    import re as _re
    today_str = date.today().strftime("%Y-%m-%d")
    log_path  = D.LIVE_LOG_FILE if hasattr(D, 'LIVE_LOG_FILE') else os.path.expanduser("~/logs/live/vrl_live.log")
    signals   = {}   # key=(strat,dir,entry) → dict
    results   = []
    try:
        with open(log_path) as fh:
            for raw in fh:
                if today_str not in raw:
                    continue
                t_match = _re.match(r'\d{4}-\d{2}-\d{2} (\d{2}:\d{2}:\d{2})', raw)
                t_str   = t_match.group(1) if t_match else ''
                # SIGNAL lines
                m = _re.search(
                    r'\[(SHADOW-P[12])\] (\w+) \d+ SIGNAL entry=([\d.]+)', raw)
                if m:
                    strat, dir_, entry = m.group(1), m.group(2), float(m.group(3))
                    key = (strat, dir_, entry)
                    signals[key] = {'strat': strat, 'dir': dir_, 'entry': entry,
                                    'entry_time': t_str, 'exit': None, 'exit_time': None,
                                    'pnl': None, 'peak': None, 'reason': None}
                    continue
                # EXIT lines (V1 only — no 'V2' in line)
                if 'V2' not in raw:
                    m = _re.search(
                        r'\[(SHADOW-P[12])\] (\w+) (SL-HIT|PROFIT|TARGET\+\d+|EOD) '
                        r'entry=([\d.]+) exit=([\d.]+) pnl=([+\-\d.]+) peak=\+?([\d.]+)', raw)
                    if m:
                        strat, dir_, reason = m.group(1), m.group(2), m.group(3)
                        entry, exit_, pnl, peak = (float(m.group(4)), float(m.group(5)),
                                                   float(m.group(6)), float(m.group(7)))
                        key = (strat, dir_, entry)
                        if key in signals:
                            signals[key].update(exit=exit_, exit_time=t_str,
                                                pnl=pnl, peak=peak, reason=reason)
                            results.append(signals.pop(key))
    except Exception as e:
        logger.error("[CTRL] Shadow trades parse error: " + str(e))
    # Append still-open signals
    for key, sig in signals.items():
        results.append(sig)
    results.sort(key=lambda x: x['entry_time'])
    return results


def _cmd_trades(args):
    live_trades   = _read_today_trades()
    shadow_trades = _read_today_shadow_trades()

    if not live_trades and not shadow_trades:
        _tg_send("📒 No trades today.")
        return

    lines = ""
    total = 0.0
    idx   = 1

    # Live/paper V10 trades from CSV
    for t in live_trades:
        pts  = float(t.get("pnl_pts", 0))
        total += pts
        sign = "+" if pts >= 0 else ""
        icon = "✅" if pts >= 0 else "❌"
        peak = float(t.get("peak_pnl", 0))
        captured = round(pts / peak * 100) if peak > 0 else 0
        lines += (
            icon + " <b>V10 Trade " + str(idx) + "</b>  " + t.get("direction", "") + "\n"
            "  " + t.get("entry_time", "") + " → " + t.get("exit_time", "") + "\n"
            "  Entry: ₹" + str(t.get("entry_price", "")) + " → Exit: ₹" + str(t.get("exit_price", "")) + "\n"
            "  PNL: " + sign + str(round(pts, 1)) + "pts\n"
            "  Peak: +" + str(round(peak, 1)) + "pts  Captured: " + str(captured) + "%\n"
            "  Reason: " + t.get("exit_reason", "") + "\n"
        )
        idx += 1

    # Shadow trades from log
    shadow_total = 0.0
    for t in shadow_trades:
        pnl  = t.get('pnl')
        peak = t.get('peak') or 0.0
        is_open = pnl is None
        if not is_open:
            shadow_total += pnl
        icon = "🟢" if is_open else ("✅" if pnl > 0 else "❌")
        strat_label = t['strat'].replace('SHADOW-', '')
        exit_t = t.get('exit_time') or '—'
        pnl_str = ("🟢 open" if is_open else
                   ("+" if pnl >= 0 else "") + str(round(pnl, 1)) + "pts")
        peak_str = ("+?" if is_open else "+" + str(round(peak, 1))) + "pts"
        lines += (
            icon + " <b>" + strat_label + " S" + str(idx) + "</b>  " + t['dir'] + "\n"
            "  " + t['entry_time'] + " → " + exit_t + "\n"
            "  Entry: " + str(t['entry']) + "  Exit: " + (str(t.get('exit') or '—') + "\n"
            "  PNL: " + pnl_str + "  Peak: " + peak_str + "\n"
            "  Reason: " + (t.get('reason') or 'open') + "\n")
        )
        idx += 1

    total += shadow_total
    sign = "+" if total >= 0 else ""
    _tg_send(
        "📒 <b>TODAY'S TRADES</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        + lines
        + "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "Shadow Net: " + ("+" if shadow_total >= 0 else "") + str(round(shadow_total, 1)) + "pts  "
        "| Total: " + sign + str(round(total, 1)) + "pts"
    )


def _cmd_vishal_stock_fno(args):
    try:
        import csv as _csv
        _tracker = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "screener", "fno_tracker.csv"
        )
        if not os.path.isfile(_tracker):
            _tg_send("📭 F&O tracker not found.")
            return

        with open(_tracker) as _f:
            rows = list(_csv.DictReader(_f))

        # Show OPEN + T1-HIT + SL-HIT rows (all active positions)
        active_rows = [r for r in rows if
                       str(r.get("status","")).startswith("OPEN") or
                       "HIT" in str(r.get("status",""))]
        if not active_rows:
            _tg_send("📭 No F&O positions.")
            return

        lines = ""
        total_pnl = 0.0
        open_count = 0
        for r in active_rows:
            st       = str(r.get("status",""))
            is_open  = st.startswith("OPEN")
            is_t1    = "T1-HIT" in st or "T3-HIT" in st
            is_sl    = "SL-HIT" in st
            if is_open: open_count += 1
            # Use pre-calculated CSV values — correct for any lot count
            ltp      = float(r.get("current_premium") or r.get("entry_premium") or 0)
            entry    = float(r.get("entry_premium") or 0)
            sl       = float(r.get("sl_premium") or 0)
            t1       = float(r.get("t1_premium") or 0)
            pnl_pct  = float(r.get("current_return_pct") or 0)
            pnl_rs   = float(r.get("pnl_rs") or 0)
            total_pnl += pnl_rs
            sign     = "+" if pnl_rs >= 0 else ""
            dist_sl  = round(ltp - sl, 2)
            dist_t1  = round(t1 - ltp, 2)
            if is_t1:   icon = "🎯"
            elif is_sl: icon = "❌"
            elif pnl_rs >= 0: icon = "✅"
            else: icon = "⚠️"
            lines += (
                icon + " <b>" + r["symbol"] + " " + r["direction"] + "</b>"
                + (" <i>" + st + "</i>" if not is_open else "") + "\n"
                "  Entry ₹" + str(round(entry,2)) + " → Now ₹" + str(round(ltp,2)) + "\n"
                "  P&L: " + sign + str(round(pnl_pct,1)) + "%  " + sign + "₹" + str(int(pnl_rs)) + "\n"
                + ("  SL ₹" + str(sl) + " (" + str(dist_sl) + " away)  T1 ₹" + str(t1) + " (" + str(dist_t1) + " away)\n" if is_open else "")
            )

        total_sign = "+" if total_pnl >= 0 else ""
        _tg_send(
            "📊 <b>F&O POSITIONS</b> · " + str(open_count) + " open\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            + lines +
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "Total P&L: " + total_sign + "₹" + str(int(total_pnl)) + "\n"
            "Updated: " + _now_str()
        )
    except Exception as e:
        _tg_send("📊 F&O error: " + str(e))
        logger.error("[FNO_CMD] " + str(e))


_DISPATCH = {
    "/help"               : _cmd_help,
    "/pulse"              : _cmd_pulse,
    "/status"             : _cmd_status,
    "/trades"             : _cmd_trades,
    "/account"            : _cmd_account,
    "/pause"              : _cmd_pause,
    "/resume"             : _cmd_resume,
    "/forceexit"          : _cmd_forceexit,
    "/deploy"             : _cmd_deploy,
    "/restart"            : _cmd_restart,
    "/livecheck"          : _cmd_livecheck,
    "/download"           : _cmd_download,
    "/vishal_stock_fno"   : _cmd_vishal_stock_fno,
}


# ═══════════════════════════════════════════════════════════════
# === TRADE EXECUTION (merged from VRL_TRADE) ===
# ═══════════════════════════════════════════════════════════════

def _verify_timeout(kind: str, default: int) -> int:
    try:
        v = (CFG.get().get("trade") or {}).get("verify_timeout_" + kind)
        if v is not None:
            return int(v)
    except Exception:
        pass
    return default


# verify_order_fill(kite, ...) removed — orders now via MStock.


def place_entry(kite, symbol: str, token: int,
                option_type: str, qty: int,
                entry_price_ref: float) -> dict:
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
                + " buffer=" + str(buffer) + " limit=" + str(limit_price)
                + " broker=MStock")

    mc     = MSTOCK.get_mstock()
    result = MSTOCK.ms_place_buy(mc, symbol, qty, limit_price,
                                 timeout_secs=_verify_timeout("entry", 8))

    if result["ok"] and not os.path.isfile(_first_live_flag):
        try:
            with open(_first_live_flag, "w") as _f:
                _f.write(datetime.now().isoformat())
        except Exception:
            pass

    if result["ok"] and result["fill_qty"] < qty:
        logger.critical("[TRADE] Partial fill REJECTED: "
                        + str(result["fill_qty"]) + "/" + str(qty)
                        + " — " + symbol + ". MANUAL CHECK REQUIRED.")
        return {"ok": False, "fill_price": 0.0, "fill_qty": 0,
                "order_id": result.get("order_id", ""), "error": "PARTIAL_FILL_REJECTED", "slippage": 0}

    # Re-compute slippage relative to original ref price (not limit price)
    if result["ok"]:
        result["slippage"] = round(result["fill_price"] - entry_price_ref, 2)

    return result


def place_exit(kite, symbol: str, token: int,
               option_type: str, qty: int,
               exit_price_ref: float, reason: str) -> dict:
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

    logger.info("[TRADE] MARKET EXIT: " + symbol
                + " qty=" + str(qty) + " reason=" + reason + " broker=MStock")

    mc     = MSTOCK.get_mstock()
    result = MSTOCK.ms_place_sell(mc, symbol, qty,
                                  timeout_secs=_verify_timeout("exit", 8))

    if result["ok"]:
        result["slippage"] = round(exit_price_ref - result["fill_price"], 2)
        return result

    # First attempt failed — retry with exponential backoff (3 attempts total)
    for _retry in range(2, 4):
        _wait = 2 ** (_retry - 1)  # 2s, 4s
        logger.warning(f"[TRADE] Exit attempt {_retry-1} failed — retry {_retry} in {_wait}s")
        time.sleep(_wait)
        result = MSTOCK.ms_place_sell(mc, symbol, qty,
                                      timeout_secs=_verify_timeout("exit", 8))
        if result["ok"]:
            result["slippage"] = round(exit_price_ref - result["fill_price"], 2)
            return result

    # MStock is the only broker for orders — Kite is data-only
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
    logger.info(f"[TG-CMD] {raw_cmd}" + (f" {' '.join(args)}" if args else ""))
    handler = _DISPATCH.get(raw_cmd)
    if handler:
        handler(args)
    else:
        _WATCHDOG = ("/deploy","/serverstatus","/serverlog","/gitlog")
        if raw_cmd not in _WATCHDOG:
            _tg_send("Unknown command: " + raw_cmd + "\nType /help")

def _tg_handle_callback(callback: dict):
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
            time.sleep(1.5)
        except Exception as _tge:
            logger.debug("[MAIN] Shutdown telegram send failed: " + str(_tge))
    _save_state()
    _remove_pid()
    time.sleep(0.5)  # ensure pending TG messages flush
    logger.info("[MAIN] Clean shutdown")
    sys.exit(0)

# ═══════════════════════════════════════════════════════════════
#  ENTRY POINT
# ═══════════════════════════════════════════════════════════════

def main():
    global _kite
    logger.info("[MAIN] ═══ VISHAL RAJPUT TRADE " + D.VERSION + " STARTING ═══")
    _mode_str = "PAPER" if D.PAPER_MODE else "LIVE"
    logger.info("[MAIN] Mode: " + _mode_str)
    _tg_send(
        ("🟡 <b>Bot starting in PAPER mode</b>" if D.PAPER_MODE else "🟢 <b>Bot starting in LIVE mode</b>")
        + "\nVersion: " + D.VERSION
        + "\nMode: <b>" + _mode_str + "</b>"
        + ("\n⚠️ Real orders will be placed!" if not D.PAPER_MODE else ""),
        priority="critical"
    )
    logger.info("[MAIN] Scalps: DISABLED (data-backed decision)")

    _write_pid()
    signal.signal(signal.SIGINT,  _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)
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

    # Phase 1 health: Token + REST spot (before WS starts — WS check happens after start_websocket)
    _health_lines_pre = []
    _health_ok_pre = True
    try:
        _prof = kite.profile()
        _health_lines_pre.append("Token: ✅ " + str(_prof.get("user_name", "?")))
    except Exception as _he:
        _health_lines_pre.append("Token: ❌ " + str(_he)[:60])
        _health_ok_pre = False
    try:
        _sq = kite.ltp(["NSE:NIFTY 50"])
        _sp = float(list(_sq.values())[0]["last_price"])
        _health_lines_pre.append("Spot: ✅ " + str(round(_sp, 1)))
    except Exception as _he:
        _health_lines_pre.append("Spot: ❌ " + str(_he)[:60])
        _health_ok_pre = False

    try:
        D.set_autoheal_callback(_tg_send)
    except Exception:
        pass

    try:
        D.fetch_account_info(kite)
    except Exception:
        pass

    live_lot_size = D.get_lot_size(kite)
    D.LOT_SIZE    = live_lot_size
    logger.info("[MAIN] Lot size from broker: " + str(live_lot_size))

    _load_state()
    _load_v8_state()
    _load_shadow_state()
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

    try:
        D.cleanup_old_lab_data()
    except Exception as e:
        logger.warning("[MAIN] Lab cleanup failed: " + str(e))
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
                    raw_rows = list(_trade_csv_reader(f))

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
                state["daily_pnl"] = round(pnl, 2)

            logger.info("[MAIN] Restored: " + str(len(trades_today))
                        + " trades | " + str(len(wins)) + "W / " + str(len(losses)) + "L | pnl="
                        + str(round(pnl,1)) + "pts")
        else:
            logger.info("[MAIN] No trades found for today — starting fresh")
    except Exception as e:
        logger.warning("[MAIN] Trade log restore failed: " + str(e))
    D.start_websocket()
    D.subscribe_tokens([D.NIFTY_SPOT_TOKEN, D.INDIA_VIX_TOKEN])
    time.sleep(2)

    # Phase 2 health: WS tick check (runs after WS is started + subscribed)
    try:
        import time as _time_h
        _ws_ltp = 0.0
        _market_open_now = D.is_market_open()
        _health_lines_ws = list(_health_lines_pre)
        _health_ok_ws = _health_ok_pre
        if _market_open_now:
            for _ in range(30):  # up to 30s for WS to connect and deliver first tick
                _ws_ltp = D.get_ltp(D.NIFTY_SPOT_TOKEN)
                if _ws_ltp > 0:
                    break
                _time_h.sleep(1)
            if _ws_ltp > 0:
                _health_lines_ws.append("WS: ✅ tick=" + str(round(_ws_ltp, 1)))
            else:
                _health_lines_ws.append("WS: ⚠️ no tick after 30s (feed may be down)")
                _health_ok_ws = False
        else:
            with D._tick_lock:
                _entry = D._ticks.get(int(D.NIFTY_SPOT_TOKEN))
            if _entry:
                _age_min = int((_time_h.time() - _entry["ts"]) / 60)
                _health_lines_ws.append(
                    "WS: 💤 market closed (last tick "
                    + str(_age_min) + "m ago at "
                    + str(round(_entry["ltp"], 1)) + ")"
                )
            else:
                _health_lines_ws.append("WS: 💤 market closed (no ticks yet)")
        _icon = "✅" if _health_ok_ws else "⚠️"
        _tg_send(
            _icon + " <b>TOKEN HEALTH CHECK</b>\n"
            + "\n".join(_health_lines_ws) + "\n"
            + "Time: " + datetime.now().strftime("%H:%M:%S IST")
        )
        logger.info("[MAIN] Token health: " + (" | ".join(_health_lines_ws)))
    except Exception as _the:
        logger.warning("[MAIN] Token health check error: " + str(_the))

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

    # ── Compute institutional levels (shadow mode — no live blocking) ──
    try:
        LEVELS.compute_today(D, kite, None)
    except Exception as _le:
        logger.warning(f"[LEVELS] startup compute failed: {_le}")

    # ── VWAP startup compute ──────────────────────────────────────
    try:
        LEVELS.update_vwap(kite)
    except Exception as _ve:
        logger.warning(f"[VWAP] startup compute failed: {_ve}")

    logger.info("[MAIN] All systems ready. Strategy loop starting.")
    _cmd_help([])

    # ── Start web dashboard as daemon thread ─────────────────────
    try:
        threading.Thread(target=_start_web_server, daemon=True).start()
        logger.info("[MAIN] Web dashboard daemon thread started")
    except Exception as _we:
        logger.warning(f"[MAIN] Web dashboard failed to start: {_we}")

    _strategy_loop(kite)


# ===============================================================
# ===============================================================

# ═══════════════════════════════════════════════════════════════
#  Dashboard server with admin login + subscriber token access.
# ═══════════════════════════════════════════════════════════════

import glob as _web_glob
import hashlib as _web_hashlib
import secrets as _web_secrets
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse as _web_urlparse, parse_qs as _web_parse_qs
from http.cookies import SimpleCookie as _web_SimpleCookie

_WEB_BASE = os.path.expanduser("~")
# v16.6: read port from config.yaml (websocket section) with 8080 default.
_WEB_PORT = CFG.web_port() or 8080
_WEB_STATE_DIR = D.STATE_DIR
_WEB_DASH_FILE = os.path.join(_WEB_STATE_DIR, "vrl_dashboard.json")
_WEB_TRADE_LOG = os.path.join(_WEB_BASE, "lab_data", "vrl_trade_log.csv")
_WEB_LOG_FILE_PATH = os.path.join(D.WEB_LOG_DIR, "vrl_web.log")
try:
    _web_logger = D.setup_logger("vrl_web", _WEB_LOG_FILE_PATH)
except Exception:
    _web_logger = logging.getLogger("vrl_web")

# ── AUTH CONFIG ──
_WEB_ADMIN_USER = "vishal"
_web_env_pass = ""
try:
    with open(os.path.join(_WEB_BASE, ".env")) as _ef:
        for _line in _ef:
            if _line.strip().startswith("VRL_DASHBOARD_PASS="):
                _web_env_pass = _line.strip().split("=", 1)[1].strip()
except Exception:
    pass
_WEB_ADMIN_PASS_HASH = _web_hashlib.sha256(_web_env_pass.encode()).hexdigest() if _web_env_pass else ""

# Sessions: {token: {"user": str, "role": "admin"|"subscriber", "expires": datetime}}
_web_sessions = {}
_web_sessions_lock = threading.Lock()
_WEB_SESSION_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state", "vrl_web_sessions.json")

def _web_save_sessions():
    try:
        with _web_sessions_lock:
            data = {k: {"user": v["user"], "role": v["role"], "expires": v["expires"].isoformat()}
                    for k, v in _web_sessions.items()}
            os.makedirs(os.path.dirname(_WEB_SESSION_FILE), exist_ok=True)
            with open(_WEB_SESSION_FILE, "w") as _sf:
                json.dump(data, _sf)
    except Exception:
        pass

def _web_load_sessions():
    try:
        if not os.path.isfile(_WEB_SESSION_FILE):
            return
        with open(_WEB_SESSION_FILE) as _sf:
            data = json.load(_sf)
        now = datetime.now()
        loaded = 0
        with _web_sessions_lock:
            for k, v in data.items():
                try:
                    exp = datetime.fromisoformat(v["expires"])
                    if exp > now:
                        _web_sessions[k] = {"user": v["user"], "role": v["role"], "expires": exp}
                        loaded += 1
                except Exception:
                    pass
        print(f"[SESSION] Loaded {loaded} active sessions from disk")
    except Exception:
        pass

# Login rate limit: {ip: [timestamps]}
_web_login_attempts = {}
_WEB_LOGIN_LIMIT = 5
_WEB_LOGIN_BLOCK_SECS = 900  # 15 min

def _web_get_session(cookie_header):
    """Extract session from cookie header. Returns session dict or None."""
    if not cookie_header:
        return None
    try:
        c = _web_SimpleCookie()
        c.load(cookie_header)
        if "vrl_session" in c:
            token = c["vrl_session"].value
            with _web_sessions_lock:
                sess = _web_sessions.get(token)
                if sess and datetime.now() < sess["expires"]:
                    return sess
                if sess:
                    del _web_sessions[token]
    except Exception:
        pass
    return None

def _web_create_session(user, role="admin", days=30):
    """Create session, return token."""
    token = _web_secrets.token_hex(16)
    with _web_sessions_lock:
        _web_sessions[token] = {
            "user": user, "role": role,
            "expires": datetime.now() + timedelta(days=days),
        }
    _web_save_sessions()
    return token

def _web_cleanup_sessions():
    """Remove expired sessions."""
    with _web_sessions_lock:
        expired = [k for k, v in _web_sessions.items() if datetime.now() > v["expires"]]
        for k in expired:
            del _web_sessions[k]

# Clean sessions every hour
def _web_session_cleaner():
    while True:
        time.sleep(3600)
        _web_cleanup_sessions()

_WEB_LOGIN_HTML = """<!DOCTYPE html><html><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>VRL Login</title><style>
*{margin:0;padding:0;box-sizing:border-box}
body{background:#f5f0e8;font-family:'DM Sans',sans-serif;display:flex;justify-content:center;align-items:center;min-height:100vh}
.box{background:#fff;border-radius:16px;padding:40px;width:340px;box-shadow:0 4px 24px rgba(0,0,0,0.08)}
h1{font-size:18px;font-weight:700;margin-bottom:6px}h1 span{color:#e85d04}
.sub{color:#888;font-size:12px;margin-bottom:24px}
input{width:100%;padding:12px;border:1px solid #ddd;border-radius:8px;font-size:14px;margin-bottom:12px;font-family:inherit}
input:focus{outline:none;border-color:#e85d04}
button{width:100%;padding:12px;background:#e85d04;color:#fff;border:none;border-radius:8px;font-size:14px;font-weight:700;cursor:pointer}
button:hover{background:#d45003}
.err{color:#e33;font-size:12px;margin-bottom:12px;display:none}
</style></head><body>
<div class="box"><h1><span>VISHAL RAJPUT</span> TRADE</h1>
<div class="sub">Dashboard Login</div>
<div class="err" id="err">ERR_MSG</div>
<form method="POST" action="/login">
<input name="username" placeholder="Username" required autofocus>
<input name="password" type="password" placeholder="Password" required>
<button type="submit">Login</button></form></div></body></html>"""

_WEB_TOKEN_ERROR_HTML = """<!DOCTYPE html><html><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>VRL Access</title><style>
body{background:#f5f0e8;font-family:'DM Sans',sans-serif;display:flex;justify-content:center;align-items:center;min-height:100vh}
.box{background:#fff;border-radius:16px;padding:40px;width:380px;box-shadow:0 4px 24px rgba(0,0,0,0.08);text-align:center}
h2{font-size:16px;margin-bottom:8px}
.msg{color:#888;font-size:13px}
</style></head><body><div class="box"><h2>MSG_TITLE</h2><div class="msg">MSG_BODY</div></div></body></html>"""

def _web_today_trade_summary():
    today = date.today().isoformat()
    summary = {"trades": 0, "wins": 0, "losses": 0,
               "pnl": 0.0, "pnl_rs": 0.0,
               "gross_pnl_rs": 0.0, "total_charges": 0.0, "net_pnl_rs": 0.0}
    if not os.path.isfile(_WEB_TRADE_LOG):
        return summary
    try:
        with open(_WEB_TRADE_LOG) as f:
            for r in csv.DictReader(f):
                if r.get("date") != today:
                    continue
                summary["trades"] += 1
                try:
                    p = float(r.get("pnl_pts", 0) or 0)
                    summary["pnl"] += p
                    if p > 0:
                        summary["wins"] += 1
                    else:
                        summary["losses"] += 1
                except Exception:
                    pass
                try:
                    summary["pnl_rs"] += float(r.get("pnl_rs", 0) or 0)
                except Exception:
                    pass
                try:
                    summary["gross_pnl_rs"]  += float(r.get("gross_pnl_rs", 0) or 0)
                    summary["total_charges"] += float(r.get("total_charges", 0) or 0)
                    summary["net_pnl_rs"]    += float(r.get("net_pnl_rs", 0) or 0)
                except Exception:
                    pass
    except Exception:
        pass
    summary["pnl"]            = round(summary["pnl"], 1)
    summary["pnl_rs"]         = round(summary["pnl_rs"], 0)
    summary["gross_pnl_rs"]   = round(summary["gross_pnl_rs"], 0)
    summary["total_charges"]  = round(summary["total_charges"], 0)
    summary["net_pnl_rs"]     = round(summary["net_pnl_rs"], 0)
    return summary


def _web_read_dash():
    data = {"version": VERSION}
    if os.path.isfile(_WEB_DASH_FILE):
        try:
            with open(_WEB_DASH_FILE) as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            try:
                _web_logger.debug("[WEB] _read_dash error: " + str(e))
            except Exception:
                pass
            # keep data = {"version": VERSION} — don't wipe it on read error
    csv_summary = _web_today_trade_summary()
    today_block = data.get("today") or {}
    today_block.update({
        "trades": csv_summary["trades"],
        "wins":   csv_summary["wins"],
        "losses": csv_summary["losses"],
        "pnl":    csv_summary["pnl"],
        "pnl_rs": csv_summary["pnl_rs"],
    })
    data["today"] = today_block
    return data


_WEB_FOLDERS = {
    "trade_log":    ("Trade Log",            os.path.join(_WEB_BASE, "lab_data")),
    "reports":      ("Daily Summary",        os.path.join(_WEB_BASE, "lab_data", "reports")),
    "spot":         ("Spot Data",            os.path.join(_WEB_BASE, "lab_data", "spot")),
    "options_3min": ("Options 3-Min CE+PE",  os.path.join(_WEB_BASE, "lab_data", "options_3min")),
    "options_1min": ("Options 1-Min + Scan", os.path.join(_WEB_BASE, "lab_data", "options_1min")),
    "logs_live":    ("Live Logs",            os.path.join(_WEB_BASE, "logs", "live")),
    "logs_errors":  ("Error Logs",           os.path.join(_WEB_BASE, "logs", "errors")),
}

def _web_list_files(folder=""):
    if not folder:
        return {"folders": [{"key": k, "name": v[0]} for k, v in _WEB_FOLDERS.items()]}
    info = _WEB_FOLDERS.get(folder)
    if not info or not os.path.isdir(info[1]):
        return {"files": [], "folder": folder}
    files = []
    for f in sorted(os.listdir(info[1]), reverse=True):
        fp = os.path.join(info[1], f)
        if os.path.isfile(fp):
            size = os.path.getsize(fp)
            if size > 0:
                files.append({
                    "name": f,
                    "size": round(size / 1024, 1),
                    "path": folder + "/" + f,
                })
    return {"files": files[:30], "folder": folder, "folder_name": info[0]}


def _web_read_multitf():
    spot_dir = os.path.join(_WEB_BASE, "lab_data", "spot")
    opt3_dir = os.path.join(_WEB_BASE, "lab_data", "options_3min")
    opt1_dir = os.path.join(_WEB_BASE, "lab_data", "options_1min")
    def _latest(d, p):
        fs = sorted(_web_glob.glob(os.path.join(d, p + "*.csv")))
        if fs: return fs[-1]
        a = os.path.join(d, p + ".csv")
        return a if os.path.isfile(a) else None
    def _last(path):
        if not path or not os.path.isfile(path): return None
        try:
            with open(path) as f: rows = list(csv.DictReader(f))
            return rows[-1] if rows else None
        except Exception: return None
    def _lasttype(path, t):
        if not path or not os.path.isfile(path): return None
        try:
            with open(path) as f: rows = list(csv.DictReader(f))
            for r in reversed(rows):
                if r.get("type") == t: return r
            return None
        except Exception: return None
    def _f(r, k, d=0):
        try: return round(float(r.get(k, d)), 1)
        except (TypeError, ValueError): return d
    def _f3(r, k, d=0):
        try: return round(float(r.get(k, d)), 3)
        except (TypeError, ValueError): return d
    spot = []
    for label, prefix in [("1m","nifty_spot_1min"),("5m","nifty_spot_5min_"),("15m","nifty_spot_15min_"),("60m","nifty_spot_60min_"),("D","nifty_spot_daily")]:
        r = _last(_latest(spot_dir, prefix))
        if r: spot.append({"tf":label,"adx":_f(r,"adx"),"rsi":_f(r,"rsi"),"spread":_f(r,"ema_spread",_f(r,"spread")),"regime":r.get("regime","")})
        else: spot.append({"tf":label,"adx":0,"rsi":0,"spread":0,"regime":""})
    try:
        _d = _web_read_dash()
        _mk = _d.get("market", {})
        spot.insert(1, {
            "tf": "3m",
            "adx": round(float(_mk.get("spot_adx_3m", 0)), 1),
            "rsi": round(float(_mk.get("spot_rsi", 0)), 1),
            "spread": round(float(_mk.get("spot_spread", 0)), 1),
            "regime": _mk.get("regime", ""),
        })
    except Exception:
        spot.insert(1, {"tf": "3m", "adx": 0, "rsi": 0, "spread": 0, "regime": ""})
    ce = []; pe = []; ce_strike = 0; pe_strike = 0
    for label, d, prefix in [("1m",opt1_dir,"nifty_option_1min_"),("3m",opt3_dir,"nifty_option_3min_")]:
        p = _latest(d, prefix)
        for side, arr in [("CE",ce),("PE",pe)]:
            r = _lasttype(p, side)
            if r:
                arr.append({"tf":label,"adx":_f(r,"adx"),"rsi":_f(r,"rsi"),"iv":_f(r,"iv_pct"),"delta":_f3(r,"delta"),"ltp":_f(r,"close"),"body":_f(r,"body_pct"),"spread":_f(r,"ema_spread",_f(r,"ema9_gap")),"strike":r.get("strike","")})
                if side == "CE" and not ce_strike: ce_strike = r.get("strike", "")
                if side == "PE" and not pe_strike: pe_strike = r.get("strike", "")
            else: arr.append({"tf":label,"adx":0,"rsi":0,"iv":0,"delta":0,"ltp":0,"body":0,"spread":0,"strike":""})
    try:
        d = _web_read_dash()
        ce_live = d.get("ce", {}).get("ltp", 0)
        pe_live = d.get("pe", {}).get("ltp", 0)
        if ce_live:
            for row in ce: row["ltp"] = round(ce_live, 1)
        if pe_live:
            for row in pe: row["ltp"] = round(pe_live, 1)
    except Exception:
        pass
    return {"spot":spot,"ce":ce,"pe":pe,"ce_strike":ce_strike,"pe_strike":pe_strike}

def _web_read_trades():
    if not os.path.isfile(_WEB_TRADE_LOG): return []
    today = date.today().isoformat()
    trades = []
    try:
        with open(_WEB_TRADE_LOG) as f:
            for r in csv.DictReader(f):
                if r.get("date","").strip() == today:
                    try:
                        row = {k: r.get(k,"") for k in r}
                        # compute held_min from timestamps (candles_held is often 0 in CSV)
                        try:
                            _t1 = datetime.strptime(f"{today} {row['entry_time']}", "%Y-%m-%d %H:%M:%S")
                            _t2 = datetime.strptime(f"{today} {row['exit_time']}", "%Y-%m-%d %H:%M:%S")
                            row["candles_held"] = int((_t2 - _t1).total_seconds() / 60)
                        except Exception:
                            pass
                        trades.append(row)
                    except Exception: pass
    except Exception: pass
    return trades

def _web_read_fno():
    fno_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "screener", "fno_tracker.csv")
    if not os.path.isfile(fno_path): return []
    rows = []
    try:
        with open(fno_path) as f:
            for r in csv.DictReader(f):
                st = str(r.get("status",""))
                if not (st.startswith("OPEN") or "HIT" in st): continue
                try:
                    rows.append({
                        "symbol":              r.get("symbol",""),
                        "direction":           r.get("direction",""),
                        "option_symbol":       r.get("option_symbol",""),
                        "strike":              r.get("strike",""),
                        "expiry":              r.get("expiry",""),
                        "lot_size":            int(r.get("lot_size",0) or 0),
                        "lots":                (lambda v: int(float(v)) if v and str(v).strip() not in ('','nan') else 1)(r.get("lots","")),
                        "entry_premium":       float(r.get("entry_premium",0) or 0),
                        "sl_premium":          float(r.get("sl_premium",0) or 0),
                        "t1_premium":          float(r.get("t1_premium",0) or 0),
                        "t2_premium":          float(r.get("t2_premium",0) or 0),
                        "current_premium":     float(r.get("current_premium",0) or 0),
                        "current_return_pct":  float(r.get("current_return_pct",0) or 0),
                        "investment":          float(r.get("investment",0) or 0),
                        "pnl_rs":              float(r.get("pnl_rs",0) or 0),
                        "score":               int(r.get("score",0) or 0),
                        "rank":                int(r.get("rank",0) or 0),
                        "stock_price":         float(r.get("stock_price",0) or 0),
                        "stock_sl":            float(r.get("stock_sl",0) or 0),
                        "pcr":                 float(r.get("pcr",0) or 0),
                        "max_pain":            float(r.get("max_pain",0) or 0),
                        "status":              st,
                        "last_checked":        r.get("last_checked",""),
                        "date_added":          r.get("date_added",""),
                    })
                except Exception: pass
    except Exception: pass
    return rows

def _web_read_weekly():
    rows = []
    try:
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "screener", "weekly_tracker.csv")
        if os.path.isfile(path):
            with open(path) as f:
                for r in csv.DictReader(f):
                    try:
                        rows.append({
                            "date_added":    r.get("date_added",""),
                            "rank":          int(r.get("rank",0) or 0),
                            "symbol":        r.get("symbol",""),
                            "name":          r.get("name",""),
                            "entry_price":   float(r.get("entry_price",0) or 0),
                            "sl":            float(r.get("sl",0) or 0),
                            "target_1y":     float(r.get("target_1y",0) or 0),
                            "target_3y":     float(r.get("target_3y",0) or 0),
                            "t3_upside_pct": float(r.get("t3_upside_pct",0) or 0),
                            "roe":           float(r.get("roe",0) or 0),
                            "roce":          float(r.get("roce",0) or 0),
                            "score":         int(r.get("score",0) or 0),
                            "grade":         r.get("grade",""),
                            "status":        r.get("status",""),
                            "exit_price":    float(r.get("exit_price",0) or 0),
                            "actual_return": float(r.get("actual_return",0) or 0),
                            "current_price":      float(r.get("current_price",0) or 0),
                            "current_return_pct": float(r.get("current_return_%",0) or 0),
                            "trail_sl":      float(r.get("trail_sl",0) or 0),
                            "rs_vs_nifty":   float(r.get("rs_vs_nifty",0) or 0),
                            "crash_flag":    int(float(r.get("crash_flag",0) or 0)),
                            "mon_status":    r.get("mon_status",""),
                            "peg":           float(r.get("peg",0) or 0),
                            "promoter":      float(r.get("promoter",0) or 0),
                            "last_updated":  r.get("last_updated",""),
                            "weeks_as_pick": int(float(r.get("weeks_as_pick",1) or 1)),
                            "reconfirmed":   r.get("reconfirmed",""),
                        })
                    except Exception: pass
    except Exception: pass
    return rows

def _web_read_shadow():
    """Read shadow state JSON — atomic file, safe read with fallback."""
    try:
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state", "vrl_shadow_state.json")
        if not os.path.isfile(path): return {}
        with open(path) as f:
            d = json.load(f)
        def _sig(d, key, side):
            s = d.get(key, {}).get(side, {})
            return {
                "active":       bool(s.get("active", False)),
                "entry_price":  float(s.get("entry_price", 0) or 0),
                "entry_time":   s.get("entry_time", ""),
                "peak_price":   float(s.get("peak_price", 0) or 0),
                "peak_pts":     float(s.get("peak_pts", 0) or 0),
                "shadow_sl":    float(s.get("shadow_sl", 0) or 0),
                "shadow_level": s.get("shadow_level", ""),
                "entry_strike": int(s.get("entry_strike", 0) or 0),
                "today_entry":  float(s.get("today_entry", 0) or 0),
                "bucket_ts":    str(s.get("bucket_ts", "")),
                "sl_ts":        float(s.get("sl_ts", 0) or 0),
                "exit_ts":      float(s.get("exit_ts", 0) or 0),
                "today_date":   s.get("today_date", ""),
                "last_exit_pnl":    float(s.get("last_exit_pnl", 0) or 0),
                "last_exit_reason": s.get("last_exit_reason", ""),
                "last_exit_ts":     float(s.get("last_exit_ts", 0) or 0),
            }
        return {
            "saved_date": d.get("saved_date", ""),
            "p1": {"CE": _sig(d,"p1","CE"), "PE": _sig(d,"p1","PE")},
            "p2": {"CE": _sig(d,"p2","CE"), "PE": _sig(d,"p2","PE")},
            "live": d.get("live", {}),
        }
    except Exception: return {}

_WEB_HTML = r"""<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>VRL War Room</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
:root{--bg:#fdf6ec;--c1:#fffaf4;--c2:#f5ebe0;--bd:#e0ccb0;--tx:#2c1f0e;--dm:#8a7055;--bl:#1a6bbf;--gn:#0a7a50;--rd:#c0392b;--am:#b06a00;--pr:#7c3aed;--cy:#0e7490;--gold:#b45309}
body{background:var(--bg);color:var(--tx);font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;font-size:13px;min-height:100vh}
@media(min-width:900px){body{display:grid;grid-template-rows:auto auto 1fr auto;grid-template-columns:1fr;max-width:1200px;margin:0 auto}}
.hd{background:var(--c1);border-bottom:1px solid var(--bd);padding:10px 12px;position:sticky;top:0;z-index:10}
.hd h1{font-size:13px;font-weight:700;letter-spacing:.5px}.hd b{color:var(--bl)}
.tags{display:flex;gap:4px;margin-top:5px;flex-wrap:wrap}
.tag{padding:2px 6px;border-radius:3px;font-size:9px;font-weight:700;letter-spacing:.3px}
.tg{background:rgba(16,185,129,.15);color:var(--gn)}.tr{background:rgba(239,68,68,.15);color:var(--rd)}
.tb{background:rgba(59,130,246,.15);color:var(--bl)}.ta{background:rgba(245,158,11,.15);color:var(--am)}
.tp{background:rgba(168,85,247,.15);color:var(--pr)}
.sect{margin:8px;background:var(--c1);border:1px solid var(--bd);border-radius:8px;overflow:hidden}
.sh{padding:8px 10px;font-size:10px;font-weight:700;color:var(--dm);text-transform:uppercase;letter-spacing:.8px;border-bottom:1px solid var(--bd);background:var(--c2)}
.row{display:flex;justify-content:space-between;padding:5px 10px;border-bottom:1px solid rgba(30,30,48,.5)}
.row:last-child{border:none}
.row .k{color:var(--dm);font-size:10px}.row .v{font-weight:700;font-size:12px}
.gate{display:flex;gap:6px;padding:8px 10px;flex-wrap:wrap}
.dot{width:28px;height:28px;border-radius:6px;display:flex;align-items:center;justify-content:center;font-size:9px;font-weight:700}
.dot-g{background:rgba(16,185,129,.2);color:var(--gn);border:1px solid rgba(16,185,129,.3)}
.dot-r{background:rgba(239,68,68,.15);color:var(--rd);border:1px solid rgba(239,68,68,.2)}
.bar-wrap{padding:6px 10px}
.bar-label{display:flex;justify-content:space-between;font-size:9px;color:var(--dm);margin-bottom:3px}
.bar{height:6px;background:var(--c2);border-radius:3px;overflow:hidden}
.bar-fill{height:100%;border-radius:3px;transition:width .5s}
.verdict{padding:8px 10px;font-size:11px;font-weight:700;text-align:center;letter-spacing:.3px}
.pos{margin:8px;border-radius:8px;padding:10px}
.pos .big{font-size:20px;font-weight:700}
.prog{height:6px;background:var(--c2);border-radius:3px;overflow:hidden;margin:6px 0;position:relative}
.prog-fill{height:100%;border-radius:3px;transition:width .5s}
.tabs{display:flex;border-bottom:1px solid var(--bd);padding:0 8px;background:var(--c1)}
.tab{padding:7px 14px;font-size:10px;font-weight:700;color:var(--dm);border-bottom:2px solid transparent;cursor:pointer;text-transform:uppercase;letter-spacing:.5px}
.tab.on{color:var(--bl);border-color:var(--bl)}
.tc{margin:4px 8px;padding:8px 10px;border-radius:6px;border:1px solid;display:flex;align-items:flex-start;gap:8px}
.tc.w{background:rgba(16,185,129,.04);border-color:rgba(16,185,129,.15)}
.tc.l{background:rgba(239,68,68,.04);border-color:rgba(239,68,68,.15)}
.H{display:none}
.ft{text-align:center;padding:6px;font-size:8px;color:#a08060;border-top:1px solid var(--bd)}
.two{display:grid;grid-template-columns:1fr 1fr;gap:0}
.two>.sect{margin:0;border-radius:0;border-right:none}.two>.sect:last-child{border-right:1px solid var(--bd)}
.ctx-row{display:grid;grid-template-columns:repeat(4,1fr);gap:0;margin:8px;background:var(--c1);border:1px solid var(--bd);border-radius:8px;overflow:hidden}
.ctx{text-align:center;padding:6px 4px;border-right:1px solid var(--bd)}.ctx:last-child{border:none}
.ctx .k{font-size:8px;color:var(--dm);text-transform:uppercase;letter-spacing:.3px}
.ctx .v{font-size:12px;font-weight:700;margin-top:1px}
.pos-lot{font-size:10px;color:#aaa;margin-top:2px}
.pos-meta{display:flex;gap:12px;font-size:9px;color:#555;margin-top:4px}
.day-bar{margin:8px;display:flex;gap:6px}
.day-box{flex:1;background:var(--c1);border:1px solid var(--bd);border-radius:6px;padding:6px 8px;text-align:center}
.day-box .dk{font-size:8px;color:#555}
.day-box .dv{font-size:15px;font-weight:700;margin:2px 0}
.day-box .ds{font-size:9px;color:#555}
</style></head><body>

<div class="hd">
  <div style="display:flex;justify-content:space-between;align-items:flex-start">
    <h1><b>VRL</b> WAR ROOM <span style="color:#444;font-size:9px" id="ver"></span></h1>
    <div style="text-align:right;line-height:1.1"><div style="font-size:9px;color:#555;letter-spacing:.5px;text-transform:uppercase">Nifty</div><div id="hd-spot" style="font-size:22px;font-weight:700;color:var(--bl);letter-spacing:-1px">—</div></div>
  </div>
  <div class="tags" id="tags"></div>
</div>

<div id="position-area"></div>

<div class="tabs">
  <div class="tab on" data-t="sig" onclick="st('sig')">⚡ SIG</div>
  <div class="tab" data-t="mkt" onclick="st('mkt')">📈 MKT</div>
  <div class="tab" data-t="fno" onclick="st('fno')">📊 F&amp;O</div>
  <div class="tab" data-t="trd" onclick="st('trd')">📒 TRD</div>
  <div class="tab" data-t="wkly" onclick="st('wkly')">📅 WEEKLY</div>
  <div class="tab" data-t="fil" onclick="st('fil')">📁 FILES</div>
</div>

<div id="p-sig"></div>
<div id="p-mkt" class="H"></div>
<div id="p-fno" class="H"></div>
<div id="p-trd" class="H"></div>
<div id="p-wkly" class="H"></div>
<div id="p-fil" class="H"></div>

<div class="ft">Auto-refresh 10s · <span id="ts"></span></div>


<script>
var _curTab='sig';
function st(t){_curTab=t;document.querySelectorAll('.tab').forEach(e=>e.classList.toggle('on',e.dataset.t===t));['sig','mkt','fno','trd','wkly','fil'].forEach(i=>document.getElementById('p-'+i).classList.toggle('H',i!==t));if(t==='fno')renderFno();if(t==='wkly')renderWeekly();if(t==='fil')loadFiles('');}

function esc(s){return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')}

function tagC(v){
  if(v==='BULL')return 'tg';if(v==='BEAR')return 'tr';
  if(v==='SIDEWAYS'||v==='NEUTRAL')return 'ta';return 'tb'}

function shortSym(sym, dir, strike){
  if(dir && strike) return dir + ' ' + strike;
  if(!sym) return '';
  var m = sym.match(/(CE|PE)$/);
  if(m){var s=sym.replace(/^NIFTY\d+/,'').replace(/(CE|PE)$/,'');return m[1]+' '+s;}
  return sym;
}

function render(d, trades, zones, mtf){ if(!d || !d.market){document.getElementById('p-sig').innerHTML='<div style="text-align:center;color:#555;padding:20px">Waiting for bot data... (FILES tab works)</div>';document.getElementById('position-area').innerHTML='';return}

  const mk=d.market,ce=d.ce||{},pe=d.pe||{},pos=d.position||{},td=d.today||{},str=d.straddle||{},rl=d.rolling||{};
  // streak lives in rolling block, not today block — map it for the day-bar
  if(!td.streak&&rl.streak)td.streak=rl.streak;

  // Version + tags
  document.getElementById('ver').textContent=d.version||'';
  let tags='<span class="tag '+(d.mode==='LIVE'?'tg':'tb')+'">'+esc(d.mode||'')+'</span>';
  tags+='<span class="tag '+(mk.dte<=1?'tr':'tb')+'">DTE '+(mk.dte||0)+'</span>';
  tags+='<span class="tag tb">CE '+(mk.locked_ce||mk.atm)+' · PE '+(mk.locked_pe||mk.atm)+' 🔒</span>';
  if(mk.vix>0)tags+='<span class="tag '+(mk.vix>22?'tr':mk.vix>18?'ta':'tg')+'">VIX '+mk.vix+'</span>';
  if(mk.bias&&mk.bias!=='')tags+='<span class="tag '+tagC(mk.bias)+'" title="Daily bias (ADX-based)">D: '+esc(mk.bias)+'</span>';
  if(mk.regime){var _rgc=mk.regime.includes('TREND')?'tg':mk.regime==='NEUTRAL'?'ta':'tr';tags+='<span class="tag '+_rgc+'" title="3-min candle EMA spread regime">3m: '+esc(mk.regime)+'</span>';}
  if(mk.market_open&&!mk.indicators_warm)tags+='<span class="tag tr">WARMUP</span>';
  document.getElementById('tags').innerHTML=tags;
  document.getElementById('hd-spot').textContent=mk.spot||'—';

  // ── POSITION CARD ──
  var ph='';
  if(pos.in_trade){
    var sym=shortSym(pos.symbol,pos.direction,pos.strike);
    var pnl=parseFloat(pos.pnl||0);
    var peak=parseFloat(pos.peak||0);
    var entry=parseFloat(pos.entry||0);
    var ltp=parseFloat(pos.ltp||0);
    var sl=parseFloat(pos.sl||0);
    var floor=parseFloat(pos.current_floor||0);
    var rsi=parseFloat(pos.current_rsi||0);
    var candles=pos.candles||0;
    var lot1=pos.lot1_active;
    var lot2=pos.lot2_active;
    var split=pos.lots_split;
    var activeLots=(lot1?1:0)+(lot2?1:0);
    var pnlRs=Math.round(pnl*(pos.lot_size||65)*activeLots);
    var pnlClr=pnl>=0?'var(--gn)':'var(--rd)';
    // RSI progress bar
    var rsiPct=Math.min(100,(rsi/80)*100);
    var rsiBarClr=rsi>=80?'var(--rd)':rsi>=75?'var(--am)':'var(--gn)';
    // State label
    var stateIcon,stateLabel;
    if(!split){stateIcon='🟢';stateLabel=sym+' IN TRADE';}
    else if(lot1&&lot2){stateIcon='⚡';stateLabel=sym+' SPLIT';}
    else{stateIcon='🏃';stateLabel=sym+' LOT2 RIDING';}
    var posClr=pos.direction==='CE'?'rgba(59,130,246,.1)':'rgba(239,68,68,.08)';
    var posBd=pos.direction==='CE'?'rgba(59,130,246,.25)':'rgba(239,68,68,.2)';
    ph='<div class="pos" style="background:linear-gradient(135deg,'+posClr+',transparent);border:1px solid '+posBd+'">';
    ph+='<div style="font-size:13px;font-weight:700;margin-bottom:4px">'+stateIcon+' '+esc(stateLabel)+'</div>';
    ph+='<div style="margin:3px 0"><span class="big" style="color:'+pnlClr+'">'+(pnl>=0?'+':'')+pnl.toFixed(1)+'pts</span>';
    ph+=' <span style="color:#888;font-size:11px">&#x20B9;'+pnlRs.toLocaleString('en-IN')+'</span>';
    ph+='<span style="color:#555;font-size:10px;float:right">Entry &#x20B9;'+entry+' → &#x20B9;'+ltp+'</span></div>';
    // RSI progress bar
    ph+='<div class="prog"><div class="prog-fill" style="width:'+rsiPct.toFixed(0)+'%;background:'+rsiBarClr+'"></div></div>';
    ph+='<div style="display:flex;justify-content:space-between;font-size:9px;color:#555;margin-bottom:6px">';
    ph+='<span>RSI '+rsi.toFixed(0)+' (cap 80)</span><span>'+rsiPct.toFixed(0)+'%</span></div>';
    // 3-box status grid: SL / TIER / HELD
    ph+='<div style="display:grid;grid-template-columns:repeat(3,1fr);gap:5px;margin-bottom:6px">';
    ph+='<div style="background:rgba(0,0,0,.35);border:1px solid var(--bd);border-radius:5px;padding:5px 4px;text-align:center"><div style="font-size:8px;color:#555;margin-bottom:2px">SL</div><div style="font-size:12px;font-weight:700;color:var(--rd)">&#x20B9;'+sl.toFixed(1)+'</div></div>';
    ph+='<div style="background:rgba(0,0,0,.35);border:1px solid var(--bd);border-radius:5px;padding:5px 4px;text-align:center"><div style="font-size:8px;color:#555;margin-bottom:2px">TIER</div><div style="font-size:12px;font-weight:700;color:var(--am)">'+(pos.active_ratchet_tier||'—')+'</div></div>';
    ph+='<div style="background:rgba(0,0,0,.35);border:1px solid var(--bd);border-radius:5px;padding:5px 4px;text-align:center"><div style="font-size:8px;color:#555;margin-bottom:2px">HELD</div><div style="font-size:12px;font-weight:700;color:var(--cy)">'+candles+'m</div></div>';
    ph+='</div>';
    // Peak-captured progress bar
    var peakPct2=peak>0?Math.min(100,Math.max(0,(pnl/peak)*100)):0;
    var peakBarClr=peakPct2>=80?'var(--gn)':peakPct2>=50?'var(--am)':'var(--rd)';
    ph+='<div class="bar-label" style="margin-bottom:3px"><span>PEAK CAPTURED</span><span style="color:'+peakBarClr+'">'+peakPct2.toFixed(0)+'%  +'+peak.toFixed(1)+'pts</span></div>';
    ph+='<div class="bar" style="margin-bottom:6px"><div class="bar-fill" style="width:'+peakPct2.toFixed(0)+'%;background:'+peakBarClr+'"></div></div>';
    // Lot status
    if(!split){
      ph+='<div class="pos-lot">LOT1: '+(lot1?'<span style="color:var(--gn)">Active</span>':'<span style="color:#555">SOLD</span>')+' &nbsp; SL &#x20B9;'+sl+' (floor +'+floor.toFixed(0)+')</div>';
      ph+='<div class="pos-lot">LOT2: '+(lot2?'<span style="color:var(--gn)">Active</span>':'<span style="color:#555">SOLD</span>')+' &nbsp; SL &#x20B9;'+sl+' (floor +'+floor.toFixed(0)+')</div>';
    } else if(lot1&&lot2){
      ph+='<div class="pos-lot">LOT1: <span style="color:var(--am)">Floor SL</span> &#x20B9;'+sl+'</div>';
      ph+='<div class="pos-lot">LOT2: <span style="color:var(--cy)">ATR Trail</span> &#x20B9;'+sl+'</div>';
    } else {
      ph+='<div class="pos-lot">Lot1: <span style="color:#555">SOLD</span> +'+peak.toFixed(1)+'pts ✅</div>';
      ph+='<div class="pos-lot">LOT2: <span style="color:var(--cy)">ATR Trail</span> &#x20B9;'+sl+'</div>';
    }
    ph+='<div class="pos-meta"><span>Peak: +'+(peak||0).toFixed(1)+'</span><span>RSI: '+rsi.toFixed(0)+'</span><span>'+candles+'min</span></div>';
    ph+='</div>';
  }

  // ── TODAY SUMMARY BAR ──
  var dpnl=parseFloat(td.pnl||0);
  var wins=parseInt(td.wins||0),losses=parseInt(td.losses||0);
  var totalT=wins+losses;
  var wr=totalT>0?Math.round((wins/totalT)*100):0;
  var dpnlRs=Math.round(parseFloat(td.pnl_rs||0));
  ph+='<div class="day-bar">';
  ph+='<div class="day-box"><div class="dk">DAY P&L</div>';
  ph+='<div class="dv" style="color:'+(dpnl>=0?'var(--gn)':'var(--rd)')+'">'+(dpnl>=0?'+':'')+dpnl.toFixed(1)+'pts</div>';
  ph+='<div class="ds">&#x20B9;'+(dpnlRs>=0?'+':'')+dpnlRs+'</div></div>';
  ph+='<div class="day-box"><div class="dk">TRADES</div>';
  ph+='<div class="dv">'+(td.trades||0)+'</div>';
  ph+='<div class="ds">'+wins+'W '+losses+'L &nbsp; WR '+wr+'%'+(td.streak>=2?' 🔴'+td.streak:'')+'</div></div>';
  ph+='<div class="day-box"><div class="dk">VIX</div>';
  ph+='<div class="dv" style="color:'+(mk.vix>22?'var(--rd)':mk.vix>18?'var(--am)':'var(--gn)')+'">'+mk.vix+'</div>';
  ph+='<div class="ds">'+(mk.vix>22?'HIGH':mk.vix>18?'ELEV':'NORM')+'</div></div>';
  ph+='<div class="day-box"><div class="dk">STATUS</div>';
  ph+='<div class="dv">'+(td.paused?'⏸':'⚡')+'</div>';
  ph+='<div class="ds">'+(td.paused?'PAUSED':mk.market_open?'SCANNING':'CLOSED')+'</div></div>';
  ph+='</div>';
  document.getElementById('position-area').innerHTML=ph;

  // ── SIGNAL TAB ──
  document.getElementById('p-sig').innerHTML=
    renderShadow(window._shadow||{});

  function renderShadow(sh){
    if(!sh||!sh.p1)return'';
    function sCard(type,label,sig){
      if(!sig)return'';
      var act=sig.active;
      var bclr=act?'var(--gn)':'#bbb';
      var btxt=act?'● LIVE':'○ idle';
      var pclr=(sig.peak_pts||0)>=0?'var(--gn)':'var(--rd)';
      var h='<div class="sect" style="padding:8px 10px;opacity:'+(act?1:0.55)+'">';
      h+='<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:4px">';
      h+='<span style="font-weight:700;font-size:11px;color:var(--tx)">'+type+' '+label+'</span>';
      h+='<span style="font-size:9px;font-weight:700;color:'+bclr+'">'+btxt+'</span></div>';
      if(act){
        if(sig.entry_strike)h+='<div class="row"><div class="k">STRIKE</div><div class="v">'+sig.entry_strike+'</div></div>';
        h+='<div class="row"><div class="k">ENTRY</div><div class="v">₹'+sig.entry_price.toFixed(1)+(sig.entry_time?' @ '+sig.entry_time:'')+'</div></div>';
        h+='<div class="row"><div class="k">PEAK</div><div class="v" style="color:'+pclr+'">'+(sig.peak_pts>=0?'+':'')+sig.peak_pts.toFixed(1)+' pts  (₹'+sig.peak_price.toFixed(1)+')</div></div>';
        h+='<div class="row"><div class="k">SL</div><div class="v">₹'+sig.shadow_sl.toFixed(1)+'  •  '+esc(sig.shadow_level||'—')+'</div></div>';
        if(sig.today_entry)h+='<div class="row"><div class="k">TODAY FIRST</div><div class="v">₹'+sig.today_entry.toFixed(1)+'</div></div>';
      } else {
        var outcome='';var outClr='#999';
        if(sig.last_exit_reason&&sig.today_entry>0){
          var lp=sig.last_exit_pnl||0;
          outClr=lp>0?'var(--gn)':(lp<0?'var(--rd)':'#999');
          var rtag=sig.last_exit_reason==='SL-HIT'?(lp>=0?'TRAIL':'SL'):sig.last_exit_reason;
          outcome=(lp>=0?'+':'')+lp.toFixed(0)+' '+rtag;
        } else if(sig.sl_ts>0&&sig.today_entry>0){outcome='SL-HIT';outClr='var(--rd)';}
        else if(sig.exit_ts>0&&sig.today_entry>0){outcome='EXITED';outClr='var(--am)';}
        if(sig.today_entry)h+='<div class="row"><div class="k">LAST ENTRY</div><div class="v" style="color:#999">₹'+sig.today_entry.toFixed(1)+(outcome?' <span style="font-size:8px;padding:1px 5px;border-radius:3px;background:rgba(0,0,0,.08);color:'+outClr+'">'+outcome+'</span>':'')+'</div></div>';
        else h+='<div style="font-size:10px;color:#aaa;padding:2px 0">No signal today</div>';
      }
      h+='</div>';
      return h;
    }
    var p1=sh.p1||{};var p2=sh.p2||{};
    var hasAny=(p1.CE&&p1.CE.active)||(p1.PE&&p1.PE.active)||(p2.CE&&p2.CE.active)||(p2.PE&&p2.PE.active);
    var dot=hasAny?'<span style="display:inline-block;width:7px;height:7px;border-radius:50%;background:var(--gn);margin-right:5px;animation:pulse 1.2s infinite"></span>':'';
    var html='<div style="margin:8px 8px 0">';
    html+='<div style="font-size:10px;font-weight:700;color:var(--dm);letter-spacing:.5px;padding:4px 10px 6px">'+dot+'⭐ V10 LIVE — P1/P2 (1-min)'+(sh.saved_date?' · '+sh.saved_date:'')+'</div>';
    // ── LIVE GATE MONITOR — watch each side approach the thresholds ──
    (function(){
      var lv=sh.live||{};
      function pill(label,val,ok){
        var c=ok?'var(--gn)':'var(--rd)';
        var bg=ok?'rgba(10,122,80,.10)':'rgba(192,57,43,.07)';
        var bd=ok?'rgba(10,122,80,.30)':'rgba(192,57,43,.20)';
        return '<div style="flex:1;text-align:center;padding:6px 2px;border-radius:10px;background:'+bg+';border:1px solid '+bd+'">'
          +'<div style="font-size:8px;font-weight:700;color:var(--dm);letter-spacing:.6px">'+label+'</div>'
          +'<div style="font-size:15px;font-weight:800;color:'+c+';line-height:1.25">'+val+'</div></div>';
      }
      function card(side,o){
        var acc=side==='CE'?'var(--gn)':'var(--rd)';
        var h='<div style="background:var(--c1);border:1px solid var(--bd);border-top:3px solid '+acc+';border-radius:13px;padding:9px 9px 10px;box-shadow:0 1px 4px rgba(0,0,0,.05)">';
        h+='<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px">';
        h+='<span style="font-size:13px;font-weight:800;color:'+acc+';letter-spacing:.6px">'+side+((o&&o.strike)?' '+o.strike:'')+'</span>'+((o&&o.price)?'<span style="font-size:14px;font-weight:800;color:var(--tx);margin-left:7px">₹'+o.price+'</span>':'');
        if(!o||o.gap===undefined){return h+'<span style="font-size:9px;color:var(--dm)">— no data —</span></div></div>';}
        if(o.ready)h+='<span style="background:var(--gn);color:#fff;font-size:10px;font-weight:800;padding:3px 12px;border-radius:20px;box-shadow:0 0 0 0 rgba(10,122,80,.5);animation:pulse 1.3s infinite">● READY</span>';
        else h+='<span style="background:var(--c2);color:var(--am);font-size:9px;font-weight:700;padding:3px 10px;border-radius:20px">⏳ '+esc(o.reject||'wait')+'</span>';
        h+='</div><div style="display:flex;gap:6px">';
        h+=pill('GAP',(o.gap_ok?'✓ ':'')+o.gap,o.gap_ok);
        h+=pill('RSI',o.rsi+(o.rsi_rising?' ↑':' ↓'),o.rsi_ok);
        h+=pill('BW',(o.bw_ok?'✓ ':'')+o.bw,o.bw_ok);
        return h+'</div></div>';
      }
      html+='<div style="margin:2px 10px 9px">';
      html+='<div style="font-size:9px;font-weight:700;color:var(--dm);padding:0 2px 6px;letter-spacing:.5px">⚡ LIVE GATES &nbsp;·&nbsp; need gap≥3.5 · RSI 55-80↑ · BW≥5</div>';
      html+='<div style="display:grid;grid-template-columns:1fr 1fr;gap:8px">'+card('CE',lv.CE)+card('PE',lv.PE)+'</div>';
      html+='</div>';
    })();
    html+='<div style="display:grid;grid-template-columns:1fr 1fr;gap:6px">';
    html+=sCard('P1','CE',p1.CE)+sCard('P1','PE',p1.PE);
    html+=sCard('P2','CE',p2.CE)+sCard('P2','PE',p2.PE);
    html+='</div>';
    html+='</div>';
    return html;
  }

  // ── MARKET TAB ──
  let mh='<div class="sect"><div class="sh">📈 SPOT NIFTY (3-MIN) · '+mk.spot+'</div>'+
    '<div class="row"><div class="k">EMA 9</div><div class="v" style="color:var(--gn)">'+mk.spot_ema9+'</div></div>'+
    '<div class="row"><div class="k">EMA 21</div><div class="v" style="color:var(--am)">'+mk.spot_ema21+'</div></div>'+
    '<div class="row"><div class="k">EMA SPREAD</div><div class="v" style="color:'+(mk.spot_spread>0?'var(--gn)':'var(--rd)')+'">'+(mk.spot_spread>0?'+':'')+mk.spot_spread+'pts</div></div>'+
    '<div class="row"><div class="k">RSI (3m)</div><div class="v" style="color:'+(mk.spot_rsi>60?'var(--gn)':mk.spot_rsi<40?'var(--rd)':'var(--am)')+'">'+mk.spot_rsi+'</div></div>'+
    '<div class="row"><div class="k">REGIME</div><div class="v" style="color:'+((mk.regime||'').includes('TREND')?'var(--gn)':'var(--am)')+'">'+esc(mk.regime||'')+'</div></div>'+
    '<div class="row"><div class="k">GAP</div><div class="v">'+(mk.gap>0?'+':'')+mk.gap+'pts</div></div>'+
    '<div style="padding:6px 10px;font-size:10px;color:'+(mk.spot_spread>5?'var(--gn)':mk.spot_spread<-5?'var(--rd)':'var(--am)')+'">'+
    (mk.spot_spread>10?'Strong uptrend — EMA9 pulling away from EMA21':
     mk.spot_spread>5?'Uptrend — spot above both EMAs':
     mk.spot_spread>0?'Weak up — EMAs close, trend unclear':
     mk.spot_spread>-5?'Weak down — EMAs close, choppy':
     mk.spot_spread>-10?'Downtrend — spot below both EMAs':
     'Strong downtrend — EMA9 falling hard')+'</div>'+
    '<div style="padding:2px 10px 6px;font-size:9px;color:#555">'+
    'RSI '+(mk.spot_rsi>=70?'OVERBOUGHT — reversal likely':mk.spot_rsi>=60?'STRONG — momentum with bulls':mk.spot_rsi<=30?'OVERSOLD — reversal likely':mk.spot_rsi<=40?'WEAK — bears in control':'NEUTRAL — no clear direction')+'</div></div>';
  mh+='<div class="ctx-row">'+
    '<div class="ctx"><div class="k">SPOT</div><div class="v" style="color:var(--bl)">'+mk.spot+'</div></div>'+
    '<div class="ctx"><div class="k">VWAP</div><div class="v" style="color:'+(mk.vwap>0?(mk.spot>mk.vwap?'var(--gn)':'var(--rd)'):'var(--dm)')+'">'+((mk.vwap>0)?mk.vwap:'—')+'</div></div>'+
    '<div class="ctx"><div class="k">EMA9</div><div class="v" style="color:var(--gn)">'+mk.spot_ema9+'</div></div>'+
    '<div class="ctx"><div class="k">SPREAD</div><div class="v" style="color:'+(mk.spot_spread>0?'var(--gn)':'var(--rd)')+'">'+(mk.spot_spread>0?'+':'')+mk.spot_spread+'</div></div></div>';
  mh+='<div class="ctx-row">'+
    '<div class="ctx"><div class="k">RSI</div><div class="v" style="color:'+(mk.spot_rsi>60?'var(--gn)':mk.spot_rsi<40?'var(--rd)':'var(--am)')+'">'+mk.spot_rsi+'</div></div>'+
    '<div class="ctx"><div class="k">H.RSI</div><div class="v" style="color:'+(mk.hourly_rsi>70?'var(--rd)':mk.hourly_rsi<30?'var(--gn)':'')+'">'+mk.hourly_rsi+'</div></div>'+
    '<div class="ctx"><div class="k">GAP</div><div class="v">'+(mk.gap>0?'+':'')+mk.gap+'</div></div>'+
    '<div class="ctx"><div class="k">SESSION</div><div class="v" style="font-size:10px">'+esc(mk.session)+'</div></div></div>';
  // Multi-TF Alignment
  var sp=mtf.spot||[],ceo=mtf.ce||[],peo=mtf.pe||[];
  function ac(v){return v>=25?'var(--gn)':v>=18?'var(--am)':'var(--rd)'}
  function al(v){return v>=25?'TR':v>=18?'WK':'FL'}
  function rc(v){return v>=60?'var(--gn)':v<=40?'var(--rd)':'var(--am)'}
  function sc(v){return v>0?'var(--gn)':v<0?'var(--rd)':'var(--dm)'}
  function gr(cols){return 'display:grid;grid-template-columns:repeat('+cols+',1fr);padding:4px 10px;font-size:11px;border-bottom:1px solid rgba(30,30,48,.5)'}
  function hdr(cols,names){var h='<div style="'+gr(names.length)+';font-size:8px;color:#555;font-weight:700">';names.forEach(function(n){h+='<div style="text-align:'+(n==='TF'?'left':'right')+'">'+n+'</div>'});return h+'</div>'}
  if(sp.some(function(s){return s.adx>0||s.rsi>0})){
    mh+='<div class="sect"><div class="sh">SPOT MULTI-TF</div>';
    mh+=hdr(4,['TF','ADX','RSI','SPREAD']);
    sp.forEach(function(t){if(!t.adx&&!t.rsi&&!t.spread)return;mh+='<div style="'+gr(4)+'"><div style="font-weight:700;color:var(--bl)">'+t.tf+'</div><div style="text-align:right;color:'+ac(t.adx)+'">'+t.adx+' <span style="font-size:7px">'+al(t.adx)+'</span></div><div style="text-align:right;color:'+rc(t.rsi)+'">'+t.rsi+'</div><div style="text-align:right;color:'+sc(t.spread)+'">'+(t.spread>0?'+':'')+t.spread+'</div></div>'});
    var trn=sp.filter(function(t){return t.adx>=25}).length,tot=sp.filter(function(t){return t.adx>0||t.rsi>0}).length;
    var up=sp.filter(function(t){return t.spread>0&&(t.adx>0||t.rsi>0)}).length,dn=sp.filter(function(t){return t.spread<0&&(t.adx>0||t.rsi>0)}).length;
    var vc=trn>=3?'var(--gn)':trn>=2?'var(--am)':'var(--rd)';
    mh+='<div style="padding:5px 10px;font-size:10px;font-weight:700;color:'+vc+'">'+(trn>=3?'STRONG':trn>=2?'MODERATE':'WEAK')+' '+trn+'/'+tot+(up>=3?' BULLISH':dn>=3?' BEARISH':'')+'</div></div>'}
  var ceStk=mtf.ce_strike||'',peStk=mtf.pe_strike||'';
  if(ceo.some(function(c){return c.rsi>0||c.ltp>0})){
    mh+='<div class="sect"><div class="sh">CE '+(ceStk||'')+' OPTION MULTI-TF</div>';
    mh+=hdr(7,['TF','ADX','RSI','BODY%','SPREAD','IV','LTP']);
    ceo.forEach(function(t){if(!t.rsi&&!t.ltp)return;mh+='<div style="'+gr(7)+'"><div style="font-weight:700;color:var(--gn)">'+t.tf+'</div><div style="text-align:right;color:'+ac(t.adx)+'">'+t.adx+'</div><div style="text-align:right;color:'+rc(t.rsi)+'">'+t.rsi+'</div><div style="text-align:right">'+(t.body||0)+'%</div><div style="text-align:right;color:'+sc(t.spread||0)+'">'+(t.spread>0?'+':'')+(t.spread||0)+'</div><div style="text-align:right">'+t.iv+'%</div><div style="text-align:right;color:var(--gn)">₹'+t.ltp+'</div></div>'});
    mh+='</div>'}
  if(peo.some(function(p){return p.rsi>0||p.ltp>0})){
    mh+='<div class="sect"><div class="sh">PE '+(peStk||'')+' OPTION MULTI-TF</div>';
    mh+=hdr(7,['TF','ADX','RSI','BODY%','SPREAD','IV','LTP']);
    peo.forEach(function(t){if(!t.rsi&&!t.ltp)return;mh+='<div style="'+gr(7)+'"><div style="font-weight:700;color:var(--rd)">'+t.tf+'</div><div style="text-align:right;color:'+ac(t.adx)+'">'+t.adx+'</div><div style="text-align:right;color:'+rc(t.rsi)+'">'+t.rsi+'</div><div style="text-align:right">'+(t.body||0)+'%</div><div style="text-align:right;color:'+sc(t.spread||0)+'">'+(t.spread>0?'+':'')+(t.spread||0)+'</div><div style="text-align:right">'+t.iv+'%</div><div style="text-align:right;color:var(--rd)">₹'+t.ltp+'</div></div>'});
    mh+='</div>'}

  // Fib Pivot Section — only render when data present
  var fp=mk.fib_pivots||{};
  if(fp.R3||fp.pivot){
    mh+='<div class="sect"><div class="sh">FIB PIVOTS · Nearest: '+(mk.fib_nearest||'—')+' ('+(mk.fib_distance>0?'+':'')+mk.fib_distance+'pts)</div>';
    var spot=mk.spot;
    function flvl(name,price){
      var dist=spot-price;var near=Math.abs(dist)<20;
      var clr=name.startsWith('R')?'var(--gn)':name.startsWith('S')?'var(--rd)':'var(--bl)';
      return '<div class="row" style="'+(near?'background:rgba(59,130,246,.08)':'')+'"><div class="k" style="color:'+clr+'">'+name+'</div><div class="v" style="font-size:11px">'+price+(near?' NEAR':' <span style=\'color:#555;font-size:9px\'>'+(dist>0?'+':'')+dist.toFixed(0)+'pts</span>')+'</div></div>';}
    mh+=flvl('R3',fp.R3||0)+flvl('R2',fp.R2||0)+flvl('R1',fp.R1||0)+flvl('PIVOT',fp.pivot||0)+flvl('S1',fp.S1||0)+flvl('S2',fp.S2||0)+flvl('S3',fp.S3||0);
    mh+='<div style="padding:5px 10px;font-size:9px;color:#555">Prev: H='+fp.prev_high+' L='+fp.prev_low+' C='+fp.prev_close+' Range='+fp.range+'pts</div>';
    mh+='</div>';
  }
  // Straddle + context
  mh+='<div class="ctx-row">'+
    '<div class="ctx"><div class="k">H.RSI</div><div class="v" style="color:'+(mk.hourly_rsi>70?'var(--rd)':mk.hourly_rsi<30?'var(--gn)':'')+'">'+mk.hourly_rsi+'</div></div>'+
    '<div class="ctx"><div class="k">STRADDLE</div><div class="v">'+(str.captured?'₹'+str.open:'—')+'</div></div>'+
    '<div class="ctx"><div class="k">EXPIRY</div><div class="v" style="font-size:10px">'+esc(mk.expiry||'—')+'</div></div>'+
    '<div class="ctx"><div class="k">SESSION</div><div class="v" style="font-size:10px">'+esc(mk.session)+'</div></div></div>';
  // Zones
  var zl=zones.zones||[];
  if(zl.length>0){
    var near=zl.filter(function(z){return Math.abs(z.distance_from_spot||999)<=100});
    mh+='<div class="sect"><div class="sh">DEMAND/SUPPLY ZONES</div>';
    if(near.length>0){
      near.forEach(function(z){
        var clr=z.zone_type==='DEMAND'?'var(--gn)':'var(--rd)';
        var icon=z.zone_type==='DEMAND'?'D':'S';
        mh+='<div class="row"><div class="k" style="color:'+clr+'">'+icon+' '+z.zone_type+'</div><div class="v" style="font-size:11px">'+z.zone_low+' - '+z.zone_high+' ['+z.strength+']'+(z.multi_tf?' MTF':'')+'</div></div>';
        mh+='<div class="row"><div class="k">Distance</div><div class="v" style="font-size:11px">'+(z.distance_from_spot>0?'+':'')+z.distance_from_spot+'pts · '+z.proximity+' · tested '+z.times_tested+'x</div></div>';
      });
    } else {
      mh+='<div style="padding:8px 10px;color:#555;font-size:10px">No zones within 100pts — open territory</div>';
    }
    mh+='<div style="padding:4px 10px;font-size:9px;color:#444">Total: '+zl.length+' active zones</div></div>';
  }
  // ── ACCOUNT + ROLLING ──
  var acct=d.account||{};
  var balAmt=Math.round(parseFloat(acct.balance||0));
  var usedAmt=Math.round(parseFloat(acct.used||0));
  var balClr=balAmt>=0?'var(--gn)':'var(--rd)';
  var balStr=(balAmt<0?'-₹':'₹')+Math.abs(balAmt).toLocaleString('en-IN')+(balAmt<0?' !':'');
  mh+='<div class="sect"><div class="sh">MSTOCK · '+esc(acct.name||'—')+'</div>'+
    '<div class="ctx-row">'+
    '<div class="ctx"><div class="k">AVAILABLE</div><div class="v" style="color:'+balClr+';font-size:11px">'+balStr+'</div></div>'+
    '<div class="ctx"><div class="k">USED MARGIN</div><div class="v" style="color:var(--am);font-size:11px">₹'+usedAmt.toLocaleString('en-IN')+'</div></div>'+
    '<div class="ctx"><div class="k">MODE</div><div class="v" style="color:'+(d.mode==='LIVE'?'var(--gn)':'var(--bl)')+'">'+esc(d.mode||'PAPER')+'</div></div>'+
    '<div class="ctx"><div class="k">VERSION</div><div class="v" style="color:var(--dm);font-size:10px">'+esc(d.version||'—')+'</div></div>'+
    '</div></div>';
  var l10c=rl.last10_wr>=60?'var(--gn)':rl.last10_wr>=40?'var(--am)':'var(--rd)';
  var l20c=rl.last20_wr>=60?'var(--gn)':rl.last20_wr>=40?'var(--am)':'var(--rd)';
  var ptsc=(rl.last10_pts||0)>=0?'var(--gn)':'var(--rd)';
  var strk=rl.streak||0;
  var strkTxt=strk>=2?(''+strk+'-WIN STREAK'):strk<=-2?(''+Math.abs(strk)+'-LOSS STREAK'):'No streak';
  var strkClr=strk>=2?'var(--gn)':strk<=-2?'var(--rd)':'var(--dm)';
  mh+='<div class="sect"><div class="sh">ROLLING PERFORMANCE</div>'+
    '<div class="ctx-row">'+
    '<div class="ctx"><div class="k">LAST 10 WR</div><div class="v" style="color:'+l10c+'">'+(rl.last10_wr||0)+'%</div></div>'+
    '<div class="ctx"><div class="k">LAST 20 WR</div><div class="v" style="color:'+l20c+'">'+(rl.last20_wr||0)+'%</div></div>'+
    '<div class="ctx"><div class="k">L10 PTS</div><div class="v" style="color:'+ptsc+'">'+(rl.last10_pts>=0?'+':'')+( rl.last10_pts||0)+'</div></div>'+
    '<div class="ctx"><div class="k">STREAK</div><div class="v" style="color:'+strkClr+';font-size:9px">'+strkTxt+'</div></div>'+
    '</div></div>';
  document.getElementById('p-mkt').innerHTML=mh;

  // ── TRADES TAB ──
  var th='';
  if(!trades||!trades.length){th='<div style="text-align:center;color:#444;padding:30px">No trades today</div>';}
  else{
    var cum=0,cumRs=0,tw=0,tl=0;
    var tcards=trades.map(function(t){
      var pts=parseFloat(t.pnl_pts||0),w=pts>0;cum+=pts;
      if(w)tw++;else tl++;
      var pk=parseFloat(t.peak_pnl||0);
      var held=t.candles_held||'?';
      var reason=esc((t.exit_reason||'').replace(/_/g,' '));
      var sym=esc((t.direction||'')+' '+(t.strike||''));
      var rs=Math.round(parseFloat(t.pnl_rs||0));cumRs+=rs;
      var dirClr=t.direction==='CE'?'var(--gn)':'var(--rd)';
      return '<div class="tc '+(w?'w':'l')+'" style="flex-direction:column;gap:4px">'+
        '<div style="display:flex;justify-content:space-between;width:100%;align-items:center">'+
        '<span style="font-size:14px">'+(w?'✅':'❌')+'</span>'+
        '<span style="font-weight:700;font-size:12px;color:'+dirClr+'">'+sym+'</span>'+
        '<span style="font-weight:700;color:'+(w?'var(--gn)':'var(--rd)')+';font-size:12px">'+(w?'+':'')+pts.toFixed(1)+'pts &#x20B9;'+(rs>=0?'+':'')+rs+'</span></div>'+
        '<div style="font-size:9px;color:#888;width:100%">'+esc(t.entry_time||'')+' → '+esc(t.exit_time||'')+' ('+held+'min)</div>'+
        '<div style="font-size:9px;color:#555;width:100%">'+reason+' | Peak: +'+pk.toFixed(1)+'pts | Entry: &#x20B9;'+esc(t.entry_price||'')+'</div>'+
        '</div>';
    }).join('');
    var totalT=tw+tl,wr=totalT>0?Math.round((tw/totalT)*100):0;
    th='<div style="margin:8px;padding:8px 10px;background:var(--c2);border:1px solid var(--bd);border-radius:6px;font-weight:700;color:'+(cum>=0?'var(--gn)':'var(--rd)')+'">'+
      (cum>=0?'+':'')+cum.toFixed(1)+'pts &#x20B9;'+(cumRs>=0?'+':'')+cumRs.toLocaleString('en-IN')+' | '+totalT+' trades | '+tw+'W '+tl+'L | WR '+wr+'%</div>';
    th+=tcards;
  }
  document.getElementById('p-trd').innerHTML=th;
  document.getElementById('ts').textContent=d.ts||new Date().toLocaleTimeString('en-IN')}

async function renderWeekly(){
  try{
    const data=await fetch('/api/weekly').then(r=>r.json()).catch(e=>[]);
    const el=document.getElementById('p-wkly');
    if(!data||!data.length){el.innerHTML='<div style="text-align:center;color:var(--dm);padding:30px">No weekly picks yet — runs every Sunday</div>';return;}
    const open=data.filter(d=>(d.status||'').includes('OPEN'));
    const closed=data.filter(d=>!(d.status||'').includes('OPEN'));
    // summary
    var totRet=0;var wins=0;
    open.forEach(function(p){var r=parseFloat(p.current_return_pct||0);totRet+=r;if(r>0)wins++;});
    var avgRet=open.length?totRet/open.length:0;
    var avgClr=avgRet>=0?'var(--gn)':'var(--rd)';
    // cards
    var cards=open.sort(function(a,b){return parseFloat(b.current_return_pct||0)-parseFloat(a.current_return_pct||0);}).map(function(p){
      var ret=parseFloat(p.current_return_pct||0);
      var cur=parseFloat(p.current_price||0);
      var entry=parseFloat(p.entry_price||0);
      var sl=parseFloat(p.sl||0);
      var peg=parseFloat(p.peg||0);
      var prom=parseFloat(p.promoter||0);
      var w=ret>=0;var clr=w?'var(--gn)':'var(--rd)';
      var mon=p.mon_status||'HOLD';
      var monClr=mon==='HOLD'?'var(--gn)':(mon.indexOf('CRASH')>=0?'var(--rd)':'var(--am)');
      return '<div style="margin:5px 8px;background:var(--c1);border:1px solid var(--bd);border-left:3px solid '+clr+';border-radius:8px;padding:9px 10px 7px">'+
        '<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:4px">'+
          '<div><span style="font-size:14px;font-weight:800;color:var(--tx)">'+esc(p.symbol)+'</span>'+
          '<div style="font-size:9px;color:var(--dm);margin-top:1px">₹'+entry.toFixed(0)+' → ₹'+(cur||entry).toFixed(0)+'</div></div>'+
          '<div style="text-align:right"><span style="font-weight:800;font-size:18px;color:'+clr+'">'+(w?'+':'')+ret.toFixed(1)+'%</span></div>'+
        '</div>'+
        '<div style="display:flex;gap:4px;margin-bottom:4px">'+
          '<div style="flex:1;text-align:center;padding:4px;border-radius:6px;background:rgba(192,57,43,.06);border:1px solid rgba(192,57,43,.15)"><div style="font-size:7px;color:var(--dm)">SL</div><div style="font-size:10px;font-weight:700;color:var(--rd)">₹'+sl.toFixed(0)+'</div></div>'+
          '<div style="flex:1;text-align:center;padding:4px;border-radius:6px;background:rgba(10,122,80,.06);border:1px solid rgba(10,122,80,.15)"><div style="font-size:7px;color:var(--dm)">T1 (1Y)</div><div style="font-size:10px;font-weight:700;color:var(--gn)">₹'+parseFloat(p.target_1y||0).toFixed(0)+'</div></div>'+
          '<div style="flex:1;text-align:center;padding:4px;border-radius:6px;background:rgba(26,107,191,.06);border:1px solid rgba(26,107,191,.15)"><div style="font-size:7px;color:var(--dm)">T3 (3Y)</div><div style="font-size:10px;font-weight:700;color:var(--bl)">₹'+parseFloat(p.target_3y||0).toFixed(0)+'</div></div>'+
          '<div style="flex:1;text-align:center;padding:4px;border-radius:6px;background:rgba(0,0,0,.03);border:1px solid var(--bd)"><div style="font-size:7px;color:var(--dm)">PEG</div><div style="font-size:10px;font-weight:700">'+(peg>0?peg.toFixed(1):'—')+'</div></div>'+
          '<div style="flex:1;text-align:center;padding:4px;border-radius:6px;background:rgba(0,0,0,.03);border:1px solid var(--bd)"><div style="font-size:7px;color:var(--dm)">PROM</div><div style="font-size:10px;font-weight:700">'+(prom>0?prom.toFixed(0)+'%':'—')+'</div></div>'+
        '</div>'+
        '<div style="display:flex;justify-content:space-between;font-size:8px;color:var(--dm)">'+
          '<span style="font-weight:600;color:'+monClr+'">'+esc(mon)+'</span>'+
          '<span>'+esc(p.last_updated||p.date_added||'')+'</span>'+
        '</div></div>';
    }).join('');
    // closed
    var closedHtml='';
    if(closed.length){
      closedHtml='<div style="padding:6px 10px;font-size:9px;font-weight:700;color:var(--dm);text-transform:uppercase;letter-spacing:.5px;border-top:1px solid var(--bd);margin-top:6px">Closed ('+closed.length+')</div>'+
      closed.map(function(p){
        var ret=parseFloat(p.current_return_pct||p.actual_return||0);var w=ret>=0;
        return '<div style="margin:3px 8px;padding:6px 10px;background:var(--c1);border:1px solid var(--bd);border-radius:6px;display:flex;justify-content:space-between;align-items:center">'+
          '<span style="font-weight:700;font-size:12px">'+esc(p.symbol)+'</span>'+
          '<span style="font-weight:700;color:'+(w?'var(--gn)':'var(--rd)')+'">'+(w?'+':'')+ret.toFixed(1)+'%</span></div>';
      }).join('');
    }
    el.innerHTML=
      '<div style="margin:8px;padding:10px 12px;background:var(--c1);border:1px solid var(--bd);border-radius:8px;display:flex;justify-content:space-between;align-items:center">'+
        '<div><div style="font-size:9px;color:var(--dm)">MULTIBAGGER PICKS</div>'+
        '<div style="font-weight:700;font-size:14px">'+open.length+' Open · '+wins+'W/'+( open.length-wins)+'L</div></div>'+
        '<div style="text-align:right"><div style="font-size:9px;color:var(--dm)">AVG RETURN</div>'+
        '<div style="font-weight:800;font-size:16px;color:'+avgClr+'">'+(avgRet>=0?'+':'')+avgRet.toFixed(1)+'%</div></div>'+
      '</div>'+cards+closedHtml;
  }catch(e){document.getElementById('p-wkly').innerHTML='<div style="color:var(--dm);padding:16px">Error loading weekly data</div>';console.error(e);}
}

async function renderFno(){
  try{
    const fno=await fetch('/api/fno').then(r=>r.json()).catch(e=>[]);
    const el=document.getElementById('p-fno');
    if(!fno||!fno.length){el.innerHTML='<div style="text-align:center;color:var(--dm);padding:30px">No F&O positions</div>';return;}
    // Split today vs prev days; open sorted by score desc (all pnl=0 initially); closed by pnl
    var today=new Date().toISOString().slice(0,10);
    var byScore=function(a,b){return (b.score||0)-(a.score||0)||((b.rank||99)-(a.rank||99))*-1;};
    var byPnl=function(a,b){return parseFloat(b.pnl_rs||0)-parseFloat(a.pnl_rs||0);};
    var openToday=fno.filter(function(p){return (p.status||'').startsWith('OPEN')&&p.date_added===today;}).sort(byScore);
    var openPrev=fno.filter(function(p){return (p.status||'').startsWith('OPEN')&&p.date_added!==today;}).sort(byScore);
    var closedPos=fno.filter(function(p){return !(p.status||'').startsWith('OPEN');}).sort(byPnl);
    var openPos=openToday.concat(openPrev);
    var totalPnl=0;var openCount=openPos.length;
    function makeCard(p){
      var ltp=parseFloat(p.current_premium||p.entry_premium||0);
      var entry=parseFloat(p.entry_premium||0);
      var sl=parseFloat(p.sl_premium||0);
      var t1=parseFloat(p.t1_premium||0);
      var t2=parseFloat(p.t2_premium||0);
      var pnlPct=parseFloat(p.current_return_pct||0);
      var pnlRs=parseFloat(p.pnl_rs||0);
      var score=p.score||0;
      var rank=p.rank||0;
      var stockPx=parseFloat(p.stock_price||0);
      var stockSl=parseFloat(p.stock_sl||0);
      var pcr=parseFloat(p.pcr||0);
      var w=pnlPct>=0;var clr=w?'var(--gn)':'var(--rd)';var sign=w?'+':'';
      var st=p.status||'';
      var isOpen=st.startsWith('OPEN');
      var isT1=st.includes('T1-HIT');var isSl=st.includes('SL-HIT');
      var cardBorder=isT1?'rgba(52,211,153,.4)':isSl?'rgba(248,113,113,.4)':'var(--bd)';
      var badgeBg=isT1?'rgba(52,211,153,.15)':isSl?'rgba(248,113,113,.15)':'rgba(0,0,0,.05)';
      var badgeClr=isT1?'var(--gn)':isSl?'var(--rd)':'var(--dm)';
      var dirClr=p.direction==='CALL'?'var(--bl)':'var(--rd)';
      var scoreClr=score>=9?'var(--gn)':score>=7?'var(--am)':'#888';
      var range=t2-sl;
      var pct=range>0?Math.max(0,Math.min(100,((ltp-sl)/range)*100)):0;
      var barClr=isSl?'var(--rd)':isT1?'var(--gn)':pct>=70?'var(--gn)':pct>=35?'var(--am)':'var(--rd)';
      var t1Pct=range>0?Math.max(0,Math.min(100,((t1-sl)/range)*100)):0;
      var dirIcon=p.direction==='CALL'?'🟢':'🔴';
      return '<div style="margin:6px 8px;background:var(--c1);border:1px solid '+cardBorder+';border-left:3px solid '+dirClr+';border-radius:8px;padding:10px 10px 8px">'+
        '<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:6px">'+
          '<div><span style="font-size:14px;font-weight:800;color:var(--tx)">'+dirIcon+' '+esc(p.symbol)+'</span>'+
          '<span style="font-size:10px;font-weight:700;color:'+dirClr+';margin-left:5px">'+esc(p.direction)+' '+(p.strike||'')+'</span>'+
          '<div style="font-size:9px;color:var(--dm);margin-top:1px">₹'+entry.toFixed(0)+' → ₹'+ltp.toFixed(0)+'</div></div>'+
          '<div style="text-align:right"><span style="font-weight:800;font-size:18px;color:'+clr+'">'+sign+pnlPct.toFixed(0)+'%</span>'+
          '<div style="font-size:10px;color:'+clr+'">'+sign+'₹'+Math.abs(Math.round(pnlRs)).toLocaleString('en-IN')+'</div></div>'+
        '</div>'+
        '<div style="position:relative;height:6px;background:var(--c2);border-radius:3px;overflow:visible;margin:0 0 3px">'+
          '<div style="height:100%;width:'+pct.toFixed(0)+'%;background:'+barClr+';border-radius:3px;transition:width .5s"></div>'+
          '<div style="position:absolute;top:-4px;left:0;width:2px;height:14px;background:var(--rd);border-radius:1px" title="SL ₹'+sl.toFixed(0)+'"></div>'+
          (t1Pct>0&&t1Pct<100?'<div style="position:absolute;top:-4px;left:'+t1Pct.toFixed(0)+'%;width:2px;height:14px;background:var(--am);border-radius:1px" title="T1 ₹'+t1.toFixed(0)+'"></div>':'')+
          '<div style="position:absolute;top:-4px;right:0;width:2px;height:14px;background:var(--gn);border-radius:1px" title="T2 ₹'+t2.toFixed(0)+'"></div>'+
        '</div>'+
        '<div style="display:flex;justify-content:space-between;font-size:8px;color:var(--dm)">'+
          '<span>SL ₹'+sl.toFixed(0)+'</span><span>T1 ₹'+t1.toFixed(0)+'</span><span>T2 ₹'+t2.toFixed(0)+'</span>'+
        '</div>'+
        '<div style="display:flex;justify-content:space-between;font-size:8px;color:var(--dm);margin-top:3px">'+
          '<span style="background:'+badgeBg+';color:'+badgeClr+';padding:1px 6px;border-radius:3px;font-weight:600">'+esc(st)+'</span>'+
          '<span>'+esc(p.date_added)+'</span>'+
        '</div></div>';
    }
    var allPos=openPos.concat(closedPos);
    allPos.forEach(function(p){totalPnl+=parseFloat(p.pnl_rs||0);});
    var todayCards=openToday.map(makeCard).join('');
    var prevCards=openPrev.map(makeCard).join('');
    var closedCards=closedPos.map(makeCard).join('');
    var prevSection=openPrev.length?
      '<div style="margin:10px 8px 4px;font-size:10px;font-weight:700;color:var(--dm);text-transform:uppercase;letter-spacing:.5px;border-top:1px solid var(--bd);padding-top:8px">Previous Days Open ('+openPrev.length+')</div>'+prevCards:'';
    var closedSection=closedPos.length?
      '<div style="margin:10px 8px 4px;font-size:10px;font-weight:700;color:var(--dm);text-transform:uppercase;letter-spacing:.5px;border-top:1px solid var(--bd);padding-top:8px">Closed / Target Hit ('+closedPos.length+')</div>'+closedCards:'';
    var totSign=totalPnl>=0?'+':'';var totClr=totalPnl>=0?'var(--gn)':'var(--rd)';
    el.innerHTML=
      '<div style="margin:8px;padding:10px 12px;background:var(--c1);border:1px solid var(--bd);border-radius:8px;display:flex;justify-content:space-between;align-items:center">'+
        '<div><div style="font-size:9px;color:var(--dm);margin-bottom:2px">TOTAL F&amp;O P&amp;L</div>'+
        '<div style="font-weight:700;font-size:18px;color:'+totClr+'">'+totSign+'&#x20B9;'+Math.abs(Math.round(totalPnl)).toLocaleString('en-IN')+'</div></div>'+
        '<div style="text-align:right;font-size:10px;color:var(--dm)">'+openCount+' open &nbsp;\xb7&nbsp; '+closedPos.length+' closed</div>'+
      '</div>'+
      (openToday.length?'<div style="margin:4px 8px;font-size:10px;font-weight:700;color:var(--bl);text-transform:uppercase;letter-spacing:.5px">Today\'s Picks ('+openToday.length+') — sorted by score</div>':'')+
      todayCards+prevSection+closedSection;
  }catch(e){document.getElementById('p-fno').innerHTML='<div style="color:var(--dm);padding:16px">Error loading F&O data</div>';console.error(e);}
}

async function loadFiles(folder){
  const d=await fetch('/api/files'+(folder?'?folder='+folder:'')).then(r=>r.json()).catch(e=>null);
  const el=document.getElementById('p-fil');
  if(!d){el.innerHTML='<div style="text-align:center;color:#555;padding:20px">Error loading files</div>';return}
  if(!folder&&d.folders){
    el.innerHTML='<div style="padding:8px 10px;font-size:11px;font-weight:700;color:var(--dm)">SELECT FOLDER</div>'+
      d.folders.map(function(f){return '<div onclick="loadFiles(\x27'+f.key+'\x27)" style="margin:3px 8px;padding:10px;background:var(--c1);border:1px solid var(--bd);border-radius:6px;cursor:pointer;display:flex;justify-content:space-between;align-items:center"><span style="font-weight:700;font-size:12px">'+f.name+'</span><span style="color:#555;font-size:18px">></span></div>'}).join('');
    return}
  var h='<div onclick="loadFiles(\x27\x27)" style="margin:8px;padding:8px 10px;background:var(--c2);border:1px solid var(--bd);border-radius:6px;cursor:pointer;font-size:11px;color:var(--bl)">Back</div>';
  h+='<div style="padding:4px 10px;font-size:11px;font-weight:700;color:var(--dm)">'+(d.folder_name||folder)+' ('+d.files.length+' files)</div>';
  if(!d.files.length){h+='<div style="text-align:center;color:#555;padding:20px">No files</div>'}
  else{d.files.forEach(function(f){h+='<a href="/api/download/'+f.path+'" style="display:block;margin:2px 8px;padding:8px 10px;background:var(--c1);border:1px solid var(--bd);border-radius:6px;text-decoration:none;color:var(--tx)"><span style="font-size:11px">'+f.name+'</span><span style="float:right;color:#555;font-size:10px">'+f.size+'KB</span></a>'})}
  el.innerHTML=h}

async function go(){
  try{
    const[d,t,z,mtf,sh]=await Promise.all([fetch('/api/dashboard').then(r=>r.json()).catch(e=>null),fetch('/api/trades').then(r=>r.json()).catch(e=>[]),fetch('/api/zones').then(r=>r.json()).catch(e=>({zones:[]})),fetch('/api/multitf').then(r=>r.json()).catch(e=>({spot:[],ce:[],pe:[]})),fetch('/api/shadow').then(r=>r.json()).catch(e=>({}))]);
    window._shadow=sh||{};
    render(d||{},t||[],z||{zones:[]},mtf||{});
    if(_curTab==='fno')renderFno();
    if(_curTab==='wkly')renderWeekly();
  }catch(e){console.error(e)}
}
go();setInterval(go,10000);
</script></body></html>"""

class _WebHandler(BaseHTTPRequestHandler):
    def log_message(self,*a):pass
    def _j(self,d):
        self.send_response(200)
        self.send_header("Content-Type","application/json")
        self.send_header("Access-Control-Allow-Origin","*")
        self.send_header("Cache-Control","no-cache, no-store, must-revalidate")
        self.send_header("Pragma","no-cache")
        self.send_header("Expires","0")
        self.end_headers()
        self.wfile.write(json.dumps(d,default=str).encode())
    def _send_file(self, path):
        try:
            parts = path.split("/", 1)
            if len(parts) != 2:
                self.send_error(404); return
            folder_key, filename = parts[0], os.path.basename(parts[1])
            info = _WEB_FOLDERS.get(folder_key)
            if not info:
                self.send_error(404); return
            filepath = os.path.realpath(os.path.join(info[1], filename))
            if not filepath.startswith(os.path.realpath(info[1])):
                self.send_error(403); return
            if not os.path.isfile(filepath):
                self.send_error(404); return
            self.send_response(200)
            if filename.endswith(".csv"):
                self.send_header("Content-Type", "text/csv")
            elif filename.endswith(".json"):
                self.send_header("Content-Type", "application/json")
            elif filename.endswith(".log"):
                self.send_header("Content-Type", "text/plain")
            else:
                self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Disposition", "attachment; filename=" + filename)
            self.send_header("Content-Length", str(os.path.getsize(filepath)))
            self.end_headers()
            with open(filepath, "rb") as f:
                self.wfile.write(f.read())
        except Exception as e:
            self.send_error(500)

    def _download_daily_logs(self):
        q = _web_parse_qs(_web_urlparse(self.path).query)
        target_date = q.get("date", [None])[0]
        if target_date is None:
            target_date = date.today().strftime("%Y-%m-%d")
        zip_path = create_daily_zip(target_date)
        if not zip_path or not os.path.isfile(zip_path):
            self.send_error(404, "No logs found for " + target_date)
            return
        try:
            fname = os.path.basename(zip_path)
            fsize = os.path.getsize(zip_path)
            self.send_response(200)
            self.send_header("Content-Type", "application/zip")
            self.send_header("Content-Disposition", "attachment; filename=" + fname)
            self.send_header("Content-Length", str(fsize))
            self.end_headers()
            with open(zip_path, "rb") as f:
                self.wfile.write(f.read())
            try:
                os.remove(zip_path)
            except Exception:
                pass
        except Exception:
            self.send_error(500)

    def _files_page(self):
        import urllib.parse
        import time as _t
        q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        folder = q.get("f",[""])[0]
        today_str = date.today().strftime("%Y%m%d")
        today_iso = date.today().isoformat()

        css = ('<html><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>VRL Files</title>'
               '<style>'
               'body{background:#080810;color:#e4e4e7;font-family:monospace;font-size:13px;padding:10px;max-width:520px;margin:0 auto}'
               'a{color:#3b82f6;text-decoration:none}'
               '.f{display:block;margin:4px 0;padding:10px 12px;background:#111118;border:1px solid #1e1e30;border-radius:6px}'
               '.f:active{background:#1e1e30}'
               '.sz{float:right;color:#555;font-size:11px}'
               '.bk{display:inline-block;margin:8px 4px;padding:6px 12px;background:#1e1e30;border-radius:6px}'
               '.sh{color:#888;font-size:11px;margin:16px 0 6px;text-transform:uppercase;letter-spacing:1px}'
               '.badge{background:#22c55e;color:#000;padding:1px 6px;border-radius:8px;font-size:10px;margin-left:6px}'
               '.badge-r{background:#ef4444}'
               '.cnt{color:#555;font-size:11px;margin-left:6px}'
               '</style></head><body>')

        html = css
        html += '<h2 style="color:#3b82f6;font-size:15px">VISHAL RAJPUT FILES</h2>'
        html += '<a href="/" class="bk">War Room</a>'

        if not folder:
            html += '<div class="sh">TODAY (' + today_iso + ')</div>'
            trade_count = 0
            tl_path = os.path.join(_WEB_BASE, "lab_data", "vrl_trade_log.csv")
            if os.path.isfile(tl_path):
                try:
                    with open(tl_path) as _tf:
                        for r in csv.DictReader(_tf):
                            if r.get("date") == today_iso:
                                trade_count += 1
                except Exception:
                    pass
            today_items = [
                ("Today's Option Data", "options_1min", "nifty_option_1min_" + today_str),
                ("Today's Spot Data", "spot", "nifty_spot_1min_" + today_str),
                ("Today's Trades", "trade_log", None),
                ("Today's Scan Log", "options_1min", "nifty_signal_scan_" + today_str),
            ]
            for label, fkey, prefix in today_items:
                badge = ""
                if "Trades" in label and trade_count > 0:
                    badge = '<span class="badge">' + str(trade_count) + '</span>'
                html += '<a href="/files?f=' + fkey + '" class="f">' + label + badge + '</a>'
            html += '<div class="sh">HISTORICAL DATA</div>'
            hist_items = [
                ("spot", "Spot (1m/5m/15m/D)"),
                ("options_3min", "Options 3-Min CE+PE"),
                ("options_1min", "Options 1m/5m/15m/Scan"),
                ("reports", "Daily Summary Reports"),
            ]
            for fkey, label in hist_items:
                info = _WEB_FOLDERS.get(fkey)
                cnt = ""
                if info and os.path.isdir(info[1]):
                    try:
                        n = len([f for f in os.listdir(info[1]) if os.path.isfile(os.path.join(info[1], f)) and os.path.getsize(os.path.join(info[1], f)) > 0])
                        cnt = '<span class="cnt">' + str(n) + ' files</span>'
                    except Exception:
                        pass
                html += '<a href="/files?f=' + fkey + '" class="f">' + label + cnt + '</a>'
            html += '<div class="sh">ANALYSIS</div>'
            analysis_items = [
                ("research", "Demand/Supply Zones"),
                ("trade_log", "Full Trade History"),
            ]
            for fkey, label in analysis_items:
                html += '<a href="/files?f=' + fkey + '" class="f">' + label + '</a>'
            html += '<div class="sh">SYSTEM</div>'
            system_items = [
                ("state", "State + Config"),
                ("logs", "Logs"),
            ]
            for fkey, label in system_items:
                html += '<a href="/files?f=' + fkey + '" class="f">' + label + '</a>'
        else:
            html += '<a href="/files" class="bk">Back</a>'
            info = _WEB_FOLDERS.get(folder)
            if info and os.path.isdir(info[1]):
                html += '<h3 style="color:#888;font-size:12px">' + info[0] + '</h3>'
                files = sorted(os.listdir(info[1]), reverse=True)
                file_list = []
                for fname in files:
                    fp = os.path.join(info[1], fname)
                    if os.path.isfile(fp) and os.path.getsize(fp) > 0:
                        sz = os.path.getsize(fp)
                        mt = os.path.getmtime(fp)
                        file_list.append((fname, sz, mt, fp))
                file_list.sort(key=lambda x: x[2], reverse=True)
                if not file_list:
                    html += '<div style="color:#555;padding:20px">No files found</div>'
                for fname, sz, mt, fp in file_list[:50]:
                    sz_str = str(round(sz / 1024, 1)) + ' KB' if sz < 1024*1024 else str(round(sz / (1024*1024), 1)) + ' MB'
                    mod = _t.strftime('%d %b %H:%M', _t.localtime(mt))
                    is_today = today_str in fname
                    style = ' style="border-left:3px solid #22c55e"' if is_today else ''
                    html += '<a href="/api/download/' + folder + '/' + fname + '" class="f"' + style + '>' + fname + '<span class="sz">' + sz_str + ' · ' + mod + '</span></a>'
            else:
                html += '<div style="color:#555;padding:20px">Folder not found</div>'

        html += '</body></html>'
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(html.encode())

    def _get_session(self):
        cookie = self.headers.get("Cookie", "")
        return _web_get_session(cookie)

    def _require_auth(self, admin_only=False):
        sess = self._get_session()
        if not sess:
            self._redirect("/login")
            return None
        if admin_only and sess.get("role") != "admin":
            self.send_error(403, "Admin access required")
            return None
        return sess

    def _redirect(self, url):
        self.send_response(302)
        self.send_header("Location", url)
        self.end_headers()

    def _html(self, html, code=200):
        self.send_response(code)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(html.encode())

    def _handle_login_get(self):
        if self._get_session():
            self._redirect("/")
            return
        self._html(_WEB_LOGIN_HTML.replace("ERR_MSG", "").replace('display:none', 'display:none'))

    def _handle_login_post(self):
        ip = self.client_address[0]
        now = time.time()
        attempts = _web_login_attempts.get(ip, [])
        attempts = [t for t in attempts if now - t < 60]
        if len(attempts) >= _WEB_LOGIN_LIMIT:
            self._html(_WEB_LOGIN_HTML.replace("ERR_MSG", "Too many attempts. Wait 15 minutes.").replace('display:none', ''), 429)
            return
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length).decode()
        params = dict(p.split("=", 1) for p in body.split("&") if "=" in p)
        from urllib.parse import unquote_plus
        username = unquote_plus(params.get("username", ""))
        password = unquote_plus(params.get("password", ""))
        pass_hash = _web_hashlib.sha256(password.encode()).hexdigest()
        if username == _WEB_ADMIN_USER and _WEB_ADMIN_PASS_HASH and pass_hash == _WEB_ADMIN_PASS_HASH:
            token = _web_create_session(username, "admin", days=30)
            self.send_response(302)
            self.send_header("Location", "/")
            self.send_header("Set-Cookie", "vrl_session=" + token + "; Path=/; Max-Age=2592000; HttpOnly")
            self.end_headers()
            return
        _web_login_attempts.setdefault(ip, []).append(now)
        self._html(_WEB_LOGIN_HTML.replace("ERR_MSG", "Invalid username or password").replace('display:none', ''), 401)

    def _handle_subscriber_token(self, token):
        ip = self.client_address[0]
        try:
            result = _DB.validate_token(token, ip=ip)
        except Exception:
            result = None
        if result is None:
            self._html(_WEB_TOKEN_ERROR_HTML.replace("MSG_TITLE", "Invalid Link").replace("MSG_BODY", "This access link is not valid."), 404)
            return
        if result.get("revoked"):
            self._html(_WEB_TOKEN_ERROR_HTML.replace("MSG_TITLE", "Access Revoked").replace("MSG_BODY", "Your access has been revoked. Contact Vishal Rajput."), 403)
            return
        if result.get("expired"):
            self._html(_WEB_TOKEN_ERROR_HTML.replace("MSG_TITLE", "Access Expired").replace("MSG_BODY", "Your access has expired. Contact Vishal Rajput to renew."), 403)
            return
        if result.get("valid"):
            if result.get("sharing_alert"):
                try:
                    _tg_token = os.environ.get("TG_TOKEN", "")
                    _tg_chat = os.environ.get("TG_GROUP_ID", "")
                    if not _tg_token:
                        with open(os.path.join(_WEB_BASE, ".env")) as _ef2:
                            for _ln in _ef2:
                                if _ln.startswith("TG_TOKEN="):
                                    _tg_token = _ln.strip().split("=", 1)[1]
                                elif _ln.startswith("TG_GROUP_ID="):
                                    _tg_chat = _ln.strip().split("=", 1)[1]
                    if _tg_token and _tg_chat:
                        requests.post("https://api.telegram.org/bot" + _tg_token + "/sendMessage",
                                      json={"chat_id": _tg_chat,
                                            "text": "SHARING ALERT\n"
                                                    + result["name"] + "'s token used from "
                                                    + str(result.get("unique_ips", 0)) + " unique IPs\n"
                                                    + "Latest: " + ip + "\n"
                                                    + "Use /token revoke " + result["name"] + " to block",
                                            "parse_mode": "HTML"}, timeout=5)
                except Exception:
                    pass
            sess_token = _web_create_session(result["name"], "subscriber", days=30)
            self.send_response(302)
            self.send_header("Location", "/")
            self.send_header("Set-Cookie", "vrl_session=" + sess_token + "; Path=/; Max-Age=2592000; HttpOnly")
            self.end_headers()

    def _handle_logout(self):
        cookie = self.headers.get("Cookie", "")
        try:
            c = _web_SimpleCookie()
            c.load(cookie)
            if "vrl_session" in c:
                token = c["vrl_session"].value
                with _web_sessions_lock:
                    _web_sessions.pop(token, None)
                _web_save_sessions()
        except Exception:
            pass
        self.send_response(302)
        self.send_header("Location", "/login")
        self.send_header("Set-Cookie", "vrl_session=; Path=/; Max-Age=0")
        self.end_headers()

    def _handle_viewers(self):
        sess = self._require_auth(admin_only=True)
        if not sess:
            return
        try:
            tokens = _DB.list_tokens()
        except Exception:
            tokens = []
        active = [t for t in tokens if t.get("active")]
        with _web_sessions_lock:
            active_sessions = len(_web_sessions)
        self._j({"tokens": tokens, "active_sessions": active_sessions})

    def _db_trades(self):
        q = _web_parse_qs(_web_urlparse(self.path).query)
        d = q.get("date", [None])[0]
        try:
            self._j(_DB.get_trades(d))
        except Exception as e:
            self._j({"error": str(e)})

    def _db_scans(self):
        q = _web_parse_qs(_web_urlparse(self.path).query)
        d = q.get("date", [None])[0]
        direction = q.get("direction", [None])[0]
        try:
            self._j(_DB.get_scans(d, direction))
        except Exception as e:
            self._j({"error": str(e)})

    def _db_spot(self):
        q = _web_parse_qs(_web_urlparse(self.path).query)
        tf = q.get("tf", ["1min"])[0]
        table_map = {"1min": "spot_1min", "5min": "spot_5min", "15min": "spot_15min",
                     "60min": "spot_60min", "daily": "spot_daily"}
        table = table_map.get(tf, "spot_1min")
        from_ts = q.get("from", [None])[0]
        to_ts = q.get("to", [None])[0]
        try:
            self._j(_DB.get_spot(table, from_ts, to_ts))
        except Exception as e:
            self._j({"error": str(e)})

    def _db_stats(self):
        q = _web_parse_qs(_web_urlparse(self.path).query)
        d = q.get("date", [None])[0]
        try:
            self._j(_DB.get_stats(d))
        except Exception as e:
            self._j({"error": str(e)})

    def do_POST(self):
        p = _web_urlparse(self.path).path
        if p == "/login":
            self._handle_login_post()
        else:
            self.send_error(404)

    def do_GET(self):
        p=_web_urlparse(self.path).path

        if p == "/login":
            self._handle_login_get(); return
        if p == "/logout":
            self._handle_logout(); return
        if p.startswith("/s/"):
            token = p[3:]
            self._handle_subscriber_token(token); return

        if not _WEB_ADMIN_PASS_HASH:
            pass
        else:
            sess = self._get_session()
            if not sess:
                self._redirect("/login"); return
            if p in ("/files",) or p.startswith("/files?") or p.startswith("/api/download/") \
               or p.startswith("/api/logs/") or p.startswith("/api/db/") or p.startswith("/api/files"):
                if sess.get("role") != "admin":
                    self.send_error(403, "Admin access required"); return

        if p=="/api/viewers":
            self._handle_viewers(); return
        if p=="/files" or p.startswith("/files?"):self._files_page();return
        if p in("/","/dashboard"):
            static_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", "VRL_DASHBOARD.html")
            if os.path.isfile(static_path):
                self.send_response(200)
                self.send_header("Content-Type","text/html")
                self.send_header("Cache-Control","no-cache, no-store, must-revalidate")
                self.end_headers()
                with open(static_path, "rb") as sf:
                    self.wfile.write(sf.read())
            else:
                self.send_response(200)
                self.send_header("Content-Type","text/html")
                self.send_header("Cache-Control","no-cache, no-store, must-revalidate")
                self.end_headers()
                self.wfile.write(_WEB_HTML.encode())
        elif p=="/api/dashboard":self._j(_web_read_dash())
        elif p=="/api/trades":self._j(_web_read_trades())
        elif p=="/api/multitf":self._j(_web_read_multitf())
        elif p=="/api/fno":self._j(_web_read_fno())
        elif p=="/api/weekly":self._j(_web_read_weekly())
        elif p=="/api/shadow":self._j(_web_read_shadow())
        elif p.startswith("/static/"):
            _allowed = {".jpg":"image/jpeg",".jpeg":"image/jpeg",".png":"image/png",
                        ".webp":"image/webp",".svg":"image/svg+xml",
                        ".css":"text/css",".js":"application/javascript",
                        ".ico":"image/x-icon"}
            _name = p[len("/static/"):]
            if "/" in _name or "\\" in _name or ".." in _name:
                self.send_error(403); return
            _ext = "." + _name.rsplit(".", 1)[-1].lower() if "." in _name else ""
            if _ext not in _allowed:
                self.send_error(404); return
            _path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", _name)
            if not os.path.isfile(_path):
                self.send_error(404); return
            self.send_response(200)
            self.send_header("Content-Type", _allowed[_ext])
            self.send_header("Cache-Control", "public, max-age=86400")
            self.end_headers()
            with open(_path, "rb") as _sf:
                self.wfile.write(_sf.read())
            return
        elif p=="/api/zones":
            zp = os.path.join(_WEB_STATE_DIR, "vrl_zones.json")
            if os.path.isfile(zp):
                with open(zp) as _zf:
                    self._j(json.load(_zf))
            else:
                self._j({"zones":[]})
        elif p=="/api/files":
            q = _web_urlparse(self.path).query
            folder = ''
            if 'folder=' in q:
                folder = q.split('folder=')[1].split('&')[0]
            self._j(_web_list_files(folder))
        elif p.startswith("/api/download/"):
            self._send_file(p[14:])
        elif p == "/api/logs/download" or p.startswith("/api/logs/download?"):
            self._download_daily_logs()
        elif p == "/api/db/trades" or p.startswith("/api/db/trades?"):
            self._db_trades()
        elif p == "/api/db/scans" or p.startswith("/api/db/scans?"):
            self._db_scans()
        elif p == "/api/db/spot" or p.startswith("/api/db/spot?"):
            self._db_spot()
        elif p == "/api/db/stats" or p.startswith("/api/db/stats?"):
            self._db_stats()
        else:self.send_error(404)

def _web_bind_host():
    if _WEB_ADMIN_PASS_HASH:
        return "0.0.0.0"
    msg = ("CRITICAL: VRL_DASHBOARD_PASS missing from ~/.env — "
           "ADMIN_PASS_HASH is empty. Binding to 127.0.0.1 only.")
    print("[VRL_WEB] " + msg, flush=True)
    try:
        _web_logger.critical(msg)
    except Exception:
        pass
    try:
        _tok = ""
        _cid = ""
        _envp = os.path.join(_WEB_BASE, ".env")
        if os.path.isfile(_envp):
            with open(_envp) as _ef:
                for _line in _ef:
                    _line = _line.strip()
                    if _line.startswith("TG_TOKEN="):
                        _tok = _line.split("=", 1)[1].strip().strip('"\'')
                    elif _line.startswith("TG_GROUP_ID="):
                        _cid = _line.split("=", 1)[1].strip().strip('"\'')
        if _tok and _cid:
            import urllib.request as _ur
            import urllib.parse as _up
            _payload = _up.urlencode({
                "chat_id": _cid,
                "text": "VRL_WEB started on 127.0.0.1 only — "
                        "VRL_DASHBOARD_PASS missing. Set it in ~/.env "
                        "and restart to re-expose the dashboard."
            }).encode()
            _req = _ur.Request(
                "https://api.telegram.org/bot" + _tok + "/sendMessage",
                data=_payload, method="POST")
            _ur.urlopen(_req, timeout=5).read()
    except Exception:
        pass
    return "127.0.0.1"


def _start_web_server():
    """Start the web dashboard server (called as daemon thread from main())."""
    # Sync static HTML on startup
    _static_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", "VRL_DASHBOARD.html")
    try:
        os.makedirs(os.path.dirname(_static_path), exist_ok=True)
        with open(_static_path, "w") as _sf:
            _sf.write(_WEB_HTML)
    except Exception as _e:
        print(f"[WARN] Could not sync static file: {_e}")

    _web_load_sessions()
    # Start session cleaner thread
    threading.Thread(target=_web_session_cleaner, daemon=True).start()

    _host = _web_bind_host()
    import socket as _socket
    ThreadingHTTPServer.allow_reuse_address = True
    ThreadingHTTPServer.allow_reuse_port    = False
    s = None
    for _attempt in range(1, 31):
        try:
            s = ThreadingHTTPServer((_host, _WEB_PORT), _WebHandler)
            break
        except OSError as _e:
            if _e.errno == 98:
                print(f"VRL Web: port {_WEB_PORT} busy, waiting for old process ... ({_attempt}/30)")
                time.sleep(1)
                continue
            raise
    if s is None:
        raise RuntimeError(f"VRL Web: port {_WEB_PORT} still in use after 30s — aborting")
    s.daemon_threads = True
    print("VRL War Room v16.7 — http://" + _host + ":" + str(_WEB_PORT))
    s.serve_forever()


# ===============================================================
# ===============================================================

# ═══════════════════════════════════════════════════════════════
#
#  Saves per weekly Tuesday expiry:
#    lab_data/collector/expiry_YYYYMMDD/
#      3min/YYYY-MM-DD.parquet   ATM+/-300 strikes, all day
#      1min/YYYY-MM-DD.parquet   ATM+/-5 strikes only (Shadow-DTF backtest)
#    lab_data/collector/spot/YYYY-MM-DD.parquet
#    lab_data/collector/meta/YYYY-MM-DD.json
#
#  Usage: python3 VRL_MAIN.py --collector
# ═══════════════════════════════════════════════════════════════

_COLLECTOR_STRIKE_RANGE     = 300
_COLLECTOR_STRIKE_STEP      = 50
_COLLECTOR_SHADOW_STEPS     = 5


def _collector_log(msg):
    print(datetime.now().strftime("%Y-%m-%d %H:%M:%S") + " | " + msg, flush=True)


def _collector_last_trading_date(df):
    try:
        return df.index.date.max()
    except Exception:
        return date.today()


def _collector_get_session_df(df, trading_date):
    if df.empty:
        return df
    try:
        return df[df.index.date == trading_date]
    except Exception:
        return df


def _collector_next_tuesday(from_date):
    days_ahead = (1 - from_date.weekday()) % 7
    return from_date + timedelta(days=days_ahead)


def _run_collector():
    _collector_log("=== VRL_COLLECTOR v2 start ===")

    try:
        kite = get_kite()
        init(kite)
        _collector_log("Auth OK")
    except Exception as e:
        _collector_log("AUTH FAILED: " + str(e))
        sys.exit(1)

    try:
        spot_df_raw      = get_historical_data(NIFTY_SPOT_TOKEN, "minute", 400)
        trading_date     = _collector_last_trading_date(spot_df_raw)
        spot_df_session  = _collector_get_session_df(spot_df_raw, trading_date)
        spot             = float(spot_df_session["close"].iloc[-1]) if not spot_df_session.empty else 0
        atm              = int(round(spot / _COLLECTOR_STRIKE_STEP) * _COLLECTOR_STRIKE_STEP)
        _collector_log(f"Trading date: {trading_date}  Spot={spot:.1f}  ATM={atm}")
    except Exception as e:
        _collector_log("Spot fetch error: " + str(e))
        trading_date = date.today()
        spot, atm = 0, 23500

    today_str = trading_date.isoformat()

    try:
        expiry = get_nearest_expiry(kite)
        if expiry.weekday() != 1:
            expiry = _collector_next_tuesday(expiry)
            _collector_log(f"Expiry adjusted to nearest Tuesday: {expiry}")
        else:
            _collector_log(f"Expiry: {expiry} (Tuesday)")
    except Exception as e:
        _collector_log("Expiry error: " + str(e))
        sys.exit(1)

    expiry_str = str(expiry).replace("-", "")

    collector_dir  = os.path.join(LAB_DIR, "collector")
    expiry_dir     = os.path.join(collector_dir, "expiry_" + expiry_str)
    dir_3min       = os.path.join(expiry_dir, "3min")
    dir_1min       = os.path.join(expiry_dir, "1min")
    spot_dir       = os.path.join(collector_dir, "spot")
    meta_dir       = os.path.join(collector_dir, "meta")
    for d in (dir_3min, dir_1min, spot_dir, meta_dir):
        os.makedirs(d, exist_ok=True)

    n_steps_full   = _COLLECTOR_STRIKE_RANGE // _COLLECTOR_STRIKE_STEP
    strikes_full   = [atm + i * _COLLECTOR_STRIKE_STEP for i in range(-n_steps_full, n_steps_full + 1)]
    strikes_shadow = [atm + i * _COLLECTOR_STRIKE_STEP for i in range(-_COLLECTOR_SHADOW_STEPS, _COLLECTOR_SHADOW_STEPS + 1)]
    _collector_log(f"3-min strikes: {strikes_full[0]}->{strikes_full[-1]} ({len(strikes_full)} strikes)")
    _collector_log(f"1-min strikes: {strikes_shadow[0]}->{strikes_shadow[-1]} ({len(strikes_shadow)} strikes, Shadow-DTF)")

    def _fetch(token, interval, candles=120):
        df = get_historical_data(token, interval, candles)
        df = _collector_get_session_df(df, trading_date)
        if not df.empty:
            df = add_indicators(df)
        return df

    _collector_log("Fetching 3-min data...")
    rows_3m = []
    failed_3m = []
    for strike in strikes_full:
        try:
            tokens = get_option_tokens(kite, strike, expiry)
        except Exception as e:
            failed_3m.append(strike)
            continue
        for opt_type in ("CE", "PE"):
            if opt_type not in tokens:
                failed_3m.append((strike, opt_type))
                continue
            tok = tokens[opt_type]["token"]
            sym = tokens[opt_type]["symbol"]
            try:
                df = _fetch(tok, "3minute", 120)
                if df.empty:
                    continue
                df["strike"]   = strike
                df["opt_type"] = opt_type
                df["symbol"]   = sym
                df["token"]    = tok
                rows_3m.append(df)
                time.sleep(0.05)
            except Exception as e:
                failed_3m.append((strike, opt_type))

    if rows_3m:
        import pandas as _pd_collector
        df_3m = _pd_collector.concat(rows_3m).sort_index()
        path_3m = os.path.join(dir_3min, today_str + ".parquet")
        df_3m.to_parquet(path_3m)
        _collector_log(f"3-min saved: {len(df_3m)} rows, {len(rows_3m)} series -> {path_3m}")
    else:
        _collector_log("WARNING: no 3-min data collected")

    _collector_log("Fetching 1-min data (Shadow-DTF strikes)...")
    rows_1m = []
    failed_1m = []
    for strike in strikes_shadow:
        try:
            tokens = get_option_tokens(kite, strike, expiry)
        except Exception as e:
            failed_1m.append(strike)
            continue
        for opt_type in ("CE", "PE"):
            if opt_type not in tokens:
                failed_1m.append((strike, opt_type))
                continue
            tok = tokens[opt_type]["token"]
            sym = tokens[opt_type]["symbol"]
            try:
                df = _fetch(tok, "minute", 400)
                if df.empty:
                    continue
                df["strike"]   = strike
                df["opt_type"] = opt_type
                df["symbol"]   = sym
                df["token"]    = tok
                rows_1m.append(df)
                time.sleep(0.05)
            except Exception as e:
                failed_1m.append((strike, opt_type))

    if rows_1m:
        import pandas as _pd_collector
        df_1m = _pd_collector.concat(rows_1m).sort_index()
        path_1m = os.path.join(dir_1min, today_str + ".parquet")
        df_1m.to_parquet(path_1m)
        _collector_log(f"1-min saved: {len(df_1m)} rows, {len(rows_1m)} series -> {path_1m}")
    else:
        _collector_log("WARNING: no 1-min data collected")

    try:
        if not spot_df_session.empty:
            spot_path = os.path.join(spot_dir, today_str + ".parquet")
            spot_df_session.to_parquet(spot_path)
            _collector_log(f"spot saved: {len(spot_df_session)} rows -> {spot_path}")
        else:
            _collector_log("WARNING: no spot data for today")
    except Exception as e:
        _collector_log("Spot save error: " + str(e))

    try:
        vix = get_vix()
        _collector_log(f"VIX: {vix}")
    except Exception as e:
        vix = 0
        _collector_log("VIX error: " + str(e))

    meta = {
        "date"            : today_str,
        "collected_at"    : datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "expiry"          : str(expiry),
        "expiry_weekday"  : expiry.strftime("%A"),
        "atm"             : atm,
        "spot"            : round(spot, 2),
        "vix"             : round(vix, 2),
        "strikes_3min"    : strikes_full,
        "strikes_1min"    : strikes_shadow,
        "series_3min_ok"  : len(rows_3m),
        "series_1min_ok"  : len(rows_1m),
        "failed_3min"     : len(failed_3m),
        "failed_1min"     : len(failed_1m),
    }
    meta_path = os.path.join(meta_dir, today_str + ".json")
    with open(meta_path, "w") as f:
        json.dump(meta, f, indent=2)
    _collector_log("meta written -> " + meta_path)

    if failed_3m:
        _collector_log(f"3-min failed ({len(failed_3m)}): {failed_3m[:5]}{'...' if len(failed_3m)>5 else ''}")
    if failed_1m:
        _collector_log(f"1-min failed ({len(failed_1m)}): {failed_1m}")

    _collector_log("=== VRL_COLLECTOR done ===")
    return meta


# ===============================================================
#  MAIN ENTRY POINT
# ===============================================================

if __name__ == "__main__":
    if "--collector" in sys.argv:
        _load_env_file(os.path.expanduser("~/.env"))  # ensure env loaded for standalone collector
        _run_collector()
        sys.exit(0)
    main()
