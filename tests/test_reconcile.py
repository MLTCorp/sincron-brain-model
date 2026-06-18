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


def test_reconcile_create_only_skips_candidate_lookup(tmp_path, monkeypatch):
    config = make_config(tmp_path)
    draft = DraftItem(id="d", content="nova info", hint_tags=["trabalho"])

    def fail_lookup(*_args, **_kwargs):
        raise AssertionError("create_only should not need candidates")

    monkeypatch.setattr(reconcile, "find_candidates", fail_lookup)

    with storage.open_db(config) as conn:
        outcome, mem = reconcile.reconcile_draft(
            conn,
            draft,
            config,
            reconcile.create_only,
        )

    assert outcome == "created"
    assert mem.major_tags == ["_uncategorized"]
    assert mem.tags == ["trabalho"]


def test_reconcile_uses_one_primary_major_tag_for_new_memory(tmp_path):
    config = make_config(tmp_path)
    draft = DraftItem(id="d", content="api key fica no .env")

    def decide(_draft, _cands):
        return Decision(
            action="create",
            major_tags=["external_access", "technical_context", "preferences"],
            tags=["API Keys", "api_key", "env files"],
            synopsis="API key fica no .env.",
        )

    with storage.open_db(config) as conn:
        _, mem = reconcile.reconcile_draft(conn, draft, config, decide)

    assert mem.major_tags == ["external_access"]
    assert mem.tags == ["api_key", "env_file"]


def test_reconcile_never_promotes_hint_tags_to_major_tag(tmp_path):
    """Regression: a draft with hint_tags=["name","identity"] used to land in a
    bogus Major Tag "name" because the reconcile auto-promoted the first hint.
    Now hint_tags are common tag candidates only — Major Tag must come from the
    judge's decision, and falls back to _uncategorized when absent.
    """
    config = make_config(tmp_path)
    draft = DraftItem(
        id="d",
        content="O nome do usuário é Massari.",
        hint_tags=["name", "identity"],
    )

    with storage.open_db(config) as conn:
        _, mem = reconcile.reconcile_draft(
            conn,
            draft,
            config,
            lambda *_: Decision(action="create"),
        )

    assert mem.major_tags == ["_uncategorized"]
    assert "name" not in mem.major_tags
    assert mem.tags == ["name", "identity"]


def test_reconcile_hint_tags_become_common_tags_when_decision_has_no_tags(tmp_path):
    config = make_config(tmp_path)
    draft = DraftItem(id="d", content="deploy toda sexta", hint_tags=["schedule", "workflows"])

    with storage.open_db(config) as conn:
        _, mem = reconcile.reconcile_draft(
            conn,
            draft,
            config,
            lambda *_: Decision(action="create"),
        )

    assert mem.major_tags == ["_uncategorized"]
    assert mem.tags == ["schedule", "workflow"]


def test_reconcile_decision_tags_take_precedence_over_hint_tags(tmp_path):
    config = make_config(tmp_path)
    draft = DraftItem(id="d", content="x", hint_tags=["fallback"])

    def decide(_draft, _cands):
        return Decision(action="create", major_tags=["soul"], tags=["preferred"])

    with storage.open_db(config) as conn:
        _, mem = reconcile.reconcile_draft(conn, draft, config, decide)

    assert mem.major_tags == ["soul"]
    assert mem.tags == ["preferred"]


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


def test_reconcile_refuses_merge_across_major_tags(tmp_path):
    """Massari/Adamastor regression.

    A previous run had a user_profile memory (Massari) and a soul draft
    (Adamastor). The judge proposed merge; the old reconcile honoured it and
    unioned both major_tags onto one memory, breaking the "one primary route"
    rule and hiding the agent identity from `list_tags("soul")`.
    """
    config = make_config(tmp_path)
    with storage.open_db(config) as conn:
        seed(
            config,
            conn,
            id="user-massari",
            major_tags=["user_profile"],
            synopsis="Usuário se apresenta como Massari.",
            content="Massari é o usuário.",
        )
        draft = DraftItem(
            id="d-soul",
            content="Agente foi batizado como Adamastor.",
            hint_tags=["adamastor", "persona"],
        )

        def decide(_draft, _cands):
            return Decision(
                action="merge",
                target_id="user-massari",
                major_tags=["soul"],
                synopsis="Massari e Adamastor — duas identidades.",
                content="Agente é Adamastor.",
                tags=["adamastor"],
            )

        outcome, mem = reconcile.reconcile_draft(conn, draft, config, decide)

    assert outcome == "created"
    assert mem.major_tags == ["soul"]
    target = storage.read_memory_file(
        config.vault_path
        / conn.execute(
            "SELECT file_path FROM memories WHERE id = ?", ("user-massari",)
        ).fetchone()["file_path"]
    )
    assert target.major_tags == ["user_profile"]
    assert "Adamastor" not in target.content


def test_reconcile_merge_preserves_target_major_tag(tmp_path):
    """A real same-major-tag merge must not invent extra major tags either."""
    config = make_config(tmp_path)
    with storage.open_db(config) as conn:
        seed(
            config,
            conn,
            id="massari-name",
            major_tags=["user_profile"],
            synopsis="Massari é o usuário.",
            content="Quem é o usuário: Massari.",
        )
        draft = DraftItem(id="d", content="Massari prefere respostas curtas.")

        def decide(_draft, _cands):
            return Decision(
                action="merge",
                target_id="massari-name",
                major_tags=["user_profile"],
                tags=["preference"],
                synopsis="Massari, gosta de respostas curtas.",
                content="Prefere brevidade.",
            )

        outcome, mem = reconcile.reconcile_draft(conn, draft, config, decide)

    assert outcome == "merged"
    assert mem.major_tags == ["user_profile"]
    assert "preference" in mem.tags


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
        row = conn.execute("SELECT file_path FROM memories WHERE id = ?", ("a",)).fetchone()
        target = storage.read_memory_file(config.vault_path / row["file_path"])
        assert target.content == big  # untouched
