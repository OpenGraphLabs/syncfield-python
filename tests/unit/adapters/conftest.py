"""Shared fixtures for adapter tests.

Hoisted from ``test_uvc_webcam.py`` so both UVC and OAK tests can
patch ``av`` with the same fake module.
"""

from __future__ import annotations

import importlib
import sys
import time
from types import SimpleNamespace
from unittest.mock import MagicMock

import numpy as np
import pytest


def _make_frame(i: int) -> MagicMock:
    """Build a fake PyAV VideoFrame whose ``to_ndarray`` returns BGR24."""
    frame = MagicMock(name=f"Frame-{i}")
    frame.to_ndarray = MagicMock(
        return_value=np.full((48, 64, 3), i % 256, dtype=np.uint8)
    )
    return frame


def _build_fake_av(
    *, frame_budget: int, pace_seconds: float
) -> tuple[SimpleNamespace, MagicMock, MagicMock]:
    """Build a fake ``av`` module.

    Returns ``(av, input_container, output_stream)``. The input container's
    ``decode(video=0)`` yields ``frame_budget`` fake frames, optionally
    paced by ``pace_seconds`` between yields so the capture thread stays
    alive across ``time.sleep()`` windows in lifecycle tests.
    """
    def _paced_frames():
        for i in range(frame_budget):
            if pace_seconds > 0:
                time.sleep(pace_seconds)
            yield _make_frame(i)

    input_container = MagicMock(name="InputContainer")
    input_container.decode = MagicMock(return_value=_paced_frames())

    # The OAK h264 → mp4 remux path uses ``.streams.video[0]`` + a
    # generator-returning ``.demux(stream)``. Stub both so tests can
    # drive the remux call through without crashing; no real packet
    # payload is produced — the remux's copy-mode loop simply sees
    # zero packets and the output ends up empty. Integration tests on
    # real OAK hardware exercise the content-producing path.
    input_stream = MagicMock(name="InputVideoStream")
    input_container.streams.video = [input_stream]
    input_container.demux = MagicMock(return_value=iter(()))

    output_stream = MagicMock(name="VideoStream")
    packet = MagicMock(name="Packet")
    output_stream.encode = MagicMock(
        side_effect=lambda frame: [packet] if frame is not None else []
    )
    output_container = MagicMock(name="OutputContainer")
    output_container.add_stream = MagicMock(return_value=output_stream)

    def _av_open(url, *args, **kwargs):  # noqa: ANN001 - MagicMock signature
        if kwargs.get("mode") == "w":
            return output_container
        return input_container

    av = SimpleNamespace()
    av.open = MagicMock(side_effect=_av_open)
    av.VideoFrame = SimpleNamespace(
        from_ndarray=MagicMock(return_value=MagicMock(name="OutFrame"))
    )
    av.codec = SimpleNamespace(
        Codec=MagicMock(side_effect=lambda n, m: SimpleNamespace(name=n))
    )
    return av, input_container, output_stream


# Modules that cache ``av`` at import time. When we swap a fake ``av``
# into ``sys.modules`` we must also force these to reload.
_AV_DEPENDENT_MODULES = (
    "syncfield.adapters._video_encoder",
    "syncfield.adapters.uvc_webcam",
    "syncfield.adapters.oak_camera",
)


def _install_fake_av(
    monkeypatch, *, frame_budget: int, pace_seconds: float
) -> SimpleNamespace:
    """Install a fake ``av`` module, evict cached imports, reimport adapters."""
    av, input_container, output_stream = _build_fake_av(
        frame_budget=frame_budget, pace_seconds=pace_seconds
    )
    monkeypatch.setitem(sys.modules, "av", av)
    for mod in _AV_DEPENDENT_MODULES:
        sys.modules.pop(mod, None)
    import syncfield.adapters as _adapters_pkg
    # Remove parent-package attribute cache so the next ``from syncfield.adapters
    # import _video_encoder`` re-resolves and binds to the fake ``av``.
    for attr in ("_video_encoder", "uvc_webcam", "oak_camera"):
        monkeypatch.delattr(_adapters_pkg, attr, raising=False)
    importlib.import_module("syncfield.adapters._video_encoder")
    return SimpleNamespace(
        av=av,
        input_container=input_container,
        output_stream=output_stream,
    )


def _evict_av_dependent_imports() -> None:
    for mod in _AV_DEPENDENT_MODULES:
        sys.modules.pop(mod, None)


@pytest.fixture
def mock_av(monkeypatch):
    """Plain fake ``av`` — short decode budget, no pacing.

    Suitable for tests that don't rely on the capture thread staying
    alive across a ``time.sleep()`` window.
    """
    handle = _install_fake_av(monkeypatch, frame_budget=3, pace_seconds=0.0)
    yield handle
    _evict_av_dependent_imports()


@pytest.fixture
def mock_av_generous(monkeypatch):
    """Paced fake ``av`` — 10k frame budget + 1 ms between yields.

    TODO(test-harness): ``pace_seconds`` is a pragmatic wall-clock
    workaround. A deterministic ``threading.Event``-driven pump would
    eliminate the wall-clock dependency. Revisit if CI flakes appear.
    """
    handle = _install_fake_av(
        monkeypatch, frame_budget=10_000, pace_seconds=0.001
    )
    yield handle
    _evict_av_dependent_imports()
