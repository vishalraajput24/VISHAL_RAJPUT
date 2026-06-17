"""
smi_paper_flow.py
─────────────────
V12 applied to stock F&O on the 15m timeframe — a LITERAL mirror of v12_vishal's
converged gate (owner 2026-06-17: "keep it simple as V12, all filters right").

V12 = E2 (SMI cross) + FLOW-GATE. Nothing else (E1 is confirm-only, E3 removed).
This engine reproduces exactly that on each stock's 15m bars:

  SIGNAL — E2 SMI cross, identical to v12_vishal / orion_v2514_backtest.gen_signals:
    SMI = orion smi(k=30, d=3, sig=3)  ·  bands ±35  ·  side-of-signal
      CE : prev_smi ≤ −35  &  smi > −35  &  smi > signal   (cross up out of OS)
      PE : prev_smi ≥ +35  &  smi < +35  &  smi < signal   (cross down out of OB)
    Same-bar. NO per-stock percentiles, NO 1h filter, NO VWAP — V12 has none.

  + FLOW-GATE veto (v12_vishal.flow_veto, computed on the stock's own 15m bars):
      L1 effort-vs-result : below-median volume push (volx) + weak 5-bar approach,
                            OR a rejection wick (CE close_pos≤0.40 / PE≥0.60).
      L2 A/D divergence   : new 20-bar price extreme the intraday A/D doesn't confirm.
    Thresholds self-calibrate from the window's own percentiles.

Reuses smi_paper.py for exits / fill / main(); orion_v2514_backtest for the SMI.
Own files (never touches frozen / loose data):
  state   : smi_paper_flow_state.json
  tracker : fno_tracker_flow.csv   (structure tag = SMI_FLOW)
  log     : smi_paper_flow_log.csv
Telegram alerts relabelled "SMI FLOW".

DATA-COLLECTION ONLY — V12's gate is in-sample / OOS-fragile (paper until ~30 Jun).
Judge alongside frozen (SMI) + loose (SMI_LOOSE) at ~06-25.

Usage:
  python3 smi_paper_flow.py          # one pass (process latest closed 15m bar)
  python3 smi_paper_flow.py --dry    # no writes, print decisions only
"""

import os
import numpy as np
import pandas as pd

import smi_paper as S                 # reuse exits / fill / main() orchestration
import orion_v2514_backtest as OB     # reuse V12's exact SMI (smi k=30/d=3/sig=3)

# capture the FROZEN telegram/tracker fns before we re-point them
_orig_send = S.send_telegram
_orig_upsert = S.tracker_upsert

# ── Redirect persistence to flow-only files ────────────────────────────────────
S.STATE_FILE = os.path.join(S.BASE_DIR, "smi_paper_flow_state.json")
S.TRACKER    = os.path.join(S.BASE_DIR, "fno_tracker_flow.csv")
S.TRADE_LOG  = os.path.join(S.BASE_DIR, "smi_paper_flow_log.csv")

# ── V12 E2 gate (literal) ──────────────────────────────────────────────────────
SMI_K, SMI_D, SMI_SIG = 30, 3, 3      # v12_vishal SMI_PERIOD=30, orion d/sig defaults
E2_OB, E2_OS = 35.0, -35.0            # v12_vishal E2_OB / E2_OS

# ── V12 flow-gate knobs (mirror v12_vishal) ────────────────────────────────────
VOL_WIN   = 20      # volume MA window for volx
VOLX_Q    = 0.45    # "quiet" volume = volx at/below this quantile of the window
APPR_Q    = 0.50    # ...and a weak 5-bar approach at/below this
CE_REJ    = 0.40    # CE rejection wick: close in bottom 40% of bar range
PE_REJ    = 0.60    # PE rejection wick: close in top 40% of bar range
DIV_WIN   = 20      # bars for the price/A-D divergence extremes


def add_flow_features(df: pd.DataFrame) -> pd.DataFrame:
    """Port of v12_vishal.add_flow_features for the stock 15m df (datetime index).
    Intraday measures group by calendar day so they reset each session."""
    df = df.copy()
    df["d"] = df.index.normalize()
    df["vol_ma"] = df["volume"].rolling(VOL_WIN).mean()
    df["volx"] = df["volume"] / df["vol_ma"]
    g = df.groupby("d", group_keys=False)
    df["approach_volx"] = g["volx"].transform(lambda s: s.shift(1).rolling(5).mean())
    rng = (df["high"] - df["low"]).replace(0, np.nan)
    df["close_pos"] = (df["close"] - df["low"]) / rng
    mfm = (((df["close"] - df["low"]) - (df["high"] - df["close"])) / rng).fillna(0)
    df["ad"] = (mfm * df["volume"]).groupby(df["d"]).cumsum()
    df["px_hi20"] = g["close"].transform(lambda s: s.rolling(DIV_WIN).max())
    df["px_lo20"] = g["close"].transform(lambda s: s.rolling(DIV_WIN).min())
    df["ad_hi20"] = g["ad"].transform(lambda s: s.rolling(DIV_WIN).max())
    df["ad_lo20"] = g["ad"].transform(lambda s: s.rolling(DIV_WIN).min())
    return df


def flow_veto(df: pd.DataFrame, bar: pd.Series, direction: str):
    """(l1, l2, veto). Identical logic to v12_vishal.flow_veto; thresholds = this
    window's own percentiles, so each stock self-calibrates."""
    volx_lo = df["volx"].quantile(VOLX_Q)
    appr_lo = df["approach_volx"].quantile(APPR_Q)
    quiet = (bar["volx"] <= volx_lo) and \
            (not pd.isna(bar["approach_volx"]) and bar["approach_volx"] <= appr_lo)
    rej = (bar["close_pos"] <= CE_REJ) if direction == "CE" else (bar["close_pos"] >= PE_REJ)
    l1 = bool(quiet or rej)
    if direction == "CE":
        l2 = bool(bar["close"] >= bar["px_hi20"] and bar["ad"] < bar["ad_hi20"])
    else:
        l2 = bool(bar["close"] <= bar["px_lo20"] and bar["ad"] > bar["ad_lo20"])
    return l1, l2, bool(l1 or l2)


def scan_entry_flow(sym: str, df: pd.DataFrame, fired: set):
    """V12 E2 SMI cross + flow-gate. Same return contract as S.scan_entry."""
    if len(df) < SMI_K + 10:
        return None
    last = len(df) - 1
    ts = df.index[last]
    if not (S.ENTRY_START <= ts.strftime("%H:%M") <= S.ENTRY_END):
        return None

    # E2 SMI cross — identical to orion gen_signals (smi k=30, bands ±35)
    smi_v, sig_v = OB.smi(df, k=SMI_K, d=SMI_D, sig=SMI_SIG)
    sv, gv = smi_v.values, sig_v.values
    if np.isnan(sv[last]) or np.isnan(sv[last - 1]) or np.isnan(gv[last]):
        return None
    prev, cur, csig = sv[last - 1], sv[last], gv[last]
    cross_up = prev <= E2_OS and cur > E2_OS and cur > csig   # CE
    cross_dn = prev >= E2_OB and cur < E2_OB and cur < csig   # PE
    direction = "CE" if cross_up else ("PE" if cross_dn else None)
    if direction is None:
        return None
    key = f"{sym}:{direction}:{ts.isoformat()}"
    if key in fired:
        return None

    # FLOW-GATE veto
    if "volume" in df.columns and len(df) >= max(VOL_WIN, DIV_WIN) + 5:
        fdf = add_flow_features(df)
        bar = fdf.loc[ts]
        if isinstance(bar, pd.DataFrame):
            bar = bar.iloc[-1]
        l1, l2, veto = flow_veto(fdf, bar, direction)
        if veto:
            why = "+".join(n for n, f in (("L1noeffort", l1), ("L2_ADdiv", l2)) if f)
            print(f"  {sym}: FLOW-SKIP {direction} [{why}] "
                  f"volx={bar['volx']:.2f} close_pos={bar['close_pos']:.2f}")
            return None
        flow_note = f" | FLOW✓ volx={bar['volx']:.2f} cp={bar['close_pos']:.2f}"
    else:
        flow_note = " | FLOW✓ (insufficient bars)"

    return {"direction": direction, "ts": ts, "key": key, "conviction": "FLOW",
            "confirm_bars": 0,
            "detail": (f"SMI FLOW | E2 {direction} cross {'+' if direction=='PE' else ''}"
                       f"{E2_OB if direction=='PE' else E2_OS:.0f} | smi={cur:.1f}/sig{csig:.1f}"
                       + flow_note)}


# ── Relabel Telegram + tracker tag so flow alerts/rows are unmistakable ────────
def _send_flow(msg: str):
    _orig_send(msg.replace("SMI PAPER", "SMI FLOW"))


def _upsert_flow(trade: dict, status: str, cur_prem: float):
    _orig_upsert(trade, status, cur_prem)
    df = S.tracker_df()
    df.loc[df["option_symbol"] == trade["option_symbol"], "structure"] = "SMI_FLOW"
    S.tracker_write(df)


# ── Wire the flow pieces into the reused engine and run ────────────────────────
S.scan_entry = scan_entry_flow
S.send_telegram = _send_flow
S.tracker_upsert = _upsert_flow


if __name__ == "__main__":
    S.main()
