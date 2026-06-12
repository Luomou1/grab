from __future__ import annotations

from decimal import Decimal, ROUND_FLOOR

from grab_app.config import PZT_ADDRESS, PZT_MAX_UM, PZT_MIN_UM


def xor8(data: bytes | bytearray) -> int:
    checksum = 0
    for value in data:
        checksum ^= int(value)
    return checksum & 0xFF


def encode_value(value: float) -> bytes:
    """复刻原 MATLAB float2Bytes：整数两字节 + 小数*10000 两字节，首字节 bit7 为符号位。"""
    sign = value < 0
    decimal_value = abs(Decimal(str(value)))
    integer = int(decimal_value.to_integral_value(rounding=ROUND_FLOOR))
    fraction = int(((decimal_value - Decimal(integer)) * Decimal(10000)).to_integral_value(rounding=ROUND_FLOOR))

    b0 = (integer // 256) & 0xFF
    b1 = integer % 256
    b2 = (fraction // 256) & 0xFF
    b3 = fraction % 256
    if sign:
        b0 |= 0x80
    return bytes((b0, b1, b2, b3))


def decode_value(b0: int, b1: int, b2: int, b3: int) -> float:
    sign = -1.0 if (b0 & 0x80) else 1.0
    b0 = b0 & 0x7F
    integer = b0 * 256 + b1
    fraction = (b2 * 256 + b3) * 0.0001
    return sign * (integer + fraction)


def append_checksum(payload: bytes) -> bytes:
    return payload + bytes((xor8(payload),))


def clamp_move_um(move_um: float) -> float:
    if move_um < PZT_MIN_UM:
        return PZT_MIN_UM
    if move_um > PZT_MAX_UM:
        return PZT_MAX_UM
    return float(move_um)


def build_send_move_packet(channel: int, move_um: float) -> bytes:
    move_um = clamp_move_um(move_um)
    payload = bytes((0xAA, PZT_ADDRESS, 0x0B, 0x01, 0x00, channel & 0xFF)) + encode_value(move_um)
    return append_checksum(payload)


def build_read_move_packet(channel: int) -> bytes:
    payload = bytes((0xAA, PZT_ADDRESS, 0x07, 0x06, 0x00, channel & 0xFF))
    return append_checksum(payload)


def build_closed_loop_packet(channel: int) -> bytes:
    payload = bytes((0xAA, PZT_ADDRESS, 0x08, 0x12, 0x00, channel & 0xFF, 0x43))
    return append_checksum(payload)


def parse_read_move_response(data: bytes | bytearray) -> float | None:
    if len(data) < 11:
        return None
    frame = bytes(data[:11])
    if frame[0] != 0xAA:
        return None
    if xor8(frame[:10]) != frame[10]:
        return None
    return decode_value(frame[6], frame[7], frame[8], frame[9])
