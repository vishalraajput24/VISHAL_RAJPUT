#!/usr/bin/env python3
"""
v12_flow_divergence_study.py  —  READ-ONLY R&D (no live state, no orders).
===========================================================================
Root-cause study for the v12 mid-session losers (2026-06-17): E2 (SMI cross)
and E3 (cluster break) fire into NO-EFFORT balance EDGES and get faded.

Proposed filter — fully dynamic, session-adaptive, NO ADX / no fixed bands:
  L1  EFFORT-vs-RESULT  : trigger / approach volume vs price displacement
                          (new extreme on below-average volume = no-demand/
                           no-supply Wyckoff signature)
  L2  A/D DIVERGENCE    : price makes a new 20-bar extreme but the intraday
                          Accumulation/Distribution line does NOT confirm
                          (distribution under a high / accumulation under a low)
  L3  BALANCE vs EXPANSION regime : VWAP +/- rolling-sigma envelope + range
                          contraction. Inside a contracting balance => the
                          edge breakout is a fade => VETO.

All thresholds are PERCENTILES of the sample's own distribution (self-
calibrating, same spirit as the SMI-LOOSE per-stock p20/p80 gate) — not
constants. We measure whether the filter removes losers like today's three
while keeping winners like the 09:30 expansion-leg CE.

Run:  ~/kite_env/bin/python3 screener/v12_flow_divergence_study.py [--days 60]
"""
import os, sys, json, argparse, datetime as dt
import pandas as pd, numpy as np

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)
import orion_v2514_backtest as OB

CACHE = os.path.join(BASE, "v12_flow_5m_cache.csv")
SMI_PERIOD, VOL_WIN = 30, 20
ENTRY_START, ENTRY_END = dt.time(9, 30), dt.time(14, 45)


# ── data ──────────────────────────────────────────────────────────────────
def fetch_5m(days, force=False):
    if os.path.isfile(CACHE) and not force:
        df = pd.read_csv(CACHE, parse_dates=["date"])
        if (dt.date.today() - df["date"].max().date()).days <= 1:
            return df
    OB.load_env()
    from kiteconnect import KiteConnect
    tok = json.load(open(OB.TOKEN_FILE))["access_token"]
    k = KiteConnect(api_key=os.environ["KITE_API_KEY"]); k.set_access_token(tok)
    inst = k.instruments("NFO")
    fut = sorted([i for i in inst if i["name"] == "NIFTY" and i["instrument_type"] == "FUT"],
                 key=lambda x: x["expiry"])[0]
    to = dt.date.today(); frm = to - dt.timedelta(days=days)
    rows = []
    cur = frm
    while cur < to:                                  # 5m history is capped per request
        nxt = min(cur + dt.timedelta(days=55), to)
        rows += k.historical_data(fut["instrument_token"], cur, nxt, "5minute")
        cur = nxt + dt.timedelta(days=1)
    df = pd.DataFrame(rows).drop_duplicates("date")
    df["date"] = pd.to_datetime(df["date"]).dt.tz_localize(None)
    df.to_csv(CACHE, index=False)
    print(f"fetched {fut['tradingsymbol']} {len(df)} 5m bars -> {CACHE}")
    return df


# ── flow features (the three dynamic layers) ───────────────────────────────
def add_flow(df):
    df = OB.add_indicators(df)
    df["smi"], df["smi_sig"] = OB.smi(df, k=SMI_PERIOD)     # v12 5m tuning
    df["vol_ma"] = df["volume"].rolling(VOL_WIN).mean()
    df["volx"] = df["volume"] / df["vol_ma"]
    g = df.groupby("d", group_keys=False)

    # L1 — effort vs result
    df["approach_volx"] = g["volx"].transform(lambda s: s.shift(1).rolling(5).mean())
    rng = (df["high"] - df["low"]).replace(0, np.nan)
    df["disp"] = (df["close"] - df["open"]).abs() / df["atr"]       # displacement in ATR
    df["close_pos"] = (df["close"] - df["low"]) / rng              # close in bar range

    # L2 — intraday Accumulation/Distribution line + 20-bar divergence
    mfm = (((df["close"] - df["low"]) - (df["high"] - df["close"])) / rng).fillna(0)
    df["ad"] = (mfm * df["volume"]).groupby(df["d"]).cumsum()
    df["px_hi20"] = g["close"].transform(lambda s: s.rolling(20).max())
    df["px_lo20"] = g["close"].transform(lambda s: s.rolling(20).min())
    df["ad_hi20"] = g["ad"].transform(lambda s: s.rolling(20).max())
    df["ad_lo20"] = g["ad"].transform(lambda s: s.rolling(20).min())

    # L3 — balance vs expansion: VWAP-sigma envelope + range contraction
    df["vwap_disp"] = df["close"] - df["vwap"]
    df["vwap_sig"] = g["vwap_disp"].transform(lambda s: s.expanding(min_periods=6).std())
    df["vwap_z"] = (df["vwap_disp"] / df["vwap_sig"]).replace([np.inf, -np.inf], np.nan)
    don = g["high"].transform(lambda s: s.rolling(20).max()) - \
          g["low"].transform(lambda s: s.rolling(20).min())
    df["don20"] = don
    df["don_med"] = g["don20"].transform(lambda s: s.expanding(min_periods=10).median())
    df["contraction"] = df["don20"] / df["don_med"]                # <1 = range shrinking
    return df


def tag_signal(r, direction, P):
    """Return (dict of layer flags, veto_bool) for a trigger bar Series r."""
    # L1 no-effort: new-extreme push but trigger AND approach both quiet
    quiet = (r["volx"] <= P["volx_lo"]) and (r["approach_volx"] <= P["appr_lo"])
    # rejection wick against the trade direction (close back inside the bar)
    rej = (r["close_pos"] <= 0.4) if direction == "CE" else (r["close_pos"] >= 0.6)
    l1_noeffort = bool(quiet or rej)

    # L2 A/D divergence at the extreme
    if direction == "CE":
        l2_div = bool(r["close"] >= r["px_hi20"] and r["ad"] < r["ad_hi20"])
    else:
        l2_div = bool(r["close"] <= r["px_lo20"] and r["ad"] > r["ad_lo20"])

    # L3 balance edge: inside a contracting range AND stretched to the edge
    at_edge = abs(r["vwap_z"]) >= P["z_hi"] if not pd.isna(r["vwap_z"]) else False
    balance = (r["contraction"] <= P["con_lo"]) if not pd.isna(r["contraction"]) else False
    l3_balance_edge = bool(at_edge and balance)

    flags = {"L1_noeffort": l1_noeffort, "L2_div": l2_div, "L3_bal_edge": l3_balance_edge}
    # VETO if any TWO of the three fire (conviction that the move is hollow)
    veto = sum(flags.values()) >= 2
    return flags, veto


# ── run ─────────────────────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=60)
    ap.add_argument("--force", action="store_true")
    a = ap.parse_args()

    raw = fetch_5m(a.days, a.force)
    df = add_flow(raw)
    df = OB.attach_1h(df, OB.build_1h(df))
    print(f"Data: {df['date'].min()} -> {df['date'].max()}  "
          f"({df['d'].nunique()} days, {len(df)} bars)")

    cfg = OB.with_sl(dict(OB.BASE_CFG, E1=False, E2=True, E2_ob=35, E2_os=-35,
                          E2_confirm="none", E3=True, E3_vol=False, E3_lookback=20),
                     ("atr", 1.0, 0.6))
    sigs = OB.gen_signals(df, cfg)
    sigs = [s for s in sigs if ENTRY_START <= s["ts"].time() <= ENTRY_END]

    # self-calibrating thresholds = percentiles of the signal-bar distribution
    SB = df.loc[[s["i"] for s in sigs]]
    P = dict(volx_lo=SB["volx"].quantile(.45), appr_lo=SB["approach_volx"].quantile(.50),
             z_hi=SB["vwap_z"].abs().quantile(.55), con_lo=SB["contraction"].quantile(.55))
    print("dynamic thresholds:", {k: round(v, 2) for k, v in P.items()})

    rows = []
    for s in sigs:
        r = df.iloc[s["i"]]
        flags, veto = tag_signal(r, s["dir"], P)
        tr = OB.simulate_trade(df, s["i"], s["dir"], s["engine"], cfg)
        rows.append({"ts": s["ts"], "eng": s["engine"], "dir": s["dir"],
                     "pnl": round(tr["pnl"], 1), **flags, "veto": veto})
    R = pd.DataFrame(rows)

    def st(d, lab):
        if not len(d): return {"bucket": lab, "n": 0}
        return {"bucket": lab, "n": len(d), "win%": round(100*(d.pnl > 0).mean(), 1),
                "exp": round(d.pnl.mean(), 2), "net": round(d.pnl.sum(), 1)}

    print("\n=== BASELINE (all E2/E3 signals, no filter) ===")
    OB.pp(st(R, "ALL"))
    print("\n=== EACH LAYER in isolation (signals it would VETO) ===")
    OB.pp([st(R[R.L1_noeffort], "L1 no-effort"), st(R[R.L2_div], "L2 A/D-div"),
           st(R[R.L3_bal_edge], "L3 balance-edge")])
    print("\n=== FILTER (veto if >=2 layers agree) ===")
    OB.pp([st(R[~R.veto], "KEPT"), st(R[R.veto], "VETOED")])
    OB.pp([st(R[(R.eng == 'E2')], "E2 all"), st(R[(R.eng == 'E2') & ~R.veto], "E2 kept"),
           st(R[(R.eng == 'E3')], "E3 all"), st(R[(R.eng == 'E3') & ~R.veto], "E3 kept")])

    today = R[R.ts.dt.date == dt.date.today()]
    if len(today):
        print("\n=== TODAY's signals (did the filter catch the losers?) ===")
        with pd.option_context("display.width", 200):
            print(today.assign(t=today.ts.dt.strftime("%H:%M")).drop(columns="ts").to_string(index=False))

    out = os.path.join(BASE, "v12_flow_divergence_trades.csv")
    R.to_csv(out, index=False)
    print(f"\ntrades -> {out}")


if __name__ == "__main__":
    main()
