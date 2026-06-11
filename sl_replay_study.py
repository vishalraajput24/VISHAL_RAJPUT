#!/usr/bin/env python3
"""SL ladder replay study — standalone, read-only (no VRL_MAIN import).

Replays every V10 trade in ~/lab_data/vrl_trade_log.csv against the recorded
1-min option candles in ~/lab_data/options_1min/ and re-runs the exit ladder
under candidate SL rules:

  baseline   : initial_sl = ema9_low of breakout candle (entry-5 fallback)
               LOCK_4 @ peak>=12 -> entry+4, TRAIL_10 @ peak>=18 -> peak-10
  cap10      : baseline, but initial_sl floored at entry-10
  protect    : baseline + mid-tier: peak>=7 -> SL=entry-2
  cap+protect: both

Per-candle simulation is pessimistic: the SL is tested against the candle low
BEFORE the candle high updates the peak/tier, so intra-candle "new high then
reversal" never rescues a simulated trade.

Usage: python3 sl_replay_study.py [--per-trade]
"""
import csv
import glob
import os
import sys
from datetime import datetime

LAB = os.path.expanduser("~/lab_data")
TRADE_LOG = os.path.join(LAB, "vrl_trade_log.csv")
OPT_DIR = os.path.join(LAB, "options_1min")

LOCK4_TRIGGER, LOCK4_OFFSET = 12.0, 4.0
TRAIL_TRIGGER, TRAIL_OFFSET = 18.0, 10.0


def load_day(date_str):
    """Return {(strike, type): [candle, ...]} sorted by timestamp."""
    path = os.path.join(OPT_DIR, "nifty_option_1min_" + date_str.replace("-", "") + ".csv")
    if not os.path.isfile(path):
        return {}
    series = {}
    with open(path) as f:
        for r in csv.DictReader(f):
            try:
                key = (int(float(r["strike"])), r["type"])
                series.setdefault(key, []).append({
                    "ts": datetime.strptime(r["timestamp"], "%Y-%m-%d %H:%M:%S"),
                    "high": float(r["high"]), "low": float(r["low"]),
                    "close": float(r["close"]),
                })
            except (KeyError, ValueError):
                continue
    for v in series.values():
        v.sort(key=lambda c: c["ts"])
    return series


def ema9_lows(candles):
    """EMA9 of candle lows, same seed style as a plain EMA (first value = first low)."""
    out, ema, k = [], None, 2.0 / 10.0
    for c in candles:
        ema = c["low"] if ema is None else c["low"] * k + ema * (1 - k)
        out.append(ema)
    return out


def simulate(trade, candles, emal, cap10=False, protect=False,
             protect_trigger=7.0, protect_offset=-2.0):
    entry = trade["entry_price"]
    e_min = trade["entry_dt"].replace(second=0, microsecond=0)
    x_min = trade["exit_dt"].replace(second=0, microsecond=0)
    idx = next((i for i, c in enumerate(candles) if c["ts"] == e_min), None)
    if idx is None:
        return None
    sl = emal[idx]
    if sl >= entry:
        sl = entry - 5.0
    if cap10:
        sl = max(sl, entry - 10.0)
    peak_ltp = entry
    for c in candles[idx + 1:]:
        if c["low"] <= sl:
            return round(sl - entry, 2)
        peak_ltp = max(peak_ltp, c["high"])
        peak = peak_ltp - entry
        if protect and peak >= protect_trigger:
            sl = max(sl, entry + protect_offset)
        if peak >= LOCK4_TRIGGER:
            sl = max(sl, entry + LOCK4_OFFSET)
        if peak >= TRAIL_TRIGGER:
            sl = max(sl, entry + LOCK4_OFFSET, peak_ltp - TRAIL_OFFSET)
        if c["ts"] >= x_min:
            break
    # survived to the actual exit candle -> use the real exit fill
    return round(trade["exit_price"] - entry, 2)


def main():
    per_trade = "--per-trade" in sys.argv
    trades = []
    with open(TRADE_LOG) as f:
        for r in csv.DictReader(f):
            if not r.get("entry_mode", "").startswith("V10"):
                continue
            try:
                trades.append({
                    "date": r["date"], "direction": r["direction"],
                    "strike": int(float(r["strike"])),
                    "entry_price": float(r["entry_price"]),
                    "exit_price": float(r["exit_price"]),
                    "pnl_pts": float(r["pnl_pts"]),
                    "peak": float(r.get("peak_pnl") or 0),
                    "reason": r["exit_reason"],
                    "entry_dt": datetime.strptime(r["date"] + " " + r["entry_time"], "%Y-%m-%d %H:%M:%S"),
                    "exit_dt": datetime.strptime(r["date"] + " " + r["exit_time"], "%Y-%m-%d %H:%M:%S"),
                })
            except (KeyError, ValueError):
                continue

    combos = [
        ("baseline    ", {}),
        ("cap10       ", {"cap10": True}),
        ("protect7/-2 ", {"protect": True}),
        ("cap+protect ", {"cap10": True, "protect": True}),
    ]
    results = {name: [] for name, _ in combos}
    skipped, day_cache = 0, {}
    for t in trades:
        if t["date"] not in day_cache:
            day_cache[t["date"]] = load_day(t["date"])
        candles = day_cache[t["date"]].get((t["strike"], t["direction"]))
        if not candles:
            skipped += 1
            continue
        emal = ema9_lows(candles)
        row = {"trade": t}
        ok = True
        for name, kw in combos:
            pnl = simulate(t, candles, emal, **kw)
            if pnl is None:
                ok = False
                break
            row[name] = pnl
        if not ok:
            skipped += 1
            continue
        for name, _ in combos:
            results[name].append((t, row[name]))

    n = len(results[combos[0][0]])
    print(f"replayed {n} trades, skipped {skipped} (no 1-min data for strike/entry candle)")
    actual = sum(t["pnl_pts"] for t, _ in results[combos[0][0]])
    print(f"actual pnl of replayed set: {actual:+.1f} pts\n")
    print(f"{'combo':14s} {'total':>8s} {'wins':>5s} {'losses':>7s} {'avg loss':>9s} {'worst':>7s}")
    for name, _ in combos:
        pnls = [p for _, p in results[name]]
        losses = [p for p in pnls if p < 0]
        print(f"{name:14s} {sum(pnls):+8.1f} {sum(1 for p in pnls if p > 0):5d} "
              f"{len(losses):7d} {sum(losses)/len(losses) if losses else 0:+9.2f} "
              f"{min(pnls):+7.1f}")

    base = dict(zip([id(t) for t, _ in results[combos[0][0]]],
                    [p for _, p in results[combos[0][0]]]))
    for name, _ in combos[1:]:
        hurt = [(t, p, base[id(t)]) for t, p in results[name] if p < base[id(t)] - 0.5]
        print(f"\n{name.strip()}: trades made WORSE vs baseline sim: {len(hurt)}"
              f" ({sum(p - b for _, p, b in hurt):+.1f} pts)")
        for t, p, b in hurt:
            print(f"   {t['date']} {t['direction']} {t['strike']} "
                  f"actual={t['pnl_pts']:+.1f} base_sim={b:+.1f} -> {p:+.1f} (peak {t['peak']:+.1f})")

    if per_trade:
        print(f"\n{'date':10s} {'dir':3s} {'strike':6s} {'actual':>7s} " +
              " ".join(f"{nm.strip():>12s}" for nm, _ in combos))
        for i, (t, _) in enumerate(results[combos[0][0]]):
            vals = " ".join(f"{results[nm][i][1]:+12.1f}" for nm, _ in combos)
            print(f"{t['date']:10s} {t['direction']:3s} {t['strike']:<6d} {t['pnl_pts']:+7.1f} {vals}")


if __name__ == "__main__":
    main()
