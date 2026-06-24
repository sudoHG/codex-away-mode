from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .config import load_config, save_config
from .lark import LarkCli, LarkCliError
from .notify import send_test_notification
from .state import open_install_store


def run_setup_feishu(
    paths,
    *,
    lark=None,
    device_code: str | None = None,
    restart_auth: bool = False,
) -> dict[str, Any]:
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

    app_config = _call_or_error(client.app_config_status)
    if not app_config.get("ok"):
        if app_config.get("failed_code") == "lark_app_config_missing":
            init_result = _call_or_error(client.start_app_config_init)
            if init_result.get("ok") and init_result.get("configured"):
                app_config = init_result
            elif init_result.get("status") == "lark_app_config_browser_pending":
                status = store.update_install_status(
                    status="lark_app_config_browser_pending",
                    waiting_for="feishu_browser_confirmation",
                    next_step="Wait for the user to confirm in browser, then rerun setup feishu --json.",
                )
                return {
                    "ok": False,
                    "status": "lark_app_config_browser_pending",
                    "waiting_for": "feishu_browser_confirmation",
                    "verification_url": init_result.get("verification_url"),
                    "browser_opened": bool(init_result.get("browser_opened")),
                    "process_id": init_result.get("process_id"),
                    "debug_command": init_result.get("debug_command"),
                    "developer_detail": init_result.get("developer_detail"),
                    "user_message": _browser_confirm_message(
                        bool(init_result.get("browser_opened"))
                    ),
                    "agent_next_step": status["next_step"],
                }
            else:
                failed_code = init_result.get("failed_code", "lark_app_config_init_failed")
                status = store.update_install_status(
                    status=init_result.get("status", "lark_app_config_failed"),
                    failed_code=failed_code,
                    waiting_for="feishu_browser_confirmation",
                    next_step=init_result.get(
                        "agent_next_step",
                        "Inspect developer_detail and retry setup feishu.",
                    ),
                )
                return {
                    "ok": False,
                    "status": init_result.get("status", "lark_app_config_failed"),
                    "failed_code": failed_code,
                    "user_message": init_result.get(
                        "user_message",
                        "飞书官方配置流程启动失败，当前还不能继续安装飞书通知。",
                    ),
                    "agent_next_step": status["next_step"],
                    "debug_command": init_result.get("debug_command"),
                    "developer_detail": init_result.get("developer_detail"),
                }
        else:
            config_command = app_config.get("config_command") or [
                config.lark_cli_path,
                "config",
                "init",
                "--new",
            ]
            status = store.update_install_status(
                status="lark_app_config_pending",
                failed_code=app_config.get("failed_code", "lark_app_config_missing"),
                waiting_for="lark_app_config",
                next_step=(
                    "Inspect the lark-cli app config status. Do not ask non-technical users "
                    "to run config commands unless using the advanced recovery path."
                ),
            )
            return {
                "ok": False,
                "status": "lark_app_config_pending",
                "failed_code": app_config.get("failed_code", "lark_app_config_missing"),
                "debug_command": config_command,
                "user_message": "飞书应用配置状态无法自动确认，当前还不能继续安装飞书通知。",
                "agent_next_step": status["next_step"],
                "detail": app_config,
            }
    auth_payload = (
        _call_or_error(lambda: client.auth_login_complete(device_code))
        if device_code
        else _call_or_error(client.auth_status)
    )
    open_id = extract_open_id(auth_payload)
    if open_id:
        store.clear_feishu_auth_pending()
        return _finalize_feishu_binding(
            paths,
            config=config,
            previous_chat_id=previous_chat_id,
            client=client,
            store=store,
            open_id=open_id,
        )

    if not open_id:
        if not device_code:
            pending_result = _continue_or_start_auth(
                store=store,
                client=client,
                restart_auth=restart_auth,
            )
            pending_open_id = extract_open_id(pending_result.get("auth_payload", {}))
            if pending_open_id:
                store.clear_feishu_auth_pending()
                return _finalize_feishu_binding(
                    paths,
                    config=config,
                    previous_chat_id=previous_chat_id,
                    client=client,
                    store=store,
                    open_id=pending_open_id,
                )
            status = store.update_install_status(
                status="feishu_authorization_pending",
                waiting_for="feishu_authorization",
                next_step=(
                    "Wait for the user to confirm the current Feishu authorization page, "
                    "then rerun setup feishu --json. Do not start a new authorization flow."
                ),
            )
            pending_response = {
                key: value
                for key, value in pending_result.items()
                if key != "auth_payload"
            }
            return {
                "ok": False,
                "status": "feishu_authorization_pending",
                "waiting_for": "feishu_authorization",
                "user_message": pending_response.pop("user_message"),
                "agent_next_step": status["next_step"],
                **pending_response,
            }
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

def _finalize_feishu_binding(
    paths,
    *,
    config,
    previous_chat_id,
    client,
    store,
    open_id: str,
) -> dict[str, Any]:
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


def _continue_or_start_auth(*, store, client, restart_auth: bool) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    restart_reason = None
    if restart_auth:
        store.clear_feishu_auth_pending()
    else:
        pending = store.feishu_auth_pending()
        if pending and not _pending_auth_is_expired(pending, now):
            complete = _call_or_error(
                lambda: client.auth_login_complete(str(pending["device_code"]))
            )
            return _pending_auth_response(
                pending,
                reused_pending=True,
                auth_payload=complete,
                user_message="飞书授权还在等待确认。请在当前打开的授权页面里确认；完成后告诉我继续。",
            )
        if pending:
            restart_reason = "expired"
            store.clear_feishu_auth_pending()

    login = _call_or_error(client.auth_login_start)
    pending = _make_pending_auth_payload(login, now)
    store.set_feishu_auth_pending(pending)
    browser_opened = bool(pending.get("browser_opened"))
    return _pending_auth_response(
        pending,
        reused_pending=False,
        auth_payload=login,
        user_message=_oauth_confirm_message(
            browser_opened=browser_opened,
            restarted=restart_auth or restart_reason == "expired",
        ),
        restart_reason=restart_reason,
    )


def _make_pending_auth_payload(login: dict[str, Any], now: datetime) -> dict[str, Any]:
    verification_url = (
        extract_field(login, "verification_url")
        or extract_field(login, "verification_uri_complete")
        or extract_field(login, "verification_uri")
    )
    device_code = extract_field(login, "device_code")
    user_code = extract_field(login, "user_code")
    expires_in = _extract_int(login, "expires_in") or 600
    if not device_code:
        raise LarkCliError("lark-cli did not return a Feishu device_code")
    started_at = _to_utc(now)
    return {
        "status": "pending",
        "device_code": device_code,
        "verification_url": verification_url,
        "user_code": user_code,
        "browser_opened": _extract_bool(login, "browser_opened"),
        "started_at": started_at.isoformat(),
        "expires_at": (started_at + timedelta(seconds=expires_in)).isoformat(),
        "last_poll_at": None,
        "attempt_count": 0,
    }


def _pending_auth_response(
    pending: dict[str, Any],
    *,
    reused_pending: bool,
    auth_payload: dict[str, Any],
    user_message: str,
    restart_reason: str | None = None,
) -> dict[str, Any]:
    payload = {
        "verification_url": pending.get("verification_url"),
        "user_code": pending.get("user_code"),
        "browser_opened": False if reused_pending else bool(pending.get("browser_opened")),
        "reused_pending": reused_pending,
        "user_message": user_message,
        "auth_payload": auth_payload,
    }
    if restart_reason:
        payload["restart_reason"] = restart_reason
    return payload


def _pending_auth_is_expired(pending: dict[str, Any], now: datetime) -> bool:
    expires_at = pending.get("expires_at")
    if not expires_at:
        return False
    try:
        expires = datetime.fromisoformat(str(expires_at).replace("Z", "+00:00"))
    except ValueError:
        return True
    return _to_utc(expires) <= _to_utc(now)


def extract_open_id(payload: dict[str, Any]) -> str | None:
    user_identity = _find_identity(payload, "user")
    if user_identity:
        value = _extract_direct_field(
            user_identity,
            ("openId", "userOpenId", "open_id", "user_open_id", "feishu_user_id"),
        )
        if value:
            return value

    for key in ("user_open_id", "userOpenId", "open_id", "feishu_user_id"):
        value = extract_field(payload, key)
        if value:
            return value
    for container in (payload, payload.get("data") if isinstance(payload, dict) else None):
        if isinstance(container, dict):
            value = _extract_direct_field(container, ("openId",))
            if value:
                return value
    return None


def _find_identity(value: Any, identity: str) -> dict[str, Any] | None:
    if isinstance(value, dict):
        identities = value.get("identities")
        if isinstance(identities, dict) and isinstance(identities.get(identity), dict):
            return identities[identity]
        for child in value.values():
            found = _find_identity(child, identity)
            if found:
                return found
    if isinstance(value, list):
        for child in value:
            found = _find_identity(child, identity)
            if found:
                return found
    return None


def _extract_direct_field(value: dict[str, Any], keys: tuple[str, ...]) -> str | None:
    for key in keys:
        candidate = value.get(key)
        if isinstance(candidate, str) and candidate:
            return candidate
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


def _extract_int(value: Any, key: str) -> int | None:
    if isinstance(value, dict):
        candidate = value.get(key)
        if isinstance(candidate, int):
            return candidate
        if isinstance(candidate, str) and candidate.isdigit():
            return int(candidate)
        for child in value.values():
            found = _extract_int(child, key)
            if found is not None:
                return found
    if isinstance(value, list):
        for child in value:
            found = _extract_int(child, key)
            if found is not None:
                return found
    return None


def _extract_bool(value: Any, key: str) -> bool:
    if isinstance(value, dict):
        candidate = value.get(key)
        if isinstance(candidate, bool):
            return candidate
        for child in value.values():
            if _extract_bool(child, key):
                return True
    if isinstance(value, list):
        for child in value:
            if _extract_bool(child, key):
                return True
    return False


def _to_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _call_or_error(fn):
    try:
        return fn()
    except LarkCliError as exc:
        return {"ok": False, "error": str(exc)}


def _browser_confirm_message(browser_opened: bool) -> str:
    if browser_opened:
        return "我已经打开飞书配置页面。请在浏览器里确认，完成后告诉我继续。"
    return "浏览器没有自动打开。请点击返回的飞书配置链接完成确认，完成后告诉我继续。"


def _oauth_confirm_message(*, browser_opened: bool, restarted: bool) -> str:
    if browser_opened:
        if restarted:
            return "上一轮飞书授权已超时。我已经打开新的授权页面，请在浏览器里确认。"
        return "我已经打开飞书授权页面。请在浏览器里确认授权，完成后告诉我继续。"
    if restarted:
        return "上一轮飞书授权已超时。浏览器没有自动打开，请点击返回的飞书授权链接完成确认。"
    return "浏览器没有自动打开。请点击返回的飞书授权链接完成确认，完成后告诉我继续。"
