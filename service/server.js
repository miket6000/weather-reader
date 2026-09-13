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

// minimum vertical spread per chart axis (see README "chart_min_spread")
const SPREAD_DEFAULTS = { y: 5, yH: 20, yW: 2, yR: 0 };
const SPREAD_METRIC = { temperature: "y", humidity: "yH", wind: "yW", rain: "yR" };

function resolveMinSpread() {
  const out = { ...SPREAD_DEFAULTS };
  const cfg = CONF.chart_min_spread;
  if (cfg === undefined || cfg === null) return out;
  const num = (v) => {
    const n = Number(v);
    return Number.isFinite(n) && n >= 0 ? n : undefined;
  };
  if (typeof cfg === "number" || typeof cfg === "string") {
    const v = num(cfg);
    if (v !== undefined) out.y = v; // bare value = temperature only
  } else if (typeof cfg === "object") {
    for (const metric of Object.keys(SPREAD_METRIC)) {
      const v = num(cfg[metric]);
      if (v !== undefined) out[SPREAD_METRIC[metric]] = v;
    }
  }
  return out;
}
const MIN_SPREAD = resolveMinSpread();

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

const SECTORS = 16;

function windowStats(windowS) {
  const now = Date.now() / 1000;
  const cur = currentConditions();
  const win = store.filter((r) => r.ts >= now - windowS);

  const hla = (field) => {
    let mn = null, mx = null, sum = 0, n = 0;
    for (const r of win) {
      const v = r[field];
      if (v === null || v === undefined) continue;
      if (mn === null || v < mn) mn = v;
      if (mx === null || v > mx) mx = v;
      sum += v; n += 1;
    }
    return n
      ? { min: mn, avg: Math.round((sum / n) * 100) / 100, max: mx }
      : { min: null, avg: null, max: null };
  };

  let rainRef = null, rainSum = 0;
  for (const r of win) {
    if (r.rain_ok && r.rain_mm !== null && r.rain_mm !== undefined) {
      if (rainRef !== null) {
        const d = r.rain_mm - rainRef;
        if (d >= 0 && d < 1000) rainSum += d;
      }
      rainRef = r.rain_mm;
    }
  }

  const dirs = new Array(SECTORS).fill(0);
  for (const r of win) {
    const d = r.wind_dir_deg;
    if (d !== null && d !== undefined) {
      dirs[Math.round((((d % 360) + 360) % 360) / 22.5) % SECTORS] += 1;
    }
  }
  let mode = null;
  let best = 0;
  for (let i = 1; i < SECTORS; i++) if (dirs[i] > dirs[best]) best = i;
  if (dirs[best]) mode = Math.round(best * 22.5) % 360;

  return {
    window_s: windowS,
    readings: win.length,
    since: now - windowS,
    metrics: {
      temperature_C: { cur: cur.temperature_C, ...hla("temperature_C") },
      humidity: { cur: cur.humidity, ...hla("humidity") },
      wind_avg_m_s: { cur: cur.wind_avg_m_s, ...hla("wind_avg_m_s") },
      wind_gust_m_s: { cur: cur.wind_gust_m_s, ...hla("wind_gust_m_s") },
      uvi: { cur: cur.uvi, ...hla("uvi") },
      rain_mm: { cur: cur.rain_mm, total: Math.round(rainSum * 100) / 100 },
      wind_dir_deg: { cur: cur.wind_dir_deg, mode },
      battery_ok: { cur: cur.battery_ok },
    },
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

app.get("/stats", (req, res) => {
  const w = parseInt(req.query.window || "86400", 10);
  const windowS = Number.isInteger(w) && w > 0 ? Math.min(w, 14 * 86400) : 86400;
  res.json(windowStats(windowS));
});

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

// ---------------------------------------------------------------------------
// /aggregate: long-term roll-ups (hourly/daily blocks written nightly by
// reader/aggregate.py). Rows are JSONL per UTC year; cached by mtime+size.
// ---------------------------------------------------------------------------
const AGG_DIR = process.env.WEATHER_AGG_DIR || path.join(LOG_DIR, "..", "aggregates");
const aggCache = new Map(); // abs path -> {mtimeMs, size, rows}
const AGG_POINT = ["temperature_C", "humidity", "wind_avg_m_s", "wind_gust_m_s",
                   "rain_mm", "uvi", "wind_dir_deg"];

function aggRows(prefix) {
  let names = [];
  try {
    names = fs.readdirSync(AGG_DIR)
      .filter((n) => n.startsWith(prefix) && n.endsWith(".jsonl")).sort();
  } catch {
    return [];
  }
  const out = [];
  for (const name of names) {
    const p = path.join(AGG_DIR, name);
    let st;
    try { st = fs.statSync(p); } catch { continue; }
    let hit = aggCache.get(p);
    if (!hit || hit.mtimeMs !== st.mtimeMs || hit.size !== st.size) {
      const rows = [];
      for (const line of fs.readFileSync(p, "utf8").split("\n")) {
        if (!line.trim()) continue;
        try { rows.push(JSON.parse(line)); } catch { /* skip bad row */ }
      }
      hit = { mtimeMs: st.mtimeMs, size: st.size, rows };
      aggCache.set(p, hit);
    }
    for (const r of hit.rows) out.push(r);
  }
  return out;
}

app.get("/aggregate", (req, res) => {
  const base = ["hour", "day"].includes(req.query.base) ? req.query.base : "day";
  const now = Date.now() / 1000;
  const defaultWin = base === "day" ? 6 * 365 * 86400 : 45 * 86400;
  const from = parseFloat(req.query.from);
  const to = parseFloat(req.query.to);
  const f = Number.isFinite(from) ? Math.min(from, now) : now - defaultWin;
  const t = Number.isFinite(to) ? to : now;
  let rows = aggRows(base === "day" ? "daily-" : "hourly-")
    .filter((r) => r.base === base && r.start >= f && r.start <= t)
    .sort((a, b) => a.start - b.start);
  const limit = Math.min(parseInt(req.query.limit || "4000", 10) || 4000, 40000);
  if (rows.length > limit) rows = rows.slice(-limit);
  const points = rows.map((r) => {
    const p = { t: r.start, count: r.count };
    for (const k of AGG_POINT) if (r[k] !== undefined) p[k] = r[k];
    return p;
  });
  res.json({ base, from: f, to: t, points });
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
.cards{display:grid;grid-template-columns:repeat(4,1fr);
gap:.8em;margin-bottom:1em}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;
padding:.75em .9em;box-shadow:0 1px 2px rgba(15,23,42,.06);
display:flex;flex-direction:column;min-height:122px}
.card .k{color:var(--mut);font-size:.72rem;text-transform:uppercase;letter-spacing:.05em;
white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.card .v{font-size:1.5rem;font-weight:650;margin-top:.18em;line-height:1.15;
white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.card .v.n{color:var(--mut);font-weight:400}
.card .s{display:grid;grid-template-columns:repeat(3,1fr);gap:.15em .4em;
border-top:1px solid var(--line);margin-top:auto;padding-top:.5em}
.card .s .sn{display:flex;flex-direction:column;line-height:1.25;
min-width:0;overflow:hidden;text-overflow:ellipsis}
.card .s .sn i{font-style:normal;font-size:.62rem;text-transform:uppercase;
letter-spacing:.05em;color:var(--mut)}
.card .s .sn b{font-weight:600;font-size:.88rem;white-space:nowrap;
overflow:hidden;text-overflow:ellipsis}
.card .s .wide{grid-column:1/-1;flex-direction:row;align-items:baseline;
justify-content:center;gap:.45em}
.card .s .wide b{font-size:1rem}
.lowb{color:var(--gust)}
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
@media (max-width:900px){.cards{grid-template-columns:repeat(2,1fr)}}
@media (max-width:560px){.cards{grid-template-columns:1fr}}
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
  <section class="cards" id="cards"></section>

  <section class="panel">
    <div class="toolbar">
      <div class="title">History</div>
      <div class="seg" id="seg">
        <button data-b="minute">Minute</button>
        <button data-b="hour" class="on">Hour</button>
        <button data-b="day">Day</button>
        <button data-b="week">Week</button>
        <button data-b="years">Years</button>
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
const BASE = { minute:"Minute", hour:"Hour", day:"Day", week:"Week", years:"Years" };
const MIN_SPREAD = ${JSON.stringify(MIN_SPREAD)};
let base = "hour", chart = null;
const $ = (id) => document.getElementById(id);
const fmt = (t,b) => {
  const d = new Date(t*1000);
  if (b==="years") return d.toLocaleDateString(undefined,{year:"numeric",month:"short"});
  if (b==="week") return d.toLocaleDateString(undefined,{month:"short",day:"numeric"});
  if (b==="day") return d.toLocaleString(undefined,{month:"short",day:"numeric",hour:"2-digit",minute:"2-digit"});
  return d.toLocaleTimeString(undefined,{hour:"2-digit",minute:"2-digit"});
};
const compass = (deg) => {
  if (deg==null) return null;
  const dirs=["N","NNE","NE","ENE","E","ESE","SE","SSE","S","SSW","SW","WSW","W","WNW","NW","NNW"];
  return dirs[Math.round(((deg%360)+360)%360/22.5)%16];
};
const METRIC = {
  temperature_C: { k:"Temperature", cls:"temp",
    v:m=>m&&m.cur!=null?m.cur.toFixed(1)+"\u00b0":null,
    s:m=>m?[["min",m.min!=null?m.min.toFixed(1)+"\u00b0":null],
             ["avg",m.avg!=null?m.avg.toFixed(1)+"\u00b0":null],
             ["max",m.max!=null?m.max.toFixed(1)+"\u00b0":null]]:null },
  humidity: { k:"Humidity", cls:"hum",
    v:m=>m&&m.cur!=null?m.cur+"%":null,
    s:m=>m?[["min",m.min!=null?m.min+"%":null],
             ["avg",m.avg!=null?m.avg+"%":null],
             ["max",m.max!=null?m.max+"%":null]]:null },
  wind_avg_m_s: { k:"Wind", cls:"wind",
    v:m=>m&&m.cur!=null?m.cur.toFixed(1)+" m/s":null,
    s:m=>m?[["min",m.min!=null?m.min.toFixed(1)+" m/s":null],
             ["avg",m.avg!=null?m.avg.toFixed(1)+" m/s":null],
             ["max",m.max!=null?m.max.toFixed(1)+" m/s":null]]:null },
  wind_gust_m_s: { k:"Gust", cls:"gust",
    v:m=>m&&m.cur!=null?m.cur.toFixed(1)+" m/s":null,
    s:m=>m?[["min",m.min!=null?m.min.toFixed(1)+" m/s":null],
             ["avg",m.avg!=null?m.avg.toFixed(1)+" m/s":null],
             ["max",m.max!=null?m.max.toFixed(1)+" m/s":null]]:null },
  uvi: { k:"UV Index",
    v:m=>m&&m.cur!=null?m.cur.toFixed(1):null,
    s:m=>m?[["min",m.min!=null?m.min.toFixed(1):null],
             ["avg",m.avg!=null?m.avg.toFixed(1):null],
             ["max",m.max!=null?m.max.toFixed(1):null]]:null },
  rain_mm: { k:"Rain (acc.)", cls:"rain",
    v:m=>m&&m.cur!=null?m.cur.toFixed(1)+" mm":null,
    s:m=>m&&m.total!=null?[["24h total",m.total.toFixed(1)+" mm"]]:null, wide:true },
  wind_dir_deg: { k:"Direction",
    v:m=>m&&m.cur!=null?(compass(m.cur)!==null?compass(m.cur)+" \u00b7 "+m.cur+"\u00b0":null):null,
    s:m=>m&&m.mode!=null?[["24h prevailing",compass(m.mode)+" \u00b7 "+m.mode+"\u00b0"]]:null,
    wide:true },
  battery_ok: { k:"Battery",
    v:m=>m==null||m.cur==null?null:(m.cur?"OK":"LOW"),
    s:()=>[["state","no 24h range"]], wide:true, bat:true },
};

function renderCurrent(c){
  $("hdrId").textContent = (c && c.ts!=null)
    ? c.model + " \u00b7 id " + (c.id||"?")
    : "no readings yet";
}

function renderCards(st){
  if (!st) return;
  const dash = "\u2013";
  let html = "";
  for (const key in METRIC) {
    const def = METRIC[key], m = (st.metrics||{})[key];
    let v = def.v(m);
    if (v===null||v==="") v = dash;
    let row = "";
    const cells = def.s ? def.s(m) : null;
    if (cells && cells.length) {
      const spans = [];
      for (const c of cells) {
        let b = c[1];
        if (b===null||b==="") b = dash;
        const low = (def.bat && c[1]==="LOW") ? ' class="lowb"' : "";
        spans.push('<span class="sn' + (def.wide?" wide":"") + '"><i>' + c[0]
          + "</i><b" + low + ">" + b + "</b></span>");
      }
      row = '<div class="s">' + spans.join("") + "</div>";
    }
    html += '<div class="card"><div class="k">' + def.k + "</div>"
      + '<div class="v ' + (def.cls||"") + (v===dash?" n":"")
      + (def.bat&&v==="LOW"?" lowb":"") + '">' + v
      + "</div>" + row + "</div>";
  }
  $("cards").innerHTML = html;
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
  const datasets = base==="years"
    ? [
        mk("Temp min", "#93c5fd", "y", s.points.map(p=>p.temperature_C&&p.temperature_C.min)),
        mk("Temp avg", "#ef4444", "y", s.points.map(p=>p.temperature_C&&p.temperature_C.avg)),
        mk("Temp max", "#f59e0b", "y", s.points.map(p=>p.temperature_C&&p.temperature_C.max)),
        mk("Rain / day", "#06b6d4", "yR", s.points.map(p=>p.rain_mm&&p.rain_mm.sum)),
      ]
    : [
        mk("Temperature", "#ef4444", "y", s.points.map(p=>p.temperature_C)),
        mk("Humidity",    "#3b82f6", "yH", s.points.map(p=>p.humidity)),
        mk("Wind avg",    "#10b981", "yW", s.points.map(p=>p.wind_avg_m_s)),
        mk("Wind gust",   "#f59e0b", "yW", s.points.map(p=>p.wind_gust_m_s)),
        mk("Rain",        "#06b6d4", "yR", s.points.map(p=>p.rain_mm)),
      ];
  const niceGran = (t) => {
    for (const g of [0.5, 1, 2, 5, 10, 25, 50, 100, 200, 500]) {
      if (g * 10 >= t) return g;
    }
    return 1000;
  };
  const scaleLimits = {};
  for (const axis of ["y", "yH", "yW", "yR"]) {
    const spread = (MIN_SPREAD||{})[axis];
    if (!spread) continue;
    let mn = null, mx = null;
    for (const d of datasets) {
      if (d.yAxisID !== axis || !d.data) continue;
      for (const v of d.data) {
        if (v === null || v === undefined) continue;
        if (mn === null || v < mn) mn = v;
        if (mx === null || v > mx) mx = v;
      }
    }
    if (mn === null || mx - mn >= spread) continue;
    const center = (mn + mx) / 2;
    const gran = niceGran(spread);
    const lo = Math.floor((center - spread / 2) / gran) * gran;
    const hi = Math.ceil((center + spread / 2) / gran) * gran;
    scaleLimits[axis] = axis === "yR"
      ? { min: 0, max: Math.max(hi, spread) }
      : { min: lo, max: hi };
  }
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
  for (const a in scaleLimits) {
    opts.scales[a].min = scaleLimits[a].min;
    opts.scales[a].max = scaleLimits[a].max;
  }
  if (!chart) {
    chart = new Chart($("chart").getContext("2d"), {type:"line",data:{labels,datasets},options:opts});
  } else {
    chart.data.labels = labels;
    chart.data.datasets = datasets; // keep 5 datasets in order
    chart.options = opts;
    chart.update();
  }
}

async function refresh(){ const url = base==="years"
    ? "/aggregate?base=day&from="+(Math.floor(Date.now()/1000)-6*365*86400)
    : "/series?base="+base;
  const [c,s,st]=await Promise.all([fetch("/current").then(r=>r.json()),
    fetch(url).then(r=>r.json()), fetch("/stats").then(r=>r.json())]);
  renderCurrent(c); renderCards(st); renderChart(s);
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