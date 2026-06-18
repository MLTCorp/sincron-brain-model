"""Vault configuration — loaded from _config.toml and environment variables."""

from __future__ import annotations

import os
import tomllib
from pathlib import Path
from typing import Literal

import tomli_w
from pydantic import BaseModel, Field

CONFIG_FILENAME = "_config.toml"

LLM_API_KEY_ENV = "LLM_API_KEY"
LLM_PROVIDER_ENV = "LLM_PROVIDER"


class JudgeConfig(BaseModel):
    """LLM-as-judge: handles sinopse, tag selection, Go Deeper, emotion weighting."""

    provider: str = "anthropic"
    model: str = "claude-haiku-4-5-20251001"
    api_key_env: str = "ANTHROPIC_API_KEY"
    max_tokens: int = 1024


class ScoreConfig(BaseModel):
    """Tuning of the cognitive scoring system."""

    initial: int = 100
    floor: int = 1
    decay_per_day: float = 1.5
    emotion_floor_increments: list[int] = Field(default_factory=lambda: [40, 20, 10, 5, 3, 2])
    emotion_bonus_max: int = 80


class SleepConfig(BaseModel):
    """When and how the indexing job runs."""

    cron: str = "0 3 * * *"
    enabled: bool = True
    batch_size: int = 50
    merge_size_threshold_chars: int = 2000


class CaptureConfig(BaseModel):
    """Default labels accepted for source_type. Free-form; this is only a hint."""

    allowed_source_types: list[str] = Field(
        default_factory=lambda: [
            "text",
            "conversation_turn",
            "user_message",
            "agent_response",
            "voice_transcript",
            "image_description",
            "web_article",
            "meeting_notes",
            "external_doc",
        ]
    )


class AuditConfig(BaseModel):
    """Local JSONL audit log for memory tool usage and sleep decisions."""

    enabled: bool = True
    retention_days: int = 90
    max_file_mb: int = 25


class VaultConfig(BaseModel):
    """Top-level vault configuration. Lives in <vault>/_config.toml."""

    vault_path: Path
    version: int = 1
    locale: str = "pt-BR"
    judge: JudgeConfig = Field(default_factory=JudgeConfig)
    score: ScoreConfig = Field(default_factory=ScoreConfig)
    sleep: SleepConfig = Field(default_factory=SleepConfig)
    capture: CaptureConfig = Field(default_factory=CaptureConfig)
    audit: AuditConfig = Field(default_factory=AuditConfig)

    @property
    def config_file(self) -> Path:
        return self.vault_path / CONFIG_FILENAME

    @property
    def index_db(self) -> Path:
        return self.vault_path / "_index.sqlite"

    @property
    def draft_dir(self) -> Path:
        return self.vault_path / "_draft"

    @property
    def reactivation_dir(self) -> Path:
        return self.vault_path / "_reactivation"

    @property
    def audit_file(self) -> Path:
        return self.vault_path / "_audit.jsonl"

    def judge_api_key(self) -> str | None:
        """Return the API key for the configured judge provider.

        Looks at the generic LLM_API_KEY first (provider-agnostic), then falls
        back to the provider-specific env var (e.g. ANTHROPIC_API_KEY). This
        lets users opt into a single canonical env var without breaking
        existing workflows that already export the provider-specific one.
        """
        return os.environ.get(LLM_API_KEY_ENV) or os.environ.get(self.judge.api_key_env)

    def judge_api_key_source(self) -> str | None:
        """Which env var was used for the key, or None if no key is set."""
        if os.environ.get(LLM_API_KEY_ENV):
            return LLM_API_KEY_ENV
        if os.environ.get(self.judge.api_key_env):
            return self.judge.api_key_env
        return None

    def save(self) -> None:
        payload: dict = {
            "version": self.version,
            "locale": self.locale,
            "judge": self.judge.model_dump(),
            "score": self.score.model_dump(),
            "sleep": self.sleep.model_dump(),
            "capture": self.capture.model_dump(),
            "audit": self.audit.model_dump(),
        }
        self.config_file.write_bytes(tomli_w.dumps(payload).encode("utf-8"))


def load_config(vault_path: Path) -> VaultConfig:
    """Load _config.toml from the given vault. Raises if missing."""
    config_file = vault_path / CONFIG_FILENAME
    if not config_file.exists():
        raise FileNotFoundError(
            f"Config not found at {config_file}. Run `sincron-brain init` first."
        )
    data = tomllib.loads(config_file.read_text(encoding="utf-8"))
    return VaultConfig(vault_path=vault_path, **data)


KnownProvider = Literal[
    "anthropic",
    "openai",
    "google",
    "voyage",
    "cohere",
    "mistral",
    "azure",
    "bedrock",
    "ollama",
    "custom",
]


PROVIDER_DEFAULT_MODEL: dict[str, str] = {
    "anthropic": "claude-haiku-4-5-20251001",
    "openai": "gpt-4o-mini",
    "google": "gemini-2.0-flash",
    "voyage": "voyage-3",
    "cohere": "command-r",
    "mistral": "mistral-small-latest",
    "azure": "gpt-4o-mini",
    "bedrock": "anthropic.claude-3-5-haiku-20241022-v1:0",
    "ollama": "llama3.2",
    "custom": "gpt-4o-mini",
}


PROVIDER_API_KEY_ENV: dict[str, str] = {
    "anthropic": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
    "google": "GEMINI_API_KEY",
    "voyage": "VOYAGE_API_KEY",
    "cohere": "COHERE_API_KEY",
    "mistral": "MISTRAL_API_KEY",
    "azure": "AZURE_API_KEY",
    "bedrock": "AWS_ACCESS_KEY_ID",
    "ollama": "OLLAMA_API_KEY",
    "custom": "CUSTOM_API_KEY",
}
