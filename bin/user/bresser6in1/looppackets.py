"""Map validated XC0432 frame records to weewx loop packets (US units).

Kept free of weewx imports so the mapping can be unit-tested standalone.
The two transmissions of the 6-in-1 station become partial loop packets:

* wind/temperature frame -> outTemp, outHumidity, windSpeed, windGust,
  windDir, windGustDir, outTempBatteryStatus
* rain frame -> rain (the measured rainfall *since the previous* rain
  frame, i.e. the delta, not the lifetime counter)

`dateTime` is left for the caller; `usUnits` is set to 0x01 (weewx.US).
"""

US = 0x01  # weewx.US

C_TO_F_SCALE = 9.0 / 5.0
C_TO_F_OFFSET = 32.0
MPS_TO_MPH = 2.2369362920544025
MM_TO_IN = 1.0 / 25.4
RAIN_MAX_DELTA_MM = 200.0

__all__ = [
    "US",
    "c_to_f",
    "mps_to_mph",
    "mm_to_in",
    "battery_ok_to_status",
    "frame_to_loop_packets",
]


def c_to_f(value):
    return value * C_TO_F_SCALE + C_TO_F_OFFSET


def mps_to_mph(value):
    return value * MPS_TO_MPH


def mm_to_in(value):
    return value * MM_TO_IN


def battery_ok_to_status(battery_ok):
    """Invert the sensor's battery flag for weewx's 0 = good convention.

    The station reports bit=1 for a good battery; weewx battery status
    fields use 0 = good, 1 = low.  None (rain frames carry no flag) is
    passed through.
    """
    if battery_ok is None:
        return None
    return 0 if battery_ok else 1


def frame_to_loop_packets(frame, prev_rain_mm=None):
    """Map one decoded frame record to (list-of-packets, next_rain_mm).

    A wind frame yields one packet with temperature/humidity/wind fields.
    A rain frame yields one packet whose `rain` is the amount that fell
    since the previous rain frame (the first rain frame after startup only
    establishes the baseline, so it contributes a 0).

    The returned list is empty only if a rain frame had no usable counter
    (rain_mm is None).  ``next_rain_mm`` is the counter value to pass back
    in on the next call, or unchanged when the frame was not a rain frame.

    A rain counter that jumps backwards (sensor reset / counter overflow)
    beyond a plausibility cap is re-baselined rather than reported.
    """
    if frame.get("rain_ok"):
        cur_rain_mm = frame["rain_mm"]
        if cur_rain_mm is None:
            return [], prev_rain_mm
        if prev_rain_mm is None:
            delta_mm = 0.0
        elif 0.0 <= cur_rain_mm - prev_rain_mm <= RAIN_MAX_DELTA_MM:
            delta_mm = cur_rain_mm - prev_rain_mm
        else:
            delta_mm = 0.0
        packet = {"usUnits": US, "rain": round(mm_to_in(delta_mm), 2)}
        return [packet], cur_rain_mm

    packet = {"usUnits": US}
    packet["outTempBatteryStatus"] = battery_ok_to_status(frame.get("battery_ok"))
    temp = frame.get("temperature_C")
    if temp is not None:
        packet["outTemp"] = round(c_to_f(temp), 2)
    humidity = frame.get("humidity")
    if humidity is not None:
        packet["outHumidity"] = int(humidity)
    gust = frame.get("wind_gust_m_s")
    if gust is not None:
        packet["windGust"] = round(mps_to_mph(gust), 2)
    avg = frame.get("wind_avg_m_s")
    if avg is not None:
        packet["windSpeed"] = round(mps_to_mph(avg), 2)
    wdir = frame.get("wind_dir_deg")
    if wdir is not None:
        packet["windDir"] = float(wdir)
        packet["windGustDir"] = float(wdir)
    return [packet], prev_rain_mm