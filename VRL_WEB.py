#!/usr/bin/env python3
"""
═══════════════════════════════════════════════════════════════
 VRL_WEB.py — VISHAL RAJPUT TRADE Dashboard Server v12.14
 Run: python3 VRL_WEB.py
 Open: http://YOUR_SERVER_IP:8080
 Reads live data from CSV files + bot state
═══════════════════════════════════════════════════════════════
"""

import csv
import json
import os
import sys
import time
from datetime import date, datetime, timedelta
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

sys.path.insert(0, os.path.expanduser("~/VISHAL_RAJPUT"))

PORT = 8080
BASE = os.path.expanduser("~")
LAB  = os.path.join(BASE, "lab_data")
STATE_FILE = os.path.join(BASE, "state", "vrl_live_state.json")


def _today():
    return date.today().strftime("%Y%m%d")


def _read_csv(path, max_rows=500):
    if not os.path.isfile(path):
        return []
    try:
        with open(path) as f:
            rows = list(csv.DictReader(f))
        return rows[-max_rows:] if len(rows) > max_rows else rows
    except Exception:
        return []


def _read_state():
    if not os.path.isfile(STATE_FILE):
        return {}
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def _get_spot_data(tf="1m", count=100):
    """Read spot 1-min CSV, optionally resample."""
    path = os.path.join(LAB, "spot", "nifty_spot_1min_" + _today() + ".csv")
    rows = _read_csv(path, 500)
    if not rows:
        return []

    result = []
    for r in rows:
        try:
            result.append({
                "time": r.get("timestamp", "")[-8:-3],  # HH:MM
                "open": float(r.get("open", 0)),
                "high": float(r.get("high", 0)),
                "low": float(r.get("low", 0)),
                "close": float(r.get("close", 0)),
                "volume": int(r.get("volume", 0)),
            })
        except Exception:
            continue

    # Resample for higher timeframes
    mins = {"1m": 1, "3m": 3, "5m": 5, "15m": 15}.get(tf, 1)
    if mins > 1 and result:
        resampled = []
        for i in range(0, len(result), mins):
            chunk = result[i:i + mins]
            if chunk:
                resampled.append({
                    "time": chunk[0]["time"],
                    "open": chunk[0]["open"],
                    "high": max(c["high"] for c in chunk),
                    "low": min(c["low"] for c in chunk),
                    "close": chunk[-1]["close"],
                    "volume": sum(c["volume"] for c in chunk),
                })
        result = resampled

    # Add EMA9, EMA21, RSI
    if len(result) >= 2:
        ema9 = result[0]["close"]
        ema21 = result[0]["close"]
        gains = []
        losses = []
        prev_close = result[0]["close"]

        for i, r in enumerate(result):
            c = r["close"]
            ema9 = ema9 + (2 / 10) * (c - ema9)
            ema21 = ema21 + (2 / 22) * (c - ema21)
            r["ema9"] = round(ema9, 2)
            r["ema21"] = round(ema21, 2)

            delta = c - prev_close
            gain = max(delta, 0)
            loss = max(-delta, 0)
            gains.append(gain)
            losses.append(loss)

            if i >= 14:
                avg_gain = sum(gains[i-13:i+1]) / 14
                avg_loss = sum(losses[i-13:i+1]) / 14
                if avg_loss > 0:
                    rs = avg_gain / avg_loss
                    r["rsi"] = round(100 - 100 / (1 + rs), 1)
                else:
                    r["rsi"] = 100
            else:
                r["rsi"] = 50

            prev_close = c

    return result[-count:]


def _get_option_data(tf="3m", count=100):
    """Read option 3-min or 1-min CSV for CE and PE."""
    if tf in ("1m",):
        path = os.path.join(LAB, "options_1min", "nifty_option_1min_" + _today() + ".csv")
    else:
        path = os.path.join(LAB, "options_3min", "nifty_option_3min_" + _today() + ".csv")

    rows = _read_csv(path, 1000)
    ce_data = []
    pe_data = []
    for r in rows:
        try:
            entry = {
                "time": r.get("timestamp", "")[-8:-3],
                "close": float(r.get("close", 0)),
                "rsi": float(r.get("rsi", 50)),
                "volume": int(r.get("volume", 0)),
                "ema9": float(r.get("ema9", 0)),
                "body_pct": float(r.get("body_pct", 0)),
                "delta": float(r.get("delta", 0)),
                "iv_pct": float(r.get("iv_pct", 0)),
            }
            if r.get("type") == "CE":
                ce_data.append(entry)
            elif r.get("type") == "PE":
                pe_data.append(entry)
        except Exception:
            continue

    return {"CE": ce_data[-count:], "PE": pe_data[-count:]}


def _get_trades():
    """Read today's trades from trade log."""
    path = os.path.join(LAB, "vrl_trade_log.csv")
    rows = _read_csv(path, 200)
    today_str = date.today().isoformat()
    trades = []
    for r in rows:
        if r.get("date", "").strip() != today_str:
            continue
        try:
            trades.append({
                "time": r.get("entry_time", ""),
                "dir": r.get("direction", ""),
                "entry": float(r.get("entry_price", 0)),
                "exit": float(r.get("exit_price", 0)),
                "pnl": float(r.get("pnl_pts", 0)),
                "peak": float(r.get("peak_pnl", 0)),
                "trough": float(r.get("trough_pnl", 0)),
                "reason": r.get("exit_reason", ""),
                "score": int(r.get("score", 0)),
                "phase": int(r.get("exit_phase", 0)) if r.get("exit_phase") else 0,
                "session": r.get("session", ""),
                "candles": int(r.get("candles_held", 0)),
                "regime": r.get("regime", ""),
                "bias": r.get("bias", ""),
                "vix": float(r.get("vix_at_entry", 0)) if r.get("vix_at_entry") else 0,
            })
        except Exception:
            continue
    return trades


def _get_scan_stats():
    """Quick scan stats from today's scan log."""
    path = os.path.join(LAB, "options_1min", "nifty_signal_scan_" + _today() + ".csv")
    rows = _read_csv(path, 2000)
    if not rows:
        return {"total": 0, "fired": 0, "blocked": {}}

    fired = sum(1 for r in rows if r.get("fired") == "1")
    reasons = {}
    for r in rows:
        if r.get("fired") != "1":
            reason = r.get("reject_reason", "UNKNOWN")
            reasons[reason] = reasons.get(reason, 0) + 1

    return {"total": len(rows), "fired": fired, "blocked": reasons}


def _get_daily_summary():
    """Read daily summary if exists."""
    path = os.path.join(LAB, "reports", "vrl_daily_summary.csv")
    rows = _read_csv(path, 30)
    return rows[-10:] if rows else []


# ─── HTML DASHBOARD ────────────────────────────────

DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0">
<title>VRL Dashboard</title>
<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.min.js"></script>
<style>
  :root { --bg: #0a0a0f; --card: #111118; --border: #1e1e2e; --text: #e4e4e7; --dim: #71717a; --blue: #3b82f6; --green: #10b981; --red: #ef4444; --amber: #f59e0b; --purple: #a855f7; }
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body { background: var(--bg); color: var(--text); font-family: -apple-system, 'SF Pro', 'Segoe UI', sans-serif; font-size: 14px; overflow-x: hidden; }
  .header { background: var(--card); border-bottom: 1px solid var(--border); padding: 12px 16px; position: sticky; top: 0; z-index: 10; }
  .header h1 { font-size: 16px; font-weight: 700; } .header h1 span { color: var(--blue); }
  .badges { display: flex; gap: 6px; margin-top: 8px; flex-wrap: wrap; }
  .badge { padding: 3px 8px; border-radius: 6px; font-size: 11px; font-weight: 600; border: 1px solid; }
  .badge-green { background: rgba(16,185,129,.15); color: var(--green); border-color: rgba(16,185,129,.3); }
  .badge-red { background: rgba(239,68,68,.15); color: var(--red); border-color: rgba(239,68,68,.3); }
  .badge-blue { background: rgba(59,130,246,.15); color: var(--blue); border-color: rgba(59,130,246,.3); }
  .badge-amber { background: rgba(245,158,11,.15); color: var(--amber); border-color: rgba(245,158,11,.3); }
  .metrics { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; padding: 12px; }
  .metric { background: var(--card); border: 1px solid var(--border); border-radius: 12px; padding: 12px; }
  .metric .label { font-size: 11px; color: var(--dim); text-transform: uppercase; letter-spacing: .5px; }
  .metric .value { font-size: 20px; font-weight: 700; margin-top: 2px; }
  .metric .sub { font-size: 11px; color: var(--dim); margin-top: 2px; }
  .tabs { display: flex; border-bottom: 1px solid var(--border); padding: 0 12px; }
  .tab { padding: 10px 16px; font-size: 13px; font-weight: 600; color: var(--dim); border-bottom: 2px solid transparent; cursor: pointer; }
  .tab.active { color: var(--blue); border-color: var(--blue); }
  .tf-bar { display: flex; gap: 4px; padding: 4px; background: rgba(30,30,46,.5); border-radius: 8px; margin: 12px; }
  .tf-btn { padding: 6px 12px; border-radius: 6px; font-size: 12px; font-weight: 700; cursor: pointer; border: none; background: transparent; color: var(--dim); }
  .tf-btn.active { background: var(--blue); color: #fff; box-shadow: 0 2px 8px rgba(59,130,246,.3); }
  .chart-wrap { margin: 0 12px 12px; background: var(--card); border: 1px solid var(--border); border-radius: 12px; padding: 8px; }
  .toggle-row { display: flex; gap: 8px; margin: 0 12px 8px; }
  .toggle-btn { padding: 4px 10px; border-radius: 6px; font-size: 11px; font-weight: 600; cursor: pointer; border: 1px solid var(--border); background: transparent; color: var(--dim); }
  .toggle-btn.on { border-color: var(--purple); background: rgba(168,85,247,.15); color: var(--purple); }
  .trade-card { margin: 4px 12px; padding: 12px; border-radius: 10px; border: 1px solid; display: flex; align-items: center; gap: 10px; }
  .trade-card.win { background: rgba(16,185,129,.05); border-color: rgba(16,185,129,.2); }
  .trade-card.loss { background: rgba(239,68,68,.05); border-color: rgba(239,68,68,.2); }
  .trade-info { flex: 1; }
  .trade-dir { font-weight: 700; font-size: 13px; }
  .trade-detail { font-size: 11px; color: var(--dim); margin-top: 2px; }
  .trade-pnl { text-align: right; font-weight: 700; font-size: 15px; }
  .trade-peaks { font-size: 10px; color: var(--dim); }
  .pos-bar { margin: 0 12px 8px; background: linear-gradient(135deg, rgba(59,130,246,.08), transparent); border: 1px solid rgba(59,130,246,.2); border-radius: 12px; padding: 12px; }
  .progress { height: 8px; background: #1e1e2e; border-radius: 99px; overflow: hidden; position: relative; margin: 8px 0; }
  .progress-fill { height: 100%; border-radius: 99px; transition: width .5s; }
  .footer { text-align: center; padding: 8px; font-size: 11px; color: var(--dim); border-top: 1px solid var(--border); }
  .section-title { font-size: 13px; font-weight: 700; color: var(--dim); padding: 8px 12px 4px; }
  .hidden { display: none; }
  @media (min-width: 600px) { .metrics { grid-template-columns: repeat(4, 1fr); } }
</style>
</head>
<body>

<div class="header">
  <h1><span>VISHAL RAJPUT</span> TRADE <span style="color:var(--dim);font-size:12px;font-weight:400">v12.14</span></h1>
  <div class="badges" id="badges"></div>
</div>

<div class="metrics" id="metrics"></div>
<div id="position-bar"></div>

<div class="tabs">
  <div class="tab active" data-tab="chart" onclick="switchTab('chart')">📈 Chart</div>
  <div class="tab" data-tab="trades" onclick="switchTab('trades')">📒 Trades</div>
  <div class="tab" data-tab="pnl" onclick="switchTab('pnl')">💹 P&L</div>
</div>

<div id="tab-chart">
  <div class="tf-bar" id="tf-bar"></div>
  <div class="toggle-row">
    <button class="toggle-btn" id="btn-rsi" onclick="toggleRSI()">RSI</button>
    <button class="toggle-btn" id="btn-ce" onclick="toggleOption('CE')">CE</button>
    <button class="toggle-btn" id="btn-pe" onclick="toggleOption('PE')">PE</button>
  </div>
  <div class="chart-wrap"><canvas id="spotChart" height="200"></canvas></div>
  <div class="chart-wrap hidden" id="rsi-wrap"><canvas id="rsiChart" height="80"></canvas></div>
  <div class="chart-wrap hidden" id="opt-wrap"><canvas id="optChart" height="150"></canvas></div>
</div>

<div id="tab-trades" class="hidden"></div>
<div id="tab-pnl" class="hidden">
  <div class="chart-wrap"><canvas id="pnlChart" height="180"></canvas></div>
  <div class="metrics" id="trade-stats"></div>
</div>

<div class="footer">Auto-refresh 15s · <span id="last-update"></span></div>

<script>
let activeTF = '3m', showRSI = false, showOpt = '', charts = {};
const TFs = ['1m','3m','5m','15m'];

// ── API fetch ──
async function api(path) {
  try { const r = await fetch('/api/' + path); return await r.json(); }
  catch(e) { console.error(e); return null; }
}

// ── Tab switch ──
function switchTab(t) {
  document.querySelectorAll('.tab').forEach(el => el.classList.toggle('active', el.dataset.tab === t));
  ['chart','trades','pnl'].forEach(id => {
    document.getElementById('tab-' + id).classList.toggle('hidden', id !== t);
  });
}

// ── Timeframe buttons ──
function renderTFBar() {
  document.getElementById('tf-bar').innerHTML = TFs.map(tf =>
    `<button class="tf-btn ${tf===activeTF?'active':''}" onclick="setTF('${tf}')">${tf}</button>`
  ).join('');
}

function setTF(tf) { activeTF = tf; renderTFBar(); refresh(); }

function toggleRSI() { showRSI = !showRSI; document.getElementById('rsi-wrap').classList.toggle('hidden', !showRSI); document.getElementById('btn-rsi').classList.toggle('on', showRSI); refresh(); }

function toggleOption(side) {
  showOpt = showOpt === side ? '' : side;
  document.getElementById('opt-wrap').classList.toggle('hidden', !showOpt);
  document.getElementById('btn-ce').classList.toggle('on', showOpt === 'CE');
  document.getElementById('btn-pe').classList.toggle('on', showOpt === 'PE');
  refresh();
}

// ── Render ──
function renderBadges(state) {
  const s = state || {};
  const mode = s.mode || 'PAPER';
  const bias = s._daily_bias || 'UNKNOWN';
  const vix = s._vix || 0;
  const regime = s.regime_at_entry || '';
  const dte = s.dte_at_entry || 0;
  document.getElementById('badges').innerHTML = [
    `<span class="badge badge-${mode==='LIVE'?'green':'blue'}">${mode}</span>`,
    bias !== 'UNKNOWN' ? `<span class="badge badge-${bias==='BULL'?'green':bias==='BEAR'?'red':'amber'}">${bias}</span>` : '',
    vix > 0 ? `<span class="badge badge-${vix>22?'red':vix>18?'amber':'green'}">VIX ${vix.toFixed?vix.toFixed(1):vix}</span>` : '',
    dte >= 0 ? `<span class="badge badge-${dte===0?'red':'blue'}">DTE ${dte}</span>` : '',
  ].filter(Boolean).join('');
}

function renderMetrics(state, trades) {
  const s = state || {};
  const inTrade = s.in_trade || false;
  const entry = s.entry_price || 0;
  const pnl = s.peak_pnl || 0;
  const dailyPnl = s.daily_pnl || 0;
  const dt = s.daily_trades || 0;
  const dl = s.daily_losses || 0;
  const dw = dt - dl;

  document.getElementById('metrics').innerHTML = `
    <div class="metric">
      <div class="label">${inTrade ? '🎯 Position' : '⏸ Status'}</div>
      <div class="value" style="color:${inTrade?(pnl>0?'var(--green)':'var(--red)'):'var(--dim)'}">
        ${inTrade ? (s.direction||'') + ' ' + (pnl>0?'+':'') + pnl.toFixed?pnl.toFixed(1)+'pts' : 'FLAT' : 'FLAT'}
      </div>
      <div class="sub">${inTrade ? '₹'+entry+' Ph'+(s.exit_phase||1) : 'Scanning...'}</div>
    </div>
    <div class="metric">
      <div class="label">💰 Day P&L</div>
      <div class="value" style="color:${dailyPnl>=0?'var(--green)':'var(--red)'}">
        ${dailyPnl>=0?'+':''}${(+dailyPnl).toFixed(1)}pts
      </div>
      <div class="sub">₹${Math.round(dailyPnl*65)} · ${dt}T W${dw} L${dl}</div>
    </div>
  `;
}

function renderPosition(state) {
  const s = state || {};
  if (!s.in_trade) { document.getElementById('position-bar').innerHTML = ''; return; }
  const entry = +s.entry_price || 0;
  const sl = +s.phase1_sl || 0;
  const peak = +s.peak_pnl || 0;
  const trough = +s.trough_pnl || 0;
  const pnl = +s.daily_pnl || 0;
  const pct = peak > 0 ? Math.min(90, 25 + (pnl / peak) * 55) : 30;

  document.getElementById('position-bar').innerHTML = `
    <div class="pos-bar">
      <div style="display:flex;justify-content:space-between;font-size:12px;font-weight:700">
        <span style="color:var(--blue)">${s.symbol||''}</span>
        <span style="color:var(--dim)">Score ${s.score_at_entry||0}/7 · Ph${s.exit_phase||1}</span>
      </div>
      <div class="progress">
        <div class="progress-fill" style="width:25%;background:rgba(239,68,68,.3)"></div>
        <div class="progress-fill" style="width:${pct}%;background:rgba(16,185,129,.3);position:absolute;left:25%;top:0;height:100%"></div>
      </div>
      <div style="display:flex;justify-content:space-between;font-size:11px;color:var(--dim)">
        <span style="color:var(--red)">SL ₹${sl.toFixed?sl.toFixed(1):sl}</span>
        <span>Entry ₹${entry}</span>
        <span style="color:var(--green)">Peak +${peak.toFixed?peak.toFixed(1):peak}pts</span>
      </div>
      <div style="display:flex;justify-content:space-between;font-size:10px;color:#555;margin-top:4px">
        <span>Trough: ${trough.toFixed?trough.toFixed(1):trough}pts</span>
        <span>Candles: ${s.candles_held||0}</span>
      </div>
    </div>`;
}

function renderTrades(trades) {
  const el = document.getElementById('tab-trades');
  if (!trades || !trades.length) { el.innerHTML = '<div style="text-align:center;color:var(--dim);padding:40px">No trades yet today</div>'; return; }
  el.innerHTML = '<div class="section-title">Today\'s Trades (' + trades.length + ')</div>' +
    trades.map(t => {
      const win = t.pnl > 0;
      return `<div class="trade-card ${win?'win':'loss'}">
        <div style="font-size:22px">${win?'✅':'❌'}</div>
        <div class="trade-info">
          <div class="trade-dir" style="color:${t.dir==='CE'?'var(--green)':'var(--red)'}">${t.dir} <span style="color:var(--dim);font-weight:400;font-size:11px">${t.time} Ph${t.phase} ${t.session}</span></div>
          <div class="trade-detail">₹${t.entry} → ₹${t.exit} · ${(t.reason||'').replace(/_/g,' ')}</div>
        </div>
        <div>
          <div class="trade-pnl" style="color:${win?'var(--green)':'var(--red)'}">${win?'+':''}${t.pnl.toFixed(1)}pts</div>
          <div class="trade-peaks">↑${t.peak.toFixed(1)} ↓${t.trough.toFixed(1)}</div>
        </div>
      </div>`;
    }).join('');
}

// ── Charts ──
function renderSpotChart(data) {
  if (charts.spot) charts.spot.destroy();
  const ctx = document.getElementById('spotChart').getContext('2d');
  charts.spot = new Chart(ctx, {
    type: 'line',
    data: {
      labels: data.map(d => d.time),
      datasets: [
        { label: 'Spot', data: data.map(d => d.close), borderColor: '#3b82f6', borderWidth: 2, pointRadius: 0, fill: true, backgroundColor: 'rgba(59,130,246,.08)', tension: 0.3 },
        { label: 'EMA9', data: data.map(d => d.ema9), borderColor: '#10b981', borderWidth: 1.5, pointRadius: 0, borderDash: [], tension: 0.3 },
        { label: 'EMA21', data: data.map(d => d.ema21), borderColor: '#f59e0b', borderWidth: 1.5, pointRadius: 0, borderDash: [4,2], tension: 0.3 },
      ]
    },
    options: {
      responsive: true, animation: { duration: 400 },
      scales: {
        x: { ticks: { color: '#555', font: { size: 10 }, maxTicksLimit: 10 }, grid: { color: '#1a1a2e' } },
        y: { ticks: { color: '#555', font: { size: 10 } }, grid: { color: '#1a1a2e' } }
      },
      plugins: { legend: { labels: { color: '#888', font: { size: 10 } } } }
    }
  });
}

function renderRSIChart(data) {
  if (charts.rsi) charts.rsi.destroy();
  const ctx = document.getElementById('rsiChart').getContext('2d');
  charts.rsi = new Chart(ctx, {
    type: 'line',
    data: {
      labels: data.map(d => d.time),
      datasets: [
        { label: 'RSI', data: data.map(d => d.rsi), borderColor: '#a855f7', borderWidth: 1.5, pointRadius: 0, tension: 0.3 }
      ]
    },
    options: {
      responsive: true, animation: { duration: 400 },
      scales: {
        x: { display: false },
        y: { min: 20, max: 80, ticks: { color: '#555', font: { size: 9 } }, grid: { color: '#1a1a2e' } }
      },
      plugins: {
        legend: { display: false },
        annotation: { annotations: {
          ob: { type: 'line', yMin: 70, yMax: 70, borderColor: 'rgba(239,68,68,.3)', borderDash: [3,3] },
          os: { type: 'line', yMin: 30, yMax: 30, borderColor: 'rgba(16,185,129,.3)', borderDash: [3,3] },
        }}
      }
    }
  });
}

function renderOptionChart(data, side) {
  if (charts.opt) charts.opt.destroy();
  const d = data[side] || [];
  if (!d.length) return;
  const ctx = document.getElementById('optChart').getContext('2d');
  const color = side === 'CE' ? '#10b981' : '#ef4444';
  charts.opt = new Chart(ctx, {
    type: 'line',
    data: {
      labels: d.map(x => x.time),
      datasets: [
        { label: side + ' LTP', data: d.map(x => x.close), borderColor: color, borderWidth: 2, pointRadius: 0, fill: true, backgroundColor: color + '15', tension: 0.3 }
      ]
    },
    options: {
      responsive: true, animation: { duration: 400 },
      scales: {
        x: { ticks: { color: '#555', font: { size: 10 }, maxTicksLimit: 8 }, grid: { color: '#1a1a2e' } },
        y: { ticks: { color: '#555', font: { size: 10 } }, grid: { color: '#1a1a2e' } }
      },
      plugins: { legend: { labels: { color: '#888', font: { size: 10 } } } }
    }
  });
}

function renderPnLChart(trades) {
  if (charts.pnl) charts.pnl.destroy();
  if (!trades || !trades.length) return;
  let cumPnl = 0;
  const data = trades.map(t => { cumPnl += t.pnl; return { time: t.time, pnl: Math.round(cumPnl*10)/10 }; });
  const ctx = document.getElementById('pnlChart').getContext('2d');
  charts.pnl = new Chart(ctx, {
    type: 'line',
    data: {
      labels: data.map(d => d.time),
      datasets: [{
        label: 'Cumulative P&L',
        data: data.map(d => d.pnl),
        borderColor: cumPnl >= 0 ? '#10b981' : '#ef4444',
        borderWidth: 2.5, pointRadius: 4, pointBackgroundColor: data.map(d => d.pnl >= 0 ? '#10b981' : '#ef4444'),
        fill: true, backgroundColor: cumPnl >= 0 ? 'rgba(16,185,129,.1)' : 'rgba(239,68,68,.1)', tension: 0.3,
      }]
    },
    options: {
      responsive: true, animation: { duration: 400 },
      scales: {
        x: { ticks: { color: '#555', font: { size: 10 } }, grid: { color: '#1a1a2e' } },
        y: { ticks: { color: '#555', font: { size: 10 } }, grid: { color: '#1a1a2e' } }
      },
      plugins: { legend: { display: false } }
    }
  });

  // Stats
  const wins = trades.filter(t => t.pnl > 0);
  const avgPeak = trades.reduce((s,t) => s + (t.peak||0), 0) / trades.length;
  const avgTrough = trades.reduce((s,t) => s + (t.trough||0), 0) / trades.length;
  document.getElementById('trade-stats').innerHTML = `
    <div class="metric"><div class="label">🎯 Win Rate</div><div class="value">${Math.round(wins.length/trades.length*100)}%</div></div>
    <div class="metric"><div class="label">📊 Avg Peak</div><div class="value">+${avgPeak.toFixed(1)}</div></div>
    <div class="metric"><div class="label">📉 Avg Trough</div><div class="value">${avgTrough.toFixed(1)}</div></div>
  `;
}

// ── Main refresh ──
async function refresh() {
  const [spotData, optData, trades, state] = await Promise.all([
    api('spot?tf=' + activeTF),
    api('options?tf=' + activeTF),
    api('trades'),
    api('state'),
  ]);

  renderBadges(state);
  renderMetrics(state, trades);
  renderPosition(state);
  if (spotData && spotData.length) renderSpotChart(spotData);
  if (showRSI && spotData) renderRSIChart(spotData);
  if (showOpt && optData) renderOptionChart(optData, showOpt);
  renderTrades(trades);
  renderPnLChart(trades);
  document.getElementById('last-update').textContent = new Date().toLocaleTimeString('en-IN');
}

renderTFBar();
refresh();
setInterval(refresh, 15000);
</script>
</body>
</html>"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass  # Suppress logs

    def _json(self, data):
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(json.dumps(data, default=str).encode())

    def _html(self, content):
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(content.encode())

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        params = parse_qs(parsed.query)

        if path == "/" or path == "/dashboard":
            self._html(DASHBOARD_HTML)
        elif path == "/api/spot":
            tf = params.get("tf", ["3m"])[0]
            self._json(_get_spot_data(tf))
        elif path == "/api/options":
            tf = params.get("tf", ["3m"])[0]
            self._json(_get_option_data(tf))
        elif path == "/api/trades":
            self._json(_get_trades())
        elif path == "/api/state":
            self._json(_read_state())
        elif path == "/api/scans":
            self._json(_get_scan_stats())
        elif path == "/api/summary":
            self._json(_get_daily_summary())
        else:
            self.send_error(404)


def main():
    server = HTTPServer(("0.0.0.0", PORT), Handler)
    print(f"═══════════════════════════════════════════")
    print(f"  VRL Dashboard Server v12.14")
    print(f"  http://0.0.0.0:{PORT}")
    print(f"  Open on any device on same network")
    print(f"═══════════════════════════════════════════")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nDashboard stopped")
        server.server_close()


if __name__ == "__main__":
    main()
