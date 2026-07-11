"""OGLO USB binary frame parser — pure, no serial/hardware."""

from __future__ import annotations

import struct

import pytest

from syncfield.adapters.oglo.packet import OgloProtocolError
from syncfield.adapters.oglo.usb_packet import (
    USB_FRAME_LEN,
    USB_MAGIC,
    UsbFrame,
    iter_usb_frames,
    parse_usb_frame,
)

NUM_TAXELS = 80


def build_frame(
    *,
    ts_us: int = 1_000_000,
    taxels=None,
    imu=(51_00, -21_00, 100, 200, 300, 1, 2, 3),  # roll,pitch,ax,ay,az,gx,gy,gz (cdeg + raw)
    ok: int = 1,
) -> bytes:
    """A well-formed 183-byte frame, matching the firmware's writeSerialBinaryFrame."""
    taxels = taxels if taxels is not None else [0] * NUM_TAXELS
    assert len(taxels) == NUM_TAXELS
    body = bytearray(USB_MAGIC)
    body += struct.pack("<I", ts_us)
    body += struct.pack(f"<{NUM_TAXELS}H", *taxels)
    body += struct.pack("<8h", *imu)
    body += bytes([ok])
    assert len(body) == USB_FRAME_LEN
    return bytes(body)


class TestParseFrame:
    def test_decodes_all_fields(self):
        taxels = list(range(NUM_TAXELS))  # 0..79
        f = parse_usb_frame(build_frame(ts_us=1_234_567, taxels=taxels, imu=(5125, -2138, 1631, 3249, 2607, 6, -9, -2), ok=1))
        assert f.device_us == 1_234_567
        assert f.taxels == tuple(range(NUM_TAXELS))
        assert f.roll_cdeg == 5125
        assert f.pitch_cdeg == -2138
        assert f.imu_raw == (1631, 3249, 2607, 6, -9, -2)  # ax..gz, NOT roll/pitch
        assert f.imu_ok is True

    def test_imu_not_ok_flag(self):
        f = parse_usb_frame(build_frame(ok=0))
        assert f.imu_ok is False

    def test_wrong_length_raises(self):
        with pytest.raises(OgloProtocolError, match="183 bytes"):
            parse_usb_frame(build_frame()[:-1])

    def test_bad_magic_raises(self):
        bad = bytearray(build_frame())
        bad[0] = 0x00
        with pytest.raises(OgloProtocolError, match="magic"):
            parse_usb_frame(bytes(bad))

    def test_to_sample_narrows_to_ogloSample(self):
        f = parse_usb_frame(build_frame(ts_us=2_000_000, imu=(10, 20, 7, 8, 9, 1, 2, 3), ok=1))
        s = f.to_sample(seq=42)
        assert s.seq == 42
        assert s.device_us == 2_000_000
        assert s.device_ns == 2_000_000_000
        assert s.imu == (7, 8, 9, 1, 2, 3)  # ax..gz; roll/pitch dropped
        assert len(s.taxels) == NUM_TAXELS

    def test_to_sample_drops_imu_when_not_ok(self):
        f = parse_usb_frame(build_frame(ok=0))
        assert f.to_sample(seq=0).imu is None


class TestIterFrames:
    def test_extracts_multiple_frames(self):
        buf = build_frame(ts_us=1) + build_frame(ts_us=2) + build_frame(ts_us=3)
        frames, leftover = iter_usb_frames(buf)
        frames = list(frames)
        assert [f.device_us for f in frames] == [1, 2, 3]
        assert leftover == b""

    def test_holds_a_partial_trailing_frame(self):
        whole = build_frame(ts_us=1)
        partial = build_frame(ts_us=2)[:100]
        frames, leftover = iter_usb_frames(whole + partial)
        assert [f.device_us for f in list(frames)] == [1]
        assert leftover == partial  # kept intact for the next read

    def test_resyncs_past_leading_garbage(self):
        # A leftover #HB text line, then a real frame.
        garbage = b"#HB t_us=123 f0=0 ble=1\r\n"
        frames, _ = iter_usb_frames(garbage + build_frame(ts_us=7))
        assert [f.device_us for f in list(frames)] == [7]

    def test_a_magic_inside_the_body_does_not_desync(self):
        # A taxel value of 0x55AA puts the magic bytes inside the frame body.
        taxels = [0] * NUM_TAXELS
        taxels[0] = 0x55AA  # bytes 0xAA 0x55 little-endian
        buf = build_frame(ts_us=9, taxels=taxels) + build_frame(ts_us=10)
        frames, leftover = iter_usb_frames(buf)
        got = [f.device_us for f in list(frames)]
        assert got == [9, 10], "must consume the whole first frame, not resync on inner magic"
        assert leftover == b""

    def test_non_magic_garbage_between_frames_is_skipped(self):
        # Realistic CDC case: a stray text line (no magic) between two frames.
        # Without a checksum the parser can only resync on the magic, which is
        # exactly what happens here — the garbage carries no magic, so it is
        # dropped cleanly and both frames survive.
        good = build_frame(ts_us=1)
        garbage = b"#STATUS {...}\r\n"  # firmware status line, no 0xAA55
        buf = good + garbage + build_frame(ts_us=2)
        got = [f.device_us for f in list(iter_usb_frames(buf)[0])]
        assert got == [1, 2]

    def test_keeps_a_trailing_partial_magic_byte(self):
        frames, leftover = iter_usb_frames(build_frame(ts_us=1) + b"\xAA")
        assert [f.device_us for f in list(frames)] == [1]
        assert leftover == b"\xAA"

    def test_empty_buffer(self):
        frames, leftover = iter_usb_frames(b"")
        assert list(frames) == []
        assert leftover == b""
