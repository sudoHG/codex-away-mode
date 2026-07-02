from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from . import cards, hook_trust, notify
from .config import AppConfig, load_config, save_config
from .lark import LarkCli, LarkCliError
from .state import StateStore, open_install_store
from .time import SystemClock


def run_doctor(
    paths,
    *,
    route_probe: bool = False,
    e2e_notify: bool = False,
    e2e_approval_urgent: bool = False,
    lark=None,
    clock=None,
    cwd: str | None = None,
    probe_timeout_seconds: float = 60,
    poll_interval_seconds: float | None = None,
) -> dict[str, Any]:
    report = _new_report()
    doctor_clock = clock or SystemClock()
    _warn_legacy_workspace_artifacts(report, cwd)
    config_path = Path(paths.config_path)
    config: AppConfig | None = None

    if not config_path.exists():
        _fail(
            report,
            "local_config_missing",
            "Run codex-away-mode install --yes --json to create local config.",
        )
        return _finalize(report)

    config = load_config(config_path)
    report["passed_codes"].append("local_config")

    try:
        store = open_install_store(paths)
    except Exception as exc:  # pragma: no cover - defensive diagnostic path
        _fail(
            report,
            "sqlite",
            "Codex Away Mode install state is not writable or readable. Check CODEX_AWAY_HOME or ~/.codex-away-mode permissions, then rerun doctor.",
        )
        report["warnings"].append(f"SQLite state check failed: {exc}")
        return _finalize(report)
    else:
        report["passed_codes"].append("sqlite")
    _warn_runtime_stale_sessions(report, paths, doctor_clock)

    if not config.feishu_chat_id:
        _fail(
            report,
            "feishu_chat_id_missing",
            "Run codex-away-mode setup feishu --json to send a test notification and persist feishu_chat_id.",
        )
        return _finalize(report)
    report["passed_codes"].append("feishu_chat_id")

    if route_probe and config.feishu_chat_id and "sqlite" in report["passed_codes"]:
        if config.route_key_verified:
            _merge_probe_result(report, _route_probe_already_verified_result())
            if not report["next_step"]:
                report["next_step"] = "No immediate action required."
            return _finalize(report)
        probe_lark = lark or LarkCli(config.lark_cli_path)
        probe_clock = doctor_clock
        interval = poll_interval_seconds
        if interval is None:
            interval = max(float(config.poll_interval_seconds), 0.1)
        probe_result = run_route_probe(
            paths,
            config=config,
            lark=probe_lark,
            clock=probe_clock,
            timeout_seconds=probe_timeout_seconds,
            poll_interval_seconds=interval,
        )
        _merge_probe_result(report, probe_result)
        if not report["next_step"]:
            report["next_step"] = "No immediate action required."
        return _finalize(report)

    if e2e_notify:
        e2e_result = run_e2e_notify(
            paths,
            config=config,
            lark=lark or LarkCli(config.lark_cli_path),
            clock=clock or SystemClock(),
            cwd=cwd,
        )
        if e2e_result["ok"]:
            report["passed_codes"].append("notify_delivery_verified")
        else:
            _fail(report, e2e_result["failed_code"], e2e_result["agent_next_step"])
        if not report["next_step"]:
            report["next_step"] = e2e_result.get("next_step", "No immediate action required.")
        return _finalize(report)

    if e2e_approval_urgent:
        urgent_result = run_e2e_approval_urgent(
            paths,
            config=config,
            lark=lark or LarkCli(config.lark_cli_path),
            clock=clock or SystemClock(),
        )
        if urgent_result["ok"]:
            report["passed_codes"].append("approval_urgent_verified")
        else:
            _fail(report, urgent_result["failed_code"], urgent_result["agent_next_step"])
        if not report["next_step"]:
            report["next_step"] = urgent_result.get("next_step", "No immediate action required.")
        return _finalize(report)

    hooks_result = hook_trust.inspect_managed_hooks(paths)
    if not hooks_result["ok"]:
        _fail(
            report,
            hooks_result["failed_code"],
            hooks_result["next_step"],
        )
        return _finalize(report)
    report["passed_codes"].append("hooks")

    fingerprint = hooks_fingerprint(paths)
    e2e_state = store.get_install_state("e2e_notify", {})
    if e2e_state.get("status") == "verified":
        if e2e_state.get("hooks_fingerprint") == fingerprint:
            report["passed_codes"].append("notify_delivery_verified")
            last_invocation = _last_stop_hook_invocation(paths, hooks_fingerprint=fingerprint)
            if last_invocation:
                report.setdefault("diagnostics", {})["last_hook_execution_observed"] = {
                    "stop": last_invocation.get("created_at")
                }
            trust_result = hook_trust.evaluate_hook_trust(paths)
            report["hook_trust"] = trust_result.get("hooks", {})
            if trust_result["ok"]:
                runtime_trust = _runtime_hook_trust_observation(
                    paths,
                    hooks=report["hook_trust"],
                    hooks_fingerprint=fingerprint,
                )
                if runtime_trust is not None:
                    report["hook_trust"] = runtime_trust["hooks"]
                    report.setdefault("diagnostics", {})["last_hook_execution_observed"] = runtime_trust[
                        "observed"
                    ]
                report["passed_codes"].append(trust_result["passed_code"])
                store.update_install_status(
                    status="installed",
                    next_step="Installation is verified.",
                )
                _append_approval_urgent_status(report, paths=paths, config=config, store=store)
                if not report["next_step"]:
                    report["next_step"] = "No immediate action required."
                return _finalize(report)
            if trust_result.get("failed_code"):
                runtime_trust = _runtime_hook_trust_override(
                    paths,
                    trust_result=trust_result,
                    hooks_fingerprint=fingerprint,
                )
                if runtime_trust is not None:
                    report["hook_trust"] = runtime_trust["hooks"]
                    report.setdefault("diagnostics", {})["last_hook_execution_observed"] = runtime_trust[
                        "observed"
                    ]
                    report["passed_codes"].append("hook_trust_verified")
                    store.update_install_status(
                        status="installed",
                        next_step="Installation is verified.",
                    )
                    _append_approval_urgent_status(report, paths=paths, config=config, store=store)
                    if not report["next_step"]:
                        report["next_step"] = "No immediate action required."
                    return _finalize(report)
                _fail(report, trust_result["failed_code"], trust_result["next_step"])
                return _finalize(report)
            warning_code = trust_result.get("warning_code")
            if warning_code:
                report["warnings"].append(warning_code)
                if last_invocation:
                    if not report["next_step"]:
                        report["next_step"] = trust_result["next_step"]
                    return _finalize(report)
                _degrade(report, warning_code, trust_result["next_step"])
                return _finalize(report)
            _degrade(
                report,
                "hook_trust_unverified",
                trust_result.get(
                    "next_step",
                    "请在 Codex Desktop 设置 -> 钩子（英文界面为 Settings -> Hooks）中信任 Codex Away Mode Hook。",
                ),
            )
            return _finalize(report)
        _degrade(
            report,
            "notify_delivery_stale",
            "Managed hooks changed after the last notification-delivery verification. Run codex-away-mode doctor --e2e-notify --json again.",
        )
        return _finalize(report)

    _degrade(
        report,
        "notify_delivery_unverified",
        "Hook 已写入；请先运行 codex-away-mode doctor --e2e-notify --json 验证通知发送链路。",
    )
    return _finalize(report)

    if not report["next_step"]:
        report["next_step"] = "No immediate action required."
    return _finalize(report)


def run_e2e_notify(
    paths,
    *,
    config: AppConfig,
    lark,
    clock,
    cwd: str | None,
) -> dict[str, Any]:
    cwd = cwd or str(Path.cwd())
    wrapper = _wrapper_path(paths)
    if not wrapper.exists():
        return {
            "ok": False,
            "failed_code": "cli_entry_missing",
            "agent_next_step": "Run codex-away-mode install --yes --json to create the managed wrapper.",
        }

    now = clock.now()
    summary = (
        "**项目**\n"
        "Codex Away Mode E2E\n\n"
        "**工作目录**\n"
        f"{cwd}\n\n"
        "**任务**\n"
        "安装端到端通知验证\n\n"
        "**完成**\n"
        "测试 Stop hook 通知链路。\n"
    )
    notify.mark_prompt(paths, cwd=cwd, now=now)
    notify.stage_summary(paths, cwd=cwd, summary_markdown=summary, now=now)
    client = _E2ENotificationClient(lark=lark, chat_id=config.feishu_chat_id)
    result = notify.send_completion_from_summary(
        paths,
        client,
        cwd=cwd,
        now=now,
    )
    if result.status != "summary_sent":
        return {
            "ok": False,
            "failed_code": "e2e_notify_unverified",
            "agent_next_step": f"Expected summary_sent, got {result.status}. Check summary path, cwd, and Feishu binding.",
        }
    open_install_store(paths).set_install_state(
        "e2e_notify",
        {
            "status": "verified",
            "scope": "notify_delivery_only",
            "verified_at": now.astimezone(timezone.utc).isoformat(),
            "cwd": cwd,
            "summary_key": StateStore.cwd_hash(cwd),
            "message_id": getattr(client.last_result, "message_id", None),
            "hooks_fingerprint": hooks_fingerprint(paths),
        },
    )
    return {
        "ok": True,
        "status": "notify_delivery_verified",
        "next_step": (
            "通知投递链已验证。请确认 Codex Desktop 设置 -> 钩子"
            "（英文界面为 Settings -> Hooks）中已信任 Codex Away Mode Hook，"
            "然后运行 codex-away-mode doctor --json 检查当前 Hook 信任状态。"
        ),
    }


def run_e2e_approval_urgent(
    paths,
    *,
    config: AppConfig,
    lark,
    clock,
) -> dict[str, Any]:
    if not getattr(config, "approval_notifications_enabled", True) or not getattr(
        config, "approval_notifications_urgent_app_enabled", True
    ):
        return {
            "ok": False,
            "failed_code": "approval_urgent_disabled",
            "agent_next_step": "审批提醒或飞书应用内加急已关闭；如需验证，请先开启后再运行 doctor --e2e-approval-urgent --json。",
        }
    if not config.feishu_chat_id:
        return {
            "ok": False,
            "failed_code": "feishu_chat_id_missing",
            "agent_next_step": "请先运行 setup feishu 绑定 Bot 私聊会话，再验证审批加急。",
        }
    if not config.feishu_user_id:
        return {
            "ok": False,
            "failed_code": "feishu_user_id_missing",
            "agent_next_step": "请先运行 setup feishu 绑定飞书用户 open_id，再验证审批加急。",
        }
    try:
        preflight = getattr(lark, "preflight_urgent_app_command", lambda: {"ok": True})()
    except LarkCliError:
        return {
            "ok": False,
            "failed_code": "approval_urgent_command_unverified",
            "agent_next_step": "当前 lark-cli 不能确认 urgent_app 命令面；请检查本工具固定的 lark-cli 版本后重试。",
        }
    if not preflight.get("ok"):
        return {
            "ok": False,
            "failed_code": preflight.get("failed_code", "approval_urgent_command_unverified"),
            "agent_next_step": "当前 lark-cli 不能确认 urgent_app 命令面；请检查本工具固定的 lark-cli 版本后重试。",
        }

    now = clock.now()
    try:
        sent = lark.send_interactive_card(
            chat_id=config.feishu_chat_id,
            card=cards.permission_request_card(
                project="Codex Away Mode",
                cwd=str(Path.cwd()),
                tool_name="审批加急验证",
                description="这是一条用于验证飞书应用内加急能力的测试审批提醒。",
                command="doctor --e2e-approval-urgent",
                now=now,
            ),
        )
    except LarkCliError:
        return {
            "ok": False,
            "failed_code": "approval_urgent_card_send_failed",
            "agent_next_step": "审批加急验证卡发送失败；请先确认基础通知可正常发送，再重试 doctor --e2e-approval-urgent --json。",
        }
    try:
        urgent_result = lark.urgent_app(
            message_id=sent.message_id,
            user_id_list=[config.feishu_user_id],
        )
    except Exception as exc:
        return {
            "ok": False,
            "failed_code": _approval_urgent_failed_code(exc),
            "agent_next_step": _approval_urgent_failure_next_step(exc),
        }

    invalid_ids = notify._extract_invalid_user_ids(urgent_result)
    invalid_hashes = [notify._short_sensitive_hash(value) for value in invalid_ids]
    if invalid_ids:
        return {
            "ok": False,
            "failed_code": "approval_urgent_invalid_user",
            "agent_next_step": "飞书 urgent_app 返回 invalid_user_id_list；请重新运行 setup feishu 绑定当前用户后再验证审批加急。",
        }
    context = _approval_urgent_verification_context(paths, config, lark=lark)
    open_install_store(paths).set_install_state(
        "approval_urgent",
        {
            "status": "verified",
            "verified_at": now.astimezone(timezone.utc).isoformat(),
            "message_id": sent.message_id,
            "urgent_invalid_user_count": len(invalid_ids),
            "urgent_invalid_user_hashes": invalid_hashes,
            **context,
        },
    )
    return {
        "ok": True,
        "status": "approval_urgent_verified",
        "next_step": "审批提醒飞书应用内加急已验证。后续 PermissionRequest 审批提醒会尝试对同一张卡片追加应用内加急。",
    }


def _warn_legacy_workspace_artifacts(report: dict[str, Any], cwd: str | None) -> None:
    if not cwd:
        return
    legacy_dir = Path(cwd) / ".codex-away-mode"
    if not legacy_dir.exists():
        return
    legacy_names = {
        "latest-summary.md",
        "state.sqlite",
        "state.sqlite-wal",
        "state.sqlite-shm",
        "user-turns",
    }
    if any((legacy_dir / name).exists() for name in legacy_names):
        report["warnings"].append("legacy_workspace_artifacts_present")


def _warn_runtime_stale_sessions(report: dict[str, Any], paths, clock) -> None:
    try:
        result = StateStore(Path(paths.runtime_state_path)).cleanup_stale_away_sessions(
            now=clock.now().astimezone(timezone.utc).isoformat(),
            dry_run=True,
        )
    except Exception:
        return
    if result.get("closed_count", 0) <= 0:
        return
    report["warnings"].append("runtime_stale_sessions_present")
    report.setdefault("diagnostics", {})["runtime_stale_sessions"] = {
        "cleanup_candidate_count": result.get("closed_count", 0),
        "skipped_waiter_alive_count": result.get("skipped_waiter_alive_count", 0),
    }


def run_route_probe(
    paths,
    *,
    config: AppConfig,
    lark,
    clock,
    timeout_seconds: float,
    poll_interval_seconds: float,
) -> dict[str, Any]:
    probe_started_at = clock.now()
    store = open_install_store(paths)
    sent = lark.send_interactive_card(
        chat_id=config.feishu_chat_id,
        card=_probe_card(),
    )
    probe_message_id = sent.message_id
    deadline = clock.now() + timedelta(seconds=timeout_seconds)

    while clock.now() <= deadline:
        messages = lark.list_messages(chat_id=config.feishu_chat_id, page_size=50)
        for message in messages:
            if not _is_user_message(message):
                continue
            if getattr(message, "reply_to", None) == probe_message_id:
                config.route_key_verified = True
                config.multi_window_enabled = True
                save_config(paths.config_path, config)
                store.set_route_key_state(
                    status="verified",
                    source="doctor_route_probe",
                    verified_at=clock.now().astimezone(timezone.utc).isoformat(),
                )
                return {
                    "status": "passed",
                    "code": "route_probe",
                    "next_step": "Route-key probe passed; multi-window Away Mode is enabled.",
                }
            if not _message_at_or_after(message, probe_started_at):
                continue
            if _looks_like_probe_reply(message):
                config.route_key_verified = False
                config.multi_window_enabled = False
                save_config(paths.config_path, config)
                store.set_route_key_state(
                    status="failed",
                    source="doctor_route_probe",
                    last_failure_reason="mismatch_reply_to",
                )
                return {
                    "status": "failed",
                    "code": "route_probe_failed",
                    "next_step": (
                        "Route-key probe reply was not attached to the probe card; "
                        "multi-window Away Mode has been disabled."
                    ),
                }
        if poll_interval_seconds <= 0:
            break
        clock.sleep(poll_interval_seconds)

    if store.route_key_state().get("status") != "verified":
        store.set_route_key_state(
            status="inconclusive",
            source="doctor_route_probe",
        )
    return {
        "status": "inconclusive",
        "code": "route_probe_inconclusive",
        "next_step": (
            "No probe reply was observed before timeout; rerun doctor --route-probe "
            "when you are ready to reply to the Feishu card."
        ),
    }


def parse_utc(value: str) -> datetime:
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def parse_lark_message_time(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return _from_unix_like(float(value))
    raw = str(value).strip()
    if not raw:
        return None
    if raw.isdigit():
        return _from_unix_like(float(raw))
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            local_tz = datetime.now().astimezone().tzinfo
            return datetime.strptime(raw, fmt).replace(tzinfo=local_tz).astimezone(timezone.utc)
        except ValueError:
            pass
    try:
        return parse_utc(raw)
    except ValueError:
        return None


def _new_report() -> dict[str, Any]:
    return {
        "ok": False,
        "passed_codes": [],
        "failed_codes": [],
        "warnings": [],
        "degraded_codes": [],
        "next_step": "",
    }


def _fail(report: dict[str, Any], code: str, next_step: str) -> None:
    report["failed_codes"].append(code)
    report["next_step"] = next_step


def _degrade(report: dict[str, Any], code: str, next_step: str) -> None:
    report["degraded_codes"].append(code)
    report["next_step"] = next_step


def _finalize(report: dict[str, Any]) -> dict[str, Any]:
    report["ok"] = not report["failed_codes"] and not report["degraded_codes"]
    return report


def _append_approval_urgent_status(
    report: dict[str, Any],
    *,
    paths,
    config: AppConfig,
    store: StateStore,
) -> None:
    if not getattr(config, "approval_notifications_enabled", True):
        return
    if not getattr(config, "approval_notifications_urgent_app_enabled", True):
        return
    if not config.feishu_user_id:
        _degrade(
            report,
            "approval_urgent_unverified",
            "基础飞书通知可用，但审批提醒加急尚未验证；请先运行 setup feishu 绑定飞书用户，再显式运行 doctor --e2e-approval-urgent --json。",
        )
        return
    state = store.get_install_state("approval_urgent", {})
    current = _approval_urgent_verification_context(
        paths,
        config,
        lark=LarkCli(config.lark_cli_path),
    )
    if state.get("status") == "verified" and _approval_urgent_context_matches(state, current):
        report["passed_codes"].append("approval_urgent_verified")
        return
    _degrade(
        report,
        "approval_urgent_unverified",
        "基础飞书通知可用；审批提醒加急尚未验证。如需验证，请先告知用户会发送真实飞书加急测试，再运行 codex-away-mode doctor --e2e-approval-urgent --json。",
    )


def _approval_urgent_verification_context(paths, config: AppConfig, *, lark=None) -> dict[str, Any]:
    app_id_hash, profile_hash = _lark_app_config_hashes(lark, config)
    return {
        "approval_urgent_verified_lark_cli_version": _lark_cli_version(lark),
        "approval_urgent_verified_app_id_hash": app_id_hash,
        "approval_urgent_verified_profile_hash": profile_hash,
        "approval_urgent_verified_feishu_user_id_hash": StateStore.hash_sensitive(
            config.feishu_user_id
        ),
        "approval_urgent_verified_feishu_chat_id_hash": StateStore.hash_sensitive(
            config.feishu_chat_id
        ),
        "approval_urgent_verified_lark_cli_path_hash": StateStore.hash_sensitive(
            config.lark_cli_path
        ),
        "approval_urgent_verified_hooks_fingerprint": hooks_fingerprint(paths),
    }


def _approval_urgent_context_matches(state: dict[str, Any], current: dict[str, Any]) -> bool:
    for key, value in current.items():
        if state.get(key) != value:
            return False
    return True


def _lark_cli_version(lark) -> str:
    if lark is None:
        return "unknown"
    version_method = getattr(lark, "version_info", None)
    if version_method is None:
        return "unknown"
    try:
        data = version_method()
    except LarkCliError:
        return "unknown"
    return json.dumps(data, ensure_ascii=False, sort_keys=True)


def _lark_app_config_hashes(lark, config: AppConfig) -> tuple[str | None, str | None]:
    if lark is None:
        return None, StateStore.hash_sensitive(config.lark_cli_path)
    status_method = getattr(lark, "app_config_status", None)
    if status_method is None:
        return None, StateStore.hash_sensitive(getattr(lark, "binary", None) or config.lark_cli_path)
    try:
        status = status_method()
    except LarkCliError:
        return (
            StateStore.hash_sensitive("app_config:unknown"),
            StateStore.hash_sensitive(getattr(lark, "binary", None) or config.lark_cli_path),
        )
    detail = status.get("detail", status) if isinstance(status, dict) else status
    app_id = _find_lark_config_value(detail, {"appid", "clientid", "cliappid"})
    profile = _find_lark_config_value(detail, {"profile", "profilename", "configname"})
    app_identity = _stable_json(detail)
    profile_identity = profile or app_id or getattr(lark, "binary", None) or config.lark_cli_path
    return StateStore.hash_sensitive(app_identity), StateStore.hash_sensitive(profile_identity)


def _find_lark_config_value(value: Any, keys: set[str]) -> str | None:
    if isinstance(value, dict):
        for key, item in value.items():
            normalized = "".join(char for char in str(key).lower() if char.isalnum())
            if normalized in keys and item is not None:
                return str(item)
        for item in value.values():
            found = _find_lark_config_value(item, keys)
            if found is not None:
                return found
    if isinstance(value, list):
        for item in value:
            found = _find_lark_config_value(item, keys)
            if found is not None:
                return found
    return None


def _stable_json(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    except TypeError:
        return str(value)


def _approval_urgent_failed_code(exc: Exception) -> str:
    text = str(exc).lower()
    if "im:message.urgent" in text or "permission" in text or "scope" in text:
        return "approval_urgent_permission_missing"
    if "user" in text and "missing" in text:
        return "feishu_user_id_missing"
    return "approval_urgent_unverified"


def _approval_urgent_failure_next_step(exc: Exception) -> str:
    if _approval_urgent_failed_code(exc) == "approval_urgent_permission_missing":
        return (
            "飞书审批提醒卡已可发送，但应用内加急权限不足。请在飞书开放平台 -> 权限管理 -> 开通权限中增加 "
            "im:message.urgent（发送应用内加急消息）。如果页面也展示 im:message.urgent:app_send，"
            "则一并开通。发布并完成管理员审批后，再运行 doctor --e2e-approval-urgent --json。"
        )
    return "审批提醒加急验证失败；请检查 lark-cli、飞书 Bot 权限和用户绑定后重试。"


def _last_hook_invocation(
    paths,
    *,
    hook_event_name: str,
    hooks_fingerprint: str,
) -> dict[str, Any] | None:
    try:
        events = StateStore(Path(paths.runtime_state_path)).list_diagnostic_events(
            "codex_hook_invocation",
            limit=200,
        )
    except Exception:
        return False
    for event in reversed(events):
        try:
            detail = json.loads(event.get("detail_json") or "{}")
        except json.JSONDecodeError:
            continue
        if detail.get("hook_event_name") != hook_event_name:
            continue
        if detail.get("hooks_fingerprint") != hooks_fingerprint:
            continue
        return event
    return None


def _last_stop_hook_invocation(paths, *, hooks_fingerprint: str) -> dict[str, Any] | None:
    return _last_hook_invocation(
        paths,
        hook_event_name="Stop",
        hooks_fingerprint=hooks_fingerprint,
    )


def _stop_hook_invocation_verified(paths, *, hooks_fingerprint: str) -> bool:
    return _last_stop_hook_invocation(paths, hooks_fingerprint=hooks_fingerprint) is not None


def _runtime_hook_trust_override(
    paths,
    *,
    trust_result: dict[str, Any],
    hooks_fingerprint: str,
) -> dict[str, Any] | None:
    return _runtime_hook_trust_observation(
        paths,
        hooks=trust_result.get("hooks") or {},
        hooks_fingerprint=hooks_fingerprint,
        require_other_hooks_trusted=True,
    )


def _runtime_hook_trust_observation(
    paths,
    *,
    hooks: dict[str, Any],
    hooks_fingerprint: str,
    require_other_hooks_trusted: bool = False,
) -> dict[str, Any] | None:
    hooks = dict(hooks)
    permission = hooks.get("permission_request")
    if not permission or permission.get("status") not in {
        "missing_enabled",
        "trust_record_present",
    }:
        return None
    if require_other_hooks_trusted:
        other_untrusted = [
            event_key
            for event_key, state in hooks.items()
            if event_key != "permission_request" and state.get("status") != "trusted"
        ]
        if other_untrusted:
            return None
    event = _last_hook_invocation(
        paths,
        hook_event_name="PermissionRequest",
        hooks_fingerprint=hooks_fingerprint,
    )
    if event is None:
        return None
    hooks["permission_request"] = {
        **permission,
        "status": "observed",
    }
    return {
        "hooks": hooks,
        "observed": {
            "permission_request": event.get("created_at"),
        },
    }


def _hooks_installed(paths) -> bool:
    path = Path(paths.hooks_json)
    if not path.exists():
        return False
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return False
    return "Codex Away Mode managed hook" in text and "notify stop --json" in text


def hooks_fingerprint(paths) -> str:
    path = Path(paths.hooks_json)
    if not path.exists():
        payload: Any = {"hooks": {}}
    else:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            payload = {"unreadable": True}
    managed: dict[str, list[dict[str, Any]]] = {}
    hooks_root = payload.get("hooks", {}) if isinstance(payload, dict) else {}
    if isinstance(hooks_root, dict):
        for event in ("UserPromptSubmit", "Stop", "PermissionRequest"):
            entries: list[dict[str, Any]] = []
            for group in hooks_root.get(event, []):
                if not isinstance(group, dict):
                    continue
                for hook in group.get("hooks", []):
                    if not isinstance(hook, dict):
                        continue
                    command = str(hook.get("command", ""))
                    status_message = str(hook.get("statusMessage", ""))
                    if (
                        "codex-away-mode" not in command
                        and "notify stop --json" not in command
                        and "notify mark-prompt --json" not in command
                        and "notify permission-request --hook-json" not in command
                        and status_message != "Codex Away Mode managed hook"
                    ):
                        continue
                    entries.append(
                        {
                            "type": hook.get("type"),
                            "command": command,
                            "timeout": hook.get("timeout"),
                            "statusMessage": status_message,
                        }
                    )
            managed[event] = sorted(entries, key=lambda item: item.get("command") or "")
    encoded = json.dumps(managed, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return f"sha256:{hashlib.sha256(encoded.encode('utf-8')).hexdigest()}"


def _wrapper_path(paths) -> Path:
    return Path(getattr(paths, "wrapper_path", Path(paths.data_dir) / "bin" / "codex-away-mode"))


class _E2ENotificationClient:
    def __init__(self, *, lark, chat_id: str) -> None:
        self.lark = lark
        self.chat_id = chat_id
        self.last_result = None

    def send_summary_card(self, markdown: str, cwd: str | None = None):
        self.last_result = self.lark.send_interactive_card(
            chat_id=self.chat_id,
            card=cards.completion_card(
                title="Codex Away Mode E2E",
                project=cards.project_from_cwd(cwd),
                fields={"摘要": markdown},
                footer_cwd=cwd,
                footer_mode_text="安装验证。",
            ),
        )
        return self.last_result

    def send_fallback_card(self, cwd: str):
        self.last_result = self.lark.send_interactive_card(
            chat_id=self.chat_id,
            card=cards.fallback_completion_card(
                reason="summary missing or not usable",
                cwd=cwd,
                now=datetime.now(timezone.utc).isoformat(),
            ),
        )
        return self.last_result


def _merge_probe_result(report: dict[str, Any], probe_result: dict[str, Any]) -> None:
    status = probe_result["status"]
    code = probe_result["code"]
    if status in {"passed", "skipped"}:
        report["passed_codes"].append(code)
    else:
        report["degraded_codes"].append(code)
        report["warnings"].append(probe_result["next_step"])
    report["next_step"] = probe_result["next_step"]
    if status == "skipped":
        report.setdefault("diagnostics", {})["route_probe"] = {
            "status": "skipped",
            "sent_probe_card": False,
        }


def _route_probe_already_verified_result() -> dict[str, Any]:
    return {
        "status": "skipped",
        "code": "route_probe_already_verified",
        "next_step": "当前环境已经验证过卡片回复路由，不需要再次发送路由探针。",
    }


def _probe_card() -> dict[str, Any]:
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "template": "blue",
            "title": {
                "tag": "plain_text",
                "content": "Codex Away Mode 路由探针",
            },
        },
        "elements": [
            {
                "tag": "markdown",
                "content": (
                    "请直接回复这张卡片任意文字。\n\n"
                    "Codex 会用这次回复验证飞书是否提供稳定的 `reply_to` 路由。"
                ),
            },
            {
                "tag": "note",
                "elements": [
                    {
                        "tag": "plain_text",
                        "content": "普通私聊不会用于这次探针。",
                    }
                ],
            },
        ],
    }


def _is_user_message(message) -> bool:
    sender_type = getattr(message, "sender_type", None)
    return sender_type not in {"app", "bot"}


def _looks_like_probe_reply(message) -> bool:
    return bool(getattr(message, "reply_to", None))


def _message_at_or_after(message, probe_started_at: datetime) -> bool:
    message_time = parse_lark_message_time(getattr(message, "create_time", None))
    if message_time is None:
        return False
    return message_time >= probe_started_at


def _from_unix_like(value: float) -> datetime:
    if value > 1_000_000_000_000:
        value = value / 1000
    return datetime.fromtimestamp(value, tz=timezone.utc)
