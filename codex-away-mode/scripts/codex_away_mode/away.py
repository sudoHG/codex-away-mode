from __future__ import annotations

import hashlib
import hmac
import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from . import cards
from .config import AppConfig, load_config, save_config
from .lark import LarkMessage
from .thread_context import resolve_card_title_context


@dataclass(frozen=True)
class ReplyClass:
    kind: str
    text: str


@dataclass(frozen=True)
class RouteDecision:
    kind: str
    message: LarkMessage | None = None


def classify_reply(text: str) -> ReplyClass:
    normalized = (text or "").strip()
    if normalized == "/结束等待":
        return ReplyClass(kind="end", text=normalized)
    if normalized == "/延长等待":
        return ReplyClass(kind="extend", text=normalized)
    if normalized == "/状态":
        return ReplyClass(kind="status", text=normalized)
    if normalized.startswith("/"):
        return ReplyClass(kind="unknown_command", text=normalized)
    return ReplyClass(kind="prompt", text=normalized)


def route_message(message: LarkMessage, card_message_id: str) -> RouteDecision:
    if message.sender_type in {"bot", "app"}:
        return RouteDecision(kind="ignored")
    if message.reply_to == card_message_id:
        return RouteDecision(kind="card_reply", message=message)
    if message.reply_to:
        return RouteDecision(kind="ignored")
    return RouteDecision(kind="ordinary_dm", message=message)


class AwayWaiter:
    def __init__(
        self,
        *,
        lark: Any,
        store: Any,
        clock: Any,
        config: AppConfig,
        config_path: Any | None = None,
        install_store: Any | None = None,
    ) -> None:
        self.lark = lark
        self.store = store
        self.install_store = install_store
        self.clock = clock
        self.config = config
        self.config_path = config_path
        self.waiter_owner = f"waiter_{uuid.uuid4().hex}"

    def wait(self, context: dict[str, Any]) -> dict[str, Any]:
        chat_id = self.config.feishu_chat_id
        if not chat_id:
            return {
                "status": "error",
                "error_code": "missing_feishu_chat_id",
                "message": "Missing Feishu chat binding. Run codex-away-mode notify test --json or installation verification first.",
            }
        if context.get("resume"):
            return self._resume_wait(context, chat_id=chat_id)
        return self._start_wait(context, chat_id=chat_id)

    def _start_wait(self, context: dict[str, Any], *, chat_id: str) -> dict[str, Any]:
        started_at = _ensure_utc(self.clock.now())
        wait_minutes = int(context.get("wait_minutes") or self.config.default_wait_minutes)
        deadline = started_at + timedelta(minutes=wait_minutes)
        session_id = self.store.new_session_id()
        guarded_window = not self.config.route_key_verified or not self.config.multi_window_enabled
        if guarded_window and not self.store.reserve_away_window_guard(
            chat_id,
            owner=session_id,
            lock_expires_at=deadline.isoformat(),
            now=started_at.isoformat(),
        ):
            return _active_away_window_error()
        session_id = self.store.create_away_session(
            project=str(context.get("project") or "Codex Away Mode"),
            cwd=str(context.get("cwd") or ""),
            task=str(context.get("task") or ""),
            started_at=started_at.isoformat(),
            completed=_optional_text(context.get("completed")),
            changed=_optional_text(context.get("changed")),
            verification=_optional_text(context.get("verification")),
            unverified=_optional_text(context.get("unverified")),
            need_user=_optional_text(context.get("need_user")),
            deadline_at=deadline.isoformat(),
            codex_session_id=_optional_text(context.get("codex_session_id")),
            session_id=session_id,
        )

        card_result = self.lark.send_interactive_card(
            chat_id=chat_id,
            card=cards.away_card(
                context=context,
                deadline=deadline,
                title_context=self._title_context(context=context),
            ),
        )
        window_id = self._create_window(
            chat_id=chat_id,
            session_id=session_id,
            card_message_id=card_result.message_id,
            started_at=started_at,
            deadline=deadline,
            guard_reserved=guarded_window,
        )
        if window_id is None:
            return _active_away_window_error()

        self.store.record_card(
            card_message_id=card_result.message_id,
            window_id=window_id,
            session_id=session_id,
            card_kind="initial",
            status="active",
            sent_at=started_at.isoformat(),
        )
        session = self.store.get_away_session(session_id) or {}
        window = self.store.get_window(window_id) or {}
        return self._poll_until_result(session=session, window=window, context=context, deadline=deadline)

    def _resume_wait(self, context: dict[str, Any], *, chat_id: str) -> dict[str, Any]:
        session_id = str(context.get("resume") or "")
        session = self.store.get_active_session_for_resume(session_id)
        if not session:
            return {
                "status": "error",
                "error_code": "session_not_active",
                "keep_waiting": False,
            }
        token_error = self._validate_resume_token(
            session_id=session_id,
            resume_token=context.get("resume_token"),
        )
        if token_error is not None:
            return token_error
        self.store.clear_resume_token(session_id)
        window = self.store.get_window(str(session.get("active_window_id") or ""))
        if not window:
            return {
                "status": "error",
                "error_code": "active_window_missing",
                "away_session_id": session_id,
                "keep_waiting": False,
            }
        deadline = _parse_time(str(session.get("deadline_at") or window["deadline_at"]))
        explicit_extend_minutes = int(context.get("extend_minutes") or 0)
        if explicit_extend_minutes > 0:
            deadline = deadline + timedelta(minutes=explicit_extend_minutes)
            self.store.extend_window(window["window_id"], new_deadline_at=deadline.isoformat())
            self.lark.send_text(
                chat_id=chat_id,
                text=cards.command_feedback_text("extend", new_deadline=deadline),
            )
            session = self.store.get_away_session(session_id) or session
            window = self.store.get_window(window["window_id"]) or window
        now = _ensure_utc(self.clock.now())
        if now >= deadline:
            return self._close_for_timeout(
                session=session,
                window=window,
                deadline=now,
                context=context,
            )

        history = self.lark.list_messages(chat_id=chat_id)
        backlog_result, deadline = self._drain_backlog_before_rotation(
            session=session,
            window=window,
            deadline=deadline,
            messages=history,
        )
        if backlog_result is not None:
            return backlog_result

        progress_context = {**session, **context}
        card_result = self.lark.send_interactive_card(
            chat_id=chat_id,
            card=cards.away_progress_card(
                project=str(progress_context.get("project") or "Codex Away Mode"),
                cwd=str(progress_context.get("cwd") or ""),
                completed=str(progress_context.get("completed") or "无"),
                changed=str(progress_context.get("changed") or "无"),
                verification=str(progress_context.get("verification") or "未运行"),
                unverified=str(progress_context.get("unverified") or "无"),
                need_user=str(progress_context.get("need_user") or "请回复这张卡片继续。"),
                deadline=deadline,
                title_context=self._title_context(context=progress_context, session=session),
            ),
        )
        sent_at = _ensure_utc(self.clock.now()).isoformat()
        self.store.rotate_window_card(
            window_id=window["window_id"],
            new_card_message_id=card_result.message_id,
            card_kind="progress",
            sent_at=sent_at,
        )
        window = self.store.get_window(window["window_id"]) or window
        session = self.store.get_away_session(session_id) or session
        return self._poll_until_result(
            session=session,
            window=window,
            context=progress_context,
            deadline=deadline,
            initial_messages=history,
        )

    def _poll_until_result(
        self,
        *,
        session: dict[str, Any],
        window: dict[str, Any],
        context: dict[str, Any],
        deadline: datetime,
        initial_messages: list[LarkMessage] | None = None,
    ) -> dict[str, Any]:
        chat_id = window["recipient_id"]
        project = str(context.get("project") or session.get("project") or "Codex Away Mode")
        title_context = self._title_context(context=context, session=session)
        wait_minutes_remaining = (deadline - _ensure_utc(self.clock.now())).total_seconds() / 60
        reminder_allowed = wait_minutes_remaining > int(self.config.pre_timeout_reminder_minutes)
        reminder_at = deadline - timedelta(minutes=int(self.config.pre_timeout_reminder_minutes))
        pending_messages = list(initial_messages or [])
        while True:
            now = _ensure_utc(self.clock.now())
            self._renew_waiter_lease(str(session["session_id"]), now=now)
            window = self.store.get_window(window["window_id"]) or window

            if (
                reminder_allowed
                and not window.get("reminder_sent_at")
                and now >= reminder_at
                and now < deadline
            ):
                reminder_result = self.lark.send_interactive_card(
                    chat_id=chat_id,
                    card=cards.pre_timeout_reminder_card(
                        project=project,
                        deadline=deadline,
                        minutes_left=int(self.config.pre_timeout_reminder_minutes),
                        title_context=title_context,
                    ),
                )
                self.store.record_card(
                    card_message_id=reminder_result.message_id,
                    window_id=window["window_id"],
                    session_id=window["session_id"],
                    card_kind="pre_timeout_reminder",
                    status="active",
                    sent_at=now.isoformat(),
                )
                self.store.mark_reminder_sent(window["window_id"], sent_at=now.isoformat())

            if now >= deadline:
                return self._close_for_timeout(session=session, window=window, deadline=now, context=context)

            if pending_messages:
                messages = pending_messages
                pending_messages = []
            else:
                messages = self.lark.list_messages(chat_id=chat_id)

            for message in messages:
                result = self._handle_routed_message(
                    chat_id=chat_id,
                    session=session,
                    window=window,
                    message=message,
                    deadline=deadline,
                )
                if result["status"] in {"ignored", "continue"}:
                    continue
                if result["status"] == "extend":
                    deadline = result["deadline"]
                    reminder_at = deadline - timedelta(minutes=int(self.config.pre_timeout_reminder_minutes))
                    reminder_allowed = True
                    continue
                if result["status"] in {"reply", "ended", "error"}:
                    self.store.release_waiter_lease(str(session["session_id"]), owner=self.waiter_owner)
                return result

            self.clock.sleep(int(self.config.poll_interval_seconds))

    def _drain_backlog_before_rotation(
        self,
        *,
        session: dict[str, Any],
        window: dict[str, Any],
        deadline: datetime,
        messages: list[LarkMessage],
    ) -> tuple[dict[str, Any] | None, datetime]:
        eligible: list[LarkMessage] = []
        for message in messages:
            if message.sender_type in {"bot", "app"}:
                continue
            if message.reply_to == window["card_message_id"]:
                eligible.append(message)
                continue
            if message.reply_to:
                card = self.store.find_card(message.reply_to)
                if card and card.get("status") == "retired" and _message_time(message) < _parse_time(card["retired_at"]):
                    eligible.append(message)
        for message in sorted(eligible, key=_message_time):
            result = self._handle_routed_message(
                chat_id=window["recipient_id"],
                session=session,
                window=window,
                message=message,
                deadline=deadline,
                allow_historical_retired=True,
            )
            if result["status"] in {"ignored", "continue", "extend"}:
                if result["status"] == "extend":
                    deadline = result["deadline"]
                continue
            return result, deadline
        return None, deadline

    def _handle_routed_message(
        self,
        *,
        chat_id: str,
        session: dict[str, Any],
        window: dict[str, Any],
        message: LarkMessage,
        deadline: datetime,
        allow_historical_retired: bool = False,
    ) -> dict[str, Any]:
        if message.sender_type in {"bot", "app"}:
            return {"status": "ignored"}
        if message.reply_to == window["card_message_id"]:
            self._mark_route_key_verified(source="live_card_reply")
            return self._handle_card_reply(
                chat_id=chat_id,
                session=session,
                window=window,
                message=message,
                deadline=deadline,
            )
        if message.reply_to:
            card = self.store.find_card(message.reply_to)
            if not card:
                return {"status": "ignored"}
            if card["status"] == "active" and card["window_id"] == window["window_id"]:
                self._mark_route_key_verified(source="live_card_reply")
                return self._handle_card_reply(
                    chat_id=chat_id,
                    session=session,
                    window=window,
                    message=message,
                    deadline=deadline,
                )
            if card["status"] == "retired":
                if allow_historical_retired or _message_time(message) < _parse_time(card["retired_at"]):
                    return self._handle_card_reply(
                        chat_id=chat_id,
                        session=session,
                        window=self.store.get_window(card["window_id"]) or window,
                        message=message,
                        deadline=deadline,
                    )
                if self.store.mark_processed(
                    message.message_id,
                    "card_reply",
                    card["window_id"],
                    "retired_card_reply",
                    _text_hash(message.content_text),
                ):
                    self.lark.send_text(chat_id=chat_id, text=cards.retired_card_reply_text())
                return {"status": "continue"}
            if card["status"] == "closed":
                self.store.mark_processed(
                    message.message_id,
                    "card_reply",
                    card["window_id"],
                    "late_closed_reply",
                    _text_hash(message.content_text),
                )
            return {"status": "ignored"}
        self._handle_ordinary_dm(
            chat_id=chat_id,
            window_id=window["window_id"],
            message=message,
        )
        return {"status": "continue"}

    def _create_window(
        self,
        *,
        chat_id: str,
        session_id: str,
        card_message_id: str,
        started_at: datetime,
        deadline: datetime,
        guard_reserved: bool = False,
    ) -> str | None:
        if not self.config.route_key_verified or not self.config.multi_window_enabled:
            if guard_reserved:
                return self.store.create_away_window(
                    session_id=session_id,
                    recipient_id=chat_id,
                    card_message_id=card_message_id,
                    created_at=started_at.isoformat(),
                    deadline_at=deadline.isoformat(),
                )
            return self.store.create_away_window_guarded(
                chat_id,
                session_id=session_id,
                card_message_id=card_message_id,
                created_at=started_at.isoformat(),
                deadline_at=deadline.isoformat(),
                owner=session_id,
                lock_expires_at=deadline.isoformat(),
                now=started_at.isoformat(),
            )
        return self.store.create_away_window(
            session_id=session_id,
            recipient_id=chat_id,
            card_message_id=card_message_id,
            created_at=started_at.isoformat(),
            deadline_at=deadline.isoformat(),
        )

    def _handle_card_reply(
        self,
        *,
        chat_id: str,
        session: dict[str, Any],
        window: dict[str, Any],
        message: LarkMessage,
        deadline: datetime,
    ) -> dict[str, Any]:
        session_id = window["session_id"]
        window_id = window["window_id"]
        classified = classify_reply(message.content_text)
        if classified.kind == "prompt":
            if not self.store.mark_processed(
                message.message_id,
                "card_reply",
                window_id,
                "deliver_prompt",
                _text_hash(message.content_text),
            ):
                return {"status": "continue"}
            delivered_at = _ensure_utc(self.clock.now()).isoformat()
            self.store.mark_prompt_delivered(
                session_id=session_id,
                window_id=window_id,
                message_id=message.message_id,
                processed_at=delivered_at,
            )
            try:
                self.lark.add_reaction(message_id=message.message_id, emoji_type="Get")
            except Exception:
                self.lark.send_text(chat_id=chat_id, text="收到，已把这条卡片回复交给 Codex。")
            return {
                "status": "reply",
                "reply_text": classified.text,
                "keep_waiting": True,
                "away_session_id": session_id,
                "window_id": window_id,
                "deadline_at": deadline.isoformat(),
                "resume_token": self._issue_resume_token(session_id),
            }

        if classified.kind == "extend":
            new_deadline = deadline + timedelta(minutes=int(self.config.extend_minutes))
            if self.store.mark_processed(
                message.message_id,
                "card_reply",
                window_id,
                "extend",
                _text_hash(message.content_text),
            ):
                self.store.extend_window(window_id, new_deadline_at=new_deadline.isoformat())
                self.lark.send_text(
                    chat_id=chat_id,
                    text=cards.command_feedback_text("extend", new_deadline=new_deadline),
                )
                return {"status": "extend", "deadline": new_deadline}
            return {"status": "continue"}

        if classified.kind == "status":
            if self.store.mark_processed(
                message.message_id,
                "card_reply",
                window_id,
                "status",
                _text_hash(message.content_text),
            ):
                self.lark.send_text(
                    chat_id=chat_id,
                    text=cards.command_feedback_text(
                        "status",
                        deadline=deadline,
                    ),
                )
            return {"status": "continue"}

        if classified.kind == "end":
            if self.store.mark_processed(
                message.message_id,
                "card_reply",
                window_id,
                "end",
                _text_hash(message.content_text),
            ):
                self.lark.send_text(chat_id=chat_id, text=cards.command_feedback_text("end"))
            closed_at = _ensure_utc(self.clock.now()).isoformat()
            self.lark.send_interactive_card(
                chat_id=chat_id,
                card=cards.user_ended_card(
                    project=str(session.get("project") or "Codex Away Mode"),
                    ended_at=closed_at,
                    title_context=self._title_context(session=session),
                ),
            )
            self.store.close_away_session(
                session_id,
                status="ended_by_user",
                reason="user_requested",
                closed_at=closed_at,
            )
            self.store.clear_resume_token(session_id)
            self.store.close_active_card(window_id, closed_at=closed_at)
            self.store.close_window(
                window_id,
                status="closed",
                reason="user_requested",
                closed_at=closed_at,
            )
            return {"status": "ended", "away_session_id": session_id, "keep_waiting": False}

        if self.store.mark_processed(
            message.message_id,
            "card_reply",
            window_id,
            "unknown_command",
            _text_hash(message.content_text),
        ):
            self.lark.send_text(
                chat_id=chat_id,
                text=cards.command_feedback_text("unknown", command=classified.text),
            )
        return {"status": "continue"}

    def _close_for_timeout(
        self,
        *,
        session: dict[str, Any],
        window: dict[str, Any],
        deadline: datetime,
        context: dict[str, Any],
    ) -> dict[str, Any]:
        chat_id = window["recipient_id"]
        project = str(context.get("project") or session.get("project") or "Codex Away Mode")
        self.lark.send_interactive_card(
            chat_id=chat_id,
            card=cards.timeout_card(
                project=project,
                deadline=deadline,
                title_context=self._title_context(context=context, session=session),
            ),
        )
        closed_at = _ensure_utc(self.clock.now()).isoformat()
        self.store.close_away_session(
            window["session_id"],
            status="timed_out",
            reason="timeout",
            closed_at=closed_at,
        )
        self.store.clear_resume_token(window["session_id"])
        self.store.close_active_card(window["window_id"], closed_at=closed_at)
        self.store.close_window(
            window["window_id"],
            status="timed_out",
            reason="timeout",
            closed_at=closed_at,
        )
        return {
            "status": "timeout",
            "away_session_id": window["session_id"],
            "keep_waiting": False,
        }

    def _handle_ordinary_dm(
        self,
        *,
        chat_id: str,
        window_id: str,
        message: LarkMessage,
    ) -> None:
        decision = self.store.record_ordinary_dm_event(
            message_id=message.message_id,
            window_id=window_id,
            recipient_id=chat_id,
            active_window_count=self.store.active_window_count(chat_id),
            message_text_hash=_text_hash(message.content_text),
            now=_ensure_utc(self.clock.now()).isoformat(),
        )
        if decision == "send_hint":
            self.lark.send_text(chat_id=chat_id, text=cards.ordinary_dm_hint_text())

    def _renew_waiter_lease(self, session_id: str, *, now: datetime) -> None:
        ttl_seconds = max(30, int(self.config.poll_interval_seconds) * 3 + 10)
        expires_at = now + timedelta(seconds=ttl_seconds)
        self.store.renew_waiter_lease(
            session_id,
            owner=self.waiter_owner,
            now=now.isoformat(),
            expires_at=expires_at.isoformat(),
        )

    def _mark_route_key_verified(self, *, source: str) -> None:
        if self.config.route_key_verified:
            if self.install_store is None:
                return
            if self.install_store.route_key_state().get("status") == "verified":
                return
        self.config.route_key_verified = True
        self.config.multi_window_enabled = True
        if self.config_path is not None:
            saved = load_config(self.config_path)
            saved.route_key_verified = True
            saved.multi_window_enabled = True
            save_config(self.config_path, saved)
        if self.install_store is not None:
            self.install_store.set_route_key_state(
                status="verified",
                source=source,
                verified_at=_ensure_utc(self.clock.now()).isoformat(),
            )

    def _title_context(
        self,
        *,
        context: dict[str, Any] | None = None,
        session: dict[str, Any] | None = None,
    ):
        merged: dict[str, Any] = {}
        if session:
            merged.update(session)
        if context:
            merged.update(context)
        return resolve_card_title_context(
            cwd=_optional_text(merged.get("cwd")),
            explicit_codex_session_id=_optional_text(merged.get("codex_session_id")),
        )

    def _issue_resume_token(self, session_id: str) -> str:
        token = "rt_" + secrets.token_urlsafe(24)
        self.store.set_resume_token_hash(
            session_id=session_id,
            token_hash=_token_hash(token),
            created_at=_ensure_utc(self.clock.now()).isoformat(),
        )
        return token

    def _validate_resume_token(
        self,
        *,
        session_id: str,
        resume_token: Any,
    ) -> dict[str, Any] | None:
        if not resume_token:
            return _resume_token_error("resume_token_required", session_id)
        expected_hash = self.store.get_resume_token_hash(session_id)
        if not expected_hash:
            return _resume_token_error("resume_token_required", session_id)
        actual_hash = _token_hash(str(resume_token))
        if not hmac.compare_digest(actual_hash, expected_hash):
            return _resume_token_error("resume_token_invalid", session_id)
        return None


def _text_hash(text: str) -> str:
    digest = hashlib.sha256((text or "").encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def _token_hash(token: str) -> str:
    digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def _resume_token_error(error_code: str, session_id: str) -> dict[str, Any]:
    if error_code == "resume_token_required":
        message = "不能接管这个 Away Mode 会话。只有刚收到这张卡片回复的 Codex 回合才能继续等待。"
    else:
        message = "这个 Away Mode 恢复凭据无效，不能继续等待。"
    return {
        "status": "error",
        "error_code": error_code,
        "away_session_id": session_id,
        "keep_waiting": False,
        "message": message,
        "agent_next_step": "如果用户想开启新的 Away Mode，请使用 away start；不要 resume 其他会话。",
    }


def _active_away_window_error() -> dict[str, Any]:
    return {
        "status": "error",
        "error_code": "active_away_window_exists",
        "message": "当前飞书会话里已经有一个 Away Mode 回复窗口在等待。为了避免把回复送错，请先结束当前窗口，或等它超时后再重新开启。",
        "agent_next_step": "告诉用户：请先回复当前 Away Mode 卡片；如果不需要继续等待，可以在当前卡片回复 /结束等待。",
    }


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    return str(value)


def _ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _parse_time(value: str | int | float | None) -> datetime:
    if value is None:
        return datetime.min.replace(tzinfo=timezone.utc)
    text = str(value)
    if text.isdigit():
        number = int(text)
        if number > 10_000_000_000:
            number = number / 1000
        return datetime.fromtimestamp(number, tz=timezone.utc)
    return _ensure_utc(datetime.fromisoformat(text.replace("Z", "+00:00")))


def _message_time(message: LarkMessage) -> datetime:
    return _parse_time(message.create_time)
