from codex_away_mode.hooks import managed_stop_command, managed_user_prompt_command
from codex_away_mode.lark import LarkCli
import json
import os
import subprocess
from pathlib import Path


def test_managed_hook_commands_do_not_require_cwd():
    assert managed_stop_command("/bin/codex-away-mode") == (
        "/bin/codex-away-mode notify stop --json"
    )
    assert managed_user_prompt_command("/bin/codex-away-mode") == (
        "/bin/codex-away-mode notify mark-prompt --json"
    )


def test_lark_cli_message_list_command_shape():
    cli = LarkCli("lark-cli", runner=lambda args, timeout: {"items": []})

    cli.list_messages(chat_id="oc_test")

    args, _ = cli.runner_calls[-1]
    assert args == [
        "im",
        "+chat-messages-list",
        "--as",
        "bot",
        "--chat-id",
        "oc_test",
        "--page-size",
        "50",
        "--order",
        "desc",
        "--no-reactions",
        "--json",
    ]


def test_bundled_cli_bootstrap_reports_runtime_missing_without_python(tmp_path):
    script = Path("codex-away-mode/scripts/codex-away-mode")

    result = subprocess.run(
        ["/bin/bash", str(script), "--json", "version"],
        capture_output=True,
        text=True,
        env={"PATH": str(tmp_path)},
        check=False,
    )

    assert result.returncode == 1
    payload = json.loads(result.stdout)
    assert payload["ok"] is False
    assert payload["failed_code"] == "runtime_missing"


def test_public_docs_use_away_home_and_stage_summary_contract():
    readme = Path("README.md").read_text(encoding="utf-8")
    install_ref = Path("codex-away-mode/references/install.md").read_text(encoding="utf-8")

    combined = readme + "\n" + install_ref
    assert "${CODEX_HOME:-$HOME/.codex}/codex-away-mode" not in combined
    assert "${CODEX_HOME:-~/.codex}/codex-away-mode" not in combined
    assert ".codex-away-mode/latest-summary.md" not in readme
    assert "notify stage-summary" in readme
    assert "${CODEX_AWAY_HOME:-$HOME/.codex-away-mode}/bin/codex-away-mode" in combined


def test_skill_and_usage_docs_use_quick_start_and_token_resume_contract():
    skill = Path("codex-away-mode/SKILL.md").read_text(encoding="utf-8")
    usage = Path("codex-away-mode/references/usage.md").read_text(encoding="utf-8")

    assert "quick-start" in skill
    assert "Do not run doctor" in skill
    assert "Never resume an Away Session discovered from" in skill
    assert "away status" in skill
    assert "doctor --route-probe verifies exact routing" not in skill
    assert "codex-away-mode away start" in usage
    assert "codex-away-mode away resume" in usage
    assert "--resume-token" in usage
    assert "--extend-minutes" in skill
    assert "--extend-minutes" in usage
    assert "Do not inspect or modify Away Mode SQLite/StateStore directly" in skill
    assert "Do not inspect or modify Away Mode SQLite, StateStore, or runtime files directly" in usage
    assert "write a concise Codex chat note" in skill
    assert "received Feishu text" in skill
    assert "write the result or answer in the Codex chat before calling" in usage
    assert "Heartbeat or waiting-status text is still forbidden" in usage
    assert "away wait --resume <away_session_id>" not in usage
