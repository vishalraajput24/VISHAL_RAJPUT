#!/usr/bin/env python3
"""
paper_wide.py — INDEPENDENT wide-window paper engine (data collection only).
============================================================================
Purpose (owner, 2026-06-15): the LIVE bot trades the disciplined 10:00-14:30
window. This standalone process paper-trades the WIDE 09:30-15:15 window with
EVERY V11 signal, fully independent of live, SILENT all day, and sends ONE
Telegram summary at EOD. At the ~06-25 review we A/B the wide window vs live's
narrow one to see what the extra hours (09:30-10:00 + 14:30-15:15) cost/earn.

ZERO STRATEGY DIVERGENCE: this imports VRL_MAIN and calls its REAL gate
functions (get_option_1min → ema9 bands, the same MOMENTUM/OPP-DECAY math, and
_v11_compute_trail_sl — the exact live ladder). It never touches live state,
never places a broker order, never sends an intraday alert.

Run: cron at ~09:25 Mon-Fri; the process self-exits after the EOD summary.
Fidelity note (owner-accepted): polls ~15s on the forming 1-min candle (not the
live ~1s tick). Plenty for data collection; not a tick-perfect mirror.
"""
import os, sys, json, time, csv, datetime as dt
import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import VRL_MAIN as V                      # side-effect-free import (verified: no auto-connect)
from kiteconnect import KiteConnect

LAB        = os.path.expanduser("~/lab_data")
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state", "paper_wide_state.json")
LOG_CSV    = os.path.join(LAB, "paper_wide_log.csv")
ENV_FILE   = os.path.expanduser("~/.env")

WIN_START  = dt.time(9, 30)              # wide window — entries allowed from
WIN_END    = dt.time(15, 15)            # last entry / EOD force-close
POLL_SEC   = 15
SAME_SIDE_BLOCK_SEC = 180

# ---------- tiny utils ----------
def _now(): return dt.datetime.now()
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
                      json={"chat_id": cid, "text": text, "parse_mode": "HTML",
                            "disable_web_page_preview": True}, timeout=20)
    except Exception: pass

def load_state():
    try:
        return json.load(open(STATE_FILE))
    except Exception:
        return {}
def save_state(s):
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    json.dump(s, open(STATE_FILE, "w"), indent=2, default=str)

def fresh_day(today):
    return dict(date=today, in_trade=False, direction="", token=0, strike=0,
                entry_price=0.0, entry_time="", initial_sl=0.0, peak_pnl=0.0,
                last_fired_ts="", last_exit_candle_ts="", last_exit_dir="",
                last_exit_unix=0.0, trades=0, wins=0, losses=0, eod_sent=False)

# ---------- auth (silent — read saved token directly, no get_kite TG) ----------
def connect():
    saved = json.load(open(V.TOKEN_FILE_PATH))
    if saved.get("date") != dt.date.today().isoformat() or not saved.get("access_token"):
        raise RuntimeError("no fresh Kite token for today")
    k = KiteConnect(api_key=V.KITE_API_KEY)
    k.set_access_token(saved["access_token"])
    k.profile()                          # validate
    V._kite = k                          # so V.get_option_1min/get_option_tokens work
    return k

# paper_wide runs no WebSocket, so V.get_spot_ltp() (WS tick cache) is always 0
# here — which made resolve_strike_for_direction() return strike 0 and spam
# "[DATA] Token resolve incomplete: strike=0" twice every 15s. Read spot via REST.
def spot_ltp_rest(kite):
    try:
        q = kite.ltp("NSE:NIFTY 50")
        return float(list(q.values())[0]["last_price"])
    except Exception:
        return 0.0

# ---------- gate evaluation (reuses V's exact candle/ema math) ----------
def leg(token, n=100):
    df = V.get_option_1min(int(token), n)
    if df is None or len(df) < 5: return None
    comp = df.iloc[-2]; form = df.iloc[-1]
    return dict(bk_ts=str(comp.name),
                close=float(comp["close"]),
                ema9h=float(comp.get("ema9_high", 0)),
                ema9l=float(comp.get("ema9_low", 0)),
                ltp=float(form["close"]), low=float(form["low"]), high=float(form["high"]))

def reason_for(tier):
    return {"INITIAL": "EMERGENCY_SL", "PROTECT": "PROTECT_2", "LOCK_4": "LOCK_4",
            "TRAIL_10": "VISHAL_TRAIL", "LOCK_25": "LOCK_25"}.get(tier, tier)

def log_trade(s, exit_price, pnl, reason):
    new = not os.path.exists(LOG_CSV)
    with open(LOG_CSV, "a", newline="") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["date","entry_time","exit_time","direction","strike",
                        "entry_price","exit_price","pnl_pts","peak_pnl","exit_reason"])
        w.writerow([s["date"], s["entry_time"], _now().strftime("%H:%M:%S"),
                    s["direction"], s["strike"], round(s["entry_price"],2),
                    round(exit_price,2), round(pnl,2), round(s["peak_pnl"],2), reason])

def do_exit(s, exit_price, reason):
    pnl = exit_price - s["entry_price"]
    log_trade(s, exit_price, pnl, reason)
    s["trades"] += 1
    if pnl > 0: s["wins"] += 1
    else: s["losses"] += 1
    s["in_trade"] = False
    s["last_exit_candle_ts"] = ""        # set by caller with current bk_ts
    s["last_exit_dir"] = s["direction"]
    s["last_exit_unix"] = time.time()

# ---------- EOD summary ----------
def send_eod(s):
    rows = []
    try:
        with open(LOG_CSV) as f:
            for r in csv.DictReader(f):
                if r["date"] == s["date"]: rows.append(r)
    except Exception: pass
    net = sum(float(r["pnl_pts"]) for r in rows)
    w = sum(1 for r in rows if float(r["pnl_pts"]) > 0)
    l = len(rows) - w
    best = max(rows, key=lambda r: float(r["pnl_pts"]), default=None)
    head = "📄 <b>PAPER (wide 09:30–15:15) — EOD</b>\n" + s["date"]
    if not rows:
        tg(head + "\n\nNo paper trades today."); return
    body = (f"\n\nTrades: <b>{len(rows)}</b>   W/L: <b>{w}/{l}</b>   "
            f"Net: <b>{net:+.1f} pts</b>\n")
    if best:
        body += (f"🏆 Best: {best['direction']} {best['strike']} "
                 f"{float(best['pnl_pts']):+.1f} ({best['exit_reason']})\n")
    body += "\n<i>Data-collection only — not live. vs live 10:00–14:30 window.</i>"
    tg(head + body)

# ---------- main loop ----------
def main():
    today = dt.date.today().isoformat()
    if not V.is_trading_day(_now()):
        return
    s = load_state()
    if s.get("date") != today:
        s = fresh_day(today)
        save_state(s)
    if s.get("eod_sent"):
        return

    try:
        kite = connect()
        expiry = V.get_nearest_expiry(kite)
        dte = V.calculate_dte(expiry)
    except Exception as e:
        tg(f"📄 PAPER (wide) could not start: {e}")
        return

    while True:
        now = _now()
        if now.time() >= WIN_END:
            break
        if now.time() < WIN_START:
            time.sleep(POLL_SEC); continue
        try:
            # ----- EXIT management (held position) -----
            if s["in_trade"]:
                d = leg(s["token"])
                if d:
                    s["peak_pnl"] = max(s["peak_pnl"], d["high"] - s["entry_price"])
                    sl, tier = V._v11_compute_trail_sl(s["entry_price"], s["peak_pnl"], s["initial_sl"])
                    if d["low"] <= sl:                       # SL/trail hit (intrabar)
                        do_exit(s, sl, reason_for(tier))
                        s["last_exit_candle_ts"] = d["bk_ts"]
                save_state(s)
                time.sleep(POLL_SEC); continue

            # ----- ENTRY scan (flat) -----
            spot = spot_ltp_rest(kite)
            if spot <= 0:                                # no REST quote — skip cycle
                time.sleep(POLL_SEC); continue
            ce_strike = V.resolve_strike_for_direction(spot, "CE", dte)
            pe_strike = V.resolve_strike_for_direction(spot, "PE", dte)
            toks = {}
            ce_t = V.get_option_tokens(kite, ce_strike, expiry)
            pe_t = V.get_option_tokens(kite, pe_strike, expiry)
            if ce_t.get("CE"): toks["CE"] = (ce_t["CE"]["token"], ce_strike)
            if pe_t.get("PE"): toks["PE"] = (pe_t["PE"]["token"], pe_strike)

            legs = {dirn: leg(tk) for dirn, (tk, _stk) in toks.items()}
            for dirn, (tk, stk) in toks.items():
                me = legs.get(dirn); opp = legs.get("PE" if dirn == "CE" else "CE")
                if not me or not opp: continue
                if me["ema9h"] <= 0 or opp["ema9l"] <= 0: continue
                momentum = me["close"] >= me["ema9h"] + V.V11_MIN_EMA9H_GAP
                opp_margin = round(opp["close"] - opp["ema9l"], 2)
                decay = (-8.0 <= opp_margin <= V.V11_DECAY_HIGH)
                # cooldowns (mirror live)
                if s["last_fired_ts"] == me["bk_ts"]: continue
                if s["last_exit_candle_ts"] == me["bk_ts"]: continue
                if (s["last_exit_dir"] == dirn and
                        time.time() - s["last_exit_unix"] < SAME_SIDE_BLOCK_SEC): continue
                if momentum and decay:
                    entry = me["close"]; ema9l = me["ema9l"]
                    initial_sl = (entry - 5.0) if ema9l >= entry else max(ema9l, entry - 10.0)
                    s.update(in_trade=True, direction=dirn, token=tk, strike=stk,
                             entry_price=entry, entry_time=now.strftime("%H:%M:%S"),
                             initial_sl=round(initial_sl, 2), peak_pnl=0.0,
                             last_fired_ts=me["bk_ts"])
                    save_state(s)
                    break
            save_state(s)
        except Exception:
            pass
        time.sleep(POLL_SEC)

    # ----- EOD: force-close any open trade, then ONE summary -----
    try:
        if s["in_trade"]:
            d = leg(s["token"])
            px = d["ltp"] if d else s["entry_price"]
            do_exit(s, px, "EOD_EXIT")
        send_eod(s)
        s["eod_sent"] = True
        save_state(s)
    except Exception as e:
        tg(f"📄 PAPER (wide) EOD error: {e}")

if __name__ == "__main__":
    main()
