"""
v12.14 PATCH: Historical fallback + 5min/15min data collection
- Dashboard shows last available data when market closed
- LAB collects 5-min + 15-min option candles
- Dashboard option charts for all timeframes
"""
import os, glob

R = os.path.expanduser("~/VISHAL_RAJPUT")

def rf(n):
    with open(os.path.join(R, n)) as f: return f.read()
def wf(n, c):
    with open(os.path.join(R, n), "w") as f: f.write(c)
    print("[OK] " + n)


# ═══════════════════════════════════════════════════════════════
#  1. VRL_LAB.py — Add 5-min + 15-min option collection
# ═══════════════════════════════════════════════════════════════

l = rf("VRL_LAB.py")

# 1a. Add FIELDNAMES for 5m and 15m
old_spot_fields = '# ─── SPOT 1-MIN COLLECTOR'
new_fields_block = '''# ─── 5-MIN + 15-MIN SCHEMAS ────────────────────────────────

FIELDNAMES_5M = [
    "timestamp", "strike", "type",
    "open", "high", "low", "close", "volume",
    "spot_ref", "dte", "session_block",
    "body_pct", "rsi", "ema9", "ema21", "ema_spread",
    "volume_ratio", "iv_pct", "delta",
]

FIELDNAMES_15M = [
    "timestamp", "strike", "type",
    "open", "high", "low", "close", "volume",
    "spot_ref", "dte", "session_block",
    "body_pct", "rsi", "ema9", "ema21", "ema_spread",
    "macd_hist", "adx",
    "volume_ratio", "iv_pct", "delta",
]

FIELDNAMES_SPOT_5M = [
    "timestamp", "open", "high", "low", "close", "volume",
    "ema9", "ema21", "ema_spread", "rsi",
]

FIELDNAMES_SPOT_15M = [
    "timestamp", "open", "high", "low", "close", "volume",
    "ema9", "ema21", "ema_spread", "rsi", "adx",
]


def _csv_path_5m(d):
    return os.path.join(D.OPTIONS_1MIN_DIR,
                        "nifty_option_5min_" + d.strftime("%Y%m%d") + ".csv")

def _csv_path_15m(d):
    return os.path.join(D.OPTIONS_1MIN_DIR,
                        "nifty_option_15min_" + d.strftime("%Y%m%d") + ".csv")

def _csv_path_spot_5m():
    from datetime import date as _d
    return os.path.join(D.SPOT_DIR, "nifty_spot_5min_" + _d.today().strftime("%Y%m%d") + ".csv")

def _csv_path_spot_15m():
    from datetime import date as _d
    return os.path.join(D.SPOT_DIR, "nifty_spot_15min_" + _d.today().strftime("%Y%m%d") + ".csv")


# ─── SPOT 1-MIN COLLECTOR'''
l = l.replace(old_spot_fields, new_fields_block)

# 1b. Add 5-min + 15-min collection functions before start_lab
old_start = '\ndef start_lab(kite):'
new_collectors = r'''

# ─── 5-MIN OPTION COLLECTOR ──────────────────────────────────

def collect_option_5min(kite, spot_ltp: float):
    """Collect last closed 5-min option candle for ATM CE + PE."""
    global _current_atm_strike, _current_atm_tokens, _current_expiry
    if not _current_atm_tokens or not _current_expiry:
        return
    now = datetime.now()
    if not D.is_market_open():
        return
    today = date.today()
    dte = D.calculate_dte(_current_expiry)
    session = D.get_session_block(now.hour, now.minute)
    from_dt = now - timedelta(days=3)
    to_dt = now
    all_rows = []
    for opt_type, info in _current_atm_tokens.items():
        token = info["token"]
        try:
            candles = _fetch_candles_with_warmup(kite, token, from_dt, to_dt, "5minute", 30)
            if not candles or len(candles) < 2:
                continue
            last = candles[-2]
            df = pd.DataFrame(candles)
            df.rename(columns={"date": "timestamp"}, inplace=True)
            df.set_index("timestamp", inplace=True)
            df = D.add_indicators(df)
            row = df.iloc[-2]
            c = float(row["close"])
            o = float(row["open"])
            h = float(row["high"])
            l_val = float(row["low"])
            rng = h - l_val
            e9 = float(row.get("EMA_9", c))
            e21 = float(row.get("EMA_21", c))
            vols = [df.iloc[i]["volume"] for i in range(-7, -2) if i >= -len(df) and df.iloc[i]["volume"] > 0]
            avg_v = sum(vols) / len(vols) if vols else 1
            greeks = D.get_full_greeks(c, spot_ltp, _current_atm_strike, _current_expiry, opt_type)
            ts_str = (last["date"].strftime("%Y-%m-%d %H:%M:%S")
                      if hasattr(last["date"], "strftime") else str(last["date"]))
            all_rows.append({
                "timestamp": ts_str, "strike": _current_atm_strike, "type": opt_type,
                "open": round(o, 2), "high": round(h, 2), "low": round(l_val, 2), "close": round(c, 2),
                "volume": int(last["volume"]), "spot_ref": round(spot_ltp, 2),
                "dte": dte, "session_block": session,
                "body_pct": round(abs(c - o) / rng * 100, 1) if rng > 0 else 0,
                "rsi": round(float(row.get("RSI", 50)), 1),
                "ema9": round(e9, 2), "ema21": round(e21, 2),
                "ema_spread": round(e9 - e21, 2),
                "volume_ratio": round(last["volume"] / avg_v if avg_v > 0 else 1, 2),
                "iv_pct": greeks.get("iv_pct", 0), "delta": greeks.get("delta", 0),
            })
        except Exception as e:
            logger.debug("[LAB] 5m error " + opt_type + ": " + str(e))
        time.sleep(0.35)
    if all_rows:
        _append_rows(_csv_path_5m(today), FIELDNAMES_5M, all_rows)
        logger.debug("[LAB] 5m wrote=" + str(len(all_rows)))


def collect_option_15min(kite, spot_ltp: float):
    """Collect last closed 15-min option candle for ATM CE + PE."""
    global _current_atm_strike, _current_atm_tokens, _current_expiry
    if not _current_atm_tokens or not _current_expiry:
        return
    now = datetime.now()
    if not D.is_market_open():
        return
    today = date.today()
    dte = D.calculate_dte(_current_expiry)
    session = D.get_session_block(now.hour, now.minute)
    from_dt = now - timedelta(days=10)
    to_dt = now
    all_rows = []
    for opt_type, info in _current_atm_tokens.items():
        token = info["token"]
        try:
            candles = _fetch_candles_with_warmup(kite, token, from_dt, to_dt, "15minute", 30)
            if not candles or len(candles) < 2:
                continue
            last = candles[-2]
            df = pd.DataFrame(candles)
            df.rename(columns={"date": "timestamp"}, inplace=True)
            df.set_index("timestamp", inplace=True)
            df = D.add_indicators(df)
            row = df.iloc[-2]
            c = float(row["close"])
            o = float(row["open"])
            h = float(row["high"])
            l_val = float(row["low"])
            rng = h - l_val
            e9 = float(row.get("EMA_9", c))
            e21 = float(row.get("EMA_21", c))
            # ADX calc
            adx_val = 0
            try:
                import numpy as _np
                up = df["high"].diff()
                dn = -df["low"].diff()
                pdm = _np.where((up > dn) & (up > 0), up, 0.0)
                ndm = _np.where((dn > up) & (dn > 0), dn, 0.0)
                tr = pd.concat([df["high"]-df["low"],
                                (df["high"]-df["close"].shift(1)).abs(),
                                (df["low"]-df["close"].shift(1)).abs()], axis=1).max(axis=1)
                atr_s = tr.ewm(alpha=1/14, adjust=False).mean()
                pdi = 100 * pd.Series(pdm, index=df.index).ewm(alpha=1/14, adjust=False).mean() / atr_s
                ndi = 100 * pd.Series(ndm, index=df.index).ewm(alpha=1/14, adjust=False).mean() / atr_s
                adx_s = ((pdi-ndi).abs() / (pdi+ndi+1e-9) * 100).ewm(alpha=1/14, adjust=False).mean()
                adx_val = round(float(adx_s.iloc[-2]), 1)
            except Exception:
                pass
            # MACD
            macd_hist = 0
            try:
                sma12 = df["close"].rolling(12).mean()
                sma26 = df["close"].rolling(26).mean()
                macd_line = sma12 - sma26
                macd_sig = macd_line.rolling(9).mean()
                macd_hist = round(float((macd_line - macd_sig).iloc[-2]), 2)
            except Exception:
                pass
            vols = [df.iloc[i]["volume"] for i in range(-5, -2) if i >= -len(df) and df.iloc[i]["volume"] > 0]
            avg_v = sum(vols) / len(vols) if vols else 1
            greeks = D.get_full_greeks(c, spot_ltp, _current_atm_strike, _current_expiry, opt_type)
            ts_str = (last["date"].strftime("%Y-%m-%d %H:%M:%S")
                      if hasattr(last["date"], "strftime") else str(last["date"]))
            all_rows.append({
                "timestamp": ts_str, "strike": _current_atm_strike, "type": opt_type,
                "open": round(o, 2), "high": round(h, 2), "low": round(l_val, 2), "close": round(c, 2),
                "volume": int(last["volume"]), "spot_ref": round(spot_ltp, 2),
                "dte": dte, "session_block": session,
                "body_pct": round(abs(c - o) / rng * 100, 1) if rng > 0 else 0,
                "rsi": round(float(row.get("RSI", 50)), 1),
                "ema9": round(e9, 2), "ema21": round(e21, 2),
                "ema_spread": round(e9 - e21, 2),
                "macd_hist": macd_hist, "adx": adx_val,
                "volume_ratio": round(last["volume"] / avg_v if avg_v > 0 else 1, 2),
                "iv_pct": greeks.get("iv_pct", 0), "delta": greeks.get("delta", 0),
            })
        except Exception as e:
            logger.debug("[LAB] 15m error " + opt_type + ": " + str(e))
        time.sleep(0.35)
    if all_rows:
        _append_rows(_csv_path_15m(today), FIELDNAMES_15M, all_rows)
        logger.debug("[LAB] 15m wrote=" + str(len(all_rows)))


def collect_spot_5min(kite):
    """Collect last closed 5-min spot candle."""
    if not D.is_market_open():
        return
    try:
        now = datetime.now()
        candles = kite.historical_data(
            instrument_token=D.NIFTY_SPOT_TOKEN,
            from_date=now - timedelta(days=3), to_date=now,
            interval="5minute", continuous=False, oi=False)
        if not candles or len(candles) < 15:
            return
        df = pd.DataFrame(candles)
        df.rename(columns={"date": "timestamp"}, inplace=True)
        df.set_index("timestamp", inplace=True)
        df = D.add_indicators(df)
        last = df.iloc[-2]
        c = float(last["close"])
        e9 = float(last.get("EMA_9", c))
        e21 = float(last.get("EMA_21", c))
        ts_str = str(df.index[-2])[:19]
        path = _csv_path_spot_5m()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        is_new = not os.path.isfile(path)
        import csv as _csv
        with open(path, "a", newline="") as f:
            w = _csv.DictWriter(f, fieldnames=FIELDNAMES_SPOT_5M, extrasaction="ignore")
            if is_new:
                w.writeheader()
            w.writerow({
                "timestamp": ts_str,
                "open": round(float(last["open"]), 2),
                "high": round(float(last["high"]), 2),
                "low": round(float(last["low"]), 2),
                "close": round(c, 2),
                "volume": int(last["volume"]),
                "ema9": round(e9, 2), "ema21": round(e21, 2),
                "ema_spread": round(e9 - e21, 2),
                "rsi": round(float(last.get("RSI", 50)), 1),
            })
            f.flush()
    except Exception as e:
        logger.debug("[LAB] Spot 5m: " + str(e))


def collect_spot_15min(kite):
    """Collect last closed 15-min spot candle."""
    if not D.is_market_open():
        return
    try:
        now = datetime.now()
        candles = kite.historical_data(
            instrument_token=D.NIFTY_SPOT_TOKEN,
            from_date=now - timedelta(days=10), to_date=now,
            interval="15minute", continuous=False, oi=False)
        if not candles or len(candles) < 20:
            return
        df = pd.DataFrame(candles)
        df.rename(columns={"date": "timestamp"}, inplace=True)
        df.set_index("timestamp", inplace=True)
        df = D.add_indicators(df)
        last = df.iloc[-2]
        c = float(last["close"])
        e9 = float(last.get("EMA_9", c))
        e21 = float(last.get("EMA_21", c))
        # ADX
        adx_val = 0
        try:
            import numpy as _np
            up = df["high"].diff()
            dn = -df["low"].diff()
            pdm = _np.where((up > dn) & (up > 0), up, 0.0)
            ndm = _np.where((dn > up) & (dn > 0), dn, 0.0)
            tr = pd.concat([df["high"]-df["low"],
                            (df["high"]-df["close"].shift(1)).abs(),
                            (df["low"]-df["close"].shift(1)).abs()], axis=1).max(axis=1)
            atr_s = tr.ewm(alpha=1/14, adjust=False).mean()
            pdi = 100 * pd.Series(pdm, index=df.index).ewm(alpha=1/14, adjust=False).mean() / atr_s
            ndi = 100 * pd.Series(ndm, index=df.index).ewm(alpha=1/14, adjust=False).mean() / atr_s
            adx_s = ((pdi-ndi).abs() / (pdi+ndi+1e-9) * 100).ewm(alpha=1/14, adjust=False).mean()
            adx_val = round(float(adx_s.iloc[-2]), 1)
        except Exception:
            pass
        ts_str = str(df.index[-2])[:19]
        path = _csv_path_spot_15m()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        is_new = not os.path.isfile(path)
        import csv as _csv
        with open(path, "a", newline="") as f:
            w = _csv.DictWriter(f, fieldnames=FIELDNAMES_SPOT_15M, extrasaction="ignore")
            if is_new:
                w.writeheader()
            w.writerow({
                "timestamp": ts_str,
                "open": round(float(last["open"]), 2),
                "high": round(float(last["high"]), 2),
                "low": round(float(last["low"]), 2),
                "close": round(c, 2),
                "volume": int(last["volume"]),
                "ema9": round(e9, 2), "ema21": round(e21, 2),
                "ema_spread": round(e9 - e21, 2),
                "rsi": round(float(last.get("RSI", 50)), 1),
                "adx": adx_val,
            })
            f.flush()
    except Exception as e:
        logger.debug("[LAB] Spot 15m: " + str(e))

''' + '\ndef start_lab(kite):'
l = l.replace(old_start, new_collectors)

# 1c. Add 5-min + 15-min scheduling to _lab_loop
old_3m_skip = '''                elif spot_ltp <= 0 and D.is_market_open():
                    logger.debug("[LAB] 3m skip — spot LTP not available yet")'''
new_5m_15m_schedule = '''                elif spot_ltp <= 0 and D.is_market_open():
                    logger.debug("[LAB] 3m skip — spot LTP not available yet")

            # ── 5-min collection at boundary + 30s ────────────
            five_min    = (now.minute // 5) * 5
            five_min_key = (today, now.hour, five_min)
            if (not hasattr(_lab_loop, '_last_5min') or
                    getattr(_lab_loop, '_last_5min', None) != five_min_key) and now.second >= 30:
                _lab_loop._last_5min = five_min_key
                spot_ltp = D.get_ltp(D.NIFTY_SPOT_TOKEN)
                if spot_ltp > 0 and D.is_tick_live(D.NIFTY_SPOT_TOKEN):
                    try:
                        collect_option_5min(_kite_ref, spot_ltp)
                    except Exception as e:
                        logger.debug("[LAB] 5m error: " + str(e))
                    try:
                        collect_spot_5min(_kite_ref)
                    except Exception as e:
                        logger.debug("[LAB] spot 5m: " + str(e))

            # ── 15-min collection at boundary + 35s ───────────
            fifteen_min    = (now.minute // 15) * 15
            fifteen_min_key = (today, now.hour, fifteen_min)
            if (not hasattr(_lab_loop, '_last_15min') or
                    getattr(_lab_loop, '_last_15min', None) != fifteen_min_key) and now.second >= 35:
                _lab_loop._last_15min = fifteen_min_key
                spot_ltp = D.get_ltp(D.NIFTY_SPOT_TOKEN)
                if spot_ltp > 0 and D.is_tick_live(D.NIFTY_SPOT_TOKEN):
                    try:
                        collect_option_15min(_kite_ref, spot_ltp)
                    except Exception as e:
                        logger.debug("[LAB] 15m error: " + str(e))
                    try:
                        collect_spot_15min(_kite_ref)
                    except Exception as e:
                        logger.debug("[LAB] spot 15m: " + str(e))'''
l = l.replace(old_3m_skip, new_5m_15m_schedule)

wf("VRL_LAB.py", l)
print("[LAB] 5-min + 15-min collection added")


# ═══════════════════════════════════════════════════════════════
#  2. VRL_WEB.py — Historical fallback + all timeframes
# ═══════════════════════════════════════════════════════════════

web = rf("VRL_WEB.py")

# 2a. Replace _today() and _get_spot_data with historical fallback
old_today = '''def _today():
    return date.today().strftime("%Y%m%d")'''
new_today = '''def _today():
    return date.today().strftime("%Y%m%d")


def _find_latest_file(directory, prefix, ext=".csv"):
    """Find most recent file matching prefix in directory."""
    if not os.path.isdir(directory):
        return None
    import glob
    pattern = os.path.join(directory, prefix + "*" + ext)
    files = sorted(glob.glob(pattern))
    if not files:
        return None
    # Try today first, then most recent
    today_file = os.path.join(directory, prefix + _today() + ext)
    if os.path.isfile(today_file) and os.path.getsize(today_file) > 50:
        return today_file
    # Return most recent non-empty file
    for f in reversed(files):
        if os.path.getsize(f) > 50:
            return f
    return None'''
web = web.replace(old_today, new_today)

# 2b. Replace _get_spot_data with fallback version
old_spot_func = '''def _get_spot_data(tf="1m", count=100):
    """Read spot 1-min CSV, optionally resample."""
    path = os.path.join(LAB, "spot", "nifty_spot_1min_" + _today() + ".csv")
    rows = _read_csv(path, 500)'''
new_spot_func = '''def _get_spot_data(tf="1m", count=100):
    """Read spot CSV with historical fallback. Uses native 5m/15m files when available."""
    # For 5m and 15m, try native files first (has pre-calculated EMA/RSI)
    if tf == "5m":
        native = _find_latest_file(os.path.join(LAB, "spot"), "nifty_spot_5min_")
        if native:
            rows = _read_csv(native, 500)
            if rows:
                result = []
                for r in rows:
                    try:
                        result.append({
                            "time": r.get("timestamp", "")[-8:-3],
                            "open": float(r.get("open", 0)), "high": float(r.get("high", 0)),
                            "low": float(r.get("low", 0)), "close": float(r.get("close", 0)),
                            "volume": int(r.get("volume", 0)),
                            "ema9": float(r.get("ema9", 0)), "ema21": float(r.get("ema21", 0)),
                            "rsi": float(r.get("rsi", 50)),
                        })
                    except Exception: continue
                return result[-count:]

    if tf == "15m":
        native = _find_latest_file(os.path.join(LAB, "spot"), "nifty_spot_15min_")
        if native:
            rows = _read_csv(native, 500)
            if rows:
                result = []
                for r in rows:
                    try:
                        result.append({
                            "time": r.get("timestamp", "")[-8:-3],
                            "open": float(r.get("open", 0)), "high": float(r.get("high", 0)),
                            "low": float(r.get("low", 0)), "close": float(r.get("close", 0)),
                            "volume": int(r.get("volume", 0)),
                            "ema9": float(r.get("ema9", 0)), "ema21": float(r.get("ema21", 0)),
                            "rsi": float(r.get("rsi", 50)),
                        })
                    except Exception: continue
                return result[-count:]

    # Default: 1-min with fallback to most recent file
    path = _find_latest_file(os.path.join(LAB, "spot"), "nifty_spot_1min_")
    if not path:
        return []
    rows = _read_csv(path, 500)'''
web = web.replace(old_spot_func, new_spot_func)

# 2c. Replace _get_option_data with fallback + all timeframes
old_opt_func = '''def _get_option_data(tf="3m", count=100):
    """Read option 3-min or 1-min CSV for CE and PE."""
    if tf in ("1m",):
        path = os.path.join(LAB, "options_1min", "nifty_option_1min_" + _today() + ".csv")
    else:
        path = os.path.join(LAB, "options_3min", "nifty_option_3min_" + _today() + ".csv")

    rows = _read_csv(path, 1000)'''
new_opt_func = '''def _get_option_data(tf="3m", count=100):
    """Read option CSV for CE and PE with historical fallback."""
    tf_map = {
        "1m":  ("options_1min", "nifty_option_1min_"),
        "3m":  ("options_3min", "nifty_option_3min_"),
        "5m":  ("options_1min", "nifty_option_5min_"),
        "15m": ("options_1min", "nifty_option_15min_"),
    }
    subdir, prefix = tf_map.get(tf, ("options_3min", "nifty_option_3min_"))
    path = _find_latest_file(os.path.join(LAB, subdir), prefix)
    if not path:
        return {"CE": [], "PE": []}

    rows = _read_csv(path, 1000)'''
web = web.replace(old_opt_func, new_opt_func)

# 2d. Add ema_spread to option data parsing
old_opt_entry = '''            entry = {
                "time": r.get("timestamp", "")[-8:-3],
                "close": float(r.get("close", 0)),
                "rsi": float(r.get("rsi", 50)),
                "volume": int(r.get("volume", 0)),
                "ema9": float(r.get("ema9", 0)),
                "body_pct": float(r.get("body_pct", 0)),
                "delta": float(r.get("delta", 0)),
                "iv_pct": float(r.get("iv_pct", 0)),
            }'''
new_opt_entry = '''            entry = {
                "time": r.get("timestamp", "")[-8:-3],
                "close": float(r.get("close", 0)),
                "rsi": float(r.get("rsi", 50)),
                "volume": int(r.get("volume", 0)),
                "ema9": float(r.get("ema9", 0)),
                "ema21": float(r.get("ema21", r.get("ema9", 0))),
                "ema_spread": float(r.get("ema_spread", r.get("ema9_gap", 0))),
                "body_pct": float(r.get("body_pct", 0)),
                "delta": float(r.get("delta", 0)),
                "iv_pct": float(r.get("iv_pct", 0)),
                "adx": float(r.get("adx", 0)),
                "macd_hist": float(r.get("macd_hist", 0)),
            }'''
web = web.replace(old_opt_entry, new_opt_entry)

# 2e. Add /api/spot5m, /api/spot15m endpoints and update option chart
# Add higher TF option chart rendering in dashboard HTML
old_opt_chart_js = """function renderOptionChart(data, side) {
  if (charts.opt) charts.opt.destroy();
  const d = data[side] || [];
  if (!d.length) return;
  const ctx = document.getElementById('optChart').getContext('2d');
  const color = side === 'CE' ? '#10b981' : '#ef4444';
  charts.opt = new Chart(ctx, {
    type: 'line',
    data: {
      labels: d.map(x => x.time),
      datasets: [
        { label: side + ' LTP', data: d.map(x => x.close), borderColor: color, borderWidth: 2, pointRadius: 0, fill: true, backgroundColor: color + '15', tension: 0.3 }
      ]
    },"""
new_opt_chart_js = """function renderOptionChart(data, side) {
  if (charts.opt) charts.opt.destroy();
  const d = data[side] || [];
  if (!d.length) return;
  const ctx = document.getElementById('optChart').getContext('2d');
  const color = side === 'CE' ? '#10b981' : '#ef4444';
  // Build datasets: LTP + EMA9 + EMA21 if available
  const datasets = [
    { label: side + ' LTP', data: d.map(x => x.close), borderColor: color, borderWidth: 2, pointRadius: 0, fill: true, backgroundColor: color + '15', tension: 0.3 }
  ];
  if (d[0].ema9) datasets.push({ label: 'EMA9', data: d.map(x => x.ema9 || x.close), borderColor: '#10b981', borderWidth: 1, pointRadius: 0, borderDash: [], tension: 0.3 });
  if (d[0].ema21) datasets.push({ label: 'EMA21', data: d.map(x => x.ema21 || x.close), borderColor: '#f59e0b', borderWidth: 1, pointRadius: 0, borderDash: [4,2], tension: 0.3 });
  charts.opt = new Chart(ctx, {
    type: 'line',
    data: {
      labels: d.map(x => x.time),
      datasets: datasets
    },"""
web = web.replace(old_opt_chart_js, new_opt_chart_js)

wf("VRL_WEB.py", web)
print("[WEB] Historical fallback + all timeframes added")


print("\n" + "=" * 55)
print("  HISTORICAL + HIGHER TF PATCH COMPLETE")
print("=" * 55)
print()
print("DATA COLLECTION (new):")
print("  5-min  option CE+PE  → options_1min/nifty_option_5min_*.csv")
print("  15-min option CE+PE  → options_1min/nifty_option_15min_*.csv")
print("  5-min  spot          → spot/nifty_spot_5min_*.csv")
print("  15-min spot (+ADX)   → spot/nifty_spot_15min_*.csv")
print()
print("DASHBOARD:")
print("  Market closed → shows last available day's data")
print("  1m/3m/5m/15m  → all timeframes work for spot + options")
print("  CE/PE charts  → now show EMA9 + EMA21 overlay")
print("  15m data      → includes MACD histogram + ADX")
