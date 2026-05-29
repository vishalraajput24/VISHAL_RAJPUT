"""
=============================================================================
F&O DATA COLLECTOR & ANALYSIS ENGINE
=============================================================================
Collects F&O signal data every 15-min candle close.
No telegram spam — pure data collection for market insight.

Modes:
  --morning   Full technical scan at 9:00 AM, cache results (one-time/day)
  --tick      15-min tick: fast LTP + OI scan, log all signals
  --report    Full analysis: win rate, SL/T1/T2 with timestamps
  --entries   Show only active tracked entries

Flow:
  9:00 AM      → --morning  (full 119-stock tech scan, ~5 min, cache saved)
  9:15 AM+     → --tick     (every 15 min, reads cache, fast scan, ~30 sec)
  Anytime      → --report   (full breakdown of all logged data)

Data files:
  fno_tech_cache.json    Daily technical scores (refreshed each morning)
  fno_signals_log.csv    Every 15-min tick snapshot of all qualified signals
  fno_history.csv        Event log: entry/update/SL-hit/T1-hit/T2-hit + timestamp
=============================================================================
"""

import os, sys, json, time, math
import pandas as pd
import numpy as np
from datetime import datetime, date, timedelta
from dotenv import load_dotenv
from kiteconnect import KiteConnect
from colorama import Fore, Style, init
import warnings
warnings.filterwarnings("ignore")

init(autoreset=True)

load_dotenv(os.path.expanduser("~/.env"))
API_KEY   = os.getenv("KITE_API_KEY", "")

BASE_DIR       = os.path.dirname(os.path.abspath(__file__))
TOKEN_FILE     = os.path.join(os.path.expanduser("~"), "VISHAL_RAJPUT", "state", "access_token.json")
INST_CACHE_NSE = os.path.join(BASE_DIR, "inst_cache_nse.csv")
INST_CACHE_NFO = os.path.join(BASE_DIR, "inst_cache_nfo.csv")
TECH_CACHE     = os.path.join(BASE_DIR, "fno_tech_cache.json")
SIGNALS_LOG    = os.path.join(BASE_DIR, "fno_signals_log.csv")
HISTORY_FILE   = os.path.join(BASE_DIR, "fno_history.csv")
ENTRIES_FILE   = os.path.join(BASE_DIR, "fno_tracker.csv")   # shared with screener

# ── Signal threshold for auto-entry logging ────────────────────────────────
MIN_SCORE_TO_LOG = 5      # log signal to history
MIN_SCORE_ENTRY  = 6      # track as potential trade entry

# =============================================================================
# 🔐  AUTH
# =============================================================================

def get_kite():
    if not API_KEY:
        print(f"{Fore.RED}KITE_API_KEY not found{Style.RESET_ALL}"); sys.exit(1)
    kite = KiteConnect(api_key=API_KEY)
    with open(TOKEN_FILE) as f:
        saved = json.load(f)
    if saved.get("date") != date.today().isoformat():
        print(f"{Fore.RED}Token expired{Style.RESET_ALL}"); sys.exit(1)
    kite.set_access_token(saved["access_token"])
    kite.profile()
    return kite


def load_instruments(kite):
    today = date.today().isoformat()
    def cached(path):
        if not os.path.exists(path): return None
        tmp = pd.read_csv(path, nrows=1)
        return pd.read_csv(path) if "_date" in tmp.columns and str(tmp["_date"].iloc[0]) == today else None

    nse_df = cached(INST_CACHE_NSE)
    if nse_df is None:
        nse_df = pd.DataFrame(kite.instruments("NSE")); nse_df["_date"] = today
        nse_df.to_csv(INST_CACHE_NSE, index=False)

    nfo_df = cached(INST_CACHE_NFO)
    if nfo_df is None:
        nfo_df = pd.DataFrame(kite.instruments("NFO")); nfo_df["_date"] = today
        nfo_df.to_csv(INST_CACHE_NFO, index=False)

    nfo_df["expiry"] = pd.to_datetime(nfo_df["expiry"])
    return nse_df, nfo_df


# =============================================================================
# 📊  TECHNICAL INDICATORS (for morning full scan)
# =============================================================================

def compute_rsi(series, period=14):
    delta = series.diff()
    gain  = delta.clip(lower=0).ewm(com=period-1, min_periods=period).mean()
    loss  = (-delta.clip(upper=0)).ewm(com=period-1, min_periods=period).mean()
    return 100 - (100 / (1 + gain / loss.replace(0, np.nan)))

def compute_atr(df, period=14):
    hl  = df["high"] - df["low"]
    hcp = (df["high"] - df["close"].shift()).abs()
    lcp = (df["low"]  - df["close"].shift()).abs()
    return pd.concat([hl, hcp, lcp], axis=1).max(axis=1).ewm(com=period-1, min_periods=period).mean()

def get_technicals(kite, nse_df, symbol):
    try:
        inst = nse_df[(nse_df["tradingsymbol"] == symbol) & (nse_df["instrument_type"] == "EQ")]
        if inst.empty: return None
        token     = int(inst.iloc[0]["instrument_token"])
        to_date   = datetime.now()
        from_date = to_date - timedelta(days=130)
        candles   = kite.historical_data(token, from_date, to_date, "day")
        if len(candles) < 30: return None
        df = pd.DataFrame(candles)
        df["ema20"]   = df["close"].ewm(span=20, adjust=False).mean()
        df["ema50"]   = df["close"].ewm(span=50, adjust=False).mean()
        df["rsi"]     = compute_rsi(df["close"])
        df["atr"]     = compute_atr(df)
        df["vol_avg"] = df["volume"].rolling(10).mean()
        last = df.iloc[-1]; prev = df.iloc[-2]
        return {
            "symbol"    : symbol,
            "price"     : round(float(last["close"]), 2),
            "ema20"     : round(float(last["ema20"]), 2),
            "ema50"     : round(float(last["ema50"]), 2),
            "rsi"       : round(float(last["rsi"]), 1),
            "atr"       : round(float(last["atr"]), 2),
            "prev_close": float(prev["close"]),
            "volume"    : float(last["volume"]),
            "vol_avg"   : float(last["vol_avg"]) if not math.isnan(float(last["vol_avg"])) else 0,
        }
    except Exception:
        return None

def tech_score(tech):
    price = tech["price"]; ema20 = tech["ema20"]; ema50 = tech["ema50"]
    rsi   = tech["rsi"];   vol   = tech["volume"]; vol_avg = tech["vol_avg"]
    call_pts = 0; put_pts = 0; signals = []

    ema20_dist = round((price - ema20) / ema20 * 100, 2)
    if price > ema20:
        call_pts += 1
        signals.append(f"P>EMA20({ema20:.0f})+{ema20_dist:.1f}%✅")
    else:
        put_pts += 1
        if abs(ema20_dist) >= 2.0:
            signals.append(f"P<EMA20({ema20:.0f}){ema20_dist:.1f}%🔻")
        else:
            put_pts -= 1    # penalise — too close to EMA20, reversal risk
            signals.append(f"P<EMA20({ema20:.0f}){ema20_dist:.1f}%⚠️EMA_support")
    if price > ema50:  call_pts += 1; signals.append(f"P>EMA50({ema50:.0f})✅")
    else:              put_pts  += 1; signals.append(f"P<EMA50({ema50:.0f})🔻")
    if ema20 > ema50 * 1.005:   call_pts += 1; signals.append("EMA20>EMA50✅")
    elif ema20 < ema50 * 0.995: put_pts  += 1; signals.append("EMA20<EMA50🔻")

    if   55 <= rsi <= 75: call_pts += 2; signals.append(f"RSI={rsi:.0f}✅")
    elif 25 <= rsi <= 45: put_pts  += 2; signals.append(f"RSI={rsi:.0f}🔻")
    elif rsi > 75:        call_pts -= 1; signals.append(f"RSI={rsi:.0f}⚠️OB")
    elif rsi < 25:        put_pts  -= 1; signals.append(f"RSI={rsi:.0f}⚠️OS")

    if vol_avg > 0 and vol > vol_avg * 1.4:
        if price >= tech["prev_close"]: call_pts += 1; signals.append("VolSpike+Up✅")
        else:                           put_pts  += 1; signals.append("VolSpike+Dn🔻")

    if call_pts >= 3 and call_pts > put_pts:   return "CALL", call_pts, signals
    elif put_pts >= 3 and put_pts > call_pts:  return "PUT",  put_pts,  signals
    else:                                      return "NEUTRAL", max(call_pts, put_pts), signals


# =============================================================================
# 📈  OPTION CHAIN (used in tick scan)
# =============================================================================

def get_nearest_expiry(nfo_df, symbol):
    opts   = nfo_df[(nfo_df["name"] == symbol) & (nfo_df["instrument_type"].isin(["CE","PE"]))]
    if opts.empty: return None
    today  = pd.Timestamp(date.today())
    future = sorted([e for e in opts["expiry"].unique() if pd.Timestamp(e) >= today])
    for exp in future:
        if (pd.Timestamp(exp) - today).days >= 3:
            return pd.Timestamp(exp).date()
    return pd.Timestamp(future[0]).date() if future else None

def get_option_chain_fast(kite, nfo_df, symbol, price, expiry):
    """Lightweight option chain — ATM ±5 strikes only."""
    try:
        opts = nfo_df[
            (nfo_df["name"] == symbol) &
            (nfo_df["instrument_type"].isin(["CE","PE"])) &
            (nfo_df["expiry"].dt.date == expiry)
        ].copy()
        if opts.empty: return None
        opts["strike"] = opts["strike"].astype(float)
        lot_size = int(opts.iloc[0]["lot_size"])
        strikes  = sorted(opts["strike"].unique())
        atm_idx  = min(range(len(strikes)), key=lambda i: abs(strikes[i] - price))
        lo = max(0, atm_idx - 8); hi = min(len(strikes)-1, atm_idx + 8)
        sel = set(strikes[lo:hi+1]); atm = strikes[atm_idx]
        opts = opts[opts["strike"].isin(sel)]

        keys   = [f"NFO:{r['tradingsymbol']}" for _, r in opts.iterrows()]
        quotes = {}
        for i in range(0, len(keys), 499):
            quotes.update(kite.quote(keys[i:i+499]))
            if i+499 < len(keys): time.sleep(0.3)

        ce = {}; pe = {}
        for _, row in opts.iterrows():
            q   = quotes.get(f"NFO:{row['tradingsymbol']}", {})
            oi  = int(q.get("oi", 0) or 0)
            ltp = float(q.get("last_price", 0) or 0)
            entry = {"oi": oi, "ltp": ltp, "strike": float(row["strike"]),
                     "tradingsymbol": row["tradingsymbol"]}
            if row["instrument_type"] == "CE": ce[float(row["strike"])] = entry
            else:                              pe[float(row["strike"])] = entry

        total_ce = sum(v["oi"] for v in ce.values())
        total_pe = sum(v["oi"] for v in pe.values())
        pcr = round(total_pe / total_ce, 2) if total_ce > 0 else 1.0

        # Max pain
        all_s = sorted(set(list(ce.keys()) + list(pe.keys())))
        max_pain = atm; min_pain = float("inf")
        for s in all_s:
            pain = sum((s-k)*v["oi"]*lot_size for k,v in ce.items() if s>k) + \
                   sum((k-s)*v["oi"]*lot_size for k,v in pe.items() if s<k)
            if pain < min_pain: min_pain = pain; max_pain = s

        # ATM premiums
        ce_atm = ce.get(atm, {}); pe_atm = pe.get(atm, {})
        ce_prem = float(ce_atm.get("ltp", 0))
        pe_prem = float(pe_atm.get("ltp", 0))
        ce_sym  = ce_atm.get("tradingsymbol", "")
        pe_sym  = pe_atm.get("tradingsymbol", "")

        return {"pcr": pcr, "max_pain": max_pain, "atm": atm, "lot_size": lot_size,
                "ce_prem": ce_prem, "pe_prem": pe_prem,
                "ce_sym": ce_sym, "pe_sym": pe_sym,
                "total_ce_oi": total_ce, "total_pe_oi": total_pe}
    except Exception:
        return None

def opt_score(opt, direction, price=0):
    pts = 0; sigs = []
    pcr = opt["pcr"]; mp = opt["max_pain"]

    # ── PCR ────────────────────────────────────────────────────────────────
    if direction == "CALL":
        if pcr >= 1.2:   pts += 2; sigs.append(f"PCR={pcr}✅✅")
        elif pcr >= 1.0: pts += 1; sigs.append(f"PCR={pcr}✅")
        elif pcr < 0.8:  pts -= 1; sigs.append(f"PCR={pcr}⚠️")
    else:
        if pcr <= 0.8:   pts += 2; sigs.append(f"PCR={pcr}✅✅")
        elif pcr <= 1.0: pts += 1; sigs.append(f"PCR={pcr}✅")
        elif pcr > 1.2:  pts -= 1; sigs.append(f"PCR={pcr}⚠️")

    # ── Max Pain gravity ────────────────────────────────────────────────────
    if price > 0 and mp > 0:
        if direction == "CALL":
            if price < mp:
                pts += 1; sigs.append(f"MP={mp:.0f}>price — gravity↑✅")
            elif price > mp * 1.01:
                pts -= 1; sigs.append(f"MP={mp:.0f}<price — no upward pull⚠️")
        else:  # PUT
            if price > mp:
                pts += 1; sigs.append(f"MP={mp:.0f}<price — gravity↓✅")
            elif price < mp * 0.99:
                pts -= 1; sigs.append(f"MP={mp:.0f}>price — no downward pull⚠️")

    return pts, sigs


# =============================================================================
# 💾  DATA LOGGING
# =============================================================================

SIGNALS_COLS = [
    "timestamp", "date", "time", "symbol", "direction", "tech_score",
    "opt_score", "total_score", "price", "ema20", "ema50", "rsi", "atr",
    "pcr", "max_pain", "atm_strike", "ce_prem", "pe_prem",
    "ce_sym", "pe_sym", "lot_size", "signals"
]

HISTORY_COLS = [
    "event_time", "date", "time", "symbol", "direction",
    "option_symbol", "entry_time", "entry_premium",
    "current_premium", "pnl_pct", "pnl_rs", "lot_size",
    "sl_premium", "t1_premium", "t2_premium",
    "event_type",   # NEW_ENTRY / UPDATE / SL_HIT / T1_HIT / T2_HIT / EXPIRED
    "hold_minutes", "notes"
]

def append_signal(row_dict):
    df_new = pd.DataFrame([row_dict], columns=SIGNALS_COLS)
    if os.path.exists(SIGNALS_LOG):
        df_new.to_csv(SIGNALS_LOG, mode="a", header=False, index=False)
    else:
        df_new.to_csv(SIGNALS_LOG, index=False)

def append_history(row_dict):
    df_new = pd.DataFrame([row_dict], columns=HISTORY_COLS)
    if os.path.exists(HISTORY_FILE):
        df_new.to_csv(HISTORY_FILE, mode="a", header=False, index=False)
    else:
        df_new.to_csv(HISTORY_FILE, index=False)


# =============================================================================
# 🌅  MORNING SCAN — full tech scan, cache results
# =============================================================================

# Import FNO_UNIVERSE from screener
sys.path.insert(0, BASE_DIR)
try:
    from vishal_fno_screener import FNO_UNIVERSE
except Exception:
    FNO_UNIVERSE = []

def morning_scan(kite, nse_df, nfo_df):
    today   = date.today().isoformat()
    total   = len(FNO_UNIVERSE)
    results = {}

    print(f"\n{Fore.CYAN}{'='*60}")
    print(f"  🌅 MORNING SCAN — {today}  ({total} stocks)")
    print(f"{'='*60}{Style.RESET_ALL}\n")

    for i, sym in enumerate(FNO_UNIVERSE, 1):
        print(f"  [{i:3}/{total}] {sym:<14}", end="", flush=True)
        tech = get_technicals(kite, nse_df, sym)
        if tech is None:
            print(f"{Fore.YELLOW}skip{Style.RESET_ALL}")
            time.sleep(0.3); continue

        direction, score, signals = tech_score(tech)
        col = Fore.GREEN if direction == "CALL" else (Fore.RED if direction == "PUT" else Fore.WHITE)
        print(f"{col}{direction:<8}{Style.RESET_ALL} RSI={tech['rsi']:4.0f} score={score}")

        results[sym] = {
            "symbol"    : sym,
            "direction" : direction,
            "tech_score": score,
            "signals"   : "|".join(signals),
            "price"     : tech["price"],
            "ema20"     : tech["ema20"],
            "ema50"     : tech["ema50"],
            "rsi"       : tech["rsi"],
            "atr"       : tech["atr"],
            "prev_close": tech["prev_close"],
            "volume"    : tech["volume"],
            "vol_avg"   : tech["vol_avg"],
            "cached_at" : today,
        }
        time.sleep(0.35)

    # Save cache
    with open(TECH_CACHE, "w") as f:
        json.dump(results, f, indent=2)

    qualified = [v for v in results.values() if v["direction"] != "NEUTRAL" and v["tech_score"] >= 3]
    print(f"\n{Fore.GREEN}✅ Morning scan done — {len(qualified)}/{total} qualified{Style.RESET_ALL}")
    print(f"   Cache saved: {TECH_CACHE}\n")
    return results


# =============================================================================
# ⏱️  15-MIN TICK — fast scan using cached technicals
# =============================================================================

def tick_scan(kite, nfo_df):
    now      = datetime.now()
    ts       = now.strftime("%Y-%m-%d %H:%M")
    date_str = now.strftime("%Y-%m-%d")
    time_str = now.strftime("%H:%M")

    # Load tech cache
    if not os.path.exists(TECH_CACHE):
        print(f"{Fore.YELLOW}No tech cache. Run --morning first.{Style.RESET_ALL}")
        return

    with open(TECH_CACHE) as f:
        cache = json.load(f)

    if not cache:
        print("Empty cache."); return

    # Filter: only stocks with direction + score >= 3
    qualified = [v for v in cache.values()
                 if v["direction"] != "NEUTRAL" and v["tech_score"] >= 3]

    print(f"\n  ⏱️  TICK [{time_str}] — {len(qualified)} qualified stocks\n")

    # Bulk LTP for quick price update
    ltp_keys = [f"NSE:{v['symbol']}" for v in qualified]
    ltp_map  = {}
    try:
        for i in range(0, len(ltp_keys), 499):
            ltp_map.update(kite.ltp(ltp_keys[i:i+499]))
    except Exception:
        pass

    new_entries  = 0
    signal_count = 0

    for tech_data in qualified:
        sym       = tech_data["symbol"]
        direction = tech_data["direction"]
        tech_pts  = tech_data["tech_score"]

        # Update price from LTP
        ltp_val = ltp_map.get(f"NSE:{sym}", {}).get("last_price", 0)
        price   = float(ltp_val) if ltp_val else tech_data["price"]

        # BUG3 FIX: Recalculate tech direction with live price (cached EMA/RSI, live price)
        live_tech = dict(tech_data)
        live_tech["price"] = price
        direction, tech_pts, _ = tech_score(live_tech)
        if direction == "NEUTRAL":
            time.sleep(0.3); continue

        # Get option chain
        expiry = get_nearest_expiry(nfo_df, sym)
        if expiry is None: continue

        opt = get_option_chain_fast(kite, nfo_df, sym, price, expiry)
        if opt is None: continue

        o_pts, o_sigs = opt_score(opt, direction, price)   # BUG1 FIX: pass live price
        total         = tech_pts + o_pts

        if total < MIN_SCORE_TO_LOG:
            time.sleep(0.3); continue

        signal_count += 1

        # Select option symbol and premium
        if direction == "CALL":
            opt_sym  = opt["ce_sym"]
            premium  = opt["ce_prem"]
        else:
            opt_sym  = opt["pe_sym"]
            premium  = opt["pe_prem"]

        sl_prem = round(premium * 0.65, 1)
        t1_prem = round(premium * 1.50, 1)
        t2_prem = round(premium * 1.80, 1)
        atr     = tech_data["atr"]

        # ── Log to signals CSV ─────────────────────────────────────────────
        append_signal({
            "timestamp"  : ts,
            "date"       : date_str,
            "time"       : time_str,
            "symbol"     : sym,
            "direction"  : direction,
            "tech_score" : tech_pts,
            "opt_score"  : o_pts,
            "total_score": total,
            "price"      : price,
            "ema20"      : tech_data["ema20"],
            "ema50"      : tech_data["ema50"],
            "rsi"        : tech_data["rsi"],
            "atr"        : atr,
            "pcr"        : opt["pcr"],
            "max_pain"   : opt["max_pain"],
            "atm_strike" : opt["atm"],
            "ce_prem"    : opt["ce_prem"],
            "pe_prem"    : opt["pe_prem"],
            "ce_sym"     : opt["ce_sym"],
            "pe_sym"     : opt["pe_sym"],
            "lot_size"   : opt["lot_size"],
            "signals"    : tech_data["signals"] + "|" + "|".join(o_sigs),
        })

        # ── Check if already tracked ───────────────────────────────────────
        already_tracked = False
        if os.path.exists(ENTRIES_FILE):
            edf = pd.read_csv(ENTRIES_FILE)
            open_e = edf[edf["status"].str.startswith("OPEN", na=False)]
            already_tracked = ((open_e["symbol"] == sym) &
                               (open_e["direction"] == direction)).any()

        # ── New entry if score >= threshold and not already tracked ────────
        if total >= MIN_SCORE_ENTRY and not already_tracked and premium > 0:
            new_entries += 1
            lot_size     = opt["lot_size"]
            investment   = round(premium * lot_size, 0)

            # Save to entries tracker
            if os.path.exists(ENTRIES_FILE):
                edf = pd.read_csv(ENTRIES_FILE)
            else:
                from vishal_fno_screener import TRACKER_COLS
                edf = pd.DataFrame(columns=TRACKER_COLS)

            exp_str = expiry.strftime("%Y-%m-%d") if hasattr(expiry, "strftime") else str(expiry)
            if direction == "CALL":
                stock_sl = round(price - 1.5 * atr, 1)
            else:
                stock_sl = round(price + 1.5 * atr, 1)

            new_row = {
                "date_added"       : date_str,
                "symbol"           : sym,
                "direction"        : direction,
                "option_symbol"    : opt_sym,
                "strike"           : opt["atm"],
                "expiry"           : exp_str,
                "lot_size"         : lot_size,
                "entry_premium"    : premium,
                "sl_premium"       : sl_prem,
                "t1_premium"       : t1_prem,
                "t2_premium"       : t2_prem,
                "stock_price"      : price,
                "stock_sl"         : stock_sl,
                "pcr"              : opt["pcr"],
                "max_pain"         : opt["max_pain"],
                "score"            : total,
                "rank"             : len(edf) + 1,
                "current_premium"  : premium,
                "current_return_pct": 0.0,
                "last_checked"     : date_str,
                "status"           : "OPEN",
                "lots"             : 1,
                "investment"       : investment,
                "pnl_rs"           : 0.0,
            }
            edf = pd.concat([edf, pd.DataFrame([new_row])], ignore_index=True)
            edf.to_csv(ENTRIES_FILE, index=False)

            # Log to history
            append_history({
                "event_time"    : ts,
                "date"          : date_str,
                "time"          : time_str,
                "symbol"        : sym,
                "direction"     : direction,
                "option_symbol" : opt_sym,
                "entry_time"    : ts,
                "entry_premium" : premium,
                "current_premium": premium,
                "pnl_pct"       : 0.0,
                "pnl_rs"        : 0.0,
                "lot_size"      : lot_size,
                "sl_premium"    : sl_prem,
                "t1_premium"    : t1_prem,
                "t2_premium"    : t2_prem,
                "event_type"    : "NEW_ENTRY",
                "hold_minutes"  : 0,
                "notes"         : f"score={total} pcr={opt['pcr']} rsi={tech_data['rsi']:.0f}",
            })

            col = Fore.GREEN if direction == "CALL" else Fore.RED
            print(f"  {col}🆕 NEW ENTRY  {sym:<12} {direction}  score={total}  prem=₹{premium}  pcr={opt['pcr']}{Style.RESET_ALL}")

        time.sleep(0.4)

    # ── Update existing open entries ───────────────────────────────────────
    if os.path.exists(ENTRIES_FILE):
        edf      = pd.read_csv(ENTRIES_FILE)
        open_edf = edf[edf["status"].str.startswith("OPEN", na=False)]

        for idx, row in open_edf.iterrows():
            opt_sym  = row["option_symbol"]
            entry    = float(row["entry_premium"])
            sl       = float(row["sl_premium"])
            t1       = float(row["t1_premium"])
            t2       = float(row["t2_premium"])
            lot_size = int(row["lot_size"])
            _lots_v  = row.get("lots", 1)
            lots     = int(_lots_v) if pd.notna(_lots_v) else 1
            entry_ts = str(row.get("date_added", date_str)) + " 09:15"

            try:
                q   = kite.quote([f"NFO:{opt_sym}"])
                ltp = float(q.get(f"NFO:{opt_sym}", {}).get("last_price", 0) or 0)
            except Exception:
                continue

            if ltp <= 0: continue

            ret_pct   = round((ltp / entry - 1) * 100, 1)
            pnl_rs    = round((ltp - entry) * lot_size * lots, 0)
            entry_dt  = datetime.strptime(entry_ts, "%Y-%m-%d %H:%M")
            hold_mins = int((now - entry_dt).total_seconds() / 60)

            # Determine status + event type
            if ltp <= sl:     new_status = "SL-HIT ❌"; event = "SL_HIT"
            elif ltp >= t2:   new_status = "T2-HIT 🔥"; event = "T2_HIT"
            elif ltp >= t1:   new_status = "T1-HIT ✅"; event = "T1_HIT"
            else:             new_status = f"OPEN ({ret_pct:+.1f}%)"; event = "UPDATE"

            edf.at[idx, "current_premium"]    = ltp
            edf.at[idx, "current_return_pct"] = ret_pct
            edf.at[idx, "pnl_rs"]             = pnl_rs
            edf.at[idx, "last_checked"]        = date_str
            edf.at[idx, "status"]              = new_status

            # Log every event to history
            append_history({
                "event_time"    : ts,
                "date"          : date_str,
                "time"          : time_str,
                "symbol"        : row["symbol"],
                "direction"     : row["direction"],
                "option_symbol" : opt_sym,
                "entry_time"    : entry_ts,
                "entry_premium" : entry,
                "current_premium": ltp,
                "pnl_pct"       : ret_pct,
                "pnl_rs"        : pnl_rs,
                "lot_size"      : lot_size,
                "sl_premium"    : sl,
                "t1_premium"    : t1,
                "t2_premium"    : t2,
                "event_type"    : event,
                "hold_minutes"  : hold_mins,
                "notes"         : new_status,
            })

            col = Fore.GREEN if pnl_rs >= 0 else Fore.RED
            print(f"  {row['symbol']:<12} {row['direction']:<5}  "
                  f"₹{ltp:<8.1f}  {col}{ret_pct:+.1f}%  ₹{pnl_rs:+,.0f}{Style.RESET_ALL}  {new_status}")
            time.sleep(0.3)

        edf.to_csv(ENTRIES_FILE, index=False)

    print(f"\n  {'─'*55}")
    print(f"  Signals logged : {signal_count}")
    print(f"  New entries    : {new_entries}")
    print(f"  Log file       : {SIGNALS_LOG}")


# =============================================================================
# 📊  ANALYSIS REPORT
# =============================================================================

def analysis_report():
    print(f"\n{Fore.CYAN}{'='*68}")
    print(f"  📊 F&O DATA ANALYSIS REPORT")
    print(f"  Generated : {datetime.now().strftime('%d %b %Y %H:%M')}")
    print(f"{'='*68}{Style.RESET_ALL}\n")

    # ── 1. HISTORY summary ────────────────────────────────────────────────
    if not os.path.exists(HISTORY_FILE):
        print(f"{Fore.YELLOW}No history data yet. Run --tick first during market hours.{Style.RESET_ALL}")
        return

    hdf = pd.read_csv(HISTORY_FILE)
    print(f"  Total events logged : {len(hdf)}")
    print(f"  Date range          : {hdf['date'].min()} → {hdf['date'].max()}")
    print(f"  Unique symbols      : {hdf['symbol'].nunique()}")

    # ── 2. ENTRY/EXIT timeline ────────────────────────────────────────────
    entries  = hdf[hdf["event_type"] == "NEW_ENTRY"]
    sl_hits  = hdf[hdf["event_type"] == "SL_HIT"]
    t1_hits  = hdf[hdf["event_type"] == "T1_HIT"]
    t2_hits  = hdf[hdf["event_type"] == "T2_HIT"]
    open_pos = hdf[hdf["event_type"] == "UPDATE"]

    print(f"\n{Fore.YELLOW}{'─'*68}")
    print(f"  ENTRY / EXIT SUMMARY")
    print(f"{'─'*68}{Style.RESET_ALL}")
    print(f"  Total entries found  : {len(entries)}")
    print(f"  SL hit        ❌     : {len(sl_hits['symbol'].unique())} positions")
    print(f"  T1 hit        ✅     : {len(t1_hits['symbol'].unique())} positions")
    print(f"  T2 hit        🔥     : {len(t2_hits['symbol'].unique())} positions")
    print(f"  Still open           : {len(open_pos['symbol'].unique())} positions")

    # ── 3. Detailed per-entry breakdown ───────────────────────────────────
    if len(entries) > 0:
        print(f"\n{Fore.CYAN}{'─'*68}")
        print(f"  DETAILED ENTRY LOG — with timestamps")
        print(f"{'─'*68}{Style.RESET_ALL}\n")

        rows = []
        for _, entry_row in entries.iterrows():
            sym      = entry_row["symbol"]
            dire     = entry_row["direction"]
            opt_sym  = entry_row["option_symbol"]
            entry_t  = entry_row["event_time"]
            entry_p  = float(entry_row["entry_premium"])
            sl_p     = float(entry_row["sl_premium"])
            t1_p     = float(entry_row["t1_premium"])
            t2_p     = float(entry_row["t2_premium"])
            lot_size = int(entry_row["lot_size"])

            # Find exit event for this symbol+direction
            sym_hist = hdf[
                (hdf["symbol"] == sym) &
                (hdf["direction"] == dire) &
                (hdf["event_type"].isin(["SL_HIT","T1_HIT","T2_HIT"]))
            ].sort_values("event_time")

            if len(sym_hist) > 0:
                exit_row   = sym_hist.iloc[0]
                exit_type  = exit_row["event_type"]
                exit_time  = exit_row["event_time"]
                exit_prem  = float(exit_row["current_premium"])
                hold_mins  = int(exit_row["hold_minutes"])
                pnl_pct    = float(exit_row["pnl_pct"])
                pnl_rs     = float(exit_row["pnl_rs"])
                h_str      = f"{hold_mins//60}h{hold_mins%60}m"
            else:
                # Still open — get latest update
                latest = hdf[
                    (hdf["symbol"] == sym) &
                    (hdf["direction"] == dire)
                ].sort_values("event_time").iloc[-1]
                exit_type = "OPEN"
                exit_time = latest["event_time"]
                exit_prem = float(latest["current_premium"])
                hold_mins = int(latest["hold_minutes"])
                pnl_pct   = float(latest["pnl_pct"])
                pnl_rs    = float(latest["pnl_rs"])
                h_str     = f"{hold_mins//60}h{hold_mins%60}m"

            # Color
            if exit_type == "T2_HIT":   outcome = "🔥T2"; col = Fore.GREEN
            elif exit_type == "T1_HIT": outcome = "✅T1"; col = Fore.CYAN
            elif exit_type == "SL_HIT": outcome = "❌SL"; col = Fore.RED
            else:                       outcome = "⏳OPEN"; col = Fore.WHITE

            arrow = "📈" if dire == "CALL" else "📉"
            rows.append([
                f"{arrow}{sym}",
                dire,
                entry_t[11:16],    # entry time HH:MM
                f"₹{entry_p:.1f}",
                f"₹{exit_prem:.1f}",
                f"{col}{pnl_pct:+.1f}%{Style.RESET_ALL}",
                f"{col}₹{pnl_rs:+,.0f}{Style.RESET_ALL}",
                h_str,
                exit_time[11:16] if exit_type != "OPEN" else "OPEN",
                f"{col}{outcome}{Style.RESET_ALL}",
            ])

        print(pd.DataFrame(rows, columns=[
            "Symbol","Dir","EntryTime","EntryPrem","ExitPrem",
            "Ret%","P&L(₹)","Held","ExitTime","Result"
        ]).to_string(index=False))

    # ── 4. Time-of-day analysis ───────────────────────────────────────────
    if len(entries) > 0:
        print(f"\n{Fore.CYAN}{'─'*68}")
        print(f"  SIGNAL QUALITY BY TIME OF DAY")
        print(f"{'─'*68}{Style.RESET_ALL}\n")

        time_buckets = {
            "9:15–10:00 (Opening)": ("09:15","10:00"),
            "10:00–11:30 (Morning)": ("10:00","11:30"),
            "11:30–13:00 (Mid)":    ("11:30","13:00"),
            "13:00–14:30 (Afternoon)": ("13:00","14:30"),
            "14:30–15:30 (Closing)": ("14:30","15:30"),
        }

        sig_log = pd.read_csv(SIGNALS_LOG) if os.path.exists(SIGNALS_LOG) else pd.DataFrame()
        if len(sig_log) > 0:
            for bucket, (t_start, t_end) in time_buckets.items():
                bucket_sigs = sig_log[
                    (sig_log["time"] >= t_start) &
                    (sig_log["time"] < t_end)
                ]
                if len(bucket_sigs) == 0: continue
                avg_score = bucket_sigs["total_score"].mean()
                strong    = len(bucket_sigs[bucket_sigs["total_score"] >= 7])
                print(f"  {bucket:<35}  signals={len(bucket_sigs):3}  "
                      f"avg_score={avg_score:.1f}  high_conv={strong}")

    # ── 5. Best performing stocks ─────────────────────────────────────────
    closed = hdf[hdf["event_type"].isin(["SL_HIT","T1_HIT","T2_HIT"])]
    if len(closed) > 0:
        print(f"\n{Fore.CYAN}{'─'*68}")
        print(f"  STOCK PERFORMANCE BREAKDOWN")
        print(f"{'─'*68}{Style.RESET_ALL}\n")

        stock_stats = []
        for sym in closed["symbol"].unique():
            s_data = closed[closed["symbol"] == sym]
            wins   = len(s_data[s_data["event_type"].isin(["T1_HIT","T2_HIT"])])
            losses = len(s_data[s_data["event_type"] == "SL_HIT"])
            total  = wins + losses
            wr     = round(wins/total*100, 0) if total > 0 else 0
            avg_pnl= round(s_data["pnl_rs"].mean(), 0)
            stock_stats.append({"Symbol": sym, "Trades": total, "Wins": wins,
                                 "SL": losses, "WinRate%": wr, "AvgP&L": f"₹{avg_pnl:+,.0f}"})

        sdf = pd.DataFrame(stock_stats).sort_values("WinRate%", ascending=False)
        print(sdf.to_string(index=False))

    # ── 6. Overall scorecard ──────────────────────────────────────────────
    print(f"\n{Fore.CYAN}{'='*68}")
    print(f"  OVERALL SCORECARD")
    print(f"{'='*68}{Style.RESET_ALL}")

    n_entries = len(entries)
    n_sl      = len(sl_hits["option_symbol"].unique()) if len(sl_hits) > 0 else 0
    n_t1      = len(t1_hits["option_symbol"].unique()) if len(t1_hits) > 0 else 0
    n_t2      = len(t2_hits["option_symbol"].unique()) if len(t2_hits) > 0 else 0
    n_wins    = n_t1 + n_t2
    n_closed  = n_wins + n_sl
    win_pct   = round(n_wins/n_closed*100, 1) if n_closed > 0 else 0

    total_pnl = hdf[hdf["event_type"].isin(["SL_HIT","T1_HIT","T2_HIT"])]["pnl_rs"].sum()

    print(f"  Entries logged : {n_entries}")
    print(f"  T2-HIT 🔥     : {n_t2}")
    print(f"  T1-HIT ✅     : {n_t1}")
    print(f"  SL-HIT ❌     : {n_sl}")
    print(f"  Win rate       : {win_pct}%  ({n_wins}W / {n_sl}L)")
    pnl_col = Fore.GREEN if total_pnl >= 0 else Fore.RED
    print(f"  Total P&L      : {pnl_col}₹{total_pnl:+,.0f}{Style.RESET_ALL}  (1 lot each)")
    print()

    if n_closed >= 5:
        if win_pct >= 60:
            print(f"{Fore.GREEN}✅ STRATEGY WORKING — Win rate {win_pct}% over {n_closed} closed trades{Style.RESET_ALL}")
        elif win_pct >= 40:
            print(f"{Fore.YELLOW}⚠️  MARGINAL — {win_pct}% — review signal filters{Style.RESET_ALL}")
        else:
            print(f"{Fore.RED}❌ UNDERPERFORMING — {win_pct}% — tighten score threshold{Style.RESET_ALL}")
    else:
        print(f"{Fore.YELLOW}📊 Need 5+ closed trades for meaningful analysis ({n_closed} so far){Style.RESET_ALL}")

    print(f"\n  Data files:")
    print(f"    Signals log  : {SIGNALS_LOG}")
    print(f"    History log  : {HISTORY_FILE}")
    print(f"    Entries      : {ENTRIES_FILE}\n")


# =============================================================================
# 🚀  MAIN
# =============================================================================

def main():
    mode = next((a for a in sys.argv[1:] if a.startswith("--")), None)

    if mode == "--report":
        analysis_report()
        return

    kite = get_kite()
    nse_df, nfo_df = load_instruments(kite)

    if mode == "--morning":
        morning_scan(kite, nse_df, nfo_df)
    elif mode == "--tick":
        tick_scan(kite, nfo_df)
    elif mode == "--entries":
        analysis_report()
    else:
        print("Usage:")
        print("  python3 fno_collector.py --morning   (run at 9:00 AM)")
        print("  python3 fno_collector.py --tick      (run every 15 min)")
        print("  python3 fno_collector.py --report    (analysis report)")


if __name__ == "__main__":
    main()
