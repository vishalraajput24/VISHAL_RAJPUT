#!/usr/bin/env python3
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

# Optional auth: set VRL_WEB_TOKEN env var to require ?token=<value> on all requests
_WEB_TOKEN = os.environ.get("VRL_WEB_TOKEN", "")

def _read_dash():
    if not os.path.isfile(DASH_FILE): return {}
    try:
        with open(DASH_FILE) as f: return json.load(f)
    except: return {}

import glob as _glob
import urllib.parse as _up

_FOLDERS = {
    "trade_log":    ("📒 Trade Log",              os.path.join(BASE, "lab_data")),
    "spot":         ("📈 Spot (1m/5m/15m/60m/D)", os.path.join(BASE, "lab_data", "spot")),
    "options_3min": ("📊 Options 3-Min CE+PE",    os.path.join(BASE, "lab_data", "options_3min")),
    "options_1min": ("📊 Options 1m/5m/15m/Scan", os.path.join(BASE, "lab_data", "options_1min")),
    "reports":      ("📑 Daily Summary Reports",  os.path.join(BASE, "lab_data", "reports")),
    "sessions":     ("🗂 Sessions",               os.path.join(BASE, "lab_data", "sessions")),
    "research":     ("🔭 Zones + Research",       os.path.join(BASE, "research")),
    "state":        ("⚙️ State + Dashboard JSON", os.path.join(BASE, "state")),
    "logs":         ("📋 Live Logs",              os.path.join(BASE, "logs", "live")),
    "logs_lab":     ("🔬 Lab Logs",               os.path.join(BASE, "logs", "lab")),
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


import glob as _glob

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
        except: return None
    def _lasttype(path, t):
        if not path or not os.path.isfile(path): return None
        try:
            with open(path) as f: rows = list(csv.DictReader(f))
            for r in reversed(rows):
                if r.get("type") == t: return r
            return None
        except: return None
    def _f(r, k, d=0): 
        try: return round(float(r.get(k, d)), 1)
        except: return d
    def _f3(r, k, d=0):
        try: return round(float(r.get(k, d)), 3)
        except: return d
    spot = []
    for label, prefix in [("1m","nifty_spot_1min"),("5m","nifty_spot_5min_"),("15m","nifty_spot_15min_"),("60m","nifty_spot_60min_"),("D","nifty_spot_daily")]:
        r = _last(_latest(spot_dir, prefix))
        if r: spot.append({"tf":label,"adx":_f(r,"adx"),"rsi":_f(r,"rsi"),"spread":_f(r,"ema_spread",_f(r,"spread"))})
        else: spot.append({"tf":label,"adx":0,"rsi":0,"spread":0})
    ce = []; pe = []
    for label, d, prefix in [("1m",opt1_dir,"nifty_option_1min_"),("3m",opt3_dir,"nifty_option_3min_"),("5m",opt1_dir,"nifty_option_5min_"),("15m",opt1_dir,"nifty_option_15min_")]:
        p = _latest(d, prefix)
        for side, arr in [("CE",ce),("PE",pe)]:
            r = _lasttype(p, side)
            if r: arr.append({"tf":label,"adx":_f(r,"adx"),"rsi":_f(r,"rsi"),"iv":_f(r,"iv_pct"),"delta":_f3(r,"delta"),"ltp":_f(r,"close")})
            else: arr.append({"tf":label,"adx":0,"rsi":0,"iv":0,"delta":0,"ltp":0})
    return {"spot":spot,"ce":ce,"pe":pe}

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
  <div class="tab" data-t="fil" onclick="window.location.href='/files'">📁 FILES</div>
</div>

<div id="p-sig"></div>
<div id="p-mkt" class="H"></div>
<div id="p-trd" class="H"></div>
<div id="p-fil" class="H"></div>

<div class="ft">Auto-refresh 10s · <span id="ts"></span></div>

<script>
function st(t){document.querySelectorAll('.tab').forEach(e=>e.classList.toggle('on',e.dataset.t===t));['sig','mkt','trd','fil'].forEach(i=>document.getElementById('p-'+i).classList.toggle('H',i!==t))}

function esc(s){return String(s).replace(/</g,'&lt;')}

function tagC(v){
  if(v==='BULL')return 'tg';if(v==='BEAR')return 'tr';
  if(v==='SIDEWAYS'||v==='NEUTRAL')return 'ta';return 'tb'}

function render(d, trades, zones, mtf){ if(!d || !d.market){document.getElementById('p-sig').innerHTML='<div style="text-align:center;color:#555;padding:20px">Waiting for bot data... (FILES tab works)</div>';document.getElementById('position-area').innerHTML='';return}
  
  const mk=d.market,ce=d.ce||{},pe=d.pe||{},pos=d.position||{},td=d.today||{},str=d.straddle||{};

  // Version + tags
  document.getElementById('ver').textContent=d.version||'';
  let tags='<span class="tag '+(d.mode==='LIVE'?'tg':'tb')+'">'+esc(d.mode)+'</span>';
  tags+='<span class="tag '+(mk.dte<=1?'tr':'tb')+'">DTE '+mk.dte+'</span>';
  tags+='<span class="tag tb">ATM '+mk.atm+'</span>';
  if(mk.vix>0)tags+='<span class="tag '+(mk.vix>22?'tr':mk.vix>18?'ta':'tg')+'">VIX '+mk.vix+'</span>';
  if(mk.bias&&mk.bias!=='')tags+='<span class="tag '+tagC(mk.bias)+'">'+esc(mk.bias)+'</span>';
  if(mk.regime)tags+='<span class="tag '+(mk.regime.includes('TREND')?'tg':'ta')+'">'+esc(mk.regime)+'</span>';
    if(mk.market_open&&!mk.indicators_warm)tags+='<span class="tag tr">WARMUP</span>';
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
    let h='<div class="sect"><div class="sh">'+label+' '+mk.atm+' · ₹'+sig.ltp+'</div>';
    // 3-min gate
    h+='<div style="padding:4px 10px;font-size:8px;color:#555;font-weight:700;letter-spacing:.5px;border-bottom:1px solid var(--bd);background:rgba(59,130,246,.05)">▸ 3-MIN GATE</div>';
    h+='<div class="row"><div class="k">STATUS</div><div class="v" style="color:'+(g.met>=3?'var(--gn)':'var(--rd)')+'">'+g.met+'/4'+(g.met>=3?' ✅':' ❌')+'</div></div>';
    h+='<div class="gate">'+dotH(g.ema,'E')+dotH(g.body,'B')+dotH(g.rsi,'R')+dotH(g.price,'P')+'</div>';
    if(g.rsi_val>0)h+='<div class="row"><div class="k">3m RSI</div><div class="v">'+g.rsi_val+'</div></div>';
    if(g.spread!=0)h+='<div class="row"><div class="k">3m Spread</div><div class="v" style="color:'+(g.spread>0?'var(--gn)':'var(--rd)')+'">'+(g.spread>0?'+':'')+g.spread+'</div></div>';
    var adxV=g.adx||0;if(adxV>0)h+='<div class="row"><div class="k">3m ADX</div><div class="v" style="color:'+(adxV>=25?'var(--gn)':adxV>=18?'var(--am)':'var(--rd)')+'">'+adxV+(adxV>=25?' TREND':adxV>=18?' WEAK':' FLAT')+'</div></div>';
    var cc=g.candles||0;if(cc>0&&cc<25)h+='<div class="row"><div class="k">DATA</div><div class="v" style="color:var(--am);font-size:10px">'+cc+' candles (WARMUP '+(cc<15?'\u26a0\ufe0f cold':'\u23f3 warming')+')</div></div>';
    // 1-min section
    h+='<div style="padding:4px 10px;font-size:8px;color:#555;font-weight:700;letter-spacing:.5px;border-bottom:1px solid var(--bd);border-top:1px solid var(--bd);background:rgba(16,185,129,.05)">▸ 1-MIN ENTRY</div>';
    h+='<div class="bar-wrap"><div class="bar-label"><span>SPREAD</span><span style="color:'+barClr+'">'+(sig.spread_1m>0?'+':'')+sig.spread_1m+' / +'+minSpread+'</span></div>';
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
  let mh='<div class="sect"><div class="sh">📈 SPOT NIFTY (3-MIN) · '+mk.spot+'</div>'+
    '<div class="row"><div class="k">EMA 9</div><div class="v" style="color:var(--gn)">'+mk.spot_ema9+'</div></div>'+
    '<div class="row"><div class="k">EMA 21</div><div class="v" style="color:var(--am)">'+mk.spot_ema21+'</div></div>'+
    '<div class="row"><div class="k">EMA SPREAD</div><div class="v" style="color:'+(mk.spot_spread>0?'var(--gn)':'var(--rd)')+'">'+(mk.spot_spread>0?'+':'')+mk.spot_spread+'pts</div></div>'+
    '<div class="row"><div class="k">RSI (3m)</div><div class="v" style="color:'+(mk.spot_rsi>60?'var(--gn)':mk.spot_rsi<40?'var(--rd)':'var(--am)')+'">'+mk.spot_rsi+'</div></div>'+
    '<div class="row"><div class="k">REGIME</div><div class="v" style="color:'+(mk.regime.includes('TREND')?'var(--gn)':'var(--am)')+'">'+esc(mk.regime)+'</div></div>'+
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
    sp.forEach(function(t){if(!t.adx&&!t.rsi)return;mh+='<div style="'+gr(4)+'"><div style="font-weight:700;color:var(--bl)">'+t.tf+'</div><div style="text-align:right;color:'+ac(t.adx)+'">'+t.adx+' <span style="font-size:7px">'+al(t.adx)+'</span></div><div style="text-align:right;color:'+rc(t.rsi)+'">'+t.rsi+'</div><div style="text-align:right;color:'+sc(t.spread)+'">'+(t.spread>0?'+':'')+t.spread+'</div></div>'});
    var trn=sp.filter(function(t){return t.adx>=25}).length,tot=sp.filter(function(t){return t.adx>0}).length;
    var up=sp.filter(function(t){return t.spread>0&&t.adx>0}).length,dn=sp.filter(function(t){return t.spread<0&&t.adx>0}).length;
    var vc=trn>=3?'var(--gn)':trn>=2?'var(--am)':'var(--rd)';
    mh+='<div style="padding:5px 10px;font-size:10px;font-weight:700;color:'+vc+'">'+(trn>=3?'STRONG':trn>=2?'MODERATE':'WEAK')+' '+trn+'/'+tot+(up>=3?' \u2191 BULLISH':dn>=3?' \u2193 BEARISH':'')+'</div></div>'}
  if(ceo.some(function(c){return c.rsi>0||c.ltp>0})){
    mh+='<div class="sect"><div class="sh">\ud83d\udfe2 CE OPTION MULTI-TF</div>';
    mh+=hdr(6,['TF','ADX','RSI','IV','DELTA','LTP']);
    ceo.forEach(function(t){if(!t.rsi&&!t.ltp)return;mh+='<div style="'+gr(6)+'"><div style="font-weight:700;color:var(--gn)">'+t.tf+'</div><div style="text-align:right;color:'+ac(t.adx)+'">'+t.adx+'</div><div style="text-align:right;color:'+rc(t.rsi)+'">'+t.rsi+'</div><div style="text-align:right">'+t.iv+'%</div><div style="text-align:right">'+t.delta+'</div><div style="text-align:right;color:var(--gn)">\u20b9'+t.ltp+'</div></div>'});
    mh+='</div>'}
  if(peo.some(function(p){return p.rsi>0||p.ltp>0})){
    mh+='<div class="sect"><div class="sh">\ud83d\udd34 PE OPTION MULTI-TF</div>';
    mh+=hdr(6,['TF','ADX','RSI','IV','DELTA','LTP']);
    peo.forEach(function(t){if(!t.rsi&&!t.ltp)return;mh+='<div style="'+gr(6)+'"><div style="font-weight:700;color:var(--rd)">'+t.tf+'</div><div style="text-align:right;color:'+ac(t.adx)+'">'+t.adx+'</div><div style="text-align:right;color:'+rc(t.rsi)+'">'+t.rsi+'</div><div style="text-align:right">'+t.iv+'%</div><div style="text-align:right">'+t.delta+'</div><div style="text-align:right;color:var(--rd)">\u20b9'+t.ltp+'</div></div>'});
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

async function loadFiles(folder){
  const d=await A('files'+(folder?'?folder='+folder:''));
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

    def _files_page(self):
        import urllib.parse
        q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        folder = q.get("f",[""])[0]
        html = '<html><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>VRL Files</title>'
        html += '<style>body{background:#080810;color:#e4e4e7;font-family:monospace;font-size:13px;padding:10px;max-width:500px;margin:0 auto}'
        html += 'a{color:#3b82f6;text-decoration:none}.f{display:block;margin:4px 0;padding:10px;background:#111118;border:1px solid #1e1e30;border-radius:6px}'
        html += '.f:active{background:#1e1e30}.sz{float:right;color:#555;font-size:11px}.bk{display:inline-block;margin:8px 0;padding:6px 12px;background:#1e1e30;border-radius:6px}</style></head><body>'
        html += '<h2 style="color:#3b82f6;font-size:15px">VISHAL RAJPUT FILES</h2>'
        html += '<a href="/" class="bk">War Room</a><br>'
        if not folder:
            for k, v in _FOLDERS.items():
                html += '<a href="/files?f=' + k + '" class="f">' + v[0] + '</a>'
        else:
            html += '<a href="/files" class="bk">Back</a>'
            info = _FOLDERS.get(folder)
            if info and os.path.isdir(info[1]):
                html += '<h3 style="color:#888;font-size:12px">' + info[0] + '</h3>'
                files = sorted(os.listdir(info[1]), reverse=True)
                import time as _t
                file_list = []
                for fname in files:
                    fp = os.path.join(info[1], fname)
                    if os.path.isfile(fp) and os.path.getsize(fp) > 0:
                        sz = os.path.getsize(fp)
                        mt = os.path.getmtime(fp)
                        file_list.append((fname, sz, mt, fp))
                file_list.sort(key=lambda x: x[2], reverse=True)
                for fname, sz, mt, fp in file_list[:40]:
                    sz_str = str(round(sz / 1024, 1)) + ' KB' if sz < 1024*1024 else str(round(sz / (1024*1024), 1)) + ' MB'
                    mod = _t.strftime('%d %b %H:%M', _t.localtime(mt))
                    html += '<a href="/api/download/' + folder + '/' + fname + '" class="f">' + fname + '<span class="sz">' + sz_str + ' · ' + mod + '</span></a>'
            else:
                html += '<div style="color:#555;padding:20px">Folder not found</div>'
        html += '</body></html>'
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(html.encode())

    def _check_auth(self):
        if not _WEB_TOKEN:
            return True
        from urllib.parse import parse_qs
        q = parse_qs(urlparse(self.path).query)
        if q.get("token", [""])[0] == _WEB_TOKEN:
            return True
        self.send_error(403, "Forbidden: invalid or missing token")
        return False

    def do_GET(self):
        if not self._check_auth():
            return
        p=urlparse(self.path).path
        if p=="/files" or p.startswith("/files?"):self._files_page();return
        if p in("/","/dashboard"):
            self.send_response(200)
            self.send_header("Content-Type","text/html")
            self.end_headers()
            self.wfile.write(HTML.encode())
        elif p=="/api/dashboard":self._j(_read_dash())
        elif p=="/api/trades":self._j(_read_trades())
        elif p=="/api/multitf":self._j(_read_multitf())
        elif p=="/api/zones":
            zp = os.path.join(BASE, "state", "vrl_zones.json")
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
        else:self.send_error(404)

if __name__=="__main__":
    s=HTTPServer(("0.0.0.0",PORT),H)
    print("VRL War Room v12.14 — http://0.0.0.0:"+str(PORT))
    try:s.serve_forever()
    except KeyboardInterrupt:s.server_close()
