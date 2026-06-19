#!/usr/bin/env python3
"""
Upstox headless daily-token refresh (market-data migration, Phase 0 — 2026-06-19).

Generates a fresh Upstox access token via the headless TOTP flow (upstox-totp),
writes it back into ~/.env as UPSTOX_ACCESS_TOKEN, and verifies it with a live
profile call. STANDALONE — does NOT touch the live bot.

Upstox tokens expire daily at ~03:30 IST, so this is meant to run from cron each
trading morning BEFORE the bot starts (e.g. ~06:00 Mon-Fri).

The login flow used by upstox-totp is: username -> generate OTP -> verify with
TOTP code -> submit PIN -> OAuth. The account has no separate login password;
UPSTOX_PASSWORD is a required-but-unused model field, set to the PIN in ~/.env.

Usage:
    set -a && . ~/.env && set +a && ~/kite_env/bin/python3 upstox_auth.py
Exit code 0 on success (token written + verified), 1 on any failure.
"""
import os
import re
import sys
import requests

ENV_PATH = os.path.expanduser("~/.env")
PROFILE_URL = "https://api.upstox.com/v2/user/profile"


def _write_token_to_env(token: str) -> None:
    """Replace (or append) the UPSTOX_ACCESS_TOKEN line in ~/.env in place."""
    with open(ENV_PATH, "r") as f:
        lines = f.readlines()

    new_line = f"UPSTOX_ACCESS_TOKEN={token}\n"
    replaced = False
    for i, ln in enumerate(lines):
        if re.match(r"\s*UPSTOX_ACCESS_TOKEN\s*=", ln):
            lines[i] = new_line
            replaced = True
            break
    if not replaced:
        if lines and not lines[-1].endswith("\n"):
            lines[-1] += "\n"
        lines.append(new_line)

    with open(ENV_PATH, "w") as f:
        f.writelines(lines)
    os.chmod(ENV_PATH, 0o600)


def _verify(token: str) -> dict:
    r = requests.get(
        PROFILE_URL,
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
        timeout=15,
    )
    r.raise_for_status()
    body = r.json()
    if body.get("status") != "success":
        raise RuntimeError(f"profile returned non-success: {body}")
    return body["data"]


def main() -> int:
    try:
        from upstox_totp import UpstoxTOTP
    except ImportError:
        print("ERROR: upstox-totp not installed (~/kite_env/bin/python3 -m pip install upstox-totp)")
        return 1

    try:
        resp = UpstoxTOTP().app_token.get_access_token()
    except Exception as e:
        print(f"ERROR: headless login raised: {e}")
        return 1

    if not (resp.success and resp.data):
        print(f"ERROR: token generation failed: {getattr(resp, 'error', resp)}")
        return 1

    token = resp.data.access_token
    try:
        prof = _verify(token)
    except Exception as e:
        print(f"ERROR: token generated but verification failed: {e}")
        return 1

    _write_token_to_env(token)
    print(
        f"OK: fresh Upstox token written to {ENV_PATH} "
        f"(len={len(token)}) — verified for {prof.get('user_name')} ({prof.get('user_id')})"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
