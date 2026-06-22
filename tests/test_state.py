import inspect
import sqlite3
from concurrent.futures import ThreadPoolExecutor

from codex_away_mode.state import StateStore


def test_state_store_enables_wal_busy_timeout_and_required_tables(tmp_path):
    store = StateStore(tmp_path / "state.sqlite")

    with sqlite3.connect(tmp_path / "state.sqlite") as conn:
        journal_mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        busy_timeout = conn.execute("PRAGMA busy_timeout").fetchone()[0]
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }

    assert journal_mode == "wal"
    assert busy_timeout >= 5000
    assert {
        "install_state",
        "notification_events",
        "away_sessions",
        "away_windows",
        "processed_messages",
        "ordinary_dm_hints",
        "runtime_locks",
        "diagnostic_events",
        "away_cards",
        "away_resume_tokens",
    } <= tables

    with sqlite3.connect(tmp_path / "state.sqlite") as conn:
        session_columns = {
            row[1] for row in conn.execute("PRAGMA table_info(away_sessions)")
        }
        window_columns = {
            row[1] for row in conn.execute("PRAGMA table_info(away_windows)")
        }

    assert {
        "deadline_at",
        "active_window_id",
        "codex_session_id",
        "reply_delivered_count",
        "last_delivered_message_id",
        "updated_at",
        "closed_at",
        "close_reason",
        "last_error_summary",
    } <= session_columns
    assert {
        "reply_delivered_count",
        "last_reply_message_id",
        "last_command_message_id",
    } <= window_columns


def test_processed_messages_and_ordinary_dm_hints_dedupe(tmp_path):
    store = StateStore(tmp_path / "state.sqlite")
    session_id = store.create_away_session(
        project="Codex Away Mode",
        cwd="/workspace/project",
        task="implement state",
        started_at="2026-06-18T10:00:00Z",
    )
    window_id = store.create_away_window(
        session_id=session_id,
        recipient_id="ou_test",
        card_message_id="om_card_1",
        created_at="2026-06-18T10:00:00Z",
        deadline_at="2026-06-18T10:30:00Z",
    )

    assert store.mark_processed(
        message_id="om_msg_1",
        message_kind="card_reply",
        window_id=window_id,
        action="deliver_prompt",
        message_text_hash="sha256:abc",
    )
    assert not store.mark_processed(
        message_id="om_msg_1",
        message_kind="card_reply",
        window_id=window_id,
        action="deliver_prompt",
        message_text_hash="sha256:abc",
    )

    assert store.record_ordinary_dm_hint(
        hint_key="chat_1:msg_1",
        recipient_id="ou_test",
        active_window_count=1,
        source_message_id="om_msg_1",
    )
    assert not store.record_ordinary_dm_hint(
        hint_key="chat_1:msg_1",
        recipient_id="ou_test",
        active_window_count=1,
        source_message_id="om_msg_1",
    )


def test_ordinary_dm_event_sends_hint_for_each_distinct_message(tmp_path):
    store = StateStore(tmp_path / "state.sqlite")
    session_id = store.create_away_session(
        project="Codex Away Mode",
        cwd="/workspace/project",
        task="implement state",
        started_at="2026-06-18T10:00:00Z",
    )
    window_id = store.create_away_window(
        session_id=session_id,
        recipient_id="oc_chat",
        card_message_id="om_card_1",
        created_at="2026-06-18T10:00:00Z",
        deadline_at="2026-06-18T10:30:00Z",
    )

    assert (
        store.record_ordinary_dm_event(
            message_id="om_dm_1",
            window_id=window_id,
            recipient_id="oc_chat",
            active_window_count=1,
            message_text_hash="sha256:one",
            now="2026-06-18T10:00:00+00:00",
        )
        == "send_hint"
    )
    assert (
        store.record_ordinary_dm_event(
            message_id="om_dm_2",
            window_id=window_id,
            recipient_id="oc_chat",
            active_window_count=1,
            message_text_hash="sha256:two",
            now="2026-06-18T10:04:59+00:00",
        )
        == "send_hint"
    )
    assert (
        store.record_ordinary_dm_event(
            message_id="om_dm_3",
            window_id=window_id,
            recipient_id="oc_chat",
            active_window_count=1,
            message_text_hash="sha256:three",
            now="2026-06-18T10:05:01+00:00",
        )
        == "send_hint"
    )
    assert (
        store.record_ordinary_dm_event(
            message_id="om_dm_3",
            window_id=window_id,
            recipient_id="oc_chat",
            active_window_count=1,
            message_text_hash="sha256:three",
            now="2026-06-18T10:05:02+00:00",
        )
        == "already_processed"
    )

    with sqlite3.connect(tmp_path / "state.sqlite") as conn:
        conn.row_factory = sqlite3.Row
        processed = [
            dict(row)
            for row in conn.execute(
                "SELECT message_id, action FROM processed_messages ORDER BY message_id"
            )
        ]
        hint_count = conn.execute("SELECT COUNT(*) FROM ordinary_dm_hints").fetchone()[0]

    assert processed == [
        {"message_id": "om_dm_1", "action": "ordinary_dm_hint"},
        {"message_id": "om_dm_2", "action": "ordinary_dm_hint"},
        {"message_id": "om_dm_3", "action": "ordinary_dm_hint"},
    ]
    assert hint_count == 3


def test_resume_token_hash_round_trip_and_clear(tmp_path):
    store = StateStore(tmp_path / "state.sqlite")
    session_id = store.create_away_session(
        project="Codex Away Mode",
        cwd="/workspace/project",
        task="implement state",
        started_at="2026-06-18T10:00:00Z",
    )

    store.set_resume_token_hash(
        session_id=session_id,
        token_hash="sha256:tokenhash",
        created_at="2026-06-18T10:00:01Z",
    )

    assert store.get_resume_token_hash(session_id) == "sha256:tokenhash"
    store.clear_resume_token(session_id)
    assert store.get_resume_token_hash(session_id) is None


def test_locks_block_second_owner_until_expiry(tmp_path):
    store = StateStore(tmp_path / "state.sqlite")

    assert store.acquire_lock(
        lock_key="away-window:ou_test",
        owner="worker-1",
        expires_at="2026-06-18T10:01:00Z",
        now="2026-06-18T10:00:00Z",
    )
    assert not store.acquire_lock(
        lock_key="away-window:ou_test",
        owner="worker-2",
        expires_at="2026-06-18T10:02:00Z",
        now="2026-06-18T10:00:30Z",
    )
    assert store.acquire_lock(
        lock_key="away-window:ou_test",
        owner="worker-2",
        expires_at="2026-06-18T10:03:00Z",
        now="2026-06-18T10:01:01Z",
    )


def test_waiter_lease_round_trip_and_release(tmp_path):
    store = StateStore(tmp_path / "state.sqlite")

    assert store.renew_waiter_lease(
        "sess_1",
        owner="waiter-1",
        now="2026-06-18T10:00:00Z",
        expires_at="2026-06-18T10:00:45Z",
    )
    lease = store.get_waiter_lease("sess_1")
    assert lease["lock_key"] == "away-waiter:sess_1"
    assert lease["owner"] == "waiter-1"
    assert lease["expires_at"] == "2026-06-18T10:00:45Z"

    assert store.release_waiter_lease("sess_1")
    assert store.get_waiter_lease("sess_1") is None


def test_diagnostic_events_are_capped_per_kind(tmp_path):
    store = StateStore(tmp_path / "state.sqlite")

    for index in range(205):
        store.record_diagnostic_event(
            event_kind="away_active_stop_ignored",
            severity="info",
            message=f"ignored {index}",
            detail={"index": index},
            created_at=f"2026-06-18T10:{index // 60:02d}:{index % 60:02d}Z",
            per_kind_limit=200,
        )

    events = store.list_diagnostic_events("away_active_stop_ignored", limit=500)
    assert len(events) == 200
    assert events[0]["message"] == "ignored 5"
    assert events[-1]["message"] == "ignored 204"


def test_sessions_windows_and_window_updates_round_trip(tmp_path):
    store = StateStore(tmp_path / "state.sqlite")
    session_id = store.create_away_session(
        project="Codex Away Mode",
        cwd="/workspace/project",
        task="implement state",
        started_at="2026-06-18T10:00:00Z",
    )
    window_id = store.create_away_window(
        session_id=session_id,
        recipient_id="ou_test",
        card_message_id="om_card_1",
        created_at="2026-06-18T10:00:00Z",
        deadline_at="2026-06-18T10:30:00Z",
    )

    assert store.active_window_count("ou_test") == 1
    assert store.find_window_by_card_message_id("om_card_1")["window_id"] == window_id

    store.extend_window(window_id, new_deadline_at="2026-06-18T11:00:00Z")
    store.mark_reminder_sent(window_id, sent_at="2026-06-18T10:25:00Z")
    window = store.get_window(window_id)
    assert window["session_id"] == session_id
    assert window["deadline_at"] == "2026-06-18T11:00:00Z"
    assert window["extend_count"] == 1
    assert window["reminder_sent_at"] == "2026-06-18T10:25:00Z"

    store.close_window(
        window_id,
        status="closed",
        reason="user_requested",
        closed_at="2026-06-18T10:26:00Z",
    )
    closed = store.get_window(window_id)
    assert closed["status"] == "closed"
    assert closed["close_reason"] == "user_requested"
    assert closed["closed_at"] == "2026-06-18T10:26:00Z"
    assert store.active_window_count("ou_test") == 0


def test_session_window_card_history_and_resume_rotation_round_trip(tmp_path):
    store = StateStore(tmp_path / "state.sqlite")
    session_id = store.create_away_session(
        project="Codex Away Mode",
        cwd="/workspace/project",
        task="implement state",
        started_at="2026-06-18T10:00:00Z",
        deadline_at="2026-06-18T10:30:00Z",
        codex_session_id="codex_session_1",
    )
    window_id = store.create_away_window(
        session_id=session_id,
        recipient_id="ou_test",
        card_message_id="om_card_1",
        created_at="2026-06-18T10:00:00Z",
        deadline_at="2026-06-18T10:30:00Z",
    )
    store.record_card(
        card_message_id="om_card_1",
        window_id=window_id,
        session_id=session_id,
        card_kind="initial",
        status="active",
        sent_at="2026-06-18T10:00:00Z",
    )

    assert store.get_active_session_for_resume(session_id)["active_window_id"] == window_id
    assert store.find_card("om_card_1")["status"] == "active"

    store.rotate_window_card(
        window_id=window_id,
        new_card_message_id="om_card_2",
        card_kind="progress",
        sent_at="2026-06-18T10:05:00Z",
    )

    window = store.get_window(window_id)
    assert window["card_message_id"] == "om_card_2"
    assert store.find_card("om_card_1")["status"] == "retired"
    assert store.find_card("om_card_1")["retired_at"] == "2026-06-18T10:05:00Z"
    assert store.find_card("om_card_2")["status"] == "active"


def test_mark_prompt_delivered_pauses_session_and_window_without_releasing_lock(tmp_path):
    store = StateStore(tmp_path / "state.sqlite")
    session_id = store.create_away_session(
        project="Codex Away Mode",
        cwd="/workspace/project",
        task="first wait",
        started_at="2026-06-18T10:00:00Z",
        deadline_at="2026-06-18T11:00:00Z",
    )
    window_id = store.create_away_window_guarded(
        recipient_id="ou_test",
        session_id=session_id,
        card_message_id="om_card_1",
        created_at="2026-06-18T10:00:00Z",
        deadline_at="2026-06-18T11:00:00Z",
        owner=session_id,
        lock_expires_at="2026-06-18T11:00:00Z",
        now="2026-06-18T10:00:00Z",
    )
    assert window_id is not None

    assert store.mark_prompt_delivered(
        session_id=session_id,
        window_id=window_id,
        message_id="om_reply_1",
        processed_at="2026-06-18T10:05:00Z",
    )

    session = store.get_away_session(session_id)
    window = store.get_window(window_id)
    assert session["status"] == "waiting_paused"
    assert session["reply_delivered_count"] == 1
    assert session["last_delivered_message_id"] == "om_reply_1"
    assert window["status"] == "waiting_paused"
    assert window["reply_delivered_count"] == 1
    assert window["last_reply_message_id"] == "om_reply_1"
    assert store.get_runtime_lock("away-window:ou_test")["owner"] == session_id


def test_extend_window_updates_session_window_and_lock_deadline(tmp_path):
    store = StateStore(tmp_path / "state.sqlite")
    session_id = store.create_away_session(
        project="Codex Away Mode",
        cwd="/workspace/project",
        task="first wait",
        started_at="2026-06-18T10:00:00Z",
        deadline_at="2026-06-18T10:30:00Z",
    )
    window_id = store.create_away_window_guarded(
        recipient_id="ou_test",
        session_id=session_id,
        card_message_id="om_card_1",
        created_at="2026-06-18T10:00:00Z",
        deadline_at="2026-06-18T10:30:00Z",
        owner=session_id,
        lock_expires_at="2026-06-18T10:30:00Z",
        now="2026-06-18T10:00:00Z",
    )

    assert store.extend_window(window_id, new_deadline_at="2026-06-18T11:00:00Z")

    assert store.get_away_session(session_id)["deadline_at"] == "2026-06-18T11:00:00Z"
    assert store.get_window(window_id)["deadline_at"] == "2026-06-18T11:00:00Z"
    assert store.get_runtime_lock("away-window:ou_test")["expires_at"] == "2026-06-18T11:00:00Z"


def test_guarded_create_allows_only_one_active_window_per_recipient(tmp_path):
    store = StateStore(tmp_path / "state.sqlite")
    session_id = store.create_away_session(
        project="Codex Away Mode",
        cwd="/workspace/project",
        task="implement state",
        started_at="2026-06-18T10:00:00Z",
    )

    def create(index):
        return store.create_away_window_guarded(
            recipient_id="ou_test",
            session_id=session_id,
            card_message_id=f"om_card_{index}",
            created_at="2026-06-18T10:00:00Z",
            deadline_at="2026-06-18T10:30:00Z",
            owner=f"worker-{index}",
            lock_expires_at="2026-06-18T10:00:05Z",
            now="2026-06-18T10:00:00Z",
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(create, [1, 2]))

    created = [result for result in results if result is not None]
    assert len(created) == 1
    assert store.active_window_count("ou_test") == 1


def test_guarded_create_ignores_expired_active_window_for_same_recipient(tmp_path):
    store = StateStore(tmp_path / "state.sqlite")
    first_session_id = store.create_away_session(
        project="Codex Away Mode",
        cwd="/workspace/project",
        task="old wait",
        started_at="2026-06-18T09:00:00Z",
        deadline_at="2026-06-18T09:30:00Z",
    )
    first_window_id = store.create_away_window_guarded(
        recipient_id="ou_test",
        session_id=first_session_id,
        card_message_id="om_card_1",
        created_at="2026-06-18T09:00:00Z",
        deadline_at="2026-06-18T09:30:00Z",
        owner=first_session_id,
        lock_expires_at="2026-06-18T09:30:00Z",
        now="2026-06-18T09:00:00Z",
    )
    assert first_window_id is not None

    second_session_id = store.create_away_session(
        project="Codex Away Mode",
        cwd="/workspace/project",
        task="new wait",
        started_at="2026-06-18T10:00:00Z",
        deadline_at="2026-06-18T10:30:00Z",
    )
    second_window_id = store.create_away_window_guarded(
        recipient_id="ou_test",
        session_id=second_session_id,
        card_message_id="om_card_2",
        created_at="2026-06-18T10:00:00Z",
        deadline_at="2026-06-18T10:30:00Z",
        owner=second_session_id,
        lock_expires_at="2026-06-18T10:30:00Z",
        now="2026-06-18T10:00:00Z",
    )

    assert second_window_id is not None


def test_closing_guarded_window_releases_recipient_lock(tmp_path):
    store = StateStore(tmp_path / "state.sqlite")
    first_session_id = store.create_away_session(
        project="Codex Away Mode",
        cwd="/workspace/project",
        task="first wait",
        started_at="2026-06-18T10:00:00Z",
    )
    first_window_id = store.create_away_window_guarded(
        recipient_id="ou_test",
        session_id=first_session_id,
        card_message_id="om_card_1",
        created_at="2026-06-18T10:00:00Z",
        deadline_at="2026-06-18T11:00:00Z",
        owner=first_session_id,
        lock_expires_at="2026-06-18T11:00:00Z",
        now="2026-06-18T10:00:00Z",
    )
    assert first_window_id is not None

    store.close_window(
        first_window_id,
        status="closed",
        reason="user_requested",
        closed_at="2026-06-18T10:05:00Z",
    )

    second_session_id = store.create_away_session(
        project="Codex Away Mode",
        cwd="/workspace/project",
        task="second wait",
        started_at="2026-06-18T10:06:00Z",
    )
    second_window_id = store.create_away_window_guarded(
        recipient_id="ou_test",
        session_id=second_session_id,
        card_message_id="om_card_2",
        created_at="2026-06-18T10:06:00Z",
        deadline_at="2026-06-18T10:36:00Z",
        owner=second_session_id,
        lock_expires_at="2026-06-18T10:36:00Z",
        now="2026-06-18T10:06:00Z",
    )

    assert second_window_id is not None


def test_install_state_round_trips_status_and_next_step(tmp_path):
    store = StateStore(tmp_path / "state.sqlite")

    store.update_install_status(
        status="feishu_authorization_pending",
        failed_code="feishu_user_id_missing",
        waiting_for="feishu_authorization",
        next_step="Open the authorization URL.",
    )

    status = store.install_status()
    assert status["status"] == "feishu_authorization_pending"
    assert status["failed_code"] == "feishu_user_id_missing"
    assert status["waiting_for"] == "feishu_authorization"
    assert status["next_step"] == "Open the authorization URL."
    assert status["updated_at"]


def test_close_orphan_away_sessions_without_window_or_deadline(tmp_path):
    store = StateStore(tmp_path / "state.sqlite")
    orphan_id = store.create_away_session(
        project="Orphan",
        cwd="/workspace/orphan",
        task="lost",
        started_at="2026-06-18T10:00:00Z",
    )
    deadline_only_id = store.create_away_session(
        project="Deadline",
        cwd="/workspace/deadline",
        task="has deadline",
        started_at="2026-06-18T10:00:00Z",
        deadline_at="2026-06-18T11:00:00Z",
    )
    window_session_id = store.create_away_session(
        project="Window",
        cwd="/workspace/window",
        task="has window",
        started_at="2026-06-18T10:00:00Z",
        deadline_at="2026-06-18T11:00:00Z",
    )
    store.create_away_window(
        session_id=window_session_id,
        recipient_id="ou_test",
        card_message_id="om_card",
        created_at="2026-06-18T10:00:00Z",
        deadline_at="2026-06-18T11:00:00Z",
    )

    closed = store.close_orphan_away_sessions(
        closed_at="2026-06-18T10:05:00Z",
        reason="orphan_without_window_repaired",
    )

    assert [item["session_id"] for item in closed] == [orphan_id]
    assert store.get_away_session(orphan_id)["status"] == "closed"
    assert store.get_away_session(orphan_id)["close_reason"] == "orphan_without_window_repaired"
    assert store.get_away_session(deadline_only_id)["status"] == "active"
    assert store.get_away_session(window_session_id)["status"] == "active"


def test_cleanup_closes_stale_sessions_without_live_waiter(tmp_path):
    store = StateStore(tmp_path / "state.sqlite")
    session_id = store.create_away_session(
        project="Stale",
        cwd="/workspace/stale",
        task="wait",
        started_at="2026-06-18T09:00:00Z",
        deadline_at="2026-06-18T09:30:00Z",
    )
    window_id = store.create_away_window_guarded(
        recipient_id="oc_secret_chat",
        session_id=session_id,
        card_message_id="om_stale_card",
        created_at="2026-06-18T09:00:00Z",
        deadline_at="2026-06-18T09:30:00Z",
        owner=session_id,
        lock_expires_at="2026-06-18T09:30:00Z",
        now="2026-06-18T09:00:00Z",
    )
    assert window_id is not None
    assert store.renew_waiter_lease(
        session_id,
        owner="waiter",
        now="2026-06-18T09:00:00Z",
        expires_at="2026-06-18T09:01:00Z",
    )

    result = store.cleanup_stale_away_sessions(
        now="2026-06-18T10:00:00+00:00",
        dry_run=False,
    )

    assert result["closed_count"] == 1
    assert result["skipped_waiter_alive_count"] == 0
    assert result["closed"] == [
        {
            "session_id": session_id,
            "cwd": "/workspace/stale",
            "deadline_at": "2026-06-18T09:30:00Z",
            "close_reason": "manual_cleanup_timeout",
        }
    ]
    assert store.get_away_session(session_id)["status"] == "timed_out"
    assert store.get_away_session(session_id)["close_reason"] == "manual_cleanup_timeout"
    assert store.get_window(window_id)["status"] == "timed_out"
    assert store.get_window(window_id)["close_reason"] == "manual_cleanup_timeout"
    assert store.get_runtime_lock("away-window:oc_secret_chat") is None
    assert store.get_runtime_lock(f"away-waiter:{session_id}") is None


def test_cleanup_skips_future_deadline_and_live_waiter(tmp_path):
    store = StateStore(tmp_path / "state.sqlite")
    future_session_id = store.create_away_session(
        project="Future",
        cwd="/workspace/future",
        task="wait",
        started_at="2026-06-18T09:00:00Z",
        deadline_at="2026-06-18T11:00:00Z",
    )
    future_window_id = store.create_away_window_guarded(
        recipient_id="oc_future_chat",
        session_id=future_session_id,
        card_message_id="om_future_card",
        created_at="2026-06-18T09:00:00Z",
        deadline_at="2026-06-18T11:00:00Z",
        owner=future_session_id,
        lock_expires_at="2026-06-18T11:00:00Z",
        now="2026-06-18T09:00:00Z",
    )
    live_session_id = store.create_away_session(
        project="Live",
        cwd="/workspace/live",
        task="wait",
        started_at="2026-06-18T09:00:00Z",
        deadline_at="2026-06-18T09:30:00Z",
    )
    live_window_id = store.create_away_window_guarded(
        recipient_id="oc_live_chat",
        session_id=live_session_id,
        card_message_id="om_live_card",
        created_at="2026-06-18T09:00:00Z",
        deadline_at="2026-06-18T09:30:00Z",
        owner=live_session_id,
        lock_expires_at="2026-06-18T09:30:00Z",
        now="2026-06-18T09:00:00Z",
    )
    assert future_window_id is not None
    assert live_window_id is not None
    assert store.renew_waiter_lease(
        live_session_id,
        owner="waiter",
        now="2026-06-18T09:29:30Z",
        expires_at="2026-06-18T10:00:30Z",
    )

    result = store.cleanup_stale_away_sessions(
        now="2026-06-18T10:00:00+00:00",
        dry_run=False,
    )

    assert result["closed_count"] == 0
    assert result["skipped_waiter_alive_count"] == 1
    assert store.get_away_session(future_session_id)["status"] == "active"
    assert store.get_window(future_window_id)["status"] == "waiting"
    assert store.get_away_session(live_session_id)["status"] == "active"
    assert store.get_window(live_window_id)["status"] == "waiting"
    assert store.get_runtime_lock("away-window:oc_future_chat") is not None
    assert store.get_runtime_lock("away-window:oc_live_chat") is not None
    assert store.get_runtime_lock(f"away-waiter:{live_session_id}") is not None


def test_prompt_marker_is_keyed_by_cwd_hash(tmp_path):
    store = StateStore(tmp_path / "state.sqlite")

    marker_key = store.mark_prompt_marker(
        cwd="/workspace/demo",
        marked_at="2026-06-18T10:00:00+00:00",
        expires_at="2026-06-18T10:05:00+00:00",
    )

    assert marker_key != "/workspace/demo"
    assert store.get_prompt_marker("/workspace/demo") == {
        "cwd_hash": marker_key,
        "marked_at": "2026-06-18T10:00:00+00:00",
        "expires_at": "2026-06-18T10:05:00+00:00",
    }
    assert store.get_prompt_marker("/workspace/other") is None


def test_staged_summary_is_keyed_by_cwd_hash_and_consumable(tmp_path):
    store = StateStore(tmp_path / "state.sqlite")
    markdown = "**项目**\nDemo\n\n**完成**\nDone\n"

    summary_key = store.stage_summary(
        cwd="/workspace/demo",
        summary_markdown=markdown,
        staged_at="2026-06-18T10:00:00+00:00",
        expires_at="2026-06-18T10:05:00+00:00",
    )

    assert summary_key != "/workspace/demo"
    assert store.get_staged_summary("/workspace/demo") == {
        "cwd_hash": summary_key,
        "summary_markdown": markdown,
        "staged_at": "2026-06-18T10:00:00+00:00",
        "expires_at": "2026-06-18T10:05:00+00:00",
    }
    assert store.get_staged_summary("/workspace/other") is None

    store.delete_staged_summary("/workspace/demo")

    assert store.get_staged_summary("/workspace/demo") is None


def test_no_dao_accepts_or_stores_ordinary_prompt_reply_text(tmp_path):
    for name, method in inspect.getmembers(StateStore, inspect.isfunction):
        if not name.startswith("_"):
            assert "reply_text" not in inspect.signature(method).parameters
            assert "message_text" not in inspect.signature(method).parameters

    store = StateStore(tmp_path / "state.sqlite")
    store.mark_processed(
        message_id="om_msg_1",
        message_kind="ordinary_dm",
        window_id=None,
        action="hint_sent",
        message_text_hash="sha256:ordinary",
    )

    with sqlite3.connect(tmp_path / "state.sqlite") as conn:
        columns = {
            row[1]
            for table in ("processed_messages", "ordinary_dm_hints", "away_windows")
            for row in conn.execute(f"PRAGMA table_info({table})")
        }

    assert "reply_text" not in columns
    assert "message_text" not in columns
    assert "message_text_hash" in columns
