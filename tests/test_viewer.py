from sincron_brain import storage
from sincron_brain.config import VaultConfig
from sincron_brain.models import Memory
from sincron_brain.viewer import build_viewer_data, render_viewer_html, write_viewer


def make_config(tmp_path) -> VaultConfig:
    config = VaultConfig(vault_path=tmp_path)
    storage.ensure_vault(config)
    return config


def test_build_viewer_data_includes_memory_tags_go_deeper_and_sleep(tmp_path):
    config = make_config(tmp_path)
    with storage.open_db(config) as conn:
        storage.write_memory(
            config,
            Memory(
                id="a",
                major_tags=["projeto"],
                tags=["api_key", "env_file"],
                score=88,
                emotion_floor=40,
                access_count=2,
                synopsis="API key no .env",
                content="A API key fica no arquivo .env.",
                go_deeper=["b"],
            ),
            conn,
        )
        storage.write_memory(
            config,
            Memory(
                id="b",
                major_tags=["projeto"],
                synopsis="Configuração do projeto",
                content="Configuração geral.",
            ),
            conn,
        )
    storage.write_audit(config, "sleep.started")
    storage.write_audit(
        config,
        "sleep.finished",
        processed=1,
        created=1,
        merged=0,
        reactivated=0,
        duration_seconds=0.1,
    )

    data = build_viewer_data(config)

    memories = {memory["id"]: memory for memory in data["memories"]}

    assert data["stats"]["total"] == 2
    assert memories["a"]["score"] == 88
    assert memories["a"]["emotion_floor"] == 40
    assert memories["a"]["go_deeper"] == ["b"]
    assert memories["a"]["content_omitted"] is True
    assert memories["a"]["content"] == ""
    assert data["branding"]["logo_data_uri"].startswith("data:image/jpeg;base64,")
    assert data["branding"]["developer"] == "Sincron IA"
    assert data["branding"]["author"] == "Matheus Massari"
    assert {"from": "a", "to": "b"} in data["go_deeper_edges"]
    assert any(tag["major_tag"] == "projeto" for tag in data["major_tags"])
    assert any(tag["tag"] == "api_key" for tag in data["tags"])
    assert data["sleeps"][0]["created"] == 1


def test_write_viewer_outputs_self_contained_html(tmp_path):
    config = make_config(tmp_path)
    with storage.open_db(config) as conn:
        storage.write_memory(
            config,
            Memory(id="a", major_tags=["debug"], synopsis="Debug", content="Debug full body"),
            conn,
        )

    path = write_viewer(config)
    html = path.read_text(encoding="utf-8")

    assert path.name == "_viewer.html"
    assert "Sincron Brain Viewer" in html
    assert "Desenvolvido por Sincron IA" in html
    assert "sincronia.digital" in html
    assert "Autor Matheus Massari" in html
    assert "data:image/jpeg;base64," in html
    assert "viewer-data" in html
    assert "Debug full body" not in html
    assert "Corpos das memórias omitidos" in html
    assert 'data-tab="go-deeper"' in html
    assert "relation-grid" in html
    assert "Grafo de memórias" in html
    assert "graph-stage" in html
    assert "graph-group" in html


def test_build_viewer_data_can_limit_embedded_memories(tmp_path):
    config = make_config(tmp_path)
    with storage.open_db(config) as conn:
        storage.write_memory(
            config,
            Memory(id="top", major_tags=["debug"], score=90, synopsis="Top", content="Top body"),
            conn,
        )
        storage.write_memory(
            config,
            Memory(
                id="hidden",
                major_tags=["debug"],
                tags=["hidden_tag"],
                score=10,
                synopsis="Hidden",
                content="Hidden body",
            ),
            conn,
        )

    data = build_viewer_data(config, limit=1)

    assert data["stats"]["total"] == 2
    assert data["viewer"]["displayed_memories"] == 1
    assert data["viewer"]["omitted_memories"] == 1
    assert [memory["id"] for memory in data["memories"]] == ["top"]
    assert any(tag["tag"] == "hidden_tag" for tag in data["tags"])


def test_write_viewer_summary_only_omits_memory_bodies(tmp_path):
    config = make_config(tmp_path)
    with storage.open_db(config) as conn:
        storage.write_memory(
            config,
            Memory(id="a", major_tags=["debug"], synopsis="Debug", content="Secret body"),
            conn,
        )

    path = write_viewer(config, limit=1, summary_only=True)
    html = path.read_text(encoding="utf-8")

    assert "Secret body" not in html
    assert "Corpos das memórias omitidos" in html
    assert '"summary_only": true' in html


def test_write_viewer_can_include_memory_bodies_explicitly(tmp_path):
    config = make_config(tmp_path)
    with storage.open_db(config) as conn:
        storage.write_memory(
            config,
            Memory(id="a", major_tags=["debug"], synopsis="Debug", content="Local body"),
            conn,
        )

    path = write_viewer(config, summary_only=False)
    html = path.read_text(encoding="utf-8")

    assert "Local body" in html
    assert '"summary_only": false' in html


def test_build_viewer_data_includes_judge_status(tmp_path, monkeypatch):
    config = make_config(tmp_path)
    monkeypatch.delenv(config.judge.api_key_env, raising=False)
    data = build_viewer_data(config)
    assert data["judge_status"]["provider"] == config.judge.provider
    assert data["judge_status"]["model"] == config.judge.model
    assert data["judge_status"]["ready"] is False


def test_write_viewer_renders_fallback_judge_card(tmp_path, monkeypatch):
    config = make_config(tmp_path)
    monkeypatch.delenv(config.judge.api_key_env, raising=False)
    html = write_viewer(config).read_text(encoding="utf-8")
    assert "Judge em fallback" in html
    assert "judge-card" in html


def test_render_viewer_escapes_script_end_tag():
    html = render_viewer_html(
        {
            "vault_path": "/vault",
            "generated_at": "now",
            "config": {},
            "stats": {
                "total": 0,
                "draft_queue": 0,
                "reactivation_queue": 0,
                "avg_score": 0,
                "high_score_count": 0,
            },
            "major_tags": [],
            "tags": [],
            "memories": [{"id": "x", "content": "</script>", "major_tags": []}],
            "go_deeper_edges": [],
            "sleeps": [],
            "audit": [],
            "queues": {"drafts": [], "reactivations": []},
        }
    )

    assert "<\\/script>" in html
