#!/usr/bin/env python3
"""Re-derive every JSONL reading through the current frame decoder.

Useful after decode rules change (e.g. the wind plausibility checks): rewrites
readings-*.jsonl in place so historical data carries the same validation and
'suspect' flags the live pipeline now produces.

Usage:
    backfill.py [-d LOG_DIR] [--config PATH] [--max-wind M/S] [--no-backup]

Polarity/sync/timing fields are taken from the stored record; msg_hex is
re-parsed via reader.frames.parse_msg with the configured max wind speed.
"""

import argparse
import glob
import json
import os
import shutil
import sys

from .config import apply_cli_overrides, load_config
from .pipeline import _records_for


def backfill_file(path, max_wind_m_s, dir_offset=0.0, backup=True):
    if backup:
        shutil.copyfile(path, path + ".bak")
    fs = 1.0e6  # any fs works; T_samples is scaled to preserve stored symbol_us
    out_lines = []
    changed = 0
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            old = json.loads(line)
            msg = bytes.fromhex(old["msg_hex"].replace(" ", ""))
            cand = {
                "msg": msg,
                "polarity": old["polarity"],
                "sync_bit": old["sync_bit"],
                "sync_err_bits": old["sync_err_bits"],
                # preserved timing: recomputed symbol_us == stored symbol_us
                "T_samples": old["symbol_us"],
            }
            recs = _records_for([cand], old["burst_start_s"], fs,
                                max_wind_m_s=max_wind_m_s,
                                dir_offset=dir_offset)
            rec = recs[0]
            rec["ts"] = old["ts"]
            rec["burst_start_s"] = old["burst_start_s"]
            if rec.get("symbol_us") != old.get("symbol_us"):
                rec["symbol_us"] = old["symbol_us"]
            if rec != old:
                changed += 1
            out_lines.append(rec)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        for rec in out_lines:
            f.write(json.dumps(rec) + "\n")
    os.replace(tmp, path)
    return len(out_lines), changed


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("-d", "--log-dir",
                    default="/home/mike/weather-reader/log")
    ap.add_argument("--config", default=None)
    ap.add_argument("--max-wind", type=float, default=None)
    ap.add_argument("--dir-offset", type=float, default=None)
    ap.add_argument("--no-backup", action="store_true")
    args = ap.parse_args(argv)

    active = apply_cli_overrides(load_config(args.config), args)
    max_wind = active["max_wind_m_s"]
    dir_offset = active["dir_offset"]

    paths = sorted(glob.glob(os.path.join(args.log_dir, "readings-*.jsonl")))
    if not paths:
        print(f"no readings-*.jsonl in {args.log_dir}")
        return 1
    total = 0
    for p in paths:
        n, changed = backfill_file(p, max_wind, dir_offset=dir_offset,
                                   backup=not args.no_backup)
        print(f"{os.path.basename(p)}: {n} rows, {changed} rewritten")
        total += changed
    print(f"backfill complete ({total} rows changed)")
    return 0


if __name__ == "__main__":
    sys.exit(main())