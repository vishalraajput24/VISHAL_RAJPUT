# ═══════════════════════════════════════════════════════════════
#  VRL_COMMANDS.py — VISHAL RAJPUT TRADE v16.3
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

        # BUG-DL4 v15.2.5: Telegram bot upload cap is 50MB. If the zip
        # exceeds the soft 45MB threshold (5MB headroom for multipart
        # overhead), don't attempt the send — put it behind the local
        # web server and reply with the URL. Preserves the zip on disk
        # in all paths so SSH retrieval stays possible.
        _TG_SIZE_LIMIT_MB = 45
        caption = ("📦 VRL Logs — " + target_date
                   + "\n" + str(file_count) + " files | "
                   + str(size_mb) + " MB"
                   + "\n" + cat_summary)

        if size_mb > _TG_SIZE_LIMIT_MB:
            # Stage under STATE_DIR if it isn't already — create_daily_zip
            # already writes there, so just name the link.
            _link_hint = "http://" + str(globals().get("_WEB_IP", "localhost")) + ":8080"
            logger.warning("[DOWNLOAD] zip " + os.path.basename(zip_path)
                           + " is " + str(size_mb) + "MB > "
                           + str(_TG_SIZE_LIMIT_MB) + "MB Telegram cap — "
                           "skipping send, file preserved at " + zip_path)
            _tg_send(
                "⚠️ <b>DOWNLOAD TOO LARGE FOR TELEGRAM</b>\n"
                "Date : " + target_date + "\n"
                "Size : " + str(size_mb) + " MB (cap " + str(_TG_SIZE_LIMIT_MB) + " MB)\n"
                "Files: " + str(file_count) + "\n"
                + cat_summary + "\n"
                "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                "Local path: <code>" + zip_path + "</code>\n"
                "Fetch via SSH or browse " + _link_hint + " /files."
            )
            # Do NOT unlink — operator needs the file on disk to pull.
            return

        _ok = False
        try:
            _ok = bool(_tg_send_file(zip_path, caption=caption))
        except Exception as _se:
            logger.error("[DOWNLOAD] Telegram file send raised: "
                         + type(_se).__name__ + " " + str(_se))
            _ok = False

        if _ok:
            logger.info("[DOWNLOAD] sent " + os.path.basename(zip_path)
                        + " (" + str(size_mb) + "MB, "
                        + str(file_count) + " entries)")
            try:
                os.remove(zip_path)
            except Exception:
                pass
        else:
            # BUG-DL4: keep zip on disk on failure + report local path.
            logger.warning("[DOWNLOAD] Telegram send failed — zip "
                           "preserved for SSH retrieval: " + zip_path)
            _tg_send(
                "⚠️ <b>DOWNLOAD DELIVERY FAILED</b>\n"
                "Date : " + target_date + "\n"
                "Size : " + str(size_mb) + " MB\n"
                "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                "File kept on disk for SSH pull:\n"
                "<code>" + zip_path + "</code>"
            )
    except Exception as e:
        _tg_send("Download error: " + str(e))

# ═══════════════════════════════════════════════════════════════
#  TELEGRAM COMMAND HANDLERS
# ═══════════════════════════════════════════════════════════════

def _why_blocked(st: dict) -> str:
    if st.get("paused"):
        return "⏸ PAUSED"
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
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "<b>DATA</b>\n"
        "/files     — browse folders\n"
        "/download           — full day zip (or /download YYYY-MM-DD)\n"
        "/download_strategy  — 4-file shortcut (trade log + DB + config + state)\n"
        "/health    — system health check\n"
        "/validate  — 10 system alignment checks\n"
        "/livecheck — last 50 log lines\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "<b>CONTROL</b>\n"
        "/pause     — block new entries\n"
        "/resume    — re-enable entries\n"
        "/forceexit — emergency exit all lots\n"
        "/alerts_on — pre-entry learning alerts ON\n"
        "/alerts_off— pre-entry learning alerts OFF\n"
        "/restart   — restart bot\n"
        "/token     — manage subscriber access tokens\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        + ("PAPER" if D.PAPER_MODE else "LIVE")
        + " | v16.3 EMA9 Band Breakout (3-min)\n"
        + "Entry: close &gt; EMA9L + green + body 40% + rising\n"
        + "Exit: Vishal Trail (70% capture) | Emergency -10 | EOD 15:20\n"
        + "2 lots fixed | No entry before 9:30 or after 15:10\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "🌐 Dashboard: http://" + _WEB_IP + ":8080"
    )

def _cmd_status(args):
    global _kite
    with _state_lock:
        st = dict(state)

    if not st.get("in_trade"):
        last_scan = st.get("_last_scan", {})
        # BUG-030: Read warmup state from dashboard JSON (written by VRL_MAIN)
        _warmup_line = ""
        try:
            import json as _j
            import os as _os
            _dash_path = _os.path.join(D.STATE_DIR, "vrl_dashboard.json")
            if _os.path.isfile(_dash_path):
                with open(_dash_path) as _df:
                    _d = _j.load(_df)
                _mk = _d.get("market", {})
                if _mk.get("market_open") and not _mk.get("indicators_warm", True):
                    _wp = _mk.get("warmup_progress", 0)
                    _wn = _mk.get("warmup_needed", 14)
                    _we = _mk.get("warmup_eta", "—")
                    _warmup_line = ("🟡 WARMUP (" + str(_wp) + "/" + str(_wn) + " candles)\n"
                                    "ETA       : " + _we + "\n"
                                    "Trades blocked until indicators stable\n"
                                    "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n")
        except Exception:
            pass
        _tg_send(
            "📊 <b>STATUS — NO TRADE</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            + _warmup_line +
            "PNL    : " + str(round(st.get("daily_pnl", 0), 1)) + "pts\n"
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

    # v16.2: trail tier = active stop, else initial -10
    _tier = st.get("active_ratchet_tier", "None")
    _rsl  = float(st.get("active_ratchet_sl", 0) or 0)
    if _tier and _tier not in ("", "None", "INITIAL") and _rsl > 0:
        _stop_line = "Trail  : " + _tier + " @ Rs" + str(round(_rsl, 1))
        _stop_dist = round(ltp - _rsl, 1) if ltp > 0 else "—"
    else:
        _init_sl   = round(entry - 10, 1)    # v16.2: was -12
        _stop_line = "Trail  : INITIAL @ Rs" + str(_init_sl)
        _stop_dist = round(ltp - _init_sl, 1) if ltp > 0 else "—"

    # v15.2.5: velocity + peak_history for /status
    _vel = round(float(st.get("current_velocity", 0) or 0), 2)
    _ph  = (st.get("peak_history") or [])[-4:]
    _vel_sign = "+" if _vel >= 0 else ""
    if _vel > 1:
        _vel_tag = "GROWING"
    elif _vel > 0:
        _vel_tag = "SLOWING"
    elif _vel == 0:
        _vel_tag = "FLAT ⚠️"
    else:
        _vel_tag = "DYING ⚠️"
    _vel_line = ("Vel    : " + _vel_sign + str(_vel) + " pts/candle (" + _vel_tag + ")\n"
                 + "Peaks  : " + str(_ph) + "\n")

    _tg_send(
        "📊 <b>STATUS — IN TRADE</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "Time   : " + _now_str() + "\n"
        "Symbol : " + st.get("symbol", "") + "\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "Entry  : " + str(round(entry, 2)) + "\n"
        "LTP    : " + str(round(ltp, 2)) + "\n"
        "PNL    : " + ("+" if pnl >= 0 else "") + str(pnl) + "pts  " + _rs(pnl) + "\n"
        "Peak   : +" + str(round(peak, 1)) + "pts\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        + _stop_line + "  (" + str(_stop_dist) + "pts away)\n"
        + _vel_line +
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "Day PNL: " + str(round(st.get("daily_pnl", 0), 1)) + "pts"
    )

def _cmd_pnl(args):
    with _state_lock:
        st = dict(state)
    pnl    = st.get("daily_pnl", 0)
    sign   = "+" if pnl >= 0 else ""

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
            _g = float(_t.get("pnl_pts", 0)) * float(_t.get("qty_exited", D.get_lot_size() * 2))
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

    gate_str = "✅ Clear"

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
def _cmd_download_strategy(args):
    """Smart download — 4 key files only (trade log + DB + config + state).
    Accessible via /download_strategy.

    BUG-DL2 v15.2.5: replaced the four hardcoded paths with canonical
    constants from VRL_DATA so the zip actually picks up the file the
    bot is really reading + writing. The old hardcoded
    ~/state/vrl_live_state.json pointed at a path that hasn't existed
    since BUG-015; the real file is under D.STATE_FILE_PATH
    (~/VISHAL_RAJPUT/state/vrl_live_state.json).

    BUG-DL1 v15.2.5: renamed from _cmd_download. /download now delivers
    the full-day zip (previous /download_all behavior) — the semantics
    operators expected all along.
    """
    import zipfile as _zf
    import os as _os
    from datetime import date as _d
    _today = _d.today().strftime("%Y-%m-%d")
    _zip_name = "vrl_strategy_" + _today + ".zip"
    _zip_path = _os.path.join(_os.path.expanduser("~"), _zip_name)

    # Canonical paths — all derived from VRL_DATA so a future refactor
    # of BASE_DIR / LAB_DIR / STATE_DIR updates this command
    # automatically.
    _db_path = getattr(D, "DB_PATH", None) or _os.path.join(D.LAB_DIR, "vrl_data.db")
    _cfg_path = _os.path.join(_os.path.dirname(
        _os.path.abspath(D.__file__)), "config.yaml")
    _files = [
        (D.TRADE_LOG_PATH,   "vrl_trade_log.csv"),
        (_db_path,           "vrl_data.db"),
        (_cfg_path,          "config.yaml"),
        (D.STATE_FILE_PATH,  "vrl_live_state.json"),
    ]
    # Warn on any missing canonical path before trying to zip.
    _missing = [fname for fpath, fname in _files if not _os.path.isfile(fpath)]
    if _missing:
        logger.warning("[DOWNLOAD] missing canonical files: "
                       + ", ".join(_missing))
    try:
        with _zf.ZipFile(_zip_path, "w", _zf.ZIP_DEFLATED) as zf:
            for fpath, fname in _files:
                if _os.path.isfile(fpath):
                    zf.write(fpath, fname)
        _tg_send_file(_zip_path, "📦 Strategy data — " + _today)
        _os.remove(_zip_path)
    except Exception as e:
        _tg_send("Download error: " + str(e))


def _cmd_download(args):
    """Full day zip — all logs + data + state for a date.
    /download             → today
    /download 2026-04-16  → specific day

    BUG-DL1 v15.2.5: this replaces the old /download (now
    /download_strategy) because operators uniformly treated this
    as "give me today's full data". The 4-file shortcut is still
    available via /download_strategy for the old muscle memory.
    """
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
            _tg_send("Usage: /download or /download 2026-04-16")
            return
    _send_today_download(target)


def _cmd_download_all(args):
    """DEPRECATED alias for /download. Still registered so existing
    operator muscle memory works. BUG-DL1 v15.2.5."""
    try:
        logger.warning("[DOWNLOAD] /download_all is DEPRECATED — use /download")
    except Exception:
        pass
    _cmd_download(args)

def _cmd_health(args):
    import os as _os
    now      = datetime.now()
    spot_ltp = D.get_ltp(D.NIFTY_SPOT_TOKEN)
    vix_ltp  = D.get_vix()
    ws_ok    = D.is_tick_live(D.NIFTY_SPOT_TOKEN)
    market   = D.is_market_open()

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
        "In trade   : " + str(state.get("in_trade", False)) + "\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "Disk free  : " + str(disk_free_mb) + " MB\n"
        "Lot size   : " + str(D.get_lot_size()) + " (from broker)\n"
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
        state["paused"] = False
    _tg_send("▶️ Resumed.")
    logger.info("[CTRL] Resumed")

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
            state["qty"] = D.get_lot_size()
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

def _cmd_regime(args):
    """Current regime + detection mode."""
    try:
        spot_3m = D.get_spot_indicators("3minute")
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
        from VRL_DB import manual_validate
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
    cost = int(total * D.get_lot_size() * 2)
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


def _cmd_alerts_on(args):
    with _state_lock:
        state["pre_entry_alerts_enabled"] = True
    _tg_send("🔔 <b>Pre-entry alerts ON</b>\n"
             "REVERSAL 🔔 / APPROACHING ⏰ / READY ⚡ / BLOCKED ⚠️ "
             "events will send during the trading window.\n"
             "Use /alerts_off to silence.")


def _cmd_alerts_off(args):
    with _state_lock:
        state["pre_entry_alerts_enabled"] = False
    _tg_send("🔕 <b>Pre-entry alerts OFF</b>\n"
             "Learning-mode alerts silenced. Trade alerts + EOD still fire.\n"
             "Use /alerts_on to re-enable.")


_DISPATCH = {
    "/help"        : _cmd_help,
    "/alerts_on"   : _cmd_alerts_on,
    "/alerts_off"  : _cmd_alerts_off,
    "/status"      : _cmd_status,
    "/regime"      : _cmd_regime,
    "/align"       : _cmd_align,
    "/pnl"         : _cmd_pnl,
    "/account"     : _cmd_account,
    "/trades"      : _cmd_trades,
    "/files"       : _cmd_files,
    "/download"         : _cmd_download,
    "/download_strategy": _cmd_download_strategy,
    "/download_all"     : _cmd_download_all,   # BUG-DL1: deprecated alias
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
