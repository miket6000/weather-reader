"""WeeWX driver for the Digitech XC0432 (Bresser Weather Center 6-in-1).

The station is a one-way ~917 MHz transmitter with no data logging: it
broadcasts an 18-byte temperature/humidity/wind frame and an 18-byte rain
counter frame every ~12 seconds.  Because there is no onboard archive,
WeeWX must generate archive records in software, so ``[StdArchive]
record_generation`` should be ``software`` (the extension installer sets
this for you).

An internal decoder thread owns the rtl_sdr subprocess.  It performs
automatic frequency correction for receiver/transmitter crystal drift,
checks the LFSR-16 digest + additive checksum, filters on ``sensor_id``,
and hands validated frames to ``genLoopPackets()`` through a thread-safe
queue.  Decode state (AFC, dedupe table, rtl_sdr subprocess, rain
baseline) lives on this driver, so it survives WeeWX's archive-period
BreakLoop, which re-creates the packet generator but not the driver.

Observations, all in US units (weewx converts to the configured target
unit): temperature and humidity on the wind frame; wind speed/gust and
gust direction on the wind frame; the accumulated rainfall *since the
previous* rain frame on the rain frame; and a battery status on the wind
frame (0 = good, 1 = low).  This station reports no UV or solar.

Configuration (``[Bresser6in1]``)::

    [Bresser6in1]
        # The weewx driver to use.
        driver = user.bresser6in1

        # Decode only frames from this transmitter (hex sensor id).
        sensor_id = 9fd70875

        # Wind gust/avg cap in m/s.  MIC-valid frames with implausibly
        # large values (usually uninitialized registers at sensor startup)
        # have their wind fields nulled.
        max_wind_m_s = 60.0

        # Wind vane mounting correction in degrees, -360..+360.
        dir_offset = 0.0

        # Hardware calibration: 1 raw reading count = this many units.
        wind_mps_per_count = 0.1
        rain_mm_per_count = 0.04

        # RTL-SDR tuning.
        center = 916850000
        gain = 30.0          # dB; 0 = auto

        # Automatic frequency correction.
        afc_enable = 1
        afc_max_hz = 40000.0
        afc_alpha = 0.2
"""

import logging
import queue
import time

import weewx
import weewx.drivers

from . import looppackets
from .sdr import SdrDecoder

DRIVER_NAME = "Bresser6in1"
DRIVER_VERSION = "1.0.0"

# Defaults used when a key is missing from the configuration stanza.
DEFAULTS = {
    "sensor_id": "9fd70875",
    "max_wind_m_s": 60.0,
    "dir_offset": 0.0,
    "wind_mps_per_count": 0.1,
    "rain_mm_per_count": 0.04,
    "center": 916850000,
    "gain": 30.0,
    "afc_enable": True,
    "afc_max_hz": 40000.0,
    "afc_alpha": 0.2,
}

log = logging.getLogger(__name__)


def _to_bool(value, default):
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def loader(config_dict, engine):
    """Factory required by the weewx driver interface."""
    return Bresser6in1Driver(**config_dict[DRIVER_NAME])


class Bresser6in1Driver(weewx.drivers.AbstractDevice):
    """Decodes the XC0432 station from RTL-SDR into weewx loop packets."""

    def __init__(self, **config):
        super().__init__()
        settings = dict(DEFAULTS)
        settings.update({k: v for k, v in config.items() if v is not None})
        self._queue = queue.SimpleQueue()
        self._prev_rain_mm = None
        self._decoder = SdrDecoder(
            center=int(settings["center"]),
            gain=float(settings["gain"]),
            sensor_id=str(settings["sensor_id"] or ""),
            max_wind_m_s=float(settings["max_wind_m_s"]),
            dir_offset=float(settings["dir_offset"]),
            wind_mps_per_count=float(settings["wind_mps_per_count"]),
            rain_mm_per_count=float(settings["rain_mm_per_count"]),
            afc_enable=_to_bool(settings["afc_enable"], True),
            afc_max_hz=float(settings["afc_max_hz"]),
            afc_alpha=float(settings["afc_alpha"]),
            sink=self._queue.put,
            logger=log,
        )
        self._decoder.start()

    @property
    def hardware_name(self):
        return "Digitech XC0432 (Bresser 6-in-1) via RTL-SDR"

    @property
    def archive_interval(self):
        return 300

    def getTime(self):
        return time.time()

    def closePort(self):
        self._decoder.stop()

    def genLoopPackets(self):
        """Yield weewx loop packets for each validated frame.

        The engine re-creates this generator around every archive-period
        BreakLoop, so all decode state (the thread, AFC, the rain baseline)
        lives on the driver, not the generator.
        """
        while True:
            frame = self._queue.get()
            packets, self._prev_rain_mm = looppackets.frame_to_loop_packets(
                frame, self._prev_rain_mm)
            for packet in packets:
                packet["dateTime"] = time.time()
                packet["usUnits"] = weewx.US
                yield packet