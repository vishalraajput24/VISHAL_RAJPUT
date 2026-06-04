#!/usr/bin/env python3
"""
VRL Daily Analysis Report
Generates analysis/daily/YYYY-MM-DD.md from:
  - ~/lab_data/vrl_trade_log.csv      (live paper trades)
  - ~/VISHAL_RAJPUT/state/bw_gap_study.csv  (shadow signals study)
  - ~/logs/live/vrl_live.log          (raw session log)

Run at 4 PM IST via cron. Committed to GitHub automatically.
"""

import csv
import os
import sys
import re
from datetime import date, datetime
from collections import defaultdict

TODAY = date.today().isoformat()
REPORT_DATE = sys.argv[1] if len(sys.argv) > 1 else TODAY

BASE_DIR   = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TRADE_LOG  = os.path.expanduser("~/lab_data/vrl_trade_log.csv")
STUDY_CSV  = os.path.join(BASE_DIR, "state", "bw_gap_study.csv")
LIVE_LOG   = os.path.expanduser("~/logs/live/vrl_live.log")
OUT_DIR    = os.path.join(BASE_DIR, "analysis", "daily")
OUT_FILE   = os.path.join(OUT_DIR, f"{REPORT_DATE}.md")

os.makedirs(OUT_DIR, exist_ok=True)


# ── helpers ──────────────────────────────────────────────────────────────────

def load_csv(path):
    if not os.path.isfile(path):
        return []
    with open(path, newline="") as f:
        return list(csv.DictReader(f))

def flt(v, default=0.0):
    try:
        return float(v)
    except Exception:
        return default

def bar(val, max_val, width=20, char="█"):
    filled = int(round(val / max_val * width)) if max_val else 0
    return char * filled + "░" * (width - filled)


# ── load data ────────────────────────────────────────────────────────────────

all_trades  = load_csv(TRADE_LOG)
today_trades = [r for r in all_trades if r.get("date") == REPORT_DATE]

all_shadow  = load_csv(STUDY_CSV)
# handle both old format (outcome) and new format (pnl_pts)
today_shadow = [r for r in all_shadow if r.get("date") == REPORT_DATE]


# ── Section 1: Session Summary ────────────────────────────────────────────────

def session_summary(trades):
    if not trades:
        return "No trades today.\n"

    wins   = [r for r in trades if flt(r["pnl_pts"]) > 0]
    losses = [r for r in trades if flt(r["pnl_pts"]) <= 0]
    esls   = [r for r in trades if r.get("exit_reason") == "EMERGENCY_SL"]

    gross_pts = sum(flt(r["pnl_pts"]) for r in trades)
    net_rs    = sum(flt(r["net_pnl_rs"]) for r in trades)
    win_rate  = len(wins) / len(trades) * 100

    avg_win  = sum(flt(r["pnl_pts"]) for r in wins)  / max(len(wins), 1)
    avg_loss = sum(flt(r["pnl_pts"]) for r in losses) / max(len(losses), 1)
    expectancy = (win_rate/100 * avg_win) + ((1 - win_rate/100) * avg_loss)

    avg_peak_win  = sum(flt(r["peak_pnl"]) for r in wins)  / max(len(wins), 1)
    avg_peak_loss = sum(flt(r["peak_pnl"]) for r in losses) / max(len(losses), 1)

    lines = [
        f"| Metric | Value |",
        f"|--------|-------|",
        f"| Trades | {len(trades)} ({len(wins)}W / {len(losses)}L) |",
        f"| Win rate | {win_rate:.0f}% |",
        f"| Gross P&L | {gross_pts:+.1f} pts |",
        f"| Net after charges | Rs {net_rs:+.0f} |",
        f"| Avg win | +{avg_win:.1f} pts (peak avg +{avg_peak_win:.1f}) |",
        f"| Avg loss | {avg_loss:.1f} pts (peak avg +{avg_peak_loss:.1f}) |",
        f"| Expectancy | {expectancy:+.2f} pts/trade |",
        f"| ESL count | {len(esls)} / {len(trades)} ({len(esls)/len(trades)*100:.0f}%) |",
    ]
    return "\n".join(lines)


# ── Section 2: Trade-by-Trade ─────────────────────────────────────────────────

def trade_table(trades):
    if not trades:
        return "_No trades._\n"
    lines = [
        "| # | Time | Dir | Strike | Entry | Exit | Peak | PnL | Reason |",
        "|---|------|-----|--------|-------|------|------|-----|--------|",
    ]
    for i, r in enumerate(trades, 1):
        pnl  = flt(r["pnl_pts"])
        peak = flt(r["peak_pnl"])
        icon = "✅" if pnl > 0 else "❌"
        lines.append(
            f"| {i} | {r['entry_time']} | {r['direction']} | {r['strike']} "
            f"| {flt(r['entry_price']):.1f} | {flt(r['exit_price']):.1f} "
            f"| +{peak:.1f} | {icon} {pnl:+.1f} | {r['exit_reason']} |"
        )
    return "\n".join(lines)


# ── Section 3: ESL Deep Dive ──────────────────────────────────────────────────

def held_minutes(r, report_date):
    try:
        t1 = datetime.strptime(f"{report_date} {r['entry_time']}", "%Y-%m-%d %H:%M:%S")
        t2 = datetime.strptime(f"{report_date} {r['exit_time']}", "%Y-%m-%d %H:%M:%S")
        return int((t2 - t1).total_seconds() / 60)
    except Exception:
        return 0


def esl_analysis(trades):
    esls = [r for r in trades if r.get("exit_reason") == "EMERGENCY_SL"]
    if not esls:
        return "_No ESLs today. 🎉_\n"

    lines = [f"**{len(esls)} ESL(s) today**\n"]
    lines.append("| Time | Dir | Peak | Held (min) | Notes |")
    lines.append("|------|-----|------|------------|-------|")
    for r in esls:
        peak = flt(r["peak_pnl"])
        held = held_minutes(r, REPORT_DATE)
        note = ""
        if peak < 2:
            note = "⚠️ no momentum"
        if held > 15:
            note += (" · " if note else "") + f"⏱️ stuck {held}m"
        lines.append(f"| {r['entry_time']} | {r['direction']} | +{peak:.1f} | {held} | {note} |")

    no_momentum = [r for r in esls if flt(r["peak_pnl"]) < 2]
    stuck       = [r for r in esls if held_minutes(r, REPORT_DATE) > 15]
    if no_momentum:
        lines.append(f"\n**No-momentum entries (peak < 2):** {len(no_momentum)}/{len(esls)} — signal fired but price never moved.")
    if stuck:
        lines.append(f"**Stuck trades (held > 15 min):** {len(stuck)}/{len(esls)} — position never gained traction.")
    return "\n".join(lines)


# ── Section 4: Point 2 — Same-direction ESL re-entry patterns ────────────────

def point2_analysis(trades):
    """After an ESL, did the bot re-enter same direction within 20 min? What happened?"""
    lines = ["_Tracking: after ESL on direction X, did same-direction signal fire within 20 min?_\n"]
    lines.append("| ESL Time | Dir | Next Same-Dir | Gap (min) | Next Outcome | Action |")
    lines.append("|----------|-----|--------------|-----------|--------------|--------|")

    found = False
    for i, r in enumerate(trades):
        if r.get("exit_reason") != "EMERGENCY_SL":
            continue
        esl_dir  = r["direction"]
        esl_exit = r["exit_time"]
        try:
            t_esl = datetime.strptime(f"{REPORT_DATE} {esl_exit}", "%Y-%m-%d %H:%M:%S")
        except Exception:
            continue
        # find next trade in same direction
        for r2 in trades[i+1:]:
            if r2["direction"] != esl_dir:
                continue
            try:
                t2 = datetime.strptime(f"{REPORT_DATE} {r2['entry_time']}", "%Y-%m-%d %H:%M:%S")
            except Exception:
                continue
            gap_min = (t2 - t_esl).total_seconds() / 60
            if gap_min > 20:
                break
            pnl2 = flt(r2["pnl_pts"])
            outcome = f"+{pnl2:.0f}" if pnl2 > 0 else f"{pnl2:.0f}"
            action = "✅ was right to re-enter" if pnl2 > 0 else "❌ should have blocked"
            lines.append(
                f"| {esl_exit} | {esl_dir} | {r2['entry_time']} | {gap_min:.0f} | {outcome} pts | {action} |"
            )
            found = True
            break

    if not found:
        lines.append("| — | — | No same-direction re-entry within 20 min of ESL today | — | — | — |")

    return "\n".join(lines)


# ── Section 5: Point 3 — Consecutive ESL streaks ─────────────────────────────

def point3_analysis(trades):
    lines = ["_Tracking: consecutive ESL streaks and what trade followed._\n"]

    streaks = []
    streak = []
    for r in trades:
        if r.get("exit_reason") == "EMERGENCY_SL":
            streak.append(r)
        else:
            if len(streak) >= 2:
                streaks.append(streak[:])
            streak = []
    if len(streak) >= 2:
        streaks.append(streak[:])

    if not streaks:
        lines.append("_No streak of 2+ consecutive ESLs today._")
        return "\n".join(lines)

    lines.append("| Streak | ESL Times | Directions | Next Trade | Next PnL | Would 3-ESL pause help? |")
    lines.append("|--------|-----------|------------|------------|----------|------------------------|")

    all_trade_times = [r["entry_time"] for r in trades]
    for s in streaks:
        times = ", ".join(r["entry_time"] for r in s)
        dirs  = " → ".join(r["direction"] for r in s)
        # find next trade after this streak
        last_idx = trades.index(s[-1])
        if last_idx + 1 < len(trades):
            nxt = trades[last_idx + 1]
            nxt_pnl = flt(nxt["pnl_pts"])
            nxt_str = f"{nxt['entry_time']} {nxt['direction']} {nxt_pnl:+.0f}pts"
            help_str = "No — next trade won" if nxt_pnl > 0 else "Yes — next trade ESL'd"
        else:
            nxt_str = "—"
            help_str = "N/A"
        lines.append(f"| {len(s)} ESLs | {times} | {dirs} | {nxt_str} | — | {help_str} |")

    return "\n".join(lines)


# ── Section 6: BW × Gap Shadow Study (cumulative) ────────────────────────────

def bw_gap_study(shadow_rows):
    # Only use new-format rows that have pnl_pts column
    valid = [r for r in shadow_rows if "pnl_pts" in r and r.get("bw") and r.get("gap")]
    if not valid:
        return "_No shadow study data yet. Collecting..._\n"

    from collections import defaultdict
    buckets = defaultdict(list)
    for r in valid:
        bw_bucket  = int(float(r["bw"]))
        gap_bucket = int(float(r["gap"]))
        pnl        = flt(r.get("pnl_pts", 0))
        peak       = flt(r.get("peak_pts", 0))
        buckets[(bw_bucket, gap_bucket)].append({"pnl": pnl, "peak": peak})

    # All cumulative data
    all_valid = [r for r in all_shadow if "pnl_pts" in r and r.get("bw") and r.get("gap")]
    all_buckets = defaultdict(list)
    for r in all_valid:
        bw_bucket  = int(float(r["bw"]))
        gap_bucket = int(float(r["gap"]))
        pnl        = flt(r.get("pnl_pts", 0))
        peak       = flt(r.get("peak_pts", 0))
        all_buckets[(bw_bucket, gap_bucket)].append({"pnl": pnl, "peak": peak})

    lines = []
    if valid:
        lines.append(f"**Today — {len(valid)} shadow signals logged**\n")
        lines.append("| BW | Gap | N | Win% | Avg Peak | Avg PnL | Best | Worst |")
        lines.append("|----|-----|---|------|----------|---------|------|-------|")
        for (bw, gap) in sorted(buckets.keys()):
            rows = buckets[(bw, gap)]
            wins = [x for x in rows if x["pnl"] > 0]
            avg_pnl  = sum(x["pnl"] for x in rows) / len(rows)
            avg_peak = sum(x["peak"] for x in rows) / len(rows)
            best  = max(x["pnl"] for x in rows)
            worst = min(x["pnl"] for x in rows)
            win_pct = len(wins) / len(rows) * 100
            lines.append(f"| {bw} | {gap} | {len(rows)} | {win_pct:.0f}% "
                         f"| +{avg_peak:.1f} | {avg_pnl:+.1f} | {best:+.1f} | {worst:+.1f} |")

    if all_buckets:
        n_days = len(set(r["date"] for r in all_valid))
        lines.append(f"\n**Cumulative ({n_days} days, {len(all_valid)} signals total)**\n")
        lines.append("| BW | Gap | N | Win% | Avg Peak | Avg PnL | Verdict |")
        lines.append("|----|-----|---|------|----------|---------|---------|")
        for (bw, gap) in sorted(all_buckets.keys()):
            rows = all_buckets[(bw, gap)]
            wins = [x for x in rows if x["pnl"] > 0]
            avg_pnl  = sum(x["pnl"] for x in rows) / len(rows)
            avg_peak = sum(x["peak"] for x in rows) / len(rows)
            win_pct  = len(wins) / len(rows) * 100
            verdict = "✅ KEEP" if avg_pnl > 2 and win_pct >= 55 else ("❌ BLOCK" if avg_pnl < -3 else "📊 MORE DATA")
            lines.append(f"| {bw} | {gap} | {len(rows)} | {win_pct:.0f}% "
                         f"| +{avg_peak:.1f} | {avg_pnl:+.1f} | {verdict} |")

    return "\n".join(lines)


# ── Section 7: Trail Ladder Efficiency ───────────────────────────────────────

def trail_efficiency(trades):
    if not trades:
        return "_No trades._\n"

    tiers = defaultdict(list)
    for r in trades:
        tier = r.get("entry_mode", "").replace("V10_", "") or "INITIAL"
        tiers[tier].append(flt(r["pnl_pts"]))

    lines = ["| Exit Tier | Count | Avg PnL | Total PnL |",
             "|-----------|-------|---------|-----------|"]
    order = ["INITIAL", "LOCK_4", "LOCK_10", "LOCK_12", "LOCK_20", "LOCK_30", "LOCK_36", "LOCK_50"]
    for tier in order:
        if tier not in tiers:
            continue
        vals = tiers[tier]
        avg  = sum(vals) / len(vals)
        tot  = sum(vals)
        lines.append(f"| {tier} | {len(vals)} | {avg:+.1f} | {tot:+.1f} |")

    return "\n".join(lines)


# ── Section 8: Opening Blackout — log grep ───────────────────────────────────

def opening_blackout_log():
    if not os.path.isfile(LIVE_LOG):
        return "_Log not found._\n"

    blocked = []
    pattern = re.compile(r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}).*open_blackout")
    with open(LIVE_LOG) as f:
        for line in f:
            if REPORT_DATE not in line:
                continue
            m = pattern.search(line)
            if m:
                blocked.append(m.group(1))

    # deduplicate by direction+minute
    seen = set()
    unique = []
    for ts in blocked:
        key = ts[:16]
        if key not in seen:
            seen.add(key)
            unique.append(ts)

    if not unique:
        return "_No signals blocked by opening blackout today (or market opened after 9:45)._\n"

    lines = [f"**{len(unique)} signal(s) suppressed by 9:15–9:45 blackout:**\n"]
    lines.append("| Suppressed at |")
    lines.append("|---------------|")
    for ts in unique[:10]:
        lines.append(f"| {ts} |")
    lines.append(f"\n_Shadow signals during blackout still tracked in bw_gap_study.csv._")
    return "\n".join(lines)


# ── Section 9: Running Totals (all-time) ─────────────────────────────────────

def running_totals(all_trades):
    if not all_trades:
        return "_No historical data._\n"

    wins   = [r for r in all_trades if flt(r["pnl_pts"]) > 0]
    losses = [r for r in all_trades if flt(r["pnl_pts"]) <= 0]
    esls   = [r for r in all_trades if r.get("exit_reason") == "EMERGENCY_SL"]
    gross  = sum(flt(r["pnl_pts"]) for r in all_trades)
    net_rs = sum(flt(r["net_pnl_rs"]) for r in all_trades)
    wr     = len(wins) / len(all_trades) * 100

    first_date = min(r["date"] for r in all_trades)
    last_date  = max(r["date"] for r in all_trades)

    lines = [
        f"| Metric | Value |",
        f"|--------|-------|",
        f"| Period | {first_date} → {last_date} |",
        f"| Total trades | {len(all_trades)} |",
        f"| Win rate | {wr:.1f}% |",
        f"| Gross P&L | {gross:+.1f} pts |",
        f"| Net after charges | Rs {net_rs:+.0f} |",
        f"| ESL rate | {len(esls)/len(all_trades)*100:.0f}% ({len(esls)} total) |",
    ]
    return "\n".join(lines)


# ── Assemble report ───────────────────────────────────────────────────────────

lines = [
    f"# VRL Daily Analysis — {REPORT_DATE}",
    f"_Generated at {datetime.now().strftime('%H:%M IST')} · Data: live paper trades + shadow signals_",
    "",
    "---",
    "",
    "## 1. Session Summary",
    "",
    session_summary(today_trades),
    "",
    "---",
    "",
    "## 2. Trade-by-Trade",
    "",
    trade_table(today_trades),
    "",
    "---",
    "",
    "## 3. ESL Deep Dive",
    "",
    esl_analysis(today_trades),
    "",
    "---",
    "",
    "## 4. Point 2 — Same-Direction ESL Re-entry Patterns",
    "",
    point2_analysis(today_trades),
    "",
    "---",
    "",
    "## 5. Point 3 — Consecutive ESL Streaks",
    "",
    point3_analysis(today_trades),
    "",
    "---",
    "",
    "## 6. BW × Gap Shadow Study",
    "",
    bw_gap_study(today_shadow),
    "",
    "---",
    "",
    "## 7. Trail Ladder Efficiency",
    "",
    trail_efficiency(today_trades),
    "",
    "---",
    "",
    "## 8. Opening Blackout (9:15–9:45)",
    "",
    opening_blackout_log(),
    "",
    "---",
    "",
    "## 9. Running Totals (All-Time)",
    "",
    running_totals(all_trades),
    "",
    "---",
    f"_Auto-generated by analysis/daily_report.py · VRL v20_",
]

report = "\n".join(lines)

with open(OUT_FILE, "w") as f:
    f.write(report)

print(f"Report written: {OUT_FILE}")
