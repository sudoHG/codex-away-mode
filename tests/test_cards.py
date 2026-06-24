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


def card_tags(value):
    tags = []
    if isinstance(value, dict):
        tag = value.get("tag")
        if isinstance(tag, str):
            tags.append(tag)
        for item in value.values():
            tags.extend(card_tags(item))
    elif isinstance(value, list):
        for item in value:
            tags.extend(card_tags(item))
    return tags


def header_tag_texts(card):
    texts = []
    for item in card["header"].get("text_tag_list", []):
        text = item.get("text", {})
        if isinstance(text, dict):
            texts.append(text.get("content"))
    return texts


def body_markdown_contents(card):
    return [
        element.get("content", "")
        for element in card["body"]["elements"]
        if element.get("tag") == "markdown"
    ]


def assert_away_card_v2(card):
    assert card["schema"] == "2.0"
    assert "elements" not in card
    assert isinstance(card["body"]["elements"], list)
    assert "interactive_container" not in card_tags(card)


def assert_completion_card_v2(card):
    assert card["schema"] == "2.0"
    assert "elements" not in card
    assert isinstance(card["body"]["elements"], list)
    assert "interactive_container" not in card_tags(card)


def assert_quoted_away_footer(card, *, deadline="18:30"):
    footer = body_markdown_contents(card)[-1]
    assert footer.splitlines() == [
        f"> **回复窗口将于 {deadline} 关闭，请在此之前回复**",
        "> 可用命令：/延长等待、/状态、/结束等待",
    ]


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
        title_context=cards.CardTitleContext(
            project_name="Skill-Create",
            thread_title="建立 Skill-Create 基线",
        ),
    )

    text = flatten_text(card)
    markdowns = body_markdown_contents(card)
    assert_completion_card_v2(card)
    assert card["header"]["title"]["content"] == "建立 Skill-Create 基线 / Skill-Create"
    assert card["header"]["subtitle"]["content"] == "Codex完成通知：本轮任务已结束"
    assert header_tag_texts(card) == ["完成通知"]
    assert card["header"]["template"] == "blue"
    assert card["header"]["text_tag_list"][0]["color"] == "blue"
    assert "**工作目录**" not in text
    assert "/workspace/project" not in text
    assert "工作目录：" not in text
    assert "**完成**\n- 已完成 Task 4" in markdowns
    assert "**验证**\n- pytest passed" in markdowns
    footer_lines = markdowns[-1].splitlines()
    assert footer_lines == [
        "> **时间**：18:00 ｜ **通知模式**：每轮完成后通知",
        "> 修改通知模式：告诉Codex `暂停飞书通知 2 小时`",
    ]
    assert all(line.strip() for line in footer_lines)
    assert "2026-" not in markdowns[-1]
    assert "UTC" not in markdowns[-1]
    assert "codex-away-mode notify" not in text
    assert "config.toml" not in text


def test_fallback_completion_card_uses_cwd_project_title_and_footer_cwd(monkeypatch):
    set_local_timezone(monkeypatch, "Asia/Shanghai")
    card = fallback_completion_card(
        reason="summary missing",
        cwd="/workspace/immichSlides-app",
        now=datetime(2026, 6, 18, 10, 1, tzinfo=timezone.utc),
        title_context=cards.CardTitleContext(
            project_name="immichSlides-app",
            thread_title="判断工程状态并梳理优先事项",
        ),
    )

    text = flatten_text(card)
    markdowns = body_markdown_contents(card)
    assert_completion_card_v2(card)
    assert card["header"]["title"]["content"] == "判断工程状态并梳理优先事项 / immichSlides-app"
    assert card["header"]["subtitle"]["content"] == "Codex完成通知：本轮任务已停止但缺少摘要"
    assert header_tag_texts(card) == ["无摘要"]
    assert card["header"]["template"] == "wathet"
    assert card["header"]["text_tag_list"][0]["color"] == "wathet"
    assert "无摘要" in text
    assert "summary missing" not in text
    assert "**发生了什么**" in text
    assert "- Codex 这一轮已经停止。" in text
    assert "- 但 agent 没有写可用的完成摘要，所以无法生成正常完成通知。" in text
    assert "**你需要知道**" in text
    assert "- 这不一定代表任务失败，只代表这条通知缺少摘要。" in text
    assert "**下一步**" in text
    assert "- 需要继续：回到 Codex 直接追问或重新发起任务。" in text
    assert "- 不需要处理：可以忽略这条兜底通知。" in text
    assert "**工作目录**" not in text
    assert "/workspace/immichSlides-app" not in text
    assert markdowns[-1].splitlines() == [
        "> **时间**：18:01 ｜ **通知模式**：每轮完成后通知",
        "> 修改通知模式：告诉Codex `暂停飞书通知 2 小时`",
    ]
    assert "UTC" not in markdowns[-1]
    assert "忘了写摘要" not in text
    assert "告诉Codex" in text
    assert "codex-away-mode notify" not in text


def test_permission_request_card_is_a_desktop_approval_reminder(monkeypatch):
    set_local_timezone(monkeypatch, "Asia/Shanghai")

    card = cards.permission_request_card(
        project="Skill-Create",
        cwd="/Users/hutong/Codex项目/Skill-Create",
        tool_name="Bash",
        description="删除工作区外的临时文件",
        command="rm /workspace-outside/codex-desktop-permission-target.txt",
        now=datetime(2026, 6, 24, 8, 30, tzinfo=timezone.utc),
        title_context=cards.CardTitleContext(
            project_name="Skill-Create",
            thread_title="建立 Skill-Create 基线",
        ),
    )

    text = flatten_text(card)
    markdowns = body_markdown_contents(card)
    assert_away_card_v2(card)
    assert card["header"]["title"]["content"] == "建立 Skill-Create 基线 / Skill-Create"
    assert card["header"]["subtitle"]["content"] == "Codex审批提醒：有一项操作正在等待你确认"
    assert header_tag_texts(card) == ["需要审批"]
    assert card["header"]["template"] == "orange"
    assert "Bash" in text
    assert "删除工作区外的临时文件" in text
    assert "rm /workspace-outside/codex-desktop-permission-target.txt" in text
    assert "请回到 Codex Desktop 处理审批" in text
    assert "飞书不能直接完成审批" in text
    assert markdowns[-1].splitlines() == [
        "> **时间**：16:30",
        "> 如果这不是你预期的操作，请在 Codex Desktop 中拒绝。",
    ]


def test_summary_sections_support_completion_card_without_body_workdir(monkeypatch):
    set_local_timezone(monkeypatch, "Asia/Shanghai")
    sections = cards.summary_sections(
        "**项目**\nDemo\n\n**工作目录**\n/workspace/demo\n\n**完成**\nDone\n\n**验证**\npytest\n"
    )

    card = completion_card(
        title="Codex 完成通知",
        project=sections["项目"],
        fields={
            "完成": "Done\\n\\nMore",
            **{key: value for key, value in sections.items() if key not in {"项目", "工作目录", "完成"}},
        },
        footer_cwd=sections["工作目录"],
        footer_mode_text=cards.notification_mode_footer_text("all"),
        now=datetime(2026, 6, 18, 10, 0, tzinfo=timezone.utc),
        title_context=cards.CardTitleContext(project_name="demo"),
    )

    text = flatten_text(card)
    assert_completion_card_v2(card)
    assert card["header"]["title"]["content"] == "demo"
    assert card["header"]["subtitle"]["content"] == "Codex完成通知：本轮任务已结束"
    assert "**工作目录**" not in text
    assert "/workspace/demo" not in text
    assert "\\n" not in text
    assert "- Done" in text
    assert "- More" in text
    assert "pytest" in text


def test_notification_mode_footer_text_uses_natural_language_not_cli():
    text = cards.notification_mode_footer_text("all")

    assert text == "每轮完成后通知"
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
        title_context=cards.CardTitleContext(
            project_name="project",
            thread_title="建立 Skill-Create 基线",
        ),
    )

    text = flatten_text(card)
    assert_away_card_v2(card)
    assert card["header"]["title"]["content"] == "建立 Skill-Create 基线 / project"
    assert card["header"]["subtitle"]["content"] == "Codex Away Mode：已进入 Away Mode，正在等待你的回复"
    assert header_tag_texts(card) == ["等待中"]
    assert card["header"]["template"] == "purple"
    assert "/延长等待" in text
    assert "/状态" in text
    assert "/结束等待" in text
    assert "**本轮进度**" in text
    assert "已按你的要求进入 Away Mode" in text
    assert "**需要你看**" in text
    assert "**请回复这张卡片继续**" in text
    assert_quoted_away_footer(card)
    assert "截止时间：" not in text
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
        title_context=cards.CardTitleContext(
            project_name="project",
            thread_title="建立 Skill-Create 基线",
        ),
    )

    text = flatten_text(card)
    assert_away_card_v2(card)
    assert card["header"]["title"]["content"] == "回复窗口还有 5 分钟关闭 - Codex Away Mode"
    assert card["header"]["subtitle"]["content"] == "建立 Skill-Create 基线 / project"
    assert header_tag_texts(card) == ["即将超时"]
    assert card["header"]["template"] == "orange"
    assert "5 分钟" in text
    assert "/延长等待" in text
    assert "本任务的飞书等待窗口即将超时关闭" in text
    assert "如需继续，请回复这张卡片发送 `/延长等待`" in text
    assert "或者直接回复 `把等待时间延长1个小时`" in text
    assert "超时后，你将无法继续在飞书中给 Codex 继续下达任务指令" in text
    assert "截止时间：" not in text
    assert "工作目录" not in text
    assert "UTC" not in text
    assert "2026-" not in text
    assert "CODEX_HOME" not in text
    assert "config.toml" not in text


def test_timeout_card_says_reply_window_is_closed_with_local_time(monkeypatch):
    set_local_timezone(monkeypatch, "Asia/Shanghai")
    card = timeout_card(
        project="Away Mode",
        deadline=datetime(2026, 6, 18, 10, 30, tzinfo=timezone.utc),
        title_context=cards.CardTitleContext(
            project_name="project",
            thread_title="建立 Skill-Create 基线",
        ),
    )

    text = flatten_text(card)
    assert_away_card_v2(card)
    assert card["header"]["title"]["content"] == "回复窗口已超时关闭 - Codex Away Mode"
    assert card["header"]["subtitle"]["content"] == "建立 Skill-Create 基线 / project"
    assert header_tag_texts(card) == ["已超时"]
    assert card["header"]["template"] == "red"
    assert "本任务的飞书等待窗口已超时关闭" in text
    assert "你已无法继续在飞书中给 Codex 下达任务指令。" in text
    assert "如需继续，请回到桌面端重新开启 Codex Away Mode。" in text
    assert "关闭时间：" not in text
    assert "工作目录" not in text
    assert "UTC" not in text
    assert "2026-" not in text


def test_user_ended_card_says_away_mode_closed_and_normal_wrap_up_can_continue(monkeypatch):
    set_local_timezone(monkeypatch, "Asia/Shanghai")
    card = cards.user_ended_card(
        project="Away Mode",
        ended_at=datetime(2026, 6, 18, 10, 30, tzinfo=timezone.utc),
        title_context=cards.CardTitleContext(
            project_name="project",
            thread_title="建立 Skill-Create 基线",
        ),
    )

    text = flatten_text(card)
    assert_away_card_v2(card)
    assert card["header"]["title"]["content"] == "回复窗口已结束 - Codex Away Mode"
    assert card["header"]["subtitle"]["content"] == "建立 Skill-Create 基线 / project"
    assert header_tag_texts(card) == ["已结束"]
    assert card["header"]["template"] == "green"
    assert "本任务的飞书等待窗口已关闭" in text
    assert "Codex 会继续完成本轮收尾。" in text
    assert "收尾完成后，会通过普通完成通知告诉你结果。" in text
    assert "结束时间：" not in text
    assert "工作目录" not in text
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
    assert_away_card_v2(progress)
    assert progress["header"]["title"]["content"] == "Demo"
    assert progress["header"]["subtitle"]["content"] == "Codex Away Mode：已处理上一条回复，正在等待下一步"
    assert header_tag_texts(progress) == ["等待中"]
    assert progress["header"]["template"] == "purple"
    assert "**本轮进度**" in progress_text
    assert "已完成分析" in progress_text
    assert "变更：" not in progress_text
    assert "验证：" not in progress_text
    assert "未验证：" not in progress_text
    assert "**需要你看**" in progress_text
    assert "请回 Codex 重新开启 Away Mode" not in progress_text
    assert "**请回复这张卡片继续**" in progress_text
    assert_quoted_away_footer(progress)
    assert early_exit["header"]["title"]["content"] == "Codex 已停止 - Away Mode 已结束"
    assert early_exit["header"]["template"] == "red"
    assert "Away Mode 回复窗口已关闭" in early_exit_text
    assert "停止时间：06-18 18:30" in early_exit_text
    assert "工作目录" not in early_exit_text
    assert "2026-" not in progress_text + early_exit_text
    assert "UTC" not in progress_text + early_exit_text


def test_retired_card_reply_text_points_to_latest_card():
    text = cards.retired_card_reply_text()

    assert "旧卡" in text
    assert "最新" in text
    assert "回复" in text
