#!/home/user/kite_env/bin/python3
# ═══════════════════════════════════════════════════════════════
#  VRL_CHARGES.py — VISHAL RAJPUT TRADE v15.2.5
#  Brokerage & charges calculator. Pure math, no API calls.
#  Zerodha F&O charges as of April 2026.
#
#  BUG-K (v15.2.5 Batch 5): lot_size is no longer a module-load
#  constant. calculate_lot_charges()
#  now look it up from VRL_DATA at CALL TIME when the caller
#  doesn't pass an explicit value. This lets a mid-session lot-size
#  change (Zerodha has historically adjusted NIFTY lots) flow
#  through without a code edit or restart.
# ═══════════════════════════════════════════════════════════════

BROKERAGE_PER_ORDER = 20.0
STT_SELL_PCT = 0.000625           # 0.0625% on sell side
EXCHANGE_NSE_PCT = 0.000530       # 0.053% NSE F&O transaction
SEBI_TURNOVER_PCT = 0.000001      # ₹1 per crore
STAMP_DUTY_BUY_PCT = 0.00003     # 0.003% on buy side
GST_PCT = 0.18                    # 18% on (brokerage + exchange)


def _live_lot_size() -> int:
    """Runtime lookup of the active NIFTY lot size. Re-read on every
    call so a mid-session broker adjustment surfaces without a
    restart. Falls back to the historical default 65 only if
    VRL_DATA is somehow unavailable (e.g. unit test that imports
    CHARGES in isolation)."""
    try:
        import VRL_DATA
        lot = int(getattr(VRL_DATA, "LOT_SIZE", 0) or 0)
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
    """BUG-K: lot_size defaults to live VRL_DATA.LOT_SIZE when None,
    so the broker's current lot value flows through on every call
    instead of being frozen at module import."""
    if lot_size is None:
        lot_size = _live_lot_size()
    return calculate_charges(entry_price, exit_price, lot_size, num_exit_orders=1)
