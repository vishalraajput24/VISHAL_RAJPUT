import { useState, useEffect, useCallback } from "react";
import * as Recharts from "recharts";

const { LineChart, Line, XAxis, YAxis, Tooltip, ResponsiveContainer, AreaChart, Area, BarChart, Bar, ReferenceLine, CartesianGrid } = Recharts;

// ═══════════════════════════════════════════════════
//  VISHAL RAJPUT TRADE — LIVE DASHBOARD v12.14
// ═══════════════════════════════════════════════════

const TIMEFRAMES = ["1m", "3m", "5m", "15m"];
const API_BASE = ""; // Will be set to server IP when deployed

// Mock data generator for demo — replaced by real API in production
function generateMockSpot(tf, count = 80) {
  const now = Date.now();
  const mins = { "1m": 1, "3m": 3, "5m": 5, "15m": 15 }[tf] || 1;
  let price = 23850 + Math.random() * 200;
  const data = [];
  let ema9 = price, ema21 = price;
  for (let i = count; i >= 0; i--) {
    const change = (Math.random() - 0.48) * 15;
    price += change;
    ema9 = ema9 + (2 / 10) * (price - ema9);
    ema21 = ema21 + (2 / 22) * (price - ema21);
    const rsi = 30 + Math.random() * 40;
    data.push({
      time: new Date(now - i * mins * 60000).toLocaleTimeString("en-IN", { hour: "2-digit", minute: "2-digit" }),
      price: Math.round(price * 100) / 100,
      ema9: Math.round(ema9 * 100) / 100,
      ema21: Math.round(ema21 * 100) / 100,
      volume: Math.floor(500 + Math.random() * 2000),
      rsi,
    });
  }
  return data;
}

function generateMockTrades() {
  return [
    { id: 1, time: "10:12", dir: "CE", entry: 142.5, exit: 164.3, pnl: 21.8, peak: 28.4, trough: -3.2, reason: "RSI_EXHAUSTION", score: 6, phase: 3, session: "MORNING" },
    { id: 2, time: "11:45", dir: "PE", entry: 98.0, exit: 85.5, pnl: -12.5, peak: 4.2, trough: -14.8, reason: "PHASE1_SL", score: 5, phase: 1, session: "MORNING" },
    { id: 3, time: "13:22", dir: "CE", entry: 155.0, exit: 171.8, pnl: 16.8, peak: 22.1, trough: -1.5, reason: "GAMMA_RIDER", score: 7, phase: 3, session: "AFTERNOON" },
  ];
}

function generateMockPnlCurve() {
  const data = [];
  let pnl = 0;
  for (let h = 9; h <= 15; h++) {
    for (let m = h === 9 ? 45 : 0; m < 60; m += 15) {
      if (h === 15 && m > 30) break;
      pnl += (Math.random() - 0.4) * 8;
      data.push({
        time: `${h}:${m.toString().padStart(2, "0")}`,
        pnl: Math.round(pnl * 10) / 10,
      });
    }
  }
  return data;
}

// ─── Components ──────────────────────────────────

function StatusBadge({ label, value, color }) {
  const colors = {
    green: "bg-emerald-500/20 text-emerald-400 border-emerald-500/30",
    red: "bg-red-500/20 text-red-400 border-red-500/30",
    yellow: "bg-amber-500/20 text-amber-400 border-amber-500/30",
    blue: "bg-blue-500/20 text-blue-400 border-blue-500/30",
    purple: "bg-purple-500/20 text-purple-400 border-purple-500/30",
    gray: "bg-gray-500/20 text-gray-400 border-gray-500/30",
  };
  return (
    <div className={`px-3 py-1.5 rounded-lg border text-xs font-medium ${colors[color] || colors.gray}`}>
      <span className="opacity-60">{label}</span>
      <span className="ml-1.5 font-bold">{value}</span>
    </div>
  );
}

function MetricCard({ icon, label, value, sub, accent }) {
  const accents = {
    green: "from-emerald-500/10 to-transparent border-emerald-500/20",
    red: "from-red-500/10 to-transparent border-red-500/20",
    blue: "from-blue-500/10 to-transparent border-blue-500/20",
    yellow: "from-amber-500/10 to-transparent border-amber-500/20",
    purple: "from-purple-500/10 to-transparent border-purple-500/20",
  };
  return (
    <div className={`bg-gradient-to-br ${accents[accent] || accents.blue} border rounded-xl p-4`}>
      <div className="flex items-center gap-2 mb-1">
        <span className="text-lg">{icon}</span>
        <span className="text-xs text-gray-400 uppercase tracking-wider">{label}</span>
      </div>
      <div className="text-xl font-bold text-white">{value}</div>
      {sub && <div className="text-xs text-gray-500 mt-0.5">{sub}</div>}
    </div>
  );
}

function TradeRow({ trade }) {
  const win = trade.pnl > 0;
  return (
    <div className={`flex items-center gap-3 px-4 py-3 rounded-lg border ${win ? "bg-emerald-500/5 border-emerald-500/20" : "bg-red-500/5 border-red-500/20"}`}>
      <div className={`text-2xl`}>{win ? "✅" : "❌"}</div>
      <div className="flex-1 min-w-0">
        <div className="flex items-center gap-2">
          <span className={`font-bold ${trade.dir === "CE" ? "text-emerald-400" : "text-red-400"}`}>{trade.dir}</span>
          <span className="text-gray-500 text-xs">{trade.time}</span>
          <span className="text-gray-600 text-xs">Ph{trade.phase}</span>
          <span className="text-gray-600 text-xs">{trade.session}</span>
        </div>
        <div className="text-xs text-gray-400 mt-0.5">
          ₹{trade.entry} → ₹{trade.exit} · {trade.reason.replace(/_/g, " ")}
        </div>
      </div>
      <div className="text-right">
        <div className={`font-bold ${win ? "text-emerald-400" : "text-red-400"}`}>
          {win ? "+" : ""}{trade.pnl}pts
        </div>
        <div className="text-xs text-gray-500">
          ↑{trade.peak} ↓{trade.trough}
        </div>
      </div>
    </div>
  );
}

function TimeframeSelector({ active, onChange }) {
  return (
    <div className="flex bg-gray-800/50 rounded-lg p-1 gap-1">
      {TIMEFRAMES.map((tf) => (
        <button
          key={tf}
          onClick={() => onChange(tf)}
          className={`px-3 py-1.5 rounded-md text-xs font-bold transition-all ${
            active === tf
              ? "bg-blue-500 text-white shadow-lg shadow-blue-500/25"
              : "text-gray-400 hover:text-white hover:bg-gray-700/50"
          }`}
        >
          {tf}
        </button>
      ))}
    </div>
  );
}

function CustomTooltip({ active, payload, label }) {
  if (!active || !payload || !payload.length) return null;
  return (
    <div className="bg-gray-900 border border-gray-700 rounded-lg px-3 py-2 shadow-xl">
      <div className="text-xs text-gray-400 mb-1">{label}</div>
      {payload.map((p, i) => (
        <div key={i} className="text-xs font-medium" style={{ color: p.color }}>
          {p.name}: {typeof p.value === "number" ? p.value.toFixed(1) : p.value}
        </div>
      ))}
    </div>
  );
}

// ─── Main Dashboard ──────────────────────────────

export default function Dashboard() {
  const [tf, setTf] = useState("3m");
  const [spotData, setSpotData] = useState([]);
  const [trades, setTrades] = useState([]);
  const [pnlCurve, setPnlCurve] = useState([]);
  const [showRSI, setShowRSI] = useState(false);
  const [showVol, setShowVol] = useState(false);
  const [tab, setTab] = useState("chart");
  const [lastRefresh, setLastRefresh] = useState(new Date());

  // Simulated state — replaced by API in production
  const [botState] = useState({
    inTrade: true,
    symbol: "NIFTY2640323900CE",
    direction: "CE",
    entry: 148.5,
    ltp: 162.3,
    pnl: 13.8,
    peak: 16.2,
    trough: -2.1,
    phase: 2,
    score: 6,
    sl: 130.5,
    dailyPnl: 26.1,
    dailyTrades: 3,
    wins: 2,
    losses: 1,
    streak: 0,
    bias: "BULL",
    vix: 16.8,
    hourlyRsi: 58.4,
    straddleDecay: -2.3,
    regime: "TRENDING",
    spot: 23962.5,
    dte: 1,
    mode: "PAPER",
  });

  const loadData = useCallback(() => {
    setSpotData(generateMockSpot(tf));
    setTrades(generateMockTrades());
    setPnlCurve(generateMockPnlCurve());
    setLastRefresh(new Date());
  }, [tf]);

  useEffect(() => { loadData(); }, [loadData]);
  useEffect(() => {
    const interval = setInterval(loadData, 15000);
    return () => clearInterval(interval);
  }, [loadData]);

  const totalPnl = trades.reduce((s, t) => s + t.pnl, 0);
  const spotLast = spotData[spotData.length - 1];
  const spotMin = spotData.length ? Math.min(...spotData.map(d => Math.min(d.price, d.ema21))) - 5 : 0;
  const spotMax = spotData.length ? Math.max(...spotData.map(d => Math.max(d.price, d.ema9))) + 5 : 100;

  return (
    <div className="min-h-screen bg-gray-950 text-white">
      {/* ── Header ── */}
      <div className="bg-gray-900/80 border-b border-gray-800 px-4 py-3">
        <div className="flex items-center justify-between">
          <div>
            <h1 className="text-lg font-bold tracking-tight">
              <span className="text-blue-400">VISHAL RAJPUT</span>
              <span className="text-gray-500 ml-2 text-sm font-normal">TRADE v12.14</span>
            </h1>
            <div className="flex items-center gap-2 mt-1 flex-wrap">
              <StatusBadge label="" value={botState.mode} color={botState.mode === "LIVE" ? "green" : "blue"} />
              <StatusBadge label="Bias" value={botState.bias} color={botState.bias === "BULL" ? "green" : botState.bias === "BEAR" ? "red" : "yellow"} />
              <StatusBadge label="VIX" value={botState.vix} color={botState.vix > 22 ? "red" : botState.vix > 18 ? "yellow" : "green"} />
              <StatusBadge label="DTE" value={botState.dte} color={botState.dte === 0 ? "red" : "gray"} />
              <StatusBadge label="" value={botState.regime} color={botState.regime.includes("TREND") ? "green" : "yellow"} />
            </div>
          </div>
          <div className="text-right">
            <div className="text-xs text-gray-500">
              {lastRefresh.toLocaleTimeString("en-IN")}
            </div>
            <div className="w-2 h-2 rounded-full bg-emerald-400 animate-pulse inline-block ml-1"></div>
          </div>
        </div>
      </div>

      {/* ── Metrics Row ── */}
      <div className="grid grid-cols-2 gap-2 p-3">
        <MetricCard
          icon={botState.inTrade ? "🎯" : "⏸"}
          label={botState.inTrade ? "Position" : "Status"}
          value={botState.inTrade ? `${botState.direction} +${botState.pnl}pts` : "FLAT"}
          sub={botState.inTrade ? `₹${botState.entry} → ₹${botState.ltp} · Ph${botState.phase}` : "Scanning..."}
          accent={botState.inTrade ? (botState.pnl > 0 ? "green" : "red") : "blue"}
        />
        <MetricCard
          icon="💰"
          label="Day P&L"
          value={`${totalPnl > 0 ? "+" : ""}${totalPnl.toFixed(1)}pts`}
          sub={`₹${Math.round(totalPnl * 65)} · ${botState.dailyTrades}T W${botState.wins} L${botState.losses}`}
          accent={totalPnl >= 0 ? "green" : "red"}
        />
        <MetricCard
          icon="📊"
          label="Spot"
          value={spotLast ? spotLast.price.toFixed(1) : "—"}
          sub={`EMA9: ${spotLast ? spotLast.ema9.toFixed(1) : "—"}`}
          accent="blue"
        />
        <MetricCard
          icon="📈"
          label="Straddle"
          value={`${botState.straddleDecay}%`}
          sub={`H.RSI: ${botState.hourlyRsi}`}
          accent={botState.straddleDecay < -5 ? "red" : "green"}
        />
      </div>

      {/* ── Live Position Bar ── */}
      {botState.inTrade && (
        <div className="mx-3 mb-2 bg-gradient-to-r from-blue-500/10 via-blue-500/5 to-transparent border border-blue-500/20 rounded-xl p-3">
          <div className="flex items-center justify-between mb-2">
            <span className="text-sm font-bold text-blue-400">{botState.symbol}</span>
            <span className="text-xs text-gray-400">Score {botState.score}/7 · Phase {botState.phase}</span>
          </div>
          {/* SL → Entry → LTP → Peak progress bar */}
          <div className="relative h-3 bg-gray-800 rounded-full overflow-hidden">
            {/* SL zone */}
            <div className="absolute h-full bg-red-500/30 rounded-l-full" style={{ left: 0, width: "25%" }}></div>
            {/* Profit zone */}
            <div className="absolute h-full bg-emerald-500/30" style={{ left: "25%", width: `${Math.min(75, botState.pnl / botState.peak * 55 + 20)}%` }}></div>
            {/* Current position dot */}
            <div className="absolute top-1/2 -translate-y-1/2 w-3 h-3 bg-white rounded-full shadow-lg shadow-white/30 transition-all" style={{ left: `${Math.min(90, 25 + botState.pnl / Math.max(botState.peak, 1) * 55)}%` }}></div>
          </div>
          <div className="flex justify-between text-xs mt-1.5 text-gray-500">
            <span className="text-red-400">SL ₹{botState.sl}</span>
            <span>Entry ₹{botState.entry}</span>
            <span className="text-emerald-400">Peak +{botState.peak}pts</span>
          </div>
          <div className="flex justify-between text-xs mt-1 text-gray-600">
            <span>↓ Trough: {botState.trough}pts</span>
            <span>SL dist: {(botState.ltp - botState.sl).toFixed(1)}pts</span>
          </div>
        </div>
      )}

      {/* ── Tab Bar ── */}
      <div className="flex border-b border-gray-800 px-3">
        {[
          { key: "chart", icon: "📈", label: "Chart" },
          { key: "trades", icon: "📒", label: "Trades" },
          { key: "pnl", icon: "💹", label: "P&L" },
        ].map(({ key, icon, label }) => (
          <button
            key={key}
            onClick={() => setTab(key)}
            className={`flex items-center gap-1.5 px-4 py-2.5 text-sm font-medium border-b-2 transition-all ${
              tab === key
                ? "border-blue-500 text-blue-400"
                : "border-transparent text-gray-500 hover:text-gray-300"
            }`}
          >
            <span>{icon}</span>{label}
          </button>
        ))}
      </div>

      {/* ── Chart Tab ── */}
      {tab === "chart" && (
        <div className="p-3">
          <div className="flex items-center justify-between mb-3">
            <TimeframeSelector active={tf} onChange={setTf} />
            <div className="flex gap-2">
              <button
                onClick={() => setShowRSI(!showRSI)}
                className={`text-xs px-2.5 py-1 rounded-md border transition-all ${
                  showRSI ? "bg-purple-500/20 border-purple-500/30 text-purple-400" : "border-gray-700 text-gray-500"
                }`}
              >RSI</button>
              <button
                onClick={() => setShowVol(!showVol)}
                className={`text-xs px-2.5 py-1 rounded-md border transition-all ${
                  showVol ? "bg-cyan-500/20 border-cyan-500/30 text-cyan-400" : "border-gray-700 text-gray-500"
                }`}
              >Vol</button>
            </div>
          </div>

          {/* Price + EMA Chart */}
          <div className="bg-gray-900/50 rounded-xl border border-gray-800 p-2">
            <ResponsiveContainer width="100%" height={280}>
              <AreaChart data={spotData} margin={{ top: 5, right: 5, bottom: 5, left: 0 }}>
                <defs>
                  <linearGradient id="priceGrad" x1="0" y1="0" x2="0" y2="1">
                    <stop offset="0%" stopColor="#3b82f6" stopOpacity={0.15} />
                    <stop offset="100%" stopColor="#3b82f6" stopOpacity={0} />
                  </linearGradient>
                </defs>
                <CartesianGrid strokeDasharray="3 3" stroke="#1f2937" />
                <XAxis dataKey="time" tick={{ fontSize: 10, fill: "#6b7280" }} interval="preserveStartEnd" />
                <YAxis domain={[spotMin, spotMax]} tick={{ fontSize: 10, fill: "#6b7280" }} width={55} tickFormatter={v => v.toFixed(0)} />
                <Tooltip content={<CustomTooltip />} />
                <Area type="monotone" dataKey="price" stroke="#3b82f6" strokeWidth={2} fill="url(#priceGrad)" name="Spot" dot={false} />
                <Line type="monotone" dataKey="ema9" stroke="#10b981" strokeWidth={1.5} dot={false} name="EMA9" strokeDasharray="" />
                <Line type="monotone" dataKey="ema21" stroke="#f59e0b" strokeWidth={1.5} dot={false} name="EMA21" strokeDasharray="4 2" />
              </AreaChart>
            </ResponsiveContainer>
          </div>

          {/* RSI Panel */}
          {showRSI && (
            <div className="bg-gray-900/50 rounded-xl border border-gray-800 p-2 mt-2">
              <div className="text-xs text-gray-500 px-2 mb-1">RSI (14)</div>
              <ResponsiveContainer width="100%" height={100}>
                <LineChart data={spotData} margin={{ top: 5, right: 5, bottom: 5, left: 0 }}>
                  <CartesianGrid strokeDasharray="3 3" stroke="#1f2937" />
                  <XAxis dataKey="time" tick={false} />
                  <YAxis domain={[20, 80]} tick={{ fontSize: 9, fill: "#6b7280" }} width={30} />
                  <ReferenceLine y={70} stroke="#ef4444" strokeDasharray="3 3" strokeOpacity={0.5} />
                  <ReferenceLine y={30} stroke="#10b981" strokeDasharray="3 3" strokeOpacity={0.5} />
                  <ReferenceLine y={45} stroke="#6b7280" strokeDasharray="2 4" strokeOpacity={0.3} />
                  <ReferenceLine y={65} stroke="#6b7280" strokeDasharray="2 4" strokeOpacity={0.3} />
                  <Line type="monotone" dataKey="rsi" stroke="#a855f7" strokeWidth={1.5} dot={false} name="RSI" />
                  <Tooltip content={<CustomTooltip />} />
                </LineChart>
              </ResponsiveContainer>
            </div>
          )}

          {/* Volume Panel */}
          {showVol && (
            <div className="bg-gray-900/50 rounded-xl border border-gray-800 p-2 mt-2">
              <div className="text-xs text-gray-500 px-2 mb-1">Volume</div>
              <ResponsiveContainer width="100%" height={80}>
                <BarChart data={spotData} margin={{ top: 5, right: 5, bottom: 5, left: 0 }}>
                  <XAxis dataKey="time" tick={false} />
                  <YAxis tick={false} width={10} />
                  <Bar dataKey="volume" fill="#06b6d4" fillOpacity={0.4} radius={[2, 2, 0, 0]} />
                </BarChart>
              </ResponsiveContainer>
            </div>
          )}
        </div>
      )}

      {/* ── Trades Tab ── */}
      {tab === "trades" && (
        <div className="p-3 space-y-2">
          <div className="text-sm font-bold text-gray-400 mb-2">
            Today's Trades ({trades.length})
          </div>
          {trades.map((t) => (
            <TradeRow key={t.id} trade={t} />
          ))}
          {trades.length === 0 && (
            <div className="text-center text-gray-600 py-8">No trades yet today</div>
          )}
        </div>
      )}

      {/* ── P&L Tab ── */}
      {tab === "pnl" && (
        <div className="p-3">
          <div className="bg-gray-900/50 rounded-xl border border-gray-800 p-3">
            <div className="text-sm font-bold text-gray-400 mb-3">Intraday P&L Curve</div>
            <ResponsiveContainer width="100%" height={250}>
              <AreaChart data={pnlCurve} margin={{ top: 5, right: 5, bottom: 5, left: 0 }}>
                <defs>
                  <linearGradient id="pnlUp" x1="0" y1="0" x2="0" y2="1">
                    <stop offset="0%" stopColor="#10b981" stopOpacity={0.2} />
                    <stop offset="100%" stopColor="#10b981" stopOpacity={0} />
                  </linearGradient>
                </defs>
                <CartesianGrid strokeDasharray="3 3" stroke="#1f2937" />
                <XAxis dataKey="time" tick={{ fontSize: 10, fill: "#6b7280" }} />
                <YAxis tick={{ fontSize: 10, fill: "#6b7280" }} width={40} />
                <ReferenceLine y={0} stroke="#374151" strokeWidth={2} />
                <Tooltip content={<CustomTooltip />} />
                <Area type="monotone" dataKey="pnl" stroke="#10b981" strokeWidth={2} fill="url(#pnlUp)" name="P&L (pts)" dot={false} />
              </AreaChart>
            </ResponsiveContainer>
          </div>

          {/* Trade Stats Grid */}
          <div className="grid grid-cols-3 gap-2 mt-3">
            {[
              { label: "Win Rate", value: `${Math.round(botState.wins / Math.max(botState.dailyTrades, 1) * 100)}%`, icon: "🎯" },
              { label: "Avg Peak", value: `+${(trades.reduce((s, t) => s + t.peak, 0) / Math.max(trades.length, 1)).toFixed(1)}`, icon: "📊" },
              { label: "Avg Trough", value: `${(trades.reduce((s, t) => s + t.trough, 0) / Math.max(trades.length, 1)).toFixed(1)}`, icon: "📉" },
            ].map(({ label, value, icon }) => (
              <div key={label} className="bg-gray-900/50 border border-gray-800 rounded-lg p-3 text-center">
                <div className="text-lg mb-1">{icon}</div>
                <div className="text-xs text-gray-500">{label}</div>
                <div className="text-sm font-bold text-white">{value}</div>
              </div>
            ))}
          </div>
        </div>
      )}

      {/* ── Footer ── */}
      <div className="border-t border-gray-800 px-4 py-2 text-center">
        <span className="text-xs text-gray-600">Auto-refresh 15s · Spot + EMA + RSI + Volume</span>
      </div>
    </div>
  );
}
