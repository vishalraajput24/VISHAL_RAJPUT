#!/usr/bin/env python3
"""
smi_loose_optimize.py  — READ-ONLY study (no live state touched)
================================================================
Optimise the LOOSE SMI gate (smi_paper_loose.scan_entry_loose). Motivation:
on 2026-06-17 the loose engine mass-fired 11 PEs into a rising market — every
one counter-trend — because its 1h filter is DIRECTION-ONLY (PE needs only
smi1h < sig1h), so a stock with 1h SMI pinned at ~88 (strongly bullish) still
fires a PE when sig is a razor 0.1 above it.

Replays the exact loose gate + the engine's real exit (1% SL · arm +1.5% →
close-vs-SMA8 · 15:15 force) over the 40-day 15m cache (smi_ohlcv_15m.pkl,
2026-04-15→06-11) and compares candidate 1h sanity filters. Reuses smi_paper's
own compute_smi for zero math divergence.
"""
import os, pickle, numpy as np, pandas as pd
import smi_paper as S

PKL = os.path.join(S.BASE_DIR, "smi_ohlcv_15m.pkl")
OS_PCT, OB_PCT = 20.0, 80.0
PCT_LOOKBACK = 600           # ~24 trading days of 15m bars for the per-stock percentile
ENTRY_START, ENTRY_END, LAST_BAR = "09:30", "14:30", "15:15"

def prep(df):
    """Per-symbol precompute: 15m smi/sig, 1h smi/sig aligned by known_at, sma8."""
    df = df.sort_index().copy()
    smi, sig = S.compute_smi(df)
    df["smi"], df["sig"] = smi, sig
    df["sma8"] = df["close"].rolling(8).mean()
    h = S.resample_1h(df)
    hsmi, hsig = S.compute_smi(h)
    h1 = pd.DataFrame({"known_at": h["known_at"].values,
                       "smi1h": hsmi.values, "sig1h": hsig.values}).dropna()
    # for each 15m bar, last 1h reading known at/before it
    merged = pd.merge_asof(pd.DataFrame({"ts": df.index}).sort_values("ts"),
                           h1.sort_values("known_at"),
                           left_on="ts", right_on="known_at", direction="backward")
    df["smi1h"] = merged["smi1h"].values
    df["sig1h"] = merged["sig1h"].values
    return df

def day_vwap_series(df):
    tp = (df["high"] + df["low"] + df["close"]) / 3.0
    g = df.groupby(df.index.normalize())
    cum_tpv = (tp * df["volume"]).groupby(df.index.normalize()).cumsum()
    cum_v = df["volume"].groupby(df.index.normalize()).cumsum().replace(0, np.nan)
    return cum_tpv / cum_v

def sim_exit(day_df, entry_i, direction, entry_px):
    """Replicate smi_paper.check_exit within the entry day. Returns pnl_pct."""
    sgn = 1 if direction == "CE" else -1
    sl_price = entry_px * (1 - S.SL_PCT/100) if sgn == 1 else entry_px * (1 + S.SL_PCT/100)
    peak, armed = 0.0, False
    rows = day_df.iloc[entry_i+1:]
    for ts, row in rows.iterrows():
        hi, lo, cl, s8 = row["high"], row["low"], row["close"], row["sma8"]
        adverse = lo if sgn == 1 else hi
        if (adverse - sl_price) * sgn <= 0:
            return (sl_price - entry_px) * sgn / entry_px * 100, "SL"
        pnl = (cl - entry_px) * sgn / entry_px * 100
        peak = max(peak, pnl)
        if peak >= S.TRAIL_ARM:
            armed = True
        if armed and not np.isnan(s8) and (cl - s8) * sgn < 0:
            return pnl, "TRAIL"
        if ts.strftime("%H:%M") >= LAST_BAR:
            return pnl, "EOD"
    return (rows.iloc[-1]["close"] - entry_px) * sgn / entry_px * 100, "EOD" if len(rows) else ("flat", 0)

def passes_1h(direction, smi1h, sig1h, variant):
    if np.isnan(smi1h) or np.isnan(sig1h):
        return False
    if variant == "BASE":                       # current loose: direction only
        return smi1h > sig1h if direction == "CE" else smi1h < sig1h
    if variant == "ZONE50":                      # don't short >50 bull / don't buy <-50 bear
        base = smi1h > sig1h if direction == "CE" else smi1h < sig1h
        zone = smi1h > -50 if direction == "CE" else smi1h < 50
        return base and zone
    if variant == "ZONE40":
        base = smi1h > sig1h if direction == "CE" else smi1h < sig1h
        zone = smi1h > -40 if direction == "CE" else smi1h < 40
        return base and zone
    if variant == "MARGIN5":                     # real 1h cross, not razor-thin
        return (smi1h > sig1h + 5) if direction == "CE" else (smi1h < sig1h - 5)
    if variant == "ZONE50+MARGIN5":
        base = (smi1h > sig1h + 5) if direction == "CE" else (smi1h < sig1h - 5)
        zone = smi1h > -50 if direction == "CE" else smi1h < 50
        return base and zone
    # ── asymmetric: CE unchanged (its edge is fine), zone-cap PE only ──
    if variant == "CE_base|PE_z40":
        if direction == "CE":
            return smi1h > sig1h
        return smi1h < sig1h and smi1h < 40
    if variant == "CE_base|PE_z30":
        if direction == "CE":
            return smi1h > sig1h
        return smi1h < sig1h and smi1h < 30
    if variant == "CE_base|PE_z40m5":
        if direction == "CE":
            return smi1h > sig1h
        return smi1h < sig1h - 5 and smi1h < 40
    return False

VARIANTS = ["BASE", "ZONE40", "CE_base|PE_z40", "CE_base|PE_z30", "CE_base|PE_z40m5"]

def run():
    data = pickle.load(open(PKL, "rb"))
    n_days = 0
    res = {v: {"ce": [], "pe": []} for v in VARIANTS}
    for sym, raw in data.items():
        if len(raw) < S.SMI_LENGTH * 4:
            continue
        df = prep(raw)
        df["vwap"] = day_vwap_series(df)
        sv, gv = df["smi"].values, df["sig"].values
        days = sorted(set(df.index.normalize()))
        for d in days:
            day_df = df[df.index.normalize() == d]
            if day_df.empty:
                continue
            for k in range(1, len(day_df)):
                ts = day_df.index[k]
                if not (ENTRY_START <= ts.strftime("%H:%M") <= ENTRY_END):
                    continue
                gi = df.index.get_loc(ts)
                if gi < PCT_LOOKBACK // 4 or gi < 1:
                    continue
                win = sv[max(0, gi - PCT_LOOKBACK):gi + 1]
                win = win[~np.isnan(win)]
                if len(win) < 50:
                    continue
                os_thr = np.percentile(win, OS_PCT)
                ob_thr = np.percentile(win, OB_PCT)
                smi1h, sig1h = df["smi1h"].iloc[gi], df["sig1h"].iloc[gi]
                cl, vw = day_df["close"].iloc[k], day_df["vwap"].iloc[k]
                # CE cross up through own p20
                ce = (sv[gi-1] <= os_thr and sv[gi] > os_thr and sv[gi] > gv[gi])
                # PE cross down through own p80, below vwap (matches loose gate confirm)
                pe = (sv[gi-1] >= ob_thr and sv[gi] < ob_thr and sv[gi] < gv[gi]
                      and not np.isnan(vw) and cl < vw)
                for v in VARIANTS:
                    if ce and passes_1h("CE", smi1h, sig1h, v):
                        pnl, _ = sim_exit(day_df, k, "CE", cl)
                        if isinstance(pnl, float): res[v]["ce"].append(pnl)
                    if pe and passes_1h("PE", smi1h, sig1h, v):
                        pnl, _ = sim_exit(day_df, k, "PE", cl)
                        if isinstance(pnl, float): res[v]["pe"].append(pnl)
        n_days = max(n_days, len(days))

    print(f"\n40-day cache · {len(data)} symbols · ~{n_days} sessions\n")
    print(f"{'variant':<16} {'N':>5} {'/day':>5} {'win%':>6} {'exp%':>7} "
          f"{'CE n/win/exp':>22} {'PE n/win/exp':>22}")
    print("-" * 92)
    for v in VARIANTS:
        ce, pe = res[v]["ce"], res[v]["pe"]
        allt = ce + pe
        def st(x):
            if not x: return (0, 0.0, 0.0)
            a = np.array(x); return (len(a), 100*np.mean(a > 0), np.mean(a))
        n, w, e = st(allt); cn, cw, ce_ = st(ce); pn, pw, pe_ = st(pe)
        print(f"{v:<16} {n:>5} {n/max(n_days,1):>5.1f} {w:>5.0f}% {e:>+7.3f} "
              f"{cn:>5}/{cw:>3.0f}%/{ce_:>+6.3f}   {pn:>5}/{pw:>3.0f}%/{pe_:>+6.3f}")

if __name__ == "__main__":
    run()
