"""mDNS-based multi-host session rendezvous for SyncField.

The :mod:`syncfield.multihost` subpackage lets several hosts on the
same local network coordinate around a single SyncField session
without any central coordinator. The two components are:

- :class:`SessionAdvertiser` — the leader registers a session with
  :data:`SERVICE_TYPE` and drives its status through the ``preparing
  → recording → stopped`` lifecycle.
- :class:`SessionBrowser` — every follower watches the same service
  type, filters by session id, and blocks on status transitions.

The wire format is documented in :class:`SessionAnnouncement` and is
dependency-free; only the advertiser/browser implementations actually
touch ``zeroconf``. Install with::

    pip install syncfield[multihost]

Chirps remain the *real* sync anchor — this module only removes the
"who's with whom" friction so followers know when to start recording
and when the leader has finished.

Distinct from :mod:`syncfield.discovery`, which is a separate,
parallel subsystem for *hardware* device enumeration (cameras, IMUs,
tactile sensors). One finds *peers*, the other finds *devices* —
they share nothing beyond the English word "discover".
"""

from syncfield.multihost.advertiser import SERVICE_TYPE, SessionAdvertiser
from syncfield.multihost.browser import SessionBrowser
from syncfield.multihost.naming import generate_session_id, is_valid_session_id
from syncfield.multihost.types import SessionAdvertStatus, SessionAnnouncement

__all__ = [
    "SERVICE_TYPE",
    "SessionAdvertStatus",
    "SessionAdvertiser",
    "SessionAnnouncement",
    "SessionBrowser",
    "generate_session_id",
    "is_valid_session_id",
]
