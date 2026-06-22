from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import cards
from .config import AppConfig, effective_notification_mode as _config_mode
from .config import ensure_runtime_state_writable, load_config, save_config
from .state import StateStore


DEFAULT_SUMMARY_MAX_AGE_SECONDS = 300
DEFAULT_PROMPT_MARKER_MAX_AGE_SECONDS = 300
_SAFE_CAPTURE_VALUE_KEYS = {
    "approval_policy",
    "cwd",
    "event",
    "event_name",
    "goal_state",
    "goal_status",
    "hook_event_name",
    "model",
    "sandbox_mode",
    "session_id",
    "source",
    "status",
    "thread_id",
    "thread_source",
    "turn_id",
    "type",
}
_MAX_CAPTURE_DEPTH = 6
_MAX_CAPTURE_LIST_ITEMS = 20


@dataclass(frozen=True)
class NotifyResult:
    status: str
    detail: str | None = None


def _runtime_store(paths) -> StateStore:
    ensure_runtime_state_writable(paths)
    return StateStore(Path(paths.runtime_state_path))


def mark_prompt(paths, cwd: str, now: datetime) -> str:
    store = _runtime_store(paths)
    return store.mark_prompt_marker(
        cwd=cwd,
        marked_at=_to_utc(now).isoformat(),
        expires_at=(_to_utc(now) + timedelta(seconds=DEFAULT_PROMPT_MARKER_MAX_AGE_SECONDS)).isoformat(),
    )


def stage_summary(
    paths,
    *,
    cwd: str,
    summary_markdown: str,
    now: datetime,
    max_age_seconds: int = DEFAULT_SUMMARY_MAX_AGE_SECONDS,
) -> str:
    store = _runtime_store(paths)
    return store.stage_summary(
        cwd=cwd,
        summary_markdown=summary_markdown,
        staged_at=_to_utc(now).isoformat(),
        expires_at=(_to_utc(now) + timedelta(seconds=max_age_seconds)).isoformat(),
    )


def record_hook_invocation(
    paths,
    *,
    hook_event_name: str,
    cwd: str,
    now: datetime,
    hooks_fingerprint: str,
    hook_stdin: str | bytes | None,
) -> str | None:
    if not hook_stdin:
        return None
    try:
        store = _runtime_store(paths)
        return store.record_diagnostic_event(
            event_kind="codex_hook_invocation",
            severity="info",
            message=f"{hook_event_name} hook executed.",
            detail={
                "hook_event_name": hook_event_name,
                "cwd_hash": StateStore.cwd_hash(cwd),
                "hooks_fingerprint": hooks_fingerprint,
            },
            created_at=_to_utc(now).isoformat(),
        )
    except Exception:
        return None


def resolve_notify_cwd(
    explicit_cwd: str | None,
    hook_stdin: str | bytes | None,
    process_cwd: str | None,
) -> str:
    if explicit_cwd:
        return explicit_cwd
    stdin_cwd = _extract_stdin_cwd(hook_stdin)
    if stdin_cwd and os.path.isabs(stdin_cwd):
        return stdin_cwd
    return process_cwd or os.getcwd()


def send_completion_from_summary(
    paths,
    lark,
    cwd: str,
    now: datetime,
    hook_stdin: str | bytes | None = None,
    max_age_seconds: int = DEFAULT_SUMMARY_MAX_AGE_SECONDS,
    prompt_marker_max_age_seconds: int = DEFAULT_PROMPT_MARKER_MAX_AGE_SECONDS,
) -> NotifyResult:
    skip_reason = skip_cwd_reason(paths, cwd)
    if skip_reason:
        try:
            _runtime_store(paths).delete_prompt_marker(cwd)
        except Exception:
            pass
        return NotifyResult("skipped", skip_reason)

    store = _runtime_store(paths)
    summary = store.get_staged_summary(cwd)
    if summary and _runtime_record_is_fresh(summary, "staged_at", max_age_seconds, now):
        lark.send_summary_card(summary["summary_markdown"], cwd=cwd)
        store.delete_staged_summary(cwd)
        store.delete_prompt_marker(cwd)
        return NotifyResult("summary_sent")
    if summary:
        store.delete_staged_summary(cwd)

    marker = store.get_prompt_marker(cwd)
    if marker is None:
        return NotifyResult("skipped", "summary_missing")
    if not _runtime_record_is_fresh(marker, "marked_at", prompt_marker_max_age_seconds, now):
        store.delete_prompt_marker(cwd)
        return NotifyResult("skipped", "prompt_marker_stale")

    goal_status = goal_status_from_hook_stdin(hook_stdin)
    if goal_status == "active":
        return NotifyResult("skipped", "goal_active")
    if goal_status == "unknown":
        store.delete_prompt_marker(cwd)
        return NotifyResult("skipped", "summary_missing_goal_unknown")

    lark.send_fallback_card(cwd)
    store.delete_prompt_marker(cwd)
    return NotifyResult("fallback_sent", "summary_missing")


def send_away_early_exit_if_needed(
    paths,
    lark,
    cwd: str,
    now: datetime,
    hook_stdin: str | bytes | None = None,
) -> NotifyResult | None:
    store = _runtime_store(paths)
    codex_session_id = (
        _extract_stdin_string_field(hook_stdin, "session_id")
        or _extract_stdin_string_field(hook_stdin, "thread_id")
        or _extract_stdin_string_field(hook_stdin, "turn_id")
    )
    sessions = store.find_active_away_sessions(
        cwd=cwd,
        codex_session_id=codex_session_id,
    )
    if not sessions:
        return None

    for session in sessions:
        window = store.get_window(str(session.get("active_window_id") or ""))
        if not window:
            _record_away_stop_ignored(
                store,
                session=session,
                reason="active_window_missing",
                now=now,
            )
            return NotifyResult("away_active_stop_ignored", "active_window_missing")

        deadline = _parse_iso_time(session.get("deadline_at") or window.get("deadline_at"))
        lease = store.get_waiter_lease(str(session["session_id"]))
        if _lease_is_alive(lease, now):
            _record_away_stop_ignored(
                store,
                session=session,
                reason="waiter_alive",
                now=now,
            )
            return NotifyResult("away_active_stop_ignored", "waiter_alive")

        if deadline and deadline <= _to_utc(now):
            if hasattr(lark, "send_away_timeout_card"):
                lark.send_away_timeout_card(
                    {
                        "project": session.get("project") or "Codex Away Mode",
                        "deadline": deadline,
                    }
                )
            _close_away_for_stop(
                store,
                session=session,
                window=window,
                reason="stale_timeout",
                status="timed_out",
                closed_at=now,
            )
            store.record_diagnostic_event(
                event_kind="away_deadline_closed",
                severity="warning",
                message="Stop hook closed an Away Session whose deadline had passed.",
                detail={
                    "session_id": session.get("session_id"),
                    "window_id": window.get("window_id"),
                    "reason": "stale_timeout",
                },
                created_at=_to_utc(now).isoformat(),
            )
            return NotifyResult("away_deadline_closed", "stale_timeout")

        _record_away_stop_ignored(
            store,
            session=session,
            reason="insufficient_evidence",
            now=now,
        )
        return NotifyResult("away_active_stop_ignored", "insufficient_evidence")
    return None


def capture_hook_payload(
    paths,
    *,
    event_kind: str,
    hook_stdin: str | bytes | None,
    cwd: str,
    now: datetime,
) -> None:
    if not hook_stdin:
        return
    try:
        config = load_config(Path(paths.config_path))
        if not config.capture_hook_payloads:
            return
        record = {
            "captured_at": _to_utc(now).isoformat(),
            "event_kind": event_kind,
            "resolved_cwd": cwd,
            "payload": _redacted_hook_payload(hook_stdin),
        }
        _append_private_jsonl(Path(paths.log_dir) / "hook-payload-samples.jsonl", record)
    except Exception:
        return


def goal_status_from_hook_stdin(hook_stdin: str | bytes | None) -> str:
    transcript_path = _extract_stdin_string_field(hook_stdin, "transcript_path")
    if not transcript_path:
        return "unknown"
    return goal_status_from_transcript(Path(transcript_path))


def goal_status_from_transcript(path) -> str:
    path = Path(path)
    if not path.exists() or not path.is_file():
        return "unknown"

    call_names: dict[str, str] = {}
    latest_status: str | None = None
    try:
        with path.open(encoding="utf-8") as handle:
            for line in handle:
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(record, dict):
                    continue
                payload = record.get("payload")
                if not isinstance(payload, dict) or record.get("type") != "response_item":
                    continue
                item_type = payload.get("type")
                if item_type == "function_call" and payload.get("name") in {
                    "create_goal",
                    "get_goal",
                    "update_goal",
                }:
                    call_id = payload.get("call_id")
                    if isinstance(call_id, str):
                        call_names[call_id] = payload["name"]
                    continue
                if item_type != "function_call_output":
                    continue
                call_id = payload.get("call_id")
                if not isinstance(call_id, str) or call_id not in call_names:
                    continue
                status = _goal_status_from_tool_output(payload.get("output"))
                if status in {"active", "blocked", "complete", "none"}:
                    latest_status = status
    except OSError:
        return "unknown"
    return latest_status or "none"


def skip_cwd_reason(paths, cwd: str | None) -> str | None:
    if not cwd or not os.path.isabs(cwd):
        return "non_user_workspace"
    path = _resolve_path(cwd)
    if path == Path("/"):
        return "non_user_workspace"

    codex_home = _resolve_path(getattr(paths, "codex_home", Path.home() / ".codex"))
    data_dir = _resolve_path(getattr(paths, "data_dir", codex_home / "codex-away-mode"))
    tmp_paths = {
        _resolve_path("/tmp"),
        _resolve_path("/private/tmp"),
        _resolve_path(tempfile.gettempdir()),
    }
    if _is_within(path, codex_home) or _is_within(path, data_dir):
        return "non_user_workspace"
    if any(path == tmp_path or _is_within(path, tmp_path) for tmp_path in tmp_paths):
        return "non_user_workspace"
    return None


def send_test_notification(paths, lark):
    result = lark.send_test_notification()
    chat_id = getattr(result, "chat_id", None)
    if chat_id:
        config = load_config(Path(paths.config_path))
        config.feishu_chat_id = chat_id
        save_config(Path(paths.config_path), config)
    return result


def set_notification_mode(
    paths,
    mode: str,
    *,
    until: datetime | None = None,
) -> AppConfig:
    if mode not in {"all", "off", "snooze"}:
        raise ValueError(f"unsupported notification mode: {mode}")

    config_path = Path(paths.config_path)
    config = load_config(config_path)
    if mode == "snooze":
        if until is None:
            raise ValueError("snooze requires until")
        config.notification_mode = "all"
        config.snooze_until = _to_utc(until).isoformat()
    else:
        config.notification_mode = mode
        config.snooze_until = None
    save_config(config_path, config)
    return config


def effective_notification_mode(paths, now: datetime | None = None) -> str:
    return _config_mode(load_config(Path(paths.config_path)), now=now)


def _close_away_for_stop(
    store: StateStore,
    *,
    session: dict,
    window: dict,
    reason: str,
    status: str,
    closed_at: datetime,
) -> None:
    closed_at_text = _to_utc(closed_at).isoformat()
    store.close_away_session(
        session["session_id"],
        status=status,
        reason=reason,
        closed_at=closed_at_text,
    )
    store.close_active_card(window["window_id"], closed_at=closed_at_text)
    store.close_window(
        window["window_id"],
        status=status,
        reason=reason,
        closed_at=closed_at_text,
    )


def _record_away_stop_ignored(
    store: StateStore,
    *,
    session: dict,
    reason: str,
    now: datetime,
) -> None:
    store.record_diagnostic_event(
        event_kind="away_active_stop_ignored",
        severity="info",
        message="Stop hook ignored an active Away Session.",
        detail={
            "session_id": session.get("session_id"),
            "status": session.get("status"),
            "reason": reason,
        },
        created_at=_to_utc(now).isoformat(),
    )


def _lease_is_alive(lease: dict | None, now: datetime) -> bool:
    if not lease:
        return False
    expires_at = _parse_iso_time(lease.get("expires_at"))
    return bool(expires_at and expires_at > _to_utc(now))


def _parse_iso_time(value) -> datetime | None:
    if not value:
        return None
    try:
        return _to_utc(datetime.fromisoformat(str(value).replace("Z", "+00:00")))
    except ValueError:
        return None


def _runtime_record_is_fresh(
    record: dict,
    timestamp_key: str,
    max_age_seconds: int,
    now: datetime,
) -> bool:
    expires_at = _parse_iso_time(record.get("expires_at"))
    now_utc = _to_utc(now)
    if expires_at is not None:
        return expires_at >= now_utc
    timestamp = _parse_iso_time(record.get(timestamp_key))
    if timestamp is None:
        return False
    age = now_utc.timestamp() - timestamp.timestamp()
    return 0 <= age <= max_age_seconds


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def _extract_stdin_cwd(hook_stdin: str | bytes | None) -> str | None:
    return _extract_stdin_string_field(hook_stdin, "cwd")


def _extract_stdin_string_field(hook_stdin: str | bytes | None, field: str) -> str | None:
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
    return value if isinstance(value, str) else None


def _goal_status_from_tool_output(output) -> str | None:
    if not isinstance(output, str):
        return None
    start = output.find("{")
    if start == -1:
        return None
    try:
        payload = json.loads(output[start:])
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict) or "goal" not in payload:
        return None
    goal = payload.get("goal")
    if goal is None:
        return "none"
    if not isinstance(goal, dict):
        return None
    status = goal.get("status")
    return status if status in {"active", "blocked", "complete"} else None


def _redacted_hook_payload(hook_stdin: str | bytes):
    if isinstance(hook_stdin, bytes):
        hook_text = hook_stdin.decode("utf-8", errors="replace")
    else:
        hook_text = hook_stdin
    try:
        payload = json.loads(hook_text)
    except json.JSONDecodeError:
        return _redacted_string(None, hook_text)
    return _redact_value(payload, key=None, depth=0)


def _redact_value(value, *, key: str | None, depth: int):
    if depth > _MAX_CAPTURE_DEPTH:
        return {"type": type(value).__name__, "truncated": "depth"}
    if isinstance(value, dict):
        return {
            str(child_key): _redact_value(child_value, key=str(child_key), depth=depth + 1)
            for child_key, child_value in value.items()
        }
    if isinstance(value, list):
        return {
            "type": "list",
            "length": len(value),
            "items": [
                _redact_value(item, key=key, depth=depth + 1)
                for item in value[:_MAX_CAPTURE_LIST_ITEMS]
            ],
        }
    if isinstance(value, str):
        return _redacted_string(key, value)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return {"type": type(value).__name__}


def _redacted_string(key: str | None, value: str):
    if key in _SAFE_CAPTURE_VALUE_KEYS and len(value) <= 240 and "\n" not in value:
        return value
    return {
        "type": "string",
        "length": len(value),
        "sha256": hashlib.sha256(value.encode("utf-8")).hexdigest()[:16],
    }


def _append_private_jsonl(path: Path, record: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND
    fd = os.open(str(path), flags, 0o600)
    try:
        with os.fdopen(fd, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    finally:
        os.chmod(path, 0o600)


def _to_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _resolve_path(value) -> Path:
    return Path(value).expanduser().resolve(strict=False)


def _is_within(path: Path, base: Path) -> bool:
    try:
        path.relative_to(base)
    except ValueError:
        return False
    return True
