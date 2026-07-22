"""MJPEGPassthroughStream — writer round-trip and construction contract.

The capture loop's integration truth lives on the kiosk (live cameras); here
we prove the pieces a Mac can prove: the passthrough writer produces a valid,
decodable MJPEG MP4 with capture-time pts, and the stream subclass locks the
kiosk configuration (pyav backend, mjpeg input).
"""

from __future__ import annotations

import importlib
import sys
from fractions import Fraction

import pytest


def _real_module(name: str):
    """Import *name* against the REAL ``av`` (other tests cache fake-av copies)."""
    for mod in (
        "syncfield.adapters._video_encoder",
        "syncfield.adapters.uvc_webcam",
        "syncfield.adapters.uvc_mjpeg_passthrough",
    ):
        sys.modules.pop(mod, None)
    return importlib.import_module(name)


def _encode_mjpeg_packets(count: int, width: int = 320, height: int = 240):
    """Real MJPEG packets, made with the real encoder."""
    import numpy as np

    av = importlib.import_module("av")
    ctx = av.CodecContext.create("mjpeg", "w")
    ctx.width = width
    ctx.height = height
    ctx.pix_fmt = "yuvj420p"
    ctx.time_base = Fraction(1, 30)
    packets = []
    for i in range(count):
        arr = np.full((height, width, 3), i * 20 % 255, dtype=np.uint8)
        frame = av.VideoFrame.from_ndarray(arr, format="rgb24").reformat(format="yuvj420p")
        frame.pts = i
        packets.extend(ctx.encode(frame))
    packets.extend(ctx.encode(None))
    return packets


def test_passthrough_writer_roundtrip(tmp_path):
    av = importlib.import_module("av")
    mod = _real_module("syncfield.adapters.uvc_mjpeg_passthrough")

    packets = _encode_mjpeg_packets(5)
    assert len(packets) >= 5

    # A template stream to copy codec parameters from: mux the first packet
    # set into a scratch container and reopen it for reading.
    scratch = tmp_path / "scratch.mp4"
    out = av.open(str(scratch), mode="w")
    ostream = out.add_stream("mjpeg", rate=30)
    ostream.width, ostream.height = 320, 240
    ostream.pix_fmt = "yuvj420p"
    for i, p in enumerate(packets):
        p.stream = ostream
        p.time_base = Fraction(1, 30)
        p.pts = p.dts = i
        out.mux(p)
    out.close()

    src = av.open(str(scratch))
    template = src.streams.video[0]

    writer = mod.PassthroughWriter.open(tmp_path / "out.mp4", template_stream=template)
    t0 = 1_000_000_000
    for i, packet in enumerate(src.demux(template)):
        if packet.size == 0:
            continue
        # 30 fps apart in capture time.
        writer.write_packet(packet, t0 + i * 33_333_333)
    writer.close()
    src.close()

    check = av.open(str(tmp_path / "out.mp4"))
    stream = check.streams.video[0]
    frames = [f for f in check.decode(stream)]
    assert stream.codec_context.name == "mjpeg"
    assert len(frames) == 5
    # pts follow capture time on the 90 kHz timebase: 33.333 ms ~ 3000 ticks.
    pts = [f.pts for f in frames]
    deltas = [b - a for a, b in zip(pts, pts[1:])]
    assert all(2990 <= d <= 3010 for d in deltas), deltas
    check.close()


def test_writer_write_after_close_raises(tmp_path):
    av = importlib.import_module("av")
    mod = _real_module("syncfield.adapters.uvc_mjpeg_passthrough")

    scratch = tmp_path / "s.mp4"
    out = av.open(str(scratch), mode="w")
    ostream = out.add_stream("mjpeg", rate=30)
    ostream.width, ostream.height = 320, 240
    ostream.pix_fmt = "yuvj420p"
    for i, p in enumerate(_encode_mjpeg_packets(2)):
        p.stream = ostream
        p.time_base = Fraction(1, 30)
        p.pts = p.dts = i
        out.mux(p)
    out.close()
    src = av.open(str(scratch))
    template = src.streams.video[0]
    writer = mod.PassthroughWriter.open(tmp_path / "o.mp4", template_stream=template)
    writer.close()
    writer.close()  # idempotent
    with pytest.raises(RuntimeError):
        writer.write_packet(next(src.demux(template)), 0)
    src.close()


def test_stream_locks_kiosk_configuration(tmp_path):
    mod = _real_module("syncfield.adapters.uvc_mjpeg_passthrough")
    stream = mod.MJPEGPassthroughStream("cam_wrist_left", device_index=3, output_dir=tmp_path)
    assert stream._backend == "pyav"
    assert stream._pixel_format == "mjpeg"
    assert (stream._width, stream._height) == (1920, 1080)
    assert stream._fps == 30.0
    assert stream._file_path == tmp_path / "cam_wrist_left.mp4"
    assert stream.kind == "video"  # -> orchestrator StreamWriter timestamps
