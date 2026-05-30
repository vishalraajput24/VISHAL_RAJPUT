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
    """Refresh current price + status for every LIVE pick.

    Two correctness rules:
      * Per-ROW (per weekly cohort): a symbol recurs across weeks (the tracker
        appends a new cohort every Sunday), so each cohort's return is computed
        against its OWN entry price — not the first cohort's.
      * T1 (1Y, +25%) is a MILESTONE, not an exit: keep tracking the pick toward
        the 3Y target. Only SL-HIT or T3-HIT are terminal (stop updating).
    """
    if not os.path.exists(TRACKER_FILE):
        print("No tracker file found. Run screener first.")
        return

    df = pd.read_csv(TRACKER_FILE)

    # A fresh screener cohort has no tracking columns yet; .at won't create them.
    for _c in ("current_price", "current_return_%", "last_updated"):
        if _c not in df.columns:
            df[_c] = pd.NA
    df["last_updated"] = df["last_updated"].astype("object")  # holds date strings

    def _terminal(s):
        s = str(s)
        return ("SL-HIT" in s) or ("T3-HIT" in s)

    live_idx = df.index[~df["status"].map(_terminal)]
    if len(live_idx) == 0:
        print("No live positions to update.")
        return

    syms = list(dict.fromkeys(df.loc[live_idx, "symbol"]))   # unique, order-preserving
    print(f"Updating {len(live_idx)} live position(s) across {len(syms)} symbol(s)...\n")

    # Fetch each symbol's price ONCE so duplicate cohorts don't double-scrape.
    price_cache = {}
    for sym in syms:
        print(f"  {sym:<15}", end="", flush=True)
        p = get_current_price(sym)
        price_cache[sym] = p
        print(f"{Fore.YELLOW}price not found{Style.RESET_ALL}" if p is None
              else f"{Fore.GREEN}₹{p}{Style.RESET_ALL}")
        time.sleep(2)

    updated = 0
    for idx in live_idx:                       # one row = one cohort entry
        sym   = df.at[idx, "symbol"]
        price = price_cache.get(sym)
        try:
            entry = float(df.at[idx, "entry_price"]); sl = float(df.at[idx, "sl"])
            t1    = float(df.at[idx, "target_1y"]);   t3 = float(df.at[idx, "target_3y"])
        except (TypeError, ValueError):
            continue
        if price is None or entry <= 0:
            continue

        chg    = round((price / entry - 1) * 100, 1)        # vs THIS cohort's entry
        hit_t1 = str(df.at[idx, "status"]).startswith("T1")  # already past T1 milestone

        if price <= sl:
            new_status = "SL-HIT ❌"                         # terminal — exit
        elif price >= t3:
            new_status = "T3-HIT 🔥"                         # terminal — full multibagger
        elif price >= t1 or hit_t1:
            new_status = f"T1✅ ({chg:+.1f}%)"               # milestone — keep riding to T3
        else:
            new_status = f"OPEN ({chg:+.1f}%)"

        df.at[idx, "current_price"]    = price
        df.at[idx, "current_return_%"] = chg
        df.at[idx, "last_updated"]     = date.today().isoformat()
        df.at[idx, "status"]           = new_status
        updated += 1

    df.to_csv(TRACKER_FILE, index=False)
    print(f"\n{Fore.GREEN}✅ Tracker updated — {updated} live row(s): {TRACKER_FILE}{Style.RESET_ALL}")


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
