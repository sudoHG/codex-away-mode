from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import cards
from .config import load_config
from .state import StateStore, open_install_store


def run_away_status(
    paths,
    *,
    session_id: str | None = None,
    cwd: str | None = None,
    active_only: bool = True,
    include_closed: bool = False,
    include_internal_ids: bool = False,
    limit: int = 20,
    now: datetime | None = None,
) -> dict[str, Any]:
    now = _ensure_utc(now or datetime.now(timezone.utc))
    runtime_state_path = Path(paths.runtime_state_path)
    install_store = open_install_store(paths)
    config = load_config(paths.config_path)
    if not runtime_state_path.exists():
        items: list[dict[str, Any]] = []
        runtime_store_present = False
    else:
        store = StateStore(runtime_state_path)
        sessions = store.list_away_sessions(
            session_id=session_id,
            cwd=cwd,
            active_only=active_only,
            include_closed=include_closed,
            limit=limit,
        )
        items = []
        for session in sessions:
            item = _session_status(
                store,
                session,
                config=config,
                install_store=install_store,
                now=now,
            )
            items.append(item if include_internal_ids else _without_internal_ids(item))
        runtime_store_present = True
    return {
        "ok": True,
        "runtime_store_present": runtime_store_present,
        "sessions": items,
        "summary": {
            "active_count": sum(1 for item in items if item["status"] == "active"),
            "waiting_count": sum(1 for item in items if item["status"] == "waiting"),
            "waiting_paused_count": sum(1 for item in items if item["status"] == "waiting_paused"),
            "stale_count": sum(1 for item in items if item.get("stale")),
        },
    }


def _session_status(
    store: StateStore,
    session: dict[str, Any],
    *,
    config,
    install_store: StateStore,
    now: datetime,
) -> dict[str, Any]:
    window = store.get_window(str(session.get("active_window_id") or "")) if session.get("active_window_id") else None
    deadline = _parse_time(session.get("deadline_at") or (window or {}).get("deadline_at"))
    lease = store.get_waiter_lease(str(session["session_id"]))
    warnings: list[str] = []
    active = session.get("status") in {"active", "waiting", "waiting_paused"}

    if active and deadline and deadline <= now:
        warnings.append("deadline_passed_but_not_closed")
    if active and not window:
        warnings.append("active_session_without_window")
    if window and window.get("status") in {"waiting", "waiting_paused"} and not window.get("card_message_id"):
        warnings.append("waiting_window_without_card")

    lease_status = _lease_status(lease, now)
    if lease_status == "expired":
        warnings.append("expired_waiter_lease")
    if not config.route_key_verified:
        warnings.append("route_key_not_verified")
    e2e_state = install_store.get_install_state("e2e_notify", {})
    if e2e_state.get("status") != "verified":
        warnings.append("notify_delivery_unverified")
    elif install_store.install_status().get("status") != "installed":
        warnings.append("hook_trust_unverified")

    return {
        "session_id": session["session_id"],
        "project": session.get("project"),
        "cwd": session.get("cwd"),
        "status": session.get("status"),
        "deadline_at": deadline.isoformat() if deadline else None,
        "deadline_display": cards.away_time(deadline) if deadline else None,
        "active_window_id": window.get("window_id") if window else None,
        "active_card_present": bool(window and window.get("card_message_id")),
        "close_reason": session.get("close_reason"),
        "waiter_lease": {
            "status": lease_status,
            "expires_at": lease.get("expires_at") if lease else None,
        },
        "lock_status": _window_lock_status(store, window, now),
        "warnings": warnings,
        "resume_allowed_for_current_turn": False,
        "stale": bool(active and deadline and deadline <= now),
    }


def _without_internal_ids(item: dict[str, Any]) -> dict[str, Any]:
    public = dict(item)
    public.pop("session_id", None)
    public.pop("active_window_id", None)
    return public


def _window_lock_status(store: StateStore, window: dict[str, Any] | None, now: datetime) -> str:
    if not window:
        return "missing_window"
    lock = store.get_runtime_lock(f"away-window:{window['recipient_id']}")
    if not lock:
        return "not_required"
    expires_at = _parse_time(lock.get("expires_at"))
    if expires_at and expires_at <= now:
        return "stale"
    return "ok"


def _lease_status(lease: dict[str, Any] | None, now: datetime) -> str:
    if not lease:
        return "missing"
    expires_at = _parse_time(lease.get("expires_at"))
    if expires_at and expires_at > now:
        return "alive"
    return "expired"


def _parse_time(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return _ensure_utc(parsed)


def _ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
