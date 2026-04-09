"""SyncField desktop viewer — a MuJoCo-style bundled GUI.

Usage::

    import syncfield as sf
    import syncfield.viewer

    session = sf.SessionOrchestrator(host_id="rig_01", output_dir="./data")
    session.add(...)

    # Blocking mode — opens the window, returns when it closes
    syncfield.viewer.launch(session)

    # Passive mode — context manager, caller keeps control of the session
    with syncfield.viewer.launch_passive(session) as viewer:
        session.start()
        while viewer.is_running():
            time.sleep(0.1)
        session.stop()

The viewer renders in the same process as the SDK. No HTTP, no IPC — the
poller holds a reference to the :class:`SessionOrchestrator` and reads its
state directly. Video frames are published by each adapter via a
thread-safe ``latest_frame`` property and uploaded to the GPU as raw
textures.

Requires the ``viewer`` extra::

    pip install 'syncfield[viewer]'

which installs ``dearpygui`` and ``numpy``. The SDK core stays stdlib-only
for users who never open the GUI.
"""

from __future__ import annotations

try:
    import dearpygui.dearpygui as _dpg  # noqa: F401
    import numpy as _np  # noqa: F401
except ImportError as exc:  # pragma: no cover - exercised at import time on CI
    raise ImportError(
        "syncfield.viewer requires the 'viewer' extra. "
        "Install with `pip install 'syncfield[viewer]'`."
    ) from exc

from syncfield.viewer.app import ViewerHandle, launch, launch_passive

__all__ = ["launch", "launch_passive", "ViewerHandle"]
