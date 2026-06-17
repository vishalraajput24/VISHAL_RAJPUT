"""Batch V12-gate tuner across many stocks — prints each stock's BEST config.
Wide grid (SMI k/smoothing + bands + SL + trail + flow + direction), reuses the
live-engine math. In-sample 40d, single-stock-independent (overfit by design)."""
import os, sys, pickle, numpy as np, pandas as pd
import smi_paper as S, smi_paper_flow as F, orion_v2514_backtest as OB

DATA = pickle.load(open(os.path.join(S.BASE_DIR, "smi_ohlcv_15m.pkl"), "rb"))
SYMS = sys.argv[1:] or ["TCS","INFY","HDFCBANK","SBIN","AXISBANK",
                        "MARUTI","LT","TITAN","BAJFINANCE","SUNPHARMA"]

def run(DF, c):
    sv, gv = (lambda a, b: (a.values, b.values))(*OB.smi(DF, k=c["k"], d=c["d"], sig=c["sig"]))
    fdf = F.add_flow_features(DF) if c["flow"] else None
    _sv = S.TRAIL_ARM; S.TRAIL_ARM = c["trail"]
    tr = []; i, n = 50, len(DF)
    while i < n:
        ts = DF.index[i]
        if not (S.ENTRY_START <= ts.strftime("%H:%M") <= S.ENTRY_END): i += 1; continue
        if np.isnan(sv[i]) or np.isnan(sv[i-1]) or np.isnan(gv[i]): i += 1; continue
        p, cc, cs = sv[i-1], sv[i], gv[i]
        d = "CE" if (p <= c["os"] and cc > c["os"] and cc > cs) else \
            ("PE" if (p >= c["ob"] and cc < c["ob"] and cc < cs) else None)
        if d is None or d not in c["dirs"]: i += 1; continue
        if c["flow"]:
            _, _, v = F.flow_veto(fdf, fdf.iloc[i], d)
            if v: i += 1; continue
        spot = float(DF["close"].iloc[i]); sg = 1 if d == "CE" else -1
        t = {"direction":d,"stock_entry":spot,"sl_price":spot*(1-sg*c["sl"]/100),
             "peak":0.0,"armed":False,"last_bar_checked":str(ts),"entry_time":str(ts)}
        r = S.check_exit(t, DF)
        if r:
            tr.append((r[1]-spot)*sg/spot*100)
            nx = DF.index.get_indexer([r[0]])[0]; i = nx+1 if nx >= 0 else i+1
        else: i += 1
    S.TRAIL_ARM = _sv
    return tr

GRID = [dict(k=k,d=3,sig=3,ob=ob,os=os_,sl=1.0,trail=1.5,flow=fl,dirs=di)
        for k in (21,30,40)
        for ob,os_ in ((35,-35),(40,-40),(45,-45),(50,-50))
        for fl in (True,False) for di in (("CE","PE"),("CE",),("PE",))]

print(f"V12 per-stock tuner · {len(GRID)} configs/stock · 40d in-sample\n")
print(f"{'stock':12} | {'best-win config':38} | {'n':>2} {'win%':>5} {'avg%':>7} {'tot%':>6}")
print("-"*86)
rows=[]
for sym in SYMS:
    if sym not in DATA: print(f"{sym:12} | not in cache"); continue
    DF = DATA[sym].sort_index()
    best=None
    for c in GRID:
        t = run(DF, c)
        if len(t) < 12: continue
        t = pd.Series(t); win=(t>0).mean()*100; avg=t.mean(); tot=t.sum()
        score=(win, avg)                       # rank by win%, then avg
        if best is None or score>best[0]:
            best=(score, c, len(t), win, avg, tot)
    if not best: print(f"{sym:12} | no config with n>=12"); continue
    _,c,n,win,avg,tot=best
    tag=f"k{c['k']} {c['os']}/{c['ob']} fl{str(c['flow'])[0]} {'/'.join(c['dirs'])}"
    print(f"{sym:12} | {tag:38} | {n:>2} {win:>5.1f} {avg:>+7.3f} {tot:>+6.1f}", flush=True)
    rows.append((sym,win,avg))
print("-"*86)
if rows:
    keep=[r for r in rows if r[1]>=70 and r[2]>0]
    print(f"keepers (win>=70% & avg>0): {', '.join(f'{s}({w:.0f}%)' for s,w,a in keep) or 'none'}")
