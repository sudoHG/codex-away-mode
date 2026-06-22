from __future__ import annotations

import json
import re
import subprocess
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

    def config_init_new(self) -> dict[str, Any]:
        return self._run_json(["config", "init", "--new", "--json"])

    def auth_status(self) -> dict[str, Any]:
        return self._run_json(["auth", "status", "--json", "--verify"])

    def auth_login_start(self) -> dict[str, Any]:
        return self._run_json(["auth", "login", "--recommend", "--no-wait", "--json"])

    def auth_login_complete(self, device_code: str) -> dict[str, Any]:
        return self._run_json(["auth", "login", "--device-code", device_code, "--json"])

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

    def _run_json(self, args: list[str]) -> dict[str, Any]:
        self.runner_calls.append((list(args), self.timeout))
        try:
            raw = self.runner(args, self.timeout)
        except subprocess.CalledProcessError as exc:
            detail = exc.stderr or exc.stdout or str(exc)
            raise LarkCliError(f"lark-cli failed: {self._redact(str(detail))}") from exc
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
            check=True,
            capture_output=True,
            text=True,
            timeout=timeout,
        )

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
