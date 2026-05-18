#!/usr/bin/env python3
"""VRL 15-min market monitor — sends Telegram summary during market hours."""

import os, re, requests
from datetime import datetime, date
from collections import Counter, defaultdict

TG_TOKEN  = os.getenv("TG_TOKEN", "")
TG_CHATID = os.getenv("TG_GROUP_ID", "")
LOG_LIVE  = os.path.expanduser("~/logs/live/vrl_live.log")
TRADE_CSV = os.path.expanduser("~/lab_data/vrl_trade_log.csv")
ERROR_LOG = os.path.expanduser(f"~/logs/errors/{date.today().isoformat()}.log")

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

def check_bot_running():
    import subprocess
    try:
        r = subprocess.run(["pgrep", "-f", "VRL_MAIN.py"], capture_output=True)
        return r.returncode == 0
    except:
        return False

def get_mode():
    lines = tail(LOG_LIVE, 500)
    for line in reversed(lines):
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

def get_rejections(minutes=15):
    lines = tail(LOG_LIVE, 500)
    now = datetime.now()
    rejects = Counter()
    last_signal_time = None
    for line in lines:
        # parse timestamp
        m = re.match(r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})", line)
        if not m:
            continue
        try:
            ts = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S")
        except:
            continue
        age_min = (now - ts).total_seconds() / 60
        if age_min > minutes:
            continue
        if "[REJECT" in line or "reject_reason" in line:
            # extract reason
            rm = re.search(r"(band_too_\w+|rsi_\w+|same_candle\w*|exit_candle\w*|both_sides\w*|force_exit\w*|cooldown\w*|candle_not_green\w*|close_below\w*|slope\w*|gate\w*)", line)
            reason = rm.group(1) if rm else "unknown"
            rejects[reason] += 1
        if "[SIGNAL]" in line or "gate=PASS" in line.lower() or "fired=True" in line:
            last_signal_time = ts
    return rejects, last_signal_time

def get_recent_errors(minutes=15):
    lines = tail(ERROR_LOG, 100) + tail(LOG_LIVE, 300)
    now = datetime.now()
    errors = []
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
            errors.append(line.strip()[-120:])
    return errors[-5:]  # max 5

def get_ws_status():
    lines = tail(LOG_LIVE, 100)
    for line in reversed(lines):
        if "[WS]" in line:
            if "Connected" in line: return "Connected"
            if "Disconnected" in line or "error" in line.lower(): return "Disconnected"
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

    mode_icon = "🟢" if mode == "LIVE" else "🟡"
    bot_icon  = "✅" if running else "❌"
    ws_icon   = "✅" if ws == "Connected" else "⚠️"
    pnl_icon  = "🟢" if pnl > 0 else ("🔴" if pnl < 0 else "⚪")

    msg = f"<b>📊 VRL Monitor — {now.strftime('%H:%M')}</b>\n"
    msg += f"{'─'*28}\n"
    msg += f"Bot: {bot_icon} {'Running' if running else 'STOPPED'}  |  Mode: {mode_icon} {mode}\n"
    msg += f"WebSocket: {ws_icon} {ws}\n"
    msg += f"{'─'*28}\n"
    msg += f"<b>Today's Trades</b>\n"
    msg += f"Total: {trades}  |  W: {wins}  L: {losses}\n"
    msg += f"PnL: {pnl_icon} {pnl:+.1f} pts\n"

    if last_trade:
        msg += f"Last: {last_trade['symbol']} | {last_trade['pnl']} pts | {last_trade['exit']} @ {last_trade['time']}\n"

    msg += f"{'─'*28}\n"
    msg += f"<b>Gate Rejections (last 15 min)</b>\n"
    if rejects:
        for reason, count in rejects.most_common(5):
            msg += f"  • {reason}: {count}x\n"
    else:
        msg += "  No rejections\n"

    if last_signal:
        age = int((now - last_signal).total_seconds() / 60)
        msg += f"Last signal: {age} min ago\n"

    if errors:
        msg += f"{'─'*28}\n"
        msg += f"<b>⚠️ Recent Errors</b>\n"
        for e in errors:
            msg += f"  • {e[-100:]}\n"

    if not running:
        msg += f"\n🚨 <b>BOT IS NOT RUNNING!</b>"

    send(msg)
    print(msg)

if __name__ == "__main__":
    main()
