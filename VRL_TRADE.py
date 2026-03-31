# ═══════════════════════════════════════════════════════════════
#  VRL_TRADE.py — VISHAL RAJPUT TRADE v12.15.1
#  Sealed order execution machine.
#  ONLY file that touches Kite orders.
#  Paper mode: simulated fills. Live mode: real orders + verification.
#  If Telegram/lab/reports crash, this keeps running.
#  Merged from: VRL_FLOW
# ═══════════════════════════════════════════════════════════════

import time
import logging
from datetime import datetime

import VRL_DATA as D

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
    """
    Poll order history until filled or timeout.
    Returns (fill_price: float, fill_qty: int).
    Returns (0.0, 0) if not filled within timeout.
    """
    deadline = time.time() + timeout_secs
    while time.time() < deadline:
        try:
            history = kite.order_history(order_id)
            if not history:
                time.sleep(0.5)
                continue
            last   = history[-1]
            status = last.get("status", "")
            if status == "COMPLETE":
                price = float(last.get("average_price", 0))
                qty   = int(last.get("filled_quantity", 0))
                return price, qty
            elif status in ("REJECTED", "CANCELLED"):
                logger.error("[TRADE] Order " + order_id
                             + " status=" + status
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
    Place BUY order.
    Paper mode: simulated fill at entry_price_ref.
    Live mode:  MARKET order → verify fill → handle partial.

    Returns:
        {"ok": bool, "fill_price": float, "fill_qty": int,
         "order_id": str, "error": str}
    """
    if D.PAPER_MODE:
        logger.info("[TRADE] PAPER ENTRY: " + symbol
                    + " qty=" + str(qty)
                    + " ref=" + str(round(entry_price_ref, 2)))
        return {
            "ok"        : True,
            "fill_price": round(entry_price_ref, 2),
            "fill_qty"  : qty,
            "order_id"  : "PAPER_" + datetime.now().strftime("%H%M%S%f")[:12],
            "error"     : "",
        }

    try:
        order_id = kite.place_order(
            variety          = kite.VARIETY_REGULAR,
            exchange         = D.EXCHANGE_NFO,
            tradingsymbol    = symbol,
            transaction_type = kite.TRANSACTION_TYPE_BUY,
            quantity         = qty,
            order_type       = kite.ORDER_TYPE_MARKET,
            product          = kite.PRODUCT_MIS,
        )
        logger.info("[TRADE] LIVE ENTRY placed: " + str(order_id))

        fill_price, fill_qty = verify_order_fill(kite, order_id)

        if fill_qty == 0:
            logger.error("[TRADE] Entry not filled: " + str(order_id))
            return {"ok": False, "fill_price": 0.0, "fill_qty": 0,
                    "order_id": str(order_id), "error": "Not filled"}

        if fill_qty < qty:
            # Accept partial fill as-is — do NOT cancel a partially filled MARKET order.
            # Broker may reject cancel on already-filled qty and leave state inconsistent.
            # Trade with whatever qty was filled; risk is proportionally smaller.
            logger.warning("[TRADE] Partial fill accepted: " + str(fill_qty) + "/" + str(qty)
                           + " — trading with partial qty")

        return {
            "ok"        : True,
            "fill_price": fill_price,
            "fill_qty"  : fill_qty,
            "order_id"  : str(order_id),
            "error"     : "",
        }

    except Exception as e:
        logger.error("[TRADE] Entry order error: " + str(e))
        return {"ok": False, "fill_price": 0.0, "fill_qty": 0,
                "order_id": "", "error": str(e)}


# ─── EXIT ORDER ───────────────────────────────────────────────

def place_exit(kite, symbol: str, token: int,
               option_type: str, qty: int,
               exit_price_ref: float, reason: str) -> dict:
    """
    Place SELL order to close position.
    Paper mode: simulated fill.
    Live mode:  MARKET order with 1 retry. If both fail → CRITICAL.
    On CRITICAL: do NOT reset state — position still open in broker.
    """
    if D.PAPER_MODE:
        logger.info("[TRADE] PAPER EXIT: " + symbol
                    + " qty=" + str(qty)
                    + " ref=" + str(round(exit_price_ref, 2))
                    + " reason=" + reason)
        return {
            "ok"        : True,
            "fill_price": round(exit_price_ref, 2),
            "fill_qty"  : qty,
            "order_id"  : "PAPER_" + datetime.now().strftime("%H%M%S%f")[:12],
            "error"     : "",
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
            )
            logger.info("[TRADE] LIVE EXIT placed attempt=" + str(attempt + 1)
                        + " order=" + str(order_id))

            fill_price, fill_qty = verify_order_fill(kite, order_id)

            if fill_qty > 0:
                return {
                    "ok"        : True,
                    "fill_price": fill_price,
                    "fill_qty"  : fill_qty,
                    "order_id"  : str(order_id),
                    "error"     : "",
                }

            logger.warning("[TRADE] Exit attempt " + str(attempt + 1) + " not filled")
            time.sleep(1)

        except Exception as e:
            logger.error("[TRADE] Exit error attempt=" + str(attempt + 1) + ": " + str(e))
            time.sleep(1)

    # Both attempts failed — manual intervention required
    msg = ("CRITICAL: Exit failed for " + symbol
           + " qty=" + str(qty) + ". MANUAL ACTION REQUIRED.")
    logger.critical(msg)
    return {"ok": False, "fill_price": 0.0, "fill_qty": 0,
            "order_id": "", "error": "EXIT_FAILED_MANUAL_REQUIRED"}
