#!/usr/bin/env python3
"""V12 flow-veto daily auto-audit (standalone, read-only).

Every day after close, replays each FLOW-vetoed signal in ~/logs/v12_vishal.log
against the day's real 1-min option candles under V12's own option exit ladder
(SL entry-22, arm +8, trail peak-6, EOD 15:30) and appends the would-have-been
outcome to ~/lab_data/v12_veto_audit.csv.

Purpose: build the correct-vs-FAILED ledger for the flow-gate. A veto is
"correct" if the skipped trade would have lost, "FAILED" if it would have won
(the gate killed a winner — the real cost case we're hunting before the ~06-30
re-judge). Never touches the live engine / its state.

Usage:  v12_veto_audit.py [YYYY-MM-DD]   (default: today)
"""
import sys, os, re, csv, datetime as dt
import pandas as pd

# V12 option-leg exit constants (mirror v12_vishal.py — keep in sync)
OPT_SL, OPT_ARM, OPT_GAP, STRIKE_STEP = 22.0, 8.0, 6.0, 100
HOME = "/home/vishalraajput24"
LOG = f"{HOME}/logs/v12_vishal.log"
OPT_DIR = f"{HOME}/lab_data/options_1min"
SPOT_DIR = f"{HOME}/lab_data/spot"
OUT = f"{HOME}/lab_data/v12_veto_audit.csv"

SKIP_RE = re.compile(
    r"FLOW-SKIP\s+(?P<eng>\S+)\s+(?P<dir>CE|PE)\s+(?P<why>\S+)\s+@\s+"
    r"(?P<time>\d{2}:\d{2}:\d{2})\s+volx=(?P<volx>[\d.]+)\s+close_pos=(?P<cp>[\d.]+)")


def vetoes_for(day):
    """Unique FLOW-SKIP events logged during `day`'s run (dedup by time+dir)."""
    if not os.path.exists(LOG):
        return []
    out, seen, in_day = [], set(), False
    with open(LOG) as f:
        for ln in f:
            if f"start {day}" in ln:
                in_day = True            # enter today's run section
            elif "start " in ln and "[V12-VR]" in ln:
                in_day = False           # a later day's run started
            if not in_day:
                continue
            m = SKIP_RE.search(ln)
            if not m:
                continue
            key = (m["time"], m["dir"])
            if key in seen:
                continue                 # engine restart logs the same bar twice
            seen.add(key)
            out.append(m.groupdict())
    return out


def replay(day, sig_time, direction, opt, spot):
    t0 = pd.Timestamp(f"{day} {sig_time}")
    sp_rows = spot[spot["timestamp"] <= t0]
    if not len(sp_rows):
        return None
    sp = float(sp_rows.iloc[-1]["close"])
    if direction == "PE":               # PE ceils to 100 above spot -> ITM put
        strike = ((int(sp) + STRIKE_STEP - 1) // STRIKE_STEP) * STRIKE_STEP
    else:                               # CE floors to 100 below -> ITM call
        strike = (int(sp) // STRIKE_STEP) * STRIKE_STEP
    leg = opt[(opt["strike"] == strike) & (opt["type"] == direction) &
              (opt["timestamp"] >= t0)].sort_values("timestamp")
    if not len(leg):
        return dict(strike=strike, spot=sp, note="no_option_data")
    entry = float(leg.iloc[0]["close"])
    prem_sl = entry - OPT_SL
    peak = 0.0
    armed = False
    eod = pd.Timestamp(f"{day} 15:30:00")
    exit_px = exit_t = rsn = None
    for _, r in leg.iterrows():
        prem, ts = float(r["close"]), r["timestamp"]
        peak = max(peak, prem - entry)
        if not armed and peak >= OPT_ARM:
            armed = True
        if armed:
            prem_sl = max(prem_sl, entry + peak - OPT_GAP)
        if prem <= prem_sl:
            exit_px, exit_t, rsn = prem, ts, ("TRAIL" if armed else "SL")
            break
        if ts >= eod:
            exit_px, exit_t, rsn = prem, ts, "EOD"
            break
    if exit_px is None:
        last = leg.iloc[-1]
        exit_px, exit_t, rsn = float(last["close"]), last["timestamp"], "EOD/last"
    pnl = exit_px - entry
    return dict(strike=strike, spot=round(sp, 1), entry=round(entry, 1),
                peak=round(peak, 1), armed=armed, exit=round(exit_px, 1),
                exit_time=exit_t.strftime("%H:%M"), reason=rsn,
                pnl=round(pnl, 1),
                verdict=("CORRECT" if pnl < 0 else "FAILED"))


def load_done():
    if not os.path.exists(OUT):
        return set()
    with open(OUT) as f:
        return {(r["date"], r["sig_time"], r["direction"])
                for r in csv.DictReader(f)}


COLS = ["date", "sig_time", "engine", "direction", "why", "volx", "close_pos",
        "strike", "spot", "entry_prem", "peak_pts", "armed", "exit_prem",
        "exit_time", "exit_reason", "pnl_pts", "verdict"]


def main():
    day = sys.argv[1] if len(sys.argv) > 1 else dt.date.today().isoformat()
    ymd = day.replace("-", "")
    opt_path = f"{OPT_DIR}/nifty_option_1min_{ymd}.csv"
    spot_path = f"{SPOT_DIR}/nifty_spot_1min_{ymd}.csv"
    vetoes = vetoes_for(day)
    print(f"[v12_veto_audit] {day}: {len(vetoes)} veto(s) in log")
    if not vetoes:
        return
    if not (os.path.exists(opt_path) and os.path.exists(spot_path)):
        print(f"  data not ready ({opt_path} / {spot_path}) — skip, retry next run")
        return
    opt = pd.read_csv(opt_path); opt["timestamp"] = pd.to_datetime(opt["timestamp"])
    spot = pd.read_csv(spot_path); spot["timestamp"] = pd.to_datetime(spot["timestamp"])

    done = load_done()
    new_first = not os.path.exists(OUT)
    rows = []
    for v in vetoes:
        if (day, v["time"], v["dir"]) in done:
            continue
        r = replay(day, v["time"], v["dir"], opt, spot)
        if r is None or r.get("note") == "no_option_data":
            print(f"  {v['dir']} @ {v['time']}: no option data, skipped")
            continue
        rows.append({
            "date": day, "sig_time": v["time"], "engine": v["eng"],
            "direction": v["dir"], "why": v["why"], "volx": v["volx"],
            "close_pos": v["cp"], "strike": r["strike"], "spot": r["spot"],
            "entry_prem": r["entry"], "peak_pts": r["peak"], "armed": r["armed"],
            "exit_prem": r["exit"], "exit_time": r["exit_time"],
            "exit_reason": r["reason"], "pnl_pts": r["pnl"], "verdict": r["verdict"]})
        mark = "✅saved-a-loser" if r["verdict"] == "CORRECT" else "❌KILLED A WINNER"
        print(f"  {v['dir']} {r['strike']} @ {v['time']}  entry {r['entry']} "
              f"peak +{r['peak']}  exit {r['exit']} ({r['reason']})  "
              f"PnL {r['pnl']:+}  -> {r['verdict']} {mark}")
    if not rows:
        print("  nothing new to append (already audited)")
        return
    with open(OUT, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=COLS)
        if new_first:
            w.writeheader()
        w.writerows(rows)
    correct = sum(1 for r in rows if r["verdict"] == "CORRECT")
    failed = len(rows) - correct
    saved = sum(r["pnl_pts"] for r in rows)
    print(f"  appended {len(rows)} row(s): {correct} correct / {failed} FAILED, "
          f"net {-saved:+.1f} pts saved by gate")


if __name__ == "__main__":
    main()
