"""Cross-platform OS-level scheduling for the nightly sleep/indexing job.

The MCP server is a stdio subprocess that only lives while a client has it
spawned — it cannot run a 3am cron itself. So `connect` registers a *real*
OS-level scheduled job that runs `sincron-brain sleep-now --vault <vault>` once
a day at the configured time.

Design goals (see the connect flow in cli.py):

* **Native, no prompt.** Scheduling is part of install — `connect` just does it.
* **Agnostic.** Windows (Scheduled Task), macOS (LaunchAgent), Linux/VPS
  (user crontab) are all covered.
* **No admin/root.** Everything is registered at the *current user* level, which
  is what avoids permission prompts on the happy path.
* **Idempotent.** Each vault gets a deterministic job id derived from its path,
  so connecting twice updates the existing job instead of stacking duplicates.
* **Never breaks connect.** Any failure is caught and turned into a structured
  remediation block aimed at the AI assistant that is running the install, so it
  can finish the setup, plus a copy-paste command for a human.
"""

from __future__ import annotations

import hashlib
import os
import shlex
import shutil
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from sincron_brain.config import VaultConfig

SKIP_ENV = "SINCRON_BRAIN_NO_SCHEDULE"
JOB_PREFIX = "SincronBrainSleep"
CRON_MARKER = "sincron-brain-sleep"

# A runner takes a command (argv list) and optional stdin text, and returns an
# object exposing returncode/stdout/stderr — i.e. subprocess.CompletedProcess.
# Injecting it keeps the OS installers unit-testable without touching the host.
Runner = Callable[..., "subprocess.CompletedProcess[str]"]


@dataclass
class ScheduleResult:
    """Outcome of trying to register the nightly job."""

    ok: bool
    system: str
    method: str
    detail: str = ""
    manual_command: str = ""
    error: str | None = None
    skipped: bool = False


def _default_runner(
    cmd: list[str], input: str | None = None
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, input=input, capture_output=True, text=True)


def normalize_system(platform: str) -> str:
    """Map sys.platform to one of: windows / darwin / linux / unknown."""
    if platform.startswith("win"):
        return "windows"
    if platform == "darwin":
        return "darwin"
    if platform.startswith("linux"):
        return "linux"
    return "unknown"


def parse_daily_time(cron: str) -> tuple[int, int] | None:
    """Return (hour, minute) for a simple ``m h * * *`` daily cron, else None.

    Windows and macOS schedulers need an explicit clock time rather than a cron
    string, so only the plain daily shape is auto-translatable for them. Anything
    fancier (ranges, steps, specific weekdays) returns None and the caller emits
    a remediation block instead of guessing.
    """
    parts = cron.split()
    if len(parts) != 5:
        return None
    minute, hour, dom, month, dow = parts
    if dom != "*" or month != "*" or dow != "*":
        return None
    if not (minute.isdigit() and hour.isdigit()):
        return None
    m, h = int(minute), int(hour)
    if not (0 <= m < 60 and 0 <= h < 24):
        return None
    return h, m


def job_id(vault_path: Path) -> str:
    """Deterministic per-vault id so re-connecting updates rather than duplicates."""
    digest = hashlib.sha1(str(vault_path).encode("utf-8")).hexdigest()[:8]
    return f"{JOB_PREFIX}_{digest}"


def resolve_cli_command() -> list[str]:
    """How the scheduler should invoke the CLI.

    Prefer the installed `sincron-brain` entry point on PATH; fall back to
    ``<python> -m sincron_brain`` when it can't be found (e.g. an editable dev
    install without the console script on PATH).
    """
    found = shutil.which("sincron-brain")
    if found:
        return [found]
    return [sys.executable, "-m", "sincron_brain"]


def _join(args: list[str], *, windows: bool) -> str:
    return subprocess.list2cmdline(args) if windows else shlex.join(args)


def _sleep_argv(cli: list[str], vault: Path) -> list[str]:
    return [*cli, "sleep-now", "--vault", str(vault)]


def install_windows(
    cli: list[str], vault: Path, hour: int, minute: int, *, runner: Runner
) -> ScheduleResult:
    name = job_id(vault)
    action = _join(_sleep_argv(cli, vault), windows=True)
    st = f"{hour:02d}:{minute:02d}"
    cmd = ["schtasks", "/Create", "/TN", name, "/TR", action, "/SC", "DAILY", "/ST", st, "/F"]
    manual = _join(cmd, windows=True)
    try:
        proc = runner(cmd)
    except OSError as e:
        return ScheduleResult(
            False, "windows", "schtasks", manual_command=manual, error=str(e)
        )
    if proc.returncode == 0:
        return ScheduleResult(
            True,
            "windows",
            "schtasks",
            detail=f"Windows Scheduled Task '{name}' runs daily at {st}.",
            manual_command=manual,
        )
    return ScheduleResult(
        False,
        "windows",
        "schtasks",
        manual_command=manual,
        error=(proc.stderr or proc.stdout or "schtasks failed").strip(),
    )


def _render_plist(label: str, program_args: list[str], hour: int, minute: int) -> str:
    args_xml = "\n".join(f"    <string>{a}</string>" for a in program_args)
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
        '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
        '<plist version="1.0">\n'
        "<dict>\n"
        "  <key>Label</key>\n"
        f"  <string>{label}</string>\n"
        "  <key>ProgramArguments</key>\n"
        "  <array>\n"
        f"{args_xml}\n"
        "  </array>\n"
        "  <key>StartCalendarInterval</key>\n"
        "  <dict>\n"
        f"    <key>Hour</key><integer>{hour}</integer>\n"
        f"    <key>Minute</key><integer>{minute}</integer>\n"
        "  </dict>\n"
        "</dict>\n"
        "</plist>\n"
    )


def install_macos(
    cli: list[str],
    vault: Path,
    hour: int,
    minute: int,
    *,
    runner: Runner,
    home: Path | None = None,
) -> ScheduleResult:
    label = f"com.sincron.brain.sleep.{job_id(vault).rsplit('_', 1)[-1]}"
    plist_dir = (home or Path.home()) / "Library" / "LaunchAgents"
    plist_path = plist_dir / f"{label}.plist"
    plist = _render_plist(label, _sleep_argv(cli, vault), hour, minute)
    manual = f"launchctl unload {plist_path}; launchctl load -w {plist_path}"
    try:
        plist_dir.mkdir(parents=True, exist_ok=True)
        plist_path.write_text(plist, encoding="utf-8")
    except OSError as e:
        return ScheduleResult(False, "darwin", "launchd", manual_command=manual, error=str(e))

    # Reload so an updated plist takes effect; a failing unload (not yet loaded)
    # is expected and ignored.
    try:
        runner(["launchctl", "unload", str(plist_path)])
        proc = runner(["launchctl", "load", "-w", str(plist_path)])
    except OSError as e:
        return ScheduleResult(False, "darwin", "launchd", manual_command=manual, error=str(e))
    if proc.returncode == 0:
        return ScheduleResult(
            True,
            "darwin",
            "launchd",
            detail=f"macOS LaunchAgent '{label}' runs daily at {hour:02d}:{minute:02d}.",
            manual_command=manual,
        )
    return ScheduleResult(
        False,
        "darwin",
        "launchd",
        manual_command=manual,
        error=(proc.stderr or proc.stdout or "launchctl load failed").strip(),
    )


def install_linux(cli: list[str], vault: Path, cron: str, *, runner: Runner) -> ScheduleResult:
    marker = f"# {CRON_MARKER}:{job_id(vault)}"
    line = f"{cron} {_join(_sleep_argv(cli, vault), windows=False)} {marker}"
    manual = f"(crontab -l 2>/dev/null | grep -v '{marker}'; echo '{line}') | crontab -"
    try:
        existing = runner(["crontab", "-l"])
        kept: list[str] = []
        # crontab -l exits non-zero when the user has no crontab yet — that's a
        # clean slate, not an error.
        if existing.returncode == 0 and existing.stdout:
            kept = [ln for ln in existing.stdout.splitlines() if marker not in ln]
        content = "\n".join([*kept, line]).strip() + "\n"
        proc = runner(["crontab", "-"], input=content)
    except OSError as e:
        return ScheduleResult(False, "linux", "crontab", manual_command=manual, error=str(e))
    if proc.returncode == 0:
        return ScheduleResult(
            True,
            "linux",
            "crontab",
            detail=f"Linux user crontab entry '{cron}'.",
            manual_command=manual,
        )
    return ScheduleResult(
        False,
        "linux",
        "crontab",
        manual_command=manual,
        error=(proc.stderr or proc.stdout or "crontab failed").strip(),
    )


def _untranslatable(system: str, method: str, cron: str, manual: str) -> ScheduleResult:
    return ScheduleResult(
        False,
        system,
        method,
        manual_command=manual,
        error=(
            f"Cron expression {cron!r} is not a simple daily schedule, so it "
            f"cannot be auto-translated for the {system} scheduler."
        ),
    )


def install_schedule(
    config: VaultConfig,
    *,
    runner: Runner = _default_runner,
    system: str | None = None,
    home: Path | None = None,
) -> ScheduleResult:
    """Register the nightly job on the host OS. Pure dispatch over OS installers."""
    system = system or normalize_system(sys.platform)
    vault = config.vault_path
    cron = config.sleep.cron
    cli = resolve_cli_command()

    if system == "windows":
        parsed = parse_daily_time(cron)
        if parsed is None:
            return _untranslatable(system, "schtasks", cron, "")
        return install_windows(cli, vault, *parsed, runner=runner)

    if system == "darwin":
        parsed = parse_daily_time(cron)
        if parsed is None:
            return _untranslatable(system, "launchd", cron, "")
        return install_macos(cli, vault, *parsed, runner=runner, home=home)

    if system == "linux":
        return install_linux(cli, vault, cron, runner=runner)

    return ScheduleResult(
        False,
        system,
        "none",
        error=f"Automatic scheduling is not supported on platform {sys.platform!r}.",
    )


def maybe_install_schedule(config: VaultConfig) -> ScheduleResult | None:
    """connect entry point. Returns None when scheduling is intentionally skipped.

    Skipped when SINCRON_BRAIN_NO_SCHEDULE is set (tests, power users wiring
    their own cron) or when sleep is disabled in config. Any unexpected error is
    swallowed into a failure ScheduleResult so install never crashes.
    """
    if os.environ.get(SKIP_ENV):
        return None
    if not config.sleep.enabled:
        return ScheduleResult(
            True, normalize_system(sys.platform), "none", skipped=True,
            detail="Sleep is disabled in _config.toml; nothing to schedule.",
        )
    try:
        return install_schedule(config)
    except Exception as e:  # never let scheduling break connect
        return ScheduleResult(
            False, normalize_system(sys.platform), "none", error=str(e)
        )


def render_ai_remediation(result: ScheduleResult, config: VaultConfig) -> str:
    """A machine-readable block for the AI assistant running the install.

    Deliberately delimited and prefixed with AI_ACTION_REQUIRED so the assisting
    model (Claude, GPT, any) notices it must finish the setup, with the exact
    command to run and how to verify it.
    """
    vault = config.vault_path
    sleep_cmd = _join(_sleep_argv(resolve_cli_command(), vault), windows=result.system == "windows")
    lines = [
        "<<<SINCRON_BRAIN_SCHEDULE_SETUP_NEEDED>>>",
        "AI_ACTION_REQUIRED: the nightly sleep/indexing job could NOT be scheduled "
        "automatically. Finish it for the user — no admin/root is required.",
        f"system: {result.system}",
        f"method: {result.method}",
        f"error: {result.error or 'unknown'}",
        f"goal: run `{sleep_cmd}` every day at the time in {config.config_file} "
        f"([sleep] cron = {config.sleep.cron!r}).",
    ]
    if result.manual_command:
        lines.append(f"fix: run this command, then confirm it exits 0:\n  {result.manual_command}")
    else:
        lines.append(
            "fix: translate the cron into the host scheduler "
            "(Windows schtasks / macOS launchd / Linux crontab) for the current user."
        )
    lines.append(
        "verify_after: `sincron-brain stats` and check the job exists in the OS scheduler."
    )
    lines.append("<<<END>>>")
    return "\n".join(lines)
