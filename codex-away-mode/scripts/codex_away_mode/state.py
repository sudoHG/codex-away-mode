from __future__ import annotations

import sqlite3
import uuid
import json
import hashlib
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


INSTALL_STATE_KEYS = ("status", "route_key", "e2e_notify")


def open_install_store(paths) -> "StateStore":
    migrate_legacy_install_state(paths)
    return StateStore(Path(paths.install_state_path))


def migrate_legacy_install_state(paths) -> bool:
    install_path = Path(paths.install_state_path)
    if install_path.exists():
        return False

    values: dict[str, Any] = {}
    for legacy_path in _legacy_install_state_paths(paths):
        if not legacy_path.exists() or legacy_path == install_path:
            continue
        values.update(_read_legacy_install_state(legacy_path))

    if not values:
        return False

    install_store = StateStore(install_path)
    for key, value in values.items():
        install_store.set_install_state(key, value)
    return True


def _legacy_install_state_paths(paths) -> list[Path]:
    if not hasattr(paths, "codex_home"):
        return []
    old_data_dir = Path(paths.codex_home) / "codex-away-mode"
    return [
        old_data_dir / "install-state.sqlite",
        old_data_dir / "state.sqlite",
    ]


def _read_legacy_install_state(legacy_path: Path) -> dict[str, Any]:
    values: dict[str, Any] = {}
    uri = legacy_path.resolve().as_uri() + "?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True)
    except sqlite3.Error:
        return values
    try:
        conn.row_factory = sqlite3.Row
        placeholders = ",".join("?" for _ in INSTALL_STATE_KEYS)
        rows = conn.execute(
            f"SELECT key, value FROM install_state WHERE key IN ({placeholders})",
            INSTALL_STATE_KEYS,
        ).fetchall()
    except sqlite3.Error:
        return values
    finally:
        conn.close()

    for row in rows:
        try:
            values[row["key"]] = json.loads(row["value"])
        except (TypeError, json.JSONDecodeError):
            continue
    return values


class StateStore:
    def __init__(self, path: str | Path, *, busy_timeout_ms: int = 5000) -> None:
        self.path = Path(path)
        self.busy_timeout_ms = busy_timeout_ms
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(
            self.path,
            timeout=max(self.busy_timeout_ms / 1000, 5),
            isolation_level=None,
        )
        conn.row_factory = sqlite3.Row
        conn.execute(f"PRAGMA busy_timeout = {self.busy_timeout_ms}")
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def _initialize(self) -> None:
        with self._connect() as conn:
            conn.execute("PRAGMA journal_mode = WAL")
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS install_state (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS notification_events (
                    event_id TEXT PRIMARY KEY,
                    event_kind TEXT NOT NULL,
                    recipient_id TEXT,
                    created_at TEXT NOT NULL,
                    status TEXT NOT NULL,
                    diagnostic_json TEXT
                );

                CREATE TABLE IF NOT EXISTS approval_notifications (
                    dedupe_key TEXT PRIMARY KEY,
                    session_id TEXT,
                    turn_id TEXT,
                    cwd_hash TEXT,
                    tool_name TEXT,
                    command_hash TEXT NOT NULL,
                    first_seen_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL,
                    sent_at TEXT,
                    suppressed_count INTEGER NOT NULL DEFAULT 0,
                    status TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS away_sessions (
                    session_id TEXT PRIMARY KEY,
                    project TEXT NOT NULL,
                    cwd TEXT NOT NULL,
                    task TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    deadline_at TEXT,
                    active_window_id TEXT,
                    codex_session_id TEXT,
                    completed TEXT,
                    changed TEXT,
                    verification TEXT,
                    unverified TEXT,
                    need_user TEXT,
                    status TEXT NOT NULL DEFAULT 'active',
                    reply_delivered_count INTEGER NOT NULL DEFAULT 0,
                    last_delivered_message_id TEXT,
                    updated_at TEXT,
                    closed_at TEXT,
                    close_reason TEXT,
                    last_error_summary TEXT
                );

                CREATE TABLE IF NOT EXISTS away_windows (
                    window_id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    recipient_id TEXT NOT NULL,
                    card_message_id TEXT NOT NULL UNIQUE,
                    status TEXT NOT NULL DEFAULT 'waiting',
                    created_at TEXT NOT NULL,
                    deadline_at TEXT NOT NULL,
                    reminder_sent_at TEXT,
                    extend_count INTEGER NOT NULL DEFAULT 0,
                    reply_delivered_count INTEGER NOT NULL DEFAULT 0,
                    last_reply_message_id TEXT,
                    last_command_message_id TEXT,
                    closed_at TEXT,
                    close_reason TEXT,
                    FOREIGN KEY (session_id) REFERENCES away_sessions(session_id)
                );

                CREATE INDEX IF NOT EXISTS idx_away_windows_recipient_status
                    ON away_windows(recipient_id, status);

                CREATE TABLE IF NOT EXISTS processed_messages (
                    message_id TEXT PRIMARY KEY,
                    message_kind TEXT NOT NULL,
                    window_id TEXT,
                    action TEXT NOT NULL,
                    message_text_hash TEXT,
                    processed_at TEXT NOT NULL,
                    FOREIGN KEY (window_id) REFERENCES away_windows(window_id)
                );

                CREATE TABLE IF NOT EXISTS away_cards (
                    card_message_id TEXT PRIMARY KEY,
                    window_id TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    card_kind TEXT NOT NULL,
                    status TEXT NOT NULL,
                    sent_at TEXT NOT NULL,
                    retired_at TEXT,
                    FOREIGN KEY (window_id) REFERENCES away_windows(window_id),
                    FOREIGN KEY (session_id) REFERENCES away_sessions(session_id)
                );

                CREATE TABLE IF NOT EXISTS ordinary_dm_hints (
                    hint_key TEXT PRIMARY KEY,
                    recipient_id TEXT NOT NULL,
                    active_window_count INTEGER NOT NULL,
                    source_message_id TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS runtime_locks (
                    lock_key TEXT PRIMARY KEY,
                    owner TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS diagnostic_events (
                    event_id TEXT PRIMARY KEY,
                    event_kind TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    severity TEXT NOT NULL,
                    message TEXT NOT NULL,
                    detail_json TEXT
                );

                CREATE TABLE IF NOT EXISTS prompt_markers (
                    cwd_hash TEXT PRIMARY KEY,
                    marked_at TEXT NOT NULL,
                    expires_at TEXT
                );

                CREATE TABLE IF NOT EXISTS staged_summaries (
                    cwd_hash TEXT PRIMARY KEY,
                    summary_markdown TEXT NOT NULL,
                    staged_at TEXT NOT NULL,
                    expires_at TEXT
                );

                CREATE TABLE IF NOT EXISTS away_resume_tokens (
                    session_id TEXT PRIMARY KEY,
                    token_hash TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    expires_at TEXT,
                    FOREIGN KEY (session_id) REFERENCES away_sessions(session_id)
                );
                """
            )
            self._migrate_columns(conn)

    def _migrate_columns(self, conn: sqlite3.Connection) -> None:
        session_columns = self._table_columns(conn, "away_sessions")
        for name, ddl in {
            "deadline_at": "TEXT",
            "active_window_id": "TEXT",
            "codex_session_id": "TEXT",
            "reply_delivered_count": "INTEGER NOT NULL DEFAULT 0",
            "last_delivered_message_id": "TEXT",
            "updated_at": "TEXT",
            "closed_at": "TEXT",
            "close_reason": "TEXT",
            "last_error_summary": "TEXT",
        }.items():
            if name not in session_columns:
                conn.execute(f"ALTER TABLE away_sessions ADD COLUMN {name} {ddl}")

        window_columns = self._table_columns(conn, "away_windows")
        for name, ddl in {
            "reply_delivered_count": "INTEGER NOT NULL DEFAULT 0",
            "last_reply_message_id": "TEXT",
            "last_command_message_id": "TEXT",
        }.items():
            if name not in window_columns:
                conn.execute(f"ALTER TABLE away_windows ADD COLUMN {name} {ddl}")

    @staticmethod
    def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
        return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}

    def create_away_session(
        self,
        *,
        project: str,
        cwd: str,
        task: str,
        started_at: str,
        completed: str | None = None,
        changed: str | None = None,
        verification: str | None = None,
        unverified: str | None = None,
        need_user: str | None = None,
        deadline_at: str | None = None,
        codex_session_id: str | None = None,
        session_id: str | None = None,
    ) -> str:
        session_id = session_id or self._new_id("sess")
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                """
                INSERT INTO away_sessions (
                    session_id, project, cwd, task, started_at, deadline_at,
                    codex_session_id, completed, changed, verification,
                    unverified, need_user, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session_id,
                    project,
                    cwd,
                    task,
                    started_at,
                    deadline_at,
                    codex_session_id,
                    completed,
                    changed,
                    verification,
                    unverified,
                    need_user,
                    started_at,
                ),
            )
            conn.execute("COMMIT")
        return session_id

    def new_session_id(self) -> str:
        return self._new_id("sess")

    def create_away_window(
        self,
        *,
        session_id: str,
        recipient_id: str,
        card_message_id: str,
        created_at: str,
        deadline_at: str,
        window_id: str | None = None,
    ) -> str:
        window_id = window_id or self._new_id("win")
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._insert_away_window(
                conn,
                window_id=window_id,
                session_id=session_id,
                recipient_id=recipient_id,
                card_message_id=card_message_id,
                created_at=created_at,
                deadline_at=deadline_at,
            )
            self._set_session_active_window(
                conn,
                session_id=session_id,
                window_id=window_id,
                deadline_at=deadline_at,
                updated_at=created_at,
            )
            conn.execute("COMMIT")
        return window_id

    def create_away_window_guarded(
        self,
        recipient_id: str,
        *,
        session_id: str,
        card_message_id: str,
        created_at: str,
        deadline_at: str,
        owner: str,
        lock_expires_at: str,
        now: str | None = None,
        window_id: str | None = None,
    ) -> str | None:
        lock_key = f"away-window:{recipient_id}"
        now = now or self._utc_now()
        window_id = window_id or self._new_id("win")
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if not self._acquire_lock_in_transaction(
                conn,
                lock_key=lock_key,
                owner=owner,
                expires_at=lock_expires_at,
                now=now,
            ):
                conn.execute("ROLLBACK")
                return None

            active_count = self._active_window_count_in_transaction(conn, recipient_id, now)
            if active_count:
                conn.execute("ROLLBACK")
                return None

            self._insert_away_window(
                conn,
                window_id=window_id,
                session_id=session_id,
                recipient_id=recipient_id,
                card_message_id=card_message_id,
                created_at=created_at,
                deadline_at=deadline_at,
            )
            self._set_session_active_window(
                conn,
                session_id=session_id,
                window_id=window_id,
                deadline_at=deadline_at,
                updated_at=created_at,
            )
            conn.execute("COMMIT")
        return window_id

    def reserve_away_window_guard(
        self,
        recipient_id: str,
        *,
        owner: str,
        lock_expires_at: str,
        now: str | None = None,
    ) -> bool:
        lock_key = f"away-window:{recipient_id}"
        now = now or self._utc_now()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if not self._acquire_lock_in_transaction(
                conn,
                lock_key=lock_key,
                owner=owner,
                expires_at=lock_expires_at,
                now=now,
            ):
                conn.execute("ROLLBACK")
                return False
            if self._active_window_count_in_transaction(conn, recipient_id, now):
                conn.execute("ROLLBACK")
                return False
            conn.execute("COMMIT")
        return True

    def get_window(self, window_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM away_windows WHERE window_id = ?",
                (window_id,),
            ).fetchone()
        return self._row_to_dict(row)

    def get_away_session(self, session_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM away_sessions WHERE session_id = ?",
                (session_id,),
            ).fetchone()
        return self._row_to_dict(row)

    def get_active_session_for_resume(self, session_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM away_sessions
                WHERE session_id = ?
                  AND status IN ('active', 'waiting', 'waiting_paused')
                """,
                (session_id,),
            ).fetchone()
        return self._row_to_dict(row)

    def find_active_away_session(
        self,
        *,
        cwd: str | None = None,
        codex_session_id: str | None = None,
    ) -> dict[str, Any] | None:
        sessions = self.find_active_away_sessions(
            cwd=cwd,
            codex_session_id=codex_session_id,
        )
        return sessions[0] if sessions else None

    def find_active_away_sessions(
        self,
        *,
        cwd: str | None = None,
        codex_session_id: str | None = None,
    ) -> list[dict[str, Any]]:
        active_statuses = ("active", "waiting", "waiting_paused")
        with self._connect() as conn:
            if codex_session_id:
                rows = conn.execute(
                    """
                    SELECT * FROM away_sessions
                    WHERE codex_session_id = ?
                      AND status IN (?, ?, ?)
                    ORDER BY COALESCE(updated_at, started_at) DESC
                    """,
                    (codex_session_id, *active_statuses),
                ).fetchall()
                if rows:
                    return [dict(row) for row in rows]
            if not cwd:
                return []
            rows = conn.execute(
                """
                SELECT * FROM away_sessions
                WHERE status IN (?, ?, ?)
                ORDER BY COALESCE(updated_at, started_at) DESC
                """,
                active_statuses,
            ).fetchall()
        target = Path(cwd).expanduser().resolve(strict=False)
        matched: list[dict[str, Any]] = []
        for row in rows:
            if Path(row["cwd"]).expanduser().resolve(strict=False) == target:
                matched.append(dict(row))
        return matched

    def list_away_sessions(
        self,
        *,
        session_id: str | None = None,
        cwd: str | None = None,
        active_only: bool = True,
        include_closed: bool = False,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        statuses = ("active", "waiting", "waiting_paused")
        params: list[Any] = []
        clauses: list[str] = []
        if session_id:
            clauses.append("session_id = ?")
            params.append(session_id)
        if active_only:
            clauses.append("status IN (?, ?, ?)")
            params.extend(statuses)
        elif not include_closed:
            clauses.append("(closed_at IS NULL OR status IN (?, ?, ?))")
            params.extend(statuses)
        where = "WHERE " + " AND ".join(clauses) if clauses else ""
        params.append(limit)
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT * FROM away_sessions
                {where}
                ORDER BY COALESCE(updated_at, started_at) DESC
                LIMIT ?
                """,
                params,
            ).fetchall()
        result = [dict(row) for row in rows]
        if cwd:
            target = Path(cwd).expanduser().resolve(strict=False)
            result = [
                row
                for row in result
                if Path(row["cwd"]).expanduser().resolve(strict=False) == target
            ]
        return result

    def find_window_by_card_message_id(
        self, card_message_id: str
    ) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM away_windows WHERE card_message_id = ?",
                (card_message_id,),
            ).fetchone()
        return self._row_to_dict(row)

    def active_window_count(self, recipient_id: str) -> int:
        with self._connect() as conn:
            return int(
                conn.execute(
                    """
                    SELECT COUNT(*) FROM away_windows
                    WHERE recipient_id = ? AND status IN ('waiting', 'waiting_paused')
                    """,
                    (recipient_id,),
                ).fetchone()[0]
            )

    def record_card(
        self,
        *,
        card_message_id: str,
        window_id: str,
        session_id: str,
        card_kind: str,
        status: str,
        sent_at: str,
        retired_at: str | None = None,
    ) -> bool:
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute(
                    """
                    INSERT INTO away_cards (
                        card_message_id, window_id, session_id, card_kind,
                        status, sent_at, retired_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        card_message_id,
                        window_id,
                        session_id,
                        card_kind,
                        status,
                        sent_at,
                        retired_at,
                    ),
                )
            except sqlite3.IntegrityError:
                conn.execute("ROLLBACK")
                return False
            conn.execute("COMMIT")
        return True

    def find_card(self, card_message_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM away_cards WHERE card_message_id = ?",
                (card_message_id,),
            ).fetchone()
        return self._row_to_dict(row)

    def retire_active_card(self, window_id: str, retired_at: str) -> bool:
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            cursor = conn.execute(
                """
                UPDATE away_cards
                SET status = 'retired', retired_at = ?
                WHERE window_id = ? AND status = 'active'
                """,
                (retired_at, window_id),
            )
            conn.execute("COMMIT")
        return cursor.rowcount > 0

    def close_active_card(self, window_id: str, closed_at: str) -> bool:
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            cursor = conn.execute(
                """
                UPDATE away_cards
                SET status = 'closed', retired_at = COALESCE(retired_at, ?)
                WHERE window_id = ? AND status = 'active'
                """,
                (closed_at, window_id),
            )
            conn.execute("COMMIT")
        return cursor.rowcount > 0

    def rotate_window_card(
        self,
        *,
        window_id: str,
        new_card_message_id: str,
        card_kind: str,
        sent_at: str,
    ) -> bool:
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            window = conn.execute(
                "SELECT session_id FROM away_windows WHERE window_id = ?",
                (window_id,),
            ).fetchone()
            if window is None:
                conn.execute("ROLLBACK")
                return False
            conn.execute(
                """
                UPDATE away_cards
                SET status = 'retired', retired_at = ?
                WHERE window_id = ? AND status = 'active'
                """,
                (sent_at, window_id),
            )
            conn.execute(
                """
                UPDATE away_windows
                SET card_message_id = ?, status = 'waiting'
                WHERE window_id = ?
                """,
                (new_card_message_id, window_id),
            )
            conn.execute(
                """
                UPDATE away_sessions
                SET status = 'active', active_window_id = ?, updated_at = ?
                WHERE session_id = ?
                """,
                (window_id, sent_at, window["session_id"]),
            )
            conn.execute(
                """
                INSERT INTO away_cards (
                    card_message_id, window_id, session_id, card_kind,
                    status, sent_at
                )
                VALUES (?, ?, ?, ?, 'active', ?)
                """,
                (
                    new_card_message_id,
                    window_id,
                    window["session_id"],
                    card_kind,
                    sent_at,
                ),
            )
            conn.execute("COMMIT")
        return True

    def mark_prompt_delivered(
        self,
        *,
        session_id: str,
        window_id: str,
        message_id: str,
        processed_at: str,
    ) -> bool:
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            session_cursor = conn.execute(
                """
                UPDATE away_sessions
                SET status = 'waiting_paused',
                    reply_delivered_count = reply_delivered_count + 1,
                    last_delivered_message_id = ?,
                    updated_at = ?
                WHERE session_id = ?
                """,
                (message_id, processed_at, session_id),
            )
            window_cursor = conn.execute(
                """
                UPDATE away_windows
                SET status = 'waiting_paused',
                    reply_delivered_count = reply_delivered_count + 1,
                    last_reply_message_id = ?
                WHERE window_id = ?
                """,
                (message_id, window_id),
            )
            ok = session_cursor.rowcount == 1 and window_cursor.rowcount == 1
            conn.execute("COMMIT" if ok else "ROLLBACK")
        return ok

    def extend_window(self, window_id: str, new_deadline_at: str) -> bool:
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            window = conn.execute(
                """
                SELECT recipient_id, session_id
                FROM away_windows
                WHERE window_id = ?
                """,
                (window_id,),
            ).fetchone()
            cursor = conn.execute(
                """
                UPDATE away_windows
                SET deadline_at = ?, extend_count = extend_count + 1
                WHERE window_id = ?
                """,
                (new_deadline_at, window_id),
            )
            if cursor.rowcount == 1 and window is not None:
                now = self._utc_now()
                conn.execute(
                    """
                    UPDATE away_sessions
                    SET deadline_at = ?, updated_at = ?
                    WHERE session_id = ?
                    """,
                    (new_deadline_at, now, window["session_id"]),
                )
                conn.execute(
                    """
                    UPDATE runtime_locks
                    SET expires_at = ?, updated_at = ?
                    WHERE lock_key = ? AND owner = ?
                    """,
                    (
                        new_deadline_at,
                        now,
                        f"away-window:{window['recipient_id']}",
                        window["session_id"],
                    ),
                )
            conn.execute("COMMIT")
        return cursor.rowcount == 1

    def mark_reminder_sent(self, window_id: str, sent_at: str) -> bool:
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            cursor = conn.execute(
                """
                UPDATE away_windows
                SET reminder_sent_at = ?
                WHERE window_id = ?
                """,
                (sent_at, window_id),
            )
            conn.execute("COMMIT")
        return cursor.rowcount == 1

    def close_window(
        self, window_id: str, status: str, reason: str, closed_at: str
    ) -> bool:
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            window = conn.execute(
                """
                SELECT recipient_id, session_id
                FROM away_windows
                WHERE window_id = ?
                """,
                (window_id,),
            ).fetchone()
            cursor = conn.execute(
                """
                UPDATE away_windows
                SET status = ?, close_reason = ?, closed_at = ?
                WHERE window_id = ?
                """,
                (status, reason, closed_at, window_id),
            )
            if cursor.rowcount == 1 and window is not None:
                conn.execute(
                    """
                    DELETE FROM runtime_locks
                    WHERE lock_key = ? AND owner = ?
                    """,
                    (
                        f"away-window:{window['recipient_id']}",
                        window["session_id"],
                    ),
                )
            conn.execute("COMMIT")
        return cursor.rowcount == 1

    def close_away_session(
        self, session_id: str, status: str, reason: str, closed_at: str
    ) -> bool:
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            cursor = conn.execute(
                """
                UPDATE away_sessions
                SET status = ?, close_reason = ?, closed_at = ?, updated_at = ?
                WHERE session_id = ?
                """,
                (status, reason, closed_at, closed_at, session_id),
            )
            if cursor.rowcount == 1:
                conn.execute(
                    "DELETE FROM runtime_locks WHERE lock_key = ?",
                    (f"away-waiter:{session_id}",),
                )
            conn.execute("COMMIT")
        return cursor.rowcount == 1

    def close_orphan_away_sessions(
        self,
        *,
        closed_at: str,
        reason: str = "orphan_without_window_repaired",
    ) -> list[dict[str, Any]]:
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute(
                """
                SELECT *
                FROM away_sessions
                WHERE status IN ('active', 'waiting', 'waiting_paused')
                  AND active_window_id IS NULL
                  AND (deadline_at IS NULL OR deadline_at = '')
                ORDER BY COALESCE(updated_at, started_at) ASC
                """
            ).fetchall()
            sessions = [dict(row) for row in rows]
            for session in sessions:
                conn.execute(
                    """
                    UPDATE away_sessions
                    SET status = 'closed',
                        close_reason = ?,
                        closed_at = ?,
                        updated_at = ?
                    WHERE session_id = ?
                    """,
                    (reason, closed_at, closed_at, session["session_id"]),
                )
                conn.execute(
                    "DELETE FROM runtime_locks WHERE lock_key = ?",
                    (f"away-waiter:{session['session_id']}",),
                )
            conn.execute("COMMIT")
        return sessions

    def cleanup_stale_away_sessions(self, *, now: str, dry_run: bool = False) -> dict[str, Any]:
        now_dt = _parse_datetime(now)
        now_iso = now_dt.isoformat()
        reason = "manual_cleanup_timeout"
        closed: list[dict[str, Any]] = []
        skipped_waiter_alive: list[str] = []

        with self._connect() as conn:
            if not dry_run:
                conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute(
                """
                SELECT *
                FROM away_sessions
                WHERE status IN ('active', 'waiting', 'waiting_paused')
                  AND deadline_at IS NOT NULL
                  AND deadline_at != ''
                ORDER BY deadline_at ASC, started_at ASC
                """
            ).fetchall()
            for row in rows:
                session = dict(row)
                try:
                    deadline = _parse_datetime(str(session["deadline_at"]))
                except (TypeError, ValueError):
                    continue
                if deadline > now_dt:
                    continue

                session_id = str(session["session_id"])
                lease = conn.execute(
                    "SELECT * FROM runtime_locks WHERE lock_key = ?",
                    (f"away-waiter:{session_id}",),
                ).fetchone()
                if lease is not None:
                    try:
                        lease_expires_at = _parse_datetime(str(lease["expires_at"]))
                    except (TypeError, ValueError):
                        lease_expires_at = None
                    if lease_expires_at is not None and lease_expires_at > now_dt:
                        skipped_waiter_alive.append(session_id)
                        continue

                closed.append(
                    {
                        "session_id": session_id,
                        "cwd": session["cwd"],
                        "deadline_at": session["deadline_at"],
                        "close_reason": reason,
                    }
                )
                if dry_run:
                    continue

                window_rows = conn.execute(
                    """
                    SELECT window_id, recipient_id
                    FROM away_windows
                    WHERE session_id = ?
                      AND status IN ('active', 'waiting', 'waiting_paused')
                    """,
                    (session_id,),
                ).fetchall()
                conn.execute(
                    """
                    UPDATE away_sessions
                    SET status = 'timed_out',
                        close_reason = ?,
                        closed_at = ?,
                        updated_at = ?
                    WHERE session_id = ?
                    """,
                    (reason, now_iso, now_iso, session_id),
                )
                conn.execute(
                    """
                    UPDATE away_windows
                    SET status = 'timed_out',
                        close_reason = ?,
                        closed_at = ?
                    WHERE session_id = ?
                      AND status IN ('active', 'waiting', 'waiting_paused')
                    """,
                    (reason, now_iso, session_id),
                )
                conn.execute(
                    """
                    UPDATE away_cards
                    SET status = 'closed',
                        retired_at = COALESCE(retired_at, ?)
                    WHERE session_id = ?
                      AND status = 'active'
                    """,
                    (now_iso, session_id),
                )
                conn.execute(
                    "DELETE FROM runtime_locks WHERE lock_key = ?",
                    (f"away-waiter:{session_id}",),
                )
                for window in window_rows:
                    conn.execute(
                        "DELETE FROM runtime_locks WHERE lock_key = ? AND owner = ?",
                        (f"away-window:{window['recipient_id']}", session_id),
                    )
            if not dry_run:
                conn.execute("COMMIT")

        return {
            "closed_count": len(closed),
            "skipped_waiter_alive_count": len(skipped_waiter_alive),
            "closed": closed,
        }

    def get_runtime_lock(self, lock_key: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM runtime_locks WHERE lock_key = ?",
                (lock_key,),
            ).fetchone()
        return self._row_to_dict(row)

    def renew_waiter_lease(
        self,
        session_id: str,
        *,
        owner: str,
        now: str,
        expires_at: str,
    ) -> bool:
        return self.acquire_lock(
            lock_key=f"away-waiter:{session_id}",
            owner=owner,
            expires_at=expires_at,
            now=now,
        )

    def get_waiter_lease(self, session_id: str) -> dict[str, Any] | None:
        return self.get_runtime_lock(f"away-waiter:{session_id}")

    def release_waiter_lease(self, session_id: str, owner: str | None = None) -> bool:
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if owner is None:
                cursor = conn.execute(
                    "DELETE FROM runtime_locks WHERE lock_key = ?",
                    (f"away-waiter:{session_id}",),
                )
            else:
                cursor = conn.execute(
                    "DELETE FROM runtime_locks WHERE lock_key = ? AND owner = ?",
                    (f"away-waiter:{session_id}", owner),
                )
            conn.execute("COMMIT")
        return cursor.rowcount > 0

    def mark_processed(
        self,
        message_id: str,
        message_kind: str,
        window_id: str | None,
        action: str,
        message_text_hash: str | None = None,
    ) -> bool:
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute(
                    """
                    INSERT INTO processed_messages (
                        message_id, message_kind, window_id, action,
                        message_text_hash, processed_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        message_id,
                        message_kind,
                        window_id,
                        action,
                        message_text_hash,
                        self._utc_now(),
                    ),
                )
            except sqlite3.IntegrityError:
                conn.execute("ROLLBACK")
                return False
            conn.execute("COMMIT")
        return True

    def record_ordinary_dm_hint(
        self,
        hint_key: str,
        recipient_id: str,
        active_window_count: int,
        source_message_id: str,
    ) -> bool:
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute(
                    """
                    INSERT INTO ordinary_dm_hints (
                        hint_key, recipient_id, active_window_count,
                        source_message_id, created_at
                    )
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        hint_key,
                        recipient_id,
                        active_window_count,
                        source_message_id,
                        self._utc_now(),
                    ),
                )
            except sqlite3.IntegrityError:
                conn.execute("ROLLBACK")
                return False
            conn.execute("COMMIT")
        return True

    def record_ordinary_dm_event(
        self,
        *,
        message_id: str,
        window_id: str | None,
        recipient_id: str,
        active_window_count: int,
        message_text_hash: str | None,
        now: str,
    ) -> str:
        hint_key = f"{recipient_id}:{message_id}"
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                "SELECT 1 FROM processed_messages WHERE message_id = ?",
                (message_id,),
            ).fetchone()
            if existing is not None:
                conn.execute("ROLLBACK")
                return "already_processed"

            try:
                conn.execute(
                    """
                    INSERT INTO processed_messages (
                        message_id, message_kind, window_id, action,
                        message_text_hash, processed_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        message_id,
                        "ordinary_dm",
                        window_id,
                        "ordinary_dm_hint",
                        message_text_hash,
                        now,
                    ),
                )
                conn.execute(
                    """
                    INSERT INTO ordinary_dm_hints (
                        hint_key, recipient_id, active_window_count,
                        source_message_id, created_at
                    )
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        hint_key,
                        recipient_id,
                        active_window_count,
                        message_id,
                        now,
                    ),
                )
            except sqlite3.IntegrityError:
                conn.execute("ROLLBACK")
                return "already_processed"
            conn.execute("COMMIT")
        return "send_hint"

    def acquire_lock(
        self,
        lock_key: str,
        owner: str,
        expires_at: str,
        now: str | None = None,
    ) -> bool:
        now = now or self._utc_now()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            acquired = self._acquire_lock_in_transaction(
                conn,
                lock_key=lock_key,
                owner=owner,
                expires_at=expires_at,
                now=now,
            )
            conn.execute("COMMIT" if acquired else "ROLLBACK")
        return acquired

    def set_install_state(self, key: str, value: Any) -> None:
        encoded = json.dumps(value, ensure_ascii=False)
        now = self._utc_now()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                """
                INSERT INTO install_state (key, value, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    value = excluded.value,
                    updated_at = excluded.updated_at
                """,
                (key, encoded, now),
            )
            conn.execute("COMMIT")

    def get_install_state(self, key: str, default: Any = None) -> Any:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT value FROM install_state WHERE key = ?",
                (key,),
            ).fetchone()
        if row is None:
            return default
        try:
            return json.loads(row["value"])
        except json.JSONDecodeError:
            return row["value"]

    def set_resume_token_hash(
        self,
        *,
        session_id: str,
        token_hash: str,
        created_at: str,
        expires_at: str | None = None,
    ) -> None:
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                """
                INSERT INTO away_resume_tokens (
                    session_id, token_hash, created_at, expires_at
                )
                VALUES (?, ?, ?, ?)
                ON CONFLICT(session_id) DO UPDATE SET
                    token_hash = excluded.token_hash,
                    created_at = excluded.created_at,
                    expires_at = excluded.expires_at
                """,
                (session_id, token_hash, created_at, expires_at),
            )
            conn.execute("COMMIT")

    def get_resume_token_hash(self, session_id: str) -> str | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT token_hash FROM away_resume_tokens WHERE session_id = ?",
                (session_id,),
            ).fetchone()
        if row is None:
            return None
        return str(row["token_hash"])

    def clear_resume_token(self, session_id: str) -> None:
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "DELETE FROM away_resume_tokens WHERE session_id = ?",
                (session_id,),
            )
            conn.execute("COMMIT")

    def update_install_status(
        self,
        *,
        status: str,
        failed_code: str | None = None,
        waiting_for: str | None = None,
        next_step: str = "",
    ) -> dict[str, Any]:
        payload = {
            "status": status,
            "failed_code": failed_code,
            "waiting_for": waiting_for,
            "next_step": next_step,
            "updated_at": self._utc_now(),
        }
        self.set_install_state("status", payload)
        return payload

    def set_route_key_state(
        self,
        *,
        status: str,
        source: str,
        verified_at: str | None = None,
        last_failure_reason: str | None = None,
    ) -> dict[str, Any]:
        payload = {
            "status": status,
            "source": source,
            "verified_at": verified_at,
            "last_failure_reason": last_failure_reason,
            "updated_at": self._utc_now(),
        }
        self.set_install_state("route_key", payload)
        return payload

    def route_key_state(self) -> dict[str, Any]:
        return self.get_install_state(
            "route_key",
            {
                "status": "unknown",
                "source": None,
                "verified_at": None,
                "last_failure_reason": None,
                "updated_at": None,
            },
        )

    def record_diagnostic_event(
        self,
        *,
        event_kind: str,
        severity: str,
        message: str,
        detail: Any | None = None,
        created_at: str | None = None,
        per_kind_limit: int = 200,
    ) -> str:
        event_id = self._new_id("diag")
        created_at = created_at or self._utc_now()
        encoded = json.dumps(detail or {}, ensure_ascii=False)
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                """
                INSERT INTO diagnostic_events (
                    event_id, event_kind, created_at, severity, message, detail_json
                )
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (event_id, event_kind, created_at, severity, message, encoded),
            )
            if per_kind_limit > 0:
                conn.execute(
                    """
                    DELETE FROM diagnostic_events
                    WHERE event_kind = ?
                      AND event_id NOT IN (
                        SELECT event_id
                        FROM diagnostic_events
                        WHERE event_kind = ?
                        ORDER BY created_at DESC, event_id DESC
                        LIMIT ?
                      )
                    """,
                    (event_kind, event_kind, per_kind_limit),
                )
            conn.execute("COMMIT")
        return event_id

    def list_diagnostic_events(
        self,
        event_kind: str | None = None,
        *,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        with self._connect() as conn:
            if event_kind is None:
                rows = conn.execute(
                    """
                    SELECT * FROM diagnostic_events
                    ORDER BY created_at ASC, event_id ASC
                    LIMIT ?
                    """,
                    (limit,),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT * FROM diagnostic_events
                    WHERE event_kind = ?
                    ORDER BY created_at ASC, event_id ASC
                    LIMIT ?
                    """,
                    (event_kind, limit),
                ).fetchall()
        return [dict(row) for row in rows]

    def install_status(self) -> dict[str, Any]:
        return self.get_install_state(
            "status",
            {
                "status": "not_started",
                "failed_code": None,
                "waiting_for": None,
                "next_step": "Run codex-away-mode install --dry-run --json.",
                "updated_at": None,
            },
        )

    def reserve_approval_notification(
        self,
        *,
        dedupe_key: str,
        session_id: str | None,
        turn_id: str | None,
        cwd: str | None,
        tool_name: str,
        command_hash: str,
        seen_at: str,
        throttle_seconds: int,
    ) -> dict[str, Any]:
        cwd_hash = self.cwd_hash(cwd or "")
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM approval_notifications WHERE dedupe_key = ?",
                (dedupe_key,),
            ).fetchone()
            existing = self._row_to_dict(row)
            if existing and _seconds_between(existing.get("last_seen_at"), seen_at) <= throttle_seconds:
                suppressed_count = int(existing.get("suppressed_count") or 0) + 1
                conn.execute(
                    """
                    UPDATE approval_notifications
                    SET last_seen_at = ?, suppressed_count = ?, status = ?
                    WHERE dedupe_key = ?
                    """,
                    (seen_at, suppressed_count, "suppressed", dedupe_key),
                )
                conn.execute("COMMIT")
                existing["last_seen_at"] = seen_at
                existing["suppressed_count"] = suppressed_count
                existing["status"] = "suppressed"
                return existing

            conn.execute(
                """
                INSERT INTO approval_notifications (
                    dedupe_key, session_id, turn_id, cwd_hash, tool_name,
                    command_hash, first_seen_at, last_seen_at, sent_at,
                    suppressed_count, status
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, 0, ?)
                ON CONFLICT(dedupe_key) DO UPDATE SET
                    session_id = excluded.session_id,
                    turn_id = excluded.turn_id,
                    cwd_hash = excluded.cwd_hash,
                    tool_name = excluded.tool_name,
                    command_hash = excluded.command_hash,
                    first_seen_at = excluded.first_seen_at,
                    last_seen_at = excluded.last_seen_at,
                    sent_at = NULL,
                    suppressed_count = 0,
                    status = excluded.status
                """,
                (
                    dedupe_key,
                    session_id,
                    turn_id,
                    cwd_hash,
                    tool_name,
                    command_hash,
                    seen_at,
                    seen_at,
                    "reserved",
                ),
            )
            conn.execute("COMMIT")
        return {
            "dedupe_key": dedupe_key,
            "session_id": session_id,
            "turn_id": turn_id,
            "cwd_hash": cwd_hash,
            "tool_name": tool_name,
            "command_hash": command_hash,
            "first_seen_at": seen_at,
            "last_seen_at": seen_at,
            "sent_at": None,
            "suppressed_count": 0,
            "status": "reserved",
        }

    def mark_approval_notification_result(
        self,
        dedupe_key: str,
        *,
        status: str,
        sent_at: str | None = None,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE approval_notifications
                SET status = ?, sent_at = COALESCE(?, sent_at)
                WHERE dedupe_key = ?
                """,
                (status, sent_at, dedupe_key),
            )

    def get_approval_notification(self, dedupe_key: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM approval_notifications WHERE dedupe_key = ?",
                (dedupe_key,),
            ).fetchone()
        return self._row_to_dict(row)

    def mark_prompt_marker(
        self,
        *,
        cwd: str,
        marked_at: str,
        expires_at: str | None = None,
    ) -> str:
        cwd_hash = self.cwd_hash(cwd)
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO prompt_markers (cwd_hash, marked_at, expires_at)
                VALUES (?, ?, ?)
                ON CONFLICT(cwd_hash) DO UPDATE SET
                    marked_at = excluded.marked_at,
                    expires_at = excluded.expires_at
                """,
                (cwd_hash, marked_at, expires_at),
            )
        return cwd_hash

    def get_prompt_marker(self, cwd: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT cwd_hash, marked_at, expires_at
                FROM prompt_markers
                WHERE cwd_hash = ?
                """,
                (self.cwd_hash(cwd),),
            ).fetchone()
        return self._row_to_dict(row)

    def delete_prompt_marker(self, cwd: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "DELETE FROM prompt_markers WHERE cwd_hash = ?",
                (self.cwd_hash(cwd),),
            )

    def stage_summary(
        self,
        *,
        cwd: str,
        summary_markdown: str,
        staged_at: str,
        expires_at: str | None = None,
    ) -> str:
        cwd_hash = self.cwd_hash(cwd)
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO staged_summaries (
                    cwd_hash, summary_markdown, staged_at, expires_at
                )
                VALUES (?, ?, ?, ?)
                ON CONFLICT(cwd_hash) DO UPDATE SET
                    summary_markdown = excluded.summary_markdown,
                    staged_at = excluded.staged_at,
                    expires_at = excluded.expires_at
                """,
                (cwd_hash, summary_markdown, staged_at, expires_at),
            )
        return cwd_hash

    def get_staged_summary(self, cwd: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT cwd_hash, summary_markdown, staged_at, expires_at
                FROM staged_summaries
                WHERE cwd_hash = ?
                """,
                (self.cwd_hash(cwd),),
            ).fetchone()
        return self._row_to_dict(row)

    def delete_staged_summary(self, cwd: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "DELETE FROM staged_summaries WHERE cwd_hash = ?",
                (self.cwd_hash(cwd),),
            )

    def _insert_away_window(
        self,
        conn: sqlite3.Connection,
        *,
        window_id: str,
        session_id: str,
        recipient_id: str,
        card_message_id: str,
        created_at: str,
        deadline_at: str,
    ) -> None:
        conn.execute(
            """
            INSERT INTO away_windows (
                window_id, session_id, recipient_id, card_message_id,
                created_at, deadline_at
            )
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                window_id,
                session_id,
                recipient_id,
                card_message_id,
                created_at,
                deadline_at,
            ),
        )
        self._set_session_active_window(
            conn,
            session_id=session_id,
            window_id=window_id,
            deadline_at=deadline_at,
            updated_at=created_at,
        )

    def _set_session_active_window(
        self,
        conn: sqlite3.Connection,
        *,
        session_id: str,
        window_id: str,
        deadline_at: str,
        updated_at: str,
    ) -> None:
        conn.execute(
            """
            UPDATE away_sessions
            SET active_window_id = ?, deadline_at = ?, updated_at = ?
            WHERE session_id = ?
            """,
            (window_id, deadline_at, updated_at, session_id),
        )

    @staticmethod
    def _active_window_count_in_transaction(
        conn: sqlite3.Connection,
        recipient_id: str,
        now: str,
    ) -> int:
        return int(
            conn.execute(
                """
                SELECT COUNT(*) FROM away_windows
                WHERE recipient_id = ?
                  AND status IN ('waiting', 'waiting_paused')
                  AND julianday(deadline_at) > julianday(?)
                """,
                (recipient_id, now),
            ).fetchone()[0]
        )

    def _acquire_lock_in_transaction(
        self,
        conn: sqlite3.Connection,
        *,
        lock_key: str,
        owner: str,
        expires_at: str,
        now: str,
    ) -> bool:
        row = conn.execute(
            "SELECT owner, expires_at FROM runtime_locks WHERE lock_key = ?",
            (lock_key,),
        ).fetchone()
        if row is not None and row["expires_at"] > now and row["owner"] != owner:
            return False

        conn.execute(
            """
            INSERT INTO runtime_locks (lock_key, owner, expires_at, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(lock_key) DO UPDATE SET
                owner = excluded.owner,
                expires_at = excluded.expires_at,
                updated_at = excluded.updated_at
            """,
            (lock_key, owner, expires_at, now),
        )
        return True

    @staticmethod
    def _new_id(prefix: str) -> str:
        return f"{prefix}_{uuid.uuid4().hex}"

    @staticmethod
    def cwd_hash(cwd: str) -> str:
        normalized = str(Path(cwd).expanduser().resolve(strict=False))
        return hashlib.sha256(normalized.encode("utf-8")).hexdigest()

    @staticmethod
    def _row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        return dict(row)

    @staticmethod
    def _utc_now() -> str:
        return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _parse_datetime(value: str) -> datetime:
    normalized = value.replace("Z", "+00:00")
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _seconds_between(earlier: str | None, later: str | None) -> float:
    if not earlier or not later:
        return float("inf")
    try:
        return abs((_parse_datetime(later) - _parse_datetime(earlier)).total_seconds())
    except (TypeError, ValueError):
        return float("inf")
