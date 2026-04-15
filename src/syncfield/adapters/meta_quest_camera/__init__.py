"""Meta Quest 3 stereo passthrough camera adapter (streaming-only).

Public entry point is :class:`MetaQuestCameraStream`. Internal
collaborators live in sibling modules and are composed by the stream
class:

* :mod:`.preview` — :class:`MjpegPreviewConsumer` that pulls one
  ``/preview/{eye}`` MJPEG stream per eye.
* :mod:`.mp4_writer` — :class:`StreamingVideoRecorder` that mux-passes
  each JPEG into an MP4 container without re-encoding.
* :mod:`.http_client` — :class:`QuestHttpClient` for the small set of
  control-plane calls (``/status``, ``/tracker/target``).
"""

from syncfield.adapters.meta_quest_camera.stream import MetaQuestCameraStream

__all__ = ["MetaQuestCameraStream"]
