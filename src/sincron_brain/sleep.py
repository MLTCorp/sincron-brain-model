"""Sleep job: processes the draft queue into the indexed vault.

Each draft is reconciled (see reconcile.py): the injected decider chooses to
create a new memory or enrich an existing one, so re-touched topics aggregate
instead of duplicating. After draining the queue, every memory's score decays,
floored at its emotional floor. Reactivation events are applied last, so
memories used in final answer context return to the surface after consolidation.

The default decider indexes every draft as new (no dedup). The LLM judge — the
next milestone — is wired in as the real decider to enable merging.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime

from sincron_brain import reconcile, scoring, storage
from sincron_brain.config import VaultConfig
from sincron_brain.reconcile import Decider


def run_sleep(config: VaultConfig, decide: Decider | None = None) -> dict:
    """Drain drafts, decay stale memories, then apply reactivation events."""
    decide = decide or reconcile.create_only
    start = time.monotonic()
    created = merged = reactivated = 0
    storage.write_audit(config, "sleep.started")

    with storage.open_db(config) as conn:
        for path, draft in storage.iter_drafts(config):
            results = reconcile.reconcile_draft(conn, draft, config, decide)
            if len(results) > 1:
                storage.write_audit(
                    config,
                    "sleep.draft_decomposed",
                    draft_id=draft.id,
                    total_decisions=len(results),
                    major_tags=[memory.major_tags for _, memory in results],
                )
            for decision_index, (outcome, memory) in enumerate(results):
                if outcome == "merged":
                    merged += 1
                else:
                    created += 1
                storage.write_audit(
                    config,
                    "sleep.draft_processed",
                    draft_id=draft.id,
                    decision_index=decision_index,
                    decisions_total=len(results),
                    outcome=outcome,
                    memory_id=memory.id,
                    score=memory.score,
                    emotion_floor=memory.emotion_floor,
                )
            path.unlink()

        _apply_decay(conn, config)

        for path, event in storage.iter_reactivations(config):
            seen: set[str] = set()
            for memory_id in event.memory_ids:
                if memory_id in seen:
                    continue
                seen.add(memory_id)
                memory = storage.reactivate_memory(config, conn, memory_id)
                if memory is not None:
                    reactivated += 1
                    storage.write_audit(
                        config,
                        "sleep.memory_reactivated",
                        event_id=event.id,
                        memory_id=memory.id,
                        score=memory.score,
                        access_count=memory.access_count,
                    )
            path.unlink()

    result = {
        "processed": created + merged,
        "created": created,
        "merged": merged,
        "reactivated": reactivated,
        "duration_seconds": round(time.monotonic() - start, 3),
    }
    storage.write_audit(config, "sleep.finished", **result)
    return result


def _apply_decay(conn, config: VaultConfig) -> None:
    """Decay every memory by elapsed days, floored at max(global floor, emotion floor)."""
    now = datetime.now(UTC)
    rows = conn.execute("SELECT id, score, emotion_floor, last_scored FROM memories").fetchall()
    for r in rows:
        last_scored = datetime.fromisoformat(r["last_scored"].replace("Z", "+00:00"))
        days = max(0.0, (now - last_scored).total_seconds() / 86400.0)
        if days < 1.0:
            continue
        new_score = scoring.decayed_score(r["score"], r["emotion_floor"], days, config.score)
        if new_score != r["score"]:
            storage.write_audit(
                config,
                "sleep.memory_decayed",
                memory_id=r["id"],
                old_score=r["score"],
                new_score=new_score,
                days=round(days, 3),
                emotion_floor=r["emotion_floor"],
            )
        conn.execute(
            "UPDATE memories SET score = ?, last_scored = ? WHERE id = ?",
            (new_score, now.isoformat(), r["id"]),
        )
    conn.commit()
