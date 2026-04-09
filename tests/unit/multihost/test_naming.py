"""Tests for session id generation and validation."""

from __future__ import annotations

import random
import re

from syncfield.multihost.naming import generate_session_id, is_valid_session_id


class TestGenerateSessionId:
    def test_format_is_two_words_plus_number(self):
        sid = generate_session_id()
        assert re.match(r"^[a-z]+-[a-z]+-\d{3}$", sid), sid

    def test_generates_many_distinct_values(self):
        """Crude uniqueness check — a few collisions are OK, many are not."""
        values = {generate_session_id() for _ in range(50)}
        assert len(values) > 40

    def test_deterministic_when_seeded(self):
        """Same Random state → same session id (useful for reproducible tests)."""
        rng1 = random.Random(42)
        rng2 = random.Random(42)
        assert generate_session_id(rng=rng1) == generate_session_id(rng=rng2)

    def test_generated_id_passes_validator(self):
        for _ in range(20):
            assert is_valid_session_id(generate_session_id())


class TestIsValidSessionId:
    def test_accepts_user_supplied_slug(self):
        assert is_valid_session_id("kitchen-trial-042")
        assert is_valid_session_id("trial42")
        assert is_valid_session_id("a_b_c")

    def test_rejects_empty(self):
        assert not is_valid_session_id("")

    def test_rejects_whitespace(self):
        assert not is_valid_session_id("kitchen trial")
        assert not is_valid_session_id("kitchen\ttrial")

    def test_rejects_too_long(self):
        assert not is_valid_session_id("x" * 65)
        assert is_valid_session_id("x" * 64)

    def test_rejects_dot(self):
        """mDNS label separator."""
        assert not is_valid_session_id("foo.bar")

    def test_rejects_slash(self):
        """URL path separator."""
        assert not is_valid_session_id("foo/bar")

    def test_accepts_max_length(self):
        assert is_valid_session_id("a" * 64)
