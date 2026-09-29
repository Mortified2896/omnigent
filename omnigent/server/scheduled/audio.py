"""Stable speech policy for scheduled responses, including legacy sessions."""

SCHEDULED_AUDIO_BACKEND = "kokoro"
DEFAULT_SCHEDULED_AUDIO_VOICE = "kokoro-heart"


def scheduled_audio_voice(profile: str) -> str:
    """Upgrade the original Qwen daily-brief profile without changing recordings."""
    return DEFAULT_SCHEDULED_AUDIO_VOICE if profile == "daily-brief" else profile
