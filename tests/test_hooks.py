import json

from codex_away_mode import hooks


def test_managed_commands_do_not_require_cwd():
    assert (
        hooks.managed_stop_command("/bin/codex-away-mode")
        == "/bin/codex-away-mode notify stop --json"
    )
    assert (
        hooks.managed_user_prompt_command("/bin/codex-away-mode")
        == "/bin/codex-away-mode notify mark-prompt --json"
    )


def test_guidance_block_is_idempotent_and_mentions_contract():
    original = "# Existing\n\nKeep this."

    once = hooks.install_guidance_block(original)
    twice = hooks.install_guidance_block(once)

    assert once == twice
    assert "notify stage-summary" in once
    assert '--session-id "${CODEX_THREAD_ID:-}"' in once
    assert ".codex-away-mode/latest-summary.md" not in once
    assert "Do not write Codex Away Mode summary" in once
    assert "~/.codex" not in once
    assert "Away Mode" in once
    assert "away start" in once
    assert "away resume" in once
    assert "keep_waiting" in once
    assert "--resume-token" in once
    assert "--extend-minutes <minutes>" in once
    assert "do not read or write Away Mode SQLite/StateStore directly" in once
    assert "Never call `away wait --resume <away_session_id>`" in once
    assert "away status" in once
    assert "goal" in once
    assert "not active" in once
    assert "Stop hook also suppresses completion notifications" in once
    assert "missing summary" in once
    assert "keep the Codex chat quiet" in once
    assert "heartbeat" in once
    assert "only send user-visible updates when a routed card reply arrives" in once
    assert "When a routed card reply arrives, write a concise Codex chat note that includes the received Feishu text and the action or answer you are about to give" in once
    assert "After completing that reply_text work, write the result or answer in the Codex chat before calling away resume" in once


def test_guidance_block_uses_managed_cli_command():
    content = hooks.install_guidance_block(
        "",
        cli_command="/managed/bin/codex-away-mode",
    )

    assert "`/managed/bin/codex-away-mode notify stage-summary --cwd \"$PWD\" --session-id \"${CODEX_THREAD_ID:-}\" --json`" in content
    assert "use `/managed/bin/codex-away-mode away start ... --json`" in content
    assert "call `/managed/bin/codex-away-mode away resume \"$away_session_id\" --resume-token \"$resume_token\"`" in content
    assert "`codex-away-mode notify stage-summary" not in content


def test_install_hooks_preserves_existing_groups_and_backs_up(tmp_path):
    hooks_path = tmp_path / "hooks.json"
    backup_dir = tmp_path / "backups"
    hooks_path.write_text(
        json.dumps(
            {
                "hooks": {
                    "Stop": [{"hooks": [{"type": "command", "command": "other stop"}]}],
                    "UserPromptSubmit": [
                        {"hooks": [{"type": "command", "command": "other prompt"}]}
                    ],
                }
            }
        ),
        encoding="utf-8",
    )

    hooks.install_hooks(
        hooks_path=hooks_path,
        backup_dir=backup_dir,
        cli_command="/bin/codex-away-mode",
    )

    installed = json.loads(hooks_path.read_text(encoding="utf-8"))
    stop_groups = installed["hooks"]["Stop"]
    prompt_groups = installed["hooks"]["UserPromptSubmit"]
    assert any(
        hook["command"] == "other stop"
        for group in stop_groups
        for hook in group["hooks"]
    )
    assert any(
        hook["command"] == "/bin/codex-away-mode notify stop --json"
        for group in stop_groups
        for hook in group["hooks"]
    )
    assert any(
        hook["command"] == "other prompt"
        for group in prompt_groups
        for hook in group["hooks"]
    )
    assert any(
        hook["command"] == "/bin/codex-away-mode notify mark-prompt --json"
        for group in prompt_groups
        for hook in group["hooks"]
    )
    assert all("matcher" not in group for group in stop_groups + prompt_groups)
    assert list(backup_dir.glob("hooks.json.*.bak"))


def test_install_hooks_is_idempotent(tmp_path):
    hooks_path = tmp_path / "hooks.json"
    backup_dir = tmp_path / "backups"

    hooks.install_hooks(
        hooks_path=hooks_path,
        backup_dir=backup_dir,
        cli_command="/bin/codex-away-mode",
    )
    hooks.install_hooks(
        hooks_path=hooks_path,
        backup_dir=backup_dir,
        cli_command="/bin/codex-away-mode",
    )

    installed = json.loads(hooks_path.read_text(encoding="utf-8"))
    stop_commands = [
        hook["command"]
        for group in installed["hooks"]["Stop"]
        for hook in group["hooks"]
    ]
    prompt_commands = [
        hook["command"]
        for group in installed["hooks"]["UserPromptSubmit"]
        for hook in group["hooks"]
    ]
    assert stop_commands.count("/bin/codex-away-mode notify stop --json") == 1
    assert prompt_commands.count("/bin/codex-away-mode notify mark-prompt --json") == 1


def test_install_hooks_replaces_old_managed_commands(tmp_path):
    hooks_path = tmp_path / "hooks.json"
    backup_dir = tmp_path / "backups"
    hooks_path.write_text(
        json.dumps(
            {
                "hooks": {
                    "Stop": [
                        {
                            "hooks": [
                                {
                                    "type": "command",
                                    "command": "codex-away-mode notify stop --json",
                                    "statusMessage": hooks.MANAGED_STATUS_MESSAGE,
                                }
                            ]
                        },
                        {"hooks": [{"type": "command", "command": "other stop"}]},
                    ],
                    "UserPromptSubmit": [
                        {
                            "hooks": [
                                {
                                    "type": "command",
                                    "command": "codex-away-mode notify mark-prompt --json",
                                    "statusMessage": hooks.MANAGED_STATUS_MESSAGE,
                                }
                            ]
                        },
                        {"hooks": [{"type": "command", "command": "other prompt"}]},
                    ],
                }
            }
        ),
        encoding="utf-8",
    )

    hooks.install_hooks(
        hooks_path=hooks_path,
        backup_dir=backup_dir,
        cli_command="/bin/codex-away-mode",
    )

    installed = json.loads(hooks_path.read_text(encoding="utf-8"))
    stop_commands = [
        hook["command"]
        for group in installed["hooks"]["Stop"]
        for hook in group["hooks"]
    ]
    prompt_commands = [
        hook["command"]
        for group in installed["hooks"]["UserPromptSubmit"]
        for hook in group["hooks"]
    ]
    assert stop_commands == ["other stop", "/bin/codex-away-mode notify stop --json"]
    assert prompt_commands == [
        "other prompt",
        "/bin/codex-away-mode notify mark-prompt --json",
    ]


def test_uninstall_removes_only_managed_entries(tmp_path):
    hooks_path = tmp_path / "hooks.json"
    backup_dir = tmp_path / "backups"
    hooks_path.write_text(
        json.dumps(
            {
                "hooks": {
                    "Stop": [
                        {
                            "hooks": [
                                {"type": "command", "command": "other stop"},
                                {
                                    "type": "command",
                                    "command": "/bin/codex-away-mode notify stop --json",
                                    "statusMessage": hooks.MANAGED_STATUS_MESSAGE,
                                },
                            ]
                        }
                    ],
                    "UserPromptSubmit": [
                        {
                            "hooks": [
                                {"type": "command", "command": "other prompt"},
                                {
                                    "type": "command",
                                    "command": "/bin/codex-away-mode notify mark-prompt --json",
                                    "statusMessage": hooks.MANAGED_STATUS_MESSAGE,
                                },
                            ]
                        }
                    ],
                }
            }
        ),
        encoding="utf-8",
    )

    hooks.uninstall_hooks(hooks_path=hooks_path, backup_dir=backup_dir)

    installed = json.loads(hooks_path.read_text(encoding="utf-8"))
    stop_commands = [
        hook["command"]
        for group in installed["hooks"]["Stop"]
        for hook in group["hooks"]
    ]
    prompt_commands = [
        hook["command"]
        for group in installed["hooks"]["UserPromptSubmit"]
        for hook in group["hooks"]
    ]
    assert stop_commands == ["other stop"]
    assert prompt_commands == ["other prompt"]
    assert list(backup_dir.glob("hooks.json.*.bak"))
