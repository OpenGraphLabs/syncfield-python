"""End-to-end: real SessionOrchestrator + FakeStream mix survives one
stream failing to connect, and incidents.jsonl captures it."""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from syncfield.orchestrator import SessionOrchestrator
from syncfield.testing import FakeStream
from syncfield.types import SessionState


@pytest.mark.slow
def test_partial_connect_end_to_end(tmp_path: Path):
    sess = SessionOrchestrator(host_id="h", output_dir=tmp_path)
    sess.add(FakeStream("good_a"))
    sess.add(FakeStream("bad", fail_on_start=True))
    sess.add(FakeStream("good_b"))

    sess.connect()
    assert sess.state is SessionState.CONNECTED

    # Give the health worker a moment to ingest the startup-failure event.
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        if any(i.fingerprint == "bad:startup-failure" for i in sess.health.open_incidents()):
            break
        time.sleep(0.05)

    open_fps = [i.fingerprint for i in sess.health.open_incidents()]
    assert "bad:startup-failure" in open_fps

    sess.start(countdown_s=0)
    time.sleep(0.5)
    sess.stop()
    sess.disconnect()

    out = list(tmp_path.rglob("incidents.jsonl"))
    assert out, "no incidents.jsonl written"
    lines = [json.loads(l) for l in out[0].read_text().strip().splitlines() if l]
    fingerprints = {l["fingerprint"] for l in lines}
    assert "bad:startup-failure" in fingerprints
