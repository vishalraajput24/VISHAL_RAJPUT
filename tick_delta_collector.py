#!/usr/bin/env python3
"""
tick_delta_collector.py — STANDALONE order-flow / tick data collector.
================================================================================
Captures, per 1-minute bucket, the order-flow that the live bot currently throws
away (it subscribes MODE_FULL but keeps only last_price). DATA ONLY — never
trades, never touches V11/V12 state, never places an order.

For NIFTY current-month FUTURE + the ATM weekly CE & PE it records, per minute:
  • n_ticks               — how many ticks printed (CE-vs-PE tick count = owner's idea)
  • traded_vol            — volume traded in the minute (diff of cumulative volume)
  • buy_vol / sell_vol    — tick-rule classified (uptick vol vs downtick vol)
  • tick_delta            — buy_vol - sell_vol (per-minute order-flow imbalance)
  • cum_delta             — running daily sum of tick_delta (futures cumulative delta)
  • tbq / tsq             — total buy/sell qty in the book (depth pressure snapshot)

Output: ~/lab_data/tick_delta_log.csv   ·   log: ~/logs/tick_delta.log
Run:    cron 09:14 Mon-Fri; self-exits after 15:30.

STUDY GOAL (later, on collected data): does futures tick_delta / cum_delta, or
CE-minus-PE tick count, predict the NEXT-minute futures move? Test BEFORE any
trading is built on it. (Three prior studies show entry signals ~= coin flip;
this is the no-regret way to find out if order-flow is different — without risk.)
"""
import os, sys, json, csv, time, datetime as dt, threading
from kiteconnect import KiteConnect, KiteTicker

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import VRL_MAIN as V                      # reuse expiry / option-token helpers (no main() run)

LOG_CSV = os.path.expanduser("~/lab_data/tick_delta_log.csv")
LOG_TXT = os.path.expanduser("~/logs/tick_delta.log")
ENV_FILE = os.path.expanduser("~/.env")
RELOCK_PTS = 50                            # re-pick ATM if spot drifts this far

def log(*a):
    line = "[TICKΔ] " + " ".join(str(x) for x in a)
    print(line, flush=True)
    try:
        with open(LOG_TXT, "a") as f:
            f.write(time.strftime("%H:%M:%S ") + line + "\n")
    except Exception:
        pass

def _env():
    e = {}
    for ln in open(ENV_FILE):
        ln = ln.strip()
        if "=" in ln and not ln.startswith("#"):
            k, v = ln.split("=", 1); e[k] = v.strip().strip('"').strip("'")
    return e

# ---- shared state (guarded by _lock) ----
_lock = threading.Lock()
_cur_min = None                            # the minute bucket currently accumulating
_acc = {}                                  # token -> per-minute accumulator
_meta = {}                                 # token -> {"role","strike","last_price","last_vol"}
_cum_delta = {}                            # token -> running daily cum delta
_csv_inited = False

def _fresh(tok):
    return dict(n=0, vol=0, buy=0, sell=0, tbq=0, tsq=0, last_price=_meta[tok]["last_price"])

def _init_csv():
    global _csv_inited
    if _csv_inited or os.path.isfile(LOG_CSV):
        _csv_inited = True; return
    os.makedirs(os.path.dirname(LOG_CSV), exist_ok=True)
    with open(LOG_CSV, "w", newline="") as f:
        csv.writer(f).writerow([
            "minute", "fut_close", "fut_ticks", "fut_vol", "fut_delta", "fut_cumdelta",
            "fut_tbq", "fut_tsq",
            "ce_strike", "ce_close", "ce_ticks", "ce_vol", "ce_delta", "ce_tbq", "ce_tsq",
            "pe_strike", "pe_close", "pe_ticks", "pe_vol", "pe_delta", "pe_tbq", "pe_tsq",
            "ce_minus_pe_ticks", "ce_minus_pe_delta"])
    _csv_inited = True

def _flush(minute):
    """write the just-completed minute and reset accumulators."""
    _init_csv()
    rows = {}
    for tok, a in _acc.items():
        role = _meta[tok]["role"]
        rows[role] = (a, _meta[tok]["strike"])
    f = rows.get("FUT", ({}, 0))[0]; ce = rows.get("CE", ({}, 0)); pe = rows.get("PE", ({}, 0))
    fut_tok = next((t for t in _meta if _meta[t]["role"] == "FUT"), None)
    row = [
        minute.strftime("%Y-%m-%d %H:%M"),
        round(f.get("last_price", 0), 1), f.get("n", 0), f.get("vol", 0),
        f.get("buy", 0) - f.get("sell", 0), _cum_delta.get(fut_tok, 0),
        f.get("tbq", 0), f.get("tsq", 0),
        ce[1], round(ce[0].get("last_price", 0), 1), ce[0].get("n", 0), ce[0].get("vol", 0),
        ce[0].get("buy", 0) - ce[0].get("sell", 0), ce[0].get("tbq", 0), ce[0].get("tsq", 0),
        pe[1], round(pe[0].get("last_price", 0), 1), pe[0].get("n", 0), pe[0].get("vol", 0),
        pe[0].get("buy", 0) - pe[0].get("sell", 0), pe[0].get("tbq", 0), pe[0].get("tsq", 0),
        ce[0].get("n", 0) - pe[0].get("n", 0),
        (ce[0].get("buy", 0) - ce[0].get("sell", 0)) - (pe[0].get("buy", 0) - pe[0].get("sell", 0)),
    ]
    with open(LOG_CSV, "a", newline="") as fh:
        csv.writer(fh).writerow(row)
    log(f"min {minute.strftime('%H:%M')} | FUTΔ {row[4]:+d} cum {row[5]:+d} | "
        f"CE-PE ticks {row[22]:+d} Δ {row[23]:+d}")
    for tok in _acc:
        _acc[tok] = _fresh(tok)

def _on_ticks(ws, ticks):
    global _cur_min
    now_min = dt.datetime.now().replace(second=0, microsecond=0)
    with _lock:
        if _cur_min is None:
            _cur_min = now_min
            for tok in _meta:
                _acc.setdefault(tok, _fresh(tok))
        if now_min != _cur_min:
            _flush(_cur_min)
            _cur_min = now_min
        for tk in ticks:
            tok = tk.get("instrument_token")
            if tok not in _meta:
                continue
            lp = float(tk.get("last_price", 0) or 0)
            vt = int(tk.get("volume_traded", 0) or 0)
            a = _acc.setdefault(tok, _fresh(tok))
            a["n"] += 1
            dv = max(0, vt - _meta[tok]["last_vol"]) if _meta[tok]["last_vol"] else 0
            a["vol"] += dv
            prev = a["last_price"] or lp
            if lp > prev:
                a["buy"] += dv
            elif lp < prev:
                a["sell"] += dv
            # equal price -> unclassified (carry nothing)
            if _meta[tok]["role"] == "FUT":
                _cum_delta[tok] = _cum_delta.get(tok, 0) + (dv if lp > prev else -dv if lp < prev else 0)
            a["last_price"] = lp
            a["tbq"] = int(tk.get("total_buy_quantity", a["tbq"]) or a["tbq"])
            a["tsq"] = int(tk.get("total_sell_quantity", a["tsq"]) or a["tsq"])
            _meta[tok]["last_price"] = lp
            _meta[tok]["last_vol"] = vt

def _on_connect(ws, resp):
    toks = list(_meta.keys())
    ws.subscribe(toks); ws.set_mode(ws.MODE_FULL, toks)
    log("WS connected, subscribed", toks)

def _on_error(ws, code, reason):
    log("WS error", code, reason)

def resolve_tokens(kite):
    inst = kite.instruments("NFO")
    fut = sorted([i for i in inst if i["name"] == "NIFTY" and i["instrument_type"] == "FUT"],
                 key=lambda x: x["expiry"])[0]
    fut_tok = fut["instrument_token"]
    spot = kite.ltp([fut_tok])[str(fut_tok)]["last_price"] if False else \
        list(kite.ltp([fut_tok]).values())[0]["last_price"]
    atm = round(spot / 50) * 50
    expiry = V.get_nearest_expiry(kite)
    toks = V.get_option_tokens(kite, atm, expiry)
    _meta.clear()
    _meta[fut_tok] = {"role": "FUT", "strike": 0, "last_price": 0.0, "last_vol": 0}
    if toks.get("CE"):
        _meta[toks["CE"]["token"]] = {"role": "CE", "strike": atm, "last_price": 0.0, "last_vol": 0}
    if toks.get("PE"):
        _meta[toks["PE"]["token"]] = {"role": "PE", "strike": atm, "last_price": 0.0, "last_vol": 0}
    log(f"resolved FUT={fut_tok} spot={spot:.0f} ATM={atm} expiry={expiry} CE/PE={list(toks)}")
    return atm

def main():
    e = _env()
    tok = json.load(open(os.path.join(HERE, "state", "access_token.json")))["access_token"]
    kite = KiteConnect(api_key=e["KITE_API_KEY"]); kite.set_access_token(tok)
    V._kite = kite
    resolve_tokens(kite)
    kws = KiteTicker(e["KITE_API_KEY"], tok)
    kws.on_ticks = _on_ticks
    kws.on_connect = _on_connect
    kws.on_error = _on_error
    kws.connect(threaded=True)
    log("collector started")
    while dt.datetime.now().time() < dt.time(15, 30):
        time.sleep(5)
    with _lock:
        if _cur_min is not None:
            _flush(_cur_min)
    try:
        kws.close()
    except Exception:
        pass
    log("EOD done")

if __name__ == "__main__":
    main()
