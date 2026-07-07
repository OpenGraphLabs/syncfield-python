"""Parse and validate the OGLO device manifest (config characteristic).

The glove exposes a small JSON manifest on the config characteristic
(``4652535f-424c-4500-0002-000000000001``). The host reads it once at connect
to learn the wire geometry (taxel count, matrix shape, side-aware finger
order) and to hard-validate the protocol version. Only ``schema_ver == 5`` is
supported; anything else raises :class:`~syncfield.adapters.oglo.packet.OgloProtocolError`.

The firmware keeps this JSON lean (< 512 B); unknown keys are ignored, and the
wire *format* is detected from the notify header flags byte, never from here.
Emitted keys (``FW_REV 0.7.1-cfgfit``): ``device``, ``schema_ver``, ``serial``,
``side``, ``hw_rev``, ``fw_rev``, ``rate_hz``, ``samples_per_packet``,
``adc_bits``, ``stream_mode``, ``values_per_sample``, ``sample_order``,
``sample_shape``, ``channels`` (5 side-aware finger names), ``device_id``,
``pair_id``, ``batch``, ``factory_passed``, ``cal_valid``, ``imu``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Dict, Tuple

from syncfield.adapters.oglo.packet import (
    DEFAULT_VALUES_PER_SAMPLE,
    OgloProtocolError,
)

__all__ = ["OgloDeviceManifest", "SUPPORTED_SCHEMA_VER"]

#: The only config schema this adapter supports.
SUPPORTED_SCHEMA_VER = 5

#: Firmware default matrix shape: 5 fingers x 4 rows x 4 cols.
_DEFAULT_SAMPLE_SHAPE: Tuple[int, int, int] = (5, 4, 4)

# Side-aware finger order used when the manifest omits ``channels``. The
# firmware emits the physical left→right order per hand; right starts at the
# thumb, left is mirrored.
_CANONICAL_FINGERS_RIGHT: Tuple[str, ...] = ("thumb", "index", "middle", "ring", "pinky")
_CANONICAL_FINGERS_LEFT: Tuple[str, ...] = ("pinky", "ring", "middle", "index", "thumb")


@dataclass(frozen=True)
class OgloDeviceManifest:
    """Validated view of the glove's schema-5 config manifest.

    Attributes:
        device: Device model string (e.g. ``"oglo"``).
        side: ``"left"`` | ``"right"`` | ``"unknown"`` — authoritative hand.
        schema_ver: Config schema version (always 5 here — validated).
        rate_hz: Nominal sample rate (default 100).
        values_per_sample: Taxel count per sample (default 80).
        sample_shape: ``(fingers, rows, cols)`` matrix shape (default 5,4,4).
        finger_labels: Side-aware finger names, one per finger.
        serial: Device serial string (may be empty).
        fw_rev: Firmware revision string (may be empty).
        raw: The full parsed JSON, for callers that want extra fields.
    """

    device: str
    side: str
    schema_ver: int
    rate_hz: int
    values_per_sample: int
    sample_shape: Tuple[int, int, int]
    finger_labels: Tuple[str, ...]
    serial: str
    fw_rev: str
    raw: Dict[str, Any] = field(default_factory=dict, repr=False)

    @classmethod
    def from_json(cls, raw: bytes | bytearray | str) -> "OgloDeviceManifest":
        """Parse and validate a manifest read from the config characteristic.

        Raises:
            OgloProtocolError: The bytes are not valid JSON, or
                ``schema_ver != 5``.
        """
        if isinstance(raw, (bytes, bytearray)):
            text = bytes(raw).decode("utf-8", errors="replace")
        else:
            text = raw
        try:
            data = json.loads(text)
        except (ValueError, TypeError) as exc:
            raise OgloProtocolError(f"config manifest is not valid JSON: {exc}") from exc
        if not isinstance(data, dict):
            raise OgloProtocolError("config manifest must be a JSON object")

        schema_ver = data.get("schema_ver")
        if schema_ver != SUPPORTED_SCHEMA_VER:
            raise OgloProtocolError(
                f"unsupported OGLO schema_ver={schema_ver!r} "
                f"(this adapter requires {SUPPORTED_SCHEMA_VER}). "
                "Update the glove firmware or use the matching SDK version."
            )

        side = str(data.get("side") or "unknown").lower()
        values_per_sample = int(data.get("values_per_sample") or DEFAULT_VALUES_PER_SAMPLE)
        shape = _coerce_shape(data.get("sample_shape"))
        labels = _coerce_finger_labels(data.get("channels"), side, shape[0])

        return cls(
            device=str(data.get("device") or "oglo"),
            side=side,
            schema_ver=SUPPORTED_SCHEMA_VER,
            rate_hz=int(data.get("rate_hz") or 100),
            values_per_sample=values_per_sample,
            sample_shape=shape,
            finger_labels=labels,
            serial=str(data.get("serial") or ""),
            fw_rev=str(data.get("fw_rev") or ""),
            raw=data,
        )

    @property
    def per_finger(self) -> int:
        """Taxels per finger (``rows * cols``)."""
        return self.sample_shape[1] * self.sample_shape[2]

    def channel_label(self, taxel_index: int) -> str:
        """Return the ``<finger>_<row>_<col>`` label for a taxel index.

        Uses the side-aware finger order and the ``(fingers, rows, cols)``
        matrix shape, mirroring the Swift reference's labelling.
        """
        _, _, cols = self.sample_shape
        per_finger = self.per_finger
        finger_idx = taxel_index // per_finger
        rem = taxel_index % per_finger
        row = rem // cols
        col = rem % cols
        if 0 <= finger_idx < len(self.finger_labels):
            finger = self.finger_labels[finger_idx]
        else:
            finger = f"f{finger_idx}"
        return f"{finger}_{row}_{col}"

    def channel_labels(self) -> Tuple[str, ...]:
        """All ``values_per_sample`` channel labels, in taxel order."""
        return tuple(self.channel_label(i) for i in range(self.values_per_sample))


def _coerce_shape(value: Any) -> Tuple[int, int, int]:
    if isinstance(value, (list, tuple)) and len(value) == 3:
        try:
            return (int(value[0]), int(value[1]), int(value[2]))
        except (TypeError, ValueError):
            pass
    return _DEFAULT_SAMPLE_SHAPE


def _coerce_finger_labels(value: Any, side: str, finger_count: int) -> Tuple[str, ...]:
    if isinstance(value, (list, tuple)) and value:
        labels = tuple(str(v) for v in value)
        if len(labels) >= finger_count:
            return labels[:finger_count]
        # Manifest under-specified the finger names — pad from the canonical
        # order so channel labelling stays deterministic.
        canonical = _canonical_fingers(side, finger_count)
        return labels + canonical[len(labels):]
    return _canonical_fingers(side, finger_count)


def _canonical_fingers(side: str, finger_count: int) -> Tuple[str, ...]:
    base = _CANONICAL_FINGERS_LEFT if side == "left" else _CANONICAL_FINGERS_RIGHT
    if finger_count <= len(base):
        return base[:finger_count]
    return base + tuple(f"f{i}" for i in range(len(base), finger_count))
