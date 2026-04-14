# PyAV Migration — Hardware Smoke Test

Manual verification checklist for `feat/pyav-migration`. Run on a MacBook M3/M4 (Apple Silicon, macOS 14+) with real cameras attached.

## 0. Prep

```bash
cd /Users/jerry/Documents/syncfield-python-pyav   # or wherever the worktree lives
uv sync --all-extras
uv run python -c "import av; print(av.codec.Codec('h264_videotoolbox', 'w').name)"
```

Expected last line: `h264_videotoolbox`. If it raises, VideoToolbox isn't visible to PyAV on this machine — the software encoder (`libx264`) will still work but the CPU savings below won't apply.

## 1. UVC webcam at 720p

Plug a FaceTime / Logitech / BRIO webcam. Run the example:

```bash
uv run python examples/iphone_mac_webcam/record.py --width 1280 --height 720 --fps 30 --duration 30
```

**Verify:**
- The resulting `{id}.mp4` plays in QuickTime.
- Resolution reported by `ffprobe -v error -select_streams v:0 -show_entries stream=width,height -of csv=p=0 {id}.mp4` is `1280,720`.
- Frame count: roughly 900 ± 5 (30 fps × 30 s).
- The session's `FinalizationReport` in the stdout summary shows `jitter_p95_ns` and `jitter_p99_ns` as non-null integers.

## 2. VideoToolbox hardware encoder (CPU check)

Re-run the 30-second capture from §1 while watching CPU usage:

```bash
# In another terminal:
top -pid $(pgrep -f record.py)
```

**Expected:**
- Python process CPU < 30 % throughout the recording (was > 80 % on the cv2 `mp4v` software encoder).
- No fan ramp. No thermal throttling.

If CPU is still > 80 %, PyAV fell back to `libx264` — double-check §0's VideoToolbox probe.

## 3. Multi-stream capacity

Run the multi-stream example (iPhone + Mac webcam simultaneously):

```bash
uv run python examples/iphone_mac_webcam/record.py --duration 30
```

**Expected:**
- Both streams record without frame drops.
- Each `FinalizationReport.frame_count` ≈ 900 ± 5.
- Aggregate CPU remains reasonable (< 60 % for two 720p30 streams on M3 Pro).
- Per-stream `jitter_p95_ns` is typically < 3 ms on an idle host; `jitter_p99_ns` < 6 ms. Log these numbers — they're the new baseline.

## 4. OAK camera + depth

With an OAK-D or OAK-D-Lite plugged in:

```bash
uv run python examples/mac_iphone_dual_oak/record.py --duration 30
```

**Verify:**
- RGB `{id}.mp4` is valid and plays in QuickTime.
- `{id}.depth.bin` size ≈ `depth_width × depth_height × 2 × frame_count` bytes (e.g., `640 × 400 × 2 × 900 ≈ 461 MB`).
- No warnings about missing `cv2` or `opencv-python` anywhere in stdout.

## 5. Viewer MJPEG preview

Start the viewer example and open the browser tab:

```bash
uv run python examples/iphone_mac_webcam/run.py
# Visit http://localhost:8000 in Safari / Chrome
```

**Verify:**
- Each stream card shows a live thumbnail updating at ~30 fps.
- Click Record → Stop → Record again. Second recording produces a fresh `FinalizationReport` with `frame_count` starting from 0 (confirms the counter-reset fix from Task 4 is working).
- No cv2 errors anywhere in the server logs.

## 6. Device disconnect produces a health event

Start a recording, then unplug the webcam mid-session. Check the session log:

```bash
grep -i 'capture loop ended' logs/session_*.log
```

**Expected:** one `HealthEvent` entry with `kind="error"` and a `detail` like `"capture loop ended: ..."`. (This is the observability fix from Task 4 — silent disconnects are now a first-class signal.)

## 7. Jitter report sanity

Inspect the `FinalizationReport` JSON (or Python-repr in the CLI output) for any 30-second recording:

```python
{
  "stream_id": "uvc_ego",
  "status": "completed",
  "frame_count": 900,
  ...
  "jitter_p95_ns": 2_734_112,    # ~2.7 ms
  "jitter_p99_ns": 5_891_440     # ~5.9 ms
}
```

**Expected on an idle M3 with 4 active streams:**
- `jitter_p95_ns < 3_000_000` (3 ms)
- `jitter_p99_ns < 6_000_000` (6 ms)

If either metric is consistently higher, that's the signal described in the design discussion — time to revisit multi-process capture or the PTS-based timestamp path.

## Sign-off

All seven sections must pass before `feat/pyav-migration` merges to `main`. Capture the jitter p95/p99 numbers from §3 and §7 in the PR description — they become the ongoing baseline for the sync infrastructure.
