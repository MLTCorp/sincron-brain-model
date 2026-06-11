"""Sleep job behavior: decay respects the emotional floor."""

from datetime import UTC, datetime, timedelta

from sincron_brain import sleep, storage
from sincron_brain.config import VaultConfig
from sincron_brain.models import DraftItem, Memory, ReactivationEvent
from sincron_brain.reconcile import Decision


def make_config(tmp_path) -> VaultConfig:
    config = VaultConfig(vault_path=tmp_path)
    storage.ensure_vault(config)
    return config


def _backdate(conn, memory_id: str, days: float) -> None:
    old = (datetime.now(UTC) - timedelta(days=days)).isoformat()
    conn.execute("UPDATE memories SET last_scored = ? WHERE id = ?", (old, memory_id))
    conn.commit()


def test_decay_floors_at_emotion_floor(tmp_path):
    config = make_config(tmp_path)
    mem = Memory(id="m", major_tags=["x"], score=100, emotion_floor=20, synopsis="s")
    with storage.open_db(config) as conn:
        storage.write_memory(config, mem, conn)
        _backdate(conn, "m", days=1000)
        sleep._apply_decay(conn, config)
        row = conn.execute("SELECT score FROM memories WHERE id = ?", ("m",)).fetchone()
    assert row["score"] == 20


def test_decay_drops_to_global_floor_without_emotion(tmp_path):
    config = make_config(tmp_path)
    mem = Memory(id="m", major_tags=["x"], score=100, emotion_floor=0, synopsis="s")
    with storage.open_db(config) as conn:
        storage.write_memory(config, mem, conn)
        _backdate(conn, "m", days=1000)
        sleep._apply_decay(conn, config)
        row = conn.execute("SELECT score FROM memories WHERE id = ?", ("m",)).fetchone()
    assert row["score"] == config.score.floor


def test_run_sleep_creates_memory_and_clears_draft(tmp_path):
    config = make_config(tmp_path)
    draft = DraftItem(id="d1", content="primeira memória", hint_tags=["trabalho"])
    draft_path = storage.write_draft(config, draft)

    result = sleep.run_sleep(config)

    assert result["created"] == 1
    assert not draft_path.exists()
    with storage.open_db(config) as conn:
        assert conn.execute("SELECT COUNT(*) AS c FROM memories").fetchone()["c"] == 1


def test_run_sleep_merges_with_injected_decider(tmp_path):
    config = make_config(tmp_path)
    with storage.open_db(config) as conn:
        storage.write_memory(
            config,
            Memory(id="a", major_tags=["pessoas"], synopsis="Mateus", content="Cofundador."),
            conn,
        )
    storage.write_draft(config, DraftItem(id="d", content="Pai do Pedro.", hint_tags=["pessoas"]))

    def merge_first(_draft, candidates):
        if candidates:
            return Decision(action="merge", target_id=candidates[0].id, content="Pai do Pedro.")
        return Decision(action="create")

    result = sleep.run_sleep(config, decide=merge_first)

    assert result["merged"] == 1
    with storage.open_db(config) as conn:
        assert conn.execute("SELECT COUNT(*) AS c FROM memories").fetchone()["c"] == 1  # no dup
        body = conn.execute("SELECT content FROM memories_fts WHERE id = ?", ("a",)).fetchone()
    assert "Cofundador." in body["content"] and "Pedro" in body["content"]


def test_run_sleep_reactivates_used_memories_after_decay(tmp_path):
    config = make_config(tmp_path)
    old = datetime.now(UTC) - timedelta(days=10)
    with storage.open_db(config) as conn:
        storage.write_memory(
            config,
            Memory(
                id="a",
                major_tags=["pessoas"],
                score=30,
                last_accessed=old,
                last_scored=old,
                synopsis="Mateus",
            ),
            conn,
        )
    event_path = storage.write_reactivation(
        config,
        ReactivationEvent(id="r1", memory_ids=["a"], reason="used in answer"),
    )

    result = sleep.run_sleep(config)

    assert result["reactivated"] == 1
    assert not event_path.exists()
    with storage.open_db(config) as conn:
        row = conn.execute(
            "SELECT score, access_count FROM memories WHERE id = ?", ("a",)
        ).fetchone()
    assert row["score"] == config.score.initial
    assert row["access_count"] == 1
