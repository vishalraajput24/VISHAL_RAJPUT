"""
smi_single_filter.py
────────────────────
Find ONE confirmation filter for the E2-SMI engine (owner dropped regime/RSI/MACD).

Trigger (locked): RMA SMI 14/3/3 — CE cross up −35 same-bar, PE cross down +55
with confirm-within-6-bars. SMI vs its signal line is part of the trigger.

Each candidate runs as the SOLE filter on both sides:
  F1  1h SMI vs signal        (the current 1h alignment)
  F2  1h SMI sign             (>0 CE / <0 PE)
  F3  1h SMI vs signal + sign (F1 AND F2)
  F4  VWAP side               (close > VWAP CE / < VWAP PE)
  F5  Supertrend(10,3) 15m    (direction up CE / down PE)
  F6  1h EMA20 vs EMA50       (1h regime)
  F7  ADX(14) 15m             (ADX>20 and +DI>-DI CE / -DI>+DI PE)
  F8  1h MACD vs signal
  F9  F1 + VWAP side          (two cheap ones, for reference)

Usage: python3 smi_single_filter.py
"""

import pickle
import numpy as np
import pandas as pd
import smi_backtest as sb
from smi_pe_tuning import build_arrays, LENGTH, SMOOTH, FLAVOR, OS_CE, SL_MODE

OB_PE = 55
PE_LOOKAHEAD = 6
REPORT = sb.BASE_DIR + "/smi_single_filter_report.md"


# ── extra indicators ─────────────────────────────────────────────────────────

def add_vwap(df: pd.DataFrame) -> pd.Series:
    d = df.index.date
    tp = (df["high"] + df["low"] + df["close"]) / 3
    tpv = (tp * df["volume"]).groupby(d).cumsum()
    vv = df["volume"].groupby(d).cumsum()
    return tpv / vv.replace(0, np.nan)


def supertrend(df: pd.DataFrame, period: int = 10, mult: float = 3.0) -> np.ndarray:
    """Returns +1 (up) / -1 (down) per bar."""
    h, l, c = df["high"].values, df["low"].values, df["close"].values
    prev_c = np.roll(c, 1); prev_c[0] = c[0]
    tr = np.maximum(h - l, np.maximum(abs(h - prev_c), abs(l - prev_c)))
    atr = pd.Series(tr).ewm(alpha=1 / period, adjust=False).mean().values
    mid = (h + l) / 2
    ub, lb = mid + mult * atr, mid - mult * atr
    fub, flb = ub.copy(), lb.copy()
    trend = np.ones(len(c))
    for i in range(1, len(c)):
        fub[i] = ub[i] if (ub[i] < fub[i-1] or c[i-1] > fub[i-1]) else fub[i-1]
        flb[i] = lb[i] if (lb[i] > flb[i-1] or c[i-1] < flb[i-1]) else flb[i-1]
        if trend[i-1] == 1:
            trend[i] = -1 if c[i] < flb[i] else 1
        else:
            trend[i] = 1 if c[i] > fub[i] else -1
    return trend


def adx(df: pd.DataFrame, period: int = 14):
    h, l, c = df["high"].values, df["low"].values, df["close"].values
    prev_c = np.roll(c, 1); prev_c[0] = c[0]
    up = h - np.roll(h, 1); dn = np.roll(l, 1) - l
    up[0] = dn[0] = 0
    pdm = np.where((up > dn) & (up > 0), up, 0.0)
    ndm = np.where((dn > up) & (dn > 0), dn, 0.0)
    tr = np.maximum(h - l, np.maximum(abs(h - prev_c), abs(l - prev_c)))
    a = 1 / period
    atr = pd.Series(tr).ewm(alpha=a, adjust=False).mean()
    pdi = 100 * pd.Series(pdm).ewm(alpha=a, adjust=False).mean() / atr
    ndi = 100 * pd.Series(ndm).ewm(alpha=a, adjust=False).mean() / atr
    dx = 100 * (pdi - ndi).abs() / (pdi + ndi).replace(0, np.nan)
    return dx.ewm(alpha=a, adjust=False).mean().values, pdi.values, ndi.values


def map_1h(base: pd.DataFrame, df1h: pd.DataFrame, series: pd.Series) -> np.ndarray:
    """Map a 1h series onto the 15m index without lookahead (known at bar close)."""
    h = pd.DataFrame({"v": series.values, "known_at": df1h["known_at"].values}).dropna()
    h = h.sort_values("known_at")
    m = pd.merge_asof(pd.DataFrame({"ts": base.index}), h,
                      left_on="ts", right_on="known_at", direction="backward")
    return m["v"].values


# ── per-symbol context: all candidate filter masks (CE side, PE side) ────────

def build_context(sym, base, df1h):
    ctx = {}
    smi1h, sig1h = sb.align_1h_smi(base, df1h, LENGTH, SMOOTH, FLAVOR)
    ok1 = ~np.isnan(smi1h) & ~np.isnan(sig1h)
    ctx["F1 1h SMI vs signal"]      = (ok1 & (smi1h > sig1h), ok1 & (smi1h < sig1h))
    ctx["F2 1h SMI sign"]           = (ok1 & (smi1h > 0),     ok1 & (smi1h < 0))
    ctx["F3 1h SMI signal+sign"]    = (ok1 & (smi1h > sig1h) & (smi1h > 0),
                                       ok1 & (smi1h < sig1h) & (smi1h < 0))
    vwap = add_vwap(base).values
    okv = ~np.isnan(vwap)
    cl = base["close"].values
    ctx["F4 VWAP side"]             = (okv & (cl > vwap), okv & (cl < vwap))
    st = supertrend(base)
    ctx["F5 Supertrend 15m"]        = (st > 0, st < 0)
    e20 = df1h["close"].ewm(span=20, adjust=False).mean()
    e50 = df1h["close"].ewm(span=50, adjust=False).mean()
    e20m, e50m = map_1h(base, df1h, e20), map_1h(base, df1h, e50)
    oke = ~np.isnan(e20m) & ~np.isnan(e50m)
    ctx["F6 1h EMA20>50"]           = (oke & (e20m > e50m), oke & (e20m < e50m))
    a, pdi, ndi = adx(base)
    oka = ~np.isnan(a)
    ctx["F7 ADX>20 +DI/-DI"]        = (oka & (a > 20) & (pdi > ndi),
                                       oka & (a > 20) & (ndi > pdi))
    m12 = df1h["close"].ewm(span=12, adjust=False).mean()
    m26 = df1h["close"].ewm(span=26, adjust=False).mean()
    mac = m12 - m26
    msig = mac.ewm(span=9, adjust=False).mean()
    macm, msigm = map_1h(base, df1h, mac), map_1h(base, df1h, msig)
    okm = ~np.isnan(macm) & ~np.isnan(msigm)
    ctx["F8 1h MACD"]               = (okm & (macm > msigm), okm & (macm < msigm))
    f1ce, f1pe = ctx["F1 1h SMI vs signal"]
    f4ce, f4pe = ctx["F4 VWAP side"]
    ctx["F9 1h SMI + VWAP"]         = (f1ce & f4ce, f1pe & f4pe)
    return ctx


def scan_side(base, arr, ce_mask, pe_mask):
    smi, sig = sb.compute_smi(base, LENGTH, SMOOTH, FLAVOR)
    sv, gv = smi.values, sig.values
    prev = np.roll(sv, 1); prev[0] = np.nan
    in_w = (arr["tstr"] >= sb.ENTRY_START) & (arr["tstr"] <= sb.ENTRY_END)
    valid = ~np.isnan(sv) & ~np.isnan(prev)

    out = []
    # CE: same-bar
    ce = (prev <= OS_CE) & (sv > OS_CE) & (sv > gv) & valid & in_w & ce_mask
    last = -1
    for i in np.where(ce)[0]:
        if i <= last: continue
        t = sb.simulate_exit(arr, i, "CE", SL_MODE); last = t["exit_idx"]
        out.append(("CE", str(base.index[i].date()),
                    sb.price_band(float(arr["close"][i])), t["pnl_pct"], t["result"]))
    # PE: confirm within 6 bars (filter may come good after the cross)
    cross = (prev >= OB_PE) & (sv < OB_PE) & valid
    last = -1
    n = len(sv)
    for i in np.where(cross)[0]:
        for j in range(i, min(i + PE_LOOKAHEAD + 1, n)):
            if j <= last or not in_w[j]: continue
            if arr["day"][j] != arr["day"][i]: break
            if sv[j] < gv[j] and sv[j] < OB_PE and pe_mask[j]:
                t = sb.simulate_exit(arr, j, "PE", SL_MODE); last = t["exit_idx"]
                out.append(("PE", str(base.index[j].date()),
                            sb.price_band(float(arr["close"][j])), t["pnl_pct"], t["result"]))
                break
    return out


def main():
    data = pickle.load(open(sb.PKL_CACHE, "rb"))
    prepared = {s: sb.prepare_base(df) for s, df in data.items()}
    hourly = {s: sb.resample_1h(df) for s, df in data.items()}
    arrays = {s: build_arrays(b) for s, b in prepared.items()}
    contexts = {s: build_context(s, prepared[s], hourly[s]) for s in prepared}

    labels = list(next(iter(contexts.values())).keys())
    R = ["# E2-SMI single-filter search (regime/RSI/MACD dropped by owner)",
         f"\nGenerated: {pd.Timestamp.now().strftime('%Y-%m-%d %H:%M')} | "
         f"{len(data)} stocks ~40 days | CE same-bar, PE confirm-within-6 | SL={SL_MODE}\n",
         "| Filter | Side | Trades | Win% | Exp %/trade | Total % | Apr | May | Jun |",
         "|--------|------|--------|------|-------------|---------|-----|-----|-----|"]

    summary = {}
    for label in labels:
        rows = []
        for s in prepared:
            ce_m, pe_m = contexts[s][label]
            rows.extend(scan_side(prepared[s], arrays[s], ce_m, pe_m))
        d = pd.DataFrame(rows, columns=["side", "date", "band", "pnl_pct", "result"])
        d["month"] = d["date"].str[:7]
        for side in ("CE", "PE"):
            sub = d[d["side"] == side]
            n = len(sub)
            if n == 0:
                R.append(f"| {label} | {side} | 0 | — | — | — | — | — | — |"); continue
            w = (sub["result"] == "WIN").mean() * 100
            e = sub["pnl_pct"].mean()
            mm = {m: g["pnl_pct"].mean() for m, g in sub.groupby("month")}
            R.append(f"| {label} | {side} | {n} | {w:.1f}% | {e:+.3f}% | "
                     f"{sub['pnl_pct'].sum():+.1f}% | "
                     f"{mm.get('2026-04', float('nan')):+.2f} | "
                     f"{mm.get('2026-05', float('nan')):+.2f} | "
                     f"{mm.get('2026-06', float('nan')):+.2f} |")
            summary[(label, side)] = (n, e)
            print(f"{label:24s} {side} n={n:4d} win={w:5.1f}% exp={e:+.3f}%")

    # combined score: min of the two sides' expectancy, both sides n>=25
    R.append("\n## Ranking (worse side's expectancy, both sides ≥25 trades)\n")
    elig = {}
    for label in labels:
        ce = summary.get((label, "CE")); pe = summary.get((label, "PE"))
        if ce and pe and ce[0] >= 25 and pe[0] >= 25:
            elig[label] = min(ce[1], pe[1])
    for label, v in sorted(elig.items(), key=lambda kv: -kv[1]):
        ce, pe = summary[(label, "CE")], summary[(label, "PE")]
        R.append(f"- **{label}** — worse side {v:+.3f}% "
                 f"(CE n={ce[0]} {ce[1]:+.3f}% | PE n={pe[0]} {pe[1]:+.3f}%)")

    with open(REPORT, "w") as f:
        f.write("\n".join(R))
    print(f"\nReport → {REPORT}")


if __name__ == "__main__":
    main()
