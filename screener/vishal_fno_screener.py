"""
=============================================================================
VISHAL F&O SCREENER — Top 5 Call/Put Setups with Precise Entry/SL/Target
=============================================================================
Strategy : OI + Trend Confluence (3-layer filter)
  Layer 1 : Technical  — EMA20, EMA50, RSI14, Volume spike (Daily chart)
  Layer 2 : OI Data    — PCR, Max Pain, OI buildup (Option chain)
  Layer 3 : Combined   — All signals must align for high conviction

Entry timing:
  → Confirm on 15min chart at 9:30 AM
  → Enter on 5min pullback between 9:30–10:30 AM
  → Hold 2–4 days (monthly expiry, avoid last 2 days theta crush)

Usage:
    python3 vishal_fno_screener.py          ← full scan (100 F&O stocks)
    python3 vishal_fno_screener.py --quick  ← quick scan (30 stocks)
    python3 vishal_fno_screener.py --help   ← show help

Output: Top 5 CALL + Top 5 PUT setups with precise levels
=============================================================================
"""

import os, sys, json, time, math
import pandas as pd
import numpy as np
from datetime import datetime, date, timedelta
try:
    from dotenv import load_dotenv
except ModuleNotFoundError:                 # python-dotenv optional — never hard-crash
    def load_dotenv(dotenv_path=None, **_kw):
        _p = os.path.expanduser(dotenv_path or "~/.env")
        if not os.path.isfile(_p):
            return False
        with open(_p) as _fh:
            for _ln in _fh:
                _ln = _ln.strip()
                if not _ln or _ln.startswith("#") or "=" not in _ln:
                    continue
                _k, _, _v = _ln.partition("=")
                os.environ.setdefault(_k.strip(), _v.strip().strip('"').strip("'"))
        return True
from kiteconnect import KiteConnect
from colorama import Fore, Style, init
import warnings
warnings.filterwarnings("ignore")

import fno_strategy as FS   # single source of truth: gate, regime, structure, caps

init(autoreset=True)

# ── Load credentials ───────────────────────────────────────────────────────
load_dotenv(os.path.expanduser("~/.env"))
API_KEY    = os.getenv("KITE_API_KEY", "")
API_SECRET = os.getenv("KITE_API_SECRET", "")

BASE_DIR       = os.path.dirname(os.path.abspath(__file__))
TOKEN_FILE     = os.path.join(os.path.expanduser("~"), "VISHAL_RAJPUT", "state", "access_token.json")
INST_CACHE_NSE = os.path.join(BASE_DIR, "inst_cache_nse.csv")
INST_CACHE_NFO = os.path.join(BASE_DIR, "inst_cache_nfo.csv")

# ── F&O Universe — Curated liquid stocks only ─────────────────────────────
# Selection criteria:
#   ✅ Strike count ≥ 35 in NFO (proxy for market maker interest)
#   ✅ Large/mid cap with active retail + institutional participation
#   ✅ Tight bid-ask spread in real trading
#   ❌ Removed: NYKAA, PAYTM, POLICYBZR, OBEROIRLTY, NHPC, RVNL, IRFC,
#               CROMPTON, KEI, BERGERPAINTS, BIOCON, TORNTPHARM, NMDC,
#               SAIL, ADANIGREEN (erratic / thin options)
#
# Total: 120 stocks across 12 sectors
# ──────────────────────────────────────────────────────────────────────────

FNO_UNIVERSE = [
    # ── BANKS (most liquid F&O segment) ───────────────────────────────────
    "HDFCBANK",    # #1 most liquid stock F&O
    "ICICIBANK",   # top 3 always
    "KOTAKBANK",
    "AXISBANK",
    "SBIN",        # PSU bank, huge retail interest
    "INDUSINDBK",
    "BANDHANBNK",
    "FEDERALBNK",
    "PNB",
    "BANKBARODA",
    "CANBK",
    "AUBANK",      # AU Small Finance — good OI
    "IDFCFIRSTB",  # IDFC First Bank

    # ── FINANCE / NBFC / INSURANCE ────────────────────────────────────────
    "BAJFINANCE",
    "BAJAJFINSV",
    "CHOLAFIN",
    "LICHSGFIN",
    "MUTHOOTFIN",
    "SHRIRAMFIN",  # ✅ NEW — Shriram Finance, 43 strikes, large NBFC
    "HDFCLIFE",
    "SBILIFE",
    "ICICIGI",
    "LICI",        # ✅ NEW — LIC India, massive MCap, 54 strikes
    "HDFCAMC",     # ✅ NEW — HDFC AMC, 49 strikes
    "JIOFIN",      # ✅ NEW — Jio Financial, 39 strikes, Reliance backed

    # ── IT / TECHNOLOGY ───────────────────────────────────────────────────
    "TCS",
    "INFY",
    "WIPRO",
    "HCLTECH",
    "TECHM",
    "LTM",         # LTIMindtree — correct Kite symbol
    "MPHASIS",
    "PERSISTENT",
    "COFORGE",
    "OFSS",        # ✅ NEW — Oracle Financial Services, 81 strikes, very liquid
    "KPITTECH",    # ✅ NEW — KPIT Technologies, auto-tech software

    # ── AUTO & AUTO ANCILLARY ─────────────────────────────────────────────
    "MARUTI",
    # TATAMOTORS — unavailable in Kite NSE/NFO (check later)
    "M&M",
    "BAJAJ-AUTO",
    "HEROMOTOCO",
    "EICHERMOT",
    "TVSMOTOR",
    "ASHOKLEY",
    "MOTHERSON",   # ✅ NEW — Samvardhana Motherson, 58 strikes, auto ancillary
    "BOSCHLTD",    # ✅ NEW — Bosch India, 54 strikes

    # ── OIL & GAS ─────────────────────────────────────────────────────────
    "RELIANCE",
    "ONGC",
    "BPCL",
    "IOC",
    "GAIL",
    "HINDPETRO",
    "PETRONET",    # ✅ NEW — Petronet LNG, 42 strikes

    # ── METALS & MINING ───────────────────────────────────────────────────
    "TATASTEEL",
    "JSWSTEEL",
    "HINDALCO",
    "VEDL",
    "HINDZINC",    # ✅ NEW — Hindustan Zinc, 33 strikes, Vedanta subsidiary
    "JINDALSTEL",  # ✅ NEW — Jindal Steel & Power, 44 strikes

    # ── PHARMA & HEALTHCARE ───────────────────────────────────────────────
    "SUNPHARMA",
    "DRREDDY",
    "CIPLA",
    "DIVISLAB",
    "AUROPHARMA",
    "LUPIN",
    "MANKIND",     # ✅ NEW — Mankind Pharma, 64 strikes, large cap
    "APOLLOHOSP",  # ✅ NEW — Apollo Hospitals, 58 strikes
    "MAXHEALTH",   # ✅ NEW — Max Healthcare, 44 strikes
    "FORTIS",      # ✅ NEW — Fortis Healthcare, 30 strikes
    "ZYDUSLIFE",   # ✅ NEW — Zydus Life Sciences, 47 strikes

    # ── FMCG & CONSUMER ───────────────────────────────────────────────────
    "HINDUNILVR",
    "ITC",
    "NESTLEIND",
    "BRITANNIA",
    "DABUR",
    "GODREJCP",
    "MARICO",
    "TATACONSUM",
    "COLPAL",      # ✅ NEW — Colgate-Palmolive, 43 strikes
    "VBL",         # ✅ NEW — Varun Beverages (Pepsi bottler), 41 strikes
    "UNITDSPR",    # ✅ NEW — United Spirits, 51 strikes

    # ── CAPITAL GOODS / INDUSTRIALS ───────────────────────────────────────
    "LT",
    "ABB",
    "SIEMENS",
    "BHEL",
    "HAVELLS",
    "POLYCAB",
    "CGPOWER",     # ✅ NEW — CG Power & Industrial, 36 strikes, transformers
    "DIXON",       # ✅ NEW — Dixon Technologies (electronics mfg), 55 strikes
    "CONCOR",      # ✅ NEW — Container Corp (logistics), 77 strikes

    # ── POWER ─────────────────────────────────────────────────────────────
    "NTPC",
    "POWERGRID",
    "TATAPOWER",
    "ADANIPOWER",
    "PFC",         # ✅ NEW — Power Finance Corp, 35 strikes
    "RECLTD",      # ✅ NEW — REC Ltd, 32 strikes
    "JSWENERGY",   # ✅ NEW — JSW Energy, 47 strikes

    # ── TELECOM ───────────────────────────────────────────────────────────
    "BHARTIARTL",

    # ── CONSUMER / RETAIL / LIFESTYLE ─────────────────────────────────────
    "TITAN",
    "ASIANPAINT",
    "PIDILITIND",
    "TRENT",       # ✅ NEW — Tata Trent (Zudio/Westside), 37 strikes, hot stock
    "DMART",       # ✅ NEW — Avenue Supermarts, 33 strikes
    "JUBLFOOD",    # ✅ NEW — Jubilant FoodWorks (Domino's), 44 strikes
    "INDIGO",      # ✅ NEW — IndiGo Airlines, 41 strikes

    # ── CEMENT ────────────────────────────────────────────────────────────
    "ULTRACEMCO",
    "GRASIM",
    "AMBUJACEM",   # ✅ NEW — Ambuja Cements, 30 strikes

    # ── CONGLOMERATE / ADANI ──────────────────────────────────────────────
    "ADANIENT",
    "ADANIPORTS",

    # ── REAL ESTATE ───────────────────────────────────────────────────────
    "DLF",
    "GODREJPROP",
    "LODHA",       # ✅ NEW — Macrotech (Lodha), 41 strikes

    # ── EXCHANGES & FINANCIAL SERVICES ────────────────────────────────────
    "MCX",         # ✅ NEW — Multi Commodity Exchange, 33 strikes
    "BSE",         # ✅ NEW — BSE Ltd, 45 strikes

    # ── NEW-AGE / INTERNET ────────────────────────────────────────────────
    "ETERNAL",     # Zomato renamed to Eternal — correct Kite symbol, 40 strikes

    # ── DEFENCE / PSU ─────────────────────────────────────────────────────
    "HAL",
    "BEL",
    "COALINDIA",
    "MAZDOCK",     # ✅ NEW — Mazagon Dock (defence shipyard), 56 strikes
]
FNO_UNIVERSE = list(dict.fromkeys(FNO_UNIVERSE))   # safety dedup


# =============================================================================
# 🔐  KITE AUTH
# =============================================================================

def get_kite():
    """Load saved access token and return authenticated KiteConnect instance."""
    if not API_KEY:
        print(f"{Fore.RED}KITE_API_KEY not set. Check ~/.env{Style.RESET_ALL}")
        sys.exit(1)

    kite = KiteConnect(api_key=API_KEY)

    if not os.path.exists(TOKEN_FILE):
        print(f"{Fore.RED}No access token file found at {TOKEN_FILE}")
        print(f"Run VRL bot first to generate today's token.{Style.RESET_ALL}")
        sys.exit(1)

    with open(TOKEN_FILE) as f:
        saved = json.load(f)

    token_date = saved.get("date", "")
    if token_date != date.today().isoformat():
        print(f"{Fore.RED}Access token expired (saved: {token_date}, today: {date.today()})")
        print(f"Run VRL bot to refresh the token.{Style.RESET_ALL}")
        sys.exit(1)

    kite.set_access_token(saved["access_token"])
    try:
        profile = kite.profile()
        print(f"{Fore.GREEN}✅ Kite connected — {profile['user_name']}{Style.RESET_ALL}")
    except Exception as e:
        print(f"{Fore.RED}Token invalid: {e}{Style.RESET_ALL}")
        sys.exit(1)

    return kite


# =============================================================================
# 📋  INSTRUMENT CACHE  (refresh daily — saves 2000+ rows download time)
# =============================================================================

def load_instruments(kite):
    """Load NSE + NFO instruments with daily file cache."""
    today = date.today().isoformat()

    def load_cached(path):
        if not os.path.exists(path):
            return None
        tmp = pd.read_csv(path, nrows=1)
        if "_date" in tmp.columns and str(tmp["_date"].iloc[0]) == today:
            return pd.read_csv(path)
        return None

    # ── NSE ──
    nse_df = load_cached(INST_CACHE_NSE)
    if nse_df is None:
        print("  Downloading NSE instruments...", end="", flush=True)
        nse_df = pd.DataFrame(kite.instruments("NSE"))
        nse_df["_date"] = today
        nse_df.to_csv(INST_CACHE_NSE, index=False)
        print(f" {len(nse_df):,} instruments ✅")
    else:
        print(f"  NSE instruments: loaded from cache ({len(nse_df):,})")

    # ── NFO ──
    nfo_df = load_cached(INST_CACHE_NFO)
    if nfo_df is None:
        print("  Downloading NFO instruments...", end="", flush=True)
        nfo_df = pd.DataFrame(kite.instruments("NFO"))
        nfo_df["_date"] = today
        nfo_df.to_csv(INST_CACHE_NFO, index=False)
        print(f" {len(nfo_df):,} instruments ✅")
    else:
        print(f"  NFO instruments: loaded from cache ({len(nfo_df):,})")

    nfo_df["expiry"] = pd.to_datetime(nfo_df["expiry"])
    return nse_df, nfo_df


# =============================================================================
# 📊  TECHNICAL INDICATORS
# =============================================================================

def compute_rsi(series, period=14):
    delta    = series.diff()
    gain     = delta.clip(lower=0)
    loss     = -delta.clip(upper=0)
    avg_gain = gain.ewm(com=period - 1, min_periods=period).mean()
    avg_loss = loss.ewm(com=period - 1, min_periods=period).mean()
    rs       = avg_gain / avg_loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def compute_atr(df, period=14):
    hl  = df["high"] - df["low"]
    hcp = (df["high"] - df["close"].shift()).abs()
    lcp = (df["low"]  - df["close"].shift()).abs()
    tr  = pd.concat([hl, hcp, lcp], axis=1).max(axis=1)
    return tr.ewm(com=period - 1, min_periods=period).mean()


def get_technicals(kite, nse_df, symbol):
    """
    Fetch 100 days of daily OHLCV and compute:
    EMA20, EMA50, RSI14, ATR14, volume trend
    Returns dict or None on failure.
    """
    try:
        inst = nse_df[
            (nse_df["tradingsymbol"] == symbol) &
            (nse_df["instrument_type"] == "EQ")
        ]
        if inst.empty:
            return None

        token     = int(inst.iloc[0]["instrument_token"])
        to_date   = datetime.now()
        from_date = to_date - timedelta(days=130)   # ~90 trading days

        candles = kite.historical_data(token, from_date, to_date, "day")
        if len(candles) < 30:
            return None

        df            = pd.DataFrame(candles)
        df["ema20"]   = df["close"].ewm(span=20, adjust=False).mean()
        df["ema50"]   = df["close"].ewm(span=50, adjust=False).mean()
        df["rsi"]     = compute_rsi(df["close"])
        df["atr"]     = compute_atr(df)
        df["vol_avg"] = df["volume"].rolling(10).mean()

        last  = df.iloc[-1]
        prev  = df.iloc[-2]

        return {
            "symbol"      : symbol,
            "price"       : round(float(last["close"]), 2),
            "ema20"       : round(float(last["ema20"]), 2),
            "ema50"       : round(float(last["ema50"]), 2),
            "rsi"         : round(float(last["rsi"]), 1),
            "atr"         : round(float(last["atr"]), 2),
            "prev_close"  : float(prev["close"]),
            "volume"      : float(last["volume"]),
            "vol_avg"     : float(last["vol_avg"]) if not math.isnan(float(last["vol_avg"])) else 0,
        }
    except Exception:
        return None


def tech_score(tech):
    """
    Score stock technically.
    Returns (direction: CALL/PUT/NEUTRAL, score: int, signals: list[str])
    """
    price   = tech["price"]
    ema20   = tech["ema20"]
    ema50   = tech["ema50"]
    rsi     = tech["rsi"]
    vol     = tech["volume"]
    vol_avg = tech["vol_avg"]

    call_pts = 0
    put_pts  = 0
    signals  = []

    # ── EMA position ──────────────────────────────────────────────────────
    if price > ema20:
        call_pts += 1
        signals.append(f"Price>EMA20(₹{ema20:.0f}) ✅")
    else:
        put_pts += 1
        signals.append(f"Price<EMA20(₹{ema20:.0f}) 🔻")

    if price > ema50:
        call_pts += 1
        signals.append(f"Price>EMA50(₹{ema50:.0f}) ✅")
    else:
        put_pts += 1
        signals.append(f"Price<EMA50(₹{ema50:.0f}) 🔻")

    # ── EMA alignment (trend) ─────────────────────────────────────────────
    if ema20 > ema50 * 1.005:      # 20 clearly above 50
        call_pts += 1
        signals.append(f"EMA20>EMA50 — uptrend ✅")
    elif ema20 < ema50 * 0.995:    # 20 clearly below 50
        put_pts += 1
        signals.append(f"EMA20<EMA50 — downtrend 🔻")
    else:
        signals.append(f"EMA20≈EMA50 — sideways ⚠️")

    # ── RSI ───────────────────────────────────────────────────────────────
    if 55 <= rsi <= 75:
        call_pts += 2
        signals.append(f"RSI={rsi:.0f} — bullish momentum ✅")
    elif 25 <= rsi <= 45:
        put_pts += 2
        signals.append(f"RSI={rsi:.0f} — bearish momentum 🔻")
    elif rsi > 75:
        call_pts -= 1          # overbought — risk of reversal
        signals.append(f"RSI={rsi:.0f} — OVERBOUGHT, caution ⚠️")
    elif rsi < 25:
        put_pts -= 1           # oversold
        signals.append(f"RSI={rsi:.0f} — oversold, caution ⚠️")
    else:
        signals.append(f"RSI={rsi:.0f} — neutral zone")

    # ── Volume spike ──────────────────────────────────────────────────────
    if vol_avg > 0 and vol > vol_avg * 1.4:
        if price >= tech["prev_close"]:
            call_pts += 1
            signals.append(f"Volume spike + up (+{((vol/vol_avg)-1)*100:.0f}% avg) ✅")
        else:
            put_pts += 1
            signals.append(f"Volume spike + down (+{((vol/vol_avg)-1)*100:.0f}% avg) 🔻")

    # ── Decision ──────────────────────────────────────────────────────────
    if call_pts >= 3 and call_pts > put_pts:
        return "CALL", call_pts, signals
    elif put_pts >= 3 and put_pts > call_pts:
        return "PUT", put_pts, signals
    else:
        return "NEUTRAL", max(call_pts, put_pts), signals


# =============================================================================
# 📈  OPTION CHAIN — PCR, MAX PAIN, OI DATA
# =============================================================================

def get_nearest_expiry(nfo_df, symbol):
    """Get nearest upcoming expiry for the symbol (at least today or later)."""
    opts = nfo_df[
        (nfo_df["name"] == symbol) &
        (nfo_df["instrument_type"].isin(["CE", "PE"]))
    ]
    if opts.empty:
        return None

    today    = pd.Timestamp(date.today())
    expiries = opts["expiry"].dropna().unique()
    future   = sorted([e for e in expiries if pd.Timestamp(e) >= today])

    if not future:
        return None

    # Prefer expiry at least 3 days away (avoid expiry-day rush)
    for exp in future:
        if (pd.Timestamp(exp) - today).days >= 3:
            return pd.Timestamp(exp).date()

    return pd.Timestamp(future[0]).date()


def get_option_chain_data(kite, nfo_df, symbol, price, expiry):
    """
    Fetch option chain for ±8 strikes around ATM.
    Calculates: PCR, Max Pain, CE/PE OI distribution.
    Returns dict or None on failure.
    """
    try:
        opts = nfo_df[
            (nfo_df["name"] == symbol) &
            (nfo_df["instrument_type"].isin(["CE", "PE"])) &
            (nfo_df["expiry"].dt.date == expiry)
        ].copy()

        if opts.empty:
            return None

        opts["strike"] = opts["strike"].astype(float)
        lot_size       = int(opts.iloc[0]["lot_size"])

        # Find ATM and select ±8 strikes
        all_strikes = sorted(opts["strike"].unique())
        if not all_strikes:
            return None

        atm_idx  = min(range(len(all_strikes)), key=lambda i: abs(all_strikes[i] - price))
        lo       = max(0, atm_idx - 8)
        hi       = min(len(all_strikes) - 1, atm_idx + 8)
        selected = set(all_strikes[lo : hi + 1])
        atm      = all_strikes[atm_idx]

        opts = opts[opts["strike"].isin(selected)]

        # Build quote keys
        quote_keys = [f"NFO:{row['tradingsymbol']}" for _, row in opts.iterrows()]

        # Batch quote (max ~499 per call)
        quotes = {}
        for i in range(0, len(quote_keys), 499):
            batch = quote_keys[i : i + 499]
            q     = kite.quote(batch)
            quotes.update(q)
            if i + 499 < len(quote_keys):
                time.sleep(0.3)

        # Organize
        ce_data = {}
        pe_data = {}

        for _, row in opts.iterrows():
            key = f"NFO:{row['tradingsymbol']}"
            q   = quotes.get(key, {})
            oi  = int(q.get("oi", 0) or 0)
            ltp = float(q.get("last_price", 0) or 0)
            iv  = float(q.get("oi_day_high", 0) or 0)   # proxy for activity

            entry = {
                "oi"             : oi,
                "ltp"            : ltp,
                "strike"         : float(row["strike"]),
                "tradingsymbol"  : row["tradingsymbol"],
                "instrument_token": row["instrument_token"],
            }

            if row["instrument_type"] == "CE":
                ce_data[float(row["strike"])] = entry
            else:
                pe_data[float(row["strike"])] = entry

        if not ce_data and not pe_data:
            return None

        # ── PCR ────────────────────────────────────────────────────────────
        total_ce_oi = sum(v["oi"] for v in ce_data.values())
        total_pe_oi = sum(v["oi"] for v in pe_data.values())
        pcr = round(total_pe_oi / total_ce_oi, 2) if total_ce_oi > 0 else 1.0

        # ── Max Pain ───────────────────────────────────────────────────────
        # Strike where total option writer loss is minimum
        combo_strikes = sorted(set(list(ce_data.keys()) + list(pe_data.keys())))
        max_pain      = atm
        min_pain      = float("inf")

        for s in combo_strikes:
            pain = 0
            for k, v in ce_data.items():
                if s > k:
                    pain += (s - k) * v["oi"] * lot_size
            for k, v in pe_data.items():
                if s < k:
                    pain += (k - s) * v["oi"] * lot_size
            if pain < min_pain:
                min_pain  = pain
                max_pain  = s

        # ── Highest OI strike (resistance / support) ───────────────────────
        top_ce_strike = max(ce_data, key=lambda k: ce_data[k]["oi"], default=atm)
        top_pe_strike = max(pe_data, key=lambda k: pe_data[k]["oi"], default=atm)

        return {
            "ce"            : ce_data,
            "pe"            : pe_data,
            "pcr"           : pcr,
            "max_pain"      : max_pain,
            "atm"           : atm,
            "lot_size"      : lot_size,
            "expiry"        : expiry,
            "top_ce_strike" : top_ce_strike,   # key resistance (call writers here)
            "top_pe_strike" : top_pe_strike,   # key support (put writers here)
            "total_ce_oi"   : total_ce_oi,
            "total_pe_oi"   : total_pe_oi,
        }

    except Exception as e:
        return None


# =============================================================================
# 🎯  TRADE SETUP — Entry, SL, Targets (precise levels)
# =============================================================================

def get_trade_setup(tech, opt_chain, direction, tech_signals, tech_pts):
    """
    Combine technical + OI signals.
    Returns complete trade setup dict with entry, SL, targets.
    """
    if not tech or not opt_chain:
        return None

    price    = tech["price"]
    atr      = tech["atr"]
    pcr      = opt_chain["pcr"]
    max_pain = opt_chain["max_pain"]
    atm      = opt_chain["atm"]
    lot_size = opt_chain["lot_size"]
    expiry   = opt_chain["expiry"]

    opt_pts     = 0
    opt_signals = []

    # ── OI signals ────────────────────────────────────────────────────────
    if direction == "CALL":
        if pcr >= 1.2:
            opt_pts += 2
            opt_signals.append(f"PCR={pcr} — put writing dominant (bullish) ✅✅")
        elif pcr >= 1.0:
            opt_pts += 1
            opt_signals.append(f"PCR={pcr} — slight bullish ✅")
        elif pcr < 0.8:
            opt_pts -= 1
            opt_signals.append(f"PCR={pcr} — call writing dominant (bearish) ⚠️")
        else:
            opt_signals.append(f"PCR={pcr} — neutral")

        if price >= max_pain:
            opt_pts += 1
            opt_signals.append(f"Price(₹{price:.0f}) ≥ MaxPain(₹{max_pain:.0f}) — bullish pull ✅")
        else:
            opt_signals.append(f"Price(₹{price:.0f}) < MaxPain(₹{max_pain:.0f}) — may pull higher ⚠️")

        # Resistance level
        opt_signals.append(f"Key resistance (call writers): ₹{opt_chain['top_ce_strike']:.0f}")

        # Find best CE strike (ATM)
        strike      = atm
        option_data = opt_chain["ce"].get(strike)
        if option_data is None or option_data["ltp"] <= 0:
            # Try strikes around ATM
            for s in sorted(opt_chain["ce"].keys(), key=lambda x: abs(x - price)):
                if opt_chain["ce"][s]["ltp"] > 0:
                    strike      = s
                    option_data = opt_chain["ce"][s]
                    break

    else:  # PUT
        if pcr <= 0.8:
            opt_pts += 2
            opt_signals.append(f"PCR={pcr} — call writing dominant (bearish) ✅✅")
        elif pcr <= 1.0:
            opt_pts += 1
            opt_signals.append(f"PCR={pcr} — slight bearish ✅")
        elif pcr > 1.2:
            opt_pts -= 1
            opt_signals.append(f"PCR={pcr} — put writing dominant (bullish) ⚠️")
        else:
            opt_signals.append(f"PCR={pcr} — neutral")

        if price <= max_pain:
            opt_pts += 1
            opt_signals.append(f"Price(₹{price:.0f}) ≤ MaxPain(₹{max_pain:.0f}) — bearish pull ✅")
        else:
            opt_signals.append(f"Price(₹{price:.0f}) > MaxPain(₹{max_pain:.0f}) — may pull lower ⚠️")

        # Support level
        opt_signals.append(f"Key support (put writers): ₹{opt_chain['top_pe_strike']:.0f}")

        # Find best PE strike (ATM)
        strike      = atm
        option_data = opt_chain["pe"].get(strike)
        if option_data is None or option_data["ltp"] <= 0:
            for s in sorted(opt_chain["pe"].keys(), key=lambda x: abs(x - price)):
                if opt_chain["pe"][s]["ltp"] > 0:
                    strike      = s
                    option_data = opt_chain["pe"][s]
                    break

    if option_data is None or option_data["ltp"] <= 0:
        return None

    premium = float(option_data["ltp"])

    # ─────────────────────────────────────────────────────────────────────
    # PRECISE ENTRY / SL / TARGETS
    # ─────────────────────────────────────────────────────────────────────
    #
    # Entry zone  : ±3% of current LTP (use limit order, don't market buy)
    # SL          : -35% on premium  (strict — options decay fast)
    # Target 1    : +50%  → book 50% quantity here
    # Target 2    : +80%  → exit remaining quantity here
    # Stock SL    : 1.5× ATR from current price (if stock hits this → exit option too)
    #
    entry_low   = round(premium * 0.97,  1)    # limit buy lower
    entry_high  = round(premium * 1.04,  1)    # max you should pay
    sl_prem     = round(premium * 0.65,  1)    # SL = 65% of entry = −35%
    t1_prem     = round(premium * 1.50,  1)    # Target 1 = +50%
    t2_prem     = round(premium * 1.80,  1)    # Target 2 = +80%

    sl_loss_lot = round((premium - sl_prem)  * lot_size, 0)
    t1_prof_lot = round((t1_prem - premium)  * lot_size, 0)
    t2_prof_lot = round((t2_prem - premium)  * lot_size, 0)

    # Stock price SL (based on ATR — if stock itself breaks, exit immediately)
    if direction == "CALL":
        stock_sl     = round(price - (1.5 * atr), 1)
        stock_sl_pct = round((stock_sl - price) / price * 100, 1)
    else:
        stock_sl     = round(price + (1.5 * atr), 1)
        stock_sl_pct = round((stock_sl - price) / price * 100, 1)

    # Risk:Reward ratio
    risk   = premium - sl_prem
    reward = t1_prem - premium
    rr     = round(reward / risk, 2) if risk > 0 else 0

    total_score = tech_pts + opt_pts

    return {
        "direction"       : direction,
        "symbol"          : tech["symbol"],
        "price"           : price,
        "atr"             : atr,
        "strike"          : strike,
        "expiry"          : expiry,
        "lot_size"        : lot_size,
        "option_symbol"   : option_data["tradingsymbol"],
        # Entry
        "premium"         : premium,
        "entry_low"       : entry_low,
        "entry_high"      : entry_high,
        # SL
        "sl_prem"         : sl_prem,
        "sl_loss_lot"     : sl_loss_lot,
        "stock_sl"        : stock_sl,
        "stock_sl_pct"    : stock_sl_pct,
        # Targets
        "t1_prem"         : t1_prem,
        "t1_prof_lot"     : t1_prof_lot,
        "t2_prem"         : t2_prem,
        "t2_prof_lot"     : t2_prof_lot,
        # R:R
        "rr"              : rr,
        # OI
        "pcr"             : pcr,
        "max_pain"        : max_pain,
        # Scores
        "tech_score"      : tech_pts,
        "opt_score"       : opt_pts,
        "total_score"     : total_score,
        # Signals
        "tech_signals"    : tech_signals,
        "opt_signals"     : opt_signals,
    }


# =============================================================================
# 📲  TELEGRAM ALERT
# =============================================================================

def send_telegram(msg):
    """Send Telegram message to configured group."""
    try:
        token    = os.getenv("TG_TOKEN", "")
        group_id = os.getenv("TG_GROUP_ID", "")
        if not token or not group_id:
            return
        import requests
        requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data={"chat_id": group_id, "text": msg, "parse_mode": "HTML"},
            timeout=5,
        )
    except Exception:
        pass


# =============================================================================
# 🖨️  DISPLAY
# =============================================================================

def display_setup(rank, s):
    color  = Fore.GREEN if s["direction"] == "CALL" else Fore.RED
    arrow  = "📈" if s["direction"] == "CALL" else "📉"
    exp_str = s["expiry"].strftime("%d-%b-%Y") if hasattr(s["expiry"], "strftime") else str(s["expiry"])

    print(f"\n{color}{'═'*68}{Style.RESET_ALL}")
    print(f"{color}  #{rank}  {arrow}  {s['symbol']:<12}  →  BUY {s['direction']}"
          f"  │  Score: {s['total_score']}/9  (T={s['tech_score']} + OI={s['opt_score']}){Style.RESET_ALL}")
    print(f"{color}{'═'*68}{Style.RESET_ALL}")

    print(f"\n  {Fore.WHITE}Stock Price  : ₹{s['price']:,.2f}   ATR={s['atr']:.1f}{Style.RESET_ALL}")
    print(f"  Option       : {s['option_symbol']}")
    print(f"  Expiry       : {exp_str}   Lot Size: {s['lot_size']}")

    print(f"\n  {Fore.CYAN}── ENTRY ────────────────────────────────────────────{Style.RESET_ALL}")
    print(f"  Current LTP  : ₹{s['premium']:.1f}")
    print(f"  Entry Zone   : ₹{s['entry_low']} — ₹{s['entry_high']}")
    print(f"  How to enter : Wait 15min chart confirm → enter on 5min dip")
    print(f"  Best window  : 9:30 AM – 10:30 AM only")

    print(f"\n  {Fore.RED}── STOP LOSS ────────────────────────────────────────{Style.RESET_ALL}")
    print(f"  Option SL    : ₹{s['sl_prem']:.1f}  (−35% on premium)")
    print(f"  Max loss/lot : ₹{s['sl_loss_lot']:,.0f}")
    print(f"  Stock SL     : ₹{s['stock_sl']:.1f}  ({s['stock_sl_pct']:+.1f}% on stock price)")
    print(f"  Rule         : If STOCK hits ₹{s['stock_sl']:.1f} → EXIT option immediately")

    print(f"\n  {Fore.GREEN}── TARGETS ──────────────────────────────────────────{Style.RESET_ALL}")
    print(f"  Target 1(T1) : ₹{s['t1_prem']:.1f}  (+50%)  → Book 50% qty → Profit ₹{s['t1_prof_lot']:,.0f}/lot")
    print(f"  Target 2(T2) : ₹{s['t2_prem']:.1f}  (+80%)  → Exit rest   → Profit ₹{s['t2_prof_lot']:,.0f}/lot")
    print(f"  R:R Ratio    : 1 : {s['rr']}")

    print(f"\n  {Fore.YELLOW}── SIGNALS ──────────────────────────────────────────{Style.RESET_ALL}")
    for sig in s["tech_signals"]:
        print(f"    {sig}")
    for sig in s["opt_signals"]:
        print(f"    {sig}")

    print()


def display_summary_table(setups):
    """Quick reference summary table of all setups."""
    rows = []
    for i, s in enumerate(setups, 1):
        exp_str = s["expiry"].strftime("%d%b") if hasattr(s["expiry"], "strftime") else str(s["expiry"])
        rows.append({
            "#"      : i,
            "Symbol" : s["symbol"],
            "Dir"    : s["direction"],
            "Strike" : f"₹{s['strike']:.0f}",
            "Expiry" : exp_str,
            "Entry"  : f"₹{s['entry_low']}-{s['entry_high']}",
            "SL"     : f"₹{s['sl_prem']:.0f}(-35%)",
            "T1"     : f"₹{s['t1_prem']:.0f}(+50%)",
            "T2"     : f"₹{s['t2_prem']:.0f}(+80%)",
            "R:R"    : f"1:{s['rr']}",
            "Score"  : s["total_score"],
        })
    df = pd.DataFrame(rows)
    print(df.to_string(index=False))


# =============================================================================
# 💾  TRACKER — Save top picks and check performance
# =============================================================================

TRACKER_FILE = os.path.join(BASE_DIR, "fno_tracker.csv")

TRACKER_COLS = [
    "date_added", "symbol", "direction", "option_symbol",
    "strike", "expiry", "lot_size",
    "entry_premium", "sl_premium", "t1_premium", "t2_premium",
    "stock_price", "stock_sl", "pcr", "max_pain",
    "score", "rank",
    "current_premium", "current_return_pct", "last_checked", "status",
]

def save_to_tracker(top_calls, top_puts):
    """Save top 3 Call + top 3 Put picks to fno_tracker.csv."""
    today = date.today().isoformat()

    # Load existing — always replace today's rows with fresh EOD picks
    if os.path.exists(TRACKER_FILE):
        df = pd.read_csv(TRACKER_FILE)
        if today in df["date_added"].astype(str).values:
            df = df[df["date_added"].astype(str) != today]
            print(f"\n{Fore.YELLOW}🔄  Replacing today's picks with fresh EOD data{Style.RESET_ALL}")
    else:
        df = pd.DataFrame(columns=TRACKER_COLS)

    rows = []
    for rank, s in enumerate(top_calls[:3] + top_puts[:3], 1):
        exp_str = s["expiry"].strftime("%Y-%m-%d") if hasattr(s["expiry"], "strftime") else str(s["expiry"])
        rows.append({
            "date_added"       : today,
            "symbol"           : s["symbol"],
            "direction"        : s["direction"],
            "option_symbol"    : s["option_symbol"],
            "strike"           : s["strike"],
            "expiry"           : exp_str,
            "lot_size"         : s["lot_size"],
            "entry_premium"    : s["premium"],
            "sl_premium"       : s["sl_prem"],
            "t1_premium"       : s["t1_prem"],
            "t2_premium"       : s["t2_prem"],
            "stock_price"      : s["price"],
            "stock_sl"         : s["stock_sl"],
            "pcr"              : s["pcr"],
            "max_pain"         : s["max_pain"],
            "score"            : s["total_score"],
            "rank"             : rank,
            "current_premium"  : s["premium"],
            "current_return_pct": 0.0,
            "last_checked"     : today,
            "status"           : "OPEN",
        })

    new_df = pd.DataFrame(rows, columns=TRACKER_COLS)
    df     = pd.concat([df, new_df], ignore_index=True)
    df.to_csv(TRACKER_FILE, index=False)

    print(f"\n{Fore.GREEN}✅ Saved {len(rows)} picks to tracker: {TRACKER_FILE}{Style.RESET_ALL}")
    print(f"   Top 3 CALL: {[r['symbol'] for r in rows if r['direction']=='CALL']}")
    print(f"   Top 3 PUT : {[r['symbol'] for r in rows if r['direction']=='PUT']}")


def update_tracker_prices(kite):
    """Fetch current option LTP for all OPEN positions, update ₹ P&L, send alerts."""
    if not os.path.exists(TRACKER_FILE):
        print("No tracker file found. Run screener first.")
        return

    df      = pd.read_csv(TRACKER_FILE)
    open_df = df[df["status"].str.startswith("OPEN")]

    if open_df.empty:
        print(f"{Fore.YELLOW}No open positions to update.{Style.RESET_ALL}")
        return

    now_str = datetime.now().strftime("%d-%b %H:%M")
    print(f"\n  Updating {len(open_df)} open positions  [{now_str}]\n")

    alerts = []   # collect SL/T1/T2 hits to send as Telegram

    for idx, row in open_df.iterrows():
        opt_sym  = row["option_symbol"]
        symbol   = row["symbol"]
        direction= row["direction"]
        entry    = float(row["entry_premium"])
        sl       = float(row["sl_premium"])
        t1       = float(row["t1_premium"])
        t2       = float(row["t2_premium"])
        lot_size = int(row["lot_size"])
        lots     = int(row.get("lots", 1))

        print(f"  {opt_sym:<30}", end="", flush=True)

        structure = str(row.get("structure", "NAKED"))
        sell_sym  = str(row.get("sell_symbol", "") or "")
        try:
            if structure == "SPREAD" and sell_sym and sell_sym not in ("nan", ""):
                # spread value = long leg LTP - short leg LTP (vs entry net debit)
                q = kite.quote([f"NFO:{opt_sym}", f"NFO:{sell_sym}"])
                long_ltp  = float(q.get(f"NFO:{opt_sym}", {}).get("last_price", 0) or 0)
                short_ltp = float(q.get(f"NFO:{sell_sym}", {}).get("last_price", 0) or 0)
                ltp = round(long_ltp - short_ltp, 1)
            else:
                q   = kite.quote([f"NFO:{opt_sym}"])
                ltp = float(q.get(f"NFO:{opt_sym}", {}).get("last_price", 0) or 0)
        except Exception:
            print(f"{Fore.YELLOW}fetch failed{Style.RESET_ALL}")
            time.sleep(1)
            continue

        if ltp <= 0:
            print(f"{Fore.YELLOW}value<=0 (market closed / expired){Style.RESET_ALL}")
            continue

        ret_pct = round((ltp / entry - 1) * 100, 1)
        pnl_rs  = round((ltp - entry) * lot_size * lots, 0)

        # ── Status logic ─────────────────────────────────────────────────
        prev_status = str(row.get("status", "OPEN"))

        if ltp <= sl:
            new_status = "SL-HIT ❌"
        elif ltp >= t2:
            new_status = "T2-HIT 🔥"
        elif ltp >= t1:
            new_status = "T1-HIT ✅"
        else:
            new_status = f"OPEN ({ret_pct:+.1f}%)"

        # Alert if status changed to a terminal state
        if new_status != prev_status and any(x in new_status for x in ["SL-HIT","T1-HIT","T2-HIT"]):
            arrow = "📈" if direction == "CALL" else "📉"
            emoji = "❌" if "SL" in new_status else ("🔥" if "T2" in new_status else "✅")
            alerts.append(
                f"{emoji} <b>F&O ALERT — {symbol} {direction}</b>\n"
                f"Option : {opt_sym}\n"
                f"Status : {new_status}\n"
                f"LTP    : ₹{ltp}  (entry ₹{entry})\n"
                f"P&L    : ₹{pnl_rs:+,.0f}  ({ret_pct:+.1f}%)\n"
                f"Time   : {now_str}"
            )

        df.at[idx, "current_premium"]    = ltp
        df.at[idx, "current_return_pct"] = ret_pct
        df.at[idx, "pnl_rs"]            = pnl_rs
        df.at[idx, "last_checked"]       = date.today().isoformat()
        df.at[idx, "status"]             = new_status

        color = Fore.GREEN if pnl_rs >= 0 else Fore.RED
        print(f"  ₹{ltp:<8.1f}  {color}{ret_pct:+.1f}%  ₹{pnl_rs:+,.0f}{Style.RESET_ALL}  →  {new_status}")
        time.sleep(0.4)

    df.to_csv(TRACKER_FILE, index=False)
    print(f"\n{Fore.GREEN}✅ Tracker updated — {now_str}{Style.RESET_ALL}")

    # Send Telegram alerts for any hits
    for alert_msg in alerts:
        send_telegram(alert_msg)
        print(f"\n{Fore.CYAN}📲 Telegram alert sent{Style.RESET_ALL}")


def show_tracker_report():
    """Show full performance report of all tracked picks."""
    if not os.path.exists(TRACKER_FILE):
        print("No tracker data. Run screener with --save first.")
        return

    df    = pd.read_csv(TRACKER_FILE)
    dates = sorted(df["date_added"].unique(), reverse=True)

    print(f"\n{Fore.CYAN}{'='*72}")
    print(f"  📊 F&O SCREENER — PERFORMANCE TRACKER")
    print(f"  As of : {date.today().strftime('%d %b %Y')}  |  {len(dates)} batch(es) tracked")
    print(f"{'='*72}{Style.RESET_ALL}\n")

    total_picks = 0; total_t1 = 0; total_t2 = 0; total_sl = 0

    for d in dates:
        wdf = df[df["date_added"] == d].copy()
        print(f"{Fore.YELLOW}📅 Batch: {d}  ({len(wdf)} picks){Style.RESET_ALL}")

        rows = []
        for _, r in wdf.iterrows():
            status  = str(r.get("status", "OPEN"))
            ret_pct = r.get("current_return_pct", 0)
            direction = r.get("direction", "")

            if "T2-HIT" in status:
                sc = Fore.GREEN
            elif "T1-HIT" in status:
                sc = Fore.CYAN
            elif "SL-HIT" in status:
                sc = Fore.RED
            elif isinstance(ret_pct, float) and ret_pct > 0:
                sc = Fore.GREEN
            elif isinstance(ret_pct, float) and ret_pct < 0:
                sc = Fore.RED
            else:
                sc = Fore.WHITE

            arrow = "📈" if direction == "CALL" else "📉"
            ret_str = f"{ret_pct:+.1f}%" if isinstance(ret_pct, (int, float)) else "-"

            entry_prem = float(r.get("entry_premium", 0))
            pnl_rs   = r.get("pnl_rs", 0)
            lot_size = int(r.get("lot_size", 1))
            lots     = int(r.get("lots", 1))
            invest   = r.get("investment", entry_prem * lot_size)
            pnl_str  = f"₹{float(pnl_rs):+,.0f}" if isinstance(pnl_rs, (int, float)) else "-"

            rows.append([
                f"{arrow}{r['symbol']}",
                direction,
                r["option_symbol"],
                f"₹{r['entry_premium']}",
                f"₹{r.get('current_premium', '-')}",
                f"{sc}{ret_str}{Style.RESET_ALL}",
                f"{sc}{pnl_str}{Style.RESET_ALL}",
                f"₹{r['sl_premium']}",
                f"₹{r['t1_premium']}",
                f"₹{r['t2_premium']}",
                status[:18],
            ])

            total_picks += 1
            if "T2-HIT" in status: total_t2 += 1
            elif "T1-HIT" in status: total_t1 += 1
            elif "SL-HIT" in status: total_sl += 1

        print(pd.DataFrame(rows, columns=[
            "Symbol","Dir","Option","Entry","Current","Ret%","P&L(₹)",
            "SL","T1(+50%)","T2(+80%)","Status"
        ]).to_string(index=False))

        # Batch P&L summary
        batch_pnl = df[df["date_added"] == d]["pnl_rs"].sum()
        batch_inv = df[df["date_added"] == d]["investment"].sum()
        color = Fore.GREEN if batch_pnl >= 0 else Fore.RED
        print(f"\n  {'─'*50}")
        print(f"  Batch invested  : ₹{batch_inv:,.0f}  (1 lot each, {len(wdf)} positions)")
        print(f"  Batch P&L       : {color}₹{batch_pnl:+,.0f}{Style.RESET_ALL}")
        print()

    # Scorecard
    total_wins   = total_t1 + total_t2
    total_closed = total_wins + total_sl
    win_pct      = round(total_wins / total_closed * 100, 1) if total_closed > 0 else "-"

    print(f"{Fore.CYAN}{'='*72}")
    print(f"  SCREENER SCORECARD")
    print(f"{'='*72}{Style.RESET_ALL}")
    print(f"  Total picks   : {total_picks}")
    print(f"  T2 hit (🔥)  : {total_t2}")
    print(f"  T1 hit (✅)  : {total_t1}")
    print(f"  SL hit (❌)  : {total_sl}")
    print(f"  Still open    : {total_picks - total_closed}")
    print(f"  Win rate      : {win_pct}%  (closed trades only)")
    print()

    if total_closed >= 5:
        if isinstance(win_pct, float) and win_pct >= 60:
            print(f"{Fore.GREEN}✅ STRATEGY WORKING — Win rate {win_pct}% over {total_closed} closed{Style.RESET_ALL}")
        elif isinstance(win_pct, float) and win_pct >= 40:
            print(f"{Fore.YELLOW}⚠️  MARGINAL — {win_pct}% win rate — review signals{Style.RESET_ALL}")
        else:
            print(f"{Fore.RED}❌ UNDERPERFORMING — {win_pct}% — tighten filters{Style.RESET_ALL}")
    else:
        print(f"{Fore.YELLOW}📊 Need 5+ closed trades to judge (currently {total_closed}){Style.RESET_ALL}")


# =============================================================================
# 🚀  MAIN
# =============================================================================

def run_strategy_scan(kite, nse_df, nfo_df, universe):
    """SINGLE SOURCE OF TRUTH scan. Market regime -> per-stock multi-factor gate
    (fno_strategy.evaluate) -> rank qualified candidates -> apply daily/portfolio
    caps in ranked order (elite trades get an extra slot) -> save. Replaces the
    old per-side tech_score + get_trade_setup + blind top-3/top-3 flow."""
    cfg   = FS.load_config()
    today = date.today().isoformat()

    regime = FS.compute_index_regime(kite, nse_df)
    if cfg.get("require_regime_align", True):
        _dir_note = (f"(call={'Y' if regime['allow_call'] else 'N'} "
                     f"put={'Y' if regime['allow_put'] else 'N'})")
    else:
        _dir_note = "(regime-align OFF — CALL+PUT both allowed, best-scoring side per stock)"
    print(f"\n{Fore.CYAN}  MARKET REGIME: {regime['regime']}  "
          f"{_dir_note}  {regime.get('detail','')}{Style.RESET_ALL}")
    print(f"  Mode={cfg['mode'].upper()} | min_score={cfg['min_score']} elite={cfg['elite_score']} | "
          f"caps: {cfg['max_new_per_day']}/day, {cfg['max_open_total']} open, "
          f"Rs{cfg['max_capital_deploy']:,.0f}\n")

    tracker_df = pd.read_csv(TRACKER_FILE) if os.path.exists(TRACKER_FILE) else None

    total = len(universe)
    candidates, rejects = [], {}
    for i, sym in enumerate(universe, 1):
        print(f"  [{i:3}/{total}] {sym:<14}", end="", flush=True)
        tech = FS.get_technicals(kite, nse_df, sym)
        if tech is None:
            print(f"{Fore.YELLOW}skip (no data){Style.RESET_ALL}"); time.sleep(0.3); continue
        expiry = FS.get_nearest_expiry(nfo_df, sym, cfg)
        opt = FS.get_option_chain(kite, nfo_df, sym, tech["price"], expiry) if expiry else None
        setup, reason = FS.evaluate(sym, tech, opt, regime, tracker_df, cfg,
                                    today=today, apply_caps=False)
        if setup is None:
            rejects[reason] = rejects.get(reason, 0) + 1
            print(f"{Fore.YELLOW}{reason}{Style.RESET_ALL}")
        else:
            col = Fore.GREEN if setup["direction"] == "CALL" else Fore.RED
            print(f"{col}{setup['direction']:<5} score={setup['score']} {setup['structure']} "
                  f"prem=Rs{setup['entry_premium']}{' ELITE' if setup.get('elite') else ''}{Style.RESET_ALL}")
            candidates.append(setup)
        time.sleep(0.35)

    top_rej = sorted(rejects.items(), key=lambda x: -x[1])[:6]
    print(f"\n  Qualified: {len(candidates)} | top rejects: "
          + ", ".join(f"{k}={v}" for k, v in top_rej))

    accepted = FS.select_with_caps(candidates, tracker_df, cfg, today)
    print(f"\n{Fore.GREEN}  ACCEPTED {len(accepted)} trade(s) after caps:{Style.RESET_ALL}")
    for s in sorted(accepted, key=lambda x: -x["score"]):
        tag = " ELITE*" if s.get("elite") else ""
        print(f"    {s['direction']:<5} {s['symbol']:<12} score={s['score']:<3} {s['structure']:<6} "
              f"entry=Rs{s['entry_premium']:<7} SL=Rs{s['sl_premium']} T1=Rs{s['t1_premium']}{tag}")

    if accepted:
        save_setups(accepted, today)
    else:
        print(f"  {Fore.YELLOW}No trade cleared the gate today — capital protected, "
              f"not forced.{Style.RESET_ALL}")


def save_setups(accepted, today=None):
    """Write accepted setups to the tracker, replacing today's prior rows. Keeps
    the existing TRACKER_COLS plus new structure/spread/elite columns so the
    dashboard and update_tracker_prices keep working."""
    today = today or date.today().isoformat()
    df = None
    if os.path.exists(TRACKER_FILE):
        df = pd.read_csv(TRACKER_FILE)
        if today in df["date_added"].astype(str).values:
            df = df[df["date_added"].astype(str) != today]
            print(f"\n{Fore.YELLOW}🔄  Replacing today's rows with fresh scan{Style.RESET_ALL}")

    rows = [FS.setup_to_tracker_row(s, today, rank)
            for rank, s in enumerate(sorted(accepted, key=lambda x: -x["score"]), 1)]
    new_df = pd.DataFrame(rows)
    out = pd.concat([df, new_df], ignore_index=True) if (df is not None and len(df)) else new_df
    out.to_csv(TRACKER_FILE, index=False)
    print(f"\n{Fore.GREEN}✅ Saved {len(rows)} trade(s) to tracker: {TRACKER_FILE}{Style.RESET_ALL}")


def main():
    if "--help" in sys.argv:
        print(__doc__)
        return

    # Tracker-only modes (no scan needed)
    if "--report" in sys.argv:
        show_tracker_report()
        return

    if "--update" in sys.argv:
        kite = get_kite()
        update_tracker_prices(kite)
        show_tracker_report()
        return

    quick = "--quick" in sys.argv
    save  = "--save"  in sys.argv or "--quick" not in sys.argv  # auto-save on full run

    print(f"\n{Fore.CYAN}{'='*68}")
    print(f"  📊 VISHAL F&O SCREENER  — OI + Trend Confluence")
    print(f"  Date   : {date.today().strftime('%d %b %Y')}")
    print(f"  Mode   : {'QUICK (30 stocks)' if quick else 'FULL (100 stocks)'}")
    print(f"  Strategy: 15min confirm + 5min entry | Monthly expiry")
    print(f"{'='*68}{Style.RESET_ALL}\n")

    # ── Connect ────────────────────────────────────────────────────────────
    kite = get_kite()

    # ── Load instruments ───────────────────────────────────────────────────
    print("\nLoading instruments...")
    nse_df, nfo_df = load_instruments(kite)

    universe = FNO_UNIVERSE[:30] if quick else FNO_UNIVERSE
    total    = len(universe)

    # ── New single-source-of-truth scan (regime + multi-factor gate + caps) ──
    run_strategy_scan(kite, nse_df, nfo_df, universe)

    # ── Trading rules reminder ─────────────────────────────────────────────
    print(f"\n{Fore.CYAN}{'='*68}")
    print(f"  📌  TRADING RULES — READ BEFORE ENTERING")
    print(f"{'='*68}{Style.RESET_ALL}")
    print("  ENTRY   : Check 15min chart → confirm trend still valid")
    print("            Drop to 5min → enter on pullback (not at breakout top)")
    print("            Best window: 9:30 AM – 10:30 AM")
    print("            Use LIMIT ORDER in the entry zone shown above")
    print()
    print("  SL      : HARD stop at −35% on option premium — NO exceptions")
    print("            Also exit if STOCK price hits the stock SL level")
    print("            Don't average down on losing options")
    print()
    print("  TARGETS : Book 50% quantity at T1 (+50%)")
    print("            Move SL to breakeven after T1 hit")
    print("            Exit remaining at T2 (+80%) or before expiry")
    print()
    print("  EXPIRY  : EXIT 2 days before expiry — theta crushes fast")
    print("            Never hold options to expiry day")
    print()
    print("  RISK    : Max 2% of total capital per trade")
    print("            Max 3 open positions at a time")
    print()


if __name__ == "__main__":
    main()
