# utils.py
import logging
logger = logging.getLogger(__name__)
from digital_professor.max_compat import InlineKeyboardButton, InlineKeyboardMarkup
from digital_professor.database import get_user
from digital_professor.database import is_task_completed, is_course_completed, is_topic_completed
from digital_professor.database import get_user, is_topic_completed, is_task_completed, is_course_completed
from digital_professor.database import is_big5_initialized, get_big5_profile, log_ping
from digital_professor.database import get_user_ab_group
from digital_professor.database import update_user_moodle_course
import datetime
import re
import pytz
import json
import random
from functools import lru_cache
#==============================================================================================
from typing import Optional, Tuple

# --- Moodle: компактная интеграция ---
# Внутренний модуль direct-API к Moodle (без внешнего микросервиса)
from digital_professor.moodle_client import MoodleClient
import os
#==============================================================================================

logger = logging.getLogger(__name__)

PING_GREETINGS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'rag', 'data', 'ping_greetings.json')
PING_GREETINGS_FALLBACK = [
    "Привет, {name}! Пора взяться за обучение — ты отлично справишься."
]


@lru_cache(maxsize=1)
def _load_ping_greetings() -> list[str]:
    try:
        with open(PING_GREETINGS_PATH, 'r', encoding='utf-8') as f:
            data = json.load(f)
        if not isinstance(data, list):
            raise ValueError("ping greetings json must be a list")
        cleaned: list[str] = []
        for item in data:
            if isinstance(item, str):
                text = item.strip()
                if text:
                    cleaned.append(text)
        if not cleaned:
            raise ValueError("ping greetings list is empty")
        logger.debug(f"Loaded {len(cleaned)} ping greeting variations from {PING_GREETINGS_PATH}")
        return cleaned
    except Exception as e:
        logger.warning(f"⚠️ FALLBACK: Could not load ping greetings from {PING_GREETINGS_PATH}: {e}. Using fallback text.")
        return PING_GREETINGS_FALLBACK


def _replace_student_placeholder(template: str, username: str) -> str:
    placeholders = ['[студент]', '[student]', '{name}', '{username}']
    result = template
    for token in placeholders:
        if token in result:
            result = result.replace(token, username)
    if result == template:
        return template.replace('{name}', username)
    return result


def build_ping_greeting(username: str) -> str:
    """
    Выбирает случайное приветствие из пула вариантов.
    Каждый вызов возвращает новый случайный вариант.
    """
    username = username or "Студент"
    variations = _load_ping_greetings()
    if not variations:
        logger.warning(f"⚠️ FALLBACK: No ping greeting variations loaded, using fallback text")
        return PING_GREETINGS_FALLBACK[0].format(name=username)
    idx = random.randint(0, len(variations) - 1)
    template = variations[idx]
    result = _replace_student_placeholder(template, username)
    logger.debug(f"Selected ping greeting #{idx} from {len(variations)} variations")
    return result


def compose_ping_message(greeting: str, body: str) -> str:
    greeting = (greeting or "").strip()
    body = (body or "").strip()
    if not greeting:
        return body
    if not body:
        return greeting
    return f"{greeting}\n\n{body}"


async def send_reminder(context, user_id: int, force: bool = False):
    """
    Отправляет пинг пользователю, если это необходимо согласно его расписанию,
    либо принудительно при force=True.
    """
    logger.info(f"Checking if reminder should be sent to user_id: {user_id}")

    # --- ЛОГИКА: Проверка расписания ---
    from digital_professor.database import (get_user_ping_settings, update_user_last_ping_date, 
                         get_user_timezone, get_user_ping_time, is_big5_initialized, get_big5_profile)

    ping_settings = get_user_ping_settings(user_id)
    pings_per_week = ping_settings['pings_per_week']
    ping_days_list = ping_settings['ping_days'] # Это теперь используется
    last_ping_date_str = ping_settings['last_ping_date']
    
    user_timezone_str = get_user_timezone(user_id) 
    user_ping_time_str = get_user_ping_time(user_id)
    
    if not user_timezone_str or not user_ping_time_str:
        logger.warning(f"User {user_id} has no timezone or ping time set. Skipping ping.")
        return

    try:
        user_tz = pytz.timezone(user_timezone_str)
    except pytz.UnknownTimeZoneError:
        logger.error(f"Unknown timezone for user {user_id}: {user_timezone_str}. Skipping ping.")
        return

    # Получаем текущую дату пользователя
    now_user_tz = datetime.datetime.now(user_tz)
    today_user_date_str = now_user_tz.date().isoformat() # 'YYYY-MM-DD'
    # Сопоставление weekday() с сокращениями
    weekday_map = {0: 'mon', 1: 'tue', 2: 'wed', 3: 'thu', 4: 'fri', 5: 'sat', 6: 'sun'}
    current_weekday_user_str = weekday_map[now_user_tz.weekday()]

    # --- Проверка на выходной (если требуется строго будни) ---
    if now_user_tz.weekday() >= 5 and not force: # 5 = суббота, 6 = воскресенье
        logger.debug(f"Today is a weekend ({current_weekday_user_str}) for user {user_id}. Skipping ping.")
        return
    # --- КОНЕЦ ОГРАНИЧЕНИЯ ---
    
    # --- ЛОГИКА ВЫБОРА ДНЯ ---
    should_ping_today = force
    if not force:
        if ping_days_list:
            # Если заданы конкретные дни, используем их
            if current_weekday_user_str in ping_days_list:
                should_ping_today = True
            else:
                logger.debug(f"Today ({current_weekday_user_str}) is not in the scheduled ping days {ping_days_list} for user {user_id}. Skipping.")
        else:
            # Если дни не заданы, используем логику частоты (но только по будням)
            if now_user_tz.weekday() >= 5: # Проверка на выходной
                 logger.debug(f"Today is a weekend ({current_weekday_user_str}) and no specific days set. Skipping ping for user {user_id}.")
                 return

            # Интерпретация pings_per_week
            if pings_per_week == 7:
                # Специальный случай: каждый день (включая выходные, если бы не проверка выше)
                should_ping_today = True
            elif pings_per_week == 5:
                # Каждый будний день
                should_ping_today = True
            elif pings_per_week == 3:
                # Пн, Ср, Пт
                if current_weekday_user_str in ['mon', 'wed', 'fri']:
                    should_ping_today = True
            elif pings_per_week == 2:
                # Вт, Чт
                if current_weekday_user_str in ['tue', 'thu']:
                    should_ping_today = True
            elif pings_per_week == 1:
                # Пн
                if current_weekday_user_str == 'mon':
                    should_ping_today = True
            else:
                # Для других значений (1-4, кроме 2 и 3) используем упрощенную логику
                # или можно считать как "каждый будний день" (pings_per_week=5)
                # или можно игнорировать (should_ping_today=False)
                # Выберем "каждый будний день" как дефолт для нестандартных значений
                should_ping_today = True 
                logger.debug(f"Non-standard pings_per_week ({pings_per_week}) for user {user_id}. Defaulting to every weekday.")
            
    if not should_ping_today:
        return
    # --- КОНЕЦ ЛОГИКИ ВЫБОРА ДНЯ ---

    # Проверяем, не отправляли ли мы пинг сегодня уже
    if last_ping_date_str == today_user_date_str and not force:
        logger.debug(f"Already sent ping to user {user_id} today ({today_user_date_str}). Skipping.")
        return
        
    # --- Остальная логика send_reminder (Big5, генерация текста) ---
    # ... (оставляем без изменений, как в предыдущем примере) ...
    # Получаем имя пользователя для персонализации
    username = "Студент" # Значение по умолчанию
    try:
        chat = await context.bot.get_chat(user_id)
        username = chat.first_name or chat.username or "Студент"
    except Exception as e:
        logger.warning(f"Could not fetch username for user {user_id}: {e}. Using default.")

    # Вычисляем группу пользователя
    try:
        ab_group = get_user_ab_group(user_id)
        logger.debug(f"User {user_id} AB group: '{ab_group}'")
    except Exception as e:
        ab_group = 'a'
        logger.warning(f"Failed to get AB group for user {user_id}: {e}, defaulting to 'a'")

    # Если первичный Big5 ещё не собран — предлагаем опрос из 3 вопросов (только для группы 'c')
    if ab_group == 'c' and not is_big5_initialized(user_id):
        ping_type = 'initial_survey'
        keyboard = [
            [InlineKeyboardButton("Пройти 3 коротких вопроса", callback_data='big5_start')],
            [InlineKeyboardButton("Позже", callback_data='big5_later')]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        # Статичный текст для первого пинга (Big5 опрос)
        message_text = f"Привет, {username}! Хочу лучше подстроиться под тебя. Ответишь на 3 коротких вопроса? Это займёт 1–2 минуты."
        try:
            await context.bot.send_message(
                chat_id=user_id,
                text=message_text,
                reply_markup=reply_markup
            )
            logger.info(f"Sent Big5 initialization ping to user {user_id}")
            
            # Предполагается, что log_ping определена в database.py
            log_ping(
                user_id=user_id,
                ping_type=ping_type,
                sent_at=datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
                message_text=message_text,
                big5_profile_id=None,
                meta_data={"username": username}
            )
            
        except Exception as e:
            logger.error(f"Failed to send big5 invite to user_id: {user_id}: {e}")
        return # Не отправляем обычный пинг

    # --- Генерация текста: для всех групп используем статические варианты ---
    ping_type = 'standard'
    logger.debug(f"Using static ping greeting for user {user_id} (group '{ab_group}')")
    greeting_text = build_ping_greeting(username)
    message_text = greeting_text
#==============================================================================================
    # --- ДОБАВЛЕНО: Блок с прогрессом Moodle и ссылкой на следующее задание ---
    # Логика: по номеру студенческого (student_id) находим Moodle user_id,
    # затем собираем проценты завершения по всем курсам и одно (первое) невыполненное задание.
    per_course_buttons: list = []
    try:
        u = get_user(user_id)
        student_number = (u or {}).get('student_id')
        if student_number:
            # Создаём клиента из переменных окружения (.env или ../config.txt)
            client = await MoodleClient.from_env()
            async with client as m:
                data = await m.get_student_progress_and_next_task(student_number)
            # Формируем текстовую вставку с прогрессом по курсам
            courses = data.get('courses') or []
            if courses:
                # Ограничим вывод до 5 курсов, чтобы не перегружать сообщение
                try:
                    max_courses = int(os.getenv('MOODLE_MAX_COURSES_IN_PING', '5'))
                except ValueError:
                    max_courses = 5
                first = courses[:max(0, max_courses)]
                lines = ["\n📊 Прогресс в Moodle:"]
                for c in first:
                    lines.append(f" • {c.get('fullname')}: {c.get('percent')}%")
                other_count = max(0, len(courses) - len(first))
                if other_count:
                    lines.append(f" … и ещё {other_count} курс(ов)")
                message_text = message_text + "\n\n" + "\n".join(lines)

            # --- Сохраняем список ID курсов пользователя в users.moodle_course (только при изменении) ---
            try:
                course_ids = []
                for c in (data.get('courses') or []):
                    cid = c.get('id')
                    if cid is not None:
                        try:
                            course_ids.append(int(cid))
                        except Exception:
                            continue
                # Уникальные, отсортированные ID как строка через запятую; пустая строка если нет курсов
                new_courses_str = ",".join(str(x) for x in sorted(set(course_ids))) if course_ids else ""
                existing_courses_str = (u or {}).get('moodle_course') or ""
                if new_courses_str != existing_courses_str:
                    update_user_moodle_course(user_id, new_courses_str)
            except Exception as _e:
                # Не мешаем отправке пинга, просто логируем
                logger.debug(f"Skip updating moodle_course for {user_id}: {_e}")

            # Добавляем строки с дедлайнами по каждому курсу, где найден next_task с дедлайном
            try:
                deadline_lines = []
                for r in (data.get('course_details') or []):
                    nt = r.get('next_task') or {}
                    due_ts = nt.get('deadline_ts')
                    if due_ts:
                        try:
                            dt_local = datetime.datetime.fromtimestamp(int(due_ts), tz=user_tz)
                            task = nt.get('name') or 'задание'
                            if len(task) > 28:
                                task = task[:25] + '…'
                            deadline_lines.append(f"⏰ {task}: до {dt_local.strftime('%d.%m %H:%M')}")
                        except Exception:
                            continue
                if deadline_lines:
                    message_text = message_text + "\n" + "\n".join(deadline_lines)
            except Exception:
                pass

            # Кнопки на следующее невыполненное задание ПО КАЖДОМУ курсу
            for r in (data.get('course_details') or []):
                nt = r.get('next_task')
                if nt and nt.get('url'):
                    task = nt.get('name') or 'задание'
                    if len(task) > 28:
                        task = task[:25] + '…'
                    btn_text = f"Перейти к заданию ({task})"
                    per_course_buttons.append([InlineKeyboardButton(btn_text[:64], url=nt.get('url'))])
        else:
            logger.debug(f"User {user_id} has no student_id — Moodle-пинг пропущен в тексте.")
    except Exception as e:
        # Не ломаем основной пинг, просто логируем
        logger.error(f"Moodle addendum failed for user {user_id}: {e}")

    # Базовая клавиатура + (опционально) кнопка на задание Moodle
    keyboard = []
    if per_course_buttons:
        keyboard.extend(per_course_buttons)
    keyboard.extend([
        [InlineKeyboardButton("Готов приступить", callback_data='Готов приступить')],
        [InlineKeyboardButton("Отложу", callback_data='Отложу')],
        [InlineKeyboardButton("Не хочу", callback_data='Не хочу')]
    ])
    #==============================================================================================
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    try:
        await context.bot.send_message(chat_id=user_id, text=message_text, reply_markup=reply_markup)
        logger.info(f"Sent dynamic ping to user {user_id}")
        
        # --- Обновляем дату последнего пинга ---
        update_user_last_ping_date(user_id, today_user_date_str)
        # --- 
        
        # --- Логируем отправленный пинг ---
        big5_row = get_big5_profile(user_id)
        big5_data = dict(big5_row) if big5_row else None
        meta_to_log = { "username": username }
        if big5_data:
            meta_to_log["big5_at_send_time"] = {
                k: big5_data.get(k) for k in 
                ['extraversion', 'agreeableness', 'conscientiousness', 'emotional_stability', 'openness']
            }
        
        log_ping(
            user_id=user_id,
            ping_type=ping_type,
            sent_at=datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
            message_text=message_text,
            big5_profile_id=big5_data.get('user_id') if big5_data else None,
            meta_data=meta_to_log
        )
        # --- 
        
        # --- ЕЖЕНЕДЕЛЬНЫЙ ОПРОС: отправляем в пятницу после основного пинга ---
        if current_weekday_user_str == 'fri':
            from digital_professor.database import has_weekly_ping_this_week
            if not has_weekly_ping_this_week(user_id):
                await send_weekly_survey(context, user_id, username)
        
    except Exception as e:
        logger.error(f"Failed to send reminder to user_id: {user_id}: {e}")
    # --- Конец остальной логики ---


async def send_weekly_survey(context, user_id: int, username: str):
    """
    Отправляет еженедельный опрос пользователю в пятницу.
    
    Args:
        context: Контекст бота
        user_id: ID пользователя
        username: Имя пользователя
    """
    try:
        survey_text = (
            "Привет! Я задам тебе пару вопросов, чтобы понять, есть ли у тебя трудности.\n\n"
            "1. Понятен ли тебе материал, который вы изучили на этой неделе?"
        )
        
        keyboard = [
            [InlineKeyboardButton("Да", callback_data='weekly_survey_q1_yes')],
            [InlineKeyboardButton("Нет", callback_data='weekly_survey_q1_no')]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await context.bot.send_message(
            chat_id=user_id,
            text=survey_text,
            reply_markup=reply_markup
        )
        
        logger.info(f"Sent weekly survey to user {user_id}")
        
        # Логируем отправку опроса в БД
        log_ping(
            user_id=user_id,
            ping_type='weekly_survey',
            sent_at=datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
            message_text=survey_text,
            big5_profile_id=None,
            meta_data={"username": username, "survey_type": "weekly"}
        )
    except Exception as e:
        logger.error(f"Failed to send weekly survey to user_id: {user_id}: {e}")


def is_admin(user_id: int) -> bool:
    u = get_user(user_id)
    return bool(u and u.get('is_admin'))

def main_menu_keyboard():
    keyboard = [
        [InlineKeyboardButton("ℹ Инфо", callback_data='info')],
        # [InlineKeyboardButton("📚 Приступить к обучению", callback_data='start_learning')],
        [InlineKeyboardButton("⏳ Сменить часовой пояс", callback_data='change_timezone')],
        [InlineKeyboardButton("⏰ Сменить время напоминания", callback_data='change_ping_time')],
        [InlineKeyboardButton("🎫 Сменить номер студенческого", callback_data='change_student_id')],
    ]
    return InlineKeyboardMarkup(keyboard)

def main_menu_keyboard_for(user_id: int | None):
    """Главное меню: Мои курсы и Настройки"""
    kb = [
        [InlineKeyboardButton("📚 Мои курсы", callback_data='my_courses')],
        [InlineKeyboardButton("⚙ Настройки", callback_data='info')],
    ]
    return InlineKeyboardMarkup(kb)

def keyboard_with_main_menu(rows):
    kb = list(rows)
    # kb.append([InlineKeyboardButton("Главное меню", callback_data='main_menu')])
    return InlineKeyboardMarkup(kb)


def courses_keyboard(courses, user_id):
    """
    courses: list of dicts with id & title
    """
    kb = []
    for c in courses:
        cid = c.get('id')
        if cid is None:
            continue
        title = c.get('title') or f"Курс {cid}"
        source = c.get('source')
        if source == 'moodle':
            mark = ""
            callback = f"moodle_course:{cid}"
        else:
            mark = " ✅" if is_course_completed(cid, user_id) else ""
            callback = f"select_course:{cid}"
        kb.append([
            InlineKeyboardButton(
                f"{title}{mark}",
                callback_data=callback
            )
        ])
    kb.append([InlineKeyboardButton("Главное меню", callback_data='main_menu')])
    return InlineKeyboardMarkup(kb)

#Клавиатура для топиков
def topics_keyboard(topics, course_id, user_id):
    kb = []
    for t in topics:
        mark = " ✅" if is_topic_completed(t['id'], user_id) else ""
        title = t['title'] or f"Тема {t['id']}"
        kb.append([
            InlineKeyboardButton(
                f"{title}{mark}",
                callback_data=f"select_topic:{t['id']}"
            )
        ])
    kb.append([InlineKeyboardButton("Назад (курсы)", callback_data='my_courses')])
    kb.append([InlineKeyboardButton("Главное меню", callback_data='main_menu')])
    return InlineKeyboardMarkup(kb)


def tasks_keyboard(tasks, user_id):
    kb = []
    for t in tasks:
        completed = is_task_completed(t['id'], user_id)
        mark = " ✅" if completed else ""
        title = t['title'] or f"Задание {t['id']}"
        label = f"{title}{mark}"
        kb.append([InlineKeyboardButton(label, callback_data=f"start_task:{t['id']}")])
    kb.append([InlineKeyboardButton("Назад (курсы)", callback_data='my_courses')])
    kb.append([InlineKeyboardButton("Главное меню", callback_data='main_menu')])
    return InlineKeyboardMarkup(kb)

def question_choices_keyboard(task_id, question):
    """
    question: dict with 'id' and 'choices' (each choice has id & choice_text)
    """
    kb = []
    for ch in question.get('choices', []):
        kb.append([InlineKeyboardButton(ch['choice_text'], callback_data=f"answer:{task_id}:{question['id']}:{ch['id']}")])
    kb.append([InlineKeyboardButton("Прервать тест", callback_data='main_menu')])
    return InlineKeyboardMarkup(kb)




