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
