"""Process-wide registry of live Go3SBLECamera instances.

Why this exists
---------------
CoreBluetooth allows only one active GATT connection per peripheral, so
the aggregation downloader can't open its own BLE link to a camera that
the recording stream is already holding open. The downloader needs that
link to send periodic ``CMD_CHECK_AUTH`` keep-alive frames during the
WiFi association handshake — without them the camera's WiFi radio drops
into low-power standby and macOS gets ``-3925 tmpErr`` on the join.

The pattern: ``Go3SStream`` registers its live cam via :func:`register`
right after a successful BLE connect, and unregisters via
:func:`unregister` before disconnecting. The aggregation downloader
calls :func:`get` to look up the live cam by BLE address and reuses
its connection.
"""
from __future__ import annotations

import threading
from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from .camera import Go3SBLECamera


_LOCK = threading.Lock()
_LIVE: dict[str, "Go3SBLECamera"] = {}


def register(address: str, cam: "Go3SBLECamera") -> None:
    """Publish a live camera instance keyed by its BLE address."""
    with _LOCK:
        _LIVE[address.lower()] = cam


def unregister(address: str, cam: "Go3SBLECamera") -> None:
    """Remove the cam from the registry if it's still the published one."""
    with _LOCK:
        existing = _LIVE.get(address.lower())
        if existing is cam:
            _LIVE.pop(address.lower(), None)


def get(address: str) -> Optional["Go3SBLECamera"]:
    """Return the live cam for this BLE address, or None.

    Drops stale entries whose underlying GATT link has dropped, so callers
    can trust ``cam.is_connected`` on the returned value.
    """
    with _LOCK:
        cam = _LIVE.get(address.lower())
        if cam is None:
            return None
        try:
            connected = bool(cam.is_connected)
        except Exception:
            connected = False
        if not connected:
            _LIVE.pop(address.lower(), None)
            return None
        return cam


async def send_wake(cam: "Go3SBLECamera") -> None:
    """Send a ``CMD_CHECK_AUTH`` ping to the camera over its open BLE link.

    The camera keeps its WiFi radio in "actively listening" mode for several
    seconds after seeing a recent BLE client. Without this nudge, macOS's
    ``networksetup -setairportnetwork`` hits ``-3925 tmpErr`` because the
    camera-side WiFi has dropped into low-power standby.

    Best-effort — a failed wake should never abort the aggregation flow.
    """
    if not cam.is_connected:
        return
    # Use the same protocol the recorder reverse-engineered: CHECK_AUTH
    # with the device address as auth_id. We reach into the cam's private
    # _send because Go3SBLECamera doesn't (yet) expose a public wake hook.
    from .protocol import CMD_CHECK_AUTH, build_check_auth_payload
    try:
        await cam._send(  # noqa: SLF001 — intentional cross-module reuse
            CMD_CHECK_AUTH,
            build_check_auth_payload(cam._address),  # noqa: SLF001
            timeout=2.0,
        )
    except Exception:
        # Camera might be momentarily unresponsive; wake is best-effort.
        pass
