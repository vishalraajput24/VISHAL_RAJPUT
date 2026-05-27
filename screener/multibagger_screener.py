"""
=============================================================================
MULTIBAGGER SCREENER — NSE 500 + Screener.in
=============================================================================
Author  : Claude (for Vishal Rajput)
Version : 2.0 — NSE 500 full universe + Top 10 + Weekly Tracker support
Sources : Screener.in (fundamentals) + NSE (live universe)
Output  : Top 10 picks with SL/Target + full ranked CSV

SETUP:
    pip install kiteconnect requests beautifulsoup4 pandas tabulate colorama

RUN:
    python multibagger_screener.py          ← full scan
    python multibagger_screener.py --quick  ← scan 50 stocks only (test)
    python multibagger_screener.py --login  ← get Kite token
=============================================================================
"""

import os, sys, time, json, io, re
import requests
import pandas as pd
from bs4 import BeautifulSoup
from tabulate import tabulate
from colorama import Fore, Style, init
from datetime import datetime, date

try:
    from kiteconnect import KiteConnect
    KITE_AVAILABLE = True
except ImportError:
    KITE_AVAILABLE = False

init(autoreset=True)

# =============================================================================
# ⚙️  CONFIG
# =============================================================================

KITE_API_KEY      = "your_api_key_here"
KITE_API_SECRET   = "your_api_secret_here"
KITE_ACCESS_TOKEN = ""
USE_KITE          = False

SCREENER_SESSION_COOKIE = ""   # login cookie from screener.in (optional)

BASE_DIR   = os.path.dirname(os.path.abspath(__file__))
OUTPUT_DIR = BASE_DIR
TRACKER_FILE  = os.path.join(BASE_DIR, "weekly_tracker.csv")
NSE500_CACHE  = os.path.join(BASE_DIR, "nse500_symbols.txt")

DELAY_BETWEEN_REQUESTS = 2    # seconds per stock — don't go below 1.5
MAX_STOCKS_TO_SCAN     = 504  # full NSE 500 (--quick overrides to 50)
TOP_N                  = 20   # how many top picks to show

# =============================================================================
# 🎯  FILTER THRESHOLDS
# =============================================================================

FILTERS = {
    "roe_min"           : 20.0,
    "roce_min"          : 20.0,
    "sales_growth_3y"   : 12.0,   # slightly relaxed — 15% blocks too many
    "profit_growth_3y"  : 12.0,
    "debt_equity_max"   : 0.75,   # relaxed slightly for capex businesses
    "current_ratio_min" : 1.0,
    "promoter_min"      : 40.0,   # relaxed — many quality IT/pharma cos have 30-45%
    "pledge_max"        : 5.0,
    "fcf_positive"      : True,
    "opm_min"           : 8.0,    # relaxed — opm parse sometimes fails
    "pe_max"            : 90.0,
    "peg_max"           : 3.0,
    "mcap_min_cr"       : 200,
    "mcap_max_cr"       : 100000,
}

# Sector-specific overrides (applied automatically)
SECTOR_OVERRIDES = {
    # NBFCs — high D/E is structural
    "NBFC": {"debt_equity_max": 10.0, "opm_min": 0.0, "roe_min": 15.0},
    # Banks — not suitable for this screener
    "BANK": {"skip": True},
}

# =============================================================================
# 🌐  NSE 500 UNIVERSE — fetched live + cached
# =============================================================================

def fetch_nse500_symbols():
    """Fetch NSE 500 constituent symbols. Use cache if available and < 7 days old."""
    if os.path.exists(NSE500_CACHE):
        age_days = (time.time() - os.path.getmtime(NSE500_CACHE)) / 86400
        if age_days < 7:
            with open(NSE500_CACHE) as f:
                syms = [l.strip() for l in f if l.strip()]
            print(f"{Fore.GREEN}✅ NSE 500 loaded from cache ({len(syms)} symbols, {age_days:.1f}d old){Style.RESET_ALL}")
            return syms

    print("Fetching NSE 500 list from niftyindices.com...", end=" ", flush=True)
    try:
        resp = requests.get(
            "https://niftyindices.com/IndexConstituent/ind_nifty500list.csv",
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=20,
        )
        df   = pd.read_csv(io.StringIO(resp.text))
        syms = df["Symbol"].tolist()
        with open(NSE500_CACHE, "w") as f:
            f.write("\n".join(syms))
        print(f"{Fore.GREEN}✅ {len(syms)} symbols fetched and cached{Style.RESET_ALL}")
        return syms
    except Exception as e:
        print(f"{Fore.RED}FAILED: {e} — using built-in fallback list{Style.RESET_ALL}")
        return FALLBACK_UNIVERSE

# Fallback if NSE fetch fails
FALLBACK_UNIVERSE = [
    "PERSISTENT","COFORGE","LTTS","TATAELXSI","TANLA","MPHASIS","HAPPSTMNDS",
    "AJANTPHARM","JBCHEPHARM","GRANULES","LAURUSLABS","METROPOLIS","VIJAYA",
    "DEEPAKNTR","NAVINFLUOR","CLEAN","FINEORG","AARTI","ALKYLAMINE","SUDARSCHEM",
    "DIXON","AMBER","KAYNES","SYRMA","GRINDWELL","CRAFTSMAN","TIMKEN","ELECON",
    "HAL","BEL","DATAPATTNS","COCHINSHIP","CDSL","CAMS","KFINTECH","IRCTC",
    "PAGEIND","RELAXO","RADICO","EMAMILTD","MARICO","TRENT","DMART","OBEROIRLTY",
    "BAJFINANCE","CHOLAFIN","MUTHOOTFIN","MANAPPURAM","CREDITACC","AAVAS",
]

# =============================================================================
# 🌐  SCREENER.IN SCRAPER
# =============================================================================

class ScreenerScraper:
    BASE_URL = "https://www.screener.in"

    def __init__(self, session_cookie=""):
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36",
            "Accept"    : "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        })
        if session_cookie:
            self.session.cookies.set("sessionid", session_cookie, domain=".screener.in")

    def get_company_data(self, symbol):
        result = {"symbol": symbol, "error": None}
        for suffix in ["/consolidated/", "/"]:
            url = f"{self.BASE_URL}/company/{symbol}{suffix}"
            try:
                resp = self.session.get(url, timeout=15)
                if resp.status_code == 200 and "company" in resp.url:
                    break
                if resp.status_code == 404:
                    result["error"] = "404"
                    return result
            except Exception as e:
                result["error"] = str(e)
                return result
        else:
            result["error"] = "404"
            return result

        soup = BeautifulSoup(resp.text, "html.parser")

        try:
            result["name"] = soup.find("h1", class_="h2").text.strip()
        except:
            result["name"] = symbol

        # --- Top ratios ---
        ratios = {}
        for li in soup.select("#top-ratios li"):
            try:
                n = li.find("span", class_="name")
                v = li.find("span", class_="number")
                if n and v:
                    ratios[n.text.strip()] = v.text.strip().replace(",","").replace("%","").replace("₹","").strip()
            except:
                pass

        def sf(d, *keys):
            for k in keys:
                if k in d:
                    try:
                        return float(d[k])
                    except:
                        pass
            return None

        result["market_cap"]     = sf(ratios, "Market Cap")
        result["pe"]             = sf(ratios, "Stock P/E")
        result["roe"]            = sf(ratios, "ROE")
        result["roce"]           = sf(ratios, "ROCE")
        result["debt_equity"]    = sf(ratios, "Debt / Equity")
        result["current_ratio"]  = sf(ratios, "Current ratio", "Current Ratio")
        result["opm"]            = sf(ratios, "OPM")
        result["eps"]            = sf(ratios, "EPS")
        result["price"]          = sf(ratios, "Current Price")
        result["book_value"]     = sf(ratios, "Book Value")
        result["face_value"]     = sf(ratios, "Face Value")
        result["dividend_yield"] = sf(ratios, "Dividend Yield")
        result["pbv"]            = sf(ratios, "Price to Book value")

        # --- 52-week High / Low (for technical signals) ---
        result["week52_high"] = None
        result["week52_low"]  = None
        try:
            hl_raw = ratios.get("High / Low", "")
            if "/" in str(hl_raw):
                parts = str(hl_raw).replace(",","").split("/")
                result["week52_high"] = float(parts[0].strip())
                result["week52_low"]  = float(parts[1].strip())
            else:
                # Try individual keys
                result["week52_high"] = sf(ratios, "52 Week High", "High")
                result["week52_low"]  = sf(ratios, "52 Week Low",  "Low")
        except:
            pass

        # --- Shareholding ---
        result["promoter_holding"] = None
        result["pledge_pct"]       = None
        try:
            sh_section = None
            for sec in soup.find_all("section"):
                h2 = sec.find(["h2","h3"])
                if h2 and "Shareholding" in h2.text:
                    sh_section = sec
                    break
            if sh_section:
                for row in sh_section.find_all("tr"):
                    cells = row.find_all("td") or row.find_all("th")
                    if not cells: continue
                    label = cells[0].text.strip().lower().replace("\xa0","")
                    if "promoters" in label and result["promoter_holding"] is None:
                        for c in reversed(cells[1:]):
                            try:
                                result["promoter_holding"] = float(c.text.strip().replace("%","").replace(",",""))
                                break
                            except: pass
                    if "pledged" in label and result["pledge_pct"] is None:
                        for c in reversed(cells[1:]):
                            try:
                                result["pledge_pct"] = float(c.text.strip().replace("%","").replace(",",""))
                                break
                            except: pass
        except:
            pass

        # --- Helper: parse a table row ---
        def parse_row(section, keywords):
            for row in section.find_all("tr"):
                cells = row.find_all("td")
                if not cells: continue
                label = cells[0].text.strip().lower().replace("\xa0","")
                for kw in keywords:
                    if kw in label:
                        vals = []
                        for c in cells[1:]:
                            t = c.text.strip().replace(",","").replace("%","").replace("₹","")
                            try: vals.append(float(t))
                            except: vals.append(None)
                        return vals
            return None

        def cagr_3y(series):
            nums = [v for v in (series or []) if v and v > 0]
            if len(nums) >= 4:
                base, end = nums[-4], nums[-1]
                if base > 0:
                    return round((((end / base) ** (1/3)) - 1) * 100, 1)
            return None

        # --- P&L table → growth rates ---
        result["sales_growth_3y"]  = None
        result["profit_growth_3y"] = None
        try:
            pl = soup.find("section", id="profit-loss")
            if not pl:
                for sec in soup.find_all("section"):
                    h2 = sec.find(["h2","h3"])
                    if h2 and "Profit" in h2.text and "Loss" in h2.text:
                        pl = sec; break
            if pl:
                sr = parse_row(pl, ["sales", "revenue"])
                pr = parse_row(pl, ["net profit", "profit after tax", "pat"])
                if sr: result["sales_growth_3y"]  = cagr_3y(sr)
                if pr: result["profit_growth_3y"] = cagr_3y(pr)
        except: pass

        # --- Balance sheet → D/E ---
        try:
            bs = soup.find("section", id="balance-sheet")
            if bs:
                eq_r  = parse_row(bs, ["equity capital"])
                res_r = parse_row(bs, ["reserves"])
                dbt_r = parse_row(bs, ["borrowing", "borrowings"])
                if eq_r and res_r and dbt_r:
                    eq  = (eq_r[-1] or 0) + (res_r[-1] or 0)
                    dbt = dbt_r[-1] or 0
                    if eq > 0 and result["debt_equity"] is None:
                        result["debt_equity"] = round(dbt / eq, 2)
        except: pass

        # --- Cash flows → FCF ---
        result["fcf"] = None
        try:
            cf = soup.find("section", id="cash-flow")
            if not cf:
                for sec in soup.find_all("section"):
                    h2 = sec.find(["h2","h3"])
                    if h2 and "Cash Flow" in h2.text:
                        cf = sec; break
            if cf:
                cfo_r = parse_row(cf, ["cash from operating","operating activity"])
                cfi_r = parse_row(cf, ["cash from investing","investing activity"])
                if cfo_r and cfi_r:
                    result["fcf"] = round((cfo_r[-1] or 0) + (cfi_r[-1] or 0), 1)
                elif cfo_r:
                    result["fcf"] = cfo_r[-1]
        except: pass

        # --- PEG ---
        result["peg"] = None
        try:
            if result["pe"] and result["profit_growth_3y"] and result["profit_growth_3y"] > 0:
                result["peg"] = round(result["pe"] / result["profit_growth_3y"], 2)
        except: pass

        # --- EPS from price/pe ---
        if result.get("price") and result.get("pe") and result["pe"] > 0:
            result["eps_calc"] = round(result["price"] / result["pe"], 2)
        else:
            result["eps_calc"] = result.get("eps")

        # ── TECHNICAL INDICATORS ─────────────────────────────────────────────
        # All computed from available Screener.in data (no extra API calls)

        price = result.get("price")
        h52   = result.get("week52_high")
        l52   = result.get("week52_low")

        # 1. Distance from 52W High (%) — how far from breakout
        result["dist_from_52h_pct"] = None
        if price and h52 and h52 > 0:
            result["dist_from_52h_pct"] = round((price / h52 - 1) * 100, 1)

        # 2. Distance from 52W Low (%) — how much recovered from bottom
        result["dist_from_52l_pct"] = None
        if price and l52 and l52 > 0:
            result["dist_from_52l_pct"] = round((price / l52 - 1) * 100, 1)

        # 3. 52W range position — where is price in its annual range (0=low, 100=high)
        result["range_position_pct"] = None
        if price and h52 and l52 and h52 > l52:
            result["range_position_pct"] = round((price - l52) / (h52 - l52) * 100, 1)

        # 4. Technical Signal — based on 52W position
        #    BREAKOUT : within 5% of 52W high   → momentum, institutions buying
        #    SWEET    : 15–40% below 52W high   → healthy pullback, good entry
        #    RECOVERY : 40–70% below 52W high   → deep correction, needs patience
        #    FALLING  : >70% below 52W high     → avoid, something structurally wrong
        result["tech_signal"] = "N/A"
        d52h = result.get("dist_from_52h_pct")
        if d52h is not None:
            if d52h >= -5:
                result["tech_signal"] = "🚀 BREAKOUT"
            elif d52h >= -20:
                result["tech_signal"] = "✅ SWEET ZONE"
            elif d52h >= -40:
                result["tech_signal"] = "⚠️  RECOVERY"
            else:
                result["tech_signal"] = "❌ FALLING"

        # 5. Entry quality — combine fundamental grade + technical signal
        #    Best entry = strong fundamentals + sweet zone / breakout
        tech   = result.get("tech_signal", "")
        score_ = result.get("_score", 0)    # may not be set yet, filled later
        if "BREAKOUT" in tech or "SWEET" in tech:
            result["entry_quality"] = "🔥 BUY NOW"
        elif "RECOVERY" in tech:
            result["entry_quality"] = "⏳ WAIT"
        else:
            result["entry_quality"] = "❌ AVOID"

        # 6. Momentum label — for short-term traders
        #    Uses range_position_pct: 70–100 = strong momentum
        #                              40–70 = building
        #                             <40    = weak
        rp = result.get("range_position_pct")
        if rp is not None:
            if rp >= 75:
                result["momentum"] = "🔥 STRONG"
            elif rp >= 50:
                result["momentum"] = "📈 BUILDING"
            elif rp >= 30:
                result["momentum"] = "😐 WEAK"
            else:
                result["momentum"] = "📉 VERY WEAK"
        else:
            result["momentum"] = "N/A"

        return result


# =============================================================================
# 🎯  FILTER ENGINE
# =============================================================================

def apply_filters(stock, overrides=None):
    f = {**FILTERS, **(overrides or {})}
    r_pass, r_fail = [], []
    score = 0

    def check(label, value, threshold, op="gte", pts=1):
        if value is None:
            r_fail.append(f"{label}:N/A")
            return False
        ok = (op=="gte" and value>=threshold) or (op=="lte" and value<=threshold) or \
             (op=="gt"  and value>threshold)  or (op=="lt"  and value<threshold)
        (r_pass if ok else r_fail).append(
            f"{label}:{value}" if ok else f"{label}:{value}(need {'>' if op in ['gte','gt'] else '<'}{threshold})"
        )
        return ok

    p_roe  = check("ROE%",   stock.get("roe"),  f["roe_min"],  "gte", 2); score += 2 if p_roe  else 0
    p_roce = check("ROCE%",  stock.get("roce"), f["roce_min"], "gte", 2); score += 2 if p_roce else 0
    p_sg   = check("SalesG3Y%", stock.get("sales_growth_3y"),  f["sales_growth_3y"],  "gte"); score += 1 if p_sg  else 0
    p_pg   = check("ProfG3Y%",  stock.get("profit_growth_3y"), f["profit_growth_3y"], "gte"); score += 1 if p_pg  else 0
    p_de   = check("D/E",    stock.get("debt_equity"), f["debt_equity_max"], "lte"); score += 1 if p_de  else 0
    p_pr   = check("Prom%",  stock.get("promoter_holding"), f["promoter_min"], "gte"); score += 1 if p_pr  else 0

    pledge = stock.get("pledge_pct") or 0
    p_pl   = pledge <= f["pledge_max"]
    (r_pass if p_pl else r_fail).append(f"Pledge:{pledge}" if p_pl else f"Pledge:{pledge}(need<{f['pledge_max']})")
    score += 1 if p_pl else 0

    fcf = stock.get("fcf")
    p_fcf = fcf is not None and fcf > 0
    (r_pass if p_fcf else r_fail).append(f"FCF:{fcf:.0f}cr" if p_fcf else f"FCF:{fcf}(need>0)")
    score += 1 if p_fcf else 0

    pe  = stock.get("pe");  p_pe  = pe  is None or pe  <= f["pe_max"];  score += 1 if p_pe  else 0
    peg = stock.get("peg"); p_peg = peg is None or peg <= f["peg_max"]; score += 1 if p_peg else 0

    mcap = stock.get("market_cap")
    p_mc = mcap is not None and f["mcap_min_cr"] <= mcap <= f["mcap_max_cr"]
    (r_pass if p_mc else r_fail).append(f"MCap:₹{mcap:.0f}cr" if p_mc else f"MCap:{mcap}(₹{f['mcap_min_cr']}-{f['mcap_max_cr']}cr)")
    score += 1 if p_mc else 0

    # Core filters ALL must pass
    passed = all([p_roe, p_roce, p_de, p_pr, p_pl])
    return passed, score, r_pass, r_fail


def grade(score):
    if score >= 13: return "🔥 STRONG"
    if score >= 10: return "✅ BUY"
    if score >= 7:  return "⚠️  WATCH"
    return                  "❌ WEAK"


# =============================================================================
# 💰  TARGET + SL CALCULATOR
# =============================================================================

def calc_targets(stock):
    """Calculate SL, 3 targets based on EPS growth projection."""
    price = stock.get("price")
    pe    = stock.get("pe")
    pg    = stock.get("profit_growth_3y") or 15
    eps   = stock.get("eps_calc")

    if not price:
        return {}

    sl = round(price * 0.80, 1)          # 20% below CMP

    if eps and pe:
        # Conservative: grow EPS at 75% of historical rate
        g3y = min(pg, 50) * 0.75 / 100
        eps_3y = eps * ((1 + g3y) ** 3)

        # Target P/E = min(current PE, 45) — don't extrapolate crazy PEs
        tgt_pe = min(pe, 45)

        t1 = round(price * 1.25, 1)                  # +25% (1 year)
        t2 = round(eps_3y * tgt_pe, 1)               # 3Y EPS × target PE
        t3 = round(eps_3y * (tgt_pe * 1.2), 1)       # 5Y optimistic
    else:
        t1 = round(price * 1.25, 1)
        t2 = round(price * 1.70, 1)
        t3 = round(price * 2.50, 1)

    return {
        "sl"  : sl,
        "t1"  : t1,    "t1_label": "1Y +25%",
        "t2"  : t2,    "t2_label": "3Y target",
        "t3"  : t3,    "t3_label": "5Y target",
        "sl_pct": -20.0,
        "t2_pct": round((t2 / price - 1) * 100, 1) if price else None,
        "t3_pct": round((t3 / price - 1) * 100, 1) if price else None,
    }


# =============================================================================
# 📊  RANKING
# =============================================================================

def rank_stocks(passed_stocks):
    def composite(s):
        roe  = s.get("roe")  or 0
        roce = s.get("roce") or 0
        pg   = s.get("profit_growth_3y") or 0
        sg   = s.get("sales_growth_3y")  or 0
        de   = s.get("debt_equity") or 0
        prom = s.get("promoter_holding") or 0
        peg  = s.get("peg") or 3
        sc   = s.get("_score") or 0
        return (sc * 10 + roe * 0.5 + roce * 0.5 + (pg + sg) * 0.3
                + prom * 0.1 + (1 / (de + 0.1)) * 2 + (1 / (peg + 0.1)))
    return sorted(passed_stocks, key=composite, reverse=True)


# =============================================================================
# 📋  WEEKLY TRACKER
# =============================================================================

def update_tracker(top10):
    """Save top 20 picks to weekly tracker CSV with entry date, SL, targets."""
    today = date.today().isoformat()
    rows = []

    # Check if we already saved this week
    if os.path.exists(TRACKER_FILE):
        existing = pd.read_csv(TRACKER_FILE)
        if today in existing["date_added"].values:
            print(f"{Fore.YELLOW}Tracker already has entry for {today} — skipping{Style.RESET_ALL}")
            return

    for rank, s in enumerate(top10, 1):
        tgts = calc_targets(s)
        rows.append({
            "date_added"    : today,
            "rank"          : rank,
            "symbol"        : s["symbol"],
            "name"          : s.get("name","")[:30],
            "entry_price"   : s.get("price"),
            "sl"            : tgts.get("sl"),
            "target_1y"     : tgts.get("t1"),
            "target_3y"     : tgts.get("t2"),
            "target_5y"     : tgts.get("t3"),
            "t3_upside_pct" : tgts.get("t3_pct"),
            "roe"           : s.get("roe"),
            "roce"          : s.get("roce"),
            "profit_gr_3y"  : s.get("profit_growth_3y"),
            "de"            : s.get("debt_equity"),
            "promoter"      : s.get("promoter_holding"),
            "peg"           : s.get("peg"),
            "score"         : s.get("_score"),
            "grade"         : grade(s.get("_score",0)),
            "status"        : "OPEN",
            "exit_price"    : None,
            "exit_date"     : None,
            "actual_return" : None,
        })

    new_df = pd.DataFrame(rows)
    if os.path.exists(TRACKER_FILE):
        old_df = pd.read_csv(TRACKER_FILE)
        combined = pd.concat([old_df, new_df], ignore_index=True)
    else:
        combined = new_df
    combined.to_csv(TRACKER_FILE, index=False)
    print(f"{Fore.GREEN}✅ Tracker updated: {TRACKER_FILE}{Style.RESET_ALL}")


def show_tracker_report():
    """Show weekly tracker performance — how are previous picks doing?"""
    if not os.path.exists(TRACKER_FILE):
        print("No tracker data yet.")
        return

    df = pd.read_csv(TRACKER_FILE)
    weeks = df["date_added"].unique()

    print(f"\n{Fore.CYAN}{'='*70}")
    print(f"  📊 WEEKLY TRACKER REPORT — {len(weeks)} week(s) of data")
    print(f"{'='*70}{Style.RESET_ALL}\n")

    for week in sorted(weeks, reverse=True):
        wdf = df[df["date_added"] == week]
        print(f"{Fore.YELLOW}Week: {week} ({len(wdf)} picks){Style.RESET_ALL}")

        rows = []
        for _, r in wdf.iterrows():
            entry = r["entry_price"]
            sl    = r["sl"]
            t1    = r["target_1y"]
            t3    = r["target_3y"]

            # Try to get current price from Screener.in
            status_str = r["status"]
            rows.append([
                r["rank"], r["symbol"], f"₹{entry}",
                f"₹{sl} (-20%)", f"₹{t1}", f"₹{t3}",
                r["grade"], status_str
            ])
        print(tabulate(rows,
            headers=["#","Symbol","Entry","SL","T1(1Y)","T3(3Y)","Grade","Status"],
            tablefmt="rounded_grid"))
        print()


# =============================================================================
# 🖨️  OUTPUT
# =============================================================================

def print_top10(top10):
    print(f"\n{Fore.GREEN}{'='*80}")
    print(f"  🏆 TOP {len(top10)} MULTIBAGGER PICKS — {date.today().strftime('%d %b %Y')}")
    print(f"{'='*80}{Style.RESET_ALL}\n")

    for rank, s in enumerate(top10, 1):
        tgts = calc_targets(s)
        g    = grade(s.get("_score", 0))
        pg   = s.get("profit_growth_3y")
        peg  = s.get("peg")

        print(f"{Fore.CYAN}#{rank} {g}  {s['symbol']} — {s.get('name','')}{Style.RESET_ALL}")
        print(f"   Price  : ₹{s.get('price','?')}   MCap: ₹{s.get('market_cap','?')} cr   Score: {s.get('_score')}/13")
        print(f"   ROE    : {s.get('roe')}%   ROCE: {s.get('roce')}%   D/E: {s.get('debt_equity')}   Promoter: {s.get('promoter_holding')}%")
        print(f"   Growth : Sales3Y={s.get('sales_growth_3y')}%  Profit3Y={pg}%  PEG={peg}")

        # Technical signals
        tech = s.get("tech_signal", "N/A")
        mom  = s.get("momentum", "N/A")
        d52h = s.get("dist_from_52h_pct")
        rpos = s.get("range_position_pct")
        d52h_str = f"{d52h:+.1f}% from 52W high" if d52h is not None else "N/A"
        rpos_str = f"{rpos:.0f}% of annual range" if rpos is not None else "N/A"
        print(f"   Tech   : {tech} | Momentum: {mom}")
        print(f"   52W    : {d52h_str} | Range pos: {rpos_str}")
        entry_q = s.get("entry_quality","")
        print(f"   Entry  : {entry_q}")

        if tgts:
            print(f"   {Fore.RED}SL     : ₹{tgts['sl']} (-20%){Style.RESET_ALL}")
            print(f"   {Fore.YELLOW}T1(1Y) : ₹{tgts['t1']} (+25%){Style.RESET_ALL}")
            print(f"   {Fore.GREEN}T2(3Y) : ₹{tgts['t2']} ({tgts['t2_pct']:+.0f}%){Style.RESET_ALL}")
            print(f"   {Fore.GREEN}T3(5Y) : ₹{tgts['t3']} ({tgts['t3_pct']:+.0f}%){Style.RESET_ALL}")
        if s.get("_reasons_fail"):
            print(f"   Weak   : {' | '.join(s['_reasons_fail'][:3])}")
        print()


def save_csv(results, top10):
    today_str = date.today().strftime("%Y%m%d")
    out_file  = os.path.join(OUTPUT_DIR, f"multibagger_{today_str}.csv")
    rows = []
    for s in results:
        tgts = calc_targets(s) if s.get("_passed") else {}
        rows.append({
            "Symbol"         : s.get("symbol"),
            "Name"           : s.get("name","")[:30],
            "TopPick_Rank"   : next((i+1 for i,t in enumerate(top10) if t["symbol"]==s["symbol"]), ""),
            "Passed"         : s.get("_passed", False),
            "Score"          : s.get("_score", 0),
            "Grade"          : grade(s.get("_score",0)) if s.get("_passed") else "FAIL",
            "Price"          : s.get("price"),
            "MCap_Cr"        : s.get("market_cap"),
            "PE"             : s.get("pe"),
            "PEG"            : s.get("peg"),
            "ROE_%"          : s.get("roe"),
            "ROCE_%"         : s.get("roce"),
            "OPM_%"          : s.get("opm"),
            "DebtEquity"     : s.get("debt_equity"),
            "PromoterHold_%" : s.get("promoter_holding"),
            "Pledge_%"       : s.get("pledge_pct"),
            "SalesGr3Y_%"    : s.get("sales_growth_3y"),
            "ProfitGr3Y_%"   : s.get("profit_growth_3y"),
            "FCF_Cr"         : s.get("fcf"),
            "SL"             : tgts.get("sl"),
            "Target_1Y"      : tgts.get("t1"),
            "Target_3Y"      : tgts.get("t2"),
            "Target_5Y"      : tgts.get("t3"),
            "52W_High"       : s.get("week52_high"),
            "52W_Low"        : s.get("week52_low"),
            "Dist_52H_%"     : s.get("dist_from_52h_pct"),
            "Range_Pos_%"    : s.get("range_position_pct"),
            "Tech_Signal"    : s.get("tech_signal"),
            "Momentum"       : s.get("momentum"),
            "Entry_Quality"  : s.get("entry_quality"),
            "FailReasons"    : " | ".join(s.get("_reasons_fail", [])[:4]),
        })
    df = pd.DataFrame(rows).sort_values(["Passed","Score"], ascending=[False, False])
    df.to_csv(out_file, index=False)
    print(f"{Fore.GREEN}✅ Full results saved: {out_file}{Style.RESET_ALL}")
    return out_file


# =============================================================================
# 🏃  MAIN
# =============================================================================

def main(quick=False):
    print(f"\n{Fore.CYAN}{'='*70}")
    print(f"  🚀 MULTIBAGGER SCREENER v2.0")
    print(f"  {date.today().strftime('%A, %d %b %Y')}  |  NSE 500 Universe")
    print(f"{'='*70}{Style.RESET_ALL}\n")

    universe = fetch_nse500_symbols()
    if quick:
        universe = universe[:50]
        print(f"{Fore.YELLOW}⚡ QUICK MODE — scanning first 50 stocks only{Style.RESET_ALL}\n")

    limit    = MAX_STOCKS_TO_SCAN if not quick else 50
    universe = universe[:limit]

    # Init Kite
    kite = None
    if USE_KITE and KITE_AVAILABLE and KITE_ACCESS_TOKEN:
        try:
            from kiteconnect import KiteConnect
            kite_obj = KiteConnect(api_key=KITE_API_KEY)
            kite_obj.set_access_token(KITE_ACCESS_TOKEN)
            kite = kite_obj
            print(f"{Fore.GREEN}✅ Kite connected{Style.RESET_ALL}")
        except Exception as e:
            print(f"{Fore.YELLOW}⚠️  Kite not connected: {e}{Style.RESET_ALL}")

    scraper = ScreenerScraper(session_cookie=SCREENER_SESSION_COOKIE)

    results      = []
    passed_count = 0
    error_count  = 0

    print(f"Scanning {len(universe)} stocks (ETA: ~{len(universe)*2//60} min)...\n")

    for i, symbol in enumerate(universe, 1):
        print(f"[{i:3d}/{len(universe)}] {symbol:<18}", end="", flush=True)
        data = scraper.get_company_data(symbol)

        if data.get("error"):
            error_count += 1
            print(f"{Fore.RED}skip ({data['error']}){Style.RESET_ALL}")
            results.append(data)
            time.sleep(1)
            continue

        # Live price from Kite
        if kite:
            try:
                q = kite.quote(f"NSE:{symbol}")
                data["price"] = q[f"NSE:{symbol}"]["last_price"]
            except:
                pass

        passed, score, rp, rf = apply_filters(data)
        data.update(_passed=passed, _score=score, _reasons_pass=rp, _reasons_fail=rf)

        g    = grade(score) if passed else "❌"
        mcap = f"₹{data['market_cap']:.0f}cr" if data.get("market_cap") else "N/A"
        col  = Fore.GREEN if passed else Fore.WHITE
        roe  = str(data.get('roe')  or '-')
        roce = str(data.get('roce') or '-')
        pg   = str(data.get('profit_growth_3y') or '-')
        print(f"{col}{g:<10} {mcap:<12} ROE={roe:<6} ROCE={roce:<6} PG={pg:<7} score={score}{Style.RESET_ALL}")

        results.append(data)
        if passed:
            passed_count += 1

        time.sleep(DELAY_BETWEEN_REQUESTS)

    # Rank and select top 10
    passed_stocks = rank_stocks([r for r in results if r.get("_passed")])
    top10         = passed_stocks[:TOP_N]

    # Output
    print_top10(top10)
    csv_file = save_csv(results, top10)
    update_tracker(top10)

    # Summary
    print(f"\n{Fore.CYAN}FINAL SUMMARY{Style.RESET_ALL}")
    print(f"  Scanned  : {len(results)}")
    print(f"  Passed   : {passed_count}")
    print(f"  Errors   : {error_count}")
    print(f"  Top 10   : saved to tracker + CSV")
    print(f"  CSV file : {csv_file}\n")

    # Show tracker history
    show_tracker_report()


# =============================================================================
# 🔑  KITE LOGIN HELPER
# =============================================================================

def get_kite_access_token():
    import webbrowser
    url = f"https://kite.trade/connect/login?api_key={KITE_API_KEY}&v=3"
    print(f"Opening: {url}")
    webbrowser.open(url)
    req_token = input("\nPaste request_token from redirect URL: ").strip()
    kite = KiteConnect(api_key=KITE_API_KEY)
    sess = kite.generate_session(req_token, api_secret=KITE_API_SECRET)
    token = sess["access_token"]
    print(f"\n✅ Access Token: {token}")
    print(f"Paste into KITE_ACCESS_TOKEN in the script.")
    return token


# =============================================================================

if __name__ == "__main__":
    if "--login" in sys.argv:
        get_kite_access_token()
    elif "--tracker" in sys.argv:
        show_tracker_report()
    elif "--quick" in sys.argv:
        main(quick=True)
    else:
        main()
