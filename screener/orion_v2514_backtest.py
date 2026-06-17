"""
orion_v2514_backtest.py
───────────────────────
ORION V2.5.14 — NIFTY signal-level backtest + R&D harness.

Signals fire on NIFTY (near-month FUTURE, has volume) 15m candles, confirmed
on 1h. P&L is measured in NIFTY points (delta proxy for an ATM weekly option —
local option 1min data is too sparse for reliable fill simulation, see CLAUDE.md
/ memory project_5m15m_strategy_rnd).

Engines:
  E1 VWAP   — 15m close above/below daily VWAP, body >= 65%, any regime
  E2 SMI    — %K10/%D3/sig3, OB63/OS-37, cross + side-of-signal, BULL/BEAR only,
              confirmed by SMA20/50 + RSI + MACD on 15m AND 1h
  E3 Cluster— break above/below a swing-high/low cluster, vol >= 1.5x avg(10)
  E4 FLIP   — piggybacks E2 (path A: aged winner fading + SMI reversal;
              path B: SMI reversal within 60min of an E2 exit). Max 3/day.

Exits: hard SL (fixed pts OR ATR(14) mult) · trail arms at peak >= +ARM ·
       SMA8 trail (E2/E3/FLIP) / VWAP trail (E1) · force close 15:25.
Risk:  one trade at a time · 3 non-flip losses/day stops entries · max 3 flips/day.

Usage:
  ~/kite_env/bin/python3 orion_v2514_backtest.py            # baseline + all tests
  ~/kite_env/bin/python3 orion_v2514_backtest.py --sweep    # + parameter sweep
"""

import os, sys, json, datetime as dt
import pandas as pd, numpy as np

BASE = os.path.dirname(os.path.abspath(__file__))
TOKEN_FILE = os.path.join(os.path.expanduser("~"), "VISHAL_RAJPUT", "state", "access_token.json")
CACHE_15 = os.path.join(BASE, "orion_v2514_nifty15m.csv")

# ── V2.5.14 spec params ───────────────────────────────────────────────
ENTRY_START = "09:45"
ENTRY_END   = "14:30"
FORCE_CLOSE = "15:25"
SMI_K, SMI_D, SMI_SIG = 10, 3, 3
SMI_OB, SMI_OS = 63.0, -37.0
SWING_LR    = 3       # pivot left/right bars for cluster swings
CLUSTER_LOOKBACK = 20 # bars to find the nearest swing level to break
VOL_MULT    = 1.5     # E3 volume filter
VOL_WIN     = 10
MAX_FLIPS   = 3
MAX_NONFLIP_LOSSES = 3


# ── data ──────────────────────────────────────────────────────────────
def load_env():
    for ln in open(os.path.expanduser("~/.env")):
        ln = ln.strip()
        if "=" in ln and not ln.startswith("#"):
            k, _, v = ln.partition("=")
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def fetch_15m(days=75, force=False):
    if os.path.isfile(CACHE_15) and not force:
        df = pd.read_csv(CACHE_15, parse_dates=["date"])
        return df
    load_env()
    from kiteconnect import KiteConnect
    tok = json.load(open(TOKEN_FILE))["access_token"]
    k = KiteConnect(api_key=os.environ["KITE_API_KEY"]); k.set_access_token(tok)
    inst = k.instruments("NFO")
    fut = sorted([i for i in inst if i["name"] == "NIFTY" and i["instrument_type"] == "FUT"],
                 key=lambda x: x["expiry"])[0]
    to = dt.date.today(); frm = to - dt.timedelta(days=days)
    d = k.historical_data(fut["instrument_token"], frm, to, "15minute")
    df = pd.DataFrame(d)
    df["date"] = pd.to_datetime(df["date"]).dt.tz_localize(None)
    df.to_csv(CACHE_15, index=False)
    print(f"fetched {fut['tradingsymbol']} {len(df)} bars -> {CACHE_15}")
    return df


# ── indicators ──────────────────────────────────────────────────────────
def ema(s, span): return s.ewm(span=span, adjust=False).mean()

def rsi(s, n=14):
    d = s.diff()
    up = d.clip(lower=0).ewm(alpha=1/n, adjust=False).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1/n, adjust=False).mean()
    rs = up / dn.replace(0, np.nan)
    return (100 - 100/(1+rs)).fillna(50)

def smi(df, k=SMI_K, d=SMI_D, sig=SMI_SIG):
    hh = df["high"].rolling(k).max(); ll = df["low"].rolling(k).min()
    rel = df["close"] - (hh+ll)/2
    rng = (hh - ll)
    er = ema(ema(rel, d), d)
    eg = ema(ema(rng, d), d)
    smi_v = 100 * (er / (eg/2)).replace([np.inf, -np.inf], np.nan)
    return smi_v, ema(smi_v, sig)

def atr(df, n=14):
    h, l, c = df["high"], df["low"], df["close"]
    tr = pd.concat([h-l, (h-c.shift()).abs(), (l-c.shift()).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1/n, adjust=False).mean()

def add_indicators(df):
    df = df.sort_values("date").reset_index(drop=True)
    df["d"] = df["date"].dt.date
    tp = (df["high"]+df["low"]+df["close"])/3
    df["_cpv"] = (tp*df["volume"]).groupby(df["d"]).cumsum()
    df["_cv"]  = df["volume"].groupby(df["d"]).cumsum()
    df["vwap"] = df["_cpv"]/df["_cv"]
    rng = (df["high"]-df["low"]).replace(0, np.nan)
    df["body_pct"] = (df["close"]-df["open"]).abs()/rng*100
    df["sma8"]  = df["close"].rolling(8).mean()
    df["sma20"] = df["close"].rolling(20).mean()
    df["sma50"] = df["close"].rolling(50).mean()
    df["rsi"]   = rsi(df["close"])
    macd = ema(df["close"],12)-ema(df["close"],26)
    df["macd"] = macd; df["macd_sig"] = ema(macd,9)
    df["smi"], df["smi_sig"] = smi(df)
    df["atr"] = atr(df)
    df["vol_ma"] = df["volume"].rolling(VOL_WIN).mean()
    # pivots (swing highs/lows) for clusters
    L = SWING_LR
    sh = []; sl = []
    hi = df["high"].values; lo = df["low"].values
    for i in range(len(df)):
        if i < L or i >= len(df)-L:
            sh.append(np.nan); sl.append(np.nan); continue
        win_h = hi[i-L:i+L+1]; win_l = lo[i-L:i+L+1]
        sh.append(hi[i] if hi[i] == win_h.max() else np.nan)
        sl.append(lo[i] if lo[i] == win_l.min() else np.nan)
    df["swing_hi"] = sh; df["swing_lo"] = sl
    return df

def build_1h(df15):
    g = df15.set_index("date")
    o = g["open"].resample("1h").first()
    h = g["high"].resample("1h").max()
    l = g["low"].resample("1h").min()
    c = g["close"].resample("1h").last()
    v = g["volume"].resample("1h").sum()
    h1 = pd.DataFrame({"open":o,"high":h,"low":l,"close":c,"volume":v}).dropna().reset_index()
    h1["sma20"]=h1["close"].rolling(20).mean()
    h1["sma50"]=h1["close"].rolling(50).mean()
    h1["rsi"]=rsi(h1["close"])
    m=ema(h1["close"],12)-ema(h1["close"],26)
    h1["macd"]=m; h1["macd_sig"]=ema(m,9)
    h1["smi"],h1["smi_sig"]=smi(h1)
    return h1.set_index("date")

def attach_1h(df15, h1):
    # as-of merge: each 15m bar gets the most recent COMPLETED 1h bar
    h1r = h1.reset_index()
    h1r = h1r.rename(columns={c:f"h1_{c}" for c in h1r.columns if c!="date"})
    h1r["date"]=h1r["date"]+pd.Timedelta(hours=1)  # available only after close
    out = pd.merge_asof(df15.sort_values("date"), h1r.sort_values("date"),
                        on="date", direction="backward")
    return out


# ── confirmations / regime ─────────────────────────────────────────────
def _c15(r, d):
    if d=="CE":
        return (r["close"]>r["sma20"]) and (r["sma20"]>=r["sma50"]) and (r["rsi"]>50) and (r["macd"]>r["macd_sig"])
    return (r["close"]<r["sma20"]) and (r["sma20"]<=r["sma50"]) and (r["rsi"]<50) and (r["macd"]<r["macd_sig"])

def _c1h(r, d):
    if d=="CE":
        return (r.get("h1_close",np.nan)>r.get("h1_sma50",np.nan)) and (r.get("h1_rsi",50)>50) and (r.get("h1_macd",0)>r.get("h1_macd_sig",0))
    return (r.get("h1_close",np.nan)<r.get("h1_sma50",np.nan)) and (r.get("h1_rsi",50)<50) and (r.get("h1_macd",0)<r.get("h1_macd_sig",0))

def confirm(r, d, level):
    """level: none | regime | 15m | 1h | full"""
    if level=="none":   return True
    if level=="regime": return regime(r)==("BULL" if d=="CE" else "BEAR")
    if level=="15m":    return bool(_c15(r,d))
    if level=="1h":     return bool(_c1h(r,d))
    return bool(_c15(r,d) and _c1h(r,d))   # full

def regime(r):
    hc, hs = r.get("h1_close",np.nan), r.get("h1_sma50",np.nan)
    if pd.isna(hc) or pd.isna(hs): return "NEUTRAL"
    return "BULL" if hc>hs else "BEAR"

def compute_swings(df, lr):
    hi=df["high"].values; lo=df["low"].values; n=len(df)
    sh=np.full(n,np.nan); sl=np.full(n,np.nan)
    for i in range(lr, n-lr):
        if hi[i]==hi[i-lr:i+lr+1].max(): sh[i]=hi[i]
        if lo[i]==lo[i-lr:i+lr+1].min(): sl[i]=lo[i]
    return sh, sl


# ── signal generation ──────────────────────────────────────────────────
def gen_signals(df, cfg):
    """Return list of dicts: {i, ts, engine, dir, ...} per bar (pre-risk)."""
    body_min = cfg.get("E1_body", 65)
    ob = cfg.get("E2_ob", SMI_OB); os_ = cfg.get("E2_os", SMI_OS)
    e2conf = cfg.get("E2_confirm", "full")
    volmult = cfg.get("E3_volmult", VOL_MULT)
    lookback = cfg.get("E3_lookback", CLUSTER_LOOKBACK)
    lr = cfg.get("E3_lr", SWING_LR)
    sh_arr, sl_arr = compute_swings(df, lr)
    sigs = []
    sh_hist=[]; sl_hist=[]   # (bar_index, level)
    for i in range(1, len(df)):
        r = df.iloc[i]; p = df.iloc[i-1]
        if not np.isnan(sh_arr[i-1]): sh_hist.append((i-1, sh_arr[i-1]))
        if not np.isnan(sl_arr[i-1]): sl_hist.append((i-1, sl_arr[i-1]))
        if pd.isna(r["vwap"]) or pd.isna(r["smi"]) or pd.isna(r["sma50"]): continue
        reg = regime(r)
        bar = []

        # E1 VWAP — cross of daily VWAP on a strong body
        if cfg["E1"] and not pd.isna(r["body_pct"]) and r["body_pct"]>=body_min:
            if r["close"]>r["vwap"] and p["close"]<=p["vwap"]:
                bar.append(("E1","CE"))
            elif r["close"]<r["vwap"] and p["close"]>=p["vwap"]:
                bar.append(("E1","PE"))

        # E2 SMI cross at extreme + side-of-signal + confirmation
        if cfg["E2"]:
            cross_up = p["smi"]<=os_ and r["smi"]>os_ and r["smi"]>r["smi_sig"]
            cross_dn = p["smi"]>=ob  and r["smi"]<ob  and r["smi"]<r["smi_sig"]
            if cross_up and confirm(r,"CE",e2conf):
                bar.append(("E2","CE"))
            elif cross_dn and confirm(r,"PE",e2conf):
                bar.append(("E2","PE"))

        # E3 cluster break (nearest swing within lookback) + optional volume
        if cfg["E3"]:
            volok = (not cfg["E3_vol"]) or (not pd.isna(r["vol_ma"]) and r["volume"]>=volmult*r["vol_ma"])
            if volok:
                res = next((lv for (bi,lv) in reversed(sh_hist) if i-bi<=lookback), np.nan)
                sup = next((lv for (bi,lv) in reversed(sl_hist) if i-bi<=lookback), np.nan)
                if not np.isnan(res) and p["close"]<=res and r["close"]>res:
                    bar.append(("E3","CE"))
                elif not np.isnan(sup) and p["close"]>=sup and r["close"]<sup:
                    bar.append(("E3","PE"))

        for eng, d in bar:
            sigs.append({"i":i,"ts":r["date"],"engine":eng,"dir":d,
                         "close":r["close"],"reg":reg})
    return sigs


# ── exit simulation for one trade (in NIFTY points) ─────────────────────
def simulate_trade(df, entry_i, direction, engine, cfg):
    e = df.iloc[entry_i]
    entry = e["close"]; entry_ts = e["date"]
    a = e["atr"] if not pd.isna(e["atr"]) else 30.0
    if cfg["sl_mode"]=="atr":
        sl_dist = cfg["sl_atr"]*a
        arm     = cfg["arm_atr"]*a
    else:
        sl_dist = cfg["sl_pts"]
        arm     = cfg["arm_pts"]
    sl  = entry - sl_dist if direction=="CE" else entry + sl_dist
    peak = 0.0; armed=False; flip_armed=False
    fc = pd.to_datetime(FORCE_CLOSE).time()
    for j in range(entry_i+1, len(df)):
        b = df.iloc[j]
        if b["date"].date()!=entry_ts.date():
            # gap to next day shouldn't happen intraday; close at prev close
            px = df.iloc[j-1]["close"]
            return _pnl(entry,px,direction,engine,entry_ts,df.iloc[j-1]["date"],"EOD_GAP")
        # favorable excursion (peak) using bar extreme
        fav = (b["high"]-entry) if direction=="CE" else (entry-b["low"])
        peak = max(peak, fav)
        # SL breach (intrabar, conservative: SL checked before trail tighten)
        if direction=="CE" and b["low"]<=sl:
            return _pnl(entry,sl,direction,engine,entry_ts,b["date"],"SL" if not armed else "TRAIL")
        if direction=="PE" and b["high"]>=sl:
            return _pnl(entry,sl,direction,engine,entry_ts,b["date"],"SL" if not armed else "TRAIL")
        # E4 Path A: E2 winner aged >=30min, peaked >=+15, faded to <=+10, SMI reversed → exit+flip
        if cfg.get("E4") and engine=="E2":
            if peak>=15: flip_armed=True
            aged = (b["date"]-entry_ts).total_seconds()>=1800
            if flip_armed and aged and fav<=10:
                rev = (b["smi"]<b["smi_sig"]) if direction=="CE" else (b["smi"]>b["smi_sig"])
                if rev:
                    return _pnl(entry,b["close"],direction,engine,entry_ts,b["date"],"FLIP_A")
        # arm trail
        if not armed and peak>=arm:
            armed=True
        # trail update
        if armed:
            if engine=="E1":  # VWAP trail
                t = b["vwap"]
            else:             # SMA8 trail
                t = b["sma8"]
            if not pd.isna(t):
                if direction=="CE": sl=max(sl, t)
                else:               sl=min(sl, t)
        # force close
        if b["date"].time()>=fc:
            return _pnl(entry,b["close"],direction,engine,entry_ts,b["date"],"FORCE_CLOSE")
    last=df.iloc[-1]
    return _pnl(entry,last["close"],direction,engine,entry_ts,last["date"],"EOD")

def _pnl(entry,exit_px,direction,engine,ets,xts,reason):
    pts = (exit_px-entry) if direction=="CE" else (entry-exit_px)
    return {"engine":engine,"dir":direction,"entry":entry,"exit":exit_px,
            "pnl":pts,"entry_ts":ets,"exit_ts":xts,"reason":reason}


# ── full run with risk controls ─────────────────────────────────────────
def run(df, cfg, signals=None):
    if signals is None: signals = gen_signals(df, cfg)
    by_i = {}
    for s in signals: by_i.setdefault(s["i"], []).append(s)
    es = pd.to_datetime(ENTRY_START).time(); ee = pd.to_datetime(ENTRY_END).time()
    trades=[]; i=0; n=len(df)
    day=None; nonflip_losses=0; flips=0
    busy_until=-1
    while i<n:
        r=df.iloc[i]
        if r["date"].date()!=day:
            day=r["date"].date(); nonflip_losses=0; flips=0
        t=r["date"].time()
        if i>busy_until and es<=t<=ee and nonflip_losses<MAX_NONFLIP_LOSSES and i in by_i:
            # confluence: engines firing same dir this bar
            cands=by_i[i]
            dirs={}
            for s in cands: dirs.setdefault(s["dir"],[]).append(s["engine"])
            best_dir=max(dirs,key=lambda d:len(dirs[d]))
            engs=dirs[best_dir]
            conf=len(set(engs))
            eng = "E2" if "E2" in engs else ("E3" if "E3" in engs else "E1")
            tr=simulate_trade(df,i,best_dir,eng,cfg)
            tr["conf"]=conf; tr["engines"]="+".join(sorted(set(engs)))
            tr["entry_time"]=t; tr["is_flip"]=False
            trades.append(tr)
            if tr["pnl"]<0: nonflip_losses+=1
            exit_i=_xi(df,tr["exit_ts"],i)
            # ── E4 FLIP chain (piggybacks E2 only) ──
            if cfg.get("E4") and eng=="E2":
                cur_dir=best_dir; cur_exit_i=exit_i; cur_reason=tr["reason"]
                while flips<MAX_FLIPS:
                    fdir="PE" if cur_dir=="CE" else "CE"
                    fe=None
                    if cur_reason=="FLIP_A":
                        fe=cur_exit_i                          # Path A: immediate flip
                    else:                                       # Path B: SMI reversal within window
                        wb=max(1,cfg.get("flip_window_min",60)//15)
                        for j in range(cur_exit_i+1, min(cur_exit_i+1+wb,n)):
                            bj=df.iloc[j]; bp=df.iloc[j-1]
                            if bj["date"].date()!=df.iloc[cur_exit_i]["date"].date(): break
                            if not (es<=bj["date"].time()<=ee): continue
                            rev=(bj["smi"]>bj["smi_sig"] and bp["smi"]<=bp["smi_sig"]) if fdir=="CE" \
                                else (bj["smi"]<bj["smi_sig"] and bp["smi"]>=bp["smi_sig"])
                            if rev: fe=j; break
                    if fe is None: break
                    ftr=simulate_trade(df,fe,fdir,"E4",cfg)
                    ftr["conf"]=1; ftr["engines"]="E4"
                    ftr["entry_time"]=df.iloc[fe]["date"].time(); ftr["is_flip"]=True
                    trades.append(ftr); flips+=1
                    cur_dir=fdir; cur_reason=ftr["reason"]; cur_exit_i=_xi(df,ftr["exit_ts"],fe)
                busy_until=cur_exit_i
            else:
                busy_until=exit_i
            i=busy_until
        i+=1
    return pd.DataFrame(trades)

def _xi(df, ts, fallback):
    xi=df.index[df["date"]==ts]
    return int(xi[0]) if len(xi) else fallback+1


# ── reporting ───────────────────────────────────────────────────────────
def stats(tr, label=""):
    if len(tr)==0: return {"label":label,"n":0}
    wins=tr[tr.pnl>0]; losses=tr[tr.pnl<=0]
    eq=tr["pnl"].cumsum()
    dd=(eq-eq.cummax()).min()
    return {"label":label,"n":len(tr),"win%":round(100*len(wins)/len(tr),1),
            "avg_win":round(wins.pnl.mean() if len(wins) else 0,2),
            "avg_loss":round(losses.pnl.mean() if len(losses) else 0,2),
            "exp":round(tr.pnl.mean(),2),"net":round(tr.pnl.sum(),1),
            "maxDD":round(dd,1)}

def pp(rows):
    if isinstance(rows,dict): rows=[rows]
    df=pd.DataFrame(rows)
    print(df.to_string(index=False))


BASE_CFG = dict(E1=False,E2=False,E3=False,E4=False,E3_vol=True,
                E1_body=65,E2_ob=SMI_OB,E2_os=SMI_OS,E2_confirm="full",
                E3_lr=SWING_LR,E3_lookback=CLUSTER_LOOKBACK,E3_volmult=VOL_MULT,
                flip_window_min=60,
                sl_mode="atr",sl_atr=1.0,arm_atr=0.6,sl_pts=30.0,arm_pts=20.0)

SL_GRID=[("atr",1.0,0.6),("atr",1.2,0.8),("atr",1.5,1.0),("pts",30,20),("pts",40,25)]

def with_sl(cfg, sl):
    mode,d,arm=sl
    return dict(cfg,sl_mode=mode,sl_atr=d,arm_atr=arm,sl_pts=d,arm_pts=arm)

def best_of(rows, min_n=8):
    cand=[r for r in rows if r["n"]>=min_n]
    if not cand: cand=[r for r in rows if r["n"]>0]
    return max(cand, key=lambda r:(r["exp"], r["net"])) if cand else None

def tune_engine(df, name, base, grid):
    """grid: list of (label_suffix, cfg_overrides). Returns (best_row, best_cfg)."""
    rows=[]; cfgs={}
    for suf,ov in grid:
        for sl in SL_GRID:
            c=with_sl(dict(base,**ov), sl)
            lab=f"{name} {suf} | {sl[0]} {sl[1]}/{sl[2]}"
            t=run(df,c)
            if len(t)==0:
                sub=t
            else:
                sub=t[t.engine==name] if name!="E4" else t[t.is_flip==True]
            st=stats(sub,lab); rows.append(st); cfgs[lab]=c
    rows=[r for r in rows if r["n"]>0]
    rows.sort(key=lambda r:r["net"],reverse=True)
    print(f"\n=== TUNE {name} (top 8 by net) ===")
    pp(rows[:8])
    b=best_of(rows)
    print(f"PICK {name}: {b['label']}  (n={b['n']} exp={b['exp']} net={b['net']})")
    return b, cfgs[b["label"]]

def main():
    df=fetch_15m(); df=add_indicators(df); h1=build_1h(df); df=attach_1h(df,h1)
    print(f"\nData: {df['date'].min()} -> {df['date'].max()}  ({df['d'].nunique()} days, {len(df)} bars)")

    picks={}
    # ── E1 VWAP: tune body% ──
    g1=[(f"body{b}",dict(E1=True,E1_body=b)) for b in [50,55,60,65,70]]
    b1,c1=tune_engine(df,"E1",BASE_CFG,g1); picks["E1"]=c1

    # ── E2 SMI: tune OB/OS + confirmation strictness ──
    obos=[("63/-37",dict(E2_ob=63,E2_os=-37)),("50/-50",dict(E2_ob=50,E2_os=-50)),
          ("40/-40",dict(E2_ob=40,E2_os=-40)),("70/-30",dict(E2_ob=70,E2_os=-30))]
    g2=[(f"{lab} cf={cf}", dict(E2=True,**ov,E2_confirm=cf))
        for lab,ov in obos for cf in ["none","regime","1h","full"]]
    b2,c2=tune_engine(df,"E2",BASE_CFG,g2); picks["E2"]=c2

    # ── E3 cluster: tune swing lr + lookback + volume mult (incl off) ──
    g3=[]
    for lr in [2,3,5]:
        for lb in [10,20,30]:
            g3.append((f"lr{lr} lb{lb} volOFF", dict(E3=True,E3_lr=lr,E3_lookback=lb,E3_vol=False)))
            for vm in [1.2,1.5,2.0]:
                g3.append((f"lr{lr} lb{lb} vol{vm}", dict(E3=True,E3_lr=lr,E3_lookback=lb,E3_vol=True,E3_volmult=vm)))
    b3,c3=tune_engine(df,"E3",BASE_CFG,g3); picks["E3"]=c3

    # ── E4 FLIP: piggyback best E2, tune flip window ──
    print("\n=== TUNE E4 (flip window, on best E2) ===")
    rows=[]
    for fw in [30,45,60,90]:
        c=dict(c2,E4=True,flip_window_min=fw)
        t=run(df,c); rows.append(stats(t[t.is_flip==True],f"E4 win={fw}m"))
    rows=[r for r in rows if r["n"]>0]
    if rows:
        pp(rows); b4=max(rows,key=lambda r:r["net"])
        fw4=int(b4["label"].split("=")[1].rstrip("m")); print(f"PICK E4: {b4['label']}")
    else:
        fw4=60; print("E4: no flip trades generated at any window (E2 too rare).")

    # ── ASSEMBLE best-of-all (engine enables OR'd, each engine its own tuned params) ──
    # one shared SL must be chosen for the combined run → take E1's (it dominates volume)
    sl=(c1["sl_mode"], c1["sl_atr"] if c1["sl_mode"]=="atr" else c1["sl_pts"],
        c1["arm_atr"] if c1["sl_mode"]=="atr" else c1["arm_pts"])
    combo=with_sl(dict(BASE_CFG,
        E1=True,E1_body=c1["E1_body"],
        E2=True,E2_ob=c2["E2_ob"],E2_os=c2["E2_os"],E2_confirm=c2["E2_confirm"],
        E3=True,E3_lr=c3["E3_lr"],E3_lookback=c3["E3_lookback"],
        E3_vol=c3["E3_vol"],E3_volmult=c3["E3_volmult"],
        E4=True,flip_window_min=fw4), sl)
    tr=run(df,combo)
    print("\n"+"="*60)
    print("=== BEST COMBINED (all engines, each tuned) ===")
    print("config:",{k:combo[k] for k in ["E1_body","E2_ob","E2_os","E2_confirm",
          "E3_lr","E3_lookback","E3_vol","E3_volmult","flip_window_min",
          "sl_mode","sl_atr","arm_atr","sl_pts","arm_pts"]})
    pp(stats(tr,"COMBINED"))
    print()
    pp([stats(tr[tr.engine==e],e) for e in ["E1","E2","E3","E4"] if len(tr[tr.engine==e])])
    pp([stats(tr[tr.is_flip==False],"non-flip"),stats(tr[tr.is_flip==True],"flips")])

    # ── retained test scenarios on the tuned combined book ──
    print("\n=== TEST 3 — entries before vs after 13:30 (tuned) ===")
    cut=dt.time(13,30)
    pp([stats(tr[tr.entry_time<cut],"before 13:30"),stats(tr[tr.entry_time>=cut],"after 13:30")])
    print("\n=== TEST 4 — single vs 2+ confluence (tuned) ===")
    pp([stats(tr[tr.conf<=1],"single"),stats(tr[tr.conf>=2],"confluence 2+")])

    tr.to_csv(os.path.join(BASE,"orion_v2514_trades.csv"),index=False)
    json.dump(combo, open(os.path.join(BASE,"orion_v2514_best_cfg.json"),"w"), indent=2, default=str)
    print("\ntrades -> orion_v2514_trades.csv | best cfg -> orion_v2514_best_cfg.json")

if __name__=="__main__":
    main()
