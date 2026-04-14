"""SyncField web viewer — browser-based session monitor and control.

Usage::

    import syncfield as sf
    import syncfield.viewer

    session = sf.SessionOrchestrator(host_id="rig_01", output_dir="./data")
    session.add(...)

    # Blocking mode — opens browser, returns on Ctrl+C
    syncfield.viewer.launch(session)

    # Passive mode — context manager, caller keeps control of the session
    with syncfield.viewer.launch_passive(session) as viewer:
        session.start()
        while viewer.is_running():
            time.sleep(0.1)
        session.stop()

The viewer starts a FastAPI server and opens a browser tab. The React
frontend connects via WebSocket for real-time state updates, MJPEG for
video preview, and SSE for sensor chart data.

Requires the ``viewer`` extra::

    pip install 'syncfield[viewer]'

which installs ``fastapi``, ``uvicorn``, ``av``, and ``Pillow``.
"""

from __future__ import annotations

try:
    import fastapi as _fastapi  # noqa: F401
    import uvicorn as _uvicorn  # noqa: F401
except ImportError as exc:  # pragma: no cover
    raise ImportError(
        "syncfield.viewer requires the 'viewer' extra. "
        "Install with `pip install 'syncfield[viewer]'`."
    ) from exc

from syncfield.viewer.app import ViewerHandle, launch, launch_passive

__all__ = ["launch", "launch_passive", "ViewerHandle"]
