"""Data models for memories and frontmatter."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, Field


def _utcnow() -> datetime:
    return datetime.now(UTC)


class Memory(BaseModel):
    """A single memory card.

    Stored on disk as a .md file with YAML frontmatter (this model) + body.
    The `synopsis` field is the queryable cabeçalho — ~300-400 chars, dense.
    The `content` is the full body.
    """

    id: str
    major_tags: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    score: int = 100
    created: datetime = Field(default_factory=_utcnow)
    last_accessed: datetime = Field(default_factory=_utcnow)
    last_scored: datetime = Field(default_factory=_utcnow)
    access_count: int = 0
    emotion_floor: int = 0
    source_type: str = "text"
    asset_ref: str | None = None
    go_deeper: list[str] = Field(default_factory=list)
    synopsis: str = ""
    content: str = ""

    def frontmatter(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "major_tags": self.major_tags,
            "tags": self.tags,
            "score": self.score,
            "created": self.created.isoformat(),
            "last_accessed": self.last_accessed.isoformat(),
            "last_scored": self.last_scored.isoformat(),
            "access_count": self.access_count,
            "emotion_floor": self.emotion_floor,
            "source_type": self.source_type,
            "asset_ref": self.asset_ref,
            "go_deeper": self.go_deeper,
            "synopsis": self.synopsis,
        }


class DraftItem(BaseModel):
    """A raw piece of content waiting to be processed at the next sleep."""

    id: str
    content: str
    source_type: str = "text"
    asset_ref: str | None = None
    hint_tags: list[str] = Field(default_factory=list)
    timestamp: datetime = Field(default_factory=_utcnow)
    metadata: dict[str, Any] = Field(default_factory=dict)
    user_message: str | None = None
    agent_response: str | None = None
    memory_reason: str | None = None


class ReactivationEvent(BaseModel):
    """A record that specific memories were used in an answer context."""

    id: str
    memory_ids: list[str]
    reason: str = ""
    timestamp: datetime = Field(default_factory=_utcnow)
