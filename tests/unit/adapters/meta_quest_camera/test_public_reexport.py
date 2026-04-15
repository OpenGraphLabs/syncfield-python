"""Smoke test: the adapter is visible from the top-level adapters module."""


def test_reexport_top_level():
    from syncfield.adapters import MetaQuestCameraStream
    assert MetaQuestCameraStream is not None

def test_listed_in_all():
    import syncfield.adapters as adapters
    assert "MetaQuestCameraStream" in adapters.__all__
