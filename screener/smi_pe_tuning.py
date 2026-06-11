"""
smi_pe_tuning.py
────────────────
PE-side filter tuning for the E2-SMI engine.

CE side is LOCKED (concluded 2026-06-11):
  RMA SMI 14/3/3, cross up through OS −35, SMI > signal,
  SMA20>SMA50 + RSI>50 + MACD>signal + 1h SMI>signal, SL=fixed 10 pts.

PE problem: same-bar confirmations (RSI<50, MACD<signal) contradict the
overbought trigger (SMI ≥ +55) — only 5 PE trades survived in 40 days.

This script sweeps PE-specific variants:
  - which confirmations to require (regime / RSI / MACD / 1h)
  - PE overbought threshold (55 / 45 / 40)
  - "recent" confirmations: RSI/MACD may turn bearish within N bars after cross

Usage: python3 smi_pe_tuning.py   (uses smi_ohlcv_15m.pkl cache)
"""

import pickle
import numpy as np
import pandas as pd
import smi_backtest as sb

LENGTH, SMOOTH, FLAVOR = 14, 3, "rma"
OS_CE = -35
SL_MODE = "pts"
REPORT = sb.BASE_DIR + "/smi_pe_tuning_report.md"


def build_arrays(base: pd.DataFrame) -> dict:
    return {
        "close": base["close"].values, "high": base["high"].values,
        "low": base["low"].values, "sma8": base["sma8"].values,
        "day": base.index.normalize().values,
        "tstr": np.array([t.strftime("%H:%M") for t in base.index]),
    }


def scan_pe(sym, base, df1h, arr, ob, use_regime, use_rsi, use_macd, use_1h,
            confirm_lookahead=0):
    """PE-only scan. confirm_lookahead=N: after a raw cross, entry is allowed on
    the cross bar or any of the next N bars, on the first bar where all
    confirmations hold (SMI must still be < its signal and below OB)."""
    smi, sig = sb.compute_smi(base, LENGTH, SMOOTH, FLAVOR)
    sv, gv = smi.values, sig.values
    prev = np.roll(sv, 1); prev[0] = np.nan

    sma20, sma50 = base["sma20"].values, base["sma50"].values
    rsi = base["rsi"].values
    macd, ms = base["macd"].values, base["macd_signal"].values
    if use_1h:
        smi1h, sig1h = sb.align_1h_smi(base, df1h, LENGTH, SMOOTH, FLAVOR)

    in_window = (arr["tstr"] >= sb.ENTRY_START) & (arr["tstr"] <= sb.ENTRY_END)
    valid = ~np.isnan(sv) & ~np.isnan(prev) & ~np.isnan(sma50) & ~np.isnan(rsi) & ~np.isnan(ms)
    cross = (prev >= ob) & (sv < ob) & valid

    def confirms(i):
        if sv[i] >= gv[i] or sv[i] >= ob:
            return False
        if use_regime and not (sma20[i] < sma50[i]): return False
        if use_rsi and not (rsi[i] < 50): return False
        if use_macd and not (macd[i] < ms[i]): return False
        if use_1h and (np.isnan(smi1h[i]) or smi1h[i] >= sig1h[i]): return False
        return True

    signals = []
    last_exit = -1
    n = len(sv)
    for i in np.where(cross)[0]:
        for j in range(i, min(i + confirm_lookahead + 1, n)):
            if j <= last_exit or not in_window[j]:
                continue
            if arr["day"][j] != arr["day"][i]:
                break
            if confirms(j):
                trade = sb.simulate_exit(arr, j, "PE", SL_MODE)
                last_exit = trade["exit_idx"]
                signals.append({
                    "symbol": sym, "date": str(base.index[j].date()),
                    "entry_time": str(base.index[j]), "direction": "PE",
                    "entry_price": round(float(arr["close"][j]), 2),
                    "band": sb.price_band(float(arr["close"][j])),
                    **{k: v for k, v in trade.items() if k != "exit_idx"},
                })
                break
    return signals


def main():
    data = pickle.load(open(sb.PKL_CACHE, "rb"))
    prepared = {s: sb.prepare_base(df) for s, df in data.items()}
    hourly = {s: sb.resample_1h(df) for s, df in data.items()}
    arrays = {s: build_arrays(b) for s, b in prepared.items()}

    # (label, ob, regime, rsi, macd, 1h, lookahead)
    VARIANTS = [
        ("P0 baseline (same-bar all)",      55, True,  True,  True,  True,  0),
        ("P1 drop RSI",                     55, True,  False, True,  True,  0),
        ("P2 drop MACD",                    55, True,  True,  False, True,  0),
        ("P3 drop RSI+MACD",                55, True,  False, False, True,  0),
        ("P4 drop 1h",                      55, True,  True,  True,  False, 0),
        ("P5 regime only",                  55, True,  False, False, False, 0),
        ("P6 confirm within 3 bars",        55, True,  True,  True,  True,  3),
        ("P7 confirm within 6 bars",        55, True,  True,  True,  True,  6),
        ("P8 OB=45, same-bar all",          45, True,  True,  True,  True,  0),
        ("P9 OB=40, same-bar all",          40, True,  True,  True,  True,  0),
        ("P10 OB=40, drop RSI",             40, True,  False, True,  True,  0),
        ("P11 OB=40, confirm within 3",     40, True,  True,  True,  True,  3),
        ("P12 OB=45, confirm within 3",     45, True,  True,  True,  True,  3),
        ("P13 OB=40, drop MACD",            40, True,  True,  False, True,  0),
    ]

    R = []
    R.append("# E2-SMI PE-side tuning (CE locked: RMA 14/3/3, OS −35, full filters)")
    R.append(f"\nGenerated: {pd.Timestamp.now().strftime('%Y-%m-%d %H:%M')} | "
             f"universe {len(data)} stocks, ~40 days, SL mode = {SL_MODE}\n")
    R.append("| Variant | OB | Trades | Win% | Exp %/trade | Total % | SL% |")
    R.append("|---------|----|--------|------|-------------|---------|-----|")

    results = {}
    for label, ob, rg, rs, mc, h1, la in VARIANTS:
        sigs = []
        for s in prepared:
            sigs.extend(scan_pe(s, prepared[s], hourly[s], arrays[s],
                                ob, rg, rs, mc, h1, la))
        st = sb.stats(sigs)
        results[label] = (st, sigs)
        R.append(f"| {label} | {ob} | {st['n']} | {st['win']}% | {st['exp_pct']:+.3f}% "
                 f"| {st['tot_pct']:+.1f}% | {st['sl']}% |")
        print(f"{label:32s} n={st['n']:4d} win={st['win']:5.1f}% "
              f"exp={st['exp_pct']:+.3f}% tot={st['tot_pct']:+.1f}%")

    # best with n>=30
    elig = {k: v for k, (v, _) in results.items() if v["n"] >= 30}
    if elig:
        best = max(elig, key=lambda k: elig[k]["exp_pct"])
        st, sigs = results[best]
        R.append(f"\n**Best PE variant (n≥30): {best}** — n={st['n']}, win={st['win']}%, "
                 f"exp={st['exp_pct']:+.3f}%/trade, total={st['tot_pct']:+.1f}%\n")
        d = pd.DataFrame(sigs)
        R.append("### Best variant by price band\n")
        R.append("| Band | Trades | Win% | Exp %/trade |")
        R.append("|------|--------|------|-------------|")
        for _, _, name in sb.BANDS:
            sub = d[d["band"] == name]
            if sub.empty:
                R.append(f"| {name} | 0 | — | — |"); continue
            w = (sub["result"] == "WIN").mean() * 100
            R.append(f"| {name} | {len(sub)} | {w:.1f}% | {sub['pnl_pct'].mean():+.3f}% |")
        R.append("\n### Best variant exit reasons\n")
        for reason, grp in d.groupby("exit_reason"):
            R.append(f"- {reason}: {len(grp)} trades, avg {grp['pnl_pct'].mean():+.3f}%")
        d.to_csv(sb.BASE_DIR + "/smi_pe_best_trades.csv", index=False)

    with open(REPORT, "w") as f:
        f.write("\n".join(R))
    print(f"\nReport → {REPORT}")


if __name__ == "__main__":
    main()
