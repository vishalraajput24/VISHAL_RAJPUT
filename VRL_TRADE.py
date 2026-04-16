#!/home/user/kite_env/bin/python3
# ═══════════════════════════════════════════════════════════════
#  VRL_TRADE.py — VISHAL RAJPUT TRADE v13.3
#  Sealed order execution machine.
#  ONLY file that touches Kite orders.
#  Paper mode: simulated fills. Live mode: LIMIT entry + smart exit.
# ═══════════════════════════════════════════════════════════════

# v13.1: Live paths verified — LIMIT entry, MARKET exit, market_protection=-1

import os
import time
import logging
from datetime import datetime

import VRL_DATA as D
import VRL_CONFIG as CFG


def _verify_timeout(kind: str, default: int) -> int:
    """BUG-Q v15.2.5 Batch 6: pull verify_order_fill timeouts from
    config.yaml -> trade.verify_timeout_{entry,exit}. Falls back to
    the historical hardcoded value if config lacks the key."""
    try:
        v = (CFG.get().get("trade") or {}).get("verify_timeout_" + kind)
        if v is not None:
            return int(v)
    except Exception:
        pass
    return default

try:
    from kiteconnect.exceptions import (
        TokenException, NetworkException, GeneralException,
        OrderException, InputException,
    )
except ImportError:
    TokenException = NetworkException = GeneralException = Exception
    OrderException = InputException = Exception

logger = logging.getLogger("vrl_live")


# ─── MARGIN CHECKS ────────────────────────────────────────────

def get_margin_available(kite) -> float:
    """Return available cash margin. Returns -1.0 on error."""
    try:
        margins = kite.margins(segment="equity")
        return float(margins.get("net", 0))
    except Exception as e:
        logger.error("[TRADE] Margin fetch error: " + str(e))
        return -1.0


# ─── ORDER VERIFICATION ───────────────────────────────────────

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


# ─── ENTRY ORDER ──────────────────────────────────────────────

def place_entry(kite, symbol: str, token: int,
                option_type: str, qty: int,
                entry_price_ref: float) -> dict:
    """
    Paper mode: simulated fill at entry_price_ref.
    Live mode: LIMIT order at LTP + buffer for better fill quality.
    """
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

    # Live mode: LIMIT entry
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


# ─── EXIT ORDER ───────────────────────────────────────────────

def place_exit(kite, symbol: str, token: int,
               option_type: str, qty: int,
               exit_price_ref: float, reason: str) -> dict:
    """
    Paper mode: simulated fill.
    Live mode: MARKET for urgent exits, LIMIT for stale (with MARKET fallback).
    """
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

    # STALE_ENTRY: try LIMIT first (not urgent), fallback to MARKET
    use_limit_first = reason in ("STALE_ENTRY",)

    for attempt in range(2):
        try:
            if use_limit_first and attempt == 0:
                # LIMIT exit attempt
                buffer = max(1.0, round(exit_price_ref * 0.005, 1))
                limit_price = round(exit_price_ref - buffer, 1)
                order_id = kite.place_order(
                    variety          = kite.VARIETY_REGULAR,
                    exchange         = D.EXCHANGE_NFO,
                    tradingsymbol    = symbol,
                    transaction_type = kite.TRANSACTION_TYPE_SELL,
                    quantity         = qty,
                    order_type       = kite.ORDER_TYPE_LIMIT,
                    price            = limit_price,
                    product          = kite.PRODUCT_MIS,
                )
                logger.info("[TRADE] LIMIT EXIT placed: " + str(order_id)
                            + " limit=" + str(limit_price))

                fill_price, fill_qty = verify_order_fill(
                    kite, order_id, timeout_secs=_verify_timeout("exit", 5))

                if fill_qty > 0:
                    slippage = round(exit_price_ref - fill_price, 2)
                    return {
                        "ok": True, "fill_price": fill_price, "fill_qty": fill_qty,
                        "order_id": str(order_id), "error": "", "slippage": slippage,
                    }

                # Not filled — cancel and fall through to MARKET
                try:
                    kite.cancel_order(kite.VARIETY_REGULAR, order_id)
                except Exception:
                    pass
                logger.warning("[TRADE] LIMIT exit not filled, falling back to MARKET")
                use_limit_first = False  # next attempt is MARKET
                continue

            # MARKET exit (default for all reasons except first STALE attempt)
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

    # Both attempts failed
    logger.critical("CRITICAL: Exit failed for " + symbol
                    + " qty=" + str(qty) + ". MANUAL ACTION REQUIRED.")
    return {"ok": False, "fill_price": 0.0, "fill_qty": 0,
            "order_id": "", "error": "EXIT_FAILED_MANUAL_REQUIRED", "slippage": 0}


# ─── EXCHANGE SL BACKUP ──────────────────────────────────────

def place_sl_order(kite, symbol: str, qty: int, sl_price: float) -> str:
    """Place SL-M at exchange as crash backup."""
    if D.PAPER_MODE:
        return "PAPER_SL"
    try:
        order_id = kite.place_order(
            variety          = kite.VARIETY_REGULAR,
            exchange         = D.EXCHANGE_NFO,
            tradingsymbol    = symbol,
            transaction_type = kite.TRANSACTION_TYPE_SELL,
            quantity         = qty,
            order_type       = kite.ORDER_TYPE_SLM,
            trigger_price    = round(sl_price, 1),
            product          = kite.PRODUCT_MIS,
        )
        logger.info("[TRADE] SL-M placed: " + str(order_id)
                     + " trigger=₹" + str(round(sl_price, 1)))
        return str(order_id)
    except Exception as e:
        logger.error("[TRADE] SL-M failed: " + str(e))
        return ""


def cancel_sl_order(kite, order_id: str) -> bool:
    """Cancel backup SL before normal exit."""
    if D.PAPER_MODE or not order_id or order_id == "PAPER_SL":
        return True
    try:
        kite.cancel_order(kite.VARIETY_REGULAR, order_id)
        logger.info("[TRADE] SL-M cancelled: " + order_id)
        return True
    except Exception as e:
        logger.warning("[TRADE] SL-M cancel: " + str(e))
        return False


def modify_sl_order(kite, order_id: str, new_trigger: float) -> bool:
    """Update SL trigger when floors lock higher."""
    if D.PAPER_MODE or not order_id or order_id == "PAPER_SL":
        return True
    try:
        kite.modify_order(
            variety       = kite.VARIETY_REGULAR,
            order_id      = order_id,
            trigger_price = round(new_trigger, 1),
        )
        logger.info("[TRADE] SL-M modified: trigger=₹" + str(round(new_trigger, 1)))
        return True
    except Exception as e:
        logger.warning("[TRADE] SL-M modify: " + str(e))
        return False
