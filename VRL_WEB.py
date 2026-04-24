#!/usr/bin/env python3
"""
VRL_WEB.py — VISHAL RAJPUT TRADE War Room v13.9
Dashboard server with admin login + subscriber token access.
"""
import csv, json, os, hashlib, secrets, time, threading
from datetime import date, datetime, timedelta
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
from http.cookies import SimpleCookie

PORT = 8080
BASE = os.path.expanduser("~")
# State files live inside the repo so AUTH/MAIN/WEB all agree. BUG-016.
import VRL_DATA as _D
STATE_DIR = _D.STATE_DIR
DASH_FILE = os.path.join(STATE_DIR, "vrl_dashboard.json")
TRADE_LOG = os.path.join(BASE, "lab_data", "vrl_trade_log.csv")

# ── AUTH CONFIG ──
ADMIN_USER = "vishal"
_env_pass = ""
try:
    with open(os.path.join(BASE, ".env")) as _ef:
        for _line in _ef:
            if _line.strip().startswith("VRL_DASHBOARD_PASS="):
                _env_pass = _line.strip().split("=", 1)[1].strip()
except Exception:
    pass
ADMIN_PASS_HASH = hashlib.sha256(_env_pass.encode()).hexdigest() if _env_pass else ""

# Sessions: {token: {"user": str, "role": "admin"|"subscriber", "expires": datetime}}
_sessions = {}
_sessions_lock = threading.Lock()

# Login rate limit: {ip: [timestamps]}
_login_attempts = {}
_LOGIN_LIMIT = 5
_LOGIN_BLOCK_SECS = 900  # 15 min

def _get_session(cookie_header):
    """Extract session from cookie header. Returns session dict or None."""
    if not cookie_header:
        return None
    try:
        c = SimpleCookie()
        c.load(cookie_header)
        if "vrl_session" in c:
            token = c["vrl_session"].value
            with _sessions_lock:
                sess = _sessions.get(token)
                if sess and datetime.now() < sess["expires"]:
                    return sess
                if sess:
                    del _sessions[token]
    except Exception:
        pass
    return None

def _create_session(user, role="admin", days=30):
    """Create session, return token."""
    token = secrets.token_hex(16)
    with _sessions_lock:
        _sessions[token] = {
            "user": user, "role": role,
            "expires": datetime.now() + timedelta(days=days),
        }
    return token

def _cleanup_sessions():
    """Remove expired sessions."""
    with _sessions_lock:
        expired = [k for k, v in _sessions.items() if datetime.now() > v["expires"]]
        for k in expired:
            del _sessions[k]

# Clean sessions every hour
def _session_cleaner():
    while True:
        time.sleep(3600)
        _cleanup_sessions()
threading.Thread(target=_session_cleaner, daemon=True).start()

LOGIN_HTML = """<!DOCTYPE html><html><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>VRL Login</title><style>
*{margin:0;padding:0;box-sizing:border-box}
body{background:#f5f0e8;font-family:'DM Sans',sans-serif;display:flex;justify-content:center;align-items:center;min-height:100vh}
.box{background:#fff;border-radius:16px;padding:40px;width:340px;box-shadow:0 4px 24px rgba(0,0,0,0.08)}
h1{font-size:18px;font-weight:700;margin-bottom:6px}h1 span{color:#e85d04}
.sub{color:#888;font-size:12px;margin-bottom:24px}
input{width:100%;padding:12px;border:1px solid #ddd;border-radius:8px;font-size:14px;margin-bottom:12px;font-family:inherit}
input:focus{outline:none;border-color:#e85d04}
button{width:100%;padding:12px;background:#e85d04;color:#fff;border:none;border-radius:8px;font-size:14px;font-weight:700;cursor:pointer}
button:hover{background:#d45003}
.err{color:#e33;font-size:12px;margin-bottom:12px;display:none}
</style></head><body>
<div class="box"><h1><span>VISHAL RAJPUT</span> TRADE</h1>
<div class="sub">Dashboard Login</div>
<div class="err" id="err">ERR_MSG</div>
<form method="POST" action="/login">
<input name="username" placeholder="Username" required autofocus>
<input name="password" type="password" placeholder="Password" required>
<button type="submit">Login</button></form></div></body></html>"""

TOKEN_ERROR_HTML = """<!DOCTYPE html><html><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>VRL Access</title><style>
body{background:#f5f0e8;font-family:'DM Sans',sans-serif;display:flex;justify-content:center;align-items:center;min-height:100vh}
.box{background:#fff;border-radius:16px;padding:40px;width:380px;box-shadow:0 4px 24px rgba(0,0,0,0.08);text-align:center}
h2{font-size:16px;margin-bottom:8px}
.msg{color:#888;font-size:13px}
</style></head><body><div class="box"><h2>MSG_TITLE</h2><div class="msg">MSG_BODY</div></div></body></html>"""

def _today_trade_summary():
    """v15.2 BUG-2: dashboard trade count is the single source of truth.
    Reads the CSV trade log directly and computes counts/PNL for today.
    Falls back gracefully on missing/corrupt file."""
    today = date.today().isoformat()
    summary = {"trades": 0, "wins": 0, "losses": 0,
               "pnl": 0.0, "pnl_rs": 0.0,
               "gross_pnl_rs": 0.0, "total_charges": 0.0, "net_pnl_rs": 0.0}
    if not os.path.isfile(TRADE_LOG):
        return summary
    try:
        with open(TRADE_LOG) as f:
            for r in csv.DictReader(f):
                if r.get("date") != today:
                    continue
                summary["trades"] += 1
                try:
                    p = float(r.get("pnl_pts", 0) or 0)
                    summary["pnl"] += p
                    if p > 0:
                        summary["wins"] += 1
                    else:
                        summary["losses"] += 1
                except Exception:
                    pass
                try:
                    summary["pnl_rs"] += float(r.get("pnl_rs", 0) or 0)
                except Exception:
                    pass
                try:
                    summary["gross_pnl_rs"]  += float(r.get("gross_pnl_rs", 0) or 0)
                    summary["total_charges"] += float(r.get("total_charges", 0) or 0)
                    summary["net_pnl_rs"]    += float(r.get("net_pnl_rs", 0) or 0)
                except Exception:
                    pass
    except Exception:
        pass
    summary["pnl"]            = round(summary["pnl"], 1)
    summary["pnl_rs"]         = round(summary["pnl_rs"], 0)
    summary["gross_pnl_rs"]   = round(summary["gross_pnl_rs"], 0)
    summary["total_charges"]  = round(summary["total_charges"], 0)
    summary["net_pnl_rs"]     = round(summary["net_pnl_rs"], 0)
    return summary


def _read_dash():
    """v15.2: reads vrl_dashboard.json but ALWAYS overlays the today block
    with a fresh recount from trade_log.csv. This is the single source of
    truth — fixes BUG-2 dashboard vs state count drift."""
    data = {"version": _D.VERSION}
    if os.path.isfile(DASH_FILE):
        try:
            with open(DASH_FILE) as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            try:
                import logging
                logging.getLogger("vrl_web").debug("[WEB] _read_dash error: " + str(e))
            except Exception:
                pass
            data = {}
    # Always inject current VERSION so dashboard shows correct version
    data["version"] = _D.VERSION
    # v15.2 BUG-2: reconcile today block from trade_log.csv (single source)
    csv_summary = _today_trade_summary()
    today_block = data.get("today") or {}
    today_block.update({
        "trades":         csv_summary["trades"],
        "wins":           csv_summary["wins"],
        "losses":         csv_summary["losses"],
        "pnl":            csv_summary["pnl"],
        "pnl_rs":         csv_summary["pnl_rs"],
        "gross_pnl_rs":   csv_summary["gross_pnl_rs"],
        "total_charges":  csv_summary["total_charges"],
        "net_pnl_rs":     csv_summary["net_pnl_rs"],
    })
    data["today"] = today_block
    return data

import glob as _glob

_FOLDERS = {
    "trade_log":    ("📒 Trade Log",              os.path.join(BASE, "lab_data")),
    "spot":         ("📈 Spot (1m/5m/15m/D)",     os.path.join(BASE, "lab_data", "spot")),
    "options_3min": ("📊 Options 3-Min CE+PE",    os.path.join(BASE, "lab_data", "options_3min")),
    "options_1min": ("📊 Options 1m/5m/15m/Scan", os.path.join(BASE, "lab_data", "options_1min")),
    "reports":      ("📑 Daily Summary",          os.path.join(BASE, "lab_data", "reports")),
    "research":     ("🔭 Zones + Research",       os.path.join(BASE, "research")),
    "state":        ("⚙️ State + Config",         STATE_DIR),
    "logs_live":    ("📋 Live Logs",              os.path.join(BASE, "logs", "live")),
    "logs_lab":     ("📋 Lab Logs",               os.path.join(BASE, "logs", "lab")),
    "logs_auth":    ("📋 Auth Logs",              os.path.join(BASE, "logs", "auth")),
    "logs_errors":  ("📋 Error Logs",             os.path.join(BASE, "logs", "errors")),
    "logs_health":  ("📋 Health Logs",            os.path.join(BASE, "logs", "health")),
}

def _list_files(folder=""):
    if not folder:
        return {"folders": [{"key": k, "name": v[0]} for k, v in _FOLDERS.items()]}
    info = _FOLDERS.get(folder)
    if not info or not os.path.isdir(info[1]):
        return {"files": [], "folder": folder}
    files = []
    for f in sorted(os.listdir(info[1]), reverse=True):
        fp = os.path.join(info[1], f)
        if os.path.isfile(fp):
            size = os.path.getsize(fp)
            if size > 0:
                files.append({
                    "name": f,
                    "size": round(size / 1024, 1),
                    "path": folder + "/" + f,
                })
    return {"files": files[:30], "folder": folder, "folder_name": info[0]}


def _read_multitf():
    spot_dir = os.path.join(BASE, "lab_data", "spot")
    opt3_dir = os.path.join(BASE, "lab_data", "options_3min")
    opt1_dir = os.path.join(BASE, "lab_data", "options_1min")
    def _latest(d, p):
        fs = sorted(_glob.glob(os.path.join(d, p + "*.csv")))
        if fs: return fs[-1]
        a = os.path.join(d, p + ".csv")
        return a if os.path.isfile(a) else None
    def _last(path):
        if not path or not os.path.isfile(path): return None
        try:
            with open(path) as f: rows = list(csv.DictReader(f))
            return rows[-1] if rows else None
        except Exception: return None
    def _lasttype(path, t):
        if not path or not os.path.isfile(path): return None
        try:
            with open(path) as f: rows = list(csv.DictReader(f))
            for r in reversed(rows):
                if r.get("type") == t: return r
            return None
        except Exception: return None
    def _f(r, k, d=0):
        try: return round(float(r.get(k, d)), 1)
        except (TypeError, ValueError): return d
    def _f3(r, k, d=0):
        try: return round(float(r.get(k, d)), 3)
        except (TypeError, ValueError): return d
    spot = []
    for label, prefix in [("1m","nifty_spot_1min"),("3m","nifty_spot_3min_"),("5m","nifty_spot_5min_"),("15m","nifty_spot_15min_"),("D","nifty_spot_daily")]:
        r = _last(_latest(spot_dir, prefix))
        if not r and label == "3m":
            # 3m spot comes from get_spot_indicators, not CSV — use dashboard JSON
            try:
                d = _read_dash()
                mk = d.get("market", {})
                r = {"adx": mk.get("spot_adx_3m", 0), "rsi": mk.get("spot_rsi", 0), "ema_spread": mk.get("spot_spread", 0), "regime": mk.get("regime", "")}
            except Exception:
                r = None
        if r: spot.append({"tf":label,"adx":_f(r,"adx"),"rsi":_f(r,"rsi"),"spread":_f(r,"ema_spread",_f(r,"spread")),"regime":r.get("regime","")})
        else: spot.append({"tf":label,"adx":0,"rsi":0,"spread":0,"regime":""})
    ce = []; pe = []; ce_strike = 0; pe_strike = 0
    for label, d, prefix in [("1m",opt1_dir,"nifty_option_1min_"),("3m",opt3_dir,"nifty_option_3min_"),("5m",opt1_dir,"nifty_option_5min_"),("15m",opt1_dir,"nifty_option_15min_")]:
        p = _latest(d, prefix)
        for side, arr in [("CE",ce),("PE",pe)]:
            r = _lasttype(p, side)
            if r:
                arr.append({"tf":label,"adx":_f(r,"adx"),"rsi":_f(r,"rsi"),"iv":_f(r,"iv_pct"),"delta":_f3(r,"delta"),"ltp":_f(r,"close"),"body":_f(r,"body_pct"),"spread":_f(r,"ema_spread",_f(r,"ema9_gap")),"strike":r.get("strike","")})
                if side == "CE" and not ce_strike: ce_strike = r.get("strike", "")
                if side == "PE" and not pe_strike: pe_strike = r.get("strike", "")
            else: arr.append({"tf":label,"adx":0,"rsi":0,"iv":0,"delta":0,"ltp":0,"body":0,"spread":0,"strike":""})
    # Override LTP with current websocket price (same across all TFs)
    try:
        d = _read_dash()
        ce_live = d.get("ce", {}).get("ltp", 0)
        pe_live = d.get("pe", {}).get("ltp", 0)
        if ce_live:
            for row in ce: row["ltp"] = round(ce_live, 1)
        if pe_live:
            for row in pe: row["ltp"] = round(pe_live, 1)
    except Exception:
        pass
    return {"spot":spot,"ce":ce,"pe":pe,"ce_strike":ce_strike,"pe_strike":pe_strike}

def _read_trades():
    if not os.path.isfile(TRADE_LOG): return []
    today = date.today().isoformat()
    trades = []
    try:
        with open(TRADE_LOG) as f:
            for r in csv.DictReader(f):
                if r.get("date","").strip() == today:
                    try: trades.append({k: r.get(k,"") for k in r})
                    except Exception: pass
    except Exception: pass
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
.pos{margin:8px;border-radius:8px;padding:10px}
.pos .big{font-size:20px;font-weight:700}
.prog{height:6px;background:var(--c2);border-radius:3px;overflow:hidden;margin:6px 0;position:relative}
.prog-fill{height:100%;border-radius:3px;transition:width .5s}
.tabs{display:flex;border-bottom:1px solid var(--bd);padding:0 8px;background:var(--c1)}
.tab{padding:7px 14px;font-size:10px;font-weight:700;color:var(--dm);border-bottom:2px solid transparent;cursor:pointer;text-transform:uppercase;letter-spacing:.5px}
.tab.on{color:var(--bl);border-color:var(--bl)}
.tc{margin:4px 8px;padding:8px 10px;border-radius:6px;border:1px solid;display:flex;align-items:flex-start;gap:8px}
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
.pos-lot{font-size:10px;color:#aaa;margin-top:2px}
.pos-meta{display:flex;gap:12px;font-size:9px;color:#555;margin-top:4px}
.day-bar{margin:8px;display:flex;gap:6px}
.day-box{flex:1;background:var(--c1);border:1px solid var(--bd);border-radius:6px;padding:6px 8px;text-align:center}
.day-box .dk{font-size:8px;color:#555}
.day-box .dv{font-size:15px;font-weight:700;margin:2px 0}
.day-box .ds{font-size:9px;color:#555}
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
  <div class="tab" data-t="fil" onclick="window.location.href='/files'">📁 FILES</div>
</div>

<div id="p-sig"></div>
<div id="p-mkt" class="H"></div>
<div id="p-trd" class="H"></div>
<div id="p-fil" class="H"></div>

<div class="ft">Auto-refresh 10s · <span id="ts"></span></div>


<script>
function st(t){document.querySelectorAll('.tab').forEach(e=>e.classList.toggle('on',e.dataset.t===t));['sig','mkt','trd','fil'].forEach(i=>document.getElementById('p-'+i).classList.toggle('H',i!==t))}

function esc(s){return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')}

function tagC(v){
  if(v==='BULL')return 'tg';if(v==='BEAR')return 'tr';
  if(v==='SIDEWAYS'||v==='NEUTRAL')return 'ta';return 'tb'}

function shortSym(sym, dir, strike){
  if(dir && strike) return dir + ' ' + strike;
  if(!sym) return '';
  var m = sym.match(/(CE|PE)$/);
  if(m){var s=sym.replace(/^NIFTY\d+/,'').replace(/(CE|PE)$/,'');return m[1]+' '+s;}
  return sym;
}

function render(d, trades, zones, mtf){ if(!d || !d.market){document.getElementById('p-sig').innerHTML='<div style="text-align:center;color:#555;padding:20px">Waiting for bot data... (FILES tab works)</div>';document.getElementById('position-area').innerHTML='';return}

  const mk=d.market,ce=d.ce||{},pe=d.pe||{},pos=d.position||{},td=d.today||{},str=d.straddle||{};

  // Version + tags
  document.getElementById('ver').textContent=d.version||'';
  let tags='<span class="tag '+(d.mode==='LIVE'?'tg':'tb')+'">'+esc(d.mode||'')+'</span>';
  tags+='<span class="tag '+(mk.dte<=1?'tr':'tb')+'">DTE '+(mk.dte||0)+'</span>';
  tags+='<span class="tag tb">CE '+(mk.locked_ce||mk.atm)+' · PE '+(mk.locked_pe||mk.atm)+' \uD83D\uDD12</span>';
  if(mk.vix>0)tags+='<span class="tag '+(mk.vix>22?'tr':mk.vix>18?'ta':'tg')+'">VIX '+mk.vix+'</span>';
  if(mk.bias&&mk.bias!=='')tags+='<span class="tag '+tagC(mk.bias)+'">'+esc(mk.bias)+'</span>';
  if(mk.regime){var rc=mk.regime.includes('TREND')?'tg':mk.regime==='NEUTRAL'?'ta':'tr';tags+='<span class="tag '+rc+'">'+esc(mk.regime)+'</span>';}
  if(mk.market_open&&!mk.indicators_warm)tags+='<span class="tag tr">WARMUP</span>';
  document.getElementById('tags').innerHTML=tags;

  // ── POSITION CARD ──
  var ph='';
  if(pos.in_trade){
    var sym=shortSym(pos.symbol,pos.direction,pos.strike);
    var pnl=parseFloat(pos.pnl||0);
    var peak=parseFloat(pos.peak||0);
    var entry=parseFloat(pos.entry||0);
    var ltp=parseFloat(pos.ltp||0);
    var sl=parseFloat(pos.sl||0);
    var floor=parseFloat(pos.current_floor||0);
    var rsi=parseFloat(pos.current_rsi||0);
    var candles=pos.candles||0;
    var lot1=pos.lot1_active;
    var lot2=pos.lot2_active;
    var split=pos.lots_split;
    var activeLots=(lot1?1:0)+(lot2?1:0);
    var pnlRs=Math.round(pnl*65*activeLots);
    var pnlClr=pnl>=0?'var(--gn)':'var(--rd)';
    // RSI progress bar
    var rsiPct=Math.min(100,(rsi/70)*100);
    var rsiBarClr=rsi>=70?'var(--rd)':rsi>=65?'var(--am)':'var(--gn)';
    // State label
    var stateIcon,stateLabel;
    if(!split){stateIcon='\uD83D\uDFE2';stateLabel=sym+' IN TRADE';}
    else if(lot1&&lot2){stateIcon='\u26A1';stateLabel=sym+' SPLIT';}
    else{stateIcon='\uD83C\uDFC3';stateLabel=sym+' LOT2 RIDING';}
    var posClr=pos.direction==='CE'?'rgba(59,130,246,.1)':'rgba(239,68,68,.08)';
    var posBd=pos.direction==='CE'?'rgba(59,130,246,.25)':'rgba(239,68,68,.2)';
    ph='<div class="pos" style="background:linear-gradient(135deg,'+posClr+',transparent);border:1px solid '+posBd+'">';
    ph+='<div style="font-size:13px;font-weight:700;margin-bottom:4px">'+stateIcon+' '+esc(stateLabel)+'</div>';
    ph+='<div style="margin:3px 0"><span class="big" style="color:'+pnlClr+'">'+(pnl>=0?'+':'')+pnl.toFixed(1)+'pts</span>';
    ph+=' <span style="color:#888;font-size:11px">&#x20B9;'+pnlRs.toLocaleString('en-IN')+'</span>';
    ph+='<span style="color:#555;font-size:10px;float:right">Entry &#x20B9;'+entry+' \u2192 &#x20B9;'+ltp+'</span></div>';
    // RSI progress bar
    ph+='<div class="prog"><div class="prog-fill" style="width:'+rsiPct.toFixed(0)+'%;background:'+rsiBarClr+'"></div></div>';
    ph+='<div style="display:flex;justify-content:space-between;font-size:9px;color:#555;margin-bottom:4px">';
    ph+='<span>RSI '+rsi.toFixed(0)+' (cap 75)</span><span>'+rsiPct.toFixed(0)+'%</span></div>';
    // Lot status
    if(!split){
      ph+='<div class="pos-lot">LOT1: '+(lot1?'<span style="color:var(--gn)">Active</span>':'<span style="color:#555">SOLD</span>')+' &nbsp; SL &#x20B9;'+sl+' (floor +'+floor.toFixed(0)+')</div>';
      ph+='<div class="pos-lot">LOT2: '+(lot2?'<span style="color:var(--gn)">Active</span>':'<span style="color:#555">SOLD</span>')+' &nbsp; SL &#x20B9;'+sl+' (floor +'+floor.toFixed(0)+')</div>';
    } else if(lot1&&lot2){
      ph+='<div class="pos-lot">LOT1: <span style="color:var(--am)">Floor SL</span> &#x20B9;'+sl+'</div>';
      ph+='<div class="pos-lot">LOT2: <span style="color:var(--cy)">ATR Trail</span> &#x20B9;'+sl+'</div>';
    } else {
      ph+='<div class="pos-lot">Lot1: <span style="color:#555">SOLD</span> +'+peak.toFixed(1)+'pts \u2705</div>';
      ph+='<div class="pos-lot">LOT2: <span style="color:var(--cy)">ATR Trail</span> &#x20B9;'+sl+'</div>';
    }
    ph+='<div class="pos-meta"><span>Peak: +'+(peak||0).toFixed(1)+'</span><span>RSI: '+rsi.toFixed(0)+'</span><span>'+candles+'min</span></div>';
    ph+='</div>';
  }

  // ── TODAY SUMMARY BAR ──
  var dpnl=parseFloat(td.pnl||0);
  var wins=parseInt(td.wins||0),losses=parseInt(td.losses||0);
  var totalT=wins+losses;
  var wr=totalT>0?Math.round((wins/totalT)*100):0;
  var dpnlRs=Math.round(dpnl*65);
  ph+='<div class="day-bar">';
  ph+='<div class="day-box"><div class="dk">DAY P&L</div>';
  ph+='<div class="dv" style="color:'+(dpnl>=0?'var(--gn)':'var(--rd)')+'">'+(dpnl>=0?'+':'')+dpnl.toFixed(1)+'pts</div>';
  ph+='<div class="ds">&#x20B9;'+(dpnlRs>=0?'+':'')+dpnlRs+'</div></div>';
  ph+='<div class="day-box"><div class="dk">TRADES</div>';
  ph+='<div class="dv">'+(td.trades||0)+'</div>';
  ph+='<div class="ds">'+wins+'W '+losses+'L &nbsp; WR '+wr+'%'+(td.streak>=2?' \uD83D\uDD34'+td.streak:'')+'</div></div>';
  ph+='<div class="day-box"><div class="dk">STATUS</div>';
  ph+='<div class="dv">'+(td.paused?'\u23F8':'\u26A1')+'</div>';
  ph+='<div class="ds">'+(td.paused?'PAUSED':mk.market_open?'SCANNING':'CLOSED')+'</div></div>';
  ph+='</div>';
  document.getElementById('position-area').innerHTML=ph;

  // ── SIGNAL TAB ──
  function sigVerdict(sig, label, cd){
    // Cooldown overrides everything
    if(cd && cd.remaining > 0){
      return {txt:'\u23F3 COOLDOWN '+cd.remaining+'min \u2014 '+label+' blocked', clr:'var(--am)'};}
    var reasons=[];
    if(!sig.ema_ok) reasons.push('EMA '+(sig.ema_gap>0?'+':'')+sig.ema_gap+' not aligned');
    if(!sig.rsi_ok) reasons.push('RSI '+sig.rsi+' not rising');
    if(!sig.candle_green) reasons.push('candle red');
    if(!sig.gap_widening) reasons.push('gap shrinking');
    if(reasons.length===0){
      if(sig.verdict==='FIRED') return {txt:'FIRED \u2705',clr:'var(--gn)'};
      return {txt:'EMA +'+(sig.ema_gap||0)+' RSI '+(sig.rsi||0)+' \u2014 READY',clr:'var(--cy)'};
    }
    return {txt:reasons[0].charAt(0).toUpperCase()+reasons[0].slice(1),clr:'var(--am)'};
  }

  function signalBlock(label, sig){
    var strike=sig.strike||mk.atm;
    var ltp=sig.ltp||0;
    var emaClr=sig.ema_ok?'var(--gn)':'var(--rd)';
    var rsiClr=sig.rsi_ok?'var(--gn)':'var(--rd)';
    var cd=d.cooldown&&d.cooldown[label]?d.cooldown[label]:(d.cooldown&&d.cooldown.remaining?d.cooldown:null);
    var vd=sigVerdict(sig,label,cd);
    var h='<div class="sect"><div class="sh">'+label+' '+strike+' \xB7 &#x20B9;'+ltp+'</div>';
    h+='<div class="row"><div class="k">EMA9</div><div class="v">'+(sig.ema9||0)+'</div></div>';
    h+='<div class="row"><div class="k">EMA21</div><div class="v">'+(sig.ema21||0)+'</div></div>';
    h+='<div class="row"><div class="k">EMA GAP</div><div class="v" style="color:'+emaClr+'">'+(sig.ema_gap>0?'+':'')+sig.ema_gap+(sig.ema_ok?' \u2705':' \u274C')+'</div></div>';
    h+='<div class="row"><div class="k">RSI</div><div class="v" style="color:'+rsiClr+'">'+sig.rsi+(sig.rsi_ok?' \u2191 \u2705':' \u274C')+'</div></div>';
    var gcClr=sig.candle_green?'var(--gn)':'var(--rd)';
    h+='<div class="row"><div class="k">CANDLE</div><div class="v" style="color:'+gcClr+'">'+(sig.candle_green?'GREEN \u2705':'RED \u274C')+'</div></div>';
    var gwClr=sig.gap_widening?'var(--gn)':'var(--rd)';
    h+='<div class="row"><div class="k">GAP TREND</div><div class="v" style="color:'+gwClr+'">'+(sig.gap_widening?'WIDENING \u2705':'SHRINKING \u274C')+'</div></div>';
    h+='<div class="verdict" style="color:'+vd.clr+'">'+esc(vd.txt)+'</div></div>';
    return h;}

  document.getElementById('p-sig').innerHTML=
    '<div class="two" style="margin:8px;gap:6px;display:grid;grid-template-columns:1fr 1fr">'+
    signalBlock('CE',ce)+signalBlock('PE',pe)+'</div>';

  // ── MARKET TAB ──
  let mh='<div class="sect"><div class="sh">📈 SPOT NIFTY (3-MIN) · '+mk.spot+'</div>'+
    '<div class="row"><div class="k">EMA 9</div><div class="v" style="color:var(--gn)">'+mk.spot_ema9+'</div></div>'+
    '<div class="row"><div class="k">EMA 21</div><div class="v" style="color:var(--am)">'+mk.spot_ema21+'</div></div>'+
    '<div class="row"><div class="k">EMA SPREAD</div><div class="v" style="color:'+(mk.spot_spread>0?'var(--gn)':'var(--rd)')+'">'+(mk.spot_spread>0?'+':'')+mk.spot_spread+'pts</div></div>'+
    '<div class="row"><div class="k">RSI (3m)</div><div class="v" style="color:'+(mk.spot_rsi>60?'var(--gn)':mk.spot_rsi<40?'var(--rd)':'var(--am)')+'">'+mk.spot_rsi+'</div></div>'+
    '<div class="row"><div class="k">REGIME</div><div class="v" style="color:'+((mk.regime||'').includes('TREND')?'var(--gn)':'var(--am)')+'">'+esc(mk.regime||'')+'</div></div>'+
    '<div class="row"><div class="k">GAP</div><div class="v">'+(mk.gap>0?'+':'')+mk.gap+'pts</div></div>'+
    '<div style="padding:6px 10px;font-size:10px;color:'+(mk.spot_spread>5?'var(--gn)':mk.spot_spread<-5?'var(--rd)':'var(--am)')+'">'+
    (mk.spot_spread>10?'🚀 Strong uptrend — EMA9 pulling away from EMA21':
     mk.spot_spread>5?'📈 Uptrend — spot above both EMAs':
     mk.spot_spread>0?'⚠️ Weak up — EMAs close, trend unclear':
     mk.spot_spread>-5?'⚠️ Weak down — EMAs close, choppy':
     mk.spot_spread>-10?'📉 Downtrend — spot below both EMAs':
     '🔻 Strong downtrend — EMA9 falling hard')+'</div>'+
    '<div style="padding:2px 10px 6px;font-size:9px;color:#555">'+
    'RSI '+(mk.spot_rsi>=70?'OVERBOUGHT — reversal likely':mk.spot_rsi>=60?'STRONG — momentum with bulls':mk.spot_rsi<=30?'OVERSOLD — reversal likely':mk.spot_rsi<=40?'WEAK — bears in control':'NEUTRAL — no clear direction')+'</div></div>';
  mh+='<div class="ctx-row">'+
    '<div class="ctx"><div class="k">SPOT</div><div class="v" style="color:var(--bl)">'+mk.spot+'</div></div>'+
    '<div class="ctx"><div class="k">EMA9</div><div class="v" style="color:var(--gn)">'+mk.spot_ema9+'</div></div>'+
    '<div class="ctx"><div class="k">EMA21</div><div class="v" style="color:var(--am)">'+mk.spot_ema21+'</div></div>'+
    '<div class="ctx"><div class="k">SPREAD</div><div class="v" style="color:'+(mk.spot_spread>0?'var(--gn)':'var(--rd)')+'">'+(mk.spot_spread>0?'+':'')+mk.spot_spread+'</div></div></div>';
  mh+='<div class="ctx-row">'+
    '<div class="ctx"><div class="k">RSI</div><div class="v" style="color:'+(mk.spot_rsi>60?'var(--gn)':mk.spot_rsi<40?'var(--rd)':'var(--am)')+'">'+mk.spot_rsi+'</div></div>'+
    '<div class="ctx"><div class="k">H.RSI</div><div class="v" style="color:'+(mk.hourly_rsi>70?'var(--rd)':mk.hourly_rsi<30?'var(--gn)':'')+'">'+mk.hourly_rsi+'</div></div>'+
    '<div class="ctx"><div class="k">GAP</div><div class="v">'+(mk.gap>0?'+':'')+mk.gap+'</div></div>'+
    '<div class="ctx"><div class="k">SESSION</div><div class="v" style="font-size:10px">'+esc(mk.session)+'</div></div></div>';
  // Multi-TF Alignment
  var sp=mtf.spot||[],ceo=mtf.ce||[],peo=mtf.pe||[];
  function ac(v){return v>=25?'var(--gn)':v>=18?'var(--am)':'var(--rd)'}
  function al(v){return v>=25?'TR':v>=18?'WK':'FL'}
  function rc(v){return v>=60?'var(--gn)':v<=40?'var(--rd)':'var(--am)'}
  function sc(v){return v>0?'var(--gn)':v<0?'var(--rd)':'var(--dm)'}
  function gr(cols){return 'display:grid;grid-template-columns:repeat('+cols+',1fr);padding:4px 10px;font-size:11px;border-bottom:1px solid rgba(30,30,48,.5)'}
  function hdr(cols,names){var h='<div style="'+gr(names.length)+';font-size:8px;color:#555;font-weight:700">';names.forEach(function(n){h+='<div style="text-align:'+(n==='TF'?'left':'right')+'">'+n+'</div>'});return h+'</div>'}
  if(sp.some(function(s){return s.adx>0||s.rsi>0})){
    mh+='<div class="sect"><div class="sh">\ud83d\udcc8 SPOT MULTI-TF</div>';
    mh+=hdr(4,['TF','ADX','RSI','SPREAD']);
    sp.forEach(function(t){if(!t.adx&&!t.rsi&&!t.spread)return;mh+='<div style="'+gr(4)+'"><div style="font-weight:700;color:var(--bl)">'+t.tf+'</div><div style="text-align:right;color:'+ac(t.adx)+'">'+t.adx+' <span style="font-size:7px">'+al(t.adx)+'</span></div><div style="text-align:right;color:'+rc(t.rsi)+'">'+t.rsi+'</div><div style="text-align:right;color:'+sc(t.spread)+'">'+(t.spread>0?'+':'')+t.spread+'</div></div>'});
    var trn=sp.filter(function(t){return t.adx>=25}).length,tot=sp.filter(function(t){return t.adx>0||t.rsi>0}).length;
    var up=sp.filter(function(t){return t.spread>0&&(t.adx>0||t.rsi>0)}).length,dn=sp.filter(function(t){return t.spread<0&&(t.adx>0||t.rsi>0)}).length;
    var vc=trn>=3?'var(--gn)':trn>=2?'var(--am)':'var(--rd)';
    mh+='<div style="padding:5px 10px;font-size:10px;font-weight:700;color:'+vc+'">'+(trn>=3?'STRONG':trn>=2?'MODERATE':'WEAK')+' '+trn+'/'+tot+(up>=3?' \u2191 BULLISH':dn>=3?' \u2193 BEARISH':'')+'</div></div>'}
  var ceStk=mtf.ce_strike||'',peStk=mtf.pe_strike||'';
  if(ceo.some(function(c){return c.rsi>0||c.ltp>0})){
    mh+='<div class="sect"><div class="sh">\ud83d\udfe2 CE '+(ceStk||'')+' OPTION MULTI-TF</div>';
    mh+=hdr(7,['TF','ADX','RSI','BODY%','SPREAD','IV','LTP']);
    ceo.forEach(function(t){if(!t.rsi&&!t.ltp)return;mh+='<div style="'+gr(7)+'"><div style="font-weight:700;color:var(--gn)">'+t.tf+'</div><div style="text-align:right;color:'+ac(t.adx)+'">'+t.adx+'</div><div style="text-align:right;color:'+rc(t.rsi)+'">'+t.rsi+'</div><div style="text-align:right">'+(t.body||0)+'%</div><div style="text-align:right;color:'+sc(t.spread||0)+'">'+(t.spread>0?'+':'')+(t.spread||0)+'</div><div style="text-align:right">'+t.iv+'%</div><div style="text-align:right;color:var(--gn)">\u20b9'+t.ltp+'</div></div>'});
    mh+='</div>'}
  if(peo.some(function(p){return p.rsi>0||p.ltp>0})){
    mh+='<div class="sect"><div class="sh">\ud83d\udd34 PE '+(peStk||'')+' OPTION MULTI-TF</div>';
    mh+=hdr(7,['TF','ADX','RSI','BODY%','SPREAD','IV','LTP']);
    peo.forEach(function(t){if(!t.rsi&&!t.ltp)return;mh+='<div style="'+gr(7)+'"><div style="font-weight:700;color:var(--rd)">'+t.tf+'</div><div style="text-align:right;color:'+ac(t.adx)+'">'+t.adx+'</div><div style="text-align:right;color:'+rc(t.rsi)+'">'+t.rsi+'</div><div style="text-align:right">'+(t.body||0)+'%</div><div style="text-align:right;color:'+sc(t.spread||0)+'">'+(t.spread>0?'+':'')+(t.spread||0)+'</div><div style="text-align:right">'+t.iv+'%</div><div style="text-align:right;color:var(--rd)">\u20b9'+t.ltp+'</div></div>'});
    mh+='</div>'}

  // Fib Pivot Section
  mh+='<div class="sect"><div class="sh">📐 FIB PIVOTS · Nearest: '+(mk.fib_nearest||'—')+' ('+(mk.fib_distance>0?'+':'')+mk.fib_distance+'pts)</div>';
  var fp=mk.fib_pivots||{};
  if(fp.R3||fp.pivot){
    var spot=mk.spot;
    function flvl(name,price){
      var dist=spot-price;var near=Math.abs(dist)<20;
      var clr=name.startsWith('R')?'var(--gn)':name.startsWith('S')?'var(--rd)':'var(--bl)';
      return '<div class="row" style="'+(near?'background:rgba(59,130,246,.08)':'')+'"><div class="k" style="color:'+clr+'">'+name+'</div><div class="v" style="font-size:11px">'+price+(near?' ◄ NEAR':' <span style=\'color:#555;font-size:9px\'>'+(dist>0?'+':'')+dist.toFixed(0)+'pts</span>')+'</div></div>';}
    mh+=flvl('R3',fp.R3||0)+flvl('R2',fp.R2||0)+flvl('R1',fp.R1||0)+flvl('PIVOT',fp.pivot||0)+flvl('S1',fp.S1||0)+flvl('S2',fp.S2||0)+flvl('S3',fp.S3||0);
    mh+='<div style="padding:5px 10px;font-size:9px;color:#555">Prev: H='+fp.prev_high+' L='+fp.prev_low+' C='+fp.prev_close+' Range='+fp.range+'pts</div>';
  } else { mh+='<div style="padding:10px;color:#555;font-size:10px">Fib pivots load on market open</div>'; }
  mh+='</div>';
  // Straddle + context
  mh+='<div class="ctx-row">'+
    '<div class="ctx"><div class="k">H.RSI</div><div class="v" style="color:'+(mk.hourly_rsi>70?'var(--rd)':mk.hourly_rsi<30?'var(--gn)':'')+'">'+mk.hourly_rsi+'</div></div>'+
    '<div class="ctx"><div class="k">STRADDLE</div><div class="v">'+(str.captured?'₹'+str.open:'—')+'</div></div>'+
    '<div class="ctx"><div class="k">EXPIRY</div><div class="v" style="font-size:10px">'+esc(mk.expiry||'—')+'</div></div>'+
    '<div class="ctx"><div class="k">SESSION</div><div class="v" style="font-size:10px">'+esc(mk.session)+'</div></div></div>';
  // Zones
  var zl=zones.zones||[];
  if(zl.length>0){
    var near=zl.filter(function(z){return Math.abs(z.distance_from_spot||999)<=100});
    mh+='<div class="sect"><div class="sh">\ud83d\uddfa DEMAND/SUPPLY ZONES</div>';
    if(near.length>0){
      near.forEach(function(z){
        var clr=z.zone_type==='DEMAND'?'var(--gn)':'var(--rd)';
        var icon=z.zone_type==='DEMAND'?'\ud83d\udfe2':'\ud83d\udd34';
        mh+='<div class="row"><div class="k" style="color:'+clr+'">'+icon+' '+z.zone_type+'</div><div class="v" style="font-size:11px">'+z.zone_low+' - '+z.zone_high+' ['+z.strength+']'+(z.multi_tf?' \ud83d\udd25MTF':'')+'</div></div>';
        mh+='<div class="row"><div class="k">Distance</div><div class="v" style="font-size:11px">'+(z.distance_from_spot>0?'+':'')+z.distance_from_spot+'pts · '+z.proximity+' · tested '+z.times_tested+'x</div></div>';
      });
    } else {
      mh+='<div style="padding:8px 10px;color:#555;font-size:10px">No zones within 100pts — open territory</div>';
    }
    mh+='<div style="padding:4px 10px;font-size:9px;color:#444">Total: '+zl.length+' active zones</div></div>';
  }
  document.getElementById('p-mkt').innerHTML=mh;

  // ── TRADES TAB ──
  var th='';
  if(!trades||!trades.length){th='<div style="text-align:center;color:#444;padding:30px">No trades today</div>';}
  else{
    var cum=0,tw=0,tl=0;
    var tcards=trades.map(function(t){
      var pts=parseFloat(t.pnl_pts||0),w=pts>0;cum+=pts;
      if(w)tw++;else tl++;
      var pk=parseFloat(t.peak_pnl||0);
      var held=t.candles_held||'?';
      var reason=esc((t.exit_reason||'').replace(/_/g,' '));
      var sym=esc((t.direction||'')+' '+(t.strike||''));
      var rs=Math.round(pts*65);
      var dirClr=t.direction==='CE'?'var(--gn)':'var(--rd)';
      return '<div class="tc '+(w?'w':'l')+'" style="flex-direction:column;gap:4px">'+
        '<div style="display:flex;justify-content:space-between;width:100%;align-items:center">'+
        '<span style="font-size:14px">'+(w?'\u2705':'\u274C')+'</span>'+
        '<span style="font-weight:700;font-size:12px;color:'+dirClr+'">'+sym+'</span>'+
        '<span style="font-weight:700;color:'+(w?'var(--gn)':'var(--rd)')+';font-size:12px">'+(w?'+':'')+pts.toFixed(1)+'pts &#x20B9;'+(rs>=0?'+':'')+rs+'</span></div>'+
        '<div style="font-size:9px;color:#888;width:100%">'+esc(t.entry_time||'')+' \u2192 '+esc(t.exit_time||'')+' ('+held+'min)</div>'+
        '<div style="font-size:9px;color:#555;width:100%">'+reason+' | Peak: +'+pk.toFixed(1)+'pts | Entry: &#x20B9;'+esc(t.entry_price||'')+'</div>'+
        '</div>';
    }).join('');
    var totalT=tw+tl,wr=totalT>0?Math.round((tw/totalT)*100):0;
    var cumRs=Math.round(cum*65);
    th='<div style="margin:8px;padding:8px 10px;background:var(--c2);border:1px solid var(--bd);border-radius:6px;font-weight:700;color:'+(cum>=0?'var(--gn)':'var(--rd)')+'">'+
      (cum>=0?'+':'')+cum.toFixed(1)+'pts &#x20B9;'+cumRs+' | '+totalT+' trades | '+tw+'W '+tl+'L | WR '+wr+'%</div>';
    th+=tcards;
  }
  document.getElementById('p-trd').innerHTML=th;
  document.getElementById('ts').textContent=d.ts||new Date().toLocaleTimeString('en-IN')}

async function loadFiles(folder){
  const d=await fetch('/api/files'+(folder?'?folder='+folder:'')).then(r=>r.json()).catch(e=>null);
  const el=document.getElementById('p-fil');
  if(!d){el.innerHTML='<div style="text-align:center;color:#555;padding:20px">Error loading files</div>';return}
  if(!folder&&d.folders){
    el.innerHTML='<div style="padding:8px 10px;font-size:11px;font-weight:700;color:var(--dm)">SELECT FOLDER</div>'+
      d.folders.map(function(f){return '<div onclick="loadFiles(\x27'+f.key+'\x27)" style="margin:3px 8px;padding:10px;background:var(--c1);border:1px solid var(--bd);border-radius:6px;cursor:pointer;display:flex;justify-content:space-between;align-items:center"><span style="font-weight:700;font-size:12px">'+f.name+'</span><span style="color:#555;font-size:18px">></span></div>'}).join('');
    return}
  var h='<div onclick="loadFiles(\x27\x27)" style="margin:8px;padding:8px 10px;background:var(--c2);border:1px solid var(--bd);border-radius:6px;cursor:pointer;font-size:11px;color:var(--bl)">Back</div>';
  h+='<div style="padding:4px 10px;font-size:11px;font-weight:700;color:var(--dm)">'+(d.folder_name||folder)+' ('+d.files.length+' files)</div>';
  if(!d.files.length){h+='<div style="text-align:center;color:#555;padding:20px">No files</div>'}
  else{d.files.forEach(function(f){h+='<a href="/api/download/'+f.path+'" style="display:block;margin:2px 8px;padding:8px 10px;background:var(--c1);border:1px solid var(--bd);border-radius:6px;text-decoration:none;color:var(--tx)"><span style="font-size:11px">'+f.name+'</span><span style="float:right;color:#555;font-size:10px">'+f.size+'KB</span></a>'})}
  el.innerHTML=h}

async function go(){
  try{
    const[d,t,z,mtf]=await Promise.all([fetch('/api/dashboard').then(r=>r.json()).catch(e=>null),fetch('/api/trades').then(r=>r.json()).catch(e=>[]),fetch('/api/zones').then(r=>r.json()).catch(e=>({zones:[]})),fetch('/api/multitf').then(r=>r.json()).catch(e=>({spot:[],ce:[],pe:[]}))]);
    render(d||{},t||[],z||{zones:[]},mtf||{})
  }catch(e){console.error(e)}
}
go();setInterval(go,10000);
// Load files tab when clicked
document.querySelector('[data-t="fil"]').addEventListener('click',function(){loadFiles('')});
</script></body></html>"""

class H(BaseHTTPRequestHandler):
    def log_message(self,*a):pass
    def _j(self,d):
        self.send_response(200)
        self.send_header("Content-Type","application/json")
        self.send_header("Access-Control-Allow-Origin","*")
        # v15.2 BUG-2: never let any layer cache /api/dashboard
        self.send_header("Cache-Control","no-cache, no-store, must-revalidate")
        self.send_header("Pragma","no-cache")
        self.send_header("Expires","0")
        self.end_headers()
        self.wfile.write(json.dumps(d,default=str).encode())
    def _send_file(self, path):
        try:
            parts = path.split("/", 1)
            if len(parts) != 2:
                self.send_error(404); return
            folder_key, filename = parts[0], os.path.basename(parts[1])  # sanitise traversal
            info = _FOLDERS.get(folder_key)
            if not info:
                self.send_error(404); return
            filepath = os.path.realpath(os.path.join(info[1], filename))
            if not filepath.startswith(os.path.realpath(info[1])):
                self.send_error(403); return
            if not os.path.isfile(filepath):
                self.send_error(404); return
            self.send_response(200)
            if filename.endswith(".csv"):
                self.send_header("Content-Type", "text/csv")
            elif filename.endswith(".json"):
                self.send_header("Content-Type", "application/json")
            elif filename.endswith(".log"):
                self.send_header("Content-Type", "text/plain")
            else:
                self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Disposition", "attachment; filename=" + filename)
            self.send_header("Content-Length", str(os.path.getsize(filepath)))
            self.end_headers()
            with open(filepath, "rb") as f:
                self.wfile.write(f.read())
        except Exception as e:
            self.send_error(500)

    def _download_daily_logs(self):
        """
        /api/logs/download           → today's logs zip
        /api/logs/download?date=2026-04-01 → specific date
        """
        from urllib.parse import parse_qs
        import VRL_DATA as _D
        q = parse_qs(urlparse(self.path).query)
        target_date = q.get("date", [None])[0]
        if target_date is None:
            from datetime import date as _date
            target_date = _date.today().strftime("%Y-%m-%d")
        zip_path = _D.create_daily_zip(target_date)
        if not zip_path or not os.path.isfile(zip_path):
            self.send_error(404, "No logs found for " + target_date)
            return
        try:
            fname = os.path.basename(zip_path)
            fsize = os.path.getsize(zip_path)
            self.send_response(200)
            self.send_header("Content-Type", "application/zip")
            self.send_header("Content-Disposition", "attachment; filename=" + fname)
            self.send_header("Content-Length", str(fsize))
            self.end_headers()
            with open(zip_path, "rb") as f:
                self.wfile.write(f.read())
            try:
                os.remove(zip_path)
            except Exception:
                pass
        except Exception:
            self.send_error(500)

    def _files_page(self):
        import urllib.parse
        import time as _t
        q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        folder = q.get("f",[""])[0]
        today_str = date.today().strftime("%Y%m%d")
        today_iso = date.today().isoformat()

        css = ('<html><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>VRL Files</title>'
               '<style>'
               'body{background:#080810;color:#e4e4e7;font-family:monospace;font-size:13px;padding:10px;max-width:520px;margin:0 auto}'
               'a{color:#3b82f6;text-decoration:none}'
               '.f{display:block;margin:4px 0;padding:10px 12px;background:#111118;border:1px solid #1e1e30;border-radius:6px}'
               '.f:active{background:#1e1e30}'
               '.sz{float:right;color:#555;font-size:11px}'
               '.bk{display:inline-block;margin:8px 4px;padding:6px 12px;background:#1e1e30;border-radius:6px}'
               '.sh{color:#888;font-size:11px;margin:16px 0 6px;text-transform:uppercase;letter-spacing:1px}'
               '.badge{background:#22c55e;color:#000;padding:1px 6px;border-radius:8px;font-size:10px;margin-left:6px}'
               '.badge-r{background:#ef4444}'
               '.cnt{color:#555;font-size:11px;margin-left:6px}'
               '</style></head><body>')

        html = css
        html += '<h2 style="color:#3b82f6;font-size:15px">VISHAL RAJPUT FILES</h2>'
        html += '<a href="/" class="bk">War Room</a>'

        if not folder:
            # ── TODAY section ──
            html += '<div class="sh">TODAY (' + today_iso + ')</div>'

            # Count today's trades
            trade_count = 0
            tl_path = os.path.join(BASE, "lab_data", "vrl_trade_log.csv")
            if os.path.isfile(tl_path):
                try:
                    with open(tl_path) as _tf:
                        for r in csv.DictReader(_tf):
                            if r.get("date") == today_iso:
                                trade_count += 1
                except Exception:
                    pass

            # Today's files - quick links
            today_items = [
                ("📊 Today's Option Data", "options_1min", "nifty_option_1min_" + today_str),
                ("📈 Today's Spot Data", "spot", "nifty_spot_1min_" + today_str),
                ("📒 Today's Trades", "trade_log", None),
                ("📋 Today's Scan Log", "options_1min", "nifty_signal_scan_" + today_str),
            ]
            for label, fkey, prefix in today_items:
                badge = ""
                if "Trades" in label and trade_count > 0:
                    badge = '<span class="badge">' + str(trade_count) + '</span>'
                html += '<a href="/files?f=' + fkey + '" class="f">' + label + badge + '</a>'

            # ── HISTORICAL DATA section ──
            html += '<div class="sh">HISTORICAL DATA</div>'
            hist_items = [
                ("spot", "📈 Spot (1m/5m/15m/D)"),
                ("options_3min", "📊 Options 3-Min CE+PE"),
                ("options_1min", "📊 Options 1m/5m/15m/Scan"),
                ("reports", "📑 Daily Summary Reports"),
            ]
            for fkey, label in hist_items:
                info = _FOLDERS.get(fkey)
                cnt = ""
                if info and os.path.isdir(info[1]):
                    try:
                        n = len([f for f in os.listdir(info[1]) if os.path.isfile(os.path.join(info[1], f)) and os.path.getsize(os.path.join(info[1], f)) > 0])
                        cnt = '<span class="cnt">' + str(n) + ' files</span>'
                    except Exception:
                        pass
                html += '<a href="/files?f=' + fkey + '" class="f">' + label + cnt + '</a>'

            # ── ANALYSIS section ──
            html += '<div class="sh">ANALYSIS</div>'
            analysis_items = [
                ("research", "🔭 Demand/Supply Zones"),
                ("trade_log", "📒 Full Trade History"),
            ]
            for fkey, label in analysis_items:
                html += '<a href="/files?f=' + fkey + '" class="f">' + label + '</a>'

            # ── SYSTEM section ──
            html += '<div class="sh">SYSTEM</div>'
            system_items = [
                ("state", "⚙️ State + Config"),
                ("logs", "📋 Logs"),
            ]
            for fkey, label in system_items:
                html += '<a href="/files?f=' + fkey + '" class="f">' + label + '</a>'

        else:
            # ── File listing for a specific folder ──
            html += '<a href="/files" class="bk">Back</a>'
            info = _FOLDERS.get(folder)
            if info and os.path.isdir(info[1]):
                html += '<h3 style="color:#888;font-size:12px">' + info[0] + '</h3>'
                files = sorted(os.listdir(info[1]), reverse=True)
                file_list = []
                for fname in files:
                    fp = os.path.join(info[1], fname)
                    if os.path.isfile(fp) and os.path.getsize(fp) > 0:
                        sz = os.path.getsize(fp)
                        mt = os.path.getmtime(fp)
                        file_list.append((fname, sz, mt, fp))
                file_list.sort(key=lambda x: x[2], reverse=True)
                if not file_list:
                    html += '<div style="color:#555;padding:20px">No files found</div>'
                for fname, sz, mt, fp in file_list[:50]:
                    sz_str = str(round(sz / 1024, 1)) + ' KB' if sz < 1024*1024 else str(round(sz / (1024*1024), 1)) + ' MB'
                    mod = _t.strftime('%d %b %H:%M', _t.localtime(mt))
                    # Highlight today's files
                    is_today = today_str in fname
                    style = ' style="border-left:3px solid #22c55e"' if is_today else ''
                    html += '<a href="/api/download/' + folder + '/' + fname + '" class="f"' + style + '>' + fname + '<span class="sz">' + sz_str + ' · ' + mod + '</span></a>'
            else:
                html += '<div style="color:#555;padding:20px">Folder not found</div>'

        html += '</body></html>'
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(html.encode())

    def _get_session(self):
        cookie = self.headers.get("Cookie", "")
        return _get_session(cookie)

    def _require_auth(self, admin_only=False):
        """Returns session dict if authenticated, None + redirect if not."""
        sess = self._get_session()
        if not sess:
            self._redirect("/login")
            return None
        if admin_only and sess.get("role") != "admin":
            self.send_error(403, "Admin access required")
            return None
        return sess

    def _redirect(self, url):
        self.send_response(302)
        self.send_header("Location", url)
        self.end_headers()

    def _html(self, html, code=200):
        self.send_response(code)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(html.encode())

    def _handle_login_get(self):
        if self._get_session():
            self._redirect("/")
            return
        self._html(LOGIN_HTML.replace("ERR_MSG", "").replace('display:none', 'display:none'))

    def _handle_login_post(self):
        # Rate limit check
        ip = self.client_address[0]
        now = time.time()
        attempts = _login_attempts.get(ip, [])
        attempts = [t for t in attempts if now - t < 60]
        if len(attempts) >= _LOGIN_LIMIT:
            self._html(LOGIN_HTML.replace("ERR_MSG", "Too many attempts. Wait 15 minutes.").replace('display:none', ''), 429)
            return

        # Read POST body
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length).decode()
        params = dict(p.split("=", 1) for p in body.split("&") if "=" in p)
        from urllib.parse import unquote_plus
        username = unquote_plus(params.get("username", ""))
        password = unquote_plus(params.get("password", ""))

        # Verify
        pass_hash = hashlib.sha256(password.encode()).hexdigest()
        if username == ADMIN_USER and ADMIN_PASS_HASH and pass_hash == ADMIN_PASS_HASH:
            token = _create_session(username, "admin", days=30)
            self.send_response(302)
            self.send_header("Location", "/")
            self.send_header("Set-Cookie", "vrl_session=" + token + "; Path=/; Max-Age=2592000; HttpOnly")
            self.end_headers()
            return

        # Failed
        _login_attempts.setdefault(ip, []).append(now)
        self._html(LOGIN_HTML.replace("ERR_MSG", "Invalid username or password").replace('display:none', ''), 401)

    def _handle_subscriber_token(self, token):
        ip = self.client_address[0]
        try:
            import VRL_DB as _DB
            result = _DB.validate_token(token, ip=ip)
        except Exception:
            result = None

        if result is None:
            self._html(TOKEN_ERROR_HTML.replace("MSG_TITLE", "Invalid Link").replace("MSG_BODY", "This access link is not valid."), 404)
            return
        if result.get("revoked"):
            self._html(TOKEN_ERROR_HTML.replace("MSG_TITLE", "Access Revoked").replace("MSG_BODY", "Your access has been revoked. Contact Vishal Rajput."), 403)
            return
        if result.get("expired"):
            self._html(TOKEN_ERROR_HTML.replace("MSG_TITLE", "Access Expired").replace("MSG_BODY", "Your access has expired. Contact Vishal Rajput to renew."), 403)
            return
        if result.get("valid"):
            # Alert admin if token is being shared (4+ unique IPs)
            if result.get("sharing_alert"):
                try:
                    import requests as _req
                    _tg_token = os.environ.get("TG_TOKEN", "")
                    _tg_chat = os.environ.get("TG_GROUP_ID", "")
                    if not _tg_token:
                        with open(os.path.join(BASE, ".env")) as _ef2:
                            for _ln in _ef2:
                                if _ln.startswith("TG_TOKEN="):
                                    _tg_token = _ln.strip().split("=", 1)[1]
                                elif _ln.startswith("TG_GROUP_ID="):
                                    _tg_chat = _ln.strip().split("=", 1)[1]
                    if _tg_token and _tg_chat:
                        _req.post("https://api.telegram.org/bot" + _tg_token + "/sendMessage",
                                  json={"chat_id": _tg_chat,
                                        "text": "⚠️ <b>SHARING ALERT</b>\n"
                                                + result["name"] + "'s token used from "
                                                + str(result.get("unique_ips", 0)) + " unique IPs\n"
                                                + "Latest: " + ip + "\n"
                                                + "Use /token revoke " + result["name"] + " to block",
                                        "parse_mode": "HTML"}, timeout=5)
                except Exception:
                    pass
            sess_token = _create_session(result["name"], "subscriber", days=30)
            self.send_response(302)
            self.send_header("Location", "/")
            self.send_header("Set-Cookie", "vrl_session=" + sess_token + "; Path=/; Max-Age=2592000; HttpOnly")
            self.end_headers()

    def _handle_logout(self):
        cookie = self.headers.get("Cookie", "")
        try:
            c = SimpleCookie()
            c.load(cookie)
            if "vrl_session" in c:
                token = c["vrl_session"].value
                with _sessions_lock:
                    _sessions.pop(token, None)
        except Exception:
            pass
        self.send_response(302)
        self.send_header("Location", "/login")
        self.send_header("Set-Cookie", "vrl_session=; Path=/; Max-Age=0")
        self.end_headers()

    def _handle_viewers(self):
        """Admin-only: show active tokens and sessions."""
        sess = self._require_auth(admin_only=True)
        if not sess:
            return
        try:
            import VRL_DB as _DB
            tokens = _DB.list_tokens()
        except Exception:
            tokens = []
        active = [t for t in tokens if t.get("active")]
        with _sessions_lock:
            active_sessions = len(_sessions)
        self._j({"tokens": tokens, "active_sessions": active_sessions})

    def _db_trades(self):
        from urllib.parse import parse_qs
        q = parse_qs(urlparse(self.path).query)
        d = q.get("date", [None])[0]
        try:
            import VRL_DB as _DB
            self._j(_DB.get_trades(d))
        except Exception as e:
            self._j({"error": str(e)})

    def _db_scans(self):
        from urllib.parse import parse_qs
        q = parse_qs(urlparse(self.path).query)
        d = q.get("date", [None])[0]
        direction = q.get("direction", [None])[0]
        try:
            import VRL_DB as _DB
            self._j(_DB.get_scans(d, direction))
        except Exception as e:
            self._j({"error": str(e)})

    def _db_spot(self):
        from urllib.parse import parse_qs
        q = parse_qs(urlparse(self.path).query)
        tf = q.get("tf", ["1min"])[0]
        table_map = {"1min": "spot_1min", "5min": "spot_5min", "15min": "spot_15min",
                     "60min": "spot_60min", "daily": "spot_daily"}
        table = table_map.get(tf, "spot_1min")
        from_ts = q.get("from", [None])[0]
        to_ts = q.get("to", [None])[0]
        try:
            import VRL_DB as _DB
            self._j(_DB.get_spot(table, from_ts, to_ts))
        except Exception as e:
            self._j({"error": str(e)})

    def _db_stats(self):
        from urllib.parse import parse_qs
        q = parse_qs(urlparse(self.path).query)
        d = q.get("date", [None])[0]
        try:
            import VRL_DB as _DB
            self._j(_DB.get_stats(d))
        except Exception as e:
            self._j({"error": str(e)})

    def do_POST(self):
        p = urlparse(self.path).path
        if p == "/login":
            self._handle_login_post()
        else:
            self.send_error(404)

    def do_GET(self):
        p=urlparse(self.path).path

        # ── PUBLIC routes (no auth) ──
        if p == "/login":
            self._handle_login_get(); return
        if p == "/logout":
            self._handle_logout(); return
        if p.startswith("/s/"):
            token = p[3:]
            self._handle_subscriber_token(token); return

        # ── AUTH BYPASS: skip auth if no password set ──
        if not ADMIN_PASS_HASH:
            pass  # no auth configured, allow all
        else:
            # ── AUTHENTICATED routes ──
            sess = self._get_session()
            if not sess:
                self._redirect("/login"); return

            # Admin-only routes
            if p in ("/files",) or p.startswith("/files?") or p.startswith("/api/download/") \
               or p.startswith("/api/logs/") or p.startswith("/api/db/") or p.startswith("/api/files"):
                if sess.get("role") != "admin":
                    self.send_error(403, "Admin access required"); return

        if p=="/api/viewers":
            self._handle_viewers(); return
        if p=="/files" or p.startswith("/files?"):self._files_page();return
        if p in("/","/dashboard"):
            # Serve static/index.html if it exists, otherwise fallback to inline HTML
            static_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", "VRL_DASHBOARD.html")
            if os.path.isfile(static_path):
                self.send_response(200)
                self.send_header("Content-Type","text/html")
                self.send_header("Cache-Control","no-cache, no-store, must-revalidate")
                self.end_headers()
                with open(static_path, "rb") as sf:
                    self.wfile.write(sf.read())
            else:
                self.send_response(200)
                self.send_header("Content-Type","text/html")
                self.end_headers()
                self.wfile.write(HTML.encode())
        elif p=="/api/dashboard":self._j(_read_dash())
        elif p=="/api/trades":self._j(_read_trades())
        elif p=="/api/multitf":self._j(_read_multitf())
        elif p=="/api/zones":
            zp = os.path.join(STATE_DIR, "vrl_zones.json")
            if os.path.isfile(zp):
                with open(zp) as _zf:
                    self._j(json.load(_zf))
            else:
                self._j({"zones":[]})
        elif p=="/api/files":
            q = urlparse(self.path).query
            folder = ''
            if 'folder=' in q:
                folder = q.split('folder=')[1].split('&')[0]
            self._j(_list_files(folder))
        elif p.startswith("/api/download/"):
            self._send_file(p[14:])  # strip /api/download/
        elif p == "/api/logs/download" or p.startswith("/api/logs/download?"):
            self._download_daily_logs()
        elif p == "/api/db/trades" or p.startswith("/api/db/trades?"):
            self._db_trades()
        elif p == "/api/db/scans" or p.startswith("/api/db/scans?"):
            self._db_scans()
        elif p == "/api/db/spot" or p.startswith("/api/db/spot?"):
            self._db_spot()
        elif p == "/api/db/stats" or p.startswith("/api/db/stats?"):
            self._db_stats()
        else:self.send_error(404)

def _bind_host():
    """v15.2.5 BUG-M: fail-safe bind selection.
    If ADMIN_PASS_HASH is empty, admin login is impossible but the
    process would still happily listen on 0.0.0.0 and expose
    subscriber-token endpoints + /api/* to the public internet.
    Fall back to loopback-only so a misconfigured box can't leak
    data while operators are still fixing the env file. Emits a
    CRITICAL stderr line and attempts a Telegram warn (best-effort)
    so the misconfiguration surfaces immediately."""
    if ADMIN_PASS_HASH:
        return "0.0.0.0"
    msg = ("CRITICAL: VRL_DASHBOARD_PASS missing from ~/.env — "
           "ADMIN_PASS_HASH is empty. Binding to 127.0.0.1 only "
           "(loopback). Set VRL_DASHBOARD_PASS and restart "
           "vrl-web to re-expose publicly.")
    import sys as _sys
    print("[VRL_WEB] " + msg, file=_sys.stderr, flush=True)
    try:
        import logging as _logging
        _logging.getLogger("vrl_web").critical(msg)
    except Exception:
        pass
    # Best-effort Telegram — needs token + chat from env. Never raise.
    try:
        _tok = ""
        _cid = ""
        _envp = os.path.join(BASE, ".env")
        if os.path.isfile(_envp):
            with open(_envp) as _ef:
                for _line in _ef:
                    _line = _line.strip()
                    if _line.startswith("TG_TOKEN="):
                        _tok = _line.split("=", 1)[1].strip().strip('"\'')
                    elif _line.startswith("TG_GROUP_ID="):
                        _cid = _line.split("=", 1)[1].strip().strip('"\'')
        if _tok and _cid:
            import urllib.request as _ur
            import urllib.parse as _up
            _payload = _up.urlencode({
                "chat_id": _cid,
                "text": "⚠️ VRL_WEB started on 127.0.0.1 only — "
                        "VRL_DASHBOARD_PASS missing. Set it in ~/.env "
                        "and restart vrl-web to re-expose the dashboard."
            }).encode()
            _req = _ur.Request(
                "https://api.telegram.org/bot" + _tok + "/sendMessage",
                data=_payload, method="POST")
            _ur.urlopen(_req, timeout=5).read()
    except Exception:
        pass
    return "127.0.0.1"


if __name__=="__main__":
    _host = _bind_host()
    s=HTTPServer((_host,PORT),H)
    print("VRL War Room v15.2.5 — http://" + _host + ":" + str(PORT))
    try:s.serve_forever()
    except KeyboardInterrupt:s.server_close()
