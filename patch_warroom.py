"""
WAR ROOM BUILD — v12.14
1. VRL_MAIN.py: Write dashboard snapshot every cycle → ~/state/vrl_dashboard.json
2. VRL_WEB.py: Complete rewrite — dumb renderer, zero calculations
"""
import os
R = os.path.expanduser("~/VISHAL_RAJPUT")
def rf(n):
    with open(os.path.join(R, n)) as f: return f.read()
def wf(n, c):
    with open(os.path.join(R, n), "w") as f: f.write(c)
    print("[OK] " + n)

# ═══════════════════════════════════════════════════════════════
#  1. VRL_MAIN.py — Add dashboard snapshot writer
# ═══════════════════════════════════════════════════════════════
m = rf("VRL_MAIN.py")

# 1a. Add DASHBOARD_PATH constant after STATE_FILE_PATH
old_pid = 'PID_FILE_PATH    = os.path.join(STATE_DIR,    "vrl_live.pid")'
new_pid = '''PID_FILE_PATH    = os.path.join(STATE_DIR,    "vrl_live.pid")
DASHBOARD_PATH   = os.path.join(STATE_DIR,    "vrl_dashboard.json")'''
m = m.replace(old_pid, new_pid)

# 1b. Add _write_dashboard function before _strategy_loop
old_strat = 'def _strategy_loop(kite):'
dashboard_func = r'''
# ═══════════════════════════════════════════════════════════════
#  DASHBOARD SNAPSHOT — written every cycle for VRL_WEB.py
#  VRL_WEB.py reads this file. Zero calculation in web server.
# ═══════════════════════════════════════════════════════════════

def _write_dashboard(spot_ltp, atm_strike, dte, vix_ltp, session,
                     profile, all_results, expiry, now):
    """Write everything the dashboard needs to a single JSON file."""
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
        except Exception:
            pass

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
                                 "rsi_ok": False, "vol": 0, "vol_ok": False},
                    "score": 0, "score_min": D.SESSION_SCORE_MIN.get(session, 5),
                    "fired": False, "verdict": "NO DATA",
                    "greeks": {"delta": 0, "iv": 0, "theta": 0, "gamma": 0},
                    "ltp": 0, "regime": "",
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
            if result.get("fired"):
                verdict = "FIRED"
            elif conds < 3:
                verdict = "3M BLOCKED " + str(conds) + "/4"
            elif spread_1m < min_spread:
                verdict = "SPREAD " + str(round(spread_1m, 1)) + " need +" + str(min_spread)
            elif d1.get("rsi_reject"):
                rsi_v = d1.get("rsi_val", 0)
                if rsi_v > 65:
                    verdict = "RSI " + str(rsi_v) + " TOO HIGH"
                else:
                    verdict = "RSI " + str(rsi_v) + " NOT RISING"
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
                },
                "spread_1m": round(spread_1m, 1),
                "spread_1m_min": min_spread,
                "entry_1m": {
                    "body_pct": round(d1.get("body_pct", 0), 1),
                    "body_ok": d1.get("body_ok", False),
                    "rsi": round(d1.get("rsi_val", 0), 1),
                    "rsi_rising": d1.get("rsi_rising", False),
                    "rsi_ok": d1.get("rsi_ok", False),
                    "vol": round(d1.get("vol_ratio", 0), 2),
                    "vol_ok": d1.get("vol_ok", False),
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
            }

        ce_signal = _build_signal("CE", all_results.get("CE"))
        pe_signal = _build_signal("PE", all_results.get("PE"))

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
                "expiry": expiry.isoformat() if expiry else "",
                "market_open": D.is_market_open(),
            },
            "ce": ce_signal,
            "pe": pe_signal,
            "position": position,
            "today": today_block,
            "straddle": straddle_block,
        }

        # Atomic write
        tmp = D.DASHBOARD_PATH + ".tmp"
        with open(tmp, "w") as f:
            json.dump(dashboard, f, indent=2, default=str)
        os.replace(tmp, D.DASHBOARD_PATH)

    except Exception as e:
        logger.debug("[DASH] Snapshot write: " + str(e))


''' + 'def _strategy_loop(kite):'
m = m.replace(old_strat, dashboard_func)

# 1c. Add DASHBOARD_PATH to D module reference
m = m.replace(
    "D.DASHBOARD_PATH",
    "os.path.join(D.STATE_DIR, 'vrl_dashboard.json')"
)

# 1d. Call _write_dashboard after scan saves
old_scan_save_end = '''                if best_result and best_opt_info:
                    _execute_entry(kite, best_opt_info, best_type,
                                   best_result, profile, expiry, dte, session)'''
new_scan_save_end = '''                # v12.14: Write dashboard snapshot for web
                try:
                    _write_dashboard(spot_ltp, atm_strike, dte, vix_ltp, session,
                                     profile, all_results, expiry, now)
                except Exception as _de:
                    logger.debug("[DASH] " + str(_de))

                if best_result and best_opt_info:
                    _execute_entry(kite, best_opt_info, best_type,
                                   best_result, profile, expiry, dte, session)'''
m = m.replace(old_scan_save_end, new_scan_save_end)

# 1e. Also write dashboard when in trade (for position updates)
old_save_state_intrade = '''                        _save_state()

                time.sleep(0.5)
                continue'''
new_save_state_intrade = '''                        _save_state()
                        # Dashboard update during trade
                        try:
                            _write_dashboard(spot_ltp, state.get("strike", 0),
                                             dte, D.get_vix(), session,
                                             profile, {}, expiry, now)
                        except Exception:
                            pass

                time.sleep(0.5)
                continue'''
m = m.replace(old_save_state_intrade, new_save_state_intrade)

wf("VRL_MAIN.py", m)


# ═══════════════════════════════════════════════════════════════
#  2. VRL_WEB.py — Complete rewrite. DUMB RENDERER.
#     Reads vrl_dashboard.json + trade log. Zero calculations.
# ═══════════════════════════════════════════════════════════════

web_code = r'''#!/usr/bin/env python3
"""
VRL_WEB.py — VISHAL RAJPUT TRADE War Room v12.14
DUMB RENDERER. Reads vrl_dashboard.json from bot. Zero calculations.
"""
import csv, json, os
from datetime import date
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse

PORT = 8080
BASE = os.path.expanduser("~")
DASH_FILE = os.path.join(BASE, "state", "vrl_dashboard.json")
TRADE_LOG = os.path.join(BASE, "lab_data", "vrl_trade_log.csv")

def _read_dash():
    if not os.path.isfile(DASH_FILE): return {}
    try:
        with open(DASH_FILE) as f: return json.load(f)
    except: return {}

def _read_trades():
    if not os.path.isfile(TRADE_LOG): return []
    today = date.today().isoformat()
    trades = []
    try:
        with open(TRADE_LOG) as f:
            for r in csv.DictReader(f):
                if r.get("date","").strip() == today:
                    try: trades.append({k: r.get(k,"") for k in r})
                    except: pass
    except: pass
    return trades

HTML = r"""<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1">
<title>VRL War Room</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
:root{--bg:#080810;--c1:#0f0f1a;--c2:#161625;--bd:#1e1e30;--tx:#e4e4e7;--dm:#666;--bl:#3b82f6;--gn:#10b981;--rd:#ef4444;--am:#f59e0b;--pr:#a855f7;--cy:#06b6d4}
body{background:var(--bg);color:var(--tx);font-family:'SF Mono',Menlo,monospace;font-size:12px;max-width:500px;margin:0 auto}
.hd{background:var(--c1);border-bottom:1px solid var(--bd);padding:10px 12px;position:sticky;top:0;z-index:10}
.hd h1{font-size:13px;font-weight:700;letter-spacing:.5px}.hd b{color:var(--bl)}
.tags{display:flex;gap:4px;margin-top:5px;flex-wrap:wrap}
.tag{padding:2px 6px;border-radius:3px;font-size:9px;font-weight:700;letter-spacing:.3px}
.tg{background:rgba(16,185,129,.15);color:var(--gn)}.tr{background:rgba(239,68,68,.15);color:var(--rd)}
.tb{background:rgba(59,130,246,.15);color:var(--bl)}.ta{background:rgba(245,158,11,.15);color:var(--am)}
.tp{background:rgba(168,85,247,.15);color:var(--pr)}
.sect{margin:8px;background:var(--c1);border:1px solid var(--bd);border-radius:8px;overflow:hidden}
.sh{padding:8px 10px;font-size:10px;font-weight:700;color:var(--dm);text-transform:uppercase;letter-spacing:.8px;border-bottom:1px solid var(--bd);background:var(--c2)}
.row{display:flex;justify-content:space-between;padding:5px 10px;border-bottom:1px solid rgba(30,30,48,.5)}
.row:last-child{border:none}
.row .k{color:var(--dm);font-size:10px}.row .v{font-weight:700;font-size:12px}
.gate{display:flex;gap:6px;padding:8px 10px;flex-wrap:wrap}
.dot{width:28px;height:28px;border-radius:6px;display:flex;align-items:center;justify-content:center;font-size:9px;font-weight:700}
.dot-g{background:rgba(16,185,129,.2);color:var(--gn);border:1px solid rgba(16,185,129,.3)}
.dot-r{background:rgba(239,68,68,.15);color:var(--rd);border:1px solid rgba(239,68,68,.2)}
.bar-wrap{padding:6px 10px}
.bar-label{display:flex;justify-content:space-between;font-size:9px;color:var(--dm);margin-bottom:3px}
.bar{height:6px;background:var(--c2);border-radius:3px;overflow:hidden}
.bar-fill{height:100%;border-radius:3px;transition:width .5s}
.verdict{padding:8px 10px;font-size:11px;font-weight:700;text-align:center;letter-spacing:.3px}
.pos{margin:8px;background:linear-gradient(135deg,rgba(59,130,246,.08),transparent);border:1px solid rgba(59,130,246,.2);border-radius:8px;padding:10px}
.pos .big{font-size:22px;font-weight:700}
.prog{height:6px;background:var(--c2);border-radius:3px;overflow:hidden;margin:6px 0;position:relative}
.prog-fill{height:100%;border-radius:3px}
.tabs{display:flex;border-bottom:1px solid var(--bd);padding:0 8px;background:var(--c1)}
.tab{padding:7px 14px;font-size:10px;font-weight:700;color:var(--dm);border-bottom:2px solid transparent;cursor:pointer;text-transform:uppercase;letter-spacing:.5px}
.tab.on{color:var(--bl);border-color:var(--bl)}
.tc{margin:4px 8px;padding:8px 10px;border-radius:6px;border:1px solid;display:flex;align-items:center;gap:8px}
.tc.w{background:rgba(16,185,129,.04);border-color:rgba(16,185,129,.15)}
.tc.l{background:rgba(239,68,68,.04);border-color:rgba(239,68,68,.15)}
.H{display:none}
.ft{text-align:center;padding:6px;font-size:8px;color:#444;border-top:1px solid var(--bd)}
.two{display:grid;grid-template-columns:1fr 1fr;gap:0}
.two>.sect{margin:0;border-radius:0;border-right:none}.two>.sect:last-child{border-right:1px solid var(--bd)}
.ctx-row{display:grid;grid-template-columns:repeat(4,1fr);gap:0;margin:8px;background:var(--c1);border:1px solid var(--bd);border-radius:8px;overflow:hidden}
.ctx{text-align:center;padding:6px 4px;border-right:1px solid var(--bd)}.ctx:last-child{border:none}
.ctx .k{font-size:8px;color:var(--dm);text-transform:uppercase;letter-spacing:.3px}
.ctx .v{font-size:12px;font-weight:700;margin-top:1px}
</style></head><body>

<div class="hd">
  <h1><b>VISHAL RAJPUT</b> TRADE <span style="color:#444;font-size:9px" id="ver"></span></h1>
  <div class="tags" id="tags"></div>
</div>

<div id="position-area"></div>

<div class="tabs">
  <div class="tab on" data-t="sig" onclick="st('sig')">⚡ SIGNALS</div>
  <div class="tab" data-t="mkt" onclick="st('mkt')">📊 MARKET</div>
  <div class="tab" data-t="trd" onclick="st('trd')">📒 TRADES</div>
</div>

<div id="p-sig"></div>
<div id="p-mkt" class="H"></div>
<div id="p-trd" class="H"></div>

<div class="ft">Auto-refresh 10s · <span id="ts"></span></div>

<script>
function st(t){document.querySelectorAll('.tab').forEach(e=>e.classList.toggle('on',e.dataset.t===t));['sig','mkt','trd'].forEach(i=>document.getElementById('p-'+i).classList.toggle('H',i!==t))}

function esc(s){return String(s).replace(/</g,'&lt;')}

function tagC(v){
  if(v==='BULL')return 'tg';if(v==='BEAR')return 'tr';
  if(v==='SIDEWAYS'||v==='NEUTRAL')return 'ta';return 'tb'}

function render(d, trades){
  if(!d||!d.market){document.getElementById('p-sig').innerHTML='<div style="text-align:center;color:#444;padding:30px">Waiting for bot data...<br>Bot writes dashboard every scan cycle</div>';return}
  const mk=d.market,ce=d.ce||{},pe=d.pe||{},pos=d.position||{},td=d.today||{},str=d.straddle||{};

  // Version + tags
  document.getElementById('ver').textContent=d.version||'';
  let tags='<span class="tag '+(d.mode==='LIVE'?'tg':'tb')+'">'+esc(d.mode)+'</span>';
  tags+='<span class="tag '+(mk.dte<=1?'tr':'tb')+'">DTE '+mk.dte+'</span>';
  tags+='<span class="tag tb">ATM '+mk.atm+'</span>';
  if(mk.vix>0)tags+='<span class="tag '+(mk.vix>22?'tr':mk.vix>18?'ta':'tg')+'">VIX '+mk.vix+'</span>';
  if(mk.bias)tags+='<span class="tag '+tagC(mk.bias)+'">'+esc(mk.bias)+'</span>';
  if(mk.regime)tags+='<span class="tag '+(mk.regime.includes('TREND')?'tg':'ta')+'">'+esc(mk.regime)+'</span>';
  document.getElementById('tags').innerHTML=tags;

  // Position
  let ph='';
  if(pos.in_trade){
    const clr=pos.pnl>=0?'var(--gn)':'var(--rd)';
    const pct=pos.peak>0?Math.min(90,25+(pos.pnl/pos.peak)*55):30;
    ph='<div class="pos">'+
      '<div style="display:flex;justify-content:space-between;align-items:baseline">'+
      '<div><span style="color:var(--bl);font-weight:700">'+esc(pos.direction)+'</span> <span style="color:#555;font-size:10px">'+esc(pos.symbol)+'</span></div>'+
      '<span style="color:#555;font-size:9px">Score '+pos.score+' · Ph'+pos.phase+'</span></div>'+
      '<div style="margin:6px 0"><span class="big" style="color:'+clr+'">'+(pos.pnl>=0?'+':'')+pos.pnl+'pts</span>'+
      ' <span style="color:#555;font-size:11px">₹'+Math.round(pos.pnl*65)+'</span></div>'+
      '<div class="prog"><div class="prog-fill" style="width:25%;background:rgba(239,68,68,.3)"></div>'+
      '<div class="prog-fill" style="width:'+pct+'%;background:rgba(16,185,129,.3);position:absolute;left:25%;top:0;height:100%"></div></div>'+
      '<div style="display:flex;justify-content:space-between;font-size:9px;color:#555">'+
      '<span style="color:var(--rd)">SL ₹'+pos.sl+'</span><span>Entry ₹'+pos.entry+'</span><span style="color:var(--gn)">Peak +'+pos.peak+'</span></div>'+
      '<div style="display:flex;justify-content:space-between;font-size:9px;color:#444;margin-top:3px">'+
      '<span>Trough '+pos.trough+'pts</span><span>SL dist '+pos.sl_dist+'pts</span><span>'+pos.candles+' candles</span></div>'+
      '<div style="display:flex;justify-content:space-between;font-size:9px;color:#444;margin-top:3px">'+
      '<span>Trail: '+(pos.trail_tightened?'3m TIGHT ⚡':'5m WIDE')+'</span>'+
      '<span>RSI OB: '+(pos.rsi_overbought?'YES 🔥':'No')+'</span></div></div>';
  }
  // Today summary bar
  const dpnl=td.pnl||0;
  ph+='<div style="margin:8px;display:flex;gap:6px">'+
    '<div style="flex:1;background:var(--c1);border:1px solid var(--bd);border-radius:6px;padding:6px 8px;text-align:center">'+
    '<div style="font-size:8px;color:#555">DAY P&L</div>'+
    '<div style="font-size:16px;font-weight:700;color:'+(dpnl>=0?'var(--gn)':'var(--rd)')+'">'+(dpnl>=0?'+':'')+dpnl+'pts</div>'+
    '<div style="font-size:9px;color:#555">₹'+Math.round(dpnl*65)+'</div></div>'+
    '<div style="flex:1;background:var(--c1);border:1px solid var(--bd);border-radius:6px;padding:6px 8px;text-align:center">'+
    '<div style="font-size:8px;color:#555">TRADES</div>'+
    '<div style="font-size:16px;font-weight:700">'+td.trades+'</div>'+
    '<div style="font-size:9px;color:#555">W'+td.wins+' L'+td.losses+(td.streak>=2?' 🔴'+td.streak:'')+'</div></div>'+
    '<div style="flex:1;background:var(--c1);border:1px solid var(--bd);border-radius:6px;padding:6px 8px;text-align:center">'+
    '<div style="font-size:8px;color:#555">STATUS</div>'+
    '<div style="font-size:16px;font-weight:700">'+(td.paused?'⏸':'⚡')+'</div>'+
    '<div style="font-size:9px;color:#555">'+(td.paused?'PAUSED':mk.market_open?'SCANNING':'CLOSED')+'</div></div></div>';
  document.getElementById('position-area').innerHTML=ph;

  // ── SIGNAL TAB ──
  function signalBlock(label, sig, minSpread){
    const g=sig.gate_3m||{},e=sig.entry_1m||{};
    const dotH=(ok,l)=>'<div class="dot dot-'+(ok?'g':'r')+'">'+l+'</div>';
    const barPct=minSpread>0?Math.min(100,Math.max(0,sig.spread_1m/minSpread*100)):0;
    const barClr=barPct>=100?'var(--gn)':barPct>=70?'var(--am)':'var(--rd)';
    const vClr=sig.verdict==='FIRED'?'var(--gn)':sig.verdict==='READY'?'var(--cy)':sig.verdict.startsWith('3M')?'var(--rd)':'var(--am)';
    let h='<div class="sect"><div class="sh">'+label+' SIGNAL'+(sig.ltp>0?' · ₹'+sig.ltp:'')+'</div>';
    // 3-min gate
    h+='<div class="row"><div class="k">3-MIN GATE</div><div class="v" style="color:'+(g.met>=3?'var(--gn)':'var(--rd)')+'">'+g.met+'/4'+(g.met>=3?' ✅':' ❌')+'</div></div>';
    h+='<div class="gate">'+dotH(g.ema,'E')+dotH(g.body,'B')+dotH(g.rsi,'R')+dotH(g.price,'P')+'</div>';
    if(g.rsi_val>0)h+='<div class="row"><div class="k">3m RSI</div><div class="v">'+g.rsi_val+'</div></div>';
    if(g.spread!=0)h+='<div class="row"><div class="k">3m Spread</div><div class="v" style="color:'+(g.spread>0?'var(--gn)':'var(--rd)')+'">'+(g.spread>0?'+':'')+g.spread+'</div></div>';
    // 1-min spread bar
    h+='<div class="bar-wrap"><div class="bar-label"><span>1m SPREAD</span><span style="color:'+barClr+'">'+(sig.spread_1m>0?'+':'')+sig.spread_1m+' / +'+minSpread+'</span></div>';
    h+='<div class="bar"><div class="bar-fill" style="width:'+barPct+'%;background:'+barClr+'"></div></div></div>';
    // 1-min entry
    const rClr=(e.rsi_ok&&e.rsi_rising)?'var(--gn)':e.rsi>65?'var(--rd)':'var(--am)';
    h+='<div class="row"><div class="k">BODY</div><div class="v" style="color:'+(e.body_ok?'var(--gn)':'var(--rd)')+'">'+e.body_pct+'%'+(e.body_ok?' ✅':' ❌')+'</div></div>';
    h+='<div class="row"><div class="k">RSI</div><div class="v" style="color:'+rClr+'">'+e.rsi+(e.rsi_rising?' ↑':' ↓')+(e.rsi_ok?' ✅':' ❌')+'</div></div>';
    h+='<div class="row"><div class="k">VOLUME</div><div class="v" style="color:'+(e.vol_ok?'var(--gn)':'var(--rd)')+'">'+e.vol+'x'+(e.vol_ok?' ✅':' ❌')+'</div></div>';
    // Score
    h+='<div class="row"><div class="k">SCORE</div><div class="v" style="color:'+(sig.score>=sig.score_min?'var(--gn)':'var(--rd)')+'">'+sig.score+'/'+sig.score_min+'</div></div>';
    // Greeks
    if(sig.greeks&&sig.greeks.delta)h+='<div class="row"><div class="k">GREEKS</div><div class="v" style="font-size:10px">Δ'+sig.greeks.delta+' IV'+sig.greeks.iv+'% Θ'+sig.greeks.theta+'</div></div>';
    // Verdict
    h+='<div class="verdict" style="color:'+vClr+'">'+esc(sig.verdict)+'</div></div>';
    return h}

  document.getElementById('p-sig').innerHTML=
    '<div class="two" style="margin:8px;gap:6px;display:grid;grid-template-columns:1fr 1fr">'+
    signalBlock('CE',ce,ce.spread_1m_min||6)+signalBlock('PE',pe,pe.spread_1m_min||4)+'</div>';

  // ── MARKET TAB ──
  let mh='<div class="ctx-row">'+
    '<div class="ctx"><div class="k">SPOT</div><div class="v" style="color:var(--bl)">'+mk.spot+'</div></div>'+
    '<div class="ctx"><div class="k">EMA9</div><div class="v" style="color:var(--gn)">'+mk.spot_ema9+'</div></div>'+
    '<div class="ctx"><div class="k">EMA21</div><div class="v" style="color:var(--am)">'+mk.spot_ema21+'</div></div>'+
    '<div class="ctx"><div class="k">SPREAD</div><div class="v" style="color:'+(mk.spot_spread>0?'var(--gn)':'var(--rd)')+'">'+(mk.spot_spread>0?'+':'')+mk.spot_spread+'</div></div></div>';
  mh+='<div class="ctx-row">'+
    '<div class="ctx"><div class="k">RSI</div><div class="v" style="color:'+(mk.spot_rsi>60?'var(--gn)':mk.spot_rsi<40?'var(--rd)':'var(--am)')+'">'+mk.spot_rsi+'</div></div>'+
    '<div class="ctx"><div class="k">H.RSI</div><div class="v" style="color:'+(mk.hourly_rsi>70?'var(--rd)':mk.hourly_rsi<30?'var(--gn)':'')+'">'+mk.hourly_rsi+'</div></div>'+
    '<div class="ctx"><div class="k">GAP</div><div class="v">'+(mk.gap>0?'+':'')+mk.gap+'</div></div>'+
    '<div class="ctx"><div class="k">SESSION</div><div class="v" style="font-size:10px">'+esc(mk.session)+'</div></div></div>';
  mh+='<div class="ctx-row">'+
    '<div class="ctx"><div class="k">FIB</div><div class="v" style="font-size:10px">'+esc(mk.fib_nearest||'—')+'</div></div>'+
    '<div class="ctx"><div class="k">FIB DIST</div><div class="v">'+(mk.fib_distance>0?'+':'')+mk.fib_distance+'</div></div>'+
    '<div class="ctx"><div class="k">STRADDLE</div><div class="v">'+(str.captured?'₹'+str.open:'—')+'</div></div>'+
    '<div class="ctx"><div class="k">EXPIRY</div><div class="v" style="font-size:10px">'+esc(mk.expiry||'—')+'</div></div></div>';
  document.getElementById('p-mkt').innerHTML=mh;

  // ── TRADES TAB ──
  let th='';
  if(!trades||!trades.length){th='<div style="text-align:center;color:#444;padding:30px">No trades today</div>'}
  else{
    let cum=0;
    th=trades.map(t=>{
      const pts=parseFloat(t.pnl_pts||0),w=pts>0;cum+=pts;
      const pk=parseFloat(t.peak_pnl||0),tr=parseFloat(t.trough_pnl||0);
      return '<div class="tc '+(w?'w':'l')+'">'+
        '<div style="font-size:16px">'+(w?'✅':'❌')+'</div>'+
        '<div style="flex:1">'+
        '<div style="font-weight:700;font-size:11px;color:'+(t.direction==='CE'?'var(--gn)':'var(--rd)')+'">'+
        esc(t.direction)+' <span style="color:#555;font-size:9px">'+esc(t.entry_time)+' Ph'+(t.exit_phase||'')+' '+esc(t.session||'')+'</span></div>'+
        '<div style="font-size:9px;color:#555">₹'+t.entry_price+' → ₹'+t.exit_price+' · '+esc((t.exit_reason||'').replace(/_/g,' '))+'</div></div>'+
        '<div style="text-align:right"><div style="font-weight:700;color:'+(w?'var(--gn)':'var(--rd)')+'">'+(w?'+':'')+pts.toFixed(1)+'pts</div>'+
        '<div style="font-size:8px;color:#555">↑'+pk.toFixed(1)+' ↓'+tr.toFixed(1)+'</div></div></div>'}).join('');
    th+='<div style="text-align:center;padding:8px;font-weight:700;color:'+(cum>=0?'var(--gn)':'var(--rd)')+'">Net: '+(cum>=0?'+':'')+cum.toFixed(1)+'pts ₹'+Math.round(cum*65)+'</div>'}
  document.getElementById('p-trd').innerHTML=th;
  document.getElementById('ts').textContent=d.ts||new Date().toLocaleTimeString('en-IN')}

async function go(){
  try{
    const[d,t]=await Promise.all([fetch('/api/dashboard').then(r=>r.json()),fetch('/api/trades').then(r=>r.json())]);
    render(d,t)}catch(e){console.error(e)}
}
go();setInterval(go,10000);
</script></body></html>"""

class H(BaseHTTPRequestHandler):
    def log_message(self,*a):pass
    def _j(self,d):
        self.send_response(200)
        self.send_header("Content-Type","application/json")
        self.send_header("Access-Control-Allow-Origin","*")
        self.end_headers()
        self.wfile.write(json.dumps(d,default=str).encode())
    def do_GET(self):
        p=urlparse(self.path).path
        if p in("/","/dashboard"):
            self.send_response(200)
            self.send_header("Content-Type","text/html")
            self.end_headers()
            self.wfile.write(HTML.encode())
        elif p=="/api/dashboard":self._j(_read_dash())
        elif p=="/api/trades":self._j(_read_trades())
        else:self.send_error(404)

if __name__=="__main__":
    s=HTTPServer(("0.0.0.0",PORT),H)
    print("VRL War Room v12.14 — http://0.0.0.0:"+str(PORT))
    try:s.serve_forever()
    except KeyboardInterrupt:s.server_close()
'''

wf("VRL_WEB.py", web_code)

print("\n" + "=" * 55)
print("  WAR ROOM BUILD COMPLETE")
print("=" * 55)
print()
print("VRL_MAIN.py: _write_dashboard() writes ~/state/vrl_dashboard.json")
print("VRL_WEB.py:  Reads that file. Zero calculations. Dumb renderer.")
print()
print("SIGNALS TAB: CE vs PE side-by-side")
print("  3m gate dots (E B R P) + spread bar + body/RSI/vol + score + verdict")
print("MARKET TAB: Spot EMA RSI + Hourly RSI + Gap + Fib + Straddle")
print("TRADES TAB: Trade cards with peak/trough")
print("POSITION: Shows when in trade with progress bar")
