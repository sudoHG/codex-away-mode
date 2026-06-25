from __future__ import annotations

import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import hooks


def run_uninstall(
    paths,
    *,
    keep_data: bool = False,
    delete_data: bool = False,
) -> dict[str, Any]:
    if keep_data and delete_data:
        return {
            "ok": False,
            "changed": [],
            "next_step": "Choose either keep_data or delete_data, not both.",
        }

    changed = []
    agents_path = Path(paths.global_agents)
    if agents_path.exists():
        _backup_existing(agents_path, paths.backup_dir)
        agents_path.write_text(
            _remove_guidance_block(agents_path.read_text(encoding="utf-8")),
            encoding="utf-8",
        )
        changed.append(str(agents_path))

    hooks_path = Path(paths.hooks_json)
    if hooks_path.exists():
        hooks.uninstall_hooks(hooks_path=hooks_path, backup_dir=paths.backup_dir)
        changed.append(str(hooks_path))

    skill_install_dir = Path(getattr(paths, "skill_install_dir", Path(paths.codex_home) / "skills" / "codex-away-mode"))
    if _is_managed_skill_discovery(skill_install_dir):
        if skill_install_dir.is_symlink() or skill_install_dir.is_file():
            skill_install_dir.unlink()
        else:
            shutil.rmtree(skill_install_dir)
        changed.append(str(skill_install_dir))

    if delete_data:
        data_dir = Path(paths.data_dir)
        if data_dir.exists():
            shutil.rmtree(data_dir)
            changed.append(str(data_dir))
        next_step = "Managed hooks/guidance removed and data directory deleted."
    else:
        next_step = "Managed hooks/guidance removed; data directory kept."

    return {
        "ok": True,
        "changed": changed,
        "data_deleted": bool(delete_data),
        "next_step": next_step,
    }


def _remove_guidance_block(content: str) -> str:
    start = content.find(hooks.GUIDANCE_START)
    end = content.find(hooks.GUIDANCE_END)
    if start == -1 or end == -1 or end < start:
        return content
    return (content[:start] + content[end + len(hooks.GUIDANCE_END) :]).strip() + "\n"


def _backup_existing(path: Path, backup_dir) -> Path | None:
    if not path.exists():
        return None
    backup_dir = Path(backup_dir)
    backup_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    backup_path = backup_dir / f"{path.name}.{timestamp}.bak"
    shutil.copy2(path, backup_path)
    return backup_path


def _is_managed_skill_discovery(path: Path) -> bool:
    if not path.exists() and not path.is_symlink():
        return False
    if path.is_symlink():
        return True
    skill_md = path / "SKILL.md"
    if not skill_md.exists():
        return False
    try:
        return "name: codex-away-mode" in skill_md.read_text(encoding="utf-8")
    except OSError:
        return False
