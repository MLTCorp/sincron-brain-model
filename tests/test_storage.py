"""Storage round-trips: .md file <-> Memory model <-> SQLite index."""

import json
import sqlite3
from datetime import UTC, datetime, timedelta

from sincron_brain import storage
from sincron_brain.config import VaultConfig
from sincron_brain.major_tags import DEFAULT_MAJOR_TAG_NAMES
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
        tags=["matheus_massari", "sincron_ia"],
        emotion_floor=20,
        synopsis="Cofundador da Sincron.",
        content="corpo",
    )
    with storage.open_db(config) as conn:
        path = storage.write_memory(config, memory, conn)

    reloaded = storage.read_memory_file(path)
    assert reloaded.emotion_floor == 20
    assert reloaded.tags == ["matheus_massari", "sincron_ia"]
    assert isinstance(reloaded.emotion_floor, int)


def test_search_fts_any_returns_partial_matches(tmp_path):
    config = make_config(tmp_path)
    with storage.open_db(config) as conn:
        m = Memory(
            id="x", major_tags=["t"], synopsis="programa XPTO",
            tags=["access_panel"],
            content="o acesso ao programa XPTO e feito pelo painel",
        )
        storage.write_memory(config, m, conn)
        all_mode = storage.search_fts(conn, "como entro no programa XPTO")
        any_mode = storage.search_fts(conn, "como entro no programa XPTO", match_all=False)
        tag_mode = storage.search_fts(conn, "access_panel")
    assert all_mode == []  # AND-mode: requires every token, misses
    assert "x" in [h["id"] for h in any_mode]  # ANY-mode: any token, hits
    assert tag_mode[0]["tags"] == ["access_panel"]


def test_list_common_tags_and_memories_by_date(tmp_path):
    config = make_config(tmp_path)
    created = datetime(2026, 6, 13, 10, 0, tzinfo=UTC)
    memory = Memory(
        id="m-date",
        major_tags=["external_access"],
        tags=["api_key", "env_file"],
        created=created,
        last_accessed=created,
        last_scored=created,
        synopsis="API key fica no .env.",
    )
    with storage.open_db(config) as conn:
        storage.write_memory(config, memory, conn)
        tags = storage.list_common_tags(conn, "external_access")
        memories = storage.list_memories_by_date(config, conn, "2026-06-13")

    assert [tag["tag"] for tag in tags] == ["api_key", "env_file"]
    assert memories[0]["id"] == "m-date"
    assert memories[0]["tags"] == ["api_key", "env_file"]


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


def test_audit_prunes_events_older_than_retention(tmp_path):
    config = make_config(tmp_path)
    old = (datetime.now(UTC) - timedelta(days=91)).isoformat()
    recent = (datetime.now(UTC) - timedelta(days=1)).isoformat()
    config.audit_file.write_text(
        "\n".join(
            [
                json.dumps({"ts": old, "event": "old"}),
                json.dumps({"ts": recent, "event": "recent"}),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    storage.write_audit(config, "new")

    events = [event["event"] for event in storage.read_audit(config)]
    assert events == ["recent", "new"]


def test_audit_prunes_by_max_file_size(tmp_path):
    config = make_config(tmp_path)
    config.audit.max_file_mb = 1
    now = datetime.now(UTC).isoformat()
    large = "x" * 600_000
    config.audit_file.write_text(
        "\n".join(
            [
                json.dumps({"ts": now, "event": "older", "padding": large}),
                json.dumps({"ts": now, "event": "newer", "padding": large}),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    storage.write_audit(config, "newest")

    events = [event["event"] for event in storage.read_audit(config)]
    assert events == ["newer", "newest"]


def test_index_stores_emotion_floor(tmp_path):
    config = make_config(tmp_path)
    memory = Memory(id="m1", major_tags=["trabalho"], emotion_floor=10, synopsis="s")
    with storage.open_db(config) as conn:
        storage.write_memory(config, memory, conn)
        row = conn.execute(
            "SELECT emotion_floor FROM memories WHERE id = ?", (memory.id,)
        ).fetchone()
    assert row["emotion_floor"] == 10


def test_open_db_migrates_missing_tags_column(tmp_path):
    config = make_config(tmp_path)
    conn = sqlite3.connect(config.index_db)
    conn.execute(
        """
        CREATE TABLE memories (
            id TEXT PRIMARY KEY,
            major_tags TEXT NOT NULL DEFAULT '[]',
            score INTEGER NOT NULL DEFAULT 100,
            created TEXT NOT NULL,
            last_accessed TEXT NOT NULL,
            last_scored TEXT NOT NULL,
            access_count INTEGER NOT NULL DEFAULT 0,
            emotion_floor INTEGER NOT NULL DEFAULT 0,
            source_type TEXT NOT NULL DEFAULT 'text',
            asset_ref TEXT,
            go_deeper TEXT NOT NULL DEFAULT '[]',
            synopsis TEXT NOT NULL DEFAULT '',
            file_path TEXT NOT NULL
        )
        """
    )
    conn.commit()
    conn.close()

    with storage.open_db(config) as migrated:
        columns = {
            row["name"]
            for row in migrated.execute("PRAGMA table_info(memories)").fetchall()
        }

    assert "tags" in columns


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


def test_stats_includes_graph_health_metrics(tmp_path):
    config = make_config(tmp_path)
    with storage.open_db(config) as conn:
        a = Memory(
            id="a", major_tags=["projects"], synopsis="A", go_deeper=["b"]
        )
        b = Memory(
            id="b", major_tags=["projects"], synopsis="B", go_deeper=["a"]
        )
        orphan = Memory(id="orphan", major_tags=["projects"], synopsis="lonely")
        storage.write_memory(config, a, conn)
        storage.write_memory(config, b, conn)
        storage.write_memory(config, orphan, conn)
        result = storage.stats(conn)

    assert result["total"] == 3
    assert result["linked_memories"] == 2
    assert result["avg_go_deeper"] > 0
    assert result["orphan_count"] == 1
    assert result["dead_links_count"] == 0


def test_stats_counts_dead_go_deeper_links(tmp_path):
    config = make_config(tmp_path)
    with storage.open_db(config) as conn:
        broken = Memory(
            id="broken",
            major_tags=["projects"],
            synopsis="broken",
            go_deeper=["ghost-id"],
        )
        storage.write_memory(config, broken, conn)
        result = storage.stats(conn)

    assert result["dead_links_count"] == 1


def test_list_major_tags_returns_canonical_taxonomy_when_empty(tmp_path):
    config = make_config(tmp_path)
    with storage.open_db(config) as conn:
        result = storage.list_major_tags(conn)

    names = [item["major_tag"] for item in result]
    assert names == list(DEFAULT_MAJOR_TAG_NAMES)
    assert all(item["count"] == 0 for item in result)
    assert all(item["max_score"] == 0 for item in result)
    assert all(item["avg_score"] == 0.0 for item in result)


def test_list_major_tags_orders_populated_defaults_by_max_score(tmp_path):
    config = make_config(tmp_path)
    with storage.open_db(config) as conn:
        storage.write_memory(
            config, Memory(id="p", major_tags=["projects"], score=80, synopsis="p"), conn
        )
        storage.write_memory(
            config, Memory(id="s", major_tags=["soul"], score=30, synopsis="s"), conn
        )
        result = storage.list_major_tags(conn)

    assert result[0]["major_tag"] == "projects"
    assert result[0]["count"] == 1
    assert result[0]["max_score"] == 80
    assert result[1]["major_tag"] == "soul"
    assert result[1]["count"] == 1
    assert result[1]["max_score"] == 30
    remaining_names = {item["major_tag"] for item in result[2:]}
    assert remaining_names == set(DEFAULT_MAJOR_TAG_NAMES) - {"projects", "soul"}
    assert all(item["count"] == 0 for item in result[2:])
    assert len(result) == len(DEFAULT_MAJOR_TAG_NAMES)


def test_list_major_tags_appends_ad_hoc_tags_after_defaults(tmp_path):
    config = make_config(tmp_path)
    with storage.open_db(config) as conn:
        storage.write_memory(
            config, Memory(id="t", major_tags=["teste"], score=50, synopsis="t"), conn
        )
        storage.write_memory(
            config, Memory(id="s", major_tags=["soul"], score=80, synopsis="s"), conn
        )
        result = storage.list_major_tags(conn)

    assert result[0]["major_tag"] == "soul"
    assert result[-1]["major_tag"] == "teste"
    assert result[-1]["count"] == 1
    assert result[-1]["max_score"] == 50
    assert len(result) == len(DEFAULT_MAJOR_TAG_NAMES) + 1
