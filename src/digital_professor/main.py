# main.py
import warnings
import logging
warnings.filterwarnings("ignore", category=UserWarning, module="apscheduler")
from digital_professor.max_compat import Application, CallbackQueryHandler, CommandHandler, MessageHandler, filters
import asyncio
import os
from dotenv import load_dotenv
from digital_professor.bot import admin_set_ping_schedule, admin_test_ping, admin_set_ping_schedule_all, admin_force_ping, admin_test_weekly_survey
from digital_professor.bot import start, handle_message, button, menu, create_course_cmd, assign_course_command, recalc_big5, help_command
from digital_professor.database import init_db
from digital_professor.bot import initialize_rag_if_needed
from digital_professor.scheduler import schedule_reminders
# --- ИМПОРТЫ ДЛЯ RAG ---
# Предполагается, что RagPipeline и initialize_rag_if_needed находятся в rag/pipeline.py
# или в отдельном модуле rag/__init__.py

# -----------------------

# Загружаем переменные окружения
load_dotenv()

# Получаем уровень логирования из переменной окружения
log_level_str = os.getenv('LOG_LEVEL', 'INFO').upper()
log_level = getattr(logging, log_level_str, logging.INFO)

logging.basicConfig(
    level=log_level,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)

# Настройка уровня логирования для внешних библиотек
external_libs = {
    'httpcore': os.getenv('HTTPCORE_LOG_LEVEL', 'WARNING'),
    'httpx': os.getenv('HTTPX_LOG_LEVEL', 'WARNING'),
    'digital_professor.max_compat': os.getenv('MAX_LOG_LEVEL', 'WARNING'),
    'apscheduler': os.getenv('APSCHEDULER_LOG_LEVEL', 'WARNING'),
    'urllib3': os.getenv('URLLIB3_LOG_LEVEL', 'WARNING'),
}

for lib_name, level_str in external_libs.items():
    level = getattr(logging, level_str.upper(), logging.WARNING)
    logging.getLogger(lib_name).setLevel(level)

logger = logging.getLogger(__name__)
logger.info(f"Logging configured with level: {log_level_str}")

# additional admin commands implemented inside bot.py as regular handlers
# from digital_professor.bot import (
#     # admin commands (registered below)
#     # these names exist in bot.py as functions decorated for CommandHandlers style,
#     # but since we used plain functions earlier we must import them if present.
# )

async def main():
    logger.info("Starting bot...")

    token = os.getenv("MAX_BOT_TOKEN")
    if not token:
        logger.error("MAX_BOT_TOKEN not set in environment")
        return

    application = Application.builder().token(token).build()

    # core handlers
    application.add_handler(CommandHandler('start', start))
    application.add_handler(CommandHandler('help', help_command))
    application.add_handler(CommandHandler('menu', menu))
    # В register_handlers(application) добавь:
    application.add_handler(CommandHandler("create_course", create_course_cmd))
    application.add_handler(CommandHandler("assign_course", assign_course_command))

    # keep message handler broad: handles text and incoming files/photos
    application.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, handle_message))

    application.add_handler(CallbackQueryHandler(button))
    logger.info("Handlers registered")

    # Временная команда для тестирования Big-5
    application.add_handler(CommandHandler("recalc_big5", recalc_big5))
    application.add_handler(CommandHandler("setpings", admin_set_ping_schedule))
    application.add_handler(CommandHandler("setpingsall", admin_set_ping_schedule_all))
    application.add_handler(CommandHandler("testping", admin_test_ping))
    application.add_handler(CommandHandler("forceping", admin_force_ping))
    application.add_handler(CommandHandler("testweeklysurvey", admin_test_weekly_survey))

    # init DB
    init_db()
    logger.info("Database initialized")

    # schedule reminders (reads DB)
    schedule_reminders(application)
    logger.info("Scheduler started")
        # --- ИНИЦИАЛИЗАЦИЯ RAG ---
    # Инициализируем RAG в фоне, не блокируя запуск бота
    # Создаем задачу, но не дожидаемся её завершения здесь
    rag_task = asyncio.create_task(initialize_rag_if_needed())
    logger.info("Scheduled RAG Pipeline initialization task.")
    # --------------------------
    # --- ЗАПУСК БОТА ---
    await application.initialize()
    await application.start()
    await application.updater.start_polling(drop_pending_updates=True)
    logger.info("Bot polling")

    try:
        await asyncio.Event().wait()
    except asyncio.CancelledError:
        logger.info("Stopping bot...")
    finally:
        # --- ЗАВЕРШЕНИЕ РАБОТЫ ---
        # Ждем завершения задачи инициализации RAG (на всякий случай, если она еще не закончилась)
        # Это не обязательно, но хорошо для корректного завершения
        if 'rag_task' in locals():
            try:
                await rag_task
                logger.info("RAG initialization task finished (or was cancelled).")
            except Exception as e:
                logger.error(f"Error in RAG initialization task during shutdown: {e}")

        await application.updater.stop()
        await application.stop()
        await application.shutdown()
        logger.info("Bot stopped")
    # --------------------

if __name__ == '__main__':
    asyncio.run(main())
