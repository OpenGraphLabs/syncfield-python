"""USB Mass Storage aggregation downloader for Insta360 Go3S.

When a Go3S is connected to the host via USB-C and switched to Mass
Storage mode, its SD card mounts as a disk under ``/Volumes/`` (macOS)
or ``/media/<user>/`` (Linux). We translate the camera's BLE-reported
``sd_path`` (``/DCIM/Camera01/VID_xxx.mp4``) into a host path on that
mount and stream-copy.

Why USB and not WiFi
--------------------
Insta360 Go3S WiFi association requires either:

    * Insta360's signed iOS app (``NEHotspotConfiguration`` +
      ``INSCameraServiceSDK``), or
    * the proprietary BLE command that puts the camera's WiFi radio in
      "actively accepting" mode (not in any public RE'd protocol).

Without one of those, macOS's ``networksetup`` is rejected with
``-3925 kCWAssociationDeniedErr`` no matter how many times we retry or
how many BLE keep-alives we send. USB sidesteps the entire WiFi /
permission stack — the SD card is just a disk, copying a file is a
file system operation, no entitlements needed.
"""
from __future__ import annotations

import asyncio
import logging
import os
import shutil
import sys
from pathlib import Path
from typing import Optional

from .queue import AggregationDownloader, ChunkCallback, StageCallback
from .types import AggregationCameraSpec

_log = logging.getLogger(__name__)


class USBVolumeNotFoundError(RuntimeError):
    """Raised when no Insta360 Go3S mass-storage volume is mounted."""


class USBVolumeDownloader(AggregationDownloader):
    """Aggregation downloader that reads the Go3S SD card via USB Mass Storage."""

    #: Volume names known to belong to Go3S (case-insensitive substring match
    #: after stripping spaces, dashes, underscores). Insta360 ships under a
    #: handful of slightly different labels depending on firmware vintage.
    _NAME_HINTS = (
        "insta360",
        "insta360go3s",
        "go3s",
        "go3",
    )

    def __init__(self, *, mount_root: Optional[Path] = None):
        if mount_root is not None:
            self._mount_roots = (mount_root,)
        elif sys.platform == "darwin":
            self._mount_roots = (Path("/Volumes"),)
        elif sys.platform.startswith("linux"):
            # /media/<user> is GNOME/KDE convention; /run/media/<user> is
            # what udisks2 actually uses on most distros.
            self._mount_roots = (
                Path(f"/media/{os.environ.get('USER', '')}"),
                Path(f"/run/media/{os.environ.get('USER', '')}"),
                Path("/Volumes"),  # for macFUSE-style mounts
            )
        else:
            self._mount_roots = (Path("/Volumes"),)

    async def run(
        self,
        camera: AggregationCameraSpec,
        target_dir: Path,
        on_chunk: ChunkCallback,
        on_stage: Optional[StageCallback] = None,
    ) -> None:
        def stage(tok: str) -> None:
            if on_stage is not None:
                try:
                    on_stage(camera.stream_id, tok)
                except Exception:
                    pass

        _log.info(
            "[usb-aggregation] begin: stream=%s sd_path=%r",
            camera.stream_id, camera.sd_path,
        )

        # Step 1: locate the camera's mount.
        stage("locating_camera")
        mount = await asyncio.to_thread(self._find_mount)
        if mount is None:
            raise USBVolumeNotFoundError(
                "Insta360 Go3S not found as a mounted disk. "
                "Connect the camera to your computer with a USB-C cable. "
                "On the camera screen, when prompted, select \"USB / Mass "
                "Storage\" (NOT \"PC Connection\" or \"WebCam\"). "
                "It will appear in Finder as a disk named "
                "\"Insta360GO3S\"."
            )
        _log.info("[usb-aggregation] mount=%s", mount)

        # Step 2: resolve the file on the mount.
        host_path = self._resolve_camera_file(mount, camera.sd_path)
        if host_path is None:
            raise RuntimeError(
                f"Recording {camera.sd_path!r} not found on the camera's "
                f"SD card under {mount}. The file may have been deleted "
                f"on the camera, or the camera's date/time was reset and "
                f"renamed the file."
            )
        total_bytes = host_path.stat().st_size
        _log.info(
            "[usb-aggregation] resolved %s (%.1f MB) -> %s",
            host_path, total_bytes / (1024 * 1024),
            target_dir / camera.local_filename,
        )

        # Step 3: stream-copy with atomic rename.
        local_path = target_dir / camera.local_filename
        local_path.parent.mkdir(parents=True, exist_ok=True)
        partial = local_path.with_suffix(local_path.suffix + ".part")
        on_chunk(camera.stream_id, 0, total_bytes)
        stage("copying")
        try:
            await asyncio.to_thread(
                self._copy_with_progress,
                host_path,
                partial,
                total_bytes,
                lambda done: on_chunk(camera.stream_id, done, total_bytes),
            )
        except BaseException:
            # Don't leave a half-copied .part file lying around.
            try:
                partial.unlink(missing_ok=True)
            except Exception:
                pass
            raise

        size_on_disk = partial.stat().st_size
        if size_on_disk != total_bytes:
            partial.unlink(missing_ok=True)
            raise RuntimeError(
                f"Size mismatch on copy: got {size_on_disk}, expected "
                f"{total_bytes}. The USB connection may have dropped."
            )
        os.replace(partial, local_path)
        _log.info(
            "[usb-aggregation] done: stream=%s -> %s (%.1f MB)",
            camera.stream_id, local_path, size_on_disk / (1024 * 1024),
        )

    # ------------------------------------------------------------------
    # Mount discovery
    # ------------------------------------------------------------------

    def _find_mount(self) -> Optional[Path]:
        """Walk the mount roots looking for a Go3S volume.

        Strong match wins: a volume whose name contains an Insta360 hint
        AND has a ``DCIM/Camera01`` directory structure. Fallback: any
        volume with the ``DCIM/Camera01`` structure (covers users who
        renamed the disk or have a non-default firmware label).
        """
        strong: list[Path] = []
        weak: list[Path] = []
        for root in self._mount_roots:
            if not root.exists():
                continue
            try:
                entries = list(root.iterdir())
            except (OSError, PermissionError):
                continue
            for vol in entries:
                if not vol.is_dir():
                    continue
                try:
                    dcim = vol / "DCIM" / "Camera01"
                    if not (dcim.exists() and dcim.is_dir()):
                        continue
                except (OSError, PermissionError):
                    continue
                stripped = (
                    vol.name.lower()
                    .replace(" ", "")
                    .replace("-", "")
                    .replace("_", "")
                )
                if any(hint in stripped for hint in self._NAME_HINTS):
                    strong.append(vol)
                else:
                    weak.append(vol)
        if strong:
            return strong[0]
        if weak:
            return weak[0]
        return None

    # ------------------------------------------------------------------
    # File resolution + copy
    # ------------------------------------------------------------------

    @staticmethod
    def _resolve_camera_file(mount: Path, sd_path: str) -> Optional[Path]:
        """Translate the camera's ``/DCIM/...`` path into a host path.

        Direct join works for the common case. If the file is missing
        (e.g. the camera renamed it), fall back to a glob by basename
        within ``DCIM/Camera01/``.
        """
        rel = sd_path.lstrip("/")
        direct = mount / rel
        if direct.exists():
            return direct
        # Fallback: scan by basename. Useful when the camera's date
        # changed and the file got renamed with a different timestamp
        # prefix but the suffix (sequence number) is stable.
        basename = Path(rel).name
        camera_dir = mount / "DCIM" / "Camera01"
        if camera_dir.exists():
            matches = list(camera_dir.glob(basename))
            if matches:
                return matches[0]
        return None

    @staticmethod
    def _copy_with_progress(
        src: Path,
        dst: Path,
        total: int,
        callback,
    ) -> None:
        """Stream-copy with chunked progress reporting.

        4 MB chunks balance throughput against callback cadence. shutil
        could do this faster on macOS via copyfile clonefile fast-paths
        but those don't expose progress; we trade ~10% for the UX.
        """
        chunk_size = 4 * 1024 * 1024
        done = 0
        with src.open("rb") as r, dst.open("wb") as w:
            while True:
                buf = r.read(chunk_size)
                if not buf:
                    break
                w.write(buf)
                done += len(buf)
                callback(done)
