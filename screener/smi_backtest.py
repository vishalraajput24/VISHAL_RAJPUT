"""
smi_backtest.py
───────────────
E2-SMI signal backtest + parameter tuning on the stock F&O universe.

Universe : all symbols in fno_ohlcv_cache.json (119 F&O stocks)
Data     : Kite 15-min candles (last ~58 days), cached to smi_ohlcv_15m.pkl
Signal   : SMI crosses up through oversold  → CE (SMI > signal line)
           SMI crosses down through overbought → PE (SMI < signal line)
Filters  : regime SMA20/50 + RSI 50 + MACD vs signal + 1h SMI alignment
Exits    : hard SL (fixed pts or % of entry), SMA8 trail after arm, force close EOD

Phases:
  1. SMI flavor comparison (double-EMA / double-SMA / double-RMA) + SL mode
  2. Parameter sweep (%K length × smoothing × OB/OS) on best flavor
  3. Price-band calibration (<250 / 250-500 / 500-1000 / 1000-2500 / >2500)
  4. Filter impact (raw SMI vs full filters)

Usage:
  python3 smi_backtest.py            # uses cached candles if present
  python3 smi_backtest.py --refetch  # force refetch from Kite
"""

import os, sys, json, time, pickle, warnings
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
INST_CACHE  = os.path.join(BASE_DIR, "inst_cache_nse.csv")
OHLCV_CACHE = os.path.join(BASE_DIR, "fno_ohlcv_cache.json")
PKL_CACHE   = os.path.join(BASE_DIR, "smi_ohlcv_15m.pkl")
OUT_FILE    = os.path.join(BASE_DIR, "smi_backtest_results.csv")
REPORT_FILE = os.path.join(BASE_DIR, "smi_backtest_report.md")

LOOKBACK_DAYS = 58
ENTRY_START   = "09:30"
ENTRY_END     = "14:30"
LAST_BAR      = "15:15"   # last 15m bar of the day → force close at its close

HARD_SL_PTS   = 10.0
HARD_SL_PCT   = 1.0       # % of entry price
TRAIL_ARM_PTS = 15.0
TRAIL_ARM_PCT = 1.5

SIGNAL_EMA    = 3         # signal line EMA — fixed per spec

BANDS = [(0, 250, "<250"), (250, 500, "250-500"), (500, 1000, "500-1000"),
         (1000, 2500, "1000-2500"), (2500, 1e9, ">2500")]


def price_band(p: float) -> str:
    for lo, hi, name in BANDS:
        if lo <= p < hi:
            return name
    return ">2500"


# ─────────────────────────────────────────────────────────────────────────────
# Data fetch (cached)
# ─────────────────────────────────────────────────────────────────────────────

def fetch_all(refetch: bool = False) -> dict:
    if os.path.isfile(PKL_CACHE) and not refetch:
        with open(PKL_CACHE, "rb") as f:
            data = pickle.load(f)
        print(f"Loaded {len(data)} symbols from cache {PKL_CACHE}")
        return data

    from kiteconnect import KiteConnect
    load_dotenv(os.path.expanduser("~/.env"))
    api_key = os.getenv("KITE_API_KEY", "")
    if not api_key:
        print("KITE_API_KEY not found"); sys.exit(1)
    kite = KiteConnect(api_key=api_key)
    with open(TOKEN_FILE) as f:
        saved = json.load(f)
    if saved.get("date") != date.today().isoformat():
        print("Token expired — refresh first"); sys.exit(1)
    kite.set_access_token(saved["access_token"])
    kite.profile()

    symbols = list(json.load(open(OHLCV_CACHE))["symbols"].keys())
    inst = pd.read_csv(INST_CACHE)
    inst = inst[inst["instrument_type"] == "EQ"]

    to_dt   = datetime.now().replace(second=0, microsecond=0)
    from_dt = to_dt - timedelta(days=LOOKBACK_DAYS)

    data = {}
    for n, sym in enumerate(symbols, 1):
        row = inst[inst["tradingsymbol"] == sym]
        if row.empty:
            print(f"  [{n}/{len(symbols)}] {sym}: token not found, skip"); continue
        token = int(row.iloc[0]["instrument_token"])
        try:
            candles = kite.historical_data(token, from_dt, to_dt, "15minute", continuous=False)
        except Exception as e:
            print(f"  [{n}/{len(symbols)}] {sym}: fetch error {e}"); time.sleep(1); continue
        time.sleep(0.35)
        if not candles:
            print(f"  [{n}/{len(symbols)}] {sym}: no data"); continue
        df = pd.DataFrame(candles)
        df["date"] = pd.to_datetime(df["date"]).dt.tz_localize(None)
        df = df.set_index("date").sort_index()
        data[sym] = df
        if n % 20 == 0:
            print(f"  [{n}/{len(symbols)}] fetched...")

    with open(PKL_CACHE, "wb") as f:
        pickle.dump(data, f)
    print(f"Fetched {len(data)} symbols → {PKL_CACHE}")
    return data


# ─────────────────────────────────────────────────────────────────────────────
# Indicators
# ─────────────────────────────────────────────────────────────────────────────

def _smooth(s: pd.Series, p: int, flavor: str) -> pd.Series:
    if flavor == "ema":
        return s.ewm(span=p, adjust=False).mean()
    if flavor == "sma":
        return s.rolling(p).mean()
    if flavor == "rma":  # Wilder
        return s.ewm(alpha=1.0 / p, adjust=False).mean()
    raise ValueError(flavor)


def compute_smi(df: pd.DataFrame, length: int, smooth: int, flavor: str) -> tuple:
    """Returns (smi, signal). Double-smoothed midpoint-distance oscillator."""
    hh = df["high"].rolling(length).max()
    ll = df["low"].rolling(length).min()
    mid  = (hh + ll) / 2.0
    diff = df["close"] - mid
    rng  = (hh - ll) / 2.0
    num = _smooth(_smooth(diff, smooth, flavor), smooth, flavor)
    den = _smooth(_smooth(rng,  smooth, flavor), smooth, flavor).replace(0, np.nan)
    smi = 100.0 * num / den
    sig = smi.ewm(span=SIGNAL_EMA, adjust=False).mean()
    return smi, sig


def prepare_base(df: pd.DataFrame) -> pd.DataFrame:
    """Config-independent indicators, computed once per symbol."""
    df = df.copy()
    for p in (8, 20, 50):
        df[f"sma{p}"] = df["close"].rolling(p).mean()
    delta = df["close"].diff()
    gain  = delta.clip(lower=0).ewm(com=13, min_periods=14).mean()
    loss  = (-delta.clip(upper=0)).ewm(com=13, min_periods=14).mean()
    df["rsi"] = 100 - (100 / (1 + gain / loss.replace(0, np.nan)))
    exp12 = df["close"].ewm(span=12, adjust=False).mean()
    exp26 = df["close"].ewm(span=26, adjust=False).mean()
    df["macd"]        = exp12 - exp26
    df["macd_signal"] = df["macd"].ewm(span=9, adjust=False).mean()
    return df


def resample_1h(df: pd.DataFrame) -> pd.DataFrame:
    """15m → 60m bars anchored at 09:15. Values become 'known' at bar close."""
    h = df.resample("60min", origin=df.index.normalize().min() + pd.Timedelta("9h15min")).agg(
        {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
    ).dropna(subset=["close"])
    h["known_at"] = h.index + pd.Timedelta("60min")
    return h


def align_1h_smi(df15: pd.DataFrame, df1h: pd.DataFrame, length: int, smooth: int,
                 flavor: str) -> tuple:
    """1h SMI/signal mapped onto the 15m index without lookahead."""
    smi, sig = compute_smi(df1h, length, smooth, flavor)
    h = pd.DataFrame({"smi_1h": smi.values, "sig_1h": sig.values,
                      "known_at": df1h["known_at"].values}).dropna()
    h = h.sort_values("known_at")
    left = pd.DataFrame({"ts": df15.index})
    m = pd.merge_asof(left, h, left_on="ts", right_on="known_at", direction="backward")
    return m["smi_1h"].values, m["sig_1h"].values


# ─────────────────────────────────────────────────────────────────────────────
# Signal scan + exit simulation
# ─────────────────────────────────────────────────────────────────────────────

def simulate_exit(arr: dict, entry_idx: int, direction: str, sl_mode: str) -> dict:
    entry = arr["close"][entry_idx]
    dates = arr["day"]
    entry_day = dates[entry_idx]

    sl_dist   = HARD_SL_PTS if sl_mode == "pts" else entry * HARD_SL_PCT / 100.0
    arm_dist  = TRAIL_ARM_PTS if sl_mode == "pts" else entry * TRAIL_ARM_PCT / 100.0

    peak = 0.0
    armed = False
    n = len(dates)
    for i in range(entry_idx + 1, n):
        if dates[i] != entry_day:
            # ran off end of day without hitting last-bar close (data gap) — close at prev bar
            cl = arr["close"][i - 1]
            return _exit(entry, cl, i - 1, "FORCE_CLOSE", direction)
        hi, lo, cl = arr["high"][i], arr["low"][i], arr["close"][i]
        sma8 = arr["sma8"][i] if not np.isnan(arr["sma8"][i]) else entry

        if direction == "CE":
            if lo <= entry - sl_dist:
                return _exit(entry, entry - sl_dist, i, "SL", direction)
            pnl = cl - entry
        else:
            if hi >= entry + sl_dist:
                return _exit(entry, entry + sl_dist, i, "SL", direction)
            pnl = entry - cl

        peak = max(peak, pnl)
        if peak >= arm_dist:
            armed = True
        if armed:
            if direction == "CE" and cl < sma8:
                return _exit(entry, cl, i, "TRAIL_SMA8", direction)
            if direction == "PE" and cl > sma8:
                return _exit(entry, cl, i, "TRAIL_SMA8", direction)

        if arr["tstr"][i] >= LAST_BAR:
            return _exit(entry, cl, i, "FORCE_CLOSE", direction)

    return _exit(entry, arr["close"][n - 1], n - 1, "EOD", direction)


def _exit(entry, exit_price, idx, reason, direction):
    pnl = (exit_price - entry) if direction == "CE" else (entry - exit_price)
    return {"exit_price": round(float(exit_price), 2), "exit_idx": idx,
            "exit_reason": reason, "pnl_pts": round(float(pnl), 2),
            "pnl_pct": round(float(pnl) / entry * 100, 3),
            "result": "WIN" if pnl > 0 else "LOSS"}


def scan_symbol(sym: str, base: pd.DataFrame, df1h: pd.DataFrame, cfg: dict) -> list:
    """cfg: flavor, length, smooth, ob, os, sl_mode, use_filters, use_1h"""
    smi, sig = compute_smi(base, cfg["length"], cfg["smooth"], cfg["flavor"])
    smi_v, sig_v = smi.values, sig.values

    if cfg["use_1h"]:
        smi1h, sig1h = align_1h_smi(base, df1h, cfg["length"], cfg["smooth"], cfg["flavor"])

    close = base["close"].values
    arr = {
        "close": close, "high": base["high"].values, "low": base["low"].values,
        "sma8": base["sma8"].values,
        "day":  base.index.normalize().values,
        "tstr": np.array([t.strftime("%H:%M") for t in base.index]),
    }
    sma20, sma50 = base["sma20"].values, base["sma50"].values
    rsi   = base["rsi"].values
    macd, macds = base["macd"].values, base["macd_signal"].values

    prev = np.roll(smi_v, 1); prev[0] = np.nan
    ob, osold = cfg["ob"], cfg["os"]
    ce_cross = (prev <= osold) & (smi_v > osold) & (smi_v > sig_v)
    pe_cross = (prev >= ob) & (smi_v < ob) & (smi_v < sig_v)

    in_window = (arr["tstr"] >= ENTRY_START) & (arr["tstr"] <= ENTRY_END)
    valid = ~np.isnan(smi_v) & ~np.isnan(sig_v) & ~np.isnan(prev) & in_window

    if cfg["use_filters"]:
        fvalid = ~np.isnan(sma50) & ~np.isnan(rsi) & ~np.isnan(macds)
        bull = (sma20 > sma50) & (rsi > 50) & (macd > macds)
        bear = (sma20 < sma50) & (rsi < 50) & (macd < macds)
        ce_ok = ce_cross & valid & fvalid & bull
        pe_ok = pe_cross & valid & fvalid & bear
    else:
        ce_ok = ce_cross & valid
        pe_ok = pe_cross & valid

    if cfg["use_1h"]:
        h_ok_ce = ~np.isnan(smi1h) & (smi1h > sig1h)
        h_ok_pe = ~np.isnan(smi1h) & (smi1h < sig1h)
        ce_ok &= h_ok_ce
        pe_ok &= h_ok_pe

    signals = []
    last_exit_idx = -1
    fired = np.where(ce_ok | pe_ok)[0]
    for i in fired:
        if i <= last_exit_idx:          # one trade at a time per symbol
            continue
        direction = "CE" if ce_ok[i] else "PE"
        trade = simulate_exit(arr, i, direction, cfg["sl_mode"])
        last_exit_idx = trade["exit_idx"]
        signals.append({
            "symbol": sym, "date": str(base.index[i].date()),
            "entry_time": str(base.index[i]), "direction": direction,
            "entry_price": round(float(close[i]), 2),
            "band": price_band(float(close[i])),
            "smi": round(float(smi_v[i]), 1),
            **{k: v for k, v in trade.items() if k != "exit_idx"},
        })
    return signals


def run_config(prepared: dict, hourly: dict, cfg: dict) -> list:
    out = []
    for sym, base in prepared.items():
        out.extend(scan_symbol(sym, base, hourly[sym], cfg))
    return out


def stats(signals: list) -> dict:
    if not signals:
        return {"n": 0, "win": 0.0, "exp_pct": 0.0, "exp_pts": 0.0, "tot_pct": 0.0, "sl": 0.0}
    df = pd.DataFrame(signals)
    n = len(df); w = (df["result"] == "WIN").sum()
    return {"n": n, "win": round(w / n * 100, 1),
            "exp_pct": round(df["pnl_pct"].mean(), 3),
            "exp_pts": round(df["pnl_pts"].mean(), 2),
            "tot_pct": round(df["pnl_pct"].sum(), 1),
            "sl": round((df["exit_reason"] == "SL").sum() / n * 100, 1)}


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────

def main():
    refetch = "--refetch" in sys.argv
    data = fetch_all(refetch)
    print("Preparing indicators...")
    prepared = {s: prepare_base(df) for s, df in data.items()}
    hourly   = {s: resample_1h(df) for s, df in data.items()}

    R = []   # report lines
    R.append("# E2-SMI Backtest — Stock F&O Universe")
    R.append(f"\nGenerated : {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    days = max(df.index.normalize().nunique() for df in data.values())
    R.append(f"Universe  : {len(data)} F&O stocks | 15m candles | ~{days} trading days")
    R.append(f"Window    : {ENTRY_START}-{ENTRY_END} entries, force close {LAST_BAR} bar close")
    R.append(f"Exits     : hard SL (pts={HARD_SL_PTS} / pct={HARD_SL_PCT}%), SMA8 trail arms at "
             f"+{TRAIL_ARM_PTS} pts / +{TRAIL_ARM_PCT}%")
    R.append(f"Filters   : SMA20/50 regime + RSI50 + MACD + 1h SMI alignment\n")

    base_cfg = dict(length=10, smooth=3, ob=63, os=-37,
                    use_filters=True, use_1h=True)

    # ── Phase 1: flavor × SL mode ──────────────────────────────────────────
    print("\nPhase 1: SMI flavor × SL mode")
    R.append("## Phase 1 — SMI flavor × SL mode (10/3/3, OB +63 / OS −37)\n")
    R.append("| Flavor | SL mode | Signals | Win% | Exp %/trade | Exp pts | SL% |")
    R.append("|--------|---------|---------|------|-------------|---------|-----|")
    p1 = {}
    for flavor in ("ema", "sma", "rma"):
        for sl_mode in ("pts", "pct"):
            cfg = {**base_cfg, "flavor": flavor, "sl_mode": sl_mode}
            sigs = run_config(prepared, hourly, cfg)
            s = stats(sigs)
            p1[(flavor, sl_mode)] = s
            R.append(f"| {flavor} | {sl_mode} | {s['n']} | {s['win']}% | {s['exp_pct']:+.3f}% "
                     f"| {s['exp_pts']:+.2f} | {s['sl']}% |")
            print(f"  {flavor}/{sl_mode}: n={s['n']} win={s['win']}% exp={s['exp_pct']:+.3f}%")

    best_fl, best_sl = max((k for k, v in p1.items() if v["n"] >= 30),
                           key=lambda k: p1[k]["exp_pct"], default=("ema", "pct"))
    R.append(f"\n**Best: flavor={best_fl}, SL mode={best_sl}** (by expectancy %/trade, n≥30)\n")

    # ── Phase 2: parameter sweep ───────────────────────────────────────────
    print(f"\nPhase 2: sweep on flavor={best_fl}, sl={best_sl}")
    R.append(f"## Phase 2 — Parameter sweep (flavor={best_fl}, SL={best_sl})\n")
    R.append("| Len | Smooth | OB | OS | Signals | Win% | Exp %/trade | Total % |")
    R.append("|-----|--------|----|----|---------|------|-------------|---------|")
    sweep_results = {}
    for length in (8, 10, 12, 14):
        for smooth in (3, 5):
            for ob, osold in ((63, -37), (60, -40), (55, -35), (70, -45), (50, -30), (40, -40)):
                cfg = {**base_cfg, "flavor": best_fl, "sl_mode": best_sl,
                       "length": length, "smooth": smooth, "ob": ob, "os": osold}
                sigs = run_config(prepared, hourly, cfg)
                s = stats(sigs)
                sweep_results[(length, smooth, ob, osold)] = (s, sigs)
                R.append(f"| {length} | {smooth} | {ob} | {osold} | {s['n']} | {s['win']}% "
                         f"| {s['exp_pct']:+.3f}% | {s['tot_pct']:+.1f}% |")
        print(f"  length {length} done")

    best_key = max((k for k, (s, _) in sweep_results.items() if s["n"] >= 30),
                   key=lambda k: sweep_results[k][0]["exp_pct"])
    bl, bs, bob, bos = best_key
    best_stats, best_sigs = sweep_results[best_key]
    R.append(f"\n**Best config: length={bl}, smooth={bs}, OB=+{bob}, OS={bos}** — "
             f"n={best_stats['n']}, win={best_stats['win']}%, exp={best_stats['exp_pct']:+.3f}%/trade\n")
    print(f"  Best: len={bl} smooth={bs} OB={bob} OS={bos} exp={best_stats['exp_pct']:+.3f}%")

    # Also: user-spec config stats for direct comparison
    spec_s, spec_sigs = sweep_results.get((10, 3, 63, -37), (None, None))

    # ── Phase 3: price-band calibration ────────────────────────────────────
    print("\nPhase 3: price-band calibration")
    R.append("## Phase 3 — Price-band behaviour (best config)\n")
    df_best = pd.DataFrame(best_sigs)
    R.append("| Band | Signals | Win% | Exp %/trade | Exp pts | SL% |")
    R.append("|------|---------|------|-------------|---------|-----|")
    for _, _, name in BANDS:
        sub = df_best[df_best["band"] == name]
        if sub.empty:
            R.append(f"| {name} | 0 | — | — | — | — |"); continue
        n = len(sub); w = (sub["result"] == "WIN").sum()
        slr = (sub["exit_reason"] == "SL").sum() / n * 100
        R.append(f"| {name} | {n} | {w/n*100:.1f}% | {sub['pnl_pct'].mean():+.3f}% "
                 f"| {sub['pnl_pts'].mean():+.2f} | {slr:.1f}% |")

    # per-band OB/OS mini-sweep
    R.append("\n### Per-band best OB/OS (flavor/length/smooth fixed at best)\n")
    R.append("| Band | Best OB | Best OS | Signals | Win% | Exp %/trade |")
    R.append("|------|---------|---------|---------|------|-------------|")
    band_best = {}
    for ob, osold in ((63, -37), (60, -40), (55, -35), (70, -45), (50, -30), (40, -40)):
        key = (bl, bs, ob, osold)
        _, sigs = sweep_results[key]
        d = pd.DataFrame(sigs)
        if d.empty: continue
        for _, _, name in BANDS:
            sub = d[d["band"] == name]
            if len(sub) < 10:
                continue
            exp = sub["pnl_pct"].mean()
            cur = band_best.get(name)
            if cur is None or exp > cur[2]:
                band_best[name] = (ob, osold, exp, len(sub),
                                   (sub["result"] == "WIN").mean() * 100)
    for _, _, name in BANDS:
        if name not in band_best:
            R.append(f"| {name} | — | — | <10 | — | — |"); continue
        ob, osold, exp, n, win = band_best[name]
        R.append(f"| {name} | +{ob} | {osold} | {n} | {win:.1f}% | {exp:+.3f}% |")

    # ── Phase 4: filter impact ─────────────────────────────────────────────
    print("\nPhase 4: filter impact")
    R.append("\n## Phase 4 — Filter impact (best SMI config)\n")
    R.append("| Variant | Signals | Win% | Exp %/trade | Total % |")
    R.append("|---------|---------|------|-------------|---------|")
    variants = [
        ("Full filters + 1h", True, True),
        ("Filters, no 1h",    True, False),
        ("1h only",           False, True),
        ("Raw SMI only",      False, False),
    ]
    for label, uf, u1 in variants:
        cfg = {**base_cfg, "flavor": best_fl, "sl_mode": best_sl,
               "length": bl, "smooth": bs, "ob": bob, "os": bos,
               "use_filters": uf, "use_1h": u1}
        s = stats(run_config(prepared, hourly, cfg))
        R.append(f"| {label} | {s['n']} | {s['win']}% | {s['exp_pct']:+.3f}% | {s['tot_pct']:+.1f}% |")
        print(f"  {label}: n={s['n']} win={s['win']}% exp={s['exp_pct']:+.3f}%")

    if spec_s:
        R.append(f"\n---\nUser-spec config (10/3/3, +63/−37, {best_fl}/{best_sl}): "
                 f"n={spec_s['n']}, win={spec_s['win']}%, exp={spec_s['exp_pct']:+.3f}%/trade, "
                 f"total={spec_s['tot_pct']:+.1f}%")

    pd.DataFrame(best_sigs).to_csv(OUT_FILE, index=False)
    with open(REPORT_FILE, "w") as f:
        f.write("\n".join(R))
    print(f"\nSaved → {OUT_FILE}\nReport → {REPORT_FILE}")


if __name__ == "__main__":
    main()
