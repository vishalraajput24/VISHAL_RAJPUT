#!/usr/bin/env python3
"""
VRL v16.7 – Multi-day strategy analysis & V7 parameter enhancement report.

Usage:
    python3 data_analysis/strategy_analysis.py
    python3 data_analysis/strategy_analysis.py --date 20260512   # include extra day

Reads all trades_YYYYMMDD.csv from data_analysis/multi_day/ (and today/ if present).
Outputs:
  data_analysis/STRATEGY_REPORT.md   — Markdown pushed to GitHub
  Console summary
"""

import os
import sys
import csv
import glob
from collections import defaultdict
from datetime import datetime, date

# ── Paths ──
SCRIPT_DIR   = os.path.dirname(os.path.abspath(__file__))
MULTI_DIR    = os.path.join(SCRIPT_DIR, "multi_day")
TODAY_DIR    = os.path.join(SCRIPT_DIR, "today")
REPORT_PATH  = os.path.join(SCRIPT_DIR, "STRATEGY_REPORT.md")

# ── V7 SL ladder (current production) ──
V7_SL_TIERS = [
    (50, "LOCK_50", 50),
    (40, "LOCK_36", 36),
    (36, "LOCK_24", 24),
    (30, "LOCK_20", 20),
    (24, "LOCK_12", 12),
    (12, "LOCK_BE",  0),
]

# ── V7 SL ladder ENHANCED candidates ──
# Idea: tighten initial SL to -10 pts but keep rest unchanged
V7_SL_TIERS_TIGHT = [
    (50, "LOCK_50", 50),
    (40, "LOCK_36", 36),
    (36, "LOCK_24", 24),
    (30, "LOCK_20", 20),
    (24, "LOCK_12", 12),
    (12, "LOCK_BE",  0),
]
V7_INITIAL_SL_CURRENT = -12
V7_INITIAL_SL_TIGHT   = -10


# ─────────────────────────────────────────────────────────────
# Data loading
# ─────────────────────────────────────────────────────────────
def load_all_trades(extra_date=None):
    """Load all trades from multi_day/ + today/ + optional extra_date."""
    files = sorted(glob.glob(os.path.join(MULTI_DIR, "trades_*.csv")))
    today_files = sorted(glob.glob(os.path.join(TODAY_DIR, "trades_*.csv")))
    files += today_files

    if extra_date:
        for dd in [MULTI_DIR, TODAY_DIR]:
            ep = os.path.join(dd, f"trades_{extra_date}.csv")
            if os.path.isfile(ep) and ep not in files:
                files.append(ep)

    all_trades = []
    seen = set()
    for fpath in files:
        with open(fpath, newline="") as f:
            for row in csv.DictReader(f):
                key = (row.get("date"), row.get("entry_time"), row.get("symbol"))
                if key in seen:
                    continue
                seen.add(key)
                all_trades.append(row)

    return all_trades


def split_v7_v8(trades):
    v7 = [t for t in trades if not str(t.get("entry_mode", "")).startswith("V8_")]
    v8 = [t for t in trades if str(t.get("entry_mode", "")).startswith("V8_")]
    return v7, v8


def safe_float(v, default=0.0):
    try:
        return float(v) if v not in (None, "", "None") else default
    except Exception:
        return default


# ─────────────────────────────────────────────────────────────
# Core metrics
# ─────────────────────────────────────────────────────────────
def compute_metrics(trades, label="ALL"):
    if not trades:
        return {"label": label, "count": 0}

    pnls     = [safe_float(t.get("pnl_pts")) for t in trades]
    net_pnls = [safe_float(t.get("net_pnl_rs")) for t in trades]
    peaks    = [safe_float(t.get("peak_pnl")) for t in trades]

    wins     = [p for p in pnls if p > 0]
    losses   = [p for p in pnls if p <= 0]
    breakevens = [p for p in pnls if p == 0]

    gross_wins   = sum(wins)
    gross_losses = abs(sum(losses))
    profit_factor = round(gross_wins / gross_losses, 2) if gross_losses > 0 else float("inf")

    total_net = round(sum(net_pnls), 2)
    total_pts = round(sum(pnls), 2)

    win_rate  = round(len(wins) / len(trades) * 100, 1)
    avg_win   = round(sum(wins) / len(wins), 1) if wins else 0
    avg_loss  = round(sum(losses) / len(losses), 1) if losses else 0
    avg_peak  = round(sum(peaks) / len(peaks), 1)

    # Capture ratio: how much of peak_pnl did we actually lock?
    capture_ratios = []
    for t in trades:
        pk = safe_float(t.get("peak_pnl"))
        pnl = safe_float(t.get("pnl_pts"))
        if pk > 2:
            capture_ratios.append(pnl / pk)
    avg_capture = round(sum(capture_ratios) / len(capture_ratios) * 100, 1) if capture_ratios else 0

    # Exit reason breakdown
    exit_counts = defaultdict(int)
    exit_pts    = defaultdict(float)
    for t in trades:
        r = t.get("exit_reason", "UNKNOWN")
        exit_counts[r] += 1
        exit_pts[r]    += safe_float(t.get("pnl_pts"))

    # Entry mode breakdown
    mode_counts = defaultdict(int)
    mode_pts    = defaultdict(float)
    for t in trades:
        m = t.get("entry_mode", "UNKNOWN")
        mode_counts[m] += 1
        mode_pts[m]    += safe_float(t.get("pnl_pts"))

    # Time-of-day analysis (group by hour)
    hour_counts = defaultdict(int)
    hour_wins   = defaultdict(int)
    hour_pts    = defaultdict(float)
    for t in trades:
        et = t.get("entry_time", "")
        try:
            hh = int(et.split(":")[0])
        except Exception:
            continue
        hour_counts[hh] += 1
        if safe_float(t.get("pnl_pts")) > 0:
            hour_wins[hh] += 1
        hour_pts[hh] += safe_float(t.get("pnl_pts"))

    # Peak tier distribution (how far trades run)
    peak_buckets = {"<12": 0, "12-24": 0, "24-36": 0, "36-50": 0, ">50": 0}
    for pk in peaks:
        if pk < 12:
            peak_buckets["<12"] += 1
        elif pk < 24:
            peak_buckets["12-24"] += 1
        elif pk < 36:
            peak_buckets["24-36"] += 1
        elif pk < 50:
            peak_buckets["36-50"] += 1
        else:
            peak_buckets[">50"] += 1

    # SL hit analysis: trades that hit initial SL vs locked profit
    initial_sl_hits = [t for t in trades if safe_float(t.get("pnl_pts")) <= -10]
    locked_exits    = [t for t in trades if safe_float(t.get("pnl_pts")) >= 0
                       and t.get("exit_reason") in ("VISHAL_TRAIL", "EOD_EXIT")]
    trail_exits     = [t for t in trades if t.get("exit_reason") == "VISHAL_TRAIL"]

    # Candles held
    candles = [safe_float(t.get("candles_held")) for t in trades]
    avg_candles = round(sum(c for c in candles if c > 0) / max(1, sum(1 for c in candles if c > 0)), 1)

    # Body pct at entry
    bodies = [safe_float(t.get("entry_body_pct")) for t in trades if safe_float(t.get("entry_body_pct")) > 0]
    avg_body = round(sum(bodies) / len(bodies), 1) if bodies else 0

    return {
        "label": label,
        "count": len(trades),
        "total_pts": total_pts,
        "total_net_rs": total_net,
        "win_rate": win_rate,
        "wins": len(wins),
        "losses": len(losses),
        "breakevens": len(breakevens),
        "avg_win": avg_win,
        "avg_loss": avg_loss,
        "profit_factor": profit_factor,
        "avg_peak": avg_peak,
        "avg_capture_pct": avg_capture,
        "exit_counts": dict(exit_counts),
        "exit_pts": {k: round(v, 1) for k, v in exit_pts.items()},
        "mode_counts": dict(mode_counts),
        "mode_pts": {k: round(v, 1) for k, v in mode_pts.items()},
        "hour_counts": dict(hour_counts),
        "hour_wins": dict(hour_wins),
        "hour_pts": {k: round(v, 1) for k, v in hour_pts.items()},
        "peak_buckets": peak_buckets,
        "initial_sl_hits": len(initial_sl_hits),
        "locked_exits": len(locked_exits),
        "trail_exits": len(trail_exits),
        "avg_candles": avg_candles,
        "avg_body": avg_body,
    }


def daily_summary(trades):
    by_date = defaultdict(list)
    for t in trades:
        by_date[t.get("date", "")].append(t)
    rows = []
    for d in sorted(by_date):
        ts = by_date[d]
        v7t = [t for t in ts if not str(t.get("entry_mode", "")).startswith("V8_")]
        v8t = [t for t in ts if str(t.get("entry_mode", "")).startswith("V8_")]
        v7_pts  = round(sum(safe_float(t.get("pnl_pts")) for t in v7t), 1)
        v8_pts  = round(sum(safe_float(t.get("pnl_pts")) for t in v8t), 1)
        v7_net  = round(sum(safe_float(t.get("net_pnl_rs")) for t in v7t), 2)
        v8_net  = round(sum(safe_float(t.get("net_pnl_rs")) for t in v8t), 2)
        v7_w    = sum(1 for t in v7t if safe_float(t.get("pnl_pts")) > 0)
        v8_w    = sum(1 for t in v8t if safe_float(t.get("pnl_pts")) > 0)
        rows.append({
            "date": d,
            "v7_trades": len(v7t), "v7_wins": v7_w,
            "v7_pts": v7_pts, "v7_net": v7_net,
            "v8_trades": len(v8t), "v8_wins": v8_w,
            "v8_pts": v8_pts, "v8_net": v8_net,
        })
    return rows


# ─────────────────────────────────────────────────────────────
# V7 parameter enhancement simulation
# ─────────────────────────────────────────────────────────────
def simulate_v7_sl_variants(v7_trades):
    """
    Test 3 SL variants on actual V7 trades using recorded peak_pnl.
    This is an upper-bound simulation — it assumes the trail runs to peak.
    """
    results = {}

    # Current: initial SL = -12
    # Enhanced A: initial SL = -10 (save 2pts per loser)
    # Enhanced B: LOCK_BE at +8 instead of +12 (lock BE earlier, smaller wins)
    # Enhanced C: LOCK_BE at +12 (current), but tighten first lock to +3 at peak>=8

    for variant, init_sl, be_at in [
        ("Current  (SL=-12, BE@12)", -12, 12),
        ("EnhA     (SL=-10, BE@12)", -10, 12),
        ("EnhB     (SL=-12, BE@8) ", -12, 8),
        ("EnhC     (SL=-10, BE@8) ", -10, 8),
    ]:
        total_pts = 0
        wins = 0
        losses = 0
        for t in v7_trades:
            peak = safe_float(t.get("peak_pnl"))
            real_pnl = safe_float(t.get("pnl_pts"))
            exit_reason = t.get("exit_reason", "")

            # For SL trades, apply new initial SL
            if exit_reason == "EMERGENCY_SL" and real_pnl <= -10:
                adjusted_pnl = init_sl
            elif exit_reason in ("VISHAL_TRAIL", "EOD_EXIT", "FORCE_EXIT"):
                # If peak >= be_at, we at least break even
                if peak >= be_at and real_pnl < 0:
                    adjusted_pnl = 0  # locked BE
                elif peak < be_at:
                    adjusted_pnl = init_sl  # didn't reach BE lock
                else:
                    adjusted_pnl = real_pnl  # no change
            else:
                adjusted_pnl = real_pnl

            total_pts += adjusted_pnl
            if adjusted_pnl > 0:
                wins += 1
            elif adjusted_pnl < 0:
                losses += 1

        results[variant] = {
            "total_pts": round(total_pts, 1),
            "wins": wins,
            "losses": losses,
            "win_rate": round(wins / len(v7_trades) * 100, 1) if v7_trades else 0,
        }

    return results


def body_pct_gate_analysis(v7_trades):
    """Analyze what threshold for body_pct best separates wins from losses."""
    thresholds = [20, 30, 40, 50, 60]
    results = {}
    for thr in thresholds:
        above = [t for t in v7_trades if safe_float(t.get("entry_body_pct")) >= thr]
        below = [t for t in v7_trades if 0 < safe_float(t.get("entry_body_pct")) < thr]

        above_wr = round(sum(1 for t in above if safe_float(t.get("pnl_pts")) > 0)
                         / max(1, len(above)) * 100, 1) if above else 0
        below_wr = round(sum(1 for t in below if safe_float(t.get("pnl_pts")) > 0)
                         / max(1, len(below)) * 100, 1) if below else 0
        above_pts = round(sum(safe_float(t.get("pnl_pts")) for t in above), 1)
        below_pts = round(sum(safe_float(t.get("pnl_pts")) for t in below), 1)

        results[thr] = {
            "above_count": len(above), "above_wr": above_wr, "above_pts": above_pts,
            "below_count": len(below), "below_wr": below_wr, "below_pts": below_pts,
        }
    return results


def hourly_gate_analysis(v7_trades):
    """Best and worst trading hours."""
    hour_data = defaultdict(lambda: {"trades": 0, "wins": 0, "pts": 0.0})
    for t in v7_trades:
        et = t.get("entry_time", "")
        try:
            hh = int(et.split(":")[0])
        except Exception:
            continue
        hour_data[hh]["trades"] += 1
        pnl = safe_float(t.get("pnl_pts"))
        hour_data[hh]["pts"] += pnl
        if pnl > 0:
            hour_data[hh]["wins"] += 1

    results = {}
    for hh in sorted(hour_data):
        d = hour_data[hh]
        results[hh] = {
            "trades": d["trades"],
            "wins": d["wins"],
            "pts": round(d["pts"], 1),
            "win_rate": round(d["wins"] / d["trades"] * 100, 1) if d["trades"] else 0,
        }
    return results


def xleg_gate_analysis(v7_trades):
    """Does xleg PASS/FAIL predict outcome?"""
    groups = {"PASS": [], "FAIL": [], "UNKNOWN": []}
    for t in v7_trades:
        sig = t.get("xleg_signal", "UNKNOWN")
        if sig not in groups:
            sig = "UNKNOWN"
        groups[sig].append(t)

    results = {}
    for grp, ts in groups.items():
        if not ts:
            continue
        wr = round(sum(1 for t in ts if safe_float(t.get("pnl_pts")) > 0)
                   / len(ts) * 100, 1)
        pts = round(sum(safe_float(t.get("pnl_pts")) for t in ts), 1)
        avg_pk = round(sum(safe_float(t.get("peak_pnl")) for t in ts) / len(ts), 1)
        results[grp] = {"count": len(ts), "win_rate": wr, "pts": pts, "avg_peak": avg_pk}
    return results


# ─────────────────────────────────────────────────────────────
# Markdown report generator
# ─────────────────────────────────────────────────────────────
def bar(value, max_val=100, width=20, fill="█", empty="░"):
    filled = int(round(value / max(max_val, 1) * width))
    return fill * filled + empty * (width - filled)


def md_table(headers, rows):
    col_widths = [max(len(str(h)), max((len(str(r[i])) for r in rows), default=0))
                  for i, h in enumerate(headers)]
    sep = "| " + " | ".join("-" * w for w in col_widths) + " |"
    header_row = "| " + " | ".join(str(h).ljust(col_widths[i]) for i, h in enumerate(headers)) + " |"
    lines = [header_row, sep]
    for row in rows:
        lines.append("| " + " | ".join(str(row[i]).ljust(col_widths[i]) for i in range(len(headers))) + " |")
    return "\n".join(lines)


def generate_report(trades, extra_date=None):
    v7_trades, v8_trades = split_v7_v8(trades)
    v7m = compute_metrics(v7_trades, "V7 (15-min)")
    v8m = compute_metrics(v8_trades, "V8 (3-min)")
    all_m = compute_metrics(trades, "ALL")
    daily = daily_summary(trades)

    sl_variants = simulate_v7_sl_variants(v7_trades)
    body_analysis = body_pct_gate_analysis(v7_trades)
    hourly_analysis = hourly_gate_analysis(v7_trades)
    xleg_analysis = xleg_gate_analysis(v7_trades)

    run_date = date.today().strftime("%Y-%m-%d")
    lines = []

    # ── Header ──
    lines += [
        f"# VRL v16.7 — Strategy Analysis Report",
        f"",
        f"> Generated: {run_date} | Data: May 1–11 2026 (+ today if pushed)",
        f"> V7 = 15-min candle strategy | V8 = 3-min candle strategy",
        f"",
        f"---",
        f"",
    ]

    # ── Day-by-day P&L table ──
    lines += ["## Daily P&L Summary", ""]
    day_headers = ["Date", "V7 Trades", "V7 W/L", "V7 Pts", "V7 Net ₹",
                   "V8 Trades", "V8 W/L", "V8 Pts", "V8 Net ₹", "Day Total Pts"]
    day_rows = []
    for d in daily:
        day_rows.append([
            d["date"],
            d["v7_trades"],
            f"{d['v7_wins']}/{d['v7_trades'] - d['v7_wins']}",
            f"{'+' if d['v7_pts'] >= 0 else ''}{d['v7_pts']}",
            f"{'+' if d['v7_net'] >= 0 else ''}{d['v7_net']}",
            d["v8_trades"],
            f"{d['v8_wins']}/{d['v8_trades'] - d['v8_wins']}",
            f"{'+' if d['v8_pts'] >= 0 else ''}{d['v8_pts']}",
            f"{'+' if d['v8_net'] >= 0 else ''}{d['v8_net']}",
            f"{'+' if (d['v7_pts']+d['v8_pts']) >= 0 else ''}{round(d['v7_pts']+d['v8_pts'],1)}",
        ])
    lines += [md_table(day_headers, day_rows), ""]

    # ── V7 Overall metrics ──
    lines += [
        "---",
        "",
        "## V7 (15-min) Strategy — Core Metrics",
        "",
        f"| Metric | Value |",
        f"|--------|-------|",
        f"| Total Trades | {v7m['count']} |",
        f"| Win Rate | **{v7m['win_rate']}%** ({v7m['wins']}W / {v7m['losses']}L / {v7m['breakevens']}BE) |",
        f"| Total PnL (pts) | **{'+' if v7m['total_pts'] >= 0 else ''}{v7m['total_pts']} pts** |",
        f"| Total Net (₹) | {'+' if v7m['total_net_rs'] >= 0 else ''}{v7m['total_net_rs']} |",
        f"| Avg Win | +{v7m['avg_win']} pts |",
        f"| Avg Loss | {v7m['avg_loss']} pts |",
        f"| Profit Factor | {v7m['profit_factor']} |",
        f"| Avg Peak PnL | {v7m['avg_peak']} pts |",
        f"| Avg Capture % | {v7m['avg_capture_pct']}% (of peak_pnl captured at exit) |",
        f"| Avg Candles Held | {v7m['avg_candles']} |",
        f"| Avg Entry Body % | {v7m['avg_body']}% |",
        f"| Initial SL Hits | {v7m['initial_sl_hits']} ({round(v7m['initial_sl_hits']/max(1,v7m['count'])*100,1)}%) |",
        f"| Trail/EOD Exits | {v7m['trail_exits']} ({round(v7m['trail_exits']/max(1,v7m['count'])*100,1)}%) |",
        "",
    ]

    # ── Exit reason breakdown ──
    lines += ["### V7 Exit Reason Breakdown", ""]
    ec = v7m["exit_counts"]
    ep = v7m["exit_pts"]
    exit_rows = sorted(ec.items(), key=lambda x: -abs(ec[x[0]]))
    lines += [md_table(
        ["Exit Reason", "Count", "Total Pts", "Avg Pts"],
        [[r, ec[r], ep.get(r, 0), round(ep.get(r, 0) / ec[r], 1)] for r, _ in exit_rows]
    ), ""]

    # ── Entry mode breakdown ──
    lines += ["### V7 Entry Mode Breakdown", ""]
    mc = v7m["mode_counts"]
    mp = v7m["mode_pts"]
    mode_rows = sorted(mc.items(), key=lambda x: -mc[x[0]])
    lines += [md_table(
        ["Entry Mode", "Count", "Total Pts", "Avg Pts"],
        [[m, mc[m], mp.get(m, 0), round(mp.get(m, 0) / mc[m], 1)] for m, _ in mode_rows]
    ), ""]

    # ── Peak bucket distribution ──
    lines += ["### V7 Peak PnL Distribution", ""]
    pb = v7m["peak_buckets"]
    total_count = max(1, v7m["count"])
    lines += [md_table(
        ["Peak Tier", "Count", "%", "Bar"],
        [[tier, cnt, f"{round(cnt/total_count*100,1)}%", bar(cnt, total_count, 15)]
         for tier, cnt in pb.items()]
    ), ""]

    # ── Hourly analysis ──
    lines += ["### V7 Win Rate by Hour", ""]
    hourly_rows = []
    for hh in sorted(hourly_analysis):
        d = hourly_analysis[hh]
        hourly_rows.append([
            f"{hh:02d}:xx",
            d["trades"],
            f"{d['win_rate']}%",
            f"{'+' if d['pts'] >= 0 else ''}{d['pts']}",
            bar(d["win_rate"], 100, 15),
        ])
    lines += [md_table(["Hour", "Trades", "Win%", "Pts", "Win Rate Bar"], hourly_rows), ""]

    # ── xLeg gate ──
    lines += ["### V7 Cross-Leg Gate (PASS = other leg dying)", ""]
    xl_rows = []
    for grp in ["PASS", "FAIL", "UNKNOWN"]:
        if grp not in xleg_analysis:
            continue
        d = xleg_analysis[grp]
        xl_rows.append([grp, d["count"], f"{d['win_rate']}%",
                         f"{'+' if d['pts'] >= 0 else ''}{d['pts']}", d["avg_peak"]])
    lines += [md_table(["xLeg Signal", "Count", "Win%", "Pts", "Avg Peak"], xl_rows), ""]

    # ── V8 Overall metrics ──
    lines += [
        "---",
        "",
        "## V8 (3-min) Strategy — Core Metrics",
        "",
        f"| Metric | Value |",
        f"|--------|-------|",
        f"| Total Trades | {v8m['count']} |",
        f"| Win Rate | **{v8m['win_rate']}%** ({v8m['wins']}W / {v8m['losses']}L / {v8m['breakevens']}BE) |",
        f"| Total PnL (pts) | **{'+' if v8m['total_pts'] >= 0 else ''}{v8m['total_pts']} pts** |",
        f"| Total Net (₹) | {'+' if v8m['total_net_rs'] >= 0 else ''}{v8m['total_net_rs']} |",
        f"| Avg Win | +{v8m['avg_win']} pts |",
        f"| Avg Loss | {v8m['avg_loss']} pts |",
        f"| Profit Factor | {v8m['profit_factor']} |",
        f"| Avg Peak PnL | {v8m['avg_peak']} pts |",
        "",
    ]

    # ── V8 entry tier breakdown ──
    lines += ["### V8 Entry Tier Breakdown", ""]
    mc8 = v8m["mode_counts"]
    mp8 = v8m["mode_pts"]
    if mc8:
        mode_rows8 = sorted(mc8.items(), key=lambda x: -mc8[x[0]])
        lines += [md_table(
            ["Entry Tier", "Count", "Total Pts", "Avg Pts"],
            [[m, mc8[m], mp8.get(m, 0), round(mp8.get(m, 0) / mc8[m], 1)] for m, _ in mode_rows8]
        ), ""]
    else:
        lines += ["> No V8 trades yet.\n"]

    # ─────────────────────────────────────────────────────────
    # V7 PARAMETER ENHANCEMENT SECTION
    # ─────────────────────────────────────────────────────────
    lines += [
        "---",
        "",
        "## V7 Parameter Enhancement Analysis",
        "",
        "> Based on historical data — simulations use recorded peak_pnl as upper bound.",
        "",
    ]

    # ── 1. SL variant simulation ──
    lines += ["### 1. SL Ladder Variants — Simulated P&L", ""]
    lines += [
        "Testing 4 variants on actual V7 trades:",
        "- **Current**: Initial SL = -12, BE lock at peak ≥ 12",
        "- **EnhA**: Tighten Initial SL to -10 (save 2pts on each losing trade)",
        "- **EnhB**: Keep SL = -12 but lock BE earlier at peak ≥ 8",
        "- **EnhC**: Tighten SL to -10 AND lock BE earlier at peak ≥ 8",
        "",
    ]
    sl_rows = []
    for variant, data in sl_variants.items():
        sl_rows.append([
            variant,
            data["wins"],
            data["losses"],
            f"{data['win_rate']}%",
            f"{'+' if data['total_pts'] >= 0 else ''}{data['total_pts']}",
        ])
    lines += [md_table(["Variant", "Wins", "Losses", "Win%", "Total Pts"], sl_rows), ""]

    # Find best variant
    best = max(sl_variants.items(), key=lambda x: x[1]["total_pts"])
    current_pts = sl_variants.get("Current  (SL=-12, BE@12)", {}).get("total_pts", 0)
    lines += [
        f"> **Best variant: `{best[0].strip()}`** → {'+' if best[1]['total_pts'] >= 0 else ''}{best[1]['total_pts']} pts",
        f"> vs Current: {'+' if current_pts >= 0 else ''}{current_pts} pts",
        f"> Delta: {'+' if (best[1]['total_pts'] - current_pts) >= 0 else ''}{round(best[1]['total_pts'] - current_pts, 1)} pts",
        "",
    ]

    # ── 2. Body pct gate analysis ──
    lines += ["### 2. Entry Body % Gate — Which Threshold is Best?", ""]
    lines += [
        "Current: no body% gate on V7 (V7 uses close>EMA9_low + RSI gate).",
        "Testing: what if we require minimum body% at entry?",
        "",
    ]
    bp_rows = []
    for thr, d in body_analysis.items():
        bp_rows.append([
            f"body >= {thr}%",
            f"{d['above_count']} trades",
            f"{d['above_wr']}%",
            f"{'+' if d['above_pts'] >= 0 else ''}{d['above_pts']}",
            f"{d['below_count']} skipped",
            f"{d['below_wr']}% skip WR",
        ])
    lines += [md_table(
        ["Gate", "Qualifying", "Win%", "Pts", "Filtered", "Filtered Win%"],
        bp_rows
    ), ""]

    # Best body threshold
    best_thr = max(body_analysis.items(),
                   key=lambda x: x[1]["above_pts"] if x[1]["above_count"] >= 5 else -9999)
    lines += [
        f"> Best body gate: **≥ {best_thr[0]}%** → {'+' if best_thr[1]['above_pts'] >= 0 else ''}{best_thr[1]['above_pts']} pts on {best_thr[1]['above_count']} trades ({best_thr[1]['above_wr']}% WR)",
        "",
    ]

    # ── 3. Hourly window recommendation ──
    lines += ["### 3. Best Entry Window (Hour)", ""]
    best_hours = sorted(
        [(hh, d) for hh, d in hourly_analysis.items() if d["trades"] >= 3],
        key=lambda x: -x[1]["pts"]
    )[:3]
    worst_hours = sorted(
        [(hh, d) for hh, d in hourly_analysis.items() if d["trades"] >= 3],
        key=lambda x: x[1]["pts"]
    )[:3]

    if best_hours:
        lines += [f"**Top 3 hours by P&L:**"]
        for hh, d in best_hours:
            lines.append(f"- `{hh:02d}:xx` — {d['trades']} trades, {d['win_rate']}% WR, {'+' if d['pts'] >= 0 else ''}{d['pts']} pts")
        lines.append("")

    if worst_hours:
        lines += [f"**Worst 3 hours:**"]
        for hh, d in worst_hours:
            lines.append(f"- `{hh:02d}:xx` — {d['trades']} trades, {d['win_rate']}% WR, {d['pts']} pts")
        lines.append("")

    # ── 4. xLeg gate recommendation ──
    lines += ["### 4. Cross-Leg Gate Recommendation", ""]
    if "PASS" in xleg_analysis and "FAIL" in xleg_analysis:
        p = xleg_analysis["PASS"]
        f = xleg_analysis["FAIL"]
        delta_wr  = round(p["win_rate"] - f["win_rate"], 1)
        delta_pk  = round(p["avg_peak"] - f["avg_peak"], 1)
        lines += [
            f"| Signal | Count | Win% | Pts | Avg Peak |",
            f"|--------|-------|------|-----|----------|",
            f"| PASS (other leg dying) | {p['count']} | {p['win_rate']}% | {'+' if p['pts'] >= 0 else ''}{p['pts']} | {p['avg_peak']} |",
            f"| FAIL (other leg live)  | {f['count']} | {f['win_rate']}% | {'+' if f['pts'] >= 0 else ''}{f['pts']} | {f['avg_peak']} |",
            f"",
            f"> PASS vs FAIL delta: Win% +{delta_wr}pp | Peak +{delta_pk} pts",
            f"> {'✅ xLeg PASS gate adds value — filter FAIL signals' if delta_wr > 5 else '⚠️ xLeg gate shows marginal edge — keep monitoring'}",
            "",
        ]

    # ── 5. Summary recommendations ──
    lines += [
        "---",
        "",
        "## Summary — Top Recommended Enhancements for V7",
        "",
    ]

    rec_lines = []
    rec_num = 1

    if best[0] != "Current  (SL=-12, BE@12)":
        delta_pts = round(best[1]["total_pts"] - current_pts, 1)
        rec_lines.append(
            f"**{rec_num}. SL Tightening** — Switch to `{best[0].strip()}` for estimated **+{delta_pts} pts** gain.\n"
            f"   > Action: Change `EMERGENCY_SL_PTS` in config or adjust initial SL in `compute_trail_sl()`."
        )
        rec_num += 1

    if best_thr[1]["above_wr"] > v7m["win_rate"] + 3 and best_thr[1]["above_count"] >= 5:
        gap = best_thr[1]["above_pts"] - v7m["total_pts"]
        filtered = best_thr[1]["below_count"]
        rec_lines.append(
            f"**{rec_num}. Body % Entry Gate** — Require body ≥ {best_thr[0]}% at V7 entry.\n"
            f"   > Win rate improves from {v7m['win_rate']}% → {best_thr[1]['above_wr']}%. Filters {filtered} low-quality trades."
        )
        rec_num += 1

    if "PASS" in xleg_analysis and "FAIL" in xleg_analysis:
        if xleg_analysis["PASS"]["win_rate"] > xleg_analysis["FAIL"]["win_rate"] + 5:
            rec_lines.append(
                f"**{rec_num}. xLeg Gate** — Skip entries when xleg signal = FAIL.\n"
                f"   > PASS trades have {xleg_analysis['PASS']['win_rate']}% WR vs FAIL at {xleg_analysis['FAIL']['win_rate']}% WR."
            )
            rec_num += 1

    best_hr_data = max(hourly_analysis.values(), key=lambda x: x["pts"]) if hourly_analysis else {}
    worst_hr_list = [hh for hh, d in hourly_analysis.items() if d["trades"] >= 3 and d["pts"] < -10]
    if worst_hr_list:
        rec_lines.append(
            f"**{rec_num}. Time Window Filter** — Avoid entries during hours: {sorted(worst_hr_list)}.\n"
            f"   > These hours show consistent losses. Adding a `cutoff_before/after` for bad hours could help."
        )
        rec_num += 1

    if not rec_lines:
        rec_lines.append("_No significant improvements found — current parameters are well-tuned for available data._")

    lines += rec_lines
    lines += ["", "---", ""]

    # ── Footer ──
    lines += [
        "## Data Coverage",
        "",
        f"| | V7 | V8 |",
        f"|-|----|-----|",
        f"| Trades | {v7m['count']} | {v8m['count']} |",
        f"| Total Pts | {'+' if v7m['total_pts'] >= 0 else ''}{v7m['total_pts']} | {'+' if v8m['total_pts'] >= 0 else ''}{v8m['total_pts']} |",
        f"| Win Rate | {v7m['win_rate']}% | {v8m['win_rate']}% |",
        f"| Profit Factor | {v7m['profit_factor']} | {v8m['profit_factor']} |",
        "",
        f"_V8 launched {date(2026, 5, 7).strftime('%b %d')} — limited sample size, monitor more days._",
        "",
        "---",
        "*Report generated by `data_analysis/strategy_analysis.py`*",
    ]

    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────
def main():
    extra_date = None
    if len(sys.argv) > 1:
        extra_date = sys.argv[1].replace("-", "")  # accept 20260512 or 2026-05-12

    print("[VRL Analysis] Loading trades...")
    trades = load_all_trades(extra_date)
    print(f"[VRL Analysis] Loaded {len(trades)} total trades")

    v7_trades, v8_trades = split_v7_v8(trades)
    print(f"  V7: {len(v7_trades)} | V8: {len(v8_trades)}")

    if len(v7_trades) == 0:
        print("ERROR: No V7 trades found. Check data_analysis/multi_day/ path.")
        sys.exit(1)

    # Console quick summary
    v7m = compute_metrics(v7_trades)
    v8m = compute_metrics(v8_trades)
    print(f"\n{'='*55}")
    print(f"  V7  | {v7m['count']:3d} trades | WR {v7m['win_rate']:5.1f}% | "
          f"Pts {v7m['total_pts']:+.1f} | PF {v7m['profit_factor']}")
    print(f"  V8  | {v8m['count']:3d} trades | WR {v8m['win_rate']:5.1f}% | "
          f"Pts {v8m['total_pts']:+.1f} | PF {v8m['profit_factor']}")
    print(f"{'='*55}")

    print(f"\n[VRL Analysis] Generating report...")
    report = generate_report(trades, extra_date)

    with open(REPORT_PATH, "w") as f:
        f.write(report)
    print(f"[VRL Analysis] Report saved: {REPORT_PATH}")


if __name__ == "__main__":
    main()
