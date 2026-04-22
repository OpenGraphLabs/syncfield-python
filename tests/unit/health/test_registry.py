import pytest

from syncfield.health.detector import DetectorBase
from syncfield.health.registry import DetectorRegistry
from syncfield.health.severity import Severity


class Det(DetectorBase):
    name = "d1"
    default_severity = Severity.WARNING


class Det2(DetectorBase):
    name = "d2"
    default_severity = Severity.ERROR


def test_register_and_iterate():
    reg = DetectorRegistry()
    d1 = Det()
    d2 = Det2()
    reg.register(d1)
    reg.register(d2)
    assert list(reg) == [d1, d2]


def test_register_duplicate_name_raises():
    reg = DetectorRegistry()
    reg.register(Det())
    with pytest.raises(ValueError, match="already registered"):
        reg.register(Det())


def test_unregister_removes_by_name():
    reg = DetectorRegistry()
    d1 = Det()
    reg.register(d1)
    reg.unregister("d1")
    assert list(reg) == []


def test_unregister_unknown_is_noop():
    reg = DetectorRegistry()
    reg.unregister("nope")  # does not raise
