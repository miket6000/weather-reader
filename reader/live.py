#!/usr/bin/env python3
"""Live reader: RTL-SDR -> decode -> JSONL (and stdout).

Spawns rtl_sdr on the target frequency, converts U8 I/Q to complex, decodes
the XC0432 bursts from a sliding window, appends valid frames to the JSONL log
and mirrors them to stdout (consumed by the Node service).

Usage:
    live.py [-c 916850000] [-s 1800000] [-g 30] [-d DIR] [--no-log]

Exit codes: 0 clean stop (SIGINT), 1 rtl_sdr failure/restart loop.
"""

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from datetime import datetime

import numpy as np

from .config import apply_cli_overrides, load_config
from .dsp import (
    Cfg,
    estimate_offset,
    find_bursts,
    shift_to_virtual_center,
    soft_metric,
)
from .pipeline import _records_for, decode_burst

STOP = False


def _sigint(signum, frame):
    global STOP
    STOP = True


def u8_to_complex(buf):
    """Convert an rtl_sdr U8 interleaved I/Q buffer to centered complex64."""
    a = np.frombuffer(buf, dtype=np.uint8)
    n = a.size // 2
    a = a[: n * 2].reshape(n, 2)
    return ((a[:, 0] - 127.5) / 127.5 + 1j * (a[:, 1] - 127.5) / 127.5).astype(np.complex64)


def _open_log(directory):
    os.makedirs(directory, mode=0o755, exist_ok=True)
    day = datetime.now().strftime("%Y%m%d")
    path = os.path.join(directory, f"readings-{day}.jsonl")
    return open(path, "a")


def run(args):
    global STOP
    signal.signal(signal.SIGINT, _sigint)
    signal.signal(signal.SIGTERM, _sigint)

    fs = args.rate
    cfg = Cfg(fs=fs, tone_khz=args.tone_khz)
    block = int(args.block_s * fs)          # new samples per iteration
    overlap = int(args.overlap_s * fs)      # suffix kept across iterations

    # rtl_sdr emits (fs * block_s) complex samples as 4 * that many raw bytes
    buf_bytes = block * 4

    active = apply_cli_overrides(load_config(args.config), args)
    sensor_id = active["sensor_id"]
    max_wind = active["max_wind_m_s"]
    dir_offset = active["dir_offset"]
    afc_enable = active["afc_enable"]
    afc_max = active["afc_max_hz"]
    afc_alpha = active["afc_alpha"]
    mode = ("all sensors" if not sensor_id else f"sensor id {sensor_id}")
    print(f"[live] config: {mode}, max wind {max_wind} m/s, "
          f"dir offset {dir_offset} deg, afc {'on' if afc_enable else 'off'} "
          f"<={afc_max:.0f} Hz", file=sys.stderr, flush=True)

    log = None if args.no_log else _open_log(args.directory)
    recent = {}  # (id, msg_hex, polarity, sync_bit) -> timestamp

    afc = 0.0
    last_afc_log = time.time()
    sig_since = time.time()

    proc = None
    carry = None
    while not STOP:
        if proc is None or proc.poll() is not None:
            cmd = ["rtl_sdr", "-f", str(args.center), "-s", str(fs),
                   "-g", str(args.gain), "-b", "262144", "-"]
            print(f"[live] spawn {' '.join(cmd)}", file=sys.stderr, flush=True)
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, bufsize=1 << 20)
        raw = proc.stdout.read(buf_bytes)
        if not raw:
            print("[live] rtl_sdr exited; restarting in 2 s", file=sys.stderr, flush=True)
            time.sleep(2)
            continue
        new = u8_to_complex(raw)

        # Automatic frequency correction: measure the tone-pair offset on the
        # raw block (before any correction), smooth it, and shift the window
        # so the two tones sit back at +-tone_khz (crystal drift moves both
        # tones equally, so one correction cancels receiver + transmitter drift).
        meas = None
        if afc_enable:
            meas = estimate_offset(new, fs, tone_khz=args.tone_khz,
                                   search_khz=afc_max / 1e3)
            if meas is not None:
                prev = afc
                afc += afc_alpha * (max(-afc_max, min(afc_max, meas)) - afc)
                if abs(afc - prev) > 50.0:
                    print(f"[live] afc {prev:+.0f} -> {afc:+.0f} Hz",
                          file=sys.stderr, flush=True)

        raw_win = new if carry is None else np.concatenate([carry, new])
        win = shift_to_virtual_center(raw_win, fs, afc)
        soft, energy = soft_metric(win, cfg)
        bursts = find_bursts(soft, energy, fs)
        decoded = 0
        for (s0, s1) in bursts:
            cands = decode_burst(soft[s0:s1], fs)
            for rec in _records_for(cands, s0 / fs, fs, max_wind_m_s=max_wind,
                                     dir_offset=dir_offset):
                if not rec["mic_ok"]:
                    continue
                if sensor_id and rec["id"] != sensor_id:
                    continue
                if rec["suspect"]:
                    print(f"[live] suspect frame id={rec['id']} "
                          f"ch={rec['channel']} startup={rec['startup']}",
                          file=sys.stderr, flush=True)
                key = (rec["id"], rec["msg_hex"], rec["polarity"], rec["sync_bit"])
                now = time.time()
                if key in recent and now - recent[key] < args.dedupe_s:
                    continue
                recent[key] = now
                if len(recent) > 128:
                    for k in list(recent):
                        if now - recent[k] > args.dedupe_s:
                            del recent[k]
                decoded += 1
                line = json.dumps(rec)
                print(line, flush=True)
                if log:
                    log.write(line + "\n")
                    log.flush()

        # Per-block housekeeping: afc drift watchdog log + no-decode health note.
        now = time.time()
        if afc_enable and now - last_afc_log >= 300.0:
            print(f"[live] afc {afc:+.0f} Hz", file=sys.stderr, flush=True)
            last_afc_log = now
        if decoded:
            sig_since = now
        elif now - sig_since > 60.0:
            if bursts or meas is not None:
                print(f"[live] signal present but no valid frames for "
                      f"{int(now - sig_since)} s (drift beyond afc range, "
                      f"sensor issue, or interference?)", file=sys.stderr,
                      flush=True)
            else:
                print(f"[live] no signal for {int(now - sig_since)} s "
                      f"(sensor silent or dongle problem?)",
                      file=sys.stderr, flush=True)
            sig_since = now
        if carry is None or len(win) >= overlap:
            carry = raw_win[-overlap:]
    if log:
        log.close()
    if proc:
        proc.terminate()
    print("[live] stopped", file=sys.stderr, flush=True)
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("-c", "--center", type=int, default=916850000)
    ap.add_argument("-s", "--rate", type=float, default=1.8e6)
    ap.add_argument("-g", "--gain", type=float, default=30.0,
                    help="RTL-SDR gain in dB (0 = auto)")
    ap.add_argument("--tone-khz", type=float, default=60.0)
    ap.add_argument("--block-s", type=float, default=4.0)
    ap.add_argument("--overlap-s", type=float, default=1.0)
    ap.add_argument("--dedupe-s", type=float, default=8.0)
    ap.add_argument("-d", "--directory", default="/home/mike/weather-reader/log")
    ap.add_argument("--no-log", action="store_true")
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
    ap.add_argument("--no-afc", dest="afc_enable", action="store_false",
                    default=None,
                    help="disable automatic frequency correction (config "
                         "default is on)")
    ap.add_argument("--afc-max", type=float, default=None,
                    help="max tone-mid offset to track in Hz (config default "
                         "40000)")
    ap.add_argument("--afc-alpha", type=float, default=None,
                    help="AFC smoothing factor 0..1 (config default 0.2; "
                         "1 = instant)")
    return run(ap.parse_args(argv))


if __name__ == "__main__":
    sys.exit(main())