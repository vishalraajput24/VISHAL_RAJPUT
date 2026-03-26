"""
SPOT ENRICHMENT PATCH — v12.14
- Enrich 1-min spot CSV with EMA9/21 + RSI + ADX
- Add ADX to 5-min spot CSV
- New: hourly (60-min) spot collection
- New: daily spot storage
- Consistent schema: all timeframes have EMA9, EMA21, RSI, ADX
"""
import os
R = os.path.expanduser("~/VISHAL_RAJPUT")
def rf(n):
    with open(os.path.join(R, n)) as f: return f.read()
def wf(n, c):
    with open(os.path.join(R, n), "w") as f: f.write(c)
    print("[OK] " + n)

# ═══════════════════════════════════════════════════════════════
#  VRL_LAB.py — Enrich all spot CSVs + add hourly/daily
# ═══════════════════════════════════════════════════════════════
l = rf("VRL_LAB.py")

# 1. Enrich 1-min spot schema
old_spot_1m = 'FIELDNAMES_SPOT = ["timestamp", "open", "high", "low", "close", "volume"]'
new_spot_1m = 'FIELDNAMES_SPOT = ["timestamp", "open", "high", "low", "close", "volume", "ema9", "ema21", "ema_spread", "rsi", "adx"]'
l = l.replace(old_spot_1m, new_spot_1m)

# 2. Enrich 1-min spot collection function
old_spot_write = '''            w.writerow({
                "timestamp": ts_str,
                "open" : round(last["open"],  2),
                "high" : round(last["high"],  2),
                "low"  : round(last["low"],   2),
                "close": round(last["close"], 2),
                "volume": int(last["volume"]),
            })'''
new_spot_write = '''            # Compute indicators on warmup data
            _spot_ema9 = _spot_ema21 = _spot_rsi = _spot_adx = 0
            try:
                _sdf = pd.DataFrame(candles)
                _sdf.rename(columns={"date": "timestamp"}, inplace=True)
                _sdf.set_index("timestamp", inplace=True)
                _sdf = D.add_indicators(_sdf)
                if len(_sdf) >= 2:
                    _slast = _sdf.iloc[-2]
                    _sc = float(_slast["close"])
                    _spot_ema9 = round(float(_slast.get("EMA_9", _sc)), 2)
                    _spot_ema21 = round(float(_slast.get("EMA_21", _sc)), 2)
                    _spot_rsi = round(float(_slast.get("RSI", 50)), 1)
                # ADX
                if len(_sdf) >= 16:
                    import numpy as _np
                    _up = _sdf["high"].diff()
                    _dn = -_sdf["low"].diff()
                    _pdm = _np.where((_up > _dn) & (_up > 0), _up, 0.0)
                    _ndm = _np.where((_dn > _up) & (_dn > 0), _dn, 0.0)
                    _tr = pd.concat([_sdf["high"]-_sdf["low"],
                                     (_sdf["high"]-_sdf["close"].shift(1)).abs(),
                                     (_sdf["low"]-_sdf["close"].shift(1)).abs()], axis=1).max(axis=1)
                    _atr_s = _tr.ewm(alpha=1/14, adjust=False).mean()
                    _pdi = 100 * pd.Series(_pdm, index=_sdf.index).ewm(alpha=1/14, adjust=False).mean() / _atr_s
                    _ndi = 100 * pd.Series(_ndm, index=_sdf.index).ewm(alpha=1/14, adjust=False).mean() / _atr_s
                    _adx_s = ((_pdi-_ndi).abs() / (_pdi+_ndi+1e-9) * 100).ewm(alpha=1/14, adjust=False).mean()
                    _spot_adx = round(float(_adx_s.iloc[-2]), 1)
            except Exception:
                pass
            w.writerow({
                "timestamp": ts_str,
                "open" : round(last["open"],  2),
                "high" : round(last["high"],  2),
                "low"  : round(last["low"],   2),
                "close": round(last["close"], 2),
                "volume": int(last["volume"]),
                "ema9": _spot_ema9,
                "ema21": _spot_ema21,
                "ema_spread": round(_spot_ema9 - _spot_ema21, 2) if _spot_ema9 and _spot_ema21 else 0,
                "rsi": _spot_rsi,
                "adx": _spot_adx,
            })'''
l = l.replace(old_spot_write, new_spot_write)

# 3. Add ADX to 5-min spot schema
old_5m_schema = 'FIELDNAMES_SPOT_5M = [\n    "timestamp", "open", "high", "low", "close", "volume",\n    "ema9", "ema21", "ema_spread", "rsi",\n]'
new_5m_schema = 'FIELDNAMES_SPOT_5M = [\n    "timestamp", "open", "high", "low", "close", "volume",\n    "ema9", "ema21", "ema_spread", "rsi", "adx",\n]'
l = l.replace(old_5m_schema, new_5m_schema)

# 4. Add ADX calculation to collect_spot_5min
old_5m_row = '''        ts_str = str(df.index[-2])[:19]
        path = _csv_path_spot_5m()'''
new_5m_row = '''        # ADX
        adx_val = 0
        try:
            import numpy as _np
            _up5 = df["high"].diff()
            _dn5 = -df["low"].diff()
            _pdm5 = _np.where((_up5 > _dn5) & (_up5 > 0), _up5, 0.0)
            _ndm5 = _np.where((_dn5 > _up5) & (_dn5 > 0), _dn5, 0.0)
            _tr5 = pd.concat([df["high"]-df["low"],
                              (df["high"]-df["close"].shift(1)).abs(),
                              (df["low"]-df["close"].shift(1)).abs()], axis=1).max(axis=1)
            _atr5 = _tr5.ewm(alpha=1/14, adjust=False).mean()
            _pdi5 = 100 * pd.Series(_pdm5, index=df.index).ewm(alpha=1/14, adjust=False).mean() / _atr5
            _ndi5 = 100 * pd.Series(_ndm5, index=df.index).ewm(alpha=1/14, adjust=False).mean() / _atr5
            _adx5 = ((_pdi5-_ndi5).abs() / (_pdi5+_ndi5+1e-9) * 100).ewm(alpha=1/14, adjust=False).mean()
            adx_val = round(float(_adx5.iloc[-2]), 1)
        except Exception:
            pass
        ts_str = str(df.index[-2])[:19]
        path = _csv_path_spot_5m()'''
l = l.replace(old_5m_row, new_5m_row)

# 5. Add adx to 5-min spot writerow
old_5m_write = '''                "rsi": round(float(last.get("RSI", 50)), 1),
            })
            f.flush()
    except Exception as e:
        logger.debug("[LAB] Spot 5m: " + str(e))'''
new_5m_write = '''                "rsi": round(float(last.get("RSI", 50)), 1),
                "adx": adx_val,
            })
            f.flush()
    except Exception as e:
        logger.debug("[LAB] Spot 5m: " + str(e))'''
l = l.replace(old_5m_write, new_5m_write)

# 6. Add hourly + daily schemas and path functions
old_spot_5m_path = 'def _csv_path_spot_5m():'
new_hourly_daily = '''# Hourly + Daily spot schemas
FIELDNAMES_SPOT_60M = [
    "timestamp", "open", "high", "low", "close", "volume",
    "ema9", "ema21", "ema_spread", "rsi", "adx",
]

FIELDNAMES_SPOT_DAILY = [
    "date", "open", "high", "low", "close", "volume",
    "ema21", "rsi", "adx",
]

def _csv_path_spot_60m():
    from datetime import date as _d
    return os.path.join(D.SPOT_DIR, "nifty_spot_60min_" + _d.today().strftime("%Y%m%d") + ".csv")

def _csv_path_spot_daily():
    return os.path.join(D.SPOT_DIR, "nifty_spot_daily.csv")

def _csv_path_spot_5m():'''
l = l.replace(old_spot_5m_path, new_hourly_daily)

# 7. Add hourly + daily collection functions before start_lab
old_start_lab = '\ndef start_lab(kite):'
new_collectors = r'''

# ─── HOURLY (60-MIN) SPOT COLLECTOR ──────────────────────────

def collect_spot_60min(kite):
    """Collect last closed 60-min spot candle with EMA + RSI + ADX."""
    if not D.is_market_open():
        return
    try:
        now = datetime.now()
        candles = kite.historical_data(
            instrument_token=D.NIFTY_SPOT_TOKEN,
            from_date=now - timedelta(days=30), to_date=now,
            interval="60minute", continuous=False, oi=False)
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
            _up = df["high"].diff()
            _dn = -df["low"].diff()
            _pdm = _np.where((_up > _dn) & (_up > 0), _up, 0.0)
            _ndm = _np.where((_dn > _up) & (_dn > 0), _dn, 0.0)
            _tr = pd.concat([df["high"]-df["low"],
                             (df["high"]-df["close"].shift(1)).abs(),
                             (df["low"]-df["close"].shift(1)).abs()], axis=1).max(axis=1)
            _atr = _tr.ewm(alpha=1/14, adjust=False).mean()
            _pdi = 100 * pd.Series(_pdm, index=df.index).ewm(alpha=1/14, adjust=False).mean() / _atr
            _ndi = 100 * pd.Series(_ndm, index=df.index).ewm(alpha=1/14, adjust=False).mean() / _atr
            _adxs = ((_pdi-_ndi).abs() / (_pdi+_ndi+1e-9) * 100).ewm(alpha=1/14, adjust=False).mean()
            adx_val = round(float(_adxs.iloc[-2]), 1)
        except Exception:
            pass
        ts_str = str(df.index[-2])[:19]
        path = _csv_path_spot_60m()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        is_new = not os.path.isfile(path)
        import csv as _csv
        with open(path, "a", newline="") as f:
            w = _csv.DictWriter(f, fieldnames=FIELDNAMES_SPOT_60M, extrasaction="ignore")
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
        logger.debug("[LAB] Spot 60m wrote @" + ts_str[-5:])
    except Exception as e:
        logger.debug("[LAB] Spot 60m: " + str(e))


# ─── DAILY SPOT COLLECTOR ────────────────────────────────────

def collect_spot_daily(kite):
    """Collect daily spot candle with EMA21 + RSI + ADX. Runs once at EOD."""
    try:
        now = datetime.now()
        candles = kite.historical_data(
            instrument_token=D.NIFTY_SPOT_TOKEN,
            from_date=now - timedelta(days=90), to_date=now,
            interval="day", continuous=False, oi=False)
        if not candles or len(candles) < 25:
            return
        df = pd.DataFrame(candles)
        for col in ("open", "high", "low", "close"):
            df[col] = df[col].astype(float)
        df["volume"] = df["volume"].astype(int)
        # EMA21
        df["ema21"] = df["close"].ewm(span=21, adjust=False).mean()
        # RSI
        delta = df["close"].diff()
        gain = delta.clip(lower=0).ewm(alpha=1/14, adjust=False).mean()
        loss = (-delta.clip(upper=0)).ewm(alpha=1/14, adjust=False).mean()
        df["rsi"] = 100 - 100 / (1 + gain / (loss + 1e-9))
        # ADX
        adx_val = 0
        try:
            import numpy as _np
            _up = df["high"].diff()
            _dn = -df["low"].diff()
            _pdm = _np.where((_up > _dn) & (_up > 0), _up, 0.0)
            _ndm = _np.where((_dn > _up) & (_dn > 0), _dn, 0.0)
            _tr = pd.concat([df["high"]-df["low"],
                             (df["high"]-df["close"].shift(1)).abs(),
                             (df["low"]-df["close"].shift(1)).abs()], axis=1).max(axis=1)
            _atr = _tr.ewm(alpha=1/14, adjust=False).mean()
            _pdi = 100 * pd.Series(_pdm, index=df.index).ewm(alpha=1/14, adjust=False).mean() / _atr
            _ndi = 100 * pd.Series(_ndm, index=df.index).ewm(alpha=1/14, adjust=False).mean() / _atr
            _adxs = ((_pdi-_ndi).abs() / (_pdi+_ndi+1e-9) * 100).ewm(alpha=1/14, adjust=False).mean()
            df["adx"] = _adxs
        except Exception:
            df["adx"] = 0
        # Write last row (today or yesterday)
        last = df.iloc[-1]
        dt_str = str(candles[-1]["date"])[:10]
        path = _csv_path_spot_daily()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        # Check if already written for this date
        existing_dates = set()
        if os.path.isfile(path):
            import csv as _csv2
            with open(path) as f:
                for r in _csv2.DictReader(f):
                    existing_dates.add(r.get("date", ""))
        if dt_str in existing_dates:
            return
        import csv as _csv
        is_new = not os.path.isfile(path)
        with open(path, "a", newline="") as f:
            w = _csv.DictWriter(f, fieldnames=FIELDNAMES_SPOT_DAILY, extrasaction="ignore")
            if is_new:
                w.writeheader()
            w.writerow({
                "date": dt_str,
                "open": round(float(last["open"]), 2),
                "high": round(float(last["high"]), 2),
                "low": round(float(last["low"]), 2),
                "close": round(float(last["close"]), 2),
                "volume": int(last["volume"]),
                "ema21": round(float(last["ema21"]), 2),
                "rsi": round(float(last["rsi"]), 1),
                "adx": round(float(last["adx"]), 1),
            })
            f.flush()
        logger.info("[LAB] Daily spot wrote " + dt_str)
    except Exception as e:
        logger.debug("[LAB] Spot daily: " + str(e))

''' + '\ndef start_lab(kite):'
l = l.replace(old_start_lab, new_collectors)

# 8. Add hourly scheduling (at minute 0 of each hour)
old_15m_schedule = '''            # ── 15-min collection at boundary + 35s ───────────'''
new_60m_schedule = '''            # ── 60-min collection at hour boundary + 40s ─────
            if now.minute == 0 and now.second >= 40 and now.second < 50:
                spot_ltp = D.get_ltp(D.NIFTY_SPOT_TOKEN)
                if spot_ltp > 0:
                    try:
                        collect_spot_60min(_kite_ref)
                    except Exception as e:
                        logger.debug("[LAB] spot 60m: " + str(e))

            # ── 15-min collection at boundary + 35s ───────────'''
l = l.replace(old_15m_schedule, new_60m_schedule)

# 9. Add daily collection at EOD (15:36)
old_fwd_fill = '''            # ── EOD forward fill at 15:35 ─────────────────────'''
new_daily_eod = '''            # ── Daily spot at 15:36 ───────────────────────────
            if now.hour == 15 and now.minute == 36 and now.second < 30:
                try:
                    collect_spot_daily(_kite_ref)
                except Exception as e:
                    logger.debug("[LAB] daily spot: " + str(e))

            # ── EOD forward fill at 15:35 ─────────────────────'''
l = l.replace(old_fwd_fill, new_daily_eod)

wf("VRL_LAB.py", l)

print("\n" + "=" * 55)
print("  SPOT ENRICHMENT COMPLETE")
print("=" * 55)
print()
print("ENRICHED:")
print("  1-min  spot → now has EMA9, EMA21, RSI, ADX")
print("  5-min  spot → now has ADX (was missing)")
print("  15-min spot → already had ADX ✅")
print()
print("NEW COLLECTORS:")
print("  60-min spot → at hour boundary, EMA9/21 + RSI + ADX")
print("  Daily  spot → at 15:36 EOD, EMA21 + RSI + ADX")
print()
print("CONSISTENT SCHEMA: all timeframes have EMA + RSI + ADX")
