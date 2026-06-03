"""
portfolio_monitor.py — Multibagger Monitor, PHASE 1: Price / Technical layer.

Runs daily after close on the weekly_tracker.csv holdings and computes, per
cohort row:
  * trailing-SL ladder  — <+25% => hard -20%; >=+25% => breakeven, then trail
    to 18% below the peak-since-entry (ratchets up, survives normal dips)
  * 30-week MA (~150 sessions) trend flag
  * relative strength vs NIFTY since entry (laggard flag)
  * CRASH detector — day <= -8% on volume >= 3x the 20-day average
    (the "scam falls 10%" exit-first shield)

It writes monitoring columns back to weekly_tracker.csv and appends triggers to
events.log. DECISION-SUPPORT ONLY — it never places an order. Thresholds live in
monitor_config.json (edit + save, no code change).
"""

import os
import sys
import json
from datetime import datetime, date, timedelta

import numpy as np
import pandas as pd

BASE_DIR     = os.path.dirname(os.path.abspath(__file__))
TRACKER_FILE = os.path.join(BASE_DIR, "weekly_tracker.csv")
EVENTS_LOG   = os.path.join(BASE_DIR, "events.log")
NIFTY_TOKEN  = 256265

DEFAULT_CONFIG = {
    "crash_day_pct"      : -8.0,   # single-day drop that triggers a CRASH alert
    "crash_vol_mult"     : 3.0,    # ...only if volume >= this x the 20-day average
    "breakeven_after_pct": 25.0,   # once peak gain >= this, SL moves to breakeven
    "hard_sl_pct"        : -20.0,  # initial hard stop (before +25%)
    "trail_below_peak_pct": 18.0,  # after +25%, trail SL this % below the peak
    "ma_sessions"        : 150,    # ~30 trading weeks
    "rs_lag_pct"         : -10.0,  # underperforming NIFTY by more than this = LAGGARD
    "lookback_calendar_days": 430, # history window to fetch (~290 sessions)
}


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    path = os.path.join(BASE_DIR, "monitor_config.json")
    try:
        if os.path.exists(path):
            with open(path) as f:
                cfg.update(json.load(f) or {})
    except Exception:
        pass
    return cfg


def _log_event(line):
    try:
        with open(EVENTS_LOG, "a") as f:
            f.write(f"{datetime.now().strftime('%Y-%m-%d %H:%M')} | {line}\n")
    except Exception:
        pass


def fetch_hist(kite, token, cal_days):
    """Daily OHLCV DataFrame indexed by date; None on failure."""
    try:
        to_dt  = datetime.now()
        from_dt = to_dt - timedelta(days=int(cal_days))
        candles = kite.historical_data(int(token), from_dt, to_dt, "day")
        if not candles:
            return None
        d = pd.DataFrame(candles)
        d.index = pd.to_datetime(d["date"])
        return d
    except Exception:
        return None


def compute_trail_sl(entry, peak, cfg):
    """Ratcheting trailing stop. Returns (sl_price, label)."""
    peak_ret = (peak / entry - 1) * 100 if entry else 0
    if peak_ret < cfg["breakeven_after_pct"]:
        return round(entry * (1 + cfg["hard_sl_pct"] / 100.0), 1), "HARD"
    # past +25%: breakeven floor, ratcheted up to 18% below the peak
    sl = max(entry, peak * (1 - cfg["trail_below_peak_pct"] / 100.0))
    return round(sl, 1), "TRAIL"


def _since_entry(hist, added):
    try:
        d0 = pd.to_datetime(added).date()
        return hist[hist.index.date >= d0]
    except Exception:
        return hist


def run():
    cfg = load_config()
    try:
        from vishal_fno_screener import get_kite, load_instruments
        kite = get_kite()
        nse_df, _ = load_instruments(kite)
    except Exception as e:
        print(f"Kite/instruments unavailable: {e}")
        return

    if not os.path.exists(TRACKER_FILE):
        print("No tracker file — run the screener first.")
        return
    df = pd.read_csv(TRACKER_FILE)

    def _terminal(s):
        s = str(s)
        return ("SL-HIT" in s) or ("T3-HIT" in s)

    live_idx = df.index[~df["status"].map(_terminal)]
    if len(live_idx) == 0:
        print("No live holdings to monitor.")
        return

    for col in ("peak_price", "trail_sl", "ma_30wk", "rs_vs_nifty",
                "crash_flag", "mon_status", "mon_updated"):
        if col not in df.columns:
            df[col] = pd.NA
    df["mon_status"] = df["mon_status"].astype("object")
    df["mon_updated"] = df["mon_updated"].astype("object")

    # NIFTY history once (for relative strength)
    nifty = fetch_hist(kite, NIFTY_TOKEN, cfg["lookback_calendar_days"])

    eq = nse_df[nse_df["instrument_type"] == "EQ"]
    tok = dict(zip(eq["tradingsymbol"], eq["instrument_token"]))

    price_cache = {}
    alerts = 0
    rows_done = 0
    print(f"Monitoring {len(live_idx)} live holding(s)...\n")
    for idx in live_idx:
        sym   = str(df.at[idx, "symbol"])
        added = str(df.at[idx, "date_added"])
        try:
            entry = float(df.at[idx, "entry_price"])
        except (TypeError, ValueError):
            continue
        if entry <= 0:
            continue

        if sym not in price_cache:
            t = tok.get(sym)
            price_cache[sym] = fetch_hist(kite, t, cfg["lookback_calendar_days"]) if t else None
        hist = price_cache[sym]
        if hist is None or len(hist) < 21:
            df.at[idx, "mon_status"] = "no-data"
            continue

        cur  = float(hist["close"].iloc[-1])
        prev = float(hist["close"].iloc[-2])
        since = _since_entry(hist, added)
        peak = float(since["close"].max()) if len(since) else cur

        day_chg  = (cur / prev - 1) * 100 if prev else 0
        vol_now  = float(hist["volume"].iloc[-1])
        vol_avg  = float(hist["volume"].iloc[-21:-1].mean()) or 0
        vol_mult = (vol_now / vol_avg) if vol_avg > 0 else 0
        ma = float(hist["close"].iloc[-cfg["ma_sessions"]:].mean()) if len(hist) >= cfg["ma_sessions"] \
            else float(hist["close"].mean())

        trail_sl, tlab = compute_trail_sl(entry, peak, cfg)

        rs = None
        if nifty is not None and len(nifty) >= 2:
            ns = _since_entry(nifty, added)
            if len(ns):
                ne, nn = float(ns["close"].iloc[0]), float(nifty["close"].iloc[-1])
                if ne > 0:
                    rs = round((cur / entry - 1) * 100 - (nn / ne - 1) * 100, 1)

        crash = (day_chg <= cfg["crash_day_pct"]) and (vol_mult >= cfg["crash_vol_mult"])

        flags = []
        if crash:                                   flags.append("CRASH")
        if cur <= trail_sl:                         flags.append("TRAIL-SL")
        if ma > 0 and cur < ma:                     flags.append("<30wkMA")
        if rs is not None and rs <= cfg["rs_lag_pct"]: flags.append("LAGGARD")
        mon_status = ("⚠ " + ",".join(flags)) if flags else "HOLD"

        df.at[idx, "peak_price"]  = round(peak, 1)
        df.at[idx, "trail_sl"]    = trail_sl
        df.at[idx, "ma_30wk"]     = round(ma, 1)
        df.at[idx, "rs_vs_nifty"] = rs
        df.at[idx, "crash_flag"]  = int(bool(crash))
        df.at[idx, "mon_status"]  = mon_status
        df.at[idx, "mon_updated"] = date.today().isoformat()
        rows_done += 1

        if crash:
            alerts += 1
            _log_event(f"CRASH    | {sym} ({added}) | {day_chg:+.1f}% on {vol_mult:.1f}x vol | "
                       f"cur {cur} — exit-first, investigate")
        if cur <= trail_sl:
            alerts += 1
            _log_event(f"TRAIL-SL | {sym} ({added}) | cur {cur} <= SL {trail_sl} ({tlab}) | "
                       f"peak was {round(peak,1)}")

        # ── AUTO-EXIT: below 30-week MA = broken trend (data: 22% win) ──
        if ma > 0 and cur < ma:
            _prev_status = str(df.at[idx, "status"])
            if not _prev_status.startswith("OPEN"):
                pass  # already closed
            else:
                df.at[idx, "status"] = "BELOW-MA ❌"
                df.at[idx, "current_price"] = cur
                df.at[idx, "current_return_%"] = round((cur / entry - 1) * 100, 1)
                alerts += 1
                _log_event(f"BELOW-MA | {sym} ({added}) | cur {cur} < 30wkMA {round(ma,1)} | "
                           f"auto-exit — 22% win rate below MA")

        col = "⚠" if flags else " "
        print(f"  {col} {sym:<12} cur {cur:<9} peak {round(peak,1):<9} SL {trail_sl:<9} "
              f"RS {rs if rs is not None else '-':<6} {mon_status}")

    df.to_csv(TRACKER_FILE, index=False)
    print(f"\n✅ Monitored {rows_done} row(s) — {alerts} alert(s) logged to events.log")


if __name__ == "__main__":
    run()
