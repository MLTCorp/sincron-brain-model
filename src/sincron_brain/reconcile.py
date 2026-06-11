"""Reconciliation: turn a draft into either a new memory or an enrichment of an
existing one, instead of blindly indexing a duplicate.

The decision (create vs merge-into-which) is made by an injected `Decider`. This
keeps the deterministic machinery — candidate retrieval, additive merging, the
anti-bloat guard — fully testable without an LLM. The nightly judge is the real
Decider; a high-confidence FTS heuristic can serve as a cheap safety net.

Two invariants protect the vault (see "A arte de esquecer" in CLAUDE.md):
  - Merge is additive, never a rewrite — content is appended, go_deeper and
    major_tags are unioned, emotional charge takes the max.
  - A memory never grows unbounded: merging into an already-large memory is
    refused and becomes a new linked fragment, keeping retrieval fast.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Literal

from sincron_brain import scoring, storage
from sincron_brain.config import VaultConfig
from sincron_brain.models import DraftItem, Memory


@dataclass
class Candidate:
    id: str
    synopsis: str


@dataclass
class Decision:
    action: Literal["create", "merge"]
    target_id: str | None = None
    major_tags: list[str] = field(default_factory=list)
    synopsis: str = ""
    content: str = ""
    go_deeper: list[str] = field(default_factory=list)
    emotional: bool = False


Decider = Callable[[DraftItem, list[Candidate]], Decision]


def create_only(draft: DraftItem, candidates: list[Candidate]) -> Decision:
    """Default decider: index every draft as a new memory (no dedup, no LLM)."""
    return Decision(action="create")


def find_candidates(
    conn, draft: DraftItem, config: VaultConfig, limit: int = 5
) -> list[Candidate]:
    """Cheap shortlist: memories sharing a hint tag, plus FTS hits on content."""
    synopses: dict[str, str] = {}
    order: list[str] = []

    if draft.hint_tags:
        hint = set(draft.hint_tags)
        for row in conn.execute("SELECT id, major_tags, synopsis FROM memories"):
            if hint & set(json.loads(row["major_tags"])) and row["id"] not in synopses:
                synopses[row["id"]] = row["synopsis"]
                order.append(row["id"])

    for hit in storage.search_fts(conn, draft.content, limit=limit, match_all=False):
        if hit["id"] not in synopses:
            synopses[hit["id"]] = hit["synopsis"]
            order.append(hit["id"])

    return [Candidate(id=i, synopsis=synopses[i]) for i in order[:limit]]


def reconcile_draft(
    conn, draft: DraftItem, config: VaultConfig, decide: Decider
) -> tuple[str, Memory]:
    """Process one draft. Returns (outcome, memory) where outcome is created|merged."""
    candidates = find_candidates(conn, draft, config)
    decision = decide(draft, candidates)

    if decision.action == "merge" and decision.target_id:
        target = _load(conn, config, decision.target_id)
        if target is not None and not _too_large(target, config):
            merged = _apply_merge(target, decision, config)
            storage.write_memory(config, merged, conn)
            return "merged", merged

    new = _build_new(draft, decision, config)
    storage.write_memory(config, new, conn)
    return "created", new


def _apply_merge(target: Memory, decision: Decision, config: VaultConfig) -> Memory:
    if decision.synopsis:
        target.synopsis = decision.synopsis
    if decision.content:
        target.content = f"{target.content}\n\n{decision.content}".strip()
    target.go_deeper = sorted(set(target.go_deeper) | set(decision.go_deeper))
    target.major_tags = sorted(set(target.major_tags) | set(decision.major_tags))
    now = datetime.now(UTC)
    target.score = config.score.initial
    target.last_scored = now
    if decision.emotional:
        target.score, target.emotion_floor = scoring.apply_emotion_trigger(
            target.score, target.emotion_floor, config.score
        )
    target.last_accessed = now
    return target


def _build_new(draft: DraftItem, decision: Decision, config: VaultConfig) -> Memory:
    major_tags = decision.major_tags or draft.hint_tags or ["_uncategorized"]
    synopsis = decision.synopsis or _fallback_synopsis(draft.content)
    memory = Memory(
        id=storage.new_memory_id(synopsis[:40]),
        major_tags=major_tags,
        score=config.score.initial,
        source_type=draft.source_type,
        asset_ref=draft.asset_ref,
        synopsis=synopsis,
        content=draft.content,
        go_deeper=decision.go_deeper,
    )
    if decision.emotional:
        memory.score, memory.emotion_floor = scoring.apply_emotion_trigger(
            memory.score, memory.emotion_floor, config.score
        )
    return memory


def _load(conn, config: VaultConfig, memory_id: str) -> Memory | None:
    row = conn.execute(
        "SELECT file_path FROM memories WHERE id = ?", (memory_id,)
    ).fetchone()
    if row is None:
        return None
    return storage.read_memory_file(config.vault_path / row["file_path"])


def _too_large(memory: Memory, config: VaultConfig) -> bool:
    return len(memory.content) >= config.sleep.merge_size_threshold_chars


def _fallback_synopsis(text: str, max_len: int = 400) -> str:
    if len(text) <= max_len:
        return text
    return text[: max_len - 1].rsplit(" ", 1)[0] + "…"
