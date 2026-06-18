"""Deep per-stock V12-gate tuner for the FOCUS batch-3 liquid subset (06-18).

Wider grid than v12_batch_tune.py: also sweeps SMI smoothing (d/sig), SL% and
trail% — which the live FOCUS engine honors per-stock (smi_paper sl_price /
trail_arm, smi_focus9 d/sig overrides). Objective = best per-trade expectancy
(avg%) SUBJECT TO n>=12 and win>=70% (a real sample + a high hit-rate floor),
so we maximise edge rather than cherry-pick a tiny-sample 100%.

STILL IN-SAMPLE / OVERFIT BY DESIGN (best-of-grid per stock, 40d). Output is a
ready-to-paste FOCUS dict line per stock. Reuses the live-engine math.

  python3 v12_focus_tune.py                # the 18 batch-3 stocks
  python3 v12_focus_tune.py SYM1 SYM2 ...  # custom list
"""
import os, sys, pickle, numpy as np, pandas as pd
import smi_paper as S, smi_paper_flow as F, orion_v2514_backtest as OB

DATA = pickle.load(open(os.path.join(S.BASE_DIR, "smi_ohlcv_15m.pkl"), "rb"))

BATCH3 = ["MPHASIS", "TATACONSUM", "ZYDUSLIFE", "APOLLOHOSP", "DMART", "DLF",
          "JIOFIN", "LUPIN", "AUBANK", "LICHSGFIN", "INDUSINDBK", "TRENT",
          "DIVISLAB", "BPCL", "COALINDIA", "SIEMENS", "CHOLAFIN", "MCX"]
SYMS = sys.argv[1:] or BATCH3

# cache flow-feature frames per (sym) once — they don't depend on the gate
_flow_cache = {}


def run(DF, fdf, c):
    sv, gv = (lambda a, b: (a.values, b.values))(*OB.smi(DF, k=c["k"], d=c["d"], sig=c["sig"]))
    tr = []
    i, n = 50, len(DF)
    arm = c["trail"]
    while i < n:
        ts = DF.index[i]
        if not (S.ENTRY_START <= ts.strftime("%H:%M") <= S.ENTRY_END): i += 1; continue
        if np.isnan(sv[i]) or np.isnan(sv[i - 1]) or np.isnan(gv[i]): i += 1; continue
        p, cc, cs = sv[i - 1], sv[i], gv[i]
        d = "CE" if (p <= c["os"] and cc > c["os"] and cc > cs) else \
            ("PE" if (p >= c["ob"] and cc < c["ob"] and cc < cs) else None)
        if d is None or d not in c["dirs"]: i += 1; continue
        if c["flow"] and fdf is not None:
            _, _, v = F.flow_veto(fdf, fdf.iloc[i], d)
            if v: i += 1; continue
        spot = float(DF["close"].iloc[i]); sg = 1 if d == "CE" else -1
        t = {"direction": d, "stock_entry": spot, "sl_price": spot * (1 - sg * c["sl"] / 100),
             "peak": 0.0, "armed": False, "last_bar_checked": str(ts), "entry_time": str(ts),
             "trail_arm": arm}
        r = S.check_exit(t, DF)
        if r:
            tr.append((r[1] - spot) * sg / spot * 100)
            nx = DF.index.get_indexer([r[0]])[0]; i = nx + 1 if nx >= 0 else i + 1
        else: i += 1
    return tr


GRID = [dict(k=k, d=d, sig=sig, ob=b, os=-b, sl=sl, trail=tr, flow=fl, dirs=di)
        for k in (21, 30, 40)
        for (d, sig) in ((3, 3), (5, 3), (5, 5))
        for b in (35, 40, 45, 50, 55)
        for sl in (0.8, 1.0, 1.2)
        for tr in (1.0, 1.5, 2.0)
        for fl in (True, False)
        for di in (("CE", "PE"), ("CE",), ("PE",))]

print(f"FOCUS deep tuner · {len(GRID)} configs/stock · obj=max avg% s.t. n>=12 & win>=70%\n")
hdr = f"{'stock':12} | {'best config':46} | {'n':>2} {'win%':>5} {'avg%':>7} {'tot%':>6}"
print(hdr); print("-" * len(hdr))

results = {}
for sym in SYMS:
    if sym not in DATA:
        print(f"{sym:12} | not in cache"); continue
    DF = DATA[sym].sort_index()
    fdf = F.add_flow_features(DF) if "volume" in DF.columns else None
    best = None  # (avg, win, n, c, tot)
    for c in GRID:
        if c["flow"] and fdf is None:  # can't flow-gate without volume
            continue
        t = run(DF, fdf, c)
        if len(t) < 12:
            continue
        ts_ = pd.Series(t); win = (ts_ > 0).mean() * 100; avg = ts_.mean(); tot = ts_.sum()
        if win < 70:
            continue
        key = (round(avg, 4), round(win, 1), len(t))
        if best is None or key > best[0]:
            best = (key, c, len(t), win, avg, tot)
    if best is None:
        print(f"{sym:12} | no config with n>=12 & win>=70%"); continue
    _, c, n, win, avg, tot = best
    tag = (f"k{c['k']} {c['d']}/{c['sig']} ±{c['ob']} sl{c['sl']:g}/tr{c['trail']:g} "
           f"fl{str(c['flow'])[0]} {'/'.join(c['dirs'])}")
    print(f"{sym:12} | {tag:46} | {n:>2} {win:>5.1f} {avg:>+7.3f} {tot:>+6.1f}", flush=True)
    results[sym] = (c, win)

print("-" * len(hdr))
print("\n# ── paste-ready FOCUS lines ──")
for sym, (c, win) in results.items():
    extra = ""
    if (c["d"], c["sig"]) != (3, 3): extra += f" d={c['d']}, sig={c['sig']},"
    if c["sl"] != 1.0: extra += f" sl={c['sl']},"
    if c["trail"] != 1.5: extra += f" trail={c['trail']},"
    dirs = "(\"CE\", \"PE\")" if len(c["dirs"]) == 2 else f'("{c["dirs"][0]}",)'
    print(f'    "{sym}":{" " * (12 - len(sym))}dict(k={c["k"]}, ob={c["ob"]}, os={c["os"]}, '
          f'dirs={dirs}, flow={c["flow"]},{extra} wr={win:.1f}),')
