from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path
from typing import Any

from .thread_context import CardTitleContext, format_card_title


def completion_card(
    *,
    title: str,
    fields: dict[str, Any],
    footer_mode_text: str,
    project: str | None = None,
    footer_cwd: str | None = None,
    now: datetime | str | None = None,
    title_suffix: str = "",
    title_context: CardTitleContext | None = None,
) -> dict[str, Any]:
    fields = dict(fields)
    footer_cwd = footer_cwd or _pop_workdir(fields)
    body = [_markdown(_completion_section(key, value)) for key, value in fields.items()]
    body.append(
        _completion_footer(
            now=now,
            footer_cwd=footer_cwd,
            footer_mode_text=footer_mode_text,
            title_context=title_context,
        )
    )
    return _away_card(
        title=_completion_identity_title(title, project, title_suffix, title_context),
        subtitle="Codex完成通知：本轮任务已结束",
        status_label="完成通知",
        status_color="blue",
        template="blue",
        elements=body,
    )


def fallback_completion_card(
    *,
    reason: str,
    cwd: str,
    now: datetime | str,
    title_context: CardTitleContext | None = None,
) -> dict[str, Any]:
    project = _project_from_cwd(cwd)
    elements = [
        _markdown(
            "**发生了什么**\n"
            "- Codex 这一轮已经停止。\n"
            "- 但 agent 没有写可用的完成摘要，所以无法生成正常完成通知。"
        ),
        _markdown(
            "**你需要知道**\n"
            "- 这不一定代表任务失败，只代表这条通知缺少摘要。\n"
            "- 如果这不是你预期的结束点，请回到 Codex 查看会话。"
        ),
        _markdown(
            "**下一步**\n"
            "- 需要继续：回到 Codex 直接追问或重新发起任务。\n"
            "- 不需要处理：可以忽略这条兜底通知。"
        ),
        _completion_footer(
            now=now,
            footer_cwd=cwd,
            footer_mode_text=notification_mode_footer_text("all"),
            title_context=title_context,
        ),
    ]
    return _away_card(
        title=_completion_identity_title("Codex 完成通知", project, "", title_context),
        subtitle="Codex完成通知：本轮任务已停止但缺少摘要",
        status_label="无摘要",
        status_color="wathet",
        template="wathet",
        elements=elements,
    )


def permission_request_card(
    *,
    project: str | None,
    cwd: str | None,
    tool_name: str,
    description: str | None,
    command: str | None,
    now: datetime | str | None,
    title_context: CardTitleContext | None = None,
) -> dict[str, Any]:
    identity = _away_identity_title(
        title_context=title_context,
        fallback_project=project or project_from_cwd(cwd),
    )
    detail_lines = [
        "**请回到 Codex Desktop 处理审批**",
        "飞书不能直接完成审批；请在 Codex Desktop 中确认或拒绝这项操作。",
    ]
    request_lines = [
        "**审批说明**",
        str(description or "Codex 请求执行一项需要审批的操作。").strip(),
        "",
        "**请求操作**",
        f"{str(tool_name or '未知工具').strip()}：{str(command or '未提供可展示的操作内容').strip()}",
    ]
    return _away_card(
        title=identity,
        subtitle="Codex审批提醒：有一项操作正在等待你确认",
        status_label="需要审批",
        status_color="orange",
        template="orange",
        elements=[
            _markdown("\n".join(detail_lines)),
            _static_box("\n".join(request_lines)),
            _approval_footer(now=now, cwd=cwd, title_context=title_context),
        ],
    )


def notification_mode_footer_text(mode: str) -> str:
    normalized = mode or "all"
    if normalized == "all":
        return "每轮完成后通知"
    elif normalized == "off":
        return "不会发送完成通知"
    if normalized == "snooze":
        return "暂停中"
    return str(normalized)


def summary_sections(markdown: str) -> dict[str, str]:
    sections: dict[str, list[str]] = {}
    current: str | None = None
    for raw_line in markdown.splitlines():
        line = raw_line.strip()
        if line.startswith("**") and line.endswith("**") and len(line) > 4:
            current = line[2:-2].strip()
            sections.setdefault(current, [])
            continue
        if current is not None:
            sections[current].append(raw_line)
    return {
        key: "\n".join(lines).strip()
        for key, lines in sections.items()
        if "\n".join(lines).strip()
    }


def away_card(
    *,
    context: dict[str, Any],
    deadline: datetime | str,
    title_context: CardTitleContext | None = None,
) -> dict[str, Any]:
    cwd = str(context.get("cwd") or "未提供")
    return _away_card(
        title=_away_identity_title(
            title_context=title_context,
            fallback_project=str(context.get("project") or "Codex Away Mode"),
        ),
        subtitle="Codex Away Mode：已进入 Away Mode，正在等待你的回复",
        status_label="等待中",
        status_color="wathet",
        template="purple",
        elements=[
            _markdown("**本轮进度**\n已按你的要求进入 Away Mode。Codex 会在回复窗口内等待飞书卡片回复。"),
            _highlight_box(
                "普通私聊不会进入当前 Codex 回合；请直接回复这张卡片发送下一条指令。",
            ),
            _away_footer(deadline=deadline, cwd=cwd),
        ],
    )


def _highlight_box(detail: str) -> dict[str, Any]:
    content = "\n".join(
        [
            "**需要你看**",
            detail.strip() or "请回复这张卡片继续。",
            "",
            "**请回复这张卡片继续**",
        ]
    )
    return {
        "tag": "column_set",
        "background_style": "grey",
        "columns": [
            {
                "tag": "column",
                "width": "weighted",
                "weight": 1,
                "elements": [_markdown(content)],
            }
        ],
    }


def _static_box(content: str) -> dict[str, Any]:
    return {
        "tag": "column_set",
        "background_style": "grey",
        "columns": [
            {
                "tag": "column",
                "width": "weighted",
                "weight": 1,
                "elements": [_markdown(content)],
            }
        ],
    }


def _away_footer(*, deadline: datetime | str, cwd: str) -> dict[str, Any]:
    lines = [
        f"**回复窗口将于 {_display_time(deadline)} 关闭，请在此之前回复**",
        "可用命令：/延长等待、/状态、/结束等待",
        f"工作目录：{cwd}",
    ]
    return _markdown("\n".join(f"> {line}" for line in lines))


def _approval_footer(
    *,
    now: datetime | str | None,
    cwd: str | None,
    title_context: CardTitleContext | None,
) -> dict[str, Any]:
    first_line_parts: list[str] = []
    if now:
        first_line_parts.append(f"**时间**：{_display_time(now)}")
    directory = _completion_directory_label(cwd, title_context)
    if directory:
        first_line_parts.append(f"**目录**：{directory}")
    lines = []
    if first_line_parts:
        lines.append("> " + " ｜ ".join(first_line_parts))
    lines.append("> 如果这不是你预期的操作，请在 Codex Desktop 中拒绝。")
    return _markdown("\n".join(lines))


def _away_card(
    *,
    title: str,
    subtitle: str,
    status_label: str,
    status_color: str,
    template: str,
    elements: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "schema": "2.0",
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": title},
            "subtitle": {"tag": "plain_text", "content": subtitle},
            "template": template,
            "text_tag_list": [
                {
                    "tag": "text_tag",
                    "text": {"tag": "plain_text", "content": status_label},
                    "color": status_color,
                }
            ],
        },
        "body": {"elements": elements},
    }


def _away_identity_title(
    *,
    title_context: CardTitleContext | None,
    fallback_project: str | None,
) -> str:
    thread = _compact_title_part(title_context.thread_title if title_context else None)
    project = _compact_title_part(title_context.project_name if title_context else None)
    fallback = _compact_title_part(fallback_project)
    if not project:
        project = fallback
    if thread and project and thread != project:
        return f"{thread} / {project}"
    if thread:
        return thread
    if project:
        return project
    return "Codex Away Mode"


def _completion_identity_title(
    title: str,
    project: str | None,
    title_suffix: str,
    title_context: CardTitleContext | None,
) -> str:
    identity = _away_identity_title(title_context=title_context, fallback_project=project)
    compact_title = _compact_title_part(title)
    generic_titles = {"完成通知", "Codex 完成通知", "Codex完成通知"}
    if identity != "Codex Away Mode":
        if compact_title and compact_title not in generic_titles:
            return f"{compact_title} - {identity}{title_suffix}"
        return f"{identity}{title_suffix}"
    if compact_title:
        return f"{compact_title}{title_suffix}"
    return f"Codex 完成通知{title_suffix}"


def _completion_section(key: str, value: Any) -> str:
    return f"**{key}**\n{_as_markdown_list(value)}"


def _as_markdown_list(value: Any) -> str:
    normalized = _normalize_summary_text(value)
    if not normalized:
        return "- 未提供"
    lines = [line.rstrip() for line in normalized.splitlines()]
    non_empty = [line.strip() for line in lines if line.strip()]
    if any(_is_markdown_list_item(line) for line in non_empty):
        return "\n".join(lines).strip()
    paragraphs = [part.strip() for part in re.split(r"\n\s*\n+", normalized) if part.strip()]
    if len(paragraphs) <= 1:
        paragraphs = non_empty
    return "\n".join(f"- {part}" for part in paragraphs if part)


def _normalize_summary_text(value: Any) -> str:
    text = str(value or "").strip()
    if "\\n" in text:
        text = text.replace("\\r\\n", "\n").replace("\\n", "\n")
    return text.strip()


def _is_markdown_list_item(line: str) -> bool:
    return bool(re.match(r"^\s*(?:[-*•]|\d+[.)])\s+", line))


def _completion_footer(
    *,
    now: datetime | str | None,
    footer_cwd: str | None,
    footer_mode_text: str,
    title_context: CardTitleContext | None,
) -> dict[str, Any]:
    first_line_parts: list[str] = []
    if now:
        first_line_parts.append(f"**时间**：{_display_time(now)}")
    directory = _completion_directory_label(footer_cwd, title_context)
    if directory:
        first_line_parts.append(f"**目录**：{directory}")
    mode = str(footer_mode_text or "").strip()
    if mode:
        first_line_parts.append(f"**通知模式**：{mode}")

    lines = []
    if first_line_parts:
        lines.append("> " + " ｜ ".join(first_line_parts))
    lines.append("> 如需修改通知模式，请告诉Codex `暂停飞书通知 2 小时`")
    return _markdown("\n".join(lines))


def _completion_directory_label(
    footer_cwd: str | None,
    title_context: CardTitleContext | None,
) -> str | None:
    project = _compact_title_part(title_context.project_name if title_context else None)
    if project:
        return project
    if footer_cwd:
        return _project_from_cwd(str(footer_cwd))
    return None


def _compact_title_part(value: str | None) -> str | None:
    cleaned = " ".join(str(value or "").split())
    if not cleaned:
        return None
    if len(cleaned) > 40:
        return cleaned[:40] + "…"
    return cleaned


def pre_timeout_reminder_card(
    *,
    project: str,
    deadline: datetime | str,
    minutes_left: int = 5,
    title_context: CardTitleContext | None = None,
) -> dict[str, Any]:
    return _away_card(
        title=f"回复窗口还有 {minutes_left} 分钟关闭 - Codex Away Mode",
        subtitle=_away_identity_title(title_context=title_context, fallback_project=project),
        status_label="即将超时",
        status_color="orange",
        template="orange",
        elements=[
            _markdown(
                "**本任务的飞书等待窗口即将超时关闭**\n"
                "如需继续，请回复这张卡片发送 `/延长等待`\n"
                "或者直接回复 `把等待时间延长1个小时`"
            ),
            _markdown("超时后，你将无法继续在飞书中给 Codex 继续下达任务指令"),
        ],
    )


def timeout_card(
    *,
    project: str,
    deadline: datetime | str,
    title_context: CardTitleContext | None = None,
) -> dict[str, Any]:
    return _away_card(
        title="回复窗口已超时关闭 - Codex Away Mode",
        subtitle=_away_identity_title(title_context=title_context, fallback_project=project),
        status_label="已超时",
        status_color="red",
        template="red",
        elements=[
            _markdown(
                "**本任务的飞书等待窗口已超时关闭**\n"
                "你已无法继续在飞书中给 Codex 下达任务指令。\n"
                "如需继续，请回到桌面端重新开启 Codex Away Mode。"
            ),
        ],
    )


def user_ended_card(
    *,
    project: str,
    ended_at: datetime | str,
    title_context: CardTitleContext | None = None,
) -> dict[str, Any]:
    return _away_card(
        title="回复窗口已结束 - Codex Away Mode",
        subtitle=_away_identity_title(title_context=title_context, fallback_project=project),
        status_label="已结束",
        status_color="green",
        template="green",
        elements=[
            _markdown(
                "**本任务的飞书等待窗口已关闭**\n"
                "Codex 会继续完成本轮收尾。\n"
                "收尾完成后，会通过普通完成通知告诉你结果。"
            ),
        ],
    )


def command_feedback_text(command_kind: str, **kwargs: Any) -> str:
    if command_kind == "extend":
        deadline = kwargs.get("new_deadline", "新的截止时间未提供")
        return f"已处理 /延长等待，新的截止时间：{away_time(deadline)}。"
    if command_kind == "status":
        if "deadline" in kwargs:
            return f"/状态：仍在等待，当前截止时间：{away_time(kwargs['deadline'])}。"
        status = kwargs.get("status", "仍在等待你的卡片回复。")
        return f"/状态：{status}"
    if command_kind == "end":
        return "已处理 /结束等待，这个 Codex 回合的 Away Mode 回复窗口将关闭。"
    command = kwargs.get("command", "/未知")
    return f"未知命令：{command}。可用命令：/延长等待、/状态、/结束等待。"


def ordinary_dm_hint_text() -> str:
    return "这是一条普通私聊，不能进入当前 Codex 回合。请回复对应卡片继续。"


def away_progress_card(
    *,
    project: str,
    cwd: str,
    completed: str,
    changed: str,
    verification: str,
    unverified: str,
    need_user: str,
    deadline: datetime | str,
    title_context: CardTitleContext | None = None,
) -> dict[str, Any]:
    return _away_card(
        title=_away_identity_title(title_context=title_context, fallback_project=project),
        subtitle="Codex Away Mode：已处理上一条回复，正在等待下一步",
        status_label="等待中",
        status_color="wathet",
        template="purple",
        elements=[
            _markdown(f"**本轮进度**\n{_progress_detail(completed)}"),
            _highlight_box(need_user),
            _away_footer(deadline=deadline, cwd=cwd),
        ],
    )


def _progress_detail(completed: str) -> str:
    value = str(completed or "").strip()
    if value and value not in {"无", "未知", "未提供"}:
        return value
    return "已处理上一条回复，正在等待你的下一步指令。"


def away_early_exit_card(
    *,
    project: str,
    cwd: str,
    completed: str,
    changed: str,
    verification: str,
    unverified: str,
    need_user: str,
    stopped_at: datetime | str,
    title_context: CardTitleContext | None = None,
) -> dict[str, Any]:
    lines = [
        f"项目：{project}",
        f"当前进度：{completed}",
        f"变更：{changed}",
        f"验证：{verification}",
        f"未验证：{unverified}",
        f"需要你看：{need_user}",
        f"停止时间：{away_time(stopped_at)}",
        "Away Mode 回复窗口已关闭。",
        "如需继续，请回到 Codex 重新发起指令或重新开启 Away Mode。",
    ]
    return _card(
        _card_title("Codex 已停止 - Away Mode 已结束", None, "", title_context),
        [_markdown("\n".join(lines)), _note(f"工作目录：{cwd}\n模式：Away Mode 已结束")],
        template="red",
    )


def retired_card_reply_text() -> str:
    return "这张旧卡已不是当前回复入口，请回复最新 Away Mode 卡片。"


def away_time(value: datetime | str) -> str:
    return _display_datetime(value)


def completion_time(value: datetime | str) -> str:
    return _display_time(value)


def _card(title: str, elements: list[dict[str, Any]], *, template: str = "blue") -> dict[str, Any]:
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": title},
            "template": template,
        },
        "elements": elements,
    }


def _markdown(content: str) -> dict[str, Any]:
    return {"tag": "markdown", "content": content}


def _note(content: str) -> dict[str, Any]:
    return {"tag": "note", "elements": [{"tag": "plain_text", "content": content}]}


def _footer_note(contents: list[str]) -> dict[str, Any]:
    lines: list[str] = []
    for content in contents:
        lines.extend(line.strip() for line in str(content).splitlines() if line.strip())
    return _note("\n".join(lines))


def _pop_workdir(fields: dict[str, Any]) -> str | None:
    if "工作目录" not in fields:
        return None
    return str(fields.pop("工作目录"))


def project_from_cwd(cwd: str | None) -> str | None:
    if not cwd:
        return None
    return _project_from_cwd(cwd)


def _project_from_cwd(cwd: str) -> str | None:
    name = Path(cwd).name
    return name or None


def _title_with_project(title: str, project: str | None, title_suffix: str = "") -> str:
    project = (project or "").strip()
    if project:
        return f"{title} - {project}{title_suffix}"
    return f"{title}{title_suffix}"


def _card_title(
    title: str,
    project: str | None,
    title_suffix: str,
    title_context: CardTitleContext | None,
) -> str:
    if title_context is not None:
        return format_card_title(title, title_context) + title_suffix
    return _title_with_project(title, project, title_suffix)


def _display_time(value: datetime | str) -> str:
    if isinstance(value, datetime):
        local = value.astimezone()
        return f"{local:%H:%M}"
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return str(value)
    return _display_time(parsed)


def _display_datetime(value: datetime | str) -> str:
    if isinstance(value, datetime):
        return f"{value.astimezone():%m-%d %H:%M}"
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return str(value)
    return _display_datetime(parsed)
