#!/usr/bin/env python3
"""
VRL_WEB.py — VISHAL RAJPUT TRADE Dashboard v12.14
"""
import csv, json, os, sys, glob, time
from datetime import date, datetime, timedelta
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

sys.path.insert(0, os.path.expanduser("~/VISHAL_RAJPUT"))
PORT = 8080
BASE = os.path.expanduser("~")
LAB  = os.path.join(BASE, "lab_data")
STATE_FILE = os.path.join(BASE, "state", "vrl_live_state.json")

def _today(): return date.today().strftime("%Y%m%d")

def _read_csv(path, max_rows=500):
    if not os.path.isfile(path): return []
    try:
        with open(path) as f: rows = list(csv.DictReader(f))
        return rows[-max_rows:] if len(rows) > max_rows else rows
    except: return []

def _read_state():
    if not os.path.isfile(STATE_FILE): return {}
    try:
        with open(STATE_FILE) as f: return json.load(f)
    except: return {}

def _find_latest(directory, prefix):
    if not os.path.isdir(directory): return None
    today_f = os.path.join(directory, prefix + _today() + ".csv")
    if os.path.isfile(today_f) and os.path.getsize(today_f) > 50: return today_f
    files = sorted(glob.glob(os.path.join(directory, prefix + "*.csv")))
    for f in reversed(files):
        if os.path.getsize(f) > 50: return f
    return None

def _find_spot():
    p = _find_latest(os.path.join(LAB, "spot"), "nifty_spot_1min_")
    if p: return p
    alt = os.path.join(LAB, "spot", "nifty_spot_1min.csv")
    return alt if os.path.isfile(alt) else None

def _calc_dte():
    try:
        import VRL_DATA as D
        exp = D.get_nearest_expiry()
        if exp: return (exp - date.today()).days
    except: pass
    today = date.today()
    days_until = (1 - today.weekday()) % 7
    if days_until == 0 and datetime.now().hour >= 16: days_until = 7
    return days_until

def _add_ema_rsi(result):
    if len(result) < 2: return result
    ema9 = result[0]["close"]; ema21 = result[0]["close"]
    gains = []; losses = []; prev_c = result[0]["close"]
    for i, r in enumerate(result):
        c = r["close"]
        ema9 += (2/10) * (c - ema9); ema21 += (2/22) * (c - ema21)
        r["ema9"] = round(ema9, 2); r["ema21"] = round(ema21, 2)
        d = c - prev_c; gains.append(max(d,0)); losses.append(max(-d,0))
        if i >= 14:
            ag = sum(gains[i-13:i+1])/14; al = sum(losses[i-13:i+1])/14
            r["rsi"] = round(100 - 100/(1 + ag/(al+1e-9)), 1)
        else: r["rsi"] = 50
        prev_c = c
    return result

def _get_spot_data(tf="1m", count=100):
    if tf in ("5m","15m"):
        p = _find_latest(os.path.join(LAB, "spot"), "nifty_spot_"+tf.replace("m","min")+"_")
        if p:
            rows = _read_csv(p, 500)
            if rows:
                res = []
                for r in rows:
                    try: res.append({"time":r.get("timestamp","")[-8:-3],"open":float(r.get("open",0)),"high":float(r.get("high",0)),"low":float(r.get("low",0)),"close":float(r.get("close",0)),"volume":int(r.get("volume",0)),"ema9":float(r.get("ema9",0)),"ema21":float(r.get("ema21",0)),"rsi":float(r.get("rsi",50))})
                    except: continue
                return res[-count:]
    path = _find_spot()
    if not path: return []
    rows = _read_csv(path, 500)
    if not rows: return []
    result = []
    for r in rows:
        try: result.append({"time":r.get("timestamp","")[-8:-3],"open":float(r.get("open",0)),"high":float(r.get("high",0)),"low":float(r.get("low",0)),"close":float(r.get("close",0)),"volume":int(r.get("volume",0))})
        except: continue
    mins = {"1m":1,"3m":3,"5m":5,"15m":15}.get(tf, 1)
    if mins > 1 and result:
        resampled = []
        for i in range(0, len(result), mins):
            chunk = result[i:i+mins]
            if chunk: resampled.append({"time":chunk[0]["time"],"open":chunk[0]["open"],"high":max(c["high"] for c in chunk),"low":min(c["low"] for c in chunk),"close":chunk[-1]["close"],"volume":sum(c["volume"] for c in chunk)})
        result = resampled
    return _add_ema_rsi(result)[-count:]

def _get_option_data(tf="3m", count=100):
    tf_map = {"1m":("options_1min","nifty_option_1min_"),"3m":("options_3min","nifty_option_3min_"),"5m":("options_1min","nifty_option_5min_"),"15m":("options_1min","nifty_option_15min_")}
    subdir, prefix = tf_map.get(tf, ("options_3min","nifty_option_3min_"))
    path = _find_latest(os.path.join(LAB, subdir), prefix)
    if not path: return {"CE":[],"PE":[]}
    rows = _read_csv(path, 1000)
    ce, pe = [], []
    for r in rows:
        try:
            entry = {"time":r.get("timestamp","")[-8:-3],"close":float(r.get("close",0)),"rsi":float(r.get("rsi",50)),"volume":int(r.get("volume",0)),"ema9":float(r.get("ema9",0)),"ema21":float(r.get("ema21",r.get("ema9",0))),"ema_spread":float(r.get("ema_spread",r.get("ema9_gap",0))),"body_pct":float(r.get("body_pct",0)),"delta":float(r.get("delta",0)),"iv_pct":float(r.get("iv_pct",0)),"adx":float(r.get("adx",0)),"macd_hist":float(r.get("macd_hist",0))}
            if r.get("type") == "CE": ce.append(entry)
            elif r.get("type") == "PE": pe.append(entry)
        except: continue
    return {"CE":ce[-count:],"PE":pe[-count:]}

def _get_trades():
    path = os.path.join(LAB, "vrl_trade_log.csv")
    rows = _read_csv(path, 200)
    today_str = date.today().isoformat()
    trades = []
    for r in rows:
        if r.get("date","").strip() != today_str: continue
        try: trades.append({"time":r.get("entry_time",""),"dir":r.get("direction",""),"entry":float(r.get("entry_price",0)),"exit":float(r.get("exit_price",0)),"pnl":float(r.get("pnl_pts",0)),"peak":float(r.get("peak_pnl",0)),"trough":float(r.get("trough_pnl",0)),"reason":r.get("exit_reason",""),"score":int(r.get("score",0)),"phase":int(r.get("exit_phase",0)) if r.get("exit_phase") else 0,"session":r.get("session",""),"candles":int(r.get("candles_held",0)),"regime":r.get("regime",""),"bias":r.get("bias",""),"vix":float(r.get("vix_at_entry",0)) if r.get("vix_at_entry") else 0})
        except: continue
    return trades

DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1"><title>VRL</title>
<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.min.js"></script>
<style>
*{margin:0;padding:0;box-sizing:border-box}:root{--bg:#0a0a0f;--cd:#111118;--bd:#1e1e2e;--tx:#e4e4e7;--dm:#71717a;--bl:#3b82f6;--gn:#10b981;--rd:#ef4444;--am:#f59e0b;--pr:#a855f7}
body{background:var(--bg);color:var(--tx);font-family:-apple-system,sans-serif;font-size:13px}
.hd{background:var(--cd);border-bottom:1px solid var(--bd);padding:10px 12px;position:sticky;top:0;z-index:10}
.hd h1{font-size:14px;font-weight:700}.hd b{color:var(--bl)}.hd .v{color:var(--dm);font-size:10px;font-weight:400}
.bg{display:flex;gap:4px;margin-top:5px;flex-wrap:wrap}
.b{padding:2px 6px;border-radius:4px;font-size:9px;font-weight:700;border:1px solid}
.b-g{background:rgba(16,185,129,.15);color:var(--gn);border-color:rgba(16,185,129,.3)}
.b-r{background:rgba(239,68,68,.15);color:var(--rd);border-color:rgba(239,68,68,.3)}
.b-b{background:rgba(59,130,246,.15);color:var(--bl);border-color:rgba(59,130,246,.3)}
.b-a{background:rgba(245,158,11,.15);color:var(--am);border-color:rgba(245,158,11,.3)}
.mg{display:grid;grid-template-columns:1fr 1fr;gap:6px;padding:8px 10px}
.m{background:var(--cd);border:1px solid var(--bd);border-radius:8px;padding:8px}
.m .l{font-size:9px;color:var(--dm);text-transform:uppercase;letter-spacing:.4px}
.m .v{font-size:17px;font-weight:700;margin-top:1px}
.m .s{font-size:9px;color:var(--dm);margin-top:1px}
.tb{display:flex;border-bottom:1px solid var(--bd);padding:0 8px}
.t{padding:7px 12px;font-size:11px;font-weight:600;color:var(--dm);border-bottom:2px solid transparent;cursor:pointer}
.t.on{color:var(--bl);border-color:var(--bl)}
.tf{display:flex;gap:3px;padding:3px;background:rgba(30,30,46,.5);border-radius:6px;margin:8px 10px}
.f{padding:4px 10px;border-radius:4px;font-size:10px;font-weight:700;cursor:pointer;border:none;background:transparent;color:var(--dm)}
.f.on{background:var(--bl);color:#fff}
.cw{margin:0 10px 6px;background:var(--cd);border:1px solid var(--bd);border-radius:8px;padding:5px}
.tr{display:flex;gap:5px;margin:0 10px 5px}
.tg{padding:2px 7px;border-radius:4px;font-size:9px;font-weight:700;cursor:pointer;border:1px solid var(--bd);background:transparent;color:var(--dm)}
.tg.on{border-color:var(--pr);background:rgba(168,85,247,.15);color:var(--pr)}
.vp{margin:0 10px 6px;background:var(--cd);border:1px solid var(--bd);border-radius:6px;padding:6px;display:grid;gap:3px;text-align:center}
.v3{grid-template-columns:repeat(3,1fr)}.v4{grid-template-columns:repeat(4,1fr)}.v6{grid-template-columns:repeat(3,1fr)}
.vb .k{font-size:8px;color:var(--dm);text-transform:uppercase;letter-spacing:.3px}.vb .n{font-size:13px;font-weight:700}
.tc{margin:3px 10px;padding:8px;border-radius:7px;border:1px solid;display:flex;align-items:center;gap:7px}
.tc.w{background:rgba(16,185,129,.05);border-color:rgba(16,185,129,.2)}.tc.l{background:rgba(239,68,68,.05);border-color:rgba(239,68,68,.2)}
.H{display:none}
.ft{text-align:center;padding:5px;font-size:9px;color:var(--dm);border-top:1px solid var(--bd)}
</style></head><body>
<div class="hd"><h1><b>VISHAL RAJPUT</b> TRADE <span class="v">v12.14</span></h1><div class="bg" id="bg"></div></div>
<div class="mg" id="mt"></div>
<div class="tb"><div class="t on" data-t="c" onclick="st('c')">📈 Chart</div><div class="t" data-t="r" onclick="st('r')">📒 Trades</div><div class="t" data-t="p" onclick="st('p')">💹 P&L</div></div>
<div id="p-c">
<div class="tf" id="tfb"></div>
<div class="tr"><button class="tg" id="g-r" onclick="gR()">RSI</button><button class="tg" id="g-c" onclick="gO('CE')">CE</button><button class="tg" id="g-p" onclick="gO('PE')">PE</button></div>
<div class="cw"><canvas id="cs" height="200"></canvas></div>
<div class="vp v6" id="vs"></div>
<div class="cw H" id="wr"><canvas id="cr" height="80"></canvas></div>
<div class="vp v3 H" id="vr"></div>
<div class="cw H" id="wo"><canvas id="co" height="150"></canvas></div>
<div class="vp v4 H" id="vo"></div>
</div>
<div id="p-r" class="H"></div>
<div id="p-p" class="H"><div class="cw"><canvas id="cp" height="180"></canvas></div><div class="mg" id="ps"></div></div>
<div class="ft">Auto-refresh 15s · <span id="ts"></span></div>
<script>
let tf='1m',sR=0,sO='',C={};
const TF=['1m','3m','5m','15m'];
async function A(p){try{return await(await fetch('/api/'+p)).json()}catch(e){return null}}
function st(t){document.querySelectorAll('.t').forEach(e=>e.classList.toggle('on',e.dataset.t===t));['c','r','p'].forEach(i=>document.getElementById('p-'+i).classList.toggle('H',i!==t))}
function rT(){document.getElementById('tfb').innerHTML=TF.map(t=>'<button class="f'+(t===tf?' on':'')+'" onclick="sT(\''+t+'\')">'+t+'</button>').join('')}
function sT(t){tf=t;rT();go()}
function gR(){sR=!sR;document.getElementById('wr').classList.toggle('H',!sR);document.getElementById('vr').classList.toggle('H',!sR);document.getElementById('g-r').classList.toggle('on',sR);go()}
function gO(s){sO=sO===s?'':s;document.getElementById('wo').classList.toggle('H',!sO);document.getElementById('vo').classList.toggle('H',!sO);document.getElementById('g-c').classList.toggle('on',sO==='CE');document.getElementById('g-p').classList.toggle('on',sO==='PE');go()}
function V(l,v,c){return '<div class="vb"><div class="k">'+l+'</div><div class="n" style="color:'+(c||'var(--tx)')+'">'+v+'</div></div>'}
function rB(s,dte){
  let m=(s.mode&&!s.in_trade)?'PAPER':'PAPER';try{m=s.mode||'PAPER'}catch(e){}
  if(!m||m==='')m='PAPER';
  let bi='';try{bi=s._daily_bias||''}catch(e){}
  document.getElementById('bg').innerHTML=[
    '<span class="b b-b">'+m+'</span>',
    '<span class="b b-'+(dte<=0?'r':'b')+'">DTE '+dte+'</span>',
    bi?'<span class="b b-'+(bi==='BULL'?'g':bi==='BEAR'?'r':'a')+'">'+bi+'</span>':'',
  ].filter(Boolean).join('')}
function rM(s){
  const i=s.in_trade||0,dp=+(s.daily_pnl||0),dt=s.daily_trades||0,dl=s.daily_losses||0,dw=dt-dl,ep=+(s.entry_price||0),pp=+(s.peak_pnl||0);
  document.getElementById('mt').innerHTML=
    '<div class="m"><div class="l">'+(i?'🎯 Position':'⏸ Status')+'</div><div class="v" style="color:'+(i?(pp>0?'var(--gn)':'var(--rd)'):'var(--dm)')+'">'+
    (i?(s.direction||'')+' '+(pp>0?'+':'')+pp.toFixed(1)+'pts':'FLAT')+'</div><div class="s">'+(i?'₹'+ep+' Ph'+(s.exit_phase||1):'Scanning...')+'</div></div>'+
    '<div class="m"><div class="l">💰 Day P&L</div><div class="v" style="color:'+(dp>=0?'var(--gn)':'var(--rd)')+ '">'+(dp>=0?'+':'')+dp.toFixed(1)+'pts</div><div class="s">₹'+Math.round(dp*65)+' · '+dt+'T W'+dw+' L'+dl+'</div></div>'}
function rS(d){
  if(C.s)C.s.destroy();if(!d||!d.length)return;
  C.s=new Chart(document.getElementById('cs'),{type:'line',data:{labels:d.map(x=>x.time),datasets:[
    {label:'Spot',data:d.map(x=>x.close),borderColor:'#3b82f6',borderWidth:2,pointRadius:0,fill:true,backgroundColor:'rgba(59,130,246,.08)',tension:.3},
    {label:'EMA9',data:d.map(x=>x.ema9),borderColor:'#10b981',borderWidth:1.5,pointRadius:0,tension:.3},
    {label:'EMA21',data:d.map(x=>x.ema21),borderColor:'#f59e0b',borderWidth:1.5,pointRadius:0,borderDash:[4,2],tension:.3}
  ]},options:{responsive:true,animation:{duration:300},scales:{x:{ticks:{color:'#555',font:{size:8},maxTicksLimit:8},grid:{color:'#1a1a2e'}},y:{ticks:{color:'#555',font:{size:8}},grid:{color:'#1a1a2e'}}},plugins:{legend:{labels:{color:'#888',font:{size:8}}}}}});
  const L=d[d.length-1],sp=+(L.ema9-L.ema21).toFixed(1),ch=+(L.close-d[0].close).toFixed(1),hi=Math.max(...d.map(x=>x.high||x.close)),lo=Math.min(...d.map(x=>x.low||x.close));
  document.getElementById('vs').innerHTML=V('SPOT',L.close.toFixed(1),'var(--bl)')+V('EMA9',L.ema9.toFixed(1),'var(--gn)')+V('EMA21',L.ema21.toFixed(1),'var(--am)')+V('SPREAD',(sp>0?'+':'')+sp,sp>0?'var(--gn)':sp<0?'var(--rd)':'var(--dm)')+V('CHANGE',(ch>=0?'+':'')+ch,ch>=0?'var(--gn)':'var(--rd)')+V('RANGE',(hi-lo).toFixed(0)+'pts','var(--dm)')}
function rRI(d){
  if(C.r)C.r.destroy();if(!d||!d.length)return;
  C.r=new Chart(document.getElementById('cr'),{type:'line',data:{labels:d.map(x=>x.time),datasets:[{label:'RSI',data:d.map(x=>x.rsi),borderColor:'#a855f7',borderWidth:1.5,pointRadius:0,tension:.3}]},options:{responsive:true,animation:{duration:300},scales:{x:{display:false},y:{min:15,max:85,ticks:{color:'#555',font:{size:7}},grid:{color:'#1a1a2e'}}},plugins:{legend:{display:false}}}});
  const L=d[d.length-1],P=d.length>1?d[d.length-2]:L,u=L.rsi>P.rsi,z=L.rsi>=70?'OVERBOUGHT':L.rsi<=30?'OVERSOLD':L.rsi>=60?'STRONG':L.rsi<=40?'WEAK':'NEUTRAL',zc=L.rsi>=70?'var(--rd)':L.rsi<=30?'var(--gn)':'var(--dm)';
  document.getElementById('vr').innerHTML=V('RSI',L.rsi.toFixed(1),L.rsi>=60?'var(--gn)':L.rsi<=40?'var(--rd)':'var(--am)')+V('TREND',u?'RISING ↑':'FALLING ↓',u?'var(--gn)':'var(--rd)')+V('ZONE',z,zc)}
function rO(data,side){
  if(C.o)C.o.destroy();const d=data[side]||[];if(!d.length)return;
  const cl=side==='CE'?'#10b981':'#ef4444',ds=[{label:side,data:d.map(x=>x.close),borderColor:cl,borderWidth:2,pointRadius:0,fill:true,backgroundColor:cl+'15',tension:.3}];
  if(d[0].ema9)ds.push({label:'EMA9',data:d.map(x=>x.ema9||x.close),borderColor:'#10b981',borderWidth:1,pointRadius:0,tension:.3});
  if(d[0].ema21&&d[0].ema21!==d[0].ema9)ds.push({label:'EMA21',data:d.map(x=>x.ema21||x.close),borderColor:'#f59e0b',borderWidth:1,pointRadius:0,borderDash:[4,2],tension:.3});
  C.o=new Chart(document.getElementById('co'),{type:'line',data:{labels:d.map(x=>x.time),datasets:ds},options:{responsive:true,animation:{duration:300},scales:{x:{ticks:{color:'#555',font:{size:8},maxTicksLimit:8},grid:{color:'#1a1a2e'}},y:{ticks:{color:'#555',font:{size:8}},grid:{color:'#1a1a2e'}}},plugins:{legend:{labels:{color:'#888',font:{size:8}}}}}});
  const L=d[d.length-1],sp=((L.ema9||0)-(L.ema21||0)).toFixed(1),ch=(L.close-d[0].close).toFixed(1);
  document.getElementById('vo').innerHTML=V(side,'₹'+L.close.toFixed(1),side==='CE'?'var(--gn)':'var(--rd)')+V('EMA9','₹'+(L.ema9||0).toFixed(1),'var(--gn)')+V('EMA21','₹'+(L.ema21||0).toFixed(1),'var(--am)')+V('SPREAD',(sp>0?'+':'')+sp,sp>0?'var(--gn)':sp<0?'var(--rd)':'var(--dm)')+V('RSI',(L.rsi||0).toFixed(1),'')+V('DELTA',(L.delta||0).toFixed(2),'')+V('IV',(L.iv_pct||0).toFixed(1)+'%','')+V('CHG',(ch>=0?'+':'')+ch,ch>=0?'var(--gn)':'var(--rd)')}
function rTR(tr){
  const e=document.getElementById('p-r');
  if(!tr||!tr.length){e.innerHTML='<div style="text-align:center;color:var(--dm);padding:30px">No trades today</div>';return}
  e.innerHTML='<div style="font-size:11px;font-weight:700;color:var(--dm);padding:6px 10px">Today ('+tr.length+')</div>'+tr.map(t=>{const w=t.pnl>0;return '<div class="tc '+(w?'w':'l')+'"><div style="font-size:18px">'+(w?'✅':'❌')+'</div><div style="flex:1"><div style="font-weight:700;font-size:11px;color:'+(t.dir==='CE'?'var(--gn)':'var(--rd)')+'">'+t.dir+' <span style="color:var(--dm);font-size:9px">'+t.time+' Ph'+t.phase+' '+t.session+'</span></div><div style="font-size:9px;color:var(--dm)">₹'+t.entry+' → ₹'+t.exit+' · '+(t.reason||'').replace(/_/g,' ')+'</div></div><div style="text-align:right"><div style="font-weight:700;font-size:13px;color:'+(w?'var(--gn)':'var(--rd)')+'">'+(w?'+':'')+t.pnl.toFixed(1)+'pts</div><div style="font-size:8px;color:var(--dm)">↑'+t.peak.toFixed(1)+' ↓'+t.trough.toFixed(1)+'</div></div></div>'}).join('')}
function rP(tr){
  if(C.p)C.p.destroy();if(!tr||!tr.length)return;let c=0;const d=tr.map(t=>{c+=t.pnl;return{time:t.time,pnl:Math.round(c*10)/10}});
  C.p=new Chart(document.getElementById('cp'),{type:'line',data:{labels:d.map(x=>x.time),datasets:[{label:'P&L',data:d.map(x=>x.pnl),borderColor:c>=0?'#10b981':'#ef4444',borderWidth:2.5,pointRadius:4,pointBackgroundColor:d.map(x=>x.pnl>=0?'#10b981':'#ef4444'),fill:true,backgroundColor:c>=0?'rgba(16,185,129,.1)':'rgba(239,68,68,.1)',tension:.3}]},options:{responsive:true,animation:{duration:300},scales:{x:{ticks:{color:'#555',font:{size:8}},grid:{color:'#1a1a2e'}},y:{ticks:{color:'#555',font:{size:8}},grid:{color:'#1a1a2e'}}},plugins:{legend:{display:false}}}});
  const w=tr.filter(t=>t.pnl>0),ap=(tr.reduce((s,t)=>s+t.peak,0)/tr.length).toFixed(1),at=(tr.reduce((s,t)=>s+t.trough,0)/tr.length).toFixed(1);
  document.getElementById('ps').innerHTML='<div class="m"><div class="l">🎯 Win Rate</div><div class="v">'+Math.round(w.length/tr.length*100)+'%</div></div><div class="m"><div class="l">📊 Avg Peak</div><div class="v">+'+ap+'</div></div><div class="m"><div class="l">📉 Avg Trough</div><div class="v">'+at+'</div></div>'}
async function go(){
  const[sd,od,tr,s,dte]=await Promise.all([A('spot?tf='+tf),A('options?tf='+tf),A('trades'),A('state'),A('dte')]);
  if(s){rB(s,dte||0);rM(s)}
  if(sd&&sd.length)rS(sd);else document.getElementById('vs').innerHTML='<div style="grid-column:1/-1;color:var(--dm);font-size:11px;padding:8px">No spot data for this timeframe</div>';
  if(sR&&sd&&sd.length)rRI(sd);
  if(sO&&od)rO(od,sO);
  rTR(tr);rP(tr);
  document.getElementById('ts').textContent=new Date().toLocaleTimeString('en-IN')}
rT();go();setInterval(go,15000);
</script></body></html>"""

class Handler(BaseHTTPRequestHandler):
    def log_message(self,*a):pass
    def _j(self,d):
        self.send_response(200);self.send_header("Content-Type","application/json");self.send_header("Access-Control-Allow-Origin","*");self.end_headers()
        self.wfile.write(json.dumps(d,default=str).encode())
    def _h(self,c):self.send_response(200);self.send_header("Content-Type","text/html");self.end_headers();self.wfile.write(c.encode())
    def do_GET(self):
        p=urlparse(self.path);path=p.path;q=parse_qs(p.query)
        if path in("/","/dashboard"):self._h(DASHBOARD_HTML)
        elif path=="/api/spot":self._j(_get_spot_data(q.get("tf",["1m"])[0]))
        elif path=="/api/options":self._j(_get_option_data(q.get("tf",["3m"])[0]))
        elif path=="/api/trades":self._j(_get_trades())
        elif path=="/api/state":self._j(_read_state())
        elif path=="/api/dte":self._j(_calc_dte())
        else:self.send_error(404)

if __name__=="__main__":
    s=HTTPServer(("0.0.0.0",PORT),Handler)
    print("VRL Dashboard v12.14 — http://0.0.0.0:"+str(PORT))
    try:s.serve_forever()
    except KeyboardInterrupt:s.server_close()
