"""
main_for_test.py

Изолированный запуск тестового бота из `bot_for_test.py`.

Переменные окружения:
- MAX_BOT_TOKEN_TEST (рекомендуется) или MAX_BOT_TOKEN
"""

import asyncio
import logging
import os
import warnings

from dotenv import load_dotenv
from digital_professor.max_compat import Application, CallbackQueryHandler, CommandHandler, MessageHandler, filters

from digital_professor_testing.bot_for_test import button, handle_message, initialize_rag_for_test_if_needed, start
from digital_professor.database import init_db
from digital_professor_testing.test_data_for_test import init_test_tables

warnings.filterwarnings("ignore", category=UserWarning, module="apscheduler")

load_dotenv()

log_level_str = os.getenv("LOG_LEVEL", "INFO").upper()
log_level = getattr(logging, log_level_str, logging.INFO)
logging.basicConfig(level=log_level, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)


async def main():
    logger.info("Starting TEST bot...")

    token = os.getenv("MAX_BOT_TOKEN_TEST") or os.getenv("MAX_BOT_TOKEN")
    if not token:
        logger.error("MAX_BOT_TOKEN_TEST or MAX_BOT_TOKEN not set in environment")
        return

    # ВАЖНО: как и в проде, гарантируем создание SQLite и таблиц при пустой/удалённой БД
    init_db()
    logger.info("TEST bot database initialized")
    init_test_tables()

    application = Application.builder().token(token).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, handle_message))
    application.add_handler(CallbackQueryHandler(button))

    # Инициализируем RAG в фоне, чтобы первые запросы не ждали весь прогрев
    rag_task = asyncio.create_task(initialize_rag_for_test_if_needed())
    logger.info("Scheduled TEST RAG initialization task.")

    await application.initialize()
    await application.start()
    await application.updater.start_polling(drop_pending_updates=True)
    logger.info("TEST bot polling")

    try:
        await asyncio.Event().wait()
    except asyncio.CancelledError:
        logger.info("Stopping TEST bot...")
    finally:
        try:
            await rag_task
        except Exception:
            pass
        await application.updater.stop()
        await application.stop()
        await application.shutdown()
        logger.info("TEST bot stopped")


if __name__ == "__main__":
    asyncio.run(main())


