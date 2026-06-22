import os
import time
from datetime import datetime, timezone

import pytest

from codex_away_mode import cards
from codex_away_mode.cards import (
    away_card,
    command_feedback_text,
    completion_card,
    fallback_completion_card,
    ordinary_dm_hint_text,
    pre_timeout_reminder_card,
    timeout_card,
)


@pytest.fixture(autouse=True)
def restore_timezone():
    original = os.environ.get("TZ")
    yield
    if original is None:
        os.environ.pop("TZ", None)
    else:
        os.environ["TZ"] = original
    if hasattr(time, "tzset"):
        time.tzset()


def set_local_timezone(monkeypatch, name):
    monkeypatch.setenv("TZ", name)
    if hasattr(time, "tzset"):
        time.tzset()


def flatten_text(value):
    if isinstance(value, dict):
        return "\n".join(flatten_text(item) for item in value.values())
    if isinstance(value, list):
        return "\n".join(flatten_text(item) for item in value)
    return str(value)


def note_texts(card):
    texts = []
    for element in card["elements"]:
        if element.get("tag") != "note":
            continue
        texts.extend(item["content"] for item in element.get("elements", []))
    return texts


def test_completion_card_title_includes_project_and_compact_footer(monkeypatch):
    set_local_timezone(monkeypatch, "Asia/Shanghai")
    card = completion_card(
        title="完成通知",
        project="Demo",
        fields={
            "完成": "已完成 Task 4",
            "验证": "pytest passed",
        },
        footer_cwd="/workspace/project",
        footer_mode_text=cards.notification_mode_footer_text("all"),
        now=datetime(2026, 6, 18, 10, 0, tzinfo=timezone.utc),
    )

    text = flatten_text(card)
    notes = note_texts(card)
    assert card["header"]["title"]["content"] == "完成通知 - Demo"
    assert card["header"]["template"] == "blue"
    assert "**工作目录**" not in text
    assert "工作目录：/workspace/project" in text
    assert "/workspace/project" in text
    assert "发送时间：18:00" in text
    assert "2026-" not in "\n".join(notes)
    assert "UTC" not in "\n".join(notes)
    assert notes == [
        "\n".join(
            [
                "发送时间：18:00",
                "工作目录：/workspace/project",
                "当前通知模式：每轮完成后都会通知",
                "通知模式修改方式：告诉Codex「关掉飞书完成通知」或「暂停飞书通知 2 小时」",
            ]
        )
    ]
    footer_lines = notes[0].splitlines()
    assert footer_lines == [
        "发送时间：18:00",
        "工作目录：/workspace/project",
        "当前通知模式：每轮完成后都会通知",
        "通知模式修改方式：告诉Codex「关掉飞书完成通知」或「暂停飞书通知 2 小时」",
    ]
    assert all(line.strip() for line in footer_lines)
    assert "\n\n" not in notes[0]
    assert "codex-away-mode notify" not in text
    assert "config.toml" not in text


def test_fallback_completion_card_uses_cwd_project_title_and_footer_cwd(monkeypatch):
    set_local_timezone(monkeypatch, "Asia/Shanghai")
    card = fallback_completion_card(
        reason="summary missing",
        cwd="/workspace/immichSlides-app",
        now=datetime(2026, 6, 18, 10, 1, tzinfo=timezone.utc),
    )

    text = flatten_text(card)
    assert card["header"]["title"]["content"] == "Codex 回合已停止 - immichSlides-app（无摘要）"
    assert card["header"]["template"] == "blue"
    assert "无摘要" in text
    assert "summary missing" in text
    assert "**工作目录**" not in text
    assert "工作目录：/workspace/immichSlides-app" in text
    assert "发送时间：18:01" in text
    assert "UTC" not in "\n".join(note_texts(card))
    assert "忘了写摘要" in text
    assert "告诉Codex" in text
    assert "codex-away-mode notify" not in text


def test_summary_sections_support_completion_card_without_body_workdir(monkeypatch):
    set_local_timezone(monkeypatch, "Asia/Shanghai")
    sections = cards.summary_sections(
        "**项目**\nDemo\n\n**工作目录**\n/workspace/demo\n\n**完成**\nDone\n\n**验证**\npytest\n"
    )

    card = completion_card(
        title="Codex 完成通知",
        project=sections["项目"],
        fields={key: value for key, value in sections.items() if key not in {"项目", "工作目录"}},
        footer_cwd=sections["工作目录"],
        footer_mode_text=cards.notification_mode_footer_text("all"),
        now=datetime(2026, 6, 18, 10, 0, tzinfo=timezone.utc),
    )

    text = flatten_text(card)
    assert card["header"]["title"]["content"] == "Codex 完成通知 - Demo"
    assert "**工作目录**" not in text
    assert "工作目录：/workspace/demo" in text
    assert "Done" in text
    assert "pytest" in text


def test_notification_mode_footer_text_uses_natural_language_not_cli():
    text = cards.notification_mode_footer_text("all")

    assert text.splitlines() == [
        "当前通知模式：每轮完成后都会通知",
        "通知模式修改方式：告诉Codex「关掉飞书完成通知」或「暂停飞书通知 2 小时」",
    ]
    assert "codex-away-mode" not in text


def test_away_card_lists_supported_commands_and_local_deadline(monkeypatch):
    set_local_timezone(monkeypatch, "Asia/Shanghai")
    card = away_card(
        context={
            "project": "Away Mode",
            "task": "等待飞书回复",
            "cwd": "/workspace/project",
        },
        deadline=datetime(2026, 6, 18, 10, 30, tzinfo=timezone.utc),
    )

    text = flatten_text(card)
    assert card["header"]["template"] == "purple"
    assert "/延长等待" in text
    assert "/状态" in text
    assert "/结束等待" in text
    assert "截止时间：06-18 18:30" in text
    assert "UTC" not in text
    assert "2026-" not in text
    assert "回复这张卡片" in text
    assert "CODEX_HOME" not in text
    assert "config.toml" not in text


def test_pre_timeout_reminder_card_mentions_five_minutes_extend_and_closed_delivery(monkeypatch):
    set_local_timezone(monkeypatch, "Asia/Shanghai")
    card = pre_timeout_reminder_card(
        project="Away Mode",
        deadline=datetime(2026, 6, 18, 10, 30, tzinfo=timezone.utc),
        minutes_left=5,
    )

    text = flatten_text(card)
    assert card["header"]["template"] == "orange"
    assert "5 分钟" in text
    assert "/延长等待" in text
    assert "截止时间：06-18 18:30" in text
    assert "UTC" not in text
    assert "2026-" not in text
    assert "超时后" in text
    assert "无法送达这个 Codex 回合" in text
    assert "CODEX_HOME" not in text
    assert "config.toml" not in text


def test_timeout_card_says_reply_window_is_closed_with_local_time(monkeypatch):
    set_local_timezone(monkeypatch, "Asia/Shanghai")
    card = timeout_card(
        project="Away Mode",
        deadline=datetime(2026, 6, 18, 10, 30, tzinfo=timezone.utc),
    )

    text = flatten_text(card)
    assert card["header"]["template"] == "red"
    assert "回复窗口已关闭" in text
    assert "关闭时间：06-18 18:30" in text
    assert "UTC" not in text
    assert "2026-" not in text
    assert "后续飞书消息" in text
    assert "不能到达这个 Codex 回合" in text


def test_user_ended_card_says_away_mode_closed_and_normal_wrap_up_can_continue(monkeypatch):
    set_local_timezone(monkeypatch, "Asia/Shanghai")
    card = cards.user_ended_card(
        project="Away Mode",
        ended_at=datetime(2026, 6, 18, 10, 30, tzinfo=timezone.utc),
    )

    text = flatten_text(card)
    assert card["header"]["title"]["content"] == "Away Mode 已结束 - Away Mode"
    assert card["header"]["template"] == "green"
    assert "回复窗口已关闭" in text
    assert "结束时间：06-18 18:30" in text
    assert "Codex 会继续完成本轮收尾" in text
    assert "UTC" not in text
    assert "2026-" not in text


def test_command_feedback_text_formats_deadline_in_local_short_datetime(monkeypatch):
    set_local_timezone(monkeypatch, "Asia/Shanghai")

    extend = command_feedback_text(
        "extend",
        new_deadline=datetime(2026, 6, 18, 11, 0, tzinfo=timezone.utc),
    )
    status = command_feedback_text(
        "status",
        deadline="2026-06-18T10:30:00+00:00",
    )

    assert "/延长等待" in extend
    assert "06-18 19:00" in extend
    assert "2026-" not in extend
    assert "UTC" not in extend
    assert "+00:00" not in extend
    assert "/状态" in status
    assert "当前截止时间：06-18 18:30" in status
    assert "2026-" not in status
    assert "UTC" not in status
    assert "+00:00" not in status


def test_command_feedback_text_covers_known_and_unknown_commands():
    assert "/结束等待" in command_feedback_text("end")
    assert "未知命令" in command_feedback_text("unknown", command="/foo")
    assert "/foo" in command_feedback_text("unknown", command="/foo")


def test_ordinary_dm_hint_text_points_to_corresponding_card():
    text = ordinary_dm_hint_text()

    assert "请回复对应卡片" in text
    assert "普通私聊" in text
    assert "Codex 回合" in text


def test_progress_and_early_exit_cards_exist_and_use_away_time(monkeypatch):
    set_local_timezone(monkeypatch, "Asia/Shanghai")
    stopped_at = datetime(2026, 6, 18, 10, 30, tzinfo=timezone.utc)

    progress = cards.away_progress_card(
        project="Demo",
        cwd="/workspace/demo",
        completed="已完成分析",
        changed="away.py",
        verification="pytest 未运行",
        unverified="飞书 live 未测",
        need_user="请继续回复卡片",
        deadline=stopped_at,
    )
    early_exit = cards.away_early_exit_card(
        project="Demo",
        cwd="/workspace/demo",
        completed="已完成分析",
        changed="away.py",
        verification="pytest 未运行",
        unverified="飞书 live 未测",
        need_user="请回 Codex 重新开启 Away Mode",
        stopped_at=stopped_at,
    )

    progress_text = flatten_text(progress)
    early_exit_text = flatten_text(early_exit)
    assert "Codex Away Mode 进度" in progress["header"]["title"]["content"]
    assert progress["header"]["template"] == "purple"
    assert "已完成分析" in progress_text
    assert "截止时间：06-18 18:30" in progress_text
    assert "旧卡" in progress_text
    assert "最新卡片" in progress_text
    assert early_exit["header"]["title"]["content"] == "Codex 已停止 - Away Mode 已结束"
    assert early_exit["header"]["template"] == "red"
    assert "Away Mode 回复窗口已关闭" in early_exit_text
    assert "停止时间：06-18 18:30" in early_exit_text
    assert "2026-" not in progress_text + early_exit_text
    assert "UTC" not in progress_text + early_exit_text


def test_retired_card_reply_text_points_to_latest_card():
    text = cards.retired_card_reply_text()

    assert "旧卡" in text
    assert "最新" in text
    assert "回复" in text
