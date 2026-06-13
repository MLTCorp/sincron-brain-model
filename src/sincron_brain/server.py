"""MCP server entry point — exposes memory tools to AI agents."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP
from platformdirs import user_data_dir

from sincron_brain import storage
from sincron_brain.config import VaultConfig, load_config
from sincron_brain.major_tags import default_major_tag_names_csv
from sincron_brain.models import DraftItem, ReactivationEvent

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
        "their synopses, and search(query) as a fallback. Choose from synopses first. "
        "When a memory's full content is needed to answer the user, call use_memories(ids); "
        "that returns the content and queues reactivation for the next sleep. "
        "When using memory in a user-facing conversation, inspect soul and preferences "
        "first with list_tags('soul') and list_tags('preferences'), then fetch relevant "
        "identity/preference memories through use_memories(ids). "
        "Major Tags are primary retrieval routes, not free-form facets. Use one primary "
        f"Major Tag when possible. Defaults: {default_major_tag_names_csv()}. "
        "Common tags are noun-like retrieval labels: reuse existing tags when useful, "
        "create new snake_case singular tags only when they add a useful search route. "
        "Create a new Major Tag only when no default fits and it is generic, snake_case, "
        "reusable, and useful as a future search route. "
        "read_memory(id) is neutral inspection/debug compatibility and should not be the "
        "normal answer path. "
        "Use remember() to save new information for long-term recall. "
        "Use remember_turn() when both user message and agent response are available; "
        "sleep will compile the turn into contextual memory instead of preserving raw chat. "
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
    storage.write_audit(
        config,
        "tool.remember",
        draft_id=item.id,
        source_type=source_type,
        hint_tags=hint_tags or [],
        has_asset_ref=asset_ref is not None,
        queue_size=queue_size,
    )
    return {
        "draft_id": item.id,
        "queued_at": item.timestamp.isoformat(),
        "queue_size": queue_size,
    }


@mcp.tool()
def remember_turn(
    user_message: str,
    agent_response: str,
    memory_reason: str,
    hint_tags: list[str] | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict:
    """Queue a conversation turn for contextual long-term memorization.

    Use this when the host has both sides of the turn. The raw user/agent text is
    stored in the draft for sleep-time analysis, but the fallback content is a
    compact contextual note, not a raw transcript. The sleep judge should compile
    the turn into durable memory such as "the user previously corrected X; use Y"
    instead of storing alternating chat messages.

    Args:
        user_message: The user's original message.
        agent_response: The agent's response/action.
        memory_reason: Why this turn should become memory, ideally including the
            durable fact or correction in concise form.
        hint_tags: Optional suggested tags.
        metadata: Free-form dict attached to the draft for the host app's use.

    Returns:
        {"draft_id": str, "queued_at": iso8601, "queue_size": int}
    """
    config = get_config()
    item = DraftItem(
        id=storage.new_memory_id(),
        content=_compiled_turn_fallback_content(memory_reason),
        source_type="conversation_turn",
        hint_tags=hint_tags or [],
        metadata=metadata or {},
        user_message=user_message,
        agent_response=agent_response,
        memory_reason=memory_reason,
    )
    storage.write_draft(config, item)
    queue_size = len(list(config.draft_dir.glob("*.json")))
    storage.write_audit(
        config,
        "tool.remember_turn",
        draft_id=item.id,
        source_type=item.source_type,
        hint_tags=hint_tags or [],
        queue_size=queue_size,
        has_user_message=bool(user_message),
        has_agent_response=bool(agent_response),
        has_memory_reason=bool(memory_reason),
    )
    return {
        "draft_id": item.id,
        "queued_at": item.timestamp.isoformat(),
        "queue_size": queue_size,
    }


def _compiled_turn_fallback_content(memory_reason: str) -> str:
    reason = memory_reason.strip()
    if not reason:
        return "Contexto consolidado de turno conversacional para indexação no sono."
    return f"Contexto consolidado do turno: {reason}"


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
        result = storage.list_major_tags(conn)
    storage.write_audit(config, "tool.list_major_tags", result_count=len(result))
    return result


@mcp.tool()
def list_tags(major_tag: str, min_score: int = 0, limit: int = 50) -> list[dict]:
    """List memory cards under a Major Tag, with their synopses.

    Read synopses to decide which memories are likely useful. When full content
    is needed for the answer, call use_memories(); it returns content and queues
    reactivation. Avoid read_memory() in normal answer flow.

    Args:
        major_tag: The theme to drill into (from list_major_tags).
        min_score: Only return memories with score >= this value (1-100).
        limit: Max number of memories to return.

    Returns:
        List of {id, score, synopsis, last_accessed, access_count}, score DESC.
    """
    config = get_config()
    with storage.open_db(config) as conn:
        result = storage.list_tags(conn, major_tag, min_score, limit)
    storage.write_audit(
        config,
        "tool.list_tags",
        major_tag=major_tag,
        min_score=min_score,
        limit=limit,
        result_count=len(result),
        memory_ids=[item["id"] for item in result],
    )
    return result


@mcp.tool()
def list_common_tags(major_tag: str | None = None) -> list[dict]:
    """List existing common tags so agents can reuse vocabulary before creating new tags.

    Args:
        major_tag: Optional Major Tag scope. When provided, only count tags used by
            memories under that Major Tag.

    Returns:
        List of {tag, count, max_score, avg_score}.
    """
    config = get_config()
    with storage.open_db(config) as conn:
        result = storage.list_common_tags(conn, major_tag)
    storage.write_audit(
        config,
        "tool.list_common_tags",
        major_tag=major_tag,
        result_count=len(result),
    )
    return result


@mcp.tool()
def read_memory(memory_id: str) -> dict | None:
    """Inspect a memory without reactivation. Compatibility/debug escape hatch.

    Args:
        memory_id: The id returned by list_tags() or search().

    Returns:
        Full memory dict with content, synopsis, tags, go_deeper, asset_ref.
        Returns None if not found.

    Note:
        This does not change score or access_count and should not be the normal
        answer path. Use use_memories() to obtain content for answering.
    """
    config = get_config()
    with storage.open_db(config) as conn:
        memory = storage.get_memory(config, conn, memory_id)
        if memory is None:
            storage.write_audit(config, "tool.read_memory", memory_id=memory_id, found=False)
            return None
        result = {
            "id": memory.id,
            "major_tags": memory.major_tags,
            "tags": memory.tags,
            "synopsis": memory.synopsis,
            "content": memory.content,
            "go_deeper": memory.go_deeper,
            "asset_ref": memory.asset_ref,
            "source_type": memory.source_type,
            "score": memory.score,
            "access_count": memory.access_count,
        }
    storage.write_audit(
        config,
        "tool.read_memory",
        memory_id=memory_id,
        found=True,
        score=memory.score,
        access_count=memory.access_count,
    )
    return result


@mcp.tool()
def use_memories(memory_ids: list[str], reason: str = "") -> dict:
    """Fetch full memory content for answering and queue sleep-time reactivation.

    This is the main plug-and-play path from synopsis to content. Use list_tags()
    or search() to inspect synopses, then call this when the full memory is
    needed to answer the user. The score is not changed immediately; the next
    sleep consolidates drafts first, then sets these memories to 100.

    Args:
        memory_ids: IDs selected for the answer context.
        reason: Optional short note about how these memories are being used.

    Returns:
        {"memories": list[dict], "queued_reactivation": bool, "event_id": str | None}
    """
    config = get_config()
    memories = []
    found_ids = []
    with storage.open_db(config) as conn:
        for memory_id in dict.fromkeys(memory_ids):
            memory = storage.get_memory(config, conn, memory_id)
            if memory is None:
                continue
            found_ids.append(memory.id)
            memories.append(
                {
                    "id": memory.id,
                    "major_tags": memory.major_tags,
                    "tags": memory.tags,
                    "synopsis": memory.synopsis,
                    "content": memory.content,
                    "go_deeper": memory.go_deeper,
                    "asset_ref": memory.asset_ref,
                    "source_type": memory.source_type,
                    "score": memory.score,
                    "access_count": memory.access_count,
                }
            )
    if not found_ids:
        storage.write_audit(
            config,
            "tool.use_memories",
            memory_ids=memory_ids,
            found_ids=[],
            queued_reactivation=False,
            reason=reason,
        )
        return {"memories": [], "queued_reactivation": False, "event_id": None}

    event = ReactivationEvent(
        id=storage.new_memory_id("reactivation"),
        memory_ids=found_ids,
        reason=reason,
    )
    storage.write_reactivation(config, event)
    storage.write_audit(
        config,
        "tool.use_memories",
        memory_ids=memory_ids,
        found_ids=found_ids,
        queued_reactivation=True,
        event_id=event.id,
        reason=reason,
    )
    return {
        "memories": memories,
        "queued_reactivation": True,
        "event_id": event.id,
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
        result = storage.search_fts(conn, query, limit)
    storage.write_audit(
        config,
        "tool.search",
        query=query,
        limit=limit,
        result_count=len(result),
        memory_ids=[item["id"] for item in result],
    )
    return result


@mcp.tool()
def list_memories_by_date(date: str, field: str = "created", limit: int = 100) -> dict:
    """List memories associated with a specific date.

    Use this when the user asks what is in memory from a given day.

    Args:
        date: Date in YYYY-MM-DD format.
        field: Which timestamp to filter by: created, last_accessed, or last_scored.
        limit: Max number of memories.

    Returns:
        {date, field, memories}. Each memory includes id, major_tags, tags, synopsis,
        score, timestamps, access_count, and statuses inferred from the audit log.
    """
    config = get_config()
    with storage.open_db(config) as conn:
        memories = storage.list_memories_by_date(config, conn, date, field, limit)
        memories = _merge_audit_memory_events(config, conn, date, memories, limit)
    storage.write_audit(
        config,
        "tool.list_memories_by_date",
        date=date,
        field=field,
        limit=limit,
        result_count=len(memories),
        memory_ids=[item["id"] for item in memories],
    )
    return {"date": date, "field": field, "memories": memories}


@mcp.tool()
def sleep_now() -> dict:
    """Force the sleep/indexing job to run immediately instead of waiting for cron.

    Processes all queued drafts: classifies, writes synopses, picks Major Tags,
    suggests Go Deeper links, applies score decay, then reactivates memories
    selected via use_memories(). Costs LLM tokens via the configured judge provider.

    Returns:
        {"processed": int, "created": int, "merged": int,
         "reactivated": int, "duration_seconds": float}
    """
    from sincron_brain import judge
    from sincron_brain.sleep import run_sleep

    config = get_config()
    result = run_sleep(config, decide=judge.default_decider(config))
    storage.write_audit(config, "tool.sleep_now", **result)
    return result


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
    base["reactivation_queue"] = len(list(config.reactivation_dir.glob("*.json")))
    base["vault_path"] = str(config.vault_path)
    base["audit_log"] = str(config.audit_file)
    storage.write_audit(
        config,
        "tool.stats",
        total=base["total"],
        tags=base["tags"],
        draft_queue=base["draft_queue"],
        reactivation_queue=base["reactivation_queue"],
    )
    return base


def main() -> None:
    """Entry point for `sincron-brain serve` and uvx invocations."""
    mcp.run()


def _merge_audit_memory_events(
    config: VaultConfig,
    conn,
    date: str,
    memories: list[dict],
    limit: int,
) -> list[dict]:
    by_id = {item["id"]: item for item in memories}
    for event in storage.read_audit(config):
        if not str(event.get("ts", "")).startswith(date):
            continue
        memory_id = event.get("memory_id")
        if not memory_id:
            continue
        status = _memory_event_status(event)
        if not status:
            continue
        if memory_id not in by_id:
            memory = storage.get_memory(config, conn, memory_id)
            if memory is None:
                continue
            by_id[memory_id] = storage.memory_card(memory)
        statuses = by_id[memory_id].setdefault("statuses", [])
        if status not in statuses:
            statuses.append(status)
    return list(by_id.values())[:limit]


def _memory_event_status(event: dict) -> str | None:
    if event.get("event") == "sleep.draft_processed":
        return str(event.get("outcome") or "processed")
    if event.get("event") == "sleep.memory_reactivated":
        return "reactivated"
    return None


if __name__ == "__main__":
    main()
