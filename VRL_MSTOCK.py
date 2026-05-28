"""
VRL_MSTOCK.py — MStock (Mirae Asset) broker wrapper for ORDER EXECUTION only.

Market data (ticks, quotes, historical) stays on Kite.
This module handles: BUY entry, SELL exit, cancel, order-fill verification.

Auth flow (TOTP path — recommended, fully automated):
  login(client_id, password) → ugid
  verify_totp(api_key, pyotp.TOTP(secret).now()) → access_token

Env vars required in ~/.env:
  MSTOCK_CLIENT_ID      — your MStock login ID (e.g. MA2081433)
  MSTOCK_PASSWORD       — your MStock password
  MSTOCK_API_KEY        — API key from MStock developer portal
  MSTOCK_TOTP_SECRET    — TOTP secret from MStock Security settings
                          (enable Authenticator App → copy the secret key)
"""

import json
import logging
import os
import time
from datetime import datetime

logger = logging.getLogger(__name__)

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

def _read_token() -> dict:
    try:
        if os.path.isfile(MSTOCK_TOKEN_FILE):
            with open(MSTOCK_TOKEN_FILE) as f:
                return json.load(f)
    except Exception as e:
        logger.warning(f"[MSTOCK] Token read error: {e}")
    return {}


def _write_token(data: dict):
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
    Requires TOTP enabled on MStock account (Settings → Security → Auth App).

    Flow:
      1. login(client_id, password) → ugid
      2. verify_totp(api_key, pyotp.TOTP(totp_secret).now()) → access_token
    """
    import pyotp

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
    logger.info("[MSTOCK] Step 2 OK — session ready ✓")
    return access_token


def get_mstock():
    """
    Return an authenticated MConnect instance.
    Reads cached daily token first; does full login only if needed.
    Pattern mirrors VRL_CONFIG.get_kite().
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
    saved     = _read_token()

    if saved.get("date") == today_str and saved.get("access_token"):
        logger.info("[MSTOCK] Using cached daily token")
        mc.set_access_token(saved["access_token"])
        mc.set_api_key(api_key)
        return mc

    logger.info("[MSTOCK] No valid token — doing fresh login (TOTP)")
    access_token = _do_login_totp(mc, client_id, password, api_key, totp_secret)
    _write_token({"date": today_str, "access_token": access_token})
    mc.set_access_token(access_token)
    mc.set_api_key(api_key)
    return mc


# ── Order fill verification ──────────────────────────────────────────────────

def ms_verify_fill(mc, order_id: str, timeout_secs: int = 10) -> tuple[float, int]:
    """
    Poll MStock order status until COMPLETE or REJECTED/CANCELLED.
    Returns (fill_price, fill_qty). Returns (0.0, 0) on failure/timeout.
    Mirrors verify_order_fill() in VRL_MAIN.py.
    """
    deadline = time.time() + timeout_secs
    while time.time() < deadline:
        try:
            resp = mc.get_order_details(order_id, _segment="E")
            data = resp.json()
            if data.get("status") != "success":
                time.sleep(0.5)
                continue

            # get_order_details returns the order object (dict or list)
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
    """
    Place a LIMIT BUY (entry) on MStock.

    Args:
      symbol      — NSE tradingsymbol (same format as Kite)
                    NIFTY options  : "NIFTY2660223900CE"
                    Stock options  : "RELIANCE26JUN1400CE"
                    Stock futures  : "RELIANCE26JUNFUT"
      qty         — quantity (lots × lot_size)
      limit_price — limit price (bot adds 1% buffer before calling)
      exchange    — "NFO" for all F&O (default). Use "NSE"/"BSE" for equity.
      product     — "MIS" intraday (default) | "NRML" positional overnight

    Returns same dict shape as place_entry():
      {"ok": bool, "fill_price": float, "fill_qty": int,
       "order_id": str, "error": str, "slippage": float}
    """
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
            # Not filled — cancel the resting limit order
            try:
                mc.cancel_order(order_id)
                logger.info(f"[MSTOCK] Entry cancelled — price moved: {order_id}")
            except Exception:
                pass
            return {"ok": False, "fill_price": 0.0, "fill_qty": 0,
                    "order_id": order_id, "error": "LIMIT_NOT_FILLED", "slippage": 0}

        ref_price = limit_price   # limit was our reference
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
    """
    Place a MARKET SELL (exit) on MStock.

    Args:
      symbol   — NSE tradingsymbol (same as used at entry)
      qty      — quantity to exit
      exchange — "NFO" for all F&O (default)
      product  — "MIS" intraday (default) | "NRML" positional overnight
                 MUST match what was used at entry, else exchange rejects.

    Returns same dict shape as place_exit().
    """
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
# Usage when stock F&O strategy is added:
#
#   mc = MSTOCK.get_mstock()
#
#   # Intraday stock option (MIS — auto square-off by 3:20)
#   result = MSTOCK.ms_stock_buy(mc, "RELIANCE26JUN1400CE", qty=250, limit_price=45.5)
#
#   # Positional stock option (NRML — carries overnight)
#   result = MSTOCK.ms_stock_buy(mc, "TCS26JUN3600CE", qty=150, limit_price=88.0,
#                                positional=True)
#
#   # Exit (product MUST match what was used at entry)
#   result = MSTOCK.ms_stock_sell(mc, "RELIANCE26JUN1400CE", qty=250)

def ms_stock_buy(mc, symbol: str, qty: int, limit_price: float,
                 positional: bool = False,
                 timeout_secs: int = 10) -> dict:
    """
    Buy a stock F&O contract (option or future) on MStock NFO.

    positional=False → MIS  (intraday, auto square-off at 3:20 PM)
    positional=True  → NRML (carry overnight, manual exit required)

    Symbol examples:
      Stock option  : "RELIANCE26JUN1400CE", "TCS2660523600CE"
      Stock future  : "RELIANCE26JUNFUT", "TCS26JUNFUT"
    """
    product = "NRML" if positional else "MIS"
    return ms_place_buy(mc, symbol, qty, limit_price,
                        timeout_secs=timeout_secs,
                        exchange="NFO", product=product)


def ms_stock_sell(mc, symbol: str, qty: int,
                  positional: bool = False,
                  timeout_secs: int = 10) -> dict:
    """
    Sell/exit a stock F&O contract on MStock NFO.
    positional must match what was used at entry.
    """
    product = "NRML" if positional else "MIS"
    return ms_place_sell(mc, symbol, qty,
                         timeout_secs=timeout_secs,
                         exchange="NFO", product=product)


def ms_get_stock_positions(mc) -> list:
    """
    Return all open stock F&O positions from MStock (NFO only, non-NIFTY/BANKNIFTY).
    Useful for reconciliation when stock F&O strategy goes live.
    """
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
    """
    Return a one-liner for the bot startup Telegram banner, e.g.:
      "MStock: MA2081433 | Avail: ₹1,23,456 | Used: ₹0"
    Falls back to client-id-only on any error (non-blocking).
    """
    client_id = os.getenv("MSTOCK_CLIENT_ID", "MStock")
    try:
        mc   = get_mstock()

        # ── Profile (name) ──
        name = ""
        try:
            p    = mc.get_profile()
            pd   = p.json()
            if pd.get("status") == "success":
                raw  = pd.get("data") or {}
                name = str(raw.get("name") or raw.get("client_name") or "").strip()
        except Exception:
            pass

        # ── Fund summary (balance) ──
        avail_str = ""
        used_str  = ""
        try:
            f  = mc.get_fund_summary()
            fd = f.json()
            if fd.get("status") == "success":
                raw   = fd.get("data") or {}
                # MStock fund summary keys vary — try common names
                avail = float(
                    raw.get("available_cash")
                    or raw.get("net")
                    or raw.get("available_balance")
                    or raw.get("equity", {}).get("available_cash", 0)
                    or 0
                )
                used  = float(
                    raw.get("utilised_amount")
                    or raw.get("used")
                    or raw.get("equity", {}).get("utilised_amount", 0)
                    or 0
                )
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

def test_connection() -> bool:
    """Call from auth script to confirm MStock is working. Checks fund summary."""
    try:
        mc   = get_mstock()
        resp = mc.get_fund_summary()
        data = resp.json()
        ok   = data.get("status") == "success"
        if ok:
            logger.info("[MSTOCK] Connection test OK ✓")
        else:
            logger.warning(f"[MSTOCK] Connection test FAILED: {data}")
        return ok
    except Exception as e:
        logger.error(f"[MSTOCK] Connection test error: {e}")
        return False
