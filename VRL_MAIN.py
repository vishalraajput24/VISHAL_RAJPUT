# ═══════════════════════════════════════════════════════════════
#  VRL_MAIN.py — VISHAL RAJPUT TRADE v12.13
#  Master orchestration file.
#  v12.13: Expiry breakout mode, fib pivots, /pivot command,
#          spot buffer feed, expiry-specific entry logic.
# ═══════════════════════════════════════════════════════════════

import csv
import json
import logging
import os
import requests
import signal
import sys
import threading
import time
import zipfile
from copy import deepcopy
from datetime import date, datetime

# ── Bootstrap dirs first ────────────────────────────────────────
import VRL_DATA as D
D.ensure_dirs()

from VRL_DATA   import setup_logger
from VRL_AUTH   import get_kite
from VRL_ENGINE import (
    check_entry, manage_exit, pre_entry_checks,
    loss_streak_gate, check_profit_lock,
    compute_entry_sl, check_expiry_breakout,
)
from VRL_TRADE  import place_entry, place_exit
from VRL_LAB    import start_lab

# ── Loggers ─────────────────────────────────────────────────────
logger     = setup_logger("vrl_live", D.LIVE_LOG_FILE)
lab_logger = setup_logger("vrl_lab",  D.LAB_LOG_FILE)

# ── Telegram base ───────────────────────────────────────────────
_TG_BASE = "https://api.telegram.org/bot"

# Global kite instance for REST fallback
_kite = None

# ═══════════════════════════════════════════════════════════════
#  STATE (re‑entry fields removed)
# ═══════════════════════════════════════════════════════════════

_state_lock = threading.Lock()

DEFAULT_STATE = {
    "in_trade"           : False,
    "symbol"             : "",
    "token"              : None,
    "direction"          : "",
    "entry_price"        : 0.0,
    "entry_time"         : "",
    "exit_phase"         : 1,
    "phase1_sl"          : 0.0,
    "phase2_sl"          : 0.0,
    "qty"                : D.LOT_SIZE,
    "trail_tightened"    : False,
    "profit_locked"      : False,
    "consecutive_losses" : 0,
    "daily_trades"       : 0,
    "daily_losses"       : 0,
    "daily_pnl"          : 0.0,
    "peak_pnl"           : 0.0,
    "mode"               : "",
    "iv_at_entry"        : 0.0,
    "score_at_entry"     : 0,
    "regime_at_entry"    : "",
    "dte_at_entry"       : 0,
    "last_exit_time"     : "",
    "_last_trail_candle" : "",
    "strike"             : 0,
    "expiry"             : "",
    "paused"             : False,
    "force_exit"         : False,
    "candles_held"       : 0,
    "_last_1min_candle"  : "",
    "_eod_reported"      : False,
    "_last_candle_held_min": "",
    "_rsi_was_overbought": False,
    "_last_scan"         : {},
    "_exit_failed"       : False,
    "_circuit_breaker"   : False,
    "_error_count"       : 0,
    "_last_milestone"    : 0,
}

state   = deepcopy(DEFAULT_STATE)
_running = True

# ═══════════════════════════════════════════════════════════════
#  STATE PERSISTENCE
# ═══════════════════════════════════════════════════════════════

def _save_state():
    try:
        persist_fields = D.STATE_PERSIST_FIELDS + ["_rsi_was_overbought"]
        subset = {k: state.get(k) for k in persist_fields}
        tmp    = D.STATE_FILE_PATH + ".tmp"
        with open(tmp, "w") as f:
            json.dump(subset, f, indent=2, default=str)
        os.replace(tmp, D.STATE_FILE_PATH)
    except Exception as e:
        logger.error("[MAIN] State save error: " + str(e))

def _load_state():
    if not os.path.isfile(D.STATE_FILE_PATH):
        return
    try:
        with open(D.STATE_FILE_PATH) as f:
            saved = json.load(f)
        with _state_lock:
            for k, v in saved.items():
                if k in state:
                    state[k] = v
        logger.info("[MAIN] State loaded from disk")
        if state.get("in_trade"):
            logger.info("[MAIN] ⚠ Was in trade on last shutdown — "
                        + str(state.get("symbol")) + " monitoring resumed")
            _tg_send(
                "🔄 <b>Bot restarted mid-trade</b>\n"
                "Symbol : " + str(state.get("symbol")) + "\n"
                "Phase  : " + str(state.get("exit_phase")) + "\n"
                "Resuming exit monitoring."
            )
    except Exception as e:
        logger.error("[MAIN] State load error: " + str(e))

def _reset_daily(today_str: str):
    with _state_lock:
        state["daily_trades"]          = 0
        state["daily_losses"]          = 0
        state["daily_pnl"]             = 0.0
        state["profit_locked"]         = False
        state["_eod_reported"]         = False
        state["paused"]                = False
    D.clear_token_cache()
    logger.info("[MAIN] Daily reset")
    _save_state()

# ═══════════════════════════════════════════════════════════════
#  PID FILE
# ═══════════════════════════════════════════════════════════════

def _write_pid():
    try:
        with open(D.PID_FILE_PATH, "w") as f:
            f.write(str(os.getpid()))
    except Exception:
        pass

def _remove_pid():
    try:
        if os.path.isfile(D.PID_FILE_PATH):
            os.remove(D.PID_FILE_PATH)
    except Exception:
        pass

# ═══════════════════════════════════════════════════════════════
#  TRADE LOG
# ═══════════════════════════════════════════════════════════════

TRADE_FIELDNAMES = [
    "date", "entry_time", "exit_time", "symbol", "direction",
    "mode", "entry_price", "exit_price", "pnl_pts", "pnl_rs",
    "peak_pnl", "exit_reason", "score", "iv_at_entry",
    "regime", "dte", "candles_held",
]

def _log_trade(st: dict, exit_price: float, exit_reason: str,
               candles_held: int = 0):
    os.makedirs(os.path.dirname(D.TRADE_LOG_PATH), exist_ok=True)
    is_new  = not os.path.isfile(D.TRADE_LOG_PATH)
    entry   = st.get("entry_price", 0)
    pnl_pts = round(exit_price - entry, 2)
    pnl_rs  = round(pnl_pts * D.LOT_SIZE, 2)

    row = {
        "date"        : date.today().isoformat(),
        "entry_time"  : st.get("entry_time", ""),
        "exit_time"   : datetime.now().strftime("%H:%M:%S"),
        "symbol"      : st.get("symbol", ""),
        "direction"   : st.get("direction", ""),
        "mode"        : st.get("mode", ""),
        "entry_price" : entry,
        "exit_price"  : round(exit_price, 2),
        "pnl_pts"     : pnl_pts,
        "pnl_rs"      : pnl_rs,
        "peak_pnl"    : round(st.get("peak_pnl", 0), 2),
        "exit_reason" : exit_reason,
        "score"       : st.get("score_at_entry", 0),
        "iv_at_entry" : st.get("iv_at_entry", 0),
        "regime"      : st.get("regime_at_entry", ""),
        "dte"         : st.get("dte_at_entry", 0),
        "candles_held": candles_held,
    }

    try:
        with open(D.TRADE_LOG_PATH, "a", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=TRADE_FIELDNAMES, extrasaction="ignore")
            if is_new:
                writer.writeheader()
            writer.writerow(row)
            f.flush()
    except Exception as e:
        logger.error("[MAIN] Trade log error: " + str(e))

def _read_today_trades() -> list:
    today_str = date.today().isoformat()
    trades    = []
    if not os.path.isfile(D.TRADE_LOG_PATH):
        return trades
    try:
        with open(D.TRADE_LOG_PATH, "r") as f:
            for row in csv.DictReader(f):
                if row.get("date", "") == today_str:
                    trades.append(row)
    except Exception as e:
        logger.error("[MAIN] Read trades error: " + str(e))
    return trades

# ═══════════════════════════════════════════════════════════════
#  TELEGRAM — SEND HELPERS
# ═══════════════════════════════════════════════════════════════

def _now_str() -> str:
    return datetime.now().strftime("%H:%M:%S")

def _mode_tag() -> str:
    return "📄 PAPER" if D.PAPER_MODE else "💰 LIVE"

def _rs(pts: float) -> str:
    rupees = round(pts * D.LOT_SIZE, 0)
    sign   = "+" if rupees >= 0 else ""
    return sign + "₹" + str(int(rupees))

def _tg_send_sync(text: str, parse_mode: str = "HTML", chat_id: str = None) -> bool:
    """Blocking send — used internally only."""
    if not D.TELEGRAM_TOKEN or not (chat_id or D.TELEGRAM_CHAT_ID):
        return False
    cid = chat_id or D.TELEGRAM_CHAT_ID
    url = _TG_BASE + D.TELEGRAM_TOKEN + "/sendMessage"
    try:
        resp = requests.post(url, json={
            "chat_id"              : cid,
            "text"                 : text,
            "parse_mode"           : parse_mode,
            "disable_notification" : False,
        }, timeout=10)
        if not resp.ok:
            logger.warning("[TG] Send failed: " + resp.text[:200])
            return False
        return True
    except Exception as e:
        logger.error("[TG] send error: " + str(e))
        return False

def _tg_send(text: str, parse_mode: str = "HTML", chat_id: str = None) -> bool:
    """Non-blocking send — fires in background thread so strategy loop never waits."""
    t = threading.Thread(
        target=_tg_send_sync,
        args=(text, parse_mode, chat_id),
        daemon=True
    )
    t.start()
    return True

def _tg_send_file(file_path: str, caption: str = "", chat_id: str = None) -> bool:
    if not D.TELEGRAM_TOKEN:
        return False
    cid = chat_id or D.TELEGRAM_CHAT_ID
    url = _TG_BASE + D.TELEGRAM_TOKEN + "/sendDocument"
    try:
        with open(file_path, "rb") as f:
            resp = requests.post(url, data={
                "chat_id": cid,
                "caption": caption[:1024],
            }, files={"document": f}, timeout=60)
        if not resp.ok:
            logger.warning("[TG] File send failed: " + resp.text[:200])
            return False
        return True
    except Exception as e:
        logger.error("[TG] send_file error: " + str(e))
        return False

def _tg_inline_keyboard(text: str, keyboard: list, chat_id: str = None) -> dict:
    if not D.TELEGRAM_TOKEN:
        return {}
    cid = chat_id or D.TELEGRAM_CHAT_ID
    url = _TG_BASE + D.TELEGRAM_TOKEN + "/sendMessage"
    try:
        resp = requests.post(url, data={
            "chat_id"      : cid,
            "text"         : text,
            "parse_mode"   : "HTML",
            "reply_markup" : json.dumps({"inline_keyboard": keyboard}),
        }, timeout=10)
        if resp.ok:
            return resp.json().get("result", {})
    except Exception as e:
        logger.error("[TG] keyboard error: " + str(e))
    return {}

def _tg_answer_callback(callback_query_id: str, text: str = ""):
    url = _TG_BASE + D.TELEGRAM_TOKEN + "/answerCallbackQuery"
    try:
        requests.post(url, json={
            "callback_query_id": callback_query_id,
            "text": text,
        }, timeout=5)
    except Exception:
        pass

# ═══════════════════════════════════════════════════════════════
#  TELEGRAM — TRADE ALERTS
# ═══════════════════════════════════════════════════════════════

def _alert_bot_started():
    _tg_send(
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "🚀 <b>VISHAL RAJPUT TRADE " + D.VERSION + "</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "Time   : " + _now_str() + "\n"
        "Mode   : " + _mode_tag() + "\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "GATES (v12.13)\n"
        "3-min  : 3/4 conditions — option trending UP\n"
        "CE     : TRENDING regime + 1m spread ≥+8pts\n"
        "PE     : 3-min permitted + 1m spread ≥+6pts\n"
        "Both   : Option must trend UP (EMA9 > EMA21)\n"
        "Score  : ≥5 to fire  |  ≥6 after streak\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "P-Lock : +" + str(D.PROFIT_LOCK_PTS) + "pts\n"
        "/help for commands."
    )

# ── Score breakdown formatter ──────────────────────────────────
def _fmt_score_breakdown(bd: dict, score: int) -> str:
    if not bd:
        return "Score   : " + str(score) + "/7\n"
    parts = []
    if bd.get("body"):         parts.append("Body")
    if bd.get("body_bonus"):   parts.append("+Bonus")
    if bd.get("rsi"):          parts.append("RSI")
    if bd.get("volume"):       parts.append("Vol")
    if bd.get("delta"):        parts.append("Delta")
    if bd.get("double_align"): parts.append("2xAlign")
    if bd.get("gate_bonus"):   parts.append("GateBonus")
    return "Score   : " + str(score) + "/7  [" + " ".join(parts) + "]\n"

def _alert_entry(symbol: str, option_type: str, entry_price: float,
                 mode: str, score: int, profile: dict,
                 det_1m: dict, det_3m: dict, greeks: dict,
                 dte: int, regime: str,
                 score_breakdown: dict = None,
                 prediction: dict = None,
                 spread_1m: float = 0.0,
                 session: str = "MORNING"):
    sl_pts    = profile.get("conv_sl_pts", 20)
    be_pts    = profile.get("conv_breakeven_pts", 14)
    trail_pts = round(be_pts * 1.2)
    spread_3m = round(det_3m.get("ema_spread_3m", 0), 1) if det_3m else 0
    met_3m    = det_3m.get("conditions_met", 0) if det_3m else 0
    bonus     = det_3m.get("bonus", 0) if det_3m else 0
    rsi_3m    = det_3m.get("rsi_val_3m", 0) if det_3m else 0
    body_3m   = det_3m.get("body_pct_3m", 0) if det_3m else 0

    # Trend label
    def trend_lbl(sp, ot):
        if ot == "CE": return "UP 📈" if sp >= 5 else "WEAK" if sp >= 2 else "FLAT"
        else:          return "DOWN 📈" if sp <= -5 else "WEAK" if sp <= -2 else "FLAT"

    # Prediction block
    pred_line = ""
    if prediction:
        c = prediction.get("conservative", 0)
        t = prediction.get("target", 0)
        s = prediction.get("stretch", 0)
        pred_line = (
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "📊 <b>PREDICTION</b>\n"
            "Conservative : +" + str(c) + "pts  ₹" + str(c * D.LOT_SIZE) + "\n"
            "Target       : +" + str(t) + "pts  ₹" + str(t * D.LOT_SIZE) + "\n"
            "Stretch      : +" + str(s) + "pts  ₹" + str(s * D.LOT_SIZE) + "\n"
        )

    _tg_send(
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "🎯 <b>CONVICTION ENTRY — " + option_type + "</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        + _now_str() + "  " + symbol + "\n"
        "Entry   : ₹" + str(round(entry_price, 2)) + "\n"
        + _fmt_score_breakdown(score_breakdown, score)
        + "Regime  : " + regime + "  DTE:" + str(dte)
        + "  VIX:" + str(round(D.get_vix(), 1)) + "\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "WHY THIS FIRED\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "3-MIN  " + str(met_3m) + "/4 ✅  "
        + trend_lbl(spread_3m, option_type)
        + "  Gap:" + str(abs(spread_3m)) + "pts"
        + ("  ⚡BONUS" if bonus else "") + "\n"
        "  Body:" + str(body_3m) + "%  RSI:" + str(rsi_3m) + "\n"
        "1-MIN  ✅ trigger clean\n"
        "  Body:" + str(det_1m.get("body_pct", 0)) + "%"
        + ("✅" if det_1m.get("body_ok") else "❌")
        + "  RSI:" + str(det_1m.get("rsi_val", 0)) + "↑"
        + "  Vol:" + str(det_1m.get("vol_ratio", 0)) + "x\n"
        "  1m Spread:" + str(round(spread_1m, 1)) + "pts"
        + ("  🔥DOUBLE" if abs(spread_3m) >= 5 and spread_1m > 0 else "") + "\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "GREEKS  Delta:" + str(greeks.get("delta","—"))
        + "  IV:" + str(greeks.get("iv_pct","—")) + "%"
        + "  Θ:" + str(greeks.get("theta","—")) + "/day\n"
        + pred_line
        + "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "EXIT PLAN\n"
        "SL     : −" + str(sl_pts) + "pts  →  ₹" + str(sl_pts * D.LOT_SIZE) + "\n"
        "Phase2 : +" + str(be_pts) + "pts  SL moves to entry+2\n"
        "Phase3 : +" + str(trail_pts) + "pts  5-min EMA trail starts\n"
        "Top    : RSI≥76 → RSI_EXHAUSTION exit 🎯"
    )

def _alert_exit(symbol: str, entry: float, exit_price: float,
                pnl: float, reason: str, mode: str, peak_pnl: float,
                candles_held: int = 0, regime: str = "", score: int = 0,
                daily_pnl: float = 0.0, daily_trades: int = 0,
                daily_wins: int = 0, daily_losses: int = 0):

    pnl_sign = "+" if pnl >= 0 else ""
    icon     = "✅" if pnl >= 0 else "❌"
    captured = round(pnl / peak_pnl * 100) if peak_pnl > 0 else 0

    reason_map = {
        "PHASE1_SL"           : ("Stop loss hit", "Price moved against immediately — no momentum"),
        "BREAKEVEN_SL"        : ("Stopped at breakeven", "Gave back all gains — move reversed at peak"),
        "TRAIL_WIDE"          : ("5-min EMA trail exit", "Candle body closed below EMA9 — trend ended"),
        "TRAIL_TIGHT"         : ("3-min EMA trail exit", "Tight trail triggered — momentum slowing"),
        "FLOOR_WIDE"          : ("Floor breach exit", "Catastrophic reversal below low-12pts"),
        "FLOOR_TIGHT"         : ("Floor breach — tight trail", ""),
        "PEAK_DRAWDOWN_WIDE"  : ("Peak drawdown 40%", "Winner protected — gave back 40% from peak"),
        "PEAK_DRAWDOWN_TIGHT" : ("Peak drawdown tight trail", ""),
        "TIGHT_TRAIL"         : ("Tight trail triggered", "15pt+ profit, drawdown exceeded 5pts — gains locked"),
        "MODERATE_DRAWDOWN"   : ("Moderate drawdown exit", "20pt+ peak, gave back 8pts — profit protected"),
        "RSI_EXHAUSTION"      : ("RSI exhaustion exit 🎯", "RSI hit 76+ with profit — top captured"),
        "GAMMA_RIDER"         : ("Gamma rider exit 🏄", "RSI dropped from overbought — reversal caught"),
        "STALE_ENTRY"         : ("Stale entry cut 🔪", "5 candles, peak under 5pts — dead trade, saved full SL"),
        "MARKET_CLOSE"        : ("Market close exit", "Forced exit at 15:28"),
        "FORCE_EXIT"          : ("Manual force exit", ""),
    }
    reason_title, reason_why = reason_map.get(reason, (reason, ""))

    # Trade quality assessment
    if pnl > 0 and captured >= 70:
        quality = "🌟 EXCELLENT  (captured " + str(captured) + "% of peak)"
    elif pnl > 0 and captured >= 50:
        quality = "✅ GOOD  (captured " + str(captured) + "% of peak)"
    elif pnl > 0:
        quality = "⚠️ OK  (captured " + str(captured) + "% of peak)"
    elif reason == "STALE_ENTRY":
        saved = round((18 - abs(pnl)) if abs(pnl) < 18 else 0, 1)
        quality = "🛡 PROTECTED  (saved ~" + str(saved) + "pts vs full SL)"
    else:
        quality = "❌ LOSS  (peak was +" + str(round(peak_pnl, 1)) + "pts)"

    dpnl_sign = "+" if daily_pnl >= 0 else ""

    _tg_send(
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        + icon + " <b>EXIT — " + option_type_from_symbol(symbol) + "</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        + _now_str() + "  " + symbol + "\n"
        "Entry  : " + str(round(entry,2))
        + "  →  Exit: " + str(round(exit_price,2)) + "\n"
        "PNL    : " + pnl_sign + str(round(pnl,1)) + "pts  " + _rs(pnl) + "\n"
        "Peak   : +" + str(round(peak_pnl,1)) + "pts"
        + "  Held: " + str(candles_held) + " candles\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "WHY EXITED\n"
        + reason_title + "\n"
        + (reason_why + "\n" if reason_why else "")
        + "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "TRADE QUALITY\n"
        + quality + "\n"
        "Regime : " + (regime or "—") + "  Score: " + str(score) + "\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "TODAY  " + dpnl_sign + str(round(daily_pnl,1)) + "pts  " + _rs(daily_pnl) + "\n"
        + str(daily_trades) + "T  W" + str(daily_wins) + " L" + str(daily_losses)
    )

def option_type_from_symbol(symbol: str) -> str:
    if symbol.endswith("CE"): return "CE"
    if symbol.endswith("PE"): return "PE"
    return "?"

def _alert_trail_tightened(symbol: str, rsi_val: float):
    _tg_send(
        "⚡ <b>TRAIL TIGHTENED — " + symbol + "</b>\n"
        "RSI " + str(round(rsi_val,1)) + " — momentum peaked\n"
        "5-min EMA → 3-min EMA now active\n"
        "Riding with tighter net. No forced exit."
    )

def _alert_profit_lock(daily_pnl: float):
    _tg_send(
        "🔒 <b>PROFIT LOCK — +" + str(round(daily_pnl,1)) + "pts  " + _rs(daily_pnl) + "</b>\n"
        "All trails tightened to 3-min EMA.\n"
        "New entries still open but protected mode on."
    )

def _alert_loss_streak_gate(streak: int, score: int, required: int):
    _tg_send(
        "⏸ <b>STREAK GATE — " + str(streak) + " losses</b>\n"
        "Score " + str(score) + " below required " + str(required) + "\n"
        "Waiting for stronger setup. Capital protected."
    )

def _alert_exit_critical(symbol: str, qty: int):
    _tg_send(
        "🚨 <b>CRITICAL: EXIT FAILED</b>\n"
        "Symbol : " + symbol + "  Qty: " + str(qty) + "\n"
        "MANUAL EXIT REQUIRED NOW\n"
        "Open Kite app immediately."
    )

def _alert_error(message: str):
    _tg_send("⚠️ <b>ERROR</b>  " + _now_str() + "\n" + message)

# ═══════════════════════════════════════════════════════════════
#  EOD REPORT
# ═══════════════════════════════════════════════════════════════

def _generate_eod_report():
    trades = _read_today_trades()
    today  = date.today().strftime("%d %b %Y")

    if not trades:
        _tg_send("📊 <b>EOD REPORT — " + today + "</b>\nNo trades today.")
        return

    total_pts  = sum(float(t.get("pnl_pts", 0)) for t in trades)
    total_rs   = sum(float(t.get("pnl_rs",  0)) for t in trades)
    wins       = [t for t in trades if float(t.get("pnl_pts", 0)) > 0]
    losses     = [t for t in trades if float(t.get("pnl_pts", 0)) <= 0]
    n_trades   = len(trades)
    win_rate   = round(len(wins) / n_trades * 100, 0) if n_trades > 0 else 0
    best       = max((float(t.get("pnl_pts", 0)) for t in trades), default=0)
    worst      = min((float(t.get("pnl_pts", 0)) for t in trades), default=0)
    convictions = [t for t in trades if t.get("mode") == "CONVICTION"]

    sign = "+" if total_pts >= 0 else ""
    icon = "✅" if total_pts >= 0 else "❌"

    trade_lines = ""
    for i, t in enumerate(trades, 1):
        pts   = float(t.get("pnl_pts", 0))
        sign2 = "+" if pts >= 0 else ""
        trade_lines += (
            str(i) + ". " + t.get("direction", "") + " C"
            + "  " + sign2 + str(round(pts, 1)) + "pts"
            + "  [" + t.get("exit_reason", "")[:14] + "]\n"
        )

    _tg_send(
        icon + " <b>EOD REPORT — " + today + "</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "Net PNL    : " + sign + str(round(total_pts, 1)) + "pts  "
        + sign + "₹" + str(int(total_rs)) + "\n"
        "Trades     : " + str(n_trades) + "  "
        + "W=" + str(len(wins)) + " L=" + str(len(losses)) + "\n"
        "Win Rate   : " + str(win_rate) + "%\n"
        "Best       : +" + str(round(best, 1)) + "pts\n"
        "Worst      : " + str(round(worst, 1)) + "pts\n"
        "Conviction : " + str(len(convictions)) + "\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "<b>TRADES</b>\n"
        + trade_lines
        + "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        + ("Mode: PAPER 📄" if D.PAPER_MODE else "Mode: LIVE 💰")
    )

# ═══════════════════════════════════════════════════════════════
#  ENTRY + EXIT EXECUTION
# ═══════════════════════════════════════════════════════════════

def _execute_entry(kite, option_info: dict, option_type: str,
                   entry_result: dict, profile: dict,
                   expiry, dte: int, session: str = "MORNING"):
    token       = option_info["token"]
    symbol      = option_info["symbol"]
    entry_price = entry_result["entry_price"]

    fill = place_entry(kite, symbol, token, option_type,
                       D.LOT_SIZE, entry_price)

    if not fill["ok"]:
        logger.error("[MAIN] Entry failed: " + fill["error"])
        _alert_error("Entry failed: " + fill["error"])
        D.unsubscribe_tokens([token])
        return

    actual_price = fill["fill_price"]
    actual_qty   = fill["fill_qty"]
    phase1_sl    = compute_entry_sl(actual_price, profile, entry_result["mode"], token, dte)

    with _state_lock:
        state["in_trade"]           = True
        state["symbol"]             = symbol
        state["token"]              = token
        state["direction"]          = option_type
        state["entry_price"]        = actual_price
        state["entry_time"]         = datetime.now().strftime("%H:%M:%S")
        state["exit_phase"]         = 1
        state["phase1_sl"]          = phase1_sl
        state["qty"]                = actual_qty
        state["trail_tightened"]    = False
        state["peak_pnl"]           = 0.0
        state["mode"]               = entry_result["mode"]
        state["score_at_entry"]     = entry_result["score"]
        state["iv_at_entry"]        = entry_result["greeks"].get("iv_pct", 0)
        state["regime_at_entry"]    = entry_result.get("regime", "UNKNOWN")
        state["dte_at_entry"]       = dte
        state["strike"]             = D.resolve_atm_strike(
            D.get_ltp(D.NIFTY_SPOT_TOKEN), D.get_active_strike_step(dte))
        state["expiry"]             = expiry.isoformat() if expiry else ""
        state["candles_held"]       = 0
        state["_last_trail_candle"] = ""
        state["_rsi_was_overbought"] = False
        state["daily_trades"]      += 1

    D.subscribe_tokens([token])
    _save_state()

    _alert_entry(
        symbol, option_type, actual_price,
        entry_result["mode"], entry_result["score"], profile,
        entry_result["details_1m"],
        entry_result.get("details_3m", {}),
        entry_result["greeks"], dte,
        entry_result.get("regime", "UNKNOWN"),
        score_breakdown = entry_result.get("score_breakdown", {}),
        prediction      = entry_result.get("prediction", {}),
        spread_1m       = entry_result.get("spread_1m", 0.0),
        session         = session,
    )

    logger.info(
        "[MAIN] ENTRY " + option_type + " " + symbol
        + " price=" + str(actual_price)
        + " score=" + str(entry_result["score"])
        + " SL=" + str(phase1_sl)
    )

def _execute_exit(kite, option_ltp: float, reason: str):
    if state.get("_exit_failed"):
        logger.warning("[MAIN] Exit suppressed — previous CRITICAL failure unresolved")
        return

    with _state_lock:
        symbol    = state["symbol"]
        token     = state["token"]
        qty       = state["qty"]
        direction = state["direction"]
        entry     = state["entry_price"]
        peak      = state["peak_pnl"]
        mode      = state["mode"]
        candles   = state.get("candles_held", 0)
        regime    = state.get("regime_at_entry", "")
        score     = state.get("score_at_entry", 0)

    fill = place_exit(kite, symbol, token, direction,
                      qty, option_ltp, reason)

    if not fill["ok"] and fill["error"] == "EXIT_FAILED_MANUAL_REQUIRED":
        with _state_lock:
            state["_exit_failed"] = True
        _alert_exit_critical(symbol, qty)
        return

    actual_exit = fill["fill_price"] if fill["ok"] else option_ltp
    pnl         = round(actual_exit - entry, 2)

    _log_trade(state, actual_exit, reason, candles)

    with _state_lock:
        state["daily_pnl"] = round(state.get("daily_pnl", 0) + pnl, 2)
        if pnl < 0:
            state["daily_losses"]       = state.get("daily_losses", 0) + 1
            state["consecutive_losses"] = state.get("consecutive_losses", 0) + 1
        else:
            state["consecutive_losses"] = 0

        daily_pnl    = state["daily_pnl"]
        daily_trades = state.get("daily_trades", 0)
        daily_losses = state.get("daily_losses", 0)
        daily_wins   = daily_trades - daily_losses
        state["last_exit_time"] = datetime.now().isoformat()
        old_token = state["token"]

        state.update({
            "in_trade"            : False,
            "symbol"              : "",
            "token"               : None,
            "direction"           : "",
            "entry_price"         : 0.0,
            "entry_time"          : "",
            "exit_phase"          : 1,
            "phase1_sl"           : 0.0,
            "phase2_sl"           : 0.0,
            "trail_tightened"     : False,
            "peak_pnl"            : 0.0,
            "mode"                : "",
            "iv_at_entry"         : 0.0,
            "score_at_entry"      : 0,
            "regime_at_entry"     : "",
            "candles_held"        : 0,
            "_last_trail_candle"  : "",
            "_rsi_was_overbought" : False,
            "force_exit"          : False,
            "_exit_failed"        : False,
        })

    if old_token:
        D.unsubscribe_tokens([old_token])

    _save_state()
    _alert_exit(symbol, entry, actual_exit, pnl, reason, mode, peak,
                candles_held=candles, regime=regime, score=score,
                daily_pnl=daily_pnl, daily_trades=daily_trades,
                daily_wins=daily_wins, daily_losses=daily_losses)

    logger.info("[MAIN] EXIT " + symbol
                + " price=" + str(actual_exit)
                + " pnl=" + str(pnl) + "pts"
                + " reason=" + reason)

# ═══════════════════════════════════════════════════════════════
#  CANDLE BOUNDARY
# ═══════════════════════════════════════════════════════════════

def _is_new_1min_candle(now: datetime) -> bool:
    key = now.strftime("%Y%m%d%H%M")
    with _state_lock:
        if state.get("_last_1min_candle") != key and 31 <= now.second <= 36:
            state["_last_1min_candle"] = key
            return True
    return False

# ═══════════════════════════════════════════════════════════════
#  STRATEGY LOOP
# ═══════════════════════════════════════════════════════════════

def _strategy_loop(kite):
    global _running
    today_str = date.today().isoformat()
    logger.info("[MAIN] Strategy loop started")
    with _state_lock:
        state["_last_1min_candle"] = ""

    expiry = D.get_nearest_expiry(kite)
    if expiry:
        logger.info("[MAIN] Expiry on startup: " + str(expiry))
    else:
        logger.warning("[MAIN] Expiry not resolved on startup — will retry in loop")

    while _running:
        try:
            now   = datetime.now()
            today = date.today()

            if today.isoformat() != today_str:
                today_str = today.isoformat()
                _reset_daily(today_str)
                expiry = D.get_nearest_expiry(kite)
                if not expiry:
                    for _retry in range(5):
                        _wait = 2 ** (_retry + 1)
                        logger.warning('[MAIN] Expiry resolve failed, retry '
                                       + str(_retry + 1) + ' in ' + str(_wait) + 's')
                        time.sleep(_wait)
                        expiry = D.get_nearest_expiry(kite)
                        if expiry:
                            break
                if not expiry:
                    logger.critical('[MAIN] Cannot resolve expiry after 5 retries')
                    _tg_send('\U0001f6a8 <b>CRITICAL: Expiry resolution failed. Bot paused.</b>\nUse /resume after market opens.')
                    with _state_lock:
                        state['paused'] = True
                    time.sleep(60)
                    continue
            dte     = D.calculate_dte(expiry) if expiry else 0
            profile = D.get_dte_profile(dte)
            session = D.get_session_block(now.hour, now.minute)
            spot_ltp = D.get_ltp(D.NIFTY_SPOT_TOKEN)

            # v12.9 FIX: _error_count reset moved AFTER successful scan
            # (was here at top = circuit breaker never fired)

            if check_profit_lock(state, state.get("daily_pnl", 0)):
                _alert_profit_lock(state["daily_pnl"])
                _save_state()

            if (now.hour == 15 and now.minute == 35
                    and not state.get("_eod_reported")
                    and now.second < 30):
                state["_eod_reported"] = True
                try:
                    _generate_eod_report()
                except Exception as e:
                    logger.error("[MAIN] EOD report error: " + str(e))

            if state.get("force_exit") and state.get("in_trade"):
                option_ltp = D.get_ltp(state.get("token"))
                _execute_exit(kite, option_ltp or state["entry_price"], "FORCE_EXIT")
                time.sleep(1)
                continue

            if state.get("in_trade"):
                option_ltp = D.get_ltp(state.get("token"))
                if option_ltp <= 0 and kite is not None:
                    try:
                        q = kite.ltp(["NFO:" + state["symbol"]])
                        option_ltp = float(q["NFO:" + state["symbol"]]["last_price"])
                        logger.info("[MAIN] Option LTP via REST: " + str(option_ltp))
                    except Exception as e:
                        logger.warning("[MAIN] REST option LTP failed: " + str(e))
                if option_ltp > 0:
                    with _state_lock:
                        cur_1m = now.strftime("%H:%M")
                        if cur_1m != state.get("_last_candle_held_min", ""):
                            state["_last_candle_held_min"] = cur_1m
                            state["candles_held"] = state.get("candles_held", 0) + 1

                    should_exit, reason, _ = manage_exit(state, option_ltp, profile)

                    if now.hour == 15 and now.minute >= 28:
                        should_exit = True
                        reason      = "MARKET_CLOSE"

                    if should_exit:
                        _execute_exit(kite, option_ltp, reason)
                    else:
                        entry    = state.get("entry_price", 0)
                        pnl      = round(option_ltp - entry, 1)
                        last_ms  = state.get("_last_milestone", 0)
                        milestone= (int(pnl) // 10) * 10
                        if milestone > last_ms and milestone > 0:
                            with _state_lock:
                                state["_last_milestone"] = milestone
                            rs = "₹" + str(round(milestone * D.LOT_SIZE))
                            _tg_send(
                                "🔥 <b>+" + str(milestone) + "pts</b>  " + rs + "\n"
                                + state.get("symbol","") + "  |  "
                                + "Phase " + str(state.get("exit_phase",1))
                                + "  RSI check on /edge"
                            )
                        _save_state()

                time.sleep(0.5)
                continue

            # ── NO RE‑ENTRY WATCHING — removed ──────────────────

            # v12.13: Feed spot buffer for consolidation detection
            if spot_ltp > 0:
                D.update_spot_buffer({
                    "timestamp": now.isoformat(),
                    "open": spot_ltp, "high": spot_ltp,
                    "low": spot_ltp, "close": spot_ltp,
                })

            if (not state.get("paused")
                    and D.is_trading_window(now)
                    and _is_new_1min_candle(now)
                    and spot_ltp > 0
                    and expiry is not None):

                # v12.13: Feed proper 1-min candle to spot buffer
                try:
                    _spot_df = D.get_historical_data(D.NIFTY_SPOT_TOKEN, "minute", 5)
                    if not _spot_df.empty and len(_spot_df) >= 2:
                        _last_spot = _spot_df.iloc[-2]
                        D.update_spot_buffer({
                            "timestamp": str(_spot_df.index[-2]),
                            "open": float(_last_spot["open"]),
                            "high": float(_last_spot["high"]),
                            "low": float(_last_spot["low"]),
                            "close": float(_last_spot["close"]),
                        })
                except Exception:
                    pass

                step       = D.get_active_strike_step(dte)
                atm_strike = D.resolve_atm_strike(spot_ltp, step)
                tokens     = D.get_option_tokens(kite, atm_strike, expiry)

                if not tokens:
                    logger.warning("[MAIN] No tokens resolved for ATM=" + str(atm_strike))
                    time.sleep(2)
                    continue

                # v12.13: EXPIRY BREAKOUT MODE (DTE=0)
                if dte == 0:
                    try:
                        eb_result = check_expiry_breakout(
                            kite, spot_ltp, atm_strike, expiry, session)
                        if eb_result.get("fired"):
                            # Use the token/strike/symbol from breakout result
                            eb_token  = eb_result.get("token")
                            eb_type   = eb_result.get("direction", "CE")
                            eb_strike = eb_result.get("strike", atm_strike)
                            eb_symbol = eb_result.get("symbol", "")

                            # Get or create opt_info
                            eb_tokens = D.get_option_tokens(kite, eb_strike, expiry)
                            eb_opt_info = eb_tokens.get(eb_type) if eb_tokens else None

                            if eb_opt_info:
                                # Pre-entry checks (cooldown, margin, etc)
                                option_ltp_now = D.get_ltp(eb_opt_info["token"])
                                ok, reason = pre_entry_checks(
                                    kite, eb_opt_info["token"], state,
                                    option_ltp_now, profile, session)
                                if ok:
                                    _execute_entry(kite, eb_opt_info, eb_type,
                                                   eb_result, profile, expiry, dte, session)
                                    continue
                                else:
                                    logger.info("[MAIN] Expiry breakout blocked: " + reason)
                    except Exception as e:
                        logger.warning("[MAIN] Expiry breakout error: " + str(e))

                # ── NORMAL CONVICTION SCAN (all DTE) ─────────────
                best_result   = None
                best_type     = None
                best_opt_info = None
                all_results   = {}

                if not D.is_tick_live(D.INDIA_VIX_TOKEN):
                    D.subscribe_tokens([D.INDIA_VIX_TOKEN])

                for opt_type in ("CE", "PE"):
                    opt_info = tokens.get(opt_type)
                    if not opt_info:
                        continue

                    D.subscribe_tokens([opt_info["token"]])
                    time.sleep(0.3)

                    result = check_entry(
                        token       = opt_info["token"],
                        option_type = opt_type,
                        profile     = profile,
                        spot_ltp    = spot_ltp,
                        strike      = atm_strike,
                        expiry_date = expiry,
                        session     = session,
                    )

                    all_results[opt_type] = result

                    if not result["fired"]:
                        D.unsubscribe_tokens([opt_info["token"]])
                        continue

                    logger.info("[MAIN] Signal passed gate — type=" + opt_type + " score=" + str(result["score"]))
                    if not loss_streak_gate(state, result["score"]):
                        _alert_loss_streak_gate(
                            state.get("consecutive_losses", 0),
                            result["score"],
                            D.LOSS_STREAK_GATE_SCORE
                        )
                        D.unsubscribe_tokens([opt_info["token"]])
                        continue

                    option_ltp_now = D.get_ltp(opt_info["token"])
                    if option_ltp_now <= 0:
                        try:
                            q = kite.ltp(["NFO:" + opt_info["symbol"]])
                            option_ltp_now = float(list(q.values())[0]["last_price"])
                            logger.info("[MAIN] LTP via REST: " + str(option_ltp_now))
                        except Exception as _e:
                            logger.warning("[MAIN] REST LTP failed: " + str(_e))
                    ok, reason = pre_entry_checks(
                        kite, opt_info["token"], state,
                        option_ltp_now, profile, session
                    )
                    if not ok:
                        logger.info("[MAIN] Entry blocked (" + opt_type + "): " + reason)
                        D.unsubscribe_tokens([opt_info["token"]])
                        continue

                    if best_result is None or result["score"] > best_result["score"]:
                        if best_opt_info:
                            D.unsubscribe_tokens([best_opt_info["token"]])
                        best_result   = result
                        best_type     = opt_type
                        best_opt_info = opt_info

                try:
                    vix_ltp = D.get_vix()
                except Exception:
                    vix_ltp = 0.0

                def _scan_summary(res):
                    if not res:
                        return {"score":0,"fired":False,"mode":"","regime":"—",
                                "d1":{},"entry":0.0,"spread_1m":0.0}
                    return {
                        "score" : res.get("score", 0),
                        "fired" : res.get("fired", False),
                        "mode"  : res.get("mode", ""),
                        "regime": res.get("regime", "—"),
                        "d1"    : res.get("details_1m", {}),
                        "entry" : res.get("entry_price", 0.0),
                        "spread_1m": res.get("spread_1m", 0.0),
                    }

                # Always save scan regardless of gate result
                ce_res = all_results.get("CE", {})
                pe_res = all_results.get("PE", {})
                with _state_lock:
                    state["_last_scan"] = {
                        "time"      : now.strftime("%H:%M:%S"),
                        "session"   : session,
                        "regime"    : (best_result.get("regime", "—") if best_result
                                       else ce_res.get("regime",
                                            pe_res.get("regime", "—"))),
                        "vix"       : round(vix_ltp, 2),
                        "dte"       : dte,
                        "atm"       : atm_strike,
                        "fired"     : best_result["mode"] if best_result else "No",
                        "fired_type": best_type or "—",
                        "ce"        : _scan_summary(ce_res),
                        "pe"        : _scan_summary(pe_res),
                    }

                if best_result and best_opt_info:
                    _execute_entry(kite, best_opt_info, best_type,
                                   best_result, profile, expiry, dte, session)

            # v12.9: Reset error count only after a successful loop iteration
            if state.get("_error_count", 0) > 0:
                with _state_lock:
                    state["_error_count"] = 0

        except Exception as e:
            logger.error("[MAIN] Loop error: " + str(e))
            with _state_lock:
                state["_error_count"] = state.get("_error_count", 0) + 1
                if state["_error_count"] >= 5 and not state.get("_circuit_breaker"):
                    if state.get('in_trade'):
                        try:
                            cb_ltp = D.get_ltp(state.get('token', 0))
                            if cb_ltp > 0:
                                _execute_exit(kite, cb_ltp, 'CIRCUIT_BREAKER_EXIT')
                                logger.warning('[MAIN] Circuit breaker: emergency exit executed')
                            else:
                                logger.critical('[MAIN] Circuit breaker: LTP=0, manual exit required')
                        except Exception as cb_e:
                            logger.critical('[MAIN] Circuit breaker exit failed: ' + str(cb_e))
                    state["_circuit_breaker"] = True
                    state["paused"]            = True
                    logger.critical("[MAIN] ⚡ CIRCUIT BREAKER — "
                                    + str(state["_error_count"]) + " errors")
                    _tg_send("⚡ <b>CIRCUIT BREAKER</b>\n"
                             + str(state["_error_count"]) + " consecutive errors.\n"
                             + "Bot paused. /resume to restart.\n"
                             + "Error: " + str(e)[:100])
            time.sleep(2)

        time.sleep(1)

# ═══════════════════════════════════════════════════════════════
#  FILE BROWSER
# ═══════════════════════════════════════════════════════════════

_RESEARCH_DIR = os.path.expanduser("~/research")

_BROWSER_ROOTS = {
    "logs_live"   : D.LIVE_LOG_DIR,
    "logs_lab"    : D.LAB_LOG_DIR,
    "lab_options" : D.OPTIONS_3MIN_DIR,
    "lab_spot"    : D.SPOT_DIR,
    "lab_reports" : D.REPORTS_DIR,
    "lab_sessions": D.SESSIONS_DIR,
    "state"       : D.STATE_DIR,
    "backups"     : D.BACKUP_DIR,
    "trade_log"   : os.path.dirname(D.TRADE_LOG_PATH),
    "research"    : _RESEARCH_DIR,
}

_BROWSER_LABELS = {
    "logs_live"   : "📋 Live Logs",
    "logs_lab"    : "🔬 Lab Logs",
    "lab_options" : "📊 Options 3-Min",
    "lab_spot"    : "📈 Spot CSVs",
    "lab_reports" : "📑 Reports",
    "lab_sessions": "🗂 Sessions",
    "state"       : "⚙️ State Files",
    "backups"     : "💾 Backups",
    "trade_log"   : "📒 Trade Log",
    "research"    : "🔭 Research Data",
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
        filename  = parts[2]
        file_path = os.path.join(folder_path, filename)
        if os.path.isfile(file_path):
            size_kb = round(os.path.getsize(file_path) / 1024, 1)
            _tg_send_file(file_path, caption=filename + " (" + str(size_kb) + " KB)")
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

def _send_today_download():
    today_str = date.today().strftime("%Y%m%d")
    zip_path  = os.path.join(D.STATE_DIR, "today_" + today_str + ".zip")
    files_to_zip = []

    for fname in os.listdir(D.LIVE_LOG_DIR):
        if today_str in fname or fname == "vrl_live.log":
            files_to_zip.append((os.path.join(D.LIVE_LOG_DIR, fname), fname))

    if os.path.isfile(D.TRADE_LOG_PATH):
        files_to_zip.append((D.TRADE_LOG_PATH, "vrl_trade_log.csv"))

    opt_csv = os.path.join(D.OPTIONS_3MIN_DIR, "nifty_option_3min_" + today_str + ".csv")
    if os.path.isfile(opt_csv):
        files_to_zip.append((opt_csv, "nifty_option_3min_" + today_str + ".csv"))

    opt_1m = os.path.join(D.OPTIONS_1MIN_DIR, "nifty_option_1min_" + today_str + ".csv")
    if os.path.isfile(opt_1m):
        files_to_zip.append((opt_1m, "nifty_option_1min_" + today_str + ".csv"))

    if os.path.isfile(D.STATE_FILE_PATH):
        files_to_zip.append((D.STATE_FILE_PATH, "vrl_live_state.json"))

    if not files_to_zip:
        _tg_send("No files found for today.")
        return

    try:
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for fpath, arcname in files_to_zip:
                zf.write(fpath, arcname)
        size_mb = round(os.path.getsize(zip_path) / (1024 * 1024), 2)
        _tg_send_file(zip_path, caption="Today's data — " + today_str
                      + " (" + str(size_mb) + " MB)")
    except Exception as e:
        _tg_send("Today zip error: " + str(e))

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
        "<b>MARKET</b>\n"
        "/edge      — War Room (CE/PE/spot)\n"
        "/spot      — Spot trend + gap + regime\n"
        "/regime    — Regime + detection mode\n"
        "/align     — Indicator alignment check\n"
        "/pivot     — Fib pivot levels + zones\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "<b>TRADING</b>\n"
        "/status    — trade status + PNL\n"
        "/pnl       — today's P&L summary\n"
        "/trades    — today's trade list\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "<b>FILES</b>\n"
        "/files     — browse folders\n"
        "/download  — today's zip\n"
        "/source    — download all source code\n"
        "/health    — system health check\n"
        "/livecheck — last 50 log lines\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "<b>CONTROL</b>\n"
        "/pause     — block new entries\n"
        "/resume    — re-enable entries\n"
        "/forceexit — emergency exit\n"
        "/restart   — restart bot\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "Mode: " + ("📄 PAPER" if D.PAPER_MODE else "💰 LIVE") + " | ATM: 100-step"
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
    sl_key  = "phase1_sl" if phase == 1 else "phase2_sl"
    sl_val  = st.get(sl_key, 0)
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
    _tg_send(
        "💰 <b>TODAY P&amp;L</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "PNL    : " + sign + str(round(pnl, 1)) + "pts  " + _rs(pnl) + "\n"
        "Trades : " + str(st.get("daily_trades", 0)) + "/" + str(D.MAX_DAILY_TRADES) + "\n"
        "Losses : " + str(st.get("daily_losses", 0)) + "/" + str(D.MAX_DAILY_LOSSES) + "\n"
        "Wins   : " + str(st.get("daily_trades", 0) - st.get("daily_losses", 0)) + "\n"
        "Streak : " + str(streak) + (" 🔴 carries to tomorrow" if streak >= 2 else "") + "\n"
        "P-Lock : " + ("YES 🔒" if st.get("profit_locked") else "No")
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
def _cmd_download(args): _send_today_download()

def _cmd_slippage(args):
    try:
        import importlib, os
        slippage_log = os.path.join(os.path.expanduser("~"),
                                    "lab_data", "vrl_slippage_log.csv")
        if not os.path.isfile(slippage_log):
            _tg_send("📊 <b>SLIPPAGE</b>\nNo live fills recorded yet.\n"
                     "Slippage is only tracked in live mode (VRL_TRADE_LIVE.py).")
            return

        import pandas as pd
        df = pd.read_csv(slippage_log)
        if df.empty:
            _tg_send("📊 <b>SLIPPAGE</b>\nLog exists but no fills yet.")
            return

        avg_slip  = round(df["slippage_pts"].abs().mean(), 2)
        max_slip  = round(df["slippage_pts"].abs().max(), 2)
        entries   = df[df["order_type"] == "ENTRY"]
        exits     = df[df["order_type"].str.startswith("EXIT", na=False)]

        _tg_send(
            "📊 <b>SLIPPAGE SUMMARY</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "Total fills   : " + str(len(df)) + "\n"
            "Avg slip      : " + str(avg_slip) + "pts\n"
            "Max slip      : " + str(max_slip) + "pts\n"
            "Entry avg     : " + str(round(entries["slippage_pts"].abs().mean(), 2) if not entries.empty else 0) + "pts\n"
            "Exit avg      : " + str(round(exits["slippage_pts"].abs().mean(), 2) if not exits.empty else 0) + "pts\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "File: lab_data/vrl_slippage_log.csv"
        )
    except Exception as e:
        _tg_send("Slippage: " + str(e))

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
        _tg_send("📂 No research data yet.\nRun: python3 research_strikes.py\nor: python3 research_enhanced.py")
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
#  v12.13: PIVOT COMMAND
# ═══════════════════════════════════════════════════════════════

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
    "/trades"      : _cmd_trades,
    "/files"       : _cmd_files,
    "/download"    : _cmd_download,
    "/health"      : _cmd_health,
    "/pause"       : _cmd_pause,
    "/reset_exit"  : _cmd_reset_exit,
    "/resume"      : _cmd_resume,
    "/forceexit"   : _cmd_forceexit,
    "/restart"     : _cmd_restart,
    "/livecheck"   : _cmd_livecheck,
    "/source"      : _cmd_source
}

def _tg_get_updates(offset: int) -> list:
    url = _TG_BASE + D.TELEGRAM_TOKEN + "/getUpdates"
    try:
        resp = requests.get(url, params={"offset": offset, "timeout": 20}, timeout=30)
        if resp.ok:
            return resp.json().get("result", [])
    except Exception as e:
        logger.warning("[CTRL] getUpdates error: " + str(e))
    return []

def _tg_authorized(message: dict) -> bool:
    return str(message.get("chat", {}).get("id", "")) == str(D.TELEGRAM_CHAT_ID)

def _tg_handle_message(message: dict):
    if not _tg_authorized(message):
        return
    text = message.get("text", "").strip()
    if not text.startswith("/"):
        return
    parts   = text.split()
    raw_cmd = parts[0].split("@")[0].lower()
    args    = parts[1:] if len(parts) > 1 else []
    handler = _DISPATCH.get(raw_cmd)
    if handler:
        handler(args)
    else:
        _tg_send("Unknown command: " + raw_cmd + "\nType /help")

def _tg_handle_callback(callback: dict):
    query_id = callback.get("id", "")
    data     = callback.get("data", "")
    if data.startswith("FB:"):
        _handle_file_browser_callback(data, query_id)
    elif data.startswith("DL:"):
        _handle_download_callback(data, query_id)
    else:
        _tg_answer_callback(query_id, "Unknown action")

def _tg_poll_loop():
    global _tg_offset, _tg_last_update_id
    logger.info("[CTRL] Telegram listener started " + D.VERSION)
    while _tg_running:
        updates = _tg_get_updates(_tg_offset)
        for upd in updates:
            uid          = upd["update_id"]
            _tg_offset   = uid + 1
            if uid <= _tg_last_update_id:
                continue
            _tg_last_update_id = uid
            try:
                if "message" in upd:
                    _tg_handle_message(upd["message"])
                elif "callback_query" in upd:
                    _tg_handle_callback(upd["callback_query"])
            except Exception as e:
                logger.error("[CTRL] Update error: " + str(e))
        time.sleep(1)

def _start_telegram_listener():
    global _tg_running, _tg_offset
    _tg_running = True

    try:
        url  = _TG_BASE + D.TELEGRAM_TOKEN + "/getUpdates"
        resp = requests.get(url, params={"offset": -1, "timeout": 1}, timeout=5)
        if resp.ok:
            updates = resp.json().get("result", [])
            if updates:
                _tg_offset = updates[-1]["update_id"] + 1
                logger.info("[CTRL] Discarded " + str(len(updates))
                            + " pending updates on startup")
    except Exception:
        pass

    thread = threading.Thread(target=_tg_poll_loop, name="TGListener", daemon=True)
    thread.start()
    logger.info("[CTRL] Listener thread launched")

def _stop_telegram_listener():
    global _tg_running
    _tg_running = False

# ═══════════════════════════════════════════════════════════════
#  SHUTDOWN
# ═══════════════════════════════════════════════════════════════

def _shutdown(signum, frame):
    global _running
    logger.info("[MAIN] Shutdown signal received")
    _running = False
    _stop_telegram_listener()
    _save_state()
    _remove_pid()
    logger.info("[MAIN] Clean shutdown")
    sys.exit(0)

# ═══════════════════════════════════════════════════════════════
#  ENTRY POINT
# ═══════════════════════════════════════════════════════════════

def main():
    global _kite
    logger.info("[MAIN] ═══ VISHAL RAJPUT TRADE " + D.VERSION + " STARTING ═══")
    logger.info("[MAIN] Mode: " + ("PAPER" if D.PAPER_MODE else "LIVE"))
    logger.info("[MAIN] Scalps: DISABLED (data-backed decision)")

    _write_pid()
    signal.signal(signal.SIGINT,  _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    kite = get_kite()
    _kite = kite
    D.init(kite)

    live_lot_size = D.get_lot_size(kite)
    D.LOT_SIZE    = live_lot_size
    logger.info("[MAIN] Lot size from broker: " + str(live_lot_size))

    _load_state()

    try:
        import csv as _csv
        today_iso = date.today().isoformat()
        trades_today = []

        for log_path in [D.TRADE_LOG_PATH,
                         os.path.join(D.LAB_DIR, "vrl_trade_log.csv")]:
            if not os.path.isfile(log_path):
                continue
            try:
                with open(log_path) as f:
                    raw_rows = list(_csv.DictReader(f))

                found = []
                for r in raw_rows:
                    if r.get("date", "").strip() == today_iso:
                        found.append(r)
                    elif r.get("trade_id", "").strip() == today_iso:
                        found.append({
                            **r,
                            "date"   : r.get("trade_id", ""),
                            "pnl_pts": r.get("pnl_points", r.get("pnl_pts", "0")),
                        })
                if found:
                    trades_today = found
                    break
            except Exception:
                continue

        if trades_today:
            def _get_pnl(row):
                for k in ["pnl_pts", "pnl_points", "pnl_rs", "pnl"]:
                    if k in row:
                        try: return float(row[k])
                        except: pass
                return 0.0

            wins   = [t for t in trades_today if _get_pnl(t) > 0]
            losses = [t for t in trades_today if _get_pnl(t) < 0]
            pnl    = sum(_get_pnl(t) for t in trades_today)

            with _state_lock:
                state["daily_trades"]       = len(trades_today)
                state["daily_losses"]       = len(losses)
                state["daily_pnl"]          = round(pnl, 2)
                # v12.7 fix: count streak from tail — not total losses
                # e.g. W L L W → streak=0, not 2
                streak = 0
                for t in reversed(trades_today):
                    if _get_pnl(t) < 0:
                        streak += 1
                    else:
                        break
                state["consecutive_losses"] = streak

            logger.info("[MAIN] Restored: " + str(len(trades_today))
                        + " trades | " + str(len(losses)) + " losses | pnl="
                        + str(round(pnl,1)) + "pts")
        else:
            logger.info("[MAIN] No trades found for today — starting fresh")
    except Exception as e:
        logger.warning("[MAIN] Trade log restore failed: " + str(e))

    D.start_websocket()
    D.subscribe_tokens([D.NIFTY_SPOT_TOKEN, D.INDIA_VIX_TOKEN])
    time.sleep(2)

    # v12.11: Calculate spot gap on startup
    try:
        gap_info = D.calculate_spot_gap()
        if gap_info["gap_pts"] != 0:
            logger.info("[MAIN] Spot gap: " + str(gap_info["gap_pts"]) + "pts"
                        + " (" + str(gap_info["gap_pct"]) + "%)")
    except Exception as e:
        logger.warning("[MAIN] Gap calculation failed: " + str(e))

    # v12.13: Calculate fib pivot points
    try:
        pivots = D.calculate_fib_pivots()
        if pivots:
            logger.info("[MAIN] Fib pivots loaded: P=" + str(pivots.get("pivot", 0))
                        + " R1=" + str(pivots.get("R1", 0))
                        + " S1=" + str(pivots.get("S1", 0)))
    except Exception as e:
        logger.warning("[MAIN] Fib pivot calc failed: " + str(e))

    start_lab(kite)
    _start_telegram_listener()
    _alert_bot_started()

    logger.info("[MAIN] All systems ready. Strategy loop starting.")
    _strategy_loop(kite)

if __name__ == "__main__":
    main()
