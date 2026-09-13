"""Tests for automatic frequency correction (AFC) and the offset estimator."""

import os
import unittest

import numpy as np

from reader import dsp
from reader.pipeline import decode_iq, read_cf32, shift_to_virtual_center

REC = "/tmp/gqrx_20260913_004321_917144300_1800000_fc.raw"
REF = bytes.fromhex("a2029fd7087518f9aafa1128122656bbb025")


def _burst_iq(msg, offset_hz=0.0, tone_khz=60.0, sym_us=121.5, fs=1.8e6):
    """Return the raw (no added noise) complex tone burst for one frame."""
    bits = dsp.PATTERN_BITS + "".join(f"{b:08b}" for b in msg)
    sps = int(round(sym_us * 1e-6 * fs))
    tone = tone_khz * 1e3
    freqs = np.array([offset_hz + (tone if b == "1" else -tone)
                       for b in bits for _ in range(sps)])
    phase = 2.0 * np.pi * np.cumsum(freqs) / fs
    return np.exp(1j * phase).astype(np.complex64)


def _iq_with_bursts(msg, placements, tone_khz=60.0, sym_us=121.5,
                     fs=1.8e6, noise_std=0.05, total_s=8.0, seed=42):
    """Build a complex IQ vector containing one or more bursts at specified times.

    `placements` is a list of (offset_hz, start_s) tuples.
    """
    rng = np.random.default_rng(seed)
    n = int(total_s * fs)
    sig = np.zeros(n, dtype=np.complex64)
    for offset, t_s in placements:
        b = _burst_iq(msg, offset_hz=offset, tone_khz=tone_khz,
                       sym_us=sym_us, fs=fs)
        i0 = int(t_s * fs)
        i1 = min(n, i0 + len(b))
        sig[i0:i1] += b[:i1 - i0]
    noise = (rng.standard_normal(n) + 1j * rng.standard_normal(n))
    noise = (noise / np.sqrt(2) * noise_std).astype(np.complex64)
    return (sig + noise).astype(np.complex64)


class TestEstimator(unittest.TestCase):
    def test_zero_offset(self):
        iq = _iq_with_bursts(REF, [(0, 2.0)], total_s=5.0, seed=10, noise_std=0.01)
        d = dsp.estimate_offset(iq, 1.8e6, tone_khz=60.0, search_khz=40.0)
        self.assertIsNotNone(d)
        self.assertAlmostEqual(d, 0.0, delta=150)

    def test_positive_offset_5k(self):
        iq = _iq_with_bursts(REF, [(5000, 2.0)], total_s=5.0, seed=11, noise_std=0.01)
        d = dsp.estimate_offset(iq, 1.8e6, tone_khz=60.0, search_khz=40.0)
        self.assertIsNotNone(d)
        self.assertAlmostEqual(d, 5000.0, delta=150)

    def test_negative_offset_15k(self):
        iq = _iq_with_bursts(REF, [(-15000, 2.0)], total_s=5.0, seed=12, noise_std=0.01)
        d = dsp.estimate_offset(iq, 1.8e6, tone_khz=60.0, search_khz=40.0)
        self.assertIsNotNone(d)
        self.assertAlmostEqual(d, -15000.0, delta=250)

    def test_positive_offset_30k(self):
        iq = _iq_with_bursts(REF, [(30000, 2.0)], total_s=5.0, seed=13, noise_std=0.01)
        d = dsp.estimate_offset(iq, 1.8e6, tone_khz=60.0, search_khz=40.0)
        self.assertIsNotNone(d)
        self.assertAlmostEqual(d, 30000.0, delta=300)

    def test_negative_offset_30k(self):
        iq = _iq_with_bursts(REF, [(-30000, 2.0)], total_s=5.0, seed=14, noise_std=0.01)
        d = dsp.estimate_offset(iq, 1.8e6, tone_khz=60.0, search_khz=40.0)
        self.assertIsNotNone(d)
        self.assertAlmostEqual(d, -30000.0, delta=300)

    def test_no_signal_returns_none(self):
        rng = np.random.default_rng(99)
        n = int(1.8e6 * 2)
        iq = (rng.standard_normal(n) + 1j * rng.standard_normal(n))
        iq = (iq / np.sqrt(2) * 0.01).astype(np.complex64)
        self.assertIsNone(dsp.estimate_offset(iq, 1.8e6))


class TestAfcDecode(unittest.TestCase):
    def test_decode_with_afc_on_18k_offset(self):
        """Bursts at +18k offset: 18 kHz sits on a soft_metric boxcar null
        (~8.96 kHz spacing) so nothing decodes without afc; with afc enabled
        the first segment seeds the corrected offset and later segments
        produce at least one valid frame."""
        iq = _iq_with_bursts(
            REF,
            placements=[(18000, 1.0), (18000, 6.0)],
            total_s=8.0, seed=20, noise_std=0.02,
        )
        recs_no = decode_iq(iq, fs=1.8e6, afc_max_hz=0.0)
        recs_afc = decode_iq(iq, fs=1.8e6, afc_max_hz=40000.0, afc_alpha=1.0)
        self.assertEqual(len(recs_no), 0)
        self.assertGreaterEqual(len(recs_afc), 1)
        for r in recs_afc:
            self.assertTrue(r["mic_ok"])


@unittest.skipUnless(os.path.exists(REC), "recording not present")
class TestRecordingAfc(unittest.TestCase):
    def test_residual_probe_and_decode(self):
        iq = read_cf32(REC)
        shift_hz = 916849300 - 917144300
        shifted = shift_to_virtual_center(iq, 1.8e6, shift_hz)
        d = dsp.estimate_offset(shifted, 1.8e6, search_khz=40.0)
        self.assertIsNotNone(d)
        self.assertAlmostEqual(d, 0.0, delta=1500)
        recs = decode_iq(iq, fs=1.8e6, shift_hz=shift_hz,
                         afc_max_hz=40000.0, afc_alpha=0.5)
        self.assertGreaterEqual(len(recs), 2)
        for r in recs:
            self.assertTrue(r["mic_ok"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
