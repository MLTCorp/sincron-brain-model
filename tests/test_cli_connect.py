import json

from typer.testing import CliRunner

from sincron_brain.cli import _detect_provider_from_env, app
from sincron_brain.config import PROVIDER_API_KEY_ENV

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


def test_connect_fallback_message_lists_all_provider_envs(tmp_path, monkeypatch):
    project = tmp_path / "project"
    vault = tmp_path / "memory"
    project.mkdir()
    for env_var in PROVIDER_API_KEY_ENV.values():
        monkeypatch.delenv(env_var, raising=False)

    result = runner.invoke(
        app, ["connect", "--path", str(vault), "--project", str(project)]
    )

    assert result.exit_code == 0
    for env_var in PROVIDER_API_KEY_ENV.values():
        assert env_var in result.output
    assert "FALLBACK MODE" in result.output
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
