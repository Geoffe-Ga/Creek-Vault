"""Voice analysis pipeline for the Creek system.

Provides tools for collecting voice exemplars, extracting patterns,
and generating voice register profile notes with proxy prompts.
"""

from creek.voice.analyzer import VoiceAnalyzer, VoicePattern, VoiceProfile

__all__ = ["VoiceAnalyzer", "VoicePattern", "VoiceProfile"]
