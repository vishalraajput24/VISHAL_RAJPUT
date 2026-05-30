"""
=============================================================================
WEEKLY TRACKER REPORT — Check how screener picks are performing
=============================================================================
Run this every Sunday AFTER the screener to see:
- Previous picks vs current price
- SL hit? Target hit? Still open?
- Running P&L of screener recommendations

Usage:
    python weekly_report.py          ← full report
    python weekly_report.py --update ← fetch current prices and update tracker
=============================================================================
"""

import os, sys, time
import requests
import pandas as pd
from bs4 import BeautifulSoup
from colorama import Fore, Style, init
from datetime import date, datetime

init(autoreset=True)

BASE_DIR     = os.path.dirname(os.path.abspath(__file__))
TRACKER_FILE = os.path.join(BASE_DIR, "weekly_tracker.csv")


# =============================================================================
# 📈  FETCH CURRENT PRICE FROM SCREENER.IN
# =============================================================================

def get_current_price(symbol):
    """Fetch current price for a symbol from Screener.in."""
    try:
        url  = f"https://www.screener.in/company/{symbol}/consolidated/"
        resp = requests.get(url,
            headers={"User-Agent": "Mozilla/5.0"}, timeout=10)
        soup = BeautifulSoup(resp.text, "html.parser")
        for li in soup.select("#top-ratios li"):
            n = li.find("span", class_="name")
            v = li.find("span", class_="number")
            if n and v and "Current Price" in n.text:
                return float(v.text.strip().replace(",","").replace("₹",""))
    except:
        pass
    return None


# =============================================================================
# 📊  UPDATE TRACKER WITH CURRENT PRICES
# =============================================================================

def update_prices():
    """Fetch current price for all OPEN picks and update status."""
    if not os.path.exists(TRACKER_FILE):
        print("No tracker file found. Run screener first.")
        return

    df      = pd.read_csv(TRACKER_FILE)
    open_df = df[df["status"].str.startswith("OPEN", na=False)]
    unique  = open_df["symbol"].unique()

    print(f"Updating prices for {len(unique)} open positions...\n")

    for sym in unique:
        print(f"  {sym:<15}", end="", flush=True)
        price = get_current_price(sym)
        if price is None:
            print(f"{Fore.YELLOW}price not found{Style.RESET_ALL}")
            time.sleep(1)
            continue

        # Get all rows for this symbol
        mask        = (df["symbol"] == sym) & (df["status"].str.startswith("OPEN", na=False))
        entry_price = df.loc[mask, "entry_price"].values[0]
        sl          = df.loc[mask, "sl"].values[0]
        t1          = df.loc[mask, "target_1y"].values[0]
        t3          = df.loc[mask, "target_3y"].values[0]

        chg = round((price / entry_price - 1) * 100, 1)

        # Determine status
        if price <= sl:
            new_status = "SL-HIT ❌"
        elif price >= t3:
            new_status = "T3-HIT 🔥"
        elif price >= t1:
            new_status = "T1-HIT ✅"
        else:
            new_status = f"OPEN ({chg:+.1f}%)"

        df.loc[mask, "current_price"]   = price
        df.loc[mask, "current_return_%"] = chg
        df.loc[mask, "last_updated"]    = date.today().isoformat()
        df.loc[mask, "status"]          = new_status

        color = Fore.GREEN if chg > 0 else Fore.RED
        print(f"{color}₹{price} ({chg:+.1f}%) → {new_status}{Style.RESET_ALL}")
        time.sleep(2)

    df.to_csv(TRACKER_FILE, index=False)
    print(f"\n{Fore.GREEN}✅ Tracker updated: {TRACKER_FILE}{Style.RESET_ALL}")


# =============================================================================
# 📋  SHOW FULL REPORT
# =============================================================================

def show_report():
    if not os.path.exists(TRACKER_FILE):
        print("No tracker data yet. Run the screener first.")
        return

    df    = pd.read_csv(TRACKER_FILE)
    weeks = sorted(df["date_added"].unique(), reverse=True)

    print(f"\n{Fore.CYAN}{'='*75}")
    print(f"  📊 MULTIBAGGER SCREENER — WEEKLY PERFORMANCE REPORT")
    print(f"  As of: {date.today().strftime('%d %b %Y')}  |  {len(weeks)} week(s) tracked")
    print(f"{'='*75}{Style.RESET_ALL}\n")

    total_picks = 0
    total_wins  = 0
    total_sl    = 0

    for week in weeks:
        wdf = df[df["date_added"] == week].copy()
        print(f"{Fore.YELLOW}📅 Week added: {week}  ({len(wdf)} picks){Style.RESET_ALL}")

        rows = []
        for _, r in wdf.iterrows():
            entry   = r.get("entry_price", "-")
            curr    = r.get("current_price", "-")
            ret_pct = r.get("current_return_%", "")
            sl      = r.get("sl", "-")
            t1      = r.get("target_1y", "-")
            t3      = r.get("target_3y", "-")
            status  = r.get("status", "OPEN")
            grade   = r.get("grade", "")

            # Color by status
            if "SL-HIT" in str(status):
                sc = Fore.RED
            elif "T" in str(status) and "HIT" in str(status):
                sc = Fore.GREEN
            elif isinstance(ret_pct, float) and ret_pct > 0:
                sc = Fore.GREEN
            elif isinstance(ret_pct, float) and ret_pct < 0:
                sc = Fore.RED
            else:
                sc = Fore.WHITE

            ret_str = f"{ret_pct:+.1f}%" if isinstance(ret_pct, (int, float)) else "-"
            rows.append([
                f"#{int(r['rank'])}",
                r["symbol"],
                f"₹{entry}",
                f"₹{curr}" if curr != "-" else "-",
                f"{sc}{ret_str}{Style.RESET_ALL}",
                f"₹{sl}",
                f"₹{t1}",
                f"₹{t3}",
                grade,
                str(status)[:20],
            ])

            total_picks += 1
            if "T" in str(status) and "HIT" in str(status): total_wins += 1
            if "SL-HIT" in str(status): total_sl += 1

        print(pd.DataFrame(rows, columns=[
            "#","Symbol","Entry","Current","Return%",
            "SL","T1(1Y)","T3(3Y)","Grade","Status"
        ]).to_string(index=False))
        print()

    # Overall scorecard
    closed  = total_wins + total_sl
    win_pct = round(total_wins / closed * 100, 1) if closed > 0 else "-"

    print(f"\n{Fore.CYAN}SCREENER SCORECARD{Style.RESET_ALL}")
    print(f"  Total picks tracked : {total_picks}")
    print(f"  Targets hit         : {total_wins}")
    print(f"  SL hit              : {total_sl}")
    print(f"  Still open          : {total_picks - closed}")
    print(f"  Win rate (closed)   : {win_pct}%")
    print()
    if closed >= 10:
        if isinstance(win_pct, float) and win_pct >= 60:
            print(f"{Fore.GREEN}✅ SCREENER IS WORKING — Win rate {win_pct}% over {closed} closed{Style.RESET_ALL}")
        elif isinstance(win_pct, float) and win_pct >= 40:
            print(f"{Fore.YELLOW}⚠️  SCREENER MARGINAL — Win rate {win_pct}% — review filters{Style.RESET_ALL}")
        else:
            print(f"{Fore.RED}❌ SCREENER UNDERPERFORMING — Win rate {win_pct}% — tighten filters{Style.RESET_ALL}")
    else:
        print(f"{Fore.YELLOW}📊 Need at least 10 closed trades to judge screener quality "
              f"({closed} closed / {total_picks} tracked, avg unrealized shown above){Style.RESET_ALL}")


# =============================================================================

if __name__ == "__main__":
    if "--update" in sys.argv:
        update_prices()
    show_report()
