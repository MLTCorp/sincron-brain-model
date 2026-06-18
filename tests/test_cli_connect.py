import json
import os

from typer.testing import CliRunner

from sincron_brain.cli import _detect_provider_from_env, _detect_provider_from_key, app
from sincron_brain.config import (
    DOTENV_FILENAME,
    LLM_API_KEY_ENV,
    LLM_PROVIDER_ENV,
    PROVIDER_API_KEY_ENV,
    load_config,
    load_dotenv,
)

runner = CliRunner()


def test_connect_creates_vault_and_project_mcp_config(tmp_path):
    project = tmp_path / "project"
    vault = tmp_path / "memory"
    project.mkdir()

    result = runner.invoke(
        app,
        ["connect", "--path", str(vault), "--project", str(project)],
    )

    assert result.exit_code == 0
    assert (vault / "_config.toml").exists()
    assert (vault / "_index.sqlite").exists()

    payload = json.loads((project / ".mcp.json").read_text(encoding="utf-8"))
    server = payload["mcpServers"]["sincron-brain"]
    assert server == {
        "command": "sincron-brain",
        "args": ["serve"],
        "env": {"SINCRON_BRAIN_VAULT": str(vault.resolve())},
    }
    settings = json.loads(
        (project / ".claude" / "settings.local.json").read_text(encoding="utf-8")
    )
    assert settings["enabledMcpjsonServers"] == ["sincron-brain"]
    assert "use_memories(ids)" in (project / "AGENTS.md").read_text(encoding="utf-8")
    assert "remember_turn(user_message" in (project / "AGENTS.md").read_text(encoding="utf-8")
    assert 'list_tags("soul")' in (project / "AGENTS.md").read_text(encoding="utf-8")
    assert 'list_tags("preferences")' in (project / "AGENTS.md").read_text(encoding="utf-8")
    assert "Major Tags are primary retrieval routes" in (
        project / "AGENTS.md"
    ).read_text(encoding="utf-8")
    assert "list_common_tags(major_tag)" in (project / "AGENTS.md").read_text(encoding="utf-8")
    assert "snake_case singular tags" in (project / "AGENTS.md").read_text(encoding="utf-8")
    assert "soul" in (project / "AGENTS.md").read_text(encoding="utf-8")
    assert "schedule" in (project / "AGENTS.md").read_text(encoding="utf-8")
    assert "use_memories(ids)" in (project / "CLAUDE.md").read_text(encoding="utf-8")
    assert "remember_turn(user_message" in (project / "CLAUDE.md").read_text(encoding="utf-8")


def test_connect_preserves_existing_mcp_servers_and_syncs_claude_settings(tmp_path):
    project = tmp_path / "project"
    vault = tmp_path / "memory"
    project.mkdir()
    (project / ".claude").mkdir()
    (project / ".mcp.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "other": {
                        "command": "other-tool",
                        "args": ["serve"],
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    (project / ".claude" / "settings.local.json").write_text(
        json.dumps(
            {
                "someOtherSetting": True,
                "enabledMcpjsonServers": ["google-news-trends", "other"],
            }
        ),
        encoding="utf-8",
    )

    result = runner.invoke(
        app,
        ["connect", "--path", str(vault), "--project", str(project)],
    )

    assert result.exit_code == 0
    payload = json.loads((project / ".mcp.json").read_text(encoding="utf-8"))
    assert payload["mcpServers"]["other"]["command"] == "other-tool"
    assert payload["mcpServers"]["sincron-brain"]["command"] == "sincron-brain"
    settings = json.loads(
        (project / ".claude" / "settings.local.json").read_text(encoding="utf-8")
    )
    assert settings["someOtherSetting"] is True
    assert settings["enabledMcpjsonServers"] == ["other", "sincron-brain"]


def test_connect_updates_existing_agent_instruction_files_idempotently(tmp_path):
    project = tmp_path / "project"
    vault = tmp_path / "memory"
    project.mkdir()
    (project / "AGENTS.md").write_text(
        "# Existing instructions\n\nKeep this line.\n",
        encoding="utf-8",
    )

    first = runner.invoke(
        app,
        ["connect", "--path", str(vault), "--project", str(project)],
    )
    second = runner.invoke(
        app,
        ["connect", "--path", str(vault), "--project", str(project)],
    )

    assert first.exit_code == 0
    assert second.exit_code == 0
    agents = (project / "AGENTS.md").read_text(encoding="utf-8")
    assert "Keep this line." in agents
    assert agents.count("sincron-brain-memory:start") == 1
    assert "CLAUDE.md" not in {path.name for path in project.iterdir()}


def test_connect_defaults_vault_to_project_local_memory_dir(tmp_path):
    project = tmp_path / "project"
    project.mkdir()

    result = runner.invoke(app, ["connect", "--project", str(project)])

    assert result.exit_code == 0
    expected_vault = (project / "memory").resolve()
    assert (expected_vault / "_config.toml").exists()
    payload = json.loads((project / ".mcp.json").read_text(encoding="utf-8"))
    server = payload["mcpServers"]["sincron-brain"]
    assert server["env"]["SINCRON_BRAIN_VAULT"] == str(expected_vault)


def test_connect_generates_initial_viewer(tmp_path):
    project = tmp_path / "project"
    vault = tmp_path / "memory"
    project.mkdir()

    result = runner.invoke(
        app,
        ["connect", "--path", str(vault), "--project", str(project)],
    )

    assert result.exit_code == 0
    viewer = vault / "_viewer.html"
    assert viewer.exists()
    html = viewer.read_text(encoding="utf-8")
    assert "Sincron Brain Viewer" in html
    assert "soul" in html


def test_detect_provider_from_env_picks_first_set_key(monkeypatch):
    for env_var in PROVIDER_API_KEY_ENV.values():
        monkeypatch.delenv(env_var, raising=False)
    assert _detect_provider_from_env() is None

    monkeypatch.setenv("OPENAI_API_KEY", "x")
    assert _detect_provider_from_env() == "openai"


def test_connect_uses_provider_detected_from_env(tmp_path, monkeypatch):
    project = tmp_path / "project"
    vault = tmp_path / "memory"
    project.mkdir()
    for env_var in PROVIDER_API_KEY_ENV.values():
        monkeypatch.delenv(env_var, raising=False)
    monkeypatch.setenv("GEMINI_API_KEY", "x")

    result = runner.invoke(
        app,
        ["connect", "--path", str(vault), "--project", str(project)],
    )

    assert result.exit_code == 0
    config_text = (vault / "_config.toml").read_text(encoding="utf-8")
    assert 'provider = "google"' in config_text
    assert 'api_key_env = "GEMINI_API_KEY"' in config_text


def test_judge_provider_override_env_var_wins_over_detection(tmp_path, monkeypatch):
    project = tmp_path / "project"
    vault = tmp_path / "memory"
    project.mkdir()
    for env_var in PROVIDER_API_KEY_ENV.values():
        monkeypatch.delenv(env_var, raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
    monkeypatch.setenv("SINCRON_BRAIN_JUDGE_PROVIDER", "openai")

    result = runner.invoke(
        app,
        ["connect", "--path", str(vault), "--project", str(project)],
    )

    assert result.exit_code == 0
    config_text = (vault / "_config.toml").read_text(encoding="utf-8")
    assert 'provider = "openai"' in config_text
    assert 'api_key_env = "OPENAI_API_KEY"' in config_text


def test_judge_model_override_env_var_sets_custom_model(tmp_path, monkeypatch):
    project = tmp_path / "project"
    vault = tmp_path / "memory"
    project.mkdir()
    for env_var in PROVIDER_API_KEY_ENV.values():
        monkeypatch.delenv(env_var, raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "x")
    monkeypatch.setenv("SINCRON_BRAIN_JUDGE_MODEL", "gpt-5-custom-tag")

    result = runner.invoke(
        app,
        ["connect", "--path", str(vault), "--project", str(project)],
    )

    assert result.exit_code == 0
    config_text = (vault / "_config.toml").read_text(encoding="utf-8")
    assert 'model = "gpt-5-custom-tag"' in config_text


def test_set_judge_updates_existing_vault_without_recreating(tmp_path):
    project = tmp_path / "project"
    vault = tmp_path / "memory"
    project.mkdir()

    runner.invoke(app, ["connect", "--path", str(vault), "--project", str(project)])
    before = (vault / "_config.toml").read_text(encoding="utf-8")

    result = runner.invoke(
        app,
        ["set-judge", "--provider", "mistral", "--model", "mistral-large"],
        env={"SINCRON_BRAIN_VAULT": str(vault)},
    )

    assert result.exit_code == 0
    after = (vault / "_config.toml").read_text(encoding="utf-8")
    assert 'provider = "mistral"' in after
    assert 'model = "mistral-large"' in after
    assert 'api_key_env = "MISTRAL_API_KEY"' in after
    assert before != after


def test_connect_existing_vault_with_provider_flag_updates_judge(tmp_path):
    project = tmp_path / "project"
    vault = tmp_path / "memory"
    project.mkdir()

    runner.invoke(app, ["connect", "--path", str(vault), "--project", str(project)])
    result = runner.invoke(
        app,
        ["connect", "--path", str(vault), "--project", str(project), "--provider", "ollama"],
    )

    assert result.exit_code == 0
    config_text = (vault / "_config.toml").read_text(encoding="utf-8")
    assert 'provider = "ollama"' in config_text
    assert 'api_key_env = "OLLAMA_API_KEY"' in config_text


def test_set_judge_auto_detects_provider_from_env(tmp_path, monkeypatch):
    project = tmp_path / "project"
    vault = tmp_path / "memory"
    project.mkdir()
    for env_var in PROVIDER_API_KEY_ENV.values():
        monkeypatch.delenv(env_var, raising=False)
    runner.invoke(app, ["connect", "--path", str(vault), "--project", str(project)])

    monkeypatch.setenv("MISTRAL_API_KEY", "x")
    result = runner.invoke(
        app, ["set-judge", "--auto"], env={"SINCRON_BRAIN_VAULT": str(vault)}
    )

    assert result.exit_code == 0
    config_text = (vault / "_config.toml").read_text(encoding="utf-8")
    assert 'provider = "mistral"' in config_text


def test_set_judge_auto_fails_when_no_key_in_env(tmp_path, monkeypatch):
    project = tmp_path / "project"
    vault = tmp_path / "memory"
    project.mkdir()
    runner.invoke(app, ["connect", "--path", str(vault), "--project", str(project)])

    for env_var in PROVIDER_API_KEY_ENV.values():
        monkeypatch.delenv(env_var, raising=False)
    result = runner.invoke(
        app, ["set-judge", "--auto"], env={"SINCRON_BRAIN_VAULT": str(vault)}
    )

    assert result.exit_code != 0
    assert "No supported provider API key found" in result.output


def test_set_judge_requires_auto_or_provider(tmp_path):
    project = tmp_path / "project"
    vault = tmp_path / "memory"
    project.mkdir()
    runner.invoke(app, ["connect", "--path", str(vault), "--project", str(project)])

    result = runner.invoke(app, ["set-judge"], env={"SINCRON_BRAIN_VAULT": str(vault)})

    assert result.exit_code != 0
    assert "--auto" in result.output


def test_detect_provider_from_key_recognises_anthropic_prefix():
    assert _detect_provider_from_key("sk-ant-abc123") == "anthropic"


def test_detect_provider_from_key_recognises_openai_variants():
    assert _detect_provider_from_key("sk-proj-abc") == "openai"
    assert _detect_provider_from_key("sk-svcacct-abc") == "openai"
    assert _detect_provider_from_key("sk-1234") == "openai"


def test_detect_provider_from_key_recognises_google_and_bedrock_prefixes():
    assert _detect_provider_from_key("AIzaSyAbc") == "google"
    assert _detect_provider_from_key("AKIAIOSFODNN7") == "bedrock"
    assert _detect_provider_from_key("ASIAIOSFODNN7") == "bedrock"


def test_detect_provider_from_key_returns_none_for_opaque_keys():
    assert _detect_provider_from_key("opaque-mistral-token-1234") is None
    assert _detect_provider_from_key("") is None


def test_connect_uses_llm_api_key_with_anthropic_prefix(tmp_path, monkeypatch):
    project = tmp_path / "project"
    vault = tmp_path / "memory"
    project.mkdir()
    for env_var in PROVIDER_API_KEY_ENV.values():
        monkeypatch.delenv(env_var, raising=False)
    monkeypatch.delenv(LLM_PROVIDER_ENV, raising=False)
    monkeypatch.setenv(LLM_API_KEY_ENV, "sk-ant-test-key")

    result = runner.invoke(
        app, ["connect", "--path", str(vault), "--project", str(project)]
    )

    assert result.exit_code == 0
    config_text = (vault / "_config.toml").read_text(encoding="utf-8")
    assert 'provider = "anthropic"' in config_text
    assert "looks like anthropic key" in result.output


def test_connect_uses_llm_api_key_with_explicit_llm_provider(tmp_path, monkeypatch):
    """Opaque keys (Cohere, Mistral, Voyage) need LLM_PROVIDER to disambiguate."""
    project = tmp_path / "project"
    vault = tmp_path / "memory"
    project.mkdir()
    for env_var in PROVIDER_API_KEY_ENV.values():
        monkeypatch.delenv(env_var, raising=False)
    monkeypatch.setenv(LLM_API_KEY_ENV, "opaque-token")
    monkeypatch.setenv(LLM_PROVIDER_ENV, "mistral")

    result = runner.invoke(
        app, ["connect", "--path", str(vault), "--project", str(project)]
    )

    assert result.exit_code == 0
    config_text = (vault / "_config.toml").read_text(encoding="utf-8")
    assert 'provider = "mistral"' in config_text


def test_judge_api_key_prefers_llm_api_key_over_provider_specific(tmp_path, monkeypatch):
    """LLM_API_KEY is the canonical env var. Provider-specific is fallback."""
    project = tmp_path / "project"
    vault = tmp_path / "memory"
    project.mkdir()
    for env_var in PROVIDER_API_KEY_ENV.values():
        monkeypatch.delenv(env_var, raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "legacy-specific")
    runner.invoke(app, ["connect", "--path", str(vault), "--project", str(project)])

    monkeypatch.setenv(LLM_API_KEY_ENV, "canonical-generic")
    from sincron_brain.config import load_config

    config = load_config(vault)
    assert config.judge_api_key() == "canonical-generic"
    assert config.judge_api_key_source() == LLM_API_KEY_ENV


def test_connect_creates_env_template_and_gitignore(tmp_path, monkeypatch):
    project = tmp_path / "project"
    vault = tmp_path / "memory"
    project.mkdir()
    for env_var in PROVIDER_API_KEY_ENV.values():
        monkeypatch.delenv(env_var, raising=False)

    result = runner.invoke(
        app, ["connect", "--path", str(vault), "--project", str(project)]
    )

    assert result.exit_code == 0
    env_file = vault / DOTENV_FILENAME
    assert env_file.exists()
    contents = env_file.read_text(encoding="utf-8")
    assert "LLM_API_KEY" in contents
    assert "LLM_PROVIDER" in contents
    gi = vault / ".gitignore"
    assert gi.exists()
    assert ".env" in gi.read_text(encoding="utf-8")


def test_connect_does_not_overwrite_existing_env_file(tmp_path, monkeypatch):
    project = tmp_path / "project"
    vault = tmp_path / "memory"
    project.mkdir()
    vault.mkdir()
    custom_env = "LLM_API_KEY=keep-me\n"
    (vault / DOTENV_FILENAME).write_text(custom_env, encoding="utf-8")

    runner.invoke(app, ["connect", "--path", str(vault), "--project", str(project)])

    assert (vault / DOTENV_FILENAME).read_text(encoding="utf-8") == custom_env


def test_load_dotenv_populates_env_without_clobbering_existing(tmp_path, monkeypatch):
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / DOTENV_FILENAME).write_text(
        "# comment line\n"
        "LLM_API_KEY=from-file\n"
        "LLM_PROVIDER=mistral\n"
        '# QUOTED="value"\n'
        "QUOTED_VAL=\"with spaces\"\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    monkeypatch.delenv("QUOTED_VAL", raising=False)
    monkeypatch.setenv("LLM_API_KEY", "from-shell")  # shell-set must win

    loaded = load_dotenv(vault)

    assert os.environ["LLM_API_KEY"] == "from-shell"
    assert os.environ["LLM_PROVIDER"] == "mistral"
    assert os.environ["QUOTED_VAL"] == "with spaces"
    assert "LLM_PROVIDER" in loaded
    assert "LLM_API_KEY" not in loaded  # shell already set it; .env skipped


def test_load_config_applies_dotenv_so_judge_sees_the_key(tmp_path, monkeypatch):
    project = tmp_path / "project"
    vault = tmp_path / "memory"
    project.mkdir()
    for env_var in PROVIDER_API_KEY_ENV.values():
        monkeypatch.delenv(env_var, raising=False)
    monkeypatch.delenv(LLM_API_KEY_ENV, raising=False)
    runner.invoke(app, ["connect", "--path", str(vault), "--project", str(project)])

    (vault / DOTENV_FILENAME).write_text(
        f"{LLM_API_KEY_ENV}=sk-ant-from-env-file\n", encoding="utf-8"
    )

    config = load_config(vault)
    assert config.judge_api_key() == "sk-ant-from-env-file"
    assert config.judge_api_key_source() == LLM_API_KEY_ENV


def test_connect_fallback_message_points_at_vault_dotenv(tmp_path, monkeypatch):
    project = tmp_path / "project"
    vault = tmp_path / "memory"
    project.mkdir()
    for env_var in PROVIDER_API_KEY_ENV.values():
        monkeypatch.delenv(env_var, raising=False)
    monkeypatch.delenv(LLM_API_KEY_ENV, raising=False)
    monkeypatch.delenv(LLM_PROVIDER_ENV, raising=False)

    result = runner.invoke(
        app, ["connect", "--path", str(vault), "--project", str(project)]
    )

    assert result.exit_code == 0
    assert "FALLBACK MODE" in result.output
    assert str(vault / DOTENV_FILENAME) in result.output
    assert LLM_API_KEY_ENV in result.output
    assert "sincron-brain set-judge --auto" in result.output


def test_set_judge_rejects_unsupported_provider(tmp_path):
    project = tmp_path / "project"
    vault = tmp_path / "memory"
    project.mkdir()
    runner.invoke(app, ["connect", "--path", str(vault), "--project", str(project)])

    result = runner.invoke(
        app,
        ["set-judge", "--provider", "fakeai"],
        env={"SINCRON_BRAIN_VAULT": str(vault)},
    )

    assert result.exit_code != 0
    assert "Unsupported provider" in result.output


def test_stats_uses_local_project_mcp_config(tmp_path, monkeypatch):
    project = tmp_path / "project"
    vault = tmp_path / "memory"
    project.mkdir()

    connect = runner.invoke(
        app,
        ["connect", "--path", str(vault), "--project", str(project)],
    )
    assert connect.exit_code == 0

    monkeypatch.delenv("SINCRON_BRAIN_VAULT", raising=False)
    monkeypatch.chdir(project)
    stats = runner.invoke(app, ["stats"])

    assert stats.exit_code == 0
    assert "Total memories" in stats.output
