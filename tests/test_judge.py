"""LLM judge: builds the prompt, parses the decision, and is robust to bad output.

The LLM completion call is injected so the deterministic logic — prompt shape,
JSON parsing, hallucination guard, safe fallback — is tested without an API key.
"""

from pathlib import Path

from sincron_brain import reconcile
from sincron_brain.config import JudgeConfig, VaultConfig
from sincron_brain.judge import (
    _litellm_completion,
    build_messages,
    default_decider,
    make_judge,
    parse_decision,
)
from sincron_brain.models import DraftItem
from sincron_brain.reconcile import Candidate


def _cands(*ids: str) -> list[Candidate]:
    return [Candidate(id=i, synopsis=f"synopsis {i}") for i in ids]


def test_parse_create_decision():
    raw = (
        '{"action":"create","major_tags":["pessoas"],"synopsis":"Mateus",'
        '"content":"Memória contextual","go_deeper":["x"]}'
    )
    d = parse_decision(raw, _cands())
    assert d.action == "create"
    assert d.major_tags == ["pessoas"]
    assert d.synopsis == "Mateus"
    assert d.content == "Memória contextual"
    assert d.go_deeper == ["x"]


def test_parse_merge_maps_content_append():
    raw = '{"action":"merge","target_id":"a","content_append":"Pai do Pedro","emotional":true}'
    d = parse_decision(raw, _cands("a"))
    assert d.action == "merge"
    assert d.target_id == "a"
    assert d.content == "Pai do Pedro"
    assert d.emotional is True


def test_parse_merge_with_unknown_target_falls_back_to_create():
    raw = '{"action":"merge","target_id":"ghost","content_append":"x"}'
    d = parse_decision(raw, _cands("a", "b"))
    assert d.action == "create"  # hallucinated target rejected


def test_parse_malformed_json_falls_back_to_create():
    d = parse_decision("desculpe, não consigo responguardar em JSON", _cands("a"))
    assert d.action == "create"


def test_parse_strips_code_fences():
    raw = '```json\n{"action":"create","synopsis":"S"}\n```'
    d = parse_decision(raw, _cands())
    assert d.action == "create"
    assert d.synopsis == "S"


def test_build_messages_includes_draft_and_candidates():
    msgs = build_messages(
        DraftItem(id="d", content="Mateus casou com a Cacau"), _cands("a")
    )
    blob = " ".join(m["content"] for m in msgs)
    assert "Mateus casou com a Cacau" in blob
    assert "a" in blob and "synopsis a" in blob


def test_build_messages_defines_emotion_as_ai_feedback_not_narrated_feeling():
    msgs = build_messages(DraftItem(id="d", content="..."), _cands())
    blob = " ".join(m["content"] for m in msgs)
    assert "Feedback positivo" in blob
    assert "Esse cliente me deixou frustrado" in blob
    assert "não reforço emocional do sistema" in blob


def test_build_messages_includes_major_tag_taxonomy_and_primary_rule():
    msgs = build_messages(DraftItem(id="d", content="..."), _cands())
    blob = " ".join(m["content"] for m in msgs)
    assert "Major Tags default" in blob
    assert "soul" in blob
    assert "external_access" in blob
    assert "schedule" in blob
    assert "Retorne uma unica major_tag" in blob
    assert "soul e especial" in blob
    assert "preferences e especial" in blob


def test_build_messages_for_conversation_turn_instructs_contextual_compilation():
    msgs = build_messages(
        DraftItem(
            id="d",
            content="Contexto consolidado do turno: API key fica no .env.",
            source_type="conversation_turn",
            user_message="Droga, ja falei que a API key fica no .env.",
            agent_response="Desculpe, vou lembrar.",
            memory_reason="Correção do usuário: a API key fica no .env; não perguntar de novo.",
        ),
        _cands(),
    )
    blob = " ".join(m["content"] for m in msgs)
    assert "MENSAGEM DO USUÁRIO" in blob
    assert "RESPOSTA DA IA" in blob
    assert "não copie como transcrição" in blob
    assert "FALLBACK CONTEXTUAL" in blob


def test_judge_returns_merge_from_injected_llm():
    cfg = VaultConfig(vault_path=Path("/vault"))
    raw = '{"action":"merge","target_id":"a","content_append":"novo","go_deeper":["p"]}'
    decide = make_judge(cfg, complete=lambda _messages: raw)
    d = decide(DraftItem(id="d", content="..."), _cands("a"))
    assert d.action == "merge"
    assert d.target_id == "a"
    assert d.content == "novo"


def test_judge_returns_create_from_injected_llm():
    cfg = VaultConfig(vault_path=Path("/vault"))
    raw = '{"action":"create","major_tags":["t"],"synopsis":"S"}'
    decide = make_judge(cfg, complete=lambda _messages: raw)
    d = decide(DraftItem(id="d", content="..."), _cands())
    assert d.action == "create"
    assert d.synopsis == "S"


def test_judge_provider_failure_falls_back_to_safe_create():
    cfg = VaultConfig(vault_path=Path("/vault"))

    def raise_provider_error(_messages):
        raise RuntimeError("provider unavailable")

    decide = make_judge(cfg, complete=raise_provider_error)
    d = decide(DraftItem(id="d", content="..."), _cands("a"))

    assert d.action == "create"
    assert d.major_tags == []


def test_default_decider_without_api_key_is_create_only():
    cfg = VaultConfig(
        vault_path=Path("/vault"),
        judge=JudgeConfig(api_key_env="SBM_DEFINITELY_UNSET_KEY"),
    )
    assert default_decider(cfg) is reconcile.create_only


def test_default_decider_without_api_key_is_provider_agnostic(monkeypatch):
    monkeypatch.delenv("SBM_DEFINITELY_UNSET_KEY", raising=False)
    for provider in ["anthropic", "openai", "google", "mistral", "ollama", "custom"]:
        cfg = VaultConfig(
            vault_path=Path("/vault"),
            judge=JudgeConfig(
                provider=provider,
                model="model-x",
                api_key_env="SBM_DEFINITELY_UNSET_KEY",
            ),
        )
        assert default_decider(cfg) is reconcile.create_only


def test_litellm_completion_routes_provider_and_model(monkeypatch):
    calls = []

    class _Message:
        content = '{"action":"create","synopsis":"ok"}'

    class _Choice:
        message = _Message()

    class _Response:
        def __init__(self):
            self.choices = [_Choice()]

    def fake_completion(**kwargs):
        calls.append(kwargs)
        return _Response()

    monkeypatch.setattr("litellm.completion", fake_completion)
    monkeypatch.setenv("SBM_TEST_KEY", "secret")
    cfg = VaultConfig(
        vault_path=Path("/vault"),
        judge=JudgeConfig(provider="openai", model="gpt-test", api_key_env="SBM_TEST_KEY"),
    )

    raw = _litellm_completion(cfg)([{"role": "user", "content": "hello"}])

    assert raw == '{"action":"create","synopsis":"ok"}'
    assert calls[0]["model"] == "openai/gpt-test"
    assert calls[0]["api_key"] == "secret"
    assert calls[0]["temperature"] == 0
