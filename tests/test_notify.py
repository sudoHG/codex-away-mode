import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from codex_away_mode import notify
from codex_away_mode.state import StateStore


class FakePaths:
    def __init__(self, root):
        self.codex_home = root
        self.data_dir = root
        self.config_path = root / "config.toml"
        self.install_state_path = root / "install-state.sqlite"
        self.runtime_dir = root / "runtime"
        self.runtime_state_path = self.runtime_dir / "state.sqlite"
        self.runtime_prompt_marker_dir = self.runtime_dir / "user-turns"
        self.runtime_summary_dir = self.runtime_dir / "summaries"
        self.log_dir = root / "logs"


class FakeLark:
    def __init__(self, result=None):
        self.calls = []
        self.summary_cwds = []
        self.result = result

    def send_summary_card(self, markdown, cwd=None):
        self.summary_cwds.append(cwd)
        self.calls.append(("summary", markdown))
        return self.result

    def send_fallback_card(self, cwd):
        self.calls.append(("fallback", cwd))
        return self.result

    def send_test_notification(self):
        self.calls.append(("test", None))
        return self.result

    def send_away_early_exit_card(self, payload):
        self.calls.append(("away_early_exit", payload))
        return self.result

    def send_away_timeout_card(self, payload):
        self.calls.append(("away_timeout", payload))
        return self.result

    def send_permission_request_card(self, payload):
        self.calls.append(("permission_request", payload))
        return self.result


def write_summary(path, cwd, extra="done"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"**项目**\nDemo\n\n**工作目录**\n{cwd}\n\n**完成**\n{extra}\n",
        encoding="utf-8",
    )


def write_goal_transcript(path, statuses):
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = []
    for index, status in enumerate(statuses):
        call_id = f"call_{index}"
        lines.append(
            {
                "type": "response_item",
                "payload": {
                    "type": "function_call",
                    "name": "get_goal",
                    "call_id": call_id,
                    "arguments": "{}",
                },
            }
        )
        output = {"goal": None}
        if status is not None:
            output = {"goal": {"threadId": "thread_1", "status": status}}
        lines.append(
            {
                "type": "response_item",
                "payload": {
                    "type": "function_call_output",
                    "call_id": call_id,
                    "output": json.dumps(output),
                },
            }
        )
    path.write_text(
        "".join(json.dumps(line) + "\n" for line in lines),
        encoding="utf-8",
    )


def hook_stdin_for_transcript(path):
    return json.dumps({"transcript_path": str(path)})


def test_mark_prompt_writes_hashed_marker_without_cwd_leak(tmp_path):
    paths = FakePaths(tmp_path)
    workspace = tmp_path / "workspace" / "demo"
    workspace.mkdir(parents=True)
    cwd = str(workspace)
    now = datetime(2026, 6, 18, 10, 0, tzinfo=timezone.utc)

    marker_key = notify.mark_prompt(paths, cwd=cwd, now=now)
    marker = StateStore(paths.runtime_state_path).get_prompt_marker(cwd)

    assert marker is not None
    assert marker["cwd_hash"] == marker_key
    assert cwd not in marker_key
    assert not (workspace / ".codex-away-mode").exists()


def test_stage_summary_writes_to_runtime_store_without_workspace_files(tmp_path):
    paths = FakePaths(tmp_path / "codex-home")
    workspace = tmp_path / "workspace" / "demo"
    workspace.mkdir(parents=True)
    cwd = str(workspace)
    now = datetime(2026, 6, 18, 10, 0, tzinfo=timezone.utc)
    markdown = "**项目**\nDemo\n\n**完成**\nDone\n"

    summary_key = notify.stage_summary(
        paths,
        cwd=cwd,
        summary_markdown=markdown,
        now=now,
    )

    summary = StateStore(paths.runtime_state_path).get_staged_summary(cwd)
    assert summary is not None
    assert summary["cwd_hash"] == summary_key
    assert summary["summary_markdown"] == markdown
    assert not (workspace / ".codex-away-mode").exists()


def test_resolve_notify_cwd_priority_and_absolute_stdin():
    assert (
        notify.resolve_notify_cwd("/explicit", '{"cwd": "/stdin"}', "/process")
        == "/explicit"
    )
    assert notify.resolve_notify_cwd(None, '{"cwd": "/stdin"}', "/process") == "/stdin"
    assert (
        notify.resolve_notify_cwd(None, '{"cwd": "relative"}', "/process")
        == "/process"
    )


def test_goal_status_from_transcript_reads_last_goal_state(tmp_path):
    transcript = tmp_path / "session.jsonl"
    write_goal_transcript(transcript, ["active", "complete"])

    assert notify.goal_status_from_transcript(transcript) == "complete"

    write_goal_transcript(transcript, [None])
    assert notify.goal_status_from_transcript(transcript) == "none"


def test_completion_summary_requires_matching_cwd_and_freshness(tmp_path, monkeypatch):
    monkeypatch.setattr(notify.tempfile, "gettempdir", lambda: "/not-the-pytest-temp")
    now = datetime(2026, 6, 18, 10, 0, tzinfo=timezone.utc)
    paths = FakePaths(tmp_path / "codex-home")
    workspace = tmp_path / "workspace" / "demo"
    cwd = str(workspace)
    expected_markdown = f"**项目**\nDemo\n\n**工作目录**\n{cwd}\n\n**完成**\ndone\n"
    notify.stage_summary(paths, cwd=cwd, summary_markdown=expected_markdown, now=now)
    lark = FakeLark()

    result = notify.send_completion_from_summary(
        paths, lark, cwd=cwd, now=now
    )

    assert result.status == "summary_sent"
    assert lark.calls == [("summary", expected_markdown)]
    assert StateStore(paths.runtime_state_path).get_staged_summary(cwd) is None
    assert not (workspace / ".codex-away-mode").exists()


def test_completion_summary_passes_cwd_to_summary_card_sender(tmp_path, monkeypatch):
    monkeypatch.setattr(notify.tempfile, "gettempdir", lambda: "/not-the-pytest-temp")
    now = datetime(2026, 6, 18, 10, 0, tzinfo=timezone.utc)
    paths = FakePaths(tmp_path / "codex-home")
    workspace = tmp_path / "workspace" / "immichSlides-app"
    workspace.mkdir(parents=True)
    cwd = str(workspace)
    notify.stage_summary(paths, cwd=cwd, summary_markdown="plain final summary", now=now)
    lark = FakeLark()

    result = notify.send_completion_from_summary(
        paths, lark, cwd=cwd, now=now
    )

    assert result.status == "summary_sent"
    assert lark.calls == [("summary", "plain final summary")]
    assert lark.summary_cwds == [cwd]


def test_completion_summary_matches_equivalent_normalized_cwd(tmp_path, monkeypatch):
    monkeypatch.setattr(notify.tempfile, "gettempdir", lambda: "/not-the-pytest-temp")
    now = datetime(2026, 6, 18, 10, 0, tzinfo=timezone.utc)
    paths = FakePaths(tmp_path / "codex-home")
    workspace = tmp_path / "workspace" / "demo"
    workspace.mkdir(parents=True)
    cwd = str(workspace)
    equivalent_cwd = str(tmp_path / "workspace" / ".." / "workspace" / "demo")
    notify.stage_summary(paths, cwd=equivalent_cwd, summary_markdown="summary", now=now)
    lark = FakeLark()

    result = notify.send_completion_from_summary(
        paths, lark, cwd=cwd, now=now
    )

    assert result.status == "summary_sent"
    assert lark.calls[0][0] == "summary"


def test_completion_summary_accepts_child_project_inside_current_workspace(tmp_path, monkeypatch):
    monkeypatch.setattr(notify.tempfile, "gettempdir", lambda: "/not-the-pytest-temp")
    now = datetime(2026, 6, 18, 10, 0, tzinfo=timezone.utc)
    paths = FakePaths(tmp_path / "codex-home")
    workspace = tmp_path / "workspace"
    child_project = workspace / "Skill项目" / "飞书通知与AwayMode"
    child_project.mkdir(parents=True)
    cwd = str(workspace)
    notify.stage_summary(paths, cwd=cwd, summary_markdown="summary", now=now)
    lark = FakeLark()

    result = notify.send_completion_from_summary(
        paths, lark, cwd=cwd, now=now
    )

    assert result.status == "summary_sent"
    assert lark.calls[0][0] == "summary"


def test_completion_summary_isolated_by_cwd_hash(tmp_path):
    now = datetime(2026, 6, 18, 10, 0, tzinfo=timezone.utc)
    paths = FakePaths(tmp_path)
    notify.stage_summary(paths, cwd="/workspace/a", summary_markdown="summary-a", now=now)
    notify.stage_summary(paths, cwd="/workspace/b", summary_markdown="summary-b", now=now)
    lark = FakeLark()

    result = notify.send_completion_from_summary(
        paths, lark, cwd="/workspace/b", now=now
    )

    assert result.status == "summary_sent"
    assert lark.calls == [("summary", "summary-b")]
    assert StateStore(paths.runtime_state_path).get_staged_summary("/workspace/a") is not None
    assert StateStore(paths.runtime_state_path).get_staged_summary("/workspace/b") is None


def test_completion_ignores_legacy_workspace_summary_when_runtime_has_no_summary(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(notify.tempfile, "gettempdir", lambda: "/not-the-pytest-temp")
    now = datetime(2026, 6, 18, 10, 0, tzinfo=timezone.utc)
    paths = FakePaths(tmp_path / "codex-home")
    workspace = tmp_path / "workspace" / "demo"
    workspace.mkdir(parents=True)
    write_summary(workspace / ".codex-away-mode" / "latest-summary.md", str(workspace))
    lark = FakeLark()

    result = notify.send_completion_from_summary(
        paths,
        lark,
        cwd=str(workspace),
        now=now,
    )

    assert result.status == "skipped"
    assert result.detail == "summary_missing"
    assert lark.calls == []
    assert (workspace / ".codex-away-mode" / "latest-summary.md").exists()


def test_completion_summary_is_removed_after_successful_send(tmp_path, monkeypatch):
    monkeypatch.setattr(notify.tempfile, "gettempdir", lambda: "/not-the-pytest-temp")
    now = datetime(2026, 6, 18, 10, 0, tzinfo=timezone.utc)
    paths = FakePaths(tmp_path / "codex-home")
    workspace = tmp_path / "workspace" / "demo"
    cwd = str(workspace)
    notify.stage_summary(paths, cwd=cwd, summary_markdown="summary", now=now)
    notify.mark_prompt(paths, cwd=cwd, now=now)

    result = notify.send_completion_from_summary(
        paths, FakeLark(), cwd=cwd, now=now
    )

    assert result.status == "summary_sent"
    store = StateStore(paths.runtime_state_path)
    assert store.get_staged_summary(cwd) is None
    assert store.get_prompt_marker(cwd) is None


def test_completion_does_not_send_mismatched_or_stale_summary(tmp_path):
    now = datetime(2026, 6, 18, 10, 0, tzinfo=timezone.utc)
    paths = FakePaths(tmp_path)
    notify.stage_summary(paths, cwd="/workspace/other", summary_markdown="other", now=now)
    lark = FakeLark()

    mismatch = notify.send_completion_from_summary(
        paths, lark, cwd="/workspace/demo", now=now
    )

    assert mismatch.status == "skipped"
    assert lark.calls == []

    stale = now - timedelta(seconds=301)
    notify.stage_summary(paths, cwd="/workspace/demo", summary_markdown="stale", now=stale)

    stale_result = notify.send_completion_from_summary(
        paths, lark, cwd="/workspace/demo", now=now, max_age_seconds=300
    )

    assert stale_result.status == "skipped"
    assert lark.calls == []


def test_completion_sends_missing_summary_fallback_when_marker_is_fresh_and_no_goal_is_active(tmp_path):
    now = datetime(2026, 6, 18, 10, 0, tzinfo=timezone.utc)
    paths = FakePaths(tmp_path)
    cwd = "/workspace/demo"
    transcript = tmp_path / "session.jsonl"
    write_goal_transcript(transcript, [])
    notify.mark_prompt(paths, cwd=cwd, now=now)
    lark = FakeLark()

    result = notify.send_completion_from_summary(
        paths,
        lark,
        cwd=cwd,
        now=now,
        hook_stdin=hook_stdin_for_transcript(transcript),
    )

    assert result.status == "fallback_sent"
    assert result.detail == "summary_missing"
    assert lark.calls == [("fallback", cwd)]
    assert StateStore(paths.runtime_state_path).get_prompt_marker(cwd) is None


def test_completion_skips_missing_summary_fallback_while_goal_is_active_and_keeps_marker(tmp_path):
    now = datetime(2026, 6, 18, 10, 0, tzinfo=timezone.utc)
    paths = FakePaths(tmp_path)
    cwd = "/workspace/demo"
    transcript = tmp_path / "session.jsonl"
    write_goal_transcript(transcript, ["active"])
    notify.mark_prompt(paths, cwd=cwd, now=now)
    lark = FakeLark()

    result = notify.send_completion_from_summary(
        paths,
        lark,
        cwd=cwd,
        now=now,
        hook_stdin=hook_stdin_for_transcript(transcript),
    )

    assert result.status == "skipped"
    assert result.detail == "goal_active"
    assert lark.calls == []
    assert StateStore(paths.runtime_state_path).get_prompt_marker(cwd) is not None


def test_completion_skips_missing_summary_when_goal_status_is_unknown(tmp_path):
    now = datetime(2026, 6, 18, 10, 0, tzinfo=timezone.utc)
    paths = FakePaths(tmp_path)
    notify.mark_prompt(paths, cwd="/workspace/demo", now=now)
    lark = FakeLark()

    result = notify.send_completion_from_summary(
        paths, lark, cwd="/workspace/demo", now=now
    )

    assert result.status == "skipped"
    assert result.detail == "summary_missing_goal_unknown"
    assert lark.calls == []
    assert StateStore(paths.runtime_state_path).get_prompt_marker("/workspace/demo") is None


def test_completion_skips_non_user_workspace_even_with_summary_and_marker(tmp_path):
    now = datetime(2026, 6, 18, 10, 0, tzinfo=timezone.utc)
    paths = FakePaths(tmp_path)
    notify.mark_prompt(paths, cwd="/", now=now)
    lark = FakeLark()

    result = notify.send_completion_from_summary(paths, lark, cwd="/", now=now)

    assert result.status == "skipped"
    assert result.detail == "non_user_workspace"
    assert lark.calls == []
    assert StateStore(paths.runtime_state_path).get_prompt_marker("/") is None


def test_stop_hook_does_not_close_waiting_session_without_lease_evidence(tmp_path):
    now = datetime(2026, 6, 20, 10, 0, tzinfo=timezone.utc)
    paths = FakePaths(tmp_path)
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
    store.record_card(
        card_message_id="om_card",
        window_id=window_id,
        session_id=session_id,
        card_kind="initial",
        status="active",
        sent_at="2026-06-20T09:00:00Z",
    )
    store.mark_prompt_delivered(
        session_id=session_id,
        window_id=window_id,
        message_id="om_reply",
        processed_at="2026-06-20T09:30:00Z",
    )
    lark = FakeLark()

    result = notify.send_away_early_exit_if_needed(
        paths,
        lark,
        cwd="/workspace/demo",
        now=now,
        hook_stdin=json.dumps({"session_id": "codex_session_1"}),
    )

    assert result.status == "away_active_stop_ignored"
    assert result.detail == "insufficient_evidence"
    assert lark.calls == []
    assert store.get_away_session(session_id)["close_reason"] is None
    assert store.get_window(window_id)["close_reason"] is None


def test_stop_hook_keeps_waiting_session_when_waiter_lease_alive(tmp_path):
    now = datetime(2026, 6, 20, 10, 0, tzinfo=timezone.utc)
    paths = FakePaths(tmp_path)
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
    store.renew_waiter_lease(
        session_id,
        owner="waiter-1",
        now="2026-06-20T10:00:00+00:00",
        expires_at="2026-06-20T10:00:45+00:00",
    )
    lark = FakeLark()

    result = notify.send_away_early_exit_if_needed(
        paths,
        lark,
        cwd="/workspace/demo",
        now=now,
        hook_stdin=json.dumps({"session_id": "codex_session_1"}),
    )

    assert result.status == "away_active_stop_ignored"
    assert result.detail == "waiter_alive"
    assert lark.calls == []
    assert store.get_away_session(session_id)["close_reason"] is None
    assert store.get_window(window_id)["close_reason"] is None


def test_stop_hook_does_not_close_when_waiter_lease_expired_but_deadline_not_reached(tmp_path):
    now = datetime(2026, 6, 20, 10, 0, tzinfo=timezone.utc)
    paths = FakePaths(tmp_path)
    store = StateStore(paths.runtime_state_path)
    session_id = store.create_away_session(
        project="Demo",
        cwd="/workspace/demo",
        task="Task",
        started_at="2026-06-20T09:00:00Z",
        deadline_at="2026-06-20T11:00:00Z",
    )
    window_id = store.create_away_window(
        session_id=session_id,
        recipient_id="oc_chat",
        card_message_id="om_card",
        created_at="2026-06-20T09:00:00Z",
        deadline_at="2026-06-20T11:00:00Z",
    )
    store.renew_waiter_lease(
        session_id,
        owner="waiter-1",
        now="2026-06-20T09:58:00+00:00",
        expires_at="2026-06-20T09:58:30+00:00",
    )
    lark = FakeLark()

    result = notify.send_away_early_exit_if_needed(
        paths,
        lark,
        cwd="/workspace/demo",
        now=now,
        hook_stdin=None,
    )

    assert result.status == "away_active_stop_ignored"
    assert result.detail == "insufficient_evidence"
    assert lark.calls == []
    assert store.get_away_session(session_id)["close_reason"] is None
    assert store.get_window(window_id)["close_reason"] is None


def test_deadline_passed_closes_and_sends_timeout_card(tmp_path):
    now = datetime(2026, 6, 20, 10, 0, tzinfo=timezone.utc)
    paths = FakePaths(tmp_path)
    store = StateStore(paths.runtime_state_path)
    session_id = store.create_away_session(
        project="Demo",
        cwd="/workspace/demo",
        task="Task",
        started_at="2026-06-20T08:00:00Z",
        deadline_at="2026-06-20T09:00:00Z",
    )
    window_id = store.create_away_window(
        session_id=session_id,
        recipient_id="oc_chat",
        card_message_id="om_card",
        created_at="2026-06-20T08:00:00Z",
        deadline_at="2026-06-20T09:00:00Z",
    )
    lark = FakeLark()

    result = notify.send_away_early_exit_if_needed(
        paths,
        lark,
        cwd="/workspace/demo",
        now=now,
        hook_stdin=None,
    )

    assert result.status == "away_deadline_closed"
    assert result.detail == "stale_timeout"
    assert lark.calls[0][0] == "away_timeout"
    assert store.get_away_session(session_id)["close_reason"] == "stale_timeout"
    assert store.get_window(window_id)["close_reason"] == "stale_timeout"
    assert store.get_runtime_lock(f"away-waiter:{session_id}") is None


def test_stop_hook_deadline_candidate_returns_without_closing_fresh_session(tmp_path):
    now = datetime(2026, 6, 20, 10, 0, tzinfo=timezone.utc)
    paths = FakePaths(tmp_path)
    store = StateStore(paths.runtime_state_path)
    fresh_session_id = store.create_away_session(
        project="Fresh",
        cwd="/workspace/demo",
        task="Fresh task",
        started_at="2026-06-20T08:00:00Z",
        deadline_at="2026-06-20T11:00:00Z",
    )
    fresh_window_id = store.create_away_window(
        session_id=fresh_session_id,
        recipient_id="oc_chat",
        card_message_id="om_fresh_card",
        created_at="2026-06-20T08:00:00Z",
        deadline_at="2026-06-20T11:00:00Z",
    )
    stale_session_id = store.create_away_session(
        project="Stale",
        cwd="/workspace/demo",
        task="Stale task",
        started_at="2026-06-20T09:30:00Z",
        deadline_at="2026-06-20T09:45:00Z",
    )
    stale_window_id = store.create_away_window(
        session_id=stale_session_id,
        recipient_id="oc_chat",
        card_message_id="om_stale_card",
        created_at="2026-06-20T09:30:00Z",
        deadline_at="2026-06-20T09:45:00Z",
    )
    lark = FakeLark()

    result = notify.send_away_early_exit_if_needed(
        paths,
        lark,
        cwd="/workspace/demo",
        now=now,
        hook_stdin=None,
    )

    assert result.status == "away_deadline_closed"
    assert lark.calls[0][0] == "away_timeout"
    assert lark.calls[0][1]["project"] == "Stale"
    assert store.get_away_session(stale_session_id)["close_reason"] == "stale_timeout"
    assert store.get_window(stale_window_id)["close_reason"] == "stale_timeout"
    assert store.get_away_session(fresh_session_id)["close_reason"] is None
    assert store.get_window(fresh_window_id)["close_reason"] is None


def test_stop_hook_does_not_close_waiting_paused_session_by_default(tmp_path):
    now = datetime(2026, 6, 20, 10, 0, tzinfo=timezone.utc)
    paths = FakePaths(tmp_path)
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
    lark = FakeLark()

    result = notify.send_away_early_exit_if_needed(
        paths,
        lark,
        cwd="/workspace/demo",
        now=now,
        hook_stdin=json.dumps({"session_id": "codex_session_1"}),
    )

    assert result.status == "away_active_stop_ignored"
    assert result.detail == "insufficient_evidence"
    assert lark.calls == []
    assert store.get_away_session(session_id)["status"] == "waiting_paused"
    assert store.get_window(window_id)["status"] == "waiting_paused"


def test_send_test_notification_persists_chat_id(tmp_path):
    paths = FakePaths(tmp_path)
    lark = FakeLark(result=SimpleNamespace(chat_id="oc_test_chat"))

    result = notify.send_test_notification(paths, lark)

    assert result.chat_id == "oc_test_chat"
    assert "feishu_chat_id = \"oc_test_chat\"" in paths.config_path.read_text(
        encoding="utf-8"
    )


def test_send_permission_request_deduplicates_same_payload(tmp_path):
    paths = FakePaths(tmp_path)
    lark = FakeLark(result=SimpleNamespace(message_id="om_permission"))
    now = datetime(2026, 6, 24, 8, 0, tzinfo=timezone.utc)
    hook_stdin = json.dumps(
        {
            "hook_event_name": "PermissionRequest",
            "tool_name": "Bash",
            "cwd": "/workspace/demo",
            "session_id": "session_1",
            "turn_id": "turn_1",
            "tool_input": {
                "command": "rm /workspace-outside/codex-desktop-permission-target.txt",
                "description": "需要删除一个外部测试文件。",
            },
        }
    )

    first = notify.send_permission_request(
        paths,
        lark,
        hook_stdin=hook_stdin,
        now=now,
    )
    second = notify.send_permission_request(
        paths,
        lark,
        hook_stdin=hook_stdin,
        now=now + timedelta(seconds=30),
    )

    assert first.status == "sent"
    assert second.status == "suppressed"
    assert [call[0] for call in lark.calls] == ["permission_request"]
    stored = StateStore(paths.runtime_state_path).get_approval_notification(
        first.detail
    )
    assert stored["suppressed_count"] == 1
    assert "/workspace-outside" not in stored["dedupe_key"]


def test_notification_modes_and_expired_snooze_are_pure(tmp_path):
    paths = FakePaths(tmp_path)
    now = datetime(2026, 6, 18, 10, 0, tzinfo=timezone.utc)

    notify.set_notification_mode(paths, "off")
    assert notify.effective_notification_mode(paths, now=now) == "off"

    notify.set_notification_mode(paths, "all")
    assert notify.effective_notification_mode(paths, now=now) == "all"

    notify.set_notification_mode(paths, "snooze", until=now + timedelta(minutes=5))
    active_contents = paths.config_path.read_text(encoding="utf-8")
    assert notify.effective_notification_mode(paths, now=now) == "off"

    assert (
        notify.effective_notification_mode(paths, now=now + timedelta(minutes=6))
        == "all"
    )
    assert paths.config_path.read_text(encoding="utf-8") == active_contents

    with pytest.raises(ValueError):
        notify.set_notification_mode(paths, "invalid")
