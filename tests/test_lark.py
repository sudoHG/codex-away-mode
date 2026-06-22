import json
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
