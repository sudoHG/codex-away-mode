import json
import sqlite3
from types import SimpleNamespace

from codex_away_mode import doctor, hook_trust, install, notify, uninstall
from codex_away_mode.config import AppConfig, load_config, save_config
from codex_away_mode.lark import LarkMessage, SendResult
from codex_away_mode.state import StateStore
from codex_away_mode.time import FakeClock


class FakePaths:
    def __init__(self, root):
        self.codex_home = root
        self.away_home = root / ".codex-away-mode"
        self.data_dir = self.away_home
        self.bin_dir = self.data_dir / "bin"
        self.wrapper_path = self.bin_dir / "codex-away-mode"
        self.scripts_dir = self.data_dir / "scripts"
        self.skill_source_dir = self.data_dir / "skill"
        self.config_path = self.data_dir / "config.toml"
        self.install_state_path = self.data_dir / "install-state.sqlite"
        self.codex_config_path = root / "config.toml"
        self.runtime_dir = root / "runtime"
        self.runtime_state_path = self.runtime_dir / "state.sqlite"
        self.runtime_prompt_marker_dir = self.runtime_dir / "user-turns"
        self.runtime_summary_dir = self.runtime_dir / "summaries"
        self.backup_dir = self.data_dir / "backups"
        self.hooks_json = root / "hooks.json"
        self.global_agents = root / "AGENTS.md"
        self.skill_install_dir = root / "skills" / "codex-away-mode"


class FakeLark:
    def __init__(self, messages=None, test_chat_id="oc_test_chat"):
        self.cards = []
        self.list_calls = []
        self.messages = list(messages or [])
        self.test_chat_id = test_chat_id

    def send_interactive_card(self, *, card, user_id=None, chat_id=None):
        self.cards.append({"card": card, "user_id": user_id, "chat_id": chat_id})
        return SendResult(message_id="om_probe_card", chat_id=chat_id or "oc_sent_chat")

    def list_messages(self, *, chat_id, page_size=50):
        self.list_calls.append({"chat_id": chat_id, "page_size": page_size})
        return list(self.messages)

    def send_test_notification(self):
        return SimpleNamespace(chat_id=self.test_chat_id)


def managed_hooks_payload():
    return {
        "hooks": {
            "Stop": [
                {
                    "hooks": [
                        {
                            "statusMessage": "Codex Away Mode managed hook",
                            "command": "codex-away-mode notify stop --json",
                        }
                    ]
                }
            ],
            "UserPromptSubmit": [
                {
                    "hooks": [
                        {
                            "statusMessage": "Codex Away Mode managed hook",
                            "command": "codex-away-mode notify mark-prompt --json",
                        }
                    ]
                }
            ],
            "PermissionRequest": [
                {
                    "hooks": [
                        {
                            "statusMessage": "Codex Away Mode managed hook",
                            "command": "codex-away-mode notify permission-request --hook-json",
                        }
                    ]
                }
            ],
        }
    }


def user_message(message_id, reply_to):
    return LarkMessage(
        message_id=message_id,
        reply_to=reply_to,
        msg_type="text",
        content_text="收到",
        sender_type="user",
        create_time="2026-06-18T10:00:01Z",
    )


def remove_sqlite_files(path):
    for candidate in (path, path.with_name(path.name + "-wal"), path.with_name(path.name + "-shm")):
        if candidate.exists():
            candidate.unlink()


def mark_notify_delivery_verified(paths):
    StateStore(paths.install_state_path).set_install_state(
        "e2e_notify",
        {
            "status": "verified",
            "scope": "notify_delivery_only",
            "verified_at": "2026-06-18T10:00:00Z",
            "cwd": "/workspace/demo",
            "summary_key": StateStore.cwd_hash("/workspace/demo"),
            "message_id": "om_e2e",
            "hooks_fingerprint": doctor.hooks_fingerprint(paths),
        },
    )


def record_stop_hook_invocation(paths):
    StateStore(paths.runtime_state_path).record_diagnostic_event(
        event_kind="codex_hook_invocation",
        severity="info",
        message="Stop hook executed.",
        detail={
            "hook_event_name": "Stop",
            "hooks_fingerprint": doctor.hooks_fingerprint(paths),
        },
        created_at="2026-06-18T10:01:00Z",
    )


def record_permission_request_hook_invocation(paths):
    StateStore(paths.runtime_state_path).record_diagnostic_event(
        event_kind="codex_hook_invocation",
        severity="info",
        message="PermissionRequest hook executed.",
        detail={
            "hook_event_name": "PermissionRequest",
            "hooks_fingerprint": doctor.hooks_fingerprint(paths),
        },
        created_at="2026-06-18T10:01:00Z",
    )


def write_codex_hook_state(
    paths,
    *,
    stop_enabled=True,
    prompt_enabled=True,
    permission_enabled=True,
):
    path = paths.codex_config_path
    path.parent.mkdir(parents=True, exist_ok=True)

    def block(event_key, enabled):
        trust_key = f"{paths.hooks_json.resolve()}:{event_key}:0:0"
        lines = [
            f'[hooks.state."{trust_key}"]',
            'trusted_hash = "sha256:test"',
        ]
        if enabled is not None:
            lines.append(f"enabled = {'true' if enabled else 'false'}")
        return "\n".join(lines)

    path.write_text(
        block("user_prompt_submit", prompt_enabled)
        + "\n\n"
        + block("stop", stop_enabled)
        + "\n\n"
        + block("permission_request", permission_enabled)
        + "\n",
        encoding="utf-8",
    )


def test_missing_config_reports_config_failed_and_next_step(tmp_path):
    report = doctor.run_doctor(FakePaths(tmp_path))

    assert report["ok"] is False
    assert report["failed_codes"] == ["local_config_missing"]
    assert "local_config" not in report["passed_codes"]
    assert "config" in report["next_step"]


def test_existing_config_plus_sqlite_passes_config_and_sqlite_checks(tmp_path):
    paths = FakePaths(tmp_path)
    save_config(paths.config_path, AppConfig(feishu_chat_id="oc_test_chat"))
    paths.wrapper_path.parent.mkdir(parents=True)
    paths.wrapper_path.write_text("#!/bin/sh\n", encoding="utf-8")
    paths.wrapper_path.chmod(0o755)
    install.run_install(paths, yes=True, cli_command=str(paths.wrapper_path))

    report = doctor.run_doctor(paths)

    assert "local_config" in report["passed_codes"]
    assert "sqlite" in report["passed_codes"]
    assert "notify_delivery_unverified" in report["degraded_codes"]
    assert "hook_trust_unverified" not in report["degraded_codes"]
    assert "hook_trust_pending" not in report["failed_codes"]
    assert "通知发送链路" in report["next_step"]


def test_doctor_reports_sqlite_failure_when_install_store_unavailable(tmp_path, monkeypatch):
    paths = FakePaths(tmp_path)
    save_config(paths.config_path, AppConfig(feishu_chat_id="oc_test_chat"))
    paths.hooks_json.write_text(
        json.dumps(managed_hooks_payload()),
        encoding="utf-8",
    )

    def raise_store_error(_paths):
        raise RuntimeError("readonly install store")

    monkeypatch.setattr(doctor, "open_install_store", raise_store_error)

    report = doctor.run_doctor(paths)

    assert report["ok"] is False
    assert "local_config" in report["passed_codes"]
    assert "sqlite" in report["failed_codes"]
    assert "readonly install store" in " ".join(report["warnings"])


def test_missing_feishu_chat_id_reports_binding_failed(tmp_path):
    paths = FakePaths(tmp_path)
    save_config(paths.config_path, AppConfig())

    report = doctor.run_doctor(paths)

    assert report["ok"] is False
    assert "feishu_chat_id_missing" in report["failed_codes"]
    assert "feishu_chat_id" in report["next_step"]


def test_doctor_e2e_notify_stages_summary_without_marking_hook_installed(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(notify.tempfile, "gettempdir", lambda: "/not-the-pytest-temp")
    monkeypatch.setattr(notify, "skip_cwd_reason", lambda paths, cwd: None)
    paths = FakePaths(tmp_path)
    cwd = tmp_path / "workspace"
    cwd.mkdir()
    save_config(paths.config_path, AppConfig(feishu_chat_id="oc_test_chat"))
    paths.wrapper_path.parent.mkdir(parents=True)
    paths.wrapper_path.write_text("#!/bin/sh\n", encoding="utf-8")
    paths.wrapper_path.chmod(0o755)
    lark = FakeLark()

    report = doctor.run_doctor(
        paths,
        e2e_notify=True,
        lark=lark,
        cwd=str(cwd),
        clock=FakeClock(doctor.parse_utc("2026-06-18T10:00:00Z")),
    )

    assert report["ok"] is True
    assert "notify_delivery_verified" in report["passed_codes"]
    assert lark.cards[0]["chat_id"] == "oc_test_chat"
    assert not (cwd / ".codex-away-mode" / "latest-summary.md").exists()
    from codex_away_mode.state import StateStore

    store = StateStore(paths.install_state_path)
    assert store.install_status()["status"] != "installed"
    e2e_state = store.get_install_state("e2e_notify")
    assert e2e_state["status"] == "verified"
    assert e2e_state["scope"] == "notify_delivery_only"
    assert e2e_state["cwd"] == str(cwd)
    assert e2e_state["summary_key"] == StateStore.cwd_hash(str(cwd))
    assert e2e_state["hooks_fingerprint"].startswith("sha256:")
    assert "设置 -> 钩子" in report["next_step"]
    assert "Hook 信任状态" in report["next_step"]
    assert "Stop hook can record execution" not in report["next_step"]


def test_doctor_requires_current_hook_trust_after_notify_delivery_verified(tmp_path):
    paths = FakePaths(tmp_path)
    save_config(paths.config_path, AppConfig(feishu_chat_id="oc_test_chat"))
    install.run_install(paths, yes=True, cli_command="/bin/codex-away-mode")
    mark_notify_delivery_verified(paths)

    report = doctor.run_doctor(paths)

    assert report["ok"] is False
    assert "notify_delivery_verified" in report["passed_codes"]
    assert "hook_trust_missing" in report["failed_codes"]
    assert "hook_trust_static_inconclusive" not in report["warnings"]
    assert "设置 -> 钩子" in report["next_step"]


def test_doctor_passes_after_notify_delivery_and_current_hook_trust(tmp_path):
    paths = FakePaths(tmp_path)
    save_config(paths.config_path, AppConfig(feishu_chat_id="oc_test_chat"))
    install.run_install(paths, yes=True, cli_command="/bin/codex-away-mode")
    mark_notify_delivery_verified(paths)
    write_codex_hook_state(paths)

    report = doctor.run_doctor(paths)

    assert report["ok"] is True
    assert "notify_delivery_verified" in report["passed_codes"]
    assert "hook_trust_verified" in report["passed_codes"]
    assert "hook_execution_verified" not in report["passed_codes"]
    assert StateStore(paths.install_state_path).install_status()["status"] == "installed"


def test_doctor_accepts_trusted_hash_without_enabled_for_current_codex_hook_state(tmp_path):
    paths = FakePaths(tmp_path)
    save_config(paths.config_path, AppConfig(feishu_chat_id="oc_test_chat"))
    install.run_install(paths, yes=True, cli_command="/bin/codex-away-mode")
    mark_notify_delivery_verified(paths)
    write_codex_hook_state(
        paths,
        stop_enabled=None,
        prompt_enabled=None,
        permission_enabled=None,
    )

    report = doctor.run_doctor(paths)

    assert report["ok"] is True
    assert "notify_delivery_verified" in report["passed_codes"]
    assert "hook_trust_verified" in report["passed_codes"]
    assert "hook_trust_missing" not in report["failed_codes"]
    assert report["hook_trust"]["stop"]["status"] == "trust_record_present"
    assert report["hook_trust"]["user_prompt_submit"]["status"] == "trust_record_present"
    assert report["hook_trust"]["permission_request"]["status"] == "trust_record_present"
    assert StateStore(paths.install_state_path).install_status()["status"] == "installed"


def test_doctor_accepts_permission_request_trust_hash_without_enabled_before_first_run(tmp_path):
    paths = FakePaths(tmp_path)
    save_config(paths.config_path, AppConfig(feishu_chat_id="oc_test_chat"))
    install.run_install(paths, yes=True, cli_command="/bin/codex-away-mode")
    mark_notify_delivery_verified(paths)
    write_codex_hook_state(paths, permission_enabled=None)

    report = doctor.run_doctor(paths)

    assert report["ok"] is True
    assert "notify_delivery_verified" in report["passed_codes"]
    assert "hook_trust_verified" in report["passed_codes"]
    assert "hook_trust_missing" not in report["failed_codes"]
    assert report["hook_trust"]["permission_request"]["status"] == "trust_record_present"
    assert StateStore(paths.install_state_path).install_status()["status"] == "installed"


def test_doctor_accepts_permission_request_missing_enabled_after_runtime_invocation(tmp_path):
    paths = FakePaths(tmp_path)
    save_config(paths.config_path, AppConfig(feishu_chat_id="oc_test_chat"))
    install.run_install(paths, yes=True, cli_command="/bin/codex-away-mode")
    mark_notify_delivery_verified(paths)
    write_codex_hook_state(paths, permission_enabled=None)
    record_permission_request_hook_invocation(paths)

    report = doctor.run_doctor(paths)

    assert report["ok"] is True
    assert "notify_delivery_verified" in report["passed_codes"]
    assert "hook_trust_verified" in report["passed_codes"]
    assert report["hook_trust"]["permission_request"]["status"] == "observed"
    assert "hook_trust_missing" not in report["failed_codes"]
    assert StateStore(paths.install_state_path).install_status()["status"] == "installed"


def test_doctor_finds_recent_permission_request_invocation_after_many_old_events(tmp_path):
    paths = FakePaths(tmp_path)
    save_config(paths.config_path, AppConfig(feishu_chat_id="oc_test_chat"))
    install.run_install(paths, yes=True, cli_command="/bin/codex-away-mode")
    mark_notify_delivery_verified(paths)
    write_codex_hook_state(paths, permission_enabled=None)
    store = StateStore(paths.runtime_state_path)
    for index in range(60):
        store.record_diagnostic_event(
            event_kind="codex_hook_invocation",
            severity="info",
            message="Stop hook executed.",
            detail={
                "hook_event_name": "Stop",
                "hooks_fingerprint": doctor.hooks_fingerprint(paths),
            },
            created_at=f"2026-06-18T09:{index:02d}:00Z",
        )
    record_permission_request_hook_invocation(paths)

    report = doctor.run_doctor(paths)

    assert report["ok"] is True
    assert report["hook_trust"]["permission_request"]["status"] == "observed"


def test_doctor_warns_about_stale_runtime_without_cleanup(tmp_path):
    paths = FakePaths(tmp_path)
    save_config(paths.config_path, AppConfig(feishu_chat_id="oc_test_chat"))
    install.run_install(paths, yes=True, cli_command="/bin/codex-away-mode")
    mark_notify_delivery_verified(paths)
    write_codex_hook_state(paths)
    runtime_store = StateStore(paths.runtime_state_path)
    session_id = runtime_store.create_away_session(
        project="Stale",
        cwd="/workspace/stale",
        task="wait",
        started_at="2026-06-18T09:00:00Z",
        deadline_at="2026-06-18T09:30:00Z",
    )
    runtime_store.create_away_window(
        session_id=session_id,
        recipient_id="oc_secret_chat",
        card_message_id="om_stale_card",
        created_at="2026-06-18T09:00:00Z",
        deadline_at="2026-06-18T09:30:00Z",
    )

    report = doctor.run_doctor(paths)

    assert report["ok"] is True
    assert "runtime_stale_sessions_present" in report["warnings"]
    assert runtime_store.get_away_session(session_id)["status"] == "active"
    assert runtime_store.get_away_session(session_id)["close_reason"] is None


def test_doctor_reports_disabled_hook_even_when_old_stop_invocation_exists(tmp_path):
    paths = FakePaths(tmp_path)
    save_config(paths.config_path, AppConfig(feishu_chat_id="oc_test_chat"))
    install.run_install(paths, yes=True, cli_command="/bin/codex-away-mode")
    mark_notify_delivery_verified(paths)
    record_stop_hook_invocation(paths)
    write_codex_hook_state(paths, stop_enabled=False, prompt_enabled=False)

    report = doctor.run_doctor(paths)

    assert report["ok"] is False
    assert "notify_delivery_verified" in report["passed_codes"]
    assert "hook_trust_disabled" in report["failed_codes"]
    assert "hook_trust_verified" not in report["passed_codes"]
    assert "hook_execution_verified" not in report["passed_codes"]
    assert "设置 -> 钩子" in report["next_step"]
    assert StateStore(paths.install_state_path).install_status()["status"] != "installed"


def test_doctor_warns_not_fails_when_codex_hook_state_format_unknown(tmp_path):
    paths = FakePaths(tmp_path)
    save_config(paths.config_path, AppConfig(feishu_chat_id="oc_test_chat"))
    install.run_install(paths, yes=True, cli_command="/bin/codex-away-mode")
    mark_notify_delivery_verified(paths)
    record_stop_hook_invocation(paths)
    paths.codex_config_path.write_text(
        '[hooks.state]\nunsupported = "shape"\n',
        encoding="utf-8",
    )

    report = doctor.run_doctor(paths)

    assert report["ok"] is True
    assert "hook_trust_state_unknown_format" in report["warnings"]
    assert "hook_trust_disabled" not in report["failed_codes"]
    assert StateStore(paths.install_state_path).install_status()["status"] != "installed"


def test_doctor_reports_duplicate_managed_hooks_after_migration_failure(tmp_path):
    paths = FakePaths(tmp_path)
    save_config(paths.config_path, AppConfig(feishu_chat_id="oc_test_chat"))
    install.run_install(paths, yes=True, cli_command="/bin/codex-away-mode")
    data = json.loads(paths.hooks_json.read_text(encoding="utf-8"))
    data["hooks"]["Stop"].append(
        {
            "hooks": [
                {
                    "type": "command",
                    "command": "/old/codex-away-mode notify stop --json",
                    "timeout": 30,
                    "statusMessage": "Codex Away Mode managed hook",
                }
            ]
        }
    )
    paths.hooks_json.write_text(json.dumps(data), encoding="utf-8")

    report = doctor.run_doctor(paths)

    assert report["ok"] is False
    assert "hooks_duplicate" in report["failed_codes"]


def test_reinstall_invalidates_e2e_verified(tmp_path):
    paths = FakePaths(tmp_path)
    save_config(paths.config_path, AppConfig(feishu_chat_id="oc_test_chat"))
    install.run_install(paths, yes=True, cli_command="/bin/codex-away-mode")
    store = StateStore(paths.install_state_path)
    store.set_install_state(
        "e2e_notify",
        {
            "status": "verified",
            "verified_at": "2026-06-18T10:00:00Z",
            "cwd": "/workspace/demo",
            "summary_key": StateStore.cwd_hash("/workspace/demo"),
            "message_id": "om_e2e",
            "hooks_fingerprint": doctor.hooks_fingerprint(paths),
        },
    )

    install.run_install(paths, yes=True, cli_command="/bin/codex-away-mode-new")

    e2e_state = StateStore(paths.install_state_path).get_install_state("e2e_notify")
    assert e2e_state["status"] == "invalidated"
    assert e2e_state["invalidated_reason"] == "hooks_rewritten"


def test_doctor_ignores_e2e_verified_when_hooks_fingerprint_mismatch(tmp_path):
    paths = FakePaths(tmp_path)
    save_config(paths.config_path, AppConfig(feishu_chat_id="oc_test_chat"))
    install.run_install(paths, yes=True, cli_command="/bin/codex-away-mode")
    StateStore(paths.install_state_path).set_install_state(
        "e2e_notify",
        {
            "status": "verified",
            "verified_at": "2026-06-18T10:00:00Z",
            "cwd": "/workspace/demo",
            "summary_key": StateStore.cwd_hash("/workspace/demo"),
            "message_id": "om_e2e",
            "hooks_fingerprint": "sha256:old",
        },
    )

    report = doctor.run_doctor(paths)

    assert report["ok"] is False
    assert "e2e_notify_verified" not in report["passed_codes"]
    assert "notify_delivery_stale" in report["degraded_codes"]


def test_legacy_global_state_install_rows_migrate_to_install_state(tmp_path):
    paths = FakePaths(tmp_path)
    save_config(paths.config_path, AppConfig(feishu_chat_id="oc_test_chat"))
    install.run_install(paths, yes=True, cli_command="/bin/codex-away-mode")
    fingerprint = doctor.hooks_fingerprint(paths)
    remove_sqlite_files(paths.install_state_path)
    legacy_store = StateStore(paths.codex_home / "codex-away-mode" / "state.sqlite")
    legacy_store.set_install_state(
        "e2e_notify",
        {
            "status": "verified",
            "verified_at": "2026-06-18T10:00:00Z",
            "cwd": "/workspace/demo",
            "summary_key": StateStore.cwd_hash("/workspace/demo"),
            "message_id": "om_e2e",
            "hooks_fingerprint": fingerprint,
        },
    )
    legacy_store.set_route_key_state(
        status="verified",
        source="doctor_route_probe",
        verified_at="2026-06-18T09:00:00Z",
    )
    assert not paths.install_state_path.exists()

    report = doctor.run_doctor(paths)

    migrated_store = StateStore(paths.install_state_path)
    assert report["ok"] is False
    assert "notify_delivery_verified" in report["passed_codes"]
    assert "hook_trust_missing" in report["failed_codes"]
    assert migrated_store.get_install_state("e2e_notify")["status"] == "verified"
    assert migrated_store.route_key_state()["status"] == "verified"


def test_legacy_global_state_migration_reads_legacy_database_without_initializing_it(
    tmp_path, monkeypatch
):
    paths = FakePaths(tmp_path)
    save_config(paths.config_path, AppConfig(feishu_chat_id="oc_test_chat"))
    paths.hooks_json.write_text(
        json.dumps(managed_hooks_payload()),
        encoding="utf-8",
    )
    fingerprint = doctor.hooks_fingerprint(paths)
    legacy_path = paths.codex_home / "codex-away-mode" / "state.sqlite"
    legacy_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(legacy_path) as conn:
        conn.execute(
            "CREATE TABLE install_state (key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL)"
        )
        conn.execute(
            "INSERT INTO install_state VALUES (?, ?, ?)",
            (
                "e2e_notify",
                json.dumps(
                    {
                        "status": "verified",
                        "hooks_fingerprint": fingerprint,
                    }
                ),
                "2026-06-18T10:00:00Z",
            ),
        )

    original_initialize = StateStore._initialize

    def fail_if_legacy_initialized(self):
        if self.path == legacy_path:
            raise AssertionError("legacy state should be read-only during migration")
        return original_initialize(self)

    monkeypatch.setattr(StateStore, "_initialize", fail_if_legacy_initialized)

    report = doctor.run_doctor(paths)

    assert report["ok"] is False
    assert "notify_delivery_verified" in report["passed_codes"]
    assert "hook_trust_missing" in report["failed_codes"]
    assert StateStore(paths.install_state_path).get_install_state("e2e_notify")[
        "status"
    ] == "verified"


def test_install_migrates_legacy_config_to_away_home_without_deleting_old_file(tmp_path):
    paths = FakePaths(tmp_path)
    legacy_config = paths.codex_home / "codex-away-mode" / "config.toml"
    save_config(
        legacy_config,
        AppConfig(
            feishu_chat_id="oc_legacy_chat",
            route_key_verified=True,
            lark_cli_path="/custom/lark-cli",
        ),
    )
    source_scripts = tmp_path / "source-scripts"
    package = source_scripts / "codex_away_mode"
    package.mkdir(parents=True)
    (package / "__main__.py").write_text("", encoding="utf-8")

    result = install.run_install(
        paths,
        yes=True,
        source_scripts_dir=source_scripts,
    )

    config = load_config(paths.config_path)
    assert result["ok"] is True
    assert config.feishu_chat_id == "oc_legacy_chat"
    assert config.route_key_verified is True
    assert config.lark_cli_path == "/custom/lark-cli"
    assert result["lark_cli_path"] == "/custom/lark-cli"
    assert legacy_config.exists()


def test_doctor_warns_about_legacy_workspace_artifacts_without_deleting(tmp_path):
    paths = FakePaths(tmp_path)
    workspace = tmp_path / "workspace"
    legacy_dir = workspace / ".codex-away-mode"
    legacy_dir.mkdir(parents=True)
    (legacy_dir / "latest-summary.md").write_text("private summary", encoding="utf-8")
    save_config(paths.config_path, AppConfig(feishu_chat_id="oc_test_chat"))
    install.run_install(paths, yes=True, cli_command="/bin/codex-away-mode")

    report = doctor.run_doctor(paths, cwd=str(workspace))

    assert "legacy_workspace_artifacts_present" in report["warnings"]
    assert (legacy_dir / "latest-summary.md").exists()


def test_route_probe_matching_reply_verifies_route_key_and_allows_multi_window(tmp_path):
    paths = FakePaths(tmp_path)
    save_config(
        paths.config_path,
        AppConfig(
            feishu_chat_id="oc_test_chat",
            route_key_verified=False,
            multi_window_enabled=False,
        ),
    )
    lark = FakeLark(messages=[user_message("om_reply", "om_probe_card")])

    report = doctor.run_doctor(
        paths,
        route_probe=True,
        lark=lark,
        clock=FakeClock(doctor.parse_utc("2026-06-18T10:00:00Z")),
        probe_timeout_seconds=1,
        poll_interval_seconds=0.1,
    )

    config = load_config(paths.config_path)
    assert report["ok"] is True
    assert "route_probe" in report["passed_codes"]
    assert lark.cards[0]["chat_id"] == "oc_test_chat"
    assert "template_id" not in str(lark.cards[0]["card"])
    assert config.route_key_verified is True
    assert config.multi_window_enabled is True
    route_state = StateStore(paths.install_state_path).get_install_state("route_key")
    assert route_state["status"] == "verified"
    assert route_state["source"] == "doctor_route_probe"
    assert route_state["verified_at"]


def test_route_probe_already_verified_short_circuits_without_sending_card(tmp_path):
    paths = FakePaths(tmp_path)
    save_config(
        paths.config_path,
        AppConfig(
            feishu_chat_id="oc_test_chat",
            route_key_verified=True,
            multi_window_enabled=True,
        ),
    )
    StateStore(paths.install_state_path).set_route_key_state(
        status="verified",
        source="doctor_route_probe",
        verified_at="2026-06-18T09:00:00Z",
    )
    lark = FakeLark(messages=[user_message("om_reply", "om_probe_card")])

    report = doctor.run_doctor(
        paths,
        route_probe=True,
        lark=lark,
        clock=FakeClock(doctor.parse_utc("2026-06-18T10:00:00Z")),
        probe_timeout_seconds=1,
        poll_interval_seconds=0.1,
    )

    assert report["ok"] is True
    assert "route_probe_already_verified" in report["passed_codes"]
    assert report["diagnostics"]["route_probe"]["sent_probe_card"] is False
    assert lark.cards == []
    assert lark.list_calls == []


def test_route_probe_timeout_is_inconclusive_and_leaves_flags_unchanged(tmp_path):
    paths = FakePaths(tmp_path)
    save_config(
        paths.config_path,
        AppConfig(
            feishu_chat_id="oc_test_chat",
            route_key_verified=False,
            multi_window_enabled=False,
        ),
    )

    report = doctor.run_doctor(
        paths,
        route_probe=True,
        lark=FakeLark(messages=[]),
        clock=FakeClock(doctor.parse_utc("2026-06-18T10:00:00Z")),
        probe_timeout_seconds=1,
        poll_interval_seconds=0.25,
    )

    config = load_config(paths.config_path)
    assert report["ok"] is False
    assert "route_probe_inconclusive" in report["degraded_codes"]
    assert "rerun" in report["next_step"]
    assert config.route_key_verified is False
    assert config.multi_window_enabled is False
    assert StateStore(paths.install_state_path).get_install_state("route_key")["status"] == "inconclusive"


def test_route_probe_mismatched_reply_disables_multi_window(tmp_path):
    paths = FakePaths(tmp_path)
    save_config(
        paths.config_path,
        AppConfig(
            feishu_chat_id="oc_test_chat",
            route_key_verified=False,
            multi_window_enabled=True,
        ),
    )

    report = doctor.run_doctor(
        paths,
        route_probe=True,
        lark=FakeLark(messages=[user_message("om_reply", "om_other_card")]),
        clock=FakeClock(doctor.parse_utc("2026-06-18T10:00:00Z")),
        probe_timeout_seconds=1,
        poll_interval_seconds=0.1,
    )

    config = load_config(paths.config_path)
    assert report["ok"] is False
    assert "route_probe_failed" in report["degraded_codes"]
    assert "route_probe_inconclusive" not in report["degraded_codes"]
    assert config.route_key_verified is False
    assert config.multi_window_enabled is False
    route_state = StateStore(paths.install_state_path).get_install_state("route_key")
    assert route_state["status"] == "failed"
    assert route_state["last_failure_reason"] == "mismatch_reply_to"


def test_route_probe_plain_private_message_is_inconclusive_not_negative(tmp_path):
    paths = FakePaths(tmp_path)
    save_config(
        paths.config_path,
        AppConfig(
            feishu_chat_id="oc_test_chat",
            route_key_verified=False,
            multi_window_enabled=True,
        ),
    )
    ordinary_message = LarkMessage(
        message_id="om_plain_dm",
        reply_to=None,
        msg_type="text",
        content_text="我没有回复卡片",
        sender_type="user",
        create_time="2026-06-18T10:00:01Z",
    )

    report = doctor.run_doctor(
        paths,
        route_probe=True,
        lark=FakeLark(messages=[ordinary_message]),
        clock=FakeClock(doctor.parse_utc("2026-06-18T10:00:00Z")),
        probe_timeout_seconds=1,
        poll_interval_seconds=0.25,
    )

    config = load_config(paths.config_path)
    assert report["ok"] is False
    assert "route_probe_inconclusive" in report["degraded_codes"]
    assert "route_probe_failed" not in report["degraded_codes"]
    assert config.route_key_verified is False
    assert config.multi_window_enabled is True


def test_route_probe_ignores_old_history_messages_before_probe_start(tmp_path):
    paths = FakePaths(tmp_path)
    save_config(
        paths.config_path,
        AppConfig(
            feishu_chat_id="oc_test_chat",
            route_key_verified=False,
            multi_window_enabled=True,
        ),
    )
    old_message = LarkMessage(
        message_id="om_old",
        reply_to=None,
        msg_type="text",
        content_text="旧消息",
        sender_type="user",
        create_time="2026-06-18T09:59:59Z",
    )

    report = doctor.run_doctor(
        paths,
        route_probe=True,
        lark=FakeLark(messages=[old_message]),
        clock=FakeClock(doctor.parse_utc("2026-06-18T10:00:00Z")),
        probe_timeout_seconds=1,
        poll_interval_seconds=0.25,
    )

    config = load_config(paths.config_path)
    assert report["ok"] is False
    assert "route_probe_inconclusive" in report["degraded_codes"]
    assert "route_probe_failed" not in report["degraded_codes"]
    assert config.route_key_verified is False
    assert config.multi_window_enabled is True


def test_route_probe_treats_lark_local_time_as_local_timezone(tmp_path):
    paths = FakePaths(tmp_path)
    save_config(
        paths.config_path,
        AppConfig(
            feishu_chat_id="oc_test_chat",
            route_key_verified=False,
            multi_window_enabled=True,
        ),
    )
    old_local_message = LarkMessage(
        message_id="om_old",
        reply_to=None,
        msg_type="text",
        content_text="旧消息",
        sender_type="user",
        create_time="2026-06-18 22:51",
    )

    report = doctor.run_doctor(
        paths,
        route_probe=True,
        lark=FakeLark(messages=[old_local_message]),
        clock=FakeClock(doctor.parse_utc("2026-06-18T15:13:00Z")),
        probe_timeout_seconds=1,
        poll_interval_seconds=0.25,
    )

    config = load_config(paths.config_path)
    assert "route_probe_inconclusive" in report["degraded_codes"]
    assert "route_probe_failed" not in report["degraded_codes"]
    assert config.route_key_verified is False
    assert config.multi_window_enabled is True


def test_install_dry_run_plans_changes_without_writing_hooks_or_guidance(tmp_path):
    paths = FakePaths(tmp_path)

    result = install.run_install(paths, dry_run=True, cli_command="/bin/codex-away-mode")

    assert result["ok"] is True
    assert result["dry_run"] is True
    assert any("Would write" in item for item in result["planned_changes"])
    assert not paths.hooks_json.exists()
    assert not paths.global_agents.exists()
    assert not (paths.codex_home / "codex-away-mode").exists()


def test_install_yes_reports_install_state_unwritable_without_traceback(
    tmp_path, monkeypatch
):
    paths = FakePaths(tmp_path)

    def raise_store_error(_paths):
        raise RuntimeError("cannot open install-state")

    monkeypatch.setattr(install, "open_install_store", raise_store_error)

    result = install.run_install(paths, yes=True, cli_command="/bin/codex-away-mode")

    assert result["ok"] is False
    assert result["failed_code"] == "install_state_unwritable"
    assert "cannot open install-state" in result["detail"]


def test_install_yes_blocks_when_codex_access_unwritable_without_claiming_success(
    tmp_path, monkeypatch
):
    paths = FakePaths(tmp_path)
    save_config(paths.config_path, AppConfig())

    def fake_preflight(_paths):
        return {
            "ok": False,
            "away_home": {"path": str(paths.away_home), "writable": True},
            "codex_access": {
                "agents_path": str(paths.global_agents),
                "agents_writable": False,
                "hooks_path": str(paths.hooks_json),
                "hooks_writable": False,
                "skills_dir": str(paths.codex_home / "skills"),
                "skills_writable": True,
            },
            "runtime": {"path": str(paths.runtime_state_path), "writable": True},
            "legacy": {},
            "failed_code": "codex_access_unwritable",
            "next_step": "Ask the user to approve Codex access writes.",
        }

    monkeypatch.setattr(install, "run_preflight", fake_preflight)

    result = install.run_install(paths, yes=True, cli_command="/bin/codex-away-mode")

    assert result["ok"] is False
    assert result["failed_code"] == "codex_access_unwritable"
    assert result["changed"] == []
    assert not paths.global_agents.exists()
    assert not paths.hooks_json.exists()


def test_install_yes_writes_guidance_hooks_and_requires_hook_trust_verification(tmp_path):
    paths = FakePaths(tmp_path)
    save_config(paths.config_path, AppConfig())
    lark = FakeLark(test_chat_id="oc_test_from_notification")
    source_scripts = tmp_path / "source-scripts"
    package = source_scripts / "codex_away_mode"
    package.mkdir(parents=True)
    (package / "__main__.py").write_text("", encoding="utf-8")

    result = install.run_install(
        paths,
        yes=True,
        source_scripts_dir=source_scripts,
        lark=lark,
    )

    assert result["ok"] is True
    assert paths.wrapper_path.exists()
    assert "PYTHONPATH" in paths.wrapper_path.read_text(encoding="utf-8")
    assert paths.scripts_dir.exists()
    assert not (paths.codex_home / "codex-away-mode").exists()
    assert paths.global_agents.exists()
    assert paths.hooks_json.exists()
    hooks_json = paths.hooks_json.read_text(encoding="utf-8")
    assert str(paths.wrapper_path) in hooks_json
    payload = json.loads(hooks_json)
    permission_hooks = payload["hooks"].get("PermissionRequest", [])
    assert permission_hooks
    assert (
        permission_hooks[0]["hooks"][0]["command"]
        == f"{paths.wrapper_path} notify permission-request --hook-json"
    )
    assert '"command": "/bin/codex-away-mode' not in hooks_json
    assert result["status"] == "hook_trust_pending"
    assert "doctor --e2e-notify" in result["next_step"]
    assert "信任" in result["next_step"]
    assert "设置 -> 钩子" in result["next_step"]
    assert load_config(paths.config_path).feishu_chat_id == "oc_test_from_notification"


def test_hook_trust_requires_permission_request_hook_after_install(tmp_path):
    paths = FakePaths(tmp_path)
    save_config(paths.config_path, AppConfig(feishu_chat_id="oc_test_chat"))
    install.run_install(paths, yes=True, cli_command="/bin/codex-away-mode")

    result = hook_trust.inspect_managed_hooks(paths)

    assert result["ok"] is True
    assert "permission_request" in result["hooks"]
    assert "notify permission-request --hook-json" in result["hooks"]["permission_request"]["command"]


def test_install_yes_syncs_installable_skill_package(tmp_path):
    paths = FakePaths(tmp_path)
    save_config(paths.config_path, AppConfig())
    source_skill = tmp_path / "source-skill"
    source_scripts = source_skill / "scripts"
    package = source_scripts / "codex_away_mode"
    references = source_skill / "references"
    package.mkdir(parents=True)
    references.mkdir(parents=True)
    (source_skill / "SKILL.md").write_text("new skill body", encoding="utf-8")
    (references / "usage.md").write_text("stage-summary contract", encoding="utf-8")
    (package / "__main__.py").write_text("", encoding="utf-8")
    paths.skill_install_dir.mkdir(parents=True)
    (paths.skill_install_dir / "SKILL.md").write_text("old skill body", encoding="utf-8")

    result = install.run_install(
        paths,
        yes=True,
        source_scripts_dir=source_scripts,
        source_skill_dir=source_skill,
    )

    assert result["ok"] is True
    assert paths.skill_source_dir.joinpath("SKILL.md").read_text(encoding="utf-8") == "new skill body"
    assert (
        paths.skill_source_dir.joinpath("references", "usage.md").read_text(encoding="utf-8")
        == "stage-summary contract"
    )
    assert paths.skill_install_dir.joinpath("SKILL.md").read_text(encoding="utf-8") == "new skill body"
    assert paths.skill_install_dir.is_symlink()
    assert (
        paths.skill_install_dir.joinpath("references", "usage.md").read_text(encoding="utf-8")
        == "stage-summary contract"
    )
    assert str(paths.skill_install_dir) in result["changed"]


def test_install_yes_self_source_scripts_does_not_delete_installed_scripts(tmp_path):
    paths = FakePaths(tmp_path)
    save_config(paths.config_path, AppConfig())
    marker = paths.scripts_dir / "codex_away_mode" / "__main__.py"
    marker.parent.mkdir(parents=True)
    marker.write_text("print('installed')", encoding="utf-8")

    result = install.run_install(
        paths,
        yes=True,
        source_scripts_dir=paths.scripts_dir,
        source_skill_dir=None,
        cli_command=str(paths.wrapper_path),
    )

    assert result["ok"] is True
    assert result["scripts_sync_mode"] == "self_source_skipped"
    assert marker.exists()
    assert marker.read_text(encoding="utf-8") == "print('installed')"


def test_install_scripts_sync_failure_preserves_existing_destination(tmp_path, monkeypatch):
    paths = FakePaths(tmp_path)
    save_config(paths.config_path, AppConfig())
    source_scripts = tmp_path / "source-scripts"
    (source_scripts / "codex_away_mode").mkdir(parents=True)
    (source_scripts / "codex_away_mode" / "__main__.py").write_text("new", encoding="utf-8")
    old_file = paths.scripts_dir / "codex_away_mode" / "old.py"
    old_file.parent.mkdir(parents=True)
    old_file.write_text("old", encoding="utf-8")

    def fail_copytree(*_args, **_kwargs):
        raise OSError("copy failed")

    monkeypatch.setattr(install.shutil, "copytree", fail_copytree)

    result = install.run_install(
        paths,
        yes=True,
        source_scripts_dir=source_scripts,
        source_skill_dir=None,
        cli_command=str(paths.wrapper_path),
    )

    assert result["ok"] is False
    assert result["failed_code"] == "scripts_sync_failed"
    assert "copy failed" in result["detail"]
    assert old_file.exists()
    assert old_file.read_text(encoding="utf-8") == "old"


def test_install_scripts_replace_failure_rolls_back_destination(tmp_path, monkeypatch):
    paths = FakePaths(tmp_path)
    save_config(paths.config_path, AppConfig())
    source_scripts = tmp_path / "source-scripts"
    (source_scripts / "codex_away_mode").mkdir(parents=True)
    (source_scripts / "codex_away_mode" / "__main__.py").write_text("new", encoding="utf-8")
    old_file = paths.scripts_dir / "codex_away_mode" / "old.py"
    old_file.parent.mkdir(parents=True)
    old_file.write_text("old", encoding="utf-8")
    rename_calls = []
    original_rename = install.Path.rename

    def flaky_rename(self, target):
        rename_calls.append((self, target))
        if self.name.startswith(".scripts-staging-"):
            raise OSError("rename failed")
        return original_rename(self, target)

    monkeypatch.setattr(install.Path, "rename", flaky_rename)

    result = install.run_install(
        paths,
        yes=True,
        source_scripts_dir=source_scripts,
        source_skill_dir=None,
        cli_command=str(paths.wrapper_path),
    )

    assert result["ok"] is False
    assert result["failed_code"] == "scripts_sync_failed"
    assert "rename failed" in result["detail"]
    assert old_file.exists()
    assert old_file.read_text(encoding="utf-8") == "old"
    assert any(source == paths.scripts_dir for source, _target in rename_calls)


def test_install_falls_back_to_thin_skill_shim_when_symlink_fails(tmp_path, monkeypatch):
    paths = FakePaths(tmp_path)
    save_config(paths.config_path, AppConfig())
    source_skill = tmp_path / "source-skill"
    source_scripts = source_skill / "scripts"
    (source_scripts / "codex_away_mode").mkdir(parents=True)
    (source_skill / "SKILL.md").write_text(
        "---\nname: codex-away-mode\ndescription: Test skill.\n---\n",
        encoding="utf-8",
    )
    (source_scripts / "codex_away_mode" / "__main__.py").write_text("", encoding="utf-8")
    monkeypatch.setattr(install, "_create_skill_symlink", lambda _source, _destination: False)

    result = install.run_install(
        paths,
        yes=True,
        source_scripts_dir=source_scripts,
        source_skill_dir=source_skill,
    )

    assert result["ok"] is True
    assert result["skill_discovery_mode"] == "thin_shim"
    assert not paths.skill_install_dir.is_symlink()
    shim = paths.skill_install_dir / "SKILL.md"
    assert shim.exists()
    assert "name: codex-away-mode" in shim.read_text(encoding="utf-8")
    assert str(paths.skill_source_dir) in shim.read_text(encoding="utf-8")


def test_install_skips_skill_discovery_when_skills_dir_unwritable(
    tmp_path, monkeypatch
):
    paths = FakePaths(tmp_path)
    save_config(paths.config_path, AppConfig())

    def fake_preflight(_paths):
        return {
            "ok": True,
            "away_home": {"path": str(paths.away_home), "writable": True},
            "codex_access": {
                "agents_path": str(paths.global_agents),
                "agents_writable": True,
                "hooks_path": str(paths.hooks_json),
                "hooks_writable": True,
                "skills_dir": str(paths.codex_home / "skills"),
                "skills_writable": False,
            },
            "runtime": {"path": str(paths.runtime_state_path), "writable": True},
            "legacy": {},
            "failed_code": None,
            "next_step": "Skill discovery degraded.",
        }

    monkeypatch.setattr(install, "run_preflight", fake_preflight)

    result = install.run_install(paths, yes=True, cli_command="/bin/codex-away-mode")

    assert result["ok"] is True
    assert result["skill_discovery_mode"] == "degraded"
    assert "skill_discovery_degraded" in result["degraded_codes"]
    assert not paths.skill_install_dir.exists()
    assert paths.global_agents.exists()
    assert paths.hooks_json.exists()


def test_install_preserves_route_key_verified_true(tmp_path):
    paths = FakePaths(tmp_path)
    save_config(
        paths.config_path,
        AppConfig(
            route_key_verified=True,
            multi_window_enabled=True,
            feishu_chat_id="oc_test_chat",
        ),
    )
    source_scripts = tmp_path / "source-scripts"
    package = source_scripts / "codex_away_mode"
    package.mkdir(parents=True)
    (package / "__main__.py").write_text("", encoding="utf-8")

    result = install.run_install(
        paths,
        yes=True,
        source_scripts_dir=source_scripts,
    )

    config = load_config(paths.config_path)
    assert result["ok"] is True
    assert config.route_key_verified is True
    assert config.multi_window_enabled is True


def test_install_yes_backs_up_existing_global_agents_before_modifying(tmp_path):
    paths = FakePaths(tmp_path)
    save_config(paths.config_path, AppConfig())
    paths.global_agents.write_text("# Existing rules\n", encoding="utf-8")

    install.run_install(paths, yes=True, cli_command="/bin/codex-away-mode")

    backups = list(paths.backup_dir.glob("AGENTS.md.*.bak"))
    assert backups
    assert backups[0].read_text(encoding="utf-8") == "# Existing rules\n"


def test_uninstall_keep_data_removes_managed_hooks_and_guidance_but_keeps_data(tmp_path):
    paths = FakePaths(tmp_path)
    install.run_install(paths, yes=True, cli_command="/bin/codex-away-mode")
    paths.data_dir.mkdir(parents=True, exist_ok=True)
    (paths.data_dir / "state.sqlite").write_text("data", encoding="utf-8")

    result = uninstall.run_uninstall(paths, keep_data=True)

    assert result["ok"] is True
    assert paths.data_dir.exists()
    assert not paths.skill_install_dir.exists()
    assert "CODEX AWAY MODE" not in paths.global_agents.read_text(encoding="utf-8")
    assert "codex-away-mode notify" not in paths.hooks_json.read_text(encoding="utf-8")


def test_uninstall_delete_data_deletes_data_dir_only_when_explicit(tmp_path):
    paths = FakePaths(tmp_path)
    paths.data_dir.mkdir(parents=True)
    (paths.data_dir / "state.sqlite").write_text("data", encoding="utf-8")

    keep_result = uninstall.run_uninstall(paths, keep_data=True)
    assert keep_result["ok"] is True
    assert paths.data_dir.exists()

    delete_result = uninstall.run_uninstall(paths, delete_data=True)
    assert delete_result["ok"] is True
    assert not paths.data_dir.exists()
