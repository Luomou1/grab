from grab_app.pzt.protocol import (
    build_closed_loop_packet,
    build_read_move_packet,
    build_send_move_packet,
    decode_value,
    encode_value,
    parse_read_move_response,
)


def test_encode_value_matches_xmt_matlab_example() -> None:
    assert encode_value(10.001) == bytes([0x00, 0x0A, 0x00, 0x0A])
    assert decode_value(0x00, 0x0A, 0x00, 0x0A) == 10.001


def test_packets_match_existing_matlab_logic() -> None:
    assert build_closed_loop_packet(0) == bytes.fromhex("AA 01 08 12 00 00 43 F2")
    assert build_read_move_packet(0) == bytes.fromhex("AA 01 07 06 00 00 AA")
    assert build_send_move_packet(0, 10.001) == bytes.fromhex("AA 01 0B 01 00 00 00 0A 00 0A A1")


def test_parse_read_move_response_uses_data_bytes_7_to_10() -> None:
    frame = bytes.fromhex("AA 01 0B 06 00 00 00 0A 00 0A")
    checksum = 0
    for item in frame:
        checksum ^= item
    assert parse_read_move_response(frame + bytes([checksum])) == 10.001

