"""User feedback on an immutable assistant response."""

from dataclasses import dataclass


@dataclass
class ResponseFeedback:
    """One caller's rating; timestamps are Unix microseconds."""

    conversation_id: str
    response_id: str
    user_id: str
    rating: int
    comment: str | None
    created_at: int
    updated_at: int
