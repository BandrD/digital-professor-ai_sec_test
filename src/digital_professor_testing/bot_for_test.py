"""
bot_for_test.py

Изолированная тестовая версия бота (для нагрузочного тестирования/спам-аттак/промпт-инъекций).

ВАЖНО:
- Этот файл НЕ меняет продовую логику (`bot.py`), его можно просто удалить после тестов.
- Оставлены только:
  - регистрация (упрощённая: ФИО → студенческий → таймзона → время пинга)
  - одно меню с кнопкой "❓ Задать вопрос по курсу"
  - RAG-доступ сразу к двум коллекциям: GitVerse + Math (без проверки студенческого/курса)
  - входной антиспам (частота апдейтов Telegram) + лимит на RAG
"""

from __future__ import annotations

import asyncio
import glob
import html
import logging
import os
import re
from datetime import datetime
from typing import Optional

from digital_professor.max_compat import CallbackContext, InlineKeyboardButton, InlineKeyboardMarkup, Update

from digital_professor.advanced_rate_limiter import AdvancedRateLimiter
from digital_professor.database import create_user_stub, get_user, is_user_registered, save_user
from digital_professor.input_validator import validate_user_input, get_injection_risk_level
from digital_professor.llm import llm_first_turn_answer
from digital_professor.llm_censor import check_user_query, get_censor_error_message
from digital_professor.rag.pipeline import RagPipeline
from digital_professor_testing.test_data_for_test import log_test_interaction

logger = logging.getLogger(__name__)

# ==================== Приватные чаты only ====================
def check_private_chat(update: Update) -> bool:
    if update.callback_query and update.callback_query.message:
        chat = update.callback_query.message.chat
        return chat.type == "private" if chat else False
    if update.effective_chat:
        return update.effective_chat.type == "private"
    return False


async def reject_non_private_chat(update: Update, context: CallbackContext):
    try:
        if update.message:
            await update.message.reply_text(
                "❌ Извините, этот бот работает только в личных сообщениях.\n"
                "Пожалуйста, напишите мне в личку для продолжения."
            )
        elif update.callback_query:
            await update.callback_query.answer("Бот работает только в личных сообщениях", show_alert=True)
    except Exception as e:
        logger.debug(f"Failed to reject non-private chat: {e}")


# ==================== Регистрация (как в проде по UX, но короче) ====================
FULL_NAME_PART_PATTERN = re.compile(r"^[A-Za-zА-Яа-яЁё][A-Za-zА-Яа-яЁё\-']{1,50}$")
MAX_FIO_PARTS = 3
MIN_FIO_PARTS = 2


def is_valid_full_name(full_name: str | None) -> bool:
    if not full_name:
        return False
    parts = [p for p in re.split(r"\s+", full_name.strip()) if p]
    if not (MIN_FIO_PARTS <= len(parts) <= MAX_FIO_PARTS):
        return False
    return all(FULL_NAME_PART_PATTERN.match(part) for part in parts)


def extract_first_name(full_name: str | None) -> str:
    if not full_name:
        return "студент"
    parts = [p for p in re.split(r"\s+", full_name.strip()) if p]
    if len(parts) >= 2:
        return parts[1]
    return parts[0]


def main_menu_keyboard_for_test() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("❓ Задать вопрос по курсу", callback_data="ask_course")]])


WELCOME_TEXT = (
    "👋 Привет!\n\n"
    "Это <b>тестовый бот</b> для нагрузочного тестирования и проверки AI Security.\n"
    "Сначала я спрошу, как к вам обращаться, затем можно задавать вопросы по курсу.\n\n"
    "<blockquote>Коллекции RAG: GitVerse + Математика доступны всем сразу.</blockquote>"
)


async def start(update: Update, context: CallbackContext):
    if not check_private_chat(update):
        await reject_non_private_chat(update, context)
        return

    user_id = update.effective_user.id
    user_data = context.user_data

    # сброс любых “хвостов” сессии
    user_data.clear()

    if is_user_registered(user_id):
        u = get_user(user_id)
        name = extract_first_name((u or {}).get("username"))
        await update.message.reply_text(WELCOME_TEXT, parse_mode="HTML")
        await update.message.reply_text(
            f"<b>С возвращением, {html.escape(name)}!</b>\n\nВыберите действие:",
            reply_markup=main_menu_keyboard_for_test(),
            parse_mode="HTML",
        )
        return

    create_user_stub(user_id, None)
    await update.message.reply_text(WELCOME_TEXT, parse_mode="HTML")
    await update.message.reply_text(
        "Готов начать? Нажми кнопку ниже.",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Начать", callback_data="start_registration")]]),
    )
    user_data["step"] = "awaiting_registration_start"


# ==================== RAG init: GitVerse + Math ====================
rag_pipeline_math: RagPipeline | None = None
rag_pipeline_gitverse: RagPipeline | None = None
rag_init_lock = asyncio.Lock()


def _find_json_files() -> tuple[list[str], list[str]]:
    prod_rag_base = os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        "..",
        "digital_professor",
        "rag",
    )
    prod_rag_base = os.path.abspath(prod_rag_base)
    gitverse_dir = os.path.join(prod_rag_base, "data_vibecoding")
    math_dir = os.path.join(prod_rag_base, "data_math")
    gitverse_jsons = sorted(glob.glob(os.path.join(gitverse_dir, "*.json")))
    math_jsons = sorted(glob.glob(os.path.join(math_dir, "*.json")))
    return gitverse_jsons, math_jsons


async def initialize_rag_for_test_if_needed():
    global rag_pipeline_math, rag_pipeline_gitverse
    async with rag_init_lock:
        if (
            rag_pipeline_math is not None
            and getattr(rag_pipeline_math, "is_initialized", False)
            and rag_pipeline_gitverse is not None
            and getattr(rag_pipeline_gitverse, "is_initialized", False)
        ):
            return True

        gitverse_jsons, math_jsons = _find_json_files()
        if not gitverse_jsons:
            logger.warning("No JSON files found for GitVerse test pipeline")
        if not math_jsons:
            logger.warning("No JSON files found for Math test pipeline")

        if rag_pipeline_gitverse is None or not getattr(rag_pipeline_gitverse, "is_initialized", False):
            rag_pipeline_gitverse = RagPipeline(
                persist_directory="./data/chroma_db_gitverse",
                collection_name="Gitverse",
            )
            await rag_pipeline_gitverse.initialize(json_files=gitverse_jsons, force_recreate=False)
            logger.info("RAG Pipeline (gitverse) initialized for test bot")

        if rag_pipeline_math is None or not getattr(rag_pipeline_math, "is_initialized", False):
            rag_pipeline_math = RagPipeline(
                persist_directory="./data/chroma_db_math",
                collection_name="Math",
            )
            await rag_pipeline_math.initialize(json_files=math_jsons, force_recreate=False)
            logger.info("RAG Pipeline (math) initialized for test bot")

        return True


def _merge_docs(docs_a: list, docs_b: list, k_final: int = 6) -> list:
    # простая дедупликация по page_content + добор по порядку
    seen: set[str] = set()
    merged = []
    for doc in (docs_a or []) + (docs_b or []):
        key = getattr(doc, "page_content", "") or ""
        if not key:
            continue
        if key in seen:
            continue
        seen.add(key)
        merged.append(doc)
        if len(merged) >= k_final:
            break
    return merged


# ==================== Лимитеры (важно для нагрузочного теста) ====================
telegram_incoming_limiter = AdvancedRateLimiter(
    capacity=6,
    refill_rate=0.5,
    burst_window=5,
    max_burst=4,
    penalty_multiplier=2.0,
)

telegram_callback_limiter = AdvancedRateLimiter(
    capacity=10,
    refill_rate=1.0,
    burst_window=3,
    max_burst=8,
    penalty_multiplier=2.0,
)

rag_rate_limiter = AdvancedRateLimiter(
    capacity=4,
    refill_rate=0.2,   # ~12/мин
    burst_window=8,
    max_burst=2,
    penalty_multiplier=2.0,
)

_last_rate_notice_at: dict[int, datetime] = {}
_RATE_NOTICE_COOLDOWN_SEC = 10


def _should_send_rate_notice(user_id: int) -> bool:
    now = datetime.now()
    last = _last_rate_notice_at.get(user_id)
    if last and (now - last).total_seconds() < _RATE_NOTICE_COOLDOWN_SEC:
        return False
    _last_rate_notice_at[user_id] = now
    return True


async def handle_message(update: Update, context: CallbackContext):
    if not check_private_chat(update):
        await reject_non_private_chat(update, context)
        return

    msg = update.message
    if not msg:
        return

    user_id = msg.from_user.id
    user_data = context.user_data

    # входной антиспам (как в проде: режем частоту апдейтов Telegram ДО любой бизнес-логики)
    incoming_text = (msg.text or msg.caption or "")
    allowed, rate_error = telegram_incoming_limiter.is_allowed(user_id, incoming_text, "telegram_incoming")
    if not allowed:
        if _should_send_rate_notice(user_id):
            try:
                await msg.reply_text(rate_error)
            except Exception:
                pass
        return

    # --- registration steps (упрощено для теста) ---
    # Единственный шаг: "Как вас зовут?" — принимаем любое непустое имя/ник/ФИО.
    if user_data.get("step") == "name":
        if not (msg.text or "").strip():
            await msg.reply_text("Пожалуйста, напишите, как к вам обращаться (имя/ник/ФИО) — текстом.")
            return
        name_raw = (msg.text or "").strip()
        if len(name_raw) > 80:
            await msg.reply_text("Слишком длинно. Напишите имя/ник короче (до 80 символов).")
            return

        # Заполняем обязательные для продовой схемы поля синтетически (чтобы не ломать is_user_registered).
        # Эти значения НЕ используются в тесте.
        synthetic_student_id = "000000"
        synthetic_timezone = "Europe/Moscow"
        synthetic_ping_time = "10:00"

        save_user(
            user_id=user_id,
            username=name_raw,
            timezone=synthetic_timezone,
            ping_time=synthetic_ping_time,
            student_id=synthetic_student_id,
            ab_group=None,
        )
        logger.info(f"[test bot] User {user_id} registered with synthetic fields.")

        user_data.clear()
        await msg.reply_text(
            f"<b>Отлично, {html.escape(extract_first_name(name_raw))}!</b>\n\n"
            "Теперь можно задавать вопросы по курсу.",
            reply_markup=main_menu_keyboard_for_test(),
            parse_mode="HTML",
        )
        return

    # --- RAG query flow ---
    if user_data.get("step") == "rag_query":
        if not (msg.text or "").strip():
            await msg.reply_text(
                "Пожалуйста, отправьте вопрос текстом. "
                "Файлы/фото/голосовые сообщения в тестовом режиме не принимаются."
            )
            return
        question = (msg.text or "").strip()
        if not question:
            await msg.reply_text("❓ Пожалуйста, введите текст вопроса.")
            return

        # имя пользователя (для лога test_data)
        u = None
        try:
            u = get_user(user_id)
        except Exception:
            u = None
        user_name = (u or {}).get("username") or None

        # ==================== AI Security parity с продом (3 уровня) ====================
        # 🛡️ УРОВЕНЬ 0: язык входа (до regex-защиты)
        from digital_professor.language_detector import check_user_language

        lang_ok, lang_err, _lang_log = await check_user_language(question, user_id)
        if not lang_ok:
            logger.warning(f"[test bot] Language gate blocked query from user {user_id}: {lang_err}")
            log_test_interaction(
                user_id=user_id,
                user_name=user_name,
                user_query=question,
                status="blocked",
                reason="language_detector",
                llm_response=None,
                meta={"language_error": lang_err},
            )
            await msg.reply_text(lang_err or "Неподдерживаемый язык ввода.")
            return

        # 🛡️ УРОВЕНЬ 1: БАЗОВАЯ ВАЛИДАЦИЯ (регулярки/паттерны/экстремальный спам)
        is_valid, validation_error = validate_user_input(question)
        if not is_valid:
            logger.warning(f"[test bot] Invalid RAG query from user {user_id}: {validation_error}")
            log_test_interaction(
                user_id=user_id,
                user_name=user_name,
                user_query=question,
                status="blocked",
                reason="input_validator",
                llm_response=None,
                meta={"validation_error": validation_error},
            )
            await msg.reply_text(validation_error or "Некорректный ввод.")
            return

        # 🛡️ УРОВЕНЬ 2: RATE LIMITING (token bucket + burst + дубликаты)
        allowed, rate_error = rag_rate_limiter.is_allowed(user_id, question, "rag")
        if not allowed:
            logger.warning(f"[test bot] Rate limit exceeded for user {user_id} (RAG): {rate_error}")
            log_test_interaction(
                user_id=user_id,
                user_name=user_name,
                user_query=question,
                status="blocked",
                reason="rate_limiter",
                llm_response=None,
                meta={"rate_error": rate_error},
            )
            await msg.reply_text(rate_error)
            return

        # 🛡️ УРОВЕНЬ 3: LLM-ЦЕНЗОР (смысловая проверка/инъекции/мусор)
        is_allowed_by_censor, censor_reason = await check_user_query(question, user_id)
        if not is_allowed_by_censor:
            logger.warning(f"[test bot] LLM Censor blocked query from user {user_id}: {censor_reason}")
            log_test_interaction(
                user_id=user_id,
                user_name=user_name,
                user_query=question,
                status="blocked",
                reason="llm_censor",
                llm_response=None,
                meta={"censor_reason": censor_reason},
            )
            await msg.reply_text(await get_censor_error_message(censor_reason))
            return

        # Доп. логирование риск-уровня (как в проде)
        risk_level = get_injection_risk_level(question)
        if risk_level in ("medium", "high"):
            logger.warning(f"[test bot] Suspicious RAG query from user {user_id} (risk={risk_level}): {question[:100]}")

        # гарантируем инициализацию RAG
        await initialize_rag_for_test_if_needed()
        if not rag_pipeline_gitverse or not rag_pipeline_math:
            log_test_interaction(
                user_id=user_id,
                user_name=user_name,
                user_query=question,
                status="error",
                reason="rag_not_ready",
                llm_response=None,
                meta=None,
            )
            await msg.reply_text("❌ RAG ещё не готов. Попробуйте чуть позже.")
            return

        processing = await msg.reply_text("🔍 Ищу информацию по вашему вопросу...")
        try:
            docs_git = rag_pipeline_gitverse.query(question)
            docs_math = rag_pipeline_math.query(question)
            docs = _merge_docs(docs_git, docs_math, k_final=6)
            if not docs:
                log_test_interaction(
                    user_id=user_id,
                    user_name=user_name,
                    user_query=question,
                    status="ok",
                    reason="no_docs",
                    llm_response="",
                    meta={"docs_git": len(docs_git or []), "docs_math": len(docs_math or [])},
                )
                await processing.edit_text("😔 Не нашёл релевантной информации в материалах.")
                return

            answer = await llm_first_turn_answer(question, docs, user_id=user_id)
            is_safe_output, censor_reason = await check_user_query(question, user_id, model_response=answer)
            if not is_safe_output:
                logger.warning(f"[test bot] LLM Censor blocked model output for user {user_id}: {censor_reason}")
                safe_error = await get_censor_error_message(censor_reason)
                log_test_interaction(
                    user_id=user_id,
                    user_name=user_name,
                    user_query=question,
                    status="blocked",
                    reason="llm_censor_output",
                    llm_response=answer,
                    meta={"censor_reason": censor_reason},
                )
                await processing.edit_text(safe_error)
                return
            log_test_interaction(
                user_id=user_id,
                user_name=user_name,
                user_query=question,
                status="ok",
                reason=None,
                llm_response=answer,
                meta={"docs_git": len(docs_git or []), "docs_math": len(docs_math or []), "risk": risk_level},
            )
            await processing.edit_text(html.escape(answer), parse_mode="HTML")
        except Exception as e:
            logger.error(f"[test bot] RAG error for user {user_id}: {e}", exc_info=True)
            log_test_interaction(
                user_id=user_id,
                user_name=user_name,
                user_query=question,
                status="error",
                reason="exception",
                llm_response=None,
                meta={"exception": str(e)[:500]},
            )
            try:
                await processing.edit_text("❌ Ошибка при обработке запроса. Попробуйте позже.")
            except Exception:
                pass
        return

    # default: если пользователь зарегистрирован — показываем меню, иначе просим /start
    if is_user_registered(user_id):
        await msg.reply_text("Выберите действие:", reply_markup=main_menu_keyboard_for_test())
    else:
        await msg.reply_text("Запусти /start, чтобы пройти регистрацию.")


async def button(update: Update, context: CallbackContext):
    if not check_private_chat(update):
        await reject_non_private_chat(update, context)
        return

    query = update.callback_query
    if not query:
        return

    user_id = query.from_user.id
    data = query.data or ""

    allowed, rate_error = telegram_callback_limiter.is_allowed(user_id, data, "telegram_callback")
    if not allowed:
        await query.answer(rate_error, show_alert=True)
        return

    await query.answer()

    # старт регистрации
    if data == "start_registration":
        if context.user_data.get("step") != "awaiting_registration_start":
            await query.answer("Регистрация уже началась.", show_alert=True)
            return
        context.user_data["step"] = "name"
        await query.edit_message_text(
            "<b>Давай узнаем друг друга получше.</b>\n\nКак вас зовут?",
            parse_mode="HTML",
        )
        return

    # меню: один пункт
    if data == "ask_course":
        context.user_data["step"] = "rag_query"
        await query.edit_message_text(
            "❓ Введите вопрос по курсу (доступны материалы GitVerse + Математика):"
        )
        return

    # неизвестное
    await query.answer("Неизвестная команда", show_alert=False)
