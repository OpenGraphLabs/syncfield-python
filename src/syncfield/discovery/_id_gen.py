"""Stream id generation from human-readable display names.

Discovery only knows what the hardware calls itself ("FaceTime HD Camera",
"OAK-D S2"). ``scan_and_add`` needs stable, URL-safe stream ids that don't
collide with whatever is already registered in the session. This module
does the small but persnickety normalization step.
"""

from __future__ import annotations

import re
from typing import Iterable

# Normalization rules:
#
#   "FaceTime HD Camera"     → "facetime_hd_camera"
#   "OAK-D S2"               → "oak_d_s2"
#   "oglo.glove [right]"     → "oglo_glove_right"
#   "  extra   whitespace  " → "extra_whitespace"
#
# Everything non-alphanumeric collapses to a single underscore; runs of
# multiple underscores collapse to one; leading/trailing underscores are
# stripped. The result is always a valid Python identifier prefix, which
# also happens to be safe for filesystem paths and JSONL stream-id keys.

_NON_ALNUM = re.compile(r"[^a-z0-9]+")

# Hard cap on the collision-suffix loop. If we find 100 duplicates of the
# same device name in one session, something's gone wrong upstream and a
# loud error is better than silently generating name_99_0.
_MAX_COLLISION_ATTEMPTS = 100


def normalize(name: str) -> str:
    """Return the canonical snake_case form of ``name``.

    Empty input — or input that contains only non-alphanumeric characters —
    normalizes to the literal string ``"device"`` so the caller never has
    to handle an empty id.
    """
    lowered = name.lower()
    collapsed = _NON_ALNUM.sub("_", lowered).strip("_")
    return collapsed or "device"


def make_stream_id(
    display_name: str,
    existing_ids: Iterable[str],
    *,
    prefix: str = "",
) -> str:
    """Produce a collision-free stream id from a display name.

    Args:
        display_name: Human-readable label (e.g. "FaceTime HD Camera").
        existing_ids: Stream ids already in use. Any iterable works; it's
            converted to a set internally. Pass ``session._streams.keys()``
            when adding from ``SessionOrchestrator`` state.
        prefix: Optional string prepended to the normalized name before
            collision checks — useful for namespacing ("lab01_*") when
            multiple session configs share a storage backend.

    Returns:
        A snake_case id that isn't already in ``existing_ids``. If the base
        name already exists, an ``_0``, ``_1``, ... suffix is appended
        until a free slot is found.

    Raises:
        RuntimeError: If ``_MAX_COLLISION_ATTEMPTS`` collision suffixes
            have been exhausted. Indicates a bug or pathological input.
    """
    taken = set(existing_ids)
    base = normalize(display_name)
    if prefix:
        base = f"{normalize(prefix)}_{base}"

    if base not in taken:
        return base

    for attempt in range(_MAX_COLLISION_ATTEMPTS):
        candidate = f"{base}_{attempt}"
        if candidate not in taken:
            return candidate

    raise RuntimeError(
        f"exhausted {_MAX_COLLISION_ATTEMPTS} collision suffixes for "
        f"base id {base!r} — check for a bug generating duplicate devices"
    )
