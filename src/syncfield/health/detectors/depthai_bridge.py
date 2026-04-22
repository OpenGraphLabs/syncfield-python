"""DepthAILoggerBridge — translate depthai Python log records into HealthEvents.

Installed as a standard :class:`logging.Handler` on the depthai logger.
Does not subclass DetectorBase — it is a translator, not a detector.
Its outputs are fingerprinted as ``<stream_id>:adapter:<subkind>`` so
the AdapterEventPassthrough detector owns their open/close lifecycle.
"""

from __future__ import annotations

import logging
import re
import time
from typing import Callable, Optional

from syncfield.health.severity import Severity
from syncfield.types import HealthEvent, HealthEventKind

Sink = Callable[[str, HealthEvent], None]

_XLINK_RE = re.compile(r"X_LINK_ERROR.*stream: '([^']+)'|stream: '([^']+)'.*X_LINK_ERROR")
_CRASH_RE = re.compile(r"Device with id (\S+) has crashed\. Crash dump logs are stored in: (\S+)")
_RECONNECT_TRY_RE = re.compile(r"Attempting to reconnect", re.IGNORECASE)
_RECONNECT_OK_RE = re.compile(r"Reconnection successful", re.IGNORECASE)
_CONN_CLOSED_RE = re.compile(r"Closed connection", re.IGNORECASE)


class DepthAILoggerBridge(logging.Handler):
    def __init__(self, stream_id: str, sink: Sink) -> None:
        super().__init__(level=logging.WARNING)
        self._stream_id = stream_id
        self._sink = sink

    def emit(self, record: logging.LogRecord) -> None:
        if record.levelno < logging.WARNING:
            return
        msg = record.getMessage()
        now = time.monotonic_ns()

        parsed = self._parse(msg, record.levelno, now)
        if parsed is None:
            parsed = HealthEvent(
                stream_id=self._stream_id,
                kind=HealthEventKind.WARNING,
                at_ns=now,
                detail=msg,
                severity=Severity.WARNING,
                source="adapter:oak:unparsed-log",
                fingerprint=f"{self._stream_id}:adapter:unparsed-log",
                data={"raw": msg, "levelname": record.levelname},
            )
        try:
            self._sink(self._stream_id, parsed)
        except Exception:
            # Never let bridge failures crash the logging path.
            pass

    def _parse(self, msg: str, levelno: int, now: int) -> Optional[HealthEvent]:
        crash = _CRASH_RE.search(msg)
        if crash:
            device_id, path = crash.group(1), crash.group(2)
            return HealthEvent(
                stream_id=self._stream_id,
                kind=HealthEventKind.ERROR,
                at_ns=now,
                detail="OAK device crashed",
                severity=Severity.CRITICAL,
                source="adapter:oak",
                fingerprint=f"{self._stream_id}:adapter:device-crash",
                data={"device_id": device_id, "crash_dump_path": path},
            )
        xlink = _XLINK_RE.search(msg)
        if xlink:
            stream = xlink.group(1) or xlink.group(2)
            return HealthEvent(
                stream_id=self._stream_id,
                kind=HealthEventKind.ERROR,
                at_ns=now,
                detail="XLink communication error",
                severity=Severity.ERROR,
                source="adapter:oak",
                fingerprint=f"{self._stream_id}:adapter:xlink-error",
                data={"stream": stream},
            )
        if _RECONNECT_OK_RE.search(msg):
            return HealthEvent(
                stream_id=self._stream_id,
                kind=HealthEventKind.RECONNECT,
                at_ns=now,
                detail="Reconnection successful",
                severity=Severity.INFO,
                source="adapter:oak",
                fingerprint=f"{self._stream_id}:adapter:reconnect-success",
            )
        if _RECONNECT_TRY_RE.search(msg):
            return HealthEvent(
                stream_id=self._stream_id,
                kind=HealthEventKind.RECONNECT,
                at_ns=now,
                detail="Attempting reconnect",
                severity=Severity.WARNING,
                source="adapter:oak",
                fingerprint=f"{self._stream_id}:adapter:reconnect-attempt",
            )
        if _CONN_CLOSED_RE.search(msg):
            return HealthEvent(
                stream_id=self._stream_id,
                kind=HealthEventKind.WARNING,
                at_ns=now,
                detail="Connection closed",
                severity=Severity.WARNING,
                source="adapter:oak",
                fingerprint=f"{self._stream_id}:adapter:connection-closed",
            )
        return None
