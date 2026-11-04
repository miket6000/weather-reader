"""Streaming decode engine for the XC0432 (Bresser 6-in-1) over RTL-SDR.

This is the heart shared by the standalone reader and the weewx driver: it
owns the rtl_sdr subprocess, converts U8 I/Q to complex, runs the automatic
frequency correction for crystal drift, detects/demodulates/dedupes bursts,
filters on MIC and sensor_id, and hands each validated frame record to a
sink callback.  All decode state (AFC, dedupe table, subprocess handle)
lives here so callers can rebuild their packet generator without losing it.
"""

import logging
import subprocess
import threading
import time

import numpy as np

from .dsp import (
    Cfg,
    estimate_offset,
    find_bursts,
    shift_to_virtual_center,
    soft_metric,
)
from .pipeline import _records_for, decode_burst


def u8_to_complex(buf):
    """Convert an rtl_sdr U8 interleaved I/Q buffer to centered complex64."""
    a = np.frombuffer(buf, dtype=np.uint8)
    n = a.size // 2
    a = a[: n * 2].reshape(n, 2)
    return ((a[:, 0] - 127.5) / 127.5 + 1j * (a[:, 1] - 127.5) / 127.5).astype(np.complex64)


class SdrDecoder:
    """Owns the rtl_sdr process and produces validated frame records.

    Instantiate with the tuning and decode options, then ``start()`` a
    background decode thread and stop it with ``stop()``.  Every validated,
    non-duplicate frame whose MIC passes (and, when set, whose sensor_id
    matches the filter) is passed to the ``sink`` callable as a record dict
    with the same shape ``pipeline._records_for`` produces.
    """

    def __init__(self, cfg=None, *, center=916850000, gain=30.0, fs=1.8e6,
                 tone_khz=60.0, block_s=4.0, overlap_s=1.0, dedupe_s=8.0,
                 sensor_id="", max_wind_m_s=60.0, dir_offset=0.0,
                 wind_mps_per_count=0.1, rain_mm_per_count=0.04,
                 afc_enable=True, afc_max_hz=40000.0, afc_alpha=0.2,
                 rtl_bufsize=262144, rtl_cmd="rtl_sdr",
                 sink=None, logger=None):
        self.cfg = cfg or Cfg(fs=fs, tone_khz=tone_khz)
        self.tone_khz = float(tone_khz)
        self.center = int(center)
        self.gain = float(gain)
        self.fs = float(fs)
        self.block_s = float(block_s)
        self.overlap_s = float(overlap_s)
        self.dedupe_s = float(dedupe_s)
        self.sensor_id = str(sensor_id or "")
        self.max_wind_m_s = float(max_wind_m_s)
        self.dir_offset = float(dir_offset)
        self.wind_mps_per_count = float(wind_mps_per_count)
        self.rain_mm_per_count = float(rain_mm_per_count)
        self.afc_enable = bool(afc_enable)
        self.afc_max_hz = float(afc_max_hz)
        self.afc_alpha = float(afc_alpha)
        self.rtl_bufsize = int(rtl_bufsize)
        self.rtl_cmd = rtl_cmd
        self.sink = sink or (lambda record: None)
        self.log = logger or logging.getLogger(__name__)

        self.block = int(self.block_s * self.fs)
        self.overlap = int(self.overlap_s * self.fs)
        self.buf_bytes = self.block * 4

        self._stop = threading.Event()
        self._thread = None
        self._proc = None

    # -- lifecycle ------------------------------------------------------

    @property
    def running(self):
        return self._thread is not None and self._thread.is_alive()

    def start(self):
        self._stop.clear()
        self._thread = threading.Thread(target=self._run_forever,
                                        name="bresser6in1-sdr",
                                        daemon=True)
        self._thread.start()
        return self

    def stop(self):
        self._stop.set()
        proc, self._proc = self._proc, None
        if proc is not None:
            try:
                proc.terminate()
                proc.wait(timeout=2)
            except Exception:
                pass
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

    # -- threads --------------------------------------------------------

    def _run_forever(self):
        """Keep the decode loop alive; restart it if anything kills it."""
        while not self._stop.is_set():
            try:
                self._run()
            except Exception as exc:
                if self._stop.is_set():
                    break
                self.log.error("Decoder thread crashed: %s", exc)
                time.sleep(2)

    def _run(self):
        """The foreground decode loop (runs on the background thread)."""
        afc = 0.0
        last_afc_log = time.time()
        sig_since = time.time()
        proc = None
        carry = None
        recent = {}

        while not self._stop.is_set():
            if proc is None or proc.poll() is not None:
                cmd = [self.rtl_cmd, "-f", str(self.center), "-s", str(int(self.fs)),
                       "-g", str(self.gain), "-b", str(self.rtl_bufsize), "-"]
                self.log.info("spawn %s", " ".join(cmd))
                try:
                    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                            bufsize=1 << 20)
                except OSError as exc:
                    self.log.error("cannot start %s: %s; retrying in 2 s",
                                   self.rtl_cmd, exc)
                    time.sleep(2)
                    continue
                self._proc = proc
            raw = proc.stdout.read(self.buf_bytes)
            if not raw:
                if self._stop.is_set():
                    break
                self.log.error("rtl_sdr exited; restarting in 2 s")
                time.sleep(2)
                carry = None
                continue
            new = u8_to_complex(raw)

            # Automatic frequency correction: measure the tone-pair offset on
            # the raw block, smooth it, and shift the window so the two tones
            # sit back at +-tone_khz.  Crystal drift moves both tones equally,
            # so one correction cancels receiver + transmitter drift.
            meas = None
            if self.afc_enable:
                meas = estimate_offset(new, self.fs, tone_khz=self.tone_khz,
                                       search_khz=self.afc_max_hz / 1e3)
                if meas is not None:
                    prev = afc
                    afc += self.afc_alpha * (max(-self.afc_max_hz,
                                                 min(self.afc_max_hz, meas)) - afc)
                    if abs(afc - prev) > 50.0:
                        self.log.info("afc %+.0f -> %+.0f Hz", prev, afc)

            raw_win = new if carry is None else np.concatenate([carry, new])
            win = shift_to_virtual_center(raw_win, self.fs, afc)
            soft, energy = soft_metric(win, self.cfg)
            bursts = find_bursts(soft, energy, self.fs)
            decoded = 0
            now = time.time()
            for (s0, s1) in bursts:
                cands = decode_burst(soft[s0:s1], self.fs)
                for rec in _records_for(cands, s0 / self.fs, self.fs,
                                        max_wind_m_s=self.max_wind_m_s,
                                        dir_offset=self.dir_offset,
                                        wind_mps_per_count=self.wind_mps_per_count,
                                        rain_mm_per_count=self.rain_mm_per_count):
                    if not rec["mic_ok"]:
                        continue
                    if self.sensor_id and rec["id"] != self.sensor_id:
                        continue
                    if rec["suspect"]:
                        self.log.debug("suspect frame id=%s ch=%s startup=%s",
                                       rec["id"], rec["channel"], rec["startup"])
                    key = (rec["id"], rec["msg_hex"], rec["polarity"],
                           rec["sync_bit"])
                    if key in recent and now - recent[key] < self.dedupe_s:
                        continue
                    recent[key] = now
                    if len(recent) > 128:
                        for k in list(recent):
                            if now - recent[k] > self.dedupe_s:
                                del recent[k]
                    decoded += 1
                    self.sink(rec)

            # Per-block housekeeping: afc log + no-decode health note.
            if self.afc_enable and now - last_afc_log >= 300.0:
                self.log.info("afc %+.0f Hz", afc)
                last_afc_log = now
            if decoded:
                sig_since = now
            elif now - sig_since > 60.0:
                if bursts or meas is not None:
                    self.log.error("signal present but no valid frames for "
                                   "%d s (drift beyond afc range, sensor issue, "
                                   "or interference?)", int(now - sig_since))
                else:
                    self.log.error("no signal for %d s (sensor silent or dongle "
                                   "problem?)", int(now - sig_since))
                sig_since = now
            if carry is None or len(win) >= self.overlap:
                carry = raw_win[-self.overlap:]

        if proc is not None:
            try:
                proc.terminate()
                proc.wait(timeout=2)
            except Exception:
                pass
        self._proc = None