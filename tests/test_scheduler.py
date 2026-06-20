"""Tests for OS-level nightly-sleep scheduling.

OS installers are exercised with an injected fake runner so no real Scheduled
Task / LaunchAgent / crontab entry is ever created on the host running the suite.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from typer.testing import CliRunner

from sincron_brain import scheduler
from sincron_brain.cli import app
from sincron_brain.config import load_config

runner = CliRunner()


@dataclass
class FakeProc:
    returncode: int = 0
    stdout: str = ""
    stderr: str = ""


@dataclass
class FakeRunner:
    """Records calls and returns scripted results keyed by the first two argv tokens."""

    results: dict = field(default_factory=dict)
    calls: list = field(default_factory=list)
    default: FakeProc = field(default_factory=FakeProc)

    def __call__(self, cmd, input=None):
        self.calls.append({"cmd": cmd, "input": input})
        key = " ".join(cmd[:2])
        return self.results.get(key, self.default)


def _config(tmp_path: Path, cron: str = "0 3 * * *"):
    """Build a vault config via connect, but never let that connect schedule a
    real OS task — these tests drive (maybe_)install_schedule explicitly and a
    couple of them delenv the global guard, so force it just for this connect."""
    project = tmp_path / "project"
    vault = tmp_path / "memory"
    project.mkdir()
    prev = os.environ.get(scheduler.SKIP_ENV)
    os.environ[scheduler.SKIP_ENV] = "1"
    try:
        runner.invoke(app, ["connect", "--path", str(vault), "--project", str(project)])
    finally:
        if prev is None:
            os.environ.pop(scheduler.SKIP_ENV, None)
        else:
            os.environ[scheduler.SKIP_ENV] = prev
    config = load_config(vault)
    config.sleep.cron = cron
    return config


# --- pure helpers ---------------------------------------------------------


def test_parse_daily_time_simple():
    assert scheduler.parse_daily_time("0 3 * * *") == (3, 0)
    assert scheduler.parse_daily_time("30 22 * * *") == (22, 30)


def test_parse_daily_time_rejects_non_daily():
    assert scheduler.parse_daily_time("*/30 * * * *") is None
    assert scheduler.parse_daily_time("0 3 * * 1") is None
    assert scheduler.parse_daily_time("0 3 1 * *") is None
    assert scheduler.parse_daily_time("bad") is None
    assert scheduler.parse_daily_time("99 3 * * *") is None


def test_normalize_system():
    assert scheduler.normalize_system("win32") == "windows"
    assert scheduler.normalize_system("darwin") == "darwin"
    assert scheduler.normalize_system("linux") == "linux"
    assert scheduler.normalize_system("freebsd") == "unknown"


def test_job_id_is_deterministic_and_vault_specific():
    a = scheduler.job_id(Path("/vault/a"))
    b = scheduler.job_id(Path("/vault/b"))
    assert a == scheduler.job_id(Path("/vault/a"))
    assert a != b
    assert a.startswith("SincronBrainSleep_")


# --- Windows --------------------------------------------------------------


def test_install_windows_builds_schtasks_command(tmp_path):
    cli = ["sincron-brain"]
    vault = tmp_path / "memory"
    fake = FakeRunner()

    result = scheduler.install_windows(cli, vault, 3, 0, runner=fake)

    assert result.ok
    assert result.method == "schtasks"
    cmd = fake.calls[0]["cmd"]
    assert cmd[0] == "schtasks"
    assert "/Create" in cmd and "/F" in cmd
    assert "/ST" in cmd and "03:00" in cmd
    action = cmd[cmd.index("/TR") + 1]
    assert "sleep-now" in action
    assert "--vault" in action
    assert str(vault) in action


def test_install_windows_failure_surfaces_error(tmp_path):
    fake = FakeRunner(results={"schtasks /Create": FakeProc(returncode=1, stderr="access denied")})
    result = scheduler.install_windows(["sincron-brain"], tmp_path / "m", 3, 0, runner=fake)
    assert not result.ok
    assert result.error is not None and "access denied" in result.error
    assert result.manual_command.startswith("schtasks")


# --- macOS ----------------------------------------------------------------


def test_install_macos_writes_plist_and_loads(tmp_path):
    home = tmp_path / "home"
    vault = tmp_path / "memory"
    fake = FakeRunner()

    result = scheduler.install_macos(
        ["sincron-brain"], vault, 3, 0, runner=fake, home=home
    )

    assert result.ok
    plists = list((home / "Library" / "LaunchAgents").glob("*.plist"))
    assert len(plists) == 1
    body = plists[0].read_text(encoding="utf-8")
    assert "<integer>3</integer>" in body
    assert "sleep-now" in body
    assert str(vault) in body
    # unload-then-load reload sequence
    assert [c["cmd"][0] for c in fake.calls] == ["launchctl", "launchctl"]


# --- Linux ----------------------------------------------------------------


def test_install_linux_appends_crontab_entry(tmp_path):
    fake = FakeRunner(results={"crontab -l": FakeProc(returncode=1, stderr="no crontab")})
    vault = tmp_path / "memory"

    result = scheduler.install_linux(["sincron-brain"], vault, "0 3 * * *", runner=fake)

    assert result.ok
    write_call = fake.calls[-1]
    assert write_call["cmd"] == ["crontab", "-"]
    written = write_call["input"]
    assert "0 3 * * *" in written
    assert "sleep-now" in written
    assert scheduler.CRON_MARKER in written


def test_install_linux_is_idempotent(tmp_path):
    vault = tmp_path / "memory"
    marker = f"# {scheduler.CRON_MARKER}:{scheduler.job_id(vault)}"
    stale = f"0 9 * * * sincron-brain sleep-now --vault {vault} {marker}"
    existing = f"# unrelated\n0 0 * * * other-job\n{stale}\n"
    fake = FakeRunner(results={"crontab -l": FakeProc(returncode=0, stdout=existing)})

    result = scheduler.install_linux(["sincron-brain"], vault, "0 3 * * *", runner=fake)

    assert result.ok
    written = fake.calls[-1]["input"]
    # the stale entry for this vault is replaced, not duplicated
    assert written.count(marker) == 1
    assert "0 9 * * *" not in written
    assert "0 3 * * *" in written
    # unrelated lines are preserved
    assert "other-job" in written


# --- dispatch + translation ----------------------------------------------


def test_install_schedule_dispatches_by_system(tmp_path):
    config = _config(tmp_path)
    fake = FakeRunner()

    result = scheduler.install_schedule(config, runner=fake, system="linux")
    assert result.ok and result.method == "crontab"


def test_install_schedule_untranslatable_cron_returns_remediation(tmp_path):
    config = _config(tmp_path, cron="*/30 * * * *")
    fake = FakeRunner()

    result = scheduler.install_schedule(config, runner=fake, system="windows")

    assert not result.ok
    assert result.error is not None and "not a simple daily schedule" in result.error
    assert fake.calls == []  # never attempted to run schtasks


def test_install_schedule_unknown_platform(tmp_path):
    config = _config(tmp_path)
    result = scheduler.install_schedule(config, runner=FakeRunner(), system="unknown")
    assert not result.ok
    assert result.method == "none"


# --- maybe_install_schedule guards ---------------------------------------


def test_maybe_install_schedule_skips_when_env_set(tmp_path, monkeypatch):
    monkeypatch.setenv(scheduler.SKIP_ENV, "1")
    config = _config(tmp_path)
    assert scheduler.maybe_install_schedule(config) is None


def test_maybe_install_schedule_skips_when_sleep_disabled(tmp_path, monkeypatch):
    monkeypatch.delenv(scheduler.SKIP_ENV, raising=False)
    config = _config(tmp_path)
    config.sleep.enabled = False
    result = scheduler.maybe_install_schedule(config)
    assert result is not None and result.skipped


# --- AI remediation block -------------------------------------------------


def test_render_ai_remediation_contains_actionable_fields(tmp_path):
    config = _config(tmp_path)
    result = scheduler.ScheduleResult(
        ok=False,
        system="linux",
        method="crontab",
        manual_command="(crontab -l; echo x) | crontab -",
        error="crontab not found",
    )
    block = scheduler.render_ai_remediation(result, config)
    assert "AI_ACTION_REQUIRED" in block
    assert "SINCRON_BRAIN_SCHEDULE_SETUP_NEEDED" in block
    assert "crontab not found" in block
    assert "sleep-now" in block
    assert "(crontab -l; echo x) | crontab -" in block
