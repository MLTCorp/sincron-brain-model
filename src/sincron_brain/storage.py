"""Storage layer: .md files on disk + SQLite index with FTS5.

The .md files are the source of truth. SQLite is a rebuildable index — if it
gets corrupted, run `sincron-brain reindex` to rebuild from the .md files.
"""

from __future__ import annotations

import json
import re
import sqlite3
import unicodedata
import uuid
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path

import frontmatter

from sincron_brain.config import VaultConfig
from sincron_brain.models import DraftItem, Memory, ReactivationEvent

SCHEMA = """
CREATE TABLE IF NOT EXISTS memories (
    id TEXT PRIMARY KEY,
    major_tags TEXT NOT NULL DEFAULT '[]',
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


def write_audit(config: VaultConfig, event: str, **payload) -> Path | None:
    """Append a safe JSONL audit event. Never log full memory/user content."""
    if not config.audit.enabled:
        return None
    config.vault_path.mkdir(parents=True, exist_ok=True)
    row = {
        "ts": _utcnow_iso(),
        "event": event,
        **_sanitize_audit(payload),
    }
    with config.audit_file.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    return config.audit_file


def read_audit(config: VaultConfig) -> list[dict]:
    """Read audit events for diagnostics and tests."""
    if not config.audit_file.exists():
        return []
    return [
        json.loads(line)
        for line in config.audit_file.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _sanitize_audit(value):
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


def open_db(config: VaultConfig) -> sqlite3.Connection:
    """Open (or create) the index DB with the schema applied."""
    conn = sqlite3.connect(config.index_db)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


def _memory_file_path(config: VaultConfig, memory: Memory) -> Path:
    """Decide where the .md file lives. Uses first major_tag as folder."""
    folder = slugify(memory.major_tags[0]) if memory.major_tags else "_uncategorized"
    return config.vault_path / folder / f"{memory.id}.md"


def write_memory(config: VaultConfig, memory: Memory, conn: sqlite3.Connection) -> Path:
    """Write/overwrite the .md file and upsert the index row."""
    file_path = _memory_file_path(config, memory)
    file_path.parent.mkdir(parents=True, exist_ok=True)

    post = frontmatter.Post(content=memory.content, **memory.frontmatter())
    file_path.write_bytes(frontmatter.dumps(post).encode("utf-8"))

    conn.execute(
        """
        INSERT INTO memories
            (id, major_tags, score, created, last_accessed, last_scored,
             access_count, emotion_floor, source_type, asset_ref, go_deeper,
             synopsis, file_path)
        VALUES
            (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            major_tags=excluded.major_tags,
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
    conn.execute(
        "INSERT INTO memories_fts (id, synopsis, content) VALUES (?, ?, ?)",
        (memory.id, memory.synopsis, memory.content),
    )
    conn.commit()
    return file_path


def read_memory_file(path: Path) -> Memory:
    """Parse a .md file back into a Memory model."""
    post = frontmatter.load(path)
    meta = post.metadata
    return Memory(
        id=meta["id"],
        major_tags=list(meta.get("major_tags") or []),
        score=int(meta.get("score", 100)),
        created=_parse_dt(meta.get("created")),
        last_accessed=_parse_dt(meta.get("last_accessed")),
        last_scored=_parse_dt(meta.get("last_scored")),
        access_count=int(meta.get("access_count", 0)),
        emotion_floor=int(meta.get("emotion_floor", 0)),
        source_type=str(meta.get("source_type", "text")),
        asset_ref=meta.get("asset_ref"),
        go_deeper=list(meta.get("go_deeper") or []),
        synopsis=str(meta.get("synopsis", "")),
        content=post.content,
    )


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
    """Return all distinct major_tags with counts and avg score."""
    rows = conn.execute(
        "SELECT major_tags, score FROM memories"
    ).fetchall()
    bucket: dict[str, list[int]] = {}
    for r in rows:
        for tag in json.loads(r["major_tags"]):
            bucket.setdefault(tag, []).append(r["score"])
    out = []
    for tag, scores in sorted(bucket.items(), key=lambda kv: -max(kv[1])):
        out.append(
            {
                "major_tag": tag,
                "count": len(scores),
                "max_score": max(scores),
                "avg_score": round(sum(scores) / len(scores), 1),
            }
        )
    return out


def list_tags(
    conn: sqlite3.Connection, major_tag: str, min_score: int = 0, limit: int = 50
) -> list[dict]:
    """List memories under a major_tag, ordered by score DESC."""
    rows = conn.execute(
        """
        SELECT id, major_tags, score, synopsis, last_accessed, access_count
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
                "synopsis": r["synopsis"],
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
        SELECT m.id, m.score, m.synopsis, m.major_tags,
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
        }
        for r in rows
    ]


def write_draft(config: VaultConfig, item: DraftItem) -> Path:
    """Append a draft item to the queue. Processed at next sleep."""
    config.draft_dir.mkdir(exist_ok=True)
    path = config.draft_dir / f"{item.timestamp.strftime('%Y%m%d-%H%M%S')}-{item.id}.json"
    path.write_text(item.model_dump_json(indent=2), encoding="utf-8")
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
    return path


def iter_reactivations(config: VaultConfig) -> Iterable[tuple[Path, ReactivationEvent]]:
    """Yield all pending reactivation events in timestamp order."""
    for path in sorted(config.reactivation_dir.glob("*.json")):
        yield path, ReactivationEvent.model_validate_json(path.read_text(encoding="utf-8"))


def stats(conn: sqlite3.Connection) -> dict:
    """Vault summary for the stats command and MCP tool."""
    total = conn.execute("SELECT COUNT(*) AS c FROM memories").fetchone()["c"]
    if total == 0:
        return {"total": 0, "tags": 0, "avg_score": 0.0, "high_score_count": 0}
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
    return {
        "total": row["total"],
        "tags": tags_count,
        "avg_score": round(row["avg_score"] or 0.0, 1),
        "high_score_count": row["high"],
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
