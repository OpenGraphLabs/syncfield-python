from syncfield.types import StreamCapabilities


def test_live_preview_defaults_to_true():
    caps = StreamCapabilities()
    assert caps.live_preview is True


def test_live_preview_can_be_disabled():
    caps = StreamCapabilities(live_preview=False)
    assert caps.live_preview is False


def test_to_dict_includes_live_preview():
    caps = StreamCapabilities(live_preview=False)
    d = caps.to_dict()
    assert d["live_preview"] is False
    assert d["produces_file"] is False
