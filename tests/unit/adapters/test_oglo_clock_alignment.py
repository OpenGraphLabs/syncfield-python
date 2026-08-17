"""Behavioral tests for OGLO device-to-host clock projection."""

from syncfield.adapters.oglo.clock_alignment import HostDeviceClockProjector


def test_one_usb_batch_preserves_real_device_spacing() -> None:
    clock = HostDeviceClockProjector()

    capture = clock.project_batch(
        (("tactile", 1_000_000), ("tactile", 5_000_000), ("tactile", 9_000_000)),
        receive_ns=100_000_000,
    )

    assert capture == (92_000_000, 96_000_000, 100_000_000)
    assert [b - a for a, b in zip(capture, capture[1:])] == [4_000_000, 4_000_000]


def test_late_tty_backlog_cannot_drag_device_timeline_forward() -> None:
    clock = HostDeviceClockProjector()
    clock.project_batch((("tactile", 9_000_000),), receive_ns=100_000_000)

    capture = clock.project_batch(
        (("tactile", 13_000_000), ("tactile", 17_000_000), ("tactile", 21_000_000)),
        receive_ns=300_000_000,
    )

    assert capture == (104_000_000, 108_000_000, 112_000_000)


def test_lower_delay_reanchor_never_steps_a_published_modality_backward() -> None:
    clock = HostDeviceClockProjector()
    first = clock.project_batch((("imu", 100_000_000),), receive_ns=200_000_000)
    second = clock.project_batch((("imu", 201_000_000),), receive_ns=250_000_000)

    assert first == (200_000_000,)
    assert second[0] > first[0]


def test_reset_forgets_old_mcu_epoch() -> None:
    clock = HostDeviceClockProjector()
    clock.project_batch((("tactile", 9_000_000),), receive_ns=100_000_000)
    clock.reset()

    assert clock.offset_ns is None
    assert clock.project_batch(
        (("tactile", 1_000_000),), receive_ns=500_000_000
    ) == (500_000_000,)
