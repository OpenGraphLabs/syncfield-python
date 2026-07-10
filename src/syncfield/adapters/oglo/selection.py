"""Pick the right glove out of a BLE scan.

``OgloTactileStream`` used to take the first peripheral whose advertised name
contained ``ble_name`` (default ``"oglo"``) and ignore the ``hand`` argument
entirely. With both gloves powered on — which is the only configuration the
tactile setups ever run in — a left stream and a right stream would both match
the same peripheral, and dict iteration order decided which one. The right
glove's taxels would then be written to ``tactile_left.jsonl``.

Nothing downstream can detect that. The manifest's ``side`` overrides the
``hand`` hint *after* connecting, so the stream even relabels itself correctly
while its ``id`` — the thing the filename comes from — still says ``left``. A
mislabelled dataset is worse than a session that refuses to start.

So: a stream that knows which hand it wants matches on the side token in the
advertised name, and ambiguity is an error rather than a coin flip. The
firmware's names carry the side as a token with arbitrary separators and
suffixes around it — observed in the wild: ``OGLO LEFT``, ``OGLO RIGHT``,
``OGLO_LEFT_TEST01``, ``OGLO_V2_RIGHT``.

Pure and synchronous: no bleak, no asyncio, no radio. The scan is the caller's
problem; choosing is this module's.
"""

from __future__ import annotations

import re
from typing import Any, Iterable, Sequence

__all__ = [
    "AmbiguousGloveError",
    "GloveCandidate",
    "select_glove",
    "side_of",
]

_OGLO_TOKEN = "OGLO"
_SIDE_TOKENS = {"left": "LEFT", "right": "RIGHT"}

#: Split on anything that is not a letter or a digit: ``_``, ``-``, ``.``, ``  ``.
_TOKEN_SPLIT = re.compile(r"[^A-Z0-9]+")


class AmbiguousGloveError(RuntimeError):
    """Two or more peripherals answer to the same hand.

    Deliberately fatal. Choosing one would silently label a whole recording
    with the wrong hand, and no later stage can tell.
    """


class GloveCandidate:
    """One peripheral from a scan, with every name it advertises.

    A device can present a name on the ``BLEDevice`` and a different
    ``local_name`` in its advertisement data; either may carry the side token,
    so both are considered.
    """

    __slots__ = ("device", "names")

    def __init__(self, device: Any, names: Sequence[str]) -> None:
        self.device = device
        self.names = tuple(n for n in names if n)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"GloveCandidate(names={self.names!r})"


def _tokens(name: str) -> set[str]:
    return {t for t in _TOKEN_SPLIT.split(name.upper()) if t}


def side_of(name: str) -> str | None:
    """``"left"``, ``"right"``, or ``None`` for a non-OGLO or side-less name.

    The side must be a whole token. ``OGLO-RIGHT-HAND`` is a right glove;
    ``OGLO_RIGHTHAND`` and ``OGLO_BRIGHT`` are not — neither yields ``RIGHT``
    on its own. A name carrying both side tokens identifies nothing.
    """
    tokens = _tokens(name)
    if _OGLO_TOKEN not in tokens:
        return None
    matches = [side for side, token in _SIDE_TOKENS.items() if token in tokens]
    return matches[0] if len(matches) == 1 else None


def _matches_name_filter(candidate: GloveCandidate, ble_name: str) -> bool:
    needle = ble_name.lower()
    return any(needle in name.lower() for name in candidate.names)


def _matches_hand(candidate: GloveCandidate, hand: str) -> bool:
    return any(side_of(name) == hand for name in candidate.names)


def select_glove(
    candidates: Iterable[GloveCandidate],
    *,
    ble_name: str,
    hand: str = "unknown",
) -> Any | None:
    """The one device matching *ble_name* (and *hand*, when it is known).

    Returns the opaque device object, or ``None`` when nothing matches — the
    caller turns that into its own "glove not found" error, since only it knows
    the scan timeout it used.

    Raises :class:`AmbiguousGloveError` when more than one peripheral matches.
    That includes ``hand="unknown"``: if a caller cannot say which glove it
    wants and two are on the air, there is no defensible way to choose.
    """
    matched = [c for c in candidates if _matches_name_filter(c, ble_name)]

    if hand in _SIDE_TOKENS:
        matched = [c for c in matched if _matches_hand(c, hand)]

    if not matched:
        return None
    if len(matched) > 1:
        described = ", ".join(
            f"{c.names[0] if c.names else '<unnamed>'} "
            f"({getattr(c.device, 'address', '?')})"
            for c in matched
        )
        raise AmbiguousGloveError(
            f"{len(matched)} peripherals match ble_name={ble_name!r} hand={hand!r}: "
            f"{described}. Pass an explicit `address=` to say which one you mean."
        )
    return matched[0].device
