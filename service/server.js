"use strict";

const fs = require("fs");
const path = require("path");
const http = require("http");
const express = require("express");
const { WebSocketServer } = require("ws");

const LOG_DIR = process.env.WEATHER_LOG_DIR || path.join(__dirname, "..", "log");
const POLL_MS = parseInt(process.env.POLL_MS || "1000", 10);
const STORE_MAX_AGE_S = 14 * 86400; // keep 14 days of history in memory

// ---- config (shared /etc/weather-reader/config.json, else project one) -----
const CONF_CANDIDATES = [
  process.env.WEATHER_CONFIG,
  "/etc/weather-reader/config.json",
  path.join(__dirname, "..", "config.json"),
].filter(Boolean);

function loadConf() {
  for (const p of CONF_CANDIDATES) {
    try {
      return JSON.parse(fs.readFileSync(p, "utf8"));
    } catch { /* try next */ }
  }
  return {};
}

const CONF = loadConf();

function resolvePort() {
  const fromConfig = Number(CONF.http_port);
  const fromEnv = parseInt(process.env.PORT || "", 10);
  const port = Number.isInteger(fromConfig) ? fromConfig
               : Number.isInteger(fromEnv) ? fromEnv
               : 8080;
  if (port < 1 || port > 65535) {
    console.error(`weather-http: invalid port ${port}; using 8080`);
    return 8080;
  }
  return port;
}
const PORT = resolvePort();

// ---- history store -----------------------------------------------------------
let store = [];            // readings flat, newest at the end
let lastReading = null;
const fileMeta = new Map(); // filename -> {size}
const wssClients = [];

function pushReading(rec, broadcastNow) {
  if (!rec || !rec.mic_ok) return;
  const dup = store.length && store[store.length - 1].ts === rec.ts
    && store[store.length - 1].msg_hex === rec.msg_hex;
  if (dup) return;
  store.push(rec);
  lastReading = rec;
  // keep memory bounded, drop older-than-14d readings occasionally
  if (store.length % 50 === 0) {
    const cutoff = Date.now() / 1000 - STORE_MAX_AGE_S;
    store = store.filter((r) => r.ts >= cutoff);
  }
  if (broadcastNow) broadcast(rec);
}

function broadcast(rec) {
  const msg = JSON.stringify({ type: "reading", data: rec });
  for (const ws of wssClients) {
    if (ws.readyState === ws.OPEN) ws.send(msg);
  }
}

function dayPath(date) {
  const ymd =
    date.getFullYear() +
    String(date.getMonth() + 1).padStart(2, "0") +
    String(date.getDate()).padStart(2, "0");
  return path.join(LOG_DIR, `readings-${ymd}.jsonl`);
}

function parseLines(text) {
  const out = [];
  for (const line of text.split("\n")) {
    if (!line.trim()) continue;
    try {
      const rec = JSON.parse(line);
      if (rec.mic_ok) out.push(rec);
    } catch {
      /* partial/truncated line (tail of a pending write) */
    }
  }
  return out;
}

function fileNames() {
  try {
    return fs.readdirSync(LOG_DIR)
      .filter((n) => /^readings-\d{8}\.jsonl$/.test(n))
      .sort();
  } catch {
    return [];
  }
}

function syncFiles() {
  fs.mkdirSync(LOG_DIR, { recursive: true });
  const names = fileNames();
  for (const name of names) {
    const p = path.join(LOG_DIR, name);
    let size = 0;
    try {
      size = fs.statSync(p).size;
    } catch {
      continue;
    }
    const meta = fileMeta.get(name);
    if (meta && meta.size <= size) {
      if (size === meta.size) continue;
      // append-only growth: read just the new bytes
      const fd = fs.openSync(p, "r");
      const len = size - meta.size;
      const buf = Buffer.alloc(len);
      fs.readSync(fd, buf, 0, len, meta.size);
      fs.closeSync(fd);
      for (const rec of parseLines(buf.toString("utf8"))) pushReading(rec, true);
      meta.size = size;
    } else {
      // first time seeing this file (or it shrank/rotated): full load
      const all = parseLines(fs.readFileSync(p, "utf8"));
      for (const rec of all) pushReading(rec, false);
      fileMeta.set(name, { size });
    }
  }
}

setInterval(syncFiles, POLL_MS);

// ---- aggregation -------------------------------------------------------------
const WINDOWS = { minute: 60, hour: 3600, day: 86400, week: 604800 };
const BUCKETS = { minute: 0, hour: 120, day: 1800, week: 21600 };

function meanOf(values) {
  if (!values.length) return null;
  return values.reduce((a, b) => a + b, 0) / values.length;
}

function latestNonNull(f) {
  for (let i = store.length - 1; i >= 0; i--) {
    const v = f(store[i]);
    if (v !== null && v !== undefined) return v;
  }
  return null;
}

function currentConditions() {
  const last = store[store.length - 1];
  return {
    ts: last ? last.ts : null,
    id: latestNonNull((r) => r.id) || null,
    model: latestNonNull((r) => r.model) || "Bresser-6in1",
    temperature_C: latestNonNull((r) => r.temperature_C),
    humidity: latestNonNull((r) => r.humidity),
    wind_avg_m_s: latestNonNull((r) => r.wind_avg_m_s),
    wind_gust_m_s: latestNonNull((r) => r.wind_gust_m_s),
    wind_dir_deg: latestNonNull((r) => r.wind_dir_deg),
    rain_mm: latestNonNull((r) => r.rain_mm),
    uvi: latestNonNull((r) => r.uvi),
    battery_ok: latestNonNull((r) => r.battery_ok),
  };
}

function seriesFor(base) {
  const win = WINDOWS[base] || WINDOWS.hour;
  const bucket = base in BUCKETS ? BUCKETS[base] : BUCKETS.hour;
  const now = Date.now() / 1000;
  const from = now - win;
  const recent = store.filter((r) => r.ts >= from - 1 && r.ts <= now + 1)
    .sort((a, b) => a.ts - b.ts);

  if (bucket < 1) {
    // minute: raw per-reading values (with rainfall per-reading delta)
    let rainRef = null;
    const points = recent.map((r) => {
      let rain = null;
      if (r.rain_ok && r.rain_mm !== null) {
        if (rainRef !== null) {
          const d = r.rain_mm - rainRef;
          if (d >= 0 && d < 1000) rain = d;
        }
        rainRef = r.rain_mm;
      }
      return {
        t: r.ts,
        temperature_C: r.temperature_C,
        humidity: r.humidity,
        wind_avg_m_s: r.wind_avg_m_s,
        wind_gust_m_s: r.wind_gust_m_s,
        wind_dir_deg: r.wind_dir_deg,
        rain_mm: rain,
      };
    });
    return {
      base, bucket_s: 0, from, now,
      points: points.filter((p) =>
        p.temperature_C != null || p.humidity != null || p.wind_avg_m_s != null ||
        p.wind_gust_m_s != null || p.rain_mm != null),
    };
  }

  const start = Math.floor(from / bucket) * bucket;
  const points = [];
  let rainRef = null;
  for (let b = start; b < now; b += bucket) {
    const seg = recent.filter((r) => r.ts >= b && r.ts < b + bucket);
    const temps = [], hums = [], winds = [], gusts = [];
    let rainSum = 0, hasRain = false;
    for (const r of seg) {
      if (r.temperature_C !== null && r.temperature_C !== undefined) temps.push(r.temperature_C);
      if (r.humidity !== null && r.humidity !== undefined) hums.push(r.humidity);
      if (r.wind_avg_m_s !== null && r.wind_avg_m_s !== undefined) winds.push(r.wind_avg_m_s);
      if (r.wind_gust_m_s !== null && r.wind_gust_m_s !== undefined) gusts.push(r.wind_gust_m_s);
      if (r.rain_ok && r.rain_mm !== null) {
        if (rainRef !== null) {
          const d = r.rain_mm - rainRef;
          if (d >= 0 && d < 1000) rainSum += d;
        }
        rainRef = r.rain_mm;
        hasRain = true;
      }
    }
    const n = temps.length + hums.length + winds.length + gusts.length + (hasRain ? 1 : 0);
    points.push({
      t: b,
      temperature_C: meanOf(temps),
      humidity: meanOf(hums),
      wind_avg_m_s: meanOf(winds),
      wind_gust_m_s: gusts.length ? Math.max(...gusts) : null,
      rain_mm: hasRain ? Math.round(rainSum * 100) / 100 : null,
    });
  }
  return { base, bucket_s: bucket, from, now,
    points: points.filter((p) =>
      p.temperature_C != null || p.humidity != null || p.wind_avg_m_s != null ||
      p.wind_gust_m_s != null || p.rain_mm != null) };
}

// ---- HTTP + WebSocket ---------------------------------------------------------
const app = express();

app.get("/chart.js", (_req, res) => {
  res.sendFile(path.join(__dirname, "node_modules", "chart.js", "dist", "chart.umd.js"));
});

app.get("/current", (_req, res) => res.json(currentConditions()));

app.get("/status", (_req, res) => res.json(lastReading || null));

app.get("/history", (req, res) => {
  const n = Math.min(parseInt(req.query.n || "20", 10) || 20, 500);
  res.json(store.slice(-n));
});

app.get("/series", (req, res) => {
  const base = ["minute", "hour", "day", "week"].includes(req.query.base)
    ? req.query.base : "hour";
  res.json(seriesFor(base));
});

app.get("/", (_req, res) => {
  res.type("html").send(indexHtml());
});

const server = http.createServer(app);
const wss = new WebSocketServer({ server, path: "/stream" });

wss.on("connection", (ws) => {
  wssClients.push(ws);
  if (lastReading) ws.send(JSON.stringify({ type: "reading", data: lastReading }));
  ws.on("close", () => {
    const i = wssClients.indexOf(ws);
    if (i >= 0) wssClients.splice(i, 1);
  });
});

server.listen(PORT, () => {
  console.log(`[service] listening on :${PORT}, log dir ${LOG_DIR}`);
  syncFiles();
});

// ---- page ---------------------------------------------------------------------
function indexHtml() {
  return `<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>XC0432 Weather Station</title>
<style>
:root{--bg:#f1f5f9;--card:#fff;--ink:#0f172a;--mut:#64748b;--line:#e2e8f0;
--temp:#ef4444;--hum:#3b82f6;--wind:#10b981;--gust:#f59e0b;--rain:#06b6d4;}
*{box-sizing:border-box}
body{margin:0;font:14px/1.45 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;
background:var(--bg);color:var(--ink)}
header{background:#0f172a;color:#fff;padding:.9em 1.2em;display:flex;
align-items:baseline;gap:.8em;flex-wrap:wrap}
header h1{font-size:1.05rem;margin:0;font-weight:600}
header .sub{color:#94a3b8;font-size:.8rem}
main{max-width:1100px;margin:0 auto;padding:1.2em}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));
gap:.8em;margin-bottom:1em}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;
padding:.8em .9em;box-shadow:0 1px 2px rgba(15,23,42,.06)}
.card .k{color:var(--mut);font-size:.72rem;text-transform:uppercase;letter-spacing:.05em}
.card .v{font-size:1.55rem;font-weight:650;margin-top:.15em;line-height:1.1;
white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.card .u{font-size:.8rem;color:var(--mut);font-weight:500}
.card .n{padding:.9em;color:var(--mut);font-weight:400}
.temp{color:var(--temp)} .hum{color:var(--hum)} .wind{color:var(--wind)}
.gust{color:var(--gust)} .rain{color:var(--rain)}
.panel{background:var(--card);border:1px solid var(--line);border-radius:10px;
padding:1em;box-shadow:0 1px 2px rgba(15,23,42,.06)}
.toolbar{display:flex;align-items:center;justify-content:space-between;
gap:1em;margin-bottom:.8em;flex-wrap:wrap}
.toolbar .title{font-weight:600;font-size:.95rem}
.seg{display:inline-flex;border:1px solid var(--line);border-radius:8px;overflow:hidden}
.seg button{border:0;background:transparent;padding:.4em .95em;cursor:pointer;
font:inherit;color:var(--ink)}
.seg button.on{background:#0f172a;color:#fff}
.chartbox{position:relative;height:360px}
#chartjs-note{position:absolute;inset:0;display:none;align-items:center;
justify-content:center;color:var(--mut);text-align:center;padding:1em}
#lastupd{color:var(--mut);font-size:.78rem;margin-top:.5em}
footer{color:var(--mut);font-size:.75rem;max-width:1100px;margin:1em auto 2em;
padding:0 1.2em}
@media (max-width:560px){.chartbox{height:300px}}
</style>
</head><body>
<header><h1>Digitech XC0432 / Bresser 6-in-1</h1>
<span class="sub" id="hdrId"></span></header>
<main>
  <section class="cards" id="cards">
    <div class="card"><div class="k">Temperature</div><div class="v temp" id="cTemp">&ndash;</div></div>
    <div class="card"><div class="k">Humidity</div><div class="v hum" id="cHum">&ndash;</div></div>
    <div class="card"><div class="k">Wind</div><div class="v wind" id="cWind">&ndash;</div></div>
    <div class="card"><div class="k">Gust</div><div class="v gust" id="cGust">&ndash;</div></div>
    <div class="card"><div class="k">Direction</div><div class="v" id="cDir">&ndash;</div><div class="u" id="cDirU"></div></div>
    <div class="card"><div class="k">Rain (acc.)</div><div class="v rain" id="cRain">&ndash;</div></div>
    <div class="card"><div class="k">UV Index</div><div class="v" id="cUV">&ndash;</div></div>
    <div class="card"><div class="k">Battery</div><div class="v" id="cBat">&ndash;</div></div>
  </section>

  <section class="panel">
    <div class="toolbar">
      <div class="title">History</div>
      <div class="seg" id="seg">
        <button data-b="minute">Minute</button>
        <button data-b="hour" class="on">Hour</button>
        <button data-b="day">Day</button>
        <button data-b="week">Week</button>
      </div>
    </div>
    <div class="chartbox">
      <canvas id="chart"></canvas>
      <div id="chartjs-note">No data yet &mdash; waiting for the sensor&hellip;</div>
    </div>
    <div id="lastupd"></div>
  </section>
</main>
<footer>Wind speed/peak gust (m/s) &middot; rainfall per bucket (mm) &middot; temperature (&deg;C) &middot; humidity (%).<br>
MIC-validated frames decoded live on the RTL-SDR. Updates as the station transmits (~every 12 s).</footer>
<script src="/chart.js"></script>
<script>
(function(){
const BASE = { minute:"Minute", hour:"Hour", day:"Day", week:"Week" };
let base = "hour", chart = null;
const $ = (id) => document.getElementById(id);
const fmt = (t,b) => {
  const d = new Date(t*1000);
  if (b==="week") return d.toLocaleDateString(undefined,{month:"short",day:"numeric"});
  if (b==="day") return d.toLocaleString(undefined,{month:"short",day:"numeric",hour:"2-digit",minute:"2-digit"});
  return d.toLocaleTimeString(undefined,{hour:"2-digit",minute:"2-digit"});
};
const compass = (deg) => {
  if (deg==null) return null;
  const dirs=["N","NNE","NE","ENE","E","ESE","SE","SSE","S","SSW","SW","WSW","W","WNW","NW","NNW"];
  return dirs[Math.round(((deg%360)+360)%360/22.5)%16];
};
function card(id,val){ const el=$(id); if(val==null||val===""){el.textContent="\u2013";el.classList.add("n");}else{el.textContent=val;el.classList.remove("n");} }

function renderCurrent(c){
  if (!c || c.ts==null) { $("hdrId").textContent="no readings yet"; return; }
  $("hdrId").textContent = c.model + " \u00b7 id " + (c.id||"?");
  card("cTemp", c.temperature_C!=null ? c.temperature_C.toFixed(1)+"\u00b0" : null);
  card("cHum",  c.humidity!=null ? c.humidity+"%" : null);
  card("cWind", c.wind_avg_m_s!=null ? c.wind_avg_m_s.toFixed(1)+" m/s" : null);
  card("cGust", c.wind_gust_m_s!=null ? c.wind_gust_m_s.toFixed(1)+" m/s" : null);
  const dir = compass(c.wind_dir_deg);
  card("cDir", dir);
  const du = $("cDirU");
  if (du) du.textContent = (dir && c.wind_dir_deg != null) ? c.wind_dir_deg + "\u00b0" : "";
  card("cRain", c.rain_mm!=null ? c.rain_mm.toFixed(1)+" mm" : null);
  card("cUV",   c.uvi!=null ? c.uvi.toFixed(1) : null);
  const bat = c.battery_ok;
  card("cBat",  bat==null ? null : (bat ? "OK" : "LOW"));
  $("cBat").style.color = bat === false ? "var(--gust)" : "var(--wind)";
}

function renderChart(s){
  const note = $("chartjs-note");
  if (!s || !s.points.length) { note.style.display="flex"; return; }
  note.style.display="none";
  const labels = s.points.map(p=>fmt(p.t,base));
  const mk = (name,color,yAxisID,vals) => ({
    label:name,borderColor:color,backgroundColor:color,data:vals,
    yAxisID:yAxisID,
    borderWidth:2,pointRadius:1,pointHitRadius:6,tension:.25,
  });
  const datasets = [
    mk("Temperature", "#ef4444", "y", s.points.map(p=>p.temperature_C)),
    mk("Humidity",    "#3b82f6", "yH", s.points.map(p=>p.humidity)),
    mk("Wind avg",    "#10b981", "yW", s.points.map(p=>p.wind_avg_m_s)),
    mk("Wind gust",   "#f59e0b", "yW", s.points.map(p=>p.wind_gust_m_s)),
    mk("Rain",        "#06b6d4", "yR", s.points.map(p=>p.rain_mm)),
  ];
  const opts = {
    responsive:true, maintainAspectRatio:false,
    animation:false,
    interaction:{mode:"index",intersect:false},
    plugins:{legend:{labels:{boxWidth:14,boxHeight:8,usePointStyle:true,padding:14}},
             tooltip:{callbacks:{label:(ctx)=>{
               const v = ctx.parsed.y;
               if (v==null) return null;
               let u="";
               if(ctx.dataset.yAxisID==="y") u="\u00b0C";
               else if(ctx.dataset.yAxisID==="yH") u=" %";
               else if(ctx.dataset.yAxisID==="yW") u=" m/s";
               else u=" mm";
               return ctx.dataset.label+": "+(Number.isInteger(v)?v.toFixed(0):v.toFixed(1))+u;
             }}}},
    scales:{
      x:{ticks:{maxTicksLimit:12},grid:{display:false}},
      y:{type:"linear",position:"left",title:{display:true,text:"temp \u00b0C"},
         grid:{color:"rgba(15,23,42,.06)"}},
      yH:{type:"linear",position:"left",title:{display:true,text:"humidity %"},
          grid:{drawOnChartArea:false},offset:true},
      yW:{type:"linear",position:"right",title:{display:true,text:"wind m/s"},
          grid:{drawOnChartArea:false}},
      yR:{type:"linear",position:"right",title:{display:true,text:"rain mm"},
          grid:{drawOnChartArea:false},beginAtZero:true},
    },
  };
  if (!chart) {
    chart = new Chart($("chart").getContext("2d"), {type:"line",data:{labels,datasets},options:opts});
  } else {
    chart.data.labels = labels;
    chart.data.datasets = datasets; // keep 5 datasets in order
    chart.options = opts;
    chart.update();
  }
}

async function refresh(){ const [c,s]=await Promise.all([fetch("/current").then(r=>r.json()), fetch("/series?base="+base).then(r=>r.json())]);
  renderCurrent(c); renderChart(s);
  $("lastupd").textContent = c.ts ? "last reading " + new Date(c.ts*1000).toLocaleTimeString() : "";
}
let timer=null;
function refreshSoon(){ if(timer) clearTimeout(timer); timer=setTimeout(refresh,800); }

document.getElementById("seg").addEventListener("click",(e)=>{
  const btn=e.target.closest("button"); if(!btn) return;
  base=btn.dataset.b;
  document.querySelectorAll("#seg button").forEach(b=>b.classList.toggle("on",b===btn));
  $("chartjs-note").style.display="none";
  refresh();
});

let ws;
function connect(){
  ws=new WebSocket((location.protocol==="https:"?"wss://":"ws://")+location.host+"/stream");
  ws.onopen=refresh;
  ws.onmessage=(e)=>{ const m=JSON.parse(e.data); if(m.type==="reading"){ refreshSoon(); } };
  ws.onclose=()=>setTimeout(connect,3000);
}
refresh(); connect();
})();
</script>
</body></html>`;
}