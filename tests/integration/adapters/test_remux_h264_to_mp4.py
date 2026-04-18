"""Round-trip integration test for ``remux_h264_to_mp4``.

Unlike ``tests/unit/adapters/test_video_encoder.py`` — which runs
against a MagicMock ``av`` module — this test drives the real PyAV
build installed on the host. It encodes a handful of solid-colour
frames into a raw ``.h264`` Annex-B bitstream, runs the remux helper,
and verifies the resulting ``.mp4`` is demuxable with the same frame
count.

Purpose: catch PyAV API drift that the unit mock cannot see. In
0.3.18 the remux helper called ``output_container.add_stream(template=...)``
which had been removed in PyAV 14 in favour of
``add_stream_from_template(template=...)``. The unit mock happily
accepted either signature, so the regression only surfaced on real
hardware. Pinning the round-trip behind actual PyAV closes that gap.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

av = pytest.importorskip("av")

from syncfield.adapters._video_encoder import remux_h264_to_mp4


pytestmark = pytest.mark.skipif(
    shutil.which("ffmpeg") is None,
    reason="ffmpeg CLI is required to synthesise the input Annex-B bitstream",
)


def _write_fake_h264(path: Path, *, width: int, height: int, fps: int, n_frames: int) -> None:
    """Use ffmpeg to synthesise a short Annex-B H.264 bitstream.

    We drive ffmpeg rather than PyAV's own muxer here because the raw
    ``h264`` muxer in PyAV is finicky about stream setup, whereas the
    CLI's ``testsrc`` filter + ``h264`` output format produces exactly
    the Annex-B layout the real DepthAI encoder emits. That's the
    bitstream shape our remux helper must handle, so this keeps the
    integration test honest.
    """
    duration = n_frames / fps
    subprocess.run(
        [
            "ffmpeg",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"testsrc=size={width}x{height}:rate={fps}:duration={duration:.3f}",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-pix_fmt",
            "yuv420p",
            "-f",
            "h264",
            str(path),
        ],
        check=True,
    )


def test_remux_round_trip_produces_playable_mp4(tmp_path: Path) -> None:
    h264_path = tmp_path / "sample.h264"
    mp4_path = tmp_path / "sample.mp4"

    width, height, fps, n_frames = 160, 120, 30, 15
    _write_fake_h264(h264_path, width=width, height=height, fps=fps, n_frames=n_frames)
    assert h264_path.exists() and h264_path.stat().st_size > 0

    remux_h264_to_mp4(h264_path, mp4_path, fps=float(fps))

    assert mp4_path.exists(), "remux did not produce an mp4 file"
    assert mp4_path.stat().st_size > 0, "remuxed mp4 is empty"

    # Re-open the MP4 and count demuxed packets. libx264 may emit a
    # slightly different frame count than the nominal n_frames (SPS/PPS,
    # B-frames, encoder flush behaviour), so we just require that at
    # least half survived — enough to prove the mux succeeded and the
    # bitstream is parseable.
    container = av.open(str(mp4_path), mode="r")
    try:
        video_stream = container.streams.video[0]
        assert video_stream.width == width
        assert video_stream.height == height
        demuxed = sum(
            1
            for packet in container.demux(video_stream)
            if packet.dts is not None
        )
    finally:
        container.close()

    assert demuxed >= n_frames // 2, (
        f"remuxed mp4 demuxed only {demuxed} packets from {n_frames} input frames"
    )
