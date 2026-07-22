"""_uvc_input_options — platform-correct ffmpeg option mapping.

The one behavior that matters on Linux: compressed formats (mjpeg/h264) must
be requested via the v4l2 demuxer's ``input_format`` option. ``pixel_format``
only matches raw formats there, so a compressed request routed through it is
silently ignored and the camera falls back to raw YUYV — which at 1080p30
exceeds USB bandwidth and drops frames.
"""

from syncfield.adapters._video_encoder import _uvc_input_options


def test_linux_maps_compressed_pixel_format_to_input_format():
    opts = _uvc_input_options(1920, 1080, 30.0, "mjpeg", platform="linux")
    assert opts["input_format"] == "mjpeg"
    assert "pixel_format" not in opts


def test_linux_maps_h264_to_input_format():
    opts = _uvc_input_options(1920, 1080, 30.0, "h264", platform="linux")
    assert opts["input_format"] == "h264"
    assert "pixel_format" not in opts


def test_linux_keeps_raw_pixel_format():
    opts = _uvc_input_options(1280, 720, 30.0, "yuyv422", platform="linux")
    assert opts["pixel_format"] == "yuyv422"
    assert "input_format" not in opts


def test_darwin_unchanged():
    opts = _uvc_input_options(1280, 720, 30.0, "mjpeg", platform="darwin")
    assert opts["pixel_format"] == "mjpeg"
    assert "input_format" not in opts


def test_none_pixel_format_sets_neither():
    opts = _uvc_input_options(1280, 720, 30.0, None, platform="linux")
    assert "pixel_format" not in opts
    assert "input_format" not in opts


def test_geometry_and_low_latency_options_present():
    opts = _uvc_input_options(1920, 1080, 30.0, "mjpeg", platform="linux")
    assert opts["video_size"] == "1920x1080"
    assert opts["framerate"] == "30"
    assert opts["fflags"] == "nobuffer+flush_packets"


def test_software_encoder_options_tuned_for_realtime():
    from syncfield.adapters._video_encoder import _encoder_options

    opts = _encoder_options("libx264")
    # Realtime capture, not offline transcode: medium preset measured ~5 fps
    # at 1080p30 on a Pi 5 (two concurrent encoders), dropping 5 of every 6
    # frames. ultrafast+zerolatency holds 30.
    assert opts == {"preset": "ultrafast", "tune": "zerolatency", "crf": "23"}


def test_hardware_encoder_gets_no_x264_options():
    from syncfield.adapters._video_encoder import _encoder_options

    assert _encoder_options("h264_videotoolbox") == {}


def test_libx264_encode_smoke_with_options(tmp_path):
    import importlib
    import sys

    import numpy as np

    # Other tests in this directory reimport _video_encoder against a FAKE
    # ``av`` (conftest._install_fake_av) and leave that module object cached.
    # Evict and reimport so this smoke exercises the real encoder.
    sys.modules.pop("syncfield.adapters._video_encoder", None)
    ve = importlib.import_module("syncfield.adapters._video_encoder")
    VideoEncoder = ve.VideoEncoder

    enc = VideoEncoder.open(tmp_path / "t.mp4", width=320, height=240, fps=30.0, codec="libx264")
    frame = np.zeros((240, 320, 3), dtype=np.uint8)
    for _ in range(10):
        enc.write(frame)
    enc.close()
    assert (tmp_path / "t.mp4").stat().st_size > 0
