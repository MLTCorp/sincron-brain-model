"""End-to-end: write_draft → run_sleep → navigate → queue reactivation → run_sleep.

Walks the cognitive loop the host app sees: a piece of content gets queued,
the sleep job classifies it, the agent navigates Major Tag → Tag, reads the
memory neutrally, queues real answer-use, and the next sleep reactivates it.
Nothing here talks to a real LLM provider — the Decider is a deterministic stub.
"""

from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from sincron_brain import sleep, storage
from sincron_brain.config import VaultConfig
from sincron_brain.models import DraftItem, ReactivationEvent
from sincron_brain.reconcile import Decision


def make_config(tmp_path) -> VaultConfig:
    config = VaultConfig(vault_path=tmp_path)
    storage.ensure_vault(config)
    return config


def _stub_judge_for(major_tag: str, synopsis: str) -> Callable[..., Decision]:
    def decide(_draft, _candidates):
        return Decision(
            action="create",
            major_tags=[major_tag],
            synopsis=synopsis,
        )
    return decide


def test_full_loop_remember_sleep_navigate_use_reactivate(tmp_path):
    config = make_config(tmp_path)
    storage.write_draft(
        config,
        DraftItem(
            id="d-mateus",
            content="Mateus é cofundador da Sincron, casado com Cacau, pai do Pedro.",
            hint_tags=["pessoas"],
        ),
    )

    result = sleep.run_sleep(
        config,
        decide=_stub_judge_for("pessoas", "Cofundador da Sincron, marido da Cacau, pai do Pedro."),
    )
    assert result["created"] == 1 and result["merged"] == 0

    with storage.open_db(config) as conn:
        majors = storage.list_major_tags(conn)
        assert any(m["major_tag"] == "pessoas" for m in majors)

        tags_under = storage.list_tags(conn, "pessoas")
        assert len(tags_under) == 1
        memory_id = tags_under[0]["id"]

        first = storage.get_memory(config, conn, memory_id)
        second = storage.get_memory(config, conn, memory_id)

    assert first is not None and second is not None
    assert second.access_count == 0
    assert second.score == config.score.initial

    storage.write_reactivation(
        config,
        ReactivationEvent(id="r", memory_ids=[memory_id], reason="answer used Pedro context"),
    )
    result = sleep.run_sleep(config, decide=_stub_judge_for("unused", "unused"))
    assert result["reactivated"] == 1

    with storage.open_db(config) as conn:
        reactivated = storage.get_memory(config, conn, memory_id)

    on_disk = storage.read_memory_file(config.vault_path / "pessoas" / f"{memory_id}.md")
    assert reactivated is not None
    assert on_disk.access_count == 1
    assert on_disk.score == config.score.initial
    assert "Pedro" in on_disk.content


def test_reactivation_is_applied_after_decay(tmp_path):
    config = make_config(tmp_path)
    storage.write_draft(config, DraftItem(id="d", content="memória qualquer", hint_tags=["t"]))
    sleep.run_sleep(config, decide=_stub_judge_for("t", "sinopse"))

    with storage.open_db(config) as conn:
        memory_id = conn.execute("SELECT id FROM memories").fetchone()["id"]
        row = conn.execute(
            "SELECT file_path FROM memories WHERE id = ?", (memory_id,)
        ).fetchone()
        path = config.vault_path / row["file_path"]
        mem = storage.read_memory_file(path)
        mem.score = 30
        mem.last_scored = datetime.now(UTC) - timedelta(days=10)
        storage.write_memory(config, mem, conn)

        storage.write_reactivation(config, ReactivationEvent(id="r", memory_ids=[memory_id]))

    sleep.run_sleep(config, decide=_stub_judge_for("unused", "unused"))

    with storage.open_db(config) as conn:
        after_decay = conn.execute(
            "SELECT score FROM memories WHERE id = ?", (memory_id,)
        ).fetchone()["score"]

    assert after_decay == config.score.initial
