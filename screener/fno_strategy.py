"""
fno_strategy.py — SINGLE SOURCE OF TRUTH for the F&O screener entry logic.

Both vishal_fno_screener.py (EOD scan) and fno_collector.py (intraday tick) import
from here, so the entry gate, dedup, risk caps and trade-structure are identical
no matter which script runs.

WHY this exists (evidence from 3 days / 48 tracked trades / 2,297 signals):
  - +50%/+80% targets hit only 2% of the time on 30-DTE options  -> targets were fantasy
  - 35/48 trades were PUTs while CALLs made +5.5% and PUTs lost -2.1%  -> no regime filter
  - score 6/7/8 -> +1.0%/+0.2%/-19.9% avg  -> old score had ZERO predictive edge
  - 562-955 signals/day at score>=5  -> firehose of noise, no real selectivity
  - same losing trade re-added daily (INDUSINDBK CALL x3, TCS PUT x3)  -> no cross-day dedup

DESIGN
  - Market-regime filter (NIFTY trend) gates direction       <- biggest money leak fixed
  - Stronger multi-factor score (trend strength + momentum + regime + OI)
  - Liquidity filter (min OI, min premium)
  - Cross-day dedup + risk caps (trades/day, open positions, capital)
  - Two trade structures behind one config flag: "naked" (realistic targets) and
    "spread" (debit spread) so both can be paper-compared.

Nothing here places real orders. It only decides WHAT to track.
"""

import os
import json
import math
import time
from datetime import date, datetime, timedelta

import numpy as np
import pandas as pd

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# Well-known stable Kite instrument_token for the NIFTY 50 index.
NIFTY50_TOKEN = 256265

# =============================================================================
# CONFIG  — all tunables in one place. A JSON file (fno_strategy_config.json)
#           next to this module overrides any of these keys, so you can tweak
#           the strategy WITHOUT editing code.
# =============================================================================
DEFAULT_CONFIG = {
    # ── Trade structure ───────────────────────────────────────────────────
    "mode": "naked",            # "naked" | "spread"  (build-all, switch freely)
    "min_dte": 3,               # never pick an expiry closer than this many days
    "max_dte": 45,              # nor further than this (theta drag beyond ~monthly)
    "prefer_weekly": False,     # True -> pick nearest weekly (faster moves, more gamma)

    # ── Entry gate (new multi-factor score; max ~12) ──────────────────────
    "min_score": 8,             # was 6 of 9 -> raise the bar
    "require_regime_align": True,  # block CALLs in a bear tape, PUTs in a bull tape
    "rsi_call_lo": 52, "rsi_call_hi": 68,   # tightened from 55-75
    "rsi_put_lo": 38,  "rsi_put_hi": 52,    # avoid buying PUTs into deep-oversold (<38)
    "min_adx": 18,              # below this = chop, no trend to ride
    "max_ext_from_ema20_pct": 6.0,  # skip chasing: price too far from EMA20

    # ── Liquidity ─────────────────────────────────────────────────────────
    "min_oi": 500,              # contracts of OI on the chosen strike
    "min_premium": 5.0,         # skip sub-5 lottery options (spread eats them)
    "max_premium_pct_of_spot": 8.0,  # option premium shouldn't exceed this % of stock px

    # ── Risk caps (portfolio protection) ──────────────────────────────────
    "max_new_per_day": 4,
    "max_open_total": 8,              # HARD ceiling — even elite trades obey this
    "max_capital_deploy": 200000.0,   # rupees of premium outstanding (HARD)
    "per_trade_capital_cap": 60000.0, # HARD
    # An "elite" setup (score >= this) gets an EXTRA slot: it bypasses ONLY the
    # daily-count cap, so a best-trade-in-between is never blocked by 4 lesser
    # fills that merely came first. It still obeys every HARD limit above.
    "elite_score": 12,                # near the ~14 ceiling = genuine A+ setup

    # ── Naked structure targets/SL ────────────────────────────────────────
    "naked_t1_pct": 30.0,       # was 50 (hit 2% of time) -> realistic
    "naked_t2_pct": 60.0,       # was 80
    "naked_sl_pct": 25.0,       # was 35 -> tighter, cut the fat left tail
    "naked_atr_scale": True,    # widen/narrow targets by the option's own ATR move
    "time_stop_days": 7,        # exit if neither target hit -> stop theta bleed

    # ── Debit-spread structure ────────────────────────────────────────────
    "spread_width_strikes": 2,  # sell leg N strikes OTM from the long ATM leg
    "spread_t1_frac_of_max": 0.60,  # book at 60% of max spread value
    "spread_sl_frac_of_debit": 0.50,  # stop at 50% of net debit lost
}


def load_config():
    """DEFAULT_CONFIG overlaid with optional fno_strategy_config.json."""
    cfg = dict(DEFAULT_CONFIG)
    path = os.path.join(BASE_DIR, "fno_strategy_config.json")
    try:
        if os.path.exists(path):
            with open(path) as f:
                cfg.update(json.load(f) or {})
    except Exception:
        pass
    return cfg


# =============================================================================
# INDICATORS
# =============================================================================
def compute_rsi(series, period=14):
    delta = series.diff()
    gain = delta.clip(lower=0).rolling(period).mean()
    loss = (-delta.clip(upper=0)).rolling(period).mean()
    rs = gain / loss.replace(0, np.nan)
    return (100 - 100 / (1 + rs)).fillna(50)


def compute_atr(df, period=14):
    h, l, c = df["high"], df["low"], df["close"]
    pc = c.shift(1)
    tr = pd.concat([h - l, (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1)
    return tr.rolling(period).mean()


def compute_adx(df, period=14):
    """Wilder's ADX. Returns a Series aligned to df. NaN-safe."""
    h, l, c = df["high"], df["low"], df["close"]
    up = h.diff()
    dn = -l.diff()
    plus_dm = np.where((up > dn) & (up > 0), up, 0.0)
    minus_dm = np.where((dn > up) & (dn > 0), dn, 0.0)
    pc = c.shift(1)
    tr = pd.concat([h - l, (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1)
    atr = tr.ewm(alpha=1 / period, adjust=False).mean()
    plus_di = 100 * pd.Series(plus_dm, index=df.index).ewm(alpha=1 / period, adjust=False).mean() / atr.replace(0, np.nan)
    minus_di = 100 * pd.Series(minus_dm, index=df.index).ewm(alpha=1 / period, adjust=False).mean() / atr.replace(0, np.nan)
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    return dx.ewm(alpha=1 / period, adjust=False).mean().fillna(0)


# =============================================================================
# TECHNICALS  (canonical, richer than the old per-script versions)
# =============================================================================
def get_technicals(kite, nse_df, symbol):
    """100 days daily OHLCV -> rich tech dict (adds adx, ema20 slope, 5d return,
    extension-from-EMA20). Returns None on failure."""
    try:
        inst = nse_df[(nse_df["tradingsymbol"] == symbol) &
                      (nse_df["instrument_type"] == "EQ")]
        if inst.empty:
            return None
        token = int(inst.iloc[0]["instrument_token"])
        to_date = datetime.now()
        from_date = to_date - timedelta(days=130)
        candles = kite.historical_data(token, from_date, to_date, "day")
        if len(candles) < 30:
            return None

        df = pd.DataFrame(candles)
        df["ema20"] = df["close"].ewm(span=20, adjust=False).mean()
        df["ema50"] = df["close"].ewm(span=50, adjust=False).mean()
        df["rsi"] = compute_rsi(df["close"])
        df["atr"] = compute_atr(df)
        df["adx"] = compute_adx(df)
        df["vol_avg"] = df["volume"].rolling(10).mean()

        last, prev = df.iloc[-1], df.iloc[-2]
        ema20_now = float(last["ema20"])
        ema20_5ago = float(df["ema20"].iloc[-6]) if len(df) >= 6 else ema20_now
        ema20_slope_pct = round((ema20_now - ema20_5ago) / ema20_5ago * 100, 2) if ema20_5ago else 0.0
        price = round(float(last["close"]), 2)
        ret5_pct = round((price / float(df["close"].iloc[-6]) - 1) * 100, 2) if len(df) >= 6 else 0.0
        ext20_pct = round((price - ema20_now) / ema20_now * 100, 2) if ema20_now else 0.0

        return {
            "symbol": symbol,
            "price": price,
            "ema20": round(ema20_now, 2),
            "ema50": round(float(last["ema50"]), 2),
            "rsi": round(float(last["rsi"]), 1),
            "atr": round(float(last["atr"]), 2),
            "adx": round(float(last["adx"]), 1),
            "ema20_slope_pct": ema20_slope_pct,
            "ret5_pct": ret5_pct,
            "ext20_pct": ext20_pct,
            "prev_close": float(prev["close"]),
            "volume": float(last["volume"]),
            "vol_avg": float(last["vol_avg"]) if not math.isnan(float(last["vol_avg"])) else 0.0,
        }
    except Exception:
        return None


# =============================================================================
# OPTION CHAIN  (canonical — full per-strike OI/LTP so both scripts share it)
# =============================================================================
def get_nearest_expiry(nfo_df, symbol, cfg=None):
    """Nearest expiry within [min_dte, max_dte]; falls back to nearest future."""
    cfg = cfg or DEFAULT_CONFIG
    opts = nfo_df[(nfo_df["name"] == symbol) &
                  (nfo_df["instrument_type"].isin(["CE", "PE"]))]
    if opts.empty:
        return None
    today = pd.Timestamp(date.today())
    future = sorted({pd.Timestamp(e) for e in opts["expiry"].unique()
                     if pd.Timestamp(e) >= today})
    if not future:
        return None
    elig = [e for e in future
            if cfg.get("min_dte", 3) <= (e - today).days <= cfg.get("max_dte", 45)]
    pool = elig or future
    return pool[0].date()   # nearest eligible (weekly if it exists, else monthly)


def get_option_chain(kite, nfo_df, symbol, price, expiry):
    """ATM +/-8 strikes. Returns ce/pe per-strike dicts + pcr/max_pain/atm/lot_size.
    None on failure."""
    try:
        opts = nfo_df[(nfo_df["name"] == symbol) &
                      (nfo_df["instrument_type"].isin(["CE", "PE"])) &
                      (nfo_df["expiry"].dt.date == expiry)].copy()
        if opts.empty:
            return None
        opts["strike"] = opts["strike"].astype(float)
        lot_size = int(opts.iloc[0]["lot_size"])
        strikes = sorted(opts["strike"].unique())
        if not strikes:
            return None
        atm_idx = min(range(len(strikes)), key=lambda i: abs(strikes[i] - price))
        lo, hi = max(0, atm_idx - 8), min(len(strikes) - 1, atm_idx + 8)
        sel = set(strikes[lo:hi + 1]); atm = strikes[atm_idx]
        opts = opts[opts["strike"].isin(sel)]

        keys = [f"NFO:{r['tradingsymbol']}" for _, r in opts.iterrows()]
        quotes = {}
        for i in range(0, len(keys), 499):
            quotes.update(kite.quote(keys[i:i + 499]))
            if i + 499 < len(keys):
                time.sleep(0.3)

        ce, pe = {}, {}
        for _, row in opts.iterrows():
            q = quotes.get(f"NFO:{row['tradingsymbol']}", {})
            entry = {"oi": int(q.get("oi", 0) or 0),
                     "ltp": float(q.get("last_price", 0) or 0),
                     "strike": float(row["strike"]),
                     "tradingsymbol": row["tradingsymbol"]}
            (ce if row["instrument_type"] == "CE" else pe)[float(row["strike"])] = entry
        if not ce and not pe:
            return None

        total_ce = sum(v["oi"] for v in ce.values())
        total_pe = sum(v["oi"] for v in pe.values())
        pcr = round(total_pe / total_ce, 2) if total_ce > 0 else 1.0

        all_s = sorted(set(list(ce.keys()) + list(pe.keys())))
        max_pain, min_pain = atm, float("inf")
        for s in all_s:
            pain = sum((s - k) * v["oi"] * lot_size for k, v in ce.items() if s > k) + \
                   sum((k - s) * v["oi"] * lot_size for k, v in pe.items() if s < k)
            if pain < min_pain:
                min_pain, max_pain = pain, s

        return {"ce": ce, "pe": pe, "pcr": pcr, "max_pain": max_pain, "atm": atm,
                "lot_size": lot_size, "expiry": expiry,
                "total_ce_oi": total_ce, "total_pe_oi": total_pe}
    except Exception:
        return None


# =============================================================================
# MARKET REGIME  (NIFTY 50 daily trend) — THE highest-impact filter
# =============================================================================
_REGIME_CACHE = {"date": None, "data": None}


def compute_index_regime(kite, nse_df=None):
    """Returns dict: {regime, allow_call, allow_put, strong_call, strong_put, detail}.
    regime in {BULL, WEAK_BULL, NEUTRAL, WEAK_BEAR, BEAR}. Cached per calendar day."""
    today = date.today().isoformat()
    if _REGIME_CACHE["date"] == today and _REGIME_CACHE["data"]:
        return _REGIME_CACHE["data"]

    out = {"regime": "NEUTRAL", "allow_call": True, "allow_put": True,
           "strong_call": False, "strong_put": False, "detail": "default"}
    try:
        token = NIFTY50_TOKEN
        if nse_df is not None:
            m = nse_df[nse_df["tradingsymbol"] == "NIFTY 50"]
            if not m.empty:
                token = int(m.iloc[0]["instrument_token"])
        to_date = datetime.now()
        candles = kite.historical_data(token, to_date - timedelta(days=130), to_date, "day")
        df = pd.DataFrame(candles)
        if len(df) < 30:
            raise ValueError("insufficient index history")
        df["ema20"] = df["close"].ewm(span=20, adjust=False).mean()
        df["ema50"] = df["close"].ewm(span=50, adjust=False).mean()
        df["adx"] = compute_adx(df)
        last = df.iloc[-1]
        price = float(last["close"]); e20 = float(last["ema20"]); e50 = float(last["ema50"])
        adx = float(last["adx"])
        e20_5 = float(df["ema20"].iloc[-6]); slope = (e20 - e20_5) / e20_5 * 100 if e20_5 else 0.0

        up = price > e20 and e20 > e50 and slope > 0
        dn = price < e20 and e20 < e50 and slope < 0
        strong = adx >= 22
        if up:
            regime = "BULL" if strong else "WEAK_BULL"
        elif dn:
            regime = "BEAR" if strong else "WEAK_BEAR"
        else:
            regime = "NEUTRAL"

        out = {
            "regime": regime,
            # In a clear trend, block the counter-trend side outright.
            "allow_call": regime in ("BULL", "WEAK_BULL", "NEUTRAL"),
            "allow_put": regime in ("BEAR", "WEAK_BEAR", "NEUTRAL"),
            "strong_call": regime == "BULL",
            "strong_put": regime == "BEAR",
            "detail": f"px={price:.0f} e20={e20:.0f} e50={e50:.0f} slope={slope:+.2f}% adx={adx:.0f}",
        }
    except Exception as e:
        out["detail"] = f"regime-fallback({e})"
    _REGIME_CACHE["date"] = today
    _REGIME_CACHE["data"] = out
    return out


# =============================================================================
# SCORING  (multi-factor; regime-aware; returns reject reason for transparency)
# =============================================================================
def score_signal(tech, regime, opt, cfg):
    """Returns (direction, score, signals, reject_reason).
    direction in {CALL, PUT, NEUTRAL}. reject_reason is '' when a tradable
    direction qualifies, else explains why it was filtered."""
    price = tech["price"]; ema20 = tech["ema20"]; ema50 = tech["ema50"]
    rsi = tech["rsi"]; adx = tech.get("adx", 0); slope = tech.get("ema20_slope_pct", 0)
    ret5 = tech.get("ret5_pct", 0); ext = abs(tech.get("ext20_pct", 0))
    vol = tech["volume"]; vol_avg = tech["vol_avg"]
    pcr = opt["pcr"] if opt else 1.0
    mp = opt["max_pain"] if opt else price

    def side_score(direction):
        pts = 0; sig = []
        bull = direction == "CALL"
        # ── Trend structure (price vs EMAs) ──
        if (price > ema20) == bull: pts += 1; sig.append("P/EMA20 ok")
        else: return -99, sig
        if (price > ema50) == bull: pts += 1; sig.append("P/EMA50 ok")
        if (ema20 > ema50 * 1.005) if bull else (ema20 < ema50 * 0.995):
            pts += 1; sig.append("EMA trend ok")
        # ── Trend strength ──
        if adx >= cfg["min_adx"]: pts += 1; sig.append(f"ADX={adx:.0f}")
        if adx >= 30: pts += 1; sig.append("ADX strong")
        if (slope > 0) == bull and abs(slope) >= 0.2: pts += 1; sig.append(f"slope={slope:+.1f}%")
        # ── Momentum (tight RSI zone + 5d return aligned) ──
        if bull and cfg["rsi_call_lo"] <= rsi <= cfg["rsi_call_hi"]:
            pts += 2; sig.append(f"RSI={rsi:.0f}")
        elif (not bull) and cfg["rsi_put_lo"] <= rsi <= cfg["rsi_put_hi"]:
            pts += 2; sig.append(f"RSI={rsi:.0f}")
        else:
            return -99, sig  # RSI outside the tradable window for this side
        if (ret5 > 0) == bull and abs(ret5) >= 1.0: pts += 1; sig.append(f"5d={ret5:+.1f}%")
        # ── Volume confirmation ──
        if vol_avg > 0 and vol > vol_avg * 1.4:
            if (price >= tech["prev_close"]) == bull: pts += 1; sig.append("VolSpike")
        # ── OI / option-chain confirmation ──
        if bull:
            if pcr >= 1.2: pts += 2; sig.append(f"PCR={pcr}")
            elif pcr >= 1.0: pts += 1; sig.append(f"PCR={pcr}")
            elif pcr < 0.8: pts -= 1; sig.append(f"PCR={pcr}!")
            if price >= mp: pts += 1; sig.append("MaxPain pull")
        else:
            if pcr <= 0.8: pts += 2; sig.append(f"PCR={pcr}")
            elif pcr <= 1.0: pts += 1; sig.append(f"PCR={pcr}")
            elif pcr > 1.2: pts -= 1; sig.append(f"PCR={pcr}!")
            if price <= mp: pts += 1; sig.append("MaxPain pull")
        return pts, sig

    c_pts, c_sig = side_score("CALL")
    p_pts, p_sig = side_score("PUT")
    if c_pts >= p_pts:
        direction, score, sig = "CALL", c_pts, c_sig
    else:
        direction, score, sig = "PUT", p_pts, p_sig

    if score < 0:
        return "NEUTRAL", 0, sig, "no_clean_side"

    # ── Overextension (don't chase) ──
    if ext > cfg["max_ext_from_ema20_pct"]:
        return direction, score, sig, f"overextended({ext:.1f}%)"

    # ── PCR gate: PCR > max_pcr = poor win rate (F&O data 2026-06: PCR>1.0 → 25% win) ──
    _max_pcr = cfg.get("max_pcr", 0)
    if _max_pcr and pcr > _max_pcr:
        return direction, score, sig, f"pcr_high({pcr})"

    # ── Regime gate (THE big fix) ──
    if cfg["require_regime_align"]:
        if direction == "CALL" and not regime["allow_call"]:
            return direction, score, sig, f"regime_block({regime['regime']})"
        if direction == "PUT" and not regime["allow_put"]:
            return direction, score, sig, f"regime_block({regime['regime']})"
    if direction == "CALL" and regime["strong_call"]:
        score += 2; sig.append("REGIME+")
    if direction == "PUT" and regime["strong_put"]:
        score += 2; sig.append("REGIME+")

    if score < cfg["min_score"]:
        return direction, score, sig, f"score<{cfg['min_score']}"
    return direction, score, sig, ""


# =============================================================================
# LIQUIDITY
# =============================================================================
def passes_liquidity(opt_entry, spot, cfg):
    """opt_entry: {'oi','ltp','strike',...}. Returns (ok, reason)."""
    if not opt_entry:
        return False, "no_strike"
    oi = int(opt_entry.get("oi", 0) or 0)
    ltp = float(opt_entry.get("ltp", 0) or 0)
    if ltp < cfg["min_premium"]:
        return False, f"prem<{cfg['min_premium']}"
    if oi < cfg["min_oi"]:
        return False, f"oi<{cfg['min_oi']}({oi})"
    if spot > 0 and ltp > spot * cfg["max_premium_pct_of_spot"] / 100.0:
        return False, "prem_too_rich"
    return True, ""


# =============================================================================
# TRADE STRUCTURE  (naked realistic targets  |  debit spread)
# =============================================================================
def _nearest_strike(chain_side, target, with_ltp=True):
    cands = [s for s, v in chain_side.items() if (v.get("ltp", 0) > 0 or not with_ltp)]
    if not cands:
        return None
    return min(cands, key=lambda s: abs(s - target))


def build_setup(direction, tech, opt, cfg):
    """Returns a setup dict ready for the tracker, or (None, reason).
    Honors cfg['mode'] = naked | spread. Adds spread-only fields when spread."""
    side = opt["ce"] if direction == "CALL" else opt["pe"]
    atm = opt["atm"]
    price = tech["price"]; atr = tech.get("atr", 0); lot = opt["lot_size"]

    long_strike = _nearest_strike(side, atm)
    if long_strike is None:
        return None, "no_liquid_atm"
    long_leg = side[long_strike]
    ok, why = passes_liquidity(long_leg, price, cfg)
    if not ok:
        return None, why
    long_prem = float(long_leg["ltp"])

    # stock-level SL (informational): 1.5 ATR adverse move
    stock_sl = round(price - 1.5 * atr, 1) if direction == "CALL" else round(price + 1.5 * atr, 1)

    base = {
        "direction": direction,
        "strike": long_strike,
        "option_symbol": long_leg["tradingsymbol"],
        "lot_size": lot,
        "stock_price": price,
        "stock_sl": stock_sl,
        "pcr": opt["pcr"],
        "max_pain": opt["max_pain"],
        "expiry": opt["expiry"],
    }

    if cfg["mode"] == "spread":
        # sell N strikes OTM on the same side -> debit spread caps cost & theta
        strikes = sorted(side.keys())
        try:
            li = strikes.index(long_strike)
        except ValueError:
            return None, "strike_index"
        step = cfg["spread_width_strikes"]
        si = li + step if direction == "CALL" else li - step
        if si < 0 or si >= len(strikes):
            return None, "no_short_strike"
        short_strike = strikes[si]
        short_leg = side[short_strike]
        short_prem = float(short_leg.get("ltp", 0) or 0)
        width = abs(short_strike - long_strike)
        net_debit = round(long_prem - short_prem, 1)
        if net_debit <= 0 or width <= 0:
            return None, "bad_spread_debit"
        max_val = width  # per-unit value if fully ITM at expiry
        t1 = round(net_debit + cfg["spread_t1_frac_of_max"] * (max_val - net_debit), 1)
        sl = round(net_debit * (1 - cfg["spread_sl_frac_of_debit"]), 1)
        base.update({
            "structure": "SPREAD",
            "entry_premium": net_debit,
            "sl_premium": sl,
            "t1_premium": t1,
            "t2_premium": round(max_val * 0.95, 1),
            "sell_strike": short_strike,
            "sell_symbol": short_leg["tradingsymbol"],
            "net_debit": net_debit,
            "max_value": max_val,
            "invest_per_lot": round(net_debit * lot, 0),
        })
        return base, ""

    # ── naked, realistic + ATR-scaled targets ──
    t1_pct, t2_pct, sl_pct = cfg["naked_t1_pct"], cfg["naked_t2_pct"], cfg["naked_sl_pct"]
    if cfg["naked_atr_scale"] and atr > 0 and price > 0:
        # scale targets toward how big a move the stock typically makes (ATR%/day)
        atr_pct = atr / price * 100.0
        factor = max(0.7, min(1.4, atr_pct / 1.5))  # ~1.5% ATR = neutral
        t1_pct = round(t1_pct * factor, 0); t2_pct = round(t2_pct * factor, 0)
    base.update({
        "structure": "NAKED",
        "entry_premium": long_prem,
        "sl_premium": round(long_prem * (1 - sl_pct / 100.0), 1),
        "t1_premium": round(long_prem * (1 + t1_pct / 100.0), 1),
        "t2_premium": round(long_prem * (1 + t2_pct / 100.0), 1),
        "sell_strike": "", "sell_symbol": "", "net_debit": "", "max_value": "",
        "invest_per_lot": round(long_prem * lot, 0),
    })
    return base, ""


# =============================================================================
# DEDUP + RISK CAPS  (portfolio-level protection, shared by both scripts)
# =============================================================================
def _open_mask(df):
    return df["status"].astype(str).str.startswith("OPEN", na=False)


def is_open_dup(tracker_df, symbol, direction):
    """True if symbol+direction is already an OPEN position (any date)."""
    if tracker_df is None or len(tracker_df) == 0:
        return False
    o = tracker_df[_open_mask(tracker_df)]
    if o.empty:
        return False
    return bool(((o["symbol"] == symbol) & (o["direction"] == direction)).any())


def can_add_entry(tracker_df, symbol, direction, invest, cfg, today=None, score=0):
    """Returns (ok, reason). Enforces cross-day dedup + portfolio caps.

    An 'elite' setup (score >= cfg['elite_score']) gets an EXTRA slot: it bypasses
    ONLY the daily-count cap, so a best-trade-in-between is never blocked by lesser
    fills that merely came first. It still obeys every HARD limit (dedup,
    max_open_total, capital). tracker_df may be None/empty."""
    today = today or date.today().isoformat()
    if tracker_df is None or len(tracker_df) == 0:
        return True, ""
    df = tracker_df
    open_df = df[_open_mask(df)]
    elite = score >= cfg.get("elite_score", 99)

    # 1) cross-day dedup: same symbol+direction already open (any date) — HARD
    if is_open_dup(df, symbol, direction):
        return False, "already_open"

    # 2) daily cap counts only ROUTINE (non-elite) adds, so elite trades are a
    #    genuine extra slot on top of the 4 — never blocked by it.
    if not elite:
        today_rows = df[df["date_added"].astype(str) == today]
        if "elite" in df.columns:
            routine_today = int((pd.to_numeric(today_rows["elite"], errors="coerce")
                                 .fillna(0) != 1).sum())
        else:
            routine_today = len(today_rows)
        if routine_today >= cfg["max_new_per_day"]:
            return False, f"daily_cap({cfg['max_new_per_day']})"

    # 3) max concurrent open positions — HARD (even for elite)
    if len(open_df) >= cfg["max_open_total"]:
        return False, f"open_cap({cfg['max_open_total']})"

    # 4) per-trade + total capital deployed — HARD (even for elite)
    if invest and invest > cfg["per_trade_capital_cap"]:
        return False, "per_trade_capital"
    if "investment" in open_df.columns:
        deployed = pd.to_numeric(open_df["investment"], errors="coerce").fillna(0).sum()
        if deployed + (invest or 0) > cfg["max_capital_deploy"]:
            return False, "capital_cap"
    return True, ""


def setup_to_tracker_row(s, today, rank=0):
    """Map an accepted setup dict to a tracker CSV row (shared by EOD save and
    intraday append so the schema is identical from both writers)."""
    exp = s["expiry"]
    exp_str = exp.strftime("%Y-%m-%d") if hasattr(exp, "strftime") else str(exp)
    invest = s.get("invest_per_lot",
                   round(float(s["entry_premium"]) * int(s["lot_size"]), 0))
    return {
        "date_added": today, "symbol": s["symbol"], "direction": s["direction"],
        "option_symbol": s["option_symbol"], "strike": s["strike"], "expiry": exp_str,
        "lot_size": s["lot_size"], "entry_premium": s["entry_premium"],
        "sl_premium": s["sl_premium"], "t1_premium": s["t1_premium"],
        "t2_premium": s["t2_premium"], "stock_price": s["stock_price"],
        "stock_sl": s["stock_sl"], "pcr": s["pcr"], "max_pain": s["max_pain"],
        "score": s["score"], "rank": rank, "current_premium": s["entry_premium"],
        "current_return_pct": 0.0, "last_checked": today, "status": "OPEN",
        "lots": 1, "investment": invest, "pnl_rs": 0.0,
        "structure": s.get("structure", "NAKED"), "sell_strike": s.get("sell_strike", ""),
        "sell_symbol": s.get("sell_symbol", ""), "net_debit": s.get("net_debit", ""),
        "elite": 1 if s.get("elite") else 0, "regime": s.get("regime", ""),
        "signals": s.get("signals", ""),
    }


def select_with_caps(candidates, tracker_df, cfg, today=None):
    """Rank already-qualified setups by score (desc) and accept in order while
    respecting daily + portfolio caps. Elite setups bypass the daily-count cap.
    Because we sort first, the BEST setups of the day claim the limited slots —
    a mediocre early signal can never crowd out a better later one.
    Each candidate is a dict with at least symbol/direction/score/invest_per_lot.
    Returns the accepted subset (in ranked order)."""
    today = today or date.today().isoformat()
    ranked = sorted(candidates, key=lambda s: s.get("score", 0), reverse=True)
    cols = ["date_added", "symbol", "direction", "status", "investment"]
    if tracker_df is not None and len(tracker_df):
        work = tracker_df.copy()
    else:
        work = pd.DataFrame(columns=cols)
    accepted = []
    for s in ranked:
        ok, why = can_add_entry(work, s["symbol"], s["direction"],
                                s.get("invest_per_lot", 0), cfg, today,
                                score=int(s.get("score", 0)))
        if not ok:
            s["_skip_reason"] = why
            continue
        accepted.append(s)
        work = pd.concat([work, pd.DataFrame([{
            "date_added": today, "symbol": s["symbol"], "direction": s["direction"],
            "status": "OPEN", "investment": s.get("invest_per_lot", 0),
            "elite": 1 if int(s.get("score", 0)) >= cfg.get("elite_score", 99) else 0}])],
            ignore_index=True)
    return accepted


# =============================================================================
# MASTER GATE — one call returns either a ready tracker row or a reject reason.
# =============================================================================
def evaluate(symbol, tech, opt, regime, tracker_df, cfg, today=None, apply_caps=True):
    """End-to-end: score -> structure -> liquidity -> dedup [-> caps].
    Returns (setup_dict | None, reason). setup_dict has every field needed to
    append a tracker row (caller adds date_added/rank/status bookkeeping).

    apply_caps=True  -> intraday/one-at-a-time path: enforce daily+portfolio caps now.
    apply_caps=False -> EOD batch path: only qualify (dedup always on); the caller
                        ranks candidates by score, THEN applies caps in ranked order
                        so the BEST setups claim the limited slots first."""
    if tech is None:
        return None, "no_tech"
    direction, score, sig, reason = score_signal(tech, regime, opt, cfg)
    if reason:
        return None, reason
    if opt is None:
        return None, "no_chain"

    setup, why = build_setup(direction, tech, opt, cfg)
    if setup is None:
        return None, why

    # dedup vs already-open is ALWAYS enforced (cheap, prevents stacking losers)
    if is_open_dup(tracker_df, symbol, direction):
        return None, "already_open"

    if apply_caps:
        ok, why = can_add_entry(tracker_df, symbol, direction,
                                setup.get("invest_per_lot", 0), cfg, today, score=int(score))
        if not ok:
            return None, why

    setup["symbol"] = symbol
    setup["score"] = int(score)
    setup["elite"] = int(score) >= cfg.get("elite_score", 99)
    setup["signals"] = ("ELITE | " if setup["elite"] else "") + " | ".join(sig)
    setup["regime"] = regime.get("regime", "?")
    return setup, ""
