from codex_away_mode.away import classify_reply, route_message
from codex_away_mode.lark import LarkMessage


def _message(
    *,
    message_id="om_reply",
    reply_to="om_card",
    text="请继续处理",
    sender_type="user",
):
    return LarkMessage(
        message_id=message_id,
        reply_to=reply_to,
        msg_type="text",
        content_text=text,
        sender_type=sender_type,
        create_time="1710000000000",
    )


def test_classify_reply_commands_and_prompt_text():
    assert classify_reply("/结束等待").kind == "end"
    assert classify_reply(" /延长等待 ").kind == "extend"
    assert classify_reply("/状态").kind == "status"

    unknown = classify_reply("/重新开始")
    assert unknown.kind == "unknown_command"
    assert unknown.text == "/重新开始"

    prompt = classify_reply("请继续执行下一步")
    assert prompt.kind == "prompt"
    assert prompt.text == "请继续执行下一步"


def test_route_message_accepts_only_user_reply_to_target_card():
    decision = route_message(_message(), card_message_id="om_card")

    assert decision.kind == "card_reply"
    assert decision.message.message_id == "om_reply"


def test_route_message_ignores_bot_or_app_messages():
    assert route_message(
        _message(sender_type="bot"), card_message_id="om_card"
    ).kind == "ignored"
    assert route_message(
        _message(sender_type="app"), card_message_id="om_card"
    ).kind == "ignored"


def test_route_message_ignores_reply_to_another_card():
    decision = route_message(
        _message(reply_to="om_other_card"),
        card_message_id="om_card",
    )

    assert decision.kind == "ignored"
    assert decision.message is None


def test_route_message_classifies_ordinary_private_chat_separately():
    decision = route_message(
        _message(reply_to=None, text="这是一条普通私聊"),
        card_message_id="om_card",
    )

    assert decision.kind == "ordinary_dm"
    assert decision.message.content_text == "这是一条普通私聊"
