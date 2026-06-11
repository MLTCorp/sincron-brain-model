"""CLI: init / serve / sleep-now / stats."""

from __future__ import annotations

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

    console.print("[green]Vault created.[/]")
    console.print(f"  Config: {config.config_file}")
    console.print(f"  Index:  {config.index_db}")
    console.print(f"  Judge:  {chosen_provider} / {model}")
    console.print()
    console.print("[bold]Add to your MCP client config:[/]")
    console.print(
        f'  "sincron-brain": {{\n'
        f'    "command": "sincron-brain",\n'
        f'    "args": ["serve"],\n'
        f'    "env": {{ "SINCRON_BRAIN_VAULT": "{vault_path}" }}\n'
        f"  }}"
    )


@app.command()
def serve() -> None:
    """Run the MCP server (stdio). Used by MCP clients via uvx/python -m."""
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
    vault = Path(os.environ.get("SINCRON_BRAIN_VAULT", _default_vault_path()))
    try:
        return load_config(vault.expanduser().resolve())
    except FileNotFoundError as e:
        console.print(f"[red]{e}[/]")
        raise typer.Exit(1) from None


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
