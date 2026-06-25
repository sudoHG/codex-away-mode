from __future__ import annotations

import json
import sqlite3
import shutil
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from codex_away_mode.away import AwayWaiter
from codex_away_mode.config import AppConfig, load_config, save_config
from codex_away_mode.lark import LarkMessage, SendResult
from codex_away_mode.state import StateStore


START = datetime(2026, 6, 18, 10, 0, tzinfo=timezone.utc)


class FakeClock:
    def __init__(self, current=START):
        self.current = current
        self.sleeps = []

    def now(self):
        return self.current

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.current += timedelta(seconds=seconds)


class FakeLark:
    def __init__(self, messages_by_poll=None, *, reaction_error=None, next_card_index=1):
        self.messages_by_poll = list(messages_by_poll or [])
        self.reaction_error = reaction_error
        self.sent_cards = []
        self.sent_texts = []
        self.reactions = []
        self.list_calls = []
        self.next_message_index = 1
        self.next_card_index = next_card_index

    def send_interactive_card(self, *, card, user_id=None, chat_id=None):
        self.sent_cards.append({"card": card, "user_id": user_id, "chat_id": chat_id})
        message_id = "om_card" if self.next_card_index == 1 else f"om_card_{self.next_card_index}"
        self.next_card_index += 1
        return SendResult(message_id=message_id, chat_id=chat_id or "oc_test_chat")

    def list_messages(self, *, chat_id):
        self.list_calls.append(chat_id)
        if self.messages_by_poll:
            return self.messages_by_poll.pop(0)
        return []

    def add_reaction(self, *, message_id, emoji_type="Get"):
        self.reactions.append({"message_id": message_id, "emoji_type": emoji_type})
        if self.reaction_error:
            raise self.reaction_error

    def send_text(self, *, text, user_id=None, chat_id=None):
        self.sent_texts.append({"text": text, "user_id": user_id, "chat_id": chat_id})
        result = SendResult(message_id=f"om_text_{self.next_message_index}", chat_id=chat_id or "oc_test_chat")
        self.next_message_index += 1
        return result


def _message(
    message_id,
    text,
    *,
    reply_to="om_card",
    sender_type="user",
    create_time="1710000000000",
):
    return LarkMessage(
        message_id=message_id,
        reply_to=reply_to,
        msg_type="text",
        content_text=text,
        sender_type=sender_type,
        create_time=create_time,
    )


def _context(**overrides):
    context = {
        "project": "Demo",
        "cwd": "/workspace/demo",
        "task": "实现 Away Mode",
        "completed": "已完成",
        "changed": "away.py",
        "verification": "pytest",
        "unverified": "无",
        "need_user": "请确认",
        "wait_minutes": 30,
    }
    context.update(overrides)
    return context


def _resume_context(first: dict, **overrides):
    context = {
        "resume": first["away_session_id"],
        "resume_token": first["resume_token"],
        "completed": "done",
        "changed": "无",
        "verification": "未运行",
        "unverified": "无",
        "need_user": "继续",
    }
    context.update(overrides)
    return context


def _config(**overrides):
    return replace(
        AppConfig(
            feishu_user_id="ou_test_user",
            feishu_chat_id="oc_test_chat",
            route_key_verified=True,
            multi_window_enabled=True,
            poll_interval_seconds=5,
        ),
        **overrides,
    )


def _waiter(
    tmp_path,
    *,
    lark=None,
    clock=None,
    config=None,
    config_path=None,
    install_store=None,
):
    store = StateStore(tmp_path / "state.sqlite")
    return AwayWaiter(
        lark=lark or FakeLark(),
        store=store,
        clock=clock or FakeClock(),
        config=config or _config(),
        config_path=config_path,
        install_store=install_store,
    ), store


def _processed_rows(store):
    with sqlite3.connect(store.path) as conn:
        conn.row_factory = sqlite3.Row
        return [dict(row) for row in conn.execute("SELECT * FROM processed_messages")]


def _write_session_index(codex_home, thread_id="thread_1", thread_name="建立 Skill-Create 基线"):
    codex_home.mkdir(parents=True)
    (codex_home / "session_index.jsonl").write_text(
        json.dumps({"id": thread_id, "thread_name": thread_name}, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def test_initial_away_card_title_uses_programmatic_context_not_project_arg(tmp_path, monkeypatch):
    codex_home = tmp_path / "codex-home"
    _write_session_index(codex_home)
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    monkeypatch.delenv("CODEX_THREAD_ID", raising=False)
    lark = FakeLark([[_message("om_reply", "继续")]])
    waiter, _store = _waiter(tmp_path, lark=lark)

    result = waiter.wait(
        _context(
            project="Agent 传错的项目名",
            cwd="/Users/example/Codex/Skill-Create",
            codex_session_id="thread_1",
        )
    )

    assert result["status"] == "reply"
    header = lark.sent_cards[0]["card"]["header"]
    assert header["title"]["content"] == "建立 Skill-Create 基线 / Skill-Create"
    assert header["subtitle"]["content"] == "Codex Away Mode：已进入 Away Mode，正在等待你的回复"
    assert header["text_tag_list"][0]["text"]["content"] == "等待中"
    assert "Agent 传错的项目名" not in str(header)


def test_timeout_away_card_title_uses_programmatic_context(tmp_path, monkeypatch):
    codex_home = tmp_path / "codex-home"
    _write_session_index(codex_home)
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    monkeypatch.delenv("CODEX_THREAD_ID", raising=False)
    clock = FakeClock()
    lark = FakeLark([[], [], []])
    waiter, _store = _waiter(
        tmp_path,
        clock=clock,
        lark=lark,
        config=_config(poll_interval_seconds=60),
    )

    result = waiter.wait(
        _context(
            project="Agent 传错的项目名",
            cwd="/Users/example/Codex/Skill-Create",
            codex_session_id="thread_1",
            wait_minutes=2,
        )
    )

    assert result["status"] == "timeout"
    header = lark.sent_cards[-1]["card"]["header"]
    assert header["title"]["content"] == "回复窗口已超时关闭 - Codex Away Mode"
    assert header["subtitle"]["content"] == "建立 Skill-Create 基线 / Skill-Create"
    assert header["text_tag_list"][0]["text"]["content"] == "已超时"
    assert "Agent 传错的项目名" not in str(header)


def test_prompt_delivery_pauses_window_and_returns_resume_contract(tmp_path):
    lark = FakeLark([[_message("om_reply", "继续执行下一步")]])
    waiter, store = _waiter(tmp_path, lark=lark)

    result = waiter.wait(_context())

    assert result["status"] == "reply"
    assert result["reply_text"] == "继续执行下一步"
    assert result["keep_waiting"] is True
    assert result["away_session_id"].startswith("sess_")
    assert result["window_id"].startswith("win_")
    assert result["deadline_at"]
    assert result["resume_token"].startswith("rt_")
    assert result["resume_token"] != result["away_session_id"]
    assert store.get_resume_token_hash(result["away_session_id"]).startswith("sha256:")
    assert result["resume_token"] not in str(store.list_diagnostic_events())
    window = store.find_window_by_card_message_id("om_card")
    assert window["status"] == "waiting_paused"
    assert window["close_reason"] is None
    session = store.get_away_session(result["away_session_id"])
    assert session["status"] == "waiting_paused"
    assert session["last_delivered_message_id"] == "om_reply"
    assert lark.reactions == [{"message_id": "om_reply", "emoji_type": "Get"}]
    rows = _processed_rows(store)
    assert rows[0]["message_id"] == "om_reply"
    assert rows[0]["action"] == "deliver_prompt"
    assert rows[0]["message_text_hash"].startswith("sha256:")
    assert "继续执行下一步" not in str(rows)


def test_reaction_fallback_sends_text_confirmation_and_still_returns_reply(tmp_path):
    lark = FakeLark([[_message("om_reply", "继续")]], reaction_error=RuntimeError("no reaction"))
    waiter, _store = _waiter(tmp_path, lark=lark)

    result = waiter.wait(_context())

    assert result["status"] == "reply"
    assert result["reply_text"] == "继续"
    assert any("收到" in item["text"] for item in lark.sent_texts)


def test_resume_sends_progress_card_reuses_window_and_delivers_next_reply(tmp_path):
    first_lark = FakeLark([[_message("om_reply_1", "第一条")]])
    waiter, store = _waiter(tmp_path, lark=first_lark)
    first = waiter.wait(_context())

    second_reply = _message("om_reply_2", "第二条", reply_to="om_card_2")
    second_lark = FakeLark([[second_reply]], next_card_index=2)
    resume_waiter = AwayWaiter(
        lark=second_lark,
        store=store,
        clock=FakeClock(),
        config=_config(),
    )

    second = resume_waiter.wait(
        _resume_context(
            first,
            completed="第一条已处理",
            changed="away.py",
            verification="pytest 未运行",
            unverified="live 未测",
            need_user="请继续确认",
        )
    )

    assert second["status"] == "reply"
    assert second["reply_text"] == "第二条"
    assert second["resume_token"].startswith("rt_")
    assert second["resume_token"] != first["resume_token"]
    assert len(second_lark.sent_cards) == 1
    assert "第一条已处理" in str(second_lark.sent_cards[0]["card"])
    with sqlite3.connect(store.path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM away_windows").fetchone()[0] == 1
    assert store.get_window(first["window_id"])["card_message_id"] == "om_card_2"


def test_resume_token_survives_progress_card_send_failure_for_retry(tmp_path):
    first_lark = FakeLark([[_message("om_reply_1", "第一条")]])
    waiter, store = _waiter(tmp_path, lark=first_lark)
    first = waiter.wait(_context())
    original_token_hash = store.get_resume_token_hash(first["away_session_id"])

    class FailingProgressCardLark(FakeLark):
        def send_interactive_card(self, *, card, user_id=None, chat_id=None):
            raise RuntimeError("HTTP 429")

    failing_waiter = AwayWaiter(
        lark=FailingProgressCardLark([[]], next_card_index=2),
        store=store,
        clock=FakeClock(),
        config=_config(),
    )

    with pytest.raises(RuntimeError, match="HTTP 429"):
        failing_waiter.wait(_resume_context(first))

    assert store.get_resume_token_hash(first["away_session_id"]) == original_token_hash

    second_reply = _message("om_reply_2", "重试后继续", reply_to="om_card_2")
    retry_waiter = AwayWaiter(
        lark=FakeLark([[second_reply]], next_card_index=2),
        store=store,
        clock=FakeClock(),
        config=_config(),
    )

    result = retry_waiter.wait(_resume_context(first))

    assert result["status"] == "reply"
    assert result["reply_text"] == "重试后继续"


def test_resume_requires_resume_token(tmp_path):
    first_lark = FakeLark([[_message("om_reply_1", "第一条")]])
    waiter, store = _waiter(tmp_path, lark=first_lark)
    first = waiter.wait(_context())
    resume_waiter = AwayWaiter(
        lark=FakeLark(),
        store=store,
        clock=FakeClock(),
        config=_config(),
    )

    result = resume_waiter.wait(
        {
            "resume": first["away_session_id"],
            "completed": "done",
            "changed": "无",
            "verification": "未运行",
            "unverified": "无",
            "need_user": "继续",
        }
    )

    assert result["status"] == "error"
    assert result["error_code"] == "resume_token_required"
    assert result["keep_waiting"] is False


def test_resume_rejects_wrong_resume_token(tmp_path):
    first_lark = FakeLark([[_message("om_reply_1", "第一条")]])
    waiter, store = _waiter(tmp_path, lark=first_lark)
    first = waiter.wait(_context())
    resume_waiter = AwayWaiter(
        lark=FakeLark(),
        store=store,
        clock=FakeClock(),
        config=_config(),
    )

    result = resume_waiter.wait(_resume_context(first, resume_token="rt_wrong"))

    assert result["status"] == "error"
    assert result["error_code"] == "resume_token_invalid"
    assert result["keep_waiting"] is False


def test_resume_delivers_reply_sent_to_current_card_before_rotation(tmp_path):
    first_lark = FakeLark([[_message("om_reply_1", "第一条")]])
    waiter, store = _waiter(tmp_path, lark=first_lark)
    first = waiter.wait(_context())

    queued = _message(
        "om_queued",
        "处理期间发出的第二条",
        reply_to="om_card",
        create_time="2026-06-20T09:10:00Z",
    )
    second_lark = FakeLark([[queued]])
    resume_waiter = AwayWaiter(
        lark=second_lark,
        store=store,
        clock=FakeClock(),
        config=_config(),
    )

    result = resume_waiter.wait(_resume_context(first))

    assert result["status"] == "reply"
    assert result["reply_text"] == "处理期间发出的第二条"
    assert second_lark.sent_cards == []


def test_resume_drains_backlog_commands_before_prompt(tmp_path):
    first_lark = FakeLark([[_message("om_reply_1", "第一条")]])
    waiter, store = _waiter(tmp_path, lark=first_lark)
    first = waiter.wait(_context())

    queued_extend = _message(
        "om_extend",
        "/延长等待",
        reply_to="om_card",
        create_time="2026-06-20T09:10:00Z",
    )
    queued_prompt = _message(
        "om_prompt",
        "继续执行",
        reply_to="om_card",
        create_time="2026-06-20T09:11:00Z",
    )
    second_lark = FakeLark([[queued_extend, queued_prompt]])
    resume_waiter = AwayWaiter(
        lark=second_lark,
        store=store,
        clock=FakeClock(),
        config=_config(),
    )

    result = resume_waiter.wait(_resume_context(first))

    assert result["status"] == "reply"
    assert result["reply_text"] == "继续执行"
    assert any("延长" in item["text"] for item in second_lark.sent_texts)
    assert second_lark.sent_cards == []


def test_resume_backlog_extend_updates_deadline_before_waiting_again(tmp_path):
    first_lark = FakeLark([[_message("om_reply_1", "第一条")]])
    waiter, store = _waiter(tmp_path, lark=first_lark)
    first = waiter.wait(_context())

    queued_extend = _message(
        "om_extend",
        "/延长等待",
        reply_to="om_card",
        create_time="2026-06-20T09:10:00Z",
    )
    clock = FakeClock()
    second_lark = FakeLark([[queued_extend], [], []], next_card_index=2)
    resume_waiter = AwayWaiter(
        lark=second_lark,
        store=store,
        clock=clock,
        config=_config(poll_interval_seconds=1800),
    )

    result = resume_waiter.wait(_resume_context(first))

    assert result["status"] == "timeout"
    assert store.get_away_session(first["away_session_id"])["deadline_at"].startswith("2026-06-18T11:00:00")
    assert store.get_window(first["window_id"])["deadline_at"].startswith("2026-06-18T11:00:00")
    assert clock.sleeps == [1800, 1800]


def test_resume_extend_minutes_updates_deadline_without_direct_state_access(tmp_path):
    first_lark = FakeLark([[_message("om_reply_1", "第一条")]])
    waiter, store = _waiter(tmp_path, lark=first_lark)
    first = waiter.wait(_context())

    second_reply = _message("om_reply_2", "继续执行", reply_to="om_card_2")
    second_lark = FakeLark([[], [second_reply]], next_card_index=2)
    resume_waiter = AwayWaiter(
        lark=second_lark,
        store=store,
        clock=FakeClock(),
        config=_config(),
    )

    result = resume_waiter.wait(_resume_context(first, extend_minutes=180))

    assert result["status"] == "reply"
    assert result["reply_text"] == "继续执行"
    assert store.get_away_session(first["away_session_id"])["deadline_at"].startswith("2026-06-18T13:30:00")
    window = store.get_window(first["window_id"])
    assert window["deadline_at"].startswith("2026-06-18T13:30:00")
    assert window["extend_count"] == 1
    assert any("延长" in item["text"] for item in second_lark.sent_texts)
    assert second_lark.sent_cards
    assert "21:30" in str(second_lark.sent_cards[0]["card"])


def test_reply_after_card_retired_is_not_delivered(tmp_path):
    first_lark = FakeLark([[_message("om_reply_1", "第一条")]])
    waiter, store = _waiter(tmp_path, lark=first_lark)
    first = waiter.wait(_context())

    old_late_reply = _message(
        "om_old_late",
        "旧卡迟到回复",
        reply_to="om_card",
        create_time="2026-06-20T09:20:00Z",
    )
    new_reply = _message(
        "om_new",
        "新卡回复",
        reply_to="om_card_2",
        create_time="2026-06-20T09:21:00Z",
    )
    second_lark = FakeLark([[], [old_late_reply], [new_reply]], next_card_index=2)
    resume_waiter = AwayWaiter(
        lark=second_lark,
        store=store,
        clock=FakeClock(),
        config=_config(),
    )

    result = resume_waiter.wait(_resume_context(first, completed="done again"))

    assert result["reply_text"] == "新卡回复"
    assert any("旧卡" in item["text"] or "最新" in item["text"] for item in second_lark.sent_texts)


def test_five_minute_reminder_sent_once_and_marked(tmp_path):
    clock = FakeClock()
    lark = FakeLark([[], [_message("om_reply", "继续")]])
    waiter, store = _waiter(tmp_path, clock=clock, lark=lark, config=_config(poll_interval_seconds=1500))

    result = waiter.wait(_context())

    assert result["status"] == "reply"
    assert len(lark.sent_cards) == 2
    window = store.find_window_by_card_message_id("om_card")
    assert window["reminder_sent_at"] is not None


def test_reply_to_pre_timeout_reminder_card_is_delivered(tmp_path):
    clock = FakeClock()
    reminder_reply = _message(
        "om_reply_to_reminder",
        "这是哪个线程？",
        reply_to="om_card_2",
        create_time="2026-06-18T10:01:02Z",
    )
    lark = FakeLark([[], [reminder_reply]])
    waiter, store = _waiter(
        tmp_path,
        clock=clock,
        lark=lark,
        config=_config(poll_interval_seconds=60),
    )

    result = waiter.wait(_context(wait_minutes=6))

    assert result["status"] == "reply"
    assert result["reply_text"] == "这是哪个线程？"
    assert len(lark.sent_cards) == 2
    reminder_card = store.find_card("om_card_2")
    assert reminder_card["card_kind"] == "pre_timeout_reminder"
    assert reminder_card["status"] == "active"
    rows = _processed_rows(store)
    assert any(row["message_id"] == "om_reply_to_reminder" and row["action"] == "deliver_prompt" for row in rows)


def test_short_wait_skips_pre_timeout_reminder(tmp_path):
    clock = FakeClock()
    lark = FakeLark([[], [_message("om_reply", "继续")]])
    waiter, store = _waiter(tmp_path, clock=clock, lark=lark, config=_config(poll_interval_seconds=120))

    result = waiter.wait(_context(wait_minutes=4))

    assert result["status"] == "reply"
    assert len(lark.sent_cards) == 1
    window = store.find_window_by_card_message_id("om_card")
    assert window["reminder_sent_at"] is None


def test_short_wait_extend_does_not_send_past_due_reminder_immediately(tmp_path):
    lark = FakeLark([[_message("om_extend", "/延长等待")], [_message("om_reply", "继续")]])
    waiter, store = _waiter(tmp_path, lark=lark)

    result = waiter.wait(_context(wait_minutes=4))

    assert result["status"] == "reply"
    assert len(lark.sent_cards) == 1
    window = store.find_window_by_card_message_id("om_card")
    assert window["extend_count"] == 1
    assert window["reminder_sent_at"] is None


def test_extend_command_extends_deadline_increments_count_and_sends_feedback(tmp_path):
    lark = FakeLark([[_message("om_extend", "/延长等待")], [_message("om_reply", "继续")]])
    waiter, store = _waiter(tmp_path, lark=lark)

    result = waiter.wait(_context())

    assert result["status"] == "reply"
    window = store.find_window_by_card_message_id("om_card")
    assert window["extend_count"] == 1
    assert window["deadline_at"].startswith("2026-06-18T11:00:00")
    assert any("/延长等待" in item["text"] for item in lark.sent_texts)


def test_repeated_extend_history_does_not_move_in_memory_deadline_again(tmp_path):
    repeated_extend = _message("om_extend", "/延长等待")
    lark = FakeLark(
        [
            [repeated_extend],
            [repeated_extend],
            [_message("om_status", "/状态")],
            [_message("om_reply", "继续")],
        ]
    )
    waiter, store = _waiter(tmp_path, lark=lark)

    result = waiter.wait(_context())

    assert result["status"] == "reply"
    window = store.find_window_by_card_message_id("om_card")
    assert window["extend_count"] == 1
    assert window["deadline_at"].startswith("2026-06-18T11:00:00")
    status_feedback = [item["text"] for item in lark.sent_texts if "/状态" in item["text"]]
    assert len([item for item in lark.sent_texts if "/延长等待" in item["text"]]) == 1
    assert status_feedback
    assert "06-18" in status_feedback[0]
    assert "T11:00:00" not in status_feedback[0]
    assert "+00:00" not in status_feedback[0]
    assert "2026-06-18T11:30:00" not in status_feedback[0]


def test_status_command_sends_feedback_and_keeps_waiting(tmp_path):
    lark = FakeLark([[_message("om_status", "/状态")], [_message("om_reply", "继续")]])
    waiter, _store = _waiter(tmp_path, lark=lark)

    result = waiter.wait(_context())

    assert result["status"] == "reply"
    assert result["reply_text"] == "继续"
    assert any("/状态" in item["text"] for item in lark.sent_texts)


def test_end_command_sends_feedback_closes_window_and_returns_ended_without_reply_text(tmp_path):
    lark = FakeLark([[_message("om_end", "/结束等待")]])
    waiter, store = _waiter(tmp_path, lark=lark)

    result = waiter.wait(_context())

    assert result["status"] == "ended"
    assert result["keep_waiting"] is False
    assert "reply_text" not in result
    window = store.find_window_by_card_message_id("om_card")
    assert window["status"] == "closed"
    assert window["close_reason"] == "user_requested"
    assert any("/结束等待" in item["text"] for item in lark.sent_texts)
    assert len(lark.sent_cards) == 2
    end_card_text = str(lark.sent_cards[-1]["card"])
    assert "回复窗口已结束 - Codex Away Mode" in end_card_text
    assert "已结束" in end_card_text
    assert "Codex 会继续完成本轮收尾" in end_card_text


def test_unknown_slash_command_feedback_marks_processed_and_does_not_deliver(tmp_path):
    lark = FakeLark([[_message("om_unknown", "/重新开始")], [_message("om_reply", "继续")]])
    waiter, store = _waiter(tmp_path, lark=lark)

    result = waiter.wait(_context())

    assert result["status"] == "reply"
    assert result["reply_text"] == "继续"
    assert any("未知命令" in item["text"] for item in lark.sent_texts)
    rows = _processed_rows(store)
    assert any(row["message_id"] == "om_unknown" and row["action"] == "unknown_command" for row in rows)


def test_timeout_sends_timeout_card_closes_window_and_returns_timeout(tmp_path):
    clock = FakeClock()
    lark = FakeLark([[], [], []])
    waiter, store = _waiter(tmp_path, clock=clock, lark=lark, config=_config(poll_interval_seconds=60))

    result = waiter.wait(_context(wait_minutes=2))

    assert result["status"] == "timeout"
    assert result["keep_waiting"] is False
    assert len(lark.sent_cards) == 2
    window = store.find_window_by_card_message_id("om_card")
    assert window["status"] == "timed_out"
    assert window["close_reason"] == "timeout"


def test_ordinary_dm_hint_is_idempotent_for_same_message_and_never_prompt(tmp_path):
    ordinary = _message("om_dm", "普通私聊内容", reply_to=None)
    lark = FakeLark([[ordinary], [ordinary], [_message("om_reply", "卡片回复")]])
    waiter, store = _waiter(tmp_path, lark=lark)

    result = waiter.wait(_context())

    assert result["status"] == "reply"
    assert result["reply_text"] == "卡片回复"
    hints = [item for item in lark.sent_texts if "普通私聊" in item["text"]]
    assert len(hints) == 1
    rows = _processed_rows(store)
    assert any(row["message_id"] == "om_dm" and row["action"] == "ordinary_dm_hint" for row in rows)
    assert "普通私聊内容" not in str(rows)


def test_ordinary_dm_hint_is_sent_for_each_distinct_private_message(tmp_path):
    ordinary_1 = _message("om_dm_1", "普通私聊 1", reply_to=None)
    ordinary_2 = _message("om_dm_2", "普通私聊 2", reply_to=None)
    ordinary_3 = _message("om_dm_3", "普通私聊 3", reply_to=None)
    lark = FakeLark([[ordinary_1, ordinary_2, ordinary_3], [_message("om_reply", "卡片回复")]])
    waiter, store = _waiter(tmp_path, lark=lark)

    result = waiter.wait(_context())

    assert result["status"] == "reply"
    assert result["reply_text"] == "卡片回复"
    hints = [item for item in lark.sent_texts if "普通私聊" in item["text"]]
    assert len(hints) == 3
    rows = _processed_rows(store)
    assert any(row["message_id"] == "om_dm_1" and row["action"] == "ordinary_dm_hint" for row in rows)
    assert any(
        row["message_id"] == "om_dm_2" and row["action"] == "ordinary_dm_hint"
        for row in rows
    )
    assert any(
        row["message_id"] == "om_dm_3" and row["action"] == "ordinary_dm_hint"
        for row in rows
    )
    assert "普通私聊 2" not in str(rows)
    assert "普通私聊 3" not in str(rows)


def test_single_window_fallback_uses_guarded_create_and_rejects_second_active_window(tmp_path):
    clock = FakeClock()
    lark = FakeLark()
    waiter, store = _waiter(
        tmp_path,
        clock=clock,
        lark=lark,
        config=_config(route_key_verified=False, multi_window_enabled=True),
    )
    session_id = store.create_away_session(
        project="Other",
        cwd="/workspace/other",
        task="already waiting",
        started_at=clock.now().isoformat(),
    )
    store.create_away_window_guarded(
        "oc_test_chat",
        session_id=session_id,
        card_message_id="om_existing_card",
        created_at=clock.now().isoformat(),
        deadline_at=(clock.now() + timedelta(minutes=30)).isoformat(),
        owner="existing",
        lock_expires_at=(clock.now() + timedelta(seconds=10)).isoformat(),
        now=clock.now().isoformat(),
    )

    result = waiter.wait(_context())

    assert result["status"] == "error"
    assert result["error_code"] == "active_away_window_exists"
    assert "当前飞书会话里已经有一个 Away Mode 回复窗口在等待" in result["message"]
    for forbidden in ("reply_to", "route key", "single-window", "fallback", "recipient", "window_id"):
        assert forbidden not in result["message"]
        assert forbidden not in result.get("agent_next_step", "")
    assert lark.sent_cards == []
    with sqlite3.connect(store.path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM away_sessions").fetchone()[0] == 1


def test_live_card_reply_can_mark_route_key_verified(tmp_path):
    config_path = tmp_path / "config.toml"
    config = _config(route_key_verified=False, multi_window_enabled=True)
    save_config(config_path, config)
    lark = FakeLark([[_message("om_reply", "继续")]])
    waiter, runtime_store = _waiter(
        tmp_path,
        lark=lark,
        config=config,
        config_path=config_path,
        install_store=StateStore(tmp_path / "install-state.sqlite"),
    )

    result = waiter.wait(_context())

    assert result["status"] == "reply"
    assert load_config(config_path).route_key_verified is True
    route_state = waiter.install_store.get_install_state("route_key")
    assert route_state["status"] == "verified"
    assert route_state["source"] == "live_card_reply"
    assert runtime_store.route_key_state()["status"] == "unknown"


def test_live_marked_route_key_survives_runtime_wipe(tmp_path):
    config_path = tmp_path / "config.toml"
    config = _config(route_key_verified=False, multi_window_enabled=True)
    save_config(config_path, config)
    install_store = StateStore(tmp_path / "install-state.sqlite")
    lark = FakeLark([[_message("om_reply", "继续")]])
    waiter, runtime_store = _waiter(
        tmp_path / "runtime",
        lark=lark,
        config=config,
        config_path=config_path,
        install_store=install_store,
    )

    result = waiter.wait(_context())
    shutil.rmtree(runtime_store.path.parent)

    assert result["status"] == "reply"
    assert install_store.route_key_state()["status"] == "verified"
    assert install_store.route_key_state()["source"] == "live_card_reply"


def test_live_route_key_without_install_store_does_not_write_runtime_metadata(tmp_path):
    config_path = tmp_path / "config.toml"
    config = _config(route_key_verified=False, multi_window_enabled=True)
    save_config(config_path, config)
    lark = FakeLark([[_message("om_reply", "继续")]])
    waiter, runtime_store = _waiter(
        tmp_path,
        lark=lark,
        config=config,
        config_path=config_path,
    )

    result = waiter.wait(_context())

    assert result["status"] == "reply"
    assert load_config(config_path).route_key_verified is True
    assert runtime_store.route_key_state()["status"] == "unknown"


def test_live_route_mismatch_does_not_downgrade_verified(tmp_path):
    config_path = tmp_path / "config.toml"
    config = _config(route_key_verified=True, multi_window_enabled=True)
    save_config(config_path, config)
    mismatch = _message("om_other", "别的卡片", reply_to="om_other_card")
    lark = FakeLark([[mismatch], [_message("om_reply", "继续")]])
    install_store = StateStore(tmp_path / "install-state.sqlite")
    waiter, runtime_store = _waiter(
        tmp_path,
        lark=lark,
        config=config,
        config_path=config_path,
        install_store=install_store,
    )
    install_store.set_route_key_state(
        status="verified",
        source="doctor_route_probe",
        verified_at="2026-06-18T09:00:00Z",
    )

    result = waiter.wait(_context())

    assert result["status"] == "reply"
    assert load_config(config_path).route_key_verified is True
    assert install_store.route_key_state()["status"] == "verified"
    assert runtime_store.route_key_state()["status"] == "unknown"


def test_multi_window_start_allowed_when_route_key_verified(tmp_path):
    clock = FakeClock()
    lark = FakeLark([[_message("om_reply", "继续")]])
    waiter, store = _waiter(
        tmp_path,
        clock=clock,
        lark=lark,
        config=_config(route_key_verified=True, multi_window_enabled=True),
    )
    existing_session_id = store.create_away_session(
        project="Other",
        cwd="/workspace/other",
        task="already waiting",
        started_at=clock.now().isoformat(),
        deadline_at=(clock.now() + timedelta(minutes=30)).isoformat(),
    )
    store.create_away_window(
        session_id=existing_session_id,
        recipient_id="oc_test_chat",
        card_message_id="om_existing_card",
        created_at=clock.now().isoformat(),
        deadline_at=(clock.now() + timedelta(minutes=30)).isoformat(),
    )

    result = waiter.wait(_context())

    assert result["status"] == "reply"
    assert "error_code" not in result
    with sqlite3.connect(store.path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM away_windows").fetchone()[0] == 2


def test_missing_feishu_chat_id_returns_error_before_polling(tmp_path):
    lark = FakeLark()
    waiter, _store = _waiter(tmp_path, lark=lark, config=_config(feishu_chat_id=None))

    result = waiter.wait(_context())

    assert result["status"] == "error"
    assert result["error_code"] == "missing_feishu_chat_id"
    assert "notify test --json" in result["message"]
    assert lark.list_calls == []
    assert lark.sent_cards == []


def test_privacy_does_not_persist_reply_body_only_hash(tmp_path):
    lark = FakeLark([[_message("om_reply", "敏感回复正文")]])
    waiter, store = _waiter(tmp_path, lark=lark)

    waiter.wait(_context())

    with sqlite3.connect(store.path) as conn:
        dump = "\n".join(conn.iterdump())
    assert "敏感回复正文" not in dump
    assert "sha256:" in dump


def test_polling_cadence_uses_configured_sleep_and_no_busy_loop(tmp_path):
    clock = FakeClock()
    lark = FakeLark([[], [_message("om_reply", "继续")]])
    waiter, _store = _waiter(tmp_path, clock=clock, lark=lark, config=_config(poll_interval_seconds=7))

    waiter.wait(_context())

    assert clock.sleeps == [7]
