"""list_neighbors MCP tool: BFS over go_deeper edges with depth/limit caps."""

from pathlib import Path

from sincron_brain import server, storage
from sincron_brain.config import VaultConfig
from sincron_brain.models import Memory


def _bootstrap_vault(tmp_path: Path, monkeypatch) -> VaultConfig:
    config = VaultConfig(vault_path=tmp_path)
    storage.ensure_vault(config)
    config.save()
    monkeypatch.setenv("SINCRON_BRAIN_VAULT", str(tmp_path))
    server._clear_config_cache()
    return config


def _seed_chain(config: VaultConfig) -> None:
    """Build a → b → c → d (linear chain) plus an unrelated orphan."""
    with storage.open_db(config) as conn:
        for memory_id, links in [
            ("a", ["b"]),
            ("b", ["c"]),
            ("c", ["d"]),
            ("d", []),
            ("orphan", []),
        ]:
            storage.write_memory(
                config,
                Memory(
                    id=memory_id,
                    major_tags=["projects"],
                    synopsis=f"Memória {memory_id}",
                    content=f"Conteúdo de {memory_id}",
                    go_deeper=list(links),
                ),
                conn,
            )


def test_list_neighbors_depth_one_returns_direct_neighbours(tmp_path, monkeypatch):
    config = _bootstrap_vault(tmp_path, monkeypatch)
    _seed_chain(config)

    result = server.list_neighbors("a", depth=1)

    ids = [n["id"] for n in result["neighbors"]]
    assert ids == ["b"]
    assert result["neighbors"][0]["distance"] == 1


def test_list_neighbors_depth_two_returns_transitive_neighbours(tmp_path, monkeypatch):
    config = _bootstrap_vault(tmp_path, monkeypatch)
    _seed_chain(config)

    result = server.list_neighbors("a", depth=2)

    ids = [n["id"] for n in result["neighbors"]]
    distances = {n["id"]: n["distance"] for n in result["neighbors"]}
    assert set(ids) == {"b", "c"}
    assert distances["b"] == 1
    assert distances["c"] == 2


def test_list_neighbors_clamps_depth_to_safe_range(tmp_path, monkeypatch):
    config = _bootstrap_vault(tmp_path, monkeypatch)
    _seed_chain(config)

    result = server.list_neighbors("a", depth=99)

    assert result["depth"] <= 3


def test_list_neighbors_returns_empty_when_seed_missing(tmp_path, monkeypatch):
    config = _bootstrap_vault(tmp_path, monkeypatch)
    _seed_chain(config)

    result = server.list_neighbors("ghost-id", depth=2)

    assert result["neighbors"] == []
    events = [e["event"] for e in storage.read_audit(config)]
    assert "tool.list_neighbors" in events


def test_list_neighbors_audits_tool_call(tmp_path, monkeypatch):
    config = _bootstrap_vault(tmp_path, monkeypatch)
    _seed_chain(config)

    server.list_neighbors("a", depth=1)

    events = [e for e in storage.read_audit(config) if e["event"] == "tool.list_neighbors"]
    assert events
    assert events[-1]["seed_id"] == "a"
    assert events[-1]["depth"] == 1


def test_list_neighbors_orders_by_distance_then_score(tmp_path, monkeypatch):
    """At the same distance, higher-score neighbours come first."""
    config = _bootstrap_vault(tmp_path, monkeypatch)
    with storage.open_db(config) as conn:
        storage.write_memory(
            config,
            Memory(
                id="seed",
                major_tags=["projects"],
                synopsis="seed",
                content="x",
                go_deeper=["low", "high"],
            ),
            conn,
        )
        storage.write_memory(
            config,
            Memory(id="low", major_tags=["projects"], synopsis="low", score=10),
            conn,
        )
        storage.write_memory(
            config,
            Memory(id="high", major_tags=["projects"], synopsis="high", score=90),
            conn,
        )

    result = server.list_neighbors("seed", depth=1)

    ids = [n["id"] for n in result["neighbors"]]
    assert ids == ["high", "low"]
