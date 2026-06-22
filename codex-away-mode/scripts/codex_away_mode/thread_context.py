from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping


MAX_TITLE_PART_CHARS = 40


@dataclass(frozen=True)
class CardTitleContext:
    project_name: str | None = None
    thread_title: str | None = None
    thread_id: str | None = None
    project_source: str = "none"
    thread_id_source: str = "none"
    thread_title_source: str = "none"


def resolve_card_title_context(
    *,
    cwd: str | None,
    hook_stdin: str | bytes | None = None,
    env: Mapping[str, str] | None = None,
    codex_home: str | Path | None = None,
    explicit_codex_session_id: str | None = None,
) -> CardTitleContext:
    env_map = env if env is not None else os.environ
    project_name, project_source = _project_name_from_cwd(cwd)
    thread_id, thread_id_source = _thread_id_from_sources(
        hook_stdin=hook_stdin,
        env=env_map,
        explicit_codex_session_id=explicit_codex_session_id,
    )
    title, title_source = _thread_title_from_session_index(
        thread_id,
        codex_home=_codex_home(env_map, codex_home),
    )
    return CardTitleContext(
        project_name=project_name,
        project_source=project_source,
        thread_id=thread_id,
        thread_id_source=thread_id_source,
        thread_title=title,
        thread_title_source=title_source,
    )


def format_card_title(base_title: str, context: CardTitleContext | None) -> str:
    if context is None:
        return base_title
    project = _clean_title_part(context.project_name)
    thread = _clean_title_part(context.thread_title)
    if project and thread and project == thread:
        thread = None
    if project and thread:
        return f"{base_title} - {project} / {thread}"
    if project:
        return f"{base_title} - {project}"
    if thread:
        return f"{base_title} / {thread}"
    return base_title


def _project_name_from_cwd(cwd: str | None) -> tuple[str | None, str]:
    if not cwd:
        return None, "none"
    path = Path(str(cwd)).expanduser()
    if not path.is_absolute():
        return None, "invalid_cwd"
    name = path.name.strip()
    if not name:
        return None, "none"
    return name, "cwd_basename"


def _thread_id_from_sources(
    *,
    hook_stdin: str | bytes | None,
    env: Mapping[str, str],
    explicit_codex_session_id: str | None,
) -> tuple[str | None, str]:
    hook_session_id = _extract_hook_string(hook_stdin, "session_id")
    if hook_session_id:
        return hook_session_id, "hook_session_id"
    hook_thread_id = _extract_hook_string(hook_stdin, "thread_id")
    if hook_thread_id:
        return hook_thread_id, "hook_thread_id"
    env_thread_id = str(env.get("CODEX_THREAD_ID") or "").strip()
    if env_thread_id:
        return env_thread_id, "env_CODEX_THREAD_ID"
    explicit = str(explicit_codex_session_id or "").strip()
    if explicit:
        return explicit, "explicit_codex_session_id"
    return None, "none"


def _thread_title_from_session_index(
    thread_id: str | None,
    *,
    codex_home: Path,
) -> tuple[str | None, str]:
    if not thread_id:
        return None, "no_thread_id"
    path = codex_home / "session_index.jsonl"
    if not path.exists() or not path.is_file():
        return None, "session_index_missing"
    title: str | None = None
    try:
        with path.open(encoding="utf-8") as handle:
            for line in handle:
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(record, dict):
                    continue
                if record.get("id") != thread_id:
                    continue
                candidate = record.get("thread_name")
                if isinstance(candidate, str) and candidate.strip():
                    title = candidate.strip()
    except OSError:
        return None, "session_index_unreadable"
    if title:
        return title, "session_index"
    return None, "session_index_not_found"


def _extract_hook_string(hook_stdin: str | bytes | None, field: str) -> str | None:
    if not hook_stdin:
        return None
    if isinstance(hook_stdin, bytes):
        hook_stdin = hook_stdin.decode("utf-8", errors="replace")
    try:
        payload = json.loads(hook_stdin)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    value = payload.get(field)
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value or None


def _codex_home(env: Mapping[str, str], codex_home: str | Path | None) -> Path:
    if codex_home is not None:
        return Path(codex_home).expanduser()
    return Path(env.get("CODEX_HOME") or Path.home() / ".codex").expanduser()


def _clean_title_part(value: str | None) -> str | None:
    cleaned = " ".join(str(value or "").split())
    if not cleaned:
        return None
    if len(cleaned) > MAX_TITLE_PART_CHARS:
        return cleaned[:MAX_TITLE_PART_CHARS] + "…"
    return cleaned
