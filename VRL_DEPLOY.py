#!/home/user/kite_env/bin/python3
# ═══════════════════════════════════════════════════════════════
#  VRL_DEPLOY.py — VISHAL RAJPUT TRADE v13.3
#  Independent watchdog daemon. Runs separately from VRL_MAIN.
#  Polls Telegram for /deploy, /serverstatus, /serverlog.
#  Can restart the bot even when bot is dead.
#  Zero dependency on VRL_MAIN or VRL_ENGINE.
# ═══════════════════════════════════════════════════════════════

import os
import sys
import json
import time
import subprocess
from datetime import datetime

# ── CONFIG ────────────────────────────────────────────────────

def _load_env():
    env_path = os.path.expanduser("~/.env")
    if os.path.isfile(env_path):
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, val = line.partition("=")
                os.environ.setdefault(key.strip(), val.strip().strip('"').strip("'"))

_load_env()

TG_TOKEN   = os.getenv("TG_TOKEN", "")
TG_CHAT_ID = os.getenv("TG_GROUP_ID", "")
REPO_DIR   = os.path.expanduser("~/VISHAL_RAJPUT")
HOME_DIR   = os.path.expanduser("~")
LOG_FILE   = os.path.join(HOME_DIR, "logs", "live", "vrl_live.log")
OUT_LOG    = os.path.join(HOME_DIR, "vrl_out.log")
PID_FILE   = os.path.join(HOME_DIR, "state", "vrl_deploy.pid")
POLL_SEC   = 3   # Check Telegram every 3 seconds
VENV_PY    = os.path.expanduser("~/kite_env/bin/python3")

# ── TELEGRAM ──────────────────────────────────────────────────

import urllib.request
import urllib.parse

_offset = 0

def _tg_send(text: str):
    try:
        url = "https://api.telegram.org/bot" + TG_TOKEN + "/sendMessage"
        data = urllib.parse.urlencode({
            "chat_id": TG_CHAT_ID,
            "text": text,
            "parse_mode": "HTML",
        }).encode()
        urllib.request.urlopen(url, data, timeout=10)
    except Exception as e:
        print("[DEPLOY] TG send error: " + str(e))


def _tg_get_updates() -> list:
    global _offset
    try:
        url = ("https://api.telegram.org/bot" + TG_TOKEN
               + "/getUpdates?offset=" + str(_offset)
               + "&timeout=2&allowed_updates=[\"message\"]")
        resp = urllib.request.urlopen(url, timeout=10)
        data = json.loads(resp.read())
        if not data.get("ok"):
            return []
        results = data.get("result", [])
        if results:
            _offset = results[-1]["update_id"] + 1
        return results
    except Exception:
        return []


# ── COMMANDS ──────────────────────────────────────────────────

def _cmd_deploy():
    """Git pull + restart VRL_MAIN."""
    _tg_send("🔄 <b>DEPLOY STARTED</b>\nPulling from GitHub...")

    try:
        # Step 1: Git pull
        r = subprocess.run(
            ["git", "pull", "origin", "main"],
            cwd=REPO_DIR, capture_output=True, text=True, timeout=30)
        pull_msg = r.stdout.strip() or r.stderr.strip()
        _tg_send("📥 Pull: " + pull_msg[:200])

        if "Already up to date" in pull_msg:
            _tg_send("ℹ️ No changes. Restarting bot anyway...")

        # Step 2: Clear pycache
        subprocess.run(
            ["find", REPO_DIR, "-name", "*.pyc", "-delete"],
            capture_output=True, timeout=10)

        # Step 3: Restart bot via systemd
        r_restart = subprocess.run(
            ["sudo", "systemctl", "restart", "vrl-main"],
            capture_output=True, text=True, timeout=15)
        time.sleep(5)

        # Step 4: Verify bot started
        r_status = subprocess.run(
            ["systemctl", "is-active", "vrl-main"],
            capture_output=True, text=True, timeout=5)
        active = r_status.stdout.strip()

        if active == "active":
            r_pid = subprocess.run(
                ["pgrep", "-f", "VRL_MAIN.py"],
                capture_output=True, text=True, timeout=5)
            pid = r_pid.stdout.strip()
            _tg_send(
                "✅ <b>DEPLOY COMPLETE</b>\n"
                "Bot PID: " + pid + "\n"
                "Time: " + datetime.now().strftime("%H:%M:%S"))
        else:
            try:
                r2 = subprocess.run(
                    ["tail", "-5", OUT_LOG],
                    capture_output=True, text=True, timeout=5)
                _tg_send(
                    "❌ <b>DEPLOY FAILED — Bot didn't start</b>\n"
                    "Status: " + active + "\n"
                    "Last log:\n<pre>" + r2.stdout[-500:] + "</pre>")
            except Exception:
                _tg_send("❌ <b>DEPLOY FAILED — Status: " + active + "</b>")

    except Exception as e:
        _tg_send("❌ <b>DEPLOY ERROR</b>\n" + str(e)[:300])


def _cmd_serverstatus():
    """Check if bot is alive, show PID and last log."""
    try:
        # Check bot process via systemd
        r_active = subprocess.run(
            ["systemctl", "is-active", "vrl-main"],
            capture_output=True, text=True, timeout=5)
        r = subprocess.run(
            ["pgrep", "-f", "VRL_MAIN.py"],
            capture_output=True, text=True, timeout=5)
        pid = r.stdout.strip()

        # Last log line
        last_log = ""
        if os.path.isfile(LOG_FILE):
            r2 = subprocess.run(
                ["tail", "-3", LOG_FILE],
                capture_output=True, text=True, timeout=5)
            last_log = r2.stdout.strip()[-300:]

        # Disk space
        r3 = subprocess.run(
            ["df", "-h", HOME_DIR],
            capture_output=True, text=True, timeout=5)
        disk = r3.stdout.strip().split("\n")[-1].split()
        disk_free = disk[3] if len(disk) > 3 else "?"

        # Uptime
        r4 = subprocess.run(
            ["uptime", "-p"],
            capture_output=True, text=True, timeout=5)
        uptime = r4.stdout.strip()

        svc_active = r_active.stdout.strip() == "active"
        bot_status = ("🟢 RUNNING (PID " + pid + ")") if (pid and svc_active) else ("🟡 PID " + pid + " (no systemd)") if pid else "🔴 DEAD"

        _tg_send(
            "🖥 <b>SERVER STATUS</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "Bot    : " + bot_status + "\n"
            "Server : " + uptime + "\n"
            "Disk   : " + disk_free + " free\n"
            "Time   : " + datetime.now().strftime("%H:%M:%S") + "\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "Last log:\n<pre>" + last_log + "</pre>")

    except Exception as e:
        _tg_send("Server status error: " + str(e))


def _cmd_serverlog():
    """Show last 20 lines of live log."""
    try:
        log = LOG_FILE
        if not os.path.isfile(log):
            log = OUT_LOG
        r = subprocess.run(
            ["tail", "-20", log],
            capture_output=True, text=True, timeout=5)
        text = r.stdout.strip()[-3000:]
        _tg_send("<b>📋 SERVER LOG</b>\n<pre>" + text + "</pre>")
    except Exception as e:
        _tg_send("Log fetch error: " + str(e))


def _cmd_gitlog():
    """Show last 5 git commits."""
    try:
        r = subprocess.run(
            ["git", "log", "--oneline", "-5"],
            cwd=REPO_DIR, capture_output=True, text=True, timeout=10)
        _tg_send("<b>📝 GIT LOG</b>\n<pre>" + r.stdout.strip() + "</pre>")
    except Exception as e:
        _tg_send("Git log error: " + str(e))


# ── DISPATCH ──────────────────────────────────────────────────

COMMANDS = {
    "/deploy":       _cmd_deploy,
    "/serverstatus": _cmd_serverstatus,
    "/serverlog":    _cmd_serverlog,
    "/gitlog":       _cmd_gitlog,
}

# ── MAIN LOOP ─────────────────────────────────────────────────

def _write_pid():
    os.makedirs(os.path.dirname(PID_FILE), exist_ok=True)
    with open(PID_FILE, "w") as f:
        f.write(str(os.getpid()))


def _check_single_instance():
    """Ensure only one watchdog runs."""
    if os.path.isfile(PID_FILE):
        try:
            with open(PID_FILE) as f:
                old_pid = int(f.read().strip())
            os.kill(old_pid, 0)  # Check if alive
            print("[DEPLOY] Another watchdog running (PID " + str(old_pid) + "). Exiting.")
            sys.exit(0)
        except (OSError, ValueError):
            pass  # Old process dead, continue


def main():
    if not TG_TOKEN or not TG_CHAT_ID:
        print("[DEPLOY] ERROR: TG_TOKEN or TG_GROUP_ID not set")
        sys.exit(1)

    _check_single_instance()
    _write_pid()

    print("[DEPLOY] Watchdog started at " + datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    print("[DEPLOY] Repo: " + REPO_DIR)
    print("[DEPLOY] Polling Telegram every " + str(POLL_SEC) + "s")

    _tg_send("🛡 <b>WATCHDOG ACTIVE</b>\n"
             "Commands: /deploy /serverstatus /serverlog /gitlog\n"
             "Time: " + datetime.now().strftime("%H:%M:%S"))

    while True:
        try:
            updates = _tg_get_updates()
            for upd in updates:
                msg  = upd.get("message", {})
                text = msg.get("text", "").strip().split()[0].split("@")[0].lower() if msg.get("text") else ""
                chat = str(msg.get("chat", {}).get("id", ""))

                if chat != TG_CHAT_ID:
                    continue

                if text in COMMANDS:
                    print("[DEPLOY] Command: " + text)
                    COMMANDS[text]()

        except KeyboardInterrupt:
            print("[DEPLOY] Shutting down")
            break
        except Exception as e:
            print("[DEPLOY] Error: " + str(e))

        time.sleep(POLL_SEC)


if __name__ == "__main__":
    main()
