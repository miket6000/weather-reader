import json
import os
import tempfile
import unittest

import numpy as np

from reader import dsp
from reader.config import apply_cli_overrides, load_config
from reader.frames import checksum_ok, lfsr_digest16, msg_valid, parse_msg
from reader.pipeline import decode_iq, read_cf32

REC = "/tmp/gqrx_20260913_004321_917144300_1800000_fc.raw"
CENTER = 917144300.0
VMID = 916849300.0  # midpoint of the two tones
FS = 1.8e6

REF = bytes.fromhex("a2029fd7087518f9aafa1128122656bbb025")
REF_RAIN = bytes.fromhex("47729fd7087518f7ecf92488ffbd9bff0115")
# A MIC-valid startup frame from a *different* station (4feb843a, ch 4,
# type 8): wind bytes 7e b3 fe invert to 81 4c 01 -> gust 81.4 m/s with a
# non-BCD nibble, avg 2.2 m/s, direction 912 deg (uninitialized registers).
REF_WRONG_SENSOR = bytes.fromhex("10b04feb843a8c7eb3fe8ac40933315dd85c")


class TestFrames(unittest.TestCase):
    def test_reference_msg_mic_and_fields(self):
        digest_ok, sum_ok = msg_valid(REF)
        self.assertTrue(digest_ok)
        self.assertTrue(sum_ok)
        f = parse_msg(REF)
        self.assertEqual(f["id"], "9fd70875")
        self.assertEqual(f["channel"], 0)
        self.assertEqual(f["sensor_type"], 1)
        self.assertAlmostEqual(f["temperature_C"], 12.2)
        self.assertEqual(f["humidity"], 56)
        self.assertAlmostEqual(f["wind_gust_m_s"], 6.5)
        self.assertAlmostEqual(f["wind_avg_m_s"], 5.5)
        self.assertEqual(f["wind_dir_deg"], 112)
        self.assertAlmostEqual(f["uvi"], 44.4)
        self.assertEqual(f["battery_ok"], 1)  # bit1 of msg[13]: 1 = battery good
        self.assertFalse(f["suspect"])

    def test_reference_rain_msg_fields(self):
        digest_ok, sum_ok = msg_valid(REF_RAIN)
        self.assertTrue(digest_ok)
        self.assertTrue(sum_ok)
        f = parse_msg(REF_RAIN)
        self.assertTrue(f["rain_ok"])
        self.assertAlmostEqual(f["rain_mm"], 426.4)
        self.assertIsNone(f["temperature_C"])
        self.assertIsNone(f["humidity"])
        self.assertIsNone(f["battery_ok"])  # rain frames carry no battery flag

    def test_battery_ok_null_on_rain_frames(self):
        for msg in (REF, REF_RAIN):
            f = parse_msg(msg)
            if f["rain_ok"]:
                self.assertIsNone(f["battery_ok"])
            else:
                self.assertIn(f["battery_ok"], (0, 1))

    def test_bit_flip_fails_mic(self):
        buf = bytearray(REF)
        for bit in (0, 7, 40, 71, 120):
            b = bytearray(REF)
            b[bit // 8] ^= 1 << (7 - bit % 8)
            digest_ok, sum_ok = msg_valid(bytes(b))
            self.assertFalse(digest_ok and sum_ok,
                             "MIC should fail when payload bit flipped")

    def test_checksum_is_plain_sum(self):
        self.assertTrue(checksum_ok(REF))
        self.assertEqual(sum(REF[2:18]) & 0xFF, 0xFF)

    def test_impossible_gust_is_rejected_but_mic_still_passes(self):
        digest_ok, sum_ok = msg_valid(REF_WRONG_SENSOR)
        self.assertTrue(digest_ok and sum_ok)
        f = parse_msg(REF_WRONG_SENSOR)
        self.assertIsNone(f["wind_gust_m_s"])   # 81.4 m/s dropped by the cap
        self.assertIsNone(f["wind_avg_m_s"])    # built on a non-BCD nibble
        self.assertIsNone(f["wind_dir_deg"])    # 912 deg out of range
        self.assertTrue(f["suspect"])

    def test_wind_cap_is_configurable(self):
        f = parse_msg(REF, max_wind_m_s=5.0)   # gust 6.5 > cap
        self.assertIsNone(f["wind_gust_m_s"])
        self.assertTrue(f["suspect"])
        f = parse_msg(REF, max_wind_m_s=10.0)  # gust 6.5 within cap
        self.assertAlmostEqual(f["wind_gust_m_s"], 6.5)
        self.assertFalse(f["suspect"])

    def test_unknown_sensor_dropped_by_sensor_id(self):
        self.assertEqual(parse_msg(REF_WRONG_SENSOR)["id"], "4feb843a")

    def test_dir_offset_applied_and_wrapped(self):
        f = parse_msg(REF, dir_offset=-45.0)   # raw 112 - 45 = 67
        self.assertAlmostEqual(f["wind_dir_deg"], 67.0)
        f = parse_msg(REF, dir_offset=350.0)   # 112 + 350 = 462 -> 102
        self.assertAlmostEqual(f["wind_dir_deg"], 102.0)
        f = parse_msg(REF, dir_offset=0.0)     # default: raw preserved
        self.assertEqual(f["wind_dir_deg"], 112)

    def test_dir_offset_does_not_rescue_invalid_direction(self):
        f = parse_msg(REF_WRONG_SENSOR, dir_offset=-45.0)  # raw 912 is invalid
        self.assertIsNone(f["wind_dir_deg"])

    def test_dir_offset_applied_in_records(self):
        from reader.pipeline import _records_for

        cand = {"msg": REF, "polarity": 0, "sync_bit": 0,
                "sync_err_bits": 0, "T_samples": 124.0}
        recs = _records_for([cand], 0.0, 1e6, dir_offset=-45.0)
        self.assertAlmostEqual(recs[0]["wind_dir_deg"], 67.0)


class TestDsp(unittest.TestCase):
    def test_lfsr_matches_reference(self):
        self.assertEqual(lfsr_digest16(REF[2:17], 15), 0xA202)

    def test_sync_pattern_constants(self):
        self.assertEqual(len(dsp.PATTERN_BITS), 32)
        self.assertEqual(dsp.PATTERN_BITS, "10101010101010100010110111010100")

    def test_tolerant_sync_rejects_garbage(self):
        bits = "0" * 1000
        # 32-bit pattern can not appear with <=1 error in '0'*1000
        hit_ok = dsp.tolerant_sync(bits, max_err=1)
        self.assertIsNone(hit_ok)

    def test_noise_gives_no_frames(self):
        rng = np.random.default_rng(7)
        iq = (rng.standard_normal(int(FS) * 3)).astype(np.float32)
        iq = iq.view(np.complex64)
        records = decode_iq(iq, fs=FS)
        self.assertEqual(records, [])

    def test_silence_gives_no_frames(self):
        iq = np.zeros(int(FS * 3), dtype=np.complex64)
        self.assertEqual(decode_iq(iq, fs=FS), [])


class TestConfig(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)

    def _write(self, obj):
        p = os.path.join(self._tmp.name, "config.json")
        with open(p, "w") as f:
            json.dump(obj, f)
        return p

    def test_defaults_without_explicit_config(self):
        cfg = load_config(path=None)
        self.assertEqual(cfg["sensor_id"], "9fd70875")
        self.assertEqual(cfg["max_wind_m_s"], 60.0)
        self.assertEqual(cfg["dir_offset"], 0.0)

    def test_file_overrides_only_what_it_sets(self):
        p = self._write({"sensor_id": "deadbeef"})
        cfg = load_config(path=p)
        self.assertEqual(cfg["sensor_id"], "deadbeef")
        self.assertEqual(cfg["max_wind_m_s"], 60.0)  # untouched default
        self.assertEqual(cfg["dir_offset"], 0.0)

    def test_dir_offset_from_file_and_cli(self):
        p = self._write({"dir_offset": -45.0})
        self.assertEqual(load_config(path=p)["dir_offset"], -45.0)
        cfg = load_config(path=p)
        apply_cli_overrides(cfg, _Args(sensor_id=None, max_wind=None,
                                        dir_offset=350.0))
        self.assertEqual(cfg["dir_offset"], 350.0)

    def test_dir_offset_out_of_range_rejected(self):
        p = self._write({"dir_offset": 361.0})
        with self.assertRaises(SystemExit):
            load_config(path=p)
        cfg = load_config(path=None)
        with self.assertRaises(SystemExit):
            apply_cli_overrides(cfg, _Args(sensor_id=None, max_wind=None,
                                           dir_offset=-361.0))

    def test_cli_overrides_file(self):
        p = self._write({"sensor_id": "deadbeef", "max_wind_m_s": 30.0})
        cfg = load_config(path=p)
        apply_cli_overrides(cfg, _Args(sensor_id="cafebabe", max_wind=15.0,
                                       dir_offset=0.0))
        self.assertEqual(cfg["sensor_id"], "cafebabe")
        self.assertEqual(cfg["max_wind_m_s"], 15.0)

    def test_empty_sensor_id_disables_filtering(self):
        cfg = load_config(path=None)
        apply_cli_overrides(cfg, _Args(sensor_id="", max_wind=None,
                                       dir_offset=0.0))
        self.assertEqual(cfg["sensor_id"], "")


class _Args:
    def __init__(self, sensor_id, max_wind, dir_offset):
        self.sensor_id = sensor_id
        self.max_wind = max_wind
        self.dir_offset = dir_offset


@unittest.skipUnless(os.path.exists(REC), "recording not present")
class TestRecording(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.iq = read_cf32(REC)

    def test_recording_decodes_two_frames(self):
        shift = VMID - CENTER
        records = decode_iq(self.iq, fs=FS, shift_hz=shift)
        self.assertEqual(len(records), 2)
        for r in records:
            self.assertTrue(r["mic_ok"])
            self.assertEqual(r["model"], "Bresser-6in1")
            self.assertEqual(r["id"], "9fd70875")

    def test_recording_sensor_id_filter(self):
        shift = VMID - CENTER
        self.assertEqual(len(decode_iq(self.iq, fs=FS, shift_hz=shift,
                                       sensor_id="9fd70875")), 2)
        self.assertEqual(decode_iq(self.iq, fs=FS, shift_hz=shift,
                                   sensor_id="4feb843a"), [])

    def test_recording_frame_timing_and_fields(self):
        shift = VMID - CENTER
        recs = decode_iq(self.iq, fs=FS, shift_hz=shift)
        starts = [r["burst_start_s"] for r in recs]
        self.assertLess(abs(starts[1] - starts[0] - 12.0), 0.2)
        first = recs[0]
        self.assertAlmostEqual(first["temperature_C"], 12.2)
        self.assertEqual(first["humidity"], 56)
        self.assertAlmostEqual(first["wind_gust_m_s"], 6.5)
        self.assertAlmostEqual(first["wind_avg_m_s"], 5.5)
        self.assertEqual(first["wind_dir_deg"], 112)
        self.assertAlmostEqual(first["uvi"], 44.4)
        for r in recs:
            if r["rain_ok"]:
                self.assertIsNone(r["battery_ok"])
            else:
                self.assertIn(r["battery_ok"], (0, 1))


if __name__ == "__main__":
    unittest.main(verbosity=2)