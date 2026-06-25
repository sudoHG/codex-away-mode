from __future__ import annotations

import json
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


MANAGED_STATUS_MESSAGE = "Codex Away Mode managed hook"
GUIDANCE_START = "<!-- BEGIN CODEX AWAY MODE -->"
GUIDANCE_END = "<!-- END CODEX AWAY MODE -->"


def managed_stop_command(cli_command: str) -> str:
    return f"{cli_command} notify stop --json"


def managed_user_prompt_command(cli_command: str) -> str:
    return f"{cli_command} notify mark-prompt --json"


def managed_permission_request_command(cli_command: str) -> str:
    return f"{cli_command} notify permission-request --hook-json"


def install_guidance_block(content: str, *, cli_command: str = "codex-away-mode") -> str:
    block = (
        f"{GUIDANCE_START}\n"
        "## Codex Away Mode\n\n"
        f"Before a user-visible completed turn, stage the completion summary by running `{cli_command} notify stage-summary --cwd \"$PWD\" --session-id \"${{CODEX_THREAD_ID:-}}\" --json` and passing the summary markdown on stdin.\n"
        "Do not write Codex Away Mode summary, marker, state, or runtime files under the current workspace cwd.\n"
        "Do not write this summary for in-progress goal-mode continuation turns; wait until the goal is complete, blocked, or needs human attention.\n"
        "Stop hook also suppresses completion notifications when the transcript goal status is active.\n"
        "If the summary is missing, the Stop hook may send a missing summary fallback only when the session has a user prompt marker and the transcript goal status is not active.\n"
        "Away Mode wait contract: after sending an Away Mode checkpoint, wait for the configured reply window and only continue from a routed card reply.\n"
        f"When the user asks to start Away Mode or says they are leaving the computer, use `{cli_command} away start ... --json`. Do not run doctor, route probes, or away status first.\n"
        "During active Away Mode polling, keep the Codex chat quiet: do not send heartbeat or waiting-status updates; only send user-visible updates when a routed card reply arrives, the wait ends, times out, errors, or requires user action.\n"
        "When a routed card reply arrives, write a concise Codex chat note that includes the received Feishu text and the action or answer you are about to give.\n"
        "After completing that reply_text work, write the result or answer in the Codex chat before calling away resume, so the desktop thread has an auditable trace of the remote interaction.\n"
        f"When `away start` or `away resume` returns `status=reply` with `keep_waiting=true` and `resume_token`, treat `reply_text` as the next user prompt, complete that work, then call `{cli_command} away resume \"$away_session_id\" --resume-token \"$resume_token\"` with updated progress fields unless the user ended the session or the task is stopping.\n"
        "If the user asks in natural language to extend the current Away Mode wait, convert the duration to minutes and pass `--extend-minutes <minutes>` on the next `away resume` call; do not read or write Away Mode SQLite/StateStore directly.\n"
        "Never call `away wait --resume <away_session_id>` or `away resume <away_session_id>` without a resume token. Never resume a session discovered from `away status`.\n"
        "Do not claim the turn is complete while an Away Session is still active.\n"
        "If Codex asks for approval while you are away, the PermissionRequest hook may send a Feishu reminder; the user must still approve or reject in Codex Desktop.\n"
        f"{GUIDANCE_END}"
    )
    stripped = _remove_guidance_block(content).rstrip()
    if not stripped:
        return block + "\n"
    return stripped + "\n\n" + block + "\n"


def install_hooks(*, hooks_path, backup_dir, cli_command: str) -> dict[str, Any]:
    hooks_path = Path(hooks_path)
    data = _read_hooks(hooks_path)
    if hooks_path.exists():
        _backup(hooks_path, backup_dir)

    hooks_root = data.setdefault("hooks", {})
    _remove_managed_entries(hooks_root, "Stop")
    _ensure_managed_entry(
        hooks_root,
        "Stop",
        managed_stop_command(cli_command),
        timeout=30,
    )
    _remove_managed_entries(hooks_root, "UserPromptSubmit")
    _ensure_managed_entry(
        hooks_root,
        "UserPromptSubmit",
        managed_user_prompt_command(cli_command),
        timeout=10,
    )
    _remove_managed_entries(hooks_root, "PermissionRequest")
    _ensure_managed_entry(
        hooks_root,
        "PermissionRequest",
        managed_permission_request_command(cli_command),
        timeout=10,
    )
    _write_hooks(hooks_path, data)
    return data


def uninstall_hooks(*, hooks_path, backup_dir) -> dict[str, Any]:
    hooks_path = Path(hooks_path)
    data = _read_hooks(hooks_path)
    if hooks_path.exists():
        _backup(hooks_path, backup_dir)

    hooks_root = data.setdefault("hooks", {})
    for event in ("Stop", "UserPromptSubmit", "PermissionRequest"):
        _remove_managed_entries(hooks_root, event)
    _write_hooks(hooks_path, data)
    return data


def _ensure_managed_entry(
    hooks_root: dict[str, Any],
    event: str,
    command: str,
    *,
    timeout: int,
) -> None:
    groups = hooks_root.setdefault(event, [])
    for group in groups:
        group.pop("matcher", None)
        for hook in group.get("hooks", []):
            if hook.get("command") == command and _is_managed_hook(hook):
                return

    groups.append(
        {
            "hooks": [
                {
                    "type": "command",
                    "command": command,
                    "timeout": timeout,
                    "statusMessage": MANAGED_STATUS_MESSAGE,
                }
            ]
        }
    )


def _is_managed_hook(hook: dict[str, Any]) -> bool:
    command = hook.get("command", "")
    return (
        hook.get("statusMessage") == MANAGED_STATUS_MESSAGE
        or command.endswith(" notify stop --json")
        or command.endswith(" notify mark-prompt --json")
        or command.endswith(" notify permission-request --hook-json")
    )


def _remove_managed_entries(hooks_root: dict[str, Any], event: str) -> None:
    groups = hooks_root.get(event, [])
    kept_groups = []
    for group in groups:
        hooks = [
            hook
            for hook in group.get("hooks", [])
            if not _is_managed_hook(hook)
        ]
        if hooks:
            new_group = dict(group)
            new_group["hooks"] = hooks
            kept_groups.append(new_group)
    hooks_root[event] = kept_groups


def _read_hooks(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"hooks": {}}
    return json.loads(path.read_text(encoding="utf-8"))


def _write_hooks(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _backup(path: Path, backup_dir) -> Path:
    backup_dir = Path(backup_dir)
    backup_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    backup_path = backup_dir / f"{path.name}.{timestamp}.bak"
    shutil.copy2(path, backup_path)
    return backup_path


def _remove_guidance_block(content: str) -> str:
    start = content.find(GUIDANCE_START)
    end = content.find(GUIDANCE_END)
    if start == -1 or end == -1 or end < start:
        return content
    return content[:start] + content[end + len(GUIDANCE_END) :]
