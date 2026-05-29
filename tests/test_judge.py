"""LLM judge: builds the prompt, parses the decision, and is robust to bad output.

The LLM completion call is injected so the deterministic logic — prompt shape,
JSON parsing, hallucination guard, safe fallback — is tested without an API key.
"""

from pathlib import Path

from sincron_brain import reconcile
from sincron_brain.config import JudgeConfig, VaultConfig
from sincron_brain.judge import build_messages, default_decider, make_judge, parse_decision
from sincron_brain.models import DraftItem
from sincron_brain.reconcile import Candidate


def _cands(*ids: str) -> list[Candidate]:
    return [Candidate(id=i, synopsis=f"synopsis {i}") for i in ids]


def test_parse_create_decision():
    raw = '{"action":"create","major_tags":["pessoas"],"synopsis":"Mateus","go_deeper":["x"]}'
    d = parse_decision(raw, _cands())
    assert d.action == "create"
    assert d.major_tags == ["pessoas"]
    assert d.synopsis == "Mateus"
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


def test_default_decider_without_api_key_is_create_only():
    cfg = VaultConfig(
        vault_path=Path("/vault"),
        judge=JudgeConfig(api_key_env="SBM_DEFINITELY_UNSET_KEY"),
    )
    assert default_decider(cfg) is reconcile.create_only
