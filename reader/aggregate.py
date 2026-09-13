#!/usr/bin/env python3
"""Long-term roll-up aggregates: raw readings -> hourly blocks -> daily blocks.

Raw `readings-YYYYMMDD.jsonl` (from reader.live) is coalesced into UTC-hour
blocks once an hour is complete; hourly blocks older than `--fold-after`
(default 365 days) are folded into daily blocks that are kept forever.

Layout (JSONL, one file per UTC year):
    <agg-dir>/hourly-YYYY.jsonl    kept ~13 months, then folded away
    <agg-dir>/daily-YYYY.jsonl     kept forever

Aggregation is not uniform "high/low/average":
    temperature_C / humidity / wind_avg_m_s   min, avg, max + count
    wind_gust_m_s   max
    uvi             max
    rain_mm         sum of per-frame positive deltas
    wind_dir_deg    prevailing 16-sector midpoint

Idempotent: finished blocks are deterministic, so an existing block key is
kept. `--rebuild` regenerates all hourly blocks from raw (needed only after a
schema change).

Usage:
    aggregate.py --data-dir DIR [--agg-dir DIR] [--dry-run] [--rebuild]
                 [--now TS] [--fold-after SEC] [--quiet]
"""

import argparse
import glob
import gzip
import json
import os
import sys
import time

HOUR = 3600
DAY = 86400
RAW_GLOB = "readings-*.jsonl*"
SECTORS = 16
COMPASS = [round(i * 22.5) % 360 for i in range(SECTORS)]


def hour_start(ts):
    return int(ts // HOUR) * HOUR


def day_start(ts):
    return int(ts // DAY) * DAY


def utc_year(ts):
    return time.gmtime(int(ts)).tm_year


def open_lines(path):
    opener = gzip.open if path.endswith(".gz") else open
    with opener(path, "rt") as f:
        for line in f:
            line = line.strip()
            if line:
                yield json.loads(line)


def _open_text_rows(path):
    """Return {start_key: row} for an aggregate file (any year)."""
    out = {}
    if os.path.exists(path):
        try:
            for row in open_lines(path):
                out[row["start"]] = row
        except (ValueError, OSError) as exc:
            raise SystemExit(f"cannot read {path}: {exc}")
    return out


def write_jsonl(path, rows):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        for row in rows:
            f.write(json.dumps(row) + "\n")
    os.replace(tmp, path)


class Hla:
    """min/avg/max accumulator with a reading count."""
    __slots__ = ("n", "sum", "mn", "mx")

    def __init__(self):
        self.n = 0
        self.sum = 0.0
        self.mn = float("inf")
        self.mx = float("-inf")

    def add(self, v):
        if v is None:
            return
        self.n += 1
        self.sum += v
        if v < self.mn:
            self.mn = v
        if v > self.mx:
            self.mx = v

    def out(self):
        if not self.n:
            return None
        return {"min": round(self.mn, 2),
                "avg": round(self.sum / self.n, 2),
                "max": round(self.mx, 2)}


class HourAcc:
    __slots__ = ("count", "temp", "hum", "wind", "gust", "uv",
                 "rain_sum", "rain_n", "sectors")

    def __init__(self):
        self.count = 0
        self.temp = Hla()
        self.hum = Hla()
        self.wind = Hla()
        self.gust = None
        self.uv = None
        self.rain_sum = 0.0
        self.rain_n = 0
        self.sectors = [0] * SECTORS

    def add(self, r):
        self.count += 1
        self.temp.add(r.get("temperature_C"))
        self.hum.add(r.get("humidity"))
        self.wind.add(r.get("wind_avg_m_s"))
        g = r.get("wind_gust_m_s")
        if g is not None and (self.gust is None or g > self.gust):
            self.gust = g
        u = r.get("uvi")
        if u is not None and (self.uv is None or u > self.uv):
            self.uv = u
        d = r.get("wind_dir_deg")
        if d is not None:
            self.sectors[int(d / 22.5) % SECTORS] += 1
        if r.get("rain_ok") and r.get("rain_mm") is not None:
            self.rain_n += 1

    def out(self, start, base, rain_sum):
        row = {"start": start, "base": base, "count": self.count}
        t = self.temp.out()
        if t:
            row["temperature_C"] = t
        h = self.hum.out()
        if h:
            row["humidity"] = h
        w = self.wind.out()
        if w:
            row["wind_avg_m_s"] = w
        if self.gust is not None:
            row["wind_gust_m_s"] = {"max": round(self.gust, 2)}
        if rain_sum > 0 or self.rain_n:
            row["rain_mm"] = {"sum": round(rain_sum, 2)}
        if self.uv is not None:
            row["uvi"] = {"max": round(self.uv, 2)}
        best = max(range(SECTORS), key=lambda i: self.sectors[i])
        if self.sectors[best]:
            row["wind_dir_deg"] = {"mode": COMPASS[best]}
        return row


class DayAcc:
    """Fold of a set of hourly rows into a daily row (weighted by count)."""

    def __init__(self):
        self.count = 0
        self.temp = Hla()
        self.hum = Hla()
        self.wind = Hla()
        self.gust = None
        self.uv = None
        self.rain_sum = 0.0
        self.sectors = [0] * SECTORS

    def add_hour(self, h):
        base = h.get("count") or 0
        self.count += base
        self._fold_hla(self.temp, h.get("temperature_C"), base)
        self._fold_hla(self.hum, h.get("humidity"), base)
        self._fold_hla(self.wind, h.get("wind_avg_m_s"), base)
        if "wind_gust_m_s" in h:
            v = h["wind_gust_m_s"]["max"]
            if self.gust is None or v > self.gust:
                self.gust = v
        if "uvi" in h:
            v = h["uvi"]["max"]
            if self.uv is None or v > self.uv:
                self.uv = v
        if "rain_mm" in h:
            self.rain_sum += h["rain_mm"]["sum"]
        if "wind_dir_deg" in h:
            sector = int(h["wind_dir_deg"]["mode"] / 22.5) % SECTORS
            self.sectors[sector] += 1

    @staticmethod
    def _fold_hla(acc, stats, base):
        if not stats:
            return
        acc.mn = min(acc.mn, stats["min"])
        acc.mx = max(acc.mx, stats["max"])
        acc.sum += stats["avg"] * base
        acc.n += base

    def out(self, start):
        row = {"start": start, "base": "day", "count": self.count}
        t = self.temp.out()
        if t:
            row["temperature_C"] = t
        h = self.hum.out()
        if h:
            row["humidity"] = h
        w = self.wind.out()
        if w:
            row["wind_avg_m_s"] = w
        if self.gust is not None:
            row["wind_gust_m_s"] = {"max": round(self.gust, 2)}
        if self.rain_sum:
            row["rain_mm"] = {"sum": round(self.rain_sum, 2)}
        if self.uv is not None:
            row["uvi"] = {"max": round(self.uv, 2)}
        best = max(range(SECTORS), key=lambda i: self.sectors[i])
        if self.sectors[best]:
            row["wind_dir_deg"] = {"mode": COMPASS[best]}
        return row


def collect_hour_blocks(data_dir, now):
    """Aggregate raw jsonl into finished-hour blocks: {start: HourAcc}."""
    current_hour = hour_start(now)
    blocks = {}
    rain_ref = None
    pattern = glob.glob(os.path.join(data_dir, RAW_GLOB))
    for path in sorted(pattern):
        for r in open_lines(path):
            if not r.get("mic_ok"):
                continue
            ts = r.get("ts")
            if ts is None:
                continue
            b = hour_start(ts)
            if b >= current_hour:      # incomplete hour: skip entirely
                continue
            blk = blocks.get(b)
            if blk is None:
                blk = blocks[b] = HourAcc()
                blocks[b] = blk
            blk.add(r)
            if r.get("rain_ok") and r.get("rain_mm") is not None:
                rm = r["rain_mm"]
                if rain_ref is not None:
                    d = rm - rain_ref
                    if 0 <= d < 1000:
                        blk.rain_sum += d
                rain_ref = rm
    return current_hour, blocks


def hourly_files(agg_dir):
    return sorted(glob.glob(os.path.join(agg_dir, "hourly-*.jsonl")))


def daily_files(agg_dir):
    return sorted(glob.glob(os.path.join(agg_dir, "daily-*.jsonl")))


def write_hourly(agg_dir, rows):
    by_year = {}
    for row in rows:
        by_year.setdefault(utc_year(row["start"]), []).append(row)
    for year, ys in by_year.items():
        ys.sort(key=lambda x: x["start"])
        write_jsonl(os.path.join(agg_dir, f"hourly-{year}.jsonl"), ys)


def write_daily(agg_dir, rows):
    by_year = {}
    for row in rows:
        by_year.setdefault(utc_year(row["start"]), []).append(row)
    for year, ys in by_year.items():
        ys.sort(key=lambda x: x["start"])
        write_jsonl(os.path.join(agg_dir, f"daily-{year}.jsonl"), ys)


def _combine_existing(agg_dir, which):
    rows = {}
    paths = glob.glob(os.path.join(agg_dir, f"{which}-*.jsonl"))
    for p in paths:
        rows.update(_open_text_rows(p))
    return rows


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--data-dir", default=None,
                    help="raw readings dir (default: ./log)")
    ap.add_argument("--agg-dir", default=None,
                    help="aggregates dir (default: <data-dir>/../aggregates)")
    ap.add_argument("--now", type=float, default=None,
                    help="override 'now' (epoch s) for deterministic runs/tests")
    ap.add_argument("--fold-after", type=float, default=365 * DAY,
                    help="fold hour blocks older than this into days")
    ap.add_argument("--rebuild", action="store_true",
                    help="regenerate all hourly blocks from raw")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args(argv)

    now = args.now if args.now is not None else time.time()
    data_dir = args.data_dir or os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "log")
    agg_dir = args.agg_dir or os.path.join(
        os.path.dirname(data_dir) or ".", "aggregates")

    if not os.path.isdir(data_dir):
        raise SystemExit(f"data dir not found: {data_dir}")

    current_hour, raw_hours = collect_hour_blocks(data_dir, now)
    if not raw_hours:
        print(f"no raw readings found in {data_dir}")
        return 0

    # upsert newly-completed hour blocks into the rolling hourly store
    hourly = _combine_existing(agg_dir, "hourly") if not args.rebuild else {}
    new = 0
    for start in sorted(raw_hours):
        blk = raw_hours[start]
        row = blk.out(start, "hour", blk.rain_sum)
        if start in hourly:
            continue
        hourly[start] = row
        new += 1

    # fold old hours into days (atomically per day; a folded day is final)
    cutoff = int(current_hour - args.fold_after)
    days = _combine_existing(agg_dir, "daily")
    by_day = {}
    for start in sorted(hourly):
        if start < cutoff:
            by_day.setdefault(day_start(start), []).append(hourly[start])
    folded_hours = 0
    for d, hrs in by_day.items():
        if d not in days:
            acc = DayAcc()
            for h in sorted(hrs, key=lambda x: x["start"]):
                acc.add_hour(h)
            days[d] = acc.out(d)
        for h in hrs:
            del hourly[h["start"]]
        folded_hours += len(hrs)

    if not args.dry_run:
        write_hourly(agg_dir, list(hourly.values()))
        write_daily(agg_dir, list(days.values()))
        for p in hourly_files(agg_dir) + daily_files(agg_dir):
            if os.path.getsize(p) == 0:
                os.remove(p)
    else:
        summary = {"new_hour_blocks": new, "folded_hour_blocks": folded_hours,
                   "day_blocks": len(days)}
        print(json.dumps(summary))
        return 0

    if not args.quiet:
        print(f"{'rebuild' if args.rebuild else 'aggregate'}: "
              f"{len(raw_hours)} hours in raw window, {len(hourly)} hourly rows kept, "
              f"{new} added, {folded_hours} folded, {len(days)} daily rows")
    return 0


if __name__ == "__main__":
    sys.exit(main())