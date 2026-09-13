"""Configuration for the reader.

Precedence (high to low):
    1. command-line flags (--sensor-id, --max-wind)
    2. config file (--config PATH, else $WEATHER_CONFIG, else ./config.json)
    3. built-in defaults below

The config file is plain JSON, e.g.:

    {
        "sensor_id": "9fd70875",
        "max_wind_m_s": 60.0,
        "dir_offset": 0.0
    }

`sensor_id` limits decoding to that transmitter (empty string disables the
filter). `max_wind_m_s` rejects gusts/averages that are physically
implausible (values above the cap are dropped and the frame marked suspect).
`dir_offset` is a wind-direction correction in degrees, added to every
reading (result wrapped to 0..360) to compensate for physically offset
mounting of the wind vane; allowed range is -360 to +360.
"""

import json
import os
import sys

DEFAULTS = {
    "sensor_id": "9fd70875",
    "max_wind_m_s": 60.0,
    "dir_offset": 0.0,
}


def default_config_path():
    """Project-root config.json (the directory above the reader/ package)."""
    return os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "config.json",
    )


def load_config(path=None):
    """Return merged config dict. Missing file -> defaults (silent for the
    default location, warns if an explicit path was requested)."""
    explicit = bool(path or os.environ.get("WEATHER_CONFIG"))
    p = path or os.environ.get("WEATHER_CONFIG") or default_config_path()
    cfg = dict(DEFAULTS)
    if os.path.exists(p):
        try:
            with open(p) as f:
                extra = json.load(f)
        except (OSError, ValueError) as exc:
            raise SystemExit(f"config error in {p}: {exc}")
        if not isinstance(extra, dict):
            raise SystemExit(f"config error in {p}: expected a JSON object")
        cfg.update(extra)
    elif explicit:
        print(f"[reader] config not found: {p}; using defaults",
              file=sys.stderr)
    cfg["dir_offset"] = _validate_offset(cfg["dir_offset"])
    return cfg


def _validate_offset(value):
    try:
        value = float(value)
    except (TypeError, ValueError):
        raise SystemExit(f"config error: dir_offset must be a number, got {value!r}")
    if not -360.0 <= value <= 360.0:
        raise SystemExit(
            f"config error: dir_offset {value} outside -360..+360")
    return value


def apply_cli_overrides(config, args):
    """Fold explicit CLI values over the loaded config.

    Pass args only for flags that exist; None values are ignored.
    An empty string for sensor_id explicitly disables filtering.
    """
    sensor_id = getattr(args, "sensor_id", None)
    if sensor_id is not None and sensor_id != "":
        config["sensor_id"] = sensor_id
    elif sensor_id == "":
        config["sensor_id"] = ""
    max_wind = getattr(args, "max_wind", None)
    if max_wind is not None:
        config["max_wind_m_s"] = float(max_wind)
    dir_offset = getattr(args, "dir_offset", None)
    if dir_offset is not None:
        config["dir_offset"] = _validate_offset(dir_offset)
    return config