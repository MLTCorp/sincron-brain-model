# sincron-brain-model

Plug-and-play memory layer for AI agents. Distributed as an MCP server.

Any project with an AI can plug in and gain structured long-term memory inspired by Obsidian + human brain cognition: Major Tag → Tag → synopsis → content, with cognitive scoring (temporal decay, access bonuses, emotional weighting) and nightly sleep-based indexing.

## Status

`0.1.0` — scaffold. Core storage, MCP tools, CLI working. Full LLM-as-judge sleep loop is the next milestone.

## Design principles

- **Major Tag → Tag is the sole retrieval axis.** No vector embeddings. The agent's own reasoning bridges semantic gaps.
- **The agent receives only text.** Multimodal (audio, image, web) is the host app's responsibility — it textualizes, we organize.
- **One API key.** A single LLM provider (the "judge") handles synopsis writing, tag selection, Go Deeper suggestion, and emotion weighting at sleep time.
- **Sleep, not eager indexing.** Conversation flows in the host's context window during the day. The sleep cron (default 03:00) processes the draft queue.

See [CLAUDE.md](CLAUDE.md) for full architectural decisions.

## Install

Requires Python 3.11+.

```bash
# Install the CLI globally (or use uvx for ephemeral runs)
pip install sincron-brain-model
# or
uv tool install sincron-brain-model
```

## Quick start

```bash
# Create a vault. Prompts for provider unless --yes is set.
sincron-brain init

# Export the API key for your judge provider (matches the prompt choice)
export ANTHROPIC_API_KEY=sk-ant-...

# (Optional) verify it works
sincron-brain stats

# Force a sleep run on demand
sincron-brain sleep-now
```

## MCP client configuration

Add to your MCP client config (Claude Desktop / Claude Code / Cursor / etc.):

```json
{
  "mcpServers": {
    "sincron-brain": {
      "command": "uvx",
      "args": ["sincron-brain-model", "serve"],
      "env": {
        "SINCRON_BRAIN_VAULT": "/absolute/path/to/your/memory",
        "ANTHROPIC_API_KEY": "sk-ant-..."
      }
    }
  }
}
```

After restart, your agent will have access to these tools:

| Tool | Purpose |
|---|---|
| `remember(content, source_type, asset_ref, hint_tags, metadata)` | Queue content for indexing at next sleep. |
| `list_major_tags()` | List all themes in the vault. Entry point for navigation. |
| `list_tags(major_tag, min_score, limit)` | List memory cards (id + synopsis) under a theme. |
| `read_memory(memory_id)` | Open full content of a specific memory. |
| `search(query, limit)` | Full-text fallback when tag navigation isn't enough. |
| `sleep_now()` | Force indexing job to run immediately. |
| `stats()` | Vault diagnostics. |

## Vault structure

```
memory/
├── _config.toml            ← provider, schedule, score tuning
├── _index.sqlite           ← rebuildable index (scores, FTS, metadata)
├── _draft/                 ← queue waiting for next sleep
│   └── 20260513-143200-xyz.json
├── pessoas/
│   ├── mateus-massari-abc12345.md
│   └── luizao-def67890.md
└── trabalho/
    └── reuniao-cliente-acme.md
```

Each `.md` is a memory card with YAML frontmatter (id, major_tags, score, synopsis, etc.) + body. Readable directly in Obsidian.

## Supported judge providers

OpenAI, Anthropic, Google Gemini, Voyage, Cohere, Mistral, Azure OpenAI, AWS Bedrock, Ollama (local), custom OpenAI-compatible endpoints. Routed via [litellm](https://github.com/BerriAI/litellm) — adding a new one means one config entry.

## Why no embeddings

The agent's own LLM reasoning is smarter than vector similarity for navigation. Major Tag → Tag bypasses the embedding tax entirely. See [CLAUDE.md](CLAUDE.md) for the full rationale.

## License

MIT
