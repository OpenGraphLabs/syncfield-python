from __future__ import annotations

from syncfield.adapters.ovision_camera import OvisionCameraStream


class _Packet:
    size = 4
    is_keyframe = True

    def __bytes__(self) -> bytes:
        return b"idr!"


class _Input:
    def demux(self, *, video: int):
        assert video == 0
        return iter([_Packet()])


def test_recording_capture_keeps_preview_tap_live_without_decoding(tmp_path):
    stream = OvisionCameraStream("cam_ego", tmp_path)
    stream._input = _Input()
    stream._recording = True

    stream._capture_loop()

    assert stream._preview_packet == b"idr!"
    assert stream._preview_wake.is_set()
    packet, encoded, _capture_ns, is_keyframe = stream._packet_queue.get_nowait()
    assert isinstance(packet, _Packet)
    assert encoded == b"idr!"
    assert is_keyframe is True
