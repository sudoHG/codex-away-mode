from __future__ import annotations

import json
import os
import sqlite3
import tempfile
from dataclasses import dataclass, fields
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


class RuntimeStateError(RuntimeError):
    def __init__(self, detail: str, *, error_code: str = "runtime_state_unwritable") -> None:
        super().__init__(detail)
        self.error_code = error_code
        self.detail = detail


@dataclass(frozen=True)
class RuntimePaths:
    codex_home: Path
    away_home: Path
    data_dir: Path
    bin_dir: Path
    wrapper_path: Path
    scripts_dir: Path
    skill_source_dir: Path
    skill_install_dir: Path
    config_path: Path
    install_state_path: Path
    log_dir: Path
    backup_dir: Path
    runtime_dir: Path
    runtime_state_path: Path
    runtime_prompt_marker_dir: Path
    runtime_summary_dir: Path
    hooks_json: Path
    codex_config_path: Path
    global_agents: Path

    @classmethod
    def from_environment(cls):
        codex_home = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
        away_home = _away_home_from_environment()
        data_dir = away_home
        bin_dir = data_dir / "bin"
        runtime_dir = _runtime_dir_from_environment()
        return cls(
            codex_home=codex_home,
            away_home=away_home,
            data_dir=data_dir,
            bin_dir=bin_dir,
            wrapper_path=bin_dir / "codex-away-mode",
            scripts_dir=data_dir / "scripts",
            skill_source_dir=data_dir / "skill",
            skill_install_dir=codex_home / "skills" / "codex-away-mode",
            config_path=data_dir / "config.toml",
            install_state_path=data_dir / "install-state.sqlite",
            log_dir=data_dir / "logs",
            backup_dir=data_dir / "backups",
            runtime_dir=runtime_dir,
            runtime_state_path=runtime_dir / "state.sqlite",
            runtime_prompt_marker_dir=runtime_dir / "user-turns",
            runtime_summary_dir=runtime_dir / "summaries",
            hooks_json=codex_home / "hooks.json",
            codex_config_path=codex_home / "config.toml",
            global_agents=codex_home / "AGENTS.md",
        )


@dataclass
class AppConfig:
    notification_mode: str = "all"
    snooze_until: Optional[str] = None
    feishu_user_id: Optional[str] = None
    feishu_chat_id: Optional[str] = None
    feishu_bot_name: Optional[str] = None
    lark_cli_path: str = "lark-cli"
    persist_reply_text: bool = False
    lightweight_logs: bool = True
    default_wait_minutes: int = 30
    pre_timeout_reminder_minutes: int = 5
    extend_minutes: int = 30
    poll_interval_seconds: int = 5
    multi_window_enabled: bool = True
    route_key_verified: bool = False
    capture_hook_payloads: bool = False


def load_config(path):
    path = Path(path)
    if not path.exists():
        return AppConfig()
    values = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = _parse_toml_value(value.strip())
    allowed = {field.name for field in fields(AppConfig)}
    return AppConfig(**{key: value for key, value in values.items() if key in allowed})


def save_config(path, config):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    content = "".join(
        f"{field.name} = {_format_toml_value(getattr(config, field.name))}\n"
        for field in fields(AppConfig)
    )
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    fd = os.open(str(path), flags, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
    finally:
        os.chmod(path, 0o600)


def sqlite_state_path_is_writable(path: str | Path) -> bool:
    path = Path(path)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(path, timeout=1, isolation_level=None)
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("CREATE TABLE __codex_away_write_probe (id TEXT)")
            conn.execute("ROLLBACK")
            return True
        except sqlite3.Error:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            return False
        finally:
            conn.close()
    except OSError:
        return False
    except sqlite3.Error:
        return False


def prepare_runtime_dir(runtime_dir: str | Path) -> Path:
    runtime_dir = Path(runtime_dir).expanduser()
    if not runtime_dir.is_absolute():
        raise RuntimeStateError("runtime_dir_not_absolute")
    if runtime_dir.exists() and not runtime_dir.is_dir():
        raise RuntimeStateError("runtime_dir_not_directory")
    if not runtime_dir.exists():
        runtime_dir.mkdir(parents=True, mode=0o700)
    _validate_runtime_dir(runtime_dir)
    try:
        os.chmod(runtime_dir, 0o700)
    except OSError:
        pass
    return runtime_dir


def ensure_runtime_state_writable(paths: RuntimePaths) -> None:
    prepare_runtime_dir(paths.runtime_dir)
    if not sqlite_state_path_is_writable(paths.runtime_state_path):
        raise RuntimeStateError("runtime_sqlite_unwritable")


def _runtime_dir_from_environment() -> Path:
    raw = os.environ.get("CODEX_AWAY_RUNTIME_DIR")
    if raw:
        runtime_dir = Path(raw).expanduser()
        if not runtime_dir.is_absolute():
            raise RuntimeStateError("runtime_dir_not_absolute")
        return runtime_dir
    base = Path(os.environ.get("TMPDIR") or tempfile.gettempdir()).expanduser()
    if not base.is_absolute():
        base = base.resolve()
    return base / "codex-away-mode"


def _away_home_from_environment() -> Path:
    raw = os.environ.get("CODEX_AWAY_HOME")
    if raw:
        away_home = Path(raw).expanduser()
        if not away_home.is_absolute():
            raise RuntimeStateError("away_home_not_absolute", error_code="away_home_unwritable")
        return away_home
    return Path.home() / ".codex-away-mode"


def _validate_runtime_dir(runtime_dir: Path) -> None:
    try:
        stat_result = runtime_dir.stat()
    except OSError:
        raise RuntimeStateError("runtime_dir_not_writable")
    if hasattr(os, "getuid") and stat_result.st_uid != os.getuid():
        raise RuntimeStateError("runtime_dir_not_owned_by_user")
    if stat_result.st_mode & 0o077:
        raise RuntimeStateError("runtime_dir_permissions_too_open")
    if not os.access(runtime_dir, os.W_OK):
        raise RuntimeStateError("runtime_dir_not_writable")


def effective_notification_mode(config, now=None):
    if config.snooze_until and _parse_datetime(config.snooze_until) > _now_utc(now):
        return "off"
    if config.snooze_until:
        return "all"
    return config.notification_mode


def _format_toml_value(value):
    if value is None:
        return '""'
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    return json.dumps(value, ensure_ascii=False)


def _parse_toml_value(value):
    if value in ("true", "false"):
        return value == "true"
    if value == '""':
        return None
    try:
        return int(value)
    except ValueError:
        return json.loads(value)


def _now_utc(now):
    if now is None:
        return datetime.now(timezone.utc)
    if now.tzinfo is None:
        return now.replace(tzinfo=timezone.utc)
    return now.astimezone(timezone.utc)


def _parse_datetime(value):
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)
