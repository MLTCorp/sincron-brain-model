import json

from typer.testing import CliRunner

from sincron_brain.cli import app

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
