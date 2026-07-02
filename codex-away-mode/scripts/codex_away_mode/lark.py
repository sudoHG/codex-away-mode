from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import webbrowser
from dataclasses import dataclass
from typing import Any, Callable


Runner = Callable[[list[str], int], Any]


class LarkCliError(RuntimeError):
    """Raised when lark-cli returns a non-zero result or cannot be executed."""


class InvalidJsonError(LarkCliError):
    """Raised when lark-cli output does not contain a JSON object."""


@dataclass(frozen=True)
class SendResult:
    message_id: str
    chat_id: str


@dataclass(frozen=True)
class LarkMessage:
    message_id: str
    reply_to: str | None
    msg_type: str
    content_text: str
    sender_type: str | None
    create_time: str | int | None


class LarkCli:
    def __init__(self, binary: str = "lark-cli", runner: Runner | None = None, timeout: int = 30):
        self.binary = binary
        self._custom_runner = runner is not None
        self.runner = runner or self._run_subprocess
        self.timeout = timeout
        self.runner_calls: list[tuple[list[str], int]] = []

    def send_interactive_card(
        self,
        *,
        card: dict[str, Any],
        user_id: str | None = None,
        chat_id: str | None = None,
    ) -> SendResult:
        args = self._send_args(user_id=user_id, chat_id=chat_id, msg_type="interactive", content=card)
        return self._map_send_result(self._run_json(args))

    def send_text(
        self,
        *,
        text: str,
        user_id: str | None = None,
        chat_id: str | None = None,
    ) -> SendResult:
        args = self._send_args(user_id=user_id, chat_id=chat_id, msg_type="text", content={"text": text})
        return self._map_send_result(self._run_json(args))

    def list_messages(self, *, chat_id: str, page_size: int = 50) -> list[LarkMessage]:
        args = [
            "im",
            "+chat-messages-list",
            "--as",
            "bot",
            "--chat-id",
            chat_id,
            "--page-size",
            str(page_size),
            "--order",
            "desc",
            "--no-reactions",
            "--json",
        ]
        data = self._run_json(args)
        items = data.get("items")
        if items is None:
            payload = data.get("data", {})
            items = payload.get("items") or payload.get("messages") or []
        return [self._map_message(item) for item in items]

    def add_reaction(self, *, message_id: str, emoji_type: str = "Get") -> None:
        data = json.dumps({"reaction_type": {"emoji_type": emoji_type}}, separators=(",", ":"))
        args = [
            "im",
            "reactions",
            "create",
            "--as",
            "bot",
            "--message-id",
            message_id,
            "--data",
            data,
            "--json",
        ]
        self._run_json(args)

    def urgent_app(self, *, message_id: str, user_id_list: list[str]) -> dict[str, Any]:
        data = json.dumps(
            {"user_id_list": user_id_list},
            ensure_ascii=False,
            separators=(",", ":"),
        )
        return self._run_json(
            [
                "im",
                "messages",
                "urgent_app",
                "--as",
                "bot",
                "--message-id",
                message_id,
                "--user-id-type",
                "open_id",
                "--data",
                data,
                "--json",
            ]
        )

    def preflight_urgent_app_command(self) -> dict[str, Any]:
        text = self._run_text(["im", "messages", "urgent_app", "--help"])
        required = ["--as", "--message-id", "--user-id-type", "--data", "--json"]
        missing = [term for term in required if term not in text]
        if missing:
            return {
                "ok": False,
                "failed_code": "approval_urgent_command_unverified",
                "missing": missing,
            }
        return {"ok": True}

    def version_info(self) -> dict[str, str]:
        text = self._run_text(["--version"]).strip()
        return {"binary": self.binary, "version": text}

    def preflight_auth_commands(self) -> dict[str, Any]:
        checks = {
            "auth": (["auth", "--help"], ["login", "status", "qrcode"]),
            "auth_login": (
                ["auth", "login", "--help"],
                ["--json", "--no-wait", "--device-code", "--recommend"],
            ),
            "auth_status": (["auth", "status", "--help"], ["--json", "--verify"]),
            "auth_qrcode": (["auth", "qrcode", "--help"], ["--output", "--ascii"]),
            "chat_list": (
                ["im", "+chat-list", "--help"],
                ["--as", "--types", "p2p"],
            ),
            "chat_messages_list": (
                ["im", "+chat-messages-list", "--help"],
                ["--as", "--chat-id", "--user-id"],
            ),
            "config_init": (
                ["config", "init", "--help"],
                ["--new"],
            ),
        }
        missing: dict[str, list[str]] = {}
        for name, (args, required) in checks.items():
            text = self._run_text(args)
            missing_terms = [term for term in required if term not in text]
            if missing_terms:
                missing[name] = missing_terms
        if missing:
            return {
                "ok": False,
                "failed_code": "lark_auth_command_unverified",
                "missing": missing,
            }
        return {"ok": True}

    def app_config_status(self) -> dict[str, Any]:
        config_command = [self.binary, "config", "init", "--new"]
        try:
            data = self._run_json_allow_error(["config", "show"])
        except LarkCliError as exc:
            return {
                "ok": False,
                "status": "lark_app_config_unknown",
                "failed_code": "lark_app_config_unverified",
                "config_command": config_command,
                "error": str(exc),
            }
        if data.get("ok") is False:
            error = data.get("error") or {}
            if error.get("subtype") == "not_configured" or error.get("type") == "config":
                return {
                    "ok": False,
                    "status": "lark_app_config_pending",
                    "failed_code": "lark_app_config_missing",
                    "config_command": config_command,
                    "detail": data,
                }
            return {
                "ok": False,
                "status": "lark_app_config_unknown",
                "failed_code": "lark_app_config_unverified",
                "config_command": config_command,
                "detail": data,
            }
        return {"ok": True, "configured": True, "detail": data}

    def start_app_config_init(
        self,
        *,
        opener: Callable[[str], bool] | None = None,
        wait_seconds: int = 90,
    ) -> dict[str, Any]:
        args = ["config", "init", "--new"]
        opener = opener or self._open_url
        if self._custom_runner:
            return self._start_app_config_init_with_runner(
                args,
                opener=opener,
                wait_seconds=wait_seconds,
            )
        return self._start_app_config_init_background(
            args,
            opener=opener,
            wait_seconds=wait_seconds,
        )

    def auth_status(self) -> dict[str, Any]:
        return self._run_json(["auth", "status", "--json", "--verify"])

    def auth_login_start(self, opener: Callable[[str], bool] | None = None) -> dict[str, Any]:
        result = self._run_json(["auth", "login", "--recommend", "--no-wait", "--json"])
        url = self._extract_json_field(
            result,
            ("verification_url", "verification_uri_complete", "verification_uri"),
        )
        browser_opened = self._try_open_url(url, opener or self._open_url) if url else False
        if url and "verification_url" not in result:
            result["verification_url"] = url
        result["browser_opened"] = browser_opened
        return result

    def auth_login_complete(self, device_code: str, *, timeout: int = 90) -> dict[str, Any]:
        try:
            return self._run_json(
                ["auth", "login", "--device-code", device_code, "--json"],
                timeout=timeout,
            )
        except LarkCliError as exc:
            if isinstance(exc.__cause__, subprocess.TimeoutExpired):
                return {
                    "ok": False,
                    "status": "feishu_authorization_still_pending",
                    "failed_code": "feishu_authorization_still_pending",
                }
            raise

    def _send_args(
        self,
        *,
        user_id: str | None,
        chat_id: str | None,
        msg_type: str,
        content: dict[str, Any],
    ) -> list[str]:
        if bool(user_id) == bool(chat_id):
            raise ValueError("exactly one of user_id or chat_id is required")
        recipient_flag = "--user-id" if user_id else "--chat-id"
        recipient_value = user_id or chat_id
        assert recipient_value is not None
        return [
            "im",
            "+messages-send",
            "--as",
            "bot",
            recipient_flag,
            recipient_value,
            "--msg-type",
            msg_type,
            "--content",
            json.dumps(content, ensure_ascii=False),
            "--json",
        ]

    def _run_json(self, args: list[str], *, timeout: int | None = None) -> dict[str, Any]:
        command_timeout = self.timeout if timeout is None else timeout
        self.runner_calls.append((list(args), command_timeout))
        try:
            raw = self.runner(args, command_timeout)
        except subprocess.CalledProcessError as exc:
            detail = exc.stderr or exc.stdout or str(exc)
            raise LarkCliError(f"lark-cli failed: {self._redact(str(detail))}") from exc
        except subprocess.TimeoutExpired as exc:
            raise LarkCliError("lark-cli timed out waiting for Feishu authorization") from exc
        except OSError as exc:
            raise LarkCliError(f"lark-cli failed: {self._redact(str(exc))}") from exc

        if isinstance(raw, dict):
            return raw
        if isinstance(raw, subprocess.CompletedProcess):
            if raw.returncode != 0:
                detail = raw.stderr or raw.stdout or f"exit {raw.returncode}"
                raise LarkCliError(f"lark-cli failed: {self._redact(str(detail))}")
            raw = raw.stdout
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", errors="replace")
        if not isinstance(raw, str):
            raise InvalidJsonError(f"lark-cli returned unsupported output: {type(raw).__name__}")
        return self._parse_json(raw)

    def _run_json_allow_error(self, args: list[str]) -> dict[str, Any]:
        self.runner_calls.append((list(args), self.timeout))
        try:
            raw = self.runner(args, self.timeout)
        except subprocess.CalledProcessError as exc:
            raw = exc.stderr or exc.stdout or str(exc)
        except OSError as exc:
            raise LarkCliError(f"lark-cli failed: {self._redact(str(exc))}") from exc

        if isinstance(raw, dict):
            return raw
        if isinstance(raw, subprocess.CompletedProcess):
            raw = (raw.stdout or "") + "\n" + (raw.stderr or "")
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", errors="replace")
        if not isinstance(raw, str):
            raise InvalidJsonError(f"lark-cli returned unsupported output: {type(raw).__name__}")
        return self._parse_json(raw)

    def _run_text(self, args: list[str]) -> str:
        self.runner_calls.append((list(args), self.timeout))
        try:
            raw = self.runner(args, self.timeout)
        except subprocess.CalledProcessError as exc:
            detail = exc.stderr or exc.stdout or str(exc)
            raise LarkCliError(f"lark-cli failed: {self._redact(str(detail))}") from exc
        except OSError as exc:
            raise LarkCliError(f"lark-cli failed: {self._redact(str(exc))}") from exc

        if isinstance(raw, subprocess.CompletedProcess):
            if raw.returncode != 0:
                detail = raw.stderr or raw.stdout or f"exit {raw.returncode}"
                raise LarkCliError(f"lark-cli failed: {self._redact(str(detail))}")
            raw = (raw.stdout or "") + (raw.stderr or "")
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", errors="replace")
        if isinstance(raw, dict):
            return json.dumps(raw, ensure_ascii=False)
        if not isinstance(raw, str):
            raise InvalidJsonError(f"lark-cli returned unsupported output: {type(raw).__name__}")
        return raw

    def _run_subprocess(self, args: list[str], timeout: int) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [self.binary, *args],
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )

    def _start_app_config_init_with_runner(
        self,
        args: list[str],
        *,
        opener: Callable[[str], bool],
        wait_seconds: int,
    ) -> dict[str, Any]:
        self.runner_calls.append((list(args), wait_seconds))
        try:
            raw = self.runner(args, wait_seconds)
        except subprocess.TimeoutExpired as exc:
            stdout = getattr(exc, "stdout", None) or getattr(exc, "output", None)
            text = self._output_text(stdout) + "\n" + self._output_text(exc.stderr)
            return self._pending_app_config_result(
                text,
                opener=opener,
                process_id=None,
                debug_command=[self.binary, *args],
            )
        except subprocess.CalledProcessError as exc:
            detail = exc.stderr or exc.stdout or str(exc)
            return self._app_config_init_failed(detail, debug_command=[self.binary, *args])
        except OSError as exc:
            return self._app_config_init_failed(str(exc), debug_command=[self.binary, *args])

        if isinstance(raw, subprocess.CompletedProcess):
            text = self._output_text(raw.stdout) + "\n" + self._output_text(raw.stderr)
            url = self._extract_url(text)
            browser_opened = self._try_open_url(url, opener) if url else False
            if raw.returncode != 0:
                return self._app_config_init_failed(text, debug_command=[self.binary, *args])
            status = self.app_config_status()
            if status.get("ok"):
                status.update(
                    {
                        "verification_url": url,
                        "browser_opened": browser_opened,
                        "debug_command": [self.binary, *args],
                    }
                )
                return status
            return self._pending_app_config_result(
                text,
                opener=opener,
                process_id=None,
                debug_command=[self.binary, *args],
            )
        text = self._output_text(raw)
        return self._pending_app_config_result(
            text,
            opener=opener,
            process_id=None,
            debug_command=[self.binary, *args],
        )

    def _start_app_config_init_background(
        self,
        args: list[str],
        *,
        opener: Callable[[str], bool],
        wait_seconds: int,
    ) -> dict[str, Any]:
        debug_command = [self.binary, *args]
        fd, log_path = tempfile.mkstemp(prefix="codex-away-lark-config-", suffix=".log")
        log_file = os.fdopen(fd, "wb", buffering=0)
        process: subprocess.Popen[bytes] | None = None
        try:
            process = subprocess.Popen(
                debug_command,
                stdout=log_file,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                start_new_session=True,
            )
        except OSError as exc:
            log_file.close()
            self._unlink_quietly(log_path)
            return self._app_config_init_failed(str(exc), debug_command=debug_command)
        finally:
            try:
                log_file.close()
            except Exception:
                pass

        deadline = time.monotonic() + max(wait_seconds, 1)
        last_text = ""
        try:
            while time.monotonic() < deadline:
                last_text = self._read_text_file(log_path)
                url = self._extract_url(last_text)
                if url:
                    browser_opened = self._try_open_url(url, opener)
                    return {
                        "ok": False,
                        "status": "lark_app_config_browser_pending",
                        "waiting_for": "feishu_browser_confirmation",
                        "verification_url": url,
                        "browser_opened": browser_opened,
                        "process_id": process.pid,
                        "debug_command": debug_command,
                        "developer_detail": {
                            "command": debug_command,
                            "stderr_excerpt": self._redact(last_text[:800]),
                        },
                    }
                return_code = process.poll()
                if return_code is not None:
                    last_text = self._read_text_file(log_path)
                    if return_code == 0:
                        status = self.app_config_status()
                        if status.get("ok"):
                            status.update(
                                {
                                    "verification_url": self._extract_url(last_text),
                                    "browser_opened": False,
                                    "debug_command": debug_command,
                                }
                            )
                            return status
                    return self._app_config_init_failed(last_text, debug_command=debug_command)
                time.sleep(0.2)
            if process.poll() is None:
                process.terminate()
            return {
                "ok": False,
                "failed_code": "lark_app_config_url_missing",
                "status": "lark_app_config_failed",
                "user_message": "飞书配置流程已经启动，但没有拿到可打开的确认链接。请把这段输出交给 Agent 排查。",
                "agent_next_step": "Inspect developer_detail and retry setup feishu after confirming lark-cli config init output.",
                "debug_command": debug_command,
                "developer_detail": {
                    "command": debug_command,
                    "stderr_excerpt": self._redact(last_text[:800]),
                },
            }
        finally:
            self._unlink_quietly(log_path)

    def _parse_json(self, output: str) -> dict[str, Any]:
        decoder = json.JSONDecoder()
        for index, char in enumerate(output):
            if char != "{":
                continue
            try:
                data, _ = decoder.raw_decode(output[index:])
            except json.JSONDecodeError:
                continue
            if isinstance(data, dict):
                return data
        raise InvalidJsonError("lark-cli output did not contain valid JSON")

    def _pending_app_config_result(
        self,
        text: str,
        *,
        opener: Callable[[str], bool],
        process_id: int | None,
        debug_command: list[str],
    ) -> dict[str, Any]:
        url = self._extract_url(text)
        browser_opened = self._try_open_url(url, opener) if url else False
        if not url:
            return {
                "ok": False,
                "failed_code": "lark_app_config_url_missing",
                "status": "lark_app_config_failed",
                "user_message": "飞书配置流程已经启动，但没有拿到可打开的确认链接。请把这段输出交给 Agent 排查。",
                "agent_next_step": "Inspect developer_detail and retry setup feishu after confirming lark-cli config init output.",
                "debug_command": debug_command,
                "developer_detail": {
                    "command": debug_command,
                    "stderr_excerpt": self._redact(text[:800]),
                },
            }
        return {
            "ok": False,
            "status": "lark_app_config_browser_pending",
            "waiting_for": "feishu_browser_confirmation",
            "verification_url": url,
            "browser_opened": browser_opened,
            "process_id": process_id,
            "debug_command": debug_command,
            "developer_detail": {
                "command": debug_command,
                "stderr_excerpt": self._redact(text[:800]),
            },
        }

    def _app_config_init_failed(self, detail: Any, *, debug_command: list[str]) -> dict[str, Any]:
        return {
            "ok": False,
            "failed_code": "lark_app_config_init_failed",
            "status": "lark_app_config_failed",
            "user_message": "飞书官方配置流程启动失败，当前还不能继续安装飞书通知。",
            "agent_next_step": "Inspect developer_detail, then decide whether to retry, reinstall the pinned lark-cli, or use the manual advanced path.",
            "debug_command": debug_command,
            "developer_detail": {
                "command": debug_command,
                "redacted_error": self._redact(self._output_text(detail)[:800]),
            },
        }

    def _try_open_url(self, url: str | None, opener: Callable[[str], bool]) -> bool:
        if not url:
            return False
        try:
            return bool(opener(url))
        except Exception:
            return False

    def _open_url(self, url: str) -> bool:
        try:
            if webbrowser.open(url):
                return True
        except Exception:
            pass
        command: list[str] | None = None
        if os.name == "nt":
            try:
                os.startfile(url)  # type: ignore[attr-defined]
                return True
            except Exception:
                return False
        if shutil.which("open"):
            command = ["open", url]
        elif shutil.which("xdg-open"):
            command = ["xdg-open", url]
        if not command:
            return False
        try:
            subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return True
        except OSError:
            return False

    def _extract_url(self, text: str) -> str | None:
        text = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", text)
        match = re.search(r"https?://[^\s<>()\[\]\"']+", text)
        if not match:
            return None
        return match.group(0).rstrip(".,;")

    def _extract_json_field(self, value: Any, keys: tuple[str, ...]) -> str | None:
        if isinstance(value, dict):
            for key in keys:
                candidate = value.get(key)
                if isinstance(candidate, str) and candidate:
                    return candidate
            for child in value.values():
                found = self._extract_json_field(child, keys)
                if found:
                    return found
        elif isinstance(value, list):
            for child in value:
                found = self._extract_json_field(child, keys)
                if found:
                    return found
        return None

    def _output_text(self, value: Any) -> str:
        if value is None:
            return ""
        if isinstance(value, bytes):
            return value.decode("utf-8", errors="replace")
        if isinstance(value, str):
            return value
        if isinstance(value, subprocess.CompletedProcess):
            return self._output_text(value.stdout) + "\n" + self._output_text(value.stderr)
        return str(value)

    def _read_text_file(self, path: str) -> str:
        try:
            with open(path, "rb") as handle:
                return handle.read().decode("utf-8", errors="replace")
        except OSError:
            return ""

    def _unlink_quietly(self, path: str) -> None:
        try:
            os.unlink(path)
        except OSError:
            pass

    def _map_send_result(self, data: dict[str, Any]) -> SendResult:
        payload = data.get("data", data)
        return SendResult(
            message_id=str(payload.get("message_id", "")),
            chat_id=str(payload.get("chat_id", "")),
        )

    def _map_message(self, item: dict[str, Any]) -> LarkMessage:
        return LarkMessage(
            message_id=str(item.get("message_id", "")),
            reply_to=self._reply_to(item),
            msg_type=str(item.get("msg_type", "")),
            content_text=self._content_text(item),
            sender_type=(item.get("sender") or {}).get("sender_type"),
            create_time=item.get("create_time"),
        )

    def _reply_to(self, item: dict[str, Any]) -> str | None:
        for key in (
            "reply_to",
            "reply_to_message_id",
            "parent_id",
            "parent_message_id",
            "root_id",
            "root_message_id",
        ):
            value = item.get(key)
            extracted = self._string_value(value)
            if extracted:
                return extracted
        return None

    def _content_text(self, item: dict[str, Any]) -> str:
        content = item.get("content")
        if content is None:
            content = (item.get("body") or {}).get("content", "")
        original_content = content
        try:
            if isinstance(content, str):
                content = json.loads(content)
        except json.JSONDecodeError:
            return content
        extracted = self._extract_content_text(content)
        if extracted:
            return extracted
        return original_content if isinstance(original_content, str) else ""

    def _extract_content_text(self, value: Any) -> str:
        if isinstance(value, dict):
            text = value.get("text")
            if isinstance(text, str) and text:
                return text
            collected: list[str] = []
            for child in value.values():
                child_text = self._extract_content_text(child)
                if child_text:
                    collected.append(child_text)
            return "".join(collected)
        if isinstance(value, list):
            collected = []
            for child in value:
                child_text = self._extract_content_text(child)
                if child_text:
                    collected.append(child_text)
            return "".join(collected)
        return ""

    def _string_value(self, value: Any) -> str | None:
        if isinstance(value, str) and value:
            return value
        if isinstance(value, dict):
            for key in ("message_id", "id", "parent_id", "root_id"):
                nested = value.get(key)
                if isinstance(nested, str) and nested:
                    return nested
        return None

    def _redact(self, value: str) -> str:
        value = re.sub(r"\b(?:ou|oc|om|cli|app)_[A-Za-z0-9_\-]+\b", "[redacted-id]", value)
        return re.sub(r"\b[A-Za-z0-9_\-]{16,}\b", "[redacted-token]", value)
