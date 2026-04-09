"""Data model for discovery results.

Two frozen dataclasses the rest of the discovery layer returns:

- :class:`DiscoveredDevice` — one physical device that a scan found.
- :class:`DiscoveryReport`  — the full result set, including partial-failure
  errors and timing information.

Keeping these immutable (``frozen=True``) means the viewer can pass them
around between threads without worrying about anyone mutating a field
mid-render.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar, Mapping, Tuple, Type, TYPE_CHECKING

if TYPE_CHECKING:
    from syncfield.stream import Stream


@dataclass(frozen=True)
class DiscoveredDevice:
    """One physical device that discovery found, ready to be constructed.

    Every field is populated by the adapter's :meth:`discover` classmethod.
    The key field is :attr:`construct_kwargs` — that dict carries the
    hardware identifiers the adapter needs (``device_index``, ``device_id``,
    ``mac``, …). Callers add application-level fields (``id``, typically
    ``output_dir``) at construction time via :meth:`construct`.

    Attributes:
        adapter_type: Stable string identifier for the adapter kind —
            ``"uvc_webcam"``, ``"oak_camera"``, ``"ble_imu"``,
            ``"oglo_tactile"``, ``"ble_peripheral"``. Used for filtering
            and UX grouping, not for class lookup (that's :attr:`adapter_cls`).
        adapter_cls: The Stream subclass to instantiate. Held as a direct
            class reference so :meth:`construct` needs no string → class
            lookup. Typed loosely as ``type`` because circular imports with
            :mod:`syncfield.stream` would otherwise force runtime gymnastics.
        kind: Stream kind (``"video" | "audio" | "sensor" | "custom"``) —
            mirrors :attr:`StreamBase.kind` so the viewer can group devices
            without instantiating them.
        display_name: Short human-readable label. Shown directly in the
            viewer's discovery modal and in CLI output.
        description: Additional one-line context (e.g. resolution, USB
            speed, BLE address tail). Rendered in a muted color below the
            name in UIs.
        device_id: Stable identifier for this specific device —
            ``cv2`` index, OAK serial, BLE MAC. Used for cache keys and
            user-facing copy like "already added".
        construct_kwargs: Keyword arguments for
            ``adapter_cls(**construct_kwargs, **caller_kwargs)``. Contains
            everything the discoverer learned from the hardware; callers
            provide ``id`` and (when applicable) ``output_dir`` at
            construct time.
        accepts_output_dir: Whether the underlying Stream ``__init__``
            takes an ``output_dir`` argument. ``scan_and_add`` uses this
            to decide whether to inject the session's output directory.
            True for video adapters, False for BLE-only sensors.
        in_use: Heuristic flag — True if the discoverer detected the
            device is already held by another process. ``scan_and_add``
            skips these; the viewer UI renders them disabled with a
            tooltip. Best-effort detection only.
        warnings: Tuple of short caveats the discoverer wants to surface
            (e.g. ``"requires characteristic_uuid for construction"`` for
            generic BLE devices). A non-empty warnings tuple causes
            :func:`scan_and_add` to skip this device — the user must add
            it explicitly via code.
    """

    adapter_type: str
    adapter_cls: Type[Any]
    kind: str
    display_name: str
    description: str
    device_id: str
    construct_kwargs: Mapping[str, Any] = field(default_factory=dict)
    accepts_output_dir: bool = False
    in_use: bool = False
    warnings: Tuple[str, ...] = ()

    def construct(self, **kwargs: Any) -> "Stream":
        """Instantiate the Stream for this device.

        Merges :attr:`construct_kwargs` with caller-supplied ``kwargs``
        and calls the adapter class. Caller-supplied values win on
        conflict so users can override discovery-found defaults
        (e.g. force a specific ``depth_enabled=True`` on an OAK).

        Args:
            **kwargs: Must include ``id``. Include ``output_dir`` when
                :attr:`accepts_output_dir` is True. Any additional adapter
                options are forwarded.

        Returns:
            A freshly constructed Stream instance, ready to be passed to
            :meth:`SessionOrchestrator.add`.

        Raises:
            TypeError: If required ``id`` is missing, or if the merged
                kwargs don't match the adapter's ``__init__`` signature.
        """
        if "id" not in kwargs:
            raise TypeError(
                f"{self.adapter_type}.construct() requires an 'id' keyword"
            )
        merged: dict[str, Any] = {**self.construct_kwargs, **kwargs}
        return self.adapter_cls(**merged)


@dataclass(frozen=True)
class DiscoveryReport:
    """Aggregated result of a single :func:`scan` call.

    Attributes:
        devices: Every device that any registered discoverer returned,
            in discovery order. Tuple so callers can rely on immutability.
        errors: Map from ``adapter_type`` to an error message string for
            discoverers that raised. Partial-failure friendly — a BLE
            stack crash never masks the OAK and UVC results.
        duration_s: Wall-clock seconds the whole scan took.
        timed_out: Tuple of ``adapter_type`` strings whose ``discover()``
            did not complete within the scan budget.
    """

    devices: Tuple[DiscoveredDevice, ...]
    errors: Mapping[str, str] = field(default_factory=dict)
    duration_s: float = 0.0
    timed_out: Tuple[str, ...] = ()

    def by_kind(self, kind: str) -> Tuple[DiscoveredDevice, ...]:
        """Return devices whose Stream kind matches ``kind``."""
        return tuple(d for d in self.devices if d.kind == kind)

    def by_adapter_type(self, adapter_type: str) -> Tuple[DiscoveredDevice, ...]:
        """Return devices from a specific adapter class."""
        return tuple(d for d in self.devices if d.adapter_type == adapter_type)

    @property
    def is_success(self) -> bool:
        """True when all registered discoverers completed without errors."""
        return not self.errors and not self.timed_out

    def summary(self) -> str:
        """Short one-line summary useful for log output."""
        parts = [f"{len(self.devices)} devices in {self.duration_s:.1f}s"]
        if self.errors:
            parts.append(f"{len(self.errors)} error(s)")
        if self.timed_out:
            parts.append(f"{len(self.timed_out)} timed out")
        return ", ".join(parts)
