from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
from datetime import timedelta
from pathlib import Path

from . import cards, doctor, install, notify, setup, status as away_status, uninstall
from .away import AwayWaiter
from .config import RuntimePaths, RuntimeStateError, ensure_runtime_state_writable, load_config, save_config
from .lark import LarkCli, LarkCliError
from .state import StateStore, open_install_store
from .thread_context import resolve_card_title_context
from .time import SystemClock

COMMANDS = {"version", "install", "setup", "doctor", "notify", "away", "uninstall"}


class MissingFeishuBinding(RuntimeError):
    pass


def emit_json(payload):
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))


def build_parser():
    parser = argparse.ArgumentParser(prog="codex-away-mode")
    parser.add_argument("--json", action="store_true", dest="global_json")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("version")

    install = subparsers.add_parser("install")
    install.add_argument("--json", action="store_true")
    install_mode = install.add_mutually_exclusive_group()
    install_mode.add_argument("--dry-run", action="store_true")
    install_mode.add_argument("--yes", action="store_true")
    install_subparsers = install.add_subparsers(dest="install_command")
    install_preflight = install_subparsers.add_parser("preflight")
    install_preflight.add_argument("--json", action="store_true")
    install_status = install_subparsers.add_parser("status")
    install_status.add_argument("--json", action="store_true")

    setup = subparsers.add_parser("setup")
    setup_subparsers = setup.add_subparsers(dest="setup_command", required=True)
    setup_feishu = setup_subparsers.add_parser("feishu")
    setup_feishu.add_argument("--json", action="store_true")
    setup_feishu.add_argument("--device-code")
    setup_feishu.add_argument("--restart-auth", action="store_true")

    doctor = subparsers.add_parser("doctor")
    doctor.add_argument("--json", action="store_true")
    doctor.add_argument("--route-probe", action="store_true")
    doctor.add_argument("--e2e-notify", action="store_true")

    notify = subparsers.add_parser("notify")
    notify_subparsers = notify.add_subparsers(dest="notify_command", required=True)
    notify_mode = notify_subparsers.add_parser("mode")
    notify_mode.add_argument("mode", choices=["all", "off"])
    notify_snooze = notify_subparsers.add_parser("snooze")
    notify_snooze.add_argument("duration")
    notify_mark_prompt = notify_subparsers.add_parser("mark-prompt")
    notify_mark_prompt.add_argument("--cwd")
    notify_mark_prompt.add_argument("--json", action="store_true")
    notify_stage_summary = notify_subparsers.add_parser("stage-summary")
    notify_stage_summary.add_argument("--cwd")
    notify_stage_summary.add_argument("--json", action="store_true")
    notify_stop = notify_subparsers.add_parser("stop")
    notify_stop.add_argument("--cwd")
    notify_stop.add_argument("--json", action="store_true")
    notify_test = notify_subparsers.add_parser("test")
    notify_test.add_argument("--json", action="store_true")
    notify_permission = notify_subparsers.add_parser("permission-request")
    notify_permission.add_argument("--hook-json", action="store_true")
    notify_permission.add_argument("--json", action="store_true")

    away = subparsers.add_parser("away")
    away_subparsers = away.add_subparsers(dest="away_command", required=True)
    away_wait = away_subparsers.add_parser("wait")
    away_wait.add_argument("--resume")
    away_wait.add_argument("--resume-token")
    away_wait.add_argument("--extend-minutes", type=int)
    for name in (
        "project",
        "cwd",
        "task",
        "completed",
        "changed",
        "verification",
        "unverified",
        "need-user",
    ):
        away_wait.add_argument("--" + name)
    away_wait.add_argument("--codex-session-id")
    away_wait.add_argument("--wait-minutes", type=int)
    away_wait.add_argument("--poll-interval", type=int)
    away_wait.add_argument("--json", action="store_true")
    away_start = away_subparsers.add_parser("start")
    for name in (
        "project",
        "cwd",
        "task",
        "completed",
        "changed",
        "verification",
        "unverified",
        "need-user",
    ):
        away_start.add_argument("--" + name)
    away_start.add_argument("--codex-session-id")
    away_start.add_argument("--wait-minutes", type=int)
    away_start.add_argument("--poll-interval", type=int)
    away_start.add_argument("--json", action="store_true")
    away_resume = away_subparsers.add_parser("resume")
    away_resume.add_argument("session_id")
    away_resume.add_argument("--resume-token")
    away_resume.add_argument("--extend-minutes", type=int)
    for name in (
        "completed",
        "changed",
        "verification",
        "unverified",
        "need-user",
    ):
        away_resume.add_argument("--" + name)
    away_resume.add_argument("--poll-interval", type=int)
    away_resume.add_argument("--json", action="store_true")
    away_status_parser = away_subparsers.add_parser("status")
    away_status_parser.add_argument("--session")
    away_status_parser.add_argument("--cwd")
    away_status_parser.add_argument("--active-only", action="store_true")
    away_status_parser.add_argument("--include-closed", action="store_true")
    away_status_parser.add_argument("--include-internal-ids", action="store_true")
    away_status_parser.add_argument("--debug", action="store_true")
    away_status_parser.add_argument("--limit", type=int, default=20)
    away_status_parser.add_argument("--json", action="store_true")
    away_cleanup = away_subparsers.add_parser("cleanup")
    away_cleanup.add_argument("--dry-run", action="store_true")
    away_cleanup.add_argument("--json", action="store_true")

    uninstall = subparsers.add_parser("uninstall")
    uninstall.add_argument("--json", action="store_true")
    uninstall_mode = uninstall.add_mutually_exclusive_group()
    uninstall_mode.add_argument("--keep-data", action="store_true")
    uninstall_mode.add_argument("--delete-data", action="store_true")

    return parser


def wants_json(argv):
    return "--json" in argv


def unknown_command(argv):
    tokens = [arg for arg in argv if not arg.startswith("-")]
    return bool(tokens) and tokens[0] not in COMMANDS


def main(argv=None, *, stdin=None):
    if argv is None:
        argv = sys.argv[1:]

    try:
        return _main(argv, stdin=stdin)
    except SystemExit:
        raise
    except Exception as exc:
        if wants_json(argv):
            emit_json(_internal_error_payload(exc))
            return 1
        raise


def _main(argv, *, stdin=None):

    if unknown_command(argv):
        if wants_json(argv):
            emit_json({"ok": False, "error_code": "unknown_command"})
            return 2
        print("unknown command", file=sys.stderr)
        return 2

    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "version":
        emit_json({"ok": True, "command": "version", "name": "codex-away-mode"})
        return 0

    paths = RuntimePaths.from_environment()

    if args.command == "doctor":
        emit_json(
            doctor.run_doctor(
                paths,
                route_probe=args.route_probe,
                e2e_notify=args.e2e_notify,
                cwd=os.getcwd(),
            )
        )
        return 0

    if args.command == "install":
        if args.install_command == "status":
            emit_json(install.install_status(paths))
        elif args.install_command == "preflight":
            emit_json(install.run_preflight(paths))
        else:
            emit_json(
                install.run_install(
                    paths,
                    dry_run=args.dry_run,
                    yes=args.yes,
                    ensure_lark_cli=True,
                )
            )
        return 0

    if args.command == "setup" and args.setup_command == "feishu":
        emit_json(
            setup.run_setup_feishu(
                paths,
                device_code=args.device_code,
                restart_auth=args.restart_auth,
            )
        )
        return 0

    if args.command == "uninstall":
        emit_json(
            uninstall.run_uninstall(
                paths,
                keep_data=args.keep_data,
                delete_data=args.delete_data,
            )
        )
        return 0

    if args.command == "notify":
        return _handle_notify(args, paths, stdin=stdin)

    if args.command == "away" and args.away_command in {"wait", "start", "resume"}:
        return _handle_away_wait(args, paths)

    if args.command == "away" and args.away_command == "status":
        emit_json(
            away_status.run_away_status(
                paths,
                session_id=args.session,
                cwd=args.cwd,
                active_only=args.active_only or not args.include_closed,
                include_closed=args.include_closed,
                include_internal_ids=args.include_internal_ids or args.debug,
                limit=args.limit,
            )
        )
        return 0

    if args.command == "away" and args.away_command == "cleanup":
        result = StateStore(paths.runtime_state_path).cleanup_stale_away_sessions(
            now=SystemClock().now().isoformat(),
            dry_run=args.dry_run,
        )
        emit_json(
            {
                "ok": True,
                "command": "away cleanup",
                "dry_run": args.dry_run,
                **result,
                "next_step": "Run codex-away-mode away status --json to confirm there are no stale sessions.",
            }
        )
        return 0

    emit_json({"ok": True, "command": command_name(args), "implemented": False})
    return 0


def _handle_notify(args, paths, *, stdin=None):
    if args.notify_command == "mode":
        config = notify.set_notification_mode(paths, args.mode)
        emit_json({"ok": True, "command": "notify mode", "mode": config.notification_mode})
        return 0

    if args.notify_command == "snooze":
        until = SystemClock().now() + _parse_duration(args.duration)
        config = notify.set_notification_mode(paths, "snooze", until=until)
        emit_json(
            {
                "ok": True,
                "command": "notify snooze",
                "mode": "snooze",
                "snooze_until": config.snooze_until,
            }
        )
        return 0

    if args.notify_command == "mark-prompt":
        hook_stdin = _read_hook_stdin(stdin)
        cwd = notify.resolve_notify_cwd(args.cwd, hook_stdin, os.getcwd())
        now = SystemClock().now()
        notify.capture_hook_payload(
            paths,
            event_kind="UserPromptSubmit",
            hook_stdin=hook_stdin,
            cwd=cwd,
            now=now,
        )
        notify.record_hook_invocation(
            paths,
            hook_event_name="UserPromptSubmit",
            hook_stdin=hook_stdin,
            cwd=cwd,
            now=now,
            hooks_fingerprint=doctor.hooks_fingerprint(paths),
        )
        skip_reason = notify.skip_cwd_reason(paths, cwd)
        if skip_reason:
            emit_json({"ok": True, "command": "notify mark-prompt", "status": "skipped", "reason": skip_reason, "cwd": cwd})
            return 0
        try:
            marker_key = notify.mark_prompt(paths, cwd=cwd, now=now)
        except RuntimeStateError as exc:
            return _emit_runtime_state_error("notify mark-prompt", exc)
        emit_json({"ok": True, "command": "notify mark-prompt", "status": "marked", "marker_key": marker_key})
        return 0

    if args.notify_command == "stage-summary":
        hook_stdin = _read_hook_stdin(stdin)
        cwd = notify.resolve_notify_cwd(args.cwd, None, os.getcwd())
        now = SystemClock().now()
        skip_reason = notify.skip_cwd_reason(paths, cwd)
        if skip_reason:
            emit_json({"ok": True, "command": "notify stage-summary", "status": "skipped", "reason": skip_reason, "cwd": cwd})
            return 0
        summary = hook_stdin or ""
        try:
            summary_key = notify.stage_summary(paths, cwd=cwd, summary_markdown=summary, now=now)
        except RuntimeStateError as exc:
            return _emit_runtime_state_error("notify stage-summary", exc)
        emit_json({"ok": True, "command": "notify stage-summary", "status": "staged", "summary_key": summary_key, "cwd": cwd})
        return 0

    if args.command == "notify" and args.notify_command == "stop":
        hook_stdin = _read_hook_stdin(stdin)
        cwd = notify.resolve_notify_cwd(args.cwd, hook_stdin, os.getcwd())
        now = SystemClock().now()
        notify.capture_hook_payload(
            paths,
            event_kind="Stop",
            hook_stdin=hook_stdin,
            cwd=cwd,
            now=now,
        )
        notify.record_hook_invocation(
            paths,
            hook_event_name="Stop",
            hook_stdin=hook_stdin,
            cwd=cwd,
            now=now,
            hooks_fingerprint=doctor.hooks_fingerprint(paths),
        )
        try:
            early_exit = notify.send_away_early_exit_if_needed(
                paths,
                _NotificationClient(paths, hook_stdin=hook_stdin),
                cwd=cwd,
                now=now,
                hook_stdin=hook_stdin,
            )
        except RuntimeStateError as exc:
            return _emit_runtime_state_error("notify stop", exc)
        except MissingFeishuBinding as exc:
            emit_json({"ok": True, "command": "notify stop", "status": "skipped", "reason": "missing_feishu_binding", "message": str(exc), "cwd": cwd})
            return 0
        if early_exit is not None:
            emit_json({"ok": True, "command": "notify stop", "status": early_exit.status, "detail": early_exit.detail, "cwd": cwd})
            return 0
        mode = notify.effective_notification_mode(paths, now=now)
        if mode == "off":
            emit_json({"ok": True, "command": "notify stop", "status": "skipped", "reason": "notification_mode_off", "cwd": cwd})
            return 0
        try:
            result = notify.send_completion_from_summary(
                paths,
                _NotificationClient(paths, hook_stdin=hook_stdin),
                cwd=cwd,
                now=now,
                hook_stdin=hook_stdin,
            )
        except RuntimeStateError as exc:
            return _emit_runtime_state_error("notify stop", exc)
        except MissingFeishuBinding as exc:
            emit_json({"ok": True, "command": "notify stop", "status": "skipped", "reason": "missing_feishu_binding", "message": str(exc), "cwd": cwd})
            return 0
        emit_json({"ok": True, "command": "notify stop", "status": result.status, "detail": result.detail, "cwd": cwd})
        return 0

    if args.notify_command == "test":
        try:
            result = notify.send_test_notification(paths, _NotificationClient(paths))
        except MissingFeishuBinding as exc:
            emit_json({"ok": False, "command": "notify test", "error_code": "missing_feishu_binding", "message": str(exc)})
            return 1
        emit_json({"ok": True, "command": "notify test", "chat_id": result.chat_id, "message_id": result.message_id})
        return 0

    if args.notify_command == "permission-request":
        hook_stdin = _read_hook_stdin(stdin)
        now = SystemClock().now()
        notify.capture_hook_payload(
            paths,
            event_kind="PermissionRequest",
            hook_stdin=hook_stdin,
            cwd=notify.resolve_notify_cwd(None, hook_stdin, os.getcwd()),
            now=now,
        )
        notify.record_hook_invocation(
            paths,
            hook_event_name="PermissionRequest",
            hook_stdin=hook_stdin,
            cwd=notify.resolve_notify_cwd(None, hook_stdin, os.getcwd()),
            now=now,
            hooks_fingerprint=doctor.hooks_fingerprint(paths),
        )
        if args.hook_json:
            try:
                notify.send_permission_request(
                    paths,
                    _NotificationClient(paths, hook_stdin=hook_stdin),
                    hook_stdin=hook_stdin,
                    now=now,
                )
            except Exception:
                pass
            print("{}")
            return 0
        try:
            result = notify.send_permission_request(
                paths,
                _NotificationClient(paths, hook_stdin=hook_stdin),
                hook_stdin=hook_stdin,
                now=now,
            )
        except RuntimeStateError as exc:
            return _emit_runtime_state_error("notify permission-request", exc)
        emit_json(
            {
                "ok": result.status in {"sent", "suppressed", "skipped"},
                "command": "notify permission-request",
                "status": result.status,
                "detail": result.detail,
            }
        )
        return 0 if result.status in {"sent", "suppressed", "skipped"} else 1

    emit_json({"ok": False, "command": command_name(args), "error_code": "unknown_notify_command"})
    return 2


def _handle_away_wait(args, paths):
    _validate_away_wait_args(args)
    try:
        ensure_runtime_state_writable(paths)
    except RuntimeStateError as exc:
        emit_json(
            {
                "ok": False,
                "status": "error",
                "error_code": exc.error_code,
                "detail": exc.detail,
            }
        )
        return 1
    config = load_config(paths.config_path)
    wait_minutes = getattr(args, "wait_minutes", None)
    poll_interval = getattr(args, "poll_interval", None)
    if wait_minutes is not None:
        config.default_wait_minutes = wait_minutes
    if poll_interval is not None:
        config.poll_interval_seconds = poll_interval
    context = {
        "completed": args.completed,
        "changed": args.changed,
        "verification": args.verification,
        "unverified": args.unverified,
        "need_user": args.need_user,
    }
    resume_id = _away_resume_id(args)
    if resume_id:
        context["resume"] = resume_id
        context["resume_token"] = getattr(args, "resume_token", None)
        extend_minutes = getattr(args, "extend_minutes", None)
        if extend_minutes is not None:
            context["extend_minutes"] = extend_minutes
    else:
        context.update(
            {
                "project": args.project,
                "cwd": args.cwd,
                "task": args.task,
                "wait_minutes": wait_minutes,
                "codex_session_id": args.codex_session_id,
            }
        )
    try:
        result = AwayWaiter(
            lark=LarkCli(config.lark_cli_path),
            store=StateStore(paths.runtime_state_path),
            clock=SystemClock(),
            config=config,
            config_path=paths.config_path,
            install_store=open_install_store(paths),
        ).wait(context)
    except LarkCliError as exc:
        error_code = "lark_cli_unavailable" if _looks_like_lark_cli_unavailable(exc) else "feishu_transport_error"
        emit_json(_lark_cli_error_payload(error_code, str(exc)))
        return 1
    emit_json(result)
    return 0


def _emit_runtime_state_error(command: str, exc: RuntimeStateError) -> int:
    emit_json(
        {
            "ok": False,
            "command": command,
            "status": "error",
            "error_code": exc.error_code,
            "detail": exc.detail,
        }
    )
    return 1


def _lark_cli_unavailable(binary: str | None) -> bool:
    if not binary:
        return True
    path = Path(binary).expanduser()
    if path.is_absolute() or os.sep in binary:
        return not path.exists()
    return shutil.which(binary) is None


def _looks_like_lark_cli_unavailable(exc: LarkCliError) -> bool:
    detail = str(exc).lower()
    return "no such file or directory" in detail or "not found" in detail


def _lark_cli_error_payload(error_code: str, detail: str) -> dict:
    if error_code == "lark_cli_unavailable":
        message = "没有找到飞书 CLI，Away Mode 暂时无法发送或读取飞书消息。"
        next_step = "请先完成安装向导，或确认 lark-cli 已安装并且 codex-away-mode 配置里的路径可执行。"
    else:
        message = "飞书消息通道调用失败，Away Mode 暂时无法继续等待回复。"
        next_step = "请先运行 codex-away-mode doctor --json 查看飞书配置和通知通道状态。"
    return {
        "ok": False,
        "status": "error",
        "error_code": error_code,
        "message": message,
        "detail": _redact_detail(str(detail)),
        "agent_next_step": next_step,
    }


def _internal_error_payload(exc: Exception) -> dict:
    detail = f"{type(exc).__name__}: {_redact_detail(str(exc))}"
    return {
        "ok": False,
        "status": "error",
        "error_code": "internal_error",
        "message": "Codex Away Mode 遇到内部错误，当前命令没有完成。",
        "detail": detail,
        "agent_next_step": "请把这段 JSON 输出交给 Codex 排查；不要重复尝试同一轮 Away Mode，避免产生重复窗口。",
    }


def _redact_detail(value: str) -> str:
    value = re.sub(r"\b(?:ou|oc|om|cli|app)_[A-Za-z0-9_\-]+\b", "[redacted-id]", value)
    return re.sub(r"\b[A-Za-z0-9_\-]{16,}\b", "[redacted-token]", value)


def _validate_away_wait_args(args):
    progress_fields = ["completed", "changed", "verification", "unverified", "need_user"]
    missing_progress = [name for name in progress_fields if getattr(args, name) is None]
    if missing_progress:
        raise SystemExit(2)
    if _away_resume_id(args):
        extend_minutes = getattr(args, "extend_minutes", None)
        if extend_minutes is not None and extend_minutes <= 0:
            raise SystemExit(2)
        return
    required = ["project", "cwd", "task", "wait_minutes"]
    missing = [name for name in required if getattr(args, name) is None]
    if missing:
        raise SystemExit(2)


def _away_resume_id(args) -> str | None:
    if getattr(args, "away_command", None) == "resume":
        return getattr(args, "session_id", None)
    return getattr(args, "resume", None)


def command_name(args):
    if args.command == "notify":
        return "notify " + args.notify_command
    if args.command == "away":
        return "away " + args.away_command
    return args.command


def _parse_duration(value: str) -> timedelta:
    raw = value.strip().lower()
    if raw.endswith("h"):
        return timedelta(hours=float(raw[:-1]))
    if raw.endswith("m"):
        return timedelta(minutes=float(raw[:-1]))
    if raw.endswith("s"):
        return timedelta(seconds=float(raw[:-1]))
    return timedelta(minutes=float(raw))


def _read_hook_stdin(stdin) -> str | None:
    if stdin is not None:
        return stdin.read()
    stream = sys.stdin
    if hasattr(stream, "isatty") and stream.isatty():
        return None
    try:
        return stream.read()
    except OSError:
        return None


class _NotificationClient:
    def __init__(self, paths, *, hook_stdin=None, env=None) -> None:
        self.paths = paths
        self.hook_stdin = hook_stdin
        self.env = env
        self.config = load_config(paths.config_path)
        self.lark = LarkCli(self.config.lark_cli_path)

    def send_summary_card(self, markdown: str, cwd: str | None = None):
        sections = cards.summary_sections(markdown)
        footer_cwd = sections.get("工作目录") or cwd
        fields = {
            key: value
            for key, value in sections.items()
            if key not in {"项目", "工作目录"}
        } or {"完成": markdown.strip() or "未提供摘要内容。"}
        return self._send_card(
            cards.completion_card(
                title="Codex 完成通知",
                fields=fields,
                footer_cwd=footer_cwd,
                footer_mode_text=self._notification_footer_text(),
                now=SystemClock().now(),
                title_context=self._title_context(cwd=footer_cwd or cwd),
            )
        )

    def send_fallback_card(self, cwd: str):
        return self._send_card(
            cards.fallback_completion_card(
                reason="summary missing or not usable",
                cwd=cwd,
                now=SystemClock().now(),
                title_context=self._title_context(cwd=cwd),
            )
        )

    def send_test_notification(self):
        return self._send_card(
            cards.completion_card(
                title="Codex Away Mode 测试通知",
                fields={"完成": "测试通知已发送。"},
                footer_mode_text=self._notification_footer_text(),
                now=SystemClock().now(),
            )
        )

    def send_away_early_exit_card(self, payload):
        return self._send_card(
            cards.away_early_exit_card(
                project=payload["project"],
                cwd=payload["cwd"],
                completed=payload["completed"],
                changed=payload["changed"],
                verification=payload["verification"],
                unverified=payload["unverified"],
                need_user=payload["need_user"],
                stopped_at=payload["stopped_at"],
                title_context=self._title_context(
                    cwd=payload.get("cwd"),
                    explicit_codex_session_id=payload.get("codex_session_id"),
                ),
            )
        )

    def send_away_timeout_card(self, payload):
        return self._send_card(
            cards.timeout_card(
                project=payload["project"],
                deadline=payload["deadline"],
                title_context=self._title_context(
                    cwd=payload.get("cwd"),
                    explicit_codex_session_id=payload.get("codex_session_id"),
                ),
            )
        )

    def send_permission_request_card(self, payload):
        return self._send_card(
            cards.permission_request_card(
                project=payload.get("project"),
                cwd=payload.get("cwd"),
                tool_name=payload.get("tool_name") or "未知工具",
                description=payload.get("description"),
                command=payload.get("command"),
                now=payload.get("now") or SystemClock().now(),
                title_context=self._title_context(
                    cwd=payload.get("cwd"),
                    explicit_codex_session_id=payload.get("session_id"),
                ),
            )
        )

    def _send_card(self, card):
        if self.config.feishu_chat_id:
            return self.lark.send_interactive_card(chat_id=self.config.feishu_chat_id, card=card)
        if self.config.feishu_user_id:
            return self.lark.send_interactive_card(user_id=self.config.feishu_user_id, card=card)
        raise MissingFeishuBinding(
            "Missing feishu_chat_id or feishu_user_id; run install/config setup first."
        )

    def _notification_footer_text(self):
        return cards.notification_mode_footer_text(self.config.notification_mode)

    def _title_context(self, *, cwd=None, explicit_codex_session_id=None):
        return resolve_card_title_context(
            cwd=cwd,
            hook_stdin=self.hook_stdin,
            env=self.env,
            codex_home=self.paths.codex_home,
            explicit_codex_session_id=explicit_codex_session_id,
        )
