"""Installer for the Bresser 6-in-1 weewx driver extension.

Installed files land under ``bin/user/bresser6in1/`` and a ``[Bresser6in1]``
configuration section is added to weewx.conf.  Because the hardware has no
data logging, ``[StdArchive] record_generation`` is forced to ``software``
and ``[Station] station_type`` to ``Bresser6in1``.
"""

from weecfg.extension import ExtensionInstaller


class _Installer(ExtensionInstaller):
    def configure(self, engine):
        changed = False
        try:
            if engine.config_dict['Station']['station_type'] != 'Bresser6in1':
                engine.config_dict['Station']['station_type'] = 'Bresser6in1'
                changed = True
        except KeyError:
            engine.config_dict['Station']['station_type'] = 'Bresser6in1'
            changed = True
        try:
            if engine.config_dict['StdArchive']['record_generation'] != 'software':
                engine.config_dict['StdArchive']['record_generation'] = 'software'
                changed = True
        except KeyError:
            engine.config_dict['StdArchive']['record_generation'] = 'software'
            changed = True
        return changed


def loader():
    return _Installer(
        version="1.0.0",
        name="bresser6in1",
        description="WeeWX driver for the Digitech XC0432 wireless weather "
                    "station (Bresser Weather Center 6-in-1 protocol) via "
                    "RTL-SDR.",
        author="weather-reader",
        home_url="https://grotto/weewx/",
        files=[
            ("bin/user", [
                "bin/user/bresser6in1/__init__.py",
                "bin/user/bresser6in1/looppackets.py",
                "bin/user/bresser6in1/sdr.py",
                "bin/user/bresser6in1/dsp.py",
                "bin/user/bresser6in1/frames.py",
                "bin/user/bresser6in1/pipeline.py",
            ]),
        ],
        config={
            "Bresser6in1": {
                "driver": "user.bresser6in1",
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
            },
        },
    )