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
                major_tags=["projeto", "api-key"],
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
    assert {"from": "a", "to": "b"} in data["go_deeper_edges"]
    assert any(tag["major_tag"] == "projeto" for tag in data["major_tags"])
    assert any(tag["tag"] == "api-key" for tag in data["tags"])
    assert data["sleeps"][0]["created"] == 1


def test_write_viewer_outputs_self_contained_html(tmp_path):
    config = make_config(tmp_path)
    with storage.open_db(config) as conn:
        storage.write_memory(
            config,
            Memory(id="a", major_tags=["debug"], synopsis="Debug", content="Conteúdo"),
            conn,
        )

    path = write_viewer(config)
    html = path.read_text(encoding="utf-8")

    assert path.name == "_viewer.html"
    assert "Sincron Brain Viewer" in html
    assert "viewer-data" in html
    assert "Conteúdo" in html


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
