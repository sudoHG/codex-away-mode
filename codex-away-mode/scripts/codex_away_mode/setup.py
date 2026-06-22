from __future__ import annotations

from pathlib import Path
from typing import Any

from .config import load_config, save_config
from .lark import LarkCli, LarkCliError
from .notify import send_test_notification
from .state import open_install_store


def run_setup_feishu(paths, *, lark=None, device_code: str | None = None) -> dict[str, Any]:
    config = load_config(Path(paths.config_path))
    previous_chat_id = config.feishu_chat_id
    client = lark or LarkCli(config.lark_cli_path)
    store = open_install_store(paths)

    preflight = _call_or_error(client.preflight_auth_commands)
    if not preflight.get("ok"):
        status = store.update_install_status(
            status="lark_auth_command_unverified",
            failed_code="lark_auth_command_unverified",
            next_step="Run lark-cli auth help preflight and update the setup adapter.",
        )
        return {
            "ok": False,
            "failed_code": "lark_auth_command_unverified",
            "user_message": "当前 lark-cli 的授权命令和这个 Skill 预期不一致，不能继续自动配置飞书。",
            "agent_next_step": status["next_step"],
            "detail": preflight,
        }

    init_result = _call_or_error(client.config_init_new)
    if not init_result.get("ok", True):
        status = store.update_install_status(
            status="lark_app_config_missing",
            failed_code="lark_app_config_missing",
            next_step="Finish lark-cli config init --new, then rerun setup feishu.",
        )
        return {
            "ok": False,
            "failed_code": "lark_app_config_missing",
            "user_message": "飞书应用配置还没有完成。",
            "agent_next_step": status["next_step"],
            "detail": init_result,
        }

    auth_payload = (
        _call_or_error(lambda: client.auth_login_complete(device_code))
        if device_code
        else _call_or_error(client.auth_status)
    )
    open_id = extract_open_id(auth_payload)
    if not open_id and not device_code:
        login = _call_or_error(client.auth_login_start)
        verification_url = extract_field(login, "verification_url") or extract_field(login, "verification_uri")
        pending_device_code = extract_field(login, "device_code")
        status = store.update_install_status(
            status="feishu_authorization_pending",
            waiting_for="feishu_authorization",
            next_step="Ask the user to confirm the Feishu authorization URL, then rerun setup feishu with the device code.",
        )
        return {
            "ok": False,
            "status": "feishu_authorization_pending",
            "verification_url": verification_url,
            "device_code": pending_device_code,
            "user_message": "请在打开的飞书授权页面里确认授权。",
            "agent_next_step": status["next_step"],
        }

    if not open_id:
        status = store.update_install_status(
            status="feishu_p2p_binding_unverified",
            failed_code="feishu_p2p_binding_unverified",
            waiting_for="feishu_p2p_binding_preflight",
            next_step="Do not guess open_id/chat_id. Run the Feishu P2P binding preflight or use a documented manual binding step.",
        )
        return {
            "ok": False,
            "failed_code": "feishu_p2p_binding_unverified",
            "user_message": "飞书授权后仍未拿到用户 ID。请先让用户给 bot 发一条私聊，再做 P2P 绑定 preflight；在验证前不要手填 open_id/chat_id。",
            "agent_next_step": status["next_step"],
        }

    config.feishu_user_id = open_id
    save_config(paths.config_path, config)
    result = send_test_notification(paths, client)
    current_chat_id = getattr(result, "chat_id", None)
    if previous_chat_id and current_chat_id and previous_chat_id != current_chat_id:
        updated = load_config(Path(paths.config_path))
        updated.route_key_verified = False
        save_config(paths.config_path, updated)
        store.set_route_key_state(
            status="unknown",
            source="feishu_binding_changed",
        )
    store.update_install_status(
        status="feishu_binding_verified",
        next_step="Write managed hooks, ask the user to trust them, then run doctor --e2e-notify.",
    )
    return {
        "ok": True,
        "status": "feishu_binding_verified",
        "feishu_chat_id": getattr(result, "chat_id", None),
        "message_id": getattr(result, "message_id", None),
        "next_step": "Trust hooks, then run codex-away-mode doctor --e2e-notify --json.",
    }


def extract_open_id(payload: dict[str, Any]) -> str | None:
    for key in ("user_open_id", "open_id", "feishu_user_id"):
        value = extract_field(payload, key)
        if value:
            return value
    return None


def extract_field(value: Any, key: str) -> str | None:
    if isinstance(value, dict):
        candidate = value.get(key)
        if isinstance(candidate, str) and candidate:
            return candidate
        for child in value.values():
            found = extract_field(child, key)
            if found:
                return found
    if isinstance(value, list):
        for child in value:
            found = extract_field(child, key)
            if found:
                return found
    return None


def _call_or_error(fn):
    try:
        return fn()
    except LarkCliError as exc:
        return {"ok": False, "error": str(exc)}
