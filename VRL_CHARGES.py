#!/home/user/kite_env/bin/python3
# ═══════════════════════════════════════════════════════════════
#  VRL_CHARGES.py — VISHAL RAJPUT TRADE v13.1
#  Brokerage & charges calculator. Pure math, no API calls.
#  Zerodha F&O charges as of April 2026.
# ═══════════════════════════════════════════════════════════════

BROKERAGE_PER_ORDER = 20.0
STT_SELL_PCT = 0.000625           # 0.0625% on sell side
EXCHANGE_NSE_PCT = 0.000530       # 0.053% NSE F&O transaction
SEBI_TURNOVER_PCT = 0.000001      # ₹1 per crore
STAMP_DUTY_BUY_PCT = 0.00003     # 0.003% on buy side
GST_PCT = 0.18                    # 18% on (brokerage + exchange)


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
                          lot_size: int = 65) -> dict:
    return calculate_charges(entry_price, exit_price, lot_size, num_exit_orders=1)


def calculate_split_charges(entry_price: float,
                            exit1_price: float, exit2_price: float,
                            lot_size: int = 65) -> dict:
    lot1 = calculate_charges(entry_price, exit1_price, lot_size, num_exit_orders=1)
    lot2 = calculate_charges(entry_price, exit2_price, lot_size, num_exit_orders=1)
    # Correct: 1 entry + 2 exits = 3 orders, not 4
    lot2["brokerage"] = round(lot2["brokerage"] - BROKERAGE_PER_ORDER, 2)
    lot2["gst"] = round((lot2["brokerage"] + lot2["exchange"]) * GST_PCT, 2)
    lot2["total_charges"] = round(lot2["brokerage"] + lot2["stt"] + lot2["exchange"]
                                   + lot2["sebi"] + lot2["stamp"] + lot2["gst"], 2)
    lot2["net_pnl"] = round(lot2["gross_pnl"] - lot2["total_charges"], 2)
    lot2["num_orders"] = 1
    return {
        "lot1": lot1, "lot2": lot2,
        "gross_pnl": round(lot1["gross_pnl"] + lot2["gross_pnl"], 2),
        "total_charges": round(lot1["total_charges"] + lot2["total_charges"], 2),
        "net_pnl": round(lot1["net_pnl"] + lot2["net_pnl"], 2),
        "total_brokerage": round(lot1["brokerage"] + lot2["brokerage"], 2),
        "num_orders": 3,
    }
