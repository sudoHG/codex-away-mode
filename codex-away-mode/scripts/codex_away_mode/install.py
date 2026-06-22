from __future__ import annotations

import os
import shutil
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import hooks, notify
from .config import AppConfig, RuntimeStateError, ensure_runtime_state_writable, load_config, save_config
from .state import open_install_store


class InstallSyncError(RuntimeError):
    """Raised when install cannot safely sync package files."""


def run_preflight(paths) -> dict[str, Any]:
    away_home = Path(getattr(paths, "away_home", paths.data_dir))
    agents_path = Path(paths.global_agents)
    hooks_path = Path(paths.hooks_json)
    skills_dir = Path(getattr(paths, "skill_install_dir", Path(paths.codex_home) / "skills" / "codex-away-mode")).parent
    legacy_data_dir = Path(paths.codex_home) / "codex-away-mode"

    away_home_writable = _directory_writable(away_home)
    agents_writable = _path_parent_writable(agents_path)
    hooks_writable = _path_parent_writable(hooks_path)
    skills_writable = _directory_writable(skills_dir)
    runtime_writable = True
    runtime_detail = None
    try:
        ensure_runtime_state_writable(paths)
    except RuntimeStateError as exc:
        runtime_writable = False
        runtime_detail = exc.detail

    failed_code = None
    next_step = "Run codex-away-mode install --dry-run --json."
    if not away_home_writable:
        failed_code = "away_home_unwritable"
        next_step = "Ask the user to approve writing ~/.codex-away-mode, or set CODEX_AWAY_HOME to a writable absolute path."
    elif not (agents_writable and hooks_writable):
        failed_code = "codex_access_unwritable"
        next_step = "Ask the user to approve the minimal Codex access writes to AGENTS.md and hooks.json, then rerun install."
    elif not runtime_writable:
        failed_code = "hook_runtime_unwritable"
        next_step = "Choose a secure writable CODEX_AWAY_RUNTIME_DIR or fix TMPDIR permissions, then rerun doctor --e2e-notify --json."
    elif not skills_writable:
        next_step = "Codex hooks can be installed, but Skill discovery may be degraded because ~/.codex/skills is not writable."

    return {
        "ok": failed_code is None,
        "away_home": {
            "path": str(away_home),
            "writable": away_home_writable,
        },
        "codex_access": {
            "agents_path": str(agents_path),
            "agents_writable": agents_writable,
            "hooks_path": str(hooks_path),
            "hooks_writable": hooks_writable,
            "skills_dir": str(skills_dir),
            "skills_writable": skills_writable,
        },
        "runtime": {
            "path": str(paths.runtime_state_path),
            "writable": runtime_writable,
            "detail": runtime_detail,
        },
        "legacy": {
            "old_codex_data_dir_present": legacy_data_dir.exists(),
            "old_hooks_reference_old_wrapper": _hooks_reference_old_wrapper(hooks_path, legacy_data_dir),
        },
        "failed_code": failed_code,
        "next_step": next_step,
    }


def run_install(
    paths,
    *,
    dry_run: bool = False,
    yes: bool = False,
    cli_command: str | None = None,
    source_scripts_dir=None,
    source_skill_dir=None,
    runtime_resolver=None,
    lark=None,
) -> dict[str, Any]:
    config = load_config(paths.config_path)
    lark_cli_path = _official_lark_cli_path(config.lark_cli_path)
    away_home = Path(getattr(paths, "away_home", paths.data_dir))
    wrapper_path = _wrapper_path(paths)
    scripts_dir = _scripts_dir(paths)
    skill_source_dir = _skill_source_dir(paths)
    skill_install_dir = _skill_install_dir(paths)
    planned_changes = [
        f"Would write Codex Away Mode files to {away_home}.",
        f"Would write managed global guidance to {paths.global_agents}.",
        f"Would write managed Codex hooks to {paths.hooks_json}.",
        f"Would create managed CLI wrapper at {wrapper_path}.",
        f"Would copy runtime scripts to {scripts_dir}.",
        f"Would copy source Skill package to {skill_source_dir}.",
        f"Would sync Skill discovery entry at {skill_install_dir}.",
        f"Would keep runtime state at {paths.runtime_state_path}.",
        "Would use browser-confirmed config init --new and Feishu permission setup as the guided setup path.",
        f"Would use official lark-cli at {lark_cli_path}; this installer does not create Open Platform bots.",
    ]

    if dry_run or not yes:
        return {
            "ok": True,
            "dry_run": True,
            "planned_changes": planned_changes,
            "changed": [],
            "lark_cli_path": lark_cli_path,
            "next_step": "Review the planned global writes, then rerun with --yes.",
        }

    preflight = run_preflight(paths)
    if not preflight["ok"]:
        return {
            "ok": False,
            "dry_run": False,
            "failed_code": preflight["failed_code"],
            "preflight": preflight,
            "user_message": _preflight_user_message(preflight["failed_code"]),
            "agent_next_step": preflight["next_step"],
            "changed": [],
        }

    migrated_config = _migrate_legacy_config_if_needed(paths)
    if migrated_config:
        config = load_config(paths.config_path)
        lark_cli_path = _official_lark_cli_path(config.lark_cli_path)

    try:
        store = open_install_store(paths)
    except Exception as exc:
        return {
            "ok": False,
            "dry_run": False,
            "failed_code": "install_state_unwritable",
            "detail": str(exc),
            "user_message": "Codex Away Mode 需要更新全局安装状态，但当前环境不能写入安装状态文件。",
            "agent_next_step": (
                "Ask the user to approve global CODEX_HOME writes, then rerun "
                "codex-away-mode install --yes --json."
            ),
            "changed": [],
        }
    repaired_orphans = store.close_orphan_away_sessions(
        closed_at=datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
    )
    resolver = runtime_resolver or resolve_runtime
    runtime = resolver(paths)
    if runtime is None:
        status = store.update_install_status(
            status="runtime_missing",
            failed_code="runtime_missing",
            waiting_for="runtime_install",
            next_step="Install Python 3 or uv, then rerun codex-away-mode install --yes --json.",
        )
        return {
            "ok": False,
            "dry_run": False,
            "failed_code": "runtime_missing",
            "user_message": "还缺一个运行环境。这个 Skill 需要 Python 3 或 uv 来运行本地通知程序。",
            "agent_next_step": status["next_step"],
            "changed": [],
        }

    changed = []
    degraded_codes: list[str] = []
    skill_discovery_mode = "not_configured"
    explanations = [
        f"Writing managed guidance block to global AGENTS file: {paths.global_agents}.",
        f"Writing managed command hooks to Codex hooks file: {paths.hooks_json}.",
        f"Writing managed CLI wrapper to {wrapper_path}.",
    ]

    source_scripts_dir = Path(source_scripts_dir) if source_scripts_dir else _source_scripts_dir()
    try:
        scripts_sync_mode = _sync_runtime_scripts(source_scripts_dir, scripts_dir)
    except InstallSyncError as exc:
        return {
            "ok": False,
            "dry_run": False,
            "failed_code": "scripts_sync_failed",
            "detail": str(exc),
            "user_message": "Codex Away Mode 更新本地程序文件失败，已尽量保留当前可运行版本。",
            "agent_next_step": "请保留这段错误信息，检查安装目录权限后重新运行 codex-away-mode install --yes --json。",
            "changed": changed,
        }
    changed.append(str(scripts_dir))
    source_skill_dir = Path(source_skill_dir) if source_skill_dir else _source_skill_dir()
    if (source_skill_dir / "SKILL.md").exists():
        _sync_skill_package(source_skill_dir, skill_source_dir)
        changed.append(str(skill_source_dir))
        if preflight["codex_access"].get("skills_writable"):
            skill_discovery_mode = _sync_skill_discovery(skill_source_dir, skill_install_dir)
            changed.append(str(skill_install_dir))
        else:
            skill_discovery_mode = "degraded"
            degraded_codes.append("skill_discovery_degraded")
    _write_wrapper(wrapper_path, runtime=runtime, scripts_dir=scripts_dir)
    changed.append(str(wrapper_path))

    if not Path(paths.config_path).exists():
        config = AppConfig()
    config.lark_cli_path = lark_cli_path
    save_config(paths.config_path, config)
    changed.append(str(paths.config_path))
    if migrated_config:
        changed.append(str(_legacy_config_path(paths)))
    store.update_install_status(
        status="local_config_created",
        next_step="Run codex-away-mode setup feishu --json.",
    )

    agents_path = Path(paths.global_agents)
    agents_path.parent.mkdir(parents=True, exist_ok=True)
    existing_guidance = (
        agents_path.read_text(encoding="utf-8") if agents_path.exists() else ""
    )
    _backup_existing(agents_path, paths.backup_dir)
    agents_path.write_text(
        hooks.install_guidance_block(existing_guidance),
        encoding="utf-8",
    )
    changed.append(str(agents_path))

    hooks.install_hooks(
        hooks_path=paths.hooks_json,
        backup_dir=paths.backup_dir,
        cli_command=cli_command or str(wrapper_path),
    )
    _invalidate_e2e_notify_if_verified(store)
    changed.append(str(paths.hooks_json))
    store.update_install_status(
        status="hook_trust_pending",
        waiting_for="hook_trust",
        next_step=(
            "Ask the user to trust the managed hooks in Codex Desktop Settings -> Hooks, "
            "run codex-away-mode doctor --e2e-notify --json to verify notification delivery, "
            "then run codex-away-mode doctor --json."
        ),
    )

    if lark is not None:
        notify.send_test_notification(paths, lark)
        changed.append(str(paths.config_path))

    store.set_install_state(
        "skill_discovery",
        {
            "status": skill_discovery_mode,
            "mode": skill_discovery_mode,
        },
    )

    return {
        "ok": True,
        "dry_run": False,
        "planned_changes": planned_changes,
        "write_explanations": explanations,
        "changed": changed,
        "degraded_codes": degraded_codes,
        "skill_discovery_mode": skill_discovery_mode,
        "scripts_sync_mode": scripts_sync_mode,
        "repaired_orphan_away_sessions": len(repaired_orphans),
        "lark_cli_path": lark_cli_path,
        "wrapper_path": str(wrapper_path),
        "status": "hook_trust_pending",
        "next_step": (
            "Run codex-away-mode doctor --e2e-notify --json to verify notification delivery. "
            "Then ask the user to trust the managed hooks in Codex Desktop Settings -> Hooks "
            "and run codex-away-mode doctor --json."
        ),
    }


def install_status(paths) -> dict[str, Any]:
    status = open_install_store(paths).install_status()
    return {"ok": True, **status}


def resolve_runtime(paths) -> str | None:
    wrapper = _wrapper_path(paths)
    if wrapper.exists() and os.access(wrapper, os.X_OK):
        return sys.executable
    for candidate in ("python3", "python"):
        resolved = shutil.which(candidate)
        if resolved and _is_python3(resolved):
            return resolved
    uv = shutil.which("uv")
    if uv:
        return sys.executable
    if sys.executable and _is_python3(sys.executable):
        return sys.executable
    return None


def _is_python3(path: str) -> bool:
    try:
        completed = subprocess.run(
            [path, "-c", "import sys; print(sys.version_info[0])"],
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return completed.stdout.strip() == "3"


def _source_scripts_dir() -> Path:
    return Path(__file__).resolve().parents[1]


def _scripts_dir(paths) -> Path:
    return Path(getattr(paths, "scripts_dir", Path(paths.data_dir) / "scripts"))


def _skill_install_dir(paths) -> Path:
    return Path(getattr(paths, "skill_install_dir", Path(paths.codex_home) / "skills" / "codex-away-mode"))


def _skill_source_dir(paths) -> Path:
    return Path(getattr(paths, "skill_source_dir", Path(paths.data_dir) / "skill"))


def _wrapper_path(paths) -> Path:
    return Path(getattr(paths, "wrapper_path", Path(paths.data_dir) / "bin" / "codex-away-mode"))


def _sync_runtime_scripts(source: Path, destination: Path) -> str:
    source = source.resolve()
    destination = destination.resolve()
    if source == destination:
        return "self_source_skipped"
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = destination.parent / f".{destination.name}-staging-{uuid.uuid4().hex}"
    backup = destination.parent / f".{destination.name}-backup-{uuid.uuid4().hex}"
    backup_created = False
    try:
        shutil.copytree(
            source,
            staging,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".pytest_cache"),
        )
        if destination.exists():
            destination.rename(backup)
            backup_created = True
        try:
            staging.rename(destination)
        except OSError as exc:
            if backup_created:
                try:
                    backup.rename(destination)
                except OSError as rollback_exc:
                    raise InstallSyncError(
                        f"replace failed: {exc}; rollback failed: {rollback_exc}; backup retained at {backup}"
                    ) from exc
            raise InstallSyncError(f"replace failed: {exc}") from exc
        if backup_created and backup.exists():
            shutil.rmtree(backup, ignore_errors=True)
        return "replaced"
    except InstallSyncError:
        raise
    except OSError as exc:
        raise InstallSyncError(str(exc)) from exc
    finally:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)


def _source_skill_dir() -> Path:
    return Path(__file__).resolve().parents[2]


def _sync_skill_package(source: Path, destination: Path) -> None:
    source = source.resolve()
    destination = destination.resolve()
    if source == destination:
        return
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        shutil.rmtree(destination)
    shutil.copytree(
        source,
        destination,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".pytest_cache"),
    )


def _sync_skill_discovery(source: Path, destination: Path) -> str:
    if _create_skill_symlink(source, destination):
        return "symlink"
    if _write_thin_skill_shim(source, destination):
        return "thin_shim"
    _sync_skill_package(source, destination)
    return "full_copy"


def _create_skill_symlink(source: Path, destination: Path) -> bool:
    try:
        _remove_path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.symlink_to(source, target_is_directory=True)
        return True
    except OSError:
        return False


def _write_thin_skill_shim(source: Path, destination: Path) -> bool:
    try:
        _remove_path(destination)
        destination.mkdir(parents=True, exist_ok=True)
        destination.joinpath("SKILL.md").write_text(
            _thin_skill_shim_content(source),
            encoding="utf-8",
        )
        return True
    except OSError:
        return False


def _thin_skill_shim_content(source: Path) -> str:
    return (
        "---\n"
        "name: codex-away-mode\n"
        "description: Keep a live Codex turn reachable while the user is away, with Feishu completion notifications, Away Mode checkpoint cards, and short-window Feishu replies that continue the current Codex turn. Use when the user asks for Codex Away Mode, Feishu completion notifications, waiting for Feishu replies before continuing, or remote continuation while away from the computer.\n"
        "---\n\n"
        "# Codex Away Mode\n\n"
        f"The installed Codex Away Mode package lives at `{source}`.\n\n"
        f"Read `{source / 'SKILL.md'}` and its `references/` files before using this skill.\n"
    )


def _remove_path(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.exists():
        shutil.rmtree(path)


def _write_wrapper(path: Path, *, runtime: str, scripts_dir: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    content = (
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n\n"
        f'RUNTIME="{runtime}"\n'
        f'export PYTHONPATH="{scripts_dir}:${{PYTHONPATH:-}}"\n'
        'exec "$RUNTIME" -m codex_away_mode "$@"\n'
    )
    path.write_text(content, encoding="utf-8")
    path.chmod(0o755)


def _official_lark_cli_path(configured_path: str | None) -> str:
    if configured_path and configured_path != "lark-cli":
        return configured_path
    return shutil.which(configured_path or "lark-cli") or (configured_path or "lark-cli")


def _backup_existing(path: Path, backup_dir) -> Path | None:
    if not path.exists():
        return None
    backup_dir = Path(backup_dir)
    backup_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    backup_path = backup_dir / f"{path.name}.{timestamp}.bak"
    shutil.copy2(path, backup_path)
    return backup_path


def _migrate_legacy_config_if_needed(paths) -> bool:
    config_path = Path(paths.config_path)
    legacy_path = _legacy_config_path(paths)
    if config_path.exists() or not legacy_path.exists() or config_path == legacy_path:
        return False
    config_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(legacy_path, config_path)
    return True


def _legacy_config_path(paths) -> Path:
    return Path(paths.codex_home) / "codex-away-mode" / "config.toml"


def _directory_writable(path: Path) -> bool:
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / ".codex-away-mode-write-probe"
        probe.write_text("probe", encoding="utf-8")
        probe.unlink()
        return True
    except OSError:
        return False


def _path_parent_writable(path: Path) -> bool:
    return _directory_writable(path.parent)


def _hooks_reference_old_wrapper(hooks_path: Path, legacy_data_dir: Path) -> bool:
    if not hooks_path.exists():
        return False
    try:
        return str(legacy_data_dir) in hooks_path.read_text(encoding="utf-8")
    except OSError:
        return False


def _preflight_user_message(failed_code: str | None) -> str:
    if failed_code == "away_home_unwritable":
        return "Codex Away Mode 需要写入 ~/.codex-away-mode 保存程序和配置，但当前环境不可写。"
    if failed_code == "codex_access_unwritable":
        return "程序主体可以安装，但还不能写入 Codex 的 AGENTS.md 或 hooks.json 接入层。"
    if failed_code == "hook_runtime_unwritable":
        return "Codex Away Mode runtime store 不可写，Stop hook 和 Away Mode 无法可靠运行。"
    return "Codex Away Mode install preflight failed."


def _invalidate_e2e_notify_if_verified(store: StateStore) -> None:
    state = store.get_install_state("e2e_notify", {})
    if state.get("status") != "verified":
        return
    store.set_install_state(
        "e2e_notify",
        {
            **state,
            "status": "invalidated",
            "invalidated_at": datetime.now(timezone.utc).isoformat(),
            "invalidated_reason": "hooks_rewritten",
        },
    )
