import json

from typer.testing import CliRunner

from sincron_brain.benchmark import run_benchmark
from sincron_brain.cli import app

runner = CliRunner()


def test_run_benchmark_creates_vault_measures_core_operations(tmp_path):
    vault = tmp_path / "brain-benchmark"

    result = run_benchmark(
        vault,
        memories=12,
        drafts=3,
        force=False,
        render_viewer=True,
        run_sleep_job=True,
    )

    assert result["final_stats"]["total"] == 15
    assert result["draft_queue"] == 0
    assert result["storage"]["files"] >= 17
    assert result["storage"]["total_mb"] > 0
    assert (vault / "_config.toml").exists()
    assert (vault / "_viewer.html").exists()

    steps = {step["name"]: step for step in result["steps"]}
    assert steps["populate_memories"]["result"] == 12
    assert steps["queue_drafts"]["result"] == 3
    assert steps["sleep_now_create_only"]["result"]["processed"] == 3
    assert steps["stats"]["result"]["total"] == 15
    assert steps["list_major_tags"]["result_count"] > 0
    assert steps["list_common_tags_external_access"]["result_count"] > 0
    assert steps["search_api_key"]["result_count"] > 0
    assert steps["viewer_html"]["result"].endswith("_viewer.html")


def test_benchmark_cli_refuses_existing_path_without_force(tmp_path):
    vault = tmp_path / "brain-benchmark"
    vault.mkdir()

    result = runner.invoke(app, ["benchmark", "--path", str(vault), "--memories", "1"])

    assert result.exit_code == 1
    assert "already exists" in result.output


def test_benchmark_cli_json_output(tmp_path):
    vault = tmp_path / "brain-benchmark"

    result = runner.invoke(
        app,
        [
            "benchmark",
            "--path",
            str(vault),
            "--memories",
            "2",
            "--skip-viewer",
            "--json",
        ],
    )

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["requested_memories"] == 2
    assert payload["final_stats"]["total"] == 2
    assert payload["viewer_path"] is None
