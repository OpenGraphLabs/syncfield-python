"""Session ID generation and validation for multi-host rendezvous.

Session ids are short, typeable, Docker-style slugs so operators can
read them aloud, scan them from a QR code, or type them into a second
device. The generator uses a small hand-curated wordlist (no external
dependency) biased toward physical-ai-adjacent imagery so a printed
or spoken id still feels at home in a lab setting.

The validator is stricter than the generator: it accepts any ASCII
slug up to 64 characters so users may supply their own ids
(``kitchen-trial-042``) while rejecting anything that would break the
mDNS label grammar (``.``, ``/``) or the JSONL manifest (whitespace).
"""

from __future__ import annotations

import random
import re
from typing import Optional

_ADJECTIVES = [
    "agile", "amber", "bold", "brave", "bright", "calm", "clever", "cosmic",
    "crisp", "daring", "deep", "eager", "electric", "fast", "fierce", "frosty",
    "gentle", "gleaming", "golden", "graceful", "humble", "kind", "lively",
    "lucid", "lucky", "mighty", "nimble", "noble", "quiet", "quick", "rapid",
    "rising", "robust", "sharp", "silver", "sleek", "solar", "steady",
    "stellar", "swift", "tidal", "vivid", "warm", "wild", "zesty",
]
_NOUNS = [
    "atlas", "beacon", "breeze", "canyon", "cedar", "comet", "cove", "dawn",
    "delta", "ember", "falcon", "fjord", "forge", "harbor", "harvest",
    "helix", "horizon", "journey", "lagoon", "lantern", "meadow", "mesa",
    "nebula", "ocean", "orbit", "prairie", "quartz", "reef", "ridge",
    "river", "signal", "spiral", "storm", "stream", "summit", "sunrise",
    "tempo", "terra", "tide", "tiger", "trail", "valley", "voyage",
    "whisper", "zenith",
]

#: Slug grammar: 1–64 characters of ASCII alphanumerics, ``-``, or ``_``.
#: Disallows ``.`` (mDNS label separator), ``/`` (URL separator), and
#: whitespace so ids round-trip through both channels untouched.
_SLUG_RE = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")


def generate_session_id(rng: Optional[random.Random] = None) -> str:
    """Return a new Docker-style session id (e.g. ``pouring-tiger-042``).

    Args:
        rng: Optional :class:`random.Random` instance for deterministic
            generation in tests. Defaults to the module-global RNG.

    The generated id is a three-part slug ``{adjective}-{noun}-{NNN}``
    where ``NNN`` is a zero-padded integer in ``[0, 999]``, giving
    roughly ``len(ADJECTIVES) * len(NOUNS) * 1000 ≈ 2M`` combinations.
    That's enough that two operators in the same lab will not collide
    by accident, while staying short enough to read aloud.
    """
    r = rng if rng is not None else random
    adj = r.choice(_ADJECTIVES)
    noun = r.choice(_NOUNS)
    num = r.randint(0, 999)
    return f"{adj}-{noun}-{num:03d}"


def is_valid_session_id(session_id: str) -> bool:
    """Return ``True`` if *session_id* is a legal slug.

    Rules:

    - non-empty
    - at most 64 characters
    - ASCII alphanumerics plus ``-`` and ``_`` only
    - no whitespace, no ``.`` (mDNS label separator), no ``/``

    Used by :class:`LeaderRole`, :class:`FollowerRole`, and
    :class:`SessionAdvertiser` to reject malformed ids at construction
    time so failures surface at the API boundary rather than during
    mDNS registration.
    """
    if not session_id:
        return False
    return bool(_SLUG_RE.match(session_id))
