"""MCP server entry point — exposes memory tools to AI agents."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP
from platformdirs import user_data_dir

from sincron_brain import storage
from sincron_brain.config import VaultConfig, load_config
from sincron_brain.models import DraftItem

VAULT_ENV = "SINCRON_BRAIN_VAULT"


def resolve_vault_path() -> Path:
    """Resolve the vault location from env, falling back to user data dir."""
    if env := os.environ.get(VAULT_ENV):
        return Path(env).expanduser().resolve()
    return Path(user_data_dir("sincron-brain", "sincron")).resolve() / "memory"


def get_config() -> VaultConfig:
    """Load vault config from the resolved path. Lazy — called per tool invocation."""
    return load_config(resolve_vault_path())


mcp = FastMCP(
    name="sincron-brain",
    instructions=(
        "Long-term memory layer organized by Major Tag → Tag → synopsis → content. "
        "Use list_major_tags() to see themes, list_tags(major_tag) to see topics with "
        "their synopses, then read_memory(id) only when you need the full content. "
        "Use remember() to save new information for long-term recall. "
        "The actual indexing happens during the sleep job (nightly cron), not on remember()."
    ),
)


@mcp.tool()
def remember(
    content: str,
    source_type: str = "text",
    asset_ref: str | None = None,
    hint_tags: list[str] | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict:
    """Queue content for long-term memorization. Indexed at next sleep.

    Args:
        content: The textualized content to remember. Multimodal sources (audio,
            image, web) must be converted to text by the host app before calling.
        source_type: Free-form label describing origin
            (e.g. 'user_message', 'voice_transcript', 'image_description', 'web_article').
        asset_ref: Optional opaque reference (file path or URL) to the original
            binary. Stored as a string, never opened by this server.
        hint_tags: Optional suggested tags. The judge validates/refines them at sleep.
        metadata: Free-form dict attached to the draft for the host app's use.

    Returns:
        {"draft_id": str, "queued_at": iso8601, "queue_size": int}
    """
    config = get_config()
    item = DraftItem(
        id=storage.new_memory_id(),
        content=content,
        source_type=source_type,
        asset_ref=asset_ref,
        hint_tags=hint_tags or [],
        metadata=metadata or {},
    )
    storage.write_draft(config, item)
    queue_size = len(list(config.draft_dir.glob("*.json")))
    return {
        "draft_id": item.id,
        "queued_at": item.timestamp.isoformat(),
        "queue_size": queue_size,
    }


@mcp.tool()
def list_major_tags() -> list[dict]:
    """List all themes (Major Tags) in the vault with counts and score stats.

    This is the entry point for memory navigation. Pick the major_tag(s) most
    relevant to the user's question, then call list_tags() to drill down.

    Returns:
        List of {major_tag, count, max_score, avg_score} sorted by max_score DESC.
    """
    config = get_config()
    with storage.open_db(config) as conn:
        return storage.list_major_tags(conn)


@mcp.tool()
def list_tags(major_tag: str, min_score: int = 0, limit: int = 50) -> list[dict]:
    """List memory cards under a Major Tag, with their synopses.

    Read the synopses to decide which full memory to open with read_memory().

    Args:
        major_tag: The theme to drill into (from list_major_tags).
        min_score: Only return memories with score >= this value (1-100).
        limit: Max number of memories to return.

    Returns:
        List of {id, score, synopsis, last_accessed, access_count}, score DESC.
    """
    config = get_config()
    with storage.open_db(config) as conn:
        return storage.list_tags(conn, major_tag, min_score, limit)


@mcp.tool()
def read_memory(memory_id: str) -> dict | None:
    """Open the full content of a specific memory. Increments access counter.

    Args:
        memory_id: The id returned by list_tags() or search().

    Returns:
        Full memory dict with content, synopsis, tags, go_deeper, asset_ref.
        Returns None if not found.
    """
    config = get_config()
    with storage.open_db(config) as conn:
        memory = storage.get_memory(config, conn, memory_id)
        if memory is None:
            return None
        return {
            "id": memory.id,
            "major_tags": memory.major_tags,
            "synopsis": memory.synopsis,
            "content": memory.content,
            "go_deeper": memory.go_deeper,
            "asset_ref": memory.asset_ref,
            "source_type": memory.source_type,
            "score": memory.score,
            "access_count": memory.access_count,
        }


@mcp.tool()
def search(query: str, limit: int = 20) -> list[dict]:
    """Full-text fallback search across all memory content + synopses.

    Use this when Major Tag → Tag navigation didn't surface what you needed,
    or when the user's query is vague enough that you don't know which
    major_tag to start from.

    Args:
        query: Words to search for. Substring/stemmed matching.
        limit: Max results.

    Returns:
        List of {id, score, synopsis, major_tags} ordered by relevance + score.
    """
    config = get_config()
    with storage.open_db(config) as conn:
        return storage.search_fts(conn, query, limit)


@mcp.tool()
def sleep_now() -> dict:
    """Force the sleep/indexing job to run immediately instead of waiting for cron.

    Processes all queued drafts: classifies, writes synopses, picks Major Tags,
    suggests Go Deeper links, applies score decay/bonuses. Costs LLM tokens
    via the configured judge provider.

    Returns:
        {"processed": int, "created": int, "merged": int, "duration_seconds": float}
    """
    from sincron_brain import judge
    from sincron_brain.sleep import run_sleep

    config = get_config()
    return run_sleep(config, decide=judge.default_decider(config))


@mcp.tool()
def stats() -> dict:
    """Vault diagnostics: counts, score distribution, queue size.

    Returns:
        {total, tags, avg_score, high_score_count, draft_queue, vault_path}
    """
    config = get_config()
    with storage.open_db(config) as conn:
        base = storage.stats(conn)
    base["draft_queue"] = len(list(config.draft_dir.glob("*.json")))
    base["vault_path"] = str(config.vault_path)
    return base


def main() -> None:
    """Entry point for `sincron-brain serve` and uvx invocations."""
    mcp.run()


if __name__ == "__main__":
    main()
