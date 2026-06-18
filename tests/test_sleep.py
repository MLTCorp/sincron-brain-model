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
    events = storage.read_audit(config)
    assert "sleep.started" in [event["event"] for event in events]
    assert "sleep.draft_processed" in [event["event"] for event in events]
    assert "sleep.finished" in [event["event"] for event in events]
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
            return [
                Decision(action="merge", target_id=candidates[0].id, content="Pai do Pedro.")
            ]
        return [Decision(action="create")]

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
    events = storage.read_audit(config)
    assert "sleep.memory_decayed" in [event["event"] for event in events]
    assert "sleep.memory_reactivated" in [event["event"] for event in events]
    with storage.open_db(config) as conn:
        row = conn.execute(
            "SELECT score, access_count FROM memories WHERE id = ?", ("a",)
        ).fetchone()
    assert row["score"] == config.score.initial
    assert row["access_count"] == 1


def test_sleep_simulates_days_emotion_floor_and_reactivation(tmp_path):
    config = make_config(tmp_path)
    storage.write_draft(
        config,
        DraftItem(
            id="d-emotional",
            content="Usuario corrigiu a IA: eu ja disse que a API key fica no arquivo .env.",
            hint_tags=["projeto"],
        ),
    )

    def emotional_create(_draft, _candidates):
        return [
            Decision(
                action="create",
                major_tags=["projeto"],
                synopsis="API key fica no arquivo .env.",
                emotional=True,
            )
        ]

    sleep.run_sleep(config, decide=emotional_create)

    with storage.open_db(config) as conn:
        row = conn.execute(
            "SELECT id, score, emotion_floor FROM memories"
        ).fetchone()
        memory_id = row["id"]
        assert row["score"] == config.score.initial
        assert row["emotion_floor"] == 40
        _backdate(conn, memory_id, days=1000)

    sleep.run_sleep(config)

    with storage.open_db(config) as conn:
        decayed = conn.execute(
            "SELECT score, emotion_floor, access_count FROM memories WHERE id = ?",
            (memory_id,),
        ).fetchone()
        assert decayed["score"] == 40
        assert decayed["emotion_floor"] == 40
        assert decayed["access_count"] == 0

    storage.write_reactivation(
        config,
        ReactivationEvent(id="r-emotional", memory_ids=[memory_id], reason="used correction"),
    )
    sleep.run_sleep(config)

    with storage.open_db(config) as conn:
        reactivated = conn.execute(
            "SELECT score, emotion_floor, access_count FROM memories WHERE id = ?",
            (memory_id,),
        ).fetchone()
        assert reactivated["score"] == config.score.initial
        assert reactivated["emotion_floor"] == 40
        assert reactivated["access_count"] == 1
        _backdate(conn, memory_id, days=19)

    sleep.run_sleep(config)

    with storage.open_db(config) as conn:
        later = conn.execute(
            "SELECT score, emotion_floor, access_count FROM memories WHERE id = ?",
            (memory_id,),
        ).fetchone()

    assert later["score"] == 71
    assert later["emotion_floor"] == 40
    assert later["access_count"] == 1


def test_sleep_decomposes_multi_major_draft_into_separate_memories(tmp_path):
    """One draft → two memories, one per Major Tag. Audit shows decomposition."""
    config = make_config(tmp_path)
    storage.write_draft(
        config,
        DraftItem(
            id="d-intro",
            content="Olá, sou Massari, quero que sejas Adamastor sempre bem-humorado.",
            hint_tags=["nome", "adamastor", "humor"],
        ),
    )

    def split(_draft, _candidates):
        return [
            Decision(
                action="create",
                major_tags=["user_profile"],
                tags=["massari"],
                synopsis="Usuário se apresenta como Massari, identidade durável do humano.",
                content="Nome do usuário: Massari.",
            ),
            Decision(
                action="create",
                major_tags=["soul"],
                tags=["adamastor", "persona", "humor"],
                synopsis="Agente foi batizado Adamastor, persona bem-humorada, tom leve.",
                content="Identidade do agente: Adamastor, gigante de bom humor.",
            ),
        ]

    result = sleep.run_sleep(config, decide=split)

    assert result["created"] == 2
    assert result["merged"] == 0
    assert result["processed"] == 2

    events = storage.read_audit(config)
    event_names = [e["event"] for e in events]
    assert "sleep.draft_decomposed" in event_names
    decomposed = next(e for e in events if e["event"] == "sleep.draft_decomposed")
    assert decomposed["total_decisions"] == 2
    assert decomposed["draft_id"] == "d-intro"

    processed_events = [e for e in events if e["event"] == "sleep.draft_processed"]
    assert len(processed_events) == 2
    assert {e["decision_index"] for e in processed_events} == {0, 1}

    import json as _json

    with storage.open_db(config) as conn:
        majors = {
            _json.loads(row[0])[0]
            for row in conn.execute("SELECT major_tags FROM memories")
        }
    assert majors == {"user_profile", "soul"}


def test_sleep_uses_compiled_context_for_conversation_turn(tmp_path):
    config = make_config(tmp_path)
    storage.write_draft(
        config,
        DraftItem(
            id="d-turn",
            content="Contexto consolidado do turno: API key fica no .env.",
            source_type="conversation_turn",
            user_message="Droga, ja falei que a API key fica no .env.",
            agent_response="Desculpe, vou lembrar.",
            memory_reason="Correção do usuário: API key fica no .env; não perguntar de novo.",
            hint_tags=["projeto"],
        ),
    )

    def compiled_create(_draft, _candidates):
        return [
            Decision(
                action="create",
                major_tags=["projeto"],
                synopsis="Usuário já corrigiu a IA sobre a API key no .env.",
                content=(
                    "O usuário já corrigiu a IA por perguntar repetidamente onde fica a API key. "
                    "Neste projeto, considerar que a API key fica no arquivo .env e evitar "
                    "perguntar novamente."
                ),
                emotional=True,
            )
        ]

    sleep.run_sleep(config, decide=compiled_create)

    with storage.open_db(config) as conn:
        row = conn.execute("SELECT id, file_path, emotion_floor FROM memories").fetchone()
        memory = storage.read_memory_file(config.vault_path / row["file_path"])

    assert row["emotion_floor"] == 40
    assert "API key fica no arquivo .env" in memory.content
    assert "Droga" not in memory.content
    assert "Desculpe" not in memory.content
