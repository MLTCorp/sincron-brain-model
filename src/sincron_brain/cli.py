"""CLI: init / connect / serve / sleep-now / stats / viewer."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Annotated

import typer
from platformdirs import user_data_dir
from rich.console import Console
from rich.table import Table

from sincron_brain import storage
from sincron_brain.config import (
    PROVIDER_API_KEY_ENV,
    PROVIDER_DEFAULT_MODEL,
    JudgeConfig,
    VaultConfig,
    load_config,
)
from sincron_brain.major_tags import default_major_tag_names_csv

app = typer.Typer(
    name="sincron-brain",
    help="Plug-and-play memory layer for AI agents. MCP server.",
    no_args_is_help=True,
)
console = Console()


def _default_vault_path() -> Path:
    return Path(user_data_dir("sincron-brain", "sincron")) / "memory"


def _create_vault(
    vault_path: Path,
    provider: str | None,
    yes: bool,
) -> VaultConfig:
    console.print(f"[bold]Creating vault at:[/] {vault_path}")

    chosen_provider = provider or ("anthropic" if yes else _prompt_provider())
    api_key_env = PROVIDER_API_KEY_ENV.get(chosen_provider, "ANTHROPIC_API_KEY")
    model = PROVIDER_DEFAULT_MODEL.get(chosen_provider, "claude-haiku-4-5-20251001")

    if not yes and not os.environ.get(api_key_env):
        console.print(
            f"[yellow]Warning:[/] {api_key_env} not set in environment. "
            f"Set it before running sleep_now()."
        )

    config = VaultConfig(
        vault_path=vault_path,
        judge=JudgeConfig(
            provider=chosen_provider,
            model=model,
            api_key_env=api_key_env,
        ),
    )
    storage.ensure_vault(config)
    with storage.open_db(config):
        pass
    config.save()
    return config


@app.command()
def init(
    path: Annotated[
        Path | None, typer.Option("--path", help="Vault directory. Default: user data dir.")
    ] = None,
    provider: Annotated[
        str | None, typer.Option("--provider", help="LLM judge provider (anthropic, openai, ...).")
    ] = None,
    yes: Annotated[
        bool, typer.Option("--yes", "-y", help="Skip prompts, use defaults.")
    ] = False,
) -> None:
    """Create a new memory vault."""
    vault_path = path or _default_vault_path()
    vault_path = vault_path.expanduser().resolve()

    if (vault_path / "_config.toml").exists():
        console.print(f"[yellow]Vault already exists at {vault_path}[/]")
        raise typer.Exit(1)

    config = _create_vault(vault_path, provider, yes)

    console.print("[green]Vault created.[/]")
    console.print(f"  Config: {config.config_file}")
    console.print(f"  Index:  {config.index_db}")
    console.print(f"  Judge:  {config.judge.provider} / {config.judge.model}")
    console.print()
    console.print("[bold]Connect a project to this vault:[/]")
    console.print(f'  sincron-brain connect --path "{vault_path}"')


@app.command()
def connect(
    path: Annotated[
        Path | None, typer.Option("--path", help="Vault directory. Default: user data dir.")
    ] = None,
    project: Annotated[
        Path, typer.Option("--project", help="Project directory where .mcp.json will be written.")
    ] = Path("."),
    provider: Annotated[
        str | None, typer.Option("--provider", help="LLM judge provider if a vault is created.")
    ] = None,
    yes: Annotated[
        bool, typer.Option("--yes", "-y", help="Skip prompts, use defaults if a vault is created.")
    ] = True,
) -> None:
    """Create/use a vault and write .mcp.json for the current project."""
    vault_path = path or _default_vault_path()
    vault_path = vault_path.expanduser().resolve()
    project_path = project.expanduser().resolve()

    if not project_path.exists():
        console.print(f"[red]Project directory not found:[/] {project_path}")
        raise typer.Exit(1)

    if (vault_path / "_config.toml").exists():
        config = load_config(vault_path)
        console.print(f"[green]Using existing vault:[/] {config.vault_path}")
    else:
        config = _create_vault(vault_path, provider, yes)
        console.print("[green]Vault created.[/]")

    mcp_file = _write_project_mcp_config(project_path, config)
    claude_settings = _sync_claude_project_settings(project_path)
    instruction_files = _sync_agent_instruction_files(project_path)
    console.print(f"[green]MCP config written:[/] {mcp_file}")
    console.print(f"[green]Claude project settings synced:[/] {claude_settings}")
    for instruction_file in instruction_files:
        console.print(f"[green]Agent instructions synced:[/] {instruction_file}")
    console.print()
    console.print("[bold]Next steps:[/]")
    console.print("  1. Restart your MCP client/agent.")
    console.print("  2. Run: sincron-brain stats")


@app.command()
def serve() -> None:
    """Run the MCP server (stdio). Used by MCP clients."""
    from sincron_brain.server import main

    main()


@app.command("sleep-now")
def sleep_now() -> None:
    """Force the sleep/indexing job to run now."""
    from sincron_brain import judge
    from sincron_brain.sleep import run_sleep

    config = _load_or_die()
    console.print("[bold]Running sleep job...[/]")
    result = run_sleep(config, decide=judge.default_decider(config))
    console.print(
        f"[green]Done.[/] processed={result['processed']} "
        f"created={result['created']} merged={result['merged']} "
        f"({result['duration_seconds']}s)"
    )


@app.command()
def stats() -> None:
    """Print vault statistics."""
    config = _load_or_die()
    with storage.open_db(config) as conn:
        s = storage.stats(conn)

    table = Table(title=f"Vault: {config.vault_path}", show_header=False)
    table.add_column("metric", style="cyan")
    table.add_column("value", style="bold")
    table.add_row("Total memories", str(s["total"]))
    table.add_row("Distinct Major Tags", str(s["tags"]))
    table.add_row("Avg score", f"{s['avg_score']}")
    table.add_row("High-score (>=50)", str(s["high_score_count"]))
    table.add_row("Draft queue", str(len(list(config.draft_dir.glob("*.json")))))
    console.print(table)


@app.command()
def viewer(
    output: Annotated[
        Path | None,
        typer.Option("--output", "-o", help="HTML output path. Default: <vault>/_viewer.html."),
    ] = None,
) -> None:
    """Generate a static HTML debug viewer for the current vault."""
    from sincron_brain.viewer import write_viewer

    config = _load_or_die()
    output_path = write_viewer(config, output)
    console.print("[green]Viewer generated.[/]")
    console.print(f"  {output_path}")


def _load_or_die() -> VaultConfig:
    vault_value = (
        os.environ.get("SINCRON_BRAIN_VAULT")
        or _vault_path_from_project_mcp_config(Path.cwd())
        or str(_default_vault_path())
    )
    vault = Path(vault_value)
    try:
        return load_config(vault.expanduser().resolve())
    except FileNotFoundError as e:
        console.print(f"[red]{e}[/]")
        raise typer.Exit(1) from None


def _mcp_server_payload(config: VaultConfig) -> dict:
    return {
        "command": "sincron-brain",
        "args": ["serve"],
        "env": {
            "SINCRON_BRAIN_VAULT": str(config.vault_path),
        },
    }


def _write_project_mcp_config(project_path: Path, config: VaultConfig) -> Path:
    mcp_file = project_path / ".mcp.json"
    if mcp_file.exists():
        try:
            payload = json.loads(mcp_file.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            console.print(f"[red]Invalid JSON at {mcp_file}: {e}[/]")
            raise typer.Exit(1) from None
        if not isinstance(payload, dict):
            console.print(f"[red]{mcp_file} must contain a JSON object.[/]")
            raise typer.Exit(1)
    else:
        payload = {}

    mcp_servers = payload.setdefault("mcpServers", {})
    if not isinstance(mcp_servers, dict):
        console.print(f"[red]{mcp_file} has an invalid mcpServers value.[/]")
        raise typer.Exit(1)

    mcp_servers["sincron-brain"] = _mcp_server_payload(config)
    mcp_file.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return mcp_file


def _sync_claude_project_settings(project_path: Path) -> Path:
    """Enable project .mcp.json servers that exist, avoiding dangling Claude entries."""
    mcp_file = project_path / ".mcp.json"
    payload = json.loads(mcp_file.read_text(encoding="utf-8"))
    valid_servers = set(payload.get("mcpServers", {}).keys())

    claude_dir = project_path / ".claude"
    claude_dir.mkdir(exist_ok=True)
    settings_file = claude_dir / "settings.local.json"

    if settings_file.exists():
        try:
            settings = json.loads(settings_file.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            console.print(f"[red]Invalid JSON at {settings_file}: {e}[/]")
            raise typer.Exit(1) from None
        if not isinstance(settings, dict):
            console.print(f"[red]{settings_file} must contain a JSON object.[/]")
            raise typer.Exit(1)
    else:
        settings = {}

    enabled = settings.get("enabledMcpjsonServers", [])
    if not isinstance(enabled, list):
        enabled = []

    synced = []
    for name in enabled:
        if isinstance(name, str) and name in valid_servers and name not in synced:
            synced.append(name)
    if "sincron-brain" in valid_servers and "sincron-brain" not in synced:
        synced.append("sincron-brain")

    settings["enabledMcpjsonServers"] = synced
    settings_file.write_text(
        json.dumps(settings, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return settings_file


MEMORY_INSTRUCTIONS_START = "<!-- sincron-brain-memory:start -->"
MEMORY_INSTRUCTIONS_END = "<!-- sincron-brain-memory:end -->"
MEMORY_INSTRUCTIONS_BLOCK = f"""{MEMORY_INSTRUCTIONS_START}
## Sincron Brain Memory

Use the `sincron-brain` MCP server as the project's long-term memory layer.

- Before answering questions that may depend on prior project/user context, inspect memory first:
  `list_major_tags()` or `search()` -> `list_tags()` -> `use_memories(ids)`.
- When using memory in a user-facing conversation, always inspect `preferences` first with
  `list_tags("preferences")`; if any preference should shape the answer, fetch it through
  `use_memories(ids)` so it is injected into the working context and reactivated.
- Use `use_memories(ids)` when memory content is used in an answer. Do not use `read_memory()`
  for normal answers, because `use_memories()` queues reactivation for sleep-time scoring.
- Prefer `remember_turn(user_message, agent_response, memory_reason)` when both sides of a
  conversation turn are available. Use `remember()` for standalone durable facts, user
  preferences, project decisions, corrections, and information the user explicitly asks you
  to remember.
- Do not store secrets, API keys, tokens, passwords, or unrelated transient chatter.
- Major Tags are primary retrieval routes, not free-form facets. Use one primary Major Tag
  whenever possible. Defaults: {default_major_tag_names_csv()}.
- Create a new Major Tag only when no default category fits and the new category is generic,
  reusable across future memories, snake_case, and useful as a future search route. There is
  no separate registry command; a new Major Tag is registered when sleep indexes a memory
  with that Major Tag.
- `remember()` and `remember_turn()` only queue drafts. The sleep job indexes drafts later with
  `sleep_now()` or the configured sleep routine, compiling conversation turns into contextual
  memories rather than raw transcripts.
{MEMORY_INSTRUCTIONS_END}
"""


def _sync_agent_instruction_files(project_path: Path) -> list[Path]:
    candidates = [project_path / "AGENTS.md", project_path / "CLAUDE.md"]
    existing = [path for path in candidates if path.exists()]
    targets = existing or candidates
    synced = []

    for path in targets:
        current = path.read_text(encoding="utf-8") if path.exists() else ""
        updated = _upsert_managed_block(current, MEMORY_INSTRUCTIONS_BLOCK)
        path.write_text(updated, encoding="utf-8")
        synced.append(path)

    return synced


def _upsert_managed_block(current: str, block: str) -> str:
    if MEMORY_INSTRUCTIONS_START in current and MEMORY_INSTRUCTIONS_END in current:
        before = current.split(MEMORY_INSTRUCTIONS_START, 1)[0].rstrip()
        after = current.split(MEMORY_INSTRUCTIONS_END, 1)[1].lstrip()
        parts = [before, block.strip(), after.rstrip()]
        return "\n\n".join(part for part in parts if part) + "\n"

    if current.strip():
        return current.rstrip() + "\n\n" + block
    return block


def _vault_path_from_project_mcp_config(project_path: Path) -> str | None:
    mcp_file = project_path / ".mcp.json"
    if not mcp_file.exists():
        return None

    try:
        payload = json.loads(mcp_file.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None

    if not isinstance(payload, dict):
        return None

    server = payload.get("mcpServers", {}).get("sincron-brain")
    if not isinstance(server, dict):
        return None

    env = server.get("env")
    if not isinstance(env, dict):
        return None

    vault_path = env.get("SINCRON_BRAIN_VAULT")
    return vault_path if isinstance(vault_path, str) and vault_path else None


def _prompt_provider() -> str:
    providers = list(PROVIDER_DEFAULT_MODEL.keys())
    console.print("[bold]Choose LLM judge provider:[/]")
    for i, p in enumerate(providers, 1):
        model = PROVIDER_DEFAULT_MODEL[p]
        env = PROVIDER_API_KEY_ENV[p]
        marker = "✓" if os.environ.get(env) else " "
        console.print(f"  {i}. [{marker}] {p:<10}  {model:<40} (uses {env})")
    choice = typer.prompt("Number", default="1")
    try:
        return providers[int(choice) - 1]
    except (ValueError, IndexError):
        return "anthropic"
