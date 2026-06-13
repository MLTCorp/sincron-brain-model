"""CLI: init / connect / serve / sleep-now / stats."""

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
    console.print(f"[green]MCP config written:[/] {mcp_file}")
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
