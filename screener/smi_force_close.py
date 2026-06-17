#!/usr/bin/env python3
"""
smi_force_close.py  — one-off manual flat-out of open SMI paper trades.
Owner instruction 2026-06-17: close ALL current stock-F&O paper trades (frozen +
loose) to start a clean slate and monitor new (bugfree, post PR #267) trades.

Usage:  python3 smi_force_close.py frozen   |   python3 smi_force_close.py loose
Reuses the engine's OWN log_exit / tracker_upsert so logs + tracker + state stay
consistent. Exits at live option LTP, reason=FORCE-CLOSE.
"""
import sys, json
from datetime import datetime

mode = sys.argv[1] if len(sys.argv) > 1 else ""
if mode == "loose":
    import smi_paper_loose as _L   # applies loose file paths + SMI_LOOSE tracker tag
    import smi_paper as S
    label = "LOOSE"
elif mode == "frozen":
    import smi_paper as S
    label = "FROZEN"
else:
    print("usage: smi_force_close.py frozen|loose"); sys.exit(1)

state = S.load_state()
opens = state.get("open_trades", {})
if not opens:
    print(f"[{label}] no open trades — nothing to close"); sys.exit(0)

kite = S.get_kite()
opt_syms = [t["option_symbol"] for t in opens.values()]
prem = S.get_ltps(kite, opt_syms)                      # NFO option LTPs

# best-effort live stock prices (for the log's stock_exit field only)
stk = {}
try:
    nse = [f"NSE:{t['symbol']}" for t in opens.values()]
    for i in range(0, len(nse), 200):
        q = kite.ltp(nse[i:i+200])
        for k, v in q.items():
            stk[k.split(":", 1)[1]] = float(v["last_price"])
except Exception as e:
    print(f"  stock ltp warn: {e}")

now = datetime.now()
tot_rs = 0.0
closed = []
for sym, t in list(opens.items()):
    exit_prem = prem.get(t["option_symbol"], t["entry_premium"])
    stock_exit = stk.get(t["symbol"], t["stock_entry"])
    pnl_pct, pnl_rs = S.log_exit(t, now.strftime("%Y-%m-%d %H:%M:%S"),
                                 stock_exit, exit_prem, "FORCE-CLOSE")
    S.tracker_upsert(t, "FORCE-CLOSED ⏹", exit_prem)
    tot_rs += pnl_rs
    closed.append((t["symbol"], t["direction"], round(pnl_rs)))
    del opens[sym]

state["open_trades"] = opens
S.save_state(state)

lines = "\n".join(f"  {s} {d}: ₹{r:+,}" for s, d, r in closed)
print(f"[{label}] force-closed {len(closed)} trades · net ₹{tot_rs:+,.0f}\n{lines}")
S.send_telegram(f"⏹ SMI {label} — force-closed {len(closed)} open paper trades "
                f"(clean slate). Net ₹{tot_rs:+,.0f}. Monitoring new trades next.")
