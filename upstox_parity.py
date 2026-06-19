#!/usr/bin/env python3
"""
Upstox <-> Kite DATA PARITY harness (migration Phase 1 — 2026-06-19).

Replicates every VRL_MAIN market-data read on Upstox and diffs the values
against Kite, the source of truth today. STANDALONE / READ-ONLY — imports
VRL_MAIN only to reuse its REAL Kite functions; never places orders, never
touches live state, never starts the bot.

What it checks (today's session):
  1. SPOT 1-min candles      (get_historical_data NIFTY_SPOT_TOKEN "minute")
  2. SPOT LTP                (get_spot_ltp / kite.ltp)
  3. INDIA VIX               (get_vix / kite.quote)
  4. ATM OPTION 1-min candles + ema9_high/ema9_low  (get_option_1min — the GATE inputs)

Run:
    set -a && . ~/.env && set +a && ~/kite_env/bin/python3 upstox_parity.py
"""
import os, gzip, json, sys, datetime as dt
import urllib.request, urllib.parse
import requests
import pandas as pd
from kiteconnect import KiteConnect

import VRL_MAIN as V

UTOKEN = os.getenv("UPSTOX_ACCESS_TOKEN", "")
UHEAD  = {"Authorization": f"Bearer {UTOKEN}", "Accept": "application/json"}
UBASE  = "https://api.upstox.com"
SPOT_KEY = "NSE_INDEX|Nifty 50"
VIX_KEY  = "NSE_INDEX|India VIX"
INSTR_URL = "https://assets.upstox.com/market-quote/instruments/exchange/NSE.json.gz"

PASS, FAIL = "\033[92mPASS\033[0m", "\033[91mFAIL\033[0m"


# ---------- Kite (reuse live saved token, silent — same as paper_wide) ----------
def connect_kite():
    saved = json.load(open(V.TOKEN_FILE_PATH))
    if saved.get("date") != dt.date.today().isoformat() or not saved.get("access_token"):
        raise RuntimeError("no fresh Kite token for today")
    k = KiteConnect(api_key=V.KITE_API_KEY)
    k.set_access_token(saved["access_token"])
    k.profile()
    V._kite = k
    return k


# ---------- Upstox helpers ----------
def u_intraday(instrument_key, unit="minutes", interval="1"):
    """Today's intraday candles -> DataFrame[open,high,low,close,volume] indexed by IST string."""
    ek = urllib.parse.quote(instrument_key, safe="")
    url = f"{UBASE}/v3/historical-candle/intraday/{ek}/{unit}/{interval}"
    r = requests.get(url, headers=UHEAD, timeout=20); r.raise_for_status()
    candles = r.json().get("data", {}).get("candles", [])
    rows = []
    for c in candles:  # [ts,o,h,l,c,vol,oi] ; Upstox = newest-first
        ts = pd.to_datetime(c[0]).strftime("%Y-%m-%d %H:%M:%S")
        rows.append((ts, c[1], c[2], c[3], c[4], c[5]))
    df = pd.DataFrame(rows, columns=["timestamp", "open", "high", "low", "close", "volume"])
    df = df.iloc[::-1].set_index("timestamp")        # -> oldest-first like Kite
    return df.apply(pd.to_numeric, errors="coerce")


def u_ltp(keys):
    ek = ",".join(urllib.parse.quote(k, safe="") for k in keys)
    url = f"{UBASE}/v2/market-quote/ltp?instrument_key={ek}"
    r = requests.get(url, headers=UHEAD, timeout=20); r.raise_for_status()
    return r.json().get("data", {})


def kite_df_to_iststr(df):
    """VRL get_historical_data returns tz-aware index -> normalize to IST string keys."""
    d = df.copy()
    d.index = pd.to_datetime(d.index).strftime("%Y-%m-%d %H:%M:%S")
    return d


# ---------- candle comparison ----------
def compare_candles(name, kdf, udf, cols=("open", "high", "low", "close")):
    if kdf.empty or udf.empty:
        print(f"  [{name}] {FAIL}: empty (kite={len(kdf)} upstox={len(udf)})")
        return False
    common = kdf.index.intersection(udf.index)
    if len(common) == 0:
        print(f"  [{name}] {FAIL}: no overlapping timestamps "
              f"(kite {kdf.index.min()}..{kdf.index.max()} / "
              f"upstox {udf.index.min()}..{udf.index.max()})")
        return False
    k, u = kdf.loc[common], udf.loc[common]
    n = len(common) * len(cols)
    worst = 0.0; worst_at = None; exact = 0; within05 = 0; within2 = 0
    for c in cols:
        diff = (k[c] - u[c]).abs()
        exact   += int((diff <= 0.001).sum())
        within05 += int((diff <= 0.5).sum())
        within2  += int((diff <= 2.0).sum())
        if diff.max() > worst:
            worst = float(diff.max()); worst_at = (c, diff.idxmax())
    ok = exact == n
    print(f"  [{name}] {PASS if ok else FAIL}: {len(common)} candles | "
          f"exact={exact}/{n} ({100*exact/n:.1f}%)  "
          f"≤0.5pt={100*within05/n:.1f}%  ≤2pt={100*within2/n:.1f}%  "
          f"max={worst:.2f}")
    if not ok and worst_at:
        c, ts = worst_at
        print(f"        worst: {ts} {c}: kite={k.loc[ts,c]} upstox={u.loc[ts,c]}")
    return ok


# ---------- option instrument mapping ----------
def load_upstox_options():
    raw = urllib.request.urlopen(INSTR_URL, timeout=60).read()
    data = json.loads(gzip.decompress(raw))
    out = {}
    for r in data:
        if r.get("segment") == "NSE_FO" and r.get("name") == "NIFTY" \
                and r.get("instrument_type") in ("CE", "PE"):
            exp = dt.datetime.fromtimestamp(r["expiry"] / 1000).date()
            out[(int(r["strike_price"]), exp.isoformat(), r["instrument_type"])] = r["instrument_key"]
    return out


def main():
    print("=== Upstox <-> Kite DATA PARITY ===")
    kite = connect_kite()
    results = []

    # 1. SPOT 1-min candles (the spot data layer)
    print("\n[1] SPOT 1-min candles (NIFTY 50, today)")
    kdf = kite_df_to_iststr(V.get_historical_data(V.NIFTY_SPOT_TOKEN, "minute", 400))
    udf = u_intraday(SPOT_KEY)
    results.append(compare_candles("spot 1m", kdf, udf))

    # 2. SPOT LTP
    print("\n[2] SPOT LTP")
    kl = float(list(kite.ltp("NSE:NIFTY 50").values())[0]["last_price"])
    ul = float(u_ltp([SPOT_KEY])[SPOT_KEY.replace("|", ":")]["last_price"])
    ok = abs(kl - ul) < 0.05
    print(f"  [spot ltp] {PASS if ok else FAIL}: kite={kl} upstox={ul} diff={abs(kl-ul):.4f}")
    results.append(ok)

    # 3. INDIA VIX
    print("\n[3] INDIA VIX")
    try:
        kv = float(kite.quote(["NSE:INDIA VIX"])["NSE:INDIA VIX"]["last_price"])
        uv = float(u_ltp([VIX_KEY])[VIX_KEY.replace("|", ":")]["last_price"])
        ok = abs(kv - uv) < 0.05
        print(f"  [vix] {PASS if ok else FAIL}: kite={kv} upstox={uv} diff={abs(kv-uv):.4f}")
        results.append(ok)
    except Exception as e:
        print(f"  [vix] skipped: {e}")

    # 4. ATM OPTION 1-min candles + gate inputs (ema9_high / ema9_low)
    print("\n[4] ATM OPTION 1-min candles + EMA9 bands (GATE inputs)")
    expiry = V.get_nearest_expiry(kite)
    atm = V.resolve_atm_strike(kl)
    print(f"  expiry={expiry} atm_strike={atm}")
    ktok = V.get_option_tokens(kite, atm, expiry)
    uopts = load_upstox_options()
    for side in ("CE", "PE"):
        if side not in ktok:
            print(f"  [{side}] kite token missing, skip"); continue
        ukey = uopts.get((int(atm), expiry.isoformat(), side))
        if not ukey:
            print(f"  [{side}] {FAIL}: no Upstox instrument_key for {atm} {expiry} {side}"); continue
        # Both EMAs must seed at the SAME first candle (today 09:15), else the
        # warmup window — not the data — drives the band diff. So slice Kite to
        # today BEFORE add_indicators, exactly mirroring Upstox intraday.
        today = dt.date.today().isoformat()
        kraw = kite_df_to_iststr(V.get_historical_data(int(ktok[side]["token"]), "minute", 400))
        kraw = kraw[[s.startswith(today) for s in kraw.index]]
        kdf = V.add_indicators(kraw)
        udf = V.add_indicators(u_intraday(ukey))
        print(f"  --- {side}  kite_sym={ktok[side]['symbol']}  upstox_key={ukey} ---")
        ok1 = compare_candles(f"{side} 1m OHLC", kdf, udf)
        ok2 = compare_candles(f"{side} ema9 bands", kdf, udf, cols=("ema9_high", "ema9_low"))
        results.append(ok1 and ok2)

    print("\n=== SUMMARY ===")
    print(f"  {sum(results)}/{len(results)} checks PASS")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
