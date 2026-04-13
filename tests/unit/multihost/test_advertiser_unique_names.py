"""Two hosts in the same session must not collide on the mDNS instance name."""

from unittest.mock import MagicMock

from syncfield.multihost.advertiser import SessionAdvertiser
import syncfield.multihost.advertiser as adv_mod


def test_two_hosts_same_session_get_distinct_instance_names(monkeypatch):
    """Regression: Phase 8 generalised _maybe_start_advertising to followers,
    causing every host in the same session to claim the same mDNS instance
    name and crash on the second registrant with NonUniqueNameException."""
    captured_names: list[str] = []

    def fake_service_info_factory():
        def _factory(service_type, name, **kwargs):
            captured_names.append(name)
            return MagicMock()
        return _factory

    monkeypatch.setattr(adv_mod, "_get_service_info_cls", fake_service_info_factory)
    monkeypatch.setattr(
        adv_mod, "_get_zeroconf_cls",
        lambda: lambda: MagicMock(),
    )

    leader = SessionAdvertiser(
        session_id="amber-tiger-042",
        host_id="mac_a",
        sdk_version="0.2.0",
        chirp_enabled=True,
        control_plane_port=7878,
    )
    follower = SessionAdvertiser(
        session_id="amber-tiger-042",
        host_id="mac_b",
        sdk_version="0.2.0",
        chirp_enabled=True,
        control_plane_port=7879,
    )

    leader.start()
    follower.start()

    assert len(captured_names) == 2
    assert captured_names[0] != captured_names[1], (
        f"Same-session different-host advertisers must use distinct instance "
        f"names; got {captured_names!r}"
    )
    # Both names should still encode the session id so a tooling user
    # can grep the wire for a session.
    assert "amber-tiger-042" in captured_names[0]
    assert "amber-tiger-042" in captured_names[1]
    assert "mac_a" in captured_names[0]
    assert "mac_b" in captured_names[1]
