import json
import shutil
from datetime import datetime, timezone
from io import StringIO
from types import SimpleNamespace

import pytest

from codex_away_mode import config as config_module
from codex_away_mode.config import AppConfig, RuntimePaths, load_config, save_config
from codex_away_mode import cli, notify
from codex_away_mode.state import StateStore


def run_cli(capsys, *args):
    code = cli.main(list(args))
    captured = capsys.readouterr()
    return code, captured


def parse_stdout(captured):
    return json.loads(captured.out)


def flatten_text(value):
    if isinstance(value, dict):
        return "\n".join(flatten_text(item) for item in value.values())
    if isinstance(value, list):
        return "\n".join(flatten_text(item) for item in value)
    return str(value)


class FixedClock:
    def now(self):
        return datetime(2026, 6, 18, 10, 0, tzinfo=timezone.utc)


class CapturingLark:
    def __init__(self):
        self.cards = []

    def send_interactive_card(self, **kwargs):
        self.cards.append(kwargs["card"])
        return SimpleNamespace(message_id="msg_1", chat_id=kwargs.get("chat_id") or "chat_1")


class CapturingNotificationClient:
    def __init__(self):
        self.calls = []

    def send_summary_card(self, markdown, cwd=None):
        self.calls.append(("summary", markdown, cwd))
        return SimpleNamespace(message_id="msg_summary", chat_id="oc_chat")

    def send_fallback_card(self, cwd):
        self.calls.append(("fallback", cwd))
        return SimpleNamespace(message_id="msg_fallback", chat_id="oc_chat")

    def send_away_early_exit_card(self, payload):
        self.calls.append(("away_early_exit", payload))
        return SimpleNamespace(message_id="msg_early", chat_id="oc_chat")


@pytest.fixture(autouse=True)
def isolate_runtime_store(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_AWAY_RUNTIME_DIR", str(tmp_path / "runtime"))


def test_version_json_ok(capsys):
    code, captured = run_cli(capsys, "--json", "version")

    assert code == 0
    payload = parse_stdout(captured)
    assert payload["ok"] is True
    assert payload["command"] == "version"
    assert payload["name"] == "codex-away-mode"


def test_unknown_command_returns_json_error(capsys):
    code, captured = run_cli(capsys, "--json", "wat")

    assert code == 2
    payload = parse_stdout(captured)
    assert payload["ok"] is False
    assert payload["error_code"] == "unknown_command"


def test_notify_stop_json_does_not_require_cwd(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-home"))
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.chdir(workspace)

    code, captured = run_cli(capsys, "notify", "stop", "--json")

    assert code == 0
    payload = parse_stdout(captured)
    assert payload["ok"] is True
    assert payload["command"] == "notify stop"
    assert payload["cwd"] == str(workspace)


def test_notify_permission_request_hook_mode_sends_card_and_outputs_empty_decision(
    tmp_path,
    monkeypatch,
    capsys,
):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-home"))
    paths = RuntimePaths.from_environment()
    save_config(paths.config_path, AppConfig(feishu_chat_id="oc_chat"))
    sent_payloads = []

    class FakePermissionClient:
        def __init__(self, _paths, *, hook_stdin=None, env=None):
            self.hook_stdin = hook_stdin

        def send_permission_request_card(self, payload):
            sent_payloads.append(payload)
            return SimpleNamespace(message_id="om_permission", chat_id="oc_chat")

    monkeypatch.setattr(cli, "_NotificationClient", FakePermissionClient)
    payload = {
        "hook_event_name": "PermissionRequest",
        "tool_name": "Bash",
        "cwd": "/workspace/Skill-Create",
        "session_id": "session_test",
        "turn_id": "turn_test",
        "tool_input": {
            "command": "rm /workspace-outside/codex-desktop-permission-target.txt",
            "description": "需要删除一个外部测试文件。",
        },
    }

    code = cli.main(
        ["notify", "permission-request", "--hook-json"],
        stdin=StringIO(json.dumps(payload)),
    )
    captured = capsys.readouterr()

    assert code == 0
    assert captured.out == "{}\n"
    assert sent_payloads
    sent = sent_payloads[0]
    assert sent["tool_name"] == "Bash"
    assert sent["cwd"] == "/workspace/Skill-Create"
    assert sent["description"] == "需要删除一个外部测试文件。"
    assert "codex-desktop-permission-target.txt" in sent["command"]
    assert "rm /workspace-outside" not in captured.out
    events = StateStore(paths.runtime_state_path).list_diagnostic_events(
        "codex_hook_invocation"
    )
    assert events
    detail = json.loads(events[-1]["detail_json"])
    assert detail["hook_event_name"] == "PermissionRequest"
    assert detail["hooks_fingerprint"]


def test_notify_mode_updates_config_under_codex_home(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-home"))

    code, captured = run_cli(capsys, "notify", "mode", "off")

    assert code == 0
    payload = parse_stdout(captured)
    paths = RuntimePaths.from_environment()
    assert payload["ok"] is True
    assert payload["mode"] == "off"
    assert load_config(paths.config_path).notification_mode == "off"


def test_notification_client_summary_card_uses_project_title_and_compact_footer(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-home"))
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir(parents=True, exist_ok=True)
    (codex_home / "session_index.jsonl").write_text(
        json.dumps(
            {"id": "thread_1", "thread_name": "建立 Skill-Create 基线"},
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("CODEX_THREAD_ID", "thread_1")
    paths = RuntimePaths.from_environment()
    save_config(paths.config_path, AppConfig(feishu_chat_id="oc_chat"))
    monkeypatch.setattr(cli, "SystemClock", lambda: FixedClock())
    client = cli._NotificationClient(paths)
    client.lark = CapturingLark()
    markdown = (
        "**项目**\n"
        "Skill-Create / 飞书通知与 AwayMode\n\n"
        "**工作目录**\n"
        "/workspace/Skill-Create\n\n"
        "**完成**\n"
        "Done\n"
    )

    client.send_summary_card(markdown)

    card = client.lark.cards[0]
    text = flatten_text(card)
    assert card["schema"] == "2.0"
    assert card["header"]["title"]["content"] == "建立 Skill-Create 基线 / Skill-Create"
    assert card["header"]["subtitle"]["content"] == "Codex完成通知：本轮任务已结束"
    assert card["header"]["text_tag_list"][0]["text"]["content"] == "完成通知"
    assert "飞书通知与 AwayMode" not in card["header"]["title"]["content"]
    assert "**工作目录**" not in text
    assert "/workspace/Skill-Create" not in text
    assert "- Done" in text
    assert "**时间**：18:00" in text
    assert "**目录**" not in text
    assert "**通知模式**：每轮完成后通知" in text
    assert "告诉Codex" in text
    assert "暂停飞书通知 2 小时" in text
    assert "codex-away-mode notify" not in text


def test_notification_client_plain_summary_uses_cwd_fallback_project_and_compact_footer(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-home"))
    paths = RuntimePaths.from_environment()
    save_config(paths.config_path, AppConfig(feishu_chat_id="oc_chat"))
    monkeypatch.setattr(cli, "SystemClock", lambda: FixedClock())
    client = cli._NotificationClient(paths)
    client.lark = CapturingLark()
    markdown = (
        "已用 /Applications/Xcode.app 打开 worktree 工程："
        "/workspace/immichSlides-app/worktrees/demo/immichSlides.xcodeproj。"
    )

    client.send_summary_card(markdown, cwd="/workspace/immichSlides-app")

    card = client.lark.cards[0]
    text = flatten_text(card)
    assert card["schema"] == "2.0"
    assert card["header"]["title"]["content"] == "immichSlides-app"
    assert card["header"]["subtitle"]["content"] == "Codex完成通知：本轮任务已结束"
    assert card["header"]["text_tag_list"][0]["text"]["content"] == "完成通知"
    assert "**完成**" in text
    assert "**摘要**" not in text
    assert "工作目录：/workspace/immichSlides-app" not in text
    assert "**目录**" not in text
    assert "**时间**：18:00" in text


def test_notify_snooze_sets_snooze_until(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-home"))

    code, captured = run_cli(capsys, "notify", "snooze", "2h")

    assert code == 0
    payload = parse_stdout(captured)
    config = load_config(RuntimePaths.from_environment().config_path)
    assert payload["ok"] is True
    assert payload["mode"] == "snooze"
    assert config.snooze_until is not None


def test_notify_mark_prompt_writes_marker_for_current_cwd(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-home"))
    monkeypatch.setattr(notify.tempfile, "gettempdir", lambda: "/not-the-pytest-temp")
    workspace = tmp_path / "workspace" / "demo"
    workspace.mkdir(parents=True)

    code, captured = run_cli(
        capsys,
        "notify",
        "mark-prompt",
        "--cwd",
        str(workspace),
        "--json",
    )

    assert code == 0
    payload = parse_stdout(captured)
    assert payload["ok"] is True
    assert payload["status"] == "marked"
    paths = RuntimePaths.from_environment()
    assert StateStore(paths.runtime_state_path).get_prompt_marker(str(workspace)) is not None
    assert not (workspace / ".codex-away-mode").exists()


def test_notify_mark_prompt_uses_hook_stdin_cwd_when_no_explicit_cwd(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-home"))
    monkeypatch.setattr(notify.tempfile, "gettempdir", lambda: "/not-the-pytest-temp")
    workspace = tmp_path / "wrong-process-cwd"
    workspace.mkdir()
    hook_workspace = tmp_path / "from-hook-stdin"
    hook_workspace.mkdir()
    monkeypatch.chdir(workspace)

    code = cli.main(
        ["notify", "mark-prompt", "--json"],
        stdin=StringIO(json.dumps({"cwd": str(hook_workspace)})),
    )
    captured = capsys.readouterr()

    assert code == 0
    payload = parse_stdout(captured)
    assert payload["ok"] is True
    paths = RuntimePaths.from_environment()
    assert StateStore(paths.runtime_state_path).get_prompt_marker(str(hook_workspace)) is not None
    events = StateStore(paths.runtime_state_path).list_diagnostic_events(
        "codex_hook_invocation"
    )
    assert json.loads(events[-1]["detail_json"])["hook_event_name"] == "UserPromptSubmit"
    assert not (hook_workspace / ".codex-away-mode").exists()


def test_notify_hook_payload_capture_redacts_prompt_text(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-home"))
    paths = RuntimePaths.from_environment()
    save_config(paths.config_path, AppConfig(capture_hook_payloads=True))
    payload = {
        "cwd": "/workspace/demo",
        "hook_event_name": "UserPromptSubmit",
        "goal_status": "in_progress",
        "prompt": "secret user prompt",
        "nested": {"session_id": "sess_123", "content": "hidden content"},
    }

    code = cli.main(
        ["notify", "mark-prompt", "--json"],
        stdin=StringIO(json.dumps(payload)),
    )
    captured = capsys.readouterr()

    assert code == 0
    assert parse_stdout(captured)["ok"] is True
    sample = (paths.log_dir / "hook-payload-samples.jsonl").read_text(
        encoding="utf-8"
    )
    assert "goal_status" in sample
    assert "in_progress" in sample
    assert "session_id" in sample
    assert "sess_123" in sample
    assert "secret user prompt" not in sample
    assert "hidden content" not in sample


def test_notify_mark_prompt_skips_root_cwd(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-home"))

    code, captured = run_cli(
        capsys,
        "notify",
        "mark-prompt",
        "--cwd",
        "/",
        "--json",
    )

    assert code == 0
    payload = parse_stdout(captured)
    assert payload["ok"] is True
    assert payload["status"] == "skipped"
    assert payload["reason"] == "non_user_workspace"
    assert not RuntimePaths.from_environment().runtime_state_path.exists()


def test_notify_stage_summary_writes_runtime_store_without_workspace_files(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-home"))
    monkeypatch.setattr(notify, "skip_cwd_reason", lambda paths, cwd: None)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    summary = f"**项目**\nDemo\n\n**工作目录**\n{workspace}\n\n**完成**\nDone\n"

    code = cli.main(
        ["notify", "stage-summary", "--cwd", str(workspace), "--json"],
        stdin=StringIO(summary),
    )
    captured = capsys.readouterr()

    assert code == 0
    payload = parse_stdout(captured)
    assert payload["status"] == "staged"
    paths = RuntimePaths.from_environment()
    assert StateStore(paths.runtime_state_path).get_staged_summary(str(workspace))[
        "summary_markdown"
    ] == summary
    assert not (workspace / ".codex-away-mode").exists()


def test_notify_stage_summary_rejects_insecure_runtime_dir_without_workspace_fallback(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-home"))
    monkeypatch.setattr(notify, "skip_cwd_reason", lambda paths, cwd: None)
    runtime_dir = tmp_path / "open-runtime"
    runtime_dir.mkdir(mode=0o755)
    monkeypatch.setenv("CODEX_AWAY_RUNTIME_DIR", str(runtime_dir))
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    code = cli.main(
        ["notify", "stage-summary", "--cwd", str(workspace), "--json"],
        stdin=StringIO("summary"),
    )
    captured = capsys.readouterr()

    assert code == 1
    payload = parse_stdout(captured)
    assert payload["ok"] is False
    assert payload["error_code"] == "runtime_state_unwritable"
    assert payload["detail"] == "runtime_dir_permissions_too_open"
    assert not (workspace / ".codex-away-mode").exists()


def test_notify_test_missing_binding_returns_json_error(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-home"))
    save_config(RuntimePaths.from_environment().config_path, AppConfig())

    code, captured = run_cli(capsys, "notify", "test", "--json")

    assert code == 1
    payload = parse_stdout(captured)
    assert payload["ok"] is False
    assert payload["error_code"] == "missing_feishu_binding"


def test_notify_stop_missing_binding_skips_instead_of_hook_error(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-home"))
    monkeypatch.setattr(notify.tempfile, "gettempdir", lambda: "/not-the-pytest-temp")
    monkeypatch.setattr(cli, "SystemClock", lambda: FixedClock())
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    cwd = str(workspace)
    save_config(RuntimePaths.from_environment().config_path, AppConfig())
    paths = RuntimePaths.from_environment()
    notify.stage_summary(
        paths,
        cwd=cwd,
        summary_markdown=f"**项目**\nDemo\n\n**工作目录**\n{cwd}\n\n**完成**\nDone\n",
        now=datetime(2026, 6, 18, 10, 0, tzinfo=timezone.utc),
    )

    code, captured = run_cli(
        capsys,
        "notify",
        "stop",
        "--cwd",
        cwd,
        "--json",
    )

    assert code == 0
    payload = parse_stdout(captured)
    assert payload["ok"] is True
    assert payload["status"] == "skipped"
    assert payload["reason"] == "missing_feishu_binding"
    assert StateStore(paths.runtime_state_path).get_staged_summary(cwd) is not None
    assert not (workspace / ".codex-away-mode").exists()


def test_notify_stop_does_not_read_workspace_latest_summary(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-home"))
    monkeypatch.setattr(cli, "SystemClock", lambda: FixedClock())
    monkeypatch.setattr(notify, "skip_cwd_reason", lambda paths, cwd: None)
    workspace = tmp_path / "workspace"
    legacy_dir = workspace / ".codex-away-mode"
    legacy_dir.mkdir(parents=True)
    (legacy_dir / "latest-summary.md").write_text(
        f"**项目**\nLegacy\n\n**工作目录**\n{workspace}\n\n**完成**\nShould not send\n",
        encoding="utf-8",
    )
    save_config(
        RuntimePaths.from_environment().config_path,
        AppConfig(feishu_chat_id="oc_chat"),
    )
    client = CapturingNotificationClient()
    monkeypatch.setattr(cli, "_NotificationClient", lambda paths, **kwargs: client)

    code, captured = run_cli(
        capsys,
        "notify",
        "stop",
        "--cwd",
        str(workspace),
        "--json",
    )

    assert code == 0
    payload = parse_stdout(captured)
    assert payload["status"] == "skipped"
    assert payload["detail"] == "summary_missing"
    assert client.calls == []


def test_notify_stop_active_away_guard_runs_before_notification_mode_off(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-home"))
    paths = RuntimePaths.from_environment()
    save_config(paths.config_path, AppConfig(notification_mode="off"))
    store = StateStore(paths.runtime_state_path)
    session_id = store.create_away_session(
        project="Demo",
        cwd="/workspace/demo",
        task="Task",
        started_at="2026-06-20T09:00:00Z",
        deadline_at="2026-06-20T11:00:00Z",
        codex_session_id="codex_session_1",
    )
    window_id = store.create_away_window(
        session_id=session_id,
        recipient_id="oc_chat",
        card_message_id="om_card",
        created_at="2026-06-20T09:00:00Z",
        deadline_at="2026-06-20T11:00:00Z",
    )
    store.mark_prompt_delivered(
        session_id=session_id,
        window_id=window_id,
        message_id="om_reply",
        processed_at="2026-06-20T09:30:00Z",
    )
    client = CapturingNotificationClient()
    monkeypatch.setattr(cli, "_NotificationClient", lambda paths, **kwargs: client)
    monkeypatch.setattr(cli, "SystemClock", lambda: FixedClock())

    code = cli.main(
        ["notify", "stop", "--cwd", "/workspace/demo", "--json"],
        stdin=StringIO(json.dumps({"session_id": "codex_session_1"})),
    )
    captured = capsys.readouterr()

    assert code == 0
    payload = parse_stdout(captured)
    assert payload["status"] == "away_active_stop_ignored"
    assert payload["detail"] == "insufficient_evidence"
    assert payload.get("reason") != "notification_mode_off"
    assert client.calls == []


def test_doctor_json_reports_missing_config(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-home"))

    code, captured = run_cli(capsys, "doctor", "--json")

    assert code == 0
    payload = parse_stdout(captured)
    assert payload["ok"] is False
    assert "local_config_missing" in payload["failed_codes"]


def test_install_status_json_reports_persisted_state(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-home"))
    paths = RuntimePaths.from_environment()
    from codex_away_mode.state import StateStore

    StateStore(paths.install_state_path).update_install_status(
        status="hook_trust_pending",
        waiting_for="hook_trust",
        next_step="Ask user to trust hooks.",
    )

    code, captured = run_cli(capsys, "install", "status", "--json")

    assert code == 0
    payload = parse_stdout(captured)
    assert payload["ok"] is True
    assert payload["status"] == "hook_trust_pending"
    assert payload["waiting_for"] == "hook_trust"
    assert payload["next_step"] == "Ask user to trust hooks."


def test_setup_feishu_passes_restart_auth_flag(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-home"))
    captured_kwargs = {}

    def fake_setup(paths, **kwargs):
        captured_kwargs.update(kwargs)
        return {"ok": True, "status": "captured"}

    monkeypatch.setattr(cli.setup, "run_setup_feishu", fake_setup)

    code, captured = run_cli(capsys, "setup", "feishu", "--restart-auth", "--json")

    assert code == 0
    assert parse_stdout(captured)["status"] == "captured"
    assert captured_kwargs["restart_auth"] is True


def test_install_dry_run_accepts_json_flag(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-home"))
    monkeypatch.setenv("CODEX_AWAY_HOME", str(tmp_path / "away-home"))

    code, captured = run_cli(capsys, "install", "--dry-run", "--json")

    assert code == 0
    payload = parse_stdout(captured)
    assert payload["ok"] is True
    assert payload["dry_run"] is True
    assert payload["changed"] == []
    text = "\n".join(payload["planned_changes"])
    assert str(tmp_path / "away-home") in text
    assert str(tmp_path / "codex-home" / "codex-away-mode") not in text


def test_install_preflight_reports_away_home_codex_access_and_runtime(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-home"))
    monkeypatch.setenv("CODEX_AWAY_HOME", str(tmp_path / "away-home"))
    monkeypatch.delenv("CODEX_AWAY_RUNTIME_DIR", raising=False)
    monkeypatch.setenv("TMPDIR", str(tmp_path / "tmp"))

    code, captured = run_cli(capsys, "install", "preflight", "--json")

    assert code == 0
    payload = parse_stdout(captured)
    assert payload["ok"] is True
    assert payload["away_home"]["path"] == str(tmp_path / "away-home")
    assert payload["away_home"]["writable"] is True
    assert payload["codex_access"]["hooks_path"] == str(tmp_path / "codex-home" / "hooks.json")
    assert payload["runtime"]["path"] == str(tmp_path / "tmp" / "codex-away-mode" / "state.sqlite")


def test_away_wait_missing_chat_binding_uses_real_service_without_sending(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-home"))
    save_config(RuntimePaths.from_environment().config_path, AppConfig())

    code, captured = run_cli(
        capsys,
        "away",
        "wait",
        "--project",
        "Test Project",
        "--cwd",
        "/tmp/project",
        "--task",
        "implement skeleton",
        "--completed",
        "tests written",
        "--changed",
        "tests/test_cli.py",
        "--verification",
        "pytest",
        "--unverified",
        "live Feishu",
        "--need-user",
        "none",
        "--wait-minutes",
        "30",
        "--poll-interval",
        "5",
        "--json",
    )

    assert code == 0
    payload = parse_stdout(captured)
    assert payload["status"] == "error"
    assert payload["error_code"] == "missing_feishu_chat_id"


def test_away_wait_accepts_required_args_and_returns_service_error(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-home"))
    save_config(RuntimePaths.from_environment().config_path, AppConfig())

    code, captured = run_cli(
        capsys,
        "away",
        "wait",
        "--project",
        "Test Project",
        "--cwd",
        "/tmp/project",
        "--task",
        "implement skeleton",
        "--completed",
        "tests written",
        "--changed",
        "tests/test_cli.py",
        "--verification",
        "pytest",
        "--unverified",
        "live Feishu",
        "--need-user",
        "none",
        "--wait-minutes",
        "30",
        "--poll-interval",
        "5",
        "--json",
    )

    assert code == 0
    payload = parse_stdout(captured)
    assert payload["status"] == "error"
    assert payload["error_code"] == "missing_feishu_chat_id"


def test_away_wait_resume_accepts_progress_fields_without_start_fields():
    parser = cli.build_parser()
    args = parser.parse_args(
        [
            "away",
            "wait",
            "--resume",
            "sess_1",
            "--resume-token",
            "rt_1",
            "--completed",
            "done",
            "--changed",
            "away.py",
            "--verification",
            "pytest",
            "--unverified",
            "无",
            "--need-user",
            "继续",
            "--poll-interval",
            "5",
            "--json",
        ]
    )

    assert args.resume == "sess_1"
    assert args.resume_token == "rt_1"


def test_away_start_parser_uses_explicit_start_command():
    parser = cli.build_parser()
    args = parser.parse_args(
        [
            "away",
            "start",
            "--project",
            "Test Project",
            "--cwd",
            "/tmp/project",
            "--task",
            "implement",
            "--completed",
            "done",
            "--changed",
            "none",
            "--verification",
            "pytest",
            "--unverified",
            "none",
            "--need-user",
            "none",
            "--wait-minutes",
            "30",
            "--poll-interval",
            "5",
            "--json",
        ]
    )

    assert args.away_command == "start"
    assert args.project == "Test Project"


def test_away_start_uses_default_poll_interval_when_omitted(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("CODEX_AWAY_HOME", str(tmp_path / "away-home"))
    paths = RuntimePaths.from_environment()
    save_config(
        paths.config_path,
        AppConfig(feishu_chat_id="oc_chat", lark_cli_path="/bin/echo"),
    )
    captured = {}

    class FakeAwayWaiter:
        def __init__(self, **kwargs):
            captured["config"] = kwargs["config"]

        def wait(self, context):
            captured["context"] = context
            return {"status": "timeout", "keep_waiting": False}

    monkeypatch.setattr(cli, "AwayWaiter", FakeAwayWaiter)

    code, captured_output = run_cli(
        capsys,
        "away",
        "start",
        "--project",
        "Test Project",
        "--cwd",
        "/tmp/project",
        "--task",
        "implement",
        "--completed",
        "done",
        "--changed",
        "none",
        "--verification",
        "pytest",
        "--unverified",
        "none",
        "--need-user",
        "none",
        "--wait-minutes",
        "30",
        "--json",
    )

    assert code == 0
    assert parse_stdout(captured_output) == {"status": "timeout", "keep_waiting": False}
    assert captured["config"].poll_interval_seconds == 5
    assert captured["context"]["wait_minutes"] == 30


def test_away_resume_parser_requires_token_shape():
    parser = cli.build_parser()
    args = parser.parse_args(
        [
            "away",
            "resume",
            "sess_1",
            "--resume-token",
            "rt_1",
            "--completed",
            "done",
            "--changed",
            "away.py",
            "--verification",
            "pytest",
            "--unverified",
            "无",
            "--need-user",
            "继续",
            "--poll-interval",
            "5",
            "--json",
        ]
    )

    assert args.away_command == "resume"
    assert args.session_id == "sess_1"
    assert args.resume_token == "rt_1"


def test_away_resume_handler_does_not_require_start_only_args(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("CODEX_AWAY_HOME", str(tmp_path / "away-home"))
    paths = RuntimePaths.from_environment()
    save_config(
        paths.config_path,
        AppConfig(feishu_chat_id="oc_chat", lark_cli_path="/bin/echo"),
    )
    captured_context = {}

    class FakeAwayWaiter:
        def __init__(self, **_kwargs):
            pass

        def wait(self, context):
            captured_context.update(context)
            return {"status": "ended", "keep_waiting": False}

    monkeypatch.setattr(cli, "AwayWaiter", FakeAwayWaiter)

    code, captured = run_cli(
        capsys,
        "away",
        "resume",
        "sess_1",
        "--resume-token",
        "rt_1",
        "--extend-minutes",
        "180",
        "--completed",
        "done",
        "--changed",
        "away.py",
        "--verification",
        "pytest",
        "--unverified",
        "无",
        "--need-user",
        "继续",
        "--poll-interval",
        "5",
        "--json",
    )

    assert code == 0
    assert parse_stdout(captured) == {"status": "ended", "keep_waiting": False}
    assert captured_context["resume"] == "sess_1"
    assert captured_context["resume_token"] == "rt_1"
    assert captured_context["extend_minutes"] == 180
    assert "wait_minutes" not in captured_context


def test_away_wait_start_accepts_optional_codex_session_id():
    parser = cli.build_parser()
    args = parser.parse_args(
        [
            "away",
            "wait",
            "--project",
            "Test Project",
            "--cwd",
            "/tmp/project",
            "--task",
            "implement",
            "--completed",
            "done",
            "--changed",
            "none",
            "--verification",
            "pytest",
            "--unverified",
            "none",
            "--need-user",
            "none",
            "--wait-minutes",
            "30",
            "--poll-interval",
            "5",
            "--codex-session-id",
            "codex_session_1",
            "--json",
        ]
    )

    assert args.codex_session_id == "codex_session_1"


def test_cli_away_status_json(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-home"))
    paths = RuntimePaths.from_environment()
    save_config(paths.config_path, AppConfig(feishu_chat_id="oc_chat", route_key_verified=True))
    store = StateStore(paths.runtime_state_path)
    session_id = store.create_away_session(
        project="Demo",
        cwd="/workspace/demo",
        task="Task",
        started_at="2026-06-20T09:00:00+00:00",
        deadline_at="2026-06-20T11:00:00+00:00",
    )
    store.create_away_window(
        session_id=session_id,
        recipient_id="oc_chat",
        card_message_id="om_card",
        created_at="2026-06-20T09:00:00+00:00",
        deadline_at="2026-06-20T11:00:00+00:00",
    )

    code, captured = run_cli(capsys, "away", "status", "--json")

    assert code == 0
    payload = parse_stdout(captured)
    assert payload["ok"] is True
    assert "session_id" not in payload["sessions"][0]
    assert payload["sessions"][0]["project"] == "Demo"
    assert "oc_chat" not in captured.out

    code, captured = run_cli(capsys, "away", "status", "--include-internal-ids", "--json")

    assert code == 0
    payload = parse_stdout(captured)
    assert payload["sessions"][0]["session_id"] == session_id


def test_away_wait_uses_central_runtime_state_not_workspace_state(
    tmp_path, monkeypatch, capsys
):
    codex_home = tmp_path / "codex-home"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    monkeypatch.chdir(workspace)
    paths = RuntimePaths.from_environment()
    save_config(paths.config_path, AppConfig(feishu_chat_id="oc_chat", route_key_verified=True))
    captured = {}

    class FakeAwayWaiter:
        def __init__(self, *, store, install_store, **_kwargs):
            captured["state_path"] = store.path
            captured["install_state_path"] = install_store.path

        def wait(self, _context):
            return {"status": "ended", "keep_waiting": False}

    monkeypatch.setattr(cli, "AwayWaiter", FakeAwayWaiter)

    code, captured_output = run_cli(
        capsys,
        "away",
        "wait",
        "--project",
        "Demo",
        "--cwd",
        str(workspace),
        "--task",
        "Task",
        "--completed",
        "done",
        "--changed",
        "none",
        "--verification",
        "pytest",
        "--unverified",
        "none",
        "--need-user",
        "continue",
        "--wait-minutes",
        "30",
        "--poll-interval",
        "5",
        "--json",
    )

    assert code == 0
    assert parse_stdout(captured_output)["status"] == "ended"
    assert captured["state_path"] == paths.runtime_state_path
    assert captured["install_state_path"] == paths.install_state_path
    assert not (workspace / ".codex-away-mode").exists()


def _away_wait_args(*, cwd="/workspace/demo"):
    return [
        "away",
        "wait",
        "--project",
        "Skill-Create",
        "--cwd",
        cwd,
        "--task",
        "broken lark",
        "--completed",
        "x",
        "--changed",
        "x",
        "--verification",
        "x",
        "--unverified",
        "x",
        "--need-user",
        "x",
        "--wait-minutes",
        "1",
        "--poll-interval",
        "1",
        "--json",
    ]


def test_away_wait_broken_lark_cli_returns_json_error_without_traceback(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setenv("CODEX_AWAY_HOME", str(tmp_path / "away-home"))
    paths = RuntimePaths.from_environment()
    save_config(
        paths.config_path,
        AppConfig(feishu_chat_id="oc_chat", lark_cli_path="/no/such/codex-away-mode-lark-cli"),
    )

    code, captured = run_cli(capsys, *_away_wait_args())

    payload = parse_stdout(captured)
    assert code == 1
    assert payload["ok"] is False
    assert payload["status"] == "error"
    assert payload["error_code"] == "lark_cli_unavailable"
    assert "没有找到飞书 CLI" in payload["message"]
    assert "traceback" not in captured.err.lower()
    assert "Traceback" not in captured.out


def test_away_wait_missing_lark_cli_name_returns_unavailable(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setenv("CODEX_AWAY_HOME", str(tmp_path / "away-home"))
    monkeypatch.setattr(shutil, "which", lambda _name: None)
    paths = RuntimePaths.from_environment()
    save_config(
        paths.config_path,
        AppConfig(feishu_chat_id="oc_chat", lark_cli_path="missing-lark-cli"),
    )

    code, captured = run_cli(capsys, *_away_wait_args())

    payload = parse_stdout(captured)
    assert code == 1
    assert payload["ok"] is False
    assert payload["status"] == "error"
    assert payload["error_code"] == "lark_cli_unavailable"
    assert "没有找到飞书 CLI" in payload["message"]
    assert "Traceback" not in captured.err


def test_json_cli_unexpected_exception_returns_internal_error_without_traceback(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setenv("CODEX_AWAY_HOME", str(tmp_path / "away-home"))
    paths = RuntimePaths.from_environment()
    save_config(paths.config_path, AppConfig(feishu_chat_id="oc_chat"))

    def raise_unexpected(_args, _paths):
        raise RuntimeError("boom secret oc_xxx")

    monkeypatch.setattr(cli, "_handle_away_wait", raise_unexpected)

    code, captured = run_cli(capsys, *_away_wait_args())

    payload = parse_stdout(captured)
    assert code == 1
    assert payload["ok"] is False
    assert payload["status"] == "error"
    assert payload["error_code"] == "internal_error"
    assert "Traceback" not in captured.err
    assert "oc_xxx" not in json.dumps(payload)


def test_away_cleanup_dry_run_reports_without_mutating(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("CODEX_AWAY_HOME", str(tmp_path / "away-home"))
    paths = RuntimePaths.from_environment()
    store = StateStore(paths.runtime_state_path)
    session_id = store.create_away_session(
        project="Demo",
        cwd="/workspace/demo",
        task="wait",
        started_at="2026-06-18T09:00:00Z",
        deadline_at="2026-06-18T09:30:00Z",
    )
    window_id = store.create_away_window_guarded(
        recipient_id="oc_secret_chat",
        session_id=session_id,
        card_message_id="om_card",
        created_at="2026-06-18T09:00:00Z",
        deadline_at="2026-06-18T09:30:00Z",
        owner=session_id,
        lock_expires_at="2026-06-18T09:30:00Z",
        now="2026-06-18T09:00:00Z",
    )
    assert window_id is not None

    code, captured = run_cli(capsys, "away", "cleanup", "--dry-run", "--json")

    payload = parse_stdout(captured)
    assert code == 0
    assert payload["ok"] is True
    assert payload["command"] == "away cleanup"
    assert payload["dry_run"] is True
    assert payload["closed_count"] == 1
    assert "oc_secret_chat" not in json.dumps(payload)
    assert store.get_away_session(session_id)["status"] == "active"
    assert store.get_window(window_id)["status"] == "waiting"


def test_away_cleanup_json_closes_stale_session(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("CODEX_AWAY_HOME", str(tmp_path / "away-home"))
    paths = RuntimePaths.from_environment()
    store = StateStore(paths.runtime_state_path)
    session_id = store.create_away_session(
        project="Demo",
        cwd="/workspace/demo",
        task="wait",
        started_at="2026-06-18T09:00:00Z",
        deadline_at="2026-06-18T09:30:00Z",
    )
    window_id = store.create_away_window_guarded(
        recipient_id="oc_secret_chat",
        session_id=session_id,
        card_message_id="om_card",
        created_at="2026-06-18T09:00:00Z",
        deadline_at="2026-06-18T09:30:00Z",
        owner=session_id,
        lock_expires_at="2026-06-18T09:30:00Z",
        now="2026-06-18T09:00:00Z",
    )
    assert window_id is not None

    code, captured = run_cli(capsys, "away", "cleanup", "--json")

    payload = parse_stdout(captured)
    assert code == 0
    assert payload["ok"] is True
    assert payload["command"] == "away cleanup"
    assert payload["dry_run"] is False
    assert payload["closed_count"] == 1
    assert "oc_secret_chat" not in json.dumps(payload)
    assert store.get_away_session(session_id)["close_reason"] == "manual_cleanup_timeout"
    assert store.get_window(window_id)["close_reason"] == "manual_cleanup_timeout"


def test_cli_away_status_does_not_read_existing_workspace_state(tmp_path, monkeypatch, capsys):
    codex_home = tmp_path / "codex-home"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    monkeypatch.chdir(workspace)
    paths = RuntimePaths.from_environment()
    local_state = workspace / ".codex-away-mode" / "state.sqlite"
    store = StateStore(local_state)
    session_id = store.create_away_session(
        project="Demo",
        cwd=str(workspace),
        task="Task",
        started_at="2026-06-20T09:00:00+00:00",
        deadline_at="2026-06-20T11:00:00+00:00",
    )
    store.create_away_window(
        session_id=session_id,
        recipient_id="oc_chat",
        card_message_id="om_card",
        created_at="2026-06-20T09:00:00+00:00",
        deadline_at="2026-06-20T11:00:00+00:00",
    )

    code, captured = run_cli(capsys, "away", "status", "--include-internal-ids", "--json")

    assert code == 0
    payload = parse_stdout(captured)
    assert payload["sessions"] == []
    assert payload["runtime_store_present"] is False


def test_notify_stop_ignores_workspace_away_state_when_present(tmp_path, monkeypatch, capsys):
    codex_home = tmp_path / "codex-home"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    paths = RuntimePaths.from_environment()
    save_config(paths.config_path, AppConfig(notification_mode="off"))
    local_state = workspace / ".codex-away-mode" / "state.sqlite"
    store = StateStore(local_state)
    session_id = store.create_away_session(
        project="Demo",
        cwd=str(workspace),
        task="Task",
        started_at="2026-06-20T09:00:00Z",
        deadline_at="2026-06-20T11:00:00Z",
        codex_session_id="codex_session_1",
    )
    window_id = store.create_away_window(
        session_id=session_id,
        recipient_id="oc_chat",
        card_message_id="om_card",
        created_at="2026-06-20T09:00:00Z",
        deadline_at="2026-06-20T11:00:00Z",
    )
    store.mark_prompt_delivered(
        session_id=session_id,
        window_id=window_id,
        message_id="om_reply",
        processed_at="2026-06-20T09:30:00Z",
    )
    client = CapturingNotificationClient()
    monkeypatch.setattr(cli, "_NotificationClient", lambda paths, **kwargs: client)
    monkeypatch.setattr(cli, "SystemClock", lambda: FixedClock())

    code = cli.main(
        ["notify", "stop", "--cwd", str(workspace), "--json"],
        stdin=StringIO(json.dumps({"session_id": "codex_session_1"})),
    )
    captured = capsys.readouterr()

    assert code == 0
    payload = parse_stdout(captured)
    assert payload["status"] == "skipped"
    assert payload["reason"] == "notification_mode_off"
    assert client.calls == []


def test_cli_away_status_uses_global_install_state_for_warnings(
    tmp_path, monkeypatch, capsys
):
    codex_home = tmp_path / "codex-home"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    monkeypatch.chdir(workspace)
    paths = RuntimePaths.from_environment()
    save_config(paths.config_path, AppConfig(route_key_verified=True))
    StateStore(paths.install_state_path).set_install_state("e2e_notify", {"status": "verified"})
    store = StateStore(paths.runtime_state_path)
    session_id = store.create_away_session(
        project="Demo",
        cwd=str(workspace),
        task="Task",
        started_at="2026-06-20T09:00:00+00:00",
        deadline_at="2026-06-20T11:00:00+00:00",
    )
    store.create_away_window(
        session_id=session_id,
        recipient_id="oc_chat",
        card_message_id="om_card",
        created_at="2026-06-20T09:00:00+00:00",
        deadline_at="2026-06-20T11:00:00+00:00",
    )

    code, captured = run_cli(capsys, "away", "status", "--include-internal-ids", "--json")

    assert code == 0
    payload = parse_stdout(captured)
    assert payload["sessions"][0]["session_id"] == session_id
    assert "doctor_e2e_unverified" not in payload["sessions"][0]["warnings"]
    assert "hook_trust_unverified" in payload["sessions"][0]["warnings"]


def test_away_wait_requires_context_args(capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(["away", "wait", "--json"])

    assert exc.value.code == 2
