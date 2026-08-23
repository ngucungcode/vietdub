"""Optional online subtitle adapters; media enters through local upload only."""

from .subtitles import CompositeSubtitleProvider, FfprobeSubtitleProbe

__all__ = ["CompositeSubtitleProvider", "FfprobeSubtitleProbe"]
