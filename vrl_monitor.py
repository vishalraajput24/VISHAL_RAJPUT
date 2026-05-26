#!/usr/bin/env python3
"""VRL 15-min market monitor — sends Telegram summary during market hours."""

import os, re, requests
from datetime import datetime, date
from collections import Counter

TG_TOKEN  = os.getenv("TG_TOKEN", "")
TG_CHATID = os.getenv("TG_GROUP_ID", "")
LOG_LIVE  = os.path.expanduser("~/logs/live/vrl_live.log")
TRADE_CSV = os.path.expanduser("~/lab_data/vrl_trade_log.csv")
ERROR_LOG = os.path.expanduser(f"~/logs/errors/{date.today().isoformat()}.log")
STATE_FILE = os.path.expanduser("~/state/vrl_live_state.json")

def send(text):
    if not TG_TOKEN or not TG_CHATID:
        print("No TG creds"); return
    requests.post(
        f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
        json={"chat_id": TG_CHATID, "text": text, "parse_mode": "HTML"},
        timeout=10
    )

def tail(path, n=300):
    try:
        with open(path) as f:
            lines = f.readlines()
        return lines[-n:]
    except:
        return []

def read_all(path):
    try:
        with open(path) as f:
            return f.readlines()
    except:
        return []

def check_bot_running():
    import subprocess
    try:
        r = subprocess.run(["pgrep", "-f", "VRL_MAIN.py"], capture_output=True)
        return r.returncode == 0
    except:
        return False

def get_mode():
    # First try state file (always up to date)
    try:
        import json
        with open(STATE_FILE) as f:
            s = json.load(f)
        mode = s.get("mode", "")
        if mode:
            return mode
    except:
        pass
    # Fallback: scan full log
    for line in reversed(read_all(LOG_LIVE)):
        if "[MAIN] Mode:" in line:
            return "LIVE" if "LIVE" in line else "PAPER"
    return "UNKNOWN"

def get_today_trades():
    today = date.today().isoformat()
    trades, wins, losses, gross_pnl = 0, 0, 0, 0.0
    try:
        with open(TRADE_CSV) as f:
            for line in f:
                if not line.startswith(today):
                    continue
                parts = line.strip().split(",")
                if len(parts) < 9:
                    continue
                try:
                    pnl = float(parts[8])
                    trades += 1
                    gross_pnl += pnl
                    if pnl > 0: wins += 1
                    else: losses += 1
                except:
                    pass
    except:
        pass
    return trades, wins, losses, round(gross_pnl, 1)

def get_last_trade_info():
    today = date.today().isoformat()
    last = None
    try:
        with open(TRADE_CSV) as f:
            for line in f:
                if line.startswith(today):
                    last = line.strip()
    except:
        pass
    if not last:
        return None
    p = last.split(",")
    if len(p) < 14:
        return None
    return {"symbol": p[3], "pnl": p[8], "exit": p[13], "time": p[2]}

# Human-readable labels for gate reject reasons
REJECT_LABELS = {
    "gate2_below_band":    "Price below band (CE/PE too weak)",
    "gate3_band_narrow":   "Band too narrow (market flat)",
    "gate3_band_wide":     "Band too wide (overextended)",
    "gate5_rsi_below_50":  "RSI below 50 (no momentum)",
    "gate5_rsi_overextended": "RSI above 65 (overbought)",
    "gate1_close_below_band": "Candle closed below band",
    "gate2_rsi_not_rising":"RSI falling (momentum lost)",
    "same_candle_guard":   "Same candle guard (already fired)",
    "both_sides_cooldown": "Both sides cooling down",
    "cooldown_skip":       "Cooldown active",
    "slope":               "EMA slope falling",
}

def get_rejections(minutes=15):
    lines = tail(LOG_LIVE, 600)
    now = datetime.now()
    rejects = Counter()
    last_signal_time = None
    for line in lines:
        m = re.match(r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})", line)
        if not m:
            continue
        try:
            ts = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S")
        except:
            continue
        if (now - ts).total_seconds() / 60 > minutes:
            continue
        if "[REJECT" in line:
            rm = re.search(r"(gate\d\w*|band_too_\w+|rsi_\w+|same_candle\w*|both_sides\w*|cooldown\w*|slope\w*)", line)
            reason = rm.group(1) if rm else "unknown"
            rejects[reason] += 1
        if "[ENGINE-V9]" in line and "FIRED" in line:
            last_signal_time = ts
    return rejects, last_signal_time

def get_recent_errors(minutes=15):
    lines = tail(ERROR_LOG, 100) + tail(LOG_LIVE, 300)
    now = datetime.now()
    errors = []
    seen = set()
    for line in lines:
        if "ERROR" not in line and "WARNING" not in line:
            continue
        m = re.match(r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})", line)
        if not m:
            continue
        try:
            ts = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S")
        except:
            continue
        if (now - ts).total_seconds() / 60 <= minutes:
            msg = line.strip()[-120:]
            if msg not in seen:
                seen.add(msg)
                errors.append(msg)
    return errors[-5:]

def get_ws_status():
    for line in reversed(read_all(LOG_LIVE)):
        if "[WS]" not in line:
            continue
        if "Connected" in line or "Subscribed" in line:
            return "Connected"
        if "Disconnected" in line or "error" in line.lower() or "Closed" in line:
            return "Disconnected"
    return "Unknown"

def main():
    now = datetime.now()
    running = check_bot_running()
    mode    = get_mode()
    trades, wins, losses, pnl = get_today_trades()
    rejects, last_signal = get_rejections(15)
    errors  = get_recent_errors(15)
    ws      = get_ws_status()
    last_trade = get_last_trade_info()

    bot_icon = "✅" if running else "❌"
    ws_icon  = "✅" if ws == "Connected" else "⚠️"
    pnl_icon = "🟢" if pnl > 0 else ("🔴" if pnl < 0 else "⚪")
    mode_icon = "🔴" if mode == "LIVE" else "🟡"

    msg = f"<b>📊 VRL — {now.strftime('%H:%M')}</b>\n"
    msg += f"{'─'*26}\n"

    # Bot status line
    status = "Running" if running else "STOPPED 🚨"
    msg += f"Bot: {bot_icon} {status}  |  {mode_icon} {mode} mode\n"
    msg += f"Feed: {ws_icon} {ws}\n"
    msg += f"{'─'*26}\n"

    # PnL summary
    msg += f"<b>Today</b>: {trades} trades  ({wins}W / {losses}L)\n"
    msg += f"<b>PnL</b>: {pnl_icon} {pnl:+.1f} pts\n"
    if last_trade:
        side = "CE" if "CE" in last_trade['symbol'] else "PE"
        exit_reason = last_trade['exit'].replace("_", " ").title()
        msg += f"Last trade: {side} {last_trade['pnl']} pts — {exit_reason} @ {last_trade['time'][:5]}\n"

    msg += f"{'─'*26}\n"

    # Why no trade (top 3 unique reasons, human readable)
    if rejects:
        msg += f"<b>Why no trade (last 15 min):</b>\n"
        shown = 0
        for reason, count in rejects.most_common():
            label = REJECT_LABELS.get(reason, reason.replace("_", " "))
            msg += f"  • {label}\n"
            shown += 1
            if shown >= 3:
                break
    else:
        msg += "No rejections in last 15 min\n"

    # Last signal
    if last_signal:
        age = int((now - last_signal).total_seconds() / 60)
        msg += f"Last V9 signal: {age} min ago\n"
    else:
        msg += f"Last V9 signal: none today\n"

    # Errors
    if errors:
        msg += f"{'─'*26}\n"
        msg += f"<b>⚠️ Errors:</b>\n"
        for e in errors:
            msg += f"  • {e[-100:]}\n"

    send(msg)
    print(msg)

if __name__ == "__main__":
    main()
