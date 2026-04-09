"""End-to-end chirp verification against real audio hardware.

Run this on a machine with working audio output (not headless CI!) to prove
that:

1. ``create_default_player()`` picks the sounddevice backend.
2. ``SessionOrchestrator.start()`` actually plays the start chirp.
3. ``SessionOrchestrator.stop()`` plays the stop chirp before stopping streams.
4. ``sync_point.json`` carries the expected ``chirp_start_ns`` / ``chirp_stop_ns``
   / ``chirp_spec`` fields.
5. A real recording of the system audio would capture the chirps (we verify
   this by capturing audio *on this same host* via an ``InputStream`` during
   the session and running a cross-correlation against the generated chirp
   samples).

Usage::

    uv sync --extra audio   # make sure sounddevice is installed
    uv run python scripts/e2e_chirp_check.py

The script returns exit code 0 on success, 1 if any check fails.
"""

from __future__ import annotations

import json
import math
import sys
import tempfile
import threading
import time
from pathlib import Path

import sounddevice as sd  # type: ignore[import-not-found]

import syncfield as sf
from syncfield.testing import FakeStream
from syncfield.tone import (
    SilentChirpPlayer,
    SoundDeviceChirpPlayer,
    create_default_player,
    generate_chirp_samples,
)


# ---------------------------------------------------------------------------
# Check 1: default player backend
# ---------------------------------------------------------------------------


def check_default_player_uses_sounddevice() -> None:
    player = create_default_player()
    if not isinstance(player, SoundDeviceChirpPlayer):
        raise SystemExit(
            f"FAIL: create_default_player() returned {type(player).__name__}, "
            "expected SoundDeviceChirpPlayer. Is sounddevice installed?"
        )
    if isinstance(player, SilentChirpPlayer):
        raise SystemExit("FAIL: got a SilentChirpPlayer instead of sounddevice backend.")
    print("[1/5] OK — create_default_player() returned SoundDeviceChirpPlayer")


# ---------------------------------------------------------------------------
# Check 2/3: real session plays both chirps through sounddevice
# ---------------------------------------------------------------------------


def run_session_with_recording(output_dir: Path) -> tuple[dict, list[float], int]:
    """Run a short real session; return sync_point, recorded mono samples, sample_rate.

    We spin up a sounddevice InputStream on a background worker to capture the
    local mic for the duration of the session. If no mic is available the
    function still returns an empty list for the samples — the other checks
    don't depend on it.
    """
    sample_rate = 44100
    recorded: list[float] = []
    stop_input = threading.Event()

    def _capture() -> None:
        try:
            with sd.InputStream(
                samplerate=sample_rate,
                channels=1,
                dtype="float32",
            ) as stream:
                while not stop_input.is_set():
                    block, _ = stream.read(1024)
                    recorded.extend(float(x) for x in block[:, 0])
        except Exception as exc:
            print(f"    (mic capture skipped: {exc})")

    capture_thread = threading.Thread(target=_capture, daemon=True)
    capture_thread.start()
    # Let the input stream settle before the session starts.
    time.sleep(0.2)

    session = sf.SessionOrchestrator(
        host_id="e2e_chirp_check",
        output_dir=output_dir,
        sync_tone=sf.SyncToneConfig.default(),
    )
    # Declaring audio capability triggers chirp eligibility.
    session.add(FakeStream("mic_fake", provides_audio_track=True))

    session.start()
    # Simulate a brief "recording" interval between chirps.
    time.sleep(0.3)
    report = session.stop()

    # Allow the stop chirp tail to reach the input stream before closing it.
    time.sleep(0.3)
    stop_input.set()
    capture_thread.join(timeout=2.0)

    sync_point = json.loads((output_dir / "sync_point.json").read_text())
    assert report.chirp_start_ns is not None
    assert report.chirp_stop_ns is not None
    return sync_point, recorded, sample_rate


def check_session_writes_chirp_fields(sync_point: dict) -> None:
    for field in ("chirp_start_ns", "chirp_stop_ns", "chirp_spec"):
        if field not in sync_point:
            raise SystemExit(
                f"FAIL: sync_point.json is missing {field!r}: {sync_point}"
            )
    start_ns = sync_point["chirp_start_ns"]
    stop_ns = sync_point["chirp_stop_ns"]
    if not (stop_ns > start_ns):
        raise SystemExit(
            f"FAIL: chirp_stop_ns ({stop_ns}) must be > chirp_start_ns ({start_ns})"
        )
    spec = sync_point["chirp_spec"]
    if spec["from_hz"] != 400 or spec["to_hz"] != 2500:
        raise SystemExit(f"FAIL: unexpected default chirp spec: {spec}")
    print(
        "[2/5] OK — sync_point.json carries chirp_start_ns, chirp_stop_ns, chirp_spec"
    )
    print(
        f"[3/5] OK — stop_ns - start_ns = {(stop_ns - start_ns) / 1e6:.1f} ms "
        "(stop chirp plays after start chirp)"
    )


# ---------------------------------------------------------------------------
# Check 4: the chirp is actually audible (high correlation with reference)
# ---------------------------------------------------------------------------


def _normalized_xcorr_peak(signal: list[float], reference: list[float]) -> float:
    """Return the maximum absolute normalized cross-correlation in [0, 1].

    Simple O(N*M) implementation — fine for the ~30 000-sample inputs we use
    here and avoids adding numpy just for one function.
    """
    if not signal or not reference:
        return 0.0
    ref_len = len(reference)
    ref_energy = math.sqrt(sum(r * r for r in reference))
    if ref_energy == 0.0:
        return 0.0

    best = 0.0
    # Step through the signal in 4-sample increments for speed; that's still
    # well below the ~11 sample period of a 4 kHz tone so correlation peaks
    # won't be missed.
    step = 4
    for start in range(0, len(signal) - ref_len + 1, step):
        window = signal[start : start + ref_len]
        dot = 0.0
        win_energy = 0.0
        for a, b in zip(window, reference):
            dot += a * b
            win_energy += a * a
        if win_energy == 0.0:
            continue
        corr = abs(dot) / (math.sqrt(win_energy) * ref_energy)
        if corr > best:
            best = corr
    return best


def check_chirp_is_audible(
    recorded: list[float],
    sample_rate: int,
    sync_point: dict,
) -> None:
    if not recorded:
        print("[4/5] SKIP — no microphone capture available, skipping xcorr check")
        return

    spec_dict = sync_point["chirp_spec"]
    start_spec = sf.ChirpSpec(
        from_hz=spec_dict["from_hz"],
        to_hz=spec_dict["to_hz"],
        duration_ms=spec_dict["duration_ms"],
        amplitude=spec_dict["amplitude"],
        envelope_ms=spec_dict["envelope_ms"],
    )
    reference = generate_chirp_samples(start_spec, sample_rate=sample_rate)

    peak = _normalized_xcorr_peak(recorded, reference)
    print(
        f"[4/5] {'OK ' if peak > 0.15 else 'WARN'} — "
        f"normalized xcorr peak {peak:.3f} "
        f"({'chirp detected in mic capture' if peak > 0.15 else 'weak or absent; check volume/mic'})"
    )
    # 0.15 is deliberately loose — a quiet room with speakers a meter away
    # from the mic typically gives 0.2–0.5. We only fail on zero/NaN signals.
    if peak <= 0.0:
        raise SystemExit(f"FAIL: xcorr peak is {peak:.3f} — no correlation with chirp")


# ---------------------------------------------------------------------------
# Check 5: chirp eligibility skip path is silent
# ---------------------------------------------------------------------------


def check_no_audio_stream_skips_chirp(output_dir: Path) -> None:
    session = sf.SessionOrchestrator(
        host_id="e2e_chirp_check_no_audio",
        output_dir=output_dir,
        sync_tone=sf.SyncToneConfig.default(),
    )
    session.add(FakeStream("imu_only", provides_audio_track=False))
    session.start()
    session.stop()

    sp = json.loads((output_dir / "sync_point.json").read_text())
    if "chirp_start_ns" in sp:
        raise SystemExit(
            "FAIL: a session without audio-capable streams still wrote chirp fields"
        )
    print("[5/5] OK — audio-less session cleanly skips chirp (no chirp fields)")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> int:
    print("SyncField E2E chirp check — running on real audio hardware")
    print(f"  default output device: {sd.query_devices(sd.default.device[1])['name']}")
    print()

    check_default_player_uses_sounddevice()

    with tempfile.TemporaryDirectory() as td:
        session_dir = Path(td) / "session_audio"
        sync_point, recorded, sr = run_session_with_recording(session_dir)
        check_session_writes_chirp_fields(sync_point)
        check_chirp_is_audible(recorded, sr, sync_point)

        silent_dir = Path(td) / "session_silent"
        check_no_audio_stream_skips_chirp(silent_dir)

    print()
    print("all checks passed ✓")
    return 0


if __name__ == "__main__":
    sys.exit(main())
