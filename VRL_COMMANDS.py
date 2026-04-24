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
        "/trades    — today's trade list\n"
        "/account   — balance + margin info\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "<b>DATA</b>\n"
        "/download  — full day zip (or /download YYYY-MM-DD)\n"
        "/livecheck — last 50 log lines\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "<b>CONTROL</b>\n"
        "/pause      — block new entries\n"
        "/resume     — re-enable entries\n"
        "/forceexit  — emergency exit all lots\n"
        "/alerts_on  — pre-entry learning alerts ON\n"
        "/alerts_off — pre-entry learning alerts OFF\n"
        "/restart    — restart bot\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "VISHAL RAJPUT TRADE v16.6 — EMA9 Band Breakout, "
        "7 entry gates, 3-rule exit chain "
        "(Emergency SL / EOD 15:20 / Vishal Trail), "
        + ("PAPER" if D.PAPER_MODE else "LIVE") + " 2 lots.\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "🌐 Dashboard: http://" + _WEB_IP + ":8080"
    )

def _cmd_status(args):
    global _kite
    with _state_lock:
        st = dict(state)

    if not st.get("in_trade"):
        last_scan = st.get("_last_scan", {})
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

    # v16.2: trail tier = active stop, else initial -10
    _tier = st.get("active_ratchet_tier", "None")
    _rsl  = float(st.get("active_ratchet_sl", 0) or 0)
    if _tier and _tier not in ("", "None", "INITIAL") and _rsl > 0:
        _stop_line = "Trail  : " + _tier + " @ Rs" + str(round(_rsl, 1))
        _stop_dist = round(ltp - _rsl, 1) if ltp > 0 else "—"
    else:
        _init_sl   = round(entry - 10, 1)
        _stop_line = "Trail  : INITIAL @ Rs" + str(_init_sl)
        _stop_dist = round(ltp - _init_sl, 1) if ltp > 0 else "—"

    _vel_line = ""

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

def _cmd_download(args):
    """Full day zip — all logs + data + state for a date.
    /download             → today
    /download 2026-04-16  → specific day

    this replaces the old /download (now
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
    "/help"      : _cmd_help,
    "/status"    : _cmd_status,
    "/trades"    : _cmd_trades,
    "/account"   : _cmd_account,
    "/pause"     : _cmd_pause,
    "/resume"    : _cmd_resume,
    "/forceexit" : _cmd_forceexit,
    "/restart"   : _cmd_restart,
    "/alerts_on" : _cmd_alerts_on,
    "/alerts_off": _cmd_alerts_off,
    "/livecheck" : _cmd_livecheck,
    "/download"  : _cmd_download,
}
