import logging
from typing import List

from syncfield.health.detectors.depthai_bridge import DepthAILoggerBridge
from syncfield.types import HealthEvent


def _mk_record(msg: str, level: int = logging.ERROR, name: str = "depthai") -> logging.LogRecord:
    return logging.LogRecord(
        name=name, level=level, pathname="", lineno=0, msg=msg, args=(), exc_info=None,
    )


def test_xlink_error_maps_to_xlink_fingerprint():
    captured: List[HealthEvent] = []
    bridge = DepthAILoggerBridge(stream_id="oak-main", sink=lambda sid, ev: captured.append(ev))
    rec = _mk_record("Communication exception - possible device error. Original message 'Couldn't read data from stream: '__x_0_1' (X_LINK_ERROR)'")
    bridge.emit(rec)
    assert len(captured) == 1
    ev = captured[0]
    assert ev.stream_id == "oak-main"
    assert ev.fingerprint == "oak-main:adapter:xlink-error"
    assert ev.source == "adapter:oak"
    assert ev.data.get("stream") == "__x_0_1"


def test_device_crash_attaches_crash_dump_path():
    captured: List[HealthEvent] = []
    bridge = DepthAILoggerBridge(stream_id="oak-main", sink=lambda sid, ev: captured.append(ev))
    rec = _mk_record("Device with id 194430 has crashed. Crash dump logs are stored in: /tmp/crash/crash_dump.json - please report to developers.")
    bridge.emit(rec)
    ev = captured[0]
    assert ev.fingerprint == "oak-main:adapter:device-crash"
    assert ev.data.get("crash_dump_path") == "/tmp/crash/crash_dump.json"


def test_reconnect_attempt_and_success_have_distinct_fingerprints():
    captured: List[HealthEvent] = []
    bridge = DepthAILoggerBridge(stream_id="oak-main", sink=lambda sid, ev: captured.append(ev))
    bridge.emit(_mk_record("Attempting to reconnect. Timeout is 10000ms", level=logging.WARNING))
    bridge.emit(_mk_record("Reconnection successful", level=logging.WARNING))
    fps = [c.fingerprint for c in captured]
    assert "oak-main:adapter:reconnect-attempt" in fps
    assert "oak-main:adapter:reconnect-success" in fps


def test_unrecognized_error_falls_back_to_warning_unparsed():
    captured: List[HealthEvent] = []
    bridge = DepthAILoggerBridge(stream_id="oak-main", sink=lambda sid, ev: captured.append(ev))
    bridge.emit(_mk_record("Something totally new and unrecognized", level=logging.ERROR))
    assert len(captured) == 1
    assert captured[0].source == "adapter:oak:unparsed-log"


def test_info_records_are_ignored():
    captured = []
    bridge = DepthAILoggerBridge(stream_id="oak-main", sink=lambda sid, ev: captured.append(ev))
    bridge.emit(_mk_record("Some info", level=logging.INFO))
    assert captured == []
