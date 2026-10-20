import sys
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

from digital_professor.max_compat.bot import (  # noqa: E402
    Bot,
    CommandHandler,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    MessageHandler,
    filters,
    markup_to_attachments,
    parse_update,
)
from digital_professor.max_compat.client import split_text  # noqa: E402


def _bot():
    return Bot("test-token")


def test_message_created_is_private_dialog():
    bot = _bot()
    update = parse_update(
        {
            "update_type": "message_created",
            "message": {
                "sender": {"user_id": 10, "first_name": "Анна", "username": "anna", "is_bot": False},
                "recipient": {"chat_id": 77, "chat_type": "dialog", "user_id": 10},
                "body": {"mid": "mid.1", "seq": 1, "text": "привет"},
            },
        },
        bot,
    )
    assert update.message.text == "привет"
    assert update.effective_user.id == 10
    assert update.effective_user.first_name == "Анна"
    assert update.effective_chat.id == 77
    assert update.effective_chat.type == "private"
    assert 77 in bot.dialog_chat_ids
    assert bot.profiles[10].username == "anna"


def test_bot_messages_are_dropped():
    update = parse_update(
        {
            "update_type": "message_created",
            "message": {
                "sender": {"user_id": 1, "first_name": "bot", "is_bot": True},
                "recipient": {"chat_id": 77, "chat_type": "dialog"},
                "body": {"mid": "mid.2", "seq": 2, "text": "ok"},
            },
        },
        _bot(),
    )
    assert update is None


def test_callback_keeps_keyboard_and_payload():
    bot = _bot()
    update = parse_update(
        {
            "update_type": "message_callback",
            "callback": {
                "callback_id": "cb1",
                "payload": "main_menu",
                "user": {"user_id": 10, "first_name": "Анна", "is_bot": False},
            },
            "message": {
                "sender": {"user_id": 1, "first_name": "bot", "is_bot": True},
                "recipient": {"chat_id": 77, "chat_type": "dialog"},
                "body": {
                    "mid": "mid.3",
                    "seq": 3,
                    "text": "меню",
                    "attachments": [
                        {
                            "type": "inline_keyboard",
                            "payload": {
                                "buttons": [[{"type": "callback", "text": "Меню", "payload": "main_menu"}]]
                            },
                        }
                    ],
                },
            },
        },
        bot,
    )
    query = update.callback_query
    assert query.data == "main_menu"
    assert query.from_user.id == 10
    assert query.message.reply_markup.inline_keyboard[0][0].callback_data == "main_menu"
    assert update.message is None


def test_bot_started_becomes_start_command():
    bot = _bot()
    update = parse_update(
        {
            "update_type": "bot_started",
            "chat_id": 77,
            "user": {"user_id": 10, "first_name": "Анна", "is_bot": False},
            "payload": "invite",
        },
        bot,
    )
    assert update.message.text == "/start invite"
    assert CommandHandler("start", None).check(update)
    assert update.message.text.split()[1:] == ["invite"]
    assert 77 in bot.dialog_chat_ids


def test_filters_skip_commands():
    bot = _bot()
    text = parse_update(
        {
            "update_type": "message_created",
            "message": {
                "sender": {"user_id": 10, "first_name": "А", "is_bot": False},
                "recipient": {"chat_id": 77, "chat_type": "dialog"},
                "body": {"mid": "m", "seq": 1, "text": "вопрос"},
            },
        },
        bot,
    )
    command = parse_update(
        {
            "update_type": "message_created",
            "message": {
                "sender": {"user_id": 10, "first_name": "А", "is_bot": False},
                "recipient": {"chat_id": 77, "chat_type": "dialog"},
                "body": {"mid": "m2", "seq": 2, "text": "/menu"},
            },
        },
        bot,
    )
    flt = filters.ALL & ~filters.COMMAND
    assert flt.check(text)
    assert not flt.check(command)
    assert MessageHandler(flt, None).check(text)
    assert CommandHandler("menu", None).check(command)


def test_keyboard_and_split():
    markup = InlineKeyboardMarkup(
        [[InlineKeyboardButton("Сайт", url="https://max.ru"), InlineKeyboardButton("Ок", callback_data="ok")]]
    )
    attachment = markup_to_attachments(markup)[0]
    row = attachment["payload"]["buttons"][0]
    assert row[0]["type"] == "link"
    assert row[1] == {"type": "callback", "text": "Ок", "payload": "ok"}
    parts = split_text("а" * 4001)
    assert len(parts) == 2
    assert sum(len(p) for p in parts) == 4001


def test_send_target_uses_user_id_until_chat_is_seen():
    bot = _bot()
    assert bot._target(10) == {"user_id": 10}
    bot.dialog_chat_ids.add(77)
    assert bot._target(77) == {"chat_id": 77}
