"""
smi_focus35.py
──────────────
FOCUSED 35-stock paper engine — one code, each stock running its OWN tuned V12 gate
(from the 06-17/06-18 per-stock tuning, v12_one_stock_tune / v12_batch_tune /
v12_focus_tune). Started as 9 keepers (hence the old "focus9" name) and grew to 35
stocks (9 original + 6 liquid + 2 batch-2 + 18 deep-tuned batch-3, all 06-18). Purpose:
forward-monitor these stocks for ~1 week to confirm the tuned edges are real and we're
on the right track, BEFORE trusting any of them.

Each stock's gate (E2 SMI cross + optional flow-gate) uses its own SMI period / bands /
direction / flow on-off. Everything else (1% SL · trail +1.5% then close-vs-SMA8 ·
15:15 EOD · 1 lot nearest-expiry ATM stock option) is the SHARED smi_paper engine —
identical exits/fill to frozen/loose/flow, so results are comparable.

Reuses smi_paper (exits/fill/main) + smi_paper_flow (flow-gate) + orion (SMI). Only the
35 FOCUS symbols ever fire; every other stock returns None. Own files (never touches
frozen/loose/flow data):
  state   : smi_focus35_state.json
  tracker : fno_tracker_focus.csv   (structure tag = SMI_FOCUS)
  log     : smi_focus35_log.csv
Telegram alerts relabelled "SMI FOCUS35".

IN-SAMPLE TUNED — this is the forward-validation run, NOT a proven edge yet. Judge the
week's results against the in-sample win-rates below.

  python3 smi_focus35.py          # one pass (latest closed 15m bar)
  python3 smi_focus35.py --dry    # no writes, print decisions only
"""

import os
import numpy as np
import pandas as pd

import smi_paper as S
import smi_paper_flow as F
import orion_v2514_backtest as OB

_orig_send = S.send_telegram
_orig_upsert = S.tracker_upsert

S.STATE_FILE = os.path.join(S.BASE_DIR, "smi_focus35_state.json")
S.TRACKER    = os.path.join(S.BASE_DIR, "fno_tracker_focus.csv")
S.TRADE_LOG  = os.path.join(S.BASE_DIR, "smi_focus35_log.csv")

# ── Per-stock tuned gate (06-17). k=SMI period · ob/os=cross bands ·
#    dirs=allowed sides · flow=apply V12 flow-gate. SL/trail = shared (1% / +1.5%).
#    In-sample win-rates kept for reference. ─────────────────────────────────────
FOCUS = {
    "MARUTI":     dict(k=21, ob=45, os=-45, dirs=("CE", "PE"), flow=True,  wr=86.7),
    "LT":         dict(k=30, ob=50, os=-50, dirs=("CE", "PE"), flow=True,  wr=86.7),
    "TITAN":      dict(k=21, ob=50, os=-50, dirs=("CE",),      flow=True,  wr=83.3),
    "INFY":       dict(k=30, ob=35, os=-35, dirs=("PE",),      flow=False, wr=83.3),
    "BHARTIARTL": dict(k=40, ob=40, os=-40, dirs=("CE", "PE"), flow=True,  wr=83.3),
    "RELIANCE":   dict(k=30, ob=50, os=-50, dirs=("CE", "PE"), flow=True,  wr=78.6),
    "BAJFINANCE": dict(k=30, ob=50, os=-50, dirs=("CE", "PE"), flow=True,  wr=78.6),
    "TCS":        dict(k=21, ob=35, os=-35, dirs=("CE", "PE"), flow=False, wr=72.7),
    "SBIN":       dict(k=30, ob=45, os=-45, dirs=("CE", "PE"), flow=True,  wr=71.4),
    # ── 06-18 liquid expansion (batch-tuned, win>=70% & avg>0, in-sample 40d) ──
    "KOTAKBANK":  dict(k=40, ob=45, os=-45, dirs=("CE", "PE"), flow=True,  wr=92.9),
    "ASIANPAINT": dict(k=21, ob=35, os=-35, dirs=("PE",),      flow=False, wr=81.2),
    "HINDUNILVR": dict(k=30, ob=50, os=-50, dirs=("PE",),      flow=False, wr=78.6),
    "M&M":        dict(k=30, ob=40, os=-40, dirs=("PE",),      flow=False, wr=75.0),
    "ADANIPORTS": dict(k=30, ob=40, os=-40, dirs=("CE", "PE"), flow=True,  wr=75.0),
    "HDFCLIFE":   dict(k=30, ob=40, os=-40, dirs=("CE", "PE"), flow=True,  wr=71.4),
    # ── 06-18 batch-2 (liquid large-caps, same win>=70% & avg>0 in-sample 40d) ──
    "HEROMOTOCO": dict(k=30, ob=35, os=-35, dirs=("PE",),      flow=False, wr=83.3),
    "NESTLEIND":  dict(k=40, ob=35, os=-35, dirs=("PE",),      flow=False, wr=73.3),
    # ── 06-18 batch-3 (liquid subset), DEEP-TUNED via v12_focus_tune.py: also sweeps
    #    SMI smoothing (d/sig), SL% and trail% — obj = max avg%/trade s.t. n>=12 & win>=70%.
    #    OVERFIT BY DESIGN (best-of-2430-configs/stock, in-sample 40d). wr = in-sample win%. ──
    "MPHASIS":    dict(k=21, ob=50, os=-50, dirs=("CE", "PE"), flow=True,  trail=1.0, wr=92.3),
    "TATACONSUM": dict(k=21, ob=40, os=-40, dirs=("PE",),      flow=True,  sl=0.8, trail=2.0, wr=91.7),
    "ZYDUSLIFE":  dict(k=21, ob=50, os=-50, dirs=("CE", "PE"), flow=True,  sl=0.8, wr=80.0),
    "APOLLOHOSP": dict(k=30, ob=40, os=-40, dirs=("CE", "PE"), flow=True,  sl=0.8, wr=75.0),
    "DMART":      dict(k=21, ob=40, os=-40, dirs=("PE",),      flow=False, sl=0.8, wr=84.6),
    "DLF":        dict(k=21, ob=55, os=-55, dirs=("CE", "PE"), flow=True,  d=5, sig=5, sl=1.2, trail=2.0, wr=75.0),
    "JIOFIN":     dict(k=30, ob=40, os=-40, dirs=("CE",),      flow=False, d=5, sig=5, sl=1.2, trail=1.0, wr=76.9),
    "LUPIN":      dict(k=30, ob=35, os=-35, dirs=("CE", "PE"), flow=True,  sl=1.2, trail=1.0, wr=85.7),
    "AUBANK":     dict(k=30, ob=35, os=-35, dirs=("PE",),      flow=False, sl=0.8, trail=1.0, wr=85.7),
    "LICHSGFIN":  dict(k=30, ob=55, os=-55, dirs=("CE", "PE"), flow=True,  d=5, sig=3, sl=0.8, wr=76.9),
    "INDUSINDBK": dict(k=40, ob=50, os=-50, dirs=("CE", "PE"), flow=True,  d=5, sig=3, wr=83.3),
    "TRENT":      dict(k=21, ob=45, os=-45, dirs=("PE",),      flow=False, trail=2.0, wr=78.6),
    "DIVISLAB":   dict(k=21, ob=50, os=-50, dirs=("PE",),      flow=False, d=5, sig=5, sl=0.8, wr=91.7),
    "BPCL":       dict(k=30, ob=45, os=-45, dirs=("CE",),      flow=False, sl=1.2, trail=2.0, wr=75.0),
    "COALINDIA":  dict(k=21, ob=55, os=-55, dirs=("CE", "PE"), flow=True,  d=5, sig=3, sl=0.8, trail=2.0, wr=78.6),
    "SIEMENS":    dict(k=21, ob=55, os=-55, dirs=("CE", "PE"), flow=True,  sl=1.2, trail=2.0, wr=76.9),
    "CHOLAFIN":   dict(k=21, ob=45, os=-45, dirs=("CE", "PE"), flow=True,  d=5, sig=3, trail=1.0, wr=76.9),
    "MCX":        dict(k=40, ob=55, os=-55, dirs=("CE", "PE"), flow=True,  sl=1.2, trail=1.0, wr=75.0),
}
SMI_D, SMI_SIG = 3, 3


def scan_entry_focus(sym, df, fired):
    """Per-stock tuned E2 SMI cross + optional flow-gate. Only FOCUS stocks fire."""
    c = FOCUS.get(sym)
    if c is None or len(df) < c["k"] + 10:
        return None
    last = len(df) - 1
    ts = df.index[last]
    if not (S.ENTRY_START <= ts.strftime("%H:%M") <= S.ENTRY_END):
        return None
    smi_v, sig_v = OB.smi(df, k=c["k"], d=c.get("d", SMI_D), sig=c.get("sig", SMI_SIG))
    sv, gv = smi_v.values, sig_v.values
    if np.isnan(sv[last]) or np.isnan(sv[last - 1]) or np.isnan(gv[last]):
        return None
    p, cur, cs = sv[last - 1], sv[last], gv[last]
    cross_up = p <= c["os"] and cur > c["os"] and cur > cs   # CE
    cross_dn = p >= c["ob"] and cur < c["ob"] and cur < cs   # PE
    direction = "CE" if cross_up else ("PE" if cross_dn else None)
    if direction is None or direction not in c["dirs"]:
        return None
    key = f"{sym}:{direction}:{ts.isoformat()}"
    if key in fired:
        return None

    flow_note = ""
    if c["flow"]:
        if "volume" not in df.columns or len(df) < F.VOL_WIN + 25:
            flow_note = " | FLOW✓(short)"
        else:
            fdf = F.add_flow_features(df)
            bar = fdf.loc[ts]
            if isinstance(bar, pd.DataFrame):
                bar = bar.iloc[-1]
            l1, l2, veto = F.flow_veto(fdf, bar, direction)
            if veto:
                why = "+".join(n for n, fl in (("L1", l1), ("L2", l2)) if fl)
                print(f"  {sym}: FOCUS FLOW-SKIP {direction} [{why}]")
                return None
            flow_note = f" | FLOW✓ volx={bar['volx']:.2f}"

    return {"direction": direction, "ts": ts, "key": key,
            "conviction": f"FOCUS(wr{c['wr']:.0f})", "confirm_bars": 0,
            "sl_pct": c.get("sl", S.SL_PCT), "trail_arm": c.get("trail", S.TRAIL_ARM),
            "detail": (f"SMI FOCUS35 {sym} | E2 k{c['k']} ±{c['ob']} {direction} "
                       f"| smi={cur:.1f}/sig{cs:.1f} | sl{c.get('sl', S.SL_PCT):g}/"
                       f"tr{c.get('trail', S.TRAIL_ARM):g}{flow_note}")}


def _send_focus(msg):
    _orig_send(msg.replace("SMI PAPER", "SMI FOCUS35"))


def _upsert_focus(trade, status, cur_prem):
    _orig_upsert(trade, status, cur_prem)
    df = S.tracker_df()
    df.loc[df["option_symbol"] == trade["option_symbol"], "structure"] = "SMI_FOCUS"
    S.tracker_write(df)


S.scan_entry = scan_entry_focus
S.send_telegram = _send_focus
S.tracker_upsert = _upsert_focus


if __name__ == "__main__":
    S.main()
