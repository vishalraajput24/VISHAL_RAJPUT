# ═══════════════════════════════════════════════════════════════
#  VRL_COMMANDS.py — VISHAL RAJPUT TRADE v13.8
#  Telegram command handlers.
# ═══════════════════════════════════════════════════════════════

import csv
import json
import os
import sys
import time
import zipfile
from datetime import date, datetime

import VRL_DATA as D

import logging
logger = logging.getLogger("vrl_live")

# Dynamic public IP — resolved once at module load
_WEB_IP = ""
try:
    import subprocess as _sp
    _WEB_IP = _sp.check_output(["curl", "-s", "ifconfig.me"], timeout=5).decode().strip()
except Exception:
    _WEB_IP = "unknown"

# ── Module refs (set by setup()) ──────────────────────────────
state       = None
_state_lock = None
_tg_send    = None
_tg_send_file = None
_tg_inline_keyboard = None
_tg_answer_callback = None
_save_state = None
_read_today_trades = None
_remove_pid = None
_now_str    = None
_rs         = None
_kite       = None

def setup(state_ref, lock_ref, tg_send_fn, tg_send_file_fn,
          tg_inline_keyboard_fn, tg_answer_callback_fn,
          save_state_fn, read_today_trades_fn, remove_pid_fn,
          now_str_fn, rs_fn, kite_ref):
    """Wire module refs from VRL_MAIN.py."""
    global state, _state_lock, _tg_send, _tg_send_file
    global _tg_inline_keyboard, _tg_answer_callback
    global _save_state, _read_today_trades, _remove_pid
    global _now_str, _rs, _kite
    state       = state_ref
    _state_lock = lock_ref
    _tg_send    = tg_send_fn
    _tg_send_file = tg_send_file_fn
    _tg_inline_keyboard = tg_inline_keyboard_fn
    _tg_answer_callback = tg_answer_callback_fn
    _save_state = save_state_fn
    _read_today_trades = read_today_trades_fn
    _remove_pid = remove_pid_fn
    _now_str    = now_str_fn
    _rs         = rs_fn
    _kite       = kite_ref


# ═══════════════════════════════════════════════════════════════
#  FILE BROWSER
# ═══════════════════════════════════════════════════════════════

_RESEARCH_DIR = os.path.expanduser("~/research")

_BROWSER_ROOTS = {
    "trade_log"   : os.path.dirname(D.TRADE_LOG_PATH),
    "lab_spot"    : D.SPOT_DIR,
    "lab_options" : D.OPTIONS_3MIN_DIR,
    "lab_1min"    : D.OPTIONS_1MIN_DIR,
    "lab_reports" : D.REPORTS_DIR,
    "research"    : _RESEARCH_DIR,
    "state"       : D.STATE_DIR,
    "logs_live"   : D.LIVE_LOG_DIR,
}

_BROWSER_LABELS = {
    "trade_log"   : "📒 Trade Log",
    "lab_spot"    : "📈 Spot (1m/5m/15m/D)",
    "lab_options" : "📊 Options 3-Min CE+PE",
    "lab_1min"    : "📊 Options 1m/5m/15m/Scan",
    "lab_reports" : "📑 Daily Summary",
    "research"    : "🔭 Zones + Research",
    "state"       : "⚙️ State + Config",
    "logs_live"   : "📋 Logs",
}

def _send_file_browser():
    keyboard = []
    row = []
    for key, label in _BROWSER_LABELS.items():
        row.append({"text": label, "callback_data": "FB:" + key})
        if len(row) == 2:
            keyboard.append(row)
            row = []
    if row:
        keyboard.append(row)
    _tg_inline_keyboard("📁 <b>FILE BROWSER</b>\nSelect a folder:", keyboard)

def _handle_file_browser_callback(callback_data: str,
                                   callback_query_id: str):
    _tg_answer_callback(callback_query_id)
    parts = callback_data.split(":")
    if len(parts) < 2:
        return
    folder_key  = parts[1]
    folder_path = _BROWSER_ROOTS.get(folder_key)

    if not folder_path or not os.path.isdir(folder_path):
        _tg_send("Folder not found: " + folder_key)
        return

    if len(parts) == 3:
        filename  = os.path.basename(parts[2])  # sanitise: strip ../ traversal
        file_path = os.path.join(folder_path, filename)
        resolved  = os.path.realpath(file_path)
        if not resolved.startswith(os.path.realpath(folder_path)):
            _tg_send("Access denied: invalid path")
            return
        if os.path.isfile(resolved):
            size_kb = round(os.path.getsize(resolved) / 1024, 1)
            _tg_send_file(resolved, caption=filename + " (" + str(size_kb) + " KB)")
        else:
            _tg_send("File not found: " + filename)
        return

    try:
        items = sorted(os.listdir(folder_path))
        files = [i for i in items if os.path.isfile(os.path.join(folder_path, i))]
    except Exception as e:
        _tg_send("Error reading folder: " + str(e))
        return

    if not files:
        _tg_send("📂 " + _BROWSER_LABELS.get(folder_key, folder_key) + "\nNo files found.")
        return

    keyboard = []
    for filename in files[-20:]:
        size_kb = round(os.path.getsize(os.path.join(folder_path, filename)) / 1024, 1)
        label   = filename + " (" + str(size_kb) + "KB)"
        keyboard.append([{"text": label,
                           "callback_data": "FB:" + folder_key + ":" + filename}])
    keyboard.append([{"text": "⬇️ Download All (zip)",
                       "callback_data": "DL:" + folder_key}])
    _tg_inline_keyboard("📂 <b>" + _BROWSER_LABELS.get(folder_key, folder_key)
                        + "</b>\n" + str(len(files)) + " file(s):", keyboard)

def _handle_download_callback(callback_data: str,
                               callback_query_id: str):
    _tg_answer_callback(callback_query_id, "Zipping files...")
    parts = callback_data.split(":")
    if len(parts) < 2:
        return
    folder_key  = parts[1]
    folder_path = _BROWSER_ROOTS.get(folder_key)
    if not folder_path or not os.path.isdir(folder_path):
        _tg_send("Cannot zip: folder not found")
        return
    zip_path = os.path.join(D.STATE_DIR, folder_key + "_export.zip")
    try:
        total_size = sum(
            os.path.getsize(os.path.join(folder_path, f))
            for f in os.listdir(folder_path)
            if os.path.isfile(os.path.join(folder_path, f))
        )
        if total_size > 40 * 1024 * 1024:
            _tg_send("⚠️ Folder too large (" + str(round(total_size / (1024*1024), 1))
                     + " MB). Use /download for today's files only.")
            return
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for fname in os.listdir(folder_path):
                fpath = os.path.join(folder_path, fname)
                if os.path.isfile(fpath):
                    zf.write(fpath, fname)
        size_mb = round(os.path.getsize(zip_path) / (1024 * 1024), 2)
        _tg_send_file(zip_path, caption=folder_key + "_export.zip (" + str(size_mb) + " MB)")
    except Exception as e:
        _tg_send("Zip error: " + str(e))

def _send_today_download(target_date: str = None):
    """
    Central log download — collects ALL logs + data for a date into one zip.
    /download        → today's logs
    /download 2026-04-01  → specific date logs
    """
    if target_date is None:
        target_date = date.today().strftime("%Y-%m-%d")

    files = D.collect_logs_for_date(target_date)
    if not files:
        _tg_send("No files found for " + target_date)
        return

    zip_path = D.create_daily_zip(target_date)
    if not zip_path or not os.path.isfile(zip_path):
        _tg_send("Failed to create zip for " + target_date)
        return

    try:
        size_mb = round(os.path.getsize(zip_path) / (1024 * 1024), 2)
        file_count = len(files)

        # Build category summary
        categories = {}
        for _, arcname in files:
            cat = arcname.split("/")[0]
            categories[cat] = categories.get(cat, 0) + 1
        cat_summary = " | ".join(k + ":" + str(v) for k, v in sorted(categories.items()))

        _tg_send_file(
            zip_path,
            caption="📦 VRL Logs — " + target_date
                    + "\n" + str(file_count) + " files | "
                    + str(size_mb) + " MB"
                    + "\n" + cat_summary
        )
        try:
            os.remove(zip_path)
        except Exception:
            pass
    except Exception as e:
        _tg_send("Download error: " + str(e))

# ═══════════════════════════════════════════════════════════════
#  TELEGRAM COMMAND HANDLERS
# ═══════════════════════════════════════════════════════════════

def _why_blocked(st: dict) -> str:
    if st.get("paused"):
        return "⏸ PAUSED"
    if st.get("daily_trades", 0) >= D.MAX_DAILY_TRADES:
        return "🚫 Max trades hit (" + str(D.MAX_DAILY_TRADES) + ")"
    if st.get("daily_losses", 0) >= D.MAX_DAILY_LOSSES:
        return "🚫 Max losses hit (" + str(D.MAX_DAILY_LOSSES) + ")"
    if st.get("profit_locked"):
        return "🔒 Profit locked — trailing only"
    if st.get("consecutive_losses", 0) >= 2:
        return "⚠️ Streak=" + str(st["consecutive_losses"]) + " — score≥" + str(D.EXCELLENCE_BYPASS_SCORE) + " needed"
    return "✅ Ready to enter"

def _cmd_help(args):
    _tg_send(
        "🤖 <b>VISHAL RAJPUT TRADE " + D.VERSION + "</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "<b>TRADING</b>\n"
        "/status    — trade status + PNL\n"
        "/pnl       — P&L with charges breakdown\n"
        "/trades    — today's trade list\n"
        "/account   — balance + margin info\n"
        "/slippage  — fill quality stats\n"
        "/streak    — rolling win rate + streak\n"
        "/spot      — Spot trend + gap\n"
        "/pivot     — Fib pivot levels\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "<b>DATA</b>\n"
        "/files     — browse folders\n"
        "/download  — strategy data (trade log + DB + config + state)\n"
        "/download_all — full day zip (or /download_all YYYY-MM-DD)\n"
        "/health    — system health check\n"
        "/validate  — 10 system alignment checks\n"
        "/livecheck — last 50 log lines\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "<b>CONTROL</b>\n"
        "/pause     — block new entries\n"
        "/resume    — re-enable entries\n"
        "/forceexit — emergency exit all lots\n"
        "/restart   — restart bot\n"
        "/token     — manage subscriber access tokens\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        + ("📄 PAPER" if D.PAPER_MODE else "💰 LIVE")
        + " | FAST: 2 green above EMA9 + RSI↑ + spot confirm\n"
        + "CONFIRMED: 3m +20pts (aggressive: 15pts)\n"
        + "RSI cap: 78 morning / 72 midday / 75 afternoon\n"
        + "FLOORS: +5→-6 | +10→+2 | +20→+12 | +30→+22 | +40→+32 | +50→+42\n"
        + "SL -12 close | EOD 15:30 | No entry 15:10 | 2 lots fixed\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "🌐 Dashboard: http://" + _WEB_IP + ":8080"
    )

def _cmd_status(args):
    global _kite
    with _state_lock:
        st = dict(state)

    streak     = st.get("consecutive_losses", 0)
    streak_str = str(streak) + (" 🔴" if streak >= 2 else " ✅" if streak == 0 else "")

    if not st.get("in_trade"):
        last_scan = st.get("_last_scan", {})
        _tg_send(
            "📊 <b>STATUS — NO TRADE</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "Trades : " + str(st.get("daily_trades", 0)) + "/" + str(D.MAX_DAILY_TRADES) + "\n"
            "Losses : " + str(st.get("daily_losses", 0)) + "/" + str(D.MAX_DAILY_LOSSES) + "\n"
            "Wins   : " + str(st.get("daily_trades", 0) - st.get("daily_losses", 0)) + "\n"
            "PNL    : " + str(round(st.get("daily_pnl", 0), 1)) + "pts\n"
            "Streak : " + streak_str + "\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "Last scan : " + last_scan.get("time", "—") + "\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "Bot       : " + _why_blocked(st)
        )
        return

    ltp = 0.0
    try:
        ltp = D.get_ltp(st.get("token"))
        if ltp <= 0 and _kite is not None:
            symbol = st.get("symbol")
            if symbol:
                q = _kite.ltp(["NFO:" + symbol])
                ltp = float(q["NFO:" + symbol]["last_price"])
                logger.info("[STATUS] LTP via REST: " + str(ltp))
    except Exception as e:
        logger.warning("[STATUS] LTP fetch error: " + str(e))
        ltp = 0.0

    entry   = st.get("entry_price", 0)
    pnl     = round(ltp - entry, 1) if ltp > 0 else 0
    peak    = st.get("peak_pnl", 0)
    phase   = st.get("exit_phase", 1)

    # BUG-018: Compute active SL incorporating profit floors
    sl_val  = st.get("phase1_sl", st.get("current_floor", 0))
    if sl_val <= 0:
        sl_val = round(entry - 12, 2)  # fallback to hard SL
    # Ratchet up to highest applicable profit floor
    try:
        import VRL_CONFIG as _CFG_sl
        _floors = _CFG_sl.get().get("profit_floors", [
            {"peak": 5, "lock": -6}, {"peak": 10, "lock": 2},
            {"peak": 20, "lock": 12}, {"peak": 30, "lock": 22},
            {"peak": 40, "lock": 32}, {"peak": 50, "lock": 42},
        ])
        for _f in _floors:
            if peak >= _f.get("peak", 0):
                _candidate = round(entry + _f.get("lock", 0), 2)
                if _candidate > sl_val:
                    sl_val = _candidate
    except Exception:
        pass
    sl_dist = round(ltp - sl_val, 1) if ltp > 0 and sl_val > 0 else "—"
    md_level = "—"
    if peak > 20 and pnl > 0:
        md_level = round(entry + peak - 8, 2)

    _tg_send(
        "📊 <b>STATUS — IN TRADE</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "Time   : " + _now_str() + "\n"
        "Symbol : " + st.get("symbol", "") + "\n"
        "Mode   : " + st.get("mode", "") + "  Score: " + str(st.get("score_at_entry", "—")) + "/7\n"
        "Phase  : " + str(phase) + "\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "Entry  : " + str(round(entry, 2)) + "\n"
        "LTP    : " + str(round(ltp, 2)) + "\n"
        "PNL    : " + ("+" if pnl >= 0 else "") + str(pnl) + "pts  " + _rs(pnl) + "\n"
        "Peak   : +" + str(round(peak, 1)) + "pts\n"
        "SL     : " + str(round(sl_val, 2)) + "  (" + str(sl_dist) + "pts away)\n"
        "Tight  : " + str(st.get("trail_tightened", False)) + "\n"
        "RSI OB : " + str(st.get("_rsi_was_overbought", False)) + "\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "Trades : " + str(st.get("daily_trades", 0)) + "/" + str(D.MAX_DAILY_TRADES) + "\n"
        "Wins   : " + str(st.get("daily_trades", 0) - st.get("daily_losses", 0)) + "\n"
        "Day PNL: " + str(round(st.get("daily_pnl", 0), 1)) + "pts\n"
        "Streak : " + streak_str
    )

def _cmd_greeks(args):
    _cmd_edge(args)

def _cmd_edge(args):
    with _state_lock:
        st = dict(state)

    try:
        spot    = D.get_ltp(D.NIFTY_SPOT_TOKEN)
        vix     = D.get_vix()
        expiry  = D.get_nearest_expiry()
        dte     = D.calculate_dte(expiry) if expiry else 0
        step    = D.get_active_strike_step(dte)
        strike  = D.resolve_atm_strike(spot, step) if spot > 0 else 0
        now     = datetime.now()
        session = D.get_session_block(now.hour, now.minute)
        prof    = D.get_dte_profile(dte)
        rsi_lo  = prof["rsi_low"]       # 3-min zone (42-72)
        rsi_hi  = prof["rsi_high"]
        rsi_1m_lo = prof.get("rsi_1m_low", D.RSI_1M_LOW)   # 1-min zone (45-65)
        rsi_1m_hi = prof.get("rsi_1m_high", D.RSI_1M_HIGH)
        vol_min = prof["volume_ratio_min"]
        body_min= prof["body_pct_min"]

        # Collect CE + PE data
        data = {}
        try:
            tmap = D.get_option_tokens(None, strike, expiry)
            if tmap:
                for ot, info in tmap.items():
                    d = {
                        "ltp":0.0,"rsi":0.0,"body":0.0,"vol":0.0,
                        "rsi_rising":False,
                        "ema9_1m":0.0,"ema21_1m":0.0,"spread_1m":0.0,
                        "aligned_1m":False,
                        "ema9_3m":0.0,"ema21_3m":0.0,"spread_3m":0.0,
                        "rsi_3m":0.0,"body_3m":0.0,"conditions_3m":0,
                    }
                    try:
                        df1 = D.get_historical_data(info["token"], "minute", D.LOOKBACK_1M)
                        df1 = D.add_indicators(df1)
                        if not df1.empty and len(df1) >= 3:
                            l1 = df1.iloc[-2]; p1 = df1.iloc[-3]
                            c  = float(l1["close"])
                            rng= float(l1["high"]) - float(l1["low"])
                            vols=[df1.iloc[i]["volume"] for i in range(-7,-2) if df1.iloc[i]["volume"]>0]
                            av = sum(vols)/len(vols) if vols else 1
                            d["ltp"]        = round(c,2)
                            d["rsi"]        = round(float(l1.get("RSI",50)),1)
                            d["body"]       = round(abs(c-float(l1["open"]))/rng*100,1) if rng>0 else 0
                            d["vol"]        = round(l1["volume"]/av if av>0 else 1,2)
                            d["rsi_rising"] = d["rsi"] > round(float(p1.get("RSI",50)),1)
                            d["ema9_1m"]    = round(float(l1.get("EMA_9",c)),2)
                            d["ema21_1m"]   = round(float(l1.get("EMA_21",c)),2)
                            # v12.11: Momentum fallback only DTE ≤ 1
                            if dte <= 1 and len(df1) < 25:
                                lb1 = min(5, len(df1) - 2)
                                d["spread_1m"] = round(c - float(df1.iloc[-2-lb1]["close"]), 2)
                            else:
                                d["spread_1m"] = round(d["ema9_1m"]-d["ema21_1m"],2)
                    except Exception: pass
                    try:
                        df3 = D.get_historical_data(info["token"], "3minute", D.LOOKBACK_3M)
                        df3 = D.add_indicators(df3)
                        if not df3.empty and len(df3) >= 3:
                            l3  = df3.iloc[-2]
                            c3  = float(l3["close"])
                            rng3= float(l3["high"])-float(l3["low"])
                            e9  = round(float(l3.get("EMA_9",c3)),2)
                            e21 = round(float(l3.get("EMA_21",c3)),2)
                            d["ema9_3m"]  = e9; d["ema21_3m"] = e21
                            d["rsi_3m"]   = round(float(l3.get("RSI",50)),1)
                            d["body_3m"]  = round(abs(c3-float(l3["open"]))/rng3*100,1) if rng3>0 else 0
                            # v12.11: Momentum fallback only DTE ≤ 1 + thin candles
                            if dte <= 1 and len(df3) < 25:
                                lb3 = min(5, len(df3) - 2)
                                d["spread_3m"] = round(c3 - float(df3.iloc[-2-lb3]["close"]), 2)
                                ema_ok  = d["spread_3m"] > 0
                                avg3    = df3.iloc[-min(6,len(df3)):]["close"].mean()
                                price_ok= c3 >= avg3
                            else:
                                d["spread_3m"]= round(e9-e21,2)
                                ema_ok = e9>e21; price_ok = c3>=e9
                            # v12.11: Store keys for gate_meter display
                            d["ema_aligned_3m"] = ema_ok
                            d["price_ok_3m"]    = price_ok
                            aln1m = d["spread_1m"]>0
                            d["conditions_3m"] = sum([ema_ok, d["body_3m"]>=body_min,
                                                      rsi_lo<=d["rsi_3m"]<=rsi_hi, price_ok])
                            d["aligned_1m"] = aln1m
                    except Exception: pass
                    data[ot] = d
        except Exception: pass

        ce = data.get("CE", {})
        pe = data.get("PE", {})

        def trend_lbl(sp, ot):
            # v12.11: Both CE and PE — option trending UP = good (we buy both)
            if sp>=12: return "STRONG UP 🚀"
            if sp>=5:  return "UP 📈"
            if sp>=2:  return "WEAK ⚠️"
            if sp>=-2: return "FLAT ➡️"
            return "DOWN ❌"

        def spread1m_lbl(d, ot):
            sp  = d.get("spread_1m", 0)
            sp3 = d.get("spread_3m", 0)
            s = ("+" if sp >= 0 else "") + str(round(sp, 1)) + "pts "
            if abs(sp) < 2: return s + "FLAT ➡️"
            if sp > 0 and abs(sp3) >= 5: return s + "✅ WITH 3m 🔥"
            if sp > 0: return s + "✅ Bullish"
            return s + "❌ Need +" + str(D.SPREAD_1M_MIN_CE if ot=="CE" else D.SPREAD_1M_MIN_PE) + "pts"

        def vix_label(v):
            if v <= 0:    return "—"
            if v < 14:    return str(round(v,1)) + " LOW"
            if v < 18:    return str(round(v,1)) + " NORMAL"
            if v < 22:    return str(round(v,1)) + " ELEVATED 💥"
            return str(round(v,1)) + " CHAOS 🔥"

        def gate_meter(d, ot):
            """Show which 3-min conditions passed: E=EMA B=Body R=RSI P=Price"""
            ema_ok   = d.get("ema_aligned_3m", False)
            body_ok  = d.get("body_3m", 0) >= body_min
            rsi_ok   = rsi_lo <= d.get("rsi_3m", 0) <= rsi_hi
            price_ok = d.get("price_ok_3m", False)
            n        = d.get("conditions_3m", 0)
            meter = (("E✓" if ema_ok else "E✗") + " " +
                     ("B✓" if body_ok else "B✗") + " " +
                     ("R✓" if rsi_ok else "R✗") + " " +
                     ("P✓" if price_ok else "P✗"))
            status = "✅" if n >= 3 else "⚠️" if n == 2 else "❌"
            return str(n) + "/4 " + status + "  " + meter

        def score_line(d, ot):
            """Show score and what's missing"""
            conds  = d.get("conditions_3m", 0)
            sp1m   = d.get("spread_1m", 0)
            body   = d.get("body", 0)
            rsi    = d.get("rsi", 0)
            rising = d.get("rsi_rising", False)
            vol    = d.get("vol", 0)
            min_sp = D.SPREAD_1M_MIN_CE if ot=="CE" else D.SPREAD_1M_MIN_PE
            missing = []
            if conds < 3:    missing.append("3m(" + str(conds) + "/4)")
            if sp1m < min_sp: missing.append("Spread(+" + str(min_sp) + ")")
            if body < body_min: missing.append("Body")
            if not (rsi_lo <= rsi <= rsi_hi and rising): missing.append("RSI")
            if vol < vol_min: missing.append("Vol")
            if not missing:
                return "🎯 READY"
            return "Need: " + "  ".join(missing)

        def gate_bar(n):
            return str(n)+"/4 "+("✅" if n>=3 else "⚠️" if n==2 else "❌")

        def rsi_bar(v, rising=None, use_1m=True):
            """v12.12: 1-min uses 45-65, 3-min uses 42-72"""
            if not v: return "—"
            lo = rsi_1m_lo if use_1m else rsi_lo
            hi = rsi_1m_hi if use_1m else rsi_hi
            ok  = lo<=v<=hi
            arr = (" ↑" if rising else " ↓") if rising is not None else ""
            return str(v)+arr+(" ✅" if ok else " ❌")

        def body_bar(v):
            return str(v)+"% "+("✅" if v>=body_min else "❌")

        def vol_bar(v):
            return str(v)+"x "+("✅" if v>=vol_min else "❌")

        def verdict(d, ot):
            conds = d.get("conditions_3m",0)
            sp1m  = d.get("spread_1m",0)
            if ot=="CE" and sp1m < D.SPREAD_1M_MIN_CE:
                return "❌ 1m spread " + str(round(sp1m,1)) + " need +"+str(D.SPREAD_1M_MIN_CE)+"pts"
            if ot=="PE" and sp1m < D.SPREAD_1M_MIN_PE:
                return "❌ 1m spread " + str(round(sp1m,1)) + " need +"+str(D.SPREAD_1M_MIN_PE)+"pts"
            if conds < 3:
                return "❌ 3m gate "+str(conds)+"/4 — need 3"
            if d.get("body",0) < body_min:
                return "⏳ Body weak ("+str(d.get("body",0))+"%) — wait"
            rsi = d.get("rsi",0); rising = d.get("rsi_rising",False)
            if not (rsi_1m_lo<=rsi<=rsi_1m_hi and rising):
                if rsi > rsi_1m_hi:
                    return "⏳ RSI "+str(rsi)+" ↑ — move done, wait pullback"
                return "⏳ RSI "+str(rsi)+(" ↑" if rising else " ↓")+" — wait"
            if d.get("vol",0) < vol_min:
                return "⏳ Volume "+str(d.get("vol",0))+"x — wait"
            return "🎯 READY — all aligned"

        secs_left = 60 - now.second
        countdown = str(secs_left) + "s to scan"
        sess_min  = D.SESSION_SCORE_MIN.get(session, 999)
        session_ok= sess_min < 999
        streak    = st.get("consecutive_losses",0)
        dpnl      = st.get("daily_pnl",0)
        dpnl_sign = "+" if dpnl>=0 else ""

        # ── IN TRADE ──────────────────────────────────────────
        if st.get("in_trade"):
            entry     = st.get("entry_price",0)
            direction = st.get("direction","CE")
            ltp_t     = D.get_ltp(st.get("token")) if st.get("token") else 0
            if ltp_t<=0 and _kite is not None:
                try:
                    sym = st.get("symbol","")
                    if sym:
                        q     = _kite.ltp(["NFO:"+sym])
                        ltp_t = float(q["NFO:"+sym]["last_price"])
                except Exception: pass

            pnl      = round(ltp_t-entry,1) if ltp_t>0 else 0
            rs_str   = ("+₹" if pnl>=0 else "₹")+str(round(pnl*D.LOT_SIZE))
            peak     = st.get("peak_pnl",0)
            phase    = st.get("exit_phase",1)
            sl_val   = st.get("phase1_sl",0) if phase==1 else st.get("phase2_sl",0)
            sl_dist  = round(ltp_t-sl_val,1) if ltp_t>0 and sl_val>0 else 0
            td       = ce if direction=="CE" else pe

            spread_3m   = td.get("spread_3m",0)
            ema9_3m     = td.get("ema9_3m",0)
            ema21_3m    = td.get("ema21_3m",0)
            spread_1m   = td.get("spread_1m",0)
            ema9_1m     = td.get("ema9_1m",0)
            ema21_1m    = td.get("ema21_1m",0)
            conds_3m    = td.get("conditions_3m",0)
            rsi_1m      = td.get("rsi",0)
            body_1m     = td.get("body",0)
            vol_1m      = td.get("vol",0)
            rsi_rising  = td.get("rsi_rising",False)

            # Spread narrowing warning
            spread_warn = ""
            try:
                df3w = D.get_historical_data(st.get("token"), "3minute", D.LOOKBACK_3M)
                df3w = D.add_indicators(df3w)
                if len(df3w)>=4:
                    s1 = df3w.iloc[-2].get("EMA_9",0)-df3w.iloc[-2].get("EMA_21",0)
                    s2 = df3w.iloc[-3].get("EMA_9",0)-df3w.iloc[-3].get("EMA_21",0)
                    if abs(s1)<abs(s2):
                        spread_warn = " ⚠️Narrowing"
            except Exception: pass

            trail_mode = "3-min EMA9 ⚡" if st.get("trail_tightened") else "5-min EMA9"
            rsi_ob_str = "YES 🔥 top soon" if st.get("_rsi_was_overbought") else "No (76 triggers)"

            _tg_send(
                "⚡ <b>WAR ROOM — "+direction+" Ph"+str(phase)+"</b>  "+_now_str()+"\n"
                +st.get("symbol","")+"  Score:"+str(st.get("score_at_entry",0))+"\n"
                "Entry "+str(round(entry,1))+" → LTP "+str(round(ltp_t,1))
                +"  PNL "+("+" if pnl>=0 else "")+str(pnl)+"pts "+rs_str+"\n"
                "Peak +"+str(round(peak,1))+"pts"
                +"  SL "+str(round(sl_val,1))+" ("+str(abs(sl_dist))+"pts)\n"
                "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                "3-MIN STRUCTURE\n"
                "EMA9   : "+str(ema9_3m)+"   EMA21: "+str(ema21_3m)+"\n"
                "RSI    : "+str(td.get("rsi_3m",0))+"   Gap: "+str(round(abs(spread_3m),1))+"pts\n"
                "Trend  : "+trend_lbl(spread_3m,direction)+spread_warn+"\n"
                "Gate   : "+gate_bar(conds_3m)+"\n"
                "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                "1-MIN STRUCTURE\n"
                "EMA9   : "+str(ema9_1m)+"   EMA21: "+str(ema21_1m)+"\n"
                "Spread : "+spread1m_lbl(td,direction)+"\n"
                "RSI    : "+rsi_bar(rsi_1m,rsi_rising)+"\n"
                "Body   : "+body_bar(body_1m)+"   Vol: "+vol_bar(vol_1m)+"\n"
                "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                "EXIT WATCH\n"
                "Trail  : "+trail_mode+"\n"
                "RSI OB : "+rsi_ob_str+"\n"
                "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                "Today "+dpnl_sign+str(round(dpnl,1))+"pts  "
                +str(st.get("daily_trades",0))+"T "
                +"W"+str(st.get("daily_trades",0)-st.get("daily_losses",0))
                +" L"+str(st.get("daily_losses",0))
                +"  "+countdown
            )
            return

        # ── NO TRADE ──────────────────────────────────────────
        streak_str = (" ⚠️ need score≥"+str(D.EXCELLENCE_BYPASS_SCORE) if streak>=2 else " ✅")

        # v12.11: Fetch spot data for display
        spot_3m = D.get_spot_indicators("3minute")
        spot_gap = D.get_spot_gap()
        gap_str = ""
        if abs(spot_gap) >= 10:
            gap_str = "  Gap:" + ("+" if spot_gap>=0 else "") + str(round(spot_gap)) + "pts"

        _tg_send(
            "⚡ <b>WAR ROOM — "+now.strftime("%H:%M")+" "+session+"</b>\n"
            "Spot "+str(round(spot,1))
            +"  ATM "+str(strike)
            +"  DTE "+str(dte)
            +"  VIX "+vix_label(vix)+"\n"
            "Today "+dpnl_sign+str(round(dpnl,1))+"pts"
            +"  "+str(st.get("daily_trades",0))+"T"
            +"  W"+str(st.get("daily_trades",0)-st.get("daily_losses",0))
            +"  L"+str(st.get("daily_losses",0))
            +"  Streak "+str(streak)+streak_str+"\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "SPOT  "+spot_3m.get("regime","—")+gap_str+"\n"
            "EMA9  "+str(spot_3m["ema9"])+"  EMA21 "+str(spot_3m["ema21"])
            +"  RSI "+str(spot_3m["rsi"])+"\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "3-MIN    CE           PE\n"
            "EMA9   : "+str(ce.get("ema9_3m","—")).ljust(13)+str(pe.get("ema9_3m","—"))+"\n"
            "EMA21  : "+str(ce.get("ema21_3m","—")).ljust(13)+str(pe.get("ema21_3m","—"))+"\n"
            "RSI    : "+str(ce.get("rsi_3m",0)).ljust(13)+str(pe.get("rsi_3m",0))+"\n"
            "Trend  : "+trend_lbl(ce.get("spread_3m",0),"CE").ljust(13)+trend_lbl(pe.get("spread_3m",0),"PE")+"\n"
            "Gate CE: "+gate_meter(ce,"CE")+"\n"
            "Gate PE: "+gate_meter(pe,"PE")+"\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "1-MIN    CE           PE\n"
            "EMA9   : "+str(ce.get("ema9_1m","—")).ljust(13)+str(pe.get("ema9_1m","—"))+"\n"
            "EMA21  : "+str(ce.get("ema21_1m","—")).ljust(13)+str(pe.get("ema21_1m","—"))+"\n"
            "Spread : "+spread1m_lbl(ce,"CE").ljust(13)+spread1m_lbl(pe,"PE")+"\n"
            "Body   : "+body_bar(ce.get("body",0)).ljust(13)+body_bar(pe.get("body",0))+"\n"
            "RSI    : "+rsi_bar(ce.get("rsi",0),ce.get("rsi_rising")).ljust(13)+rsi_bar(pe.get("rsi",0),pe.get("rsi_rising"))+"\n"
            "Vol    : "+vol_bar(ce.get("vol",0)).ljust(13)+vol_bar(pe.get("vol",0))+"\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "VERDICT\n"
            "CE  "+verdict(ce,"CE")+"\n"
            "     "+score_line(ce,"CE")+"\n"
            "PE  "+verdict(pe,"PE")+"\n"
            "     "+score_line(pe,"PE")+"\n"
            +(("✅ Scanning  "+countdown) if (session_ok and spot>0) else ("⏸ Market closed" if not D.is_market_open() else "⏸ Outside trading window"))
        )
    except Exception as e:
        _tg_send("Edge error: " + str(e))


def _cmd_pnl(args):
    with _state_lock:
        st = dict(state)
    pnl    = st.get("daily_pnl", 0)
    sign   = "+" if pnl >= 0 else ""
    streak = st.get("consecutive_losses", 0)

    # Read today's trades for charges breakdown
    _today_trades = []
    try:
        import csv as _csv_pnl
        today_str = D.date.today().isoformat() if hasattr(D, 'date') else __import__('datetime').date.today().isoformat()
        if os.path.isfile(D.TRADE_LOG_PATH):
            with open(D.TRADE_LOG_PATH) as _f:
                for _r in _csv_pnl.DictReader(_f):
                    if _r.get("date") == today_str:
                        _today_trades.append(_r)
    except Exception:
        pass

    _total_gross = 0.0
    _total_charges = 0.0
    for _t in _today_trades:
        _g = float(_t.get("gross_pnl_rs", 0))
        if _g == 0:
            _g = float(_t.get("pnl_pts", 0)) * float(_t.get("qty_exited", D.LOT_SIZE * 2))
        _total_gross += _g
        _total_charges += float(_t.get("total_charges", 0))
    _total_net = round(_total_gross - _total_charges, 2)

    # Trade-by-trade lines
    _trd_lines = ""
    for i, t in enumerate(_today_trades, 1):
        _pts = float(t.get("pnl_pts", 0))
        _ch = float(t.get("total_charges", 0))
        _net = float(t.get("net_pnl_rs", t.get("pnl_rs", 0)))
        _trd_lines += (str(i) + ". " + t.get("direction", "") + " "
                       + ("+" if _pts >= 0 else "") + str(round(_pts, 1)) + "pts"
                       + " ch₹" + str(int(_ch))
                       + " net₹" + str(int(_net)) + "\n")

    _tg_send(
        "💰 <b>TODAY P&amp;L</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "PNL    : " + sign + str(round(pnl, 1)) + "pts  " + _rs(pnl) + "\n"
        "Trades : " + str(st.get("daily_trades", 0)) + "\n"
        "W/L    : " + str(st.get("daily_trades", 0) - st.get("daily_losses", 0))
        + "W " + str(st.get("daily_losses", 0)) + "L\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        + (_trd_lines if _trd_lines else "No trades\n")
        + "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "Gross    : " + ("+" if _total_gross >= 0 else "") + "₹" + "{:,}".format(int(_total_gross)) + "\n"
        "Charges  : -₹" + "{:,}".format(int(_total_charges)) + "\n"
        "Net      : " + ("+" if _total_net >= 0 else "") + "₹" + "{:,}".format(int(_total_net))
    )


def _cmd_account(args):
    try:
        _acct = D.get_account_info()
        # Try to refresh margins
        if _kite:
            D.refresh_margin(_kite)
            _acct = D.get_account_info()
    except Exception:
        _acct = D.get_account_info()

    if not _acct.get("name"):
        _tg_send("Account info not available. Bot may not have fetched it yet.")
        return

    _tg_send(
        "👤 <b>ACCOUNT</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "Name     : " + _acct.get("name", "") + "\n"
        "User ID  : " + _acct.get("user_id", "") + "\n"
        "Broker   : " + _acct.get("broker", "Zerodha") + "\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "Balance  : ₹" + "{:,}".format(int(_acct.get("total_balance", 0))) + "\n"
        "Available: ₹" + "{:,}".format(int(_acct.get("available_margin", 0))) + "\n"
        "Used     : ₹" + "{:,}".format(int(_acct.get("used_margin", 0)))
    )

def _cmd_score(args):
    with _state_lock:
        st = dict(state)

    last_scan = st.get("_last_scan", {})
    if not last_scan:
        _tg_send("No scan data yet. Scans run every 1-min candle during market hours.")
        return

    def _tick(ok): return "✅" if ok else "❌"

    def _rsi_rising_label(d1):
        if not d1: return "—"
        return ("↑✅" if d1.get("rsi_rising") else "↓❌")

    def _score_label(score, fired):
        if score >= 7 and fired: return str(score) + "/7 ⚡"
        if score >= 6 and fired: return str(score) + "/7 🎯"
        if score >= 7:           return str(score) + "/7 ⚡ (blocked)"
        if score >= 6:           return str(score) + "/7 (blocked)"
        return str(score) + "/7 ❌"

    ce  = last_scan.get("ce", {})
    pe  = last_scan.get("pe", {})
    cd1 = ce.get("d1", {})
    pd1 = pe.get("d1", {})

    vix     = last_scan.get("vix", 0)
    dte     = last_scan.get("dte", "—")
    atm     = last_scan.get("atm", "—")
    session = last_scan.get("session", "—")
    fired   = last_scan.get("fired", "No")
    f_type  = last_scan.get("fired_type", "—")

    vix_str = str(vix)
    if vix >= 20:   vix_str += " 💥 HIGH"
    elif vix >= 15: vix_str += " ⚡ ELEVATED"
    else:           vix_str += " 😌 NORMAL"

    dte_str = str(dte)
    if isinstance(dte, int):
        if dte <= 1:   dte_str += " 🔥 EXPIRY"
        elif dte <= 2: dte_str += " ⚠️ NEAR"

    streak = st.get("consecutive_losses", 0)
    gate_str = ("⚠️ Streak=" + str(streak) + " — score≥" + str(D.EXCELLENCE_BYPASS_SCORE) + " needed"
                if streak >= 2 else "✅ Clear")

    result_str = ("→ " + f_type + " " + fired + " ⚡ ENTERING"
                  if fired != "No" else "→ No entry this scan")

    msg = (
        "🔍 <b>SCAN — " + str(last_scan.get("time","—")) + "  " + session + "</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "         <b>CE</b>          <b>PE</b>\n"
        "Score  " + str(_score_label(ce.get("score",0), ce.get("fired",False))).ljust(14)
               + str(_score_label(pe.get("score",0), pe.get("fired",False))) + "\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "<b>1-MIN</b>\n"
        "Body   " + (str(cd1.get("body_pct","—"))+"% "+_tick(cd1.get("body_ok",False))).ljust(14)
               + str(pd1.get("body_pct","—"))+"% "+_tick(pd1.get("body_ok",False)) + "\n"
        "RSI    " + (str(cd1.get("rsi_val","—"))+" 🎯").ljust(14)
               + str(pd1.get("rsi_val","—"))+" 🎯" + "\n"
        "Vol    " + (str(cd1.get("vol_ratio","—"))+"x "+_tick(cd1.get("vol_ok",False))).ljust(14)
               + str(pd1.get("vol_ratio","—"))+"x "+_tick(pd1.get("vol_ok",False)) + "\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "RSI↑   " + _rsi_rising_label(cd1).ljust(14) + _rsi_rising_label(pd1) + "\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "<b>MARKET</b>\n"
        "VIX    : " + vix_str + "\n"
        "DTE    : " + dte_str + "\n"
        "ATM    : " + str(atm) + "\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "Gate   : " + gate_str + "\n"
        + result_str
    )
    _tg_send(msg)

def _cmd_files(args):  _send_file_browser()
def _cmd_download(args):
    """Smart download — 4 key files only."""
    import zipfile as _zf
    from datetime import date as _d
    _today = _d.today().strftime("%Y-%m-%d")
    _zip_name = "vrl_strategy_" + _today + ".zip"
    _zip_path = os.path.join(os.path.expanduser("~"), _zip_name)
    _files = [
        (os.path.expanduser("~/lab_data/vrl_trade_log.csv"), "vrl_trade_log.csv"),
        (os.path.expanduser("~/lab_data/vrl_data.db"), "vrl_data.db"),
        (os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.yaml"), "config.yaml"),
        (os.path.expanduser("~/state/vrl_live_state.json"), "vrl_live_state.json"),
    ]
    try:
        with _zf.ZipFile(_zip_path, "w", _zf.ZIP_DEFLATED) as zf:
            for fpath, fname in _files:
                if os.path.isfile(fpath):
                    zf.write(fpath, fname)
        _tg_send_file(_zip_path, "📦 Strategy data — " + _today)
        os.remove(_zip_path)
    except Exception as e:
        _tg_send("Download error: " + str(e))


def _cmd_download_all(args):
    """Full download — all logs for a date."""
    target = None
    if isinstance(args, list):
        args = " ".join(args)
    if args and args.strip():
        arg = args.strip()
        if len(arg) == 8 and arg.isdigit():
            target = arg[:4] + "-" + arg[4:6] + "-" + arg[6:8]
        elif len(arg) == 10 and arg[4] == "-" and arg[7] == "-":
            target = arg
        else:
            _tg_send("Usage: /download_all or /download_all 2026-04-01")
            return
    _send_today_download(target)

def _cmd_health(args):
    import os as _os
    now      = datetime.now()
    spot_ltp = D.get_ltp(D.NIFTY_SPOT_TOKEN)
    vix_ltp  = D.get_vix()
    ws_ok    = D.is_tick_live(D.NIFTY_SPOT_TOKEN)
    market   = D.is_market_open()
    circuit  = state.get("_circuit_breaker", False)
    errors   = state.get("_error_count", 0)

    disk_free_mb = 0
    try:
        st_disk = _os.statvfs(_os.path.expanduser("~"))
        disk_free_mb = round(st_disk.f_bavail * st_disk.f_frsize / (1024*1024), 0)
    except Exception:
        pass

    _tg_send(
        "🏥 <b>SYSTEM HEALTH</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "Time       : " + now.strftime("%H:%M:%S") + "\n"
        "Market     : " + ("🟢 OPEN" if market else "🔴 CLOSED") + "\n"
        "WebSocket  : " + ("✅ Live" if ws_ok else ("⏸ N/A (market closed)" if not market else "❌ Stale")) + "\n"
        "Spot LTP   : " + (str(round(spot_ltp, 1)) if spot_ltp > 0 else ("⏸ N/A" if not market else "❌ Missing")) + "\n"
        "VIX        : " + (str(round(vix_ltp, 1)) if vix_ltp > 0 else ("⏸ N/A" if not market else "❌ Missing")) + "\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "Circuit    : " + ("🚨 TRIGGERED — use /resume" if circuit else "✅ Clear") + "\n"
        "Errors     : " + str(errors) + " consecutive\n"
        "In trade   : " + str(state.get("in_trade", False)) + "\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "Disk free  : " + str(disk_free_mb) + " MB\n"
        "Lot size   : " + str(D.LOT_SIZE) + " (from broker)\n"
        "Mode       : " + ("📄 PAPER" if D.PAPER_MODE else "💰 LIVE") + "\n"
        "Version    : " + D.VERSION
    )

def _cmd_researchdata(args):
    _ENHANCED_DIR = os.path.join(_RESEARCH_DIR, "enhanced")
    all_files = []
    for base_dir, prefix in [(_RESEARCH_DIR, ""), (_ENHANCED_DIR, "enhanced/")]:
        if not os.path.isdir(base_dir):
            continue
        for f in os.listdir(base_dir):
            fpath = os.path.join(base_dir, f)
            if os.path.isfile(fpath):
                all_files.append((fpath, prefix + f))

    if not all_files:
        _tg_send("📂 No research data yet.\nRun: ~/kite_env/bin/python3 research_strikes.py\nor: ~/kite_env/bin/python3 research_enhanced.py")
        return

    total_size = sum(os.path.getsize(f[0]) for f in all_files)
    if total_size > 45 * 1024 * 1024:
        _tg_send("⚠️ Research data too large (" + str(round(total_size/(1024*1024),1))
                 + " MB). Use /files → 🔭 Research Data to download individual files.")
        return

    zip_path = os.path.join(D.STATE_DIR, "research_export.zip")
    try:
        import zipfile as _zf
        with _zf.ZipFile(zip_path, "w", _zf.ZIP_DEFLATED) as zf:
            for fpath, arcname in all_files:
                zf.write(fpath, arcname)
        size_mb = round(os.path.getsize(zip_path) / (1024 * 1024), 2)
        _tg_send_file(zip_path,
                      caption="🔭 Research data — " + str(len(all_files)) + " files ("
                              + str(size_mb) + " MB)")
    except Exception as e:
        _tg_send("Research zip error: " + str(e))

def _cmd_pause(args):
    with _state_lock:
        state["paused"] = True
    _tg_send("⏸ Paused. No new entries.")
    logger.info("[CTRL] Paused")

def _cmd_resume(args):
    with _state_lock:
        state["paused"]           = False
        state["_circuit_breaker"] = False
        state["_error_count"]     = 0
    _tg_send("▶️ Resumed. Circuit breaker cleared.")
    logger.info("[CTRL] Resumed + circuit breaker reset")

def _cmd_forceexit(args):
    with _state_lock:
        if not state.get("in_trade"):
            _tg_send("No open trade.")
            return
        state["force_exit"] = True
    _tg_send("🚨 Force exit triggered.")
    logger.warning("[CTRL] Force exit")

def _cmd_restart(args):
    _tg_send("🔄 Restarting...")
    logger.info("[CTRL] Restart requested")
    _remove_pid()
    time.sleep(2)
    os.execv(sys.executable, [sys.executable] + sys.argv)

def _cmd_livecheck(args):
    try:
        with open(D.LIVE_LOG_FILE, "r") as f:
            lines = f.readlines()
        last_50 = "".join(lines[-50:])
        if len(last_50) > 4000:
            last_50 = last_50[-4000:]
        import re as _re
        last_50 = _re.sub(r'(api_key|access_token|token|secret|password)\s*[=:]\s*\S+',
                          r'\1=***', last_50, flags=_re.IGNORECASE)
        _tg_send("<pre>" + last_50 + "</pre>")
    except Exception as e:
        _tg_send("Log error: " + str(e))

def _cmd_reset_exit(args):
    with _state_lock:
        if state.get("in_trade"):
            # Force‑clear trade state – user must confirm position is closed manually
            state["in_trade"] = False
            state["symbol"] = ""
            state["token"] = None
            state["direction"] = ""
            state["entry_price"] = 0.0
            state["entry_time"] = ""
            state["exit_phase"] = 1
            state["phase1_sl"] = 0.0
            state["phase2_sl"] = 0.0
            state["qty"] = D.LOT_SIZE
            state["trail_tightened"] = False
            state["peak_pnl"] = 0.0
            state["mode"] = ""
            state["iv_at_entry"] = 0.0
            state["score_at_entry"] = 0
            state["regime_at_entry"] = ""
            state["candles_held"] = 0
            state["_rsi_was_overbought"] = False
            state["_last_trail_candle"] = ""
            state["force_exit"] = False
            state["_exit_failed"] = False
            _tg_send("⚠️ Trade state cleared – verify position in broker manually.")
        else:
            state["_exit_failed"] = False
        state["_exit_failed_since"] = None
    _save_state()
    _tg_send("✅ Exit failure flag cleared.")

# ═══════════════════════════════════════════════════════════════
#  NEW TELEGRAM COMMAND: /source — download all source code
# ═══════════════════════════════════════════════════════════════

def _cmd_source(args):
    """Zip all .py files in the home directory and send via Telegram."""
    home = os.path.expanduser("~")
    py_files = []
    for f in os.listdir(home):
        if f.endswith(".py") and os.path.isfile(os.path.join(home, f)):
            py_files.append(f)
    if not py_files:
        _tg_send("No .py files found in home directory.")
        return

    zip_path = os.path.join(D.STATE_DIR, "vrl_source.zip")
    try:
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for fname in py_files:
                fpath = os.path.join(home, fname)
                zf.write(fpath, fname)
        size_kb = round(os.path.getsize(zip_path) / 1024, 1)
        _tg_send_file(zip_path, caption=f"📦 Source code ({len(py_files)} files, {size_kb} KB)")
    except Exception as e:
        _tg_send(f"Error creating source zip: {e}")

# ═══════════════════════════════════════════════════════════════
#  NEW v12.11 COMMANDS
# ═══════════════════════════════════════════════════════════════

def _cmd_spot(args):
    """Spot trend + gap + regime — always reliable from candle 1."""
    try:
        spot_3m = D.get_spot_indicators("3minute")
        spot_1m = D.get_spot_indicators("minute")
        gap     = D.get_spot_gap()
        vix     = D.get_vix()
        spot_ltp= D.get_ltp(D.NIFTY_SPOT_TOKEN)

        gap_str = ""
        if abs(gap) > 0:
            direction = "UP" if gap > 0 else "DOWN"
            gap_str = (
                "Gap    : " + ("+" if gap >= 0 else "") + str(round(gap, 1)) + "pts " + direction + "\n"
            )

        _tg_send(
            "📈 <b>SPOT INTELLIGENCE</b>  " + _now_str() + "\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "Spot   : " + str(round(spot_ltp, 1)) + "  VIX: " + str(round(vix, 1)) + "\n"
            + gap_str
            + "Regime : " + spot_3m.get("regime", "—") + " (" + str(spot_3m.get("candles", 0)) + " candles)\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "3-MIN\n"
            "EMA9   : " + str(spot_3m["ema9"]) + "\n"
            "EMA21  : " + str(spot_3m["ema21"]) + "\n"
            "Spread : " + ("+" if spot_3m["spread"] >= 0 else "") + str(spot_3m["spread"]) + "pts\n"
            "RSI    : " + str(spot_3m["rsi"]) + "\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "1-MIN\n"
            "EMA9   : " + str(spot_1m["ema9"]) + "\n"
            "EMA21  : " + str(spot_1m["ema21"]) + "\n"
            "Spread : " + ("+" if spot_1m["spread"] >= 0 else "") + str(spot_1m["spread"]) + "pts\n"
            "RSI    : " + str(spot_1m["rsi"])
        )
    except Exception as e:
        _tg_send("Spot error: " + str(e))


def _cmd_regime(args):
    """Current regime + detection mode."""
    try:
        spot_3m = D.get_spot_indicators("3minute")
        gap     = D.get_spot_gap()
        now     = datetime.now()
        expiry  = D.get_nearest_expiry()
        dte     = D.calculate_dte(expiry) if expiry else 0
        session = D.get_session_block(now.hour, now.minute)

        with _state_lock:
            last_scan = dict(state.get("_last_scan", {}))

        opt_regime = last_scan.get("regime", "—")
        mode = "MOMENTUM" if (dte <= 1 and now.hour < 11) else "EMA"

        _tg_send(
            "🎯 <b>REGIME</b>  " + _now_str() + "\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "Spot regime  : " + spot_3m.get("regime", "—") + "\n"
            "Option regime: " + opt_regime + "\n"
            "Detection    : " + mode + "\n"
            "DTE          : " + str(dte) + "\n"
            "Session      : " + session + "\n"
            "Gap          : " + ("+" if gap >= 0 else "") + str(round(gap, 1)) + "pts\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            + ("💡 Spot is backup — option data thin (DTE≤1)" if dte <= 1 else "📊 Normal mode — option EMA has full history")
        )
    except Exception as e:
        _tg_send("Regime error: " + str(e))


def _cmd_align(args):
    """Alignment check — compare bot indicators vs independent fetch."""
    try:
        spot_ltp = D.get_ltp(D.NIFTY_SPOT_TOKEN)
        expiry   = D.get_nearest_expiry()
        dte      = D.calculate_dte(expiry) if expiry else 0
        step     = D.get_active_strike_step(dte)
        strike   = D.resolve_atm_strike(spot_ltp, step) if spot_ltp > 0 else 0
        tokens   = D.get_option_tokens(None, strike, expiry) if strike else {}

        lines = []
        for ot in ("CE", "PE"):
            info = tokens.get(ot)
            if not info:
                continue
            # Independent 3-min fetch
            df3 = D.get_historical_data(info["token"], "3minute", D.LOOKBACK_3M)
            df3 = D.add_indicators(df3)
            if df3.empty or len(df3) < 3:
                lines.append(ot + ": insufficient data")
                continue
            last = df3.iloc[-2]
            rsi  = round(float(last.get("RSI", 0)), 1)
            e9   = round(float(last.get("EMA_9", 0)), 2)
            e21  = round(float(last.get("EMA_21", 0)), 2)
            spread = round(e9 - e21, 2)
            n_candles = len(df3)

            # If momentum mode, show momentum too
            mom = ""
            if dte <= 1 and n_candles < 25:
                lb = min(5, n_candles - 2)
                ref = float(df3.iloc[-2 - lb]["close"])
                m = round(float(last["close"]) - ref, 2)
                mom = "\n  Momentum: " + ("+" if m >= 0 else "") + str(m) + "pts (ref " + str(round(ref, 1)) + ")"

            lines.append(
                ot + " (" + str(n_candles) + " candles"
                + (" MOMENTUM" if dte <= 1 and n_candles < 25 else " EMA") + ")\n"
                "  RSI    : " + str(rsi) + "\n"
                "  EMA9   : " + str(e9) + "\n"
                "  EMA21  : " + str(e21) + "\n"
                "  Spread : " + ("+" if spread >= 0 else "") + str(spread) + "pts"
                + mom
            )

        # Spot alignment
        spot_3m = D.get_spot_indicators("3minute")
        lines.append(
            "SPOT (always reliable)\n"
            "  RSI    : " + str(spot_3m["rsi"]) + "\n"
            "  EMA9   : " + str(spot_3m["ema9"]) + "\n"
            "  EMA21  : " + str(spot_3m["ema21"]) + "\n"
            "  Spread : " + ("+" if spot_3m["spread"] >= 0 else "") + str(spot_3m["spread"]) + "pts\n"
            "  Regime : " + spot_3m["regime"]
        )

        _tg_send(
            "🔍 <b>ALIGNMENT CHECK</b>  " + _now_str() + "\n"
            "ATM " + str(strike) + "  DTE " + str(dte) + "\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            + "\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n".join(lines)
        )
    except Exception as e:
        _tg_send("Align error: " + str(e))


def _cmd_trades(args):
    """Today's trade list with details."""
    trades = _read_today_trades()
    if not trades:
        _tg_send("📒 No trades today.")
        return
    lines = ""
    total = 0.0
    for i, t in enumerate(trades, 1):
        pts = float(t.get("pnl_pts", 0))
        total += pts
        sign = "+" if pts >= 0 else ""
        icon = "✅" if pts >= 0 else "❌"
        peak = float(t.get("peak_pnl", 0))
        captured = round(pts / peak * 100) if peak > 0 else 0
        lines += (
            icon + " <b>Trade " + str(i) + "</b>  " + t.get("direction", "") + "\n"
            "  " + t.get("entry_time", "") + " → " + t.get("exit_time", "") + "\n"
            "  Entry: ₹" + str(t.get("entry_price", "")) + " → Exit: ₹" + str(t.get("exit_price", "")) + "\n"
            "  PNL: " + sign + str(round(pts, 1)) + "pts  " + _rs(pts) + "\n"
            "  Peak: +" + str(round(peak, 1)) + "pts  Captured: " + str(captured) + "%\n"
            "  Reason: " + t.get("exit_reason", "") + "\n"
        )
    sign = "+" if total >= 0 else ""
    _tg_send(
        "📒 <b>TODAY'S TRADES</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        + lines
        + "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "Net: " + sign + str(round(total, 1)) + "pts  " + _rs(total)
    )

# ═══════════════════════════════════════════════════════════════
#  v12.15: PIVOT COMMAND
# ═══════════════════════════════════════════════════════════════

def _cmd_validate(args):
    """Manual system validation — runs 10 ad-hoc health checks."""
    try:
        from VRL_VALIDATE import manual_validate
        with _state_lock:
            st = dict(state)
        result = manual_validate(st)
        lines = ""
        for name, ok, detail in result["checks"]:
            icon = "✅" if ok else "❌"
            lines += icon + " " + name + ": " + str(detail) + "\n"
        passed = result["passed"]
        total  = result["total"]
        summary_icon = "✅" if passed == total else ("⚠️" if passed >= total - 2 else "❌")
        _tg_send(
            "🔍 <b>SYSTEM VALIDATION</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            + lines
            + "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            + summary_icon + " " + str(passed) + "/" + str(total) + " checks passed"
        )
    except Exception as e:
        _tg_send("Validate error: " + str(e))


def _cmd_pivot(args):
    """Show fib pivot levels + nearest level to current spot."""
    try:
        pivots = D.get_fib_pivots()
        if not pivots:
            _tg_send("No pivot data. Run /restart to recalculate.")
            return
        spot = D.get_ltp(D.NIFTY_SPOT_TOKEN)
        nearest = D.get_nearest_fib_level(spot)
        consol = D.detect_spot_consolidation()

        _tg_send(
            "📐 <b>FIB PIVOTS</b>  " + _now_str() + "\n"
            "Prev: " + pivots.get("prev_date","") + " H=" + str(pivots.get("prev_high",0))
            + " L=" + str(pivots.get("prev_low",0)) + " C=" + str(pivots.get("prev_close",0)) + "\n"
            "Range: " + str(pivots.get("range",0)) + "pts\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "R3  : " + str(pivots.get("R3",0)) + "\n"
            "R2  : " + str(pivots.get("R2",0)) + "\n"
            "R1  : " + str(pivots.get("R1",0)) + "\n"
            "<b>P   : " + str(pivots.get("pivot",0)) + "</b>\n"
            "S1  : " + str(pivots.get("S1",0)) + "\n"
            "S2  : " + str(pivots.get("S2",0)) + "\n"
            "S3  : " + str(pivots.get("S3",0)) + "\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "Spot : " + str(round(spot,1)) + "\n"
            "Near : " + nearest.get("level","—") + " (" + str(nearest.get("price",0))
            + ")  " + ("+" if nearest.get("distance",0)>=0 else "") + str(nearest.get("distance",0)) + "pts\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "Consolidation: " + ("YES (" + str(consol["range"]) + "pts range)" if consol["consolidating"] else "No")
        )
    except Exception as e:
        _tg_send("Pivot error: " + str(e))


# ═══════════════════════════════════════════════════════════════
#  TELEGRAM LISTENER
# ═══════════════════════════════════════════════════════════════

_tg_offset         = 0
_tg_running        = False
_tg_last_update_id = -1

def _cmd_token(args):
    """Manage subscriber access tokens."""
    if isinstance(args, list):
        parts = args
    else:
        parts = args.strip().split() if args else []
    if not parts:
        _tg_send("Usage:\n/token create [name] [days]\n/token list\n/token revoke [name]\n/token extend [name] [days]")
        return

    action = parts[0].lower()
    try:
        import VRL_DB as _DB
    except Exception as e:
        _tg_send("DB error: " + str(e))
        return

    if action == "create":
        if len(parts) < 3:
            _tg_send("Usage: /token create [name] [days]")
            return
        name = parts[1]
        days = int(parts[2])
        token = _DB.create_token(name, days)
        from datetime import datetime, timedelta
        exp = (datetime.now() + timedelta(days=days)).strftime("%Y-%m-%d")
        # Get server IP
        ip = "34.14.175.26"
        try:
            import subprocess as _sp2
            ip = _sp2.check_output(["curl", "-s", "ifconfig.me"], timeout=5).decode().strip()
        except Exception:
            pass
        _tg_send(
            "🔑 <b>Access token created</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "Name    : " + name + "\n"
            "Expires : " + exp + " (" + str(days) + " days)\n"
            "Link    : http://" + ip + ":8080/s/" + token + "\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "Send this link to the subscriber."
        )

    elif action == "list":
        tokens = _DB.list_tokens()
        if not tokens:
            _tg_send("No tokens created yet.")
            return
        from datetime import datetime
        active = [t for t in tokens if t.get("active")]
        expired_cnt = len(tokens) - len(active)
        lines = ""
        for i, t in enumerate(active, 1):
            last = t.get("last_used", "")
            ago = ""
            if last:
                try:
                    diff = (datetime.now() - datetime.fromisoformat(last)).total_seconds()
                    if diff < 3600:
                        ago = str(int(diff / 60)) + "min ago"
                    elif diff < 86400:
                        ago = str(int(diff / 3600)) + "h ago"
                    else:
                        ago = str(int(diff / 86400)) + "d ago"
                except Exception:
                    ago = last[:10]
            exp_date = t.get("expires_at", "")[:10]
            ips = [x for x in (t.get("access_ips", "") or "").split(",") if x.strip()]
            ip_warn = " ⚠️" + str(len(ips)) + "IPs" if len(ips) >= 4 else ""
            lines += (str(i) + ". " + t["name"] + " — exp " + exp_date
                      + " — used " + str(t.get("access_count", 0)) + "x"
                      + (" — last: " + ago if ago else "")
                      + ip_warn + "\n")
        _tg_send(
            "📋 <b>Access Tokens</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            + lines
            + "Active: " + str(len(active)) + " | Expired/Revoked: " + str(expired_cnt)
        )

    elif action == "revoke":
        if len(parts) < 2:
            _tg_send("Usage: /token revoke [name]")
            return
        name = parts[1]
        ok = _DB.revoke_token(name)
        _tg_send(("❌ Token revoked for " + name) if ok else ("No active token found for " + name))

    elif action == "extend":
        if len(parts) < 3:
            _tg_send("Usage: /token extend [name] [days]")
            return
        name = parts[1]
        days = int(parts[2])
        ok = _DB.extend_token(name, days)
        _tg_send(("✅ " + name + " extended by " + str(days) + " days") if ok
                 else ("No active token found for " + name))
    else:
        _tg_send("Unknown: /token " + action + "\nUse: create, list, revoke, extend")


def _cmd_streak(args):
    """Show rolling win rate and streak."""
    try:
        import VRL_DB as _DB
        l10 = _DB.query("SELECT pnl_pts, direction, date FROM trades ORDER BY date DESC, entry_time DESC LIMIT 10")
        l20 = _DB.query("SELECT pnl_pts FROM trades ORDER BY date DESC, entry_time DESC LIMIT 20")
    except Exception:
        l10 = []; l20 = []
    if not l10:
        _tg_send("No trades in database yet.")
        return
    w10 = len([t for t in l10 if float(t.get("pnl_pts", 0)) > 0])
    w20 = len([t for t in l20 if float(t.get("pnl_pts", 0)) > 0])
    pts10 = sum(float(t.get("pnl_pts", 0)) for t in l10)
    pts20 = sum(float(t.get("pnl_pts", 0)) for t in l20)
    # Current streak
    streak = 0
    for t in l10:
        if float(t.get("pnl_pts", 0)) > 0:
            streak += 1
        else:
            break
    if streak == 0:
        for t in l10:
            if float(t.get("pnl_pts", 0)) <= 0:
                streak -= 1
            else:
                break
    streak_icon = "🟢" if streak > 0 else "🔴" if streak < 0 else "⚪"
    _tg_send(
        "📊 <b>ROLLING STATS</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "Last 10 : " + str(w10) + "W " + str(10 - w10) + "L  WR " + str(round(w10 / len(l10) * 100)) + "%\n"
        "L10 PNL : " + ("+" if pts10 >= 0 else "") + str(round(pts10, 1)) + "pts\n"
        "Last 20 : " + str(w20) + "W " + str(len(l20) - w20) + "L  WR " + str(round(w20 / len(l20) * 100) if l20 else 0) + "%\n"
        "L20 PNL : " + ("+" if pts20 >= 0 else "") + str(round(pts20, 1)) + "pts\n"
        "Streak  : " + streak_icon + " " + str(abs(streak)) + (" wins" if streak > 0 else " losses" if streak < 0 else "") + "\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    )


def _cmd_slippage(args):
    """Show fill quality stats."""
    try:
        import VRL_DB as _DB
        rows = _DB.query(
            "SELECT entry_slippage, exit_slippage, signal_price, entry_price "
            "FROM trades WHERE entry_slippage IS NOT NULL "
            "ORDER BY date DESC, entry_time DESC LIMIT 50")
    except Exception:
        rows = []

    if not rows:
        _tg_send("No slippage data yet. Data starts tracking from v13.1.")
        return

    _e_slips = [float(r.get("entry_slippage", 0)) for r in rows if r.get("entry_slippage")]
    _x_slips = [float(r.get("exit_slippage", 0)) for r in rows if r.get("exit_slippage")]
    avg_e = round(sum(_e_slips) / len(_e_slips), 2) if _e_slips else 0
    avg_x = round(sum(_x_slips) / len(_x_slips), 2) if _x_slips else 0
    total = round(sum(_e_slips) + sum(_x_slips), 2)
    cost = int(total * D.LOT_SIZE * 2)
    n = len(rows)

    _tg_send(
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "📊 <b>FILL QUALITY</b> (" + str(n) + " trades)\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "Avg entry slip : " + ("+" if avg_e >= 0 else "") + str(avg_e) + "pts\n"
        "Avg exit slip  : " + ("+" if avg_x >= 0 else "") + str(avg_x) + "pts\n"
        "Total slippage : " + str(total) + "pts\n"
        "Cost           : ₹" + "{:,}".format(abs(cost)) + "\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    )


_DISPATCH = {
    "/help"        : _cmd_help,
    "/status"      : _cmd_status,
    "/edge"        : _cmd_edge,
    "/greeks"      : _cmd_edge,
    "/spot"        : _cmd_spot,
    "/regime"      : _cmd_regime,
    "/align"       : _cmd_align,
    "/pivot"       : _cmd_pivot,
    "/pnl"         : _cmd_pnl,
    "/account"     : _cmd_account,
    "/trades"      : _cmd_trades,
    "/files"       : _cmd_files,
    "/download"    : _cmd_download,
    "/download_all": _cmd_download_all,
    "/health"      : _cmd_health,
    "/pause"       : _cmd_pause,
    "/reset_exit"  : _cmd_reset_exit,
    "/resume"      : _cmd_resume,
    "/forceexit"   : _cmd_forceexit,
    "/restart"     : _cmd_restart,
    "/livecheck"   : _cmd_livecheck,
    "/source"      : _cmd_source,
    "/token"       : _cmd_token,
    "/slippage"    : _cmd_slippage,
    "/streak"      : _cmd_streak,
    "/validate"    : _cmd_validate,
}
