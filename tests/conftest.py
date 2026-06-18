"""Shared test fixtures.

Tests must not see stray API key env vars leaking from previous tests or from
the developer's shell — load_dotenv mutates os.environ, so a vault created in
one test could leave LLM_API_KEY set for the next. The autouse fixture below
scrubs all known judge-key env vars before each test runs; tests that need a
specific value just monkeypatch.setenv it.
"""

from __future__ import annotations

import pytest

from sincron_brain.config import (
    LLM_API_KEY_ENV,
    LLM_PROVIDER_ENV,
    PROVIDER_API_KEY_ENV,
)


@pytest.fixture(autouse=True)
def _clear_judge_envs(monkeypatch):
    for env_var in PROVIDER_API_KEY_ENV.values():
        monkeypatch.delenv(env_var, raising=False)
    monkeypatch.delenv(LLM_API_KEY_ENV, raising=False)
    monkeypatch.delenv(LLM_PROVIDER_ENV, raising=False)
    monkeypatch.delenv("SINCRON_BRAIN_JUDGE_PROVIDER", raising=False)
    monkeypatch.delenv("SINCRON_BRAIN_JUDGE_MODEL", raising=False)


@pytest.fixture(autouse=True)
def _sync_viewer_refresh_in_tests(monkeypatch):
    """Force the viewer refresh to be synchronous so tests can read the file
    immediately after the helper returns. Production keeps the async path."""
    monkeypatch.setenv("SINCRON_BRAIN_SYNC_VIEWER", "1")
