"""
smi_paper_flow.py
─────────────────
V12 FLOW-GATE applied to the stock F&O SMI engine, on the 15m timeframe.

Owner ask (2026-06-17): "apply V12 for stock F&O, build directly for 15 min."
V12 (`v12_vishal.py`) = a permissive SMI cross (E2) + a FLOW-GATE that vetoes
"hollow" moves. The stock SMI engine already has the cross; what it lacks is the
flow-gate. This engine ports exactly that.

Architecture — like the LOOSE sibling, it REUSES smi_paper.py wholesale (SMI math,
exits, fill model, the whole main() loop) and only swaps the entry decision:

  signal  = the LOOSE per-stock adaptive cross (smi_paper_loose.scan_entry_loose)
            — the closest analog to V12's permissive E2 (a strict frozen gate +
            veto would fire ~never, leaving nothing for the flow-gate to filter).
  +veto   = V12's flow-gate, computed on the stock's own 15m bars:
              · L1 effort-vs-result : below-median volume push (volx) + weak
                approach, OR a rejection wick against the trade (close_pos).
              · L2 A/D divergence   : price prints a new 20-bar extreme but the
                intraday accumulation/distribution line does NOT confirm.
            A cross that is hollow (L1 OR L2) is SKIPPED.

Thresholds self-calibrate from each stock's own 15m distribution (percentiles),
exactly as in v12_vishal.flow_veto — no fixed bands, no ADX.

Own files (never touches the frozen OR loose engine's data):
  state   : smi_paper_flow_state.json
  tracker : fno_tracker_flow.csv     (structure tag = SMI_FLOW)
  log     : smi_paper_flow_log.csv
Telegram alerts relabelled "SMI FLOW".

DATA-COLLECTION ONLY — not a validated edge. V12's own flow-gate is in-sample /
OOS-fragile (paper until ~30 Jun); this is the same idea ported to stocks to see
whether it filters the loose-engine noise. Judge alongside frozen/loose at ~06-25.

Usage:
  python3 smi_paper_flow.py          # one pass (process latest closed 15m bar)
  python3 smi_paper_flow.py --dry    # no writes, print decisions only
"""

import os
import numpy as np
import pandas as pd

import smi_paper as S          # reuse ALL helpers + main() orchestration
import smi_paper_loose as L    # reuse the loose adaptive signal (its import re-points
                               # S to loose files / loose gate — we override below)

# ── Redirect persistence to flow-only files (override loose's redirect) ────────
S.STATE_FILE = os.path.join(S.BASE_DIR, "smi_paper_flow_state.json")
S.TRACKER    = os.path.join(S.BASE_DIR, "fno_tracker_flow.csv")
S.TRADE_LOG  = os.path.join(S.BASE_DIR, "smi_paper_flow_log.csv")

# ── Flow-gate knobs (mirror v12_vishal) ────────────────────────────────────────
VOL_WIN   = 20      # volume MA window (bars) for volx — same as v12_vishal.VOL_WIN
VOLX_Q    = 0.45    # "quiet" volume = volx at/below this quantile of the window
APPR_Q    = 0.50    # ...and a weak 5-bar approach (prior volx mean) at/below this
CE_REJ    = 0.40    # CE rejection wick: close in bottom 40% of the bar range
PE_REJ    = 0.60    # PE rejection wick: close in top 40% of the bar range
DIV_WIN   = 20      # bars for the price/A-D divergence extremes


def add_flow_features(df: pd.DataFrame) -> pd.DataFrame:
    """Port of v12_vishal.add_flow_features for the stock 15m df (datetime index,
    OHLCV from kite). Intraday measures (approach, A/D, 20-bar extremes) group by
    calendar day so they reset each session. NaN-safe over the rolling warmup."""
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
    """(l1, l2, veto). True veto = hollow move that historically faded.
    Identical logic to v12_vishal.flow_veto; thresholds = this window's own
    percentiles, so each stock self-calibrates."""
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


def scan_entry_flow(sym: str, df: pd.DataFrame, fired: set, nifty_bear: bool):
    """Loose adaptive cross + V12 flow-gate. Same return contract as S.scan_entry."""
    e = L.scan_entry_loose(sym, df, fired, nifty_bear)
    if not e:
        return None
    if "volume" not in df.columns or len(df) < max(VOL_WIN, DIV_WIN) + 5:
        return e   # not enough data to judge flow — let the signal through
    fdf = add_flow_features(df)
    try:
        bar = fdf.loc[e["ts"]]
    except KeyError:
        return e
    if isinstance(bar, pd.DataFrame):   # dup-index guard
        bar = bar.iloc[-1]
    l1, l2, veto = flow_veto(fdf, bar, e["direction"])
    if veto:
        why = "+".join(n for n, f in (("L1noeffort", l1), ("L2_ADdiv", l2)) if f)
        print(f"  {sym}: FLOW-SKIP {e['direction']} [{why}] "
              f"volx={bar['volx']:.2f} close_pos={bar['close_pos']:.2f}")
        return None
    e = dict(e)
    e["conviction"] = "FLOW"
    e["detail"] = (e["detail"].replace("SMI LOOSE", "SMI FLOW")
                   + f" | FLOW✓ volx={bar['volx']:.2f} cp={bar['close_pos']:.2f}")
    return e


# ── Relabel Telegram + tracker tag so flow alerts/rows are unmistakable ────────
def _send_flow(msg: str):
    L._orig_send(msg.replace("SMI PAPER", "SMI FLOW").replace("SMI LOOSE", "SMI FLOW"))


def _upsert_flow(trade: dict, status: str, cur_prem: float):
    L._orig_upsert(trade, status, cur_prem)
    df = S.tracker_df()
    df.loc[df["option_symbol"] == trade["option_symbol"], "structure"] = "SMI_FLOW"
    S.tracker_write(df)


# ── Wire the flow pieces into the reused engine and run ────────────────────────
S.scan_entry = scan_entry_flow
S.send_telegram = _send_flow
S.tracker_upsert = _upsert_flow


if __name__ == "__main__":
    S.main()
