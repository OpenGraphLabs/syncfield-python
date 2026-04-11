"""CLI front-end for ``syncfield.discovery``.

Run with::

    python -m syncfield.discovery             # pretty table
    python -m syncfield.discovery --json      # machine-readable
    python -m syncfield.discovery --kinds video --timeout 5

Output is grouped by Stream kind (cameras / sensors / other) so users
can scan it at a glance. Each row shows the adapter type, display name,
device id, and any warnings the adapter surfaced. Exit code is ``0``
when at least one device is found, ``2`` when none are attached but no
errors occurred, and ``1`` on partial scan failure.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Iterable, List, Sequence

# Importing ``syncfield.adapters`` is what auto-registers the built-in
# discoverers. Without it, ``scan()`` would return an empty report
# because the registry would be empty.
import syncfield.adapters  # noqa: F401  (side effect: register discoverers)

from syncfield.discovery import DiscoveredDevice, DiscoveryReport, scan


# ---------------------------------------------------------------------------
# Pretty printing
# ---------------------------------------------------------------------------


_KIND_ORDER = ("video", "audio", "sensor", "custom")
_KIND_TITLES = {
    "video": "Cameras",
    "audio": "Audio streams",
    "sensor": "Sensors",
    "custom": "Custom",
}


def _print_table(report: DiscoveryReport) -> None:
    """Human-friendly grouped listing, one section per Stream kind."""
    header = (
        f"\nSyncField discovery — found {len(report.devices)} device(s) "
        f"in {report.duration_s:.1f}s"
    )
    print(header)
    print("=" * len(header.strip()))

    for kind in _KIND_ORDER:
        devices = report.by_kind(kind)
        if not devices:
            continue
        title = _KIND_TITLES.get(kind, kind.title())
        print(f"\n{title}")
        print("-" * len(title))
        for device in devices:
            _print_device_row(device)

    if report.errors:
        print("\nErrors (partial scan):")
        for adapter_type, message in report.errors.items():
            print(f"  ! {adapter_type:20s} {message}")

    if report.timed_out:
        print("\nTimed out:")
        for adapter_type in report.timed_out:
            print(f"  ! {adapter_type}")

    if not report.devices and not report.errors and not report.timed_out:
        print("\n(no devices found — check cables, permissions, and bleak install)")

    print()


def _print_device_row(device: DiscoveredDevice) -> None:
    """Render one device in two aligned lines: headline + sub-info."""
    tag = "⚠" if device.warnings else ("◐" if device.in_use else "●")
    print(f"  {tag} {device.display_name}")

    sub_bits = [device.adapter_type]
    if device.device_id and device.device_id != device.display_name:
        sub_bits.append(device.device_id)
    if device.description:
        sub_bits.append(device.description)
    print(f"      {'  ·  '.join(sub_bits)}")

    if device.warnings:
        for warning in device.warnings:
            print(f"      ⚠ {warning}")


# ---------------------------------------------------------------------------
# JSON output
# ---------------------------------------------------------------------------


def _report_to_json(report: DiscoveryReport) -> dict:
    """Convert a DiscoveryReport into a plain JSON-friendly dict.

    The ``adapter_cls`` field is dropped because class references aren't
    serializable; callers can reconstruct by looking up ``adapter_type``.
    """
    return {
        "duration_s": report.duration_s,
        "devices": [
            {
                "adapter_type": d.adapter_type,
                "kind": d.kind,
                "display_name": d.display_name,
                "description": d.description,
                "device_id": d.device_id,
                "construct_kwargs": dict(d.construct_kwargs),
                "accepts_output_dir": d.accepts_output_dir,
                "in_use": d.in_use,
                "warnings": list(d.warnings),
            }
            for d in report.devices
        ],
        "errors": dict(report.errors),
        "timed_out": list(report.timed_out),
    }


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def _parse_kinds(raw: Sequence[str] | None) -> List[str] | None:
    if not raw:
        return None
    kinds: List[str] = []
    for item in raw:
        kinds.extend(k.strip() for k in item.split(",") if k.strip())
    return kinds or None


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m syncfield.discovery",
        description=(
            "Enumerate cameras and sensors that can be opened by "
            "SyncField on this machine."
        ),
    )
    parser.add_argument(
        "--kinds",
        nargs="*",
        help=(
            "Filter by Stream kind (video, sensor, audio, custom). "
            "Pass 'video' to skip BLE scans, for example."
        ),
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=10.0,
        help="Overall scan budget in seconds (default 10).",
    )
    parser.add_argument(
        "--no-cache",
        action="store_true",
        help="Force a fresh scan, ignoring the 5-second result cache.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print a JSON document instead of the human-friendly table.",
    )
    args = parser.parse_args(argv)

    report = scan(
        kinds=_parse_kinds(args.kinds),
        timeout=args.timeout,
        use_cache=not args.no_cache,
    )

    if args.json:
        json.dump(_report_to_json(report), sys.stdout, indent=2)
        sys.stdout.write("\n")
    else:
        _print_table(report)

    # Exit codes:
    # 0 → found at least one device
    # 2 → clean scan but zero devices (not an error, just empty)
    # 1 → partial failure (errors or timeouts), regardless of device count
    if report.errors or report.timed_out:
        return 1
    if not report.devices:
        return 2
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
