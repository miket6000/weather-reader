"""Burst -> validated frames -> ready-to-log records."""

import json
import time

from .dsp import (
    Cfg,
    decode_burst,
    estimate_offset,
    find_bursts,
    shift_to_virtual_center,
    soft_metric,
)
from .frames import parse_msg


def decode_iq(iq, fs=1.8e6, shift_hz=0.0, tone_khz=60.0, sym_us=124.0,
              cfg=None, seg_s=4.0, overlap_s=0.5, sensor_id=None,
              max_wind_m_s=60.0, dir_offset=0.0, afc_hz=0.0,
              afc_max_hz=0.0, afc_alpha=0.2, afc_search_khz=None):
    """Decode all valid frames from a raw complex IQ array.

    Returns list of records (already JSON-serializable). Processes the IQ in
    time windows to bound memory/CPU on long dumps. If `sensor_id` is given,
    frames from any other transmitter are dropped. `dir_offset` is a vane
    correction in degrees applied to every direction.

    `shift_hz` applies a fixed frequency correction to the whole array (the
    offline "virtual center minus sample center"). When `afc_max_hz` > 0 an
    automatic frequency correction loop is enabled: each window's tone-pair
    offset is estimated and smoothed into `afc_hz`, which is applied by
    shifting the window before demodulating (see estimate_offset).
    """
    cfg = cfg or Cfg(fs=fs, tone_khz=tone_khz, sym_us=sym_us)
    iq = shift_to_virtual_center(iq, fs, shift_hz)
    afc = float(afc_hz)
    afc_max = float(afc_max_hz)
    search_khz = afc_search_khz if afc_search_khz is not None else afc_max / 1e3
    n = len(iq)
    hop = int((seg_s - overlap_s) * fs)
    seg = int(seg_s * fs)
    records = []
    t0 = 0.0
    while t0 < n:
        i0 = int(t0)
        i1 = min(n, i0 + seg)
        window = shift_to_virtual_center(iq[i0:i1], fs, afc)
        soft, energy = soft_metric(window, cfg)
        base_t = i0 / fs
        bursts = find_bursts(soft, energy, fs)
        for (s0, s1) in bursts:
            cands = decode_burst(soft[s0:s1], fs)
            recs = _records_for(cands, base_t + s0 / fs, fs,
                                max_wind_m_s=max_wind_m_s,
                                dir_offset=dir_offset)
            records.extend(r for r in recs if r["mic_ok"])
        if afc_max > 0:
            meas = estimate_offset(iq[i0:i1], fs, tone_khz=tone_khz,
                                   search_khz=search_khz)
            if meas is not None:
                clamped = max(-afc_max, min(afc_max, meas))
                afc = afc + afc_alpha * (clamped - afc)
        if i1 >= n:
            break
        t0 += hop
    if sensor_id:
        records = [r for r in records if r["id"] == sensor_id]
    return records


def _records_for(cands, burst_start_s, fs, max_wind_m_s=60.0, dir_offset=0.0):
    out = []
    seen = set()
    for c in cands:
        f = parse_msg(c["msg"], max_wind_m_s=max_wind_m_s,
                      dir_offset=dir_offset)
        key = (c["msg"].hex(), c["polarity"], c["sync_bit"])
        if key in seen:
            continue
        seen.add(key)
        t_s = c["T_samples"] / fs
        sym_us = t_s * 1e6
        rec = {
            "ts": time.time(),
            "burst_start_s": burst_start_s,
            "mic_ok": f["mic_ok"],
            "digest_ok": f["digest_ok"],
            "checksum_ok": f["checksum_ok"],
            "polarity": c["polarity"],
            "sync_bit": c["sync_bit"],
            "sync_err_bits": c["sync_err_bits"],
            "symbol_us": round(sym_us, 2),
            "model": "Bresser-6in1",
            "id": f["id"],
            "channel": f["channel"],
            "sensor_type": f["sensor_type"],
            "startup": f["startup"],
            "battery_ok": f["battery_ok"],
            "temperature_C": f["temperature_C"],
            "humidity": f["humidity"],
            "wind_gust_m_s": f["wind_gust_m_s"],
            "wind_avg_m_s": f["wind_avg_m_s"],
            "wind_dir_deg": f["wind_dir_deg"],
            "rain_ok": f["rain_ok"],
            "rain_mm": f["rain_mm"],
            "uv_ok": f["uv_ok"],
            "uvi": f["uvi"],
            "flags": f["flags"],
            "moisture": f["moisture"],
            "suspect": f["suspect"],
            "mic": "CRC" if f["mic_ok"] else "FAIL",
            "msg_hex": f["msg"],
        }
        out.append(rec)
    return out


def read_cf32(path, max_samples=None):
    """Load a GQRX 'fc' recording (float32 complex I/Q)."""
    import numpy as np

    if max_samples:
        iq = np.fromfile(path, dtype=np.float32, count=max_samples * 2)
    else:
        iq = np.fromfile(path, dtype=np.float32)
    return iq.view(np.complex64)


def frames_to_jsonl(records, stream):
    for r in records:
        stream.write(json.dumps(r) + "\n")
    stream.flush()