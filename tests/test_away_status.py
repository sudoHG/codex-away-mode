import shutil
from datetime import datetime, timezone

from codex_away_mode import status
from codex_away_mode.config import AppConfig, save_config
from codex_away_mode.state import StateStore


class FakePaths:
    def __init__(self, root):
        self.data_dir = root / "codex-away-mode"
        self.config_path = self.data_dir / "config.toml"
        self.install_state_path = self.data_dir / "install-state.sqlite"
        self.runtime_dir = root / "runtime"
        self.runtime_state_path = self.runtime_dir / "state.sqlite"


NOW = datetime(2026, 6, 20, 10, 0, tzinfo=timezone.utc)


def _active_session(
    store,
    *,
    cwd="/workspace/demo",
    deadline="2026-06-20T11:00:00+00:00",
    card_message_id="om_card_secret",
):
    session_id = store.create_away_session(
        project="Demo",
        cwd=cwd,
        task="Task",
        started_at="2026-06-20T09:00:00+00:00",
        deadline_at=deadline,
        codex_session_id="codex_session_1",
    )
    window_id = store.create_away_window(
        session_id=session_id,
        recipient_id="oc_private_chat_secret",
        card_message_id=card_message_id,
        created_at="2026-06-20T09:00:00+00:00",
        deadline_at=deadline,
    )
    return session_id, window_id


def test_away_status_lists_active_session_without_private_ids(tmp_path):
    paths = FakePaths(tmp_path)
    save_config(paths.config_path, AppConfig(feishu_chat_id="oc_private_chat_secret", route_key_verified=True))
    store = StateStore(paths.runtime_state_path)
    session_id, _window_id = _active_session(store)
    store.renew_waiter_lease(
        session_id,
        owner="waiter-1",
        now="2026-06-20T10:00:00+00:00",
        expires_at="2026-06-20T10:00:45+00:00",
    )

    result = status.run_away_status(paths, now=NOW)

    assert result["ok"] is True
    assert result["summary"]["active_count"] == 1
    item = result["sessions"][0]
    assert "session_id" not in item
    assert "active_window_id" not in item
    assert item["resume_allowed_for_current_turn"] is False
    assert item["project"] == "Demo"
    assert item["status"] == "active"
    assert item["active_card_present"] is True
    assert item["waiter_lease"]["status"] == "alive"
    dumped = str(result)
    assert "oc_private_chat_secret" not in dumped
    assert "om_card_secret" not in dumped


def test_away_status_debug_includes_internal_ids_but_not_resume_token(tmp_path):
    paths = FakePaths(tmp_path)
    save_config(paths.config_path, AppConfig(feishu_chat_id="oc_private_chat_secret", route_key_verified=True))
    store = StateStore(paths.runtime_state_path)
    session_id, window_id = _active_session(store)
    store.set_resume_token_hash(
        session_id=session_id,
        token_hash="sha256:secret-token-hash",
        created_at="2026-06-20T10:00:00+00:00",
    )

    result = status.run_away_status(paths, now=NOW, include_internal_ids=True)

    item = result["sessions"][0]
    assert item["session_id"] == session_id
    assert item["active_window_id"] == window_id
    dumped = str(result)
    assert "resume_token" not in dumped
    assert "secret-token-hash" not in dumped


def test_away_status_filters_by_session(tmp_path):
    paths = FakePaths(tmp_path)
    save_config(paths.config_path, AppConfig(route_key_verified=True))
    store = StateStore(paths.runtime_state_path)
    first, _ = _active_session(store, cwd="/workspace/one", card_message_id="om_card_one")
    second, _ = _active_session(store, cwd="/workspace/two", card_message_id="om_card_two")

    result = status.run_away_status(paths, session_id=second, now=NOW)

    assert [item["cwd"] for item in result["sessions"]] == ["/workspace/two"]
    assert first not in str(result)


def test_away_status_reports_active_session_without_window(tmp_path):
    paths = FakePaths(tmp_path)
    save_config(paths.config_path, AppConfig(route_key_verified=True))
    store = StateStore(paths.runtime_state_path)
    session_id = store.create_away_session(
        project="Demo",
        cwd="/workspace/demo",
        task="Task",
        started_at="2026-06-20T09:00:00+00:00",
        deadline_at="2026-06-20T11:00:00+00:00",
    )

    result = status.run_away_status(paths, session_id=session_id, now=NOW)

    assert "active_window_id" not in result["sessions"][0]
    assert "active_session_without_window" in result["sessions"][0]["warnings"]


def test_away_status_reports_expired_waiter_lease(tmp_path):
    paths = FakePaths(tmp_path)
    save_config(paths.config_path, AppConfig(route_key_verified=True))
    store = StateStore(paths.runtime_state_path)
    session_id, _ = _active_session(store)
    store.renew_waiter_lease(
        session_id,
        owner="waiter-1",
        now="2026-06-20T09:50:00+00:00",
        expires_at="2026-06-20T09:50:45+00:00",
    )

    result = status.run_away_status(paths, now=NOW)

    item = result["sessions"][0]
    assert item["waiter_lease"]["status"] == "expired"
    assert "expired_waiter_lease" in item["warnings"]


def test_away_status_reports_route_key_not_verified(tmp_path):
    paths = FakePaths(tmp_path)
    save_config(paths.config_path, AppConfig(route_key_verified=False, multi_window_enabled=True))
    store = StateStore(paths.runtime_state_path)
    _active_session(store)

    result = status.run_away_status(paths, now=NOW)

    assert "route_key_not_verified" in result["sessions"][0]["warnings"]


def test_away_status_missing_runtime_store_returns_empty_without_creating_db(tmp_path):
    paths = FakePaths(tmp_path)
    save_config(paths.config_path, AppConfig(route_key_verified=True))
    assert not paths.runtime_state_path.exists()

    result = status.run_away_status(paths, now=NOW)

    assert result["ok"] is True
    assert result["runtime_store_present"] is False
    assert result["sessions"] == []
    assert not paths.runtime_state_path.exists()


def test_install_state_survives_runtime_wipe(tmp_path):
    paths = FakePaths(tmp_path)
    save_config(paths.config_path, AppConfig(route_key_verified=True))
    install_store = StateStore(paths.install_state_path)
    install_store.set_install_state("e2e_notify", {"status": "verified"})
    install_store.set_route_key_state(
        status="verified",
        source="doctor_route_probe",
        verified_at="2026-06-20T09:00:00+00:00",
    )
    runtime_store = StateStore(paths.runtime_state_path)
    _active_session(runtime_store)
    shutil.rmtree(paths.runtime_dir)

    result = status.run_away_status(paths, now=NOW)

    assert result["runtime_store_present"] is False
    assert result["sessions"] == []
    assert StateStore(paths.install_state_path).get_install_state("e2e_notify")[
        "status"
    ] == "verified"
    assert StateStore(paths.install_state_path).route_key_state()["status"] == "verified"
    assert not paths.runtime_state_path.exists()
