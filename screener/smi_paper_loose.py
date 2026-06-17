"""
smi_paper_loose.py
──────────────────
LOOSE / high-frequency sibling of smi_paper.py — for DATA VISIBILITY only.

Owner ask (2026-06-16): the frozen SMI gate is so selective it can go several
sessions with zero trades ("we are fully blind"). This engine runs ALONGSIDE the
frozen one (which is untouched — the 2-week validation baseline stays clean) and
deliberately loosens the entry gate so we get a steady ~5-6 paper trades/day to
watch and learn from.

What's identical to smi_paper.py (imported & reused — zero divergence):
  · SMI math (RMA 14/3/3 on 15m + 1h), data fetch, ATM-option pick, LTP fills
  · Exit ladder (1% SL · trail arms +1.5% → close vs SMA8 · 15:15 force close)
  · State / tracker / log plumbing, Telegram alerts, the whole main() loop

What's DIFFERENT — the entry gate only (`scan_entry_loose`), made "flexible per
stock" instead of one global threshold:
  · Oversold/overbought are each stock's OWN SMI percentiles over the lookback
    window (p20 / p80) — not the global −40 / +45. A calm stock and a wild stock
    each fire on their own scale.
  · 1h filter relaxed to DIRECTION ONLY (CE: 1h SMI > signal · PE: 1h SMI <
    signal) — the global engine's +5/−5 margin + (0,50) zone killed ~99% of
    crosses. PE still requires close < day VWAP (cheap conviction, kept).
  Backtest (40-day cache): ~5.7 trades/day, balanced CE/PE.

Own files (never touches the frozen engine's data):
  state   : smi_paper_loose_state.json
  tracker : fno_tracker_loose.csv   (structure tag = SMI_LOOSE)
  log     : smi_paper_loose_log.csv
Telegram alerts are relabelled "SMI LOOSE" so they're unmistakable.

Usage:
  python3 smi_paper_loose.py          # one pass (process latest closed bar)
  python3 smi_paper_loose.py --dry    # no writes, print decisions only
"""

import os
import numpy as np
import pandas as pd

import smi_paper as S   # reuse ALL helpers + main() orchestration

# ── Redirect persistence to loose-only files (frozen engine untouched) ─────────
S.STATE_FILE = os.path.join(S.BASE_DIR, "smi_paper_loose_state.json")
S.TRACKER    = os.path.join(S.BASE_DIR, "fno_tracker_loose.csv")
S.TRADE_LOG  = os.path.join(S.BASE_DIR, "smi_paper_loose_log.csv")

# ── Loose gate knobs (per-stock adaptive) ─────────────────────────────────────
OS_PCTILE = 20.0   # CE: cross up through the stock's own p20 SMI (its oversold)
OB_PCTILE = 80.0   # PE: cross down through the stock's own p80 SMI (its overbought)
PE_1H_MAX = 40.0   # PE: also require 1h SMI < 40 — don't short a stock whose 1h
                   # momentum is still in bull territory. Added 2026-06-17 after the
                   # loose engine mass-fired 11 counter-trend PEs into a rising market
                   # (1h SMI pinned 60–88, passing the old direction-only filter by a
                   # razor margin). 40-day cache study (smi_loose_optimize.py): PE exp
                   # −0.043%→−0.006%/trade, overall +0.062%→+0.078%, ~5/day kept, CE
                   # (the actual edge, +0.100%) left untouched.


def scan_entry_loose(sym: str, df: pd.DataFrame, fired: set):
    """Per-stock adaptive entry. Same return contract as S.scan_entry, so it can
    be monkeypatched straight into S.main()."""
    if len(df) < S.SMI_LENGTH * 4:
        return None
    smi, sig = S.compute_smi(df)
    sv, gv = smi.values, sig.values
    last = len(df) - 1
    ts = df.index[last]
    tstr = ts.strftime("%H:%M")
    if not (S.ENTRY_START <= tstr <= S.ENTRY_END):
        return None
    if np.isnan(sv[last]) or np.isnan(sv[last - 1]):
        return None

    # per-stock thresholds from THIS stock's SMI distribution over the window
    os_thr = float(np.nanpercentile(sv, OS_PCTILE))
    ob_thr = float(np.nanpercentile(sv, OB_PCTILE))

    smi1h, sig1h = S.smi_1h_at(df, ts)
    if np.isnan(smi1h) or np.isnan(sig1h):
        return None

    # CE — cross up through the stock's own oversold percentile, 1h trend up
    if (sv[last - 1] <= os_thr and sv[last] > os_thr and sv[last] > gv[last]
            and smi1h > sig1h):
        key = f"{sym}:CE:{ts.isoformat()}"
        if key not in fired:
            return {"direction": "CE", "ts": ts, "key": key, "conviction": "LOOSE",
                    "confirm_bars": 0,
                    "detail": (f"SMI LOOSE | CE cross p{OS_PCTILE:.0f}={os_thr:+.1f} "
                               f"| smi15={sv[last]:.1f} | 1h={smi1h:.1f}/sig{sig1h:.1f}")}

    # PE — cross down through the stock's own overbought percentile within the
    # lookahead window; last bar is the FIRST confirming bar (1h down + below VWAP)
    for back in range(0, S.PE_LOOKAHEAD + 1):
        ci = last - back
        if ci < 1 or df.index[ci].normalize() != ts.normalize():
            break
        if not (sv[ci - 1] >= ob_thr and sv[ci] < ob_thr):
            continue

        def confirms(j):
            s1h, g1h = S.smi_1h_at(df, df.index[j])
            if np.isnan(s1h) or np.isnan(g1h):
                return False
            if not (s1h < g1h and s1h < PE_1H_MAX):   # 1h bearish AND not in bull zone
                return False
            if not (sv[j] < gv[j] and sv[j] < ob_thr):
                return False
            vw = S.day_vwap_at(df, df.index[j])
            return not np.isnan(vw) and df["close"].iloc[j] < vw

        if any(confirms(j) for j in range(ci, last)):
            break   # an earlier bar already confirmed — that signal is spent
        if confirms(last):
            key = f"{sym}:PE:{df.index[ci].isoformat()}"
            if key not in fired:
                return {"direction": "PE", "ts": ts, "key": key, "conviction": "LOOSE",
                        "confirm_bars": back,
                        "detail": (f"SMI LOOSE | PE cross p{OB_PCTILE:.0f}={ob_thr:+.1f} "
                                   f"(bar -{back}) | smi15={sv[last]:.1f} "
                                   f"| 1h={smi1h:.1f}/sig{sig1h:.1f} | below VWAP")}
        break
    return None


# ── Relabel Telegram + tracker tag so loose alerts/rows are unmistakable ───────
_orig_send = S.send_telegram


def _send_loose(msg: str):
    _orig_send(msg.replace("SMI PAPER", "SMI LOOSE"))


_orig_upsert = S.tracker_upsert


def _upsert_loose(trade: dict, status: str, cur_prem: float):
    # reuse the frozen upsert but flip the structure tag on the row it writes
    _orig_upsert(trade, status, cur_prem)
    df = S.tracker_df()
    df.loc[df["option_symbol"] == trade["option_symbol"], "structure"] = "SMI_LOOSE"
    S.tracker_write(df)


# ── Wire the loose pieces into the reused engine and run ───────────────────────
S.scan_entry = scan_entry_loose
S.send_telegram = _send_loose
S.tracker_upsert = _upsert_loose


if __name__ == "__main__":
    S.main()
