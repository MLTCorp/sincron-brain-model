import json
import os
import sys

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from sincron_brain import storage
from sincron_brain.config import VaultConfig


async def _call_text(session: ClientSession, name: str, arguments: dict | None = None):
    result = await session.call_tool(name, arguments or {})
    content = result.content[0]
    assert content.type == "text"
    return json.loads(content.text)


@pytest.mark.asyncio
async def test_generic_mcp_client_can_run_memory_lifecycle(tmp_path):
    config = VaultConfig(vault_path=tmp_path)
    storage.ensure_vault(config)
    with storage.open_db(config):
        pass
    config.save()

    env = os.environ.copy()
    env["SINCRON_BRAIN_VAULT"] = str(tmp_path)
    env.pop("ANTHROPIC_API_KEY", None)
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "sincron_brain", "serve"],
        env=env,
    )

    async with (
        stdio_client(params) as (read, write),
        ClientSession(read, write) as session,
    ):
        await session.initialize()
        tools = await session.list_tools()
        tool_names = {tool.name for tool in tools.tools}
        assert {
            "remember",
            "remember_turn",
            "stats",
            "sleep_now",
            "search",
            "list_tags",
            "use_memories",
        } <= tool_names

        before = await _call_text(session, "stats")
        assert before["draft_queue"] == 0
        assert before["total"] == 0

        remembered = await _call_text(
            session,
            "remember_turn",
            {
                "user_message": "Ja falei que este projeto usa memoria por stdio.",
                "agent_response": "Vou lembrar que o projeto usa memoria por stdio.",
                "memory_reason": (
                    "Correção do usuário: projeto MCP agnostico usa memoria por stdio."
                ),
                "hint_tags": ["mcp", "agnostico"],
            },
        )
        assert remembered["queue_size"] == 1

        indexed = await _call_text(session, "sleep_now")
        assert indexed["processed"] == 1
        assert indexed["created"] == 1

        hits = await _call_text(session, "search", {"query": "agnostico", "limit": 5})
        if isinstance(hits, dict):
            hits = [hits]
        assert len(hits) == 1
        memory_id = hits[0]["id"]

        used = await _call_text(
            session,
            "use_memories",
            {"memory_ids": [memory_id], "reason": "generic MCP answer context"},
        )
        assert used["queued_reactivation"] is True
        assert "projeto MCP agnostico usa memoria por stdio" in used["memories"][0]["content"]
        assert "agent_response" not in used["memories"][0]["content"]

        after_use = await _call_text(session, "stats")
        assert after_use["reactivation_queue"] == 1

        reactivated = await _call_text(session, "sleep_now")
        assert reactivated["reactivated"] == 1

        final = await _call_text(session, "stats")
        assert final["total"] == 1
        assert final["draft_queue"] == 0
        assert final["reactivation_queue"] == 0

    events = storage.read_audit(config)
    assert "tool.remember_turn" in [event["event"] for event in events]
    assert "tool.use_memories" in [event["event"] for event in events]
    assert "sleep.memory_reactivated" in [event["event"] for event in events]
