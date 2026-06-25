from __future__ import annotations

import sys
from pathlib import Path

from codex_away_mode import install
from codex_away_mode.config import AppConfig, load_config, save_config


class FakePaths:
    def __init__(self, root: Path):
        self.codex_home = root / ".codex"
        self.away_home = root / ".codex-away-mode"
        self.data_dir = self.away_home
        self.bin_dir = self.data_dir / "bin"
        self.wrapper_path = self.bin_dir / "codex-away-mode"
        self.scripts_dir = self.data_dir / "scripts"
        self.skill_source_dir = self.data_dir / "skill"
        self.skill_install_dir = self.codex_home / "skills" / "codex-away-mode"
        self.config_path = self.data_dir / "config.toml"
        self.install_state_path = self.data_dir / "install-state.sqlite"
        self.log_dir = self.data_dir / "logs"
        self.backup_dir = self.data_dir / "backups"
        self.runtime_dir = root / "runtime"
        self.runtime_state_path = self.runtime_dir / "state.sqlite"
        self.runtime_prompt_marker_dir = self.runtime_dir / "user-turns"
        self.runtime_summary_dir = self.runtime_dir / "summaries"
        self.hooks_json = self.codex_home / "hooks.json"
        self.codex_config_path = self.codex_home / "config.toml"
        self.global_agents = self.codex_home / "AGENTS.md"


def _source_scripts(root: Path) -> Path:
    scripts = root / "source-scripts"
    package = scripts / "codex_away_mode"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    return scripts


def _source_skill(root: Path) -> Path:
    skill = root / "source-skill"
    skill.mkdir()
    (skill / "SKILL.md").write_text(
        "---\nname: codex-away-mode\ndescription: test skill\n---\n",
        encoding="utf-8",
    )
    return skill


def test_install_uses_pinned_private_lark_cli_for_default_config(tmp_path):
    paths = FakePaths(tmp_path)
    save_config(paths.config_path, AppConfig(lark_cli_path="lark-cli"))
    calls = []

    def fake_installer(*, package: str, prefix: Path) -> Path:
        calls.append({"package": package, "prefix": prefix})
        binary = prefix / "node_modules" / ".bin" / "lark-cli"
        binary.parent.mkdir(parents=True)
        binary.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
        binary.chmod(0o755)
        return binary

    result = install.run_install(
        paths,
        yes=True,
        source_scripts_dir=_source_scripts(tmp_path),
        source_skill_dir=_source_skill(tmp_path),
        runtime_resolver=lambda _: sys.executable,
        ensure_lark_cli=True,
        lark_cli_installer=fake_installer,
    )

    expected_binary = paths.data_dir / "npm" / "node_modules" / ".bin" / "lark-cli"
    assert result["ok"] is True
    assert result["lark_cli_path"] == str(expected_binary)
    assert result["lark_cli_install_mode"] == "managed_pinned"
    assert calls == [{"package": "@larksuite/cli@1.0.57", "prefix": paths.data_dir / "npm"}]
    assert load_config(paths.config_path).lark_cli_path == str(expected_binary)
    assert all("latest" not in item for item in result["planned_changes"])
