"use strict";
// Watches /stream for a few seconds and prints each live reading.
const WebSocket = require("ws");
const url = process.argv[2] || "ws://localhost:8080/stream";
const ws = new WebSocket(url);
const seen = [];
ws.on("message", (m) => {
  const j = JSON.parse(m.toString());
  if (j.type !== "reading") return;
  const r = j.data;
  seen.push(r);
  console.log("live: temp=%s hum=%s gust=%s/%s dir=%s rain=%s mic=%s",
    r.temperature_C, r.humidity, r.wind_gust_m_s, r.wind_avg_m_s,
    r.wind_dir_deg, r.rain_mm, r.mic);
});
ws.on("open", () => setTimeout(() => { ws.close(); process.exit(0); }, 40000));
setTimeout(() => { console.error("timeout"); process.exit(1); }, 50000).unref();