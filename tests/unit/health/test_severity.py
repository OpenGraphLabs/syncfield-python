from syncfield.health.severity import Severity, max_severity


def test_severity_values():
    assert Severity.INFO.value == "info"
    assert Severity.WARNING.value == "warning"
    assert Severity.ERROR.value == "error"
    assert Severity.CRITICAL.value == "critical"


def test_severity_ordering():
    # INFO < WARNING < ERROR < CRITICAL
    order = [Severity.INFO, Severity.WARNING, Severity.ERROR, Severity.CRITICAL]
    for a, b in zip(order, order[1:]):
        assert a.rank < b.rank


def test_max_severity_picks_highest():
    assert max_severity(Severity.INFO, Severity.WARNING) == Severity.WARNING
    assert max_severity(Severity.ERROR, Severity.WARNING) == Severity.ERROR
    assert max_severity(Severity.CRITICAL, Severity.INFO, Severity.ERROR) == Severity.CRITICAL


def test_max_severity_requires_at_least_one():
    import pytest
    with pytest.raises(ValueError):
        max_severity()
