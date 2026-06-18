"""set_judge_key and verify_judge MCP tools.

These tools exist to take the human out of the loop when an API key is
provided. Editing .env by hand worked but failed silently if the server
already had stale state — the tools persist, reload, and ping the LLM so
the agent can never report "all set" when it isn't.
"""

import os
from pathlib import Path

from sincron_brain import config as config_module
from sincron_brain import server, storage
from sincron_brain.config import (
    DOTENV_FILENAME,
    LLM_API_KEY_ENV,
    LLM_PROVIDER_ENV,
    VaultConfig,
)


def _bootstrap_vault(tmp_path: Path, monkeypatch) -> VaultConfig:
    config = VaultConfig(vault_path=tmp_path)
    storage.ensure_vault(config)
    config.save()
    monkeypatch.setenv("SINCRON_BRAIN_VAULT", str(tmp_path))
    server._clear_config_cache()
    return config


def test_set_judge_key_rejects_empty(tmp_path, monkeypatch):
    _bootstrap_vault(tmp_path, monkeypatch)
    result = server.set_judge_key(api_key="   ")
    assert result["ready"] is False
    assert result["error"] == "empty_key"


def test_set_judge_key_detects_provider_from_prefix(tmp_path, monkeypatch):
    _bootstrap_vault(tmp_path, monkeypatch)
    monkeypatch.setattr(
        "sincron_brain.judge._litellm_completion",
        lambda _config: (lambda _msgs: "OK"),
    )

    result = server.set_judge_key(api_key="sk-ant-test-deadbeef")

    assert result["ready"] is True
    assert result["provider"] == "anthropic"
    dotenv = (tmp_path / DOTENV_FILENAME).read_text(encoding="utf-8")
    assert f"{LLM_API_KEY_ENV}=sk-ant-test-deadbeef" in dotenv
    assert f"{LLM_PROVIDER_ENV}=anthropic" in dotenv
    cfg_text = (tmp_path / "_config.toml").read_text(encoding="utf-8")
    assert 'provider = "anthropic"' in cfg_text


def test_set_judge_key_requires_explicit_provider_for_opaque_keys(tmp_path, monkeypatch):
    _bootstrap_vault(tmp_path, monkeypatch)
    result = server.set_judge_key(api_key="opaque-mistral-token")
    assert result["ready"] is False
    assert result["error"] == "provider_unknown"


def test_set_judge_key_with_explicit_provider_persists(tmp_path, monkeypatch):
    _bootstrap_vault(tmp_path, monkeypatch)
    monkeypatch.setattr(
        "sincron_brain.judge._litellm_completion",
        lambda _config: (lambda _msgs: "OK"),
    )

    result = server.set_judge_key(
        api_key="opaque-mistral-token", provider="mistral"
    )

    assert result["ready"] is True
    assert result["provider"] == "mistral"
    cfg_text = (tmp_path / "_config.toml").read_text(encoding="utf-8")
    assert 'provider = "mistral"' in cfg_text


def test_set_judge_key_returns_ready_false_when_ping_fails(tmp_path, monkeypatch):
    _bootstrap_vault(tmp_path, monkeypatch)

    def boom(_config):
        def _do(_msgs):
            raise RuntimeError("401 invalid_api_key")

        return _do

    monkeypatch.setattr("sincron_brain.judge._litellm_completion", boom)

    result = server.set_judge_key(api_key="sk-ant-broken")

    assert result["ready"] is False
    assert "invalid_api_key" in result["message"]
    events = [e["event"] for e in storage.read_audit(server.get_config())]
    assert "judge.ping_failed" in events
    assert "tool.set_judge_key" in events


def test_set_judge_key_never_writes_key_to_audit(tmp_path, monkeypatch):
    _bootstrap_vault(tmp_path, monkeypatch)
    monkeypatch.setattr(
        "sincron_brain.judge._litellm_completion",
        lambda _config: (lambda _msgs: "OK"),
    )

    server.set_judge_key(api_key="sk-ant-super-secret-do-not-log")

    raw = (tmp_path / "_audit.jsonl").read_text(encoding="utf-8")
    assert "sk-ant-super-secret-do-not-log" not in raw


def test_verify_judge_returns_not_ready_without_key(tmp_path, monkeypatch):
    _bootstrap_vault(tmp_path, monkeypatch)
    result = server.verify_judge()
    assert result["ready"] is False
    assert result["error"] == "api_key_missing"


def test_verify_judge_returns_ready_with_live_completion(tmp_path, monkeypatch):
    _bootstrap_vault(tmp_path, monkeypatch)
    monkeypatch.setenv(LLM_API_KEY_ENV, "sk-ant-x")
    monkeypatch.setenv(LLM_PROVIDER_ENV, "anthropic")
    monkeypatch.setattr(
        "sincron_brain.judge._litellm_completion",
        lambda _config: (lambda _msgs: "OK"),
    )

    result = server.verify_judge()

    assert result["ready"] is True
    assert result["reply_preview"] == "OK"
    events = [e["event"] for e in storage.read_audit(server.get_config())]
    assert "judge.ping_ok" in events


def test_load_dotenv_refreshes_keys_it_previously_wrote(tmp_path, monkeypatch):
    """The bug that hid the Massari OpenAI key: once load_dotenv put a value
    into os.environ, editing the file did not update it because the skip-if-set
    rule could not tell file-owned from shell-owned."""
    dotenv = tmp_path / DOTENV_FILENAME
    monkeypatch.delenv(LLM_API_KEY_ENV, raising=False)
    config_module._DOTENV_LOADED_KEYS.discard(LLM_API_KEY_ENV)

    dotenv.write_text(f"{LLM_API_KEY_ENV}=first\n", encoding="utf-8")
    config_module.load_dotenv(tmp_path)
    assert os.environ[LLM_API_KEY_ENV] == "first"

    dotenv.write_text(f"{LLM_API_KEY_ENV}=second\n", encoding="utf-8")
    config_module.load_dotenv(tmp_path)
    assert os.environ[LLM_API_KEY_ENV] == "second"


def test_load_dotenv_does_not_clobber_shell_set_values(tmp_path, monkeypatch):
    dotenv = tmp_path / DOTENV_FILENAME
    config_module._DOTENV_LOADED_KEYS.discard(LLM_API_KEY_ENV)
    monkeypatch.setenv(LLM_API_KEY_ENV, "from-shell")

    dotenv.write_text(f"{LLM_API_KEY_ENV}=from-file\n", encoding="utf-8")
    config_module.load_dotenv(tmp_path)
    assert os.environ[LLM_API_KEY_ENV] == "from-shell"
