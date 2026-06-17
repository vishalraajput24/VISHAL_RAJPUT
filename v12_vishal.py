#!/usr/bin/env python3
"""
v12_vishal.py — V12 VISHAL RAJPUT PAPER engine (5-min, option-only).
================================================================================
Forward-paper validation of the FINAL 5-minute V12 strategy (owner-approved
2026-06-16). NOT "ORION" — that 3-engine math is just imported; this is the V12
Vishal Rajput engine. Brands every alert "V12 Vishal Rajput".

SIGNAL (5m NIFTY near-month FUTURE, validated in screener/orion_v2514_backtest):
  • E2 (SMI cross) = the ONLY trigger. E3 REMOVED 2026-06-17 (flow study:
    exp -1.03, the weak engine; made 6 of 8 losers on 06-17).
  • E1 (VWAP) = CONFIRM / CONVICTION only — never opens a trade
  • FLOW GATE (2026-06-17, screener/v12_flow_divergence_study.py): an E2
    trigger is VETOED if the move is hollow — L1 effort-vs-result (below-avg
    volume push / rejection wick) OR L2 A/D divergence (new 20-bar price
    extreme not confirmed by the intraday Accumulation/Distribution line).
    Self-calibrating percentiles, NO ADX / no fixed bands. In-sample edge:
    E2 + flow-veto exp +15.7 vs +1.29 baseline. PAPER forward-validation.
  • SMI period 30 (5m-tuned), bands 35/-35, E4 off
  • window 09:30-14:45, one position at a time, max 5 losing days-trades

EXECUTION — OPTION-ONLY (futures trade leg removed 2026-06-17, owner: focus = option):
  • OPTION leg : 1 ATM weekly option, P&L in premium points. SL entry_prem-22,
                 arm +8, then trail peak-6. (defined risk; ~12% of the move)
The NIFTY future is still the SIGNAL source + ATM-strike reference; it is no
longer traded as a parallel leg. PAPER ONLY — never places a broker order,
never touches V11 state.

Run: cron ~09:25 Mon-Fri; self-exits after 15:30.
"""
import os, sys, json, time, csv, datetime as dt
import pandas as pd, numpy as np, requests

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "screener"))
import VRL_MAIN as V                         # infra: strike/expiry/option-1min
import orion_v2514_backtest as OB            # validated indicators + engines
from kiteconnect import KiteConnect

STATE_FILE = os.path.join(HERE, "state", "v12_vishal_state.json")
LOG_CSV    = os.path.join(os.path.expanduser("~/lab_data"), "v12_vishal_log.csv")
CFG_FILE   = os.path.join(HERE, "screener", "orion_v2514_best_cfg.json")
ENV_FILE   = os.path.expanduser("~/.env")

ENTRY_START = dt.time(9, 30)
ENTRY_END   = dt.time(14, 45)
FORCE_CLOSE = dt.time(15, 25)
POLL_SEC    = 30
MAX_LOSSES  = 5                              # losing option-trades/day → stop entries

# 5m-tuned signal + exit constants
SMI_PERIOD  = 30
VOL_WIN     = 20
E2_OB, E2_OS = 35.0, -35.0
OPT_SL, OPT_ARM, OPT_GAP = 22.0, 8.0, 6.0   # option premium-point exit

# ---------- env / telegram ----------
def _env():
    e = {}
    try:
        for ln in open(ENV_FILE):
            ln = ln.strip()
            if "=" in ln and not ln.startswith("#"):
                k, v = ln.split("=", 1); e[k] = v.strip().strip('"').strip("'")
    except Exception: pass
    return e
_ENV = _env()

def tg(text):
    tok = _ENV.get("TG_TOKEN"); cid = _ENV.get("TG_GROUP_ID")
    if not tok or not cid: return
    try:
        requests.post(f"https://api.telegram.org/bot{tok}/sendMessage",
                      json={"chat_id": cid, "text": text, "disable_web_page_preview": True},
                      timeout=20)
    except Exception: pass

def log(*a): print("[V12-VR]", *a, flush=True)

# ---------- state ----------
def fresh_state(today):
    return dict(date=today, in_trade=False, engine="", direction="", strike=0,
                symbol="", token=0, entry_time="", conf=1, e1_agree=False, atr=0.0,
                # option leg (the trade — futures leg removed 2026-06-17, option-only engine)
                opt_open=False, entry_prem=0.0, prem_sl=0.0,
                peak_prem=0.0, mae_prem=0.0, armed_prem=False, arm_time_opt="",
                opt_pnl=0.0,
                # day counters
                losses=0, trades=0, opt_day=0.0)

def load_state(today):
    try:
        s = json.load(open(STATE_FILE))
        if s.get("date") == today:
            base = fresh_state(today); base.update(s)   # backfill new keys on schema bump
            return base
    except Exception: pass
    return fresh_state(today)

def save_state(s):
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    json.dump(s, open(STATE_FILE, "w"), indent=2, default=str)

def log_trade(row):
    new = not os.path.isfile(LOG_CSV)
    with open(LOG_CSV, "a", newline="") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["date","engine","direction","strike","symbol","conf","e1_agree",
                        "entry_time","exit_time","leg","entry","exit","pnl_pts","reason",
                        "peak_pts","mae_pts","atr_entry","armed","arm_time","hold_min"])
        w.writerow(row)

def hold_min(entry_t, now):
    """Minutes held: entry_time 'HH:MM:SS' string vs now datetime."""
    try:
        h, m, s = (int(x) for x in entry_t.split(":"))
        e = now.replace(hour=h, minute=m, second=s, microsecond=0)
        return round((now - e).total_seconds() / 60.0, 1)
    except Exception:
        return 0.0

# ---------- kite / data ----------
def connect():
    tok = json.load(open(os.path.join(HERE, "state", "access_token.json")))["access_token"]
    k = KiteConnect(api_key=_ENV["KITE_API_KEY"]); k.set_access_token(tok)
    V._kite = k
    return k

_FUT = {}
def fut_token(kite):
    if _FUT: return _FUT["token"]
    inst = kite.instruments("NFO")
    f = sorted([i for i in inst if i["name"] == "NIFTY" and i["instrument_type"] == "FUT"],
               key=lambda x: x["expiry"])[0]
    _FUT["token"] = f["instrument_token"]; _FUT["sym"] = f["tradingsymbol"]
    return _FUT["token"]

def add_flow_features(df):
    """Flow layers for the entry gate (study screener/v12_flow_divergence_study.py):
       L1 effort-vs-result (volx / approach / rejection wick),
       L2 intraday A/D line + 20-bar price-vs-A/D divergence. Self-calibrating —
       no ADX, no fixed bands. NaN-safe (rolling warmup)."""
    df["volx"] = df["volume"] / df["vol_ma"]
    g = df.groupby("d", group_keys=False)
    df["approach_volx"] = g["volx"].transform(lambda s: s.shift(1).rolling(5).mean())
    rng = (df["high"] - df["low"]).replace(0, np.nan)
    df["close_pos"] = (df["close"] - df["low"]) / rng
    mfm = (((df["close"] - df["low"]) - (df["high"] - df["close"])) / rng).fillna(0)
    df["ad"] = (mfm * df["volume"]).groupby(df["d"]).cumsum()
    df["px_hi20"] = g["close"].transform(lambda s: s.rolling(20).max())
    df["px_lo20"] = g["close"].transform(lambda s: s.rolling(20).min())
    df["ad_hi20"] = g["ad"].transform(lambda s: s.rolling(20).max())
    df["ad_lo20"] = g["ad"].transform(lambda s: s.rolling(20).min())
    return df

def flow_veto(df, bar, direction):
    """True if the trigger is a hollow move that historically faded (L1 OR L2).
       Thresholds = percentiles of the loaded window's own distribution."""
    volx_lo = df["volx"].quantile(0.45)
    appr_lo = df["approach_volx"].quantile(0.50)
    quiet = (bar["volx"] <= volx_lo) and \
            (not pd.isna(bar["approach_volx"]) and bar["approach_volx"] <= appr_lo)
    rej = (bar["close_pos"] <= 0.40) if direction == "CE" else (bar["close_pos"] >= 0.60)
    l1 = bool(quiet or rej)
    if direction == "CE":
        l2 = bool(bar["close"] >= bar["px_hi20"] and bar["ad"] < bar["ad_hi20"])
    else:
        l2 = bool(bar["close"] <= bar["px_lo20"] and bar["ad"] > bar["ad_lo20"])
    return l1, l2, bool(l1 or l2)

def fut_5m(kite):
    """Fresh 5m future candles, indicators with SMI period 30 + vol_ma 20 (5m-tuned)."""
    tok = fut_token(kite)
    to = dt.date.today(); frm = to - dt.timedelta(days=6)
    d = kite.historical_data(tok, frm, to, "5minute")
    df = pd.DataFrame(d); df["date"] = pd.to_datetime(df["date"]).dt.tz_localize(None)
    df = OB.add_indicators(df)
    df["smi"], df["smi_sig"] = OB.smi(df, k=SMI_PERIOD)      # override 15m default (10)
    df["vol_ma"] = df["volume"].rolling(VOL_WIN).mean()
    df = add_flow_features(df)                              # L1/L2 flow gate columns
    df = OB.attach_1h(df, OB.build_1h(df))                  # E2 confirm=none → 1h unused
    return df

def fut_ltp(kite):
    try:
        q = kite.ltp([_FUT["token"]]); return float(list(q.values())[0]["last_price"])
    except Exception:
        return 0.0

def option_prem(token):
    try:
        df = V.get_option_1min(int(token), 3)
        if df is not None and len(df): return float(df.iloc[-1]["close"])
    except Exception: pass
    return 0.0

# ---------- config ----------
def load_cfg():
    c = json.load(open(CFG_FILE))
    c["E1"] = False                     # E1 confirm-only — never triggers
    c["E2"] = True; c["E2_ob"] = E2_OB; c["E2_os"] = E2_OS; c["E2_confirm"] = "none"
    c["E3"] = False                     # REMOVED 2026-06-17 (flow study: exp -1.03, the weak engine)
    c["E4"] = False
    return c
CFG = load_cfg()

# ---------- main loop ----------
def main():
    today = dt.date.today().isoformat()
    s = load_state(today)
    kite = connect()
    expiry = V.get_nearest_expiry(kite)
    dte = V.calculate_dte(expiry) if expiry else 0
    log(f"start {today} expiry={expiry} dte={dte} SMI{SMI_PERIOD} bands{E2_OB}/{E2_OS}")
    tg(f"🟢 V12 Vishal Rajput paper started {today} · expiry {expiry} · "
       f"5m E2 + FLOW-GATE (effort/A-D-div veto, E3 removed) · "
       f"OPTION-ONLY (ATM weekly, SL-22/arm+8/trail-6)")

    last_bar_ts = None
    while True:
        now = dt.datetime.now()
        if now.time() >= dt.time(15, 30):
            break
        try:
            df = fut_5m(kite)
            closed = df[df["date"] + pd.Timedelta(minutes=5) <= now]
            if len(closed) < 51:
                time.sleep(POLL_SEC); continue
            bar = closed.iloc[-1]; bar_ts = bar["date"]
            fut_now = fut_ltp(kite) or float(bar["close"])

            # ---------- MANAGE OPEN LEGS (priority over entry) ----------
            if s["in_trade"]:
                direction = s["direction"]; eng = s["engine"]
                forced = now.time() >= FORCE_CLOSE

                # --- OPTION leg (the only trade) ---
                if s["opt_open"]:
                    prem = option_prem(s["token"]) or s["entry_prem"]
                    fav = prem - s["entry_prem"]
                    s["peak_prem"] = max(s["peak_prem"], fav)
                    s["mae_prem"] = min(s["mae_prem"], fav)
                    if not s["armed_prem"] and s["peak_prem"] >= OPT_ARM:
                        s["armed_prem"] = True; s["arm_time_opt"] = now.strftime("%H:%M:%S")
                    if s["armed_prem"]:
                        s["prem_sl"] = max(s["prem_sl"], s["entry_prem"] + s["peak_prem"] - OPT_GAP)
                    rsn = None
                    if prem <= s["prem_sl"]: rsn = "TRAIL" if s["armed_prem"] else "SL"
                    if forced: rsn = "FORCE_CLOSE"
                    if rsn:
                        pnl = prem - s["entry_prem"]
                        s["opt_open"] = False; s["opt_pnl"] = pnl; s["opt_day"] += pnl
                        s["trades"] += 1
                        if pnl < 0: s["losses"] += 1
                        log_trade([today, eng, direction, s["strike"], s["symbol"], s["conf"],
                                   s["e1_agree"], s["entry_time"], now.strftime("%H:%M:%S"),
                                   "OPT", round(s["entry_prem"],1), round(prem,1),
                                   round(pnl,1), rsn,
                                   round(s["peak_prem"],1), round(s["mae_prem"],1),
                                   round(s["atr"],1), s["armed_prem"], s["arm_time_opt"],
                                   hold_min(s["entry_time"], now)])
                        tg(f"🔴 V12 VR OPT EXIT {eng} {direction} {s['strike']} {rsn} · "
                           f"prem {s['entry_prem']:.1f}→{prem:.1f} = {pnl:+.1f} · "
                           f"day opt {s['opt_day']:+.1f}")

                if not s["opt_open"]:
                    keep = dict(s); k2 = fresh_state(today)
                    k2.update(losses=keep["losses"], trades=keep["trades"],
                              opt_day=keep["opt_day"])
                    s = k2
                save_state(s); time.sleep(POLL_SEC); continue

            # ---------- ENTRY (once per new closed bar) ----------
            if bar_ts == last_bar_ts:
                time.sleep(POLL_SEC); continue
            last_bar_ts = bar_ts
            if not (ENTRY_START <= now.time() <= ENTRY_END):
                save_state(s); time.sleep(POLL_SEC); continue
            if s["losses"] >= MAX_LOSSES:
                save_state(s); time.sleep(POLL_SEC); continue

            sigs = OB.gen_signals(df, CFG)
            bi = int(df.index[df["date"] == bar_ts][0])
            hit = [g for g in sigs if g["i"] == bi]            # E2/E3 only (E1 off in CFG)
            if not hit:
                save_state(s); time.sleep(POLL_SEC); continue
            # pick direction by engine agreement; E2 > E3 priority
            dirs = {}
            for g in hit: dirs.setdefault(g["dir"], set()).add(g["engine"])
            direction = max(dirs, key=lambda d: len(dirs[d]))
            engs = dirs[direction]
            eng = "E2" if "E2" in engs else "E3"
            conf = len(engs)
            # E1 confirm: does price sit on the trade's side of VWAP?
            e1_agree = bool((direction == "CE" and bar["close"] > bar["vwap"]) or
                            (direction == "PE" and bar["close"] < bar["vwap"]))
            a = float(bar["atr"]) if not pd.isna(bar["atr"]) else 30.0

            # ---------- FLOW GATE (effort-vs-result + A/D divergence) ----------
            l1, l2, veto = flow_veto(closed, bar, direction)
            if veto:
                why = "+".join(n for n, f in (("L1noeffort", l1), ("L2_ADdiv", l2)) if f)
                log(f"FLOW-SKIP {eng} {direction} {why} @ {bar_ts.time()} "
                    f"volx={bar['volx']:.2f} close_pos={bar['close_pos']:.2f}")
                tg(f"⚪ V12 VR FLOW-SKIP {eng} {direction} [{why}] · hollow move, no entry")
                save_state(s); time.sleep(POLL_SEC); continue

            strike = V.resolve_strike_for_direction(fut_now, direction, dte)
            toks = V.get_option_tokens(kite, strike, expiry)
            leg = toks.get(direction)
            if not leg:
                log(f"no {direction} token for {strike}"); save_state(s); time.sleep(POLL_SEC); continue
            token = leg["token"]; symbol = leg.get("symbol", "") or leg.get("tradingsymbol", "")
            prem = option_prem(token)
            if prem <= 0:
                log(f"no premium for {symbol}"); save_state(s); time.sleep(POLL_SEC); continue

            s.update(in_trade=True, engine=eng, direction=direction, strike=strike,
                     symbol=symbol, token=token, entry_time=now.strftime("%H:%M:%S"),
                     conf=conf, e1_agree=e1_agree, atr=a,
                     opt_open=True, entry_prem=prem, prem_sl=prem - OPT_SL,
                     peak_prem=0.0, mae_prem=0.0, armed_prem=False, arm_time_opt="",
                     opt_pnl=0.0)
            save_state(s)
            tag = f"conf{conf}" + ("+E1" if e1_agree else "")
            tg(f"🟢 V12 VR ENTRY {eng} {direction} {strike} [{tag}] · "
               f"opt prem {prem:.1f} (SL {prem-OPT_SL:.1f})")
            log(f"ENTRY {eng} {direction} {strike} {tag} prem={prem:.1f} fut={fut_now:.0f}")
        except Exception as e:
            log("loop error:", type(e).__name__, str(e)[:160])
        time.sleep(POLL_SEC)

    # EOD summary
    tg(f"🏁 V12 Vishal Rajput EOD {today} · trades {s['trades']} · losses {s['losses']} · "
       f"OPTION {s['opt_day']:+.1f} pts")
    log("EOD done")

if __name__ == "__main__":
    main()
