#!/usr/bin/env python3
"""Decode a GQRX CF32I/Q recording of the Digitech XC0432 (Bresser 6-in-1).

Usage:
    decode_file.py RECORDING.raw [-c CENTER_HZ] [-s SAMPLE_RATE] [-o OUT.jsonl]
"""

import argparse
import json
import sys

from .config import apply_cli_overrides, load_config
from .pipeline import read_cf32, decode_iq


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("file")
    ap.add_argument("-c", "--center", type=float, default=917144300.0,
                    help="recording center frequency in Hz (default 917144300)")
    ap.add_argument("-T", "--tone-mid", type=float, default=916849300.0,
                    help="virtual center (midpoint of the two tones) in Hz")
    ap.add_argument("-s", "--rate", type=float, default=1.8e6)
    ap.add_argument("-o", "--out", default=None)
    ap.add_argument("--config", default=None,
                    help="path to JSON config (default: ./config.json)")
    ap.add_argument("--sensor-id", default=None,
                    help="only accept this sensor id (default from config; "
                         "empty string disables filtering)")
    ap.add_argument("--max-wind", type=float, default=None,
                    help="drop gusts/averages above this speed in m/s "
                         "(default from config)")
    ap.add_argument("--dir-offset", type=float, default=None,
                    help="wind-direction correction in degrees, -360..+360 "
                         "(default from config)")
    args = ap.parse_args(argv)

    active = apply_cli_overrides(load_config(args.config), args)

    iq = read_cf32(args.file)
    shift_hz = args.tone_mid - args.center
    records = decode_iq(iq, fs=args.rate, shift_hz=shift_hz,
                        sensor_id=active["sensor_id"],
                        max_wind_m_s=active["max_wind_m_s"],
                        dir_offset=active["dir_offset"])

    text = "\n".join(json.dumps(r) for r in records)
    if args.out:
        with open(args.out, "w") as f:
            f.write(text + "\n")
        print(f"wrote {len(records)} frame(s) to {args.out}")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())