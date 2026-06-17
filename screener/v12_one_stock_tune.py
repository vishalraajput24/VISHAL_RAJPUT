"""
v12_one_stock_tune.py  —  single-stock V12 gate tuner.
Iterates parameters on ONE stock to see baseline → tweaked → enhanced → excellent.
Reuses OB.smi + S.check_exit + F.flow_veto so it matches the live engine math.
In-sample, single stock (~40d). Overfit by design — this is the per-stock-independent
tuning recipe (find each stock's own band/SL/flow), to be forward-validated per stock.

  python3 v12_one_stock_tune.py [SYMBOL]      # default RELIANCE
"""
import os, sys, pickle, numpy as np, pandas as pd
import smi_paper as S, smi_paper_flow as F, orion_v2514_backtest as OB

SYM = sys.argv[1] if len(sys.argv) > 1 else "RELIANCE"
_data = pickle.load(open(os.path.join(S.BASE_DIR, "smi_ohlcv_15m.pkl"), "rb"))
if SYM not in _data:
    sys.exit(f"{SYM} not in cache. Available e.g.: {', '.join(list(_data)[:8])} ...")
DF = _data[SYM].sort_index()


def run(cfg):
    df = DF
    k, d, sig = cfg["k"], cfg["d"], cfg["sig"]
    ob, os_ = cfg["ob"], cfg["os"]
    sl_pct, trail_arm = cfg["sl"], cfg["trail"]
    dirs = cfg.get("dirs", ("CE", "PE"))
    use_flow = cfg.get("flow", True)
    smi_v, sig_v = OB.smi(df, k=k, d=d, sig=sig)
    sv, gv = smi_v.values, sig_v.values
    fdf = F.add_flow_features(df) if use_flow else None
    _save = S.TRAIL_ARM
    S.TRAIL_ARM = trail_arm
    trades = []
    i, n = 50, len(df)
    while i < n:
        ts = df.index[i]
        if not (S.ENTRY_START <= ts.strftime("%H:%M") <= S.ENTRY_END):
            i += 1; continue
        if np.isnan(sv[i]) or np.isnan(sv[i-1]) or np.isnan(gv[i]):
            i += 1; continue
        p, c, cs = sv[i-1], sv[i], gv[i]
        direction = "CE" if (p <= os_ and c > os_ and c > cs) else \
                    ("PE" if (p >= ob and c < ob and c < cs) else None)
        if direction is None or direction not in dirs:
            i += 1; continue
        if use_flow:
            _, _, veto = F.flow_veto(fdf, fdf.iloc[i], direction)
            if veto:
                i += 1; continue
        spot = float(df["close"].iloc[i]); sgn = 1 if direction == "CE" else -1
        tr = {"direction": direction, "stock_entry": spot,
              "sl_price": spot*(1 - sgn*sl_pct/100), "peak": 0.0, "armed": False,
              "last_bar_checked": str(ts), "entry_time": str(ts)}
        r = S.check_exit(tr, df)
        if r:
            trades.append({"dir": direction, "pnl": (r[1]-spot)*sgn/spot*100,
                           "reason": r[2], "exit_ts": r[0]})
            nx = df.index.get_indexer([r[0]])[0]; i = nx+1 if nx >= 0 else i+1
        else:
            i += 1
    S.TRAIL_ARM = _save
    return pd.DataFrame(trades)


def show(label, cfg):
    t = run(cfg)
    if len(t) == 0:
        print(f"{label:26s}  n=0"); return
    wr = (t["pnl"] > 0).mean()*100
    sl = (t["reason"] == "SL-HIT").mean()*100
    print(f"{label:26s}  n={len(t):3d}  win={wr:5.1f}%  avg={t['pnl'].mean():+.3f}%  "
          f"med={t['pnl'].median():+.3f}%  tot={t['pnl'].sum():+6.1f}%  SL%={sl:4.0f}  "
          f"best={t['pnl'].max():+.1f}")


B = dict(k=30, d=3, sig=3, ob=35, os=-35, sl=1.0, trail=1.5, flow=True, dirs=("CE","PE"))
print(f"=== {SYM} only · 40d · 15m · V12 gate tuning (in-sample, single stock) ===\n")
print("BASELINE (V12-literal):")
show("0 baseline", B)

# grid search on RELIANCE only
def stats(cfg):
    t = run(cfg)
    if len(t) == 0: return None
    return dict(n=len(t), win=(t["pnl"]>0).mean()*100, avg=t["pnl"].mean(),
                tot=t["pnl"].sum(), med=t["pnl"].median())

grid = []
for ob, os_ in [(35,-35),(40,-40),(45,-45),(45,-40),(40,-45),(50,-50)]:
    for sl in [0.7, 1.0, 1.5]:
        for trail in [1.0, 1.5, 2.0]:
            for flow in [True, False]:
                cfg = {**B, "ob":ob, "os":os_, "sl":sl, "trail":trail, "flow":flow}
                st = stats(cfg)
                if st and st["n"] >= 8:        # need a minimum sample
                    grid.append((cfg, st))
grid.sort(key=lambda x: x[1]["tot"], reverse=True)

print(f"\nGRID: {len(grid)} configs (n>=8) ranked by total% — top 8:")
print(f"{'bands':>10} {'SL':>4} {'trail':>5} {'flow':>5} | {'n':>3} {'win%':>5} {'avg%':>7} {'tot%':>6}")
for cfg, st in grid[:8]:
    print(f"{cfg['os']:>4}/{cfg['ob']:<3} {cfg['sl']:>4} {cfg['trail']:>5} "
          f"{str(cfg['flow']):>5} | {st['n']:>3} {st['win']:>5.1f} {st['avg']:>+7.3f} {st['tot']:>+6.1f}")

best = grid[0][0]
print(f"\nBEST-total on {SYM}: bands {best['os']}/{best['ob']} · SL {best['sl']}% · "
      f"trail {best['trail']}% · flow {best['flow']}")
show("BEST-total", best)

print("\nENHANCE — selective band ±50 + flow-gate (does the gate help when SELECTIVE?):")
show("±50 flow OFF", {**B, "ob":50, "os":-50, "flow":False})
show("±50 flow ON ", {**B, "ob":50, "os":-50, "flow":True})
show("±50 flow ON SL0.7", {**B, "ob":50, "os":-50, "flow":True, "sl":0.7})

EXC = {**B, "ob":50, "os":-50, "flow":True, "sl":1.0, "trail":1.5}
print("\nEXCELLENT (chosen) = ±50 cross + flow-gate + SL 1.0% + trail 1.5% — trade list:")
t = run(EXC)
for _, r in t.iterrows():
    print(f"  {str(r['exit_ts'])[:16]}  {r['dir']}  {r['pnl']:+6.2f}%  {r['reason']}")
print(f"  → n={len(t)}  win={(t['pnl']>0).mean()*100:.1f}%  avg={t['pnl'].mean():+.3f}%  "
      f"tot={t['pnl'].sum():+.1f}%  over 40d")
