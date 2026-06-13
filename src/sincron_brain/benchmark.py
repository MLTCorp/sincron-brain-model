"""Synthetic benchmark utilities for local vault stress tests."""

from __future__ import annotations

import shutil
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import gettempdir
from typing import Any

from sincron_brain import reconcile, storage
from sincron_brain.config import VaultConfig
from sincron_brain.major_tags import DEFAULT_MAJOR_TAG_NAMES
from sincron_brain.models import DraftItem, Memory
from sincron_brain.sleep import run_sleep
from sincron_brain.viewer import write_viewer

COMMON_TAGS = (
    "api_key",
    "env_file",
    "tone",
    "memory",
    "deploy",
    "repository",
    "client",
    "contract",
    "deadline",
    "workflow",
    "decision",
    "debug",
    "viewer",
    "python",
    "mcp",
    "audit",
)


@dataclass
class TimedStep:
    name: str
    seconds: float
    result: Any = None


def run_benchmark(
    vault_path: Path,
    memories: int = 1000,
    drafts: int = 0,
    force: bool = False,
    render_viewer: bool = True,
    run_sleep_job: bool = True,
    progress: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Create a synthetic vault and measure core operations.

    This is intentionally local and deterministic. It does not call an LLM provider.
    """
    if memories < 0 or drafts < 0:
        raise ValueError("memories and drafts must be >= 0")

    vault_path = vault_path.expanduser().resolve()
    _prepare_vault_path(vault_path, force)
    config = VaultConfig(vault_path=vault_path)
    storage.ensure_vault(config)
    config.save()

    steps: list[TimedStep] = []
    base_time = datetime.now(UTC).replace(microsecond=0)

    steps.append(
        _measure(
            "populate_memories",
            lambda: _populate_memories(config, memories, base_time, progress),
        )
    )
    steps.append(_measure("queue_drafts", lambda: _queue_drafts(config, drafts, base_time)))

    if drafts and run_sleep_job:
        steps.append(
            _measure(
                "sleep_now_create_only",
                lambda: run_sleep(config, reconcile.create_only),
            )
        )

    with storage.open_db(config) as conn:
        steps.append(_measure("stats", lambda: storage.stats(conn)))
        steps.append(_measure("list_major_tags", lambda: storage.list_major_tags(conn)))
        steps.append(
            _measure(
                "list_common_tags_external_access",
                lambda: storage.list_common_tags(conn, "external_access"),
            )
        )
        steps.append(
            _measure(
                "list_tags_external_access",
                lambda: storage.list_tags(conn, "external_access", min_score=0, limit=50),
            )
        )
        steps.append(
            _measure(
                "search_api_key",
                lambda: storage.search_fts(conn, "api key", limit=20),
            )
        )
        steps.append(
            _measure(
                "list_memories_by_date",
                lambda: storage.list_memories_by_date(
                    config,
                    conn,
                    base_time.date().isoformat(),
                    field="created",
                    limit=100,
                ),
            )
        )

    viewer_path = None
    if render_viewer:
        viewer_step = _measure("viewer_html", lambda: write_viewer(config))
        steps.append(viewer_step)
        viewer_path = str(viewer_step.result)

    with storage.open_db(config) as conn:
        final_stats = storage.stats(conn)

    return {
        "vault_path": str(vault_path),
        "requested_memories": memories,
        "requested_drafts": drafts,
        "final_stats": final_stats,
        "draft_queue": len(list(config.draft_dir.glob("*.json"))),
        "reactivation_queue": len(list(config.reactivation_dir.glob("*.json"))),
        "storage": _storage_summary(config),
        "viewer_path": viewer_path,
        "steps": [_step_payload(step) for step in steps],
    }


def _prepare_vault_path(vault_path: Path, force: bool) -> None:
    if not vault_path.exists():
        vault_path.parent.mkdir(parents=True, exist_ok=True)
        return
    if not force:
        raise FileExistsError(f"{vault_path} already exists. Use --force to replace it.")
    if _dangerous_delete_target(vault_path):
        raise ValueError(f"Refusing to delete unsafe benchmark path: {vault_path}")
    shutil.rmtree(vault_path)
    vault_path.parent.mkdir(parents=True, exist_ok=True)


def _dangerous_delete_target(path: Path) -> bool:
    resolved = path.resolve()
    root = Path(resolved.anchor).resolve()
    home = Path.home().resolve()
    cwd = Path.cwd().resolve()
    temp = Path(gettempdir()).resolve()
    return resolved in {root, home, cwd, temp}


def _populate_memories(
    config: VaultConfig,
    count: int,
    base_time: datetime,
    progress: Callable[[str], None] | None,
) -> int:
    with storage.open_db(config) as conn:
        for i in range(count):
            memory = _synthetic_memory(i, count, base_time)
            storage.write_memory(config, memory, conn)
            if progress and count >= 1000 and (i + 1) % 1000 == 0:
                progress(f"  populated {i + 1}/{count} memories")
    return count


def _synthetic_memory(index: int, total: int, base_time: datetime) -> Memory:
    major = DEFAULT_MAJOR_TAG_NAMES[index % len(DEFAULT_MAJOR_TAG_NAMES)]
    secondary = DEFAULT_MAJOR_TAG_NAMES[(index + 3) % len(DEFAULT_MAJOR_TAG_NAMES)]
    created = base_time - timedelta(days=index % 30, minutes=index % 60)
    score = max(1, 100 - (index % 100))
    tags = [COMMON_TAGS[index % len(COMMON_TAGS)], COMMON_TAGS[(index + 5) % len(COMMON_TAGS)]]
    go_deeper = []
    if index > 0 and index % 7 == 0:
        go_deeper.append(f"bench-{index - 1:06d}")
    if index > 4 and index % 23 == 0:
        go_deeper.append(f"bench-{index - 5:06d}")
    synopsis = f"Benchmark memory {index:06d} about {major} and {tags[0]}"
    content = (
        f"Synthetic benchmark memory {index:06d}. "
        f"Primary route: {major}. Related context: {secondary}. "
        f"Retrieval terms: {tags[0]}, {tags[1]}, api key, project workflow. "
        f"This content is deterministic and safe for local stress testing."
    )
    return Memory(
        id=f"bench-{index:06d}",
        major_tags=[major] if index % 11 else [major, secondary],
        tags=tags,
        score=score,
        created=created,
        last_accessed=created + timedelta(hours=index % 24),
        last_scored=created,
        access_count=index % 12,
        emotion_floor=40 if index % 17 == 0 else 0,
        source_type="benchmark",
        synopsis=synopsis,
        content=content,
        go_deeper=go_deeper,
    )


def _queue_drafts(config: VaultConfig, count: int, base_time: datetime) -> int:
    for i in range(count):
        storage.write_draft(
            config,
            DraftItem(
                id=f"bench-draft-{i:06d}",
                content=(
                    f"Benchmark draft {i:06d}. The user repeated a durable fact "
                    "about API access, project workflow, and memory testing."
                ),
                source_type="benchmark",
                hint_tags=[DEFAULT_MAJOR_TAG_NAMES[i % len(DEFAULT_MAJOR_TAG_NAMES)]],
                timestamp=base_time + timedelta(seconds=i),
                metadata={"benchmark": True, "ordinal": i},
            ),
        )
    return count


def _measure(name: str, fn: Callable[[], Any]) -> TimedStep:
    start = time.perf_counter()
    result = fn()
    return TimedStep(name=name, seconds=round(time.perf_counter() - start, 4), result=result)


def _step_payload(step: TimedStep) -> dict[str, Any]:
    payload: dict[str, Any] = {"name": step.name, "seconds": step.seconds}
    if isinstance(step.result, list):
        payload["result_count"] = len(step.result)
    elif isinstance(step.result, dict):
        payload["result"] = step.result
    elif isinstance(step.result, Path):
        payload["result"] = str(step.result)
    elif step.result is not None:
        payload["result"] = step.result
    return payload


def _storage_summary(config: VaultConfig) -> dict[str, Any]:
    files = [path for path in config.vault_path.rglob("*") if path.is_file()]
    total_bytes = sum(path.stat().st_size for path in files)
    return {
        "files": len(files),
        "total_mb": round(total_bytes / (1024 * 1024), 3),
        "sqlite_mb": round(_file_mb(config.index_db), 3),
        "audit_mb": round(_file_mb(config.audit_file), 3),
    }


def _file_mb(path: Path) -> float:
    if not path.exists():
        return 0.0
    return path.stat().st_size / (1024 * 1024)
