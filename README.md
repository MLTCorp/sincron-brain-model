# sincron-brain-model

Plug-and-play memory layer for AI agents. Distributed as an MCP server.

Any project with an AI can plug in and gain structured long-term memory inspired by Obsidian + human brain cognition: Major Tag → Tag → synopsis → content, with cognitive scoring (temporal decay, reactivation, and emotional floors) and nightly sleep-based indexing.

## Status

`0.1.0` — scaffold. Core storage, MCP tools, CLI working. Full LLM-as-judge sleep loop is the next milestone.

## Design principles

- **Major Tag → Tag is the sole retrieval axis.** No vector embeddings. The agent's own reasoning bridges semantic gaps.
- **The agent receives only text.** Multimodal (audio, image, web) is the host app's responsibility — it textualizes, we organize.
- **One API key.** A single LLM provider (the "judge") handles synopsis writing, tag selection, Go Deeper suggestion, merge decisions, and emotional-floor classification at sleep time.
- **Sleep, not eager indexing.** Conversation flows in the host's context window during the day. The sleep cron (default 03:00) processes the draft queue, applies decay, and consolidates memory updates.
- **Feedback, not narrated emotion.** Positive and negative feedback about the AI's answer or memory use can reinforce a memory floor. Emotion inside the narrated fact is stored as content, not as a reinforcement signal.

See [CLAUDE.md](CLAUDE.md) for full architectural decisions.

## Install

Windows / PowerShell one-command install:

```powershell
Set-ExecutionPolicy -Scope Process Bypass -Force; irm https://raw.githubusercontent.com/MLTCorp/sincron-brain-model/main/install.ps1 | iex
```

The installer is plug-and-play: it installs `uv` for the current user if needed,
then installs the `sincron-brain` CLI from this GitHub repository, updates the
user PATH, and creates a compatibility command shim when the current terminal or
agent has not reloaded PATH yet.

If the repository is private, the raw GitHub URL returns `404`. In that case,
run the checked-out installer directly from this repository:

```powershell
Set-ExecutionPolicy -Scope Process Bypass -Force; & "C:\Projetos\GitHub-Clones\sincron-brain-model\install.ps1"
```

## Quick start

### Agent bootstrap prompt

Paste this prompt into Claude/Codex while the agent is opened in the project that
should receive memory:

```text
Run this PowerShell command in the root folder of this project:

powershell -NoProfile -ExecutionPolicy Bypass -Command "irm https://raw.githubusercontent.com/MLTCorp/sincron-brain-model/main/bootstrap.ps1 | iex"

When it finishes, tell me to restart this conversation or reload the MCP client so the sincron-brain server is detected.
```

The bootstrap installs/updates `sincron-brain`, creates/uses `.\memory`, writes
`.mcp.json`, syncs Claude project settings, and adds the managed memory
instruction block to `AGENTS.md`/`CLAUDE.md`. Run it from the project root; the
script refuses unsafe locations such as `C:\` or the user's home directory.

```powershell
# Inside the project that should use memory:
cd C:\Temp\teste_brain

# Create/use the vault and generate .mcp.json for this project.
sincron-brain connect --path .\memory

# Export the API key for your judge provider (matches the prompt choice)
$env:ANTHROPIC_API_KEY = "sk-ant-..."

# (Optional) verify it works
sincron-brain stats

# (Optional) generate a local debug HTML viewer
sincron-brain viewer

# Force a sleep run on demand
sincron-brain sleep-now
```

`connect` is the recommended plug-and-play path. It creates the vault if needed
and writes a project-level `.mcp.json` like this. For Claude Code projects, it
also syncs `.claude/settings.local.json` so the `sincron-brain` server is enabled
and stale project MCP entries are removed. It also writes a managed memory
instruction block to `AGENTS.md`/`CLAUDE.md`, so agents know when to consult
`use_memories()` and when to save durable facts with `remember()` or full
conversation turns with `remember_turn()`.

```json
{
  "mcpServers": {
    "sincron-brain": {
      "command": "sincron-brain",
      "args": ["serve"],
      "env": {
        "SINCRON_BRAIN_VAULT": "C:\\Temp\\teste_brain\\memory"
      }
    }
  }
}
```

## Development

On Windows/PowerShell:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
.\.venv\Scripts\python.exe -m ruff check src tests
.\.venv\Scripts\python.exe -m pytest
```

If Python was installed through Microsoft Store and `python` is not on PATH yet,
use the Store alias directly:

```powershell
& "$env:LOCALAPPDATA\Microsoft\WindowsApps\python3.13.exe" -m venv .venv
```

## MCP client configuration

The simplest path is to run this inside the project:

```powershell
sincron-brain connect --path .\memory
```

That writes `.mcp.json` for MCP clients that read project-level config. Restart
your MCP client/agent after running it.

If your MCP client does not read `.mcp.json`, copy the generated server block
into that client's MCP settings:

```json
{
  "mcpServers": {
    "sincron-brain": {
      "command": "sincron-brain",
      "args": ["serve"],
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
| `remember_turn(user_message, agent_response, memory_reason, hint_tags, metadata)` | Queue both sides of a conversation turn so sleep can compile contextual memory instead of raw transcript. |
| `list_major_tags()` | List all themes in the vault. Entry point for navigation. |
| `list_tags(major_tag, min_score, limit)` | List memory cards (id + synopsis) under a theme. |
| `list_common_tags(major_tag)` | List existing common tags for vocabulary reuse. |
| `list_memories_by_date(date, field, limit)` | List memories created/used/scored on a date. |
| `use_memories(memory_ids, reason)` | Main path to fetch full memory content and queue sleep-time reactivation. |
| `read_memory(memory_id)` | Neutral inspection/debug escape hatch; not the normal answer path. |
| `search(query, limit)` | Full-text fallback when tag navigation isn't enough. |
| `sleep_now()` | Force indexing job to run immediately. |
| `stats()` | Vault diagnostics. |

## Debug viewer

The viewer is optional. It generates a static HTML snapshot of the current vault:

```powershell
sincron-brain viewer
```

By default it writes:

```text
memory/_viewer.html
```

Open that file in a browser to inspect memories, sleeps, audit events, queues,
Major Tags, tags, scores, emotional floors, and `go_deeper` links. The viewer is
a snapshot for debugging; it is not required for the MCP server to work.

The `Grafo` tab draws a local memory map: each node is a memory, `go_deeper`
links are rendered as arrows, and the vertical position shows how close each
memory is to the access surface (`score 100` at the top, lower scores deeper).

For large vaults, generate lighter snapshots:

```powershell
# Embed only the top 1000 memories by score/recency.
sincron-brain viewer --limit 1000

# Keep memory cards and diagnostics, but omit full memory bodies.
sincron-brain viewer --summary-only

# Combine both for very large vaults.
sincron-brain viewer --limit 1000 --summary-only
```

The full vault statistics, Major Tag totals, common tag totals, queues, sleeps,
and recent audit events remain visible. `--limit` only controls how many memory
cards are embedded in the HTML; `--summary-only` controls whether full memory
bodies are embedded.

## Local benchmark

Use the benchmark command to create a synthetic vault and measure core local
operations without calling an LLM provider:

```powershell
sincron-brain benchmark --path C:\Temp\brain_benchmark_1k --memories 1000 --drafts 100 --force
```

It writes deterministic synthetic memories, optionally queues drafts and runs a
local `sleep` pass with the create-only decider, then measures `stats`, Major Tag
listing, common tag listing, tag navigation, FTS search, date listing, viewer
generation, and vault size. Existing folders are never replaced unless
`--force` is passed.

Useful scale checks:

```powershell
sincron-brain benchmark --path C:\Temp\brain_benchmark_1k --memories 1000 --drafts 100 --force
sincron-brain benchmark --path C:\Temp\brain_benchmark_10k --memories 10000 --drafts 500 --force
sincron-brain benchmark --path C:\Temp\brain_benchmark_50k --memories 50000 --skip-viewer --force
```

Use `--json` when you want machine-readable timing output for comparison.

## Scoring model

Scores stay in a 1-100 range. New memories start at `100`, temporal decay lowers stale memories by `1.5` points per day, and the global floor is `1`.

Emotional reinforcement uses `emotion_floor`, not a score above 100. Feedback or correction about the AI's answer/memory use raises that floor with decreasing impact:

```text
40, +20, +10, +5, +3, +2
max emotion_floor = 80
```

Positive and negative feedback use the same table. For example, "you remembered this perfectly" and "I already told you this, don't ask again" are both priority signals. A sentence like "this client frustrated me by paying late" is stored as memory content, but does not raise the emotional floor by itself.

Exploratory navigation does not reinforce memories: `list_major_tags()`, `list_tags()`, and `search()` return tags/synopses only. When full content is needed to answer the user, the agent calls `use_memories()`. That returns the memory bodies and queues a reactivation event; the next sleep consolidates drafts first, applies decay, then sets those memories back to `100`.

`read_memory()` remains available as a neutral inspection/debug escape hatch for MCP clients that need it, but the plug-and-play answer flow is intentionally:

```text
list/search synopses -> use_memories(ids) -> answer
```

## Major Tags

Major Tags are primary retrieval routes, not free-form facets. The sleep judge
uses one primary Major Tag whenever possible, with defaults such as
`soul`, `preferences`, `projects`, `technical_context`, `external_access`,
`people`, `organizations`, and `schedule`.

`soul` and `preferences` are special: when an agent uses memory in a user-facing
conversation, it should inspect identity and preference memories first so the
AI's durable posture and the user's expected behavior are injected into the
working context.

See [docs/major-tags.md](docs/major-tags.md) for the full taxonomy and rules for
creating a new Major Tag when the defaults are not enough.

Common tags are functional retrieval labels inside a Major Tag. They should be
nouns, named entities, or nominal concepts in `snake_case`, preferably singular.
The sleep judge reuses existing tags when possible and creates new tags when they
add a useful search route. See [docs/tags.md](docs/tags.md).

## Audit log

Each vault keeps a local JSONL audit log at:

```text
memory/_audit.jsonl
```

It records tool usage and sleep decisions such as `tool.search`, `tool.use_memories`,
`sleep.draft_processed`, `sleep.memory_decayed`, and `sleep.memory_reactivated`.
The log is meant for debugging and user trust: it stores IDs, counts, scores,
timestamps, and short reasons, but redacts full content and common sensitive
fields such as API keys, tokens, passwords, and secrets.

Audit is enabled by default with bounded retention:

```toml
[audit]
enabled = true
retention_days = 90
max_file_mb = 25
```

## Vault structure

```
memory/
├── _config.toml            ← provider, schedule, score tuning
├── _index.sqlite           ← rebuildable index (scores, FTS, metadata)
├── _audit.jsonl            ← local audit trail, no full memory content
├── _draft/                 ← queue waiting for next sleep
│   └── 20260513-143200-xyz.json
├── _reactivation/          ← memories used in final answer context
│   └── 20260513-151000-reactivation.json
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
