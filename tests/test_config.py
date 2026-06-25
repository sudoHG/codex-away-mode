import os
import stat
from datetime import datetime, timezone

import pytest

from codex_away_mode.config import (
    AppConfig,
    RuntimePaths,
    RuntimeStateError,
    effective_notification_mode,
    load_config,
    prepare_runtime_dir,
    save_config,
)
from codex_away_mode.time import Clock, FakeClock


def test_runtime_paths_default_to_codex_away_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    codex_home = tmp_path / "codex-home"
    runtime_base = tmp_path / "tmp"
    runtime_base.mkdir()
    monkeypatch.delenv("CODEX_AWAY_HOME", raising=False)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    monkeypatch.setenv("TMPDIR", str(runtime_base))

    paths = RuntimePaths.from_environment()

    assert paths.codex_home == codex_home
    assert paths.away_home == home / ".codex-away-mode"
    assert paths.data_dir == home / ".codex-away-mode"
    assert paths.bin_dir == home / ".codex-away-mode" / "bin"
    assert paths.wrapper_path == home / ".codex-away-mode" / "bin" / "codex-away-mode"
    assert paths.scripts_dir == home / ".codex-away-mode" / "scripts"
    assert paths.skill_source_dir == home / ".codex-away-mode" / "skill"
    assert paths.skill_install_dir == codex_home / "skills" / "codex-away-mode"
    assert paths.config_path == home / ".codex-away-mode" / "config.toml"
    assert paths.install_state_path == home / ".codex-away-mode" / "install-state.sqlite"
    assert paths.runtime_dir == runtime_base / "codex-away-mode"
    assert paths.runtime_state_path == runtime_base / "codex-away-mode" / "state.sqlite"
    assert paths.runtime_prompt_marker_dir == runtime_base / "codex-away-mode" / "user-turns"
    assert paths.runtime_summary_dir == runtime_base / "codex-away-mode" / "summaries"
    assert not hasattr(paths, "state_path")
    assert not hasattr(paths, "summary_path")
    assert not hasattr(paths, "prompt_marker_dir")
    assert paths.log_dir == home / ".codex-away-mode" / "logs"
    assert paths.backup_dir == home / ".codex-away-mode" / "backups"
    assert paths.hooks_json == codex_home / "hooks.json"
    assert paths.global_agents == codex_home / "AGENTS.md"


def test_runtime_paths_respect_codex_away_home_override(tmp_path, monkeypatch):
    codex_home = tmp_path / "codex-home"
    away_home = tmp_path / "custom-away-home"
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    monkeypatch.setenv("CODEX_AWAY_HOME", str(away_home))

    paths = RuntimePaths.from_environment()

    assert paths.away_home == away_home
    assert paths.data_dir == away_home
    assert paths.wrapper_path == away_home / "bin" / "codex-away-mode"
    assert paths.config_path == away_home / "config.toml"
    assert paths.install_state_path == away_home / "install-state.sqlite"
    assert paths.skill_source_dir == away_home / "skill"
    assert paths.skill_install_dir == codex_home / "skills" / "codex-away-mode"


def test_runtime_paths_respect_codex_away_runtime_dir(tmp_path, monkeypatch):
    codex_home = tmp_path / "codex-home"
    away_home = tmp_path / "away-home"
    runtime_dir = tmp_path / "runtime"
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    monkeypatch.setenv("CODEX_AWAY_HOME", str(away_home))
    monkeypatch.setenv("CODEX_AWAY_RUNTIME_DIR", str(runtime_dir))

    paths = RuntimePaths.from_environment()

    assert paths.runtime_dir == runtime_dir
    assert paths.runtime_state_path == runtime_dir / "state.sqlite"
    assert paths.install_state_path == away_home / "install-state.sqlite"


def test_prepare_runtime_dir_uses_private_permissions(tmp_path):
    runtime_dir = tmp_path / "runtime"

    prepared = prepare_runtime_dir(runtime_dir)

    assert prepared == runtime_dir
    mode = stat.S_IMODE(os.stat(runtime_dir).st_mode)
    assert mode == 0o700


def test_prepare_runtime_dir_rejects_open_existing_dir(tmp_path):
    runtime_dir = tmp_path / "runtime"
    runtime_dir.mkdir(mode=0o755)

    with pytest.raises(RuntimeStateError) as exc:
        prepare_runtime_dir(runtime_dir)

    assert exc.value.error_code == "runtime_state_unwritable"
    assert exc.value.detail == "runtime_dir_permissions_too_open"


def test_prepare_runtime_dir_rejects_foreign_owned_existing_dir(
    tmp_path, monkeypatch
):
    runtime_dir = tmp_path / "runtime"
    runtime_dir.mkdir(mode=0o700)
    foreign_uid = os.stat(runtime_dir).st_uid + 1000
    monkeypatch.setattr("codex_away_mode.config.os.getuid", lambda: foreign_uid)

    with pytest.raises(RuntimeStateError) as exc:
        prepare_runtime_dir(runtime_dir)

    assert exc.value.error_code == "runtime_state_unwritable"
    assert exc.value.detail == "runtime_dir_not_owned_by_user"


def test_runtime_dir_override_must_be_absolute(monkeypatch, tmp_path):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-home"))
    monkeypatch.setenv("CODEX_AWAY_RUNTIME_DIR", "relative-runtime")

    with pytest.raises(RuntimeStateError) as exc:
        RuntimePaths.from_environment()

    assert exc.value.detail == "runtime_dir_not_absolute"


def test_app_config_defaults():
    config = AppConfig()

    assert config.notification_mode == "all"
    assert config.snooze_until is None
    assert config.feishu_user_id is None
    assert config.feishu_chat_id is None
    assert config.feishu_bot_name is None
    assert config.lark_cli_path == "lark-cli"
    assert config.persist_reply_text is False
    assert config.lightweight_logs is True
    assert config.default_wait_minutes == 30
    assert config.pre_timeout_reminder_minutes == 5
    assert config.extend_minutes == 30
    assert config.poll_interval_seconds == 5
    assert config.multi_window_enabled is True
    assert config.route_key_verified is False
    assert config.capture_hook_payloads is False


def test_save_load_escapes_toml_strings_and_uses_0600_permissions(tmp_path):
    path = tmp_path / "config.toml"
    original = AppConfig(feishu_bot_name='Codex "助手" \\ test')

    save_config(path, original)
    loaded = load_config(path)

    assert loaded.feishu_bot_name == original.feishu_bot_name
    mode = stat.S_IMODE(os.stat(path).st_mode)
    assert mode == 0o600


def test_effective_notification_mode_is_pure_for_expired_snooze(tmp_path):
    path = tmp_path / "config.toml"
    config = AppConfig(notification_mode="off", snooze_until="2026-06-18T09:00:00+00:00")
    save_config(path, config)
    before = path.read_text()

    mode = effective_notification_mode(
        load_config(path),
        now=datetime(2026, 6, 18, 10, 0, tzinfo=timezone.utc),
    )

    assert mode == "all"
    assert path.read_text() == before


def test_effective_notification_mode_respects_active_snooze():
    config = AppConfig(notification_mode="all", snooze_until="2026-06-18T11:00:00+00:00")

    mode = effective_notification_mode(
        config,
        now=datetime(2026, 6, 18, 10, 0, tzinfo=timezone.utc),
    )

    assert mode == "off"


def test_fake_clock_advances_without_real_sleep():
    start = datetime(2026, 6, 18, 10, 0, tzinfo=timezone.utc)
    clock: Clock = FakeClock(start)

    assert clock.now() == start
    clock.sleep(12.5)
    assert clock.now() == datetime(2026, 6, 18, 10, 0, 12, 500000, tzinfo=timezone.utc)
