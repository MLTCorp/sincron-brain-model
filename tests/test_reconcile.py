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


def _single(decide_fn):
    """Wrap a legacy single-Decision stub into the new list[Decision] contract."""

    def wrapped(draft, candidates):
        return [decide_fn(draft, candidates)]

    return wrapped


def _only(results):
    """Assert the reconcile produced exactly one memory and unwrap it."""
    assert len(results) == 1
    return results[0]


def test_reconcile_create_writes_new_memory(tmp_path):
    config = make_config(tmp_path)
    draft = DraftItem(id="d", content="nova info", hint_tags=["trabalho"])

    def decide(_draft, _cands):
        return Decision(action="create", major_tags=["trabalho"], synopsis="nova")

    with storage.open_db(config) as conn:
        outcome, mem = _only(
            reconcile.reconcile_draft(conn, draft, config, _single(decide))
        )
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
        outcome, mem = _only(
            reconcile.reconcile_draft(conn, draft, config, reconcile.create_only)
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
        _, mem = _only(
            reconcile.reconcile_draft(conn, draft, config, _single(decide))
        )

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
        _, mem = _only(
            reconcile.reconcile_draft(
                conn,
                draft,
                config,
                lambda *_: [Decision(action="create")],
            )
        )

    assert mem.major_tags == ["_uncategorized"]
    assert "name" not in mem.major_tags
    assert mem.tags == ["name", "identity"]


def test_reconcile_hint_tags_become_common_tags_when_decision_has_no_tags(tmp_path):
    config = make_config(tmp_path)
    draft = DraftItem(id="d", content="deploy toda sexta", hint_tags=["schedule", "workflows"])

    with storage.open_db(config) as conn:
        _, mem = _only(
            reconcile.reconcile_draft(
                conn,
                draft,
                config,
                lambda *_: [Decision(action="create")],
            )
        )

    assert mem.major_tags == ["_uncategorized"]
    assert mem.tags == ["schedule", "workflow"]


def test_reconcile_decision_tags_take_precedence_over_hint_tags(tmp_path):
    config = make_config(tmp_path)
    draft = DraftItem(id="d", content="x", hint_tags=["fallback"])

    def decide(_draft, _cands):
        return Decision(action="create", major_tags=["soul"], tags=["preferred"])

    with storage.open_db(config) as conn:
        _, mem = _only(
            reconcile.reconcile_draft(conn, draft, config, _single(decide))
        )

    assert mem.major_tags == ["soul"]
    assert mem.tags == ["preferred"]


def test_reconcile_merge_enriches_without_duplicating(tmp_path):
    config = make_config(tmp_path)
    with storage.open_db(config) as conn:
        seed(config, conn, id="luizao", major_tags=["pessoas"], synopsis="Luizão")
        seed(config, conn, id="cacau", major_tags=["pessoas"], synopsis="Cacau")
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

        outcome, mem = _only(
            reconcile.reconcile_draft(conn, draft, config, _single(decide))
        )
        assert outcome == "merged"
        assert count(conn) == 3  # the two seeds + the target
        assert "Cofundador." in mem.content and "Cacau" in mem.content  # appended, not replaced
        assert {"luizao", "cacau"} <= set(mem.go_deeper)  # both kept (auto-FTS may add more)
        assert mem.score == config.score.initial


def test_reconcile_merge_applies_emotion_trigger(tmp_path):
    config = make_config(tmp_path)
    with storage.open_db(config) as conn:
        seed(config, conn, id="a", major_tags=["pessoas"], score=40,
             synopsis="Mateus", content="x", emotion_floor=0)
        draft = DraftItem(id="d", content="Fiquei muito grato a ele.", hint_tags=["pessoas"])

        def decide(_draft, _cands):
            return Decision(action="merge", target_id="a", content="grato", emotional=True)

        _, mem = _only(
            reconcile.reconcile_draft(conn, draft, config, _single(decide))
        )
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

        outcome, mem = _only(
            reconcile.reconcile_draft(conn, draft, config, _single(decide))
        )

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

        outcome, mem = _only(
            reconcile.reconcile_draft(conn, draft, config, _single(decide))
        )

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

        outcome, _mem = _only(
            reconcile.reconcile_draft(conn, draft, config, _single(decide))
        )
        assert outcome == "created"  # too large to merge → new fragment
        assert count(conn) == 2
        row = conn.execute("SELECT file_path FROM memories WHERE id = ?", ("a",)).fetchone()
        target = storage.read_memory_file(config.vault_path / row["file_path"])
        assert target.content == big  # untouched


def test_reconcile_decomposes_draft_into_multiple_memories(tmp_path):
    """Massari/Adamastor introduction: one combined draft → two memories,
    one per Major Tag. This is the core of the multi-decision pipeline."""
    config = make_config(tmp_path)
    draft = DraftItem(
        id="d-intro",
        content="Olá, sou Massari, quero que sejas Adamastor sempre bem-humorado.",
        hint_tags=["nome", "adamastor", "humor"],
    )

    def decide(_draft, _cands):
        return [
            Decision(
                action="create",
                major_tags=["user_profile"],
                tags=["massari"],
                synopsis="O usuário se apresenta como Massari, humano que conversa com o agente.",
                content="Nome do usuário: Massari.",
            ),
            Decision(
                action="create",
                major_tags=["soul"],
                tags=["adamastor", "persona", "humor"],
                synopsis="O agente foi batizado Adamastor: persona bem-humorada, tom leve.",
                content="Identidade do agente: Adamastor, gigante de bom humor.",
            ),
        ]

    with storage.open_db(config) as conn:
        results = reconcile.reconcile_draft(conn, draft, config, decide)

    assert len(results) == 2
    majors = sorted({mem.major_tags[0] for _, mem in results})
    assert majors == ["soul", "user_profile"]


def test_reconcile_caps_decisions_at_max_per_draft(tmp_path):
    config = make_config(tmp_path)
    draft = DraftItem(id="d", content="x")

    def explode(_draft, _cands):
        return [
            Decision(
                action="create",
                major_tags=[mt],
                synopsis=f"Sinopse muito longa pra passar do limite mínimo de caracteres: {mt}.",
                content=f"Conteúdo {mt}",
            )
            for mt in ["soul", "user_profile", "preferences", "projects", "people", "schedule"]
        ]

    with storage.open_db(config) as conn:
        results = reconcile.reconcile_draft(conn, draft, config, explode)

    assert len(results) == reconcile.MAX_DECISIONS_PER_DRAFT
    events = [e["event"] for e in storage.read_audit(config)]
    assert "sleep.decision_capped" in events


def test_reconcile_dedups_decisions_with_same_major_tag(tmp_path):
    config = make_config(tmp_path)
    draft = DraftItem(id="d", content="x")

    def duplicate(_draft, _cands):
        return [
            Decision(
                action="create",
                major_tags=["user_profile"],
                tags=["massari"],
                synopsis="Sinopse robusta sobre o nome do usuário Massari.",
                content="Usuário é Massari.",
            ),
            Decision(
                action="create",
                major_tags=["user_profile"],
                tags=["massari", "email"],
                synopsis="Sinopse robusta sobre o email do usuário Massari.",
                content="Email de Massari.",
            ),
        ]

    with storage.open_db(config) as conn:
        results = reconcile.reconcile_draft(conn, draft, config, duplicate)

    assert len(results) == 1
    events = [e["event"] for e in storage.read_audit(config)]
    assert "sleep.decision_deduped" in events


def test_reconcile_drops_dead_go_deeper_references(tmp_path):
    """The judge can hallucinate IDs; reconcile drops them and audits."""
    config = make_config(tmp_path)
    draft = DraftItem(id="d", content="Texto novo")

    def decide(_draft, _cands):
        return Decision(
            action="create",
            major_tags=["projects"],
            synopsis="Memória bem comprida com bastante texto pra passar do mínimo de chars.",
            content="Conteúdo.",
            go_deeper=["does-not-exist-xyz"],
        )

    with storage.open_db(config) as conn:
        _, mem = _only(
            reconcile.reconcile_draft(conn, draft, config, _single(decide))
        )

    assert "does-not-exist-xyz" not in mem.go_deeper
    events = [e["event"] for e in storage.read_audit(config)]
    assert "go_deeper.dropped_dead_reference" in events


def test_reconcile_drops_self_reference_in_go_deeper(tmp_path):
    config = make_config(tmp_path)
    draft = DraftItem(id="d", content="Texto novo")

    captured_id: dict = {}

    def decide(_draft, _cands):
        return Decision(
            action="create",
            major_tags=["projects"],
            synopsis="Memória bem comprida com bastante texto pra passar do mínimo de chars.",
            content="Conteúdo.",
            go_deeper=[],
        )

    with storage.open_db(config) as conn:
        # We can't predict the generated ID, so first create the memory, then
        # rebuild with a self-reference baked in via a follow-up reconcile cycle.
        _, mem = _only(
            reconcile.reconcile_draft(conn, draft, config, _single(decide))
        )
        captured_id["id"] = mem.id

        def decide_self(_draft, _cands):
            return Decision(
                action="merge",
                target_id=captured_id["id"],
                synopsis="Mais texto longo o suficiente pra passar o guard.",
                content="Mais conteúdo.",
                go_deeper=[captured_id["id"]],
            )

        followup_draft = DraftItem(id="d2", content="Segundo turno.")
        _, mem2 = _only(
            reconcile.reconcile_draft(conn, followup_draft, config, _single(decide_self))
        )

    assert captured_id["id"] not in mem2.go_deeper
    events = [e["event"] for e in storage.read_audit(config)]
    assert "go_deeper.self_reference_dropped" in events


def test_reconcile_auto_extends_go_deeper_with_fts_candidates(tmp_path):
    """When the judge omits a candidate that's clearly semantically close, the
    reconcile appends up to AUTO_FTS_GO_DEEPER_LIMIT of them automatically."""
    config = make_config(tmp_path)
    with storage.open_db(config) as conn:
        seed(
            config,
            conn,
            id="seed-projeto-x",
            major_tags=["projects"],
            synopsis="Projeto X é uma plataforma de pagamentos",
            content="Resumo do projeto x.",
            tags=["projeto_x"],
        )
        seed(
            config,
            conn,
            id="seed-billing",
            major_tags=["projects"],
            synopsis="Projeto X billing decisions",
            content="Decisões financeiras do projeto x.",
            tags=["projeto_x"],
        )

    draft = DraftItem(
        id="d",
        content="Projeto X também recebeu nova feature de webhooks.",
        hint_tags=["projeto_x"],
    )

    def decide(_draft, _cands):
        return Decision(
            action="create",
            major_tags=["projects"],
            synopsis="Nova feature de webhooks no projeto X, integra com sistema externo.",
            content="Detalhes da feature.",
            go_deeper=[],
        )

    with storage.open_db(config) as conn:
        _, mem = _only(
            reconcile.reconcile_draft(conn, draft, config, _single(decide))
        )

    assert len(mem.go_deeper) >= 1
    assert "seed-projeto-x" in mem.go_deeper or "seed-billing" in mem.go_deeper
    events = [e["event"] for e in storage.read_audit(config)]
    assert "go_deeper.auto_fts_added" in events


def test_reconcile_applies_reciprocity_back_edges(tmp_path):
    """A → B creates B → A automatically so use_memories(B) sees A too."""
    config = make_config(tmp_path)
    with storage.open_db(config) as conn:
        seed(
            config,
            conn,
            id="target-mem",
            major_tags=["projects"],
            synopsis="Memória alvo da reciprocidade",
            content="Conteúdo.",
        )

    draft = DraftItem(id="d", content="Texto novo")

    def decide(_draft, _cands):
        return Decision(
            action="create",
            major_tags=["projects"],
            synopsis="Memória nova que aponta para target-mem via go_deeper explícito.",
            content="Conteúdo.",
            go_deeper=["target-mem"],
        )

    with storage.open_db(config) as conn:
        _, new_mem = _only(
            reconcile.reconcile_draft(conn, draft, config, _single(decide))
        )
        target_row = conn.execute(
            "SELECT go_deeper FROM memories WHERE id = ?", ("target-mem",)
        ).fetchone()

    import json as _json

    target_go_deeper = _json.loads(target_row["go_deeper"])
    assert new_mem.id in target_go_deeper
    events = [e["event"] for e in storage.read_audit(config)]
    assert "go_deeper.reciprocity_added" in events


def test_reconcile_caps_go_deeper_per_memory(tmp_path):
    """A memory with many proposed go_deeper IDs gets capped at MAX_GO_DEEPER_PER_MEMORY."""
    config = make_config(tmp_path)
    with storage.open_db(config) as conn:
        for i in range(reconcile.MAX_GO_DEEPER_PER_MEMORY + 3):
            seed(
                config,
                conn,
                id=f"sibling-{i}",
                major_tags=["projects"],
                synopsis=f"Sibling {i}",
            )

    draft = DraftItem(id="d", content="x")

    def decide(_draft, _cands):
        return Decision(
            action="create",
            major_tags=["projects"],
            synopsis="Memória com lista enorme de go_deeper, esperando o cap por memória.",
            content="Conteúdo.",
            go_deeper=[
                f"sibling-{i}"
                for i in range(reconcile.MAX_GO_DEEPER_PER_MEMORY + 3)
            ],
        )

    with storage.open_db(config) as conn:
        _, mem = _only(
            reconcile.reconcile_draft(conn, draft, config, _single(decide))
        )

    assert len(mem.go_deeper) == reconcile.MAX_GO_DEEPER_PER_MEMORY
    events = [e["event"] for e in storage.read_audit(config)]
    assert "go_deeper.capped" in events


def test_reconcile_rejects_thin_decisions_when_decomposing(tmp_path):
    """When the LLM fragments into many decisions but some are empty/short,
    drop the thin ones. A single-decision draft is exempt (legacy behaviour)."""
    config = make_config(tmp_path)
    draft = DraftItem(id="d", content="x")

    def thin_and_thick(_draft, _cands):
        return [
            Decision(
                action="create",
                major_tags=["soul"],
                synopsis="Adamastor é o agente, persona bem-humorada e tom leve.",
                content="Agente é Adamastor.",
            ),
            Decision(action="create", major_tags=["user_profile"], synopsis="curta"),
        ]

    with storage.open_db(config) as conn:
        results = reconcile.reconcile_draft(conn, draft, config, thin_and_thick)

    assert len(results) == 1
    assert results[0][1].major_tags == ["soul"]
    events = [e["event"] for e in storage.read_audit(config)]
    assert "sleep.decision_rejected" in events
