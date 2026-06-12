"""
smi_paper.py
────────────
SMI strategy — PAPER trading engine for the stock F&O universe.
Replaces the old vishal_fno_screener daily-pick strategy (retired 2026-06-11).
2-week paper validation before any fine-tuning / live decision.

Strategy (frozen 2026-06-11 — see smi_backtest.py / smi_pe_tuning.py /
smi_single_filter.py for the evidence):
  Indicator : SMI, RMA (Wilder) double-smoothing, %K 14 / smooth 3 / signal EMA 3,
              computed on 15m and 1h (1h bars anchored 09:15, used after close)
  CE entry  : 15m SMI crosses up through −40 (same bar) · SMI > signal
              · 1h SMI > 1h signal by +5 · 1h SMI in (0, 50)
  PE entry  : 15m SMI crossed down through +45, entry on cross bar or next 6 bars,
              first bar where: SMI < signal · SMI < +45 · 1h SMI < 1h signal by −5
              · 1h SMI in (0, 50) · stock below day VWAP
  Window    : entry bars labelled 09:30–14:30 (fill at bar close)
  Exits     : hard SL 1% of stock entry · trail arms at +1.5% peak then exit on
              close vs SMA8 · force close at the 15:15 bar close
  Paper fill: buy 1 lot nearest-expiry ATM option (CE/PE) at LTP; P&L tracked on
              option premium, exits driven by STOCK price (as backtested)
  NIFTY 1h SMI bearish = conviction tag on PE entries (logged, not a gate)

Files:
  state   : smi_paper_state.json   (open trades + fired-signal keys)
  tracker : fno_tracker.csv        (dashboard /api/fno reads this — schema kept)
  log     : smi_paper_log.csv      (clean per-trade log for the 2-week review)

Cron (every 15m bar close + 2 min, Mon–Fri):
  47 9 * * 1-5         → processes the 09:30 bar
  2,17,32,47 10-14 ... → bars 09:45 … 14:30
  2,17,31 15 ...       → bars 14:45, 15:00 and the 15:15 force-close bar

Usage:
  python3 smi_paper.py            # one pass (process latest closed bar)
  python3 smi_paper.py --dry      # no writes, print decisions only
"""

import os, sys, json, time, warnings
import pandas as pd
import numpy as np
from datetime import date, datetime, timedelta

warnings.filterwarnings("ignore")

try:
    from dotenv import load_dotenv
except ModuleNotFoundError:
    def load_dotenv(dotenv_path=None, **_kw):
        _p = os.path.expanduser(dotenv_path or "~/.env")
        if not os.path.isfile(_p): return False
        with open(_p) as f:
            for ln in f:
                ln = ln.strip()
                if not ln or ln.startswith("#") or "=" not in ln: continue
                k, _, v = ln.partition("=")
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
        return True

BASE_DIR    = os.path.dirname(os.path.abspath(__file__))
TOKEN_FILE  = os.path.join(os.path.expanduser("~"), "VISHAL_RAJPUT", "state", "access_token.json")
OHLCV_CACHE = os.path.join(BASE_DIR, "fno_ohlcv_cache.json")
INST_NFO    = os.path.join(BASE_DIR, "inst_cache_nfo.csv")
STATE_FILE  = os.path.join(BASE_DIR, "smi_paper_state.json")
TRACKER     = os.path.join(BASE_DIR, "fno_tracker.csv")
TRADE_LOG   = os.path.join(BASE_DIR, "smi_paper_log.csv")

# ── Strategy constants (FROZEN 2026-06-11 — do not tune before 2-week review) ──
SMI_LENGTH   = 14
SMI_SMOOTH   = 3
SIGNAL_EMA   = 3
OS_CE        = -40.0
OB_PE        = 45.0
MARGIN_1H    = 5.0
ZONE_LO      = 0.0
ZONE_HI      = 50.0
PE_LOOKAHEAD = 6
SL_PCT       = 1.0
TRAIL_ARM    = 1.5
ENTRY_START  = "09:30"
ENTRY_END    = "14:30"
LAST_BAR     = "15:15"
LOOKBACK_DAYS = 25          # enough for SMI-1h warmup + SMA8
NIFTY_TOKEN  = 256265

TRACKER_COLS = [
    "date_added","symbol","direction","option_symbol","strike","expiry","lot_size",
    "entry_premium","sl_premium","t1_premium","t2_premium","stock_price","stock_sl",
    "pcr","max_pain","score","rank","current_premium","current_return_pct",
    "last_checked","status","lots","investment","pnl_rs","structure","sell_strike",
    "sell_symbol","net_debit","elite","regime","signals","atm_at_entry",
    "entry_atm_dist","atm_oi","otm1_strike","otm1_ltp","otm1_oi","otm2_strike",
    "otm2_ltp","otm2_oi","itm1_strike","itm1_ltp","itm1_oi",
]

LOG_COLS = [
    "entry_time","exit_time","symbol","direction","option_symbol","lot_size",
    "stock_entry","stock_exit","stock_sl","entry_premium","exit_premium",
    "pnl_pct_stock","pnl_rs","exit_reason","peak_pnl_pct","conviction","confirm_bars",
    "signal_detail",
]


# ─────────────────────────────────────────────────────────────────────────────
# Indicators (identical math to smi_backtest.py)
# ─────────────────────────────────────────────────────────────────────────────

def rma(s: pd.Series, p: int) -> pd.Series:
    return s.ewm(alpha=1.0 / p, adjust=False).mean()


def compute_smi(df: pd.DataFrame) -> tuple:
    hh = df["high"].rolling(SMI_LENGTH).max()
    ll = df["low"].rolling(SMI_LENGTH).min()
    diff = df["close"] - (hh + ll) / 2.0
    rng  = (hh - ll) / 2.0
    num = rma(rma(diff, SMI_SMOOTH), SMI_SMOOTH)
    den = rma(rma(rng,  SMI_SMOOTH), SMI_SMOOTH).replace(0, np.nan)
    smi = 100.0 * num / den
    sig = smi.ewm(span=SIGNAL_EMA, adjust=False).mean()
    return smi, sig


def resample_1h(df: pd.DataFrame) -> pd.DataFrame:
    h = df.resample("60min", origin=df.index.normalize().min() + pd.Timedelta("9h15min")).agg(
        {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
    ).dropna(subset=["close"])
    h["known_at"] = h.index + pd.Timedelta("60min")
    return h


def smi_1h_at(df15: pd.DataFrame, ts) -> tuple:
    """(smi_1h, sig_1h) of the last 1h bar with known_at <= ts (bar label).
    Matches the backtest convention exactly (align_1h_smi merge_asof on label)."""
    h = resample_1h(df15)
    smi, sig = compute_smi(h)
    mask = h["known_at"] <= ts
    if not mask.any():
        return np.nan, np.nan
    i = int(np.where(mask)[0][-1])
    return float(smi.iloc[i]), float(sig.iloc[i])


def day_vwap_at(df15: pd.DataFrame, ts) -> float:
    day = df15[df15.index.normalize() == ts.normalize()]
    day = day[day.index <= ts]
    if day.empty or day["volume"].sum() == 0:
        return np.nan
    tp = (day["high"] + day["low"] + day["close"]) / 3
    return float((tp * day["volume"]).sum() / day["volume"].sum())


# ─────────────────────────────────────────────────────────────────────────────
# Broker helpers
# ─────────────────────────────────────────────────────────────────────────────

def get_kite():
    from kiteconnect import KiteConnect
    load_dotenv(os.path.expanduser("~/.env"))
    kite = KiteConnect(api_key=os.getenv("KITE_API_KEY", ""))
    with open(TOKEN_FILE) as f:
        saved = json.load(f)
    if saved.get("date") != date.today().isoformat():
        print("Kite token expired — skip run"); sys.exit(0)
    kite.set_access_token(saved["access_token"])
    return kite


def fetch_15m(kite, token: int):
    to_dt = datetime.now().replace(second=0, microsecond=0)
    candles = kite.historical_data(token, to_dt - timedelta(days=LOOKBACK_DAYS),
                                   to_dt, "15minute", continuous=False)
    if not candles:
        return None
    df = pd.DataFrame(candles)
    df["date"] = pd.to_datetime(df["date"]).dt.tz_localize(None)
    df = df.set_index("date").sort_index()
    # drop the still-forming bar: a 15m bar labelled T is closed once now >= T+15m
    cutoff = pd.Timestamp(to_dt) - pd.Timedelta("15min")
    return df[df.index <= cutoff]


def pick_atm_option(nfo: pd.DataFrame, symbol: str, spot: float, opt_type: str):
    sub = nfo[(nfo["name"] == symbol) & (nfo["instrument_type"] == opt_type)]
    sub = sub[pd.to_datetime(sub["expiry"]).dt.date >= date.today()]
    if sub.empty:
        return None
    expiry = sub["expiry"].min()
    sub = sub[sub["expiry"] == expiry].copy()
    sub["dist"] = (sub["strike"] - spot).abs()
    r = sub.nsmallest(1, "dist").iloc[0]
    return {"option_symbol": r["tradingsymbol"], "strike": float(r["strike"]),
            "expiry": str(r["expiry"]), "lot_size": int(r["lot_size"])}


def get_ltps(kite, symbols: list) -> dict:
    out = {}
    for i in range(0, len(symbols), 200):
        chunk = [f"NFO:{s}" for s in symbols[i:i + 200]]
        try:
            q = kite.ltp(chunk)
            for k, v in q.items():
                out[k.split(":", 1)[1]] = float(v["last_price"])
        except Exception as e:
            print(f"  ltp error: {e}")
    return out


def send_telegram(msg: str):
    try:
        import requests
        load_dotenv(os.path.expanduser("~/.env"))
        token, gid = os.getenv("TG_TOKEN", ""), os.getenv("TG_GROUP_ID", "")
        if not token or not gid:
            return
        requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                      data={"chat_id": gid, "text": msg, "parse_mode": "HTML"}, timeout=10)
    except Exception as e:
        print(f"  TG error: {e}")


# ─────────────────────────────────────────────────────────────────────────────
# State + tracker
# ─────────────────────────────────────────────────────────────────────────────

def load_state() -> dict:
    if os.path.isfile(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return {"open_trades": {}, "fired_keys": []}


def save_state(state: dict):
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=1, default=str)
    os.replace(tmp, STATE_FILE)


def tracker_df() -> pd.DataFrame:
    if os.path.isfile(TRACKER):
        return pd.read_csv(TRACKER)
    return pd.DataFrame(columns=TRACKER_COLS)


def tracker_write(df: pd.DataFrame):
    df.to_csv(TRACKER, index=False)


def tracker_upsert(trade: dict, status: str, cur_prem: float):
    df = tracker_df()
    key = trade["option_symbol"]
    ret = (cur_prem - trade["entry_premium"]) / trade["entry_premium"] * 100 \
        if trade["entry_premium"] else 0.0
    pnl = (cur_prem - trade["entry_premium"]) * trade["lot_size"]
    row = {c: "" for c in TRACKER_COLS}
    row.update({
        "date_added": trade["entry_time"][:10], "symbol": trade["symbol"],
        "direction": "CALL" if trade["direction"] == "CE" else "PUT",
        "option_symbol": key, "strike": trade["strike"], "expiry": trade["expiry"],
        "lot_size": trade["lot_size"], "entry_premium": trade["entry_premium"],
        "stock_price": trade["stock_entry"], "stock_sl": round(trade["sl_price"], 2),
        "score": "", "rank": "", "current_premium": cur_prem,
        "current_return_pct": round(ret, 1),
        "last_checked": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "status": status, "lots": 1,
        "investment": round(trade["entry_premium"] * trade["lot_size"], 0),
        "pnl_rs": round(pnl, 0), "structure": "SMI",
        "regime": trade.get("conviction", ""), "signals": trade.get("signal_detail", ""),
        "atm_at_entry": trade["strike"], "entry_atm_dist": 0.0,
    })
    df = df[df["option_symbol"] != key]
    df = pd.concat([df, pd.DataFrame([row])], ignore_index=True)
    tracker_write(df)


def log_exit(trade: dict, exit_ts, stock_exit: float, exit_prem: float, reason: str):
    pnl_stock = (stock_exit - trade["stock_entry"]) / trade["stock_entry"] * 100
    if trade["direction"] == "PE":
        pnl_stock = -pnl_stock
    pnl_rs = (exit_prem - trade["entry_premium"]) * trade["lot_size"]
    row = {
        "entry_time": trade["entry_time"], "exit_time": str(exit_ts),
        "symbol": trade["symbol"], "direction": trade["direction"],
        "option_symbol": trade["option_symbol"], "lot_size": trade["lot_size"],
        "stock_entry": trade["stock_entry"], "stock_exit": round(stock_exit, 2),
        "stock_sl": round(trade["sl_price"], 2),
        "entry_premium": trade["entry_premium"], "exit_premium": exit_prem,
        "pnl_pct_stock": round(pnl_stock, 3), "pnl_rs": round(pnl_rs, 0),
        "exit_reason": reason, "peak_pnl_pct": round(trade.get("peak", 0.0), 3),
        "conviction": trade.get("conviction", ""),
        "confirm_bars": trade.get("confirm_bars", 0),
        "signal_detail": trade.get("signal_detail", ""),
    }
    new = pd.DataFrame([row])
    if os.path.isfile(TRADE_LOG):
        new.to_csv(TRADE_LOG, mode="a", header=False, index=False)
    else:
        new.to_csv(TRADE_LOG, index=False, columns=LOG_COLS)
    return pnl_stock, pnl_rs


# ─────────────────────────────────────────────────────────────────────────────
# Signal scan on one symbol — returns entry dict if the LAST closed bar fires
# ─────────────────────────────────────────────────────────────────────────────

def scan_entry(sym: str, df: pd.DataFrame, fired: set, nifty_bear: bool):
    if len(df) < SMI_LENGTH * 4:
        return None
    smi, sig = compute_smi(df)
    sv, gv = smi.values, sig.values
    last = len(df) - 1
    ts = df.index[last]
    tstr = ts.strftime("%H:%M")
    if not (ENTRY_START <= tstr <= ENTRY_END):
        return None
    if np.isnan(sv[last]) or np.isnan(sv[last - 1]):
        return None

    smi1h, sig1h = smi_1h_at(df, ts)
    if np.isnan(smi1h) or np.isnan(sig1h):
        return None
    in_zone = ZONE_LO < smi1h < ZONE_HI

    # CE — same-bar cross
    if (sv[last - 1] <= OS_CE and sv[last] > OS_CE and sv[last] > gv[last]
            and smi1h > sig1h + MARGIN_1H and in_zone):
        key = f"{sym}:CE:{ts.isoformat()}"
        if key not in fired:
            return {"direction": "CE", "ts": ts, "key": key, "conviction": "NORMAL",
                    "confirm_bars": 0,
                    "detail": (f"SMI v1 | CE cross {OS_CE:+.0f} | smi15={sv[last]:.1f} "
                               f"| 1h={smi1h:.1f}/sig{sig1h:.1f}")}

    # PE — cross within last PE_LOOKAHEAD bars, last bar is FIRST confirm
    today = df[df.index.normalize() == ts.normalize()]
    for back in range(0, PE_LOOKAHEAD + 1):
        ci = last - back
        if ci < 1 or df.index[ci].normalize() != ts.normalize():
            break
        if not (sv[ci - 1] >= OB_PE and sv[ci] < OB_PE):
            continue
        # found the cross; last bar must be the first confirming bar since ci
        vwap = day_vwap_at(df, ts)

        def confirms(j):
            s1h, g1h = smi_1h_at(df, df.index[j])
            if np.isnan(s1h) or np.isnan(g1h): return False
            if not (ZONE_LO < s1h < ZONE_HI): return False
            if not (s1h < g1h - MARGIN_1H): return False
            if not (sv[j] < gv[j] and sv[j] < OB_PE): return False
            vw = day_vwap_at(df, df.index[j])
            return not np.isnan(vw) and df["close"].iloc[j] < vw

        if any(confirms(j) for j in range(ci, last)):
            break   # an earlier bar already confirmed — that signal is spent
        if confirms(last):
            key = f"{sym}:PE:{df.index[ci].isoformat()}"
            if key not in fired:
                conv = "PE-HIGH (NIFTY 1h bear)" if nifty_bear else "NORMAL"
                return {"direction": "PE", "ts": ts, "key": key, "conviction": conv,
                        "confirm_bars": back,
                        "detail": (f"SMI v1 | PE cross +{OB_PE:.0f} (bar -{back}) "
                                   f"| smi15={sv[last]:.1f} | 1h={smi1h:.1f}/sig{sig1h:.1f} "
                                   f"| below VWAP")}
        break
    return None


# ─────────────────────────────────────────────────────────────────────────────
# Exit management — replay bars since last check (bar-based, like backtest)
# ─────────────────────────────────────────────────────────────────────────────

def check_exit(trade: dict, df: pd.DataFrame):
    """Returns (exit_ts, stock_exit_px, reason) or None. Mutates trade peak/sl/armed."""
    sgn = 1 if trade["direction"] == "CE" else -1
    entry = trade["stock_entry"]
    sma8 = df["close"].rolling(8).mean()
    start_ts = pd.Timestamp(trade["last_bar_checked"])
    bars = df[df.index > start_ts]
    for ts, row in bars.iterrows():
        if str(ts.date()) != trade["entry_time"][:10]:
            # day rolled over without a force close (missed runs) — close at prev close
            prev = df[df.index < ts]["close"].iloc[-1]
            return ts, float(prev), "EOD-LATE"
        hi, lo, cl = float(row["high"]), float(row["low"]), float(row["close"])
        adverse = lo if sgn == 1 else hi
        if (adverse - trade["sl_price"]) * sgn <= 0:
            return ts, trade["sl_price"], "SL-HIT"
        pnl = (cl - entry) * sgn / entry * 100
        trade["peak"] = max(trade.get("peak", 0.0), pnl)
        if trade["peak"] >= TRAIL_ARM:
            trade["armed"] = True
        if trade.get("armed"):
            s8 = sma8.loc[ts]
            if not np.isnan(s8) and (cl - s8) * sgn < 0:
                return ts, cl, "TRAIL-SMA8"
        if ts.strftime("%H:%M") >= LAST_BAR:
            return ts, cl, "EOD-CLOSE"
        trade["last_bar_checked"] = str(ts)
    return None


# ─────────────────────────────────────────────────────────────────────────────
# Main pass
# ─────────────────────────────────────────────────────────────────────────────

def main():
    dry = "--dry" in sys.argv
    now = datetime.now()
    if now.weekday() >= 5:
        return
    if not ("09:40" <= now.strftime("%H:%M") <= "15:35"):
        print(f"{now:%H:%M} outside run window"); return

    state = load_state()
    fired = set(state.get("fired_keys", []))
    open_trades = state.get("open_trades", {})

    kite = get_kite()
    symbols = list(json.load(open(OHLCV_CACHE))["symbols"].keys())
    nfo = pd.read_csv(INST_NFO)
    inst_nse = pd.read_csv(os.path.join(BASE_DIR, "inst_cache_nse.csv"))
    inst_nse = inst_nse[inst_nse["instrument_type"] == "EQ"]
    tok = {r["tradingsymbol"]: int(r["instrument_token"])
           for _, r in inst_nse.iterrows() if r["tradingsymbol"] in symbols}

    # NIFTY conviction context (PE tag only)
    nifty_bear = False
    try:
        ndf = fetch_15m(kite, NIFTY_TOKEN)
        if ndf is not None and len(ndf) > SMI_LENGTH * 4:
            s1h, g1h = smi_1h_at(ndf, ndf.index[-1])
            nifty_bear = (not np.isnan(s1h)) and s1h < g1h
    except Exception as e:
        print(f"NIFTY fetch error: {e}")

    entries, exits = [], []
    t0 = time.time()
    for n, sym in enumerate(symbols, 1):
        if sym not in tok:
            continue
        try:
            df = fetch_15m(kite, tok[sym])
        except Exception as e:
            print(f"  {sym}: fetch error {e}"); time.sleep(1); continue
        time.sleep(0.25)
        if df is None or df.empty:
            continue

        # 1) manage open trade
        if sym in open_trades:
            tr = open_trades[sym]
            res = check_exit(tr, df)
            if res:
                exits.append((sym, tr, *res))
            continue   # no new entry while a trade is open or just closed this bar

        # 2) new entry?
        e = scan_entry(sym, df, fired, nifty_bear)
        if e:
            spot = float(df["close"].iloc[-1])
            opt = pick_atm_option(nfo, sym, spot, e["direction"])
            if opt is None:
                print(f"  {sym}: no option found"); continue
            entries.append((sym, spot, e, opt))

    print(f"scan done in {time.time()-t0:.0f}s | entries={len(entries)} exits={len(exits)}")

    if dry:
        for sym, spot, e, opt in entries:
            print(f"  DRY ENTRY {sym} {e['direction']} @ {spot} → {opt['option_symbol']} | {e['detail']}")
        for sym, tr, ts, px, reason in exits:
            print(f"  DRY EXIT  {sym} {tr['direction']} @ {px} ({reason})")
        return

    # premiums for fills
    need = [opt["option_symbol"] for _, _, _, opt in entries] + \
           [tr["option_symbol"] for _, tr, *_ in exits]
    ltps = get_ltps(kite, need) if need else {}

    # exits first
    for sym, tr, ts, px, reason in exits:
        prem = ltps.get(tr["option_symbol"], tr["entry_premium"])
        pnl_stock, pnl_rs = log_exit(tr, ts, px, prem, reason)
        emoji = "✅" if pnl_rs > 0 else "❌"
        status = f"{reason} {emoji}"
        tracker_upsert(tr, status, prem)
        del open_trades[sym]
        send_telegram(
            f"📕 <b>SMI PAPER EXIT</b> {emoji}\n{sym} {tr['direction']} {tr['option_symbol']}\n"
            f"stock {tr['stock_entry']} → {px:.2f} ({pnl_stock:+.2f}%)\n"
            f"prem {tr['entry_premium']} → {prem} = ₹{pnl_rs:+,.0f}\n"
            f"reason: {reason} | peak {tr.get('peak',0):+.2f}%")
        print(f"  EXIT {sym} {reason} stock {pnl_stock:+.2f}% ₹{pnl_rs:+,.0f}")

    # entries
    for sym, spot, e, opt in entries:
        prem = ltps.get(opt["option_symbol"])
        if not prem:
            print(f"  {sym}: no LTP for {opt['option_symbol']}, skip"); continue
        sgn = 1 if e["direction"] == "CE" else -1
        tr = {
            "symbol": sym, "direction": e["direction"],
            "entry_time": str(e["ts"]), "stock_entry": spot,
            "sl_price": spot * (1 - sgn * SL_PCT / 100),
            "peak": 0.0, "armed": False, "last_bar_checked": str(e["ts"]),
            "entry_premium": prem, "conviction": e["conviction"],
            "confirm_bars": e.get("confirm_bars", 0),
            "signal_detail": e["detail"], **opt,
        }
        open_trades[sym] = tr
        fired.add(e["key"])
        tracker_upsert(tr, "OPEN (+0.0%)", prem)
        send_telegram(
            f"📗 <b>SMI PAPER ENTRY</b>\n{sym} {e['direction']} @ {spot}\n"
            f"{opt['option_symbol']} 1 lot ({opt['lot_size']}) @ {prem} "
            f"= ₹{prem*opt['lot_size']:,.0f}\nSL {tr['sl_price']:.2f} (1%) | {e['conviction']}\n"
            f"{e['detail']}")
        print(f"  ENTRY {sym} {e['direction']} @ {spot} {opt['option_symbol']} @ {prem}")

    # refresh open-trade marks on the dashboard
    if open_trades:
        marks = get_ltps(kite, [t["option_symbol"] for t in open_trades.values()])
        for sym, tr in open_trades.items():
            prem = marks.get(tr["option_symbol"], tr["entry_premium"])
            ret = (prem - tr["entry_premium"]) / tr["entry_premium"] * 100
            tracker_upsert(tr, f"OPEN ({ret:+.1f}%)", prem)

    state["open_trades"] = open_trades
    state["fired_keys"] = sorted(fired)[-500:]
    save_state(state)


if __name__ == "__main__":
    main()
