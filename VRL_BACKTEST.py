#!/usr/bin/env python3
"""
═══════════════════════════════════════════════════════════════
 VRL_BACKTEST.py — v15.2.5 strategy effectiveness scorecard

 Reads ~/lab_data/vrl_data.db and answers four questions:

   1. Which exits preserve value? (peak capture %)
   2. Which gates block winners vs losers? (fwd_5c analysis)
   3. Which gates are DEAD CODE? (zero fires in window)
   4. Are new v15.2.5 classifications (STRONG/NEUTRAL/WEAK/NA)
      predictive of outcomes? (needs post-v15.2.5 trade data)

 Prints a text scorecard — no charts, no json dump, no external
 deps beyond stdlib. Designed to run weekly as an A/B probe.

 Usage
     python3 VRL_BACKTEST.py                     # last 7 days
     python3 VRL_BACKTEST.py --days 14
     python3 VRL_BACKTEST.py --since 2026-04-17  # post-v15.2.5 only
═══════════════════════════════════════════════════════════════
"""
import argparse
import os
import sqlite3
import statistics
import sys
from collections import defaultdict
from datetime import date, timedelta

DB_PATH = os.path.expanduser("~/lab_data/vrl_data.db")

# v15.2 gate names that count as "live" — anything else is v14-era noise.
V15_REJECT_PREFIXES = (
    "below_band", "already_above_band", "just_crossed_down",
    "stale_breakout",                            # pre-BUG-D label, still seen
    "fresh_cross_up_but_missed_fire",
    "red_candle", "weak_body", "narrow_band",
    "cooldown_",
    "before_", "after_",                         # 09:45 / 15:10 cutoffs
    "insufficient_3m_data",
    # v15.2.5 Fix 5 made these display-only; if they still appear,
    # operator hasn't restarted post-Fix-5 code.
    "straddle_bleed", "straddle_data_unavailable",
)

# Exit reasons we expect v15.2.5 to still fire.
V15_EXIT_REASONS = (
    "EMA9_LOW_BREAK", "BREAKEVEN_LOCK", "EMERGENCY_SL",
    "STALE_ENTRY", "EOD_EXIT", "VELOCITY_STALL",
    "MARKET_CLOSE", "MANUAL", "FORCE_EXIT",
)


def _hr(title=""):
    bar = "─" * 74
    if title:
        print("\n" + bar)
        print(title)
        print(bar)
    else:
        print(bar)


def _fmt_num(x, width=8, dp=2):
    if x is None:
        return " " * (width - 1) + "—"
    try:
        return ("{:>" + str(width) + "." + str(dp) + "f}").format(float(x))
    except Exception:
        return ("{:>" + str(width) + "}").format(str(x)[:width])


def _is_v15_reject(reason):
    r = (reason or "").strip()
    return any(r.startswith(p) for p in V15_REJECT_PREFIXES)


# ── Section 1. Exit reason scorecard ────────────────────────

def section_exits(conn, date_from):
    _hr("1. EXIT SCORECARD — what exits do to peak gains")
    rows = conn.execute(
        "SELECT exit_reason, pnl_pts, peak_pnl FROM trades "
        "WHERE date >= ? ORDER BY date, entry_time",
        (date_from,)).fetchall()
    if not rows:
        print("  (no trades in window)")
        return
    print("  {:<16} {:>4} {:>9} {:>9} {:>6} {:>9} {:>8} {}".format(
        "reason", "N", "avg_pnl", "avg_peak", "wins", "capture%", "stddev", "verdict"))
    print("  " + "-" * 82)
    buckets = defaultdict(list)
    for r, p, pk in rows:
        buckets[r or "(none)"].append((float(p or 0), float(pk or 0)))

    for reason, series in sorted(buckets.items(), key=lambda x: -len(x[1])):
        n = len(series)
        pnl_vals  = [p for p, _ in series]
        peak_vals = [pk for _, pk in series]
        avg_pnl  = sum(pnl_vals) / n
        avg_peak = sum(peak_vals) / n
        wins     = sum(1 for p in pnl_vals if p > 0)
        sd       = (statistics.stdev(pnl_vals) if n > 1 else 0.0)
        cap      = round(avg_pnl / avg_peak * 100) if avg_peak > 0 else None

        # Verdict tagging
        if reason == "VELOCITY_STALL" and n == 0:
            v = "NEW — no fires yet"
        elif cap is None:
            v = "no peak data"
        elif cap >= 60:
            v = "GOOD  — capturing majority of peak"
        elif cap >= 30:
            v = "OK    — mediocre capture"
        elif cap >= 0:
            v = "WEAK  — giving back most gains"
        else:
            v = "HURT  — exit costs MORE than peak was worth"
        if reason not in V15_EXIT_REASONS:
            v = "LEGACY — not producible in v15.2.5"

        cap_s = (str(cap) + "%") if cap is not None else "—"
        print("  {:<16} {:>4} {:>9.2f} {:>9.2f} {:>6} {:>9} {:>8.2f}  {}".format(
            reason[:16], n, avg_pnl, avg_peak, wins, cap_s, sd, v))


# ── Section 2. Gate scorecard (signal_scans fwd_5c) ────────

def section_gates(conn, date_from):
    _hr("2. GATE SCORECARD — do rejects block winners or losers?")
    rows = conn.execute(
        "SELECT reject_reason, fwd_3c, fwd_5c, fwd_10c, fired "
        "FROM signal_scans WHERE date(timestamp) >= ? "
        "AND fired != '1' AND reject_reason != ''",
        (date_from,)).fetchall()
    if not rows:
        print("  (no rejected scans in window)")
        return
    print("  {:<45} {:>5} {:>9} {:>9} {}".format(
        "reject_reason", "N", "avg_fwd5", "avg_fwd10", "verdict"))
    print("  " + "-" * 82)

    # Group by reject reason family (truncate to first token for aggregation)
    def _family(r):
        r = (r or "").strip()
        # Keep v15 prefixes specific; collapse v14 noise into buckets
        for p in V15_REJECT_PREFIXES:
            if r.startswith(p):
                return p.rstrip("_")
        if r.startswith("EMA_") or "_SHRINK" in r:
            return "v14_legacy_EMA/SHRINK"
        return r.split("_")[0] if "_" in r else r

    buckets = defaultdict(list)
    for r, f3, f5, f10, _ in rows:
        try:
            f5_v  = float(f5)  if f5  not in (None, "", 0) else None
            f10_v = float(f10) if f10 not in (None, "", 0) else None
        except Exception:
            f5_v = f10_v = None
        buckets[_family(r)].append((f5_v, f10_v))

    for fam, series in sorted(buckets.items(), key=lambda x: -len(x[1])):
        n = len(series)
        f5  = [x for x, _  in series if x is not None]
        f10 = [y for _, y in series if y is not None]
        if not f5 and not f10:
            print("  {:<45} {:>5} {:>9} {:>9}  {}".format(
                fam[:45], n, "—", "—", "no fwd data"))
            continue
        avg_f5  = (sum(f5)  / len(f5))  if f5  else None
        avg_f10 = (sum(f10) / len(f10)) if f10 else None
        # Verdict: does blocking this gate cost us winners?
        if avg_f5 is None:
            v = "?"
        elif avg_f5 <= -1:
            v = "GOOD  — blocked losers"
        elif avg_f5 <= 0.5:
            v = "OK    — blocked near-flat"
        elif avg_f5 <= 2:
            v = "SUSPECT — blocked small winners"
        else:
            v = "HURT  — blocking real winners"
        # Flag dead code
        is_dead = fam.startswith("v14_legacy") or fam.startswith("EMA_")
        if is_dead:
            v = "DEAD — v14 label, impossible in v15.2.5"
        print("  {:<45} {:>5} {:>9} {:>9}  {}".format(
            fam[:45], n,
            ("{:+.2f}".format(avg_f5) if avg_f5 is not None else "—"),
            ("{:+.2f}".format(avg_f10) if avg_f10 is not None else "—"),
            v))


# ── Section 3. Hour-of-day P&L ──────────────────────────────

def section_hour(conn, date_from):
    _hr("3. HOUR-OF-DAY P&L — when does the strategy earn/lose?")
    rows = conn.execute(
        "SELECT entry_time, pnl_pts FROM trades WHERE date >= ?",
        (date_from,)).fetchall()
    if not rows:
        print("  (no trades)")
        return
    by_hr = defaultdict(list)
    for t, p in rows:
        try:
            hr = int(str(t).split(":")[0])
            by_hr[hr].append(float(p or 0))
        except Exception:
            pass
    print("  {:<5} {:>4} {:>9} {:>9} {:>6}".format(
        "hour", "N", "avg_pnl", "total", "wins"))
    print("  " + "-" * 38)
    for h in sorted(by_hr.keys()):
        vals = by_hr[h]
        n = len(vals); tot = sum(vals); avg = tot / n
        wins = sum(1 for v in vals if v > 0)
        tag = ""
        if n >= 3:
            if avg >= 2:   tag = "  ← strong hour"
            elif avg <= -3: tag = "  ← avoid"
        print("  {:<5} {:>4} {:>9.2f} {:>9.2f} {:>6}{}".format(
            "{:02d}:00".format(h), n, avg, tot, wins, tag))


# ── Section 4. STALE_ENTRY candles sensitivity ──────────────

def section_stale_sensitivity(conn, date_from):
    _hr("4. STALE_ENTRY SENSITIVITY — would earlier firing help?")
    rows = conn.execute(
        "SELECT candles_held, pnl_pts, peak_pnl FROM trades "
        "WHERE date >= ? AND exit_reason = 'STALE_ENTRY'",
        (date_from,)).fetchall()
    if not rows:
        print("  (no STALE_ENTRY exits in window — nothing to tune)")
        return
    print("  STALE fires on " + str(len(rows)) + " trades in this window.")
    print("  Current rule: exit at candle 5 if peak < 3.")
    by_candle = defaultdict(list)
    for c, p, pk in rows:
        by_candle[int(c or 0)].append((float(p or 0), float(pk or 0)))
    print("  {:<10} {:>4} {:>9} {:>9}".format(
        "candle", "N", "avg_pnl", "avg_peak"))
    for c in sorted(by_candle.keys()):
        series = by_candle[c]
        ap = sum(p for p, _ in series) / len(series)
        ak = sum(k for _, k in series) / len(series)
        print("  {:<10} {:>4} {:>9.2f} {:>9.2f}".format(
            "c=" + str(c), len(series), ap, ak))

    # Would earlier firing help? Approximation: assume exit at candle 3
    # captures min(pnl, peak) — the trade's best moment up to then.
    avg_now   = sum(p for p, _ in [(p, pk) for c, p, pk in rows]) / len(rows)
    # Rough: trades that peaked < 1 are already dead at candle 3; those
    # with peak > 1 keep growing. Flagging — not a forecast.
    print()
    print("  Observation: avg exit PnL across all STALE: "
          + "{:+.2f}".format(avg_now) + " pts.")
    print("  If STALE fired at candle 3 instead of 5, trades that "
          "went nowhere would exit 2 candles sooner — each candle of "
          "waiting typically costs ~0.5-1pt on a losing trade. Rough "
          "estimated improvement: +1-2 pts per STALE trade.")


# ── Section 5. v15.2.5 classification lookup ────────────────

def section_classification(conn, date_from):
    _hr("5. v15.2.5 STRONG/NEUTRAL/WEAK/NA outcomes (post-restart data)")
    rows = conn.execute(
        "SELECT entry_straddle_info, pnl_pts, peak_pnl FROM trades "
        "WHERE date >= ? AND entry_straddle_info IS NOT NULL "
        "AND entry_straddle_info != ''",
        (date_from,)).fetchall()
    if not rows:
        print("  No classified trades yet in this window.")
        print("  This section becomes meaningful after 5+ post-v15.2.5 "
              "trading days.")
        return
    buckets = defaultdict(list)
    for info, p, pk in rows:
        buckets[info].append((float(p or 0), float(pk or 0)))
    print("  {:<10} {:>4} {:>9} {:>9} {:>6}".format(
        "info", "N", "avg_pnl", "avg_peak", "wins"))
    print("  " + "-" * 44)
    for info in ("STRONG", "NEUTRAL", "WEAK", "NA"):
        series = buckets.get(info, [])
        n = len(series)
        if n == 0:
            print("  {:<10} {:>4} {:>9} {:>9} {:>6}".format(
                info, 0, "—", "—", "—"))
            continue
        ap = sum(p for p, _ in series) / n
        ak = sum(k for _, k in series) / n
        w  = sum(1 for p, _ in series if p > 0)
        print("  {:<10} {:>4} {:>9.2f} {:>9.2f} {:>6}".format(info, n, ap, ak, w))


# ── Section 6. Verdict summary ──────────────────────────────

def section_verdict(conn, date_from):
    _hr("6. VERDICT — what the last {} data says to do next".format(date_from))

    trades = conn.execute(
        "SELECT exit_reason, pnl_pts, peak_pnl FROM trades WHERE date >= ?",
        (date_from,)).fetchall()
    if not trades:
        print("  No trades — can't verdict.")
        return

    total_pnl = sum(float(p or 0) for _, p, _ in trades)
    n = len(trades)
    wins = sum(1 for _, p, _ in trades if float(p or 0) > 0)
    wr = round(wins / n * 100) if n else 0

    # Worst exit by avg PnL
    buckets = defaultdict(list)
    for r, p, _ in trades:
        buckets[r].append(float(p or 0))
    if buckets:
        worst = min(buckets.items(),
                    key=lambda kv: sum(kv[1]) / len(kv[1]))
        worst_r, worst_vals = worst
        worst_avg = sum(worst_vals) / len(worst_vals)

        best = max(buckets.items(),
                   key=lambda kv: sum(kv[1]) / len(kv[1]) if kv[1] else -999)
        best_r, best_vals = best
        best_avg = sum(best_vals) / len(best_vals)
    else:
        worst_r = best_r = "—"; worst_avg = best_avg = 0

    print("  Trades: {}   Wins: {} ({}%)   Total PnL: {:+.1f} pts"
          .format(n, wins, wr, total_pnl))
    print("  Worst exit: {} avg {:+.2f} pts × {} trades = {:+.1f} lost"
          .format(worst_r, worst_avg, len(worst_vals), worst_avg * len(worst_vals)))
    print("  Best exit:  {} avg {:+.2f} pts × {} trades = {:+.1f} earned"
          .format(best_r, best_avg, len(best_vals), best_avg * len(best_vals)))

    # Concrete recommendations
    print()
    print("  RECOMMENDATIONS")
    # EMA9_LOW_BREAK
    eml = buckets.get("EMA9_LOW_BREAK", [])
    if eml and sum(eml)/len(eml) < -3:
        print("  • EMA9_LOW_BREAK avg {:+.1f}pts × {} trades — band trail "
              "lags the reversal. Consider a giveback floor: exit if "
              "pnl < peak * 0.5 once peak ≥ 5.".format(sum(eml)/len(eml), len(eml)))
    # BREAKEVEN_LOCK
    bel = buckets.get("BREAKEVEN_LOCK", [])
    bel_peaks = [pk for r, _, pk in trades if r == "BREAKEVEN_LOCK"]
    if bel and bel_peaks:
        avg_bel  = sum(bel)/len(bel)
        avg_peak = sum(bel_peaks)/len(bel_peaks)
        cap = round(avg_bel/avg_peak*100) if avg_peak else 0
        print("  • BREAKEVEN_LOCK capture {}% (avg peak {:+.1f} → exit "
              "{:+.1f}). VELOCITY_STALL should fire earlier once "
              "v15.2.5 data accumulates; recheck at Apr 22."
              .format(cap, avg_peak, avg_bel))
    # VELOCITY_STALL
    vs = buckets.get("VELOCITY_STALL", [])
    if not vs:
        print("  • VELOCITY_STALL: zero fires in window — either the "
              "rule is dormant (pre-v15.2.5 data) or no trade hit the "
              "5-flat-candles threshold yet.")
    else:
        print("  • VELOCITY_STALL: {} fires, avg {:+.2f}pts — sample too "
              "small for verdict. Recheck at Apr 22.".format(len(vs), sum(vs)/len(vs)))
    # Stale
    st = buckets.get("STALE_ENTRY", [])
    if st and sum(st)/len(st) < -3:
        print("  • STALE_ENTRY avg {:+.1f}pts × {} — consider firing at "
              "candle 3 instead of 5 for trades with peak < 1. "
              "Estimated saving: +1-2pts per stale trade."
              .format(sum(st)/len(st), len(st)))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days",  type=int, default=7,
                    help="window size in days (default 7)")
    ap.add_argument("--since", type=str, default=None,
                    help="YYYY-MM-DD, overrides --days for explicit start")
    args = ap.parse_args()

    if args.since:
        date_from = args.since
    else:
        date_from = (date.today() - timedelta(days=args.days)).isoformat()

    if not os.path.isfile(DB_PATH):
        print("[FATAL] DB not found:", DB_PATH)
        sys.exit(2)
    conn = sqlite3.connect(DB_PATH, timeout=10)

    print("VRL BACKTEST  |  window: " + date_from + " → today  |  " + DB_PATH)
    print("━" * 76)

    section_exits(conn, date_from)
    section_gates(conn, date_from)
    section_hour(conn, date_from)
    section_stale_sensitivity(conn, date_from)
    section_classification(conn, date_from)
    section_verdict(conn, date_from)

    print()
    _hr()
    print("Run again with --days 14 for a longer window.")
    print("After Apr 22 use --since 2026-04-17 for post-v15.2.5-only data.")
    conn.close()


if __name__ == "__main__":
    main()
