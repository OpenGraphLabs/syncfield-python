"""Parse and validate the OGLO device manifest (config characteristic).

The glove exposes a small JSON manifest on the config characteristic
(``4652535f-424c-4500-0002-000000000001``). The host reads it once at connect
to learn the wire geometry (taxel count, matrix shape, side-aware finger
order) and to hard-validate the protocol version. Only ``schema_ver == 6`` is
supported; anything else raises :class:`~syncfield.adapters.oglo.packet.OgloProtocolError`.

The firmware keeps this JSON lean (< 512 B); unknown keys are ignored, and the
wire *format* is detected from the notify header flags byte, never from here.
Emitted keys (``FW_REV 0.9.3+``): ``device``, ``schema_ver``, ``serial``,
``side``, ``hw_rev``, ``fw_rev``, ``rate_hz``, ``samples_per_packet``,
``adc_bits``, ``stream_mode``, ``values_per_sample``, ``sample_order``,
``sample_shape``, ``channels`` (5 side-aware finger names), ``device_id``,
``pair_id``, ``batch``, ``factory_passed``, ``cal_valid``, ``imu`` and the
optional firmware-0.9.16 ``link_ping`` capability.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Dict, Tuple

from syncfield.adapters.oglo.packet import (
    DEFAULT_VALUES_PER_SAMPLE,
    OgloProtocolError,
)

__all__ = [
    "LINK_PING_MIN_FW_REV",
    "MIN_SUPPORTED_FW_REV",
    "OgloDeviceManifest",
    "SUPPORTED_SCHEMA_VER",
    "firmware_revision_at_least",
    "is_supported_firmware",
]

#: The only production config schema this adapter supports.
SUPPORTED_SCHEMA_VER = 6

# Tagged USB first shipped in 0.9.3. Later patch releases keep schema 6 and
# the same wire contract; the current production golden is 0.9.8.
MIN_SUPPORTED_FW_REV = (0, 9, 3)
LINK_PING_MIN_FW_REV = (0, 9, 16)
_FW_REV_PATTERN = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")


def firmware_revision_at_least(value: str, minimum: tuple[int, int, int]) -> bool:
    """Compare a stable three-component firmware revision, failing closed."""
    match = _FW_REV_PATTERN.fullmatch(value.strip())
    return bool(match and tuple(int(part) for part in match.groups()) >= minimum)


def is_supported_firmware(value: str) -> bool:
    """Whether *value* is a stable schema-6 TAG firmware release.

    Fail closed on malformed/prerelease labels. Wire compatibility is anchored
    by ``schema_ver == 6``; the minimum rejects pre-TAG Rev-D firmware while
    allowing validated patch releases such as the 0.9.8 golden image.
    """
    return firmware_revision_at_least(value, MIN_SUPPORTED_FW_REV)

#: Firmware default matrix shape: 5 fingers x 4 rows x 4 cols.
_DEFAULT_SAMPLE_SHAPE: Tuple[int, int, int] = (5, 4, 4)

# Side-aware finger order used when the manifest omits ``channels``. The
# firmware emits the physical left→right order per hand; right starts at the
# thumb, left is mirrored.
_CANONICAL_FINGERS_RIGHT: Tuple[str, ...] = ("thumb", "index", "middle", "ring", "pinky")
_CANONICAL_FINGERS_LEFT: Tuple[str, ...] = ("pinky", "ring", "middle", "index", "thumb")


@dataclass(frozen=True)
class OgloDeviceManifest:
    """Validated view of the glove's schema-6 config manifest.

    Attributes:
        device: Device model string (e.g. ``"oglo"``).
        side: ``"left"`` | ``"right"`` | ``"unknown"`` — authoritative hand.
        schema_ver: Config schema version (always 6 here — validated).
        rate_hz: Nominal tactile sample rate (default 250).
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
    tag_ver_max: int = 1
    boot_id: str = ""
    link_ping: bool = False
    raw: Dict[str, Any] = field(default_factory=dict, repr=False)

    @classmethod
    def from_json(cls, raw: bytes | bytearray | str) -> "OgloDeviceManifest":
        """Parse and validate a manifest read from the config characteristic.

        Raises:
            OgloProtocolError: The bytes are not valid JSON, or
                ``schema_ver != 6``.
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
        tag_ver_max = _coerce_tag_ver_max(data.get("tag_ver_max", 1))
        boot_id = _coerce_boot_id(data.get("boot_id"))
        # Capability negotiation is deliberately exact. In particular, the
        # strings "true"/"1" and integer 1 must not opt an older or malformed
        # manifest into a command it may parse as an error inside TAG output.
        link_ping = data.get("link_ping") is True

        return cls(
            device=str(data.get("device") or "oglo"),
            side=side,
            schema_ver=SUPPORTED_SCHEMA_VER,
            rate_hz=int(data.get("rate_hz") or 250),
            values_per_sample=values_per_sample,
            sample_shape=shape,
            finger_labels=labels,
            serial=str(data.get("serial") or ""),
            fw_rev=str(data.get("fw_rev") or ""),
            tag_ver_max=tag_ver_max,
            boot_id=boot_id,
            link_ping=link_ping,
            raw=data,
        )

    @property
    def per_finger(self) -> int:
        """Taxels per finger (``rows * cols``)."""
        return self.sample_shape[1] * self.sample_shape[2]

    @property
    def supports_link_ping(self) -> bool:
        """Whether this exact firmware/config pair authorizes ``LINK PING``.

        Both conditions are load-bearing: capability-only gating would send a
        new command to a mislabeled old image, while version-only gating would
        send it to a 0.9.16 build that did not actually implement the command.
        """
        return self.link_ping and firmware_revision_at_least(
            self.fw_rev, LINK_PING_MIN_FW_REV
        )

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


def _coerce_tag_ver_max(value: Any) -> int:
    """Validate the transport capability without changing schema version.

    Firmware before 0.9.13 omits the key and is therefore TAG v1. A future
    firmware may advertise a higher maximum; this host will still negotiate
    the highest version it understands.
    """
    # JSON booleans are integers in Python, and int("2") / int(2.9) would
    # silently accept non-canonical manifests. Keep the wire contract exact:
    # one JSON integer in the one-byte protocol-version range.
    if type(value) is not int or not 1 <= value <= 0xFF:
        raise OgloProtocolError(
            "config tag_ver_max must be an integer between 1 and 255"
        )
    return value


def _coerce_boot_id(value: Any) -> str:
    """Return the optional per-boot identity advertised by TAG v2 firmware."""
    if value is None or value == "":
        return ""
    if isinstance(value, bool):
        raise OgloProtocolError("config boot_id must be 32 hexadecimal characters")
    if isinstance(value, int):
        if value < 0 or value >= (1 << 128):
            raise OgloProtocolError(
                "config boot_id must be 32 hexadecimal characters"
            )
        text = f"{value:032x}"
    elif isinstance(value, str):
        text = value.strip().lower()
    else:
        raise OgloProtocolError("config boot_id must be 32 hexadecimal characters")
    if not re.fullmatch(r"[0-9a-f]{32}", text):
        raise OgloProtocolError("config boot_id must be 32 hexadecimal characters")
    return text


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
