"""The viewer is opt-in: state-changing tools refresh it only when it exists."""

from sincron_brain import server, storage
from sincron_brain.config import VaultConfig
from sincron_brain.models import Memory
from sincron_brain.viewer import write_viewer


def make_config(tmp_path) -> VaultConfig:
    config = VaultConfig(vault_path=tmp_path)
    storage.ensure_vault(config)
    return config


def test_refresh_viewer_is_noop_when_viewer_absent(tmp_path):
    config = make_config(tmp_path)
    viewer_path = config.vault_path / server.VIEWER_FILENAME
    assert not viewer_path.exists()

    server._refresh_viewer_if_exists(config)

    assert not viewer_path.exists()


def test_refresh_viewer_regenerates_existing_file(tmp_path):
    config = make_config(tmp_path)
    viewer_path = write_viewer(config)
    initial = viewer_path.read_text(encoding="utf-8")

    with storage.open_db(config) as conn:
        storage.write_memory(
            config,
            Memory(
                id="new-memory",
                major_tags=["soul"],
                synopsis="Be precise and warm.",
                content="durable posture",
            ),
            conn,
        )

    server._refresh_viewer_if_exists(config)

    updated = viewer_path.read_text(encoding="utf-8")
    assert updated != initial
    assert "Be precise and warm." in updated


def test_refresh_viewer_failure_is_logged_not_raised(tmp_path, monkeypatch):
    config = make_config(tmp_path)
    write_viewer(config)

    def boom(_config):
        raise RuntimeError("viewer broken")

    monkeypatch.setattr("sincron_brain.viewer.write_viewer", boom)

    server._refresh_viewer_if_exists(config)

    events = [event["event"] for event in storage.read_audit(config)]
    assert "viewer.refresh_failed" in events
