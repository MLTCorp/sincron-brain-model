"""Storage layer: .md files on disk + SQLite index with FTS5.

The .md files are the source of truth. SQLite is a rebuildable index — if it
gets corrupted, run `sincron-brain reindex` to rebuild from the .md files.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import time
import unicodedata
import uuid
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import frontmatter

from sincron_brain.config import VaultConfig
from sincron_brain.major_tags import DEFAULT_MAJOR_TAG_NAMES
from sincron_brain.models import DraftItem, Memory, ReactivationEvent
from sincron_brain.tags import normalize_tags

SCHEMA = """
CREATE TABLE IF NOT EXISTS memories (
    id TEXT PRIMARY KEY,
    major_tags TEXT NOT NULL DEFAULT '[]',
    tags TEXT NOT NULL DEFAULT '[]',
    score INTEGER NOT NULL DEFAULT 100,
    created TEXT NOT NULL,
    last_accessed TEXT NOT NULL,
    last_scored TEXT NOT NULL,
    access_count INTEGER NOT NULL DEFAULT 0,
    emotion_floor INTEGER NOT NULL DEFAULT 0,
    source_type TEXT NOT NULL DEFAULT 'text',
    asset_ref TEXT,
    go_deeper TEXT NOT NULL DEFAULT '[]',
    synopsis TEXT NOT NULL DEFAULT '',
    file_path TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_memories_score ON memories(score DESC);
CREATE INDEX IF NOT EXISTS idx_memories_last_accessed ON memories(last_accessed DESC);

CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
    id UNINDEXED,
    synopsis,
    content,
    tokenize = "unicode61 remove_diacritics 2"
);
"""


def _utcnow_iso() -> str:
    return datetime.now(UTC).isoformat()


SENSITIVE_KEYS = {"content", "api_key", "apikey", "token", "password", "secret"}
AUDIT_PRUNE_INTERVAL_SECONDS = 60.0
_AUDIT_LAST_PRUNED: dict[Path, float] = {}


def write_audit(config: VaultConfig, event: str, **payload: Any) -> Path | None:
    """Append a safe JSONL audit event. Never log full memory/user content."""
    if not config.audit.enabled:
        return None
    config.vault_path.mkdir(parents=True, exist_ok=True)
    _prune_audit_if_due(config)
    clean_payload = cast(dict[str, Any], _sanitize_audit(payload))
    row: dict[str, Any] = {
        "ts": _utcnow_iso(),
        "event": event,
        **clean_payload,
    }
    with config.audit_file.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    _restrict_owner_access(config.audit_file)
    return config.audit_file


def _prune_audit_if_due(config: VaultConfig) -> None:
    """Prune occasionally, not on every audit row during large sleep runs."""
    audit_file = config.audit_file.resolve()
    now = time.monotonic()
    last = _AUDIT_LAST_PRUNED.get(audit_file)
    if last is not None and now - last < AUDIT_PRUNE_INTERVAL_SECONDS:
        return
    _prune_audit(config)
    _AUDIT_LAST_PRUNED[audit_file] = now


def read_audit(config: VaultConfig) -> list[dict]:
    """Read audit events for diagnostics and tests."""
    if not config.audit_file.exists():
        return []
    return [
        json.loads(line)
        for line in config.audit_file.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _sanitize_audit(value: Any) -> Any:
    if isinstance(value, dict):
        clean = {}
        for key, item in value.items():
            if key.lower() in SENSITIVE_KEYS:
                clean[key] = "[redacted]"
            else:
                clean[key] = _sanitize_audit(item)
        return clean
    if isinstance(value, list):
        return [_sanitize_audit(item) for item in value]
    if isinstance(value, tuple):
        return [_sanitize_audit(item) for item in value]
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    return value


def _prune_audit(config: VaultConfig) -> None:
    if not config.audit_file.exists():
        return

    lines = [line for line in config.audit_file.read_text(encoding="utf-8").splitlines() if line]
    lines = _retain_recent_audit_lines(lines, config.audit.retention_days)
    lines = _retain_audit_size(lines, config.audit.max_file_mb)
    config.audit_file.write_text(_join_jsonl(lines), encoding="utf-8")
    _restrict_owner_access(config.audit_file)


def _retain_recent_audit_lines(lines: list[str], retention_days: int) -> list[str]:
    if retention_days <= 0:
        return lines
    cutoff = datetime.now(UTC) - timedelta(days=retention_days)
    kept = []
    for line in lines:
        try:
            ts = _parse_dt(json.loads(line).get("ts"))
        except (json.JSONDecodeError, TypeError, ValueError):
            kept.append(line)
            continue
        if ts >= cutoff:
            kept.append(line)
    return kept


def _retain_audit_size(lines: list[str], max_file_mb: int) -> list[str]:
    if max_file_mb <= 0:
        return lines
    max_bytes = max_file_mb * 1024 * 1024
    kept_reversed = []
    total = 0
    for line in reversed(lines):
        line_bytes = len((line + "\n").encode("utf-8"))
        if kept_reversed and total + line_bytes > max_bytes:
            break
        kept_reversed.append(line)
        total += line_bytes
    return list(reversed(kept_reversed))


def _join_jsonl(lines: list[str]) -> str:
    if not lines:
        return ""
    return "\n".join(lines) + "\n"


def slugify(text: str) -> str:
    """Lowercase ASCII slug, safe as filename and id."""
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    text = re.sub(r"[^\w\s-]", "", text.lower())
    text = re.sub(r"[\s_-]+", "-", text).strip("-")
    return text or "memory"


def ensure_vault(config: VaultConfig) -> None:
    """Create the vault directory tree if missing."""
    config.vault_path.mkdir(parents=True, exist_ok=True)
    config.draft_dir.mkdir(exist_ok=True)
    config.reactivation_dir.mkdir(exist_ok=True)
    _restrict_owner_access(config.vault_path, directory=True)
    _restrict_owner_access(config.draft_dir, directory=True)
    _restrict_owner_access(config.reactivation_dir, directory=True)


def open_db(config: VaultConfig) -> sqlite3.Connection:
    """Open (or create) the index DB with the schema applied."""
    conn = sqlite3.connect(config.index_db)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    _ensure_columns(conn)
    conn.commit()
    _restrict_owner_access(config.index_db)
    return conn


def _ensure_columns(conn: sqlite3.Connection) -> None:
    columns = {
        row["name"]
        for row in conn.execute("PRAGMA table_info(memories)").fetchall()
    }
    if "tags" not in columns:
        conn.execute("ALTER TABLE memories ADD COLUMN tags TEXT NOT NULL DEFAULT '[]'")


def _memory_file_path(config: VaultConfig, memory: Memory) -> Path:
    """Decide where the .md file lives. Uses first major_tag as folder."""
    folder = slugify(memory.major_tags[0]) if memory.major_tags else "_uncategorized"
    return config.vault_path / folder / f"{memory.id}.md"


def write_memory(config: VaultConfig, memory: Memory, conn: sqlite3.Connection) -> Path:
    """Write/overwrite the .md file and upsert the index row."""
    memory.tags = normalize_tags(memory.tags)
    file_path = _memory_file_path(config, memory)
    file_path.parent.mkdir(parents=True, exist_ok=True)

    post = frontmatter.Post(content=memory.content, **memory.frontmatter())
    file_path.write_bytes(frontmatter.dumps(post).encode("utf-8"))
    _restrict_owner_access(file_path)

    conn.execute(
        """
        INSERT INTO memories
            (id, major_tags, tags, score, created, last_accessed, last_scored,
             access_count, emotion_floor, source_type, asset_ref, go_deeper,
             synopsis, file_path)
        VALUES
            (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            major_tags=excluded.major_tags,
            tags=excluded.tags,
            score=excluded.score,
            last_accessed=excluded.last_accessed,
            last_scored=excluded.last_scored,
            access_count=excluded.access_count,
            emotion_floor=excluded.emotion_floor,
            source_type=excluded.source_type,
            asset_ref=excluded.asset_ref,
            go_deeper=excluded.go_deeper,
            synopsis=excluded.synopsis,
            file_path=excluded.file_path
        """,
        (
            memory.id,
            json.dumps(memory.major_tags),
            json.dumps(memory.tags),
            memory.score,
            memory.created.isoformat(),
            memory.last_accessed.isoformat(),
            memory.last_scored.isoformat(),
            memory.access_count,
            memory.emotion_floor,
            memory.source_type,
            memory.asset_ref,
            json.dumps(memory.go_deeper),
            memory.synopsis,
            str(file_path.relative_to(config.vault_path)),
        ),
    )

    conn.execute("DELETE FROM memories_fts WHERE id = ?", (memory.id,))
    fts_content = "\n".join(
        [
            memory.content,
            " ".join(memory.major_tags),
            " ".join(memory.tags),
        ]
    )
    conn.execute(
        "INSERT INTO memories_fts (id, synopsis, content) VALUES (?, ?, ?)",
        (memory.id, memory.synopsis, fts_content),
    )
    conn.commit()
    return file_path


def read_memory_file(path: Path) -> Memory:
    """Parse a .md file back into a Memory model."""
    post = frontmatter.load(path)
    meta: dict[str, Any] = dict(post.metadata)
    asset_ref = meta.get("asset_ref")
    return Memory(
        id=str(meta["id"]),
        major_tags=_string_list(meta.get("major_tags")),
        tags=_string_list(meta.get("tags")),
        score=int(meta.get("score", 100)),
        created=_parse_dt(meta.get("created")),
        last_accessed=_parse_dt(meta.get("last_accessed")),
        last_scored=_parse_dt(meta.get("last_scored")),
        access_count=int(meta.get("access_count", 0)),
        emotion_floor=int(meta.get("emotion_floor", 0)),
        source_type=str(meta.get("source_type", "text")),
        asset_ref=str(asset_ref) if asset_ref is not None else None,
        go_deeper=_string_list(meta.get("go_deeper")),
        synopsis=str(meta.get("synopsis", "")),
        content=post.content,
    )


def _string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item) for item in value]
    if isinstance(value, tuple):
        return [str(item) for item in value]
    return [str(value)]


def get_memory(config: VaultConfig, conn: sqlite3.Connection, memory_id: str) -> Memory | None:
    """Read a memory by id without changing its score or access counters."""
    row = conn.execute(
        "SELECT file_path FROM memories WHERE id = ?", (memory_id,)
    ).fetchone()
    if row is None:
        return None
    path = config.vault_path / row["file_path"]
    return read_memory_file(path)


def reactivate_memory(
    config: VaultConfig, conn: sqlite3.Connection, memory_id: str
) -> Memory | None:
    """Mark a memory as used in an answer: score=initial, access_count++, timestamps=now."""
    memory = get_memory(config, conn, memory_id)
    if memory is None:
        return None
    now = datetime.now(UTC)
    memory.access_count += 1
    memory.last_accessed = now
    memory.last_scored = now
    memory.score = config.score.initial
    write_memory(config, memory, conn)
    return memory


def list_major_tags(conn: sqlite3.Connection) -> list[dict]:
    """Return the Major Tag taxonomy overlaid with actual usage.

    The default canonical taxonomy is always present (count=0 when empty),
    so agents and the viewer always see the full set of retrieval routes
    available in the vault. Ad-hoc Major Tags created by the judge appear
    after the defaults.

    Ordering:
      1. Defaults with memories, by max_score DESC.
      2. Defaults still empty, in canonical order.
      3. Non-default tags, by max_score DESC.
    """
    rows = conn.execute("SELECT major_tags, score FROM memories").fetchall()
    bucket: dict[str, list[int]] = {}
    for r in rows:
        for tag in json.loads(r["major_tags"]):
            bucket.setdefault(tag, []).append(r["score"])

    def card(tag: str, scores: list[int]) -> dict:
        if not scores:
            return {"major_tag": tag, "count": 0, "max_score": 0, "avg_score": 0.0}
        return {
            "major_tag": tag,
            "count": len(scores),
            "max_score": max(scores),
            "avg_score": round(sum(scores) / len(scores), 1),
        }

    defaults = set(DEFAULT_MAJOR_TAG_NAMES)
    populated_defaults = sorted(
        ((tag, bucket[tag]) for tag in DEFAULT_MAJOR_TAG_NAMES if tag in bucket),
        key=lambda kv: -max(kv[1]),
    )
    empty_defaults = [(tag, []) for tag in DEFAULT_MAJOR_TAG_NAMES if tag not in bucket]
    extras = sorted(
        ((tag, scores) for tag, scores in bucket.items() if tag not in defaults),
        key=lambda kv: -max(kv[1]),
    )
    return [card(tag, scores) for tag, scores in (*populated_defaults, *empty_defaults, *extras)]


def list_tags(
    conn: sqlite3.Connection, major_tag: str, min_score: int = 0, limit: int = 50
) -> list[dict]:
    """List memories under a major_tag, ordered by score DESC."""
    rows = conn.execute(
        """
        SELECT id, major_tags, tags, score, synopsis, go_deeper, last_accessed, access_count
        FROM memories
        WHERE score >= ?
        ORDER BY score DESC
        LIMIT ?
        """,
        (min_score, limit * 4),
    ).fetchall()
    out = []
    for r in rows:
        tags = json.loads(r["major_tags"])
        if major_tag not in tags:
            continue
        out.append(
            {
                "id": r["id"],
                "score": r["score"],
                "tags": json.loads(r["tags"]),
                "synopsis": r["synopsis"],
                "go_deeper": json.loads(r["go_deeper"]),
                "last_accessed": r["last_accessed"],
                "access_count": r["access_count"],
            }
        )
        if len(out) >= limit:
            break
    return out


def search_fts(
    conn: sqlite3.Connection, query: str, limit: int = 20, match_all: bool = True
) -> list[dict]:
    """Full-text search fallback when Major Tag → Tag navigation isn't enough.

    match_all=True requires every token (precision, default — user-facing search);
    match_all=False matches any token (recall — candidate retrieval at sleep).
    """
    terms = [f'"{t}"*' for t in re.findall(r"\w+", query) if t]
    if not terms:
        return []
    fts_query = (" " if match_all else " OR ").join(terms)
    rows = conn.execute(
        """
        SELECT m.id, m.score, m.synopsis, m.major_tags, m.tags, m.go_deeper,
               bm25(memories_fts) AS rank
        FROM memories_fts
        JOIN memories m ON m.id = memories_fts.id
        WHERE memories_fts MATCH ?
        ORDER BY rank, m.score DESC
        LIMIT ?
        """,
        (fts_query, limit),
    ).fetchall()
    return [
        {
            "id": r["id"],
            "score": r["score"],
            "synopsis": r["synopsis"],
            "major_tags": json.loads(r["major_tags"]),
            "tags": json.loads(r["tags"]),
            "go_deeper": json.loads(r["go_deeper"]),
        }
        for r in rows
    ]


def list_common_tags(conn: sqlite3.Connection, major_tag: str | None = None) -> list[dict]:
    """Return common tags with usage counts, optionally scoped to a Major Tag."""
    rows = conn.execute("SELECT major_tags, tags, score FROM memories").fetchall()
    bucket: dict[str, list[int]] = {}
    for row in rows:
        major_tags = json.loads(row["major_tags"])
        if major_tag and major_tag not in major_tags:
            continue
        for tag in json.loads(row["tags"]):
            bucket.setdefault(tag, []).append(row["score"])
    return [
        {
            "tag": tag,
            "count": len(scores),
            "max_score": max(scores),
            "avg_score": round(sum(scores) / len(scores), 1),
        }
        for tag, scores in sorted(bucket.items(), key=lambda kv: (-len(kv[1]), kv[0]))
    ]


def list_memories_by_date(
    config: VaultConfig,
    conn: sqlite3.Connection,
    date: str,
    field: str = "created",
    limit: int = 100,
) -> list[dict]:
    """List memory cards whose selected timestamp falls on YYYY-MM-DD."""
    valid_fields = {"created", "last_accessed", "last_scored"}
    if field not in valid_fields:
        raise ValueError(f"field must be one of {sorted(valid_fields)}")
    _validate_date(date)
    rows = conn.execute(
        f"""
        SELECT id, file_path
        FROM memories
        WHERE substr({field}, 1, 10) = ?
        ORDER BY {field} ASC
        LIMIT ?
        """,
        (date, limit),
    ).fetchall()
    out = []
    for row in rows:
        memory = read_memory_file(config.vault_path / row["file_path"])
        out.append(memory_card(memory, status="matched"))
    return out


def memory_card(memory: Memory, status: str | None = None) -> dict:
    card = {
        "id": memory.id,
        "major_tags": memory.major_tags,
        "tags": memory.tags,
        "score": memory.score,
        "synopsis": memory.synopsis,
        "go_deeper": memory.go_deeper,
        "created": memory.created.isoformat(),
        "last_accessed": memory.last_accessed.isoformat(),
        "last_scored": memory.last_scored.isoformat(),
        "access_count": memory.access_count,
    }
    if status:
        card["status"] = status
    return card


def _validate_date(value: str) -> None:
    try:
        datetime.strptime(value, "%Y-%m-%d")
    except ValueError as e:
        raise ValueError("date must use YYYY-MM-DD") from e


def write_draft(config: VaultConfig, item: DraftItem) -> Path:
    """Append a draft item to the queue. Processed at next sleep."""
    config.draft_dir.mkdir(exist_ok=True)
    path = config.draft_dir / f"{item.timestamp.strftime('%Y%m%d-%H%M%S')}-{item.id}.json"
    path.write_text(item.model_dump_json(indent=2), encoding="utf-8")
    _restrict_owner_access(path)
    return path


def iter_drafts(config: VaultConfig) -> Iterable[tuple[Path, DraftItem]]:
    """Yield all pending drafts in timestamp order."""
    for path in sorted(config.draft_dir.glob("*.json")):
        yield path, DraftItem.model_validate_json(path.read_text(encoding="utf-8"))


def write_reactivation(config: VaultConfig, event: ReactivationEvent) -> Path:
    """Append a reactivation event. Processed at next sleep."""
    config.reactivation_dir.mkdir(exist_ok=True)
    path = config.reactivation_dir / f"{event.timestamp.strftime('%Y%m%d-%H%M%S')}-{event.id}.json"
    path.write_text(event.model_dump_json(indent=2), encoding="utf-8")
    _restrict_owner_access(path)
    return path


def iter_reactivations(config: VaultConfig) -> Iterable[tuple[Path, ReactivationEvent]]:
    """Yield all pending reactivation events in timestamp order."""
    for path in sorted(config.reactivation_dir.glob("*.json")):
        yield path, ReactivationEvent.model_validate_json(path.read_text(encoding="utf-8"))


def stats(conn: sqlite3.Connection) -> dict:
    """Vault summary for the stats command and MCP tool."""
    total = conn.execute("SELECT COUNT(*) AS c FROM memories").fetchone()["c"]
    if total == 0:
        return {
            "total": 0,
            "tags": 0,
            "avg_score": 0.0,
            "high_score_count": 0,
            "linked_memories": 0,
            "avg_go_deeper": 0.0,
            "orphan_count": 0,
            "dead_links_count": 0,
        }
    row = conn.execute(
        """
        SELECT
            COUNT(*) AS total,
            AVG(score) AS avg_score,
            SUM(CASE WHEN score >= 50 THEN 1 ELSE 0 END) AS high
        FROM memories
        """
    ).fetchone()
    tags_count = len(list_major_tags(conn))
    graph = _graph_health(conn, total)
    return {
        "total": row["total"],
        "tags": tags_count,
        "avg_score": round(row["avg_score"] or 0.0, 1),
        "high_score_count": row["high"],
        **graph,
    }


def _graph_health(conn: sqlite3.Connection, total: int) -> dict:
    """Per-vault go_deeper health: density, orphans, broken references."""
    all_ids: set[str] = set()
    edges_from: dict[str, list[str]] = {}
    edges_to: dict[str, list[str]] = {}
    for row in conn.execute("SELECT id, go_deeper FROM memories").fetchall():
        memory_id = row["id"]
        all_ids.add(memory_id)
        try:
            targets = json.loads(row["go_deeper"]) or []
        except (json.JSONDecodeError, TypeError):
            targets = []
        edges_from[memory_id] = list(targets)
        for target in targets:
            edges_to.setdefault(target, []).append(memory_id)

    linked_memories = sum(1 for ids in edges_from.values() if ids)
    total_edges = sum(len(ids) for ids in edges_from.values())
    avg_go_deeper = round(total_edges / total, 2) if total else 0.0
    orphan_count = sum(
        1
        for memory_id in all_ids
        if not edges_from.get(memory_id) and not edges_to.get(memory_id)
    )
    dead_links_count = sum(
        1
        for targets in edges_from.values()
        for target in targets
        if target not in all_ids
    )
    return {
        "linked_memories": linked_memories,
        "avg_go_deeper": avg_go_deeper,
        "orphan_count": orphan_count,
        "dead_links_count": dead_links_count,
    }


def new_memory_id(prefix: str | None = None) -> str:
    """Generate a stable id. Prefix is slugified if provided."""
    short = uuid.uuid4().hex[:8]
    return f"{slugify(prefix)}-{short}" if prefix else short


def _parse_dt(value) -> datetime:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value
    if value is None:
        return datetime.now(UTC)
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def _restrict_owner_access(path: Path, directory: bool = False) -> None:
    """Best-effort POSIX permission hardening for local memory files."""
    if os.name != "posix":
        return
    try:
        path.chmod(0o700 if directory else 0o600)
    except OSError:
        return
