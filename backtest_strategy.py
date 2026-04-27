#!/usr/bin/env python3
"""Backtest Vishal Clean v16.7 strategy on N days of historical data.

Strategy under test:
  ENTRY: 3 gates (GREEN + close > EMA9_low + body >= 40)
  EXIT:  EMERGENCY_SL (-10) → VISHAL_TRAIL (peak ladder) → FLAT_2X
         (slope 0..3 for 2 candles)
  TRAIL: INITIAL → LOCK_3 (+8) → LOCK_5 (+12) → LOCK_8 (+15) →
         LOCK_15 (+20) → LOCK_DYN (peak-5)

Filters under evaluation:
  CROSS-LEG: PASS = other leg dying (other_close < other_ema9_low)
             FAIL = other leg holding
  ANTI-SPIKE: target = close - 2; fill if next 1-min low <= target,
              else skip ("missed pullback")

Outputs 4 strategy variants:
  V0  baseline           (no filters)
  V1  + anti-spike       (pullback fill or skip)
  V2  + xleg gate        (skip FAIL signals)
  V3  + both filters

Usage:
    cd /home/vishalraajput24/VISHAL_RAJPUT
    python3 backtest_strategy.py 5      # last 5 trading days
    python3 backtest_strategy.py 5 ATM  # ATM strikes only (default)
"""

import os
import sys
import csv
import glob
from datetime import date, datetime, timedelta
from collections import defaultdict

# ── Strategy parameters (must match VRL_ENGINE / config.yaml) ──
BODY_MIN          = 40
WARMUP_HHMM       = (9, 35)
CUTOFF_HHMM       = (15, 10)
EOD_HHMM          = (15, 20)
EMERGENCY_SL_PTS  = -10
FLAT_SLOPE_MAX    = 3        # slope 0..3 = "flat"
FLAT_STREAK_MIN   = 2
SPIKE_BUFFER_PTS  = 2

# Where to look for lab data
LAB_3M_DIR = os.path.expanduser("~/lab_data/options_3min")
LAB_1M_DIR = os.path.expanduser("~/lab_data/options_1min")


# ─────────────────────────────────────────────────────────────
# SL ladder (mirror of VRL_ENGINE.compute_trail_sl)
# ─────────────────────────────────────────────────────────────
def compute_trail_sl(entry_price, peak_pnl):
    if peak_pnl >= 21:
        return round(entry_price + (peak_pnl - 5), 2), "LOCK_DYN"
    if peak_pnl >= 20:
        return round(entry_price + 15, 2), "LOCK_15"
    if peak_pnl >= 15:
        return round(entry_price + 8, 2), "LOCK_8"
    if peak_pnl >= 12:
        return round(entry_price + 5, 2), "LOCK_5"
    if peak_pnl >= 8:
        return round(entry_price + 3, 2), "LOCK_3"
    return round(entry_price - 10, 2), "INITIAL"


# ─────────────────────────────────────────────────────────────
# Data loading
# ─────────────────────────────────────────────────────────────
def trading_dates(n):
    """Last n weekdays from today (excl. today)."""
    out = []
    d = date.today()
    while len(out) < n:
        d -= timedelta(days=1)
        if d.weekday() < 5:
            out.append(d)
    return list(reversed(out))


def load_3m_day(d):
    """Returns dict[(strike,side)] -> list of candle dicts sorted by ts."""
    path = os.path.join(LAB_3M_DIR, "nifty_option_3min_"
                        + d.strftime("%Y%m%d") + ".csv")
    if not os.path.isfile(path):
        return None
    by_leg = defaultdict(list)
    with open(path) as f:
        for r in csv.DictReader(f):
            try:
                strike = int(float(r.get("strike") or 0))
                side = (r.get("type") or "").upper()
                if not strike or side not in ("CE", "PE"):
                    continue
                rec = {
                    "ts":        r.get("timestamp") or "",
                    "strike":    strike,
                    "side":      side,
                    "open":      float(r.get("open") or 0),
                    "high":      float(r.get("high") or 0),
                    "low":       float(r.get("low") or 0),
                    "close":     float(r.get("close") or 0),
                    "ema9_high": float(r.get("ema9_high") or 0),
                    "ema9_low":  float(r.get("ema9_low") or 0),
                    "atm_distance": float(r.get("atm_distance") or 0),
                }
                by_leg[(strike, side)].append(rec)
            except Exception:
                continue
    for k in by_leg:
        by_leg[k].sort(key=lambda x: x["ts"])
    return by_leg


def load_1m_day(d):
    """Returns dict[(strike,side)] -> list of 1-min candle dicts."""
    path = os.path.join(LAB_1M_DIR, "nifty_option_1min_"
                        + d.strftime("%Y%m%d") + ".csv")
    if not os.path.isfile(path):
        return {}
    by_leg = defaultdict(list)
    with open(path) as f:
        for r in csv.DictReader(f):
            try:
                strike = int(float(r.get("strike") or 0))
                side = (r.get("type") or "").upper()
                if not strike or side not in ("CE", "PE"):
                    continue
                rec = {
                    "ts":    r.get("timestamp") or "",
                    "open":  float(r.get("open") or 0),
                    "high":  float(r.get("high") or 0),
                    "low":   float(r.get("low") or 0),
                    "close": float(r.get("close") or 0),
                }
                by_leg[(strike, side)].append(rec)
            except Exception:
                continue
    for k in by_leg:
        by_leg[k].sort(key=lambda x: x["ts"])
    return by_leg


# ─────────────────────────────────────────────────────────────
# Per-candle gate evaluation
# ─────────────────────────────────────────────────────────────
def evaluate_gates(candle, prev_candle):
    """Returns (fired, reject_reason, body_pct, slope)."""
    o, h, l, c = candle["open"], candle["high"], candle["low"], candle["close"]
    el = candle["ema9_low"]
    if el <= 0 or h <= 0:
        return False, "no_data", 0, 0

    # Time window
    try:
        t = candle["ts"][-8:-3]  # "HH:MM"
        hh, mm = int(t[:2]), int(t[3:])
    except Exception:
        return False, "ts_parse", 0, 0
    mins = hh * 60 + mm
    w_min = WARMUP_HHMM[0] * 60 + WARMUP_HHMM[1]
    c_min = CUTOFF_HHMM[0] * 60 + CUTOFF_HHMM[1]
    if mins < w_min:
        return False, f"before_{WARMUP_HHMM[0]:02d}:{WARMUP_HHMM[1]:02d}", 0, 0
    if mins >= c_min:
        return False, f"after_{CUTOFF_HHMM[0]:02d}:{CUTOFF_HHMM[1]:02d}", 0, 0

    # Body + green
    rng = h - l
    body_pct = round((abs(c - o) / rng * 100) if rng > 0 else 0, 1)
    is_green = c > o
    slope = round(el - (prev_candle["ema9_low"] if prev_candle else el), 2)

    if not is_green:                    return False, "red_candle",  body_pct, slope
    if c <= el:                          return False, "below_ema9l", body_pct, slope
    if body_pct < BODY_MIN:              return False, f"weak_body_{int(body_pct)}", body_pct, slope
    return True, "", body_pct, slope


def evaluate_xleg(other_candle):
    """Cross-leg: PASS if other_close < other_ema9_low."""
    if not other_candle: return "NA", 0
    oc = other_candle["close"]; oel = other_candle["ema9_low"]
    if oel <= 0: return "NA", 0
    margin = round(oc - oel, 2)
    return ("PASS" if oc < oel else "FAIL"), margin


# ─────────────────────────────────────────────────────────────
# Single-trade simulator
# ─────────────────────────────────────────────────────────────
def simulate_trade(entry_idx, leg_3m, leg_1m, anti_spike):
    """Walk forward from entry_idx, applying SL ladder + FLAT_2X.
    Returns dict with entry_price, exit_price, pnl, peak, reason,
    candles, spike_used, exit_idx, exit_ts. Returns None if anti-spike
    skipped the entry.
    Entry candle = leg_3m[entry_idx]. Trade enters at end-of-candle.
    """
    entry_candle = leg_3m[entry_idx]
    raw_close = entry_candle["close"]
    spike_used = False
    spike_skipped = False

    # Anti-spike: target = close - 2. Look at the very next 1-min candle
    # within the next 60s. We approximate by checking the FIRST 1-min
    # candle whose ts is strictly AFTER entry_candle["ts"].
    if anti_spike:
        target = round(raw_close - SPIKE_BUFFER_PTS, 2)
        ent_ts = entry_candle["ts"]
        # Find next 1-min candle (only the first one — represents 60s window)
        next_1m = None
        for c1 in leg_1m:
            if c1["ts"] > ent_ts:
                next_1m = c1
                break
        if next_1m and next_1m["low"] <= target:
            # Fill at the BETTER of target or actual low (limit-pullback
            # semantics, user chose "fill at LTP"). If LTP plunged below
            # target during the 60s window, our limit order would fill
            # at the lower price.
            entry_price = min(target, next_1m["low"])
            spike_used = True
        else:
            # No pullback within 60s → skip trade
            spike_skipped = True
            return None
    else:
        entry_price = raw_close
        target = 0

    peak = 0.0
    flat_streak = 0
    exit_reason = None
    exit_price = None

    # Walk forward through subsequent 3-min candles
    for j in range(entry_idx + 1, len(leg_3m)):
        c = leg_3m[j]
        prev = leg_3m[j - 1]
        c_close = c["close"]
        c_low   = c["low"]
        c_high  = c["high"]

        # Update peak using HIGH of candle (best PNL during candle)
        candle_peak_pnl = c_high - entry_price
        candle_min_pnl  = c_low  - entry_price
        if candle_peak_pnl > peak:
            peak = candle_peak_pnl

        # Compute current SL based on peak
        sl, tier = compute_trail_sl(entry_price, peak)

        # 1. Emergency SL — if candle's low touched/crossed entry-10
        if candle_min_pnl <= EMERGENCY_SL_PTS:
            return _result(entry_price, entry_price + EMERGENCY_SL_PTS, peak,
                           "EMERGENCY_SL", j - entry_idx, raw_close,
                           target if anti_spike else 0, spike_used,
                           exit_idx=j, exit_ts=c["ts"])

        # 2. EOD cutoff
        try:
            t = c["ts"][-8:-3]
            hh, mm = int(t[:2]), int(t[3:])
            if hh * 60 + mm >= EOD_HHMM[0] * 60 + EOD_HHMM[1]:
                return _result(entry_price, c_close, peak, "EOD_EXIT",
                               j - entry_idx, raw_close,
                               target if anti_spike else 0, spike_used,
                               exit_idx=j, exit_ts=c["ts"])
        except Exception:
            pass

        # 3. VISHAL_TRAIL — if candle close <= trail SL
        if sl > 0 and c_close <= sl:
            return _result(entry_price, sl, peak, "VISHAL_TRAIL",
                           j - entry_idx, raw_close,
                           target if anti_spike else 0, spike_used,
                           exit_idx=j, exit_ts=c["ts"])

        # 4. FLAT_2X — slope 0..3 for 2 consecutive candles
        slope = round(c["ema9_low"] - prev["ema9_low"], 2)
        if slope <= FLAT_SLOPE_MAX:
            flat_streak += 1
        else:
            flat_streak = 0
        if flat_streak >= FLAT_STREAK_MIN:
            return _result(entry_price, c_close, peak, "FLAT_2X",
                           j - entry_idx, raw_close,
                           target if anti_spike else 0, spike_used,
                           exit_idx=j, exit_ts=c["ts"])

    # Ran out of candles (end of day) — close at last candle close
    if leg_3m:
        last_c = leg_3m[-1]
        return _result(entry_price, last_c["close"], peak, "EOD_EXIT",
                       len(leg_3m) - entry_idx - 1, raw_close,
                       target if anti_spike else 0, spike_used,
                       exit_idx=len(leg_3m) - 1, exit_ts=last_c["ts"])
    return None


def _result(entry, exit_p, peak, reason, candles, raw_close, target,
            spike_used, exit_idx=0, exit_ts=""):
    return {
        "entry": round(entry, 2),
        "exit":  round(exit_p, 2),
        "pnl":   round(exit_p - entry, 2),
        "peak":  round(peak, 2),
        "reason": reason,
        "candles": candles,
        "raw_close": round(raw_close, 2),
        "spike_target": round(target, 2),
        "spike_used":   spike_used,
        "exit_idx":     exit_idx,
        "exit_ts":      exit_ts,
    }


# ─────────────────────────────────────────────────────────────
# Day replay
# ─────────────────────────────────────────────────────────────
def _ts_to_min(ts):
    """Convert 'YYYY-MM-DD HH:MM:SS' or similar → minutes-since-midnight."""
    try:
        t = ts[-8:-3]  # HH:MM
        return int(t[:2]) * 60 + int(t[3:])
    except Exception:
        return 0


def replay_day(d, atm_only=True, anti_spike=False, xleg_gate=False):
    """Replay one trading day with MULTI-trade support + cooldown.
    Returns list of trade result dicts (incl. spike-skips).
    """
    legs_3m = load_3m_day(d)
    if not legs_3m:
        return None
    legs_1m = load_1m_day(d)

    # Pick strikes by ATM distance — in CSV atm_distance==0 means ATM
    strikes_used = set()
    for (strike, side), rows in legs_3m.items():
        if not rows: continue
        if atm_only:
            atm_d = abs(rows[0].get("atm_distance", 999))
            if atm_d <= 50:
                strikes_used.add(strike)
        else:
            strikes_used.add(strike)

    # Build a unified timeline of 3-min ts in chronological order
    timeline_set = set()
    for (s, side), rows in legs_3m.items():
        if s in strikes_used:
            for r in rows:
                timeline_set.add(r["ts"])
    timeline = sorted(timeline_set)
    if not timeline:
        return []

    # Fast (strike,side,ts)→idx map
    leg_idx = defaultdict(dict)
    for (s, side), rows in legs_3m.items():
        for i, r in enumerate(rows):
            leg_idx[(s, side)][r["ts"]] = i

    trades = []
    # next_ok_ts_by_side[side] = earliest ts at which we can re-enter
    # this direction (5-min same-direction cooldown after exit).
    next_ok_ts_by_side = {"CE": "", "PE": ""}
    # ts cursor — when we're in a trade, advance past its exit_ts
    blocked_until_ts = ""

    for ts in timeline:
        if blocked_until_ts and ts <= blocked_until_ts:
            continue  # in trade, skip until exit ts

        # Evaluate every (strike, side) candidate at this ts
        candidates = []
        for s in strikes_used:
            for side in ("CE", "PE"):
                # Same-direction cooldown
                if next_ok_ts_by_side[side] and ts <= next_ok_ts_by_side[side]:
                    continue
                rows = legs_3m.get((s, side), [])
                idx = leg_idx[(s, side)].get(ts)
                if idx is None or idx < 1:
                    continue
                c = rows[idx]
                prev_c = rows[idx - 1]
                fired, why, body, slope = evaluate_gates(c, prev_c)
                if not fired:
                    continue
                # Cross-leg
                other = "PE" if side == "CE" else "CE"
                other_rows = legs_3m.get((s, other), [])
                other_idx = leg_idx[(s, other)].get(ts)
                other_c = other_rows[other_idx] if other_idx is not None else None
                xl_sig, xl_margin = evaluate_xleg(other_c)
                if xleg_gate and xl_sig == "FAIL":
                    continue
                gap = c["close"] - c["ema9_low"]
                score = body * max(gap, 0.1)
                candidates.append({
                    "strike": s, "side": side, "score": score,
                    "candle": c, "idx": idx, "xleg": xl_sig,
                    "xleg_margin": xl_margin, "body": body, "slope": slope,
                    "rows_3m": rows,
                })

        if not candidates:
            continue
        candidates.sort(key=lambda x: -x["score"])
        best = candidates[0]

        leg_1m_rows = legs_1m.get((best["strike"], best["side"]), [])
        result = simulate_trade(best["idx"], best["rows_3m"], leg_1m_rows,
                                anti_spike=anti_spike)
        if result is None:
            # Spike-skipped — log + continue scanning
            trades.append({
                "date": d.isoformat(), "ts": ts, "strike": best["strike"],
                "side": best["side"], "xleg": best["xleg"],
                "xleg_margin": best["xleg_margin"], "body": best["body"],
                "raw_close": best["candle"]["close"],
                "spike_target": round(best["candle"]["close"] - SPIKE_BUFFER_PTS, 2),
                "spike_skipped": True, "pnl": 0, "peak": 0,
                "reason": "SPIKE_SKIP", "entry": 0, "exit": 0, "candles": 0,
            })
            continue

        result["date"]   = d.isoformat()
        result["ts"]     = ts
        result["strike"] = best["strike"]
        result["side"]   = best["side"]
        result["xleg"]   = best["xleg"]
        result["xleg_margin"] = best["xleg_margin"]
        result["body"]   = best["body"]
        result["spike_skipped"] = False
        trades.append(result)

        # Block timeline until exit ts; same-side cooldown 5 min after exit
        blocked_until_ts = result.get("exit_ts", "")
        ex_min = _ts_to_min(blocked_until_ts)
        # Cooldown ts = ex_min + 5 min — convert back to lookup-friendly
        # form. Since we compare by ts string lexicographically, build a
        # marker like "YYYY-MM-DD HH:MM:00" at ex_min+5.
        try:
            cool_min = ex_min + 5
            cool_hh = cool_min // 60
            cool_mm = cool_min % 60
            cool_marker = (blocked_until_ts[:11]
                           + f"{cool_hh:02d}:{cool_mm:02d}:59")
        except Exception:
            cool_marker = blocked_until_ts
        next_ok_ts_by_side[best["side"]] = cool_marker

    return trades


# ─────────────────────────────────────────────────────────────
# Aggregate + report
# ─────────────────────────────────────────────────────────────
def aggregate(trades, label):
    real = [t for t in trades if not t.get("spike_skipped")]
    skipped = [t for t in trades if t.get("spike_skipped")]
    total_pts = sum(t["pnl"] for t in real)
    wins = [t for t in real if t["pnl"] > 0]
    losses = [t for t in real if t["pnl"] <= 0]
    n = len(real)
    wr = (len(wins) / n * 100) if n else 0
    avg_w = (sum(t["pnl"] for t in wins) / len(wins)) if wins else 0
    avg_l = (sum(t["pnl"] for t in losses) / len(losses)) if losses else 0
    print(f"\n=== {label} ===")
    print(f"  Real trades : {n}")
    print(f"  Spike skips : {len(skipped)}")
    print(f"  W / L       : {len(wins)} / {len(losses)}  ({wr:.1f}%)")
    print(f"  Total pts   : {total_pts:+.1f}")
    print(f"  Avg winner  : {avg_w:+.2f} pts")
    print(f"  Avg loser   : {avg_l:+.2f} pts")
    # Exit reason breakdown
    by_reason = defaultdict(list)
    for t in real:
        by_reason[t["reason"]].append(t["pnl"])
    print(f"  Exit reasons:")
    for rsn, pts in sorted(by_reason.items(), key=lambda x: -len(x[1])):
        w = sum(1 for p in pts if p > 0)
        print(f"    {rsn:14}  {len(pts):>3} trades  W={w}  total={sum(pts):+.1f}")
    # X-leg accuracy on the trades we DID take
    by_xl = defaultdict(list)
    for t in real:
        by_xl[t.get("xleg", "NA")].append(t["pnl"])
    print(f"  X-leg accuracy:")
    for sig in ("PASS", "FAIL", "NA"):
        pts = by_xl.get(sig, [])
        if not pts: continue
        w = sum(1 for p in pts if p > 0)
        wr_x = w / len(pts) * 100
        print(f"    {sig:5}  {len(pts):>3}  W={w}/{len(pts)} ({wr_x:.0f}%)  total={sum(pts):+.1f}")


def main():
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 5
    atm_only = True
    if len(sys.argv) > 2 and sys.argv[2].upper() == "ALL":
        atm_only = False

    dates = trading_dates(n)
    print(f"Backtesting {len(dates)} trading days: "
          + ", ".join(d.isoformat() for d in dates))
    print(f"Strikes: {'ATM and ATM+/-50' if atm_only else 'ALL'}")
    print(f"Looking in: {LAB_3M_DIR}")

    # Probe what data exists
    found = []
    for d in dates:
        p = os.path.join(LAB_3M_DIR, "nifty_option_3min_"
                         + d.strftime("%Y%m%d") + ".csv")
        sz = os.path.getsize(p) if os.path.isfile(p) else 0
        status = "✓" if sz > 0 else "✗"
        print(f"  {status} {d.isoformat()}: {sz:,} bytes ({p})")
        if sz > 0:
            found.append(d)

    if not found:
        print("\nNo historical 3-min data found. Aborting.")
        sys.exit(1)

    print(f"\nReplaying {len(found)} days across 4 strategy variants...\n")

    variants = [
        ("V0  baseline           ", False, False),
        ("V1  + anti-spike       ", True,  False),
        ("V2  + xleg gate        ", False, True),
        ("V3  + both filters     ", True,  True),
    ]

    for label, anti_spike, xleg_gate in variants:
        all_trades = []
        for d in found:
            day_trades = replay_day(d, atm_only=atm_only,
                                     anti_spike=anti_spike, xleg_gate=xleg_gate)
            if day_trades:
                all_trades.extend(day_trades)
        aggregate(all_trades, label)

    print("\n" + "=" * 60)
    print("VERDICT GUIDE")
    print("=" * 60)
    print("Promote anti-spike to default if V1 total > V0 total")
    print("Promote xleg gate to hard if V2 total > V0 total AND")
    print("  V2 PASS-WR > FAIL-WR by >= 10pts")
    print("Use V3 if it has the highest total + acceptable trade count")


if __name__ == "__main__":
    main()
