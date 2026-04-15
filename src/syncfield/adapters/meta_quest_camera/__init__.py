"""Meta Quest 3 stereo passthrough camera adapter.

Public entry point is :class:`MetaQuestCameraStream`. Internal collaborators
(``QuestHttpClient``, ``MjpegPreviewConsumer``, ``TimestampTailReader``,
``RecordingFilePuller``) live in sibling modules and are composed by the
stream class.
"""

from syncfield.adapters.meta_quest_camera.stream import MetaQuestCameraStream

__all__ = ["MetaQuestCameraStream"]
