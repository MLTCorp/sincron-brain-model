"""Reconciliation: a draft either creates a new memory or enriches an existing
one. The decision is injected (a Decider) so the deterministic machinery is
tested without an LLM. The judge is the eventual real Decider.
"""

from sincron_brain import reconcile, storage
from sincron_brain.config import VaultConfig
from sincron_brain.models import DraftItem, Memory
from sincron_brain.reconcile import Decision


def make_config(tmp_path) -> VaultConfig:
    config = VaultConfig(vault_path=tmp_path)
    storage.ensure_vault(config)
    return config


def seed(config, conn, **kw) -> Memory:
    mem = Memory(**kw)
    storage.write_memory(config, mem, conn)
    return mem


def count(conn) -> int:
    return conn.execute("SELECT COUNT(*) AS c FROM memories").fetchone()["c"]


def test_find_candidates_by_shared_major_tag(tmp_path):
    config = make_config(tmp_path)
    with storage.open_db(config) as conn:
        seed(config, conn, id="a", major_tags=["pessoas"], synopsis="Mateus")
        seed(config, conn, id="b", major_tags=["trabalho"], synopsis="Sincron")
        draft = DraftItem(id="d", content="algo", hint_tags=["pessoas"])
        cands = reconcile.find_candidates(conn, draft, config)
    assert [c.id for c in cands] == ["a"]


def test_find_candidates_by_fts_content(tmp_path):
    config = make_config(tmp_path)
    with storage.open_db(config) as conn:
        seed(config, conn, id="x", major_tags=["t"], synopsis="programa XPTO",
             content="o acesso ao programa XPTO é feito pelo painel")
        draft = DraftItem(id="d", content="como entro no programa XPTO?")
        cands = reconcile.find_candidates(conn, draft, config)
    assert "x" in [c.id for c in cands]


def test_reconcile_create_writes_new_memory(tmp_path):
    config = make_config(tmp_path)
    draft = DraftItem(id="d", content="nova info", hint_tags=["trabalho"])

    def decide(_draft, _cands):
        return Decision(action="create", major_tags=["trabalho"], synopsis="nova")

    with storage.open_db(config) as conn:
        outcome, mem = reconcile.reconcile_draft(conn, draft, config, decide)
        assert outcome == "created"
        assert count(conn) == 1
        assert mem.synopsis == "nova"


def test_reconcile_merge_enriches_without_duplicating(tmp_path):
    config = make_config(tmp_path)
    with storage.open_db(config) as conn:
        seed(config, conn, id="a", major_tags=["pessoas"], score=40,
             synopsis="Mateus", content="Cofundador.", go_deeper=["luizao"])
        draft = DraftItem(id="d", content="Casado com a Cacau.", hint_tags=["pessoas"])

        def decide(_draft, _cands):
            return Decision(
                action="merge", target_id="a",
                synopsis="Mateus, cofundador, casado com Cacau.",
                content="Casado com a Cacau.",
                go_deeper=["cacau"],
            )

        outcome, mem = reconcile.reconcile_draft(conn, draft, config, decide)
        assert outcome == "merged"
        assert count(conn) == 1  # no duplicate created
        assert "Cofundador." in mem.content and "Cacau" in mem.content  # appended, not replaced
        assert set(mem.go_deeper) == {"luizao", "cacau"}  # unioned
        assert mem.score == config.score.initial


def test_reconcile_merge_applies_emotion_trigger(tmp_path):
    config = make_config(tmp_path)
    with storage.open_db(config) as conn:
        seed(config, conn, id="a", major_tags=["pessoas"], score=40,
             synopsis="Mateus", content="x", emotion_floor=0)
        draft = DraftItem(id="d", content="Fiquei muito grato a ele.", hint_tags=["pessoas"])

        def decide(_draft, _cands):
            return Decision(action="merge", target_id="a", content="grato", emotional=True)

        _, mem = reconcile.reconcile_draft(conn, draft, config, decide)
        assert mem.emotion_floor == 40  # first emotional feedback trigger


def test_reconcile_bloat_guard_falls_back_to_create(tmp_path):
    config = make_config(tmp_path)
    big = "x" * (config.sleep.merge_size_threshold_chars + 1)
    with storage.open_db(config) as conn:
        seed(config, conn, id="a", major_tags=["t"], synopsis="big", content=big)
        draft = DraftItem(id="d", content="mais um pedaço", hint_tags=["t"])

        def decide(_draft, _cands):
            return Decision(action="merge", target_id="a", content="mais um pedaço")

        outcome, _mem = reconcile.reconcile_draft(conn, draft, config, decide)
        assert outcome == "created"  # too large to merge → new fragment
        assert count(conn) == 2
        target = conn.execute("SELECT content FROM memories_fts WHERE id = ?", ("a",)).fetchone()
        assert target["content"] == big  # untouched
