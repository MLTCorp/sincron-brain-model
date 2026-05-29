"""Sleep job: processes the draft queue into the indexed vault.

Each draft is reconciled (see reconcile.py): the injected decider chooses to
create a new memory or enrich an existing one, so re-touched topics aggregate
instead of duplicating. After draining the queue, every memory's score decays,
floored at its emotional floor.

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
    """Drain the draft queue through reconciliation, then decay. Returns counters."""
    decide = decide or reconcile.create_only
    start = time.monotonic()
    created = merged = 0

    with storage.open_db(config) as conn:
        for path, draft in storage.iter_drafts(config):
            outcome, _ = reconcile.reconcile_draft(conn, draft, config, decide)
            if outcome == "merged":
                merged += 1
            else:
                created += 1
            path.unlink()

        _apply_decay(conn, config)

    return {
        "processed": created + merged,
        "created": created,
        "merged": merged,
        "duration_seconds": round(time.monotonic() - start, 3),
    }


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
        conn.execute(
            "UPDATE memories SET score = ?, last_scored = ? WHERE id = ?",
            (new_score, now.isoformat(), r["id"]),
        )
    conn.commit()
