"""Wire types for mDNS-based multi-host session rendezvous.

These types are the contract between a :class:`SessionAdvertiser`
(running on the leader host) and a :class:`SessionBrowser` (running on
every follower host). The module is deliberately dependency-free —
only the advertiser and browser modules touch ``zeroconf`` — so
``syncfield.multihost.types`` stays importable on machines that
haven't installed the ``multihost`` extra.

The :class:`SessionAnnouncement` dataclass doubles as:

- the source of truth the leader holds for the current session state, and
- the parsed representation a follower reconstructs from a TXT record.

It is **never** used by the chirp-anchored alignment math itself — that
happens post-hoc from the audio tracks. These types only carry enough
information for followers to know *which* leader to attach to and
*when* the leader is recording vs. stopped.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Mapping, Optional

SessionAdvertStatus = Literal["preparing", "recording", "stopped"]
"""Lifecycle phase advertised by a leader to prospective followers.

- ``"preparing"`` — the leader has registered the service but streams
  are still being brought up; followers should wait.
- ``"recording"`` — the leader has entered the ``RECORDING`` state;
  followers may start their own streams and expect the start chirp
  imminently.
- ``"stopped"`` — the leader has played the stop chirp and finalized
  streams; followers should stop if they have not already.
"""

_VALID_STATUSES = frozenset({"preparing", "recording", "stopped"})


@dataclass(frozen=True)
class SessionAnnouncement:
    """One leader's session advert, as propagated over an mDNS TXT record.

    Attributes:
        session_id: Shared identifier across leader and all followers.
        host_id: The leader's host id (different from ``session_id``).
            Followers persist this into their manifest so the sync core
            can reconstruct the leader/follower relationship after the
            fact.
        status: Current lifecycle phase.
        sdk_version: Leader's syncfield SDK version string.
        chirp_enabled: Whether the leader will play sync chirps. When
            ``False`` followers know inter-host precision will fall
            back to coarse timestamp alignment.
        started_at_ns: Leader's ``time.monotonic_ns()`` at the moment
            it transitioned to ``recording``, or ``None`` while still
            preparing. This value lives in the leader's clock domain
            and must not be compared directly to a follower's clock.
        last_seen_ns: Follower-side field only: the local monotonic ns
            of the most recent TXT update that refreshed this
            announcement. The leader ignores this when building a
            record; the browser sets it when parsing one.
    """

    session_id: str
    host_id: str
    status: SessionAdvertStatus
    sdk_version: str
    chirp_enabled: bool
    started_at_ns: Optional[int] = None
    last_seen_ns: Optional[int] = None

    def __post_init__(self) -> None:
        if self.status not in _VALID_STATUSES:
            raise ValueError(
                "SessionAnnouncement.status must be one of "
                f"{sorted(_VALID_STATUSES)}; got {self.status!r}"
            )

    def to_txt_record(self) -> dict[bytes, bytes]:
        """Serialize to a ``zeroconf``-compatible TXT record (bytes→bytes).

        ``last_seen_ns`` is never written — it is follower-local.
        ``started_at_ns`` is included only when set so the
        ``preparing`` advert stays minimal.
        """
        record: dict[bytes, bytes] = {
            b"session_id": self.session_id.encode("utf-8"),
            b"host_id": self.host_id.encode("utf-8"),
            b"status": self.status.encode("utf-8"),
            b"sdk_version": self.sdk_version.encode("utf-8"),
            b"chirp_enabled": (b"1" if self.chirp_enabled else b"0"),
        }
        if self.started_at_ns is not None:
            record[b"started_at_ns"] = str(self.started_at_ns).encode("utf-8")
        return record

    @classmethod
    def from_txt_record(
        cls,
        record: Mapping[bytes, bytes | None],
        *,
        last_seen_ns: Optional[int] = None,
    ) -> "SessionAnnouncement":
        """Reconstruct from a ``zeroconf`` TXT record.

        Missing or empty TXT values fall back to defaults so that
        partial advertisements still yield a usable announcement —
        callers can then decide whether to trust it based on the
        reconstructed fields. Any value that is not a ``bytes``
        instance is coerced via ``repr`` rather than raised, keeping
        the parser resilient to oddball peer implementations.

        Raises:
            ValueError: If the reconstructed ``status`` is not one of
                the legal values. This is the only hard failure — it
                catches wire-level corruption where the parser cannot
                safely downgrade.
        """

        def _get(key: bytes) -> str:
            val = record.get(key)
            if val is None:
                return ""
            if isinstance(val, bytes):
                return val.decode("utf-8", errors="replace")
            return str(val)

        started_raw = _get(b"started_at_ns")
        started: Optional[int] = int(started_raw) if started_raw.isdigit() else None
        status = _get(b"status") or "preparing"
        return cls(
            session_id=_get(b"session_id"),
            host_id=_get(b"host_id"),
            status=status,  # type: ignore[arg-type]
            sdk_version=_get(b"sdk_version"),
            chirp_enabled=_get(b"chirp_enabled") == "1",
            started_at_ns=started,
            last_seen_ns=last_seen_ns,
        )
