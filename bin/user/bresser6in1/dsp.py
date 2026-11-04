"""DSP for the XC0432 2-FSK burst decoder.

Signal: 2-level FSK (NRZ PCM), symbol period 124 us, deviation ~ +-60 kHz.
Frames: 0xAA... preamble, 0x2DD4 sync, 18-byte message.

All processing happens at the input sample rate (1.8 MHz). The IQ stream is
first shifted so the two tones sit at +tone1 (bit '1') / -tone1 relative to a
virtual center, then a two-tone correlator produces a soft metric in [-1, 1].
"""

import numpy as np

from .frames import SYNC_BITS, MSG_BYTES

SYMBOL_S = 124e-6
PATTERN_BITS = bin(0xAAAA2DD4)[2:].zfill(32)  # 0xAA 0xAA 0x2D 0xD4 (rtl_433 preamble)


class Cfg:
    def __init__(self, fs=1.8e6, tone_khz=60.0, sym_us=124.0):
        self.fs = float(fs)
        self.tone = float(tone_khz) * 1e3
        self.sym = float(sym_us) * 1e-6
        self.tone0 = -self.tone  # '0'
        self.tone1 = +self.tone  # '1'


def shift_to_virtual_center(iq, fs, shift_hz):
    """Multiply by exp(-j2pi*shift*t); shift_hz = virtual_center - sample_center."""
    if abs(shift_hz) < 1:
        return iq
    n = len(iq)
    t = np.arange(n) / fs
    return iq * np.exp(-1j * 2 * np.pi * shift_hz * t)


def estimate_offset(iq, fs, tone_khz=60.0, search_khz=40.0, sym_us=121.5,
                    lag=8, coher=0.30, min_tone_hits=4):
    """Estimate the carrier offset of the two FSK tones relative to DC.

    The station's two tones sit at +-tone_khz*kHz around a tone midpoint;
    receiver-crystal and transmitter drift shift *both* tones equally. The
    instantaneous frequency is measured per ~one-symbol hop (phase slope of a
    delayed-conjugate product, which a short FSK burst's spectral smear cannot
    corrupt), then the measured hops are split into the two tone groups by
    sign. The midpoint of the two groups' medians is the carrier offset.

    Hops are kept only when the hop's coherent gain r = |sum(prod)|/sum(|iq|^2)
    exceeds `coher`: a CW tone integrates coherently (r ~ 0.5..1) while noise
    hops stay near r ~ 0.06..0.2 no matter how loud, so weak bursts are found
    and quiet/empty blocks yield None.
    """
    iq = np.asarray(iq)
    n = len(iq)
    if n < 4096:
        return None
    lag = max(1, int(lag))
    hop = max(8, int(round(sym_us * 1e-6 * fs)))  # ~one symbol

    iq64 = iq.astype(np.complex128)
    prod = iq64[:-lag] * np.conj(iq64[lag:])      # phase advance per `lag`
    power = np.abs(iq64[:-lag]) ** 2
    nh = (len(prod)) // hop
    if nh < 8:
        return None
    idx = np.arange(nh) * hop
    sums = np.add.reduceat(prod[:nh * hop], idx)
    pows = np.add.reduceat(power[:nh * hop], idx)

    with np.errstate(divide="ignore", invalid="ignore"):
        r = np.abs(sums) / pows
    freqs = np.full(nh, np.nan)
    hits = r > coher
    if np.count_nonzero(hits) == 0:
        return None
    freqs[hits] = -np.angle(sums[hits]) * fs / (2.0 * np.pi * lag)

    limit = (tone_khz + search_khz) * 1e3
    inband = hits & (np.abs(freqs) <= limit) & ~np.isnan(freqs)
    if np.count_nonzero(inband) < 2 * min_tone_hits:
        return None
    f = freqs[inband]
    pos = f[f > 0]
    neg = f[f < 0]
    if pos.size < min_tone_hits or neg.size < min_tone_hits:
        return None
    med_pos = float(np.median(pos))
    med_neg = float(np.median(neg))
    # both groups must be tight clusters around the tones (rejects noise hops)
    for group in (pos, neg):
        if np.std(group) > 0.4 * tone_khz * 1e3:
            return None
    cand = 0.5 * (med_pos + med_neg)
    if abs(cand) > search_khz * 1e3:
        return None
    return cand


def soft_metric(iq, cfg, win=None):
    """Two-tone correlator soft metric in [-1, 1]; positive == tone1 ('1').

    Uses a boxcar integrate-and-dump via cumulative sums (O(N), float64) so
    long dumps are cheap. `win` is the integration window in samples.
    Returns (soft, energy) where energy = e1 + e0 is used for burst detection.
    """
    if win is None:
        win = max(8, int(round(cfg.sym * cfg.fs * 0.9)))
    n = len(iq)
    tt = np.arange(n) / cfg.fs
    d1 = iq * np.exp(-1j * 2 * np.pi * cfg.tone1 * tt)
    d0 = iq * np.exp(-1j * 2 * np.pi * cfg.tone0 * tt)
    cs1 = np.concatenate([[0.0], np.cumsum(d1, dtype=np.complex128)])
    cs0 = np.concatenate([[0.0], np.cumsum(d0, dtype=np.complex128)])
    s1 = cs1[win:] - cs1[:-win]
    s0 = cs0[win:] - cs0[:-win]
    e1 = np.abs(s1) ** 2
    e0 = np.abs(s0) ** 2
    return (e1 - e0) / (e1 + e0 + 1e-9), e1 + e0


def _boxcar(x, w):
    """Sliding-window mean via cumsum (O(n))."""
    if w <= 1:
        return x.astype(float)
    cs = np.concatenate([[0.0], np.cumsum(x, dtype=float)])
    out = np.empty(len(x), dtype=float)
    c0 = cs[:len(x)] if w > len(x) else cs[:len(x) - w + 1]
    out[:len(c0)] = (cs[w:] - cs[:-w]) / w
    out[len(c0):] = out[len(c0) - 1]
    return out


def find_bursts(soft, energy, fs, thresh_dB=12.0, min_len_s=0.02, margin_s=0.008):
    """Return list of (start_sample, end_sample) burst regions.

    Uses absolute two-tone power `energy` against a robust noise-floor estimate
    (median), thresholded at median + `thresh_dB` dB, coarse-averaged over
    500 ms, then padded by margin_s.
    """
    floor = np.median(energy)
    thr = floor * (10 ** (thresh_dB / 10.0))
    act = energy > thr
    act = _boxcar(act.astype(float), max(1, int(0.01 * fs))) > 0.5
    on = act.astype(int)
    if on.size == 0:
        return []
    first_on = bool(on[0])
    edges = np.flatnonzero(np.diff(on) != 0)
    segs = np.concatenate([[0], edges + 1, [len(soft)]])
    m = int(margin_s * fs)
    min_len = int(min_len_s * fs)
    out = []
    for j in range(len(segs) - 1):
        run_on = (first_on == (j % 2 == 0))
        if not run_on or segs[j + 1] - segs[j] <= min_len:
            continue
        s0, s1 = segs[j], segs[j + 1]
        out.append((max(0, s0 - m), min(len(soft), s1 + m)))
    return out


def _bits(soft, T, p):
    """Sample soft at period T, phase p (samples). Returns '0'/'1' string."""
    n = len(soft)
    if n < 2:
        return ""
    ks = np.arange(int((n - p) / T))
    if len(ks) < 2:
        return ""
    c0 = np.maximum(0, (ks * T + p - T / 2).astype(int))
    c1 = np.minimum(n, (ks * T + p + T / 2).astype(int) + 1)
    cs = np.concatenate([[0.0], np.cumsum(soft)])
    v = (cs[c1] - cs[c0]) / np.maximum(1, c1 - c0)
    return "".join("1" if x > 0 else "0" for x in v)


def tolerant_sync(bits, pat=PATTERN_BITS, max_err=2):
    """Find preamble+sync pattern allowing <= max_err bit errors.

    Returns (start, errors) or None.
    """
    plen = len(pat)
    if len(bits) < plen:
        return None
    arr = np.array([1 if c == "1" else 0 for c in bits], dtype=np.int8)
    pat_arr = np.array([1 if c == "1" else 0 for c in pat], dtype=np.int8)
    win = pat_arr ^ np.lib.stride_tricks.sliding_window_view(arr, plen)
    errs = win.sum(axis=1)
    m = int(np.argmin(errs))
    if errs[m] <= max_err:
        return m, int(errs[m])
    return None


def flip(bits):
    return "".join("1" if c == "0" else "0" for c in bits)


def decode_burst(soft, fs, sym=SYMBOL_S, t_min_us=120.0, t_max_us=123.75,
                 t_step_us=0.25, max_sync_err=1):
    """Produce candidate frame decodes for a burst soft-metric vector.

    The symbol period is estimated by a dense grid over [t_min_us, t_max_us]
    (the XC0432 runs ~122 us, close to the 124 us nominal). Returns a list of
    candidate dicts; the caller keeps those whose MIC passes.
    """
    T_nom = sym * fs
    cands = []
    t_us = t_min_us
    while t_us <= t_max_us:
        T = t_us * 1e-6 * fs
        for p in np.arange(0, T_nom, 1.0):
            bits = _bits(soft, T, p)
            if len(bits) < len(PATTERN_BITS) + MSG_BYTES * 8:
                continue
            for pol in ("as-is", "flip"):
                s = bits if pol == "as-is" else flip(bits)
                hit = tolerant_sync(s, max_err=max_sync_err)
                if not hit:
                    continue
                pos, err = hit
                p0 = pos + len(PATTERN_BITS)
                payload_bits = s[p0:p0 + MSG_BYTES * 8]
                if len(payload_bits) < MSG_BYTES * 8:
                    continue
                msg = bytes(int(payload_bits[i:i + 8], 2)
                            for i in range(0, MSG_BYTES * 8, 8))
                cands.append({
                    "msg": msg,
                    "polarity": pol,
                    "sync_bit": pos,
                    "sync_err_bits": err,
                    "T_samples": T,
                    "phase": float(p),
                })
        t_us += t_step_us
    # dedup by (msg, polarity)
    seen = set()
    deduped = []
    for c in cands:
        key = (c["msg"], c["polarity"])
        if key in seen:
            continue
        seen.add(key)
        deduped.append(c)
    deduped.sort(key=lambda c: (c["sync_err_bits"], c["sync_bit"]))
    return deduped