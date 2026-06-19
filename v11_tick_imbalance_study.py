#!/usr/bin/env python3
"""
v11_tick_imbalance_study.py — STANDALONE, READ-ONLY tick-flow study for V11.
================================================================================
Joins the per-minute order-flow capture (~/lab_data/tick_delta_log.csv, from
tick_delta_collector.py) against the live V11 trade log
(~/lab_data/vrl_trade_log.csv) and answers TWO questions the owner asked:

  (A) ENTRY  — at the trade's entry minute, does tick imbalance separate winners
               from losers?  Tested signals:
                 • own_delta        (buy/sell imbalance of the leg we BOUGHT)
                 • opp_delta        (imbalance of the opposite leg)
                 • ce_minus_pe_delta aligned WITH the trade direction
                 • fut_delta        aligned WITH direction (CE wants fut buying)
                 • cum_delta        (daily futures cumulative delta at entry)

  (B) IN-TRADE — minute by minute from entry to exit, how does imbalance behave?
                 • fraction of held minutes where own-leg flow was SUPPORTIVE
                 • does own-leg flow FLIP against us in the last 1-2 min before
                   exit (i.e. does order-flow LEAD the SL / trail-out)?
                 • does supportive in-trade flow correlate with bigger peak/pnl?

NOTHING here trades or writes state. It only reads two CSVs and prints.
Until ~2 weeks of tick data exist it will say "insufficient" — that is expected;
this is the harness, parked to run on the ~26 Jun data review.

Usage:  python3 v11_tick_imbalance_study.py
"""
import csv, os, datetime as dt
from collections import defaultdict

TICK_CSV  = os.path.expanduser("~/lab_data/tick_delta_log.csv")
TRADE_CSV = os.path.expanduser("~/lab_data/vrl_trade_log.csv")


def _f(x, d=0.0):
    try:
        return float(x)
    except (TypeError, ValueError):
        return d


def _minkey(date_str, time_str):
    """Floor a 'HH:MM:SS' (or 'HH:MM') entry/exit stamp to a 'YYYY-MM-DD HH:MM' key."""
    t = time_str.strip().split(".")[0]
    parts = t.split(":")
    if len(parts) < 2:
        return None
    return f"{date_str} {parts[0].zfill(2)}:{parts[1].zfill(2)}"


def load_ticks():
    """minute-key -> row dict of per-minute flow."""
    if not os.path.exists(TICK_CSV):
        return {}
    out = {}
    with open(TICK_CSV) as f:
        for r in csv.DictReader(f):
            out[r["minute"].strip()] = r
    return out


def load_trades():
    if not os.path.exists(TRADE_CSV):
        return []
    with open(TRADE_CSV) as f:
        return list(csv.DictReader(f))


def supportive_own(direction, row):
    """Is the leg we hold being net-bought this minute?  CE->ce_delta, PE->pe_delta."""
    leg = "ce_delta" if direction == "CE" else "pe_delta"
    return _f(row.get(leg))


def dir_aligned(direction, val):
    """A futures/CE-minus-PE signal aligned to the trade direction.
    CE wants fut buying / CE-favoured flow (positive); PE wants the negative."""
    return val if direction == "CE" else -val


def minutes_between(k0, k1):
    """List of 'YYYY-MM-DD HH:MM' keys from k0..k1 inclusive (same day)."""
    fmt = "%Y-%m-%d %H:%M"
    a, b = dt.datetime.strptime(k0, fmt), dt.datetime.strptime(k1, fmt)
    if b < a:
        return [k0]
    out, cur = [], a
    while cur <= b:
        out.append(cur.strftime(fmt))
        cur += dt.timedelta(minutes=1)
    return out


def pct(n, d):
    return f"{100.0*n/d:.0f}%" if d else "—"


def avg(xs):
    return sum(xs) / len(xs) if xs else 0.0


def bucket_report(title, rows, keyfn):
    """rows = list of (signal_value, pnl_pts, is_win). Split by keyfn(signal)->label."""
    groups = defaultdict(list)
    for sig, pnl, win in rows:
        groups[keyfn(sig)].append((pnl, win))
    print(f"\n  {title}")
    if not rows:
        print("    (no aligned trades yet)")
        return
    for label in sorted(groups):
        g = groups[label]
        wins = sum(1 for _, w in g if w)
        print(f"    {label:<22} n={len(g):<3} WR={pct(wins,len(g)):>4}  avg={avg([p for p,_ in g]):+.2f} pts")


def main():
    ticks = load_ticks()
    trades = load_trades()
    print("=" * 72)
    print("V11 TICK-IMBALANCE STUDY")
    print(f"  tick rows : {len(ticks)}   ({TICK_CSV})")
    print(f"  trades    : {len(trades)} ({TRADE_CSV})")
    print("=" * 72)

    if not ticks:
        print("\nNo tick data yet — run tick_delta_collector.py for ~2 weeks, then re-run.")
        return

    # only V11 trades that overlap the tick-capture date range
    tick_days = {k.split(" ")[0] for k in ticks}
    aligned = []
    for t in trades:
        d, et = t.get("date", ""), t.get("entry_time", "")
        if d not in tick_days or not et:
            continue
        ek = _minkey(d, et)
        if ek and ek in ticks:
            aligned.append((t, ek))

    print(f"\n  trades overlapping tick days: {len(aligned)}")
    if len(aligned) < 5:
        print("  insufficient overlap (<5) — harness OK, waiting on data (~26 Jun).")
        # still fall through so partial output is visible during early collection

    # ---------- (A) ENTRY-MINUTE IMBALANCE ----------
    print("\n" + "-" * 72)
    print("(A) ENTRY-MINUTE IMBALANCE  —  does flow at entry separate W/L?")
    print("-" * 72)
    own_rows, oppdir_rows, fut_rows, cum_rows = [], [], [], []
    for t, ek in aligned:
        direction = t.get("direction", "")
        pnl = _f(t.get("pnl_pts"))
        win = pnl > 0
        row = ticks[ek]
        own_rows.append((supportive_own(direction, row), pnl, win))
        oppdir_rows.append((dir_aligned(direction, _f(row.get("ce_minus_pe_delta"))), pnl, win))
        fut_rows.append((dir_aligned(direction, _f(row.get("fut_delta"))), pnl, win))
        cum_rows.append((dir_aligned(direction, _f(row.get("fut_cumdelta"))), pnl, win))

    bucket_report("own-leg delta (the leg we bought):", own_rows,
                  lambda s: "BOUGHT (delta>0)" if s > 0 else "SOLD (delta<=0)")
    bucket_report("CE-minus-PE delta, dir-aligned:", oppdir_rows,
                  lambda s: "flow WITH trade" if s > 0 else "flow AGAINST")
    bucket_report("futures delta, dir-aligned:", fut_rows,
                  lambda s: "fut WITH trade" if s > 0 else "fut AGAINST")
    bucket_report("futures cum-delta, dir-aligned:", cum_rows,
                  lambda s: "cum WITH trade" if s > 0 else "cum AGAINST")

    # ---------- (B) IN-TRADE IMBALANCE BEHAVIOR ----------
    print("\n" + "-" * 72)
    print("(B) IN-TRADE IMBALANCE  —  how flow behaves while position is open")
    print("-" * 72)
    support_frac_win, support_frac_loss = [], []
    flip_before_sl, no_flip_before_sl = 0, 0
    n_intrade = 0
    for t, ek in aligned:
        d = t.get("date", "")
        xt = t.get("exit_time", "")
        xk = _minkey(d, xt) if xt else None
        if not xk:
            continue
        keys = [k for k in minutes_between(ek, xk) if k in ticks]
        if len(keys) < 2:
            continue
        n_intrade += 1
        direction = t.get("direction", "")
        pnl = _f(t.get("pnl_pts"))
        win = pnl > 0
        deltas = [supportive_own(direction, ticks[k]) for k in keys]
        frac = sum(1 for x in deltas if x > 0) / len(deltas)
        (support_frac_win if win else support_frac_loss).append(frac)
        # did own-leg flow flip against us in the last 2 minutes before exit?
        reason = t.get("exit_reason", "")
        is_sl = reason in ("EMERGENCY_SL", "PROTECT_2")
        if is_sl:
            last2 = deltas[-2:]
            if any(x < 0 for x in last2):
                flip_before_sl += 1
            else:
                no_flip_before_sl += 1

    print(f"\n  trades with >=2 in-trade tick minutes: {n_intrade}")
    print(f"  supportive-flow fraction  WINNERS avg = {avg(support_frac_win)*100:.0f}%  (n={len(support_frac_win)})")
    print(f"  supportive-flow fraction  LOSERS  avg = {avg(support_frac_loss)*100:.0f}%  (n={len(support_frac_loss)})")
    tot_sl = flip_before_sl + no_flip_before_sl
    print(f"\n  SL/PROTECT exits: own-leg flow flipped AGAINST in last 2 min "
          f"= {flip_before_sl}/{tot_sl} ({pct(flip_before_sl, tot_sl)})")
    print("    (high % => order-flow LEADS the stop => candidate in-trade exit signal)")

    print("\n" + "=" * 72)
    print("READ: (A) tells you whether to ADD a tick-confirm at entry;")
    print("      (B) tells you whether tick-flip is an earlier exit than price SL.")
    print("      Both are CANDIDATES — shadow-log before gating (CE flow-veto burned us).")
    print("=" * 72)


if __name__ == "__main__":
    main()
