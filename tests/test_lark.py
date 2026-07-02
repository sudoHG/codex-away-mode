import json
from pathlib import Path
import subprocess

import pytest

from codex_away_mode.lark import InvalidJsonError, LarkCli, LarkCliError


class FakeRunner:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, args, timeout):
        self.calls.append((args, timeout))
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


def test_send_interactive_card_builds_verified_command_and_maps_result():
    runner = FakeRunner(
        [
            {
                "data": {
                    "message_id": "om_test_message",
                    "chat_id": "oc_test_chat",
                }
            }
        ]
    )
    cli = LarkCli("lark-cli", runner=runner)

    result = cli.send_interactive_card(
        user_id="ou_test_user",
        card={"config": {"wide_screen_mode": True}},
    )

    args, timeout = runner.calls[0]
    assert args[:8] == [
        "im",
        "+messages-send",
        "--as",
        "bot",
        "--user-id",
        "ou_test_user",
        "--msg-type",
        "interactive",
    ]
    assert "--json" in args
    content = json.loads(args[args.index("--content") + 1])
    assert content == {"config": {"wide_screen_mode": True}}
    assert timeout == 30
    assert result.message_id == "om_test_message"
    assert result.chat_id == "oc_test_chat"


def test_send_text_builds_verified_chat_command():
    runner = FakeRunner([{"data": {"message_id": "om_text", "chat_id": "oc_chat"}}])
    cli = LarkCli("lark-cli", runner=runner)

    result = cli.send_text(chat_id="oc_chat", text="请回复对应卡片")

    args, _ = runner.calls[0]
    assert args == [
        "im",
        "+messages-send",
        "--as",
        "bot",
        "--chat-id",
        "oc_chat",
        "--msg-type",
        "text",
        "--content",
        json.dumps({"text": "请回复对应卡片"}, ensure_ascii=False),
        "--json",
    ]
    assert result.message_id == "om_text"
    assert result.chat_id == "oc_chat"


def test_urgent_app_builds_verified_command():
    runner = FakeRunner([{"data": {"invalid_user_id_list": []}}])
    cli = LarkCli("lark-cli", runner=runner)

    result = cli.urgent_app(message_id="om_permission", user_id_list=["ou_test_user"])

    args, timeout = runner.calls[0]
    assert args == [
        "im",
        "messages",
        "urgent_app",
        "--as",
        "bot",
        "--message-id",
        "om_permission",
        "--user-id-type",
        "open_id",
        "--data",
        json.dumps({"user_id_list": ["ou_test_user"]}, separators=(",", ":")),
        "--json",
    ]
    assert timeout == 30
    assert result["data"]["invalid_user_id_list"] == []


def test_preflight_urgent_app_command_checks_verified_terms():
    runner = FakeRunner(
        [
            "--as --message-id --user-id-type --data --json",
        ]
    )
    cli = LarkCli("lark-cli", runner=runner)

    assert cli.preflight_urgent_app_command() == {"ok": True}
    assert runner.calls[0][0] == ["im", "messages", "urgent_app", "--help"]


def test_version_info_uses_lark_cli_version_command():
    runner = FakeRunner(["lark-cli version 1.0.57\n"])
    cli = LarkCli("/managed/lark-cli", runner=runner)

    result = cli.version_info()

    assert result == {"binary": "/managed/lark-cli", "version": "lark-cli version 1.0.57"}
    assert runner.calls[0][0] == ["--version"]


def test_list_messages_builds_verified_command_and_maps_messages():
    runner = FakeRunner(
        [
            {
                "items": [
                    {
                        "message_id": "om_reply",
                        "reply_to": "om_card",
                        "msg_type": "text",
                        "body": {"content": "{\"text\":\"收到\"}"},
                        "sender": {"sender_type": "user"},
                        "create_time": "1710000000000",
                    }
                ]
            }
        ]
    )
    cli = LarkCli("lark-cli", runner=runner)

    messages = cli.list_messages(chat_id="oc_test")

    assert runner.calls[0][0] == [
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
    assert messages[0].message_id == "om_reply"
    assert messages[0].reply_to == "om_card"
    assert messages[0].msg_type == "text"
    assert messages[0].content_text == "收到"
    assert messages[0].sender_type == "user"
    assert messages[0].create_time == "1710000000000"


def test_list_messages_maps_real_lark_cli_data_messages_shape():
    runner = FakeRunner(
        [
            {
                "ok": True,
                "data": {
                    "messages": [
                        {
                            "message_id": "om_reply",
                            "reply_to": "om_card",
                            "msg_type": "text",
                            "content": "{\"text\":\"收到\"}",
                            "sender": {"sender_type": "user"},
                            "create_time": "1710000000000",
                        }
                    ]
                },
            }
        ]
    )
    cli = LarkCli("lark-cli", runner=runner)

    messages = cli.list_messages(chat_id="oc_test")

    assert len(messages) == 1
    assert messages[0].message_id == "om_reply"
    assert messages[0].reply_to == "om_card"
    assert messages[0].content_text == "收到"


def test_list_messages_maps_reply_route_fallback_fields():
    runner = FakeRunner(
        [
            {
                "data": {
                    "messages": [
                        {
                            "message_id": "om_reply",
                            "parent_id": "om_card",
                            "msg_type": "text",
                            "content": "{\"text\":\"收到\"}",
                            "sender": {"sender_type": "user"},
                        }
                    ]
                },
            }
        ]
    )
    cli = LarkCli("lark-cli", runner=runner)

    messages = cli.list_messages(chat_id="oc_test")

    assert messages[0].reply_to == "om_card"


def test_list_messages_extracts_nested_lark_content_text():
    runner = FakeRunner(
        [
            {
                "data": {
                    "messages": [
                        {
                            "message_id": "om_reply",
                            "reply_to": "om_card",
                            "msg_type": "post",
                            "content": json.dumps(
                                {
                                    "zh_cn": {
                                        "content": [
                                            [
                                                {"tag": "text", "text": "/延长等待"},
                                                {"tag": "at", "user_name": "Codex"},
                                            ]
                                        ]
                                    }
                                },
                                ensure_ascii=False,
                            ),
                            "sender": {"sender_type": "user"},
                        }
                    ]
                },
            }
        ]
    )
    cli = LarkCli("lark-cli", runner=runner)

    messages = cli.list_messages(chat_id="oc_test")

    assert messages[0].content_text == "/延长等待"


def test_list_messages_preserves_repeated_nested_text_chunks():
    runner = FakeRunner(
        [
            {
                "data": {
                    "messages": [
                        {
                            "message_id": "om_reply",
                            "reply_to": "om_card",
                            "msg_type": "post",
                            "content": json.dumps(
                                {"content": [[{"text": "哈"}, {"text": "哈"}]]},
                                ensure_ascii=False,
                            ),
                            "sender": {"sender_type": "user"},
                        }
                    ]
                },
            }
        ]
    )
    cli = LarkCli("lark-cli", runner=runner)

    messages = cli.list_messages(chat_id="oc_test")

    assert messages[0].content_text == "哈哈"


def test_add_reaction_builds_verified_command():
    runner = FakeRunner([{"data": {"ok": True}}])
    cli = LarkCli("lark-cli", runner=runner)

    cli.add_reaction(message_id="om_reply", emoji_type="Get")

    assert runner.calls[0][0] == [
        "im",
        "reactions",
        "create",
        "--as",
        "bot",
        "--message-id",
        "om_reply",
        "--data",
        '{"reaction_type":{"emoji_type":"Get"}}',
        "--json",
    ]


def test_non_zero_subprocess_raises_typed_error_with_redacted_ids():
    runner = FakeRunner(
        [
            subprocess.CalledProcessError(
                returncode=2,
                cmd=["lark-cli", "im"],
                stderr="failed for ou_private_user token abcdef1234567890",
            )
        ]
    )
    cli = LarkCli("lark-cli", runner=runner)

    with pytest.raises(LarkCliError) as excinfo:
        cli.list_messages(chat_id="oc_private_chat")

    message = str(excinfo.value)
    assert "ou_private_user" not in message
    assert "oc_private_chat" not in message
    assert "abcdef1234567890" not in message


def test_invalid_json_raises_typed_error():
    cli = LarkCli("lark-cli", runner=FakeRunner(["not-json"]))

    with pytest.raises(InvalidJsonError):
        cli.list_messages(chat_id="oc_test")


def test_json_parser_accepts_non_json_prefix():
    cli = LarkCli("lark-cli", runner=FakeRunner(["debug line\n{\"items\": []}\n"]))

    assert cli.list_messages(chat_id="oc_test") == []


def test_preflight_checks_config_init_new_without_requiring_json():
    runner = FakeRunner(
        [
            "auth help login status qrcode",
            "auth login --json --no-wait --device-code --recommend",
            "auth status --json --verify",
            "auth qrcode --output --ascii",
            "im +chat-list --as --types p2p",
            "im +chat-messages-list --as --chat-id --user-id",
            "config init --new --app-id --app-secret-stdin",
        ]
    )
    cli = LarkCli("lark-cli", runner=runner)

    assert cli.preflight_auth_commands() == {"ok": True}
    assert runner.calls[-1][0] == ["config", "init", "--help"]


def test_app_config_status_maps_not_configured_json_from_stderr():
    runner = FakeRunner(
        [
            subprocess.CompletedProcess(
                args=["lark-cli", "config", "show"],
                returncode=1,
                stdout="",
                stderr=json.dumps(
                    {
                        "ok": False,
                        "error": {
                            "type": "config",
                            "subtype": "not_configured",
                            "hint": "run `lark-cli config init --new`",
                        },
                    }
                ),
            )
        ]
    )
    cli = LarkCli("/managed/lark-cli", runner=runner)

    result = cli.app_config_status()

    assert result["ok"] is False
    assert result["status"] == "lark_app_config_pending"
    assert result["config_command"] == ["/managed/lark-cli", "config", "init", "--new"]
    assert "--json" not in result["config_command"]


def test_app_config_status_reports_configured():
    runner = FakeRunner(
        [
            subprocess.CompletedProcess(
                args=["lark-cli", "config", "show"],
                returncode=0,
                stdout='{"ok":true,"appId":"cli_xxx"}',
                stderr="",
            )
        ]
    )
    cli = LarkCli("/managed/lark-cli", runner=runner)

    result = cli.app_config_status()

    assert result["ok"] is True
    assert result["configured"] is True


def test_start_app_config_init_opens_browser_from_cli_output_without_json():
    opened_urls = []
    runner = FakeRunner(
        [
            subprocess.TimeoutExpired(
                cmd=["lark-cli", "config", "init", "--new"],
                timeout=2,
                output="Open https://example.feishu.cn/config?code=abc123 to continue",
                stderr="",
            )
        ]
    )
    cli = LarkCli("/managed/lark-cli", runner=runner)

    result = cli.start_app_config_init(
        opener=lambda url: opened_urls.append(url) or True,
        wait_seconds=2,
    )

    assert result["ok"] is False
    assert result["status"] == "lark_app_config_browser_pending"
    assert result["verification_url"] == "https://example.feishu.cn/config?code=abc123"
    assert result["browser_opened"] is True
    assert opened_urls == ["https://example.feishu.cn/config?code=abc123"]
    assert runner.calls[0][0] == ["config", "init", "--new"]
    assert "--json" not in runner.calls[0][0]


def test_start_app_config_init_returns_url_when_browser_open_fails():
    runner = FakeRunner(
        [
            subprocess.TimeoutExpired(
                cmd=["lark-cli", "config", "init", "--new"],
                timeout=2,
                output="Visit https://example.feishu.cn/config?code=abc123",
                stderr="",
            )
        ]
    )
    cli = LarkCli("/managed/lark-cli", runner=runner)

    result = cli.start_app_config_init(opener=lambda url: False, wait_seconds=2)

    assert result["ok"] is False
    assert result["status"] == "lark_app_config_browser_pending"
    assert result["browser_opened"] is False
    assert result["verification_url"] == "https://example.feishu.cn/config?code=abc123"


def test_start_app_config_init_rechecks_status_when_process_completes():
    runner = FakeRunner(
        [
            subprocess.CompletedProcess(
                args=["lark-cli", "config", "init", "--new"],
                returncode=0,
                stdout="Done https://example.feishu.cn/config?code=abc123",
                stderr="",
            ),
            subprocess.CompletedProcess(
                args=["lark-cli", "config", "show"],
                returncode=0,
                stdout='{"ok":true,"appId":"cli_xxx"}',
                stderr="",
            ),
        ]
    )
    cli = LarkCli("/managed/lark-cli", runner=runner)

    result = cli.start_app_config_init(opener=lambda url: True, wait_seconds=2)

    assert result["ok"] is True
    assert result["configured"] is True
    assert runner.calls[0][0] == ["config", "init", "--new"]
    assert runner.calls[1][0] == ["config", "show"]


def test_start_app_config_init_redacts_failure_detail():
    runner = FakeRunner(
        [
            subprocess.CompletedProcess(
                args=["lark-cli", "config", "init", "--new"],
                returncode=1,
                stdout="",
                stderr="failed token abcdef1234567890 for app_fakecredential",
            )
        ]
    )
    cli = LarkCli("/managed/lark-cli", runner=runner)

    result = cli.start_app_config_init(wait_seconds=2)

    assert result["ok"] is False
    assert result["failed_code"] == "lark_app_config_init_failed"
    detail = json.dumps(result["developer_detail"], ensure_ascii=False)
    assert "abcdef1234567890" not in detail
    assert "app_fakecredential" not in detail


def test_auth_login_start_opens_oauth_verification_url():
    opened_urls = []
    runner = FakeRunner(
        [
            {
                "ok": True,
                "data": {
                    "verification_url": "https://accounts.feishu.cn/oauth/v1/device/verify?user_code=ABCD",
                    "device_code": "device-code-1",
                },
            }
        ]
    )
    cli = LarkCli("/managed/lark-cli", runner=runner)

    result = cli.auth_login_start(opener=lambda url: opened_urls.append(url) or True)

    assert result["browser_opened"] is True
    assert result["verification_url"] == "https://accounts.feishu.cn/oauth/v1/device/verify?user_code=ABCD"
    assert opened_urls == ["https://accounts.feishu.cn/oauth/v1/device/verify?user_code=ABCD"]
    assert runner.calls == [
        (["auth", "login", "--recommend", "--no-wait", "--json"], 30)
    ]


def test_auth_login_start_reports_browser_open_failure():
    runner = FakeRunner(
        [
            {
                "ok": True,
                "data": {
                    "verification_uri_complete": "https://accounts.feishu.cn/oauth/v1/device/verify?user_code=ABCD",
                    "device_code": "device-code-1",
                },
            }
        ]
    )
    cli = LarkCli("/managed/lark-cli", runner=runner)

    result = cli.auth_login_start(opener=lambda url: False)

    assert result["browser_opened"] is False
    assert result["verification_url"] == "https://accounts.feishu.cn/oauth/v1/device/verify?user_code=ABCD"


def test_auth_login_complete_uses_oauth_timeout():
    runner = FakeRunner([{"ok": True, "data": {"openId": "ou_user"}}])
    cli = LarkCli("/managed/lark-cli", runner=runner)

    result = cli.auth_login_complete("device-code-1")

    assert result["data"]["openId"] == "ou_user"
    assert runner.calls == [
        (
            ["auth", "login", "--device-code", "device-code-1", "--json"],
            90,
        )
    ]


def test_auth_login_complete_timeout_returns_still_pending():
    runner = FakeRunner(
        [
            subprocess.TimeoutExpired(
                cmd=["lark-cli", "auth", "login", "--device-code", "device-code-1", "--json"],
                timeout=90,
                output="waiting for user authorization",
                stderr="",
            )
        ]
    )
    cli = LarkCli("/managed/lark-cli", runner=runner)

    result = cli.auth_login_complete("device-code-1")

    assert result["ok"] is False
    assert result["status"] == "feishu_authorization_still_pending"
    assert result["failed_code"] == "feishu_authorization_still_pending"
    assert runner.calls[0][1] == 90


def test_install_docs_do_not_make_config_init_a_primary_user_step():
    root = Path(__file__).resolve().parents[1]
    install_doc = (root / "codex-away-mode" / "references" / "install.md").read_text(
        encoding="utf-8"
    )

    assert "returns the exact `lark-cli config init --new` command to run" not in install_doc
    assert "打开飞书配置页面" in install_doc or "browser" in install_doc.lower()
