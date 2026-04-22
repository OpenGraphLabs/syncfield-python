"""Unit tests for OAK adapter health wiring (no real hardware required)."""

import logging
import pytest


pytest.importorskip("depthai")  # skip entire module if the oak extra isn't installed


def test_oak_declares_target_hz(tmp_path):
    from syncfield.adapters.oak_camera import OakCameraStream
    s = OakCameraStream(id="oak-main", rgb_fps=30, output_dir=tmp_path)
    assert s.capabilities.target_hz == 30.0


def test_oak_bridge_install_routes_depthai_errors_to_emit_health(tmp_path):
    from syncfield.adapters.oak_camera import OakCameraStream

    captured = []
    s = OakCameraStream(id="oak-main", rgb_fps=30, output_dir=tmp_path)
    s.on_health(lambda ev: captured.append(ev))

    # Install the bridge directly (connect() builds a pipeline we can't easily mock here).
    s._install_depthai_bridge()
    try:
        logging.getLogger("depthai").error(
            "Communication exception - Original message 'Couldn't read data from stream: '__x_0_1' (X_LINK_ERROR)'"
        )
    finally:
        s._uninstall_depthai_bridge()

    assert any(ev.fingerprint == "oak-main:adapter:xlink-error" for ev in captured), \
        f"no xlink-error event captured; got fingerprints: {[c.fingerprint for c in captured]}"


def test_oak_bridge_uninstall_is_idempotent(tmp_path):
    from syncfield.adapters.oak_camera import OakCameraStream

    s = OakCameraStream(id="oak-main", rgb_fps=30, output_dir=tmp_path)
    s._uninstall_depthai_bridge()   # no-op before install
    s._install_depthai_bridge()
    s._uninstall_depthai_bridge()
    s._uninstall_depthai_bridge()   # no-op after uninstall
