"""Frame-level decoder for the Bresser Weather Center 6-in-1 protocol.

This is a faithful port of rtl_433's `src/devices/bresser_6in1.c`
(device id 172). The Digitech XC0432 transmits this protocol on ~917 MHz.

Message layout (18 bytes, after the 0xAAAA2DD4 sync):
    msg[0:2]   LFSR-16 digest (gen 0x8810, key 0x5412) over msg[2:16]
    msg[2:6]   32-bit sensor id
    msg[6]     sensor type:4  startup:1  channel:3
    msg[7:9]   wind gust / avg, inverted BCD
    msg[10:11] wind direction, BCD-like
    msg[12:14] temperature (inverted BCD) or rain counter
    msg[14]    humidity (BCD)
    msg[15:16] UV index (inverted BCD) + flags
    msg[17]    additive checksum (sum msg[2:17] + msg[17] & 0xff == 0xff)

Integrity checks: LFSR-16 digest in msg[0:2] and sum to 0xff in msg[17].
"""

LFSR_GEN = 0x8810
LFSR_KEY = 0x5412
MSG_BYTES = 18
CRC_BYTES = 15  # digest spans msg[2:2+15]
CHECKSUM_BYTES = 16  # checksum spans msg[2:2+16] (includes msg[17])

PREAMBLE_HEX = "aaaa2dd4"  # bit pattern searched for by rtl_433
SYNC_BITS = "0010110111010100"  # 0x2dd4


def lfsr_digest16(data: bytes, bytes_, gen=LFSR_GEN, key=LFSR_KEY):
    """16-bit LFSR digest; exact port of rtl_433's lfsr_digest16()."""
    digest = 0
    for k in range(bytes_):
        byte = data[k]
        for i in range(7, -1, -1):
            if (byte >> i) & 1:
                digest ^= key
            if key & 1:
                key = (key >> 1) ^ gen
            else:
                key >>= 1
    return digest


def checksum_ok(msg: bytes) -> bool:
    """Additive checksum: plain sum of msg[2:18] & 0xff == 0xff."""
    return (sum(msg[2:2 + CHECKSUM_BYTES]) & 0xFF) == 0xFF


def msg_valid(msg: bytes) -> tuple:
    """Return (digest_ok, checksum_ok) for an 18-byte message."""
    stored = (msg[0] << 8) | msg[1]
    calc = lfsr_digest16(msg[2:2 + CRC_BYTES], CRC_BYTES)
    return (stored == calc, checksum_ok(msg))


def parse_msg(msg: bytes, max_wind_m_s: float = 60.0,
              dir_offset: float = 0.0) -> dict:
    """Decode an 18-byte message into fields (mirrors bresser_6in1.c).

    max_wind_m_s caps gust/avg; values above it (or with non-BCD digits,
    or a direction outside 0..360) are nulled and the frame flagged
    'suspect' (MIC still passes -- the bytes are what the sensor sent).
    dir_offset (deg, -360..+360) is added to the raw direction to correct
    for vane mounting, wrapped into 0..360.
    """
    if len(msg) < MSG_BYTES:
        raise ValueError("message too short")
    digest_ok, sum_ok = msg_valid(msg)
    out = {
        "mic_ok": digest_ok and sum_ok,
        "digest_ok": digest_ok,
        "checksum_ok": sum_ok,
        "msg": msg.hex(" "),
        "id": "%08x" % int.from_bytes(msg[2:6], "big"),
        "channel": msg[6] & 0x7,
        "startup": (msg[6] >> 3) & 1,
        "sensor_type": msg[6] >> 4,
        "flags": msg[16] & 0x0F,
    }

    # --- temperature / humidity (shared with rain counter) ---
    temp_ok = msg[12] <= 0x99 and (msg[13] & 0xF0) <= 0x90
    temp_raw = (msg[12] >> 4) * 100 + (msg[12] & 0x0F) * 10 + (msg[13] >> 4)
    temp_c = temp_raw * 0.1
    if (msg[13] >> 3) & 1:
        temp_c = (temp_raw - 1000) * 0.1
    humidity = (msg[14] >> 4) * 10 + (msg[14] & 0x0F)

    # --- wind ---
    w = bytes(0xFF ^ x for x in msg[7:10])
    bcd = lambda b: (b >> 4) <= 9 and (b & 0x0F) <= 9
    wind_ok = all(bcd(x) for x in w)          # every nibble a valid BCD digit
    gust_raw = (w[0] >> 4) * 100 + (w[0] & 0x0F) * 10 + (w[1] >> 4)
    avg_raw = (w[2] >> 4) * 100 + (w[2] & 0x0F) * 10 + (w[1] & 0x0F)
    out["wind_ok"] = bool(wind_ok)
    out["wind_gust_m_s"] = gust_raw * 0.1
    out["wind_avg_m_s"] = avg_raw * 0.1
    dir_ok = bcd(msg[10]) and (msg[11] >> 4) <= 9
    out["wind_dir_deg"] = (
        (msg[10] >> 4) * 100 + (msg[10] & 0x0F) * 10 + (msg[11] >> 4))

    # --- rain (only valid when flag set) ---
    r = bytes(0xFF ^ x for x in msg[12:15])
    out["rain_ok"] = msg[16] & 1
    out["rain_mm"] = (
        (r[0] >> 4) * 100000 + (r[0] & 0x0F) * 10000
        + (r[1] >> 4) * 1000 + (r[1] & 0x0F) * 100
        + (r[2] >> 4) * 10 + (r[2] & 0x0F)
    ) * 0.1

    # --- UV (inverted BCD) ---
    uv_ok = ((msg[16] & 0x0F) == 0
             and ((msg[15] ^ 0xFF) & 0xFF) <= 0x99
             and ((msg[16] ^ 0xFF) & 0xF0) <= 0x90)
    out["uv_ok"] = uv_ok
    out["uvi"] = ((((msg[15] ^ 0xFF) >> 4) & 0xF) * 100
                  + ((msg[15] ^ 0xFF) & 0x0F) * 10
                  + (((msg[16] ^ 0xFF) >> 4) & 0xF)) * 0.1

    # --- moisture (soil probe sensor type 4) ---
    moisture_map = [0, 7, 13, 20, 27, 33, 40, 47, 53, 60, 67, 73, 80, 87, 93, 99]
    out["moisture"] = None
    if out["sensor_type"] == 4 and temp_ok and 1 <= humidity <= 16:
        out["moisture"] = moisture_map[humidity - 1]

    # rtl_433 suppresses meaningless fields with DATA_COND
    out["temperature_C"] = temp_c if temp_ok else None
    out["humidity"] = humidity if (temp_ok and out["moisture"] is None) else None
    # strict wind sanity: valid BCD digits, direction in range, and a
    # physically plausible speed cap (catches startup frames from other
    # stations / uninitialized registers that MIC alone cannot reject).
    wind_bad = (not wind_ok
                or not dir_ok
                or out["wind_dir_deg"] > 360
                or gust_raw * 0.1 > max_wind_m_s
                or avg_raw * 0.1 > max_wind_m_s)
    out["suspect"] = bool(wind_bad)
    if wind_bad:
        out["wind_gust_m_s"] = None
        out["wind_avg_m_s"] = None
    if not dir_ok or out["wind_dir_deg"] > 360:
        out["wind_dir_deg"] = None
    elif dir_offset:
        out["wind_dir_deg"] = (out["wind_dir_deg"] + dir_offset) % 360
    if out["sensor_type"] in (2, 4):
        out["wind_gust_m_s"] = None
        out["wind_avg_m_s"] = None
        out["wind_dir_deg"] = None
        out["uv_ok"] = False
        out["uvi"] = None
    if not out["rain_ok"]:
        out["rain_mm"] = None
    # Battery flag (msg[13] bit 1) is only present in non-rain frames; in rain
    # frames msg[13] is a rain-counter digit. Mirrors rtl_433's
    # `DATA_COND, !rain_ok` battery output. bit=1 means battery good.
    if out["rain_ok"]:
        out["battery_ok"] = None
    else:
        out["battery_ok"] = (msg[13] >> 1) & 1
    if not uv_ok:
        out["uvi"] = None
    return out