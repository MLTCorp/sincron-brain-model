"""Storage round-trips: .md file <-> Memory model <-> SQLite index."""

from sincron_brain import storage
from sincron_brain.config import VaultConfig
from sincron_brain.models import Memory


def make_config(tmp_path) -> VaultConfig:
    config = VaultConfig(vault_path=tmp_path)
    storage.ensure_vault(config)
    return config


def test_memory_roundtrip_preserves_emotion_floor(tmp_path):
    config = make_config(tmp_path)
    memory = Memory(
        id="mateus-cofundador",
        major_tags=["pessoas"],
        emotion_floor=20,
        synopsis="Cofundador da Sincron.",
        content="corpo",
    )
    with storage.open_db(config) as conn:
        path = storage.write_memory(config, memory, conn)

    reloaded = storage.read_memory_file(path)
    assert reloaded.emotion_floor == 20
    assert isinstance(reloaded.emotion_floor, int)


def test_search_fts_any_returns_partial_matches(tmp_path):
    config = make_config(tmp_path)
    with storage.open_db(config) as conn:
        m = Memory(
            id="x", major_tags=["t"], synopsis="programa XPTO",
            content="o acesso ao programa XPTO e feito pelo painel",
        )
        storage.write_memory(config, m, conn)
        all_mode = storage.search_fts(conn, "como entro no programa XPTO")
        any_mode = storage.search_fts(conn, "como entro no programa XPTO", match_all=False)
    assert all_mode == []  # AND-mode: requires every token, misses
    assert "x" in [h["id"] for h in any_mode]  # ANY-mode: any token, hits


def test_index_stores_emotion_floor(tmp_path):
    config = make_config(tmp_path)
    memory = Memory(id="m1", major_tags=["trabalho"], emotion_floor=10, synopsis="s")
    with storage.open_db(config) as conn:
        storage.write_memory(config, memory, conn)
        row = conn.execute(
            "SELECT emotion_floor FROM memories WHERE id = ?", (memory.id,)
        ).fetchone()
    assert row["emotion_floor"] == 10
