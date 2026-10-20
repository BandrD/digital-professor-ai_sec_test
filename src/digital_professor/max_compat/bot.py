"""Совместимый с python-telegram-bot слой поверх MAX.

Хендлеры профессора остаются прежними: Update, reply_text, callback, inline-кнопки.
Доставка идёт на platform-api2.max.ru. Топиков форума в MAX нет.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from digital_professor.max_compat.client import MaxApiError, MaxClient, clip_edit_text, split_text

logger = logging.getLogger(__name__)

_UNSET = object()
_CHAT_TYPE = {"dialog": "private", "chat": "group", "channel": "channel"}


class User:
    def __init__(self, user_id: int, first_name: str = "", username: str | None = None, is_bot: bool = False):
        self.id = int(user_id)
        self.first_name = first_name or ""
        self.username = username
        self.is_bot = bool(is_bot)


class Chat:
    def __init__(self, chat_id: int, type_: str = "private"):
        self.id = int(chat_id)
        self.type = type_


class ChatInfo:
    """Ответ get_chat: имя берётся из уже виденных апдейтов, не из GET /chats."""

    def __init__(self, user_id: int, first_name: str, username: str | None):
        self.id = int(user_id)
        self.first_name = first_name or "Студент"
        self.username = username
        self.type = "private"


class InlineKeyboardButton:
    def __init__(self, text: str, callback_data: str | None = None, url: str | None = None, **_kwargs):
        self.text = text
        self.callback_data = callback_data
        self.url = url


class InlineKeyboardMarkup:
    def __init__(self, inline_keyboard):
        self.inline_keyboard = inline_keyboard


class Message:
    def __init__(self, message_id, text, from_user, chat, reply_markup=None, bot=None):
        self.message_id = message_id
        self.text = text if text is not None else ""
        self.from_user = from_user
        self.chat = chat
        self.reply_markup = reply_markup
        self._bot = bot

    async def reply_text(self, text, reply_markup=None, parse_mode=None, **kwargs):
        return await self._bot.send_message(
            chat_id=self.chat.id,
            text=text,
            reply_markup=reply_markup,
            parse_mode=parse_mode,
            reply_to_message_id=kwargs.get("reply_to_message_id", self.message_id),
        )

    async def edit_text(self, text, reply_markup=_UNSET, parse_mode=None, **_kwargs):
        edited = await self._bot.edit_message_text(
            chat_id=self.chat.id,
            message_id=self.message_id,
            text=text,
            reply_markup=reply_markup,
            parse_mode=parse_mode,
        )
        self.text = text
        if reply_markup is not _UNSET:
            self.reply_markup = reply_markup
        return edited


class CallbackQuery:
    def __init__(self, callback_id, data, from_user, message, bot):
        self.id = callback_id
        self.data = data or ""
        self.from_user = from_user
        self.message = message
        self._bot = bot

    async def answer(self, text=None, show_alert=False, **_kwargs):
        # show_alert в MAX — тот же toast (notification), отдельного alert-окна нет.
        await self._bot.answer_callback(self.id, text, chat_key=getattr(self.from_user, "id", 0))

    async def edit_message_text(self, text, reply_markup=_UNSET, parse_mode=None, **kwargs):
        if self.message is not None:
            return await self.message.edit_text(text, reply_markup=reply_markup, parse_mode=parse_mode)
        return await self._bot.edit_message_text(
            message_id=kwargs.get("message_id"),
            text=text,
            reply_markup=reply_markup,
            parse_mode=parse_mode,
        )

    async def edit_message_reply_markup(self, reply_markup=None):
        if self.message is None:
            return None
        return await self._bot.edit_message_reply_markup(self.message, reply_markup)


class Update:
    def __init__(self, message=None, callback_query=None):
        self.message = message
        self.callback_query = callback_query

    @property
    def effective_user(self):
        if self.callback_query is not None:
            return self.callback_query.from_user
        if self.message is not None:
            return self.message.from_user
        return None

    @property
    def effective_chat(self):
        if self.callback_query is not None and self.callback_query.message is not None:
            return self.callback_query.message.chat
        if self.message is not None:
            return self.message.chat
        return None

    @property
    def effective_message(self):
        if self.callback_query is not None and self.callback_query.message is not None:
            return self.callback_query.message
        return self.message


def _parse_user(raw: dict | None) -> User | None:
    if not raw:
        return None
    return User(
        raw.get("user_id") or 0,
        first_name=raw.get("first_name") or raw.get("name") or "",
        username=raw.get("username"),
        is_bot=bool(raw.get("is_bot")),
    )


def _parse_keyboard(attachments) -> InlineKeyboardMarkup | None:
    if not attachments:
        return None
    for item in attachments:
        if not isinstance(item, dict) or item.get("type") != "inline_keyboard":
            continue
        rows = []
        for row in (item.get("payload") or {}).get("buttons") or []:
            buttons = []
            for btn in row:
                if not isinstance(btn, dict):
                    continue
                kind = btn.get("type")
                if kind == "link":
                    buttons.append(InlineKeyboardButton(btn.get("text") or "", url=btn.get("url")))
                else:
                    buttons.append(
                        InlineKeyboardButton(btn.get("text") or "", callback_data=btn.get("payload"))
                    )
            if buttons:
                rows.append(buttons)
        if rows:
            return InlineKeyboardMarkup(rows)
    return None


def _parse_message(raw: dict | None, bot: "Bot") -> Message | None:
    if not raw or not isinstance(raw, dict):
        return None
    body = raw.get("body") or {}
    recipient = raw.get("recipient") or {}
    chat_type = _CHAT_TYPE.get(recipient.get("chat_type") or "dialog", "private")
    chat_id = recipient.get("chat_id") or recipient.get("user_id") or 0
    if recipient.get("chat_id"):
        bot.dialog_chat_ids.add(int(recipient["chat_id"]))
    sender = _parse_user(raw.get("sender"))
    if sender is not None and not sender.is_bot:
        bot.profiles[sender.id] = sender
    return Message(
        message_id=body.get("mid"),
        text=body.get("text") or "",
        from_user=sender,
        chat=Chat(chat_id, chat_type),
        reply_markup=_parse_keyboard(body.get("attachments")),
        bot=bot,
    )


def parse_update(raw: dict, bot: "Bot") -> Update | None:
    kind = raw.get("update_type")
    if kind == "message_created":
        message = _parse_message(raw.get("message"), bot)
        if message is None or (message.from_user and message.from_user.is_bot):
            return None
        return Update(message=message)
    if kind == "message_callback":
        callback = raw.get("callback") or {}
        user = _parse_user(callback.get("user"))
        if user is not None:
            bot.profiles[user.id] = user
        message = _parse_message(raw.get("message") or callback.get("message"), bot)
        if message is not None and message.chat.id:
            bot.dialog_chat_ids.add(int(message.chat.id))
        return Update(
            callback_query=CallbackQuery(
                callback.get("callback_id"),
                callback.get("payload") or "",
                user,
                message,
                bot,
            )
        )
    if kind == "bot_started":
        user = _parse_user(raw.get("user"))
        chat_id = raw.get("chat_id") or (user.id if user else 0)
        if user is not None:
            bot.profiles[user.id] = user
        if chat_id:
            bot.dialog_chat_ids.add(int(chat_id))
        payload = (raw.get("payload") or "").strip()
        text = f"/start {payload}" if payload else "/start"
        return Update(
            message=Message(
                message_id=None,
                text=text,
                from_user=user,
                chat=Chat(chat_id, "private"),
                bot=bot,
            )
        )
    return None


def markup_to_attachments(markup) -> list | None:
    if markup is None:
        return []
    if not isinstance(markup, InlineKeyboardMarkup):
        return None
    rows = []
    for row in markup.inline_keyboard:
        buttons = []
        for btn in row:
            text = (getattr(btn, "text", None) or "")[:128] or " "
            url = getattr(btn, "url", None)
            if url:
                buttons.append({"type": "link", "text": text, "url": str(url)[:2048]})
            else:
                payload = getattr(btn, "callback_data", None) or ""
                buttons.append({"type": "callback", "text": text, "payload": str(payload)[:1024]})
        if buttons:
            rows.append(buttons[:7])
    if not rows:
        return []
    return [{"type": "inline_keyboard", "payload": {"buttons": rows[:30]}}]


def _format_name(parse_mode) -> str | None:
    if not parse_mode:
        return None
    name = str(parse_mode).lower()
    if name in ("html", "markdown"):
        return name
    return None


def _message_body(text: str, parse_mode, attachments, reply_to_message_id=None) -> dict:
    body: dict[str, Any] = {"text": text}
    fmt = _format_name(parse_mode)
    if fmt:
        body["format"] = fmt
    if attachments is not None:
        body["attachments"] = attachments
    if reply_to_message_id:
        body["link"] = {"type": "reply", "mid": str(reply_to_message_id)}
    return body


class Bot:
    def __init__(self, token: str | None):
        self.client = MaxClient(token)
        self.dialog_chat_ids: set[int] = set()
        self.profiles: dict[int, User] = {}
        self._answer_at: dict[int, float] = {}
        self._marker = None
        self._stop = False

    def _target(self, chat_id) -> dict:
        cid = int(chat_id)
        if cid in self.dialog_chat_ids:
            return {"chat_id": cid}
        return {"user_id": cid}

    async def send_message(
        self,
        chat_id,
        text,
        reply_markup=None,
        parse_mode=None,
        reply_to_message_id=None,
        **_kwargs,
    ):
        parts = split_text(text)
        attachments = markup_to_attachments(reply_markup) if reply_markup is not None else None
        sent = None
        for index, part in enumerate(parts):
            body = _message_body(
                part,
                parse_mode,
                attachments if index == len(parts) - 1 else None,
                reply_to_message_id if index == 0 else None,
            )
            data = await self.client.request(
                "POST",
                "/messages",
                params=self._target(chat_id),
                json_body=body,
            )
            sent = _parse_message((data or {}).get("message"), self)
        if sent is None:
            sent = Message(None, text, None, Chat(int(chat_id), "private"), reply_markup, self)
        return sent

    async def edit_message_text(
        self,
        chat_id=None,
        message_id=None,
        text="",
        reply_markup=_UNSET,
        parse_mode=None,
        **_kwargs,
    ):
        parts = split_text(text)
        if reply_markup is _UNSET:
            attachments = None
        else:
            attachments = markup_to_attachments(reply_markup)
        body = _message_body(parts[0], parse_mode, attachments)
        if attachments is None:
            body["attachments"] = None
        await self.client.request("PUT", "/messages", params={"message_id": message_id}, json_body=body)
        if chat_id and len(parts) > 1:
            for part in parts[1:]:
                await self.send_message(chat_id, part, parse_mode=parse_mode)
        return Message(message_id, text, None, Chat(int(chat_id or 0), "private"), None, self)

    async def edit_message_reply_markup(self, message: Message, reply_markup):
        attachments = markup_to_attachments(reply_markup)
        body = _message_body(clip_edit_text(message.text), None, attachments)
        await self.client.request(
            "PUT",
            "/messages",
            params={"message_id": message.message_id},
            json_body=body,
        )
        message.reply_markup = reply_markup
        return message

    async def delete_message(self, chat_id=None, message_id=None, **_kwargs):
        await self.client.request("DELETE", "/messages", params={"message_id": message_id})

    async def answer_callback(self, callback_id, text, chat_key=0):
        if not callback_id:
            return
        last = self._answer_at.get(chat_key, 0.0)
        wait = 0.5 - (time.monotonic() - last)
        if wait > 0:
            await asyncio.sleep(wait)
        body = {}
        if text:
            body["notification"] = str(text)[:200]
        await self.client.request("POST", "/answers", params={"callback_id": callback_id}, json_body=body)
        self._answer_at[chat_key] = time.monotonic()

    async def get_chat(self, user_id):
        user = self.profiles.get(int(user_id))
        if user is None:
            return ChatInfo(int(user_id), "Студент", None)
        return ChatInfo(user.id, user.first_name or "Студент", user.username)

    async def drop_pending(self):
        while not self._stop:
            data = await self.client.request(
                "GET",
                "/updates",
                params={
                    "limit": 100,
                    "timeout": 0,
                    "marker": self._marker,
                    "types": "message_created,message_callback,bot_started",
                },
            )
            if isinstance(data, dict) and data.get("marker") is not None:
                self._marker = data.get("marker")
            updates = (data or {}).get("updates") or []
            if not updates:
                return

    async def poll_forever(self, application: "Application"):
        while not self._stop:
            started = time.monotonic()
            try:
                data = await self.client.request(
                    "GET",
                    "/updates",
                    params={
                        "limit": 100,
                        "timeout": 30,
                        "marker": self._marker,
                        "types": "message_created,message_callback,bot_started",
                    },
                )
                if isinstance(data, dict) and data.get("marker") is not None:
                    self._marker = data.get("marker")
                for raw in (data or {}).get("updates") or []:
                    update = parse_update(raw, self)
                    if update is not None:
                        await application.process(update)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("MAX long poll failed")
                await asyncio.sleep(1)
            delay = 0.3 - (time.monotonic() - started)
            if delay > 0:
                await asyncio.sleep(delay)

    async def close(self):
        self._stop = True
        await self.client.close()


class _Filter:
    def check(self, update: Update) -> bool:
        return True

    def __and__(self, other):
        return _And(self, other)

    def __invert__(self):
        return _Not(self)


class _And(_Filter):
    def __init__(self, left, right):
        self.left = left
        self.right = right

    def check(self, update: Update) -> bool:
        return self.left.check(update) and self.right.check(update)


class _Not(_Filter):
    def __init__(self, inner):
        self.inner = inner

    def check(self, update: Update) -> bool:
        return not self.inner.check(update)


class _All(_Filter):
    def check(self, update: Update) -> bool:
        return update.message is not None and update.callback_query is None


class _Command(_Filter):
    def check(self, update: Update) -> bool:
        text = update.message.text if update.message is not None else ""
        return bool(text) and text.startswith("/")


class _Filters:
    ALL = _All()
    COMMAND = _Command()


filters = _Filters()


class CommandHandler:
    def __init__(self, command: str, callback):
        self.command = command.lstrip("/").lower()
        self.callback = callback

    def check(self, update: Update) -> bool:
        if update.message is None or not update.message.text:
            return False
        token = update.message.text.strip().split()[0].split("@", 1)[0]
        if token.startswith("/"):
            token = token[1:]
        return token.lower() == self.command


class MessageHandler:
    def __init__(self, message_filter, callback):
        self.message_filter = message_filter
        self.callback = callback

    def check(self, update: Update) -> bool:
        return self.message_filter.check(update)


class CallbackQueryHandler:
    def __init__(self, callback, pattern=None):
        self.callback = callback
        self.pattern = pattern

    def check(self, update: Update) -> bool:
        return update.callback_query is not None


class CallbackContext:
    def __init__(self, application: "Application", user_data: dict, args: list):
        self.application = application
        self.bot = application.bot
        self.user_data = user_data
        self.args = args


class ContextTypes:
    DEFAULT_TYPE = CallbackContext


class Application:
    def __init__(self, token: str | None):
        self.bot = Bot(token)
        self.updater = Updater(self)
        self._handlers = []
        self._user_data: dict[int, dict] = {}

    @staticmethod
    def builder():
        return ApplicationBuilder()

    def add_handler(self, handler):
        self._handlers.append(handler)

    def create_task(self, coro):
        return asyncio.get_running_loop().create_task(coro)

    async def initialize(self):
        return None

    async def start(self):
        return None

    async def stop(self):
        return None

    async def shutdown(self):
        await self.bot.close()

    async def process(self, update: Update):
        user = update.effective_user
        uid = user.id if user is not None else 0
        args: list[str] = []
        if update.message is not None and update.message.text.startswith("/"):
            args = update.message.text.split()[1:]
        ctx = CallbackContext(self, self._user_data.setdefault(uid, {}), args)
        for handler in self._handlers:
            if not handler.check(update):
                continue
            try:
                await handler.callback(update, ctx)
            except Exception:
                logger.exception("MAX handler failed")
            return


class ApplicationBuilder:
    def __init__(self):
        self._token = None

    def token(self, token: str):
        self._token = token
        return self

    def build(self) -> Application:
        return Application(self._token)


class Updater:
    def __init__(self, application: Application):
        self.application = application
        self._task: asyncio.Task | None = None

    async def start_polling(self, drop_pending_updates: bool = False, **_kwargs):
        self._task = asyncio.create_task(self._run(drop_pending_updates))

    async def _run(self, drop_pending_updates: bool):
        bot = self.application.bot
        if drop_pending_updates:
            try:
                await bot.drop_pending()
            except MaxApiError:
                logger.exception("Failed to drop pending MAX updates")
        await bot.poll_forever(self.application)

    async def stop(self):
        self.application.bot._stop = True
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
