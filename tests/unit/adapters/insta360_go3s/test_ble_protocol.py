import pytest

from syncfield.adapters.insta360_go3s.ble import protocol as p


# CRC-16/MODBUS reference vectors (poly 0xA001, init 0xFFFF, no reflection)
@pytest.mark.parametrize(
    "data,expected",
    [
        (b"\x01\x02\x03\x04", 0x2BA1),
        (b"123456789", 0x4B37),
        (b"\xff", 0x00FF),
    ],
)
def test_crc16_modbus_known_vectors(data, expected):
    assert p.crc16_modbus(data) == expected


def test_crc16_modbus_empty():
    assert p.crc16_modbus(b"") == 0xFFFF


def test_constants_match_protocol_spec():
    assert p.SERVICE_UUID == "0000be80-0000-1000-8000-00805f9b34fb"
    assert p.WRITE_CHAR_UUID == "0000be81-0000-1000-8000-00805f9b34fb"
    assert p.NOTIFY_CHAR_UUID == "0000be82-0000-1000-8000-00805f9b34fb"
    assert p.CMD_START_CAPTURE == 0x0004
    assert p.CMD_STOP_CAPTURE == 0x0005
    assert p.CMD_CHECK_AUTH == 0x0027
    assert p.CMD_SET_OPTIONS == 0x0002
    assert p.STATUS_OK == 0x00C8


def test_build_message_packet_structure():
    """A message packet starts with FF, type 0x07 (app->cam), subtype 0x40.

    Inner header is 16 bytes; CRC-16 trails."""
    pkt = p.build_message_packet(
        cmd=p.CMD_START_CAPTURE,
        seq=1,
        protobuf_payload=b"\x08\x01",
    )
    assert pkt[0] == 0xFF
    assert pkt[1] == 0x07
    assert pkt[2] == 0x40
    body = pkt[:-2]
    crc = int.from_bytes(pkt[-2:], "little")
    assert crc == p.crc16_modbus(body)


def test_build_sync_response_is_constant():
    sync = p.build_sync_response()
    assert sync[0] == 0xFF
    assert sync[1] == 0x07
    assert sync[2] == 0x41  # SUBTYPE_SYNC


def test_build_check_auth_payload_format():
    """auth_id is wrapped: [0x0A, len(addr)] + addr + [0x10, 0x02]."""
    addr = b"AA:BB:CC:DD:EE:FF"
    pb = p.build_check_auth_payload(addr)
    assert pb[0] == 0x0A
    assert pb[1] == len(addr)
    assert pb[2 : 2 + len(addr)] == addr
    assert pb[-2:] == b"\x10\x02"


def test_parse_response_extracts_seq_and_status():
    """A camera response (type 0x06) carries status at inner[7:9] (LE) and
    seq at inner[10], inside the 16-byte inner header that follows the
    5-byte FFFrame outer header.

    The camera does NOT echo the request's cmd_code; the inner[7:9] slot
    that holds cmd_code in app->cam requests is reused for status_code in
    cam->app responses. Correlation is by seq only.
    """
    inner = bytearray(16)
    inner[4] = 0x04                                 # mode
    inner[7:9] = p.STATUS_OK.to_bytes(2, "little")  # status_code (LE) = 0x00C8
    inner[9] = 0x02                                 # content_type
    inner[10] = 1                                   # seq
    payload = bytes(inner)
    outer_no_crc = (
        bytes([0xFF, 0x06, 0x40])
        + len(payload).to_bytes(2, "little")
        + payload
    )
    pkt = outer_no_crc + p.crc16_modbus(outer_no_crc).to_bytes(2, "little")
    parsed = p.parse_response_packet(pkt)
    assert parsed.seq == 1
    assert parsed.status == p.STATUS_OK
