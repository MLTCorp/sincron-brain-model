"""MCP server entry point — exposes memory tools to AI agents."""

from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP
from platformdirs import user_data_dir

from sincron_brain import storage
from sincron_brain.config import (
    DOTENV_FILENAME,
    LLM_API_KEY_ENV,
    LLM_PROVIDER_ENV,
    PROVIDER_API_KEY_ENV,
    PROVIDER_DEFAULT_MODEL,
    VaultConfig,
    load_config,
)
from sincron_brain.major_tags import default_major_tag_names_csv
from sincron_brain.models import DraftItem, ReactivationEvent

VAULT_ENV = "SINCRON_BRAIN_VAULT"
VIEWER_FILENAME = "_viewer.html"


def resolve_vault_path() -> Path:
    """Resolve the vault location from env, falling back to user data dir."""
    if env := os.environ.get(VAULT_ENV):
        return Path(env).expanduser().resolve()
    return Path(user_data_dir("sincron-brain", "sincron")).resolve() / "memory"


_CONFIG_CACHE: dict[Path, tuple[VaultConfig, tuple[float, float]]] = {}


def _config_signature(vault_path: Path) -> tuple[float, float]:
    """mtime tuple of the files that can change VaultConfig behavior."""
    config_file = vault_path / "_config.toml"
    dotenv_file = vault_path / ".env"
    config_mtime = config_file.stat().st_mtime if config_file.exists() else 0.0
    dotenv_mtime = dotenv_file.stat().st_mtime if dotenv_file.exists() else 0.0
    return (config_mtime, dotenv_mtime)


def get_config() -> VaultConfig:
    """Resolve the vault path and return its config, cached per-process.

    Cache invalidates when _config.toml or .env mtimes change, so a user
    editing either file picks up the new value on the next tool call without
    restarting the MCP server. Without the cache, every MCP tool re-read the
    .env (file I/O + parsing) and re-ran Pydantic validation; over a chatty
    session that dominated tool-call latency.
    """
    vault_path = resolve_vault_path()
    signature = _config_signature(vault_path)
    cached = _CONFIG_CACHE.get(vault_path)
    if cached is not None and cached[1] == signature:
        return cached[0]
    config = load_config(vault_path)
    _CONFIG_CACHE[vault_path] = (config, signature)
    return config


def _clear_config_cache() -> None:
    """Test helper — drop the cache so monkeypatched env vars are honoured."""
    _CONFIG_CACHE.clear()


_VIEWER_REFRESH_LOCK = threading.Lock()


def _refresh_viewer_now(config: VaultConfig) -> None:
    """Synchronous viewer write. Used by tests and by the async helper."""
    if not (config.vault_path / VIEWER_FILENAME).exists():
        return
    with _VIEWER_REFRESH_LOCK:
        try:
            from sincron_brain.viewer import write_viewer

            write_viewer(config)
        except Exception as exc:
            storage.write_audit(config, "viewer.refresh_failed", error=str(exc))


def _refresh_viewer_if_exists(config: VaultConfig) -> None:
    """Keep _viewer.html in sync after a mutation without blocking the tool.

    The viewer is opt-in: it only exists after the user runs `sincron-brain viewer`
    (or the connect did). When present, agents and users expect it to reflect
    the current vault state. Without async, every remember/use_memories paid the
    ~13 ms cost of rebuilding 80 KB of HTML before returning to the MCP client.
    Spawning a daemon thread lets the tool reply immediately while the refresh
    runs in the background; the lock serializes concurrent refreshes so two
    writes never race on the file.

    Tests can set SINCRON_BRAIN_SYNC_VIEWER=1 to force the legacy synchronous
    behaviour (so the file is up-to-date the moment the helper returns).
    """
    if not (config.vault_path / VIEWER_FILENAME).exists():
        return
    if os.environ.get("SINCRON_BRAIN_SYNC_VIEWER"):
        _refresh_viewer_now(config)
        return
    threading.Thread(
        target=_refresh_viewer_now, args=(config,), daemon=True, name="viewer-refresh"
    ).start()


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
        hint_tags: Optional suggested **common tags** (noun-like retrieval labels).
            These are never promoted to major_tag; the judge picks the major_tag from
            the canonical taxonomy. Pass common-tag-style values like "api_key",
            "matheus_massari", "env_file"; do not pass single words like "name" or
            "identity" expecting them to become a Major Tag.
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
    _refresh_viewer_if_exists(config)
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
        hint_tags: Optional suggested **common tags** only (never major_tag).
            See `remember()` for the same constraint.
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
    _refresh_viewer_if_exists(config)
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
    _refresh_viewer_if_exists(config)
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
def list_neighbors(memory_id: str, depth: int = 1, limit: int = 20) -> dict:
    """Expand the neighbourhood of a memory by following go_deeper edges.

    Use this after `use_memories([id])` to fetch the semantic context the
    memory points to (and points back from, thanks to reciprocity) in a
    single MCP call, rather than chaining N read_memory roundtrips.

    Args:
        memory_id: The seed memory to expand from.
        depth: BFS depth (default 1, max 3).
        limit: Max neighbours returned (default 20, max 100).

    Returns:
        {"memory_id", "depth", "neighbors": [{id, synopsis, major_tags, tags,
        score, distance}, ...]} sorted by distance asc, then score desc.
    """
    depth = max(1, min(depth, 3))
    limit = max(1, min(limit, 100))
    config = get_config()
    with storage.open_db(config) as conn:
        seed = storage.get_memory(config, conn, memory_id)
        if seed is None:
            storage.write_audit(
                config,
                "tool.list_neighbors",
                seed_id=memory_id,
                depth=depth,
                limit=limit,
                result_count=0,
                seed_missing=True,
            )
            return {"memory_id": memory_id, "depth": depth, "neighbors": []}

        visited: dict[str, int] = {memory_id: 0}
        frontier = [memory_id]
        for hop in range(1, depth + 1):
            next_frontier: list[str] = []
            for node in frontier:
                row = conn.execute(
                    "SELECT go_deeper FROM memories WHERE id = ?", (node,)
                ).fetchone()
                if row is None:
                    continue
                import json as _json

                targets = _json.loads(row["go_deeper"]) or []
                for target in targets:
                    if target in visited:
                        continue
                    visited[target] = hop
                    next_frontier.append(target)
            frontier = next_frontier
            if not frontier:
                break

        neighbour_ids = [nid for nid, dist in visited.items() if dist > 0]
        neighbours: list[dict] = []
        for nid in neighbour_ids:
            memory = storage.get_memory(config, conn, nid)
            if memory is None:
                continue
            neighbours.append(
                {
                    "id": memory.id,
                    "synopsis": memory.synopsis,
                    "major_tags": memory.major_tags,
                    "tags": memory.tags,
                    "score": memory.score,
                    "distance": visited[nid],
                }
            )

    neighbours.sort(key=lambda n: (n["distance"], -n["score"]))
    neighbours = neighbours[:limit]

    storage.write_audit(
        config,
        "tool.list_neighbors",
        seed_id=memory_id,
        depth=depth,
        limit=limit,
        result_count=len(neighbours),
    )
    return {"memory_id": memory_id, "depth": depth, "neighbors": neighbours}


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


def _provider_from_key_prefix(key: str) -> str | None:
    """Same prefix sniffer the CLI uses, kept local to avoid importing the CLI."""
    key = key.strip()
    if not key:
        return None
    if key.startswith("sk-ant-"):
        return "anthropic"
    if key.startswith(("sk-proj-", "sk-svcacct-")):
        return "openai"
    if key.startswith("sk-"):
        return "openai"
    if key.startswith("AIza"):
        return "google"
    if key.startswith(("AKIA", "ASIA")):
        return "bedrock"
    return None


def _write_judge_key_to_dotenv(config: VaultConfig, provider: str, api_key: str) -> Path:
    """Update <vault>/.env to carry LLM_API_KEY/LLM_PROVIDER, replacing any prior value."""
    dotenv_path = config.vault_path / DOTENV_FILENAME
    existing_lines = (
        dotenv_path.read_text(encoding="utf-8").splitlines()
        if dotenv_path.exists()
        else []
    )
    managed = {LLM_API_KEY_ENV: api_key, LLM_PROVIDER_ENV: provider}
    new_lines: list[str] = []
    seen: set[str] = set()
    for line in existing_lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            new_lines.append(line)
            continue
        key = stripped.partition("=")[0].strip()
        if key in managed:
            new_lines.append(f"{key}={managed[key]}")
            seen.add(key)
        else:
            new_lines.append(line)
    for key, value in managed.items():
        if key not in seen:
            new_lines.append(f"{key}={value}")
    dotenv_path.write_text("\n".join(new_lines) + "\n", encoding="utf-8")
    storage._restrict_owner_access(dotenv_path)
    return dotenv_path


def _sync_provider_to_key(config: VaultConfig) -> VaultConfig:
    """Reconcile config.judge.provider with whatever LLM_API_KEY actually is.

    When the user (or an early-session agent) only edited the vault `.env` and
    never called set_judge_key, _config.toml is still pointing at the install
    placeholder (anthropic). Pinging that provider with, say, an sk-proj- key
    fails for the wrong reason and confuses the diagnosis. Sniff the key's
    prefix and, if it implies a different provider than what's configured,
    rewrite the judge section and persist it before pinging.
    """
    key = config.judge_api_key()
    if not key:
        return config
    sniffed = _provider_from_key_prefix(key)
    if sniffed is None or sniffed == config.judge.provider:
        return config
    storage.write_audit(
        config,
        "judge.provider_auto_corrected",
        previous_provider=config.judge.provider,
        new_provider=sniffed,
        reason="LLM_API_KEY prefix did not match configured provider",
    )
    config.judge.provider = sniffed
    config.judge.model = PROVIDER_DEFAULT_MODEL[sniffed]
    config.judge.api_key_env = PROVIDER_API_KEY_ENV[sniffed]
    config.save()
    _clear_config_cache()
    return get_config()


def _ping_judge(config: VaultConfig) -> dict:
    """Run a minimal completion against the configured judge to confirm liveness."""
    from sincron_brain import judge

    if not judge.judge_available(config):
        return {
            "ready": False,
            "error": "api_key_missing",
            "message": (
                f"No API key resolved. Edit {config.vault_path / DOTENV_FILENAME} or "
                f"call set_judge_key(api_key)."
            ),
        }

    config = _sync_provider_to_key(config)

    try:
        import time as _time

        do_complete = judge._litellm_completion(config)
        start = _time.monotonic()
        raw = do_complete(
            [
                {"role": "system", "content": "Reply with the single word OK."},
                {"role": "user", "content": "ping"},
            ]
        )
        duration_ms = int((_time.monotonic() - start) * 1000)
    except Exception as exc:
        storage.write_audit(
            config,
            "judge.ping_failed",
            error=type(exc).__name__,
            error_message=str(exc)[:200],
            provider=config.judge.provider,
            model=config.judge.model,
        )
        return {
            "ready": False,
            "error": type(exc).__name__,
            "message": str(exc)[:300],
            "provider": config.judge.provider,
            "model": config.judge.model,
        }

    storage.write_audit(
        config,
        "judge.ping_ok",
        duration_ms=duration_ms,
        provider=config.judge.provider,
        model=config.judge.model,
    )
    return {
        "ready": True,
        "provider": config.judge.provider,
        "model": config.judge.model,
        "duration_ms": duration_ms,
        "reply_preview": raw[:80],
    }


@mcp.tool()
def set_judge_key(api_key: str, provider: str | None = None) -> dict:
    """Persist a judge API key into the vault .env and verify it works.

    Use this whenever the user provides an API key for the indexing judge —
    do NOT edit the .env by hand. The tool detects the provider from the
    key prefix (or accepts an explicit `provider` for opaque keys like
    Mistral/Cohere/Voyage/Azure), writes both LLM_API_KEY and LLM_PROVIDER
    to <vault>/.env, updates _config.toml so the judge points at the right
    provider/model, and performs a real ping completion against the LLM to
    confirm the credentials work end to end.

    The API key value is never echoed back nor written to the audit log.

    Returns:
        {ready: bool, provider, model, duration_ms?, reply_preview?,
         error?, message?, dotenv_path}
    """
    if not api_key or not api_key.strip():
        return {"ready": False, "error": "empty_key", "message": "api_key is empty."}

    api_key = api_key.strip()
    provider = (provider or "").strip().lower() or _provider_from_key_prefix(api_key)
    if not provider:
        return {
            "ready": False,
            "error": "provider_unknown",
            "message": (
                "Could not infer provider from the key prefix. Pass `provider=...` "
                f"with one of: {', '.join(PROVIDER_API_KEY_ENV)}."
            ),
        }
    if provider not in PROVIDER_API_KEY_ENV:
        return {
            "ready": False,
            "error": "provider_unsupported",
            "message": f"Provider {provider!r} is not supported. "
            f"Pick one of: {', '.join(PROVIDER_API_KEY_ENV)}.",
        }

    config = get_config()
    config.judge.provider = provider
    config.judge.model = PROVIDER_DEFAULT_MODEL[provider]
    config.judge.api_key_env = PROVIDER_API_KEY_ENV[provider]
    config.save()
    dotenv_path = _write_judge_key_to_dotenv(config, provider, api_key)
    _clear_config_cache()

    storage.write_audit(
        config,
        "tool.set_judge_key",
        provider=provider,
        model=config.judge.model,
        dotenv_path=str(dotenv_path),
    )

    fresh = get_config()
    result = _ping_judge(fresh)
    result["dotenv_path"] = str(dotenv_path)
    return result


@mcp.tool()
def verify_judge() -> dict:
    """Run a small completion against the configured judge to confirm liveness.

    Use after editing .env by hand, after switching judge providers, or any
    time the user wants to confirm cognitive indexing will actually work
    before kicking off a sleep that costs tokens.

    Returns:
        {ready, provider, model, duration_ms, reply_preview} on success,
        {ready=false, error, message} on failure.
    """
    _clear_config_cache()
    config = get_config()
    return _ping_judge(config)


@mcp.tool()
def sleep_now() -> dict:
    """Force the sleep/indexing job to run immediately instead of waiting for cron.

    Processes all queued drafts: classifies, writes synopses, picks Major Tags,
    suggests Go Deeper links, applies score decay, then reactivates memories
    selected via use_memories(). Costs LLM tokens via the configured judge provider.

    When the configured judge's API key is missing, sleep falls back to a
    mechanical create-only mode: drafts still persist but Major Tags collapse
    to `_uncategorized`, synopses are not rewritten, and no go_deeper links are
    proposed. The result dict's `judge_used: bool` makes this visible.

    Returns:
        {"processed": int, "created": int, "merged": int,
         "reactivated": int, "duration_seconds": float, "judge_used": bool}
    """
    from sincron_brain import judge
    from sincron_brain.sleep import run_sleep

    config = get_config()
    judge_used = judge.judge_available(config)
    if not judge_used:
        storage.write_audit(
            config,
            "sleep.using_fallback_decider",
            reason="judge_api_key_missing",
            api_key_env=config.judge.api_key_env,
        )
    result = run_sleep(config, decide=judge.default_decider(config))
    result["judge_used"] = judge_used
    storage.write_audit(config, "tool.sleep_now", **result)
    _refresh_viewer_if_exists(config)
    return result


@mcp.tool()
def stats() -> dict:
    """Vault diagnostics: counts, score distribution, queue size, judge status.

    Returns:
        {total, tags, avg_score, high_score_count, draft_queue, vault_path,
         judge_status: {provider, model, api_key_env, api_key_present, ready}}
    """
    from sincron_brain import judge

    config = get_config()
    with storage.open_db(config) as conn:
        base = storage.stats(conn)
    base["draft_queue"] = len(list(config.draft_dir.glob("*.json")))
    base["reactivation_queue"] = len(list(config.reactivation_dir.glob("*.json")))
    base["vault_path"] = str(config.vault_path)
    base["audit_log"] = str(config.audit_file)
    base["judge_status"] = judge.judge_status(config)
    storage.write_audit(
        config,
        "tool.stats",
        total=base["total"],
        tags=base["tags"],
        draft_queue=base["draft_queue"],
        reactivation_queue=base["reactivation_queue"],
        judge_ready=base["judge_status"]["ready"],
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
