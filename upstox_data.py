#!/usr/bin/env python3
"""
Upstox market-DATA backend for VRL_MAIN (migration Phase 2 — 2026-06-19).

Drop-in replacement for the Kite *read* calls (historical candles, LTP, quote,
instrument/expiry/strike resolution). Orders stay on m.Stock; this module never
places orders and holds no strategy logic.

DESIGN — keep `token` an int so VRL_MAIN's state schema, CSV logs and the many
`int(token)` casts are untouched. Under the Upstox provider the canonical token
is Upstox's **exchange_token** (NSE's own integer id, present in the instrument
master). An internal bridge maps that int -> Upstox `instrument_key` string
("NSE_FO|50973") for the REST/WS calls. VRL_MAIN keeps passing ints around and
never sees a key.

Output of historical_df() is byte-compatible with VRL_MAIN.get_historical_data:
a tz-aware (Asia/Kolkata) DatetimeIndex named "timestamp" with float columns
open/high/low/close/volume, oldest-first.

Parity vs Kite (06-19): spot/index/VIX identical; option 1-min candles differ
~0.2pt avg (vendor tick aggregation) — see upstox_parity.py.
"""
import os, gzip, json, time, threading, datetime as dt
import urllib.request, urllib.parse
import requests
import pandas as pd

IST       = dt.timezone(dt.timedelta(hours=5, minutes=30))
UBASE     = "https://api.upstox.com"
INSTR_URL = "https://assets.upstox.com/market-quote/instruments/exchange/NSE.json.gz"
INSTR_CACHE = os.path.expanduser("~/state/upstox_nse_instruments.json")
ENV_PATH  = os.path.expanduser("~/.env")

# Index instrument_keys (no exchange_token in the NSE_INDEX master; pinned here)
SPOT_KEY = "NSE_INDEX|Nifty 50"
VIX_KEY  = "NSE_INDEX|India VIX"

# Kite "interval" -> Upstox (unit, interval)
_INTERVAL = {
    "minute":   ("minutes", "1"),
    "3minute":  ("minutes", "3"),
    "5minute":  ("minutes", "5"),
    "15minute": ("minutes", "15"),
    "30minute": ("minutes", "30"),
    "60minute": ("hours",   "1"),
}

_lock          = threading.RLock()
_bridge        = {}     # exchange_token(int) -> instrument_key(str) [options, cleared on reload]
_index_bridge  = {}     # configured spot/vix int token -> index key [persistent]
_nfo_by_date   = {}     # date.isoformat() -> list[dict] (Kite-shaped NFO rows)
_instr_raw     = None
_instr_day     = None


# ---------------------------------------------------------------- token / auth
def access_token() -> str:
    """Current Upstox token: env first, then the UPSTOX_ACCESS_TOKEN line in ~/.env."""
    t = os.getenv("UPSTOX_ACCESS_TOKEN", "")
    if t:
        return t
    try:
        with open(ENV_PATH) as f:
            for ln in f:
                if ln.startswith("UPSTOX_ACCESS_TOKEN="):
                    return ln.split("=", 1)[1].strip()
    except FileNotFoundError:
        pass
    return ""


def _headers():
    return {"Authorization": f"Bearer {access_token()}", "Accept": "application/json"}


# ---------------------------------------------------------- instrument master
def _load_instruments():
    """Download (once/day, disk-cached) the Upstox NSE master; build NFO rows +
    the exchange_token->instrument_key bridge. Returns the raw list."""
    global _instr_raw, _instr_day
    today = dt.date.today().isoformat()
    with _lock:
        if _instr_raw is not None and _instr_day == today:
            return _instr_raw
        data = None
        # disk cache
        if os.path.exists(INSTR_CACHE):
            try:
                cached = json.load(open(INSTR_CACHE))
                if cached.get("day") == today:
                    data = cached["rows"]
            except Exception:
                data = None
        if data is None:
            raw = urllib.request.urlopen(INSTR_URL, timeout=60).read()
            data = json.loads(gzip.decompress(raw))
            try:
                os.makedirs(os.path.dirname(INSTR_CACHE), exist_ok=True)
                json.dump({"day": today, "rows": data}, open(INSTR_CACHE, "w"))
            except Exception:
                pass
        _instr_raw, _instr_day = data, today
        _bridge.clear(); _nfo_by_date.clear()
        # pin index keys (their exchange_token isn't needed; bridge by config token
        # is registered separately by VRL_MAIN via register_index_tokens()).
        return data


def _nfo_rows():
    """NIFTY NFO option rows shaped like Kite instruments() output."""
    if _instr_raw is None or _instr_day != dt.date.today().isoformat():
        _load_instruments()
    cache_key = _instr_day
    with _lock:
        if cache_key in _nfo_by_date:
            return _nfo_by_date[cache_key]
        rows = []
        for r in _instr_raw:
            if r.get("segment") == "NSE_FO" and r.get("name") == "NIFTY" \
                    and r.get("instrument_type") in ("CE", "PE"):
                exp = dt.datetime.fromtimestamp(r["expiry"] / 1000, tz=IST).date()
                ext = int(r["exchange_token"])
                _bridge[ext] = r["instrument_key"]
                rows.append({
                    "name": "NIFTY",
                    "strike": float(r["strike_price"]),
                    "expiry": exp,
                    "instrument_type": r["instrument_type"],
                    "instrument_token": ext,            # canonical int = exchange_token
                    "tradingsymbol": r["trading_symbol"],
                    "lot_size": int(r.get("lot_size", 0)),
                    "instrument_key": r["instrument_key"],
                })
        _nfo_by_date[cache_key] = rows
        return rows


def register_index_tokens(spot_token: int, vix_token: int):
    """VRL_MAIN passes its configured int spot/vix tokens; bridge them to keys.
    Kept in a persistent dict so an instrument-master reload can't wipe them."""
    with _lock:
        _index_bridge[int(spot_token)] = SPOT_KEY
        _index_bridge[int(vix_token)]  = VIX_KEY


def key_for_token(token: int) -> str | None:
    t = int(token)
    with _lock:
        k = _index_bridge.get(t) or _bridge.get(t)
    if k is None:
        _nfo_rows()                       # ensure option bridge populated, retry
        with _lock:
            k = _index_bridge.get(t) or _bridge.get(t)
    return k


# ---------------------------------------------------- expiry / strike / lot
def nearest_expiry(reference_date=None) -> dt.date:
    ref = reference_date or dt.date.today()
    exps = sorted({r["expiry"] for r in _nfo_rows() if r["expiry"] >= ref})
    if not exps:
        raise RuntimeError("Upstox: no NIFTY expiry >= reference date")
    return exps[0]


def lot_size() -> int:
    rows = _nfo_rows()
    return rows[0]["lot_size"] if rows else 0


def option_tokens(strike: int, expiry_date) -> dict:
    """Mirror VRL_MAIN.get_option_tokens: {"CE": {token,symbol}, "PE": {...}}."""
    out = {}
    for r in _nfo_rows():
        if int(r["strike"]) == int(strike) and r["expiry"] == expiry_date:
            out[r["instrument_type"]] = {"token": r["instrument_token"],
                                         "symbol": r["tradingsymbol"]}
        if len(out) == 2:
            break
    return out


# ----------------------------------------------------------------- REST data
def _get(url, params=None):
    r = requests.get(url, headers=_headers(), params=params, timeout=20)
    r.raise_for_status()
    return r.json()


def _candles_to_df(candles):
    rows = []
    for c in candles:                       # [ts,o,h,l,c,vol,oi]; newest-first
        rows.append((c[0], c[1], c[2], c[3], c[4], c[5]))
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows, columns=["timestamp", "open", "high", "low", "close", "volume"])
    idx = pd.to_datetime(df["timestamp"], utc=True).dt.tz_convert("Asia/Kolkata")
    df = df.drop(columns=["timestamp"])
    df.index = idx; df.index.name = "timestamp"
    df = df.apply(pd.to_numeric, errors="coerce").dropna()
    df = df[~df.index.duplicated(keep="last")].sort_index()
    return df


def _historical(instrument_key, unit, interval, to_d, from_d):
    ek = urllib.parse.quote(instrument_key, safe="")
    url = f"{UBASE}/v3/historical-candle/{ek}/{unit}/{interval}/{to_d}/{from_d}"
    try:
        return _get(url).get("data", {}).get("candles", [])
    except Exception:
        return []


def _intraday(instrument_key, unit, interval):
    ek = urllib.parse.quote(instrument_key, safe="")
    url = f"{UBASE}/v3/historical-candle/intraday/{ek}/{unit}/{interval}"
    try:
        return _get(url).get("data", {}).get("candles", [])
    except Exception:
        return []


def historical_df(token: int, interval: str, lookback: int) -> pd.DataFrame:
    """Drop-in for VRL_MAIN.get_historical_data under the Upstox provider.
    Combines historical (prior days) + intraday (today) so it works pre-open
    and intraday alike. Returns the same DataFrame shape as Kite's path."""
    key = key_for_token(token)
    if not key:
        return pd.DataFrame()
    unit, ivl = _INTERVAL.get(interval, ("minutes", "1"))
    mpc = {"minutes": int(ivl), "hours": int(ivl) * 60}.get(unit, 1)
    # mirror Kite's window math (lookback * candle_minutes * 2.5 + 60), min 3 days
    total_min = lookback * mpc * 2.5
    from_d = (dt.datetime.now(IST) - dt.timedelta(minutes=int(total_min) + 60))
    from_d = min(from_d, dt.datetime.now(IST) - dt.timedelta(days=3)).date().isoformat()
    to_d   = dt.date.today().isoformat()
    candles = _historical(key, unit, ivl, to_d, from_d) + _intraday(key, unit, ivl)
    return _candles_to_df(candles)


def ltp(tokens) -> dict:
    """{token(int) -> last_price(float)} for a list of canonical int tokens."""
    keys = {t: key_for_token(t) for t in tokens}
    valid = {t: k for t, k in keys.items() if k}
    if not valid:
        return {}
    qs = ",".join(urllib.parse.quote(k, safe="") for k in valid.values())
    data = _get(f"{UBASE}/v2/market-quote/ltp?instrument_key={qs}").get("data", {})
    # response keys use ':' not '|'
    by_colon = {k.replace("|", ":"): v for k, v in
                {kk: vv for kk, vv in data.items()}.items()}
    out = {}
    for t, k in valid.items():
        v = data.get(k) or data.get(k.replace("|", ":")) or by_colon.get(k.replace("|", ":"))
        if v:
            out[t] = float(v["last_price"])
    return out


def get_ltp_one(token: int) -> float:
    return ltp([token]).get(int(token), 0.0)
