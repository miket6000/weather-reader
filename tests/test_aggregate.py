import json
import os
import tempfile
import unittest

from reader.aggregate import (COMPASS, HOUR, day_start, main,
                              _combine_existing)

NOW = 1767273600.0          # fixed 'now' for deterministic tests
H0 = int(NOW // 3600) * 3600 - 24 * 3600   # a full day before now


def frame(ts, temp=None, hum=None, wind=None, gust=None, direction=None,
          uvi=None, rain_ok=False, rain=None):
    r = {"ts": ts, "mic_ok": True, "startup": False}
    if temp is not None:
        r["temperature_C"] = temp
    if hum is not None:
        r["humidity"] = hum
    if wind is not None:
        r["wind_avg_m_s"] = wind
    if gust is not None:
        r["wind_gust_m_s"] = gust
    if direction is not None:
        r["wind_dir_deg"] = direction
    if uvi is not None:
        r["uvi"] = uvi
    if rain_ok:
        r["rain_ok"] = True
        r["rain_mm"] = rain
    return r


def write_raw(data_dir, rows_iter):
    """Writes rows to readings-YYYYMMDD.jsonl (per-UTC-day)."""
    from datetime import datetime, timezone
    by_day = {}
    for r in rows_iter:
        d = datetime.fromtimestamp(r["ts"], tz=timezone.utc).strftime("%Y%m%d")
        by_day.setdefault(d, []).append(r)
    for d, rs in by_day.items():
        with open(os.path.join(data_dir, f"readings-{d}.jsonl"), "a") as f:
            for r in rs:
                f.write(json.dumps(r) + "\n")


class AggregateTest(unittest.TestCase):

    def setUp(self):
        tmp = tempfile.mkdtemp(prefix="aggtest-")
        self.addCleanup(__import__("shutil").rmtree, tmp)
        self.data = os.path.join(tmp, "log")
        self.agg = os.path.join(tmp, "aggregates")
        os.makedirs(self.data)

    def run_agg(self, **kw):
        args = ["--data-dir", self.data, "--agg-dir", self.agg,
                "--now", str(NOW)]
        for k, v in kw.items():
            args.append("--" + k.replace("_", "-"))
            if isinstance(v, bool):
                if not v:
                    continue
            else:
                args.append(str(v))
        main(args)

    def hourly(self):
        rows = _combine_existing(self.agg, "hourly")
        return {k: rows[k] for k in sorted(rows)}

    def daily(self):
        return _combine_existing(self.agg, "daily")

    def test_hour_stats(self):
        h = H0
        write_raw(self.data, [
            frame(h + 600, 12.0, 60, 5.0, 6.0),           # temp 12.0
            frame(h + 1200, 13.2, 64, 6.1, 6.1, 200),     # temp 13.2
            frame(h + 1800, 11.6, 58, 4.9, 5.2, 220, 1.2),
            frame(h + 2400, 12.4, 61, 5.5, 5.5, 210, 3.4),
        ])
        self.run_agg()
        rows = self.hourly()
        self.assertEqual(len(rows), 1)
        r = rows[h]
        self.assertEqual(r["count"], 4)
        self.assertEqual(r["temperature_C"],
                         {"min": 11.6, "avg": 12.3, "max": 13.2})
        self.assertEqual(r["wind_avg_m_s"],
                         {"min": 4.9, "avg": 5.38, "max": 6.1})
        self.assertEqual(r["wind_gust_m_s"], {"max": 6.1})
        self.assertEqual(r["uvi"], {"max": 3.4})
        self.assertEqual(r["wind_dir_deg"],
                         {"mode": COMPASS[9]})  # sector of 200/210/220

    def test_rain_delta_sum(self):
        h = H0
        write_raw(self.data, [
            frame(h + 600, rain_ok=True, rain=100.0),
            frame(h + 1200, rain_ok=True, rain=100.4),
            frame(h + 1800, rain_ok=True, rain=103.5),
            frame(h + 2400, rain_ok=True, rain=5.0),   # reset: delta < 0
            frame(h + 3000, 12.0),                      # non-rain
        ])
        self.run_agg()
        r = self.hourly()[h]
        self.assertEqual(r["rain_mm"], {"sum": 3.5})
        self.assertEqual(r["count"], 5)

    def test_incomplete_hour_skipped(self):
        now = float(NOW)
        cur = int(now // HOUR) * HOUR
        write_raw(self.data, [
            frame(cur - 1800, 20.0),
            frame(now - 2, 99.9),          # current (incomplete) hour
        ])
        self.run_agg()
        self.assertEqual(len(self.hourly()), 1)

    def test_idempotent(self):
        h = H0
        write_raw(self.data, [frame(h + 600, 12.0),
                              frame(h + 1200, 13.0)])
        self.run_agg()
        first = self.hourly()
        self.run_agg()
        self.assertEqual(self.hourly(), first)

    def test_day_fold_and_weighted_avg(self):
        # two hours yesterday, one hour 1h before now (stays hourly)
        h0 = H0
        h1 = h0 + HOUR
        hnear = int(NOW // HOUR) * HOUR - 2 * HOUR
        write_raw(self.data, [
            frame(h0 + 600, 10.0, 50),
            frame(h0 + 1200, 14.0, 60),
            frame(h0 + 1800, 11.0, 55, 4.0, 4.0, 270),
            frame(h1 + 600, 16.0, 70, 8.0, 8.0, 270),
            frame(h1 + 1200, 18.0, 74, 8.5, 8.5, 270),
            frame(hnear + 600, 21.0),
        ])
        self.run_agg(fold_after=2 * HOUR)
        d = self.daily()
        self.assertEqual(len(d), 1)
        day = d[day_start(h0)]
        self.assertEqual(day["base"], "day")
        # hour0 avg 11.67 (2 frames), hour1 avg 17 (2 frames) filtered by
        # count weights: (11.67*3 + 17*1)/4  -- h0 has 3 frames? no:
        # h0 has 3 frames, h1 has 2 -> weighted (11.67*3 + 17*2)/5 = 13.8
        self.assertAlmostEqual(
            day["temperature_C"]["avg"], (11.6667 * 3 + 17 * 2) / 5, places=2)
        self.assertEqual(day["temperature_C"]["min"], 10.0)
        self.assertEqual(day["temperature_C"]["max"], 18.0)
        self.assertEqual(day["humidity"]["min"], 50)
        self.assertEqual(day["wind_gust_m_s"], {"max": 8.5})
        self.assertEqual(day["wind_dir_deg"],
                         {"mode": COMPASS[12]})  # sector of 270
        hours = self.hourly()
        self.assertNotIn(h0, hours)
        self.assertNotIn(h1, hours)
        self.assertIn(hnear, hours)

    def test_rebuild(self):
        h = H0
        write_raw(self.data, [frame(h + 600, 12.0, 60),
                              frame(h + 1200, 14.0, 62)])
        self.run_agg()
        before = self.hourly()
        self.run_agg(rebuild=True)
        self.assertEqual(self.hourly(), before)

    def test_gz_raw_input(self):
        import gzip
        h = H0
        rs = [frame(h + 600, 12.0, 60), frame(h + 1200, 14.0, 62)]
        p = os.path.join(self.data, "readings-00000000.jsonl")
        with gzip.open(p + ".gz", "wt") as f:
            for r in rs:
                f.write(json.dumps(r) + "\n")
        self.run_agg()
        rows = self.hourly()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[h]["count"], 2)


if __name__ == "__main__":
    unittest.main()