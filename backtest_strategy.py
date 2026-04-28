#!/usr/bin/env python3
"""Backtest Vishal Clean v16.7 strategy on N days of historical data.

Strategy under test:
  ENTRY: 3 gates (GREEN + close > EMA9_low + body >= 40)
  EXIT:  EMERGENCY_SL (-10) → EOD_EXIT → VISHAL_TRAIL (peak ladder)
         No slope-based / time-based soft exits — trail is the only path.
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
def compute_trail_sl(entry_price, peak_pnl, early_lock_5=False,
                     m5_offset=-5, lock3_peak=8, lock3_offset=3,
                     lock5_peak=12, lock5_offset=5,
                     lock8_peak=15, lock8_offset=8,
                     lock15_peak=20, lock15_offset=15,
                     dyn_peak=21, dyn_giveback=5):
    """v16.7 SL ladder. All tier thresholds + offsets are tunable for
    parameter sweeps. Defaults match current production.

       peak >=5 → SL = entry+m5_offset (LOCK_M5, default -5)
       peak >=8 → SL = entry+lock3_offset (LOCK_3, default +3)
       peak >=12→ SL = entry+lock5_offset (LOCK_5, default +5)
       ...
    """
    if peak_pnl >= dyn_peak:
        return round(entry_price + (peak_pnl - dyn_giveback), 2), "LOCK_DYN"
    if peak_pnl >= lock15_peak:
        return round(entry_price + lock15_offset, 2), "LOCK_15"
    if peak_pnl >= lock8_peak:
        return round(entry_price + lock8_offset, 2), "LOCK_8"
    if peak_pnl >= lock5_peak:
        return round(entry_price + lock5_offset, 2), "LOCK_5"
    if peak_pnl >= lock3_peak:
        return round(entry_price + lock3_offset, 2), "LOCK_3"
    if early_lock_5 and peak_pnl >= 5:
        return round(entry_price + m5_offset, 2), "LOCK_M5"
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
def evaluate_gates(candle, prev_candle, max_stretch=999, min_body=BODY_MIN):
    """Returns (fired, reject_reason, body_pct, slope, stretch)."""
    o, h, l, c = candle["open"], candle["high"], candle["low"], candle["close"]
    el = candle["ema9_low"]
    if el <= 0 or h <= 0:
        return False, "no_data", 0, 0, 0

    try:
        t = candle["ts"][-8:-3]
        hh, mm = int(t[:2]), int(t[3:])
    except Exception:
        return False, "ts_parse", 0, 0, 0
    mins = hh * 60 + mm
    w_min = WARMUP_HHMM[0] * 60 + WARMUP_HHMM[1]
    c_min = CUTOFF_HHMM[0] * 60 + CUTOFF_HHMM[1]
    if mins < w_min:
        return False, f"before_{WARMUP_HHMM[0]:02d}:{WARMUP_HHMM[1]:02d}", 0, 0, 0
    if mins >= c_min:
        return False, f"after_{CUTOFF_HHMM[0]:02d}:{CUTOFF_HHMM[1]:02d}", 0, 0, 0

    rng = h - l
    body_pct = round((abs(c - o) / rng * 100) if rng > 0 else 0, 1)
    is_green = c > o
    slope = round(el - (prev_candle["ema9_low"] if prev_candle else el), 2)
    stretch = round(c - el, 2)

    if not is_green:                    return False, "red_candle",  body_pct, slope, stretch
    if c <= el:                          return False, "below_ema9l", body_pct, slope, stretch
    if body_pct < min_body:              return False, f"weak_body_{int(body_pct)}", body_pct, slope, stretch
    # NEW v17 filter: stretch (close - ema9_low) must be <= max_stretch
    if stretch > max_stretch:           return False, f"stretched_{stretch:.1f}", body_pct, slope, stretch
    return True, "", body_pct, slope, stretch


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
def simulate_trade(entry_idx, leg_3m, leg_1m, anti_spike,
                   spike_buf=None, early_lock_5=False,
                   entry_mode="close", ladder_kwargs=None):
    """Walk forward from entry_idx, applying SL ladder.
    entry_mode:
      "close"       - fill at signal candle's close (V0 baseline)
      "anti_spike"  - target = close - spike_buf, fill at LTP if next 1m
                      low touches; else SKIP (current production)
      "candle_half" - target = (signal candle high + low) / 2, fill if
                      next 1m low touches; else SKIP (NEW idea 1)
    Returns dict on fill, None on skip.
    """
    if spike_buf is None:
        spike_buf = SPIKE_BUFFER_PTS
    entry_candle = leg_3m[entry_idx]
    raw_close = entry_candle["close"]
    spike_used = False

    # Determine target price based on entry_mode + legacy anti_spike flag.
    # Legacy: anti_spike=True is shorthand for entry_mode="anti_spike".
    if entry_mode == "candle_half":
        # Idea 1: target = midpoint of signal candle's range
        target = round((entry_candle["high"] + entry_candle["low"]) / 2.0, 2)
        wait_for_pullback = True
    elif anti_spike or entry_mode == "anti_spike":
        target = round(raw_close - spike_buf, 2)
        wait_for_pullback = True
    else:
        target = 0
        wait_for_pullback = False

    if wait_for_pullback:
        ent_ts = entry_candle["ts"]
        next_1m = None
        for c1 in leg_1m:
            if c1["ts"] > ent_ts:
                next_1m = c1
                break
        if next_1m and next_1m["low"] <= target:
            # Fill at min(target, low) — limit-pullback semantics
            entry_price = min(target, next_1m["low"])
            spike_used = True
        else:
            return None  # no pullback → skip
    else:
        entry_price = raw_close

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

        # Compute current SL based on peak (early_lock_5 toggle)
        _lk = ladder_kwargs or {}
        sl, tier = compute_trail_sl(entry_price, peak,
                                    early_lock_5=early_lock_5, **_lk)

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


def replay_day(d, atm_only=True, anti_spike=False, xleg_gate=False,
               spike_buf=None, early_lock_5=False,
               entry_mode="close", reentry_mode="off",
               max_stretch=999, min_body=BODY_MIN,
               ladder_kwargs=None):
    """Replay one trading day with MULTI-trade support + cooldown.
    entry_mode: "close" / "anti_spike" / "candle_half"
    reentry_mode:
      "off"          - no re-entry watcher (closest to V3 backtest)
      "current"      - 2-candle window, green break of original entry close
      "wait_3min"    - wait for next FULL 3-min candle to close, must
                       independently pass 3-gate, then re-enter using
                       entry_mode (NEW Idea 2)
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
                fired, why, body, slope, stretch = evaluate_gates(
                    c, prev_c, max_stretch=max_stretch, min_body=min_body)
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
        _buf = spike_buf if spike_buf is not None else SPIKE_BUFFER_PTS
        result = simulate_trade(best["idx"], best["rows_3m"], leg_1m_rows,
                                anti_spike=anti_spike,
                                spike_buf=_buf,
                                early_lock_5=early_lock_5,
                                entry_mode=entry_mode,
                                ladder_kwargs=ladder_kwargs)
        if result is None:
            trades.append({
                "date": d.isoformat(), "ts": ts, "strike": best["strike"],
                "side": best["side"], "xleg": best["xleg"],
                "xleg_margin": best["xleg_margin"], "body": best["body"],
                "raw_close": best["candle"]["close"],
                "spike_target": round(best["candle"]["close"] - _buf, 2),
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
        try:
            cool_min = ex_min + 5
            cool_hh = cool_min // 60
            cool_mm = cool_min % 60
            cool_marker = (blocked_until_ts[:11]
                           + f"{cool_hh:02d}:{cool_mm:02d}:59")
        except Exception:
            cool_marker = blocked_until_ts
        next_ok_ts_by_side[best["side"]] = cool_marker

        # ── Re-entry watcher (Idea 2: wait_3min) ──
        # After exit, look at the next 3-min candle on the SAME leg
        # AFTER exit ts. If it independently passes the 3-gate AND
        # x-leg, fire a re-entry using the same entry_mode as the
        # original trade. Skip cooldown for this re-entry.
        if reentry_mode == "wait_3min":
            re_idx = result["exit_idx"] + 1
            if re_idx < len(best["rows_3m"]) - 1:
                re_candle = best["rows_3m"][re_idx]
                re_prev   = best["rows_3m"][re_idx - 1]
                fired_re, _why, _body, _slope, _stretch = evaluate_gates(
                    re_candle, re_prev, max_stretch=max_stretch, min_body=min_body)
                if fired_re:
                    # Cross-leg snapshot at re-entry time
                    other_side = "PE" if best["side"] == "CE" else "CE"
                    other_rows = legs_3m.get((best["strike"], other_side), [])
                    other_idx_re = leg_idx[(best["strike"], other_side)].get(re_candle["ts"])
                    other_c_re = other_rows[other_idx_re] if other_idx_re is not None else None
                    re_xl, re_xlm = evaluate_xleg(other_c_re)
                    if not (xleg_gate and re_xl == "FAIL"):
                        # Fire re-entry
                        re_result = simulate_trade(
                            re_idx, best["rows_3m"], leg_1m_rows,
                            anti_spike=anti_spike,
                            spike_buf=_buf,
                            early_lock_5=early_lock_5,
                            entry_mode=entry_mode,
                        )
                        if re_result is not None:
                            re_result["date"]   = d.isoformat()
                            re_result["ts"]     = re_candle["ts"]
                            re_result["strike"] = best["strike"]
                            re_result["side"]   = best["side"]
                            re_result["xleg"]   = re_xl
                            re_result["xleg_margin"] = re_xlm
                            re_result["body"]   = _body
                            re_result["spike_skipped"] = False
                            re_result["mode"]   = "REENTRY_WAIT3"
                            trades.append(re_result)
                            blocked_until_ts = re_result.get("exit_ts", "")
                            try:
                                ex_min2 = _ts_to_min(blocked_until_ts)
                                cool_marker2 = (blocked_until_ts[:11]
                                    + f"{(ex_min2+5)//60:02d}:{(ex_min2+5)%60:02d}:59")
                                next_ok_ts_by_side[best["side"]] = cool_marker2
                            except Exception:
                                pass

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


def run_sweep(found, atm_only):
    """Parameter sweep — anti-spike buffer 1..6 × early_lock_5 on/off.
    Both filters always on (xleg_gate + anti_spike) — V3 baseline.
    """
    print("\n" + "=" * 90)
    print("PARAMETER SWEEP — anti-spike buffer × early-lock-5 toggle")
    print("=" * 90)
    print("All variants run with V3 (anti-spike ON + xleg gate ON).")
    print()

    buffers = [1, 2, 3, 4, 5, 6]
    rows = []
    for buf in buffers:
        for early in (False, True):
            all_trades = []
            for d in found:
                day_trades = replay_day(d, atm_only=atm_only,
                                         anti_spike=True, xleg_gate=True,
                                         spike_buf=buf,
                                         early_lock_5=early)
                if day_trades:
                    all_trades.extend(day_trades)
            real = [t for t in all_trades if not t.get("spike_skipped")]
            skips = [t for t in all_trades if t.get("spike_skipped")]
            wins = [t for t in real if t["pnl"] > 0]
            total_pts = sum(t["pnl"] for t in real)
            avg_w = (sum(t["pnl"] for t in wins) / len(wins)) if wins else 0
            avg_l = (sum(t["pnl"] for t in real if t["pnl"] <= 0)
                     / max(len(real) - len(wins), 1))
            wr = (len(wins) / len(real) * 100) if real else 0
            # Tier counts
            tier_count = defaultdict(int)
            for t in real:
                _, tier = compute_trail_sl(t["entry"], t["peak"],
                                           early_lock_5=early)
                tier_count[tier] += 1
            # Exit reason counts
            esl = sum(1 for t in real if t["reason"] == "EMERGENCY_SL")
            tr = sum(1 for t in real if t["reason"] == "VISHAL_TRAIL")
            f2 = 0  # FLAT_2X removed in v16.7-final, kept for table compat
            rows.append({
                "buf": buf, "early": early, "trades": len(real),
                "skips": len(skips), "wr": wr, "total": total_pts,
                "avg_w": avg_w, "avg_l": avg_l,
                "esl": esl, "tr": tr, "f2": f2,
                "tier_m5": tier_count.get("LOCK_M5", 0),
                "tier_init": tier_count.get("INITIAL", 0),
            })

    # Print table
    print(f"{'Buf':>4} {'Early-5':>8} {'Trd':>4} {'Skp':>4} {'WR%':>5} "
          f"{'Total':>7} {'AvgW':>5} {'AvgL':>5} {'ESL':>4} {'TR':>4} "
          f"{'M5':>3} {'INIT':>4}")
    print("-" * 90)
    best = max(rows, key=lambda r: r["total"])
    for r in rows:
        marker = " ←" if r is best else ""
        print(
            f"{r['buf']:>4} {('YES' if r['early'] else 'no'):>8} "
            f"{r['trades']:>4} {r['skips']:>4} {r['wr']:>5.1f} "
            f"{r['total']:>+7.1f} {r['avg_w']:>+5.1f} {r['avg_l']:>+5.1f} "
            f"{r['esl']:>4} {r['tr']:>4} "
            f"{r['tier_m5']:>3} {r['tier_init']:>4}{marker}"
        )
    print()
    print(f"BEST: buf={best['buf']}, early-lock-5={'YES' if best['early'] else 'no'} "
          f"→ {best['total']:+.1f} pts on {best['trades']} trades ({best['wr']:.1f}% WR)")
    print()
    print("Columns: Buf=spike buffer, Early-5=peak>=5 LOCK_M5 tier on/off,")
    print("  Trd=actual trades, Skp=spike-skipped, WR%=win rate,")
    print("  Total=total pts, AvgW/L=avg winner/loser, ESL=Emergency SL count,")
    print("  TR=VISHAL_TRAIL exits, M5=trades that armed LOCK_M5,")
    print("  INIT=trades stuck at INITIAL tier")


def run_idea_sweep(found, atm_only):
    """User's Idea sweep: compare baseline strategies + the 2 new ideas
    in isolation and combined.
    """
    print("\n" + "=" * 90)
    print("STRATEGY ENHANCEMENT SWEEP — 5d backtest")
    print("=" * 90)
    print()

    # variants: (label, anti_spike, xleg_gate, entry_mode, reentry_mode)
    variants = [
        ("V0  baseline 3-gate (close fill)         ", False, False, "close",       "off"),
        ("V0+ 3-gate + X-LEG only                   ", False, True,  "close",       "off"),
        ("V1  3-gate + X-LEG + anti-spike (PROD)   ", True,  True,  "anti_spike",  "off"),
        ("Vc  3-gate + X-LEG + candle/2 (idea 1)   ", False, True,  "candle_half", "off"),
        ("Vr  3-gate + X-LEG + candle/2 + ReWait3  ", False, True,  "candle_half", "wait_3min"),
        ("Vrx 3-gate + anti-spike + ReWait3 (no xl) ", True,  False, "anti_spike",  "wait_3min"),
        ("Vfull 3-gate + X-LEG + anti-spike + ReW3 ", True,  True,  "anti_spike",  "wait_3min"),
    ]

    print(f"{'Variant':<46} {'Trd':>4} {'Skp':>4} {'WR%':>5} {'Total':>7} {'AvgW':>5} {'AvgL':>5} {'ESL':>4} {'TR':>4}")
    print("-" * 95)

    rows = []
    for label, anti_spike, xleg, em, rm in variants:
        all_trades = []
        for d in found:
            day_trades = replay_day(d, atm_only=atm_only,
                                    anti_spike=anti_spike, xleg_gate=xleg,
                                    spike_buf=2, early_lock_5=True,
                                    entry_mode=em, reentry_mode=rm)
            if day_trades:
                all_trades.extend(day_trades)
        real = [t for t in all_trades if not t.get("spike_skipped")]
        skips = [t for t in all_trades if t.get("spike_skipped")]
        wins = [t for t in real if t["pnl"] > 0]
        losses = [t for t in real if t["pnl"] <= 0]
        total = sum(t["pnl"] for t in real)
        avg_w = (sum(t["pnl"] for t in wins) / len(wins)) if wins else 0
        avg_l = (sum(t["pnl"] for t in losses) / len(losses)) if losses else 0
        wr = (len(wins) / len(real) * 100) if real else 0
        esl = sum(1 for t in real if t.get("reason") == "EMERGENCY_SL")
        tr = sum(1 for t in real if t.get("reason") == "VISHAL_TRAIL")
        print(f"{label:<46} {len(real):>4} {len(skips):>4} {wr:>5.1f} "
              f"{total:>+7.1f} {avg_w:>+5.1f} {avg_l:>+5.1f} {esl:>4} {tr:>4}")
        rows.append({"label": label.strip(), "total": total, "trades": len(real)})

    print()
    best = max(rows, key=lambda r: r["total"])
    print(f"BEST: {best['label']}  → {best['total']:+.1f} pts on {best['trades']} trades")
    print()
    print("Decode:")
    print("  V0       = pure 3-gate, no filters, fill at close (closest to raw signal)")
    print("  V0+      = + X-LEG gate alone (test x-leg's contribution)")
    print("  V1       = current production (anti-spike + x-leg)")
    print("  Vc       = candle/2 fill (idea 1) + x-leg, no re-entry")
    print("  Vr       = candle/2 + x-leg + new re-entry rule (wait 3-min + 3-gate)")
    print("  Vrx      = anti-spike + new re-entry (without x-leg) — isolates re-entry effect")
    print("  Vfull    = anti-spike + x-leg + new re-entry rule")


def run_stretch_sweep(found, atm_only):
    """Stress test: stretch threshold × body% threshold matrix +
    day-by-day breakdown + walk-forward validation.
    Always uses V3-equivalent (anti-spike + early_lock_5 ON, no x-leg).
    """
    print("\n" + "=" * 100)
    print("STRESS TEST — Stretch filter on 5-day historical replay")
    print("=" * 100)

    # 1) Threshold sweep (single-dimension stretch)
    print(f"\n--- 1. STRETCH THRESHOLD SWEEP (body min=40, no x-leg) ---\n")
    print(f"{'max_stretch':>12} {'Trd':>4} {'Skp':>4} {'WR%':>5} {'Total':>8} "
          f"{'AvgW':>5} {'AvgL':>5} {'ESL':>4} {'TR':>4}")
    print("-" * 80)
    threshold_rows = []
    for mx in [3, 5, 6, 7, 8, 10, 12, 15, 999]:
        all_trades = []
        for d in found:
            day = replay_day(d, atm_only=atm_only,
                             anti_spike=True, xleg_gate=False,
                             spike_buf=2, early_lock_5=True,
                             entry_mode="anti_spike", reentry_mode="off",
                             max_stretch=mx, min_body=40)
            if day:
                all_trades.extend(day)
        real = [t for t in all_trades if not t.get("spike_skipped")]
        skips = [t for t in all_trades if t.get("spike_skipped")]
        wins = [t for t in real if t["pnl"] > 0]
        total = sum(t["pnl"] for t in real)
        avg_w = sum(t["pnl"] for t in wins) / max(len(wins), 1)
        avg_l = sum(t["pnl"] for t in real if t["pnl"] <= 0) / max(len(real)-len(wins), 1)
        wr = len(wins)/max(len(real),1)*100
        esl = sum(1 for t in real if t.get("reason") == "EMERGENCY_SL")
        tr = sum(1 for t in real if t.get("reason") == "VISHAL_TRAIL")
        label = f"≤ {mx}" if mx < 999 else "no filter"
        print(f"{label:>12} {len(real):>4} {len(skips):>4} {wr:>5.1f} "
              f"{total:>+8.1f} {avg_w:>+5.1f} {avg_l:>+5.1f} {esl:>4} {tr:>4}")
        threshold_rows.append({"mx": mx, "total": total, "trades": len(real), "wr": wr})

    # 2) 2D matrix: stretch × body
    print(f"\n--- 2. STRETCH × BODY 2D MATRIX (Total pts) ---\n")
    body_thresholds = [40, 45, 50, 55, 60]
    stretch_thresholds = [5, 6, 8, 10, 999]
    print(f"{'stretch':>10} | " + " | ".join(f"body≥{b:>2}" for b in body_thresholds))
    print("-" * 70)
    for mx in stretch_thresholds:
        row = []
        for body in body_thresholds:
            all_trades = []
            for d in found:
                day = replay_day(d, atm_only=atm_only,
                                 anti_spike=True, xleg_gate=False,
                                 spike_buf=2, early_lock_5=True,
                                 entry_mode="anti_spike", reentry_mode="off",
                                 max_stretch=mx, min_body=body)
                if day:
                    all_trades.extend(day)
            real = [t for t in all_trades if not t.get("spike_skipped")]
            total = sum(t["pnl"] for t in real)
            row.append(f"{total:>+7.1f}({len(real):>2})")
        label = f"≤ {mx}" if mx < 999 else "no max"
        print(f"{label:>10} | " + " | ".join(row))
    print(f"\n  format: total_pts(num_trades). Higher = better.\n")

    # 3) Day-by-day breakdown for the chosen winner
    best = max(threshold_rows, key=lambda r: r["total"])
    best_mx = best["mx"]
    print(f"\n--- 3. DAY-BY-DAY BREAKDOWN (stretch ≤ {best_mx} vs no filter) ---\n")
    print(f"{'Date':<12} {'Filter Trd':>11} {'Filter PNL':>11} {'Raw Trd':>9} {'Raw PNL':>9} {'Δ':>8}")
    print("-" * 70)
    cumul_filter = 0
    cumul_raw = 0
    for d in found:
        f_day = replay_day(d, atm_only=atm_only, anti_spike=True, xleg_gate=False,
                           spike_buf=2, early_lock_5=True,
                           entry_mode="anti_spike", reentry_mode="off",
                           max_stretch=best_mx, min_body=40)
        r_day = replay_day(d, atm_only=atm_only, anti_spike=True, xleg_gate=False,
                           spike_buf=2, early_lock_5=True,
                           entry_mode="anti_spike", reentry_mode="off",
                           max_stretch=999, min_body=40)
        f_real = [t for t in (f_day or []) if not t.get("spike_skipped")]
        r_real = [t for t in (r_day or []) if not t.get("spike_skipped")]
        f_total = sum(t["pnl"] for t in f_real)
        r_total = sum(t["pnl"] for t in r_real)
        delta = f_total - r_total
        cumul_filter += f_total
        cumul_raw += r_total
        print(f"{d.isoformat():<12} {len(f_real):>11} {f_total:>+11.1f} "
              f"{len(r_real):>9} {r_total:>+9.1f} {delta:>+8.1f}")
    print("-" * 70)
    print(f"{'CUMUL':<12} {'':>11} {cumul_filter:>+11.1f} {'':>9} {cumul_raw:>+9.1f} "
          f"{(cumul_filter - cumul_raw):>+8.1f}")

    # 4) Walk-forward (split days into halves)
    print(f"\n--- 4. WALK-FORWARD (split sample to avoid lookahead) ---\n")
    n = len(found)
    if n >= 4:
        half = n // 2
        train_days = found[:half]
        test_days = found[half:]
        print(f"Train days ({half}): {[d.isoformat() for d in train_days]}")
        print(f"Test days ({n-half}): {[d.isoformat() for d in test_days]}")
        # Pick best stretch on train
        best_train = None; best_train_total = -9999
        for mx in [3, 5, 6, 7, 8, 10, 12, 999]:
            tot = 0
            for d in train_days:
                day = replay_day(d, atm_only=atm_only, anti_spike=True, xleg_gate=False,
                                 spike_buf=2, early_lock_5=True,
                                 entry_mode="anti_spike", reentry_mode="off",
                                 max_stretch=mx, min_body=40)
                tot += sum(t["pnl"] for t in (day or []) if not t.get("spike_skipped"))
            if tot > best_train_total:
                best_train_total = tot; best_train = mx
        # Apply best to test
        test_filtered = 0; test_raw = 0
        for d in test_days:
            f_day = replay_day(d, atm_only=atm_only, anti_spike=True, xleg_gate=False,
                               spike_buf=2, early_lock_5=True,
                               entry_mode="anti_spike", reentry_mode="off",
                               max_stretch=best_train, min_body=40)
            r_day = replay_day(d, atm_only=atm_only, anti_spike=True, xleg_gate=False,
                               spike_buf=2, early_lock_5=True,
                               entry_mode="anti_spike", reentry_mode="off",
                               max_stretch=999, min_body=40)
            test_filtered += sum(t["pnl"] for t in (f_day or []) if not t.get("spike_skipped"))
            test_raw += sum(t["pnl"] for t in (r_day or []) if not t.get("spike_skipped"))
        print(f"\nBest stretch on TRAIN: ≤ {best_train} (train P&L: {best_train_total:+.1f})")
        print(f"Applied to TEST:")
        print(f"  Filtered P&L: {test_filtered:+.1f}")
        print(f"  Raw     P&L: {test_raw:+.1f}")
        print(f"  Delta:        {(test_filtered - test_raw):+.1f}  "
              f"({'POSITIVE — filter generalizes' if test_filtered > test_raw else 'NEGATIVE — overfit, do not ship'})")
    else:
        print("  (Need >= 4 days for walk-forward — skipping)")

    # 5) Decision summary
    print(f"\n--- 5. DECISION ---\n")
    print(f"  Best threshold (full sample):  ≤ {best_mx}  → +{best['total']:.1f} pts on {best['trades']} trades")
    print(f"  Walk-forward result:           {'✓ generalizes' if test_filtered > test_raw else '✗ overfit risk'}")
    print(f"  Trade count drop:              {best['trades']} of ~75 unfiltered ({best['trades']/75*100:.0f}%)")
    print()


def _run_variant(found, atm_only, ladder_kwargs):
    """Run a single backtest variant — V3-equivalent (anti-spike on,
    no x-leg gate, candle/2 entry style)."""
    all_trades = []
    for d in found:
        day = replay_day(d, atm_only=atm_only,
                         anti_spike=True, xleg_gate=False,
                         spike_buf=2, early_lock_5=True,
                         entry_mode="anti_spike", reentry_mode="off",
                         max_stretch=999, min_body=40,
                         ladder_kwargs=ladder_kwargs)
        if day:
            all_trades.extend(day)
    real = [t for t in all_trades if not t.get("spike_skipped")]
    wins = [t for t in real if t["pnl"] > 0]
    total = sum(t["pnl"] for t in real)
    avg_w = sum(t["pnl"] for t in wins) / max(len(wins), 1)
    losses = [t for t in real if t["pnl"] <= 0]
    avg_l = sum(t["pnl"] for t in losses) / max(len(losses), 1)
    wr = len(wins) / max(len(real), 1) * 100
    return {"trades": len(real), "wins": len(wins), "wr": wr,
            "total": total, "avg_w": avg_w, "avg_l": avg_l, "all": real}


def run_ladder_sweep(found, atm_only):
    """Sweep SL ladder parameters to find the most profitable curve.
    Tests three dimensions:
      A. LOCK_M5 SL offset (-7, -5, -3, 0)
      B. LOCK_3 peak threshold (6, 7, 8, 9, 10)
      C. LOCK_3 SL offset (+1, +3, +5)
    """
    print("\n" + "=" * 90)
    print("SL LADDER SWEEP — find optimal ratchet curve")
    print("=" * 90)
    base = _run_variant(found, atm_only, ladder_kwargs={})
    print(f"\nBaseline (current ladder): {base['trades']} trades, "
          f"{base['wr']:.1f}% WR, {base['total']:+.1f} pts "
          f"(avgW {base['avg_w']:+.1f}, avgL {base['avg_l']:+.1f})\n")

    # Dimension A: LOCK_M5 offset sweep
    print("--- A. LOCK_M5 offset sweep (peak >= 5 → SL = entry + offset) ---\n")
    print(f"{'M5 offset':>10}  {'Trd':>4} {'WR%':>5} {'Total':>8} {'AvgW':>5} {'AvgL':>5}  vs base")
    print("-" * 65)
    for m5 in [-7, -6, -5, -4, -3, -2, 0]:
        r = _run_variant(found, atm_only,
                         ladder_kwargs={"m5_offset": m5})
        delta = r["total"] - base["total"]
        marker = " ★" if r["total"] > base["total"] else ""
        print(f"{m5:>+10} {r['trades']:>5} {r['wr']:>5.1f} "
              f"{r['total']:>+8.1f} {r['avg_w']:>+5.1f} {r['avg_l']:>+5.1f}  "
              f"{delta:>+7.1f}{marker}")

    # Dimension B: LOCK_3 peak threshold
    print("\n--- B. LOCK_3 peak threshold (when does ladder lock first profit?) ---\n")
    print(f"{'lock3_peak':>11}  {'Trd':>4} {'WR%':>5} {'Total':>8} {'AvgW':>5} {'AvgL':>5}  vs base")
    print("-" * 65)
    for pk in [6, 7, 8, 9, 10, 11]:
        r = _run_variant(found, atm_only,
                         ladder_kwargs={"lock3_peak": pk})
        delta = r["total"] - base["total"]
        marker = " ★" if r["total"] > base["total"] else ""
        print(f"peak >={pk:>3}  {r['trades']:>5} {r['wr']:>5.1f} "
              f"{r['total']:>+8.1f} {r['avg_w']:>+5.1f} {r['avg_l']:>+5.1f}  "
              f"{delta:>+7.1f}{marker}")

    # Dimension C: LOCK_3 SL offset
    print("\n--- C. LOCK_3 SL offset (how much profit do we lock?) ---\n")
    print(f"{'lock3_off':>10}  {'Trd':>4} {'WR%':>5} {'Total':>8} {'AvgW':>5} {'AvgL':>5}  vs base")
    print("-" * 65)
    for off in [0, 1, 2, 3, 4, 5]:
        r = _run_variant(found, atm_only,
                         ladder_kwargs={"lock3_offset": off})
        delta = r["total"] - base["total"]
        marker = " ★" if r["total"] > base["total"] else ""
        print(f"  +{off:>+2}     {r['trades']:>5} {r['wr']:>5.1f} "
              f"{r['total']:>+8.1f} {r['avg_w']:>+5.1f} {r['avg_l']:>+5.1f}  "
              f"{delta:>+7.1f}{marker}")

    # Dimension D: dynamic giveback (LOCK_DYN tier — how much we give back from peak)
    print("\n--- D. LOCK_DYN giveback (peak >=21, SL = entry + peak - giveback) ---\n")
    print(f"{'giveback':>10}  {'Trd':>4} {'WR%':>5} {'Total':>8} {'AvgW':>5} {'AvgL':>5}  vs base")
    print("-" * 65)
    for gb in [3, 4, 5, 6, 7, 8]:
        r = _run_variant(found, atm_only,
                         ladder_kwargs={"dyn_giveback": gb})
        delta = r["total"] - base["total"]
        marker = " ★" if r["total"] > base["total"] else ""
        print(f"  -{gb:>+2}     {r['trades']:>5} {r['wr']:>5.1f} "
              f"{r['total']:>+8.1f} {r['avg_w']:>+5.1f} {r['avg_l']:>+5.1f}  "
              f"{delta:>+7.1f}{marker}")

    # Combined best — search 3-D space
    print("\n--- E. 3-D SEARCH (best combo of M5 offset × LOCK_3 peak × LOCK_3 offset) ---\n")
    best = base
    best_kwargs = {}
    for m5 in [-5, -3, 0]:
        for pk in [7, 8, 9]:
            for off in [2, 3, 4]:
                k = {"m5_offset": m5, "lock3_peak": pk, "lock3_offset": off}
                r = _run_variant(found, atm_only, ladder_kwargs=k)
                if r["total"] > best["total"]:
                    best = r; best_kwargs = k
    print(f"BEST combo: {best_kwargs}")
    print(f"  → {best['trades']} trades, {best['wr']:.1f}% WR, "
          f"{best['total']:+.1f} pts (vs baseline +{base['total']:.1f})")
    print(f"  Improvement: {(best['total'] - base['total']):+.1f} pts over 5 days")
    print(f"  Per trade:   {(best['total']/max(best['trades'],1)):+.2f} avg")
    print(f"\nNote: 5-day sample. Could be overfit. Need 10+ days for confidence.\n")


def main():
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 5
    atm_only = True
    if len(sys.argv) > 2 and sys.argv[2].upper() == "ALL":
        atm_only = False
    sweep_mode = (len(sys.argv) > 3 and sys.argv[3].lower() == "sweep")
    idea_mode = (len(sys.argv) > 3 and sys.argv[3].lower() == "ideas")

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

    if sweep_mode:
        run_sweep(found, atm_only)
        return
    if idea_mode:
        run_idea_sweep(found, atm_only)
        return
    if len(sys.argv) > 3 and sys.argv[3].lower() == "stretch":
        run_stretch_sweep(found, atm_only)
        return
    if len(sys.argv) > 3 and sys.argv[3].lower() == "ladder":
        run_ladder_sweep(found, atm_only)
        return

    print(f"\nReplaying {len(found)} days across 4 strategy variants...\n")

    variants = [
        ("V0  baseline           ", False, False),
        ("V1  + anti-spike       ", True,  False),
        ("V2  + xleg gate        ", False, True),
        ("V3  + both filters     ", True,  True),
    ]

    # Track trades per variant for detail dump
    variant_trades = {}
    for label, anti_spike, xleg_gate in variants:
        all_trades = []
        for d in found:
            day_trades = replay_day(d, atm_only=atm_only,
                                     anti_spike=anti_spike, xleg_gate=xleg_gate)
            if day_trades:
                all_trades.extend(day_trades)
        aggregate(all_trades, label)
        variant_trades[label.strip()] = all_trades

    # Per-trade detail dump for V1 + V3 (the winners)
    for vlabel in ("V1  + anti-spike", "V3  + both filters"):
        trades_v = variant_trades.get(vlabel, [])
        real_v = [t for t in trades_v if not t.get("spike_skipped")]
        if not real_v:
            continue
        print(f"\n{'='*90}")
        print(f"{vlabel} — TRADE-BY-TRADE DETAIL ({len(real_v)} trades)")
        print(f"{'='*90}")
        print(f"{'#':>3} {'Date':10} {'Time':5} {'Sid':3} {'Strike':>6} "
              f"{'RawCl':>6} {'Tgt':>6} {'Fill':>6} {'Sav':>5} "
              f"{'Peak':>5} {'Tier':9} {'SL@pk':>6} "
              f"{'Exit':>6} {'PNL':>6} {'Reason':14} {'Held':>4} {'XL':4}")
        print("-"*90)
        for i, t in enumerate(real_v, 1):
            entry = t.get("entry", 0)
            peak = t.get("peak", 0)
            sl_at_peak, tier = compute_trail_sl(entry, peak)
            raw_close = t.get("raw_close", 0)
            target = t.get("spike_target", 0) or 0
            saved = (raw_close - entry) if t.get("spike_used") else 0
            t_short = t.get("ts","")[-8:-3]  # HH:MM
            print(
                f"{i:>3} {t['date']} {t_short} {t['side']:3} "
                f"{t['strike']:>6} "
                f"{raw_close:>6.1f} {target:>6.1f} {entry:>6.1f} "
                f"{saved:>+5.1f} "
                f"{peak:>+5.1f} {tier:9} {sl_at_peak:>6.1f} "
                f"{t.get('exit',0):>6.1f} {t.get('pnl',0):>+6.1f} "
                f"{t.get('reason',''):14} "
                f"{t.get('candles',0):>4} {t.get('xleg','NA'):4}"
            )

        # Summary by tier reached
        print(f"\n{vlabel} — SUMMARY BY PEAK TIER REACHED")
        print("-"*90)
        tier_grp = defaultdict(list)
        for t in real_v:
            _, tier = compute_trail_sl(t.get("entry",0), t.get("peak",0))
            tier_grp[tier].append(t.get("pnl",0))
        for tier in ("LOCK_DYN","LOCK_15","LOCK_8","LOCK_5","LOCK_3","INITIAL"):
            pts = tier_grp.get(tier, [])
            if not pts: continue
            w = sum(1 for p in pts if p > 0)
            avg = sum(pts)/len(pts)
            print(f"  {tier:9}  {len(pts):>3} trades  W={w}/{len(pts)} ({w/len(pts)*100:.0f}%)  "
                  f"avg={avg:+.2f}  total={sum(pts):+.1f}")

    print("\n" + "=" * 60)
    print("VERDICT GUIDE")
    print("=" * 60)
    print("Promote anti-spike to default if V1 total > V0 total")
    print("Promote xleg gate to hard if V2 total > V0 total AND")
    print("  V2 PASS-WR > FAIL-WR by >= 10pts")
    print("Use V3 if it has the highest total + acceptable trade count")


if __name__ == "__main__":
    main()
