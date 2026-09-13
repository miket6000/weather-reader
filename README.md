# Digitech XC0432 Weather Station Reader

Decodes the Digitech XC0432 wireless weather station (protocol: Bresser
Weather Center 6-in-1 / "new 5-in-1", ~917 MHz, 2-FSK, 124 us symbols)
from a live RTL-SDR and publishes readings over HTTP/WebSocket, appending
every valid frame to a JSONL log.

The XC0432 transmits every ~12 s, alternating a wind/temp/humidity/UV
message with a rain message. Each 18-byte frame is integrity-checked with
an LFSR-16 digest + additive checksum (the same MICs rtl_433 uses for its
`Bresser-6in1` / device-172 decoder). The battery status (`battery_ok`,
`msg[13]` bit 1, 1 = battery good) is only present in the non-rain frames;
rain frames reuse that bit position for the rain counter, so `battery_ok`
is `null` on rain frames (radiates rtl_433's `DATA_COND, !rain_ok`).

## Layout

    reader/         Python decode worker (DSP + protocol)
      dsp.py        two-tone FSK metric, burst detection, symbol grid decoding
      frames.py     LFSR-16 digest, checksum, field decode (bresser_6in1.c port)
      pipeline.py   burst -> validated JSON records
      cli.py        decode a GQRX CF32 recording
      live.py       RTL-SDR -> continuous decode -> JSONL + stdout
      backfill.py   re-derive historical JSONL through the current decoder
      config.py     config loading (sensor id, wind cap)
    config.json     end-user configuration (see "Configuration" below)
    service/        Node/Express + WebSocket front end
      server.js     /, /status, /current, /history, /series, /stream (WS)
      package.json  express, ws, chart.js (vendored for offline serving)
    log/            readings-YYYYMMDD.jsonl
    tests/          unittest suite

## Requirements

- Python 3.10+ with numpy
- rtl_sdr (the `rtl-sdr` package), an RTL2832U dongle
- Node 18+ (for the service), `npm install` in service/

## Verify with the included recording

    python3 -m reader.cli /tmp/gqrx_20260913_004321_917144300_1800000_fc.raw \
        -c 917144300 -T 916849300

prints two frames (wind + rain), including temperature 12.2 C and humidity 56 %.

## Run live

    # 1. decode worker -> log/ and stdout
    python3 -m reader.live -c 916850000 -g 30

    # 2. HTTP/WebSocket front end
    cd service && npm install && npm start
    # then:  GET http://<host>:8080/          dashboard (current conditions + history)
    #        GET http://<host>:8080/current   latest non-null value per metric
    #        GET http://<host>:8080/status    latest reading
    #        GET http://<host>:8080/history?n=20
    #        GET http://<host>:8080/series?base=minute|hour|day|week   chart data
    #        WS  ws://<host>:8080/stream       live push

The dashboard shows current conditions (latest non-null value for each
metric) and a history chart of average wind, peak gust, rainfall,
temperature, and humidity. The time base is switchable between minute
(raw readings), hour (2-min buckets), day (30-min buckets), and week
(6-h buckets); rainfall is accumulated per bucket from the sensor's
running rain counter.

Configuration for the worker: `-c` receiver center (default 916850000 Hz =
midpoint of the two tones), `-g` tuner gain, `-s` sample rate (1.8 MS/s),
`--block-s`/`--overlap-s` windowing, `-d` JSONL directory.

## Configuration (`config.json`)

`config.json` at the project root holds the end-user settings both `live.py`
and `cli.py` read:

    {
        "sensor_id": "9fd70875",
        "max_wind_m_s": 60.0,
        "dir_offset": 0.0
    }

- `sensor_id` — only frames from this transmitter are logged. Set it to your
  station's id (the digest/id printed on every reading) to ignore other
  Bresser stations on the same frequency. An empty string disables filtering.
- `max_wind_m_s` — a physical plausibility cap for wind. Gusts/averages above
  this (or with non-BCD digits, or a direction outside 0-360) are nulled and
  flagged `"suspect": true`. This catches startup frames whose wind register
  is uninitialized; MIC alone cannot reject them because the bytes really
  were transmitted.
- `dir_offset` — wind-direction correction in degrees (-360 to +360), added
  to every reading and wrapped into 0..360. Use it to compensate for a wind
  vane that isn't mounted with its "N" mark pointing exactly north.

Precedence is CLI flag > config file > built-in default. `live.py` and
`cli.py` accept `--sensor-id` (empty string = accept any), `--max-wind`,
and `--dir-offset` to override, plus `--config PATH` to point at a different
file (otherwise `$WEATHER_CONFIG` or `./config.json`). The built-in default
sensor id is the developer's own unit, so change `config.json` before first
run.

To apply new decode rules to history (e.g. after tightening the wind cap or
setting `dir_offset`), re-derive the log in place (a `.bak` is written
first):

    python3 -m reader.backfill --log-dir log

For the service: env vars `WEATHER_LOG_DIR`, `PORT`, `POLL_MS`.

## Tests

    python3 -m unittest tests.test_decode

Coverage: reference message MIC + fields, bit-flip rejection, LFSR/digest
port, sync-pattern rejection of garbage, noise/silence rejection, and both
frames from the recorded file (timing ~12 s apart, fields match rtl_433).

## Deployment (Debian server / Pi)

The units in `deploy/` target a FHS-style layout:

    /opt/weather-reader                code (root-owned, read-only)
    /etc/weather-reader/config.json    settings (edit: sensor_id, max_wind_m_s, dir_offset)
    /var/lib/weather-reader/log        readings-YYYYMMDD.jsonl
    systemd: weather-reader, weather-http  (run as unprivileged user `weather`)

On the target server (dongle plugged in, RTL-SDR already working):

    sudo git clone <your-private-repo> /opt/weather-reader
    sudo /opt/weather-reader/deploy/install.sh

`deploy/install.sh` is idempotent: installs apt deps (python3-numpy, nodejs,
npm, rtl-sdr), blacklists the DVB-T driver, creates the `weather` user and
log dir, seeds `/etc/weather-reader/config.json` if absent, runs
`npm ci`, installs a udev rule for the RTL2832U, enables the two systemd
units, and adds a logrotate snippet.

Updates later:

    cd /opt/weather-reader && sudo git pull && sudo ./deploy/install.sh