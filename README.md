# WeeWX Bresser 6-in-1 (XC0432) RTL-SDR Driver

WeeWX driver for the Digitech XC0432 / Bresser Weather Center 6-in-1 wireless weather station, decoded from a live RTL-SDR (no serial/USB weather station interface required).

## Features

- Decodes wind/temp/humidity/rain frames transmitted ~every 12s on ~917 MHz (2-FSK)
- Integrates directly into WeeWX as a driver (no external JSONL logger/UI required)
- Automatic Frequency Correction (AFC) for RTL-SDR/transmitter drift
- Sensor ID filtering to ignore nearby stations
- Configurable wind plausibility cap and wind vane direction offset
- Configurable calibration scales for wind and rain counters
- Software archive generation (no onboard station logging)

## Requirements

- [WeeWX](http://www.weewx.com/) 4.x
- RTL-SDR dongle (RTL2832U)
- System packages: `rtl-sdr`
- Python packages: `numpy` (WeeWX environment)
- This station type: Digitech XC0432 / Bresser 6-in-1

## Installation

### 1) Install system dependencies (Debian/Ubuntu/RPi)

```bash
sudo apt install rtl-sdr python3-numpy
```

Blacklist DVB-T drivers if needed (common on some distros):

```bash
echo -e "blacklist dvb_usb_rtl28xxu\nblacklist rtl2832\nblacklist rtl2830" | sudo tee /etc/modprobe.d/blacklist-rtl.conf
sudo modprobe -r dvb_usb_rtl28xxu 2>/dev/null || true
```

### 2) Install the WeeWX extension

From the WeeWX host, run:

```bash
weectl extension install /path/to/this-repo
```

The installer will:
- Copy driver files to `bin/user/bresser6in1/`
- Add a `[Bresser6in1]` section to `weewx.conf`
- Set `Station.station_type = Bresser6in1`
- Set `StdArchive.record_generation = software` (required because the station has no onboard archive)

Restart WeeWX after installation:

```bash
sudo systemctl restart weewx
```

## Configuration

Edit `[Bresser6in1]` in `weewx.conf`:

```ini
[Bresser6in1]
    driver = user.bresser6in1
    # Only decode frames from this transmitter (hex). Find it from logs.
    sensor_id = 9fd70875
    # Max plausible wind speed (m/s). Implausible values are nulled.
    max_wind_m_s = 60.0
    # Wind vane mounting correction in degrees (-360..+360)
    dir_offset = 0.0
    # Hardware calibration: counts -> units
    wind_mps_per_count = 0.1
    rain_mm_per_count = 0.04
    # RTL-SDR tuning
    center = 916850000
    gain = 30.0          # dB; 0 = auto
    # Automatic frequency correction
    afc_enable = 1
    afc_max_hz = 40000.0
    afc_alpha = 0.2
```

Notes:
- `sensor_id` is printed in WeeWX logs when frames are received; set it to ignore other nearby Bresser stations. Set to empty to accept all.
- `center` is typically near 916.85 MHz for this station (midpoint of the two FSK tones). Try 916850000 or 917144300 depending on your setup/recordings.
- `gain` of 30 dB is a reasonable starting point for many RTL-SDRs.
- AFC helps compensate for crystal drift; increase `afc_max_hz` if drift is larger, lower if it chases noise.
- Since there is no onboard archive, `record_generation` must be `software` (installer enforces this).

## How it works

1. `SdrDecoder` spawns `rtl_sdr`, streams IQ, applies AFC, detects bursts, demodulates and validates frames (LFSR-16 digest + additive checksum).
2. Valid frames are passed to the WeeWX driver via a queue; `looppackets.frame_to_loop_packets()` maps them to WeeWX loop packets (US units).
3. Wind/temp/humidity come from the wind frame; rain comes as deltas between rain counter readings.
4. Battery status is reported on wind frames (0 = good, 1 = low in WeeWX convention).

## Troubleshooting

- **No frames in logs**: Check dongle permissions/udev, ensure DVB-T drivers are blacklisted, verify frequency/center/gain, and watch for `[Bresser6in1]` log messages from the driver.
- **Wrong station decoded**: Set `sensor_id` to your station's ID.
- **Wind/rain values look off scale**: Adjust `wind_mps_per_count` / `rain_mm_per_count` if your hardware variant differs (defaults match XC0432).
- **Frequent drift/no decodes**: Enable/tune AFC or try different `center`.
- **WeeWX not archiving**: Confirm `record_generation = software` in `[StdArchive]`.

## Files

- `bin/user/bresser6in1/` - driver package (`__init__.py`, `sdr.py`, `pipeline.py`, `frames.py`, `dsp.py`, `looppackets.py`)
- `install.py` - WeeWX extension installer
- `config/` - example configuration snippets
- `new_belchertown.py` - optional Belchertown skin extension (if bundled)

## License

See repository for license information.
