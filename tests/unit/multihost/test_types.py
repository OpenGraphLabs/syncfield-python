"""Tests for :class:`SessionAnnouncement` wire serialization."""

from __future__ import annotations

import pytest

from syncfield.multihost.types import SessionAnnouncement


class TestSessionAnnouncement:
    def test_roundtrip_txt_record(self):
        a = SessionAnnouncement(
            session_id="amber-tiger-042",
            host_id="mac_lead",
            status="recording",
            sdk_version="0.2.0",
            chirp_enabled=True,
            started_at_ns=12345,
            last_seen_ns=67890,
        )
        record = a.to_txt_record()
        b = SessionAnnouncement.from_txt_record(record, last_seen_ns=67890)
        assert b.session_id == a.session_id
        assert b.host_id == a.host_id
        assert b.status == a.status
        assert b.sdk_version == a.sdk_version
        assert b.chirp_enabled is True
        assert b.started_at_ns == 12345
        assert b.last_seen_ns == 67890

    def test_txt_values_are_all_bytes(self):
        """zeroconf expects ``{bytes: bytes}`` for ServiceInfo properties."""
        a = SessionAnnouncement(
            session_id="amber-tiger-042",
            host_id="mac_lead",
            status="preparing",
            sdk_version="0.2.0",
            chirp_enabled=False,
        )
        record = a.to_txt_record()
        for k, v in record.items():
            assert isinstance(k, bytes), f"key {k!r} not bytes"
            assert isinstance(v, bytes), f"value {v!r} not bytes"

    def test_preparing_advert_omits_started_at(self):
        a = SessionAnnouncement(
            session_id="x",
            host_id="h",
            status="preparing",
            sdk_version="0.2.0",
            chirp_enabled=True,
        )
        assert b"started_at_ns" not in a.to_txt_record()

    def test_chirp_enabled_encoded_as_0_or_1(self):
        enabled = SessionAnnouncement(
            session_id="x",
            host_id="h",
            status="preparing",
            sdk_version="0.2.0",
            chirp_enabled=True,
        ).to_txt_record()
        disabled = SessionAnnouncement(
            session_id="x",
            host_id="h",
            status="preparing",
            sdk_version="0.2.0",
            chirp_enabled=False,
        ).to_txt_record()
        assert enabled[b"chirp_enabled"] == b"1"
        assert disabled[b"chirp_enabled"] == b"0"

    def test_from_txt_record_handles_missing_keys(self):
        ann = SessionAnnouncement.from_txt_record({b"session_id": b"only"})
        assert ann.session_id == "only"
        assert ann.host_id == ""
        assert ann.status == "preparing"  # default when missing
        assert ann.chirp_enabled is False

    def test_from_txt_record_tolerates_non_numeric_started_at(self):
        """Bad ``started_at_ns`` silently falls back to ``None``."""
        ann = SessionAnnouncement.from_txt_record(
            {
                b"session_id": b"x",
                b"host_id": b"h",
                b"status": b"recording",
                b"sdk_version": b"0.2.0",
                b"chirp_enabled": b"1",
                b"started_at_ns": b"not-a-number",
            }
        )
        assert ann.started_at_ns is None

    def test_invalid_status_rejected_at_construction(self):
        with pytest.raises(ValueError, match="status"):
            SessionAnnouncement(
                session_id="x",
                host_id="h",
                status="invalid",  # type: ignore[arg-type]
                sdk_version="0.2.0",
                chirp_enabled=False,
            )

    def test_is_frozen(self):
        import dataclasses

        a = SessionAnnouncement(
            session_id="x",
            host_id="h",
            status="preparing",
            sdk_version="0.2.0",
            chirp_enabled=False,
        )
        with pytest.raises(dataclasses.FrozenInstanceError):
            a.status = "recording"  # type: ignore[misc]


class TestControlPlanePort:
    def test_default_is_none(self) -> None:
        a = SessionAnnouncement(
            session_id="sid",
            host_id="h",
            status="preparing",
            sdk_version="0.2.0",
            chirp_enabled=True,
        )
        assert a.control_plane_port is None

    def test_round_trip_when_set(self) -> None:
        a = SessionAnnouncement(
            session_id="sid",
            host_id="h",
            status="recording",
            sdk_version="0.2.0",
            chirp_enabled=True,
            control_plane_port=7878,
        )
        record = a.to_txt_record()
        # The port lives on ServiceInfo.port, not TXT — not present here.
        assert b"control_plane_port" not in record

        # Parsing doesn't touch TXT; from_txt_record leaves it None.
        parsed = SessionAnnouncement.from_txt_record(record)
        assert parsed.control_plane_port is None

    def test_explicit_field_survives_construction(self) -> None:
        a = SessionAnnouncement(
            session_id="sid",
            host_id="h",
            status="preparing",
            sdk_version="0.2.0",
            chirp_enabled=True,
            control_plane_port=55123,
        )
        assert a.control_plane_port == 55123
