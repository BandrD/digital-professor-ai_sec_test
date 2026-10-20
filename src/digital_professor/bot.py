# bot.py
from digital_professor.max_compat import (
    CallbackContext,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from datetime import datetime, timezone
import logging
import random
import html
import re
import os
from typing import List, Set
from digital_professor.database import is_admin, assign_course, get_topic, get_topics_for_course,  get_tasks_for_topic, get_user_courses_with_progress
from digital_professor.registry import COURSE_CREATORS, discover_creators_from_module
import asyncio
import inspect
import httpx
from digital_professor.max_compat import ContextTypes
from digital_professor.database import (
    save_user, get_user, update_user_timezone, update_user_ping_time,
    create_course, add_task, add_test_question, add_test_choice, remove_task,
    get_course, list_all_courses, assign_course, get_assigned_courses,
    get_tasks_for_course, get_test_questions_with_choices, get_task, save_task_result,
    get_users, get_choice, update_weekly_progress, get_kpi_for_user_course,
    log_rag_interaction, update_rag_interaction_feedback, get_rag_interaction,
    create_user_stub
)
from digital_professor.utils import (
    main_menu_keyboard, keyboard_with_main_menu,
    courses_keyboard, tasks_keyboard, question_choices_keyboard, is_admin, topics_keyboard
)
from digital_professor.utils import main_menu_keyboard_for
from digital_professor.scheduler import _weekly_big5_job
from digital_professor.scheduler import _weekly_big5_job, reschedule_all_user_pings
from digital_professor.llm import evaluate_with_llm, call_llm_json, sanitize_user_input, check_relevance, llm_continue_with_hints, llm_generate_hints
from digital_professor.course_creator import generate_telegraph_links_for_course
from digital_professor.database import save_user_response, link_last_response_to_task, is_big5_initialized, mark_big5_initialized
from digital_professor.database import upsert_big5_profile, recalc_big5_for_user_week, insert_big5_history, get_big5_profile, is_user_registered, is_valid_student_id, update_user_student_id, update_user_moodle_course
from digital_professor.database import get_user_ping_time
import json
from digital_professor.rag.pipeline import RagPipeline
from digital_professor.moodle_client import MoodleClient
from digital_professor.database import is_user_admin, set_user_ping_schedule

URFU_STUDENT_URL = "https://istudent.urfu.ru/student/index"

# Приветственное сообщение для новых пользователей
WELCOME_FIRST_TIME_TEXT = (
    "👋 Привет!\n\n"
    "Я — <b>Digital Professor</b>, твой персональный цифровой преподаватель.\n\n"
    "Вместе мы будем разбирать материалы курса, готовиться к контрольным и не забывать о дедлайнах 📚\n\n"
    "Моя задача — помочь тебе учиться эффективнее:\n\n"
    "    • подбирать объяснения под твой стиль восприятия,\n"
    "    • напоминать о дедлайнах в удобное время,\n"
    "    • отвечать на вопросы по темам курса,\n"
    "    • давать персональные рекомендации,\n"
    "    • генерировать задачи для подготовки.\n\n"
    "Перед началом давай познакомимся - мне нужно узнать несколько вещей, чтобы настроить курс под тебя."
)

# Приветственное сообщение для зарегистрированных пользователей
WELCOME_INFO_TEXT = (
    "Я — <b>Digital Professor</b>, твой персональный цифровой преподаватель.\n\n"
    "Вместе мы будем разбирать материалы курса, готовиться к контрольным и не забывать о дедлайнах 📚"
)

logger = logging.getLogger(__name__)
# --- ИМПОРТ И СОЗДАНИЕ ЭКЗЕМПЛЯРА RAG ---
# 1. Импортируем класс RagPipeline
try:
    from digital_professor.rag.pipeline import RagPipeline
    RAG_IMPORT_SUCCESS = True
except ImportError as e:
    logging.error(f"Failed to import RagPipeline: {e}")
    RAG_IMPORT_SUCCESS = False
    RagPipeline = None # Определяем переменную, чтобы избежать NameError позже

# 2. Создаем глобальные переменные для хранения экземпляров RAG по доменам
# Отдельные экземпляры для каждого домена (math и vibe)
rag_pipeline_math: RagPipeline | None = None
rag_pipeline_vibe: RagPipeline | None = None
rag_init_lock = asyncio.Lock() # Для безопасной инициализации

# 3. Упрощённый Rate Limiter (основная проверка - в LLM-цензоре)
from digital_professor.advanced_rate_limiter import AdvancedRateLimiter

# RAG запросы - ОСЛАБЛЕННЫЕ ЛИМИТЫ (смысловая проверка в LLM-цензоре)
rag_rate_limiter = AdvancedRateLimiter(
    capacity=5,              # ОСЛАБЛЕНО: 5 быстрых запросов (было 3)
    refill_rate=0.25,        # ОСЛАБЛЕНО: 0.25 токена/сек = ~15 запросов/минуту (было 0.12)
    burst_window=8,          # ОСЛАБЛЕНО: 8 секунд (было 5)
    max_burst=3,             # ОСЛАБЛЕНО: макс 3 запроса за 8 секунд (было 2 за 5)
    penalty_multiplier=2.0
)

# Продолжение диалога - ОСЛАБЛЕННЫЕ ЛИМИТЫ
rag_continue_limiter = AdvancedRateLimiter(
    capacity=5,              # ОСЛАБЛЕНО: 5 токенов (было 3)
    refill_rate=0.2,         # ОСЛАБЛЕНО: 0.2 токена/сек = ~12 запросов/минуту (было 0.1)
    burst_window=8,          # ОСЛАБЛЕНО: 8 секунд (было 5)
    max_burst=3,             # ОСЛАБЛЕНО: 3 сообщения за 8 секунд (было 2 за 5)
    penalty_multiplier=2.0
)

logger.info("Relaxed rate limiters initialized (RAG + Continue)")

# ==================== Входной антиспам (до любых LLM/RAG) ====================
# Цель: отсеивать слишком частые апдейты Telegram ещё на входе, чтобы спам не запускал llm_censor/эмбеддинги/раг.
#
# Дефолтные лимиты:
# - Разрешаем короткие "человеческие" всплески, но режем явный флуд.
# - Не используем whitelist (даже для админов) — это защита стоимости/ресурсов.
telegram_incoming_limiter = AdvancedRateLimiter(
    capacity=6,           # небольшой burst (например, несколько коротких уточнений подряд)
    refill_rate=0.5,      # ~30 сообщений/мин в среднем
    burst_window=5,       # быстрые всплески в окне 5 секунд
    max_burst=4,          # 5-е сообщение за 5 секунд уже считается флудом
    penalty_multiplier=2.0
)

telegram_callback_limiter = AdvancedRateLimiter(
    capacity=10,          # клики по кнопкам обычно легче, чем сообщения
    refill_rate=1.0,      # ~60/мин
    burst_window=3,
    max_burst=8,
    penalty_multiplier=2.0
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


async def load_moodle_courses_for_menu(user_id: int, existing_ids: Set[int]) -> List[dict]:
    """
    Возвращает список курсов из поля users.moodle_course без дополнительных запросов к Moodle.
    """
    user = get_user(user_id)
    if not user:
        return []

    raw_courses = (user.get('moodle_course') or "").strip()
    if not raw_courses:
        return []

    course_ids: Set[int] = set()
    for chunk in raw_courses.split(','):
        chunk = chunk.strip()
        if chunk.isdigit():
            cid = int(chunk)
            if cid not in existing_ids:
                course_ids.add(cid)

    if not course_ids:
        return []

    title_overrides = {
        6755: "Математика",
        8788: "Вайбкодинг",
    }
    results: list[dict] = []
    for cid in sorted(course_ids):
        # course = get_course(cid)
        # title = title_overrides.get(cid) or (course or {}).get('title') or f"Курс {cid}"
        title = title_overrides.get(cid)
        results.append({'id': cid, 'title': title, 'source': 'moodle'})

    return results


async def update_moodle_courses_background(application, user_id: int, student_number: str):
    """
    Фоновое обновление списка курсов пользователя из Moodle.
    """
    if not student_number:
        logger.info(f"[Background task] Пропускаем обновление moodle_course для {user_id}: отсутствует student_id.")
        return

    logger.info(f"[Background task] Starting moodle_course update for user {user_id} (student_id: {student_number})")
    try:
        client = await MoodleClient.from_env()
        async with client as m:
            data = await m.get_student_progress_and_next_task(student_number)

        course_ids: list[int] = []
        for course in (data.get('courses') or []):
            cid = course.get('id')
            if cid is None:
                continue
            try:
                course_ids.append(int(cid))
            except Exception:
                continue

        new_courses_str = ",".join(str(x) for x in sorted(set(course_ids))) if course_ids else ""
        existing_courses_str = (get_user(user_id) or {}).get('moodle_course') or ""

        if new_courses_str != existing_courses_str:
            # Определяем существующие и новые курсы
            existing_course_ids = set()
            if existing_courses_str:
                try:
                    existing_course_ids = {int(x.strip()) for x in existing_courses_str.split(',') if x.strip()}
                except Exception:
                    existing_course_ids = set()

            new_course_ids = set(course_ids)

            # Находим добавленные курсы
            added_course_ids = new_course_ids - existing_course_ids

            # Обновляем базу данных
            update_user_moodle_course(user_id, new_courses_str)
            logger.info(f"[Background task] moodle_course обновлён для {user_id}: '{existing_courses_str}' -> '{new_courses_str}'")

            # Отправляем уведомления о новых курсах
            for course_id in added_course_ids:
                course_name = "неизвестный курс"
                if course_id == 6755:
                    course_name = "математика"
                elif course_id == 8788:
                    course_name = "вайбкодинг"

                try:
                    await application.bot.send_message(
                        chat_id=user_id,
                        text=f"🎉 Добавлен курс {course_name}!",
                        parse_mode='HTML'
                    )
                    logger.info(f"[Background task] Отправлено уведомление о новом курсе {course_name} пользователю {user_id}")
                except Exception as e:
                    logger.error(f"[Background task] Не удалось отправить уведомление о курсе {course_name} пользователю {user_id}: {e}")

        else:
            logger.info(f"[Background task] moodle_course без изменений для {user_id}: '{existing_courses_str}'")
    except httpx.ReadTimeout:
        logger.warning(f"[Background task] Timeout при обновлении moodle_course для {user_id}.")
    except Exception as e:
        logger.error(f"[Background task] Failed to update moodle_course for user {user_id}: {e}", exc_info=True)


async def fetch_and_cache_moodle_course(application, chat_id: int, message_id: int, user_id: int, course_id: int, student_number: str, context):
    """
    Загружает данные о курсе в фоне, сохраняет в кэш и обновляет сообщение.
    """
    try:
        client = await MoodleClient.from_env()
    except Exception as e:
        logger.error(f"Failed to init MoodleClient for user {user_id}: {e}")
        try:
            await application.bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text="Moodle временно недоступен. Попробуйте позже.",
                reply_markup=main_menu_keyboard_for(user_id)
            )
        except Exception as edit_error:
            logger.error(f"Failed to edit message after Moodle init error for user {user_id}: {edit_error}")
        return

    progress = None
    async with client as m:
        try:
            logger.info(f"Fetching Moodle progress for user {user_id}, course {course_id}, student_number: {student_number}")
            progress = await m.get_student_progress_for_course(course_id, student_number)
            if progress:
                logger.info(f"Successfully fetched progress for user {user_id}, course {course_id}: {progress.get('percent')}%")
            else:
                logger.warning(f"No progress returned for user {user_id}, course {course_id}, student_number: {student_number}. "
                             f"Possible reasons: user not found, no access to course, or course not available.")
        except Exception as e:
            error_msg = str(e)
            # Если это ошибка доступа, логируем как предупреждение, а не ошибку
            if "контексте курса" in error_msg or "не доступны" in error_msg or "access" in error_msg.lower():
                logger.warning(f"No access to course {course_id} for user {user_id} (student_number: {student_number}). "
                             f"Moodle token may not have permissions for this course.")
            else:
                logger.error(f"Failed to fetch Moodle progress for user {user_id}, course {course_id}: {e}", exc_info=True)

    # Формируем текст и кнопки
    if not progress:
        text = (
            "<b>📚 Обновление курса</b>\n"
            f"Курс Moodle <code>#{course_id}</code>\n\n"
            "<blockquote>Не удалось получить подробности прямо сейчас. Попробуй ещё раз чуть позже — "
            "я запросил данные напрямую у Moodle.</blockquote>"
        )
        buttons = [
            [InlineKeyboardButton("⬅️ К списку курсов", callback_data='my_courses')],
            [InlineKeyboardButton("Главное меню", callback_data='main_menu')]
        ]
    else:
        name = progress.get('course_name') or f"Курс {course_id}"
        percent = progress.get('percent')
        try:
            percent_str = f"{float(percent):.0f}%" if percent is not None else "—"
        except Exception:
            percent_str = "—"
        name_html = html.escape(name)
        text_lines = [
            f"<b>📚 {name_html}</b>",
            f"<b>Прогресс:</b> <code>{percent_str}</code>"
        ]
        next_task = progress.get('next_task')
        if next_task:
            text_lines.append("\n<b>Следующее задание</b>")
            task_name = next_task.get('name') or '—'
            text_lines.append(f"• {html.escape(task_name)}")
            task_url = next_task.get('url')
            if task_url:
                text_lines.append(f'<a href="{html.escape(task_url)}">Открыть в Moodle</a>')
        text_lines.append(
            "\n<blockquote>Данные обновляются при каждом открытии карточки, поэтому загрузка может занять несколько секунд.</blockquote>"
        )
        text = "\n".join(text_lines)

        buttons = []

        # Добавляем кнопку "Задать вопрос по курсу"
        buttons.append([InlineKeyboardButton("❓ Задать вопрос по курсу", callback_data=f"course_ask:{course_id}")])

        buttons.extend([
            [InlineKeyboardButton("⬅️ К списку курсов", callback_data='my_courses')],
            [InlineKeyboardButton("Главное меню", callback_data='main_menu')]
        ])

    # Сохраняем данные в кэш
    cache_key = f"moodle_course_{course_id}"
    context.user_data[cache_key] = {
        'text': text,
        'buttons': buttons,
        'timestamp': datetime.now().isoformat()
    }

    try:
        await application.bot.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text=text,
            reply_markup=InlineKeyboardMarkup(buttons),
            parse_mode='HTML'
        )
    except Exception as edit_error:
        logger.error(f"Failed to edit Moodle course info message for user {user_id}, course {course_id}: {edit_error}")


# async def fetch_and_display_moodle_course(application, chat_id: int, message_id: int, user_id: int, course_id: int, student_number: str):
#     """
#     Устаревшая функция - оставлена для совместимости.
#     """
#     try:
#         client = await MoodleClient.from_env()
#     except Exception as e:
#         logger.error(f"Failed to init MoodleClient for user {user_id}: {e}")
#         try:
#             await application.bot.edit_message_text(
#                 chat_id=chat_id,
#                 message_id=message_id,
#                 text="Moodle временно недоступен. Попробуйте позже.",
#                 reply_markup=main_menu_keyboard_for(user_id)
#             )
#         except Exception as edit_error:
#             logger.error(f"Failed to edit message after Moodle init error for user {user_id}: {edit_error}")
#         return

#     progress = None
#     async with client as m:
#         try:
#             progress = await m.get_student_progress_for_course(course_id, student_number)
#         except Exception as e:
#             logger.error(f"Failed to fetch Moodle progress for user {user_id}, course {course_id}: {e}")

#     if not progress:
#         text = (
#             "<b>📚 Обновление курса</b>\n"
#             f"Курс Moodle <code>#{course_id}</code>\n\n"
#             "<blockquote>Не удалось получить подробности прямо сейчас. Попробуй ещё раз чуть позже — "
#             "я запросил данные напрямую у Moodle.</blockquote>"
#         )
#     else:
#         name = progress.get('course_name') or f"Курс {course_id}"
#         percent = progress.get('percent')
#         try:
#             percent_str = f"{float(percent):.0f}%" if percent is not None else "—"
#         except Exception:
#             percent_str = "—"
#         name_html = html.escape(name)
#         text_lines = [
#             f"<b>📚 {name_html}</b>",
#             f"<b>Прогресс:</b> <code>{percent_str}</code>"
#         ]
#         next_task = progress.get('next_task')
#         if next_task:
#             text_lines.append("\n<b>Следующее задание</b>")
#             task_name = next_task.get('name') or '—'
#             text_lines.append(f"• {html.escape(task_name)}")
#             task_url = next_task.get('url')
#             if task_url:
#                 text_lines.append(f'<a href="{html.escape(task_url)}">Открыть в Moodle</a>')
#         text_lines.append(
#             "\n<blockquote>Данные обновляются при каждом открытии карточки, поэтому загрузка может занять несколько секунд.</blockquote>"
#         )
#         text = "\n".join(text_lines)

#     buttons = []

#     # Добавляем кнопку "Задать вопрос по курсу"
#     buttons.append([InlineKeyboardButton("❓ Задать вопрос по курсу", callback_data=f"course_ask:{course_id}")])

#     buttons.extend([
#         [InlineKeyboardButton("⬅️ К списку курсов", callback_data='my_courses')],
#         [InlineKeyboardButton("Главное меню", callback_data='main_menu')]
#     ])

#     try:
#         await application.bot.edit_message_text(
#             chat_id=chat_id,
#             message_id=message_id,
#             text=text,
#             reply_markup=InlineKeyboardMarkup(buttons),
#             parse_mode='HTML'
#         )
#     except Exception as edit_error:
#         logger.error(f"Failed to edit Moodle course info message for user {user_id}, course {course_id}: {edit_error}")


def ensure_admin_whitelist(user_id: int):
    """Добавляет админа в whitelist rate limiter."""
    # ВАЖНО: авто-bypass лимитов опасен (приводит к затратам/DoS при спаме).
    # Включать только явно через env ADMIN_BYPASS_RATE_LIMITS=1.
    if os.getenv("ADMIN_BYPASS_RATE_LIMITS", "0") == "1" and is_user_admin(user_id):
        rag_rate_limiter.add_to_whitelist(user_id)
        rag_continue_limiter.add_to_whitelist(user_id)
        logger.debug(f"Admin {user_id} added to rate limiter whitelists")

# --- ЗАЩИТА ОТ РАБОТЫ В ГРУППОВЫХ ЧАТАХ ---
def check_private_chat(update: Update) -> bool:
    """
    Проверяет, что сообщение пришло из приватного чата (личные сообщения).
    Возвращает True, если чат приватный, False - если группа/канал/супергруппа.
    """
    # Для callback queries проверяем через message.chat
    if update.callback_query and update.callback_query.message:
        chat = update.callback_query.message.chat
        return chat.type == 'private' if chat else False
    
    # Для обычных сообщений проверяем через effective_chat
    if update.effective_chat:
        return update.effective_chat.type == 'private'
    
    # Если нет ни того, ни другого - считаем не приватным
    return False

async def reject_non_private_chat(update: Update, context: CallbackContext):
    """
    Отправляет сообщение об отказе работы в групповых чатах.
    """
    try:
        if update.message:
            await update.message.reply_text(
                "❌ Извините, я работаю только в личных сообщениях.\n"
                "Пожалуйста, напишите мне в личку для продолжения работы."
            )
        elif update.callback_query:
            await update.callback_query.answer(
                "Бот работает только в личных сообщениях",
                show_alert=True
            )
    except Exception as e:
        logger.error(f"Failed to send rejection message: {e}")
# --- КОНЕЦ ЗАЩИТЫ ОТ ГРУППОВЫХ ЧАТОВ ---


# ==================== Система фидбека ====================
FEEDBACK_PROMPT_TEXT = "🙏 Пожалуйста, оцени ответ кнопками ниже."


def prepare_feedback_text(text: str, include_prompt: bool = True, escape_html: bool = True) -> str:
    """
    Добавляет к сообщению просьбу оценить ответ для размещения над кнопками фидбека.
    Экранирует HTML-сущности в тексте, чтобы избежать ошибок парсинга Telegram.
    
    Args:
        text: Текст сообщения (обычно от LLM)
        include_prompt: Добавлять ли просьбу оценить ответ
        escape_html: Экранировать ли HTML-сущности (по умолчанию True)
    """
    prepared = (text or "").rstrip()
    
    # Экранируем HTML-сущности (<, >, &) чтобы Telegram не пытался их парсить
    if escape_html:
        prepared = html.escape(prepared)
    
    if not include_prompt:
        return prepared
    if FEEDBACK_PROMPT_TEXT in prepared:
        return prepared
    if prepared:
        return f"{prepared}\n\n{FEEDBACK_PROMPT_TEXT}"
    return FEEDBACK_PROMPT_TEXT


def create_feedback_keyboard(interaction_log_id: int, current_feedback: int | None = None) -> InlineKeyboardMarkup:
    like_emoji = "✅👍" if current_feedback == 1 else "👍"
    dislike_emoji = "✅👎" if current_feedback == -1 else "👎"

    keyboard = [[
        InlineKeyboardButton(like_emoji, callback_data=f"feedback_like:{interaction_log_id}"),
        InlineKeyboardButton(dislike_emoji, callback_data=f"feedback_dislike:{interaction_log_id}")
    ]]

    keyboard.append([
        InlineKeyboardButton("Я всё понял", callback_data=f"feedback_ack:{interaction_log_id}")
    ])

    if current_feedback is not None:
        keyboard.append([
            InlineKeyboardButton(
                "❌ Сбросить оценку",
                callback_data=f"feedback_reset:{interaction_log_id}"
            )
        ])

    return InlineKeyboardMarkup(keyboard)


def _merge_feedback_with_menu(
    feedback_markup: InlineKeyboardMarkup,
    menu_markup: InlineKeyboardMarkup | None
) -> InlineKeyboardMarkup:
    feedback_rows = [row[:] for row in feedback_markup.inline_keyboard]
    if not menu_markup or not isinstance(menu_markup, InlineKeyboardMarkup):
        return InlineKeyboardMarkup(feedback_rows)

    combined_keyboard = feedback_rows + [row[:] for row in menu_markup.inline_keyboard]
    return InlineKeyboardMarkup(combined_keyboard)


async def send_message_with_feedback(
    context: CallbackContext,
    chat_id: int,
    text: str,
    interaction_log_id: int | None,
    reply_markup: InlineKeyboardMarkup | None = None,
    reply_to_message_id: int | None = None,
    ensure_prompt: bool = False
) -> int:
    """
    Отправляет сообщение с кнопками фидбека, размещая их перед остальными элементами меню.
    """
    final_text = prepare_feedback_text(text, include_prompt=ensure_prompt)

    if interaction_log_id is None:
        logger.warning("Feedback message sent without interaction log ID — sending without feedback buttons")
        # Используем экранированный текст (final_text) вместо оригинального
        message = await context.bot.send_message(
            chat_id=chat_id,
            text=final_text,
            reply_markup=reply_markup,
            reply_to_message_id=reply_to_message_id,
            parse_mode='HTML'
        )
        return message.message_id

    feedback_markup = create_feedback_keyboard(interaction_log_id)
    final_reply_markup = _merge_feedback_with_menu(feedback_markup, reply_markup)

    message = await context.bot.send_message(
        chat_id=chat_id,
        text=final_text,
        reply_markup=final_reply_markup,
        reply_to_message_id=reply_to_message_id,
        parse_mode='HTML'
    )

    return message.message_id


def _split_menu_rows(reply_markup: InlineKeyboardMarkup | None):
    if not reply_markup or not isinstance(reply_markup, InlineKeyboardMarkup):
        return []

    preserved_rows = []
    for row in reply_markup.inline_keyboard:
        callbacks = [btn.callback_data for btn in row if btn.callback_data]
        if callbacks and all(cb.startswith('feedback_') for cb in callbacks):
            continue
        preserved_rows.append(row)
    return preserved_rows


async def handle_feedback_callback(query, context: CallbackContext):
    """
    Обработка нажатий на кнопки фидбека.
    """
    data = query.data or ""
    user_id = query.from_user.id

    try:
        action, interaction_id_raw = data.split(':', 1)
        interaction_log_id = int(interaction_id_raw)
    except ValueError:
        logger.error(f"Invalid feedback callback data: {data}")
        await context.bot.send_message(chat_id=user_id, text="❌ Не удалось распознать действие фидбека.")
        return

    interaction = get_rag_interaction(interaction_log_id)
    if not interaction:
        logger.warning(f"Feedback interaction {interaction_log_id} not found for user {user_id}")
        await context.bot.send_message(chat_id=user_id, text="❌ Сообщение не найдено, повторите позже.")
        return
    
    if action == 'feedback_ack':
        # Сбрасываем контекст диалога, чтобы пользователь вернулся в главное меню
        user_data = context.user_data
        user_data.pop('rag_dialogue_state', None)
        if user_data.get('step') in {'rag_flow', 'rag_query'}:
            user_data.pop('step', None)
        await context.bot.send_message(
            chat_id=user_id,
            text="👍 Отлично! Возвращаю тебя в главное меню.",
            reply_markup=main_menu_keyboard_for(user_id)
        )
        return

    current_feedback = interaction.get('user_feedback')
    feedback_value = None

    if action == 'feedback_like':
        feedback_value = 1
        feedback_text = "👍 Спасибо за оценку! Продолжаем в том же духе."
    elif action == 'feedback_dislike':
        feedback_value = -1
        feedback_text = "👎 Спасибо за обратную связь! Мы постараемся стать лучше."
    elif action == 'feedback_reset':
        if current_feedback is None:
            logger.debug(f"User {user_id} attempted to reset feedback but none was set (interaction {interaction_log_id})")
            return
        feedback_value = None
        feedback_text = "✅ Оценка сброшена. Спасибо!"
    else:
        logger.warning(f"Unknown feedback action: {action}")
        return

    if feedback_value is not None and current_feedback == feedback_value:
        logger.debug(f"User {user_id} pressed the same feedback button again (interaction {interaction_log_id})")
        return

    updated = update_rag_interaction_feedback(interaction_log_id, feedback_value)
    if not updated:
        await context.bot.send_message(chat_id=user_id, text="❌ Не удалось сохранить фидбек. Попробуй позже.")
        return

    current_feedback = feedback_value if feedback_value is not None else None
    feedback_markup = create_feedback_keyboard(interaction_log_id, current_feedback)
    preserved_menu_rows = _split_menu_rows(query.message.reply_markup if query.message else None)
    combined_rows = [row[:] for row in feedback_markup.inline_keyboard] + preserved_menu_rows

    try:
        await query.edit_message_reply_markup(reply_markup=InlineKeyboardMarkup(combined_rows))
    except Exception as e:
        message_id = query.message.message_id if query.message else "unknown"
        logger.warning(f"Failed to update feedback keyboard for message {message_id}: {e}")

    try:
        await context.bot.send_message(chat_id=user_id, text=feedback_text)
    except Exception as e:
        logger.error(f"Failed to send feedback confirmation to user {user_id}: {e}")
# ==================== Конец системы фидбека ====================

# ------------------ USER / REGISTRATION flows ------------------
# async def start(update: Update, context: CallbackContext):
#     logger.info(f"Received /start from {update.message.from_user.id}")
#     user_id = update.message.from_user.id
#     await update.message.reply_text('Привет! Введи свой username:',
#                                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Главное меню", callback_data='main_menu')]]))
#     context.user_data.clear()
#     context.user_data['step'] = 'username'
#     context.user_data['user_id'] = user_id
# ------------------ USER / REGISTRATION flows ------------------
def get_bot_description() -> str:
    """
    Возвращает описание функционала бота.
    """
    return (
        "👋 Привет!\n\n"
        "📚 Я — Digital Professor, твой цифровой помощник в учёбе. Я помогу тебе разобраться в материале, не пропустить дедлайны и учиться с комфортом.\n\n"
        "Вот как я буду тебе помогать:\n\n"
        "🔹 присылать напоминания в удобное время — для обучения без стресса,\n\n"
        "🔹 показывать ближайший дедлайн из Moodle — чтобы быть в курсе сроков,\n\n"
        "🔹 отвечать на вопросы по курсу — когда преподаватель недоступен, я рядом,\n\n"
        "🔹 отслеживать прогресс по курсам на Moodle — чтобы поддерживать тебя на каждом шаге.\n\n"
        "Давай познакомимся! Расскажи немного о себе — и я настрою курс под тебя. 🤝"
    )


async def start(update: Update, context: CallbackContext):
    """
    Обработчик команды /start.
    Инициализирует процесс регистрации или показывает главное меню,
    если пользователь уже зарегистрирован. Предотвращает конфликты
    активных сессий.
    """
    # Проверка типа чата: работаем только в приватных чатах
    if not check_private_chat(update):
        await reject_non_private_chat(update, context)
        return
    
    logger.info(f"Received /start from {update.effective_user.id}")
    user_id = update.effective_user.id
    user_data = context.user_data

    # --- ИСПРАВЛЕНИЕ: Проверка и сброс активного состояния ---
    # Определяем, находится ли пользователь в активной сессии
    # Используем get() с None, чтобы явно проверить существование ключа
    is_in_registration = user_data.get('step') is not None
    is_in_test = user_data.get('test_session') is not None
    is_awaiting_task_result = user_data.get('awaiting_task_result') is not None
    is_in_big5 = user_data.get('big5_step') is not None

    # Если пользователь находится в любой активной сессии
    if is_in_registration or is_in_test or is_awaiting_task_result or is_in_big5:
        logger.info(f"User {user_id} issued /start while in active session. Resetting state.")
        
        # Сбрасываем состояние пользователя
        # Полная очистка предпочтительнее для избежания любых конфликтов
        user_data.clear() 
        
        # Уведомляем пользователя о сбросе
        await update.message.reply_text(
            "Предыдущая операция была прервана. Начинаем сначала.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Главное меню", callback_data='main_menu')]])
        )
    # --- КОНЕЦ ИСПРАВЛЕНИЯ ---

    # Проверяем, зарегистрирован ли пользователь в базе данных
    if is_user_registered(user_id):
        # Если зарегистрирован, получаем данные пользователя для персонального приветствия
        user_data_db = get_user(user_id)
        user_name = extract_first_name(user_data_db.get('username')) if user_data_db else "студент"
        
        # Показываем описание функционала и персональное приветствие
        description = get_bot_description()
        
        await update.message.reply_text(
            description,
            parse_mode='HTML'
        )
        await update.message.reply_text(
            f"<b>C возвращением, {user_name}!</b>\n\n{WELCOME_INFO_TEXT}\n\nВыберите действие:",
            reply_markup=main_menu_keyboard_for(user_id),
            parse_mode='HTML'
        )
    else:
        # Если не зарегистрирован, создаем заготовку пользователя
        create_user_stub(user_id, None)
        
        # Показываем описание функционала
        description = get_bot_description()
        await update.message.reply_text(
            description,
            parse_mode='HTML'
        )
        
        # --- NEW REGISTRATION FLOW START ---
        # Step 1: Welcome & Role Selection
        await update.message.reply_text(
            "Готов начать? Нажми кнопку ниже.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("Начать", callback_data='start_registration')]
            ])
        )
        user_data['step'] = 'awaiting_registration_start'
        user_data['user_id'] = user_id
        # --- NEW REGISTRATION FLOW END ---
    # --- Конец оригинальной логики ---


async def help_command(update: Update, context: CallbackContext):
    """
    Обработчик команды /help.
    Показывает описание функционала бота в зависимости от группы пользователя.
    """
    # Проверка типа чата: работаем только в приватных чатах
    if not check_private_chat(update):
        await reject_non_private_chat(update, context)
        return
    
    user_id = update.effective_user.id
    
    # Показываем описание функционала
    description = get_bot_description()
    await update.message.reply_text(
        description,
        parse_mode='HTML',
        reply_markup=main_menu_keyboard_for(user_id) if is_user_registered(user_id) else None
    )


# bot.py (продолжение)

# --- ФУНКЦИЯ ИНИЦИАЛИЗАЦИИ RAG ---
async def initialize_rag_if_needed():
    """
    Инициализирует RAG-пайплайны для обоих доменов (math и vibe) отдельно.
    Потокобезопасна благодаря asyncio.Lock.
    """
    global rag_pipeline_math, rag_pipeline_vibe

    # 1. Если RAG не был импортирован успешно - выходим с ошибкой
    if not RAG_IMPORT_SUCCESS or RagPipeline is None:
        logging.error("RAG Pipeline class is not available. Initialization skipped.")
        return None

    # 2. Блокировка для предотвращения гонок
    async with rag_init_lock:
        # 3. Проверка: если оба уже инициализированы - выходим
        if (rag_pipeline_math is not None and getattr(rag_pipeline_math, 'is_initialized', False) and
            rag_pipeline_vibe is not None and getattr(rag_pipeline_vibe, 'is_initialized', False)):
            logging.debug("RAG Pipelines already initialized.")
            return True

        logging.info("Starting RAG Pipeline initialization for both domains...")
        
        # --- Определяем пути к JSON файлам для каждого домена ---
        import os
        import glob
        
        proj_dir = os.path.abspath(os.path.dirname(__file__))
        
        # Пробуем сначала в корне проекта, затем в rag/ (для обратной совместимости)
        vibe_dir_candidates = [
            os.path.join(proj_dir, "data_vibecoding"),  # В корне проекта
            os.path.join(proj_dir, "rag", "data_vibecoding"),  # В rag/ (старое расположение)
        ]
        math_dir_candidates = [
            os.path.join(proj_dir, "data_math"),  # В корне проекта
            os.path.join(proj_dir, "rag", "data_math"),  # В rag/ (старое расположение)
        ]
        
        # Находим первую существующую директорию для vibecoding
        vibe_dir = None
        for candidate in vibe_dir_candidates:
            if os.path.isdir(candidate):
                vibe_dir = candidate
                break
        
        # Находим первую существующую директорию для math
        math_dir = None
        for candidate in math_dir_candidates:
            if os.path.isdir(candidate):
                math_dir = candidate
                break
        
        # Файлы для vibecoding
        VIBE_JSON_FILES = []
        if vibe_dir:
            VIBE_JSON_FILES = sorted(glob.glob(os.path.join(vibe_dir, "*.json")))
            logging.info(f"Found {len(VIBE_JSON_FILES)} JSON files for vibecoding domain in {vibe_dir}")
        else:
            logging.warning(f"Directory for vibecoding not found. Tried: {vibe_dir_candidates}")
        
        # Файлы для math
        MATH_JSON_FILES = []
        if math_dir:
            MATH_JSON_FILES = sorted(glob.glob(os.path.join(math_dir, "*.json")))
            logging.info(f"Found {len(MATH_JSON_FILES)} JSON files for math domain in {math_dir}")
        else:
            logging.warning(f"Directory for math not found. Tried: {math_dir_candidates}")
        
        # 4. Инициализируем домен vibecoding (для курса 8788)
        if VIBE_JSON_FILES:
            try:
                if rag_pipeline_vibe is None or not getattr(rag_pipeline_vibe, 'is_initialized', False):
                    rag_pipeline_vibe = RagPipeline(
                        persist_directory="./data/chroma_db_gitverse",
                        collection_name="Gitverse"
                    )
                    await rag_pipeline_vibe.initialize(json_files=VIBE_JSON_FILES, force_recreate=False)
                    logging.info("RAG Pipeline (vibecoding) initialized successfully.")
                else:
                    logging.debug("RAG Pipeline (vibecoding) already initialized.")
            except Exception as e:
                logging.error(f"Failed to initialize RAG Pipeline (vibecoding): {e}", exc_info=True)
                rag_pipeline_vibe = None
        else:
            logging.warning("No JSON files found for vibecoding domain. Skipping initialization.")
        
        # 5. Инициализируем домен math (для курса 6755)
        if MATH_JSON_FILES:
            try:
                if rag_pipeline_math is None or not getattr(rag_pipeline_math, 'is_initialized', False):
                    rag_pipeline_math = RagPipeline(
                        persist_directory="./data/chroma_db_math",
                        collection_name="Math"
                    )
                    await rag_pipeline_math.initialize(json_files=MATH_JSON_FILES, force_recreate=False)
                    logging.info("RAG Pipeline (math) initialized successfully.")
                else:
                    logging.debug("RAG Pipeline (math) already initialized.")
            except Exception as e:
                logging.error(f"Failed to initialize RAG Pipeline (math): {e}", exc_info=True)
                rag_pipeline_math = None
        else:
            logging.warning("No JSON files found for math domain. Skipping initialization.")
        
        logging.info("RAG Pipeline initialization completed for both domains.")
        return True
# --- КОНЕЦ ФУНКЦИИ ИНИЦИАЛИЗАЦИИ ---

async def menu(update: Update, context: CallbackContext):
    # Проверка типа чата: работаем только в приватных чатах
    if not check_private_chat(update):
        await reject_non_private_chat(update, context)
        return
    
    if update.message:
        uid = update.effective_user.id if update.effective_user else None
        await update.message.reply_text(
            f"{WELCOME_INFO_TEXT}\n\nВыберите действие:",
            reply_markup=main_menu_keyboard_for(uid) if uid else main_menu_keyboard_for(None),
            parse_mode='HTML'
        )
    else:
        await update.callback_query.answer()
        uid = update.callback_query.from_user.id if update.callback_query and update.callback_query.from_user else None
        await update.callback_query.edit_message_text(
            f"{WELCOME_INFO_TEXT}\n\nВыберите действие:",
            reply_markup=main_menu_keyboard_for(uid) if uid else main_menu_keyboard_for(None),
            parse_mode='HTML'
        )
    context.user_data.clear()

async def recalc_big5(update: Update, context: CallbackContext):
    # Проверка типа чата: работаем только в приватных чатах
    if not check_private_chat(update):
        await reject_non_private_chat(update, context)
        return
    
    await _weekly_big5_job(context.application)
    await update.message.reply_text("Big-5 пересчитан вручную!")

async def handle_message(update: Update, context: CallbackContext):
    """
    Обрабатывает поток регистрации и ответы на задания.
    """
    # Проверка типа чата: работаем только в приватных чатах
    if not check_private_chat(update):
        await reject_non_private_chat(update, context)
        return
    
    msg = update.message
    if not msg:
        return

    user_data = context.user_data
    user_id = msg.from_user.id

    # --- ВХОДНОЙ АНТИСПАМ: ограничение частоты апдейтов Telegram ---
    # Делаем это ДО любой бизнес-логики/LLM, чтобы флуд не создавал нагрузку и не тратил деньги.
    incoming_text = (msg.text or "") if hasattr(msg, "text") else ""
    allowed, rate_error = telegram_incoming_limiter.is_allowed(user_id, incoming_text, "telegram_incoming")
    if not allowed:
        # Чтобы бот сам не превращался в "спам-генератор", показываем уведомление не чаще чем раз в N секунд.
        if _should_send_rate_notice(user_id):
            try:
                await msg.reply_text(rate_error)
            except Exception as e:
                logger.debug(f"Failed to send rate limit notice to user {user_id}: {e}")
        return

    if user_data.get('step') == 'student_id':
        raw_student_id = msg.text.strip() if msg.text else ""

        if not is_valid_student_id(raw_student_id):
            await msg.reply_text(
                "<b>Ой, кажется вы указали что-то другое!</b>\nУкажите номер студенческого билета.\nПосмотреть можно здесь:",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Открыть сайт", url=URFU_STUDENT_URL)]]),
                parse_mode='HTML'
            )
            return

        user_data['student_id'] = raw_student_id
        logger.info(f"Student ID {raw_student_id} temporarily saved for user {user_id} in context.user_data.")

        keyboard = [
            [InlineKeyboardButton("Екатеринбург", callback_data='Asia/Yekaterinburg')],
            [InlineKeyboardButton("Москва", callback_data='Europe/Moscow')]
        ]
        await msg.reply_text(
            "Выбери свой часовой пояс, чтобы я вовремя присылал напоминания:",
            reply_markup=keyboard_with_main_menu(keyboard)
        )
        user_data['step'] = 'timezone_select'
        return

    # --- Шаг ввода ФИО ---
    if user_data.get('step') == 'username':
        username = msg.text.strip() if msg.text else ""
        if not is_valid_full_name(username):
            await msg.reply_text(
                "<b>Ой, кажется вы указали что-то другое!</b>\nУкажите ваше ФИО:",
                parse_mode='HTML'
            )
            return

        user_data['username'] = username
        first_name = extract_first_name(username)
        user_data['first_name'] = first_name
        logger.info(f"Username '{username}' set for user {user_id}.")

        await msg.reply_text(
            f"<b>Очень приятно, {first_name}!</b>\nДля отслеживания прогресса укажи номер студенческого билета.\nПосмотреть можно здесь 👇",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Открыть сайт", url=URFU_STUDENT_URL)]]),
            parse_mode='HTML'
        )
        user_data['step'] = 'student_id'
        return
    # --- КОНЕЦ ИЗМЕНЕНИЙ ---

    # --- Шаг установки времени пинга ---
    if user_data.get('step') == 'ping_time':
        text = msg.text or ""
        parts = text.split(':')
        if len(parts) != 2:
            await msg.reply_text('Введи время напоминания в формате HH:MM (например, 10:00):')
            return
        try:
            h = int(parts[0]); m = int(parts[1])
            assert 0 <= h < 24 and 0 <= m < 60
        except Exception:
            await msg.reply_text('Неверный формат времени напоминания. Используй HH:MM')
            return
        new_time = f"{h:02d}:{m:02d}"
        
        user_data['ping_time'] = new_time

        await msg.reply_text(
            "<b>Отлично! Я буду напоминать тебе о прохождении курса в это время.</b>\nЧтобы завершить регистрацию, ответь на 3 вопроса:",
            parse_mode='HTML'
        )
        user_data['big5_step'] = 'q1'
        user_data.pop('step', None)
        await msg.reply_text("Вопрос 1/3: Что обычно помогает тебе настроиться на работу/учёбу?")
        return

    # --- ИЗМЕНЕНО: Шаг изменения времени пинга ---
    # Логика остается прежней, только добавлено перепланирование
    if user_data.get('step') == 'change_ping_time':
        text = msg.text or ""
        parts = text.split(':')
        if len(parts) != 2:
            await msg.reply_text('Введи время напоминания в формате HH:MM',
                                 reply_markup=InlineKeyboardMarkup([[
                                     InlineKeyboardButton("Главное меню", callback_data='main_menu')
                                 ]]))
            return
        try:
            h = int(parts[0]); m = int(parts[1])
            assert 0 <= h < 24 and 0 <= m < 60
        except Exception:
            await msg.reply_text('Неверный формат времени напоминания. Используй HH:MM',
                                 reply_markup=InlineKeyboardMarkup([[
                                     InlineKeyboardButton("Главное меню", callback_data='main_menu')
                                 ]]))
            return
        new_time = f"{h:02d}:{m:02d}"
        update_user_ping_time(user_id, new_time)
        # u = get_user(user_id)
        # tz = u.get('timezone') if u else None
        # if tz: # сразу пересоздаем задачу
        #     add_user_reminder(context.application, user_id, tz, new_time)
        await msg.reply_text(f'Время напоминаний успешно изменено на {new_time}.', reply_markup=main_menu_keyboard_for(user_id))
        user_data.pop('step', None)
        # --- НОВОЕ: Перепланирование задачи пинга пользователя ---
        # После изменения времени пинга нужно перепланировать задачу
        try:
            from digital_professor.scheduler import reschedule_user_ping
            context.application.create_task(
                reschedule_user_ping(context.application, user_id)
            )
            logger.info(f"Scheduled reschedule ping job for user {user_id} after ping time change.")
        except Exception as e:
            logger.error(f"Error scheduling reschedule ping job for user {user_id} after time change: {e}", exc_info=True)
            # Ошибка логируется, но не мешает основному флоу
        # --- КОНЕЦ НОВОГО ---
        return
    # --- NEW: handle changing student id ---
    if user_data.get('step') == 'change_student_id':
        new_id = (msg.text or '').strip()
        if not is_valid_student_id(new_id):
            await msg.reply_text(
                'Неверный формат. Введи номер студенческого билета (6–12 цифр):',
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Главное меню", callback_data='main_menu')]])
            )
            return
        update_user_student_id(user_id, new_id)
        await msg.reply_text(
            f'Номер студенческого билета успешно изменён на {new_id}.',
            reply_markup=main_menu_keyboard_for(user_id)
        )
        try:
            context.application.create_task(
                update_moodle_courses_background(context.application, user_id, new_id)
            )
        except Exception as e:
            logger.error(f"Failed to schedule moodle update after student_id change for user {user_id}: {e}")
        user_data.pop('step', None)
        return
    
    # --- ЕЖЕНЕДЕЛЬНЫЙ ОПРОС: обработка текстового ввода названия темы ---
    if user_data.get('weekly_survey_step') == 'waiting_topic_name':
        topic_name = (msg.text or '').strip()
        if not topic_name:
            await msg.reply_text(
                "Пожалуйста, введи название темы:",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("Главное меню", callback_data='main_menu')]
                ])
            )
            return
        
        from digital_professor.database import save_weekly_ping
        answer1 = user_data.get('weekly_survey_answer1', '')
        answer2 = topic_name
        
        # Получаем данные пользователя
        u = get_user(user_id)
        student_id = (u or {}).get('student_id') if u else None
        username = (u or {}).get('username') or msg.from_user.first_name or "Пользователь"
        
        # Сохраняем ответы
        save_weekly_ping(user_id, student_id, username, answer1, answer2)
        
        await msg.reply_text(
            "Спасибо за ответы! Преподаватель постарается тебе помочь, если ты столкнешься с трудностями.",
            reply_markup=main_menu_keyboard_for(user_id)
        )
        user_data.pop('weekly_survey_answer1', None)
        user_data.pop('weekly_survey_step', None)
        return
    
    # --- КОНЕЦ ИЗМЕНЕНИЙ ---
    #Диалог биг5: Первичный скрининг 
        # ---------- BIG5 первичный опрос (3 вопроса) ----------
    if user_data.get('big5_step') == 'q1':
        raw_answer = msg.text.strip() if msg.text else ''
        sanitized_answer = sanitize_user_input(raw_answer)
        user_data['big5_q1'] = sanitized_answer
        user_data['big5_step'] = 'q2'
        await msg.reply_text("Вопрос 2/3: Обычно ты планируешь день заранее или действуешь по ситуации?")
        return

    if user_data.get('big5_step') == 'q2':
        raw_answer = msg.text.strip() if msg.text else ''
        sanitized_answer = sanitize_user_input(raw_answer)
        user_data['big5_q2'] = sanitized_answer  # Исправлено: должно быть 'big5_q2'
        user_data['big5_step'] = 'q3'
        await msg.reply_text("Вопрос 3/3: Что тебя больше мотивирует: результат или процесс?")
        return

    if user_data.get('big5_step') == 'q3':
        raw_answer = msg.text.strip() if msg.text else ''
        sanitized_answer = sanitize_user_input(raw_answer)
        user_data['big5_q3'] = sanitized_answer  # Исправлено: должно быть 'big5_q3'
        
        # ... (остальной код обработки ответов на все три вопроса) ...

        # формируем промпт и получаем первичный вектор
        prompt = f"""
## Input
Ответы пользователя:
1) {user_data.get('big5_q1')}
2) {user_data.get('big5_q2')}
3) {user_data.get('big5_q3')}

## Task
На основе этих ответов оцени Big5 (1..5) и верни JSON:
{{"extraversion":int,"agreeableness":int,"conscientiousness":int,"emotional_stability":int,"openness":int}}
"""
        # Пробуем получить вектор от LLM, при ошибке используем значения по умолчанию
        try:
            vector = await call_llm_json(prompt)
        except (ValueError, json.JSONDecodeError) as e:
            logger.warning(f"Failed to parse Big5 JSON from LLM for user {user_id}: {e}. Using default values.")
            # Используем нейтральные значения по умолчанию (3 для всех параметров)
            vector = {
                'extraversion': 3,
                'agreeableness': 3,
                'conscientiousness': 3,
                'emotional_stability': 3,
                'openness': 3
            }
        
        # Подготовка метаданных
        meta_initial = json.dumps({
        "source": "initial_3q",
        "answers": [user_data.get('big5_q1'), user_data.get('big5_q2'), user_data.get('big5_q3')]
    })
        

        upsert_big5_profile(
            user_id,
            int(vector.get('extraversion',3)),
            int(vector.get('agreeableness',3)),
            int(vector.get('conscientiousness',3)),
            int(vector.get('emotional_stability',3)),
            int(vector.get('openness',3)),
            meta_data=meta_initial
        )
        # --- НАЧАЛО ДОБАВЛЕНИЯ В ИСТОРИЮ ---
    # Вставка записи в историю Big5 для первоначального опроса
    # Используем специальный маркер 'initial_survey' для week_start
        insert_big5_history(
            user_id,
            'initial_survey', # Маркер начального опроса
            int(vector.get('extraversion', 3)),
            int(vector.get('agreeableness', 3)),
            int(vector.get('conscientiousness', 3)),
            int(vector.get('emotional_stability', 3)),
            int(vector.get('openness', 3)),
            meta_data=meta_initial # Используем те же метаданные
        )
    # --- КОНЕЦ ДОБАВЛЕНИЯ В ИСТОРИЮ ---
        
        # Помечаем, что первичный опрос Big5 пройден
        try:
            mark_big5_initialized(user_id)
            logger.info(f"Marked Big5 initialized for user {user_id}")
        except Exception as e:
            logger.error(f"Failed to mark Big5 initialized for user {user_id}: {e}")
        
        # --- NEW REGISTRATION FINALIZATION ---
        # 1. Recover all user data from context
        final_username = user_data.get('username')
        final_student_id = user_data.get('student_id')
        final_timezone = user_data.get('timezone')
        final_ping_time = user_data.get('ping_time')
        # 2. Save complete user profile
        if final_username and final_student_id and final_timezone and final_ping_time:
            save_user(
                user_id=user_id,
                username=final_username,
                timezone=final_timezone,
                ping_time=final_ping_time,
                student_id=final_student_id,
                ab_group=None
            )
            logger.info(f"User {user_id} registration completed and profile saved.")
            
            # 3. Schedule Ping
            try:
                from digital_professor.scheduler import reschedule_user_ping
                context.application.create_task(
                    reschedule_user_ping(context.application, user_id)
                )
            except Exception as e:
                logger.error(f"Error scheduling ping for user {user_id}: {e}", exc_info=True)

            # 4. Background Moodle Update
            try:
                context.application.create_task(
                    update_moodle_courses_background(context.application, user_id, final_student_id)
                )
            except Exception as e:
                logger.error(f"Failed to schedule moodle update: {e}")

        # 5. Clean Context
        user_data.pop('big5_step', None)
        user_data.pop('big5_q1', None)
        user_data.pop('big5_q2', None)
        user_data.pop('big5_q3', None)
        user_data.pop('username', None)
        user_data.pop('first_name', None)
        user_data.pop('student_id', None)
        user_data.pop('timezone', None)
        user_data.pop('ping_time', None)
        
        # 6. Send Completion Message (Cell 90/153)
        completion_name = extract_first_name(final_username)
        # Подстрахуемся: если по какой-то причине время пинга не в контексте — возьмем из БД
        final_ping_time_display = final_ping_time or (get_user_ping_time(user_id) or "настроенное время")
        completion_text = (
            f"<b>Регистрация завершена, {completion_name}!</b>\n\n"
            f"{WELCOME_INFO_TEXT}\n\n"
            f"Завтра тебе придёт первый пинг в <b>{final_ping_time_display}</b>. До встречи!"
        )
        await msg.reply_text(
            completion_text,
            reply_markup=main_menu_keyboard_for(user_id),
            parse_mode='HTML'
        )
        return
        # --- END NEW REGISTRATION FINALIZATION ---

    #--------- Логика проверки задания, с помощью LLM -----------------------------
# If user is currently answering an 'open' or 'practical' task
# Если пользователь отвечает на задание 'open' или 'practical'
    awaiting = user_data.get('awaiting_task_result')
    if awaiting:
        task_id = awaiting.get('task_id')
        task_type = awaiting.get('task_type')

        if not msg.text:
            await msg.reply_text(
                "Пожалуйста, отправьте ваш ответ текстом в это окно чата. "
                "Файлы и фото не принимаются."
            )
            return

        answer_text = msg.text.strip()

        # Получаем задание из БД
        task = get_task(task_id)

        score = None
        feedback = None

        # Если есть эталонный ответ или критерии — проверяем через LLM
        if task_type in ('open', 'practical') and (task.get('reference_answer') or task.get('evaluation_criteria')):
            score, feedback = await evaluate_with_llm(
                user_answer=answer_text,
                reference_answer=task.get('reference_answer'),
                criteria=task.get('evaluation_criteria'),
                max_score=task.get('max_score', 10),  # по умолчанию максимум 10
                user_id=user_id
            )

        task_result_id = save_task_result( # Предполагаем, что save_task_result возвращает ID новой записи
            task_id,
            user_id,
            question_id=None,
            choice_id=None,
            answer_text=answer_text,
            file_id=None,
            score=score,
            feedback=feedback
        )
        
        logger.info(f"Saved task result with ID {task_result_id}")

        if score is not None:
            passed = False
            if task_type == 'open' and score >= 8:
                passed = True
            elif task_type == 'practical' and score >= 9:
                passed = True

            if passed:
                logger.info(f"Task {task_id} passed for user {user_id}. Updating progress and linking response.")
                update_weekly_progress(
                    user_id,
                    task['course_id'],
                    kpi=get_kpi_for_user_course(user_id, task['course_id'])
                )
                # --- ОСТАВЛЯЕМ ТОЛЬКО ЭТОТ ВЫЗОВ link_last_response_to_task ---
                submitted_at_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S") # Время сдачи/прохождения
                logger.info(f"Linking last response to task for user {user_id}, task {task_id} (after pass check)")
                link_last_response_to_task(user_id, task_id, submitted_at_str)
                logger.info(f"Linked last response to task for user {user_id}, task {task_id} (after pass check)")
                # --- КОНЕЦ ОСТАВЛЕННОГО ВЫЗОВА ---
            else:
                logger.info(f"Task {task_id} NOT passed for user {user_id} (score: {score}). Progress not updated, response not linked.")
        else:
            logger.debug(f"Task {task_id} result for user {user_id} is pending evaluation. Progress not updated yet.")

        await msg.reply_text(
            "Ответ получен и сохранён." +
            (f"\nОценка: {score}\nКомментарий: {feedback}" if score is not None else " (будет проверен позже)"),
            reply_markup=main_menu_keyboard_for(user_id)
        )

        user_data.pop('awaiting_task_result', None)
        return
    
    # --- НОВОЕ: LLM-диалог без RAG, если у пользователя ещё нет id курса ---
    if user_data.get('step') == 'llm_free_query':
        free_q = msg.text.strip() if msg.text else ""

        normalized_free_q = (free_q or "").strip().lower()
        if normalized_free_q in {"отмена", "стоп", "/stop", "/cancel", "хватит"}:
            user_data.pop('step', None)
            user_data.pop('llm_feedback_prompt_shown', None)
            await msg.reply_text(
                "Диалог завершён. Если захочешь задать новый вопрос — открой меню и выбери «Задать вопрос по курсу».",
                reply_markup=main_menu_keyboard_for(user_id)
            )
            return

        if not free_q:
            await msg.reply_text(
                "❓ Пожалуйста, введите текст вопроса или нажмите 'Отмена'.",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("❌ Отмена", callback_data='main_menu')
                ]])
            )
            return

        # Язык входа (только русский + технич. англ.) — до regex-защиты
        from digital_professor.language_detector import check_user_language
        lang_ok, lang_err, _lang_log = await check_user_language(free_q, user_id)
        if not lang_ok:
            logger.warning(f"Language gate blocked free_query from user {user_id}: {lang_err}")
            await msg.reply_text(lang_err, reply_markup=main_menu_keyboard_for(user_id))
            return

        # Базовая валидация и цензор такие же, как для RAG
        from digital_professor.input_validator import validate_user_input, get_injection_risk_level
        is_valid, validation_error = validate_user_input(free_q)
        if not is_valid:
            await msg.reply_text(validation_error, reply_markup=main_menu_keyboard_for(user_id))
            return

        from digital_professor.llm_censor import check_user_query, get_censor_error_message
        is_allowed_by_censor, censor_reason = await check_user_query(free_q, user_id)
        if not is_allowed_by_censor:
            error_message = await get_censor_error_message(censor_reason)
            await msg.reply_text(error_message, reply_markup=main_menu_keyboard_for(user_id))
            return

        # Генерация ответа без контекста (LLM only)
        try:
            from digital_professor.llm import llm_answer_with_context
            answer_text = await llm_answer_with_context(history=[], docs=[], user_message=free_q)
        except Exception as e:
            logger.error(f"LLM-only answer failed: {e}", exc_info=True)
            answer_text = "Извините, возникла ошибка при генерации ответа. Попробуйте позже."

        is_allowed_by_censor, censor_reason = await check_user_query(free_q, user_id, model_response=answer_text)
        if not is_allowed_by_censor:
            logger.warning(f"LLM Censor blocked model output from user {user_id}: {censor_reason}")
            error_message = await get_censor_error_message(censor_reason)
            await msg.reply_text(error_message, reply_markup=main_menu_keyboard_for(user_id))
            return

        include_prompt = not user_data.get('llm_feedback_prompt_shown', False)
        final_text = prepare_feedback_text(answer_text, include_prompt=include_prompt)

        interaction_log_id = log_rag_interaction(
            user_id=user_id,
            user_query=free_q,
            retrieved_context=None,
            llm_response=answer_text,
            llm_tokens_used=None
        )

        if interaction_log_id:
            await send_message_with_feedback(
                context=context,
                chat_id=user_id,
                text=final_text,
                interaction_log_id=interaction_log_id,
                reply_markup=None
            )
        else:
            # final_text уже экранирован через prepare_feedback_text
            await msg.reply_text(final_text, parse_mode='HTML')

        user_data['llm_feedback_prompt_shown'] = True
        return
    # --- КОНЕЦ НОВОГО ---
    
#     --- НОВОЕ: Обработка запроса к RAG ---
    if user_data.get('step') == 'rag_query':
        user_question = msg.text.strip() if msg.text else ""

        if not user_question:
            await msg.reply_text(
                "❓ Пожалуйста, введите текст вопроса или нажмите 'Отмена'.",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("❌ Отмена", callback_data='main_menu')
                ]])
            )
            return # Не сбрасываем состояние, позволяем повторный ввод

        user_data['step'] = 'rag_flow' # <-- НОВОЕ (правильное)
        # Определяем домен (на случай потери контекста)
        selected_domain = user_data.get('rag_domain')
        if not selected_domain:
            try:
                u_tmp = get_user(user_id)
            except Exception:
                u_tmp = None
            moodle_course_str_tmp = (u_tmp or {}).get('moodle_course') or ""
            try:
                ids_tmp = {int(x) for x in moodle_course_str_tmp.split(',') if x.strip().isdigit()}
            except Exception:
                ids_tmp = set()
            if 6755 in ids_tmp:
                selected_domain = "math"
            elif 8788 in ids_tmp:
                selected_domain = "vibe"
            user_data['rag_domain'] = selected_domain

        # Админы освобождены от лимитов
        ensure_admin_whitelist(user_id)

        # 🛡️ УРОВЕНЬ 0: ЯЗЫК ВХОДА (до regex-защиты)
        from digital_professor.language_detector import check_user_language
        lang_ok, lang_err, _lang_log = await check_user_language(user_question, user_id)
        if not lang_ok:
            logger.warning(f"Language gate blocked RAG query from user {user_id}: {lang_err}")
            await msg.reply_text(
                lang_err,
                reply_markup=main_menu_keyboard_for(user_id)
            )
            user_data.pop('step', None)
            return

        # 🛡️ УРОВЕНЬ 1: БАЗОВАЯ ВАЛИДАЦИЯ (регулярки)
        from digital_professor.input_validator import validate_user_input, get_injection_risk_level
        
        is_valid, validation_error = validate_user_input(user_question)
        if not is_valid:
            logger.warning(f"Invalid RAG query from user {user_id}: {validation_error}")
            await msg.reply_text(
                validation_error,
                reply_markup=main_menu_keyboard_for(user_id)
            )
            user_data.pop('step', None)
            return
        
        # 🛡️ УРОВЕНЬ 2: RATE LIMITING (только дубликаты и burst)
        allowed, rate_error = rag_rate_limiter.is_allowed(user_id, user_question, 'rag')
        if not allowed:
            logger.warning(f"Rate limit exceeded for user {user_id} (RAG): {rate_error}")
            await msg.reply_text(
                rate_error,
                reply_markup=main_menu_keyboard_for(user_id)
            )
            user_data.pop('step', None)
            return

        # 🛡️ УРОВЕНЬ 3: LLM-ЦЕНЗОР (смысловая проверка для ВСЕХ сообщений)
        from digital_professor.llm_censor import check_user_query, get_censor_error_message
        
        is_allowed_by_censor, censor_reason = await check_user_query(user_question, user_id)
        if not is_allowed_by_censor:
            logger.warning(f"LLM Censor blocked query from user {user_id}: {censor_reason}")
            error_message = await get_censor_error_message(censor_reason)
            await msg.reply_text(
                error_message,
                reply_markup=main_menu_keyboard_for(user_id)
            )
            user_data.pop('step', None)
            return
        
        # Логируем подозрительные запросы (из старой валидации)
        risk_level = get_injection_risk_level(user_question)
        if risk_level in ('medium', 'high'):
            logger.warning(f"Suspicious RAG query from user {user_id} (risk={risk_level}): {user_question[:100]}")

        # Отправляем сообщение о том, что обрабатываем запрос
        processing_message = await msg.reply_text("🔍 Ищу информацию по вашему вопросу...")

        try:
            # --- ПОЛУЧАЕМ ДОСТУП К RAG ---
            # rag_pipeline - это глобальная переменная, определенная в начале bot.py
            # Она должна быть уже инициализирована задачей из main.py
            
            # Выбираем правильный RAG pipeline в зависимости от домена
            rag_pipeline = None
            if selected_domain == "math":
                rag_pipeline = rag_pipeline_math
            elif selected_domain == "vibe":
                rag_pipeline = rag_pipeline_vibe
            
            if rag_pipeline is None or not getattr(rag_pipeline, 'is_initialized', False):
                # Это может произойти, если инициализация ещё не завершена
                await processing_message.edit_text(
                    f"❌ Система вопросов по {'математике' if selected_domain == 'math' else 'вайбкодинг'} ещё не готова. Пожалуйста, попробуйте позже.",
                    reply_markup=main_menu_keyboard_for(user_id)
                )
                user_data.pop('step', None) # Сбрасываем состояние в случае ошибки
                return
                 
            # --- ИСПОЛЬЗУЕМ RAG ---
            logger.info(f"RAG Query received from user {user_id} (domain: {selected_domain}): '{user_question[:50]}...'")
            
            # 1. Retrieval: Получаем релевантные документы для выбранного домена
            relevant_docs = rag_pipeline.query(user_question)
            
            if not relevant_docs:
                await processing_message.edit_text(
                    "😔 К сожалению, я не нашел информации по вашему вопросу в материалах курса.",
                    reply_markup=main_menu_keyboard_for(user_id)
                )
                user_data.pop('step', None) # Сбрасываем состояние
                return

            # --- НОВОЕ: Генерация ПЕРВОГО ответа на базе истории+RAG ---
            logger.info(f"Generating first hint for user {user_id} with RAG query...")
            import uuid 
            from digital_professor.database import log_rag_dialogue_message
            session_id = str(uuid.uuid4())
            logger.info(f"Starting new RAG dialogue session for user {user_id}: {session_id}")
            log_rag_dialogue_message(user_id, session_id, 'user', user_question)
            
            try:
                # История на старте пуста — используем профиль RAG_INIT
                from digital_professor.llm import llm_first_turn_answer_structured
                llm_out = await llm_first_turn_answer_structured(user_question, relevant_docs, user_id=user_id)
                answer_text = llm_out.user_answer
                llm_analysis = llm_out.analysis
                tokens_used = 0 # llm_generate_hints не возвращает токены, можно оставить 0 или None
                # --- КОНЕЦ ВЫЗОВА llm_answer_with_context ---
                
                # --- НОВОЕ: Подготовка данных для логирования ---
                # Обрезаем контекст для лога (пересобираем из документов)
                context_for_log = "\n\n".join([
                    f"--- Источник: {doc.metadata.get('source_file', 'N/A')} ---\n{doc.page_content}" for doc in relevant_docs
                ])
                log_context = context_for_log[:5000] if context_for_log else None
                log_response = answer_text[:2000] if answer_text else None # Ограничиваем длину
                log_tokens = tokens_used if isinstance(tokens_used, int) else None
                # --- КОНЕЦ ПОДГОТОВКИ ---

                logger.info(f"First hint generated for user {user_id}")
                        
            except ImportError:
                logger.error("llm.llm_generate_hints is not available.")
                answer_text = "Ошибка: Не найдена функция для генерации подсказки."
                llm_analysis = None
                log_context = None
                log_response = answer_text
                log_tokens = None
            except Exception as llm_error:
                logger.error(f"Error generating first hint for user {user_id}: {llm_error}", exc_info=True)
                answer_text = f"❌ Произошла ошибка при генерации подсказки: {llm_error}"
                llm_analysis = None
                log_context = None
                log_response = answer_text
                log_tokens = None
            # --- КОНЕЦ ГЕНЕРАЦИИ ПЕРВОЙ ПОДСКАЗКИ ---

            is_allowed_by_censor, censor_reason = await check_user_query(user_question, user_id, model_response=answer_text)
            if not is_allowed_by_censor:
                logger.warning(f"LLM Censor blocked RAG model output for user {user_id}: {censor_reason}")
                error_message = await get_censor_error_message(censor_reason)
                await processing_message.edit_text(
                    error_message,
                    reply_markup=main_menu_keyboard_for(user_id)
                )
                user_data.pop('step', None)
                return

            log_rag_dialogue_message(user_id, session_id, 'assistant', answer_text)

            interaction_log_id = None
            try:
                interaction_log_id = log_rag_interaction(
                    user_id=user_id,
                    user_query=user_question,
                    retrieved_context=log_context,
                    llm_response=log_response,
                    llm_tokens_used=None,
                    llm_analysis=(llm_analysis[:5000] if isinstance(llm_analysis, str) and llm_analysis else None),
                )
            except Exception as log_error:
                logger.error(f"Failed to log RAG interaction for user {user_id}: {log_error}")
            # --- КОНЕЦ СОХРАНЕНИЯ ЛОГА ---

            # --- ИНИЦИАЛИЗАЦИЯ DIALOG_STATE ---
            # Сохраняем состояние диалога в user_data
            dialog_state = {
                "topic": user_question, # Исходный вопрос
                "history": [
                    {"role": "user", "text": user_question}, # Исходный вопрос
                    {"role": "assistant", "text": answer_text} # Первая ПОДСКАЗКА
                ],
                "mode": "dialog", # Режимы больше не используются для ветвления
                "rag_context": log_context, # Сохраняем контекст лога для возможного отображения
                "status": "active",
                "session_id" : session_id # Диалог активен
            }
            user_data['rag_dialogue_state'] = dialog_state
            # user_data['step'] уже установлен в 'rag_flow'
            # --- КОНЕЦ ИНИЦИАЛИЗАЦИИ ---

            final_text = prepare_feedback_text(answer_text)

            if interaction_log_id:
                await send_message_with_feedback(
                    context=context,
                    chat_id=user_id,
                    text=final_text,
                    interaction_log_id=interaction_log_id,
                    reply_markup=None
                )
                try:
                    await context.bot.delete_message(chat_id=user_id, message_id=processing_message.message_id)
                except Exception as delete_error:
                    logger.debug(f"Failed to delete processing message for user {user_id}: {delete_error}")
            else:
                # Экранируем HTML в ответе LLM
                await processing_message.edit_text(
                    html.escape(answer_text),
                    reply_markup=None,
                    parse_mode='HTML'
                )
            # --- КОНЕЦ ОТПРАВКИ ОТВЕТА ---

        except Exception as e:
            logger.error(f"Error processing RAG query for user {user_id}: {e}", exc_info=True)
            # Редактируем сообщение об ошибке
            try:
                await processing_message.edit_text(
                    f"❌ Произошла ошибка при обработке вашего запроса: {e}\nПожалуйста, попробуйте позже.",
                    reply_markup=main_menu_keyboard_for(user_id)
                )
            except:
                # Если не удалось отредактировать, отправляем новое
                await msg.reply_text(
                    f"❌ Произошла ошибка при обработке вашего запроса: {e}\nПожалуйста, попробуйте позже.",
                    reply_markup=main_menu_keyboard_for(user_id)
                )
            user_data.pop('step', None) # Сбрасываем состояние в случае ошибки
        return # Завершаем обработку сообщения
    # --- КОНЕЦ НОВОГО ОБРАБОТЧИКА СООБЩЕНИЙ ---

    # --- НОВОЕ: Обработка диалога RAG ---
    if user_data.get('step') == 'rag_flow':
        user_message = msg.text.strip() if msg.text else ""
        
        # Админы освобождены от лимитов
        ensure_admin_whitelist(user_id)
        
        # 🛡️ УРОВЕНЬ 0: ЯЗЫК ВХОДА (до regex-защиты)
        from digital_professor.language_detector import check_user_language
        lang_ok, lang_err, _lang_log = await check_user_language(user_message, user_id)
        if not lang_ok:
            logger.warning(f"Language gate blocked rag_flow message from user {user_id}: {lang_err}")
            await msg.reply_text(
                lang_err,
                reply_markup=main_menu_keyboard_for(user_id)
            )
            return

        # 🛡️ УРОВЕНЬ 1: БАЗОВАЯ ВАЛИДАЦИЯ для продолжения диалога
        from digital_professor.input_validator import validate_user_input, get_injection_risk_level
        
        is_valid, validation_error = validate_user_input(user_message)
        if not is_valid:
            logger.warning(f"Invalid input in rag_flow from user {user_id}: {validation_error}")
            await msg.reply_text(
                validation_error,
                reply_markup=main_menu_keyboard_for(user_id)
            )
            return  # Не сбрасываем диалог, позволяем продолжить
        
        # 🛡️ УРОВЕНЬ 2: RATE LIMITING для продолжения диалога
        allowed, rate_error = rag_continue_limiter.is_allowed(user_id, user_message, 'rag_continue')
        if not allowed:
            logger.warning(f"Rate limit exceeded for user {user_id} (RAG continue): {rate_error}")
            await msg.reply_text(
                rate_error,
                reply_markup=main_menu_keyboard_for(user_id)
            )
            return  # Не сбрасываем диалог
        
        # 🛡️ УРОВЕНЬ 3: LLM-ЦЕНЗОР для продолжения диалога (для ВСЕХ сообщений)
        from digital_professor.llm_censor import check_user_query, get_censor_error_message
        
        is_allowed_by_censor, censor_reason = await check_user_query(user_message, user_id)
        if not is_allowed_by_censor:
            logger.warning(f"LLM Censor blocked message in rag_flow from user {user_id}: {censor_reason}")
            error_message = await get_censor_error_message(censor_reason)
            await msg.reply_text(
                error_message,
                reply_markup=main_menu_keyboard_for(user_id)
            )
            return  # Не сбрасываем диалог
        
        risk_level = get_injection_risk_level(user_message)
        if risk_level in ('medium', 'high'):
            logger.warning(f"Suspicious message in rag_flow from user {user_id} (risk={risk_level}): {user_message[:100]}")

        # 1. Определяем намерение
        # (Пока что упростим: если есть активный диалог, это всегда уточнение)
        dialog_state = user_data.get('rag_dialogue_state')
        
        if not dialog_state:
            # Это не должно произойти, но на всякий случай
            await msg.reply_text("Диалог не найден. Пожалуйста, начните заново.", reply_markup=main_menu_keyboard_for(user_id))
            user_data.clear()
            return

        if user_message.lower() in ["понял", "спасибо", "хватит", "закончить", "/stop"]:
            # Получаем session_id перед очисткой состояния
            dialog_state = user_data.get('rag_dialogue_state')
            session_id = dialog_state.get('session_id') if dialog_state else None
            
            # Очищаем состояние диалога
            user_data.pop('rag_dialogue_state', None)
            user_data.pop('step', None)
            
            # Логируем завершение сессии (опционально)
            if session_id:
                logger.info(f"RAG dialogue session {session_id[:8]} for user {user_id} closed.")
            else:
                logger.debug(f"RAG dialogue closed for user {user_id} (no session_id).")
                
            await msg.reply_text("✅ Отлично! Вопрос закрыт. Хотите задать новый?", reply_markup=main_menu_keyboard_for(user_id))
            return
        # --- КОНЕЦ НОВОГО ---
            
        # Неявные сигналы завершения (через проверку релевантности)
        # ... (логика проверки) ...
        # --- КОНЕЦ ПРОВЕРКИ ЗАВЕРШЕНИЯ ---

        # --- Удалено ветвление по режимам: всегда fresh retrieval + простой ответ ---

        # 2. Добавляем реплику в историю
        dialog_state['history'].append({"role": "user", "text": user_message})
        from digital_professor.database import log_rag_dialogue_message # Новый импорт
        session_id = dialog_state.get('session_id') # Получаем session_id из состояния
        if session_id:
            log_rag_dialogue_message(user_id, session_id, 'user', user_message)
        else:
            logger.warning(f"No session_id found in dialog_state for user {user_id}. Skipping user message log.")
        
        # 2.1 Всегда делаем fresh retrieval для нового сообщения (проверка готовности домена)
        # Определяем домен (если потерян в состоянии — восстановим по moodle_course)
        selected_domain = user_data.get('rag_domain')
        if not selected_domain:
            try:
                u_tmp = get_user(user_id)
            except Exception:
                u_tmp = None
            moodle_course_str_tmp = (u_tmp or {}).get('moodle_course') or ""
            try:
                ids_tmp = {int(x) for x in moodle_course_str_tmp.split(',') if x.strip().isdigit()}
            except Exception:
                ids_tmp = set()
            if 6755 in ids_tmp:
                selected_domain = "math"
            elif 8788 in ids_tmp:
                selected_domain = "vibe"
            user_data['rag_domain'] = selected_domain

        # Выбираем правильный RAG pipeline в зависимости от домена
        rag_pipeline = None
        if selected_domain == "math":
            rag_pipeline = rag_pipeline_math
        elif selected_domain == "vibe":
            rag_pipeline = rag_pipeline_vibe
        
        if rag_pipeline is None or not getattr(rag_pipeline, 'is_initialized', False):
            await msg.reply_text(
                f"❌ Система вопросов по {'математике' if selected_domain == 'math' else 'вайбкодинг'} ещё не готова. Пожалуйста, попробуйте позже.",
                reply_markup=main_menu_keyboard_for(user_id)
            )
            return

        fresh_docs = rag_pipeline.query(user_message)
        fresh_context = "\n\n".join([
            f"--- Источник: {doc.metadata.get('source_file', 'N/A')} ---\n{doc.page_content}"
            for doc in fresh_docs
        ]) if fresh_docs else ""
        dialog_state["rag_context"] = fresh_context

        # 3. Генерация ответа на основе истории и свежего контекста (RAG_TUTOR)
        try:
            from digital_professor.llm import llm_tutor_answer_structured
            llm_out = await llm_tutor_answer_structured(dialog_state['history'], user_message, fresh_docs, user_id=user_id)
            response_text = llm_out.user_answer
            llm_analysis = llm_out.analysis
        except Exception as e:
            logger.error(f"Error in llm_tutor_answer for user {user_id}: {e}", exc_info=True)
            response_text = "Извините, возникла ошибка. Попробуйте повторить вопрос позже."
            llm_analysis = None

        is_allowed_by_censor, censor_reason = await check_user_query(user_message, user_id, model_response=response_text)
        if not is_allowed_by_censor:
            logger.warning(f"LLM Censor blocked rag_flow model output for user {user_id}: {censor_reason}")
            error_message = await get_censor_error_message(censor_reason)
            await msg.reply_text(
                error_message,
                reply_markup=main_menu_keyboard_for(user_id)
            )
            return

        if session_id:
            log_rag_dialogue_message(user_id, session_id, 'assistant', response_text)

        # 4. Лог взаимодействия RAG (per-message)
        interaction_log_id = None
        try:
            interaction_log_id = log_rag_interaction(
                user_id=user_id,
                user_query=user_message,
                retrieved_context=(fresh_context[:5000] if fresh_context else None),
                llm_response=(response_text[:2000] if response_text else None),
                llm_tokens_used=None,
                llm_analysis=(llm_analysis[:5000] if isinstance(llm_analysis, str) and llm_analysis else None),
            )
        except Exception as log_err:
            logger.error(f"Failed to log RAG interaction (rag_flow) for user {user_id}: {log_err}")

        # 5. Обновляем состояние
        dialog_state['history'].append({"role": "assistant", "text": response_text})
        user_data['rag_dialogue_state'] = dialog_state

        menu_keyboard = main_menu_keyboard_for(user_id)
        final_text = prepare_feedback_text(response_text, include_prompt=False)
        if interaction_log_id:
            await send_message_with_feedback(
                context=context,
                chat_id=user_id,
                text=final_text,
                interaction_log_id=interaction_log_id,
                reply_markup=menu_keyboard
            )
        else:
            # Экранируем HTML в ответе LLM
            await msg.reply_text(html.escape(response_text), reply_markup=menu_keyboard, parse_mode='HTML')
        return
    # --- КОНЕЦ НОВОГО ---

# ------------------ CALLBACK (inline buttons) ------------------ Обработка кнопок 
async def button(update: Update, context: CallbackContext):
    # Проверка типа чата: работаем только в приватных чатах
    if not check_private_chat(update):
        await reject_non_private_chat(update, context)
        return
    
    query = update.callback_query
    data = query.data
    user_id = query.from_user.id
    user_data = context.user_data
    logger.info(f"Callback {data} from {user_id}")

    # --- ВХОДНОЙ АНТИСПАМ ДЛЯ CALLBACKS ---
    allowed, rate_error = telegram_callback_limiter.is_allowed(user_id, data or "", "telegram_callback")
    if not allowed:
        try:
            await query.answer(rate_error, show_alert=True)
        except Exception as e:
            logger.debug(f"Failed to answer rate limited callback for user {user_id}: {e}")
        return

    await query.answer()

    if data and data.startswith('feedback_'):
        await handle_feedback_callback(query, context)
        return
    if data == 'start_registration':
        if context.user_data.get('step') != 'awaiting_registration_start':
            await query.answer("Регистрация уже началась.", show_alert=True)
            return
        context.user_data['step'] = 'username'
        await query.edit_message_text(
            "<b>Давай узнаем друг друга получше, как тебя зовут?</b>\n\nУкажите ваше ФИО:",
            parse_mode='HTML'
        )
        return

    # universal: main menu
    if data == 'main_menu':
        await query.edit_message_text(
            f"{WELCOME_INFO_TEXT}\n\nВыберите действие:",
            reply_markup=main_menu_keyboard_for(user_id),
            parse_mode='HTML'
        )
        context.user_data.clear()
        return

    #Измененная логика просмотра информации о пользователе: Добавление информации о KPI и его фактическом выполнении 
    if data == 'info':
        u = get_user(user_id)
        if u:
            def format_value(value):
                if not value:
                    return "<i>не указано</i>"
                return f"<code>{html.escape(str(value))}</code>"

            text = (
                "<b>⚙ Настройки профиля</b>\n"
                "Вот что я знаю о тебе прямо сейчас:\n\n"
                f"• <b>ФИО:</b> {format_value(u.get('username'))}\n"
                f"• <b>Студенческий:</b> {format_value(u.get('student_id'))}\n"
                f"• <b>Часовой пояс:</b> {format_value(u.get('timezone'))}\n"
                f"• <b>Время напоминаний:</b> {format_value(u.get('ping_time'))}\n\n"
                "<blockquote>Используй кнопки ниже, чтобы обновить данные и получать напоминания вовремя.</blockquote>"
            )
            
            # Buttons for settings (Cells 99, 103, 460)
            buttons = [
                [InlineKeyboardButton("Изменить часовой пояс", callback_data='change_timezone')],
                [InlineKeyboardButton("Изменить время напоминаний", callback_data='change_ping_time')],
                [InlineKeyboardButton("Изменить номер студенческого", callback_data='change_student_id')],
                [InlineKeyboardButton("Главное меню", callback_data='main_menu')]
            ]
        else:
            text = (
                "<b>⚙ Настройки недоступны</b>\n"
                "Я не нашёл твоих данных. Запусти /start, чтобы пройти регистрацию."
            )
            buttons = [[InlineKeyboardButton("Главное меню", callback_data='main_menu')]]

        await query.edit_message_text(
            text,
            reply_markup=InlineKeyboardMarkup(buttons),
            parse_mode='HTML'
        )
        return    

    # timezone change (existing flow)
    if data == 'change_timezone':
        keyboard = [
            [InlineKeyboardButton("Екатеринбург", callback_data='Asia/Yekaterinburg')],
            [InlineKeyboardButton("Москва", callback_data='Europe/Moscow')],
        ]
        await query.edit_message_text('Выберите новый часовой пояс:', reply_markup=keyboard_with_main_menu(keyboard))
        context.user_data['step'] = 'change_timezone'
        return

    if data in ('Asia/Yekaterinburg', 'Europe/Moscow'):
        current_step = context.user_data.get('step')
        if current_step in ('timezone', 'timezone_select'):
            context.user_data['timezone'] = data

            if current_step == 'timezone':
                await query.edit_message_text(
                    'Введи время напоминания в формате HH:MM (например, 10:00):'
                )
                context.user_data['step'] = 'ping_time'
                return

            await query.edit_message_text(
                "Введи время, в которое ты хочешь получать напоминания о предстоящих задачах с курса.\n"
                "Укажи в формате HH:MM (например, 10:00)"
            )
            context.user_data['step'] = 'ping_time'
            return

        if current_step == 'change_timezone':
            update_user_timezone(user_id, data)
            await query.edit_message_text(
                f'Часовой пояс успешно изменён на {data}.',
                reply_markup=main_menu_keyboard_for(user_id)
            )
            context.user_data.pop('step', None)
            try:
                from digital_professor.scheduler import reschedule_user_ping
                context.application.create_task(
                    reschedule_user_ping(context.application, user_id)
                )
                logger.info(f"Scheduled reschedule ping job for user {user_id} after timezone change.")
            except Exception as e:
                logger.error(f"Error scheduling reschedule ping job for user {user_id} after timezone change: {e}", exc_info=True)
            return

    if data == 'change_ping_time':
        await query.edit_message_text('Введи новое время напоминания в формате HH:MM (например, 10:00):',
                                      reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Главное меню", callback_data='main_menu')]]))
        context.user_data['step'] = 'change_ping_time'
        return

    # --- NEW: change student id ---
    if data == 'change_student_id':
        await query.edit_message_text(
            'Введи новый номер студенческого билета (6–12 цифр):',
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Главное меню", callback_data='main_menu')]])
        )
        context.user_data['step'] = 'change_student_id'
        return

    
    if data in ('Готов приступить', 'Отложу', 'Не хочу'):
        save_user_response(user_id, data, meta_data={"source": "ping_response"})

        await query.edit_message_text(
            f"Спасибо! Я запомнил твой ответ: \"{data}\"",
            reply_markup=main_menu_keyboard_for(user_id)
        )

        context.user_data.clear()
        return
    
    #Первый пинг:Запуск опроса биг5
    if data == 'big5_start':
        context.user_data['big5_step'] = 'q1'
        await query.edit_message_text(
            "Вопрос 1/3: Что обычно помогает тебе настроиться на работу/учёбу?",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Главное меню", callback_data='main_menu')]])
        )
        return

    if data == 'big5_later':
        await query.edit_message_text("Окей! Вернёмся позже. А пока продолжим как обычно.", reply_markup=main_menu_keyboard_for(user_id))
        return

    # --- ЕЖЕНЕДЕЛЬНЫЙ ОПРОС: обработка ответов ---
    if data == 'weekly_survey_q1_yes':
        context.user_data['weekly_survey_answer1'] = 'да'
        await query.edit_message_text(
            "2. Есть ли пробелы в знаниях по прошлым темам?",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("Да", callback_data='weekly_survey_q2_yes')],
                [InlineKeyboardButton("Нет", callback_data='weekly_survey_q2_no')]
            ])
        )
        return

    if data == 'weekly_survey_q1_no':
        context.user_data['weekly_survey_answer1'] = 'нет'
        await query.edit_message_text(
            "2. Есть ли пробелы в знаниях по прошлым темам?",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("Да", callback_data='weekly_survey_q2_yes')],
                [InlineKeyboardButton("Нет", callback_data='weekly_survey_q2_no')]
            ])
        )
        return

    if data == 'weekly_survey_q2_no':
        from digital_professor.database import save_weekly_ping
        answer1 = context.user_data.get('weekly_survey_answer1', '')
        answer2 = 'нет'
        
        # Получаем данные пользователя
        u = get_user(user_id)
        student_id = (u or {}).get('student_id') if u else None
        username = (u or {}).get('username') or query.from_user.first_name or "Пользователь"
        
        # Сохраняем ответы
        save_weekly_ping(user_id, student_id, username, answer1, answer2)
        
        await query.edit_message_text(
            "Спасибо за ответы! Преподаватель постарается тебе помочь, если ты столкнешься с трудностями.",
            reply_markup=main_menu_keyboard_for(user_id)
        )
        context.user_data.pop('weekly_survey_answer1', None)
        context.user_data.pop('weekly_survey_step', None)
        return

    if data == 'weekly_survey_q2_yes':
        context.user_data['weekly_survey_step'] = 'waiting_topic_name'
        await query.edit_message_text(
            "Пожалуйста, напиши название темы, по которой у тебя возникли сложности:",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("Главное меню", callback_data='main_menu')]
            ])
        )
        return

    if data == 'ask_course_question':
        # Определяем домен RAG по id курса из Moodle, если он уже известен
        u = get_user(user_id)
        moodle_course_str = (u or {}).get('moodle_course') or ""
        try:
            course_ids = {int(x) for x in moodle_course_str.split(',') if x.strip().isdigit()}
        except Exception:
            course_ids = set()

        selected_domain = None
        if 6755 in course_ids:
            selected_domain = "math"
        elif 8788 in course_ids:
            selected_domain = "vibe"

        # Если курса ещё нет — работаем без RAG, напрямую через LLM
        if not course_ids or selected_domain is None:
            context.user_data['step'] = 'llm_free_query'
            context.user_data['llm_feedback_prompt_shown'] = False
            await query.edit_message_text(
                "📝 Пожалуйста, введите ваш вопрос по курсу (без контекстной базы, ответит LLM):",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("❌ Отмена", callback_data='main_menu')
                ]])
            )
            return

        # Для пользователей с известным курсом — проверяем готовность соответствующего RAG-домена
        try:
            # Выбираем правильный RAG pipeline в зависимости от домена
            rag_pipeline = None
            if selected_domain == "math":
                rag_pipeline = rag_pipeline_math
            elif selected_domain == "vibe":
                rag_pipeline = rag_pipeline_vibe
            
            if rag_pipeline is None or not getattr(rag_pipeline, 'is_initialized', False):
                logger.warning(f"RAG pipeline not ready for user {user_id}, domain {selected_domain}")
                await query.edit_message_text(
                    f"🔄 Готовим материалы по {'математике' if selected_domain == 'math' else 'vibecoding'}. Пожалуйста, попробуйте через минуту.",
                    reply_markup=main_menu_keyboard_for(user_id)
                )
                return
        except Exception as e:
            logger.error(f"Error checking RAG domain readiness for user {user_id}, domain {selected_domain}: {e}", exc_info=True)
            await query.edit_message_text(
                "❌ Извините, система вопросов временно недоступна. Пожалуйста, попробуйте позже.",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("Главное меню", callback_data='main_menu')]
                ])
            )
            return

        # Устанавливаем состояние пользователя и домен для RAG
        context.user_data['rag_domain'] = selected_domain
        context.user_data['step'] = 'rag_query'
        await query.edit_message_text(
            "📝 Пожалуйста, введите ваш вопрос по курсу:",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("❌ Отмена", callback_data='main_menu')
            ]])
        )
        return

    # --- НОВОЕ: Обработка запроса к RAG ---
    
    if data == 'rag_query':
        # Это callback для кнопки "Задать вопрос по курсу"
        # Логика обработки запроса теперь в handle_message
        pass # Или можно удалить этот блок, если он больше не нужен
    # --- КОНЕЦ НОВОГО ---
    
# -------- LearningFlow v2: Добавляем логику с топиками
# ---------------- student: learning flow ----------------
    if data in ('start_learning', 'my_courses'):
        courses = get_assigned_courses(user_id)
        existing_ids = {
            c.get('id') for c in courses
            if isinstance(c.get('id'), int)
        }
        show_moodle_courses = True  # Все группы имеют доступ к Moodle курсам
        moodle_courses = await load_moodle_courses_for_menu(user_id, existing_ids) if show_moodle_courses else []
        logger.info(f"moodle_courses: {moodle_courses} for user {user_id}")
        # combined_courses = courses + moodle_courses
        combined_courses = moodle_courses

        if not combined_courses:
            # Специальная клавиатура только с кнопками для пустого списка курсов
            empty_courses_keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("⚙ Настройки", callback_data='info')],
                [InlineKeyboardButton("Главное меню", callback_data='main_menu')]
            ])

            await query.edit_message_text(
                "<b>📚 Мои курсы</b>\n\n"
                "Пока список пуст. Проверь настройки (особенно номер студенческого) или попробуй позже.",
                reply_markup=empty_courses_keyboard,
                parse_mode='HTML'
            )
            return

        intro_lines = [
            "<b>📚 Мои курсы</b>",
            "Выбери программу ниже, чтобы перейти к описанию, темам или заданиям."
        ]
        if show_moodle_courses:
            intro_lines.append("<blockquote>Курсы из Moodle показывают текущий прогресс и следующее задание</blockquote>")

        await query.edit_message_text(
            "\n".join(intro_lines),
            reply_markup=courses_keyboard(combined_courses, user_id),
            parse_mode='HTML'
        )
        return

    # ---- выбор курса -> меню курса (NEW) ----
    if data and data.startswith('select_course:'):
        _, cid = data.split(':', 1)
        cid = int(cid)
        course = get_course(cid)
        if not course:
            await query.edit_message_text("Курс не найден.", reply_markup=main_menu_keyboard_for(user_id))
            return
        
        # Show Course Menu (Cell 186, 188, 189)
        description = course.get('description') or "Описание появится позже."
        title_html = html.escape(course['title'])
        description_html = html.escape(description)
        menu_text = (
            f"<b>📘 {title_html}</b>\n"
            f"{description_html}\n\n"
            "<b>Что дальше?</b>\n"
            "• <b>❓ Вопрос по курсу</b> — получи объяснение или подсказку.\n"
            "• <b>ℹ️ Описание</b> — напоминание о целях и формате курса.\n"
            "• <b>📂 Темы</b> — переход к заданиям и теории."
        )
        
        # Формируем кнопки меню
        buttons = []

        # Добавляем кнопку "Задать вопрос по курсу"
        buttons.append([InlineKeyboardButton("❓ Задать вопрос по курсу", callback_data=f"course_ask:{cid}")])

        buttons.extend([
            [InlineKeyboardButton("ℹ️ Описание", callback_data=f"course_desc:{cid}")],
            [InlineKeyboardButton("📂 Перейти к темам", callback_data=f"course_topics:{cid}")],
            [InlineKeyboardButton("⬅️ К списку курсов", callback_data="my_courses")],
            [InlineKeyboardButton("Главное меню", callback_data='main_menu')]
        ])
        
        await query.edit_message_text(
            menu_text,
            reply_markup=InlineKeyboardMarkup(buttons),
            parse_mode='HTML'
        )
        return

    # ---- Course Description ----
    if data and data.startswith('course_desc:'):
        _, cid = data.split(':', 1)
        cid = int(cid)
        course = get_course(cid)
        if not course:
            await query.edit_message_text("Курс не найден.", reply_markup=main_menu_keyboard_for(user_id))
            return
            
        desc = course.get('description') or "Описание отсутствует."
        title_html = html.escape(course['title'])
        desc_html = html.escape(desc)
        await query.edit_message_text(
            f"<b>📖 Описание курса {title_html}</b>\n\n{desc_html}",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Назад", callback_data=f"select_course:{cid}")]]),
            parse_mode='HTML'
        )
        return

    # ---- Course Ask Question (Direct RAG Trigger) ----
    if data and data.startswith('course_ask:'):
        _, cid = data.split(':', 1)
        cid = int(cid)

        # Set RAG domain and course name based on course ID
        selected_domain = None
        course_name = f'Курс {cid}'  # default

        if cid == 6755:
            selected_domain = "math"
            course_name = "математика"
        elif cid == 8788:
            selected_domain = "vibe"
            course_name = "вайбкодинг"

        if not selected_domain:
             await query.edit_message_text(
                "❌ Для этого курса пока не настроена база знаний.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Назад", callback_data=f"select_course:{cid}")]])
            )
             return

        # Сохраняем информацию о курсе для контекста
        context.user_data['rag_domain'] = selected_domain
        context.user_data['current_course_id'] = cid
        context.user_data['current_course_name'] = course_name
        context.user_data['step'] = 'rag_query'
        
        await query.edit_message_text(
            "Задайте вопрос по курсу:",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Отмена", callback_data='main_menu')]])
        )
        return

    if data and data.startswith('moodle_course:'):
        _, cid_raw = data.split(':', 1)
        try:
            cid = int(cid_raw)
        except ValueError:
            await query.edit_message_text("Курс не найден.", reply_markup=main_menu_keyboard_for(user_id))
            return

        u = get_user(user_id)
        student_number = (u or {}).get('student_id')
        if not student_number:
            await query.edit_message_text(
                "Невозможно получить информацию по курсу: не указан номер студенческого билета.",
                reply_markup=main_menu_keyboard_for(user_id)
            )
            return

        # Проверяем кэшированные данные курса
        cache_key = f"moodle_course_{cid}"
        cached_data = user_data.get(cache_key)
        cache_validity_minutes = 10  # Кэш валиден 10 минут

        if cached_data:
            cached_time = cached_data.get('timestamp')
            if cached_time:
                cache_age = (datetime.now() - datetime.fromisoformat(cached_time)).total_seconds() / 60
                if cache_age < cache_validity_minutes:
                    # Используем кэшированные данные
                    text = cached_data['text']
                    buttons = cached_data['buttons']
                    try:
                        await query.edit_message_text(
                            text,
                            reply_markup=InlineKeyboardMarkup(buttons),
                            parse_mode='HTML'
                        )
                    except Exception as e:
                        logger.error(f"Failed to display cached Moodle course data for user {user_id}, course {cid}: {e}")
                        # Если не удалось отобразить кэш, продолжаем с загрузкой
                    else:
                        return  # Успешно отобразили кэш, выходим

        # Кэша нет или он устарел - загружаем новые данные
        placeholder_markup = InlineKeyboardMarkup([[InlineKeyboardButton("Главное меню", callback_data='main_menu')]])
        try:
            await query.edit_message_text("⏳ Загружаю информацию по курсу...\n\nМожет занять около минуты", reply_markup=placeholder_markup)
        except Exception as e:
            logger.error(f"Failed to show loading message for user {user_id}, course {cid}: {e}")
            await query.answer("Не удалось обновить сообщение. Попробуйте позже.")
            return

        message = query.message
        if not message:
            logger.error(f"No message associated with callback moodle_course for user {user_id}")
            return

        try:
            # Запускаем загрузку данных о курсе
            context.application.create_task(
                fetch_and_cache_moodle_course(
                    context.application,
                    message.chat_id,
                    message.message_id,
                    user_id,
                    cid,
                    student_number,
                    context  # Передаем context для доступа к user_data
                )
            )
        except Exception as e:
            logger.error(f"Failed to schedule Moodle course fetch for user {user_id}, course {cid}: {e}")
            await query.edit_message_text(
                "Не удалось запустить загрузку информации. Попробуйте позже.",
                reply_markup=main_menu_keyboard_for(user_id)
            )
        return

    # ---- Course Topics (Old select_course logic) ----
    if data and data.startswith('course_topics:'):
        _, cid = data.split(':', 1)
        cid = int(cid)
        course = get_course(cid)
        if not course:
            await query.edit_message_text("Курс не найден.", reply_markup=main_menu_keyboard_for(user_id))
            return
        
        topics = get_topics_for_course(cid)
        if not topics:
            await query.edit_message_text("В этом курсе пока нет тем.",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("Назад", callback_data=f'select_course:{cid}')],
                    [InlineKeyboardButton("Главное меню", callback_data='main_menu')]
                ]))
            return

        await query.edit_message_text(f"Темы курса: {course['title']}",
                                      reply_markup=topics_keyboard(topics, cid, user_id))
        return


    # ---- выбор топика -> теория + задания ----
    if data and data.startswith('select_topic:'):
        _, tid = data.split(':', 1)
        tid = int(tid)
        topic = get_topic(tid)
        if not topic:
            await query.edit_message_text("Тема не найдена.", reply_markup=main_menu_keyboard_for(user_id))
            return

        tasks = get_tasks_for_topic(tid)
        if not tasks:
            await query.edit_message_text("В этой теме нет заданий.",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("Назад (курсы)", callback_data='my_courses')],
                    [InlineKeyboardButton("Главное меню", callback_data='main_menu')]
                ]))
            return

        # Получаем ссылку на теорию из Telegraph
        telegraph_link = topic.get("telegraph_link")
        
        # Создаем базовую клавиатуру с заданиями
        tasks_kb = tasks_keyboard(tasks, query.from_user.id)
        
        # Формируем финальную клавиатуру
        final_buttons = []
        
        # Добавляем кнопку "Теория", если есть ссылка
        if telegraph_link:
            final_buttons.append([InlineKeyboardButton("📖 Теория", url=telegraph_link)])
        
        # Добавляем все кнопки заданий
        for row in tasks_kb.inline_keyboard:
            final_buttons.append(row)

        # Отправляем сообщение
        await query.edit_message_text(
            f"Тема: {topic['title']}\n\nВыберите задание:",
            reply_markup=InlineKeyboardMarkup(final_buttons)
        )
        return

    # ---- запуск задания ----
    if data and data.startswith('start_task:'):
        _, tid = data.split(':', 1)
        tid = int(tid)
        task = get_task(tid)
        if not task:
            await query.edit_message_text("Задание не найдено.", reply_markup=main_menu_keyboard_for(user_id))
            return

        ttype = task['task_type']
        if ttype == 'test':
            # prepare questions (random order)
            questions = get_test_questions_with_choices(tid)
            if not questions:
                await query.edit_message_text("В тесте нет вопросов.", reply_markup=main_menu_keyboard_for(user_id))
                return
            random.shuffle(questions)
            # store session in user_data
            context.user_data['test_session'] = {
                'task_id': tid,
                'questions': questions,
                'index': 0,
                'correct': 0,
                'total': len(questions)
            }
            # send first question
            q0 = questions[0]
            await query.edit_message_text(f"Вопрос 1/{len(questions)}:\n\n{q0['question_text']}",
                                          reply_markup=question_choices_keyboard(tid, q0))
            return

        else:
            # open or practical
            context.user_data['awaiting_task_result'] = {'task_id': tid, 'task_type': ttype}
            await query.edit_message_text(
                f"Отправьте ответ на задание:\n\n{task.get('title')}\n\n{task.get('description')}\n\nОтвет принимается только текстом\n\nЗадание считается выполненным при оценке 8 или выше",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Главное меню", callback_data='main_menu')]])
            )
            return
        
# handle test answer callback
    if data and data.startswith('answer:'):
        # format: answer:{task_id}:{question_id}:{choice_id}
        try:
            _, task_id_s, question_id_s, choice_id_s = data.split(':')
            task_id = int(task_id_s); question_id = int(question_id_s); choice_id = int(choice_id_s)
        except Exception:
            await query.edit_message_text("Неверные данные ответа.", reply_markup=main_menu_keyboard_for(user_id))
            return

        # check correctness
        choice = get_choice(choice_id)
        correct = bool(choice and choice.get('is_correct'))
        feedback = "Правильно!" if correct else "Неправильно."
        try:
            await query.edit_message_text(f"{feedback}\n\n(нажатый вариант был сохранён)")
        except Exception:
            # edit may fail if message already changed — ignore
            pass

        # update session
        sess = context.user_data.get('test_session')
        if not sess or sess.get('task_id') != task_id:
            # no session (maybe restarted) — just finish
            await query.message.reply_text("Сессия теста не найдена или истекла. Вернитесь в главное меню.", reply_markup=main_menu_keyboard_for(user_id))
            context.user_data.pop('test_session', None)
            return

        sess['index'] += 1
        if correct:
            sess['correct'] += 1

        if sess['index'] >= sess['total']:
        #обновленная логика подсчета результатов теста
            total = sess['total']; correct_count = sess['correct']
            perc = int(correct_count / total * 100)
            await query.message.reply_text(f"Тест завершён.\nРезультат: {correct_count}/{total} ({perc}%)\n\nТест считается выполненным при достижении 60%",
                                        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Главное меню", callback_data='main_menu')]]))
            # Сохраняем результат: score = perc (процент), feedback можно сгенерировать или None
            save_task_result(
                task_id=task_id,
                user_id=user_id,
                question_id=None,  # Для теста целиком
                choice_id=None,
                answer_text=None,  # Или сериализованные ответы, если нужно
                file_id=None,
                score=perc,
                feedback=f"Правильных ответов: {correct_count}/{total}"
            )
            # NEW: обновляем weekly_progress
            # --- FIX: получить course_id через get_task ---
            if perc >= 60:
                task = get_task(task_id)
                if task:
                    update_weekly_progress(
                        user_id,
                        task['course_id'],
                        kpi=get_kpi_for_user_course(user_id, task['course_id'])
                    )
                link_last_response_to_task(user_id, task_id, datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"))
                logger.info(f"User {user_id} finished test {task_id}: {perc}% ({correct_count}/{total})")

            # cleanup
            context.user_data.pop('test_session', None)
            return
        
        else:
            # send next question
            nxt = sess['questions'][sess['index']]
            await context.bot.send_message(chat_id=user_id,
                                           text=f"Вопрос {sess['index']+1}/{sess['total']}:\n\n{nxt['question_text']}",
                                           reply_markup=question_choices_keyboard(task_id, nxt))
            return

    # unknown callback
    await query.edit_message_text("Неизвестная команда. Используй Главное меню.", reply_markup=main_menu_keyboard_for(user_id))


#-----asign_course v2
async def assign_course_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Обработчик команды /assign_course course_id user_id.
    Назначает курс пользователю, если команду выполняет администратор.
    """
    # Проверка типа чата: работаем только в приватных чатах
    if not check_private_chat(update):
        await reject_non_private_chat(update, context)
        return
    
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    
    # Проверяем, является ли пользователь администратором
    if not is_admin(user_id):
        await context.bot.send_message(
            chat_id=chat_id,
            text="У вас нет прав для выполнения этой команды. Только администраторы могут назначать курсы."
        )
        logger.warning(f"Пользователь {user_id} попытался выполнить /assign_course без прав администратора.")
        return
    
    # Проверяем, переданы ли аргументы
    if len(context.args) != 3:
        await context.bot.send_message(
            chat_id=chat_id,
            text="Использование: /assign_course <course_id> <user_id> <n_times>\nПример: /assign_course 1 12345 3"
        )
        return
    
    try:
        course_id = int(context.args[0])
        target_user_id = int(context.args[1])
        n_times = int(context.args[2])
    except ValueError:
        await context.bot.send_message(
            chat_id=chat_id,
            text="Ошибка: course_id, user_id и n_times должны быть числами."
        )
        return
    
    # Вызываем функцию назначения курса
    if assign_course(course_id, target_user_id, n_times):
        await context.bot.send_message(
            chat_id=chat_id,
            text=f"Курс {course_id} успешно назначен пользователю {target_user_id}. Целевое количество решенных заданий в неделю: {n_times}"
        )
    else:
        await context.bot.send_message(
            chat_id=chat_id,
            text=f"Не удалось назначить курс {course_id} пользователю {target_user_id}. Проверьте, существуют ли курс и пользователь."
        )

#-------- Универсальная логика создания курсов---------------------
# ensure creators module is imported and discovered
discover_creators_from_module("course_creator")

logger = logging.getLogger(__name__)

def _format_available():
    lines = []
    for k, meta_data in sorted(COURSE_CREATORS.items()):
        title = meta_data.get("title", k)
        desc = meta_data.get("description", "")
        line = f"{k} — {title}"
        if desc:
            # короткий фрагмент описания
            line += f" ({desc.splitlines()[0][:80]})"
        lines.append(line)
    return "\n".join(lines) if lines else "Пока нет зарегистрированных курсов."

# Команда для создания курсов: добавлена теория (создается асинхронно через телеграф апи)
async def create_course_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Проверка типа чата: работаем только в приватных чатах
    if not check_private_chat(update):
        await reject_non_private_chat(update, context)
        return
    
    user_id = update.effective_user.id
    if not is_admin(user_id):
        await update.message.reply_text("⛔ У вас нет прав для этой команды.")
        return

    # без аргументов — показываем список доступных курсов
    if not context.args:
        text = "Использование: /create_course <key>\n\nДоступные курсы:\n" + _format_available()
        await update.message.reply_text(text)
        return

    key = context.args[0]
    meta_data = COURSE_CREATORS.get(key)
    if not meta_data:
        await update.message.reply_text(f"❌ Неизвестный курс '{key}'. Доступные: {', '.join(COURSE_CREATORS.keys())}")
        return

    creator = meta_data["func"]
    await update.message.reply_text(f"⏳ Создаю курс '{meta_data.get('title', key)}'...")

    try:
        if inspect.iscoroutinefunction(creator):
            # если кто-то сделал асинхронный creator — await его напрямую
            result = await creator(created_by=user_id)
            # Обрабатываем оба варианта возврата: только course_id или (course_id, topics)
            if isinstance(result, tuple) and len(result) == 2:
                course_id, topics = result
                # Запускаем асинхронную генерацию ссылок
                asyncio.create_task(generate_telegraph_links_for_course(course_id, topics))
            else:
                course_id = result
        else:
            # блокирующая синхронная функция — запустим в threadpool чтобы не блокировать loop
            loop = asyncio.get_running_loop()
            # передаём positional arg: created_by — большинство твоих функций принимает first arg created_by
            result = await loop.run_in_executor(None, creator, user_id)
            
            # Обрабатываем оба варианта возврата: только course_id или (course_id, topics)
            if isinstance(result, tuple) and len(result) == 2:
                course_id, topics = result
                # ✅ Теперь мы в асинхронном контексте — можем запускать асинхронную задачу
                asyncio.create_task(generate_telegraph_links_for_course(course_id, topics))
            else:
                course_id = result

    except Exception as exc:
        logger.exception("Error creating course %s", key)
        await update.message.reply_text(f"❌ Ошибка при создании курса: {exc}")
        return

    await update.message.reply_text(f"✅ Курс '{meta_data.get('title', key)}' создан. id: {course_id}\n⏳ Генерация ссылок на теорию запущена в фоне.")

async def admin_set_ping_schedule(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Команда для администратора: /setpings <user_id> <параметры>
    
    Параметры могут быть:
    1. Число (1-7):
       - 1: Понедельник
       - 2: Вторник, Четверг (или каждый будний, если логика другая)
       - 3: Понедельник, Среда, Пятница
       - 5: Каждый будний день
       - 7: Каждый день недели
    2. Список дней: monday, wednesday, friday
    3. Смешанный вариант: 3 monday (игнорирует число, использует список)
    
    Примеры:
    /setpings 123456789 3
    /setpings 123456789 monday, wednesday, friday
    /setpings 123456789 5
    /setpings 123456789 7
    /setpings 123456789 monday wednesday friday
    """
    # Проверка типа чата: работаем только в приватных чатах
    if not check_private_chat(update):
        await reject_non_private_chat(update, context)
        return
    
    # Локальные импорты для избежания циклических зависимостей на этапе инициализации
    from digital_professor.database import is_user_admin, set_user_ping_schedule, get_user 

    admin_user_id = update.effective_user.id
    if not is_user_admin(admin_user_id):
        await update.message.reply_text("У вас нет прав для выполнения этой команды.")
        return

    args = context.args
    if len(args) < 2:
        await update.message.reply_text(
            "Использование: /setpings <user_id> <параметры>\n"
            "Параметры:\n"
            "- Список дней: monday, tuesday, wednesday, thursday, friday, saturday, sunday\n"
            "Примеры:\n"
            "/setpings 123456789 monday wednesday friday\n"
            "/setpings 123456789 monday"
        )
        return

    try:
        target_user_id = int(args[0])
        
        # --- Проверка существования пользователя ---
        user_data = get_user(target_user_id)
        if not user_data:
             await update.message.reply_text(f"Пользователь с ID {target_user_id} не найден в базе данных.")
             return
        # --- Конец проверки ---

        # Анализируем оставшиеся аргументы
        rest_args = args[1:]
        pings_per_week_arg = None
        ping_days_arg_list = []

        # Пытаемся определить, начинается ли список аргументов с числа
        try:
            pings_per_week_arg = int(rest_args[0])
            # Если число было, то остальные аргументы - это дни (если есть)
            ping_days_arg_list = rest_args[1:] if len(rest_args) > 1 else []
        except ValueError:
            # Первый аргумент не число, значит, это список дней
            ping_days_arg_list = rest_args

        # Обрабатываем аргументы
        final_pings_per_week = 5 # Дефолтное значение частоты
        final_ping_days_list = None # Дефолт: использовать частоту

        if ping_days_arg_list:
            # Обрабатываем список дней
            # Объединяем аргументы в одну строку и разбиваем по запятым и пробелам
            days_input = ' '.join(ping_days_arg_list)
            # Простое разделение по пробелам и запятым, с очисткой
            import re
            # Находит последовательности букв
            raw_days = re.findall(r'[a-zA-Zа-яА-Я]+', days_input) 
            # Нормализуем: берем первые 3 буквы, приводим к нижнему регистру
            normalized_days = [day[:3].lower() for day in raw_days]
            # Фильтруем по допустимым дням (можно расширить до всех дней)
            valid_days_short = {'mon', 'tue', 'wed', 'thu', 'fri', 'sat', 'sun'}
            final_ping_days_list = [day for day in normalized_days if day in valid_days_short]
            
            if not final_ping_days_list:
                 await update.message.reply_text("Не удалось распознать дни недели. Используйте названия на английском (monday, tue) или русском (понедельник, вт).")
                 return

            # Если дни указаны, частота не используется
            final_pings_per_week = 0 # Специальный маркер
            
        elif pings_per_week_arg is not None:
            # Используем число как частоту
            if not (1 <= pings_per_week_arg <= 7):
                await update.message.reply_text("Количество напоминаний в неделю должно быть от 1 до 7.")
                return
            final_pings_per_week = pings_per_week_arg
            # final_ping_days_list остается None

        # --- Вызов функции обновления расписания в БД ---
        # Передаем как частоту, так и список дней (одно из них может быть None/default)
        set_user_ping_schedule(
            user_id=target_user_id, 
            pings_per_week=final_pings_per_week, 
            ping_days_list=final_ping_days_list
        )
        # --- Конец вызова БД ---
        
        # --- Формирование ответа пользователю ---
        schedule_desc = ""
        if final_ping_days_list is not None: # Если список дней был задан и распознан

            day_names_rus = {
                'mon': 'Пн', 'tue': 'Вт', 'wed': 'Ср',
                'thu': 'Чт', 'fri': 'Пт', 'sat': 'Сб', 'sun': 'Вс'
            }
            # Формируем строку с днями
            days_display = ', '.join(day_names_rus.get(day, day) for day in final_ping_days_list)
            schedule_desc = f"Дни недели: {days_display}"
        else:
            # Описание для режима частоты
            freq_desc_map = {
                1: "1 раз в неделю (обычно Пн)",
                2: "2 раза в неделю (обычно Вт, Чт)",
                3: "3 раза в неделю (обычно Пн, Ср, Пт)",
                5: "Каждый будний день (Пн-Пт)",
                7: "Каждый день недели"
            }
            schedule_desc = f"Частота: {freq_desc_map.get(final_pings_per_week, f'{final_pings_per_week} раз в неделю')}"
        try:
            from digital_professor.scheduler import reschedule_user_ping
            # Создаем задачу для перепланирования. Это асинхронная операция.
            # application доступен как context.application
            context.application.create_task(
                reschedule_user_ping(context.application, target_user_id)
            )
            logger.info(f"Scheduled reschedule ping job for user {target_user_id} after admin change.")
        except ImportError as ie:
            logger.error(f"Failed to import scheduler.reschedule_user_ping: {ie}")
            await update.message.reply_text(
                f"⚠️ Расписание для пользователя {target_user_id} обновлено, но перепланирование задачи напоминания не удалось из-за внутренней ошибки импорта. "
                f"Пожалуйста, перезапустите бота или вручную проверьте расписание."
            )
            # Не прерываем основной поток выполнения, просто логируем ошибку
        except Exception as e:
            logger.error(f"Error scheduling reschedule ping job for user {target_user_id}: {e}", exc_info=True)
            await update.message.reply_text(
                f"⚠️ Расписание для пользователя {target_user_id} обновлено, но возникла ошибка при попытке перепланировать задачу напоминания. "
                f"Пожалуйста, проверьте логи. Пользователь может не получать напоминания по новому расписанию до перезапуска бота."
            )
        # --- КОНЕЦ НОВОГО ---            
        await update.message.reply_text(
            f"✅ Расписание напоминаний для пользователя {target_user_id} обновлено:\n"
            f"{schedule_desc}\n"
        )
        logger.info(
            f"Admin {admin_user_id} set ping schedule for user {target_user_id}: "
            f"freq={final_pings_per_week}, days={final_ping_days_list}"
        )
        # --- Конец формирования ответа ---

    except ValueError as ve:
        logger.warning(f"ValueError in admin_set_ping_schedule for admin {admin_user_id}: {ve}")
        await update.message.reply_text(
            "❌ Некорректный формат аргументов.\n"
            "Убедитесь, что ID пользователя - это число, а дни указаны правильно (например, monday, wednesday или пн, ср)."
        )
    except Exception as e:
        logger.error(f"Unexpected error in admin_set_ping_schedule for admin {admin_user_id}: {e}", exc_info=True)
        await update.message.reply_text(
            "❌ Произошла непредвиденная ошибка при установке расписания. Пожалуйста, попробуйте позже или обратитесь к разработчику."
        )

async def admin_test_ping(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Временная команда для администратора: /testping <user_id>
    Принудительно вызывает send_reminder для указанного пользователя,
    позволяя мгновенно протестировать логику отправки пинга.
    """
    # Проверка типа чата: работаем только в приватных чатах
    if not check_private_chat(update):
        await reject_non_private_chat(update, context)
        return
    
    # Локальные импорты для избежания циклических зависимостей
    from digital_professor.utils import send_reminder
    # from digital_professor.database import is_user_admin, get_user
    from digital_professor.database import is_user_admin, get_user
    admin_user_id = update.effective_user.id
    logger.info(f"Admin {admin_user_id} initiated /testping command.")

    if not is_user_admin(admin_user_id):
        await update.message.reply_text("У вас нет прав для выполнения этой команды.")
        logger.warning(f"User {admin_user_id} denied access to /testping.")
        return

    args = context.args
    if len(args) != 1:
        await update.message.reply_text("Использование: /testping <user_id>")
        logger.debug(f"Admin {admin_user_id} provided incorrect number of arguments: {len(args)}.")
        return

    try:
        target_user_id = int(args[0])
    except ValueError:
        await update.message.reply_text("Некорректный формат ID пользователя. Пожалуйста, используйте числовое значение.")
        logger.warning(f"Admin {admin_user_id} provided non-integer user_id: {args[0]}.")
        return

    # --- НОВОЕ: Проверка существования пользователя ---
    user_data = get_user(target_user_id)
    if not user_data:
        await update.message.reply_text(f"Пользователь с ID {target_user_id} не найден в базе данных.")
        logger.info(f"Admin {admin_user_id} tried to test ping for non-existent user {target_user_id}.")
        return
    # --- КОНЕЦ НОВОГО ---

    try:
        # Отправляем сообщение админу, что проверка началась
        await update.message.reply_text(f"Запуск проверки и потенциальной отправки напоминания для пользователя {target_user_id}...")
        logger.info(f"Initiating send_reminder for user {target_user_id} by admin {admin_user_id}.")

        # --- ВАЖНО: Вызов send_reminder ---
        # send_reminder сама содержит всю логику: проверка расписания, генерация текста, отправка.
        # Мы вызываем её напрямую, как если бы это сделал планировщик.
        await send_reminder(context, target_user_id)
        # --- Конец вызова ---

        await update.message.reply_text(
            f"✅ Проверка для пользователя {target_user_id} завершена. "
            f"Если условия были соблюдены (день, время напоминания), напоминание было отправлено."
        )
        logger.info(f"send_reminder executed for user {target_user_id} by admin {admin_user_id}.")

    except Exception as e:
        error_msg = f"❌ Ошибка при выполнении проверки для пользователя {target_user_id}: {e}"
        logger.error(error_msg, exc_info=True) # exc_info=True для полного трейса ошибки
        await update.message.reply_text(error_msg)


async def admin_force_ping(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Админ-команда /forceping <user_id>:
    принудительно отправляет пинг пользователю, игнорируя дневные ограничения.
    """
    if not check_private_chat(update):
        await reject_non_private_chat(update, context)
        return

    from digital_professor.utils import send_reminder
    from digital_professor.database import is_user_admin, get_user

    admin_user_id = update.effective_user.id
    logger.info(f"Admin {admin_user_id} initiated /forceping command.")

    if not is_user_admin(admin_user_id):
        await update.message.reply_text("У вас нет прав для выполнения этой команды.")
        logger.warning(f"User {admin_user_id} denied access to /forceping.")
        return

    args = context.args
    if len(args) != 1:
        await update.message.reply_text("Использование: /forceping <user_id>")
        logger.debug(f"Admin {admin_user_id} provided incorrect number of arguments for /forceping: {len(args)}.")
        return

    try:
        target_user_id = int(args[0])
    except ValueError:
        await update.message.reply_text("Некорректный формат ID пользователя. Пожалуйста, используйте числовое значение.")
        logger.warning(f"Admin {admin_user_id} provided non-integer user_id for /forceping: {args[0]}.")
        return

    user_data = get_user(target_user_id)
    if not user_data:
        await update.message.reply_text(f"Пользователь с ID {target_user_id} не найден в базе данных.")
        logger.info(f"Admin {admin_user_id} tried to force ping non-existent user {target_user_id}.")
        return

    try:
        await update.message.reply_text(
            f"🚨 Принудительная отправка напоминания пользователю {target_user_id}. "
            "Ограничения по расписанию игнорируются."
        )
        logger.info(f"Force send_reminder for user {target_user_id} by admin {admin_user_id}.")
        await send_reminder(context, target_user_id, force=True)
        await update.message.reply_text(
            f"✅ Принудительное напоминание для пользователя {target_user_id} выполнено."
        )
    except Exception as e:
        error_msg = f"❌ Ошибка при принудительной отправке напоминания пользователю {target_user_id}: {e}"
        logger.error(error_msg, exc_info=True)
        await update.message.reply_text(error_msg)


async def admin_test_weekly_survey(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Админ-команда /testweeklysurvey <user_id>:
    принудительно отправляет еженедельный опрос пользователю для тестирования.
    """
    if not check_private_chat(update):
        await reject_non_private_chat(update, context)
        return

    from digital_professor.utils import send_weekly_survey
    from digital_professor.database import is_user_admin, get_user

    admin_user_id = update.effective_user.id
    logger.info(f"Admin {admin_user_id} initiated /testweeklysurvey command.")

    if not is_user_admin(admin_user_id):
        await update.message.reply_text("У вас нет прав для выполнения этой команды.")
        logger.warning(f"User {admin_user_id} denied access to /testweeklysurvey.")
        return

    args = context.args
    if len(args) != 1:
        await update.message.reply_text("Использование: /testweeklysurvey <user_id>")
        logger.debug(f"Admin {admin_user_id} provided incorrect number of arguments for /testweeklysurvey: {len(args)}.")
        return

    try:
        target_user_id = int(args[0])
    except ValueError:
        await update.message.reply_text("Некорректный формат ID пользователя. Пожалуйста, используйте числовое значение.")
        logger.warning(f"Admin {admin_user_id} provided non-integer user_id for /testweeklysurvey: {args[0]}.")
        return

    user_data = get_user(target_user_id)
    if not user_data:
        await update.message.reply_text(f"Пользователь с ID {target_user_id} не найден в базе данных.")
        logger.info(f"Admin {admin_user_id} tried to test weekly survey for non-existent user {target_user_id}.")
        return

    try:
        username = user_data.get('username') or f"Пользователь {target_user_id}"
        await update.message.reply_text(
            f"🚨 Принудительная отправка еженедельного опроса пользователю {target_user_id}."
        )
        logger.info(f"Force send_weekly_survey for user {target_user_id} by admin {admin_user_id}.")
        await send_weekly_survey(context, target_user_id, username)
        await update.message.reply_text(
            f"✅ Еженедельный опрос для пользователя {target_user_id} отправлен."
        )
    except Exception as e:
        error_msg = f"❌ Ошибка при отправке еженедельного опроса пользователю {target_user_id}: {e}"
        logger.error(error_msg, exc_info=True)
        await update.message.reply_text(error_msg)


async def admin_set_ping_schedule_all(update: Update, context: CallbackContext):

    """

    Команда для администратора: /setpingsall <параметры>

    Назначает одно расписание пингов ВСЕМ пользователям в БД.

    

    Параметры могут быть:

    1. Число (1-7):

       - 1: Понедельник

       - 2: Вторник, Четверг (или каждый будний, если логика другая)

       - 3: Понедельник, Среда, Пятница

       - 5: Каждый будний день

       - 7: Каждый день недели

    2. Список дней: monday, wednesday, friday

    3. Смешанный вариант: 3 monday (игнорирует число, использует список)

    

    Примеры:

    /setpingsall 3

    /setpingsall monday, wednesday, friday

    /setpingsall 5

    /setpingsall 7

    /setpingsall monday wednesday friday

    """
    # Проверка типа чата: работаем только в приватных чатах
    if not check_private_chat(update):
        await reject_non_private_chat(update, context)
        return

    # from digital_professor.database import is_user_admin, set_user_ping_schedule, get_users # <-- Уже импортированы

    

    admin_user_id = update.effective_user.id

    if not is_user_admin(admin_user_id):

        await update.message.reply_text("У вас нет прав для выполнения этой команды.")

        return


    args = context.args

    if len(args) < 1:

        await update.message.reply_text(

            "Использование: /setpingsall <параметры>\n"

            "Параметры:\n"

            "- Число от 1 до 7 (частота: 1=Пн, 2=Вт/Чт, 3=Пн/Ср/Пт, 5=будни, 7=все дни)\n"

            "- Список дней: monday, tuesday, wednesday, thursday, friday, saturday, sunday\n"

            "Примеры:\n"

            "/setpingsall 3\n"

            "/setpingsall monday, wednesday, friday\n"

            "/setpingsall 5\n"

            "/setpingsall 7\n"

            "/setpingsall monday wednesday friday\n"

        )

        return


    try:

        # Анализируем аргументы

        rest_args = args

        pings_per_week_arg = None

        ping_days_arg_list = []


        # Пытаемся определить, начинается ли список аргументов с числа

        try:

            pings_per_week_arg = int(rest_args[0])

            # Если число было, то остальные аргументы - это дни (если есть)

            ping_days_arg_list = rest_args[1:] if len(rest_args) > 1 else []

        except ValueError:

            # Первый аргумент не число, значит, это список дней

            ping_days_arg_list = rest_args


        # Обрабатываем аргументы

        final_pings_per_week = 5 # Дефолтное значение частоты (будни)

        final_ping_days_list = None # Дефолт: использовать частоту


        if ping_days_arg_list:

            # Обрабатываем список дней

            # Объединяем аргументы в одну строку и разбиваем по запятым и пробелам

            import re

            days_input = ' '.join(ping_days_arg_list)

            # Простое разделение по пробелам и запятым, с очисткой

            raw_days = re.findall(r'[a-zA-Zа-яА-Я]+', days_input) 

            # Нормализуем: берем первые 3 буквы, приводим к нижнему регистру

            normalized_days = [day[:3].lower() for day in raw_days]

            # Фильтруем по допустимым дням (можно расширить до всех дней)

            valid_days_short = {'mon', 'tue', 'wed', 'thu', 'fri', 'sat', 'sun'}

            final_ping_days_list = [day for day in normalized_days if day in valid_days_short]

            

            if not final_ping_days_list:

                 await update.message.reply_text("Не удалось распознать дни недели. Используйте названия на английском (monday, tue) или русском (понедельник, вт).")

                 return


            # Если дни указаны, частота не используется

            final_pings_per_week = 0 # Специальный маркер, но database.py сама установит 0 или NULL

        elif pings_per_week_arg is not None:

            # Используем число как частоту

            if not (1 <= pings_per_week_arg <= 7):

                await update.message.reply_text("Количество напоминаний в неделю должно быть от 1 до 7.")

                return

            final_pings_per_week = pings_per_week_arg

            # final_ping_days_list остается None


        # --- НОВОЕ: Получаем всех пользователей и устанавливаем им расписание ---

        logger.info(f"Admin {admin_user_id} initiated /setpingsall with freq={final_pings_per_week}, days={final_ping_days_list}")

        

        all_users = get_users() # [(user_id, timezone, ping_time), ...]

        logger.info(f"Found {len(all_users)} users in database for /setpingsall.")

        

        success_count = 0

        error_count = 0

        for user_id, _, _ in all_users:

            try:

                # Вызываем существующую функцию для каждого пользователя

                set_user_ping_schedule(user_id, final_pings_per_week, final_ping_days_list)

                success_count += 1

                logger.debug(f"Set ping schedule for user {user_id}: freq={final_pings_per_week}, days={final_ping_days_list}")

            except Exception as e:

                error_count += 1

                logger.error(f"Failed to set ping schedule for user {user_id}: {e}")

        

        # --- НОВОЕ: Перепланирование задач пинга для всех пользователей ---

        # После массового изменения расписания нужно перепланировать задачи

        try:

            from digital_professor.scheduler import reschedule_all_user_pings # Предполагается, что функция существует

            # Создаем задачу для перепланирования всех пингов

            context.application.create_task(

                reschedule_all_user_pings(context.application)

            )

            logger.info(f"Scheduled reschedule all ping jobs after /setpingsall.")

        except Exception as e:

            logger.error(f"Error scheduling reschedule all ping jobs after /setpingsall: {e}", exc_info=True)

            # Ошибка логируется, но не мешает основному флоу

        # --- КОНЕЦ НОВОГО ---

        

        schedule_desc = ""

        if final_ping_days_list:

            day_names_rus = {

                'mon': 'Пн', 'tue': 'Вт', 'wed': 'Ср',

                'thu': 'Чт', 'fri': 'Пт', 'sat': 'Сб', 'sun': 'Вс'

            }

            days_display = ', '.join(day_names_rus.get(day, day) for day in final_ping_days_list)

            schedule_desc = f"Дни недели: {days_display}"

        else:

            freq_desc_map = {

                1: "1 раз в неделю (обычно Пн)",

                2: "2 раза в неделю (обычно Вт, Чт)",

                3: "3 раза в неделю (обычно Пн, Ср, Пт)",

                5: "Каждый будний день (Пн-Пт)",

                7: "Каждый день недели"

            }

            schedule_desc = f"Частота: {freq_desc_map.get(final_pings_per_week, f'{final_pings_per_week} раз в неделю')}"


        await update.message.reply_text(

            f"✅ Расписание напоминаний обновлено для ВСЕХ пользователей ({success_count} успешно, {error_count} с ошибками):\n"

            f"{schedule_desc}\n"

        )

        logger.info(f"Admin {admin_user_id} set ping schedule for ALL users: freq={final_pings_per_week}, days={final_ping_days_list}. Success: {success_count}, Errors: {error_count}.")


    except ValueError as ve:

        logger.warning(f"ValueError in admin_set_ping_schedule_all for admin {admin_user_id}: {ve}")

        await update.message.reply_text(

            "❌ Некорректный формат аргументов.\n"

            "Убедитесь, что количество напоминаний - это число, а дни указаны правильно (например, monday, wednesday или пн, ср)."

        )

    except Exception as e:

        logger.error(f"Unexpected error in admin_set_ping_schedule_all for admin {admin_user_id}: {e}", exc_info=True)

        await update.message.reply_text(

            "❌ Произошла непредвиденная ошибка при установке расписания для всех пользователей. Пожалуйста, попробуйте позже или обратитесь к разработчику."

        )
