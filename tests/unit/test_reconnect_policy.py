"""Reconnect policy — the pure, deterministic knob set for the supervisor.

``ReconnectPolicy`` carries no state and no threads: it just answers
"is reconnect enabled?" and "how long to wait before attempt N?". Keeping
it pure makes backoff behaviour trivially testable without sleeping.
"""

from __future__ import annotations

import pytest

from syncfield.supervision import ReconnectPolicy


class TestEnabled:
    def test_default_is_disabled(self):
        # A default-constructed policy must be a no-op so existing sessions
        # keep their exact current behaviour (zero regression).
        assert ReconnectPolicy().enabled is False

    def test_disabled_classmethod(self):
        assert ReconnectPolicy.disabled().enabled is False

    def test_positive_max_attempts_enables(self):
        assert ReconnectPolicy(max_attempts=3).enabled is True


class TestBackoff:
    def test_first_attempt_uses_initial_backoff(self):
        policy = ReconnectPolicy(max_attempts=5, initial_backoff_s=0.5)
        assert policy.backoff_s(1) == pytest.approx(0.5)

    def test_backoff_grows_geometrically(self):
        policy = ReconnectPolicy(
            max_attempts=5, initial_backoff_s=0.5, multiplier=2.0
        )
        assert policy.backoff_s(1) == pytest.approx(0.5)
        assert policy.backoff_s(2) == pytest.approx(1.0)
        assert policy.backoff_s(3) == pytest.approx(2.0)

    def test_backoff_is_capped(self):
        policy = ReconnectPolicy(
            max_attempts=20,
            initial_backoff_s=1.0,
            multiplier=2.0,
            max_backoff_s=5.0,
        )
        assert policy.backoff_s(10) == pytest.approx(5.0)
