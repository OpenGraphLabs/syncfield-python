"""OGLO tactile glove adapter (firmware ``schema_ver 5`` / ``packed12_v5``).

Public surface:

- :class:`OgloTactileStream` — the BLE :class:`~syncfield.stream.Stream` adapter.
- :class:`OgloDeviceManifest` — the parsed config manifest.
- :class:`OgloProtocolError` — raised on a non-v5 packet or manifest.

The pure wire-format parser lives in :mod:`syncfield.adapters.oglo.packet`.
"""

from __future__ import annotations

from syncfield.adapters.oglo.manifest import OgloDeviceManifest
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
    "OgloTactileStream",
    "OgloDeviceManifest",
    "OgloProtocolError",
    "OgloSubstream",
    "OgloArtifact",
    "SERVICE_UUID",
    "NOTIFY_CHAR_UUID",
    "CONFIG_CHAR_UUID",
]
