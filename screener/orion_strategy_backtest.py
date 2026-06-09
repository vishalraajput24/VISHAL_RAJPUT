"""
orion_strategy_backtest.py
──────────────────────────
ORION Strategy — Signal-level backtest on 6 stocks.
Validates E1 (VWAP), E2 (StochRSI), E4 (FLIP) on stock price.

Universe : HDFCBANK · ICICIBANK · RELIANCE · SBIN · INFY · TCS
Data     : Kite 15-min candles (last 60 days)
Level    : Stock price — no option premium
Hard SL  : entry ± 10 pts
Trail    : arms at peak ≥ entry + 15
           E1 → VWAP trail · E2/E4 → SMA8 trail
Force close: 3:25 PM same day

Usage:
  python3 orion_strategy_backtest.py
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

from kiteconnect import KiteConnect

BASE_DIR   = os.path.dirname(os.path.abspath(__file__))
TOKEN_FILE = os.path.join(os.path.expanduser("~"), "VISHAL_RAJPUT", "state", "access_token.json")
INST_CACHE = os.path.join(BASE_DIR, "inst_cache_nse.csv")
OUT_FILE   = os.path.join(BASE_DIR, "orion_strategy_results.csv")
REPORT_FILE= os.path.join(BASE_DIR, "orion_strategy_report.md")

ORION_STOCKS = ["HDFCBANK", "ICICIBANK", "RELIANCE", "SBIN", "INFY", "TCS"]

HARD_SL_PTS   = 10.0
TRAIL_ARM_PTS = 15.0
ENTRY_START   = "09:30"
ENTRY_END     = "14:30"
FORCE_CLOSE   = "15:25"
LOOKBACK_DAYS = 58   # Kite 15-min limit ~60 days

# StochRSI params
STOCH_PERIOD  = 14
SMOOTH_K      = 3
SMOOTH_D      = 3
OVERSOLD      = 20
OVERBOUGHT    = 80

load_dotenv(os.path.expanduser("~/.env"))
API_KEY = os.getenv("KITE_API_KEY", "")


# ─────────────────────────────────────────────────────────────────────────────
# Kite auth + data
# ─────────────────────────────────────────────────────────────────────────────

def get_kite():
    if not API_KEY:
        print("KITE_API_KEY not found"); sys.exit(1)
    kite = KiteConnect(api_key=API_KEY)
    with open(TOKEN_FILE) as f:
        saved = json.load(f)
    if saved.get("date") != date.today().isoformat():
        print("Token expired — refresh first"); sys.exit(1)
    kite.set_access_token(saved["access_token"])
    kite.profile()
    return kite


def get_token(symbol: str) -> int | None:
    df = pd.read_csv(INST_CACHE)
    row = df[(df["tradingsymbol"] == symbol) & (df["instrument_type"] == "EQ")]
    return int(row.iloc[0]["instrument_token"]) if not row.empty else None


def fetch_15min(kite, token: int, symbol: str) -> pd.DataFrame:
    to_dt   = datetime.now().replace(second=0, microsecond=0)
    from_dt = to_dt - timedelta(days=LOOKBACK_DAYS)
    candles = kite.historical_data(token, from_dt, to_dt, "15minute", continuous=False)
    time.sleep(0.3)
    if not candles:
        return pd.DataFrame()
    df = pd.DataFrame(candles)
    df["date"] = pd.to_datetime(df["date"])
    df = df.set_index("date").sort_index()
    df["symbol"] = symbol
    return df


# ─────────────────────────────────────────────────────────────────────────────
# Indicators
# ─────────────────────────────────────────────────────────────────────────────

def add_vwap(df: pd.DataFrame) -> None:
    df["_date"] = df.index.date
    df["_tp"]   = (df["high"] + df["low"] + df["close"]) / 3
    df["_tpv"]  = df["_tp"] * df["volume"]
    df["vwap"]  = (
        df.groupby("_date")["_tpv"].cumsum() /
        df.groupby("_date")["volume"].cumsum()
    )
    df.drop(columns=["_date", "_tp", "_tpv"], inplace=True)
    return df


def add_rsi(df: pd.DataFrame, period: int = 14) -> pd.DataFrame:
    delta = df["close"].diff()
    gain  = delta.clip(lower=0).ewm(com=period - 1, min_periods=period).mean()
    loss  = (-delta.clip(upper=0)).ewm(com=period - 1, min_periods=period).mean()
    df["rsi"] = 100 - (100 / (1 + gain / loss.replace(0, np.nan)))
    return df


def add_stochrsi(df: pd.DataFrame) -> pd.DataFrame:
    add_rsi(df, STOCH_PERIOD)
    rsi = df["rsi"]
    rsi_min = rsi.rolling(STOCH_PERIOD).min()
    rsi_max = rsi.rolling(STOCH_PERIOD).max()
    raw_k   = 100 * (rsi - rsi_min) / (rsi_max - rsi_min).replace(0, np.nan)
    df["stoch_k"] = raw_k.rolling(SMOOTH_K).mean()
    df["stoch_d"] = df["stoch_k"].rolling(SMOOTH_D).mean()
    return df


def add_sma(df: pd.DataFrame, periods: list[int]) -> pd.DataFrame:
    for p in periods:
        df[f"sma{p}"] = df["close"].rolling(p).mean()
    return df


def add_macd(df: pd.DataFrame) -> pd.DataFrame:
    exp12 = df["close"].ewm(span=12, adjust=False).mean()
    exp26 = df["close"].ewm(span=26, adjust=False).mean()
    df["macd"]        = exp12 - exp26
    df["macd_signal"] = df["macd"].ewm(span=9, adjust=False).mean()
    return df


def add_regime(df: pd.DataFrame) -> pd.DataFrame:
    # BULL = SMA20 > SMA50, BEAR = SMA20 < SMA50
    df["regime"] = np.where(df["sma20"] > df["sma50"], "BULL", "BEAR")
    return df


def add_body_pct(df: pd.DataFrame) -> pd.DataFrame:
    bar_range = (df["high"] - df["low"]).replace(0, np.nan)
    df["body_pct"] = (df["close"] - df["open"]).abs() / bar_range * 100
    return df


def prepare(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    add_vwap(df)
    add_stochrsi(df)
    add_sma(df, [8, 20, 50])
    add_macd(df)
    add_regime(df)
    add_body_pct(df)
    return df


# ─────────────────────────────────────────────────────────────────────────────
# Trade simulation
# ─────────────────────────────────────────────────────────────────────────────

def simulate_exit(df: pd.DataFrame, entry_idx: int, direction: str, engine: str) -> dict:
    """
    Walk candle-by-candle from entry_idx+1.
    Returns exit dict: {exit_price, exit_time, exit_reason, pnl_pts}
    """
    entry_price = float(df.iloc[entry_idx]["close"])
    entry_time  = df.index[entry_idx]
    force_dt    = entry_time.replace(hour=15, minute=25, second=0)

    peak_pnl    = 0.0
    trail_armed = False

    for i in range(entry_idx + 1, len(df)):
        row      = df.iloc[i]
        ts       = df.index[i]
        hi       = float(row["high"])
        lo       = float(row["low"])
        cl       = float(row["close"])
        vwap     = float(row["vwap"]) if not pd.isna(row.get("vwap", np.nan)) else entry_price
        sma8     = float(row["sma8"])  if not pd.isna(row.get("sma8",  np.nan)) else entry_price

        if direction == "CE":
            curr_pnl = cl - entry_price
            sl_price = entry_price - HARD_SL_PTS
            # SL check on bar low
            if lo <= sl_price:
                return _exit(entry_price, sl_price, ts, "SL", direction)
        else:
            curr_pnl = entry_price - cl
            sl_price = entry_price + HARD_SL_PTS
            if hi >= sl_price:
                return _exit(entry_price, sl_price, ts, "SL", direction)

        peak_pnl = max(peak_pnl, curr_pnl)

        # Arm trail at peak ≥ +15
        if peak_pnl >= TRAIL_ARM_PTS:
            trail_armed = True

        if trail_armed:
            if engine == "E1":
                # VWAP trail: exit when price crosses back through VWAP
                if direction == "CE" and cl < vwap:
                    return _exit(entry_price, cl, ts, "TRAIL_VWAP", direction)
                if direction == "PE" and cl > vwap:
                    return _exit(entry_price, cl, ts, "TRAIL_VWAP", direction)
            else:
                # SMA8 trail
                if direction == "CE" and cl < sma8:
                    return _exit(entry_price, cl, ts, "TRAIL_SMA8", direction)
                if direction == "PE" and cl > sma8:
                    return _exit(entry_price, cl, ts, "TRAIL_SMA8", direction)

        # Force close
        if ts >= force_dt or ts.time() >= pd.Timestamp(FORCE_CLOSE).time():
            return _exit(entry_price, cl, ts, "FORCE_CLOSE", direction)

    # End of data
    last_cl = float(df.iloc[-1]["close"])
    return _exit(entry_price, last_cl, df.index[-1], "EOD", direction)


def _exit(entry: float, exit_price: float, ts, reason: str, direction: str) -> dict:
    pnl = (exit_price - entry) if direction == "CE" else (entry - exit_price)
    return {
        "exit_price":  round(exit_price, 2),
        "exit_time":   ts,
        "exit_reason": reason,
        "pnl_pts":     round(pnl, 2),
        "result":      "WIN" if pnl > 0 else "LOSS",
    }


# ─────────────────────────────────────────────────────────────────────────────
# Engine 1 — VWAP
# ─────────────────────────────────────────────────────────────────────────────

def scan_e1(df: pd.DataFrame) -> list[dict]:
    signals = []
    for i in range(1, len(df)):
        row  = df.iloc[i]
        prev = df.iloc[i - 1]
        ts   = df.index[i]
        t    = ts.strftime("%H:%M")
        if t < ENTRY_START or t > ENTRY_END:
            continue
        if pd.isna(row["vwap"]) or pd.isna(prev["vwap"]) or pd.isna(row["body_pct"]):
            continue

        body      = float(row["body_pct"])
        if body < 65:
            continue

        cl        = float(row["close"])
        op        = float(row["open"])
        vwap      = float(row["vwap"])
        prev_cl   = float(prev["close"])
        prev_vwap = float(prev["vwap"])

        # Crossover only: previous close on one side, current close on other
        crossed_up   = prev_cl <= prev_vwap and cl > vwap and cl > op
        crossed_down = prev_cl >= prev_vwap and cl < vwap and cl < op

        if crossed_up:
            direction = "CE"
        elif crossed_down:
            direction = "PE"
        else:
            continue

        trade = simulate_exit(df, i, direction, "E1")
        signals.append({
            "engine": "E1", "symbol": df.iloc[i]["symbol"],
            "date": ts.date().isoformat(), "entry_time": ts,
            "direction": direction, "entry_price": round(cl, 2),
            "body_pct": round(body, 2),
            **trade
        })
    return signals


# ─────────────────────────────────────────────────────────────────────────────
# Engine 2 — StochRSI
# ─────────────────────────────────────────────────────────────────────────────

def scan_e2(df: pd.DataFrame) -> list[dict]:
    signals = []
    for i in range(1, len(df)):
        row  = df.iloc[i]
        prev = df.iloc[i - 1]
        ts   = df.index[i]
        t    = ts.strftime("%H:%M")
        if t < ENTRY_START or t > ENTRY_END:
            continue

        needed = ["stoch_k", "stoch_d", "sma20", "sma50", "rsi", "macd", "macd_signal", "regime"]
        if any(pd.isna(row.get(c, np.nan)) for c in needed):
            continue
        if any(pd.isna(prev.get(c, np.nan)) for c in ["stoch_k"]):
            continue

        k_now  = float(row["stoch_k"])
        k_prev = float(prev["stoch_k"])
        regime = str(row["regime"])
        rsi_v  = float(row["rsi"])
        macd_v = float(row["macd"])
        macd_s = float(row["macd_signal"])

        direction = None
        if k_prev <= OVERSOLD and k_now > OVERSOLD and regime == "BULL":
            # K crosses up from oversold — CE only in BULL
            if rsi_v > 50 and macd_v > macd_s:
                direction = "CE"
        elif k_prev >= OVERBOUGHT and k_now < OVERBOUGHT and regime == "BEAR":
            # K crosses down from overbought — PE only in BEAR
            if rsi_v < 50 and macd_v < macd_s:
                direction = "PE"

        if direction is None:
            continue

        cl = float(row["close"])
        trade = simulate_exit(df, i, direction, "E2")
        signals.append({
            "engine": "E2", "symbol": df.iloc[i]["symbol"],
            "date": ts.date().isoformat(), "entry_time": ts,
            "direction": direction, "entry_price": round(cl, 2),
            "stoch_k": round(k_now, 2), "regime": regime,
            **trade
        })
    return signals


# ─────────────────────────────────────────────────────────────────────────────
# Engine 4 — FLIP (piggybacks E2)
# ─────────────────────────────────────────────────────────────────────────────

def scan_e4(e2_signals: list[dict], df: pd.DataFrame) -> list[dict]:
    flip_signals = []
    # Group E2 signals by date
    by_date: dict[str, list] = {}
    for s in e2_signals:
        by_date.setdefault(s["date"], []).append(s)

    for d, day_signals in by_date.items():
        if len(day_signals) < 2:
            continue
        day_signals = sorted(day_signals, key=lambda x: x["entry_time"])
        flips = 0
        for j in range(1, len(day_signals)):
            if flips >= 3:
                break
            prev_sig = day_signals[j - 1]
            curr_sig = day_signals[j]
            # Flip = direction reversed from previous E2 signal
            if curr_sig["direction"] != prev_sig["direction"]:
                flip_signals.append({**curr_sig, "engine": "E4"})
                flips += 1

    return flip_signals


# ─────────────────────────────────────────────────────────────────────────────
# Report
# ─────────────────────────────────────────────────────────────────────────────

def summarize(signals: list[dict]) -> str:
    if not signals:
        return "No signals found."

    df = pd.DataFrame(signals)
    lines = []
    lines.append("# ORION Strategy Backtest — Stock Signal Level")
    lines.append(f"\nGenerated : {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    lines.append(f"Lookback  : {LOOKBACK_DAYS} days  |  Universe: {', '.join(ORION_STOCKS)}")
    lines.append(f"Hard SL   : ±{HARD_SL_PTS} pts  |  Trail arms at: +{TRAIL_ARM_PTS} pts\n")

    # Overall
    total = len(df)
    wins  = (df["result"] == "WIN").sum()
    lines.append(f"## Overall  —  {wins}/{total} ({wins/total*100:.1f}% win rate)")
    lines.append(f"Avg win : {df[df['result']=='WIN']['pnl_pts'].mean():.2f} pts")
    lines.append(f"Avg loss: {df[df['result']=='LOSS']['pnl_pts'].mean():.2f} pts")
    exp = df["pnl_pts"].mean()
    lines.append(f"Expectancy: {exp:.2f} pts/trade\n")

    # Per engine
    lines.append("## By Engine\n")
    lines.append("| Engine | Signals | Win% | Avg Win | Avg Loss | Expectancy |")
    lines.append("|--------|---------|------|---------|----------|------------|")
    for eng in ["E1", "E2", "E4"]:
        sub = df[df["engine"] == eng]
        if sub.empty:
            lines.append(f"| {eng} | 0 | — | — | — | — |")
            continue
        w = (sub["result"] == "WIN").sum()
        n = len(sub)
        aw = sub[sub["result"]=="WIN"]["pnl_pts"].mean() if w > 0 else 0
        al = sub[sub["result"]=="LOSS"]["pnl_pts"].mean() if (n-w) > 0 else 0
        ex = sub["pnl_pts"].mean()
        lines.append(f"| {eng} | {n} | {w/n*100:.1f}% | +{aw:.2f} | {al:.2f} | {ex:.2f} |")

    # Per stock
    lines.append("\n## By Stock\n")
    lines.append("| Stock | Signals | Win% | Expectancy |")
    lines.append("|-------|---------|------|------------|")
    for sym in ORION_STOCKS:
        sub = df[df["symbol"] == sym]
        if sub.empty:
            continue
        w = (sub["result"] == "WIN").sum()
        n = len(sub)
        ex = sub["pnl_pts"].mean()
        lines.append(f"| {sym:<12} | {n:7} | {w/n*100:.1f}% | {ex:.2f} |")

    # Exit reason breakdown
    lines.append("\n## Exit Reasons\n")
    lines.append("| Reason | Count | Win% | Avg PnL |")
    lines.append("|--------|-------|------|---------|")
    for reason, grp in df.groupby("exit_reason"):
        w = (grp["result"] == "WIN").sum()
        n = len(grp)
        lines.append(f"| {reason:<15} | {n:5} | {w/n*100:.1f}% | {grp['pnl_pts'].mean():.2f} |")

    # Best / Worst trades
    lines.append("\n## Best 5 Trades")
    top5 = df.nlargest(5, "pnl_pts")[["engine","symbol","date","direction","entry_price","exit_price","pnl_pts","exit_reason"]]
    lines.append(top5.to_string(index=False))

    lines.append("\n## Worst 5 Trades")
    bot5 = df.nsmallest(5, "pnl_pts")[["engine","symbol","date","direction","entry_price","exit_price","pnl_pts","exit_reason"]]
    lines.append(bot5.to_string(index=False))

    lines.append(f"\n---\nHard SL: ±{HARD_SL_PTS} pts · Trail: +{TRAIL_ARM_PTS} pts · E3 skipped (cluster undefined)")
    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────

def main():
    print("=" * 55)
    print("  ORION Strategy Backtest — E1 + E2 + E4")
    print(f"  Stocks : {', '.join(ORION_STOCKS)}")
    print(f"  Lookback: {LOOKBACK_DAYS} days  |  SL: ±{HARD_SL_PTS} pts")
    print("=" * 55)

    kite = get_kite()
    all_signals = []

    for sym in ORION_STOCKS:
        print(f"\n  {sym}...", flush=True)
        token = get_token(sym)
        if token is None:
            print(f"    SKIP — token not found"); continue

        df = fetch_15min(kite, token, sym)
        if df.empty:
            print(f"    SKIP — no candle data"); continue

        df = prepare(df)
        print(f"    Candles: {len(df)}  |  Days: {df.index.normalize().nunique()}")

        e1 = scan_e1(df)
        e2 = scan_e2(df)
        e4 = scan_e4(e2, df)

        print(f"    E1: {len(e1)} signals  |  E2: {len(e2)}  |  E4: {len(e4)}")
        all_signals.extend(e1 + e2 + e4)

    if not all_signals:
        print("\nNo signals generated."); return

    results_df = pd.DataFrame(all_signals)
    results_df.to_csv(OUT_FILE, index=False)
    print(f"\n  Saved → {OUT_FILE}")

    report = summarize(all_signals)
    with open(REPORT_FILE, "w") as f:
        f.write(report)
    print(f"  Saved → {REPORT_FILE}")

    # Print inline summary
    print("\n" + "=" * 55)
    df = results_df
    total = len(df)
    wins  = (df["result"] == "WIN").sum()
    print(f"  TOTAL SIGNALS : {total}")
    print(f"  WIN RATE      : {wins}/{total} = {wins/total*100:.1f}%")
    print(f"  EXPECTANCY    : {df['pnl_pts'].mean():.2f} pts/trade")
    for eng in ["E1", "E2", "E4"]:
        sub = df[df["engine"] == eng]
        if sub.empty: continue
        w = (sub["result"] == "WIN").sum()
        print(f"  {eng}: {len(sub)} signals, {w/len(sub)*100:.1f}% win rate, {sub['pnl_pts'].mean():.2f} pts avg")
    print("=" * 55)


if __name__ == "__main__":
    main()
