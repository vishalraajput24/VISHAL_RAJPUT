# ═══════════════════════════════════════════════════════════════
#  VRL_MAIN.py — VISHAL RAJPUT TRADE v21 (V11 Golden)
#  MERGED: VRL_CONFIG + VRL_DATA + VRL_ENGINE + VRL_LEVELS + VRL_LAB
#  V11 (LIVE):  1-min | Golden — Gate1: close>EMA9H+3.5  Gate2: OppDecay[-9,-7] dte>=2
#               Single-lot entry (market fill @ candle close)
#  V11 Exit:   INITIAL(ema9_low) → PROTECT(@+9,-2) → LOCK_4(@+11,+4) → TRAIL_10(@+15, max(entry+9, peak-10))
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

# Kite removed 2026-06-23 — market data is Upstox-only (see upstox_data.py).
# Orders always route to m.Stock. No Kite session, no kiteconnect dependency.

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


def data_provider() -> str:
    """Market-DATA source — Upstox-only since 2026-06-23 (Kite removed).
    Orders always route to m.Stock. Retained as a constant accessor so any
    legacy callers keep working; there is no longer a config toggle."""
    return "upstox"


def strategy_version() -> str:
    """Entry-gate variant: 'v11' (default, proven) | 'v13' (owner 2026-06-20).
    V13 swaps the gate REFERENCE LINES (same +3.5 gap / [-9,-7] band):
      MOMENTUM  own_close >= own ema9_LOW  + 3.5   (V11 used ema9_high)
      OPP DECAY opp_close  - opp ema9_HIGH in [-9,-7] (V11 used ema9_low)
    Everything else (exit ladder, ITM-100 strikes, window, single lot) is shared.
    Reversible: flip config.yaml -> strategy_version back to v11 any time."""
    return (get().get("strategy_version") or "v11").lower()



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


# ── Web ──

def web_port() -> int:
    return _deep_get(get(), "web", "port", default=8080)



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

# Status strings from MStock order book (case-insensitive compare done at use).
# m.Stock reports a FILLED order as "Traded" (NOT "complete") — using the wrong
# token here made ms_verify_fill time out on every fill and abandon real positions
# (2026-06-15 incident: 5 live lots orphaned). Match the full filled vocabulary.
_FILLED_STATUSES    = {"complete", "traded", "executed", "fully executed"}
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

def _ms_lookup_order(mc, order_id: str) -> dict:
    """Find one order in the m.Stock ORDER BOOK by id. Returns {} if absent.
    The order book is the reliable source — get_order_details was returning
    'Pending'/None for orders the book already showed as 'Traded' (2026-06-15)."""
    try:
        resp = mc.get_order_book()
        data = resp.json()
        if data.get("status") != "success":
            return {}
        orders = data.get("data") or []
        if isinstance(orders, dict):
            orders = orders.get("orders", []) or []
        for o in orders:
            if str(o.get("order_id", "")) == str(order_id):
                return o
    except Exception as e:
        logger.warning(f"[MSTOCK] order lookup error: {e}")
    return {}


def ms_verify_fill(mc, order_id: str, timeout_secs: int = 10) -> tuple:
    """
    Poll the MStock ORDER BOOK until the order is filled or rejected/cancelled.
    Returns (fill_price, fill_qty). Returns (0.0, 0) on rejection/timeout.
    """
    deadline = time.time() + timeout_secs
    while time.time() < deadline:
        order = _ms_lookup_order(mc, order_id)
        if order:
            status = str(order.get("status", "")).lower()
            if status in _FILLED_STATUSES:
                fill_price = float(order.get("average_price", 0) or 0)
                fill_qty   = int(order.get("filled_quantity", 0) or 0)
                if fill_qty > 0:
                    return fill_price, fill_qty
            elif status in (_STATUS_REJECTED, _STATUS_CANCELLED):
                logger.error(f"[MSTOCK] Order {order_id} {status}: "
                             f"{order.get('status_message', '')}")
                return 0.0, 0
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
        if isinstance(data, list):          # m.Stock place_order returns [{...}]
            data = data[0] if data else {}
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
                logger.info(f"[MSTOCK] Entry cancel sent — verifying: {order_id}")
            except Exception:
                pass
            # Cancel can race a fill landing at the deadline. Re-check the order
            # book: if it actually TRADED, ADOPT the position (never abandon a
            # filled order — that orphaned 5 live lots on 2026-06-15).
            time.sleep(0.6)
            order = _ms_lookup_order(mc, order_id)
            status = str(order.get("status", "")).lower()
            if status in _FILLED_STATUSES and int(order.get("filled_quantity", 0) or 0) > 0:
                fill_price = float(order.get("average_price", 0) or 0)
                fill_qty   = int(order.get("filled_quantity", 0) or 0)
                slippage   = round(fill_price - limit_price, 2)
                logger.warning(f"[MSTOCK] ENTRY FILLED AFTER CANCEL (adopted): "
                               f"price={fill_price} qty={fill_qty} {order_id}")
                return {"ok": True, "fill_price": fill_price, "fill_qty": fill_qty,
                        "order_id": order_id, "error": "", "slippage": slippage}
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
        if isinstance(data, list):          # m.Stock place_order returns [{...}]
            data = data[0] if data else {}
        if data.get("status") != "success":
            err = str(data.get("message", data))
            logger.error(f"[MSTOCK] SELL rejected: {err}")
            return {"ok": False, "fill_price": 0.0, "fill_qty": 0,
                    "order_id": "", "error": f"ORDER_REJECTED: {err}", "slippage": 0}

        order_id = str(data["data"]["order_id"])
        logger.info(f"[MSTOCK] MARKET SELL placed: {order_id}")

        fill_price, fill_qty = ms_verify_fill(mc, order_id, timeout_secs)

        if fill_qty == 0:
            # Before reporting failure, re-check the order book. A MARKET sell can
            # fill just after the verify deadline; if the CALLER retries on a false
            # failure it sells a position we no longer hold → NAKED SHORT. Never
            # report a filled sell as failed (mirror of the buy adopt-on-fill fix).
            time.sleep(0.6)
            order = _ms_lookup_order(mc, order_id)
            status = str(order.get("status", "")).lower()
            if status in _FILLED_STATUSES and int(order.get("filled_quantity", 0) or 0) > 0:
                fill_price = float(order.get("average_price", 0) or 0)
                fill_qty   = int(order.get("filled_quantity", 0) or 0)
                logger.warning(f"[MSTOCK] EXIT FILLED (detected after timeout): "
                               f"price={fill_price} qty={fill_qty} {order_id}")
                return {"ok": True, "fill_price": fill_price, "fill_qty": fill_qty,
                        "order_id": order_id, "error": "", "slippage": 0}
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


# ── Fund summary (cached) ────────────────────────────────────────────────────

# `ok`/available/used reflect the LAST SUCCESSFUL fetch; `stale` flags that the
# most recent refresh failed and we are serving the last-good numbers.
_ms_funds_cache = {"ts": 0.0, "ok": False, "stale": False,
                   "name": "", "available": 0.0, "used": 0.0,
                   "have_good": False, "next_retry_ts": 0.0}

# m.Stock's gateway intermittently returns a 502/HTML bot-protection page
# (validate.perfdrive.com) instead of JSON — ~50% of calls during incidents.
# Retry a few times per refresh, and on total failure keep serving the
# last-good balance (flagged stale) instead of dropping to the Kite fallback.
_MS_FUNDS_RETRIES        = 3      # attempts per refresh
_MS_FUNDS_RETRY_GAP      = 1.5    # seconds between attempts
_MS_FUNDS_FAIL_COOLDOWN  = 20.0   # after a failed refresh, retry sooner than max_age

def _ms_fetch_fund_summary_once():
    """One fund_summary REST call → (available, used) or raise. Treats a
    non-success status / non-JSON body as a retryable failure."""
    mc = get_mstock()
    f  = mc.get_fund_summary()
    fd = f.json()
    if fd.get("status") != "success":
        raise RuntimeError(f"fund_summary status={fd.get('status')}")
    rows = fd.get("data") or []
    row = next(
        (r for r in rows if str(r.get("SEG", "")).upper() in ("A", "E", "EQUITY")),
        rows[0] if rows else {}
    )
    available = float(row.get("AVAILABLE_BALANCE") or row.get("NET") or 0)
    used      = float(row.get("AMOUNT_UTILIZED") or row.get("LIMIT_SOD") or 0)
    return available, used

def ms_get_funds(max_age_secs: int = 300) -> dict:
    """m.Stock client name + fund summary, cached (one REST call / 5 min).
    Returns {"ok", "stale", "name", "available", "used"}.
      - ok=True            → numbers are trustworthy (fresh or last-good).
      - ok=True, stale=True → last refresh failed; serving last-good numbers.
      - ok=False           → never fetched successfully (login pending / cold).
    Resilient to m.Stock's intermittent 502 bot-protection page: retries a few
    times per refresh, and falls back to the last-good balance instead of the
    Kite account so the dashboard never flips to a bogus number."""
    import base64
    now_ts = time.time()
    c = _ms_funds_cache

    # Serve cache while fresh. After a failed refresh we still keep ok/values
    # (last-good), so honour a short failure-cooldown before retrying.
    age = now_ts - c["ts"]
    if age < max_age_secs and now_ts < c.get("next_retry_ts", 0.0):
        return dict(c)
    if age < max_age_secs and not c.get("stale"):
        return dict(c)

    # Name from JWT payload (cheap, no network) — refresh opportunistically.
    try:
        saved = _ms_read_token()
        jwt   = saved.get("access_token", "")
        if jwt:
            payload_b64 = jwt.split(".")[1]
            padding = 4 - len(payload_b64) % 4
            if padding != 4:
                payload_b64 += "=" * padding
            payload = json.loads(base64.urlsafe_b64decode(payload_b64))
            c["name"] = str(payload.get("CLIENTNAME", "")).strip().title()
    except Exception:
        pass

    last_err = None
    for attempt in range(_MS_FUNDS_RETRIES):
        try:
            available, used = _ms_fetch_fund_summary_once()
            c["available"]  = available
            c["used"]       = used
            c["ok"]         = True
            c["stale"]      = False
            c["have_good"]  = True
            c["ts"]         = time.time()
            c["next_retry_ts"] = 0.0
            return dict(c)
        except Exception as e:
            last_err = e
            if attempt < _MS_FUNDS_RETRIES - 1:
                time.sleep(_MS_FUNDS_RETRY_GAP)

    # All attempts failed. Keep last-good numbers (flagged stale) if we ever had
    # them; otherwise ok stays False. Retry again after a short cooldown.
    c["ts"]            = now_ts
    c["next_retry_ts"] = now_ts + _MS_FUNDS_FAIL_COOLDOWN
    if c.get("have_good"):
        c["ok"]    = True
        c["stale"] = True
        logger.warning(f"[MSTOCK] ms_get_funds: refresh failed ({last_err}); "
                       f"serving last-good balance ₹{c['available']:,.0f}")
    else:
        c["ok"]    = False
        c["stale"] = False
        logger.warning(f"[MSTOCK] ms_get_funds: no fund data yet ({last_err})")
    return dict(c)


# ── Startup banner helper ────────────────────────────────────────────────────

def ms_get_banner_line() -> str:
    """Return a one-liner for the bot startup Telegram banner."""
    client_id = os.getenv("MSTOCK_CLIENT_ID", "MStock")
    try:
        funds = ms_get_funds(max_age_secs=0)  # force fresh at startup
        label = funds.get("name") or client_id
        if funds.get("ok"):
            return ("MStock: " + label
                    + " | Avail: ₹{:,.0f}".format(funds["available"])
                    + " | Used: ₹{:,.0f}".format(funds["used"]))
        return f"MStock: {label} (funds unavailable)"
    except Exception as e:
        logger.warning(f"[MSTOCK] banner_line error: {e}")
        return f"MStock: {client_id} (login pending)"


# ── Quick connection test ─────────────────────────────────────────────────────


# ===============================================================
# ===============================================================

# ═══════════════════════════════════════════════════════════════
#  Foundation layer. Settings, logging, market data, Greeks.
# ═══════════════════════════════════════════════════════════════





VERSION  = "v22"
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
TELEGRAM_TOKEN   = os.getenv("TG_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TG_GROUP_ID", "")

BASE_DIR         = os.path.expanduser("~")
REPO_DIR         = os.path.dirname(os.path.abspath(__file__))
LOGS_DIR         = os.path.join(BASE_DIR, "logs")
LIVE_LOG_DIR     = os.path.join(LOGS_DIR, "live")
LAB_LOG_DIR      = os.path.join(LOGS_DIR, "lab")
AUTH_LOG_DIR     = os.path.join(LOGS_DIR, "auth")
WEB_LOG_DIR      = os.path.join(LOGS_DIR, "web")
HEALTH_LOG_DIR   = os.path.join(LOGS_DIR, "health")
ERROR_LOG_DIR    = os.path.join(LOGS_DIR, "errors")
# STATE_DIR lives next to the code (inside the repo) so AUTH and MAIN
# always agree on the token location..
STATE_DIR        = os.path.join(REPO_DIR, "state")
LAB_DIR          = os.path.join(BASE_DIR, "lab_data")
BACKUP_DIR       = os.path.join(BASE_DIR, "backups")
OPTIONS_3MIN_DIR = os.path.join(LAB_DIR, "options_3min")
OPTIONS_1MIN_DIR = os.path.join(LAB_DIR, "options_1min")
SPOT_DIR         = os.path.join(LAB_DIR, "spot")

LIVE_LOG_FILE    = os.path.join(LIVE_LOG_DIR, "vrl_live.log")
LAB_LOG_FILE     = os.path.join(LAB_LOG_DIR,  "vrl_lab.log")
TRADE_LOG_PATH   = os.path.join(LAB_DIR,      "vrl_trade_log.csv")
STATE_FILE_PATH        = os.path.join(STATE_DIR, "vrl_live_state.json")
V11_STATE_FILE_PATH     = os.path.join(STATE_DIR, "vrl_v11_state.json")
V13_STATE_FILE_PATH     = os.path.join(STATE_DIR, "vrl_v13_state.json")
V13_TRADE_LOG_PATH      = os.path.join(LAB_DIR,   "vrl_v13_trade_log.csv")
SHADOW_STATE_FILE_PATH = os.path.join(STATE_DIR, "vrl_shadow_state.json")
PID_FILE_PATH          = os.path.join(STATE_DIR, "vrl_live.pid")

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
    for d in [LIVE_LOG_DIR, LAB_LOG_DIR, STATE_DIR,
              OPTIONS_3MIN_DIR, OPTIONS_1MIN_DIR, SPOT_DIR, BACKUP_DIR,
              AUTH_LOG_DIR, WEB_LOG_DIR, HEALTH_LOG_DIR, ERROR_LOG_DIR]:
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
        "errors": ERROR_LOG_DIR,
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
        "errors": ERROR_LOG_DIR,
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

    # State snapshot
    if os.path.isfile(STATE_FILE_PATH):
        files.append((STATE_FILE_PATH, "state/vrl_live_state.json"))

    # Config snapshot
    config_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.yaml")
    if os.path.isfile(config_path):
        files.append((config_path, "state/config.yaml"))

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


_kite             = None    # retained as a None constant (Kite removed) for any inert references
_account_info     = {}
_token_cache      = {}
_token_cache_lock = threading.Lock()
_ticks            = {}
_tick_lock        = threading.Lock()
_subscribed       = set()
_subscribed_lock  = threading.Lock()
_ws_connected     = False


# ── cross-module "trade was taken" signal ────
# VRL_MAIN sets this after a successful entry; VRL_LAB reads it
# when building the next signal_scans row and writes trade_taken=1.
_trade_taken_lock = threading.Lock()
_trade_taken_direction = ""    # "" = no trade pending, "CE" or "PE"
_trade_taken_ts        = ""    # ISO timestamp of the entry





# ── active trade token for LAB persistence ──
# VRL_MAIN sets this on entry; VRL_LAB reads it to ensure the
# traded strike's candles are always written regardless of ATM drift.
_active_trade_lock = threading.Lock()
_active_trade = None   # None or {"token_ce": int, "token_pe": int, "strike": int, "direction": str}




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


def init(kite_instance=None):
    # Kite removed — retained as a no-op so existing call sites (init(None))
    # keep working. Market data is Upstox-only; orders go to m.Stock.
    return


def fetch_account_info(kite=None):
    # Account/margin used to come from Kite. With Kite removed the dashboard
    # shows the m.Stock balance instead (ms_get_funds / ms_get_banner_line).
    return _account_info


def get_account_info():
    return _account_info


def refresh_margin(kite=None):
    # No-op since Kite removal — m.Stock funds drive the dashboard balance.
    return

def start_websocket():
    """Start the Upstox v3 protobuf market-data feed (orders go to m.Stock)."""
    global _ws_connected
    ud = _udata_mod()
    if ud is not None:
        with _subscribed_lock:
            init_tokens = list(_subscribed)
        # Construct the streamer WITH the index feeds. A subscribe() issued
        # after connect() races the socket OPEN and is silently dropped by
        # the SDK, leaving the feed connected but tickless (2026-06-22). The
        # SDK auto-subscribes construction keys on open, so seed them here.
        for _t in (NIFTY_SPOT_TOKEN, INDIA_VIX_TOKEN):
            if _t and _t not in init_tokens:
                init_tokens.append(_t)
        ud.start_ws(init_tokens)
        with _subscribed_lock:
            _subscribed.update(int(t) for t in init_tokens if t)
        _ws_connected = True
        logger.info("[WS] Upstox protobuf feed started")

def subscribe_tokens(tokens: list) -> set:
    """Subscribe to WS feed for the given tokens. Returns the set of
    tokens actually accepted (empty set on failure). Callers that need
    to track what actually got subscribed should use the return value
    rather than the input list — prior code assumed all inputs made
    it through, leaking tokens on partial failure."""
    global _subscribed
    with _subscribed_lock:
        new = set(int(t) for t in tokens if t)
        ud = _udata_mod()
        if ud is not None:
            try:
                ud.ws_subscribe(list(new))
            except Exception as _e:
                logger.warning("[WS] Upstox subscribe failed: " + str(_e))
                return set()
        _subscribed.update(new)
        logger.info("[WS] Subscribed (upstox): " + str(new))
        return new

def subscribe_full_flow(tokens: list):
    """Upgrade the given tokens to Upstox v3 'full' mode (DOM/OI/IV) for the
    tick-flow study. The LTP path is unaffected; this only enriches
    _ws_flow/get_flow used by tick_flow.py."""
    toks = [int(t) for t in tokens if t]
    if not toks:
        return
    ud = _udata_mod()
    if ud is None:
        return
    try:
        ud.subscribe_full(toks)
    except Exception as _e:
        logger.debug("[WS] full-mode upgrade failed: " + str(_e))


def unsubscribe_tokens(tokens: list):
    global _subscribed
    with _subscribed_lock:
        rem = set(int(t) for t in tokens if t)
        _subscribed -= rem
        ud = _udata_mod()
        if ud is not None:
            try:
                ud.ws_unsubscribe(list(rem))
            except Exception:
                pass
        logger.info("[WS] Unsubscribed (upstox): " + str(rem))

def get_ltp(token) -> float:
    if token is None:
        return 0.0
    ud = _udata_mod()
    if ud is None:
        return 0.0
    v = ud.ws_get_ltp(int(token), max_age=TICK_STALE_SECS)
    if v <= 0 and is_market_open():
        logger.warning("[DATA] No fresh upstox tick token=" + str(token))
    return v


def get_spot_ltp() -> float:
    """v15.2: convenience helper — spot LTP via the Upstox WebSocket tick
    cache, falling back to a REST quote when no fresh tick is present."""
    v = get_ltp(NIFTY_SPOT_TOKEN)
    if v <= 0:
        ud = _udata_mod()
        if ud is not None:
            try:
                return ud.get_ltp_one(NIFTY_SPOT_TOKEN)
            except Exception:
                return 0.0
    return v

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
    global _last_reconnect_attempt
    if not is_market_open():
        return
    ud = _udata_mod()
    if ud is None:
        return
    if ud.ws_get_ltp(NIFTY_SPOT_TOKEN, max_age=180) > 0:
        return  # fresh upstox spot tick
    if time.time() - _last_reconnect_attempt < 600:
        return
    _last_reconnect_attempt = time.time()
    logger.warning("[DATA] Upstox spot tick stale 3+ min — restarting feed")
    try:
        if _ws_autoheal_callback:
            _ws_autoheal_callback("\u26a0\ufe0f WebSocket auto-healing after stale tick (3min+)")
    except Exception:
        pass
    try:
        with _subscribed_lock:
            toks = list(_subscribed)
        ud.restart_ws(toks)
    except Exception as e:
        logger.error("[DATA] upstox WS restart failed: " + str(e))

def get_vix() -> float:
    ltp = get_ltp(INDIA_VIX_TOKEN)
    if ltp > 0:
        return ltp
    ud = _udata_mod()
    if ud is not None:
        try:
            v = ud.get_ltp_one(INDIA_VIX_TOKEN)
            if v > 0:
                return float(v)
        except Exception:
            pass
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
    Dynamic holiday detection previously used Kite's quote last_trade_time.
    With Kite removed (Upstox-only since 2026-06-23) there is no equivalent
    REST last_trade_time field, so this always returns None and the caller
    falls back to the static TRADING_HOLIDAYS list (same behaviour the bot
    already had under the Upstox provider).
    """
    return None


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
    end = now.replace(hour=ENTRY_CUTOFF_HOUR, minute=ENTRY_CUTOFF_MIN, second=0, microsecond=0)
    return start <= now < end

def get_lot_size(kite=None) -> int:
    ud = _udata_mod()
    if ud is not None:
        try:
            lot = ud.lot_size()
            if lot > 0:
                return lot
        except Exception as e:
            logger.warning("[DATA] upstox lot_size failed: " + str(e))
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

# ── Upstox data provider (lazy) — see upstox_data.py / data_provider() ──
DATA_PROVIDER = "upstox"   # Kite removed 2026-06-23 — single data provider, no toggle
_udata = None
def _udata_mod():
    """Lazy-import the Upstox backend and register the index tokens once.
    Returns None (data unavailable) if the module can't load."""
    global _udata
    if _udata is None:
        try:
            import upstox_data
            upstox_data.register_index_tokens(NIFTY_SPOT_TOKEN, INDIA_VIX_TOKEN)
            _udata = upstox_data
        except Exception as e:
            logger.error("[DATA] upstox_data import failed (no data backend): " + str(e))
            return None
    return _udata


def get_historical_data(token: int, interval: str, lookback: int,
                        today_only: bool = False) -> pd.DataFrame:
    # Check cache first — key includes the current candle bucket, so a
    # fresh fetch is triggered exactly once per candle close. (Shared across
    # providers so a flip doesn't double-fetch.)
    cache_key = _hist_cache_key(token, interval, lookback)
    cached = _hist_cache_get(cache_key)
    if cached is not None:
        return cached
    ud = _udata_mod()
    if ud is None:
        return pd.DataFrame()
    try:
        df = ud.historical_df(int(token), interval, lookback)
    except Exception as e:
        logger.warning("[DATA] upstox historical failed token="
                       + str(token) + ": " + str(e))
        df = pd.DataFrame()
    if not df.empty:
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

V11_STRIKE_STEP = 100   # 100-pt strikes only — 50-step half-strikes too illiquid

def resolve_strike_for_direction(spot: float, direction: str, dte: int) -> int:
    """
    v22: 100-pt 'intelligent' strikes, slightly-ITM per direction (owner 2026-06-17).
    - CE floors to the 100 below spot   → strike <= spot  → ITM call
    - PE ceils  to the 100 above spot   → strike >= spot  → ITM put
    So CE and PE sit on DIFFERENT strikes, but BOTH are ITM. This makes the
    OPP DECAY gate meaningful: the opposite leg fed to the decay check is the
    other ITM option (e.g. CE entry → decay read on the ITM PE), not an OTM leg
    that is always decaying anyway. The scanner already pairs own=_locked["CE"]
    with opp=_locked["PE"], so locking these two ITM legs wires the correlation.
    At an exact round-100 spot both collapse to the same true-ATM strike.
    """
    _f = int(spot)
    if direction == "CE":
        return (_f // V11_STRIKE_STEP) * V11_STRIKE_STEP
    return ((_f + V11_STRIKE_STEP - 1) // V11_STRIKE_STEP) * V11_STRIKE_STEP

def get_nearest_expiry(kite=None, reference_date=None) -> date:
    if reference_date is None:
        reference_date = date.today()
    ud = _udata_mod()
    if ud is None:
        logger.error("[DATA] upstox_data unavailable for nearest_expiry")
        return None
    try:
        return ud.nearest_expiry(reference_date)
    except Exception as e:
        logger.error("[DATA] upstox nearest_expiry error: " + str(e))
        return None

def calculate_dte(expiry_date) -> int:
    if expiry_date is None:
        return 0
    return max((expiry_date - date.today()).days, 0)

def get_option_tokens(kite, strike: int, expiry_date) -> dict:
    if not strike or int(strike) <= 0:
        return {}
    key = (int(strike), expiry_date.isoformat() if expiry_date else "")
    with _token_cache_lock:
        if key in _token_cache:
            return dict(_token_cache[key])
    ud = _udata_mod()
    if ud is None:
        return {}
    try:
        res = ud.option_tokens(int(strike), expiry_date)
    except Exception as e:
        logger.error("[DATA] upstox option_tokens error: " + str(e))
        return {}
    if len(res) == 2:                      # only cache complete results
        with _token_cache_lock:
            _token_cache[key] = res
    return dict(res)

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

_daily_bias        = "UNKNOWN"
_daily_bias_done   = False
_hourly_rsi        = 0.0
_hourly_rsi_ts     = 0


def compute_daily_bias(kite):
    global _daily_bias, _daily_bias_done
    result = {"bias": "UNKNOWN", "ema21": 0, "adx": 0, "spot": 0, "details": ""}
    try:
        df = get_historical_data(NIFTY_SPOT_TOKEN, "day", 80)
        if df is None or len(df) < 25:
            return result
        df = df.copy()
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
        now = datetime.now()
        df = get_historical_data(NIFTY_SPOT_TOKEN, "60minute", 200)
        if df is None or len(df) < 20:
            return result
        df = df.copy()
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
    global _daily_bias, _daily_bias_done
    global _hourly_rsi, _hourly_rsi_ts
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
    dirs_to_clean = [OPTIONS_1MIN_DIR, OPTIONS_3MIN_DIR, SPOT_DIR]
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
    result = {"strike": strike, "ce_candles": 0, "pe_candles": 0,
              "fetched": False, "api_calls": 0, "error": None}

    db_path = os.path.expanduser("~/lab_data/vrl_data.db")
    cutoff = (datetime.now() - timedelta(days=lookback_days)).strftime("%Y-%m-%d")
    table_map = {"minute": "option_1min", "3minute": "option_3min"}
    tokens = get_option_tokens(None, strike, expiry)
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
                raw = _lab_hist_candles(int(token), tf, from_dt, to_dt)
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
Logs filter pass/fail per V11 entry into ~/lab_data/shadow_levels_data.csv.

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
    # VWAP was a Kite-futures dashboard widget. With Kite removed (Upstox-only,
    # 2026-06-23) the NIFTY-future VWAP feed is gone; this is now a no-op and the
    # dashboard VWAP fields stay blank. No V11/V13 gate uses VWAP.
    return _vwap_state









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
        candles = _lab_hist_candles(D.NIFTY_SPOT_TOKEN, "minute", from_dt, now)
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

def _lab_hist_candles(token: int, interval: str,
                      from_dt: datetime, to_dt: datetime) -> list:
    """Candle fetch for the lab collectors (Upstox-only since Kite removal,
    2026-06-23). Returns Kite-style list-of-dicts (keys: date, open, high, low,
    close, volume) by routing through get_historical_data (Upstox REST) and
    rebuilding the dict-list the collectors expect."""
    mpc = {"minute": 1, "3minute": 3, "5minute": 5,
           "15minute": 15, "30minute": 30, "60minute": 60}.get(interval, 1)
    span_min = max(1.0, (to_dt - from_dt).total_seconds() / 60.0)
    lookback = int(span_min / mpc) + 5
    df = get_historical_data(int(token), interval, lookback)
    if df is None or df.empty:
        return []
    _f = from_dt.replace(tzinfo=None) if from_dt else None
    _t = to_dt.replace(tzinfo=None) if to_dt else None
    out = []
    for ts, row in df.iterrows():
        tsd = ts.to_pydatetime() if hasattr(ts, "to_pydatetime") else ts
        tsn = tsd.replace(tzinfo=None) if getattr(tsd, "tzinfo", None) else tsd
        if _f and tsn < _f:
            continue
        if _t and tsn > _t:
            continue
        out.append({"date": tsd,
                    "open": float(row["open"]), "high": float(row["high"]),
                    "low": float(row["low"]), "close": float(row["close"]),
                    "volume": float(row.get("volume", 0) or 0)})
    return out


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
        all_candles = _lab_hist_candles(token, interval, warmup_from, to_dt)
        return all_candles if all_candles else []
    except Exception as e:
        logger.warning("[LAB] Warmup fetch failed, using regular fetch: " + str(e))
        return _fetch_candles(kite, token, from_dt, to_dt, interval)


def _fetch_candles(kite, token: int, from_dt: datetime,
                   to_dt: datetime, interval: str = "3minute") -> list:
    return _lab_hist_candles(token, interval, from_dt, to_dt)


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
    # V11 EMERGENCY_SL 1-candle cooldown: set True when SL fires,
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
    "_hourly_rsi_ts"     : 0,
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

_v11_state = {
    "_last_fired_candle_ts": "",     # same-candle guard
    "_signals_today": 0,             # count for /pulse
    "_last_signal_time": "",
    # Paper position state (parallel to V7, independent).
    "in_trade": False,
    # Live entry reservation: set under lock BEFORE the ~8s blocking m.Stock order
    # so a later 3s scanner tick can't fire a SECOND live order while the first
    # is still in flight (caused real double-lot fills). Cleared on abort/failure.
    "_entry_in_progress": False,
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
    "_v11_both_rejected_ts": 0.0,
    # Date of last trade — used to detect new day and reset daily counters on restart
    "_last_trade_date": "",
    # Current expiry / DTE — synced from main loop every iteration so entry/exit always sees correct value
    "expiry": "",
    "dte": 0,
    # EMERGENCY_SL direction cooldown — only blocks the side that triggered the SL
    "_sl_cooldown_direction": "",
    # Same-side 3-min blocker: records direction + unix timestamp of last exit
    "_last_exit_time_unix"    : 0.0,
    "_last_exit_direction_v10": "",
    # Strike management data collection (reset per trade, not persisted)
    "entry_spot": 0.0,
    "entry_atm_dist": 0,      # strike - true_ATM at entry (CE: + = ITM, - = OTM)
    # Vishal Anti-Chase Filter (VAC) — own-leg 3-candle run into the entry candle.
    # Analysis only (NOT a gate). High m3 = chasing an extended move; research
    # (2026-06-15) showed m3>8 entries are the bleeders. Logged to test the
    # m3<=8 block against a 60% expiry-day accuracy bar before any gate.
    "own_m3_at_entry": 0.0,
    # PDH/PDL context at entry (analysis only — not a gate)
    "pdh_prev": 0.0,
    "pdl_prev": 0.0,
    "entry_range_pos": "",    # (spot-PDL)/(PDH-PDL): 0=PDL 1=PDH >1=breakout above
    "neighbor_ltp_otm": 0.0,  # LTP of 1-strike-OTM neighbor at entry
    "neighbor_ltp_itm": 0.0,  # LTP of 1-strike-ITM neighbor at entry
    "max_otm_drift": 0.0,     # max pts the position went OTM during trade

    "initial_sl": 0.0,
    "entry_regime": "",
    "peak_ltp": 0.0,
    "xleg_other_margin": 0.0,
    "spot_regime_at_entry": "",
    # Market context at entry — persisted so they survive restart
    "vix_at_entry":          None,
    "hourly_rsi_at_entry":   None,
    "bias_at_entry":         None,
    "session_at_entry":      None,

    # Study: entry-timing markers (when did trade actually move?)
    "first_profit_candle": 0,    # candles_held when LTP first exceeded avg_entry
    "first_profit_ltp":    0.0,  # LTP at that tick
    "first_profit_ts":     "",   # time string at that tick
    "breakout_candle":     0,    # candles_held when cur_pnl first crossed V11_BREAKOUT_THRESHOLD
    "breakout_ltp":        0.0,  # LTP at breakout
    "breakout_ts":         "",   # time string at breakout
}
_v11_lock = threading.RLock()  # RLock: _save_v11_state() re-enters this lock from within exit-check block


def _v11_compute_trail_sl(entry_price: float, peak_pnl: float, initial_sl: float) -> tuple:
    """V11 dynamic exit rules (owner-approved 2026-06-13, validated by sl_replay_study.py):
    - Initial SL is 1-min ema9_low at entry (passed as initial_sl), capped at entry-10.
    - Protect entry-2.0 once profit hits +9.0 pts (giveback never exceeds 2 pts).
    - Lock entry+4.0 once profit hits +11.0 pts (capture a small win, not a scratch).
    - Lock+Trail once profit hits +15.0 pts: SL = max(entry+9, peak-10). This single
      rung absorbs the old separate +18 trail tier — peak-10 only overtakes the +9 lock
      at peak 19, so the +9 floor holds from +15..+19 then the trail takes over.
      Replay: +25.1 pts over 73 trades, 0 trades made worse vs the old 4-rung ladder.
    """
    if peak_pnl >= V11_TARGET_PTS:
        # LOCK_25 (owner-approved 2026-06-15): once peak hits +25, lock entry+25 as a hard
        # floor AND trail peak-5 above it (tight trail to grab max points on the runner) —
        # guarantees +25 min while riding the big winners. Replaced the original +25 hard exit
        # (which capped runners at +25). Floor binds for peak +25..+30; above +30 the peak-5
        # trail takes over (peak +40 → SL +35, peak +50 → SL +45).
        peak_ltp = entry_price + peak_pnl
        trail_val = max(initial_sl, entry_price + V11_TARGET_PTS, peak_ltp - 5.0)
        return round(trail_val, 2), "LOCK_25"
    elif peak_pnl >= 15.0:
        peak_ltp = entry_price + peak_pnl
        trail_val = max(initial_sl, entry_price + 9.0, peak_ltp - 10.0)
        return round(trail_val, 2), "TRAIL_10"
    elif peak_pnl >= 11.0:
        trail_val = max(initial_sl, entry_price + 4.0)
        return round(trail_val, 2), "LOCK_4"
    elif peak_pnl >= 9.0:
        trail_val = max(initial_sl, entry_price - 2.0)
        return round(trail_val, 2), "PROTECT"
    else:
        return round(initial_sl, 2), "INITIAL"


_PDHL_CACHE = {"date": "", "pdh": 0.0, "pdl": 0.0}


def _get_prev_day_hl():
    """Previous trading day's NIFTY spot high/low from lab_data/spot 1-min CSVs.
    Cached per calendar day (one file read/day). Returns (pdh, pdl); (0.0, 0.0)
    if no prior-day file exists. Analysis-only — never a gate."""
    _today = date.today().strftime("%Y%m%d")
    if _PDHL_CACHE["date"] == _today:
        return _PDHL_CACHE["pdh"], _PDHL_CACHE["pdl"]
    try:
        import glob as _g
        files = sorted(_g.glob(os.path.join(D.SPOT_DIR, "nifty_spot_1min_*.csv")))
        # basename format: nifty_spot_1min_YYYYMMDD.csv — date at chars [16:24]
        prev = [p for p in files if os.path.basename(p)[16:24] < _today]
        if not prev:
            return 0.0, 0.0
        pdh, pdl = 0.0, 0.0
        with open(prev[-1]) as f:
            for row in csv.DictReader(f):
                h = float(row.get("high", 0) or 0)
                l = float(row.get("low", 0) or 0)
                if h > pdh:
                    pdh = h
                if l and (pdl == 0.0 or l < pdl):
                    pdl = l
        _PDHL_CACHE["date"] = _today
        _PDHL_CACHE["pdh"] = round(pdh, 2)
        _PDHL_CACHE["pdl"] = round(pdl, 2)
        return _PDHL_CACHE["pdh"], _PDHL_CACHE["pdl"]
    except Exception:
        return 0.0, 0.0


def _v11_execute_paper_entry(direction: str, strike: int, symbol: str, token: int,
                             entry_price: float, entry_result: dict,
                             other_token: int = 0,
                             spot_at_entry: float = 0.0,
                             neighbor_ltp_otm: float = 0.0,
                             neighbor_ltp_itm: float = 0.0):
    """Open a V11 paper position — single lot, market fill at candle close."""
    lot_count = CFG.get().get("lots", {}).get("count", 1)
    qty = lot_count * D.get_lot_size()

    now_dt  = datetime.now()
    now_str = now_dt.strftime("%H:%M:%S")

    # ── LIVE: place the real m.Stock entry BEFORE recording state ──────────────
    # Runs outside _v11_lock — place_entry blocks up to ~8s waiting for the LIMIT
    # fill, and holding the RLock that long would freeze the exit/TG/web threads.
    # On a non-fill (LIMIT cancelled after 8s, rejection, partial) we abort the
    # entry and stamp the candle so the 3s scanner doesn't hammer the same bar.
    if not D.PAPER_MODE:
        # Reserve the entry under the lock BEFORE the ~8s blocking broker call.
        # place_entry blocks waiting for the LIMIT fill and runs OUTSIDE _v11_lock;
        # without this reservation a later 3s scanner tick would see in_trade=False
        # (not set until after the broker returns) and fire a SECOND live order —
        # the cause of real double-lot fills. _entry_in_progress closes that window.
        with _v11_lock:
            if _v11_state.get("in_trade") or _v11_state.get("_entry_in_progress"):
                logger.warning("[V11] Live entry attempted while already in_trade/in_progress — BLOCKED")
                return
            _v11_state["_entry_in_progress"] = True
        _live = place_entry(_kite, symbol, token, direction, qty, entry_price)
        if not _live.get("ok"):
            logger.warning("[V11] LIVE entry not filled: " + str(_live.get("error", "")))
            _tg_send(
                f"⚠️ <b>V11 LIVE entry MISSED {direction} {strike}</b>\n"
                f"LIMIT ~₹{entry_price:.1f} unfilled in 8s — {_live.get('error', '')}",
                priority="high")
            with _v11_lock:
                _v11_state["_last_fired_candle_ts"] = entry_result.get("fired_candle_ts", "")
                _v11_state["_entry_in_progress"] = False
            _save_v11_state()
            return
        # Use the broker's real fill price for SL/PnL math — not the candle close.
        entry_price = round(_live.get("fill_price", entry_price), 2)

    with _v11_lock:
        if _v11_state.get("in_trade"):
            logger.warning("[V11] Entry attempted while already in_trade — BLOCKED")
            return
        
        _v11_state["in_trade"]              = True
        _v11_state["_entry_in_progress"]    = False
        _v11_state["symbol"]                = symbol
        _v11_state["token"]                 = token
        _v11_state["direction"]             = direction
        _v11_state["strike"]                = int(strike or 0)
        _v11_state["entry_time"]            = now_str
        _v11_state["candles_held"]          = 0
        _v11_state["_last_fired_candle_ts"] = entry_result.get("fired_candle_ts", "")
        _v11_state["_other_token"]          = int(other_token or 0)
        
        _v11_state["entry_price"]           = entry_price
        _v11_state["qty"]                   = qty
        
        # Exits and Trailing SL
        initial_sl = entry_result.get("ema9_low", entry_price - 12)
        # Ensure initial SL is below entry price
        if initial_sl >= entry_price:
            initial_sl = round(entry_price - 5.0, 2)
        # Max-risk cap (2026-06-11, owner-approved): never risk more than 10 pts —
        # use ema9_low or entry-10, whichever is closer. Replay over 53 trades
        # (sl_replay_study.py) clipped zero winners and saved ~14 pts.
        initial_sl = max(initial_sl, round(entry_price - 10.0, 2))
        
        _v11_state["initial_sl"]            = initial_sl
        _v11_state["active_ratchet_sl"]     = initial_sl
        _v11_state["active_ratchet_tier"]   = "INITIAL"
        _v11_state["peak_ltp"]              = entry_price
        _v11_state["peak_pnl"]              = 0.0
        _v11_state["entry_regime"]          = entry_result.get("entry_mode") or ("V11_CE" if direction == "CE" else "V11_PE")
        _v11_state["xleg_other_margin"]     = entry_result.get("xleg_other_margin", 0.0)
        _v11_state["spot_regime_at_entry"]  = entry_result.get("spot_regime", "")
        _v11_state["_last_trade_date"]      = date.today().isoformat()

        # Market context at entry (for CSV analysis — bias/vix/rsi/session)
        _now_entry = datetime.now()
        _v11_state["vix_at_entry"]          = float(D.get_vix() or 0.0)
        _v11_state["hourly_rsi_at_entry"]   = float(D.get_hourly_rsi() or 0.0)
        _v11_state["bias_at_entry"]         = str(D.get_daily_bias() or "")
        _v11_state["session_at_entry"]      = D.get_session_block(_now_entry.hour, _now_entry.minute)

        # Data collection fields
        _v11_state["entry_spot"]            = float(spot_at_entry)
        _true_atm = int(round(spot_at_entry / 50) * 50) if spot_at_entry > 0 else int(strike)
        _v11_state["entry_atm_dist"]        = int(strike) - _true_atm
        # Vishal Anti-Chase Filter (VAC) — own-leg 3-candle run at entry (analysis only)
        _v11_state["own_m3_at_entry"]       = float(entry_result.get("own_m3", 0.0))
        # PDH/PDL context (analysis only — not a gate): where is spot vs yesterday's range?
        # range_pos: 0=at PDL, 1=at PDH, >1=above PDH, <0=below PDL
        _pdh, _pdl = _get_prev_day_hl()
        _v11_state["pdh_prev"] = _pdh
        _v11_state["pdl_prev"] = _pdl
        _v11_state["entry_range_pos"] = (
            round((spot_at_entry - _pdl) / (_pdh - _pdl), 3)
            if _pdh > _pdl > 0 and spot_at_entry > 0 else "")
        _v11_state["neighbor_ltp_otm"]      = float(neighbor_ltp_otm)
        _v11_state["neighbor_ltp_itm"]      = float(neighbor_ltp_itm)
        _v11_state["max_otm_drift"]         = 0.0

        # Reset entry-timing study markers
        _v11_state["first_profit_candle"] = 0
        _v11_state["first_profit_ltp"]    = 0.0
        _v11_state["first_profit_ts"]     = ""
        _v11_state["breakout_candle"]     = 0
        _v11_state["breakout_ltp"]        = 0.0
        _v11_state["breakout_ts"]         = ""

        # Clear any pending re-entry state
        _v11_state["_reentry_armed"]        = False
        _v11_state["_reentry_attempts"]     = 0

    logger.info(f"[V11] GOLDEN ENTRY: {symbol} Qty={qty} @ {entry_price}")

    # PDH/PDL proximity warning — analysis only, no gate
    try:
        _L = _daily_levels
        _spot = float(spot_at_entry or 0)
        if _spot > 0 and _L:
            if direction == "CE":
                _pdh = _L.get("PDH", 0)
                if _pdh > 0:
                    _dist = round(_pdh - _spot, 1)
                    if abs(_dist) <= 50:
                        logger.warning(f"[LEVELS] NEAR_PDH spot={_spot} pdh={_pdh} dist={_dist:+.1f} — CE entry near resistance")
            else:
                _pdl = _L.get("PDL", 0)
                if _pdl > 0:
                    _dist = round(_spot - _pdl, 1)
                    if abs(_dist) <= 50:
                        logger.warning(f"[LEVELS] NEAR_PDL spot={_spot} pdl={_pdl} dist={_dist:+.1f} — PE entry near support")
    except Exception:
        pass

    _ce_pe = "🟢" if direction == "CE" else "🔴"
    _tg_send(
        f"{_ce_pe} <b>V11 GOLDEN ENTRY {direction} {strike}</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"Entry ({'Mkt' if D.PAPER_MODE else 'Lmt'})  ₹{entry_price:.1f} ({qty} Qty) @ {now_str}\n"
        f"Initial SL   ₹{initial_sl:.1f} (1m EMA9 Low, max 10 pts)\n"
        f"XLeg Margin  {_v11_state['xleg_other_margin']:+.1f} pts\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "Trail: +9 → Protect entry-2  |  +11 → Lock entry+4  |  +15 → max(entry+9, Peak-10)",
        priority="critical"
    )
    _save_v11_state()


def _v11_execute_paper_exit(reason: str, exit_price: float):
    """Close V11 position. In LIVE mode places the real m.Stock SELL first, then
    records the close at the broker's fill price. Logs trade to CSV."""
    # ── LIVE: fire the real m.Stock MARKET SELL BEFORE clearing state ──────────
    # Outside _v11_lock — place_exit has its own retry/backoff (up to ~6s). If the
    # exit ultimately fails the position is STILL OPEN at the broker, so we keep
    # in_trade=True (no state clear, no CSV row) and alert for manual action; the
    # exit ladder / EOD check will re-fire on the next tick and retry.
    if not D.PAPER_MODE:
        with _v11_lock:
            if not _v11_state.get("in_trade"):
                return
            _ex_symbol = _v11_state.get("symbol", "")
            _ex_dir    = _v11_state.get("direction", "")
            _ex_token  = int(_v11_state.get("token", 0) or 0)
            _ex_qty    = int(_v11_state.get("qty", 0) or 0)
        _live = place_exit(_kite, _ex_symbol, _ex_token, _ex_dir, _ex_qty, exit_price, reason)
        if not _live.get("ok"):
            logger.critical("[V11] LIVE EXIT FAILED — position STILL OPEN: "
                            + _ex_symbol + " reason=" + reason
                            + " err=" + str(_live.get("error", "")))
            _tg_send(
                f"🚨 <b>V11 LIVE EXIT FAILED — MANUAL ACTION</b>\n"
                f"{_ex_symbol} ({_ex_qty} Qty) still OPEN — {reason}\n"
                f"Broker error: {_live.get('error', '')}",
                priority="critical")
            return
        # Record the close at the broker's real fill price.
        exit_price = round(_live.get("fill_price", exit_price), 2)

    with _v11_lock:
        if not _v11_state.get("in_trade"):
            return
        entry_price  = float(_v11_state.get("entry_price", 0))
        symbol       = _v11_state.get("symbol", "")
        direction    = _v11_state.get("direction", "")
        strike       = int(_v11_state.get("strike", 0) or 0)
        qty          = int(_v11_state.get("qty", 0) or 0)
        peak         = float(_v11_state.get("peak_pnl", 0))
        entry_time   = _v11_state.get("entry_time", "")
        candles      = int(_v11_state.get("candles_held", 0) or 0)
        tier         = _v11_state.get("active_ratchet_tier", "")
        token        = int(_v11_state.get("token", 0) or 0)
        other_tok    = int(_v11_state.get("_other_token", 0) or 0)
        dte_val      = int(_v11_state.get("dte", 0) or 0)
        entry_spot_val = float(_v11_state.get("entry_spot", 0))
        entry_atm_dist = int(_v11_state.get("entry_atm_dist", 0))
        own_m3_val     = float(_v11_state.get("own_m3_at_entry", 0) or 0)
        pdh_prev_val   = float(_v11_state.get("pdh_prev", 0) or 0)
        pdl_prev_val   = float(_v11_state.get("pdl_prev", 0) or 0)
        entry_range_pos_val = _v11_state.get("entry_range_pos", "")
        neighbor_otm = float(_v11_state.get("neighbor_ltp_otm", 0))
        neighbor_itm = float(_v11_state.get("neighbor_ltp_itm", 0))
        max_otm_drift = float(_v11_state.get("max_otm_drift", 0))
        entry_regime         = _v11_state.get("entry_regime", "V11_CE")
        xleg_margin          = float(_v11_state.get("xleg_other_margin", 0.0))
        initial_sl           = float(_v11_state.get("initial_sl", 0.0))
        spot_regime_at_entry  = str(_v11_state.get("spot_regime_at_entry", ""))
        vix_at_entry          = float(_v11_state.get("vix_at_entry", 0.0))
        first_profit_candle   = int(_v11_state.get("first_profit_candle", 0) or 0)
        first_profit_ltp      = float(_v11_state.get("first_profit_ltp", 0.0))
        first_profit_ts       = str(_v11_state.get("first_profit_ts", ""))
        breakout_candle       = int(_v11_state.get("breakout_candle", 0) or 0)
        breakout_ltp          = float(_v11_state.get("breakout_ltp", 0.0))
        breakout_ts           = str(_v11_state.get("breakout_ts", ""))
        hourly_rsi_at_entry  = float(_v11_state.get("hourly_rsi_at_entry", 0.0))
        bias_at_entry        = str(_v11_state.get("bias_at_entry", ""))
        session_at_entry     = str(_v11_state.get("session_at_entry", ""))

        # Clear position state
        _v11_state["in_trade"]            = False
        _v11_state["symbol"]              = ""
        _v11_state["token"]               = 0
        _v11_state["direction"]           = ""
        _v11_state["strike"]              = 0
        _v11_state["entry_price"]         = 0.0
        _v11_state["peak_pnl"]            = 0.0
        _v11_state["active_ratchet_tier"] = ""
        _v11_state["active_ratchet_sl"]   = 0.0
        _v11_state["candles_held"]        = 0
        
        _v11_state["initial_sl"]          = 0.0
        _v11_state["peak_ltp"]            = 0.0
        _v11_state["xleg_other_margin"]   = 0.0

        pnl_pts_now = round(exit_price - entry_price, 2)
        # Update daily counters under lock
        _v11_state["_pnl_today_pts"] = round(_v11_state.get("_pnl_today_pts", 0) + pnl_pts_now, 2)
        _v11_state["_trades_today"]  = _v11_state.get("_trades_today", 0) + 1
        if pnl_pts_now > 0:
            _v11_state["_wins_today"]   = _v11_state.get("_wins_today", 0) + 1
        elif pnl_pts_now < 0:
            _v11_state["_losses_today"] = _v11_state.get("_losses_today", 0) + 1
            
        if reason == "EMERGENCY_SL":
            _v11_state["_sl_cooldown_skip_next"] = True
            _v11_state["_sl_cooldown_direction"] = direction

        _v11_state["_reentry_armed"]              = False
        _v11_state["_reentry_attempts"]           = 0
        _v11_state["_reentry_last_checked_epoch"] = 0.0
        _v11_state["_reentry_direction"]          = direction
        _v11_state["_reentry_token"]              = token
        _v11_state["_reentry_strike"]             = strike
        _v11_state["_reentry_other_token"]        = other_tok
        _v11_state["_reentry_exit_price"]         = round(exit_price, 2)
        _v11_state["_last_trade_date"]            = date.today().isoformat()
        
        # Exit candle guard: record 1-min candle we are exiting in (matches _sh_1m_bk_ts)
        _now_exit = datetime.now()
        _v11_state["_last_exit_candle_ts"] = str(
            _now_exit.replace(second=0, microsecond=0)
        )
        # Same-side 3-min blocker: stamp direction + unix time of this exit
        _v11_state["_last_exit_time_unix"]     = time.time()
        _v11_state["_last_exit_direction_v10"] = direction

    # --- Lock released: safe to read captured locals for logging ---
    pnl_pts   = round(exit_price - entry_price, 2)
    pnl_rs    = round(pnl_pts * qty, 2)
    exit_time = datetime.now().strftime("%H:%M:%S")
    exit_spot = round(D.get_ltp(D.NIFTY_SPOT_TOKEN), 1)

    # Charges
    charges = {}
    try:
        charges = calculate_charges(entry_price, exit_price, qty, num_exit_orders=1)
        net_pnl = charges["net_pnl"]
        total_charges = charges["total_charges"]
    except Exception:
        net_pnl = pnl_rs
        total_charges = 0.0

    # Log to CSV
    try:
        _v11_row = {
            "date": date.today().isoformat(),
            "entry_time": entry_time, "exit_time": exit_time,
            "symbol": symbol, "direction": direction, "strike": strike,
            "entry_price": entry_price, "exit_price": exit_price,
            "pnl_pts": pnl_pts, "pnl_rs": pnl_rs,
            "gross_pnl_rs": pnl_rs, "net_pnl_rs": net_pnl,
            "peak_pnl": peak, "exit_reason": reason,
            "dte": dte_val, "candles_held": candles, "session": session_at_entry,
            "sl_pts": -12, "vix_at_entry": vix_at_entry,
            "entry_mode": entry_regime,
            "bias": bias_at_entry, "hourly_rsi": hourly_rsi_at_entry,
            "spot_regime": spot_regime_at_entry,
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
            "xleg_other_dying": "", "xleg_other_margin": xleg_margin,
            "spike_close": "", "spike_target": "", "spike_fill": "", "spike_wait_used": "",
            "entry_spot": entry_spot_val, "exit_spot": exit_spot,
            "pdh_prev": pdh_prev_val, "pdl_prev": pdl_prev_val,
            "entry_range_pos": entry_range_pos_val,
            "entry_atm_dist": entry_atm_dist,
            "own_m3_at_entry": own_m3_val,
            "neighbor_ltp_otm": neighbor_otm, "neighbor_ltp_itm": neighbor_itm,
            "max_otm_drift": round(max_otm_drift, 1),
            # Entry-timing study: when did the trade actually move?
            "first_profit_candle": first_profit_candle,
            "first_profit_ltp":    first_profit_ltp,
            "first_profit_ts":     first_profit_ts,
            "breakout_candle":     breakout_candle,
            "breakout_ltp":        breakout_ltp,
            "breakout_ts":         breakout_ts,
            "early_candles":       (breakout_candle - 1) if breakout_candle else "",
        }
        import csv as _csv
        log_path = D.TRADE_LOG_PATH
        with open(log_path, "a", newline="") as f:
            w = _csv.DictWriter(f, fieldnames=TRADE_FIELDNAMES, extrasaction="ignore")
            w.writerow(_v11_row)
    except Exception as _le:
        logger.warning("[V11] Trade log write error: " + str(_le))

    logger.info("[V11] PAPER EXIT: " + symbol + " qty=" + str(qty)
                + " ref=" + str(exit_price) + " reason=" + reason
                + " pnl=" + str(pnl_pts) + "pts")

    _tg_send(
        "⚡ <b>V11 GOLDEN EXIT {dir} {strike}</b>\n".format(dir=direction, strike=strike)
        + "<b>" + reason + "</b>    " + ("+" if pnl_pts >= 0 else "") + "{:.1f}".format(pnl_pts) + " pts\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        + f"Entry  ₹{entry_price:.1f} × {qty}  Exit ₹{exit_price:.1f}\n"
        f"Peak   +{peak:.1f} pts  Tier {tier}\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "Gross  " + ("+" if pnl_rs >= 0 else "") + "₹" + "{:.0f}".format(pnl_rs) + "\n"
        "Charges -₹" + "{:.0f}".format(total_charges) + "\n"
        "Net    " + ("+" if net_pnl >= 0 else "") + "₹" + "{:.0f}".format(net_pnl) + "\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "DAY " + ("+" if _v11_state.get("_pnl_today_pts", 0) >= 0 else "")
        + "{:.1f}".format(_v11_state.get("_pnl_today_pts", 0)) + " pts ("
        + str(_v11_state.get("_wins_today", 0)) + "W "
        + str(_v11_state.get("_losses_today", 0)) + "L)",
        priority="critical"
    )
    _save_v11_state()

    # Refresh the full dashboard snapshot now (same as the V7 exit path) —
    # _update_dashboard_ltp() re-stamps ts but never recomputes the today
    # block, so without this the snapshot shows stale day counters until
    # the next 1-min candle triggers _write_dashboard.
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
    except Exception as _de:
        logger.debug("[DASH] Post-exit refresh: " + str(_de))


def _v11_check_exit():
    """Tick-based exit check for V11 position. Called every scan cycle."""
    with _v11_lock:
        if not _v11_state.get("in_trade"):
            return
        token          = int(_v11_state.get("token", 0) or 0)
        initial_sl     = float(_v11_state.get("initial_sl", 0.0))
        peak_ltp       = float(_v11_state.get("peak_ltp", 0.0))
        peak_pnl_snap  = float(_v11_state.get("peak_pnl", 0.0))
        active_tier    = str(_v11_state.get("active_ratchet_tier", "INITIAL"))
        entry_price_snap = float(_v11_state.get("entry_price", 0.0))
        direction      = _v11_state.get("direction", "")
        strike         = int(_v11_state.get("strike", 0) or 0)
        
        # Increment candles_held once per minute
        _cur_min = datetime.now().strftime("%H:%M")
        _new_candle = _cur_min != _v11_state.get("_last_minute", "")
        if _new_candle:
            _v11_state["_last_minute"] = _cur_min
            _v11_state["candles_held"] = _v11_state.get("candles_held", 0) + 1

        candles_held   = int(_v11_state.get("candles_held", 0))

    # Persist state once per candle so peak_pnl / candles_held survive a restart.
    # Safe to call here: _v11_lock is RLock so re-entry from _save_v11_state() is allowed.
    if _new_candle:
        _save_v11_state()
        with _v11_live_lock:
            _ce_snap = dict(_v11_live.get("CE", {}))
            _pe_snap = dict(_v11_live.get("PE", {}))
        logger.info(
            f"[CANDLE] c={candles_held} dir={direction} entry={entry_price_snap:.2f}"
            f" peak={peak_pnl_snap:+.2f} tier={active_tier}"
            f" | CE mom={_ce_snap.get('momentum_gap', 0):+.2f}(ok={_ce_snap.get('momentum_ok', False)})"
            f" decay={_ce_snap.get('decay_margin', 0):+.2f}(ok={_ce_snap.get('decay_ok', False)})"
            f" | PE mom={_pe_snap.get('momentum_gap', 0):+.2f}(ok={_pe_snap.get('momentum_ok', False)})"
            f" decay={_pe_snap.get('decay_margin', 0):+.2f}(ok={_pe_snap.get('decay_ok', False)})"
        )

    if not token:
        return
    ltp = D.get_ltp(token)
    if ltp <= 0:
        # No live tick (feed down / token never subscribed after a restart).
        # SL/trail checks need a real price, but the EOD hard-close must
        # still fire — fall back to entry price, same as /forceexit
        # (2026-06-10: stuck trade after a post-15:00 restart).
        _eod_str = CFG.exit_ema9_band("eod_exit_time", "15:20") if hasattr(CFG, "exit_ema9_band") else "15:20"
        try:
            _eh, _em = _eod_str.split(":")
            _eod_mins = int(_eh) * 60 + int(_em)
        except Exception:
            _eod_mins = 15 * 60 + 20
        if datetime.now().hour * 60 + datetime.now().minute >= _eod_mins:
            logger.warning("[V11] EOD reached with no live tick — force-closing at entry price")
            _v11_execute_paper_exit("EOD_EXIT", round(entry_price_snap, 2))
        return

    with _v11_lock:
        avg_entry = entry_price_snap

        # Update peak LTP
        if ltp > peak_ltp:
            peak_ltp = ltp
            _v11_state["peak_ltp"] = peak_ltp
            _v11_state["peak_pnl"] = round(peak_ltp - avg_entry, 2)

        peak_pnl = peak_ltp - avg_entry

        # Study: mark first-profit tick and breakout tick (every ~1s)
        _cur_pnl = round(ltp - avg_entry, 2)
        if _cur_pnl > 0 and not _v11_state.get("first_profit_ts"):
            _v11_state["first_profit_candle"] = candles_held
            _v11_state["first_profit_ltp"]    = round(ltp, 2)
            _v11_state["first_profit_ts"]     = datetime.now().strftime("%H:%M:%S")
        if _cur_pnl > V11_BREAKOUT_THRESHOLD and not _v11_state.get("breakout_ts"):
            _v11_state["breakout_candle"] = candles_held
            _v11_state["breakout_ltp"]    = round(ltp, 2)
            _v11_state["breakout_ts"]     = datetime.now().strftime("%H:%M:%S")

        # Determine dynamic trail SL
        current_sl, tier = _v11_compute_trail_sl(avg_entry, peak_pnl, initial_sl)
        
        prev_tier = _v11_state.get("active_ratchet_tier", "")
        _v11_state["active_ratchet_tier"] = tier
        _v11_state["active_ratchet_sl"]   = round(current_sl, 2)

        # Tier upgrade alert
        if prev_tier and prev_tier != tier and tier != "INITIAL":
            _tg_send(
                f"⚡ <b>V11 SL UPGRADED → {tier}</b>\n"
                f"Peak: +{peak_pnl:.1f} pts (LTP ₹{ltp:.1f})\n"
                f"New Stop: ₹{current_sl:.1f} (entry ₹{avg_entry:.1f})",
                priority="critical"
            )
            _save_v11_state()

        # Track max OTM drift
        _spot_now = D.get_ltp(D.NIFTY_SPOT_TOKEN)
        if _spot_now > 0 and strike > 0:
            _otm = max(0.0, (strike - _spot_now) if direction == "CE" else (_spot_now - strike))
            if _otm > _v11_state.get("max_otm_drift", 0.0):
                _v11_state["max_otm_drift"] = _otm

    # Exits checking (tick-based). The +25 lock is now a ladder rung (LOCK_25) inside
    # _v11_compute_trail_sl — it raises the stop to entry+25 once peak hits +25 and keeps
    # trailing peak-10 above, so the runners are no longer capped (owner-approved 2026-06-15).
    if ltp <= current_sl:
        exit_reason = {"INITIAL": "EMERGENCY_SL", "PROTECT": "PROTECT_2",
                       "LOCK_4": "LOCK_4", "LOCK_25": "LOCK_25"}.get(tier, "VISHAL_TRAIL")
        _v11_execute_paper_exit(exit_reason, round(current_sl, 2))
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
        _v11_execute_paper_exit("EOD_EXIT", float(ltp))

# ═══════════════════════════════════════════════════════════════
#  STRIKE LOCKING — stable scanning, no flickering
# ═══════════════════════════════════════════════════════════════

_locked_ce_strike = None
_locked_pe_strike = None
_locked_at_spot   = None
_locked_tokens    = {}
_LOCK_SHIFT_THRESHOLD = 150  # relock if spot moves 150+ pts
_last_dash_args = {}  # cached dashboard args for post-exit refresh
_v11_last_entry_scan_ts = 0.0  # throttle V11 entry scan to every 3s
spot_3m: dict = {}  # BUG-B fix: module-level cache; updated by _write_dashboard() each call

V11_MIN_EMA9H_GAP = 3.5   # momentum breakout floor (single source of truth)
V11_OPEN_BLACKOUT_END = dtime(10, 0)  # hard gate: no entries before 10:00 (owner 2026-06-15: disciplined window; 09:00-10:00 bled -Rs788/trade per conviction_sizing_study)
# V13 SHADOW runs a WIDER paper window than V11 (owner 2026-06-21): 09:30-15:15
# for max samples for the tick-flow study. V11 (primary) is UNCHANGED — it keeps
# its proven is_trading_window cutoff (enforced explicitly in the V11 block below
# now that the scanner gate is widened to cover V13's later window).
V13_OPEN_BLACKOUT_END = dtime(9, 30)
V13_ENTRY_CUTOFF      = dtime(15, 15)
# OPP DECAY band [-9, -7] for dte>=2 (owner-approved 2026-06-18). Tightened from
# [-8, -6]: the decay-floor sweep over all logged trades + today's session showed
# the shallow half of [-8,-6] holds the losers — on dte>=2, band [-8,-7] = 78% WR
# +8.73/trade (n=9) vs [-8,-6] 44% WR +1.52 (n=27); today (dte5) the +25 winner sat
# at -7.23 (inside) while all 4 losers were -6.3..-6.9 (excluded). Deep floor extended
# -8 -> -9 (neutral on dte>=2, same 9 trades). Prior band [-8,-6] was owner-final
# 2026-06-12 (shallow (-6,-4] ran 2W/9L). dte 0/1 %-gate below is UNCHANGED.
V11_DECAY_LOW  = -9.0  # deep (lower) bound
V11_DECAY_HIGH = -7.0  # shallow (upper) bound → band [-9, -7]

# ── Per-DTE %-of-premium entry gate (owner-approved 2026-06-16) ──────────────
# Near-expiry ATM premium collapses (~50 @dte0, ~113 @dte1) so the absolute
# MOMENTUM +3.5 / OPP DECAY [-9,-7] gate is effectively a different, looser
# strategy on expiry-week days — it over-fires on cheap premium. For dte 0/1
# the gate is normalized to % of premium. Calibrated by the expiry-aligned
# per-DTE sweep (~/lab_data/perdte_pct_gate_study.py, 21 days / 5 weekly
# expiries): decay floor -4.8% is STABLE across DTE; momentum % rises away
# from expiry (2.3% @dte0, 3.0% @dte1). dte>=2 KEEPS the locked absolute gate
# untouched. In-sample (dte0 n=8 / dte1 n=17) — owner shipped for live
# validation; revisit at the ~06-26 FINAL PACKAGE review.
V11_PCT_GATE_DTE = {
    0: {"mom_pct": 0.023, "decay_lo": -0.048, "decay_hi": -0.027},
    1: {"mom_pct": 0.030, "decay_lo": -0.048, "decay_hi": -0.027},
}


def _v11_gate_check(dte, own_close, own_ema9h, opp_margin, opp_close, opp_ema9l):
    """(momentum_ok, decay_ok). dte 0/1 → %-of-premium; dte>=2 → locked abs."""
    cfg = V11_PCT_GATE_DTE.get(int(dte or 0))
    if cfg:
        mom_ok = (own_close >= own_ema9h + cfg["mom_pct"] * own_close) if own_ema9h > 0 else False
        if opp_close > 0 and opp_ema9l > 0:
            _ratio = opp_margin / opp_close
            decay_ok = cfg["decay_lo"] <= _ratio <= cfg["decay_hi"]
        else:
            decay_ok = False
        return mom_ok, decay_ok
    # dte>=2 (or unmapped): locked absolute gate — unchanged
    mom_ok = (own_close >= own_ema9h + V11_MIN_EMA9H_GAP) if own_ema9h > 0 else False
    decay_ok = (V11_DECAY_LOW <= opp_margin <= V11_DECAY_HIGH) if opp_ema9l > 0 else False
    return mom_ok, decay_ok


def _v13_gate_check(own_close, own_ema9l, opp_margin_high, opp_ema9h):
    """(momentum_ok, decay_ok) for V13 (owner 2026-06-20, strategy_version: v13).
    Same +3.5 gap / [-9,-7] band as V11 but off the OTHER ema9 lines:
      MOMENTUM  own_close >= own ema9_LOW  + 3.5   (vs V11 ema9_high)
      OPP DECAY (opp_close - opp ema9_HIGH) in [-9,-7]  (vs V11 ema9_low)
    opp_margin_high = opp_close - opp_ema9h (precomputed by caller).
    NOTE: absolute gate applied to ALL dte (per owner's literal spec). On dte 0/1
    cheap premium this fires more than V11's %-gate — owner-accepted, paper-validate.
    UNTESTED vs V11's proven edge — running paper only."""
    mom_ok = (own_close >= own_ema9l + V11_MIN_EMA9H_GAP) if own_ema9l > 0 else False
    decay_ok = (V11_DECAY_LOW <= opp_margin_high <= V11_DECAY_HIGH) if opp_ema9h > 0 else False
    return mom_ok, decay_ok


V11_BREAKOUT_THRESHOLD = 5.0          # pts above avg_entry to mark "real move started"
# LOCK_25 floor + tight trail (owner-approved 2026-06-15): once peak hits +25, the exit ladder
# locks entry+25 as a hard SL floor AND trails peak-5 above it (see _v11_compute_trail_sl) —
# guarantees +25 min while grabbing max points on the runner. Evolved same day: +25 hard-exit
# -> +25 floor w/ peak-10 trail -> owner tightened to peak-5 for max capture. target_replay.py
# (92tr): +25-floor variant +163 vs +108.7 hard-exit vs +87.8 bare trail; peak-5 grabs ~+5 more
# per clean runner (with more shakeout risk on choppy pullbacks — owner-accepted trade-off).
V11_TARGET_PTS = 25.0
# CUTOVER FLAG: True = V11 Golden scanner places the live paper trades.
V11_LIVE = True
# Live gate snapshot for dashboard monitoring — updated every scanner cycle, per side
_v11_live_lock = threading.Lock()
_v11_live = {"CE": {}, "PE": {}}
_v11_scanner_last_ts: float = 0.0   # throttle: V11 Golden scanner runs every 3s


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

    # Under Upstox the bot runs with NO Kite session (kite is None) but option
    # tokens resolve via upstox_data regardless — so gate on expiry, not kite,
    # or _locked_tokens stays empty and every scan cycle logs "Relock failed"
    # (2026-06-22). On Kite, kite is required as before.
    if (kite or DATA_PROVIDER == "upstox") and expiry:
        # Active legs (ATM)
        for _dt, _strike in [("CE", _locked_ce_strike), ("PE", _locked_pe_strike)]:
            _tk = D.get_option_tokens(kite, _strike, expiry)
            if _tk.get(_dt):
                _locked_tokens[_dt] = _tk[_dt]
                _locked_tokens[_dt]["strike"] = _strike  # ensure strike survives into V11 entry display

        # Pre-warm neighbors — ATM±100 CE+PE (always, regardless of multi flag)
        # ±100 to land on liquid 100-grid strikes (v22 ITM scheme).
        # Keys: CE_UP / CE_DN / PE_UP / PE_DN
        for _suffix, _delta in (("UP", +V11_STRIKE_STEP), ("DN", -V11_STRIKE_STEP)):
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
        # Tick-flow study: upgrade the two PRIMARY legs (CE/PE) to Upstox full
        # mode so tick_flow.py sees DOM/OI/IV. Neighbors stay ltpc. (Secret
        # collector; no-op on Kite.)
        _flow_legs = [(_locked_tokens.get("CE") or {}).get("token"),
                      (_locked_tokens.get("PE") or {}).get("token")]
        D.subscribe_full_flow(_flow_legs)

    logger.info("[MAIN] Strikes LOCKED (ITM-100): CE=" + str(_locked_ce_strike)
                + " PE=" + str(_locked_pe_strike)
                + " (neighbors ±" + str(V11_STRIKE_STEP)
                + " pre-warmed) at spot=" + str(round(spot, 1)))
    if (kite or DATA_PROVIDER == "upstox") and expiry and _locked_ce_strike:
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
            # show zeroed current_ema9_high/low until the next exit-check
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


_V11_PERSIST_FIELDS = [
    "in_trade", "symbol", "token", "direction", "strike",
    "entry_price", "entry_time", "qty",
    "peak_pnl", "active_ratchet_tier", "active_ratchet_sl",
    "candles_held", "_other_token",
    "_sl_cooldown_skip_next", "_force_exit_ts",
    "_pnl_today_pts", "_trades_today", "_wins_today", "_losses_today",
    "_v11_both_rejected_ts", "_last_trade_date", "_last_exit_candle_ts",
    "_last_exit_time_unix", "_last_exit_direction_v10",
    "initial_sl", "entry_regime",
    "peak_ltp", "xleg_other_margin",
    "spot_regime_at_entry",
    # Data-collection fields (survive restart so CSV row is correct)
    "entry_spot", "entry_atm_dist", "own_m3_at_entry",
    "pdh_prev", "pdl_prev", "entry_range_pos",
    "neighbor_ltp_otm", "neighbor_ltp_itm", "max_otm_drift",
    "vix_at_entry", "hourly_rsi_at_entry", "bias_at_entry", "session_at_entry",
    # Entry-timing study fields
    "first_profit_candle", "first_profit_ltp", "first_profit_ts",
    "breakout_candle", "breakout_ltp", "breakout_ts",
]

def _save_v11_state():
    try:
        with _v11_lock:
            subset = {k: _v11_state.get(k) for k in _V11_PERSIST_FIELDS}
        tmp = D.V11_STATE_FILE_PATH + ".tmp"
        with open(tmp, "w") as f:
            json.dump(subset, f, indent=2, default=str)
        os.replace(tmp, D.V11_STATE_FILE_PATH)
    except Exception as e:
        logger.error("[V11] State save error: " + str(e))

def _load_v11_state():
    if not os.path.isfile(D.V11_STATE_FILE_PATH):
        return
    try:
        with open(D.V11_STATE_FILE_PATH) as f:
            saved = json.load(f)
        with _v11_lock:
            for k, v in saved.items():
                if k in _v11_state:
                    _v11_state[k] = v
        logger.info("[V11] State loaded from disk")
        # Reset daily counters if state file is from a previous day
        _today = date.today().isoformat()
        _last_date = str(saved.get("_last_trade_date", ""))
        if _last_date != _today:
            with _v11_lock:
                _v11_state["_pnl_today_pts"] = 0.0
                _v11_state["_trades_today"]  = 0
                _v11_state["_wins_today"]    = 0
                _v11_state["_losses_today"]  = 0
                _v11_state["_v11_both_rejected_ts"] = 0.0
                _v11_state["_last_trade_date"] = _today
                _v11_state["_sl_cooldown_skip_next"] = False  # clear stale cooldown on new day
            logger.info("[V11] New trading day — daily counters reset (last_date=" + _last_date + ")")
            _save_v11_state()  # persist the reset so the on-disk file isn't a stale snapshot
        if _v11_state.get("in_trade"):
            _sym  = str(_v11_state.get("symbol", ""))
            _ep   = float(_v11_state.get("entry_price", 0))
            _peak = float(_v11_state.get("peak_pnl", 0))
            _tier  = str(_v11_state.get("active_ratchet_tier", "INITIAL"))
            _sl    = float(_v11_state.get("active_ratchet_sl", 0) or 0)
            if _sl <= 0: _sl = round(_ep - 12, 2)
            _tok   = int(_v11_state.get("token", 0) or 0)
            _ltp   = D.get_ltp(_tok) if _tok else 0
            _pnl   = round(_ltp - _ep, 1) if _ltp else 0
            _room  = round(_ltp - _sl, 1) if _ltp else 0
            _dir   = str(_v11_state.get("direction", ""))
            _strk  = str(_v11_state.get("strike", ""))
            _qty   = int(_v11_state.get("qty", 0) or 0)
            _etime = str(_v11_state.get("entry_time", ""))
            _emj   = "🟢" if _dir == "CE" else "🔴"
            logger.info("[V11] Was in trade on last shutdown — " + _sym + " monitoring resumed")
            _tg_send(
                "⚡ <b>V11 restarted mid-trade</b>\n"
                + _emj + " " + _dir + " " + _strk + " · qty " + str(_qty) + "\n"
                "━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                "Entry  ₹" + "{:.2f}".format(_ep) + "  @ " + _etime + "\n"
                + ("LTP    ₹" + "{:.2f}".format(_ltp)
                   + "  (" + ("+" if _pnl >= 0 else "") + str(_pnl) + " pts)\n" if _ltp else "LTP    — (no tick yet)\n")
                + "Peak   +" + "{:.1f}".format(_peak) + " pts\n"
                "━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                "Tier   " + _tier + " · SL ₹" + "{:.2f}".format(_sl)
                + ("  (Room " + ("+" if _room >= 0 else "") + str(_room) + ")" if _ltp else "") + "\n"
                "✅ Exit monitoring resumed."
            )
    except Exception as e:
        logger.error("[V11] State load error: " + str(e))


# ═══════════════════════════════════════════════════════════════
#  V13 SHADOW ENGINE — runs CONCURRENTLY with V11, PAPER-ONLY.
#  Owner 2026-06-20: surface both gates on the dashboard for a live
#  A/B. Shares the locked strikes, tick feed and exit ladder
#  (_v11_compute_trail_sl) with V11; keeps its OWN independent
#  position, cooldowns, day counters, state file and trade log.
#  NEVER places a real m.Stock order — shadow comparison only.
# ═══════════════════════════════════════════════════════════════
_v13_state = {
    "in_trade": False, "symbol": "", "token": 0, "direction": "",
    "strike": 0, "entry_price": 0.0, "entry_time": "", "qty": 0,
    "peak_pnl": 0.0, "peak_ltp": 0.0,
    "active_ratchet_tier": "", "active_ratchet_sl": 0.0,
    "initial_sl": 0.0, "candles_held": 0, "_last_minute": "",
    "_other_token": 0, "entry_mode": "", "dte": 0,
    "xleg_other_margin": 0.0, "spot_regime_at_entry": "", "entry_spot": 0.0,
    "pdh_prev": 0.0, "pdl_prev": 0.0, "entry_range_pos": "",
    "vel2_at_entry": None,
    "_last_fired_candle_ts": "", "_last_exit_candle_ts": "",
    "_last_exit_time_unix": 0.0, "_last_exit_direction": "",
    "_sl_cooldown_skip_next": False,
    "_pnl_today_pts": 0.0, "_trades_today": 0,
    "_wins_today": 0, "_losses_today": 0, "_last_trade_date": "",
}
_v13_lock = threading.RLock()  # RLock: _save_v13_state() re-enters from exit-check block
_v13_live_lock = threading.Lock()
_v13_live = {"CE": {}, "PE": {}}

_V13_PERSIST_FIELDS = [
    "in_trade", "symbol", "token", "direction", "strike",
    "entry_price", "entry_time", "qty", "peak_pnl", "peak_ltp",
    "active_ratchet_tier", "active_ratchet_sl", "initial_sl",
    "candles_held", "_other_token", "entry_mode",
    "xleg_other_margin", "spot_regime_at_entry", "entry_spot",
    "pdh_prev", "pdl_prev", "entry_range_pos", "vel2_at_entry",
    "_last_fired_candle_ts", "_last_exit_candle_ts",
    "_last_exit_time_unix", "_last_exit_direction",
    "_sl_cooldown_skip_next",
    "_pnl_today_pts", "_trades_today", "_wins_today", "_losses_today",
    "_last_trade_date",
]


def _save_v13_state():
    try:
        with _v13_lock:
            subset = {k: _v13_state.get(k) for k in _V13_PERSIST_FIELDS}
        tmp = D.V13_STATE_FILE_PATH + ".tmp"
        with open(tmp, "w") as f:
            json.dump(subset, f, indent=2, default=str)
        os.replace(tmp, D.V13_STATE_FILE_PATH)
    except Exception as e:
        logger.error("[V13] State save error: " + str(e))


def _load_v13_state():
    if not os.path.isfile(D.V13_STATE_FILE_PATH):
        return
    try:
        with open(D.V13_STATE_FILE_PATH) as f:
            saved = json.load(f)
        with _v13_lock:
            for k, v in saved.items():
                if k in _v13_state:
                    _v13_state[k] = v
        # New trading day → reset daily counters (mirror V11)
        _today = date.today().isoformat()
        if str(saved.get("_last_trade_date", "")) != _today:
            with _v13_lock:
                _v13_state["_pnl_today_pts"] = 0.0
                _v13_state["_trades_today"]  = 0
                _v13_state["_wins_today"]    = 0
                _v13_state["_losses_today"]  = 0
                _v13_state["_last_trade_date"] = _today
                _v13_state["_sl_cooldown_skip_next"] = False
            _save_v13_state()
        logger.info("[V13] Shadow state loaded from disk")
    except Exception as e:
        logger.error("[V13] State load error: " + str(e))


def _v13_execute_paper_entry(direction, strike, symbol, token, entry_price,
                             entry_mode, other_token, opp_margin_high,
                             spot_at_entry, fired_candle_ts, ema9_low, dte,
                             vel2_at_entry=None):
    """Open a V13 SHADOW paper position — single lot, fill at candle close.
    PAPER-ONLY: never calls m.Stock (shadow A/B against V11)."""
    qty = CFG.get().get("lots", {}).get("count", 1) * D.get_lot_size()
    now_str = datetime.now().strftime("%H:%M:%S")
    with _v13_lock:
        if _v13_state.get("in_trade"):
            return
        # Initial SL = breakout-candle ema9_low, capped at entry-10 (same ladder
        # base as V11). Fallback entry-5 if ema9_low not below entry.
        initial_sl = float(ema9_low) if ema9_low else (entry_price - 5.0)
        if initial_sl >= entry_price:
            initial_sl = round(entry_price - 5.0, 2)
        initial_sl = max(initial_sl, round(entry_price - 10.0, 2))
        _v13_state.update({
            "in_trade": True, "symbol": symbol, "token": int(token),
            "direction": direction, "strike": int(strike or 0),
            "entry_price": round(entry_price, 2), "entry_time": now_str,
            "qty": qty, "candles_held": 0, "_last_minute": "",
            "_other_token": int(other_token or 0),
            "_last_fired_candle_ts": fired_candle_ts,
            "initial_sl": initial_sl, "active_ratchet_sl": initial_sl,
            "active_ratchet_tier": "INITIAL",
            "peak_ltp": round(entry_price, 2), "peak_pnl": 0.0,
            "entry_mode": entry_mode,
            "xleg_other_margin": round(opp_margin_high, 2),
            "spot_regime_at_entry": spot_3m.get("regime", "") if isinstance(spot_3m, dict) else "",
            "entry_spot": float(spot_at_entry or 0),
            "dte": int(dte or 0),
            "vel2_at_entry": (round(float(vel2_at_entry), 2) if vel2_at_entry is not None else None),
            "_last_trade_date": date.today().isoformat(),
        })
        # PDH/PDL context (analysis only — feeds the box-break shadow study;
        # mirrors V11). range_pos: 0=at PDL, 1=at PDH, >1=above PDH, <0=below PDL
        _pdh, _pdl = _get_prev_day_hl()
        _spot_e = float(spot_at_entry or 0)
        _v13_state["pdh_prev"] = _pdh
        _v13_state["pdl_prev"] = _pdl
        _v13_state["entry_range_pos"] = (
            round((_spot_e - _pdl) / (_pdh - _pdl), 3)
            if _pdh > _pdl > 0 and _spot_e > 0 else "")
    logger.info(f"[V13] SHADOW ENTRY: {symbol} Qty={qty} @ {entry_price}")
    _emj = "🟢" if direction == "CE" else "🔴"
    _tg_send(
        f"🧪 <b>V13 SHADOW ENTRY {direction} {strike}</b>\n"
        f"{_emj} Entry ₹{entry_price:.1f} × {qty}  SL ₹{initial_sl:.1f}\n"
        f"Decay {opp_margin_high:+.1f} · vel2 "
        f"{('%+.1f' % vel2_at_entry) if vel2_at_entry is not None else 'n/a'} · "
        f"dte {dte} · paper A/B vs V11",
        priority="high")
    _save_v13_state()


def _v13_execute_paper_exit(reason, exit_price):
    """Close the V13 SHADOW position, log to its own CSV. PAPER-ONLY."""
    with _v13_lock:
        if not _v13_state.get("in_trade"):
            return
        entry_price    = float(_v13_state.get("entry_price", 0))
        symbol         = _v13_state.get("symbol", "")
        direction      = _v13_state.get("direction", "")
        strike         = int(_v13_state.get("strike", 0) or 0)
        qty            = int(_v13_state.get("qty", 0) or 0)
        peak           = float(_v13_state.get("peak_pnl", 0))
        entry_time     = _v13_state.get("entry_time", "")
        candles        = int(_v13_state.get("candles_held", 0) or 0)
        tier           = _v13_state.get("active_ratchet_tier", "")
        dte_val        = int(_v13_state.get("dte", 0) or 0)
        entry_mode     = _v13_state.get("entry_mode", "V13_CE")
        xleg_margin    = float(_v13_state.get("xleg_other_margin", 0.0))
        spot_regime    = str(_v13_state.get("spot_regime_at_entry", ""))
        entry_spot_val = float(_v13_state.get("entry_spot", 0))
        pdh_prev_val   = float(_v13_state.get("pdh_prev", 0) or 0)
        pdl_prev_val   = float(_v13_state.get("pdl_prev", 0) or 0)
        range_pos_val  = _v13_state.get("entry_range_pos", "")

        # Clear position
        _v13_state.update({
            "in_trade": False, "symbol": "", "token": 0, "direction": "",
            "strike": 0, "entry_price": 0.0, "peak_pnl": 0.0,
            "active_ratchet_tier": "", "active_ratchet_sl": 0.0,
            "candles_held": 0, "initial_sl": 0.0, "peak_ltp": 0.0,
            "xleg_other_margin": 0.0,
        })
        pnl_pts_now = round(exit_price - entry_price, 2)
        _v13_state["_pnl_today_pts"] = round(_v13_state.get("_pnl_today_pts", 0) + pnl_pts_now, 2)
        _v13_state["_trades_today"]  = _v13_state.get("_trades_today", 0) + 1
        if pnl_pts_now > 0:
            _v13_state["_wins_today"] = _v13_state.get("_wins_today", 0) + 1
        elif pnl_pts_now < 0:
            _v13_state["_losses_today"] = _v13_state.get("_losses_today", 0) + 1
        if reason == "EMERGENCY_SL":
            _v13_state["_sl_cooldown_skip_next"] = True
        _now_exit = datetime.now()
        _v13_state["_last_exit_candle_ts"]  = str(_now_exit.replace(second=0, microsecond=0))
        _v13_state["_last_exit_time_unix"]  = time.time()
        _v13_state["_last_exit_direction"]  = direction
        _v13_state["_last_trade_date"]      = date.today().isoformat()

    pnl_pts   = round(exit_price - entry_price, 2)
    pnl_rs    = round(pnl_pts * qty, 2)
    exit_time = datetime.now().strftime("%H:%M:%S")
    exit_spot = round(D.get_ltp(D.NIFTY_SPOT_TOKEN), 1)
    charges   = {}
    try:
        charges = calculate_charges(entry_price, exit_price, qty, num_exit_orders=1)
        net_pnl = charges["net_pnl"]; total_charges = charges["total_charges"]
    except Exception:
        net_pnl = pnl_rs; total_charges = 0.0
    try:
        _row = {
            "date": date.today().isoformat(),
            "entry_time": entry_time, "exit_time": exit_time,
            "symbol": symbol, "direction": direction, "strike": strike,
            "entry_price": entry_price, "exit_price": exit_price,
            "pnl_pts": pnl_pts, "pnl_rs": pnl_rs,
            "gross_pnl_rs": pnl_rs, "net_pnl_rs": net_pnl,
            "peak_pnl": peak, "exit_reason": reason,
            "dte": dte_val, "candles_held": candles,
            "entry_mode": entry_mode, "spot_regime": spot_regime,
            "total_charges": total_charges, "num_exit_orders": 1,
            "qty_exited": qty, "lot_id": "ALL",
            "xleg_other_margin": xleg_margin,
            "entry_spot": entry_spot_val, "exit_spot": exit_spot,
            "pdh_prev": pdh_prev_val, "pdl_prev": pdl_prev_val,
            "entry_range_pos": range_pos_val,
        }
        import csv as _csv
        _new_file = not os.path.isfile(D.V13_TRADE_LOG_PATH)
        with open(D.V13_TRADE_LOG_PATH, "a", newline="") as f:
            w = _csv.DictWriter(f, fieldnames=TRADE_FIELDNAMES, extrasaction="ignore")
            if _new_file:
                w.writeheader()
            w.writerow(_row)
    except Exception as _le:
        logger.warning("[V13] Trade log write error: " + str(_le))

    logger.info(f"[V13] SHADOW EXIT: {symbol} qty={qty} ref={exit_price} "
                f"reason={reason} pnl={pnl_pts}pts")
    _tg_send(
        f"🧪 <b>V13 SHADOW EXIT {direction} {strike}</b>\n"
        f"<b>{reason}</b>  {'+' if pnl_pts >= 0 else ''}{pnl_pts:.1f} pts\n"
        f"Entry ₹{entry_price:.1f} → Exit ₹{exit_price:.1f}  Peak +{peak:.1f} ({tier})\n"
        f"vel2@entry "
        f"{('%+.1f' % _v13_state.get('vel2_at_entry')) if _v13_state.get('vel2_at_entry') is not None else 'n/a'}\n"
        f"DAY {'+' if _v13_state.get('_pnl_today_pts', 0) >= 0 else ''}"
        f"{_v13_state.get('_pnl_today_pts', 0):.1f} pts "
        f"({_v13_state.get('_wins_today', 0)}W {_v13_state.get('_losses_today', 0)}L)",
        priority="high")
    _save_v13_state()


def _v13_check_exit():
    """Tick-based exit for the V13 shadow position. Mirrors the V11 ladder
    (reuses _v11_compute_trail_sl). Runs every scan cycle, PAPER-ONLY."""
    with _v13_lock:
        if not _v13_state.get("in_trade"):
            return
        token            = int(_v13_state.get("token", 0) or 0)
        initial_sl       = float(_v13_state.get("initial_sl", 0.0))
        peak_ltp         = float(_v13_state.get("peak_ltp", 0.0))
        entry_price_snap = float(_v13_state.get("entry_price", 0.0))
        _cur_min = datetime.now().strftime("%H:%M")
        _new_candle = _cur_min != _v13_state.get("_last_minute", "")
        if _new_candle:
            _v13_state["_last_minute"] = _cur_min
            _v13_state["candles_held"] = _v13_state.get("candles_held", 0) + 1
    if _new_candle:
        _save_v13_state()

    if not token:
        return
    ltp = D.get_ltp(token)

    eod_str = CFG.exit_ema9_band("eod_exit_time", "15:20") if hasattr(CFG, "exit_ema9_band") else "15:20"
    try:
        _eh, _em = eod_str.split(":"); eod_mins = int(_eh) * 60 + int(_em)
    except Exception:
        eod_mins = 15 * 60 + 20
    now_mins = datetime.now().hour * 60 + datetime.now().minute

    if ltp <= 0:
        # No live tick — still force EOD close at entry price (mirror V11).
        if now_mins >= eod_mins:
            _v13_execute_paper_exit("EOD_EXIT", round(entry_price_snap, 2))
        return

    with _v13_lock:
        avg_entry = entry_price_snap
        if ltp > peak_ltp:
            peak_ltp = ltp
            _v13_state["peak_ltp"] = peak_ltp
            _v13_state["peak_pnl"] = round(peak_ltp - avg_entry, 2)
        peak_pnl = peak_ltp - avg_entry
        current_sl, tier = _v11_compute_trail_sl(avg_entry, peak_pnl, initial_sl)
        _v13_state["active_ratchet_tier"] = tier
        _v13_state["active_ratchet_sl"]   = round(current_sl, 2)

    if ltp <= current_sl:
        exit_reason = {"INITIAL": "EMERGENCY_SL", "PROTECT": "PROTECT_2",
                       "LOCK_4": "LOCK_4", "LOCK_25": "LOCK_25"}.get(tier, "VISHAL_TRAIL")
        _v13_execute_paper_exit(exit_reason, round(current_sl, 2))
        return

    if now_mins >= eod_mins:
        _v13_execute_paper_exit("EOD_EXIT", float(ltp))


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
    # V11 shadow daily counters
    _v11_state["_signals_today"]    = 0
    _v11_state["_last_signal_time"] = ""
    _v11_state["_last_fired_candle_ts"] = ""
    with _v11_lock:
        _v11_state["_sl_cooldown_skip_next"] = False  # BUG-FIX: clear stale ESL flag on new day
        # BUG-FIX: reset V11 per-day trade counters when the bot crosses midnight
        # without a restart. The restart path (_load_v11_state) already does this;
        # without it here, _v11_state keeps yesterday's counts while the dashboard
        # 'today' block (CSV-driven) shows 0 → state/dashboard/TG misalignment.
        _v11_state["_pnl_today_pts"]        = 0.0
        _v11_state["_trades_today"]         = 0
        _v11_state["_wins_today"]           = 0
        _v11_state["_losses_today"]         = 0
        _v11_state["_v11_both_rejected_ts"] = 0.0
        _v11_state["_last_trade_date"]      = today_str
    _save_v11_state()  # persist the cross-midnight reset so the on-disk file isn't stale
    # V13 shadow engine — same cross-midnight daily reset
    with _v13_lock:
        _v13_state["_pnl_today_pts"]   = 0.0
        _v13_state["_trades_today"]    = 0
        _v13_state["_wins_today"]      = 0
        _v13_state["_losses_today"]    = 0
        _v13_state["_last_fired_candle_ts"] = ""
        _v13_state["_sl_cooldown_skip_next"] = False
        _v13_state["_last_trade_date"] = today_str
    _save_v13_state()
    with _state_lock:
        state["daily_pnl"]             = 0.0
        state["_eod_reported"]         = False
        state["_eod_exited"]           = False
        state["paused"]                = False
        state["_bias_done"]            = False
        state["_hourly_rsi_ts"]        = 0
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
    # Market context at entry (analysis only — not a gate)
    "spot_regime",
    # PDH/PDL context at entry (analysis only — not a gate)
    "pdh_prev", "pdl_prev", "entry_range_pos",
    # Vishal Anti-Chase Filter (VAC) — own-leg 3-candle run at entry (analysis only)
    "own_m3_at_entry",
    # Entry-timing study: when did the trade first go positive and when did it break out?
    "first_profit_candle", "first_profit_ltp", "first_profit_ts",
    "breakout_candle", "breakout_ltp", "breakout_ts",
    "early_candles",   # breakout_candle - 1 = candles spent before real move
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
        "spot_regime":         st.get("spot_regime_at_entry", ""),
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
                      "Balance : ₹" + "{:,}".format(int(_acct.get("total_balance", 0))) + "\n")
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
        "<b>STRATEGY</b>  Vishal Clean v21\n"
        ""
        "V11 LIVE   : 1-min  | Golden | " + ("PAPER" if D.PAPER_MODE else "LIVE") + " trading\n"
        "Entry   : " + V11_OPEN_BLACKOUT_END.strftime("%H:%M") + " - " + CFG.entry_ema9_band("cutoff_after", "15:00") + " IST\n"
        "Size    : 1 lot fixed\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "<b>V11 GOLDEN GATES</b>\n"
        "1) MOMENTUM  dte≥2: close > EMA9H + 3.5 pts (hard gate)\n"
        "   dte0/1: close ≥ EMA9H + 2.3%/3.0% of premium\n"
        "2) OPP DECAY dte≥2: opp close − ema9l in [−9, −7]\n"
        "   dte0/1: (opp margin / opp close) in [−4.8%, −2.7%]\n"
        "Cooldown: 10:00 blackout · same-candle · same-side 3-min\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "<b>V11 SL LADDER</b>\n"
        "INITIAL    peak < 9    → max(ema9_low, entry − 10)\n"
        "PROTECT    peak ≥ 9    → max(initial, entry − 2)\n"
        "LOCK_4     peak ≥ 11   → max(initial, entry + 4)\n"
        "TRAIL_10   peak ≥ 15   → max(initial, entry + 9, peak − 10)\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "<b>EXITS</b>  initial_sl | LOCK_4 | TRAIL_10 | EOD " + CFG.exit_ema9_band("eod_exit_time", "15:15") + "\n"
        "/help for commands"
    )
    if not D.PAPER_MODE:
        # Live orders go through m.Stock — show m.Stock funds, not the Kite ledger
        _ms_funds = MSTOCK.ms_get_funds()
        if _ms_funds.get("ok"):
            _bal_line = ("Account: " + str(_ms_funds.get("name") or "MStock") + "\n"
                         "MStock Avail: ₹" + "{:,.0f}".format(_ms_funds.get("available", 0))
                         + " | Used: ₹" + "{:,.0f}".format(_ms_funds.get("used", 0)) + "\n")
        else:
            _bal_line = ("Account: " + str(_ms_funds.get("name") or "MStock") + "\n"
                         "MStock funds unavailable\n")
        _lot_count = CFG.get().get("lots", {}).get("count", 1)
        _tg_send(
            "🔴🔴🔴🔴🔴🔴🔴🔴🔴🔴\n"
            "⚡ <b>LIVE MODE — REAL MONEY</b> ⚡\n"
            "🔴🔴🔴🔴🔴🔴🔴🔴🔴🔴\n"
            + _bal_line +
            "Lots: " + str(_lot_count) + " × " + str(D.get_lot_size()) + " = " + str(_lot_count * D.get_lot_size()) + " qty\n"
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
        + ("+" if total_rs >= 0 else "-") + "₹" + "{:,}".format(abs(int(total_rs)))
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

        # Update option LTPs. V13 shares V11's locked strikes, so refresh its
        # gate-card price from the SAME LTP here — otherwise the V13 card stays
        # frozen at the last full rebuild (once/min) while V11 ticks every
        # ~5-10s, producing a same-strike price mismatch on the dashboard.
        _v13_dash = dash.get("v13", {})
        for side in ("CE", "PE"):
            sig = dash.get(side.lower(), {})
            oi = _locked_tokens.get(side) if _locked_tokens else None
            if oi:
                ltp = D.get_ltp(oi["token"])
                if ltp > 0:
                    sig["ltp"] = round(ltp, 2)
                    _v13_sig = _v13_dash.get(side.lower(), {})
                    if (_v13_sig
                            and int(_v13_sig.get("strike", 0) or 0) == int(sig.get("strike", 0) or 0)):
                        _v13_sig["price"] = round(ltp, 2)

        # Update V11 position if in trade (_v11_state — V7 state never has V11 trades)
        with _v11_lock:
            _v11_it   = _v11_state.get("in_trade", False)
            _v11_tk   = _v11_state.get("token", 0)
            _v11_ep   = _v11_state.get("entry_price", 0)
            _v11_pk   = _v11_state.get("peak_pnl", 0)
            _v11_sl   = _v11_state.get("active_ratchet_sl", 0)
            _v11_tier = _v11_state.get("active_ratchet_tier", "INITIAL")
            _v11_can  = _v11_state.get("candles_held", 0)
        if _v11_it and _v11_tk:
            opt_ltp = D.get_ltp(_v11_tk)
            if opt_ltp > 0:
                pos = dash.get("position", {})
                pos["ltp"]                = round(opt_ltp, 2)
                pos["pnl"]                = round(opt_ltp - _v11_ep, 1)
                pos["peak"]               = round(_v11_pk, 1)
                pos["entry"]              = round(_v11_ep, 2)
                pos["sl"]                 = round(_v11_sl, 2)
                pos["active_ratchet_tier"] = _v11_tier
                pos["candles"]            = _v11_can
        elif not _v11_it and dash.get("position", {}).get("in_trade"):
            dash["position"] = {"in_trade": False}

        dash["ts"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        dash["version"] = D.VERSION
        dash.setdefault("market", {})["market_open"] = D.is_market_open()
        # tick-flow read changes every second → refresh on the FAST path too
        _inject_flow_block(dash)

        tmp = dash_path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(dash, f, default=str)
        os.replace(tmp, dash_path)
    except Exception:
        pass


def _inject_flow_block(dash):
    """Embed the tick-flow study's live read into the dashboard JSON (secret
    collector; no-op if the module isn't loaded)."""
    if _tick_flow_mod is None:
        return
    try:
        dash["flow"] = _tick_flow_mod.flow_block()
    except Exception:
        pass


def _dashboard_set_paused(flag):
    """Flip today.paused in the dashboard JSON immediately. The today block
    is otherwise only rebuilt once per 1-min candle, so without this a
    pause/resume outside market hours shows stale until the next candle."""
    try:
        dash_path = os.path.join(D.STATE_DIR, 'vrl_dashboard.json')
        if not os.path.isfile(dash_path):
            return
        with open(dash_path) as f:
            dash = json.load(f)
        dash.setdefault("today", {})["paused"] = bool(flag)
        dash["ts"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
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


def _account_block():
    """Dashboard account section. Orders go via m.Stock, so show the m.Stock
    funds (cached 5-min); fall back to the Kite margins (data-feed account)
    only when m.Stock is unavailable, labelled so the source is obvious."""
    try:
        funds = MSTOCK.ms_get_funds()
        if funds.get("ok"):
            nm = funds.get("name", "") or "m.Stock"
            if funds.get("stale"):
                nm += " (m.Stock stale)"   # last-good; refresh is currently 502'ing
            return {
                "name": nm,
                "balance": round(funds.get("available", 0), 2),
                "used": round(funds.get("used", 0), 2),
            }
    except Exception:
        pass
    acct = D.get_account_info()
    return {
        "name": (acct.get("name", "") + " (Kite)").strip(),
        "balance": acct.get("total_balance", 0),
        "used": acct.get("used_margin", 0),
    }


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
        with _v11_lock:
            st_v10 = dict(_v11_state)

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


        def _build_signal(opt_type, result):
            _ltp_fallback = 0
            try:
                _tk = (dir_strikes or {}).get(opt_type, atm_strike)
                _ltp_fallback = D.get_ltp((_locked_tokens or {}).get(opt_type, {}).get("token", 0)) or 0
            except Exception:
                pass
            if not result:
                return {
                    "close": 0.0, "ema9_high": 0.0, "ema9_low": 0.0,
                    "fired": False,
                    "verdict": "MARKET CLOSED" if not D.is_market_open() else "WARMING UP",
                    "ltp": round(_ltp_fallback, 2),
                    "strike": dir_strikes.get(opt_type, atm_strike),
                    "momentum_gap": 0.0,
                    "momentum_ok": False,
                    "decay_margin": 0.0,
                    "decay_ok": False,
                }
            _fired = result.get("fired", False)
            _close = float(result.get("close", 0.0))
            _eh = float(result.get("ema9_high", 0.0))
            _el = float(result.get("ema9_low", 0.0))
            _reject = result.get("reject_reason", "")
            _momentum_gap = float(result.get("momentum_gap", 0.0))
            _momentum_ok = bool(result.get("momentum_ok", False))
            _decay_margin = float(result.get("decay_margin", 0.0))
            _decay_ok = bool(result.get("decay_ok", False))

            if _fired:
                verdict = "✅ READY"
            elif _reject:
                verdict = _reject
            else:
                _fails = []
                _ml = "ema9l" if D.strategy_version() == "v13" else "ema9h"
                if not _momentum_ok: _fails.append(f"below_{_ml}_gap({_momentum_gap:+.1f}<3.5)")
                if not _decay_ok:
                    _fails.append(f"opp_decay({_decay_margin:+.1f} not in [{V11_DECAY_LOW:.0f},{V11_DECAY_HIGH:.0f}])")
                verdict = _fails[0] if _fails else "scanning"

            _ltp_out = round(result.get("entry_price", 0.0) or _ltp_fallback, 2)
            if _ltp_out == 0: _ltp_out = round(_ltp_fallback, 2)

            return {
                "close": round(_close, 2),
                "ema9_high": round(_eh, 2),
                "ema9_low": round(_el, 2),
                "fired": _fired,
                "verdict": verdict,
                "ltp": _ltp_out,
                "strike": result.get("_strike", dir_strikes.get(opt_type, atm_strike)),
                "momentum_gap": _momentum_gap,
                "momentum_ok": _momentum_ok,
                "decay_margin": _decay_margin,
                "decay_ok": _decay_ok,
            }

        _is_warm, _w_done, _w_need, _w_eta = _warmup_info(now, dte)
        # Feed dashboard from the live v10 gate snapshot
        def _v11_to_result(side):
            """Convert _v11_live snapshot to a result dict that _build_signal understands."""
            if not D.is_market_open():
                return None
            with _v11_live_lock:
                lv = dict(_v11_live.get(side, {}))
            if not lv or lv.get("momentum_gap") is None:
                return None
            return {
                "close": float(lv.get("price", 0.0)),
                "entry_price": float(lv.get("price", 0.0)),
                "ema9_high": float(lv.get("ema9h", 0.0)),
                "ema9_low": float(lv.get("ema9l", 0.0)),
                "momentum_gap": float(lv.get("momentum_gap", 0.0)),
                "momentum_ok": bool(lv.get("momentum_ok", False)),
                "decay_margin": float(lv.get("decay_margin", 0.0)),
                "decay_ok": bool(lv.get("decay_ok", False)),
                "fired": bool(lv.get("ready")),
                "reject_reason": lv.get("reject", ""),
                "entry_mode": "V11_GOLDEN",
            }
        ce_signal = _build_signal("CE", _v11_to_result("CE"))
        pe_signal = _build_signal("PE", _v11_to_result("PE"))

        try:
            # First cycle after startup can pass atm_strike=0 (spot LTP cache
            # not warm yet) — a strike-0 lookup scans every NFO instrument,
            # finds nothing and logs "Token resolve incomplete".
            _tokens = D.get_option_tokens(None, atm_strike, expiry) if atm_strike else {}
            for _sig, _side in [(ce_signal, "CE"), (pe_signal, "PE")]:
                _live_tok = (_locked_tokens or {}).get(_side, _tokens.get(_side, {}))
                _ltp = D.get_ltp(_live_tok.get("token", 0)) if _live_tok else 0
                if _ltp and _ltp > 0:
                    _sig["ltp"] = round(_ltp, 2)
                    if D.is_market_open():
                        _sig["close"] = round(_ltp, 2)
                elif _sig.get("ltp", 0) == 0 and _side in _tokens:
                    _ltp = D.get_ltp(_tokens[_side]["token"])
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
                _stop_type = st_v10.get("active_ratchet_tier", "INITIAL")
            else:
                _stop_price = round(st_v10.get("initial_sl", entry - 12), 2)
                _stop_type = "INITIAL"

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
                "active_ratchet_tier": _stop_type,
                "lot_size": D.get_lot_size(),
                "qty": int(st_v10.get("qty", 0) or 0),
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
                _r = _p * D.get_lot_size()  # normalize: same direction as pnl_pts
                _today_pnl_pts += _p
                _today_pnl_rs += _r
                if _p > 0:
                    _today_wins += 1
                elif _p < 0:
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

        # ── V13 shadow engine snapshot (paper A/B alongside V11) ──
        try:
            with _v13_live_lock:
                _v13_ce = dict(_v13_live.get("CE", {}))
                _v13_pe = dict(_v13_live.get("PE", {}))
            # V13 shares V11's locked strikes/tick feed — when the strike
            # matches, reuse V11's freshly-fetched gate-card LTP so the two
            # cards never show a different price for the same strike (the V13
            # scanner snapshot lags ~3s and rounds to 1dp; V11's ltp is the
            # live, fast-path-refreshed value). Falls back to the V13 snapshot
            # price only if strikes diverge (shouldn't happen).
            for _v13_sig, _v11_sig in ((_v13_ce, ce_signal), (_v13_pe, pe_signal)):
                if (_v13_sig and _v11_sig
                        and int(_v13_sig.get("strike", 0) or 0) == int(_v11_sig.get("strike", 0) or 0)
                        and float(_v11_sig.get("ltp", 0) or 0) > 0):
                    _v13_sig["price"] = round(float(_v11_sig["ltp"]), 2)
            with _v13_lock:
                _v13_st = dict(_v13_state)
            if _v13_st.get("in_trade"):
                _v13_tok = _v13_st.get("token", 0)
                _v13_ltp = D.get_ltp(_v13_tok)
                _v13_entry = float(_v13_st.get("entry_price", 0))
                _v13_pos = {
                    "in_trade": True,
                    "symbol": _v13_st.get("symbol", ""),
                    "direction": _v13_st.get("direction", ""),
                    "strike": _v13_st.get("strike", 0),
                    "entry": _v13_entry,
                    "entry_time": _v13_st.get("entry_time", ""),
                    "ltp": round(_v13_ltp, 2) if _v13_ltp > 0 else 0,
                    "pnl": round(_v13_ltp - _v13_entry, 1) if _v13_ltp > 0 else 0,
                    "peak": round(_v13_st.get("peak_pnl", 0), 1),
                    "candles": _v13_st.get("candles_held", 0),
                    "sl": round(float(_v13_st.get("active_ratchet_sl", 0) or 0), 2),
                    "active_ratchet_tier": _v13_st.get("active_ratchet_tier", "INITIAL"),
                    "qty": int(_v13_st.get("qty", 0) or 0),
                    "vel2_at_entry": _v13_st.get("vel2_at_entry"),
                }
            else:
                _v13_pos = {"in_trade": False}
            v13_block = {
                "ce": _v13_ce, "pe": _v13_pe, "position": _v13_pos,
                "today": {
                    "pnl": round(float(_v13_st.get("_pnl_today_pts", 0)), 1),
                    "trades": int(_v13_st.get("_trades_today", 0)),
                    "wins": int(_v13_st.get("_wins_today", 0)),
                    "losses": int(_v13_st.get("_losses_today", 0)),
                },
            }
        except Exception:
            v13_block = {"ce": {}, "pe": {}, "position": {"in_trade": False},
                         "today": {"pnl": 0, "trades": 0, "wins": 0, "losses": 0}}

        dashboard = {
            "ts": now.strftime("%Y-%m-%d %H:%M:%S"),
            "version": D.VERSION,
            "mode": "PAPER" if D.PAPER_MODE else "LIVE",
            "gate": D.strategy_version().upper(),
            "data_provider": D.data_provider(),
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
            "v13": v13_block,
            "position": position,
            "today": today_block,
            "account": _account_block(),
            "rolling": rolling_block,
            "cooldown": {},
        }
        _inject_flow_block(dashboard)

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

    if expiry:
        logger.info("[MAIN] Expiry on startup: " + str(expiry))
    else:
        logger.warning("[MAIN] Expiry not resolved on startup — will retry in loop")

    _last_health_log_ts = time.time()   # startup health already logged; first loop re-log in 30 min

    # Write a fresh dashboard at startup so stale/old-format JSON is never served
    try:
        _startup_spot = D.get_ltp(D.NIFTY_SPOT_TOKEN)
        _startup_dte  = D.calculate_dte(expiry) if expiry else 0
        _startup_vix  = D.get_vix()
        _write_dashboard(_startup_spot, 0, _startup_dte, _startup_vix,
                         "STARTUP", {}, {}, expiry, datetime.now())
        logger.info("[MAIN] Dashboard initialised at startup")
    except Exception as _dse:
        logger.debug("[MAIN] Startup dashboard write: " + str(_dse))

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
                    _save_state()
                    _dashboard_set_paused(True)
                    time.sleep(60)
                    continue
            dte     = D.calculate_dte(expiry) if expiry else 0
            # Keep _v11_state expiry/dte in sync — entry/exit functions read from here
            try:
                with _v11_lock:
                    _v11_state["expiry"] = expiry.isoformat() if expiry else ""
                    _v11_state["dte"]    = dte
                with _v13_lock:
                    _v13_state["dte"]    = dte
            except Exception:
                pass
            profile = {"conv_sl_pts": 12}
            session = D.get_session_block(now.hour, now.minute)
            spot_ltp = D.get_ltp(D.NIFTY_SPOT_TOKEN)

            D.check_and_reconnect()

            # ── V11 tick-based exit: runs every 1-second scan cycle ──
            # Must be OUTSIDE the _is_new_1min_candle gate — exits need to
            # fire on every tick, not once per minute at candle close.
            _v11_check_exit()
            # V13 shadow engine exit (paper-only, independent position)
            _v13_check_exit()

            # ── V11 entry: scan every 10 seconds (outside 1-min gate) ──
            # BUG-16 fix: entry was gated to once-per-minute at :35s.
            # If candle turned green at :40s, bot missed it until next minute.
            # Now checks every 10s — same_candle_guard prevents double-entry.
            global _v11_last_entry_scan_ts
            _v11_force_exit_age = time.time() - float(_v11_state.get("_force_exit_ts", 0) or 0)
            _v11_in_force_cooldown = (_v11_force_exit_age < 180 and float(_v11_state.get("_force_exit_ts", 0) or 0) > 0)
            if (_v11_in_force_cooldown
                    and not _v11_state.get("in_trade")
                    and time.time() - _v11_last_entry_scan_ts >= 3):
                _v11_last_entry_scan_ts = time.time()
                logger.info(f"[REJECT-V11] force_exit_cooldown age={int(_v11_force_exit_age)}s — entries blocked 3 min after manual exit")
            # (legacy entry scan removed — V11 Golden scanner drives all entries now)


            # ── V11 Golden Strategy Scanner ──
            # Runs even when in_trade so _v11_live stays warm with live EMA9 data
            # for the dashboard. The inner _in_trade check prevents any entry from
            # firing (sets reject_reason="in_trade", _ready_to_fire=False).
            global _v11_scanner_last_ts
            # Scanner window = UNION of V11's (is_trading_window, 09:15-14:30) and
            # V13's wider shadow window (09:30-15:15). V11 is re-gated to its own
            # window inside its block, so widening here only lets V13 fire later.
            _scan_window = D.is_trading_window(now) or (
                D.is_market_open()
                and V13_OPEN_BLACKOUT_END <= now.time() < V13_ENTRY_CUTOFF)
            if (_scan_window
                    and _locked_tokens
                    and time.time() - _v11_scanner_last_ts >= 3):
                _v11_scanner_last_ts = time.time()
                try:
                    for _sh_dir, _sh_info in [("CE", (_locked_tokens or {}).get("CE", {})),
                                               ("PE", (_locked_tokens or {}).get("PE", {}))]:
                        _sh_tok = int(_sh_info.get("token", 0) or 0)
                        if not _sh_tok:
                            continue

                        # 1-min main option data
                        _sh_1m = get_option_1min(_sh_tok, 100)
                        if _sh_1m is None or len(_sh_1m) < 4:
                            continue
                        _sh_1m_comp   = _sh_1m.iloc[-2]
                        _sh_1m_bk_ts  = str(_sh_1m_comp.name)
                        _sh_1m_close  = float(_sh_1m_comp["close"])
                        _sh_1m_open   = float(_sh_1m_comp["open"])
                        _sh_ema9h_1m  = float(_sh_1m_comp.get("ema9_high", 0))
                        _sh_ema9l_1m  = float(_sh_1m_comp.get("ema9_low", 0))
                        # Vishal Anti-Chase Filter (VAC) input — own-leg 3-candle run
                        # into the entry candle (close[-2] - close[-5]). Analysis only.
                        _sh_1m_m3 = (round(_sh_1m_close - float(_sh_1m.iloc[-5]["close"]), 2)
                                     if len(_sh_1m) >= 5 else 0.0)

                        # Opposite option data
                        _opp_dir = "PE" if _sh_dir == "CE" else "CE"
                        _opp_info = (_locked_tokens or {}).get(_opp_dir, {})
                        _opp_tok = int(_opp_info.get("token", 0) or 0)
                        _opp_1m = get_option_1min(_opp_tok, 10) if _opp_tok else None

                        _opp_close = 0.0
                        _opp_ema9l = 0.0
                        _opp_ema9h = 0.0
                        _opp_margin = 0.0
                        _opp_margin_high = 0.0
                        if _opp_1m is not None and len(_opp_1m) >= 2:
                            _opp_comp = _opp_1m.iloc[-2]
                            _opp_close = float(_opp_comp["close"])
                            _opp_ema9l = float(_opp_comp.get("ema9_low", 0))
                            _opp_ema9h = float(_opp_comp.get("ema9_high", 0))
                            _opp_margin = round(_opp_close - _opp_ema9l, 2)
                            _opp_margin_high = round(_opp_close - _opp_ema9h, 2)

                        # ═══════════════════════════════════════════════
                        #  TWO ENGINES SCAN THE SAME CANDLE, EACH PAPER,
                        #  EACH WITH ITS OWN POSITION/COOLDOWNS (owner
                        #  2026-06-20: live A/B, both on the dashboard).
                        #    V11 = primary gate (per-DTE, off ema9_high /
                        #          ema9_low). V13 = shadow gate (absolute
                        #          all-dte, off ema9_low / ema9_high).
                        # ═══════════════════════════════════════════════

                        # ── V11 ENGINE (primary) ───────────────────────
                        _momentum_ok, _decay_ok = _v11_gate_check(
                            dte, _sh_1m_close, _sh_ema9h_1m,
                            _opp_margin, _opp_close, _opp_ema9l)
                        _disp_mom_gap = round(_sh_1m_close - _sh_ema9h_1m, 2)
                        _disp_decay_margin = _opp_margin

                        # Cooldown and basic guards
                        # Treat an in-flight live entry (broker order placed, ~8s
                        # blocking, in_trade not yet set) as in_trade so this 3s tick
                        # can't fire a SECOND live order — the double-lot fix.
                        _in_trade = bool(_v11_state.get("in_trade", False)) or bool(_v11_state.get("_entry_in_progress", False))
                        _in_cooldown = False
                        _cooldown_reason = ""

                        if not D.is_trading_window(now):
                            # V11 keeps its proven window even though the scanner
                            # now runs later for V13. Behavior unchanged for V11.
                            _in_cooldown = True
                            _cooldown_reason = "outside_window"
                        elif now.time() < V11_OPEN_BLACKOUT_END:
                            _in_cooldown = True
                            _cooldown_reason = "open_blackout"
                        elif _v11_state.get("_sl_cooldown_skip_next"):
                            _in_cooldown = True
                            _cooldown_reason = "sl_cooldown"
                            _v11_state["_sl_cooldown_skip_next"] = False  # consume: blocks one scan, then exit_candle_cooldown takes over
                        elif _v11_state.get("_last_fired_candle_ts") == _sh_1m_bk_ts:
                            _in_cooldown = True
                            _cooldown_reason = "same_candle"
                        elif _v11_state.get("_last_exit_candle_ts") == _sh_1m_bk_ts:
                            _in_cooldown = True
                            _cooldown_reason = "exit_candle_cooldown"
                        elif (_v11_state.get("_last_exit_direction_v10") == _sh_dir
                              and time.time() - float(_v11_state.get("_last_exit_time_unix", 0)) < 180):
                            _in_cooldown = True
                            _remaining = int(180 - (time.time() - float(_v11_state.get("_last_exit_time_unix", 0))))
                            _cooldown_reason = f"same_side_3min({_remaining}s)"

                        # Update reject reasons
                        _reject_reason = ""
                        if _in_trade:
                            _reject_reason = "in_trade"
                        elif _in_cooldown:
                            _reject_reason = _cooldown_reason
                        elif not _momentum_ok:
                            if int(dte or 0) in V11_PCT_GATE_DTE:
                                _mp = V11_PCT_GATE_DTE[int(dte or 0)]["mom_pct"]
                                _reject_reason = f"below_mom_pct(dte{dte} gap{_disp_mom_gap:+.2f}<{_mp*_sh_1m_close:.1f})"
                            else:
                                _reject_reason = f"below_ema9h_gap({_disp_mom_gap:+.2f}<{V11_MIN_EMA9H_GAP})"
                        elif not _decay_ok:
                            if int(dte or 0) in V11_PCT_GATE_DTE and _opp_close > 0:
                                _cfg = V11_PCT_GATE_DTE[int(dte or 0)]
                                _reject_reason = f"decay_pct_weak(dte{dte} {_opp_margin/_opp_close*100:+.1f}% not in [{_cfg['decay_lo']*100:.1f},{_cfg['decay_hi']*100:.1f}])"
                            else:
                                _reject_reason = f"opp_decay_weak({_opp_margin:+.1f} not in [{V11_DECAY_LOW:.0f},{V11_DECAY_HIGH:.0f}])"

                        _decay_gate = _decay_ok
                        _ready_to_fire = (_momentum_ok and _decay_gate and not _in_trade and not _in_cooldown)

                        with _v11_live_lock:
                            _v11_live[_sh_dir] = {
                                "strike": int(_sh_info.get("strike", 0) or 0),
                                "price": round(D.get_ltp(_sh_tok) or _sh_1m_close, 1),
                                "ema9h": round(_sh_ema9h_1m, 2),
                                "ema9l": round(_sh_ema9l_1m, 2),
                                "momentum_gap": _disp_mom_gap,
                                "momentum_ok": _momentum_ok,
                                "decay_margin": _disp_decay_margin,
                                "decay_ok": _decay_ok,
                                "ready": _ready_to_fire,
                                "reject": _reject_reason,
                            }

                        if _ready_to_fire and V11_LIVE:
                            try:
                                _sh_spot_now = D.get_ltp(D.NIFTY_SPOT_TOKEN)
                                _sh_nbr_otm = D.get_ltp(dir_tokens.get("CE_UP" if _sh_dir == "CE" else "PE_DN", {}).get("token", 0) or 0)
                                _sh_nbr_itm = D.get_ltp(dir_tokens.get("CE_DN" if _sh_dir == "CE" else "PE_UP", {}).get("token", 0) or 0)
                                _v11_execute_paper_entry(
                                    direction=_sh_dir,
                                    strike=int(_sh_info.get("strike", 0) or 0),
                                    symbol=_sh_info.get("symbol", ""),
                                    token=_sh_tok,
                                    entry_price=_sh_1m_close,
                                    entry_result={
                                        "entry_price": _sh_1m_close,
                                        "entry_mode": f"V11_{_sh_dir}",
                                        "fired_candle_ts": _sh_1m_bk_ts,
                                        "close": _sh_1m_close,
                                        "open": _sh_1m_open,
                                        "ema9_low": _sh_ema9l_1m,
                                        "ema9_high": _sh_ema9h_1m,
                                        "xleg_other_margin": _disp_decay_margin,
                                        "spot_regime": spot_3m.get("regime", "") if isinstance(spot_3m, dict) else "",
                                        "own_m3": _sh_1m_m3,
                                    },
                                    other_token=_opp_tok,
                                    spot_at_entry=_sh_spot_now,
                                    neighbor_ltp_otm=_sh_nbr_otm,
                                    neighbor_ltp_itm=_sh_nbr_itm,
                                )
                            except Exception as _e_fire:
                                logger.error(f"[V11] Error firing entry: {_e_fire}")

                        # ── V13 ENGINE (shadow, paper-only) ────────────
                        try:
                            _v13_mom_ok, _v13_decay_ok = _v13_gate_check(
                                _sh_1m_close, _sh_ema9l_1m,
                                _opp_margin_high, _opp_ema9h)
                            _v13_mom_gap = round(_sh_1m_close - _sh_ema9l_1m, 2)
                            _v13_in_trade = bool(_v13_state.get("in_trade", False))
                            _v13_cd = False
                            _v13_cd_reason = ""
                            if now.time() < V13_OPEN_BLACKOUT_END or now.time() >= V13_ENTRY_CUTOFF:
                                _v13_cd = True; _v13_cd_reason = "outside_v13_window"
                            elif _v13_state.get("_sl_cooldown_skip_next"):
                                _v13_cd = True; _v13_cd_reason = "sl_cooldown"
                                _v13_state["_sl_cooldown_skip_next"] = False
                            elif _v13_state.get("_last_fired_candle_ts") == _sh_1m_bk_ts:
                                _v13_cd = True; _v13_cd_reason = "same_candle"
                            elif _v13_state.get("_last_exit_candle_ts") == _sh_1m_bk_ts:
                                _v13_cd = True; _v13_cd_reason = "exit_candle_cooldown"
                            elif (_v13_state.get("_last_exit_direction") == _sh_dir
                                  and time.time() - float(_v13_state.get("_last_exit_time_unix", 0)) < 180):
                                _v13_cd = True
                                _v13_rem = int(180 - (time.time() - float(_v13_state.get("_last_exit_time_unix", 0))))
                                _v13_cd_reason = f"same_side_3min({_v13_rem}s)"
                            # vel2 HARD GATE (owner 2026-06-24, "we are confident now"):
                            # the fast 2-min futures slope (signed in trade direction) must
                            # be > 0 to fire. The 06-24 A/B showed every V13 loser carrying
                            # vel2 data had vel2<=0 while the lone winner had vel2>0. Reads the
                            # live signed slope from the in-process tick_flow collector.
                            # FAIL-OPEN: when tick_flow is off or <3 completed futures minutes
                            # exist (vel2 is None) we allow the fire, so a feed gap never freezes
                            # V13 — the gate only blocks on a confirmed non-positive slope.
                            _v13_vel2 = None
                            try:
                                if _tick_flow_mod is not None:
                                    _v13_vel2 = _tick_flow_mod.fut_vel2(_sh_dir)
                            except Exception:
                                _v13_vel2 = None
                            _v13_vel2_ok = (_v13_vel2 is None) or (_v13_vel2 > 0)
                            _v13_reject = ""
                            if _v13_in_trade:
                                _v13_reject = "in_trade"
                            elif _v13_cd:
                                _v13_reject = _v13_cd_reason
                            elif not _v13_mom_ok:
                                _v13_reject = f"below_ema9l_gap({_v13_mom_gap:+.2f}<{V11_MIN_EMA9H_GAP})"
                            elif not _v13_decay_ok:
                                _v13_reject = f"opp_decay_high_weak({_opp_margin_high:+.1f} not in [{V11_DECAY_LOW:.0f},{V11_DECAY_HIGH:.0f}])"
                            elif not _v13_vel2_ok:
                                _v13_reject = f"vel2<=0({_v13_vel2:+.2f})"
                            _v13_ready = (_v13_mom_ok and _v13_decay_ok and _v13_vel2_ok
                                          and not _v13_in_trade and not _v13_cd)
                            with _v13_live_lock:
                                _v13_live[_sh_dir] = {
                                    "strike": int(_sh_info.get("strike", 0) or 0),
                                    "price": round(D.get_ltp(_sh_tok) or _sh_1m_close, 1),
                                    "ema9h": round(_sh_ema9h_1m, 2),
                                    "ema9l": round(_sh_ema9l_1m, 2),
                                    "momentum_gap": _v13_mom_gap,
                                    "momentum_ok": _v13_mom_ok,
                                    "decay_margin": round(_opp_margin_high, 2),
                                    "decay_ok": _v13_decay_ok,
                                    "vel2": (round(_v13_vel2, 2) if _v13_vel2 is not None else None),
                                    "vel2_ok": _v13_vel2_ok,
                                    "ready": _v13_ready,
                                    "reject": _v13_reject,
                                }
                            if _v13_ready and V11_LIVE:
                                _v13_spot_now = D.get_ltp(D.NIFTY_SPOT_TOKEN)
                                _v13_execute_paper_entry(
                                    direction=_sh_dir,
                                    strike=int(_sh_info.get("strike", 0) or 0),
                                    symbol=_sh_info.get("symbol", ""),
                                    token=_sh_tok,
                                    entry_price=_sh_1m_close,
                                    entry_mode=f"V13_{_sh_dir}",
                                    other_token=_opp_tok,
                                    opp_margin_high=_opp_margin_high,
                                    spot_at_entry=_v13_spot_now,
                                    fired_candle_ts=_sh_1m_bk_ts,
                                    ema9_low=_sh_ema9l_1m,
                                    dte=dte,
                                    vel2_at_entry=_v13_vel2,
                                )
                        except Exception as _v13_e:
                            logger.warning(f"[V13 Scanner] error: {_v13_e}")
                except Exception as _scanner_e:
                    logger.warning(f"[V11 Scanner] error: {_scanner_e}")

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

            with _state_lock:
                _eod_done = state.get("_eod_reported")
            if now.hour == 15 and now.minute >= 25:
                try:
                    _saved_via = ""
                    _safe_spot = D.get_ltp(D.NIFTY_SPOT_TOKEN)
                    if _safe_spot > 0:
                        _saved_via = "WS"
                    else:
                        _safe_spot = D.get_spot_ltp()   # Upstox REST fallback
                        if _safe_spot > 0:
                            _saved_via = "REST"
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
                    if _eod_spot <= 0:
                        _eod_spot = D.get_spot_ltp()    # Upstox REST fallback
                        if _eod_spot > 0:
                            logger.info("[MAIN] EOD spot via REST: " + str(_eod_spot))
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

            if (now.hour > 15 or (now.hour == 15 and now.minute >= 30)):
                if not state.get("_eod_exited"):
                    with _state_lock:
                        state["_eod_exited"] = True
                    logger.info("[MAIN] _eod_exited=True at "
                                + now.strftime("%H:%M:%S")
                                + " (no trade open → flag-only)")


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
                    # v22 ITM-100 scheme: CE is locked to the floor-100 of spot, so
                    # spot lives in [_locked_ce_strike, _locked_ce_strike+100).
                    # Relock only when spot crosses into a NEW 100-band, with a 15-pt
                    # hysteresis past each boundary (lower edge = locked, upper = locked+100)
                    # so it does not flap tick-by-tick right at a round strike.
                    _target_ce = (int(spot_ltp) // V11_STRIKE_STEP) * V11_STRIKE_STEP
                    if (_target_ce != _locked_ce_strike
                            and (spot_ltp >= _locked_ce_strike + V11_STRIKE_STEP + 15
                                 or spot_ltp <= _locked_ce_strike - 15)):
                        _relock = True
                        _spot_move = round(spot_ltp - _locked_at_spot, 1)
                        _old_ce = _locked_ce_strike
                        _old_pe = _locked_pe_strike
                        logger.info("[MAIN] ATM band cross past hysteresis: locked CE="
                                    + str(_locked_ce_strike) + " target="
                                    + str(_target_ce) + " spot="
                                    + str(round(spot_ltp, 1))
                                    + " — RELOCKING (neighbor pre-warmed)")

                if _relock:
                    _lock_strikes(spot_ltp, dte, kite, expiry)

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

                # V7 15-min check_entry scan removed — V11 Golden handles all entries
                # V11 entry is handled above in the 10-second scan (outside 1-min gate)

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
                        "in_trade": bool(_v11_state.get("in_trade")),
                        "direction": _v11_state.get("direction", ""),
                        "entry_price": _v11_state.get("entry_price", 0),
                        "peak_pnl": _v11_state.get("peak_pnl", 0),
                        "trades_today": _v11_state.get("_trades_today", 0),
                        "pnl_today_pts": _v11_state.get("_pnl_today_pts", 0.0),
                        "losses_today": _v11_state.get("_losses_today", 0),
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

        _v11_in_trade = _v11_state.get("in_trade", False)
        _v11_pos_str = ""
        if _v11_in_trade:
            _v11_ep  = float(_v11_state.get("entry_price", 0) or 0)
            _v11_tok = int(_v11_state.get("token", 0) or 0)
            _v11_ltp = D.get_ltp(_v11_tok) if _v11_tok else 0
            _v11_pn  = round(_v11_ltp - _v11_ep, 1) if _v11_ltp else 0
            _v11_pk  = float(_v11_state.get("peak_pnl", 0) or 0)
            _v11_tier = _v11_state.get("active_ratchet_tier", "INITIAL") or "INITIAL"
            _v11_sl  = float(_v11_state.get("active_ratchet_sl", 0) or 0)
            if _v11_sl <= 0: _v11_sl = float(_v11_state.get("initial_sl", 0) or round(_v11_ep - 12, 2))
            _v11_lock = round(_v11_sl - _v11_ep, 1)
            _v11_room = round(_v11_ltp - _v11_sl, 1) if _v11_ltp else 0
            _v11_dir_emj = "🟢" if _v11_state.get("direction") == "CE" else "🔴"
            _v11_sym = _v11_state.get("direction", "") + " " + str(_v11_state.get("strike", ""))
            _v11_pos_str = (
                "[V11] " + _v11_dir_emj + " " + _v11_sym + "  "
                + ("+" if _v11_pn >= 0 else "") + str(_v11_pn) + "pts\n"
                + "Entry ₹" + str(_v11_ep) + " → ₹" + str(round(_v11_ltp, 2))
                + " · Peak +" + str(_v11_pk) + "\n"
                + "Tier: " + _v11_tier + " @ ₹" + str(round(_v11_sl, 2))
                + " (Lock " + ("+" if _v11_lock >= 0 else "") + str(_v11_lock)
                + " · Room " + ("+" if _v11_room >= 0 else "") + str(_v11_room) + ")"
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
            + " · " + str(_lot) + " × 1 lot\n"
            + _market_icon + " market " + _market_str + "\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "<b>DATA</b>\n"
            + _ok(_user != "?") + " token: " + str(_user) + "\n"
            + _tick_icon + " spot tick: " + _tick_str + "\n"
            + _ok(_lot > 0) + " lot size: " + str(_lot) + "\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "<b>TODAY</b>\n"
            + "🕐 V11: " + str(len(_trades_today)) + " trades · "
            + str(_td_wins) + "W " + str(_td_loss) + "L · "
            + ("+" if _td_pnl >= 0 else "") + "{:.1f}".format(_td_pnl) + " pts\n"
            + "⚡ V11: "
            + str(_v11_state.get("_trades_today", 0)) + " trades · "
            + str(_v11_state.get("_wins_today", 0)) + "W "
            + str(_v11_state.get("_losses_today", 0)) + "L · "
            + ("+" if _v11_state.get("_pnl_today_pts", 0) >= 0 else "")
            + "{:.1f}".format(_v11_state.get("_pnl_today_pts", 0)) + " pts"
            + (" | V11 active: " + str(_v11_state.get("direction", "")) + " "
               + str(_v11_state.get("strike", ""))
               + " peak +" + "{:.1f}".format(_v11_state.get("peak_pnl", 0))
               if _v11_state.get("in_trade") else "")
            + "\n"
            + ("Last: " + str(_last_t.get("entry_time", "?")) + " "
               + str(_last_t.get("direction", "?")) + " "
               + str(_last_t.get("strike", "?")) + " "
               + ("+" if float(_last_t.get("pnl_pts", 0) or 0) >= 0 else "")
               + str(_last_t.get("pnl_pts", "?")) + " ("
               + str(_last_t.get("exit_reason", "?")) + ")\n" if _last_t else "")
            + "━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "<b>POSITION</b>\n"
            + (_v11_pos_str + "\n" if _v11_in_trade else "—\n")
            + "━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "<b>ENGINE</b>\n"
            + ("Locked: CE " + str(_ce_lck) + " · PE " + str(_pe_lck) + "\n"
               + "Last scan: " + str(_last_scan) + "\n"
               + "Bias: " + str(state.get("daily_bias", "?")) + "\n"
               if _market else "💤 awaiting market open\n")
            + "━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "<b>V11 GOLDEN CONFIG (1-min)</b>\n"
            "Gate 1: MOMENTUM  close > EMA9H + 3.5 pts (hard gate)\n"
            "Gate 2: OPP DECAY opp margin [−8, −6] (all day)\n"
            "Entry:  single lot, market fill at candle close\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "<b>V11 SL LADDER</b>\n"
            "INITIAL    peak<9    max(ema9_low, entry−10)\n"
            "PROTECT    peak≥9    max(initial, entry−2)\n"
            "LOCK_4     peak≥11   max(initial, entry+4)\n"
            "TRAIL_10   peak≥15   max(initial, entry+9, peak−10)\n"
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
        "VISHAL RAJPUT TRADE v21 — V11 live 1-min P1+P2, "
        "exit chain (Emergency SL / EOD " + CFG.exit_ema9_band("eod_exit_time", "15:15") + " / Vishal Trail), "
        + ("PAPER" if D.PAPER_MODE else "LIVE") + " 1 lot.\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "🌐 Dashboard: http://" + _WEB_IP + ":8080"
    )


def _cmd_status(args):
    global _kite
    # V11 is the live strategy — read _v11_state, not V7 state
    with _v11_lock:
        st = dict(_v11_state)
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
            "Bot     : V11 scanning"
        )
        return

    qty   = int(st.get("qty", 0) or D.get_lot_size() * 2)
    token = int(st.get("token", 0) or 0)
    ltp   = 0.0
    try:
        ltp = D.get_ltp(token)
        if ltp <= 0 and token:
            df = D.get_option_1min(token, 3)   # Upstox REST fallback
            if df is not None and len(df):
                ltp = float(df.iloc[-1]["close"])
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
    _isl  = float(st.get("initial_sl", 0) or round(entry - 12, 1))
    if _tier and _tier not in ("", "None", "INITIAL") and _rsl > 0:
        _stop_line = "SL     : " + _tier + " @ ₹" + str(round(_rsl, 1))
        _stop_dist = round(ltp - _rsl, 1) if ltp > 0 else "—"
    else:
        _stop_line = "SL     : INITIAL @ ₹" + str(round(_isl, 1))
        _stop_dist = round(ltp - _isl, 1) if ltp > 0 else "—"

    _day_pts = round(st.get("_pnl_today_pts", 0), 1)
    _day_w   = st.get("_wins_today", 0)
    _day_l   = st.get("_losses_today", 0)

    _tg_send(
        "📊 <b>STATUS — IN TRADE</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "Time   : " + _now_str() + "\n"
        "Symbol : " + st.get("symbol", "") + "\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "Entry  : ₹" + str(round(entry, 2)) + " × " + str(qty) + "\n"
        "LTP    : ₹" + str(round(ltp, 2)) + "\n"
        "PNL    : " + ("+" if pnl >= 0 else "") + str(pnl) + "pts  " + pnl_rs_str + "\n"
        "Peak   : +" + str(round(peak, 1)) + "pts\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        + _stop_line + "  (" + str(_stop_dist) + "pts away)\n"
        "Ladder : +9→PROTECT  +11→LOCK_4  +15→TRAIL_10(entry+9/peak−10)\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        + _post_exit_line +
        "Day PNL: " + ("+" if _day_pts >= 0 else "") + str(_day_pts) + "pts  "
        + str(_day_w) + "W " + str(_day_l) + "L"
    )


def _cmd_account(args):
    try:
        _acct = D.get_account_info()
    except Exception:
        _acct = D.get_account_info()

    if not _acct.get("name"):
        _tg_send("Account info not available. Bot may not have fetched it yet.")
        return

    _ms_block = ""
    try:
        _msf = MSTOCK.ms_get_funds(max_age_secs=0)  # user asked — fetch fresh
        if _msf.get("ok"):
            _ms_block = (
                "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                "💰 <b>m.Stock (orders)</b>" + ((" — " + _msf["name"]) if _msf.get("name") else "") + "\n"
                "Available: ₹" + "{:,}".format(int(_msf.get("available", 0))) + "\n"
                "Used     : ₹" + "{:,}".format(int(_msf.get("used", 0))) + "\n"
            )
    except Exception:
        pass

    _tg_send(
        "👤 <b>ACCOUNT</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        + _ms_block +
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "📡 <b>Kite (data)</b> — " + _acct.get("name", "") + "\n"
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
    _save_state()
    _dashboard_set_paused(True)
    _tg_send("⏸ Paused. No new entries.")
    logger.info("[CTRL] Paused")


def _cmd_resume(args):
    with _state_lock:
        state["paused"] = False
    _save_state()
    _dashboard_set_paused(False)
    _tg_send("▶️ Resumed.")
    logger.info("[CTRL] Resumed")


def _cmd_forceexit(args):
    v7_open = False
    v11_open = False
    with _state_lock:
        if state.get("in_trade"):
            state["force_exit"] = True
            v7_open = True
    _v11_tok = 0
    _v11_entry_px = 0.0
    with _v11_lock:
        if _v11_state.get("in_trade"):
            v11_open = True
            _v11_tok = int(_v11_state.get("token", 0) or 0)
            _v11_entry_px = float(_v11_state.get("entry_price", 0) or 0)
            _v11_state["_force_exit_ts"] = time.time()  # BUG-C fix: arm 3-min re-entry cooldown

    if not v7_open and not v11_open:
        _tg_send("No open trade.")
        return
    if v7_open or v11_open:
        _tg_send("🚨 Force exit triggered.")
        logger.warning("[CTRL] Force exit")
    if v11_open:
        _ltp = D.get_ltp(_v11_tok) if _v11_tok else 0
        if _ltp <= 0:
            _ltp = _v11_entry_px
        _v11_execute_paper_exit("FORCE_EXIT", round(_ltp, 2))


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

    # Live/paper V11 trades from CSV
    for t in live_trades:
        pts  = float(t.get("pnl_pts", 0))
        total += pts
        sign = "+" if pts >= 0 else ""
        icon = "✅" if pts >= 0 else "❌"
        peak = float(t.get("peak_pnl", 0))
        captured = round(pts / peak * 100) if peak > 0 else 0
        lines += (
            icon + " <b>V11 Trade " + str(idx) + "</b>  " + t.get("direction", "") + "\n"
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

    # First attempt failed — retry with exponential backoff (3 attempts total).
    # Before each retry, re-check the PREVIOUS sell order: a MARKET order can fill
    # late, and re-selling on top of a fill creates a naked short. Only place
    # another sell once the prior order is confirmed not traded.
    _prev_order_id = result.get("order_id", "")
    for _retry in range(2, 4):
        _wait = 2 ** (_retry - 1)  # 2s, 4s
        logger.warning(f"[TRADE] Exit attempt {_retry-1} failed — retry {_retry} in {_wait}s")
        time.sleep(_wait)
        if _prev_order_id:
            _prev = MSTOCK._ms_lookup_order(mc, _prev_order_id)
            _pstatus = str(_prev.get("status", "")).lower()
            if _pstatus in MSTOCK._FILLED_STATUSES and int(_prev.get("filled_quantity", 0) or 0) > 0:
                _fp = float(_prev.get("average_price", 0) or 0)
                logger.warning(f"[TRADE] Prior exit order filled late — adopting, no re-sell: {_prev_order_id}")
                return {"ok": True, "fill_price": _fp,
                        "fill_qty": int(_prev.get("filled_quantity", 0) or 0),
                        "order_id": _prev_order_id, "error": "",
                        "slippage": round(exit_price_ref - _fp, 2)}
        result = MSTOCK.ms_place_sell(mc, symbol, qty,
                                      timeout_secs=_verify_timeout("exit", 8))
        if result["ok"]:
            result["slippage"] = round(exit_price_ref - result["fill_price"], 2)
            return result
        _prev_order_id = result.get("order_id", "") or _prev_order_id

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
#  TICK-FLOW STUDY — secret collector hook (optional, data-only)
# ═══════════════════════════════════════════════════════════════
# The method/thresholds live in the gitignored tick_flow.py. If that module is
# absent (e.g. a clean GitHub checkout), this is a silent no-op. Nothing here
# touches engine state or order routing.
_tick_flow_mod = None
_flow_fut_token = None


def _tf_fut_token():
    """Resolve + cache the NIFTY front-month future token for the flow study
    (Upstox-only). None if unavailable — collector then runs on CE/PE legs."""
    global _flow_fut_token
    if _flow_fut_token is not None:
        return _flow_fut_token
    ud = _udata_mod()
    if ud is None or not hasattr(ud, "nifty_front_fut"):
        return None
    try:
        tok, _key = ud.nifty_front_fut()
        if tok:
            _flow_fut_token = int(tok)
        return _flow_fut_token
    except Exception:
        return None


def _tf_get_flow(token):
    ud = _udata_mod()
    try:
        return ud.get_flow(token) if ud else None
    except Exception:
        return None


def _tf_get_tokens():
    lt = _locked_tokens or {}
    out = {}
    for side in ("CE", "PE"):
        info = lt.get(side) or {}
        if info.get("token"):
            out[side] = {"token": int(info["token"]),
                         "strike": info.get("strike", "")}
    ft = _tf_fut_token()
    if ft:
        out["FUT"] = {"token": int(ft)}
    return out


def _tf_get_engines():
    out = {}
    _fields = ("in_trade", "direction", "token", "entry_price", "entry_time",
               "peak_pnl", "strike", "dte")
    try:
        with _v11_lock:
            out["V11"] = {k: _v11_state.get(k) for k in _fields}
    except Exception:
        pass
    try:
        with _v13_lock:
            out["V13"] = {k: _v13_state.get(k) for k in _fields}
    except Exception:
        pass
    return out


def _start_tick_flow():
    global _tick_flow_mod
    try:
        import tick_flow as _tf
    except Exception:
        logger.info("[FLOW] tick_flow module absent — collector disabled")
        return
    try:
        # subscribe the NIFTY front-month FUT in full mode (underlying flow
        # context for V11/V13 + the V12-NIFTY signal). Best-effort; CE/PE legs
        # are subscribed at strike-lock time.
        _ft = _tf_fut_token()
        if _ft:
            D.subscribe_full_flow([_ft])
            logger.info("[FLOW] NIFTY FUT token " + str(_ft) + " subscribed (full)")
        else:
            logger.info("[FLOW] NIFTY FUT token unresolved — CE/PE legs only")
        _tf.configure(_tf_get_flow, _tf_get_tokens, _tf_get_engines)
        _tf.start()
        _tick_flow_mod = _tf
        logger.info("[FLOW] tick-flow collector started (paper data-only)")
    except Exception as _fe:
        logger.warning("[FLOW] collector start failed: " + str(_fe))


# ═══════════════════════════════════════════════════════════════
#  IN-PROCESS UPSTOX TOKEN REFRESH
#  (folded in from upstox_auth.py, 2026-06-23 — single-process
#  consolidation). SELF-CONTAINED: to remove, delete this block plus the
#  _refresh_upstox_token() + _start_token_refresher() calls in main().
#  Upstox tokens expire ~03:30 IST daily; we re-mint at startup (before
#  D.init) AND once each morning ~06:00 so a long-running bot survives the
#  expiry without a manual restart. Token is set in os.environ (which
#  upstox_data.access_token() reads first) AND persisted to ~/.env.
# ═══════════════════════════════════════════════════════════════
_UPSTOX_PROFILE_URL = "https://api.upstox.com/v2/user/profile"


def _validate_upstox_token(token):
    """Hit the Upstox profile endpoint with `token`. Returns the profile dict if
    the token is still valid, else None. Never raises."""
    if not token:
        return None
    try:
        r = requests.get(_UPSTOX_PROFILE_URL,
                         headers={"Authorization": "Bearer " + token, "Accept": "application/json"},
                         timeout=15)
        if r.status_code != 200:
            return None
        body = r.json()
        if body.get("status") == "success":
            return body.get("data")
    except Exception:
        return None
    return None


def _refresh_upstox_token():
    """Ensure a VALID Upstox token is live in this process (os.environ) + ~/.env.
    Reuses the existing token if it still validates; only mints a fresh one via the
    headless TOTP flow when the current token is missing/expired. Returns True on
    success; never raises (logs + returns False)."""
    # 1) Reuse path — if the token already in env/~/.env still validates, keep it.
    try:
        import upstox_data
        existing = upstox_data.access_token()
    except Exception:
        existing = os.getenv("UPSTOX_ACCESS_TOKEN", "")
    prof = _validate_upstox_token(existing)
    if prof:
        os.environ["UPSTOX_ACCESS_TOKEN"] = existing  # ensure env has it (env beats file)
        logger.info("[AUTH] existing Upstox token still valid — reusing (no re-mint) for "
                    + str(prof.get("user_name")) + " (" + str(prof.get("user_id")) + ")")
        return True

    # 2) Mint path — existing token missing/expired, so do the headless TOTP login.
    logger.info("[AUTH] no valid Upstox token in env/~/.env — minting a fresh one")
    try:
        from upstox_totp import UpstoxTOTP
    except Exception as e:
        logger.warning("[AUTH] upstox-totp unavailable — skipping refresh (" + str(e)[:60] + ")")
        return False
    try:
        resp = UpstoxTOTP().app_token.get_access_token()
    except Exception as e:
        logger.warning("[AUTH] headless login raised: " + str(e)[:120])
        return False
    if not (getattr(resp, "success", False) and getattr(resp, "data", None)):
        logger.warning("[AUTH] token generation failed: " + str(getattr(resp, "error", resp))[:120])
        return False
    token = resp.data.access_token
    prof = _validate_upstox_token(token)
    if not prof:
        logger.warning("[AUTH] token generated but verification failed")
        return False
    # env beats the ~/.env file lookup, so set it here first → live immediately
    os.environ["UPSTOX_ACCESS_TOKEN"] = token
    try:
        env_path = os.path.expanduser("~/.env")
        with open(env_path, "r") as f:
            lines = f.readlines()
        new_line = "UPSTOX_ACCESS_TOKEN=" + token + "\n"
        for i, ln in enumerate(lines):
            if re.match(r"\s*UPSTOX_ACCESS_TOKEN\s*=", ln):
                lines[i] = new_line
                break
        else:
            if lines and not lines[-1].endswith("\n"):
                lines[-1] += "\n"
            lines.append(new_line)
        with open(env_path, "w") as f:
            f.writelines(lines)
        os.chmod(env_path, 0o600)
    except Exception as e:
        logger.warning("[AUTH] token live in env but ~/.env write failed: " + str(e)[:80])
    logger.info("[AUTH] fresh Upstox token minted + verified for "
                + str(prof.get("user_name")) + " (" + str(prof.get("user_id")) + ")")
    return True


def _start_token_refresher():
    """Daily ~06:00 IST in-process token re-mint (Mon-Fri) so a long-running bot
    survives the 03:30 expiry without a restart. Daemon thread."""
    def _loop():
        while True:
            now = datetime.now()
            nxt = now.replace(hour=6, minute=0, second=0, microsecond=0)
            if nxt <= now:
                nxt += timedelta(days=1)
            time.sleep(max(60, (nxt - now).total_seconds()))
            if datetime.now().weekday() < 5:
                _refresh_upstox_token()
    threading.Thread(target=_loop, name="TokenRefresher", daemon=True).start()
    logger.info("[AUTH] daily token refresher thread started (~06:00 Mon-Fri)")


# ═══════════════════════════════════════════════════════════════
#  LEVELS shadow helper  (folded in 2026-06-23). SELF-CONTAINED: to
#  remove, delete this function + the _start_levels_shadow() call in
#  main(). Imports levels_shadow.py as an on-disk helper module
#  (read-only, DATA-ONLY — never a gate, never touches V11/V13 state,
#  places no orders). Runs once EOD (~15:45 Mon-Fri, after the spot
#  1-min CSV is complete) to compute the NEXT session's floor-pivots +
#  CPR + confluence "make-or-break" box from prev-day H/L/C and write
#  lab_data/levels/levels_<DATE>.json + append levels_shadow_log.csv
#  (A/B log). See project_box_levels. Fully sandboxed: a levels error
#  can never reach the V11/V13 engines.
# ═══════════════════════════════════════════════════════════════
def _start_levels_shadow():
    def _loop():
        try:
            import levels_shadow as _lv
        except Exception as e:
            logger.warning("[LEVELS] import failed — helper disabled (" + str(e)[:100] + ")")
            return
        logger.info("[LEVELS] EOD make-or-break levels helper thread started")
        while True:
            now = datetime.now()
            nxt = now.replace(hour=15, minute=45, second=0, microsecond=0)
            if nxt <= now:
                nxt += timedelta(days=1)
            time.sleep(max(60, (nxt - now).total_seconds()))
            if datetime.now().weekday() < 5:
                try:
                    p = _lv.generate()        # latest spot CSV → next session's levels
                    lv = p.get("levels", {})
                    logger.info("[LEVELS] " + str(p.get("for_date")) + " "
                                + str(p.get("close_vs_cpr_bias")) + " | CPR "
                                + str(lv.get("cpr_bottom")) + "-" + str(lv.get("cpr_top"))
                                + " (" + str(lv.get("cpr_regime")) + ")")
                except Exception as e:
                    logger.warning("[LEVELS] generate failed (sandboxed): " + str(e)[:120])
    threading.Thread(target=_loop, name="LevelsShadow", daemon=True).start()


# ═══════════════════════════════════════════════════════════════
#  ENTRY POINT
# ═══════════════════════════════════════════════════════════════

def main():
    global _kite
    logger.info("[MAIN] ═══ VISHAL RAJPUT TRADE " + D.VERSION + " STARTING ═══")
    _mode_str = "PAPER" if D.PAPER_MODE else "LIVE"
    logger.info("[MAIN] Mode: " + _mode_str)
    logger.info("[MAIN] Data provider: " + D.data_provider()
                + " | Strategy gate: " + D.strategy_version().upper())
    _tg_send(
        ("🟡 <b>Bot starting in PAPER mode</b>" if D.PAPER_MODE else "🟢 <b>Bot starting in LIVE mode</b>")
        + "\nVersion: " + D.VERSION
        + "\nMode: <b>" + _mode_str + "</b>"
        + "\nData: " + D.data_provider() + " | Gate: <b>" + D.strategy_version().upper() + "</b>"
        + ("\n⚠️ Real orders will be placed!" if not D.PAPER_MODE else ""),
        priority="critical"
    )
    logger.info("[MAIN] Scalps: DISABLED (data-backed decision)")

    _write_pid()
    signal.signal(signal.SIGINT,  _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    # Mint a fresh Upstox token IN-PROCESS before any data call (folded in from
    # the old upstox_auth cron, 2026-06-23). Best-effort: on failure we fall back
    # to whatever UPSTOX_ACCESS_TOKEN is already in env/~/.env (last good token).
    _refresh_upstox_token()

    # Market data is Upstox-only (Kite removed) → no Kite session; orders = m.Stock.
    kite = None
    D.init(None)

    # Phase 1 health: Upstox token + REST spot (WS check happens after start_websocket)
    _health_lines_pre = []
    _health_ok_pre = True
    try:
        _ud = D._udata_mod()
        _nm = _ud.profile_name() if _ud is not None else "?"
        _health_lines_pre.append("Upstox token: ✅ " + str(_nm))
    except Exception as _he:
        _health_lines_pre.append("Upstox token: ❌ " + str(_he)[:60])
        _health_ok_pre = False
    # REST spot probe is redundant with the WS tick check below; track its
    # failure separately so a transient REST blip doesn't drive ⚠️ when the
    # WS is delivering live ticks (_health_ok_pre stays Token-only).
    _spot_rest_failed = False
    for _spot_try in range(3):
        try:
            _sp = float(D.get_spot_ltp())
            if _sp <= 0:
                raise RuntimeError("upstox spot 0")
            _health_lines_pre.append("Spot: ✅ " + str(round(_sp, 1)))
            break
        except Exception as _he:
            if _spot_try < 2:
                time.sleep(1)
                continue
            _health_lines_pre.append("Spot: ❌ " + str(_he)[:60])
            _spot_rest_failed = True

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
    _load_v11_state()
    _load_v13_state()
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
    # Restart with an open trade: option tokens are normally subscribed only
    # via _lock_strikes(), which runs inside is_trading_window() (09:15-15:00).
    # A restart outside that window left the position with ltp=0, and
    # _v11_check_exit()'s ltp<=0 guard then disabled SL/trail/EOD exits
    # entirely (2026-06-10: EOD_EXIT never fired on a stuck PE trade).
    # Subscribe the in-trade token + opposite leg unconditionally.
    with _v11_lock:
        _boot_trade_tokens = [
            int(_v11_state.get("token", 0) or 0),
            int(_v11_state.get("_other_token", 0) or 0),
        ] if _v11_state.get("in_trade") else []
    if _boot_trade_tokens:
        D.subscribe_tokens(_boot_trade_tokens)
        logger.info("[MAIN] Open trade restored — resubscribed trade tokens: "
                    + str([t for t in _boot_trade_tokens if t]))
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
                # Live WS tick proves spot data is healthy — a transient REST blip
                # at boot is cosmetic, so rewrite the ❌ line instead of paging ⚠️.
                if _spot_rest_failed:
                    _health_lines_ws = [
                        ("Spot: ✅ " + str(round(_ws_ltp, 1)) + " (via WS — REST blip)")
                        if _l.startswith("Spot: ❌") else _l
                        for _l in _health_lines_ws
                    ]
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

    # ── Start tick-flow study collector (secret, optional, data-only) ──
    _start_tick_flow()

    # ── Folded-in plug-in threads (single-process consolidation, 2026-06-23) ──
    _start_token_refresher()   # daily ~06:00 Upstox token re-mint (was upstox_auth cron)
    _start_levels_shadow()     # daily ~15:45 EOD make-or-break levels (shadow, data-only)

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
    base = os.path.join(os.path.dirname(os.path.abspath(__file__)), "screener")
    # SMI FOCUS35 — the only surviving stock-F&O engine (frozen/loose/flow siblings
    # removed 2026-06-18; each stock runs its own tuned V12 gate)
    sources = [("fno_tracker_focus.csv", "FOCUS")]
    rows = []
    for fname, engine in sources:
        fno_path = os.path.join(base, fname)
        if not os.path.isfile(fno_path): continue
        try:
            with open(fno_path) as f:
                for r in csv.DictReader(f):
                    st = str(r.get("status",""))
                    if not (st.startswith("OPEN") or "HIT" in st): continue
                    try:
                        rows.append({
                        "engine":              engine,
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
  <div class="tab" data-t="fno" onclick="st('fno')">📊 F&amp;O</div>
  <div class="tab" data-t="trd" onclick="st('trd')">📒 TRD</div>
  <div class="tab" data-t="wkly" onclick="st('wkly')">📅 WEEKLY</div>
  <div class="tab" data-t="fil" onclick="st('fil')">📁 FILES</div>
</div>

<div id="p-sig"></div>
<div id="p-fno" class="H"></div>
<div id="p-trd" class="H"></div>
<div id="p-wkly" class="H"></div>
<div id="p-fil" class="H"></div>

<div class="ft">Auto-refresh 10s · <span id="ts"></span></div>


<script>
var _curTab='sig';
function st(t){_curTab=t;document.querySelectorAll('.tab').forEach(e=>e.classList.toggle('on',e.dataset.t===t));['sig','fno','trd','wkly','fil'].forEach(i=>document.getElementById('p-'+i).classList.toggle('H',i!==t));if(t==='fno')renderFno();if(t==='wkly')renderWeekly();if(t==='fil')loadFiles('');}

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

function render(d, trades){ if(!d || !d.market){document.getElementById('p-sig').innerHTML='<div style="text-align:center;color:#555;padding:20px">Waiting for bot data... (FILES tab works)</div>';document.getElementById('position-area').innerHTML='';return}

  const mk=d.market,ce=d.ce||{},pe=d.pe||{},pos=d.position||{},td=d.today||{},rl=d.rolling||{};
  // streak lives in rolling block, not today block — map it for the day-bar
  if(!td.streak&&rl.streak)td.streak=rl.streak;

  // Version + tags
  document.getElementById('ver').textContent=d.version||'';
  let tags='<span class="tag '+(d.mode==='LIVE'?'tg':'tb')+'">'+esc(d.mode||'')+'</span>';
  if(d.gate)tags+='<span class="tag '+(d.gate==='V13'?'ta':'tg')+'" title="Active entry gate">'+esc(d.gate)+'</span>';
  if(d.data_provider)tags+='<span class="tag tb" title="Market-data source">'+esc(d.data_provider)+'</span>';
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
    var candles=pos.candles||0;
    var tier=pos.active_ratchet_tier||'INITIAL';
    var totalQty=parseInt(pos.qty||0);
    var pnlRs=Math.round(pnl*totalQty);
    var pnlClr=pnl>=0?'var(--gn)':'var(--rd)';
    var posClr=pos.direction==='CE'?'rgba(59,130,246,.1)':'rgba(239,68,68,.08)';
    var posBd=pos.direction==='CE'?'rgba(59,130,246,.25)':'rgba(239,68,68,.2)';
    ph='<div class="pos" style="background:linear-gradient(135deg,'+posClr+',transparent);border:1px solid '+posBd+'">';
    ph+='<div style="font-size:13px;font-weight:700;margin-bottom:4px">🟢 '+esc(sym)+' '+esc(d.gate||'V11')+'</div>';
    ph+='<div style="margin:3px 0"><span class="big" style="color:'+pnlClr+'">'+(pnl>=0?'+':'')+pnl.toFixed(1)+'pts</span>';
    ph+=' <span style="color:#888;font-size:11px">&#x20B9;'+pnlRs.toLocaleString('en-IN')+'</span>';
    ph+='<span style="color:#555;font-size:10px;float:right">Entry &#x20B9;'+entry.toFixed(1)+' → &#x20B9;'+ltp+'</span></div>';
    // Peak-captured progress bar
    var peakPct2=peak>0?Math.min(100,Math.max(0,(pnl/peak)*100)):0;
    var peakBarClr=peakPct2>=80?'var(--gn)':peakPct2>=50?'var(--am)':'var(--rd)';
    ph+='<div class="bar-label" style="margin-bottom:3px"><span>PEAK CAPTURED</span><span style="color:'+peakBarClr+'">'+peakPct2.toFixed(0)+'%  +'+peak.toFixed(1)+'pts</span></div>';
    ph+='<div class="bar" style="margin-bottom:6px"><div class="bar-fill" style="width:'+peakPct2.toFixed(0)+'%;background:'+peakBarClr+'"></div></div>';
    // 3-box status grid: SL / TIER / HELD
    ph+='<div style="display:grid;grid-template-columns:repeat(3,1fr);gap:5px;margin-bottom:6px">';
    var tierClr=tier==='TRAIL_10'?'var(--gn)':tier==='LOCK_4'?'var(--am)':tier==='PROTECT'?'var(--am)':'var(--rd)';
    ph+='<div style="background:rgba(0,0,0,.35);border:1px solid var(--bd);border-radius:5px;padding:5px 4px;text-align:center"><div style="font-size:8px;color:#555;margin-bottom:2px">SL</div><div style="font-size:12px;font-weight:700;color:var(--rd)">&#x20B9;'+sl.toFixed(1)+'</div></div>';
    ph+='<div style="background:rgba(0,0,0,.35);border:1px solid var(--bd);border-radius:5px;padding:5px 4px;text-align:center"><div style="font-size:8px;color:#555;margin-bottom:2px">TIER</div><div style="font-size:12px;font-weight:700;color:'+tierClr+'">'+tier+'</div></div>';
    ph+='<div style="background:rgba(0,0,0,.35);border:1px solid var(--bd);border-radius:5px;padding:5px 4px;text-align:center"><div style="font-size:8px;color:#555;margin-bottom:2px">HELD</div><div style="font-size:12px;font-weight:700;color:var(--cy)">'+candles+'m</div></div>';
    ph+='</div>';
    ph+='<div class="pos-meta"><span>Peak: +'+(peak||0).toFixed(1)+'</span><span>Qty: '+totalQty+'</span><span>'+candles+'min</span></div>';
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
  (function(){
    function pill(label,val,ok){
      var c=ok?'var(--gn)':'var(--rd)';
      var bg=ok?'rgba(10,122,80,.10)':'rgba(192,57,43,.07)';
      var bd=ok?'rgba(10,122,80,.30)':'rgba(192,57,43,.20)';
      return '<div style="flex:1;text-align:center;padding:6px 2px;border-radius:10px;background:'+bg+';border:1px solid '+bd+'">'
        +'<div style="font-size:8px;font-weight:700;color:var(--dm);letter-spacing:.6px">'+label+'</div>'
        +'<div style="font-size:15px;font-weight:800;color:'+c+';line-height:1.25">'+val+'</div></div>';
    }
    function gateCard(side,o){
      var acc=side==='CE'?'var(--gn)':'var(--rd)';
      var h='<div style="background:var(--c1);border:1px solid var(--bd);border-top:3px solid '+acc+';border-radius:13px;padding:9px 9px 10px;box-shadow:0 1px 4px rgba(0,0,0,.05)">';
      h+='<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px">';
      var stk=(o&&o.strike>0)?o.strike:(side==='CE'?(mk.locked_ce||mk.atm||0):(mk.locked_pe||mk.atm||0));
      var ltpTxt=(o&&o.ltp>0)?('&#x20B9;'+o.ltp):'&#x20B9;&#x2014;';
      h+='<span style="font-size:13px;font-weight:800;color:'+acc+';letter-spacing:.6px">'+side+' '+(stk||'&#x2014;')+'</span>'+'<span style="font-size:14px;font-weight:800;color:var(--tx);margin-left:7px">'+ltpTxt+'</span>';
      if(!o||o.ltp===undefined){return h+'<span style="font-size:9px;color:var(--dm)">— no data —</span></div></div>';}
      if(o.fired)h+='<span style="background:var(--gn);color:#fff;font-size:10px;font-weight:800;padding:3px 12px;border-radius:20px">● READY</span>';
      else h+='<span style="background:var(--c2);color:var(--am);font-size:9px;font-weight:700;padding:3px 10px;border-radius:20px">⏳ '+esc(o.verdict||'wait')+'</span>';
      h+='</div><div style="display:flex;gap:6px">';
      var mg=parseFloat(o.momentum_gap||0);
      var dm2=parseFloat(o.decay_margin||0);
      h+=pill('MOMENTUM',(o.momentum_ok?'✓ ':'')+(mg>=0?'+':'')+mg.toFixed(1),o.momentum_ok);
      h+=pill('OPP DECAY',(o.decay_ok?'✓ ':'')+dm2.toFixed(1),o.decay_ok);
      return h+'</div></div>';
    }
    var anyReady=(ce.fired||pe.fired);
    var dot=anyReady?'<span style="display:inline-block;width:7px;height:7px;border-radius:50%;background:var(--gn);margin-right:5px"></span>':'';
    var GATE=d.gate||'V11';
    var gateDesc=(GATE==='V13')
      ? '⚡ V13 GATES &nbsp;·&nbsp; all dte: Close > EMA9L+3.5 · Opp Decay (close−EMA9H) [−9, −7] &nbsp;·&nbsp; 10:00–14:30 · Same-side 3-min'
      : '⚡ V11 GOLDEN GATES &nbsp;·&nbsp; dte≥2: Close > EMA9H+3.5 · Opp Decay [−9, −7] &nbsp;·&nbsp; dte0/1 %-gate: Mom +2.3%/+3.0% · Opp Decay [−4.8%, −2.7%] &nbsp;·&nbsp; 10:00–14:30 · Same-side 3-min';
    var html='<div style="margin:8px 8px 0">';
    html+='<div style="font-size:10px;font-weight:700;color:var(--dm);letter-spacing:.5px;padding:4px 10px 6px">'+dot+'⭐ '+esc(GATE)+' LIVE — '+(GATE==='V13'?'owner gate':'Golden')+' (1-min)'+(d.ts?' · '+d.ts:'')+'</div>';
    html+='<div style="margin:2px 10px 9px">';
    html+='<div style="font-size:9px;font-weight:700;color:'+(GATE==='V13'?'var(--am)':'var(--dm)')+';padding:0 2px 6px;letter-spacing:.5px">'+gateDesc+'</div>';
    html+='<div style="display:grid;grid-template-columns:1fr 1fr;gap:8px">'+gateCard('CE',ce)+gateCard('PE',pe)+'</div>';
    html+='</div></div>';
    // ── V13 SHADOW panel (paper A/B alongside V11) ──
    (function(){
      var v13=d.v13||{}; var v13ce=v13.ce||{}, v13pe=v13.pe||{}, v13pos=v13.position||{}, v13td=v13.today||{};
      function v13gate(side,o){
        var acc=side==='CE'?'var(--gn)':'var(--rd)';
        var stk=(o&&o.strike>0)?o.strike:'—';
        var ltpTxt=(o&&o.price>0)?('&#x20B9;'+o.price):'&#x20B9;&#x2014;';
        var mg=parseFloat((o&&o.momentum_gap)||0), dm=parseFloat((o&&o.decay_margin)||0);
        var mok=!!(o&&o.momentum_ok), dok=!!(o&&o.decay_ok), rdy=!!(o&&o.ready);
        function chip(lbl,val,ok){var c=ok?'var(--gn)':'var(--rd)';return '<span style="font-size:9px;color:var(--dm)">'+lbl+' </span><span style="font-size:11px;font-weight:700;color:'+c+'">'+(ok?'✓':'')+val+'</span>';}
        var hasV2=(o&&o.vel2!==null&&o.vel2!==undefined);
        var v2=parseFloat((o&&o.vel2)||0), v2ok=!!(o&&o.vel2_ok);
        var v2chip=hasV2?chip('VEL2',(v2>=0?'+':'')+v2.toFixed(1),v2ok)
                        :'<span style="font-size:9px;color:var(--dm)">VEL2 </span><span style="font-size:11px;font-weight:700;color:var(--dm)">n/a</span>';
        var st=rdy?'<span style="background:var(--gn);color:#fff;font-size:8px;font-weight:800;padding:1px 7px;border-radius:10px">READY</span>'
                  :'<span style="font-size:8px;color:var(--am)">'+esc((o&&o.reject)||'wait')+'</span>';
        return '<div style="flex:1;background:rgba(0,0,0,.04);border:1px solid var(--bd);border-radius:9px;padding:6px 8px">'
          +'<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:3px">'
          +'<span style="font-size:11px;font-weight:800;color:'+acc+'">'+side+' '+stk+'</span>'
          +'<span style="font-size:11px;color:var(--tx)">'+ltpTxt+'</span></div>'
          +'<div style="display:flex;gap:10px;margin-bottom:2px">'+chip('MOM',(mg>=0?'+':'')+mg.toFixed(1),mok)+chip('DECAY',dm.toFixed(1),dok)+v2chip+'</div>'
          +'<div>'+st+'</div></div>';
      }
      var vh='<div style="margin:10px 8px 0">';
      vh+='<div style="font-size:10px;font-weight:700;color:var(--am);letter-spacing:.5px;padding:4px 10px 4px">🧪 V13 SHADOW — paper A/B (gate: all-dte Close>EMA9L+3.5 · Opp Decay(close−EMA9H) [−9,−7])</div>';
      vh+='<div style="margin:2px 10px">';
      if(v13pos.in_trade){
        var vp=parseFloat(v13pos.pnl||0), vpk=parseFloat(v13pos.peak||0);
        var vclr=vp>=0?'var(--gn)':'var(--rd)';
        vh+='<div style="background:linear-gradient(135deg,rgba(245,158,11,.10),transparent);border:1px solid rgba(245,158,11,.25);border-radius:9px;padding:7px 9px;margin-bottom:6px">';
        vh+='<span style="font-size:12px;font-weight:700">'+esc(v13pos.direction||'')+' '+(v13pos.strike||'')+'</span> ';
        vh+='<span style="font-size:14px;font-weight:800;color:'+vclr+'">'+(vp>=0?'+':'')+vp.toFixed(1)+'pts</span>';
        var v13v2=(v13pos.vel2_at_entry!==null&&v13pos.vel2_at_entry!==undefined)?('vel2@entry '+(parseFloat(v13pos.vel2_at_entry)>=0?'+':'')+parseFloat(v13pos.vel2_at_entry).toFixed(1)+' · '):'';
        vh+='<span style="font-size:10px;color:#888;float:right">'+v13v2+'Entry &#x20B9;'+parseFloat(v13pos.entry||0).toFixed(1)+' → &#x20B9;'+(v13pos.ltp||0)+' · SL &#x20B9;'+parseFloat(v13pos.sl||0).toFixed(1)+' · '+esc(v13pos.active_ratchet_tier||'INITIAL')+' · Peak +'+vpk.toFixed(1)+'</span>';
        vh+='</div>';
      }
      vh+='<div style="display:flex;gap:8px">'+v13gate('CE',v13ce)+v13gate('PE',v13pe)+'</div>';
      var vd=parseFloat(v13td.pnl||0), vw=parseInt(v13td.wins||0), vl=parseInt(v13td.losses||0);
      var vwr=(vw+vl)>0?Math.round(vw/(vw+vl)*100):0;
      vh+='<div style="font-size:10px;color:var(--dm);padding:5px 2px 0">V13 DAY <b style="color:'+(vd>=0?'var(--gn)':'var(--rd)')+'">'+(vd>=0?'+':'')+vd.toFixed(1)+'pts</b> · '+(v13td.trades||0)+' trades · '+vw+'W '+vl+'L · WR '+vwr+'%</div>';
      vh+='</div></div>';
      html+=vh;
    })();
    // ── Account + rolling performance (moved from retired MKT tab) ──
    var acct=d.account||{};
    var balAmt=Math.round(parseFloat(acct.balance||0));
    var usedAmt=Math.round(parseFloat(acct.used||0));
    var balClr=balAmt>=0?'var(--gn)':'var(--rd)';
    var balStr=(balAmt<0?'-₹':'₹')+Math.abs(balAmt).toLocaleString('en-IN')+(balAmt<0?' !':'');
    html+='<div class="sect"><div class="sh">MSTOCK · '+esc(acct.name||'—')+'</div>'+
      '<div class="ctx-row">'+
      '<div class="ctx"><div class="k">AVAILABLE</div><div class="v" style="color:'+balClr+';font-size:11px">'+balStr+'</div></div>'+
      '<div class="ctx"><div class="k">USED MARGIN</div><div class="v" style="color:var(--am);font-size:11px">₹'+usedAmt.toLocaleString('en-IN')+'</div></div>'+
      '<div class="ctx"><div class="k">MODE</div><div class="v" style="color:'+(d.mode==='LIVE'?'var(--gn)':'var(--bl)')+'">'+esc(d.mode||'PAPER')+'</div></div>'+
      '<div class="ctx"><div class="k">GATE</div><div class="v" style="color:'+(d.gate==='V13'?'var(--am)':'var(--gn)')+'">'+esc(d.gate||'V11')+'</div></div>'+
      '<div class="ctx"><div class="k">DATA</div><div class="v" style="color:var(--dm);font-size:10px">'+esc(d.data_provider||'upstox')+'</div></div>'+
      '<div class="ctx"><div class="k">VERSION</div><div class="v" style="color:var(--dm);font-size:10px">'+esc(d.version||'—')+'</div></div>'+
      '</div></div>';
    var l10c=rl.last10_wr>=60?'var(--gn)':rl.last10_wr>=40?'var(--am)':'var(--rd)';
    var l20c=rl.last20_wr>=60?'var(--gn)':rl.last20_wr>=40?'var(--am)':'var(--rd)';
    var ptsc=(rl.last10_pts||0)>=0?'var(--gn)':'var(--rd)';
    var strk=rl.streak||0;
    var strkTxt=strk>=2?(''+strk+'-WIN STREAK'):strk<=-2?(''+Math.abs(strk)+'-LOSS STREAK'):'No streak';
    var strkClr=strk>=2?'var(--gn)':strk<=-2?'var(--rd)':'var(--dm)';
    html+='<div class="sect"><div class="sh">ROLLING PERFORMANCE</div>'+
      '<div class="ctx-row">'+
      '<div class="ctx"><div class="k">LAST 10 WR</div><div class="v" style="color:'+l10c+'">'+(rl.last10_wr||0)+'%</div></div>'+
      '<div class="ctx"><div class="k">LAST 20 WR</div><div class="v" style="color:'+l20c+'">'+(rl.last20_wr||0)+'%</div></div>'+
      '<div class="ctx"><div class="k">L10 PTS</div><div class="v" style="color:'+ptsc+'">'+(rl.last10_pts>=0?'+':'')+(rl.last10_pts||0)+'</div></div>'+
      '<div class="ctx"><div class="k">STREAK</div><div class="v" style="color:'+strkClr+';font-size:9px">'+strkTxt+'</div></div>'+
      '</div></div>';
    document.getElementById('p-sig').innerHTML=html;
    try{ if(d.flow){ document.getElementById('p-sig').innerHTML += renderFlow(d.flow); } }catch(e){}
  })();

  // (MKT tab retired 2026-06-10 — account/rolling moved to SIG)

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
    // summary — model portfolio: 1 share bought in every open pick
    var totRet=0;var wins=0;var pfInv=0;var pfVal=0;
    open.forEach(function(p){var r=parseFloat(p.current_return_pct||0);totRet+=r;if(r>0)wins++;
      var e=parseFloat(p.entry_price||0);var c=parseFloat(p.current_price||0)||e;pfInv+=e;pfVal+=c;});
    var avgRet=open.length?totRet/open.length:0;
    var avgClr=avgRet>=0?'var(--gn)':'var(--rd)';
    var pfPnl=pfVal-pfInv;var pfPct=pfInv>0?pfPnl/pfInv*100:0;
    var pfClr=pfPnl>=0?'var(--gn)':'var(--rd)';var pfSign=pfPnl>=0?'+':'';
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
          '<div style="font-size:9px;color:var(--dm);margin-top:1px">1 sh · Inv ₹'+entry.toFixed(0)+' → ₹'+(cur||entry).toFixed(0)+'</div></div>'+
          '<div style="text-align:right"><span style="font-weight:800;font-size:18px;color:'+clr+'">'+(w?'+':'')+ret.toFixed(1)+'%</span>'+
          '<div style="font-size:10px;color:'+clr+'">'+(w?'+':'−')+'₹'+Math.abs(Math.round((cur||entry)-entry)).toLocaleString('en-IN')+'</div></div>'+
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
        '<div><div style="font-size:9px;color:var(--dm)">MULTIBAGGER PORTFOLIO (1 share each)</div>'+
        '<div style="font-weight:800;font-size:16px;color:'+pfClr+'">'+pfSign+'₹'+Math.abs(Math.round(pfPnl)).toLocaleString('en-IN')+' <span style="font-size:11px">('+pfSign+pfPct.toFixed(1)+'%)</span></div>'+
        '<div style="font-size:9px;color:var(--dm);margin-top:1px">Inv ₹'+Math.round(pfInv).toLocaleString('en-IN')+' → ₹'+Math.round(pfVal).toLocaleString('en-IN')+'</div></div>'+
        '<div style="text-align:right"><div style="font-size:9px;color:var(--dm)">'+open.length+' OPEN · '+wins+'W/'+(open.length-wins)+'L</div>'+
        '<div style="font-size:9px;color:var(--dm);margin-top:2px">AVG RETURN</div>'+
        '<div style="font-weight:800;font-size:16px;color:'+avgClr+'">'+(avgRet>=0?'+':'')+avgRet.toFixed(1)+'%</div></div>'+
      '</div>'+cards+closedHtml;
  }catch(e){document.getElementById('p-wkly').innerHTML='<div style="color:var(--dm);padding:16px">Error loading weekly data</div>';console.error(e);}
}

function flowChip(v){var c=v==='REAL'?'#2ecc71':(v==='BLUFF'?'#e74c3c':'#999');return '<span style="color:'+c+';font-weight:600">'+(v||'—')+'</span>';}
function renderFlow(f){
  if(!f) return '';
  var rows='';
  ['CE','PE','FUT'].forEach(function(k){var l=f[k];if(!l||l.ltp===undefined)return;
    rows+='<tr><td style="padding:2px 6px">'+k+'</td><td>'+(l.ltp||'')+'</td><td>'+(l.delta_30s||0)+'</td><td>'+(l.cum_delta||0)+'</td><td>'+(l.book_imb||0)+'</td><td>'+(l.oi_state||'')+'</td><td>'+flowChip(l.verdict)+' <span style="color:#888;font-size:10px">'+(l.sigs||'')+'</span></td></tr>';});
  if(!rows) rows='<tr><td colspan="7" style="color:#888;padding:4px">waiting for full-mode ticks…</td></tr>';
  var banner='';
  if(f.trade){var t=f.trade;var bc=t.fade?'#e74c3c':(t.ride?'#2ecc71':'#999');var bl=t.fade?'FADE':(t.ride?'RIDE':'steady');
    banner='<div style="margin-top:6px;padding:6px;border:1px solid '+bc+';border-radius:6px;color:'+bc+';font-size:11px">IN-TRADE '+(t.engine||'')+' '+(t.direction||'')+' +'+(t.peak_pnl||0)+' · '+bl+' · sinceΔ '+(t.since_entry_delta||0)+'</div>';}
  var ev='';
  if(f.events&&f.events.length){ev='<div style="margin-top:6px;font-size:10px;color:#888">'+f.events.slice().reverse().map(function(e){return e.ts+' '+e.text;}).join(' · ')+'</div>';}
  return '<div style="border:1px solid #0b8;border-radius:8px;padding:8px;margin-top:10px;background:rgba(0,180,160,.05)">'+
    '<div style="color:#0cc;font-weight:600;margin-bottom:4px;font-size:12px">FLOW · Upstox full-mode · provisional/un-calibrated</div>'+
    '<table style="width:100%;font-size:11px;border-collapse:collapse"><tr style="color:#888;text-align:left"><th style="padding:2px 6px">leg</th><th>ltp</th><th>Δ30s</th><th>cumΔ</th><th>book</th><th>OI state</th><th>verdict</th></tr>'+rows+'</table>'+banner+ev+'</div>';
}

async function renderFno(){
  try{
    const fno=await fetch('/api/fno').then(r=>r.json()).catch(e=>[]);
    const el=document.getElementById('p-fno');
    if(!fno||!fno.length){el.innerHTML='<div style="text-align:center;color:var(--dm);padding:30px">SMI paper engine live — no trades yet<div style="font-size:9px;margin-top:4px">entries 09:30–14:30 · 15m bars · 2-week validation</div></div>';return;}
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
      var lots=parseInt(p.lots||1);var lotSize=parseInt(p.lot_size||0);var qty=lots*lotSize;
      var invest=parseFloat(p.investment||0);var curVal=invest+pnlRs;
      var w=pnlPct>=0;var clr=w?'var(--gn)':'var(--rd)';var sign=w?'+':'';
      var st=p.status||'';
      var isOpen=st.startsWith('OPEN');
      var isT1=st.includes('T1-HIT');var isSl=st.includes('SL-HIT');
      var cardBorder=isT1?'rgba(52,211,153,.4)':isSl?'rgba(248,113,113,.4)':'var(--bd)';
      var badgeBg=isT1?'rgba(52,211,153,.15)':isSl?'rgba(248,113,113,.15)':'rgba(0,0,0,.05)';
      var badgeClr=isT1?'var(--gn)':isSl?'var(--rd)':'var(--dm)';
      var dirClr=p.direction==='CALL'?'var(--bl)':'var(--rd)';
      var range=t2-sl;
      var pct=range>0?Math.max(0,Math.min(100,((ltp-sl)/range)*100)):0;
      var barClr=isSl?'var(--rd)':isT1?'var(--gn)':pct>=70?'var(--gn)':pct>=35?'var(--am)':'var(--rd)';
      var t1Pct=range>0?Math.max(0,Math.min(100,((t1-sl)/range)*100)):0;
      var dirIcon=p.direction==='CALL'?'🟢':'🔴';
      var looseBadge=p.engine==='LOOSE'?'<span style="font-size:8px;font-weight:800;color:var(--am);background:rgba(245,158,11,.15);border-radius:3px;padding:1px 4px;margin-left:5px;letter-spacing:.5px">LOOSE</span>':'';
      var focusBadge=p.engine==='FOCUS'?'<span style="font-size:8px;font-weight:800;color:var(--gn);background:rgba(34,197,94,.15);border-radius:3px;padding:1px 4px;margin-left:5px;letter-spacing:.5px">FOCUS35</span>':'';
      return '<div style="margin:6px 8px;background:var(--c1);border:1px solid '+cardBorder+';border-left:3px solid '+dirClr+';border-radius:8px;padding:10px 10px 8px">'+
        '<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:6px">'+
          '<div><span style="font-size:14px;font-weight:800;color:var(--tx)">'+dirIcon+' '+esc(p.symbol)+'</span>'+looseBadge+focusBadge+
          '<span style="font-size:10px;font-weight:700;color:'+dirClr+';margin-left:5px">'+esc(p.direction)+' '+(p.strike||'')+'</span>'+
          '<div style="font-size:9px;color:var(--dm);margin-top:1px">₹'+entry.toFixed(0)+' → ₹'+ltp.toFixed(0)+' · '+lots+' lot × '+lotSize+' = '+qty+' qty</div>'+
          '<div style="font-size:9px;color:var(--dm);margin-top:1px">Inv ₹'+Math.round(invest).toLocaleString('en-IN')+' → ₹'+Math.round(curVal).toLocaleString('en-IN')+'</div></div>'+
          '<div style="text-align:right"><span style="font-weight:800;font-size:18px;color:'+clr+'">'+sign+pnlPct.toFixed(0)+'%</span>'+
          '<div style="font-size:10px;color:'+clr+'">'+sign+'₹'+Math.abs(Math.round(pnlRs)).toLocaleString('en-IN')+'</div></div>'+
        '</div>'+
        (range>0?
        '<div style="position:relative;height:6px;background:var(--c2);border-radius:3px;overflow:visible;margin:0 0 3px">'+
          '<div style="height:100%;width:'+pct.toFixed(0)+'%;background:'+barClr+';border-radius:3px;transition:width .5s"></div>'+
          '<div style="position:absolute;top:-4px;left:0;width:2px;height:14px;background:var(--rd);border-radius:1px" title="SL ₹'+sl.toFixed(0)+'"></div>'+
          (t1Pct>0&&t1Pct<100?'<div style="position:absolute;top:-4px;left:'+t1Pct.toFixed(0)+'%;width:2px;height:14px;background:var(--am);border-radius:1px" title="T1 ₹'+t1.toFixed(0)+'"></div>':'')+
          '<div style="position:absolute;top:-4px;right:0;width:2px;height:14px;background:var(--gn);border-radius:1px" title="T2 ₹'+t2.toFixed(0)+'"></div>'+
        '</div>'+
        '<div style="display:flex;justify-content:space-between;font-size:8px;color:var(--dm)">'+
          '<span>SL ₹'+sl.toFixed(0)+'</span><span>T1 ₹'+t1.toFixed(0)+'</span><span>T2 ₹'+t2.toFixed(0)+'</span>'+
        '</div>'
        :
        '<div style="display:flex;justify-content:space-between;font-size:8px;color:var(--dm)">'+
          '<span>Stock '+esc(String(p.stock_price||'—'))+' · SL '+esc(String(p.stock_sl||'—'))+' (1%)</span>'+
          '<span style="color:var(--bl);font-weight:600">'+esc(p.structure||'')+(p.regime&&p.regime!=='NORMAL'?' · '+esc(p.regime):'')+'</span>'+
        '</div>'
        )+
        '<div style="display:flex;justify-content:space-between;font-size:8px;color:var(--dm);margin-top:3px">'+
          '<span style="background:'+badgeBg+';color:'+badgeClr+';padding:1px 6px;border-radius:3px;font-weight:600">'+esc(st)+'</span>'+
          '<span>'+esc(p.date_added)+'</span>'+
        '</div></div>';
    }
    var allPos=openPos.concat(closedPos);
    allPos.forEach(function(p){totalPnl+=parseFloat(p.pnl_rs||0);});
    var openInvest=0;openPos.forEach(function(p){openInvest+=parseFloat(p.investment||0);});
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
        '<div><div style="font-size:9px;color:var(--dm);margin-bottom:2px">SMI PAPER TEST &mdash; TOTAL P&amp;L</div>'+
        '<div style="font-weight:700;font-size:18px;color:'+totClr+'">'+totSign+'&#x20B9;'+Math.abs(Math.round(totalPnl)).toLocaleString('en-IN')+'</div></div>'+
        '<div style="text-align:right;font-size:10px;color:var(--dm)">'+openCount+' open &nbsp;\xb7&nbsp; '+closedPos.length+' closed'+
        '<div style="margin-top:2px">Deployed ₹'+Math.round(openInvest).toLocaleString('en-IN')+'</div></div>'+
      '</div>'+
      (openToday.length?'<div style="margin:4px 8px;font-size:10px;font-weight:700;color:var(--bl);text-transform:uppercase;letter-spacing:.5px">Today\'s SMI Trades ('+openToday.length+')</div>':'')+
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
    const dr=await fetch('/api/dashboard');
    // session expired/absent -> /api/* 302s to the login HTML; fetch follows it,
    // so r.json() would throw and the page would hang blank. Bounce to /login instead.
    if(dr.redirected||dr.status===401||dr.status===403){location.href='/login';return;}
    const d=await dr.json().catch(e=>null);
    if(d===null){location.href='/login';return;}
    const t=await fetch('/api/trades').then(r=>r.json()).catch(e=>[]);
    render(d,t||[]);
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
                ("trade_log", "Full Trade History"),
            ]
            for fkey, label in analysis_items:
                html += '<a href="/files?f=' + fkey + '" class="f">' + label + '</a>'
            html += '<div class="sh">SYSTEM</div>'
            system_items = [
                ("logs_live", "Live Logs"),
                ("logs_errors", "Error Logs"),
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
        elif p=="/api/fno":self._j(_web_read_fno())
        elif p=="/api/weekly":self._j(_web_read_weekly())
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
        init(None)                       # Upstox-only data; no Kite session
        if _udata_mod() is None:
            raise RuntimeError("upstox_data unavailable")
        _collector_log("Upstox data init OK")
    except Exception as e:
        _collector_log("DATA INIT FAILED: " + str(e))
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
