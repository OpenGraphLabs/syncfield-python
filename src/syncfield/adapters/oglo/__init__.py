"""OGLO 0.9.3+ schema-6 wired tactile/inertial adapter.

Public surface:

- :class:`OgloTactileStream` — the wired USB :class:`~syncfield.stream.Stream` adapter.
- :class:`OgloDeviceManifest` — the parsed config manifest.
- :class:`OgloProtocolError` — raised on a non-v6 packet or manifest.

The pure wire-format parser lives in :mod:`syncfield.adapters.oglo.packet`.
"""

from __future__ import annotations

from syncfield.adapters.oglo.manifest import OgloDeviceManifest
from syncfield.adapters.oglo.selection import AmbiguousGloveError, GloveCandidate, select_glove
from syncfield.adapters.oglo.usb_packet import UsbTaggedPacket, iter_usb_packets, parse_usb_packet
from syncfield.adapters.oglo.packet import OgloProtocolError
from syncfield.adapters.oglo.stream import (
    CONFIG_CHAR_UUID,
    NOTIFY_CHAR_UUID,
    SERVICE_UUID,
    OgloArtifact,
    OgloSubstream,
    OgloTactileStream,
)

__all__ = [
    "UsbTaggedPacket",
    "iter_usb_packets",
    "parse_usb_packet",
    "AmbiguousGloveError",
    "GloveCandidate",
    "select_glove",
    "OgloTactileStream",
    "OgloDeviceManifest",
    "OgloProtocolError",
    "OgloSubstream",
    "OgloArtifact",
    "SERVICE_UUID",
    "NOTIFY_CHAR_UUID",
    "CONFIG_CHAR_UUID",
]
