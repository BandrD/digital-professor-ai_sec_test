from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
import pytz
from digital_professor.database import get_users
from digital_professor.utils import send_reminder  # Импортируем из utils.py
import logging
from digital_professor.database import get_users
from digital_professor.database import recalc_big5_for_user_week
from digital_professor.database import week_start_for
from datetime import timedelta, datetime
import asyncio

logger = logging.getLogger(__name__)

scheduler = AsyncIOScheduler()

PING_JOB_PREFIX = "user_ping_"

def _get_user_ping_job_id(user_id: int) -> str:
    """Генерирует уникальный ID для задачи пинга пользователя."""
    return f"{PING_JOB_PREFIX}{user_id}"

async def schedule_user_ping_job(application, user_id: int):
    """
    Планирует индивидуальную задачу пинга для пользователя.
    Задача будет срабатывать в ping_time пользователя по каждому дню из ping_days.
    """
    from digital_professor.database import get_user_ping_settings, get_user_timezone, get_user_ping_time
    
    # Получаем настройки пользователя
    ping_settings = get_user_ping_settings(user_id)
    ping_days_list = ping_settings['ping_days']
    # freq не используется напрямую, логика в ping_days_list
    
    # Получаем часовой пояс и время пинга пользователя
    user_timezone_str = get_user_timezone(user_id)
    user_ping_time_str = get_user_ping_time(user_id)
    
    if not user_timezone_str or not user_ping_time_str:
        logger.warning(f"Cannot schedule ping for user {user_id}: missing timezone ({user_timezone_str}) or ping time ({user_ping_time_str})")
        # Удаляем существующую задачу, если настройки неполные
        remove_user_ping_job(user_id)
        return

    try:
        user_tz = pytz.timezone(user_timezone_str)
    except pytz.UnknownTimeZoneError:
        logger.error(f"Unknown timezone for user {user_id}: {user_timezone_str}. Cannot schedule ping.")
        remove_user_ping_job(user_id)
        return

    try:
        # Парсим время пинга
        ping_hour, ping_minute = map(int, user_ping_time_str.split(':'))
        if not (0 <= ping_hour <= 23 and 0 <= ping_minute <= 59):
            raise ValueError("Invalid hour or minute")
    except (ValueError, IndexError):
        logger.error(f"Invalid ping_time format for user {user_id}: {user_ping_time_str}. Cannot schedule ping.")
        remove_user_ping_job(user_id)
        return

    # Удаляем старую задачу, если она была
    remove_user_ping_job(user_id)

    if not ping_days_list:
        logger.info(f"No ping days set for user {user_id}. Ping job not scheduled.")
        return # Нечего планировать

    # Преобразуем сокращения дней в формат APScheduler (0=понедельник, 6=воскресенье)
    day_name_to_cron = {
        'mon': 0, 'tue': 1, 'wed': 2, 'thu': 3, 'fri': 4, 'sat': 5, 'sun': 6
    }
    cron_days = [str(day_name_to_cron[day]) for day in ping_days_list if day in day_name_to_cron]
    
    if not cron_days:
        logger.warning(f"No valid ping days for user {user_id} (parsed from {ping_days_list}). Ping job not scheduled.")
        return

    # Создаем CronTrigger для конкретных дней и времени пользователя
    trigger = CronTrigger(
        day_of_week=','.join(cron_days),
        hour=ping_hour,
        minute=ping_minute,
        timezone=user_tz
    )
    
    job_id = _get_user_ping_job_id(user_id)
    
    # Планируем новую задачу
    # send_reminder теперь будет вызываться напрямую, без ежедневной обертки
    job = scheduler.add_job(
        send_reminder, # Передаем саму функцию send_reminder
        trigger=trigger,
        id=job_id,
        replace_existing=True, # На случай, если задача с таким ID уже существует
        args=[application, user_id], # Передаем application и user_id
        # name=f"Ping job for user {user_id}" # Опционально, для удобства в логах APScheduler
    )
    
    logger.info(f"Scheduled individual ping job for user {user_id}: {job.id} on days {ping_days_list} at {ping_hour:02d}:{ping_minute:02d} ({user_timezone_str})")

def remove_user_ping_job(user_id: int):
    """Удаляет индивидуальную задачу пинга пользователя, если она существует."""
    job_id = _get_user_ping_job_id(user_id)
    try:
        scheduler.remove_job(job_id)
        logger.info(f"Removed individual ping job for user {user_id}: {job_id}")
    except Exception:
        # JobLookupError если задача не найдена - это нормально
        pass # logger.debug(f"No individual ping job found to remove for user {user_id}: {job_id}")



async def _weekly_big5_job(application):
    """
    Задание, выполняемое еженедельно.
    Пересчитывает Big5 для всех пользователей.
    """
    logger.info("Running weekly Big5 recalculation job...")
    
    # Локальные импорты
    from digital_professor.database import get_users, week_start_for, recalc_big5_for_user_week, get_user_ab_group
    from digital_professor.bot import call_llm_json # Предполагаем, что функция есть в bot.py
    
    try:
        users = get_users() # [(user_id, timezone, ping_time), ...]
        logger.info(f"Found {len(users)} users for Big5 recalculation.")

        for user_id, tzname, _ in users:
            try:
                # Фильтр по A/B/C: пересчитываем Big5 только для группы 'c'
                try:
                    group = get_user_ab_group(user_id)
                except Exception:
                    group = 'a'
                if group != 'c':
                    logger.debug(f"Skipping Big5 recalculation for user {user_id}: AB group='{group}'")
                    continue

                tz = pytz.timezone(tzname)
                now = datetime.now(tz)
                # Получаем начало предыдущей недели (понедельник) в локальной таймзоне пользователя
                prev_week_start_local = week_start_for(now - timedelta(days=7), tzname) # строка 'YYYY-MM-DD'
                
                # Простая обертка для вызова LLM
                async def _llm(prompt: str):
                    # Импортируем внутри, чтобы не было циклической зависимости на уровне импортов
                    # from digital_professor.bot import call_llm_json 
                    return await call_llm_json(prompt)
                
                await recalc_big5_for_user_week(user_id, tzname, prev_week_start_local, _llm)
                logger.debug(f"Big5 recalculated for user {user_id}")
                
            except Exception as e:
                logger.error(f"Error recalculating Big5 for user {user_id}: {e}", exc_info=True)
                
    except Exception as e:
        logger.critical(f"Critical error in _weekly_big5_job: {e}", exc_info=True)
    finally:
        logger.info("Weekly Big5 recalculation job finished.")


def schedule_reminders(application):
    """
    Инициализирует планировщик.
    Ранее планировал ежедневную проверку, теперь только еженедельный Big5 и запускает
    перепланирование всех пользовательских пингов.
    """
    logger.info("Initializing scheduler for Big5 and user ping rescheduling...")

    # --- Еженедельный пересчёт Big5 ---
    big5_job = scheduler.add_job(
        _weekly_big5_job,  # передаём корутину напрямую — AsyncIOScheduler сам её await-ит
        trigger=CronTrigger(day_of_week='mon', hour=3, minute=0, timezone=pytz.UTC),
        id="weekly_big5_job",
        replace_existing=True,
        args=[application]
    )
    logger.info(f"Scheduled weekly Big5 job: {big5_job}")

    # --- НОВОЕ: Перепланирование всех пользовательских пингов ---
    # Это может быть тяжелой операцией при большом количестве пользователей.
    # Можно рассмотреть перенос этой логики в отдельную задачу или вызов по команде админа.
    # Пока что делаем это при старте.
    logger.info("Rescheduling ping jobs for all users...")
    from digital_professor.database import get_users
    users = get_users()
    for user_id, _, _ in users:
        try:
            # На старте приложения используем asyncio.create_task, так как Application ещё не запущено
            asyncio.get_running_loop().create_task(schedule_user_ping_job(application, user_id))
        except Exception as e:
            logger.error(f"Error scheduling ping job for user {user_id} at startup: {e}")
    logger.info("Finished rescheduling ping jobs for all users.")
    # --- КОНЕЦ НОВОГО ---

    if not scheduler.running:
        scheduler.start()
        logger.info("Scheduler started")
    else:
        logger.debug("Scheduler is already running.")

# --- НОВАЯ ФУНКЦИЯ: Публичная функция для вызова из bot.py при изменении настроек ---
async def reschedule_user_ping(application, user_id: int):
    """
    Публичная функция для перепланирования задачи пинга пользователя.
    Вызывается из bot.py, когда админ меняет расписание или пользователь меняет время/часовой пояс.
    """
    logger.info(f"Public call to reschedule ping job for user {user_id}")
    await schedule_user_ping_job(application, user_id)

async def reschedule_all_user_pings(application):
    """
    Перепланирует задачи пинга для ВСЕХ пользователей.
    Вызывается после массового изменения расписания.
    """
    logger.info("Rescheduling ping jobs for ALL users...")
    try:
        users = get_users() # [(user_id, timezone, ping_time), ...]
        logger.info(f"Found {len(users)} users for rescheduling.")
        
        for user_id, timezone_str, ping_time_str in users:
            try:
                # schedule_user_ping_job - асинхронная, но APScheduler ожидает синхронные функции для add_job
                # Поэтому используем application.create_task внутри лямбды
                application.create_task(schedule_user_ping_job(application, user_id))
                logger.debug(f"Scheduled reschedule ping job for user {user_id}.")
            except Exception as e:
                logger.error(f"Error scheduling reschedule ping job for user {user_id}: {e}")
                
        logger.info("Finished rescheduling ping jobs for ALL users.")
    except Exception as e:
        logger.error(f"Critical error during reschedule_all_user_pings: {e}", exc_info=True)

