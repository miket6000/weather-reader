import sys
import types
import unittest

# Mock weewx for testing
weewx = types.ModuleType('weewx')
weewx.US = 0x01
weewx.drivers = types.ModuleType('weewx.drivers')
class AbstractDevice:
    def __init__(self):
        pass
weewx.drivers.AbstractDevice = AbstractDevice
sys.modules['weewx'] = weewx
sys.modules['weewx.drivers'] = weewx.drivers

from bin.user.bresser6in1 import looppackets


class TestLooppackets(unittest.TestCase):
    def test_wind_frame_mapping(self):
        frame = {
            "rain_ok": False,
            "battery_ok": True,
            "temperature_C": 12.2,
            "humidity": 56,
            "wind_gust_m_s": 3.0,
            "wind_avg_m_s": 1.5,
            "wind_dir_deg": 180,
        }
        packets, next_rain = looppackets.frame_to_loop_packets(frame, None)
        self.assertEqual(next_rain, None)
        self.assertEqual(len(packets), 1)
        p = packets[0]
        self.assertEqual(p["usUnits"], looppackets.US)
        self.assertAlmostEqual(p["outTemp"], round(looppackets.c_to_f(12.2), 2))
        self.assertEqual(p["outHumidity"], 56)
        self.assertAlmostEqual(p["windSpeed"], round(looppackets.mps_to_mph(1.5), 2))
        self.assertAlmostEqual(p["windGust"], round(looppackets.mps_to_mph(3.0), 2))
        self.assertEqual(p["windDir"], 180.0)
        self.assertEqual(p["windGustDir"], 180.0)
        self.assertEqual(p["outTempBatteryStatus"], 0)

    def test_battery_low(self):
        frame = {"rain_ok": False, "battery_ok": False}
        packets, _ = looppackets.frame_to_loop_packets(frame, None)
        self.assertEqual(packets[0]["outTempBatteryStatus"], 1)

    def test_rain_delta_first_frame_baseline(self):
        frame = {"rain_ok": True, "rain_mm": 100.0}
        packets, next_rain = looppackets.frame_to_loop_packets(frame, None)
        self.assertEqual(len(packets), 1)
        self.assertAlmostEqual(packets[0]["rain"], 0.0)
        self.assertEqual(next_rain, 100.0)

    def test_rain_delta(self):
        frame = {"rain_ok": True, "rain_mm": 100.04}
        packets, next_rain = looppackets.frame_to_loop_packets(frame, 100.0)
        self.assertEqual(len(packets), 1)
        self.assertAlmostEqual(packets[0]["rain"], round(looppackets.mm_to_in(0.04), 2))
        self.assertEqual(next_rain, 100.04)

    def test_rain_delta_reset_rebaselines(self):
        frame = {"rain_ok": True, "rain_mm": 10.0}
        packets, next_rain = looppackets.frame_to_loop_packets(frame, 100.0)
        self.assertEqual(len(packets), 1)
        self.assertAlmostEqual(packets[0]["rain"], 0.0)
        self.assertEqual(next_rain, 10.0)

    def test_rain_delta_none(self):
        frame = {"rain_ok": True, "rain_mm": None}
        packets, next_rain = looppackets.frame_to_loop_packets(frame, 50.0)
        self.assertEqual(packets, [])
        self.assertEqual(next_rain, 50.0)


if __name__ == "__main__":
    unittest.main()
