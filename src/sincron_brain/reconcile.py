"""Reconciliation: turn a draft into either a new memory or an enrichment of an
existing one, instead of blindly indexing a duplicate.

The decision (create vs merge-into-which) is made by an injected `Decider`. This
keeps the deterministic machinery — candidate retrieval, additive merging, the
anti-bloat guard — fully testable without an LLM. The nightly judge is the real
Decider; a high-confidence FTS heuristic can serve as a cheap safety net.

Two invariants protect the vault (see "A arte de esquecer" in CLAUDE.md):
  - Merge is additive, never a rewrite — content is appended, go_deeper and
    the primary major_tag can be added, emotional charge takes the max.
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
from sincron_brain.tags import normalize_tags


@dataclass
class Candidate:
    id: str
    synopsis: str
    major_tags: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)


@dataclass
class Decision:
    action: Literal["create", "merge"]
    target_id: str | None = None
    major_tags: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    synopsis: str = ""
    content: str = ""
    go_deeper: list[str] = field(default_factory=list)
    emotional: bool = False
    feedback_targets: list[str] = field(default_factory=list)
    feedback_sentiment: Literal["positive", "negative", "neutral", ""] = ""


Decider = Callable[[DraftItem, list[Candidate]], list[Decision]]

MAX_DECISIONS_PER_DRAFT = 4
MIN_SYNOPSIS_CHARS = 40
MAX_GO_DEEPER_PER_MEMORY = 6
AUTO_FTS_GO_DEEPER_LIMIT = 3
MAX_FEEDBACK_TARGETS_PER_DECISION = 3


def create_only(draft: DraftItem, candidates: list[Candidate]) -> list[Decision]:
    """Default decider: index every draft as a new memory (no dedup, no LLM)."""
    return [Decision(action="create")]


def find_candidates(
    conn, draft: DraftItem, config: VaultConfig, limit: int = 5
) -> list[Candidate]:
    """Cheap shortlist: memories sharing a hint tag, plus FTS hits on content."""
    synopses: dict[str, str] = {}
    order: list[str] = []

    if draft.hint_tags:
        hint = set(draft.hint_tags)
        for row in conn.execute("SELECT id, major_tags, tags, synopsis FROM memories"):
            major_tags = json.loads(row["major_tags"])
            tags = json.loads(row["tags"])
            if hint & (set(major_tags) | set(tags)) and row["id"] not in synopses:
                synopses[row["id"]] = row["synopsis"]
                order.append(row["id"])

    for hit in storage.search_fts(conn, draft.content, limit=limit, match_all=False):
        if hit["id"] not in synopses:
            synopses[hit["id"]] = hit["synopsis"]
            order.append(hit["id"])

    out = []
    for memory_id in order[:limit]:
        row = conn.execute(
            "SELECT major_tags, tags FROM memories WHERE id = ?", (memory_id,)
        ).fetchone()
        out.append(
            Candidate(
                id=memory_id,
                synopsis=synopses[memory_id],
                major_tags=json.loads(row["major_tags"]) if row else [],
                tags=json.loads(row["tags"]) if row else [],
            )
        )
    return out


def reconcile_draft(
    conn, draft: DraftItem, config: VaultConfig, decide: Decider
) -> list[tuple[str, Memory]]:
    """Process one draft into ONE OR MORE memories.

    The judge may return several decisions when a single draft carries facts
    that belong to different canonical Major Tags (e.g. user identity + agent
    persona in the same message). Each decision becomes its own memory; the
    same merge/cross-major-tag guards apply per decision. Returns a list of
    (outcome, memory) tuples so the sleep job can audit each individually.

    For every resulting memory:
      - go_deeper IDs from the judge are validated (dead refs and
        self-references dropped, capped at MAX_GO_DEEPER_PER_MEMORY)
      - up to AUTO_FTS_GO_DEEPER_LIMIT extra IDs from the candidates list are
        appended, since find_candidates already returned semantic neighbours
      - reciprocity is materialised (B.go_deeper gains A when A links B)
    """
    candidates = [] if decide is create_only else find_candidates(conn, draft, config)
    recent_use_memories = (
        [] if decide is create_only else storage.recent_use_memories_targets(config, conn)
    )
    try:
        raw_decisions = (
            decide(draft, candidates, recent_use_memories=recent_use_memories) or []
        )
    except TypeError:
        # Legacy deciders / stubs that don't accept the kwarg.
        raw_decisions = decide(draft, candidates) or []
    decisions = _filter_and_cap_decisions(draft, raw_decisions, config)

    results: list[tuple[str, Memory]] = []
    for decision in decisions:
        merge_target_id = decision.target_id if decision.action == "merge" else None
        if decision.action == "merge" and decision.target_id:
            target = _load(conn, config, decision.target_id)
            if (
                target is not None
                and not _too_large(target, config)
                and not _crosses_major_tag(target, decision)
            ):
                merged = _apply_merge(target, decision, config)
                merged.go_deeper = _finalise_go_deeper(
                    merged.id,
                    merged.go_deeper,
                    candidates,
                    conn,
                    draft,
                    merge_target_id,
                    config,
                )
                storage.write_memory(config, merged, conn)
                results.append(("merged", merged))
                continue

        new = _build_new(draft, decision, config)
        new.go_deeper = _finalise_go_deeper(
            new.id,
            new.go_deeper,
            candidates,
            conn,
            draft,
            merge_target_id,
            config,
        )
        storage.write_memory(config, new, conn)
        results.append(("created", new))

    _apply_reciprocity(results, conn, config)
    _apply_feedback_to_targets(decisions, draft, conn, config)

    return results


def _apply_feedback_to_targets(
    decisions: list[Decision],
    draft: DraftItem,
    conn,
    config: VaultConfig,
) -> None:
    """Route emotional feedback to the memories that were the actual target.

    When the user reacts to the agent's previous use of memory ("you remembered
    well", "I already told you"), the judge marks `emotional=true` AND lists
    the memories that received the feedback in `feedback_targets`. Without
    this hook the boost would land on the new memory describing the feedback
    instead of the original memories that earned it.

    Cap at MAX_FEEDBACK_TARGETS_PER_DECISION per decision; dead refs are
    audited and skipped.
    """
    now = datetime.now(UTC)
    for decision in decisions:
        if not decision.emotional or not decision.feedback_targets:
            continue
        targets = decision.feedback_targets[:MAX_FEEDBACK_TARGETS_PER_DECISION]
        for target_id in targets:
            target = _load(conn, config, target_id)
            if target is None:
                storage.write_audit(
                    config,
                    "feedback.dropped_dead_reference",
                    draft_id=draft.id,
                    target_id=target_id,
                    reason="target_not_found",
                )
                continue
            new_score, new_floor = scoring.apply_emotion_trigger(
                target.score, target.emotion_floor, config.score
            )
            target.score = new_score
            target.emotion_floor = new_floor
            target.last_scored = now
            storage.write_memory(config, target, conn)
            storage.write_audit(
                config,
                "feedback.applied_to_target",
                draft_id=draft.id,
                target_id=target.id,
                sentiment=decision.feedback_sentiment or "feedback",
                new_emotion_floor=new_floor,
            )


def _finalise_go_deeper(
    source_id: str,
    raw_ids: list[str],
    candidates: list[Candidate],
    conn,
    draft: DraftItem,
    merge_target_id: str | None,
    config: VaultConfig,
) -> list[str]:
    """Auto-extend with FTS neighbours, then drop dead/self and cap."""
    extended = _auto_extend_go_deeper(
        list(raw_ids), candidates, merge_target_id, source_id, draft, config
    )
    return _clean_go_deeper(extended, conn, source_id, draft.id, config)


def _auto_extend_go_deeper(
    base_ids: list[str],
    candidates: list[Candidate],
    merge_target_id: str | None,
    source_id: str,
    draft: DraftItem,
    config: VaultConfig,
) -> list[str]:
    """Append top-K FTS-neighbour IDs to base_ids without duplicates.

    The judge already saw the same candidates, so anything it left out either
    didn't belong or was forgotten — promoting them automatically gives the
    graph a layer of semantic connectivity without spending another LLM call.
    """
    if not candidates:
        return base_ids
    seen = set(base_ids)
    added: list[str] = []
    for candidate in candidates:
        if len(added) >= AUTO_FTS_GO_DEEPER_LIMIT:
            break
        cid = candidate.id
        if cid in seen or cid == merge_target_id or cid == source_id:
            continue
        added.append(cid)
        seen.add(cid)
    if added:
        storage.write_audit(
            config,
            "go_deeper.auto_fts_added",
            draft_id=draft.id,
            source_id=source_id,
            added_ids=added,
        )
    return [*base_ids, *added]


def _clean_go_deeper(
    ids: list[str],
    conn,
    source_id: str,
    draft_id: str,
    config: VaultConfig,
) -> list[str]:
    """Drop self-references, dead references, and apply the per-memory cap."""
    deduped: list[str] = []
    seen: set[str] = set()
    for raw_id in ids:
        if not isinstance(raw_id, str):
            continue
        candidate_id = raw_id.strip()
        if not candidate_id or candidate_id in seen:
            continue
        seen.add(candidate_id)
        if candidate_id == source_id:
            storage.write_audit(
                config,
                "go_deeper.self_reference_dropped",
                memory_id=source_id,
                draft_id=draft_id,
            )
            continue
        deduped.append(candidate_id)

    if not deduped:
        return []

    placeholders = ",".join(["?"] * len(deduped))
    existing = {
        row[0]
        for row in conn.execute(
            f"SELECT id FROM memories WHERE id IN ({placeholders})", deduped
        ).fetchall()
    }
    alive: list[str] = []
    for candidate_id in deduped:
        if candidate_id in existing:
            alive.append(candidate_id)
        else:
            storage.write_audit(
                config,
                "go_deeper.dropped_dead_reference",
                draft_id=draft_id,
                source_id=source_id,
                dropped_id=candidate_id,
            )

    if len(alive) > MAX_GO_DEEPER_PER_MEMORY:
        dropped = alive[MAX_GO_DEEPER_PER_MEMORY:]
        alive = alive[:MAX_GO_DEEPER_PER_MEMORY]
        storage.write_audit(
            config,
            "go_deeper.capped",
            memory_id=source_id,
            kept=len(alive),
            dropped=len(dropped),
        )

    return alive


def _apply_reciprocity(
    results: list[tuple[str, Memory]],
    conn,
    config: VaultConfig,
) -> None:
    """Materialise back-edges: when A → B, ensure B → A too.

    Without this, list_neighbors(B) and use_memories(B) silently lose the
    A-side context. The viewer recomputed incoming edges in the browser, but
    the data layer never persisted them — this fixes that.
    """
    for _, memory in results:
        for target_id in list(memory.go_deeper):
            if target_id == memory.id:
                continue
            target = _load(conn, config, target_id)
            if target is None:
                continue
            if memory.id in target.go_deeper:
                continue
            if len(target.go_deeper) >= MAX_GO_DEEPER_PER_MEMORY:
                storage.write_audit(
                    config,
                    "go_deeper.reciprocity_capped",
                    from_id=target_id,
                    to_id=memory.id,
                    reason="target_full",
                )
                continue
            target.go_deeper = [*target.go_deeper, memory.id]
            storage.write_memory(config, target, conn)
            storage.write_audit(
                config,
                "go_deeper.reciprocity_added",
                from_id=target_id,
                to_id=memory.id,
            )


def _filter_and_cap_decisions(
    draft: DraftItem, decisions: list[Decision], config: VaultConfig
) -> list[Decision]:
    """Apply the anti-inflation guards before any memory write.

    Three barriers:
      - cap at MAX_DECISIONS_PER_DRAFT (excess decisions audited and dropped)
      - dedup by primary major_tag (merge tags + go_deeper, keep the first)
      - reject thin decisions whose synopsis is shorter than MIN_SYNOPSIS_CHARS
        and have no content — defense against the LLM rephrasing the same fact
        across multiple categories to "fake" a decomposition.

    A draft with no surviving decisions falls back to a single create so the
    content is not lost.
    """
    if not decisions:
        return [Decision(action="create")]

    if len(decisions) > MAX_DECISIONS_PER_DRAFT:
        storage.write_audit(
            config,
            "sleep.decision_capped",
            draft_id=draft.id,
            received=len(decisions),
            kept=MAX_DECISIONS_PER_DRAFT,
        )
        decisions = decisions[:MAX_DECISIONS_PER_DRAFT]

    by_major: dict[str, Decision] = {}
    extras: list[Decision] = []
    for decision in decisions:
        primary = _primary_major_tags(decision.major_tags)
        key = primary[0] if primary else ""
        if not key:
            extras.append(decision)
            continue
        existing = by_major.get(key)
        if existing is None:
            by_major[key] = decision
        else:
            existing.tags = list(dict.fromkeys([*existing.tags, *decision.tags]))
            existing.go_deeper = sorted(
                set(existing.go_deeper) | set(decision.go_deeper)
            )
            existing.emotional = existing.emotional or decision.emotional
            storage.write_audit(
                config,
                "sleep.decision_deduped",
                draft_id=draft.id,
                major_tag=key,
            )

    merged_decisions = [*by_major.values(), *extras]

    # The thin-decision guard only triggers when the LLM actually fragmented.
    # A single-decision draft is always kept, even with an empty synopsis —
    # legacy stubs and the no-LLM fallback rely on that.
    if len(merged_decisions) <= 1:
        return merged_decisions

    surviving: list[Decision] = []
    for decision in merged_decisions:
        synopsis_len = len(decision.synopsis.strip())
        if (
            decision.action == "create"
            and synopsis_len < MIN_SYNOPSIS_CHARS
            and not decision.content.strip()
        ):
            storage.write_audit(
                config,
                "sleep.decision_rejected",
                draft_id=draft.id,
                reason="thin_synopsis",
                synopsis_len=synopsis_len,
            )
            continue
        surviving.append(decision)

    if not surviving:
        return [Decision(action="create")]
    return surviving


def _crosses_major_tag(target: Memory, decision: Decision) -> bool:
    """Refuse to merge a draft into a memory whose Major Tag differs.

    Merge means "add context to a memory that already has a home". If the
    judge's primary Major Tag for the new content doesn't match the target's,
    the new content belongs in a different memory — link via go_deeper, do not
    fuse. Without this guard, a user-identity memory could swallow an
    agent-identity draft (or vice versa) and end up multi-routed.
    """
    primary = _primary_major_tags(decision.major_tags)
    if not primary:
        return False
    return primary[0] not in target.major_tags


def _apply_merge(target: Memory, decision: Decision, config: VaultConfig) -> Memory:
    if decision.synopsis:
        target.synopsis = decision.synopsis
    if decision.content:
        target.content = f"{target.content}\n\n{decision.content}".strip()
    target.go_deeper = sorted(set(target.go_deeper) | set(decision.go_deeper))
    # Merge does NOT change major_tags: the target already has its home.
    # Cross-major-tag attempts are blocked upstream by _crosses_major_tag.
    target.tags = normalize_tags([*target.tags, *decision.tags])
    now = datetime.now(UTC)
    target.score = config.score.initial
    target.last_scored = now
    if decision.emotional and not decision.feedback_targets:
        # When feedback_targets is set, the reinforcement is routed to those
        # memories instead. The new/merged memory here is just the receipt of
        # the feedback turn, not the original subject of the praise/correction.
        target.score, target.emotion_floor = scoring.apply_emotion_trigger(
            target.score, target.emotion_floor, config.score
        )
    target.last_accessed = now
    return target


def _build_new(draft: DraftItem, decision: Decision, config: VaultConfig) -> Memory:
    major_tags = _primary_major_tags(decision.major_tags) or ["_uncategorized"]
    synopsis = decision.synopsis or _fallback_synopsis(draft.content)
    memory = Memory(
        id=storage.new_memory_id(synopsis[:40]),
        major_tags=major_tags,
        tags=normalize_tags(decision.tags or draft.hint_tags),
        score=config.score.initial,
        source_type=draft.source_type,
        asset_ref=draft.asset_ref,
        synopsis=synopsis,
        content=decision.content or draft.content,
        go_deeper=decision.go_deeper,
    )
    if decision.emotional and not decision.feedback_targets:
        # Same rationale as _apply_merge: don't double-count by also boosting
        # the new "receipt" memory when feedback_targets routes the boost.
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


def _primary_major_tags(tags: list[str]) -> list[str]:
    for tag in tags:
        if tag and tag.strip():
            return [tag.strip()]
    return []
