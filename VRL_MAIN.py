# ═══════════════════════════════════════════════════════════════
#  VRL_MAIN.py — VISHAL RAJPUT TRADE v12.15
#  Master orchestration file.
#  v12.15: Expiry breakout mode, fib pivots, /pivot command,
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
    "trough_pnl"         : 0.0,
    "session_at_entry"   : "",
    "spread_1m_at_entry" : 0.0,
    "spread_3m_at_entry" : 0.0,
    "delta_at_entry"     : 0.0,
    "sl_pts_at_entry"    : 0.0,
    "_bias_done"         : False,
    "_straddle_done"     : False,
    "_hourly_rsi_ts"     : 0,
    "_vix_warned"        : False,
    "_straddle_alerted"  : False,
}

state   = deepcopy(DEFAULT_STATE)
_running = True

# ═══════════════════════════════════════════════════════════════
#  STATE PERSISTENCE
# ═══════════════════════════════════════════════════════════════

def _save_state():
    try:
        persist_fields = D.STATE_PERSIST_FIELDS + ["_rsi_was_overbought"]
        with _state_lock:
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
        state["_bias_done"]            = False
        state["_straddle_done"]        = False
        state["_hourly_rsi_ts"]        = 0
        state["_vix_warned"]           = False
        state["_straddle_alerted"]     = False
    D.clear_token_cache()
    D.reset_daily_warnings()
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
    "peak_pnl", "trough_pnl", "exit_reason", "exit_phase",
    "score", "iv_at_entry", "regime", "dte", "candles_held",
    "session", "strike", "sl_pts",
    "spread_1m", "spread_3m", "delta_at_entry",
    "bias", "vix_at_entry", "hourly_rsi",
    "straddle_decay",
]

def _cleanup_trade_log():
    """One-time cleanup: remove corrupted rows where date doesn't match YYYY-MM-DD."""
    path = D.TRADE_LOG_PATH
    if not os.path.isfile(path):
        return
    try:
        import re
        date_re = re.compile(r"^\d{4}-\d{2}-\d{2}$")
        with open(path, "r") as f:
            reader = csv.DictReader(f)
            if not reader.fieldnames:
                return
            good_rows = [r for r in reader if date_re.match(r.get("date", ""))]
        # Rewrite with correct header + good rows only
        with open(path, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=TRADE_FIELDNAMES, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(good_rows)
        logger.info("[MAIN] Trade log cleaned: " + str(len(good_rows)) + " valid rows kept")
    except Exception as e:
        logger.warning("[MAIN] Trade log cleanup error: " + str(e))

def _log_trade(st: dict, exit_price: float, exit_reason: str,
               candles_held: int = 0):
    os.makedirs(os.path.dirname(D.TRADE_LOG_PATH), exist_ok=True)
    is_new  = not os.path.isfile(D.TRADE_LOG_PATH)
    entry   = st.get("entry_price", 0)
    pnl_pts = round(exit_price - entry, 2)
    pnl_rs  = round(pnl_pts * D.LOT_SIZE, 2)

    row = {
        "date"          : date.today().isoformat(),
        "entry_time"    : st.get("entry_time", ""),
        "exit_time"     : datetime.now().strftime("%H:%M:%S"),
        "symbol"        : st.get("symbol", ""),
        "direction"     : st.get("direction", ""),
        "mode"          : st.get("mode", ""),
        "entry_price"   : entry,
        "exit_price"    : round(exit_price, 2),
        "pnl_pts"       : pnl_pts,
        "pnl_rs"        : pnl_rs,
        "peak_pnl"      : round(st.get("peak_pnl", 0), 2),
        "trough_pnl"    : round(st.get("trough_pnl", 0), 2),
        "exit_reason"   : exit_reason,
        "exit_phase"    : st.get("exit_phase", 1),
        "score"         : st.get("score_at_entry", 0),
        "iv_at_entry"   : st.get("iv_at_entry", 0),
        "regime"        : st.get("regime_at_entry", ""),
        "dte"           : st.get("dte_at_entry", 0),
        "candles_held"  : candles_held,
        "session"       : st.get("session_at_entry", ""),
        "strike"        : st.get("strike", 0),
        "sl_pts"        : st.get("sl_pts_at_entry", 0),
        "spread_1m"     : st.get("spread_1m_at_entry", 0),
        "spread_3m"     : st.get("spread_3m_at_entry", 0),
        "delta_at_entry": st.get("delta_at_entry", 0),
        "bias": D.get_daily_bias(),
        "vix_at_entry": round(D.get_vix(), 1),
        "hourly_rsi": D.get_hourly_rsi(),
        "straddle_decay": 0.0,
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
        logger.error("[TG] send error: " + type(e).__name__)
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
        logger.error("[TG] send_file error: " + type(e).__name__)
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
        logger.error("[TG] keyboard error: " + type(e).__name__)
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
        "STRATEGY (v12.15)\n"
        "Regime : Spot ADX+spread scoring (CHOPPY blocked)\n"
        "Gate   : 2/4 + spot override bypass\n"
        "RSI    : 30-50 (58 in strong trend)\n"
        "Dip    : 1m RSI must be below 3m RSI\n"
        "Strike : CE ITM/ATM, PE ITM/ATM (direction-aware)\n"
        "Score  : ≥5 to fire | ≥6 against bias/streak\n"
        "Trail  : Profit floors + adaptive 5m→3m→1m EMA\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "P-Lock : +" + str(D.PROFIT_LOCK_PTS) + "pts\n"
        "/help for commands."
    )

# ── Score breakdown formatter ──────────────────────────────────
def _fmt_score_breakdown(bd: dict, score: int) -> str:
    if not bd:
        return "Score   : " + str(score) + "/8\n"
    parts = []
    if bd.get("body"):         parts.append("Body")
    if bd.get("body_bonus"):   parts.append("+Bonus")
    if bd.get("rsi"):          parts.append("RSI")
    if bd.get("volume"):       parts.append("Vol")
    if bd.get("delta"):        parts.append("Delta")
    if bd.get("double_align"): parts.append("2xAlign")
    if bd.get("gate_bonus"):   parts.append("Gate")
    if bd.get("multi_tf_adx"): parts.append("MTF-ADX")
    return "Score   : " + str(score) + "/8  [" + " ".join(parts) + "]\n"

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

    _bias = D.get_daily_bias() if hasattr(D, "get_daily_bias") else ""
    _vix = round(D.get_vix(), 1)
    _tg_send(
        "🔵 <b>" + option_type + " " + str(state.get("strike", "")) + "</b>"
        + "  ₹" + str(round(entry_price, 1))
        + "  Score " + str(score) + "/" + str(D.SESSION_SCORE_MIN.get(session, 5)) + " ✅"
        + "  " + regime + "\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "🎯 <b>CONVICTION ENTRY — " + option_type + "</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        + _now_str() + "  " + symbol + "\n"
        "Entry   : ₹" + str(round(entry_price, 2)) + "\n"
        + _fmt_score_breakdown(score_breakdown, score)
        + "Regime  : " + regime + "  DTE:" + str(dte)
        + "  VIX:" + str(_vix) + "\n"
        "Bias    : " + (_bias or "—") + "  Session: " + session + "\n"
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
        + ("  DIP✅" if det_1m.get("rsi_1m_below_3m") else "")
        + "  Vol:" + str(det_1m.get("vol_ratio", 0)) + "x\n"
        "  Spread:" + str(round(spread_1m, 1)) + "pts"
        + (" ACCEL✅" if det_1m.get("spread_accel") else " DECEL⚠️")
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
        "STALE_ENTRY"         : ("Stale entry cut 🔪", "3 candles, peak under 5pts — dead trade, saved full SL"),
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
        quality = "❌ LOSS  (peak was +" + str(round(peak_pnl, 1)) + "pts  trough " + str(round(state.get("trough_pnl", 0), 1)) + "pts)"

    dpnl_sign = "+" if daily_pnl >= 0 else ""

    _ot = option_type_from_symbol(symbol)
    _wl = "WIN" if pnl >= 0 else "LOSS"
    _bias = D.get_daily_bias() if hasattr(D, "get_daily_bias") else ""
    _tg_send(
        icon + " <b>" + _wl + " " + pnl_sign + str(round(pnl,1)) + "pts</b>"
        + "  " + _rs(pnl)
        + "  |  " + _ot + " " + symbol.split("NIFTY")[-1].replace("CE","").replace("PE","").strip() + "\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        + _now_str() + "  " + symbol + "\n"
        "₹" + str(round(entry,1))
        + " → ₹" + str(round(exit_price,1)) + "\n"
        "Peak: +" + str(round(peak_pnl,1)) + "pts"
        + "  Captured: " + str(captured) + "%\n"
        "Held: " + str(candles_held) + "min"
        + "  Phase: " + str(state.get("exit_phase", 0)) + "\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "WHY EXITED\n"
        + reason_title + "\n"
        + (reason_why + "\n" if reason_why else "")
        + "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "TRADE QUALITY\n"
        + quality + "\n"
        "Regime : " + (regime or "—") + "  Score: " + str(score)
        + "  Bias: " + (_bias or "—") + "\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "📊 <b>TODAY</b>  " + dpnl_sign + str(round(daily_pnl,1)) + "pts  " + _rs(daily_pnl) + "\n"
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
        state["trough_pnl"]         = 0.0
        state["session_at_entry"]   = session
        state["spread_1m_at_entry"] = round(entry_result.get("spread_1m", 0.0), 2)
        state["spread_3m_at_entry"] = round(entry_result.get("ema_spread", 0.0), 2)
        state["delta_at_entry"]     = round(entry_result.get("greeks", {}).get("delta", 0), 3)
        state["sl_pts_at_entry"]    = round(actual_price - phase1_sl, 2)

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
        trough    = state.get("trough_pnl", 0)
        exit_phase= state.get("exit_phase", 1)

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
            "trough_pnl"          : 0.0,
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


# ═══════════════════════════════════════════════════════════════
#  DASHBOARD SNAPSHOT — written every cycle for VRL_WEB.py
#  VRL_WEB.py reads this file. Zero calculation in web server.
# ═══════════════════════════════════════════════════════════════

def _write_dashboard(spot_ltp, atm_strike, dte, vix_ltp, session,
                     profile, all_results, expiry, now,
                     dir_strikes=None):
    """Write everything the dashboard needs to a single JSON file."""
    if dir_strikes is None:
        dir_strikes = {}
    try:
        with _state_lock:
            st = dict(state)

        # ── Market context ──
        spot_3m = D.get_spot_indicators("3minute")
        spot_gap = D.get_spot_gap()
        fib_info = D.get_nearest_fib_level(spot_ltp) if spot_ltp > 0 else {}

        hourly_rsi = 0
        try:
            hourly_rsi = D.get_hourly_rsi() if hasattr(D, "get_hourly_rsi") else 0
        except Exception:
            pass

        bias = ""
        try:
            bias = D.get_daily_bias() if hasattr(D, "get_daily_bias") else ""
            if not bias:
                bias = ""
        except Exception:
            bias = ""

        straddle_open = getattr(D, "_straddle_open", 0)
        straddle_captured = getattr(D, "_straddle_captured", False)

        # ── Build CE/PE signal blocks ──
        def _build_signal(opt_type, result):
            if not result:
                return {
                    "gate_3m": {"ema": False, "body": False, "rsi": False, "price": False,
                                "met": 0, "spread": 0, "rsi_val": 0, "body_pct": 0, "mode": ""},
                    "spread_1m": 0, "spread_1m_min": D.SPREAD_1M_MIN_CE if opt_type == "CE" else D.SPREAD_1M_MIN_PE,
                    "entry_1m": {"body_pct": 0, "body_ok": False, "rsi": 0, "rsi_rising": False,
                                 "rsi_ok": False, "rsi_below_3m": False, "vol": 0, "vol_ok": False,
                                 "spread_accel": False},
                    "score": 0, "score_min": D.SESSION_SCORE_MIN.get(session, 5),
                    "fired": False, "verdict": "NO DATA",
                    "greeks": {"delta": 0, "iv": 0, "theta": 0, "gamma": 0},
                    "ltp": 0, "regime": "",
                    "strike": dir_strikes.get(opt_type, atm_strike),
                }

            d3 = result.get("details_3m", {})
            d1 = result.get("details_1m", {})
            g  = result.get("greeks", {})
            spread_1m = result.get("spread_1m", 0)
            min_spread = D.SPREAD_1M_MIN_CE if opt_type == "CE" else D.SPREAD_1M_MIN_PE
            score = result.get("score", 0)
            session_min = D.SESSION_SCORE_MIN.get(session, 5)

            # Verdict logic
            conds = d3.get("conditions_met", 0)
            regime = result.get("regime", "")
            if result.get("fired"):
                verdict = "FIRED"
            elif conds < 2:
                verdict = "3M BLOCKED " + str(conds) + "/4"
            elif regime in ("NEUTRAL", "CHOPPY"):
                verdict = "REGIME " + regime
            elif spread_1m < min_spread:
                verdict = "SPREAD " + str(round(spread_1m, 1)) + " need +" + str(min_spread)
            elif d1.get("rsi_reject"):
                rsi_v = d1.get("rsi_val", 0)
                if rsi_v > 60:
                    verdict = "RSI " + str(rsi_v) + " TOO HIGH"
                elif not d1.get("rsi_1m_below_3m", True):
                    verdict = "RSI " + str(rsi_v) + " > 3m (CHASING)"
                elif not d1.get("rsi_rising", False):
                    verdict = "RSI " + str(rsi_v) + " NOT RISING"
                else:
                    verdict = "RSI " + str(rsi_v) + " OUT OF ZONE"
            elif not d1.get("spread_accel", True):
                verdict = "SPREAD DECELERATING"
            elif not d1.get("vol_ok", False) and d1.get("vol_ratio", 0) > 0:
                verdict = "VOL " + str(d1.get("vol_ratio", 0)) + "x < 1.5x"
            elif not d1.get("body_ok") and d1.get("body_pct", 0) > 0:
                verdict = "BODY " + str(d1.get("body_pct", 0)) + "% WEAK"
            elif score < session_min:
                verdict = "SCORE " + str(score) + "/" + str(session_min)
            elif score >= session_min:
                verdict = "READY"
            else:
                verdict = "BLOCKED"

            return {
                "gate_3m": {
                    "ema": d3.get("ema_aligned", False),
                    "body": d3.get("body_ok", False),
                    "rsi": d3.get("rsi_ok", False),
                    "price": d3.get("price_ok", False),
                    "met": d3.get("conditions_met", 0),
                    "spread": round(d3.get("ema_spread_3m", 0), 1),
                    "rsi_val": round(d3.get("rsi_val_3m", 0), 1),
                    "body_pct": round(d3.get("body_pct_3m", 0), 1),
                    "mode": d3.get("mode", ""),
                    "adx": round(d3.get("adx_3m", 0), 1),
                    "candles": d3.get("candle_count_3m", 0),
                    "warm": d3.get("candle_count_3m", 0) >= 25,
                },
                "spread_1m": round(spread_1m, 1),
                "spread_1m_min": min_spread,
                "entry_1m": {
                    "body_pct": round(d1.get("body_pct", 0), 1),
                    "body_ok": d1.get("body_ok", False),
                    "rsi": round(d1.get("rsi_val", 0), 1),
                    "rsi_rising": d1.get("rsi_rising", False),
                    "rsi_ok": d1.get("rsi_ok", False),
                    "rsi_below_3m": d1.get("rsi_1m_below_3m", False),
                    "vol": round(d1.get("vol_ratio", 0), 2),
                    "vol_ok": d1.get("vol_ok", False),
                    "spread_accel": d1.get("spread_accel", False),
                },
                "score": score,
                "score_min": session_min,
                "fired": result.get("fired", False),
                "verdict": verdict,
                "greeks": {
                    "delta": round(g.get("delta", 0), 3),
                    "iv": round(g.get("iv_pct", 0), 1),
                    "theta": round(g.get("theta", 0), 2),
                    "gamma": round(g.get("gamma", 0), 4),
                },
                "ltp": round(result.get("entry_price", 0), 2),
                "regime": result.get("regime", ""),
                "strike": result.get("_strike", dir_strikes.get(opt_type, atm_strike)),
            }

        ce_signal = _build_signal("CE", all_results.get("CE"))
        pe_signal = _build_signal("PE", all_results.get("PE"))

        # ── Fix LTP=0 when gate blocks early ──
        try:
            _tokens = D.get_option_tokens(None, atm_strike, expiry)
            for _sig, _side in [(ce_signal, "CE"), (pe_signal, "PE")]:
                if _sig.get("ltp", 0) == 0 and _side in _tokens:
                    _ltp = D.get_ltp(_tokens[_side]["token"])
                    if _ltp <= 0:
                        try:
                            _sym = _tokens[_side]["symbol"]
                            _q = D._kite.ltp("NFO:" + _sym)
                            _ltp = float(list(_q.values())[0]["last_price"])
                        except Exception:
                            pass
                    if _ltp > 0:
                        _sig["ltp"] = round(_ltp, 2)
        except Exception:
            pass

        # ── Position block ──
        position = {}
        if st.get("in_trade"):
            opt_ltp = D.get_ltp(st.get("token", 0))
            entry = st.get("entry_price", 0)
            pnl = round(opt_ltp - entry, 1) if opt_ltp > 0 else 0
            sl_key = "phase1_sl" if st.get("exit_phase", 1) == 1 else "phase2_sl"
            sl = st.get(sl_key, 0)
            position = {
                "in_trade": True,
                "symbol": st.get("symbol", ""),
                "direction": st.get("direction", ""),
                "entry": entry,
                "ltp": round(opt_ltp, 2) if opt_ltp > 0 else 0,
                "pnl": pnl,
                "peak": round(st.get("peak_pnl", 0), 1),
                "trough": round(st.get("trough_pnl", 0), 1),
                "phase": st.get("exit_phase", 1),
                "sl": round(sl, 2),
                "sl_dist": round(opt_ltp - sl, 1) if opt_ltp > 0 and sl > 0 else 0,
                "score": st.get("score_at_entry", 0),
                "candles": st.get("candles_held", 0),
                "trail_tightened": st.get("trail_tightened", False),
                "rsi_overbought": st.get("_rsi_was_overbought", False),
                "mode": st.get("mode", ""),
                "regime": st.get("regime_at_entry", ""),
                "strike": st.get("strike", 0),
            }
        else:
            position = {"in_trade": False}

        # ── Today summary ──
        today_block = {
            "pnl": round(st.get("daily_pnl", 0), 1),
            "trades": st.get("daily_trades", 0),
            "wins": st.get("daily_trades", 0) - st.get("daily_losses", 0),
            "losses": st.get("daily_losses", 0),
            "streak": st.get("consecutive_losses", 0),
            "paused": st.get("paused", False),
            "profit_locked": st.get("profit_locked", False),
        }

        # ── Straddle ──
        straddle_block = {
            "open": round(straddle_open, 1) if straddle_captured else 0,
            "captured": straddle_captured,
        }

        # ── Full snapshot ──
        dashboard = {
            "ts": now.strftime("%Y-%m-%d %H:%M:%S"),
            "version": D.VERSION,
            "mode": "PAPER" if D.PAPER_MODE else "LIVE",
            "market": {
                "spot": round(spot_ltp, 1),
                "atm": atm_strike,
                "dte": dte,
                "vix": round(vix_ltp, 1),
                "session": session,
                "regime": spot_3m.get("regime", ""),
                "bias": bias,
                "gap": round(spot_gap, 1),
                "spot_ema9": spot_3m.get("ema9", 0),
                "spot_ema21": spot_3m.get("ema21", 0),
                "spot_spread": spot_3m.get("spread", 0),
                "spot_rsi": spot_3m.get("rsi", 0),
                "hourly_rsi": round(hourly_rsi, 1),
                "fib_nearest": fib_info.get("level", ""),
                "fib_price": fib_info.get("price", 0),
                "fib_distance": round(fib_info.get("distance", 0), 1),
                "fib_pivots": D.get_fib_pivots() if hasattr(D, "get_fib_pivots") else {},
                "expiry": expiry.isoformat() if expiry else "",
                "market_open": D.is_market_open(),
                "indicators_warm": now.hour >= 10 or (now.hour == 9 and now.minute >= 45 and dte >= 2),
            },
            "ce": ce_signal,
            "pe": pe_signal,
            "position": position,
            "today": today_block,
            "straddle": straddle_block,
        }

        # Atomic write
        tmp = os.path.join(D.STATE_DIR, 'vrl_dashboard.json') + ".tmp"
        with open(tmp, "w") as f:
            json.dump(dashboard, f, indent=2, default=str)
        os.replace(tmp, os.path.join(D.STATE_DIR, 'vrl_dashboard.json'))

    except Exception as e:
        logger.debug("[DASH] Snapshot write: " + str(e))


def _strategy_loop(kite):
    global _running
    today_str = date.today().isoformat()
    logger.info("[MAIN] Strategy loop started")
    # Ensure state dir exists
    os.makedirs(os.path.expanduser("~/state"), exist_ok=True)
    # One-time trade log cleanup
    _cleanup_trade_log()
    # Compute bias
    try:
        D.compute_daily_bias(kite)
        logger.info("[MAIN] Daily bias: " + str(D.get_daily_bias()))
    except Exception as _be:
        logger.debug("[MAIN] Bias: " + str(_be))
    # Compute hourly RSI
    try:
        D.check_hourly_rsi(kite)
        logger.info("[MAIN] Hourly RSI: " + str(D.get_hourly_rsi()))
    except Exception as _he:
        logger.debug("[MAIN] H.RSI: " + str(_he))
    with _state_lock:
        state["_last_1min_candle"] = ""

    expiry = D.get_nearest_expiry(kite)

    # Capture straddle if after 9:30 (must be AFTER expiry is resolved)
    try:
        _now = datetime.now()
        if expiry and _now.hour >= 9 and _now.minute >= 30:
            _ss = D.get_active_strike_step(D.calculate_dte(expiry))
            _sa = D.resolve_atm_strike(D.get_ltp(D.NIFTY_SPOT_TOKEN), _ss)
            if _sa > 0:
                D.capture_straddle(kite, _sa, expiry)
                logger.info("[MAIN] Straddle captured at startup")
    except Exception as _se:
        logger.debug("[MAIN] Straddle: " + str(_se))
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

            # v12.15: Warning system
            try:
                _wmsg, _wupd = D.run_warnings(
                    kite, state, expiry, dte, spot_ltp, now)
                if _wupd:
                    with _state_lock:
                        state.update(_wupd)
                for _wm in _wmsg:
                    _tg_send(_wm)
            except Exception as _we:
                logger.warning("[MAIN] Warnings: " + str(_we))

            # v12.9 FIX: _error_count reset moved AFTER successful scan
            # (was here at top = circuit breaker never fired)

            with _state_lock:
                _pnl_snapshot = state.get("daily_pnl", 0)
            if check_profit_lock(state, _pnl_snapshot):
                _alert_profit_lock(_pnl_snapshot)
                _save_state()

            with _state_lock:
                _eod_done = state.get("_eod_reported")
            if (now.hour == 15 and now.minute == 35
                    and not _eod_done
                    and now.second < 30):
                with _state_lock:
                    state["_eod_reported"] = True
                try:
                    _generate_eod_report()
                except Exception as e:
                    logger.error("[MAIN] EOD report error: " + str(e))
                try:
                    from VRL_LAB import generate_daily_summary
                    generate_daily_summary()
                except Exception as e:
                    logger.warning("[MAIN] Daily summary: " + str(e))

            with _state_lock:
                _force = state.get("force_exit")
                _in_trade = state.get("in_trade")
                _token = state.get("token")
                _symbol = state.get("symbol", "")
                _entry_px = state.get("entry_price", 0)
            if _force and _in_trade:
                option_ltp = D.get_ltp(_token)
                _execute_exit(kite, option_ltp or _entry_px, "FORCE_EXIT")
                time.sleep(1)
                continue

            if _in_trade:
                option_ltp = D.get_ltp(_token)
                if option_ltp <= 0 and kite is not None:
                    try:
                        q = kite.ltp(["NFO:" + _symbol])
                        option_ltp = float(q["NFO:" + _symbol]["last_price"])
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
                                _ms_phase = state.get("exit_phase", 1)
                                _ms_entry = state.get("entry_price", 0)
                                _ms_peak = state.get("peak_pnl", 0)
                                _ms_symbol = state.get("symbol", "")
                                _ms_held = state.get("candles_held", 0)
                                _ms_p1sl = state.get("phase1_sl", 0)
                                _ms_p2sl = state.get("phase2_sl", 0)
                            rs = "₹" + str(round(milestone * D.LOT_SIZE))
                            # Current SL level
                            if _ms_phase == 1:
                                _sl_price = round(_ms_p1sl, 1)
                                _sl_dist = round(option_ltp - _ms_p1sl, 1) if _ms_p1sl > 0 else 0
                                _sl_label = "Phase 1 SL"
                            elif _ms_phase == 2:
                                _sl_price = round(_ms_p2sl, 1) if _ms_p2sl > 0 else round(_ms_entry + 2, 1)
                                _sl_dist = round(option_ltp - _sl_price, 1)
                                _sl_label = "Breakeven SL"
                            else:
                                _sl_price = round(_ms_p2sl, 1) if _ms_p2sl > 0 else round(_ms_entry + 2, 1)
                                _sl_dist = round(option_ltp - _sl_price, 1)
                                _sl_label = "Trail SL"
                            _tg_send(
                                "🟢 <b>MILESTONE +" + str(milestone) + "pts</b>  " + rs + "\n"
                                "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                                + _ms_symbol + "  ₹" + str(round(_ms_entry, 1))
                                + " → ₹" + str(round(option_ltp, 1)) + "\n"
                                "P&L: +" + str(round(pnl, 1)) + "pts  " + _rs(pnl)  + "\n"
                                "Peak: +" + str(round(_ms_peak, 1))
                                + "  |  Held: " + str(_ms_held) + "min\n"
                                "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                                "🎯 " + _sl_label + ": ₹" + str(_sl_price)
                                + "  (" + str(_sl_dist) + "pts away)\n"
                                "Phase " + str(_ms_phase) + "/3"
                            )
                        _save_state()
                        # Dashboard update during trade
                        try:
                            _write_dashboard(spot_ltp, state.get("strike", 0),
                                             dte, D.get_vix(), session,
                                             profile, {}, expiry, now)
                        except Exception:
                            pass

                time.sleep(0.5)
                continue

            # ── NO RE‑ENTRY WATCHING — removed ──────────────────

            # v12.15: Feed spot buffer for consolidation detection
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

                # v12.15: Feed proper 1-min candle to spot buffer
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

                # v12.15: Direction-aware strike selection
                # CE → at/below spot (ITM), PE → at/above spot (ITM)
                dir_strikes = {}
                dir_tokens  = {}
                for _dt in ("CE", "PE"):
                    _strike = D.resolve_strike_for_direction(spot_ltp, _dt, dte)
                    dir_strikes[_dt] = _strike
                    _tk = D.get_option_tokens(kite, _strike, expiry)
                    if _tk.get(_dt):
                        dir_tokens[_dt] = _tk[_dt]
                    logger.info("[MAIN] Strike " + str(_strike) + " " + _dt
                                + " spot=" + str(round(spot_ltp, 1)))

                # Fallback to ATM if direction-aware failed
                if not dir_tokens:
                    tokens = D.get_option_tokens(kite, atm_strike, expiry)
                    if not tokens:
                        logger.warning("[MAIN] No tokens resolved for ATM=" + str(atm_strike))
                        time.sleep(2)
                        continue
                    dir_tokens = tokens
                    dir_strikes = {"CE": atm_strike, "PE": atm_strike}

                # v12.15: EXPIRY BREAKOUT MODE (DTE=0)
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
                    opt_info = dir_tokens.get(opt_type)
                    if not opt_info:
                        continue

                    _opt_strike = dir_strikes.get(opt_type, atm_strike)

                    D.subscribe_tokens([opt_info["token"]])
                    time.sleep(0.3)

                    result = check_entry(
                        token       = opt_info["token"],
                        option_type = opt_type,
                        profile     = profile,
                        spot_ltp    = spot_ltp,
                        strike      = _opt_strike,
                        expiry_date = expiry,
                        session     = session,
                    )

                    result["_strike"] = _opt_strike  # carry per-direction strike
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

                # v12.15: Write dashboard snapshot for web
                try:
                    _write_dashboard(spot_ltp, atm_strike, dte, vix_ltp, session,
                                     profile, all_results, expiry, now,
                                     dir_strikes=dir_strikes)
                except Exception as _de:
                    logger.debug("[DASH] " + str(_de))

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
#  TELEGRAM COMMANDS — extracted to VRL_COMMANDS.py
# ═══════════════════════════════════════════════════════════════
import VRL_COMMANDS

# ── Telegram listener state ───────────────────────────────────
_tg_offset         = 0
_tg_last_update_id = 0
_tg_running        = False

def _tg_get_updates(offset: int) -> list:
    url = _TG_BASE + D.TELEGRAM_TOKEN + "/getUpdates"
    try:
        resp = requests.get(url, params={"offset": offset, "timeout": 20}, timeout=30)
        if resp.ok:
            return resp.json().get("result", [])
    except Exception as e:
        logger.warning("[CTRL] getUpdates error: " + type(e).__name__)
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
    handler = VRL_COMMANDS._DISPATCH.get(raw_cmd)
    if handler:
        handler(args)
    else:
        _WATCHDOG = ("/deploy","/serverstatus","/serverlog","/gitlog")
        if raw_cmd not in _WATCHDOG:
            _tg_send("Unknown command: " + raw_cmd + "\nType /help")

def _tg_handle_callback(callback: dict):
    # Auth check — same as _tg_handle_message
    msg = callback.get("message", {})
    if str(msg.get("chat", {}).get("id", "")) != str(D.TELEGRAM_CHAT_ID):
        return
    query_id = callback.get("id", "")
    data     = callback.get("data", "")
    if data.startswith("FB:"):
        VRL_COMMANDS._handle_file_browser_callback(data, query_id)
    elif data.startswith("DL:"):
        VRL_COMMANDS._handle_download_callback(data, query_id)
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
    except Exception as e:
        logger.warning("[CTRL] Startup getUpdates skip: " + type(e).__name__)

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

    # Wire Telegram commands module
    VRL_COMMANDS.setup(
        state_ref=state, lock_ref=_state_lock,
        tg_send_fn=_tg_send, tg_send_file_fn=_tg_send_file,
        tg_inline_keyboard_fn=_tg_inline_keyboard,
        tg_answer_callback_fn=_tg_answer_callback,
        save_state_fn=_save_state, read_today_trades_fn=_read_today_trades,
        remove_pid_fn=_remove_pid, now_str_fn=_now_str, rs_fn=_rs,
        kite_ref=kite,
    )

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

    # v12.15: Calculate fib pivot points
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
