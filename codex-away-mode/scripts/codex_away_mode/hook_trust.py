from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any


MANAGED_STATUS_MESSAGE = "Codex Away Mode managed hook"
REQUIRED_EVENTS = {
    "UserPromptSubmit": "user_prompt_submit",
    "Stop": "stop",
    "PermissionRequest": "permission_request",
}


def evaluate_hook_trust(paths) -> dict[str, Any]:
    hooks_result = inspect_managed_hooks(paths)
    if not hooks_result["ok"]:
        return hooks_result

    state_result = load_codex_hook_state(_codex_config_path(paths))
    if state_result["status"] != "loaded":
        return {
            "ok": False,
            "status": state_result["status"],
            "warning_code": state_result["code"],
            "hooks": hooks_result["hooks"],
            "next_step": _unknown_format_next_step(),
        }

    hooks: dict[str, Any] = hooks_result["hooks"]
    entries: dict[str, dict[str, Any]] = state_result["entries"]
    missing: list[str] = []
    disabled: list[str] = []
    trusted: dict[str, Any] = {}

    for event, event_key in REQUIRED_EVENTS.items():
        hook = hooks[event_key]
        trust_key = _trust_key(paths, event_key, hook["group_index"], hook["hook_index"])
        entry = entries.get(trust_key)
        if entry is None:
            missing.append(event_key)
            trusted[event_key] = {"status": "missing", "trust_key": trust_key}
            continue
        if entry.get("enabled") is False:
            disabled.append(event_key)
            trusted[event_key] = {"status": "disabled", "trust_key": trust_key}
            continue
        if event_key == "permission_request" and entry.get("trusted_hash"):
            trusted[event_key] = {"status": "trust_record_present", "trust_key": trust_key}
            continue
        if entry.get("enabled") is not True:
            missing.append(event_key)
            trusted[event_key] = {"status": "missing_enabled", "trust_key": trust_key}
            continue
        trusted[event_key] = {"status": "trusted", "trust_key": trust_key}

    if disabled:
        return {
            "ok": False,
            "status": "disabled",
            "failed_code": "hook_trust_disabled",
            "hooks": trusted,
            "next_step": (
                "Codex Away Mode 的 Hook 当前在 Codex Desktop 里被关闭。请打开 "
                "Codex Desktop Settings -> Hooks，重新信任 Codex Away Mode 的 Stop、"
                "UserPromptSubmit 和 PermissionRequest Hook，然后重新运行 codex-away-mode doctor --json。"
            ),
        }
    if missing:
        return {
            "ok": False,
            "status": "missing",
            "failed_code": "hook_trust_missing",
            "hooks": trusted,
            "next_step": (
                "Hook 已安装，但还没有在 Codex Desktop 中找到对应的信任记录。请打开 "
                "Codex Desktop Settings -> Hooks，信任 Codex Away Mode Hook，然后重新运行 "
                "codex-away-mode doctor --json。"
            ),
        }

    return {
        "ok": True,
        "status": "verified",
        "passed_code": "hook_trust_verified",
        "hooks": trusted,
        "next_step": "No immediate action required.",
    }


def inspect_managed_hooks(paths) -> dict[str, Any]:
    hooks_path = Path(paths.hooks_json)
    try:
        payload = json.loads(hooks_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return _hook_structure_failure("hooks_missing", "Run codex-away-mode install --yes --json to write managed hooks.")
    except (OSError, json.JSONDecodeError):
        return _hook_structure_failure("hooks_unreadable", "Codex hooks.json is unreadable. Re-run codex-away-mode install --yes --json.")

    hooks_root = payload.get("hooks", {}) if isinstance(payload, dict) else {}
    if not isinstance(hooks_root, dict):
        return _hook_structure_failure("hooks_missing", "Run codex-away-mode install --yes --json to write managed hooks.")

    result: dict[str, Any] = {}
    for event, event_key in REQUIRED_EVENTS.items():
        matches = _managed_hooks_for_event(hooks_root, event)
        if not matches:
            return _hook_structure_failure("hooks_missing", "Run codex-away-mode install --yes --json to write managed hooks.")
        if len(matches) > 1:
            return _hook_structure_failure(
                "hooks_duplicate",
                "检测到多个 Codex Away Mode managed Hook。请重新运行 codex-away-mode install --yes --json 清理旧 Hook，然后重新信任。",
            )
        result[event_key] = matches[0]

    return {"ok": True, "status": "present", "hooks": result}


def load_codex_hook_state(path: str | Path) -> dict[str, Any]:
    path = Path(path)
    if not path.exists():
        return {"status": "loaded", "entries": {}}
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return {"status": "unreadable", "code": "hook_trust_state_unreadable"}

    entries: dict[str, dict[str, Any]] = {}
    current_key: str | None = None
    saw_hooks_state = False
    saw_unknown_hooks_state_shape = False
    table_pattern = re.compile(r'^\[hooks\.state\."(.+)"\]\s*$')

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("["):
            match = table_pattern.match(line)
            if match:
                saw_hooks_state = True
                current_key = match.group(1)
                entries.setdefault(current_key, {})
                continue
            current_key = None
            if line.startswith("[hooks.state"):
                saw_hooks_state = True
                saw_unknown_hooks_state_shape = True
            continue
        if current_key is None or "=" not in line:
            continue
        key, value = [part.strip() for part in line.split("=", 1)]
        if key == "trusted_hash":
            entries[current_key]["trusted_hash"] = _parse_string(value)
        elif key == "enabled":
            entries[current_key]["enabled"] = _parse_bool(value)

    if saw_hooks_state and saw_unknown_hooks_state_shape and not entries:
        return {"status": "unknown_format", "code": "hook_trust_state_unknown_format"}
    return {"status": "loaded", "entries": entries}


def _hook_structure_failure(code: str, next_step: str) -> dict[str, Any]:
    return {
        "ok": False,
        "status": "failed",
        "failed_code": code,
        "hooks": {},
        "next_step": next_step,
    }


def _managed_hooks_for_event(hooks_root: dict[str, Any], event: str) -> list[dict[str, Any]]:
    matches: list[dict[str, Any]] = []
    groups = hooks_root.get(event, [])
    if not isinstance(groups, list):
        return matches
    for group_index, group in enumerate(groups):
        if not isinstance(group, dict):
            continue
        hooks = group.get("hooks", [])
        if not isinstance(hooks, list):
            continue
        for hook_index, hook in enumerate(hooks):
            if isinstance(hook, dict) and _is_managed_hook(event, hook):
                matches.append(
                    {
                        "event": event,
                        "group_index": group_index,
                        "hook_index": hook_index,
                        "command": str(hook.get("command", "")),
                    }
                )
    return matches


def _is_managed_hook(event: str, hook: dict[str, Any]) -> bool:
    command = str(hook.get("command", ""))
    if event == "Stop" and "notify stop --json" not in command:
        return False
    if event == "UserPromptSubmit" and "notify mark-prompt --json" not in command:
        return False
    if event == "PermissionRequest" and "notify permission-request --hook-json" not in command:
        return False
    return "codex-away-mode" in command or hook.get("statusMessage") == MANAGED_STATUS_MESSAGE


def _trust_key(paths, event_key: str, group_index: int, hook_index: int) -> str:
    return f"{Path(paths.hooks_json).resolve()}:{event_key}:{group_index}:{hook_index}"


def _codex_config_path(paths) -> Path:
    return Path(getattr(paths, "codex_config_path", Path(paths.codex_home) / "config.toml"))


def _parse_bool(value: str) -> bool | None:
    lowered = value.lower()
    if lowered == "true":
        return True
    if lowered == "false":
        return False
    return None


def _parse_string(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] == '"':
        return value[1:-1]
    return value


def _unknown_format_next_step() -> str:
    return (
        "当前 Codex 版本的 Hook 信任状态格式无法静态读取。如果飞书完成通知能正常收到，"
        "可以继续使用；如果收不到，请打开 Codex Desktop Settings -> Hooks 重新信任 "
        "Codex Away Mode Hook。"
    )
