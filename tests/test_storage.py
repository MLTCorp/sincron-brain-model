"""Storage round-trips: .md file <-> Memory model <-> SQLite index."""

from datetime import UTC, datetime, timedelta

from sincron_brain import storage
from sincron_brain.config import VaultConfig
from sincron_brain.models import Memory, ReactivationEvent


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


def test_audit_log_redacts_sensitive_content(tmp_path):
    config = make_config(tmp_path)
    storage.write_audit(
        config,
        "tool.remember",
        content="do not log me",
        api_key="secret",
        memory_ids=["m1"],
    )
    events = storage.read_audit(config)
    assert events[0]["event"] == "tool.remember"
    assert events[0]["content"] == "[redacted]"
    assert events[0]["api_key"] == "[redacted]"
    assert events[0]["memory_ids"] == ["m1"]


def test_audit_can_be_disabled(tmp_path):
    config = VaultConfig(vault_path=tmp_path)
    config.audit.enabled = False
    storage.ensure_vault(config)
    assert storage.write_audit(config, "tool.stats") is None
    assert storage.read_audit(config) == []


def test_index_stores_emotion_floor(tmp_path):
    config = make_config(tmp_path)
    memory = Memory(id="m1", major_tags=["trabalho"], emotion_floor=10, synopsis="s")
    with storage.open_db(config) as conn:
        storage.write_memory(config, memory, conn)
        row = conn.execute(
            "SELECT emotion_floor FROM memories WHERE id = ?", (memory.id,)
        ).fetchone()
    assert row["emotion_floor"] == 10


def test_get_memory_is_read_only(tmp_path):
    config = make_config(tmp_path)
    older = datetime.now(UTC) - timedelta(days=3)
    memory = Memory(
        id="m-read",
        major_tags=["trabalho"],
        score=40,
        access_count=0,
        last_accessed=older,
        last_scored=older,
        synopsis="s",
        content="corpo",
    )
    with storage.open_db(config) as conn:
        path = storage.write_memory(config, memory, conn)

        first = storage.get_memory(config, conn, "m-read")
        second = storage.get_memory(config, conn, "m-read")

    assert first is not None and second is not None
    assert second.access_count == 0
    assert second.score == 40
    assert second.last_accessed == older
    assert second.last_scored == older

    on_disk = storage.read_memory_file(path)
    assert on_disk.access_count == 0
    assert on_disk.score == 40


def test_reactivate_memory_sets_score_to_initial_and_syncs_md(tmp_path):
    config = make_config(tmp_path)
    older = datetime.now(UTC) - timedelta(days=3)
    memory = Memory(
        id="m-used",
        major_tags=["trabalho"],
        score=12,
        access_count=0,
        last_accessed=older,
        last_scored=older,
        synopsis="s",
    )
    with storage.open_db(config) as conn:
        path = storage.write_memory(config, memory, conn)
        reactivated = storage.reactivate_memory(config, conn, "m-used")

    assert reactivated is not None
    assert reactivated.score == config.score.initial
    assert reactivated.access_count == 1
    assert reactivated.last_accessed > older
    assert reactivated.last_scored > older

    on_disk = storage.read_memory_file(path)
    assert on_disk.score == config.score.initial
    assert on_disk.access_count == 1


def test_reactivation_queue_roundtrip(tmp_path):
    config = make_config(tmp_path)
    event = ReactivationEvent(
        id="r1",
        memory_ids=["a", "b"],
        reason="answer context",
    )
    storage.write_reactivation(config, event)
    queued = list(storage.iter_reactivations(config))
    assert len(queued) == 1
    assert queued[0][1].memory_ids == ["a", "b"]
    assert queued[0][1].reason == "answer context"


def test_get_memory_returns_none_for_missing(tmp_path):
    config = make_config(tmp_path)
    with storage.open_db(config) as conn:
        assert storage.get_memory(config, conn, "nope") is None
