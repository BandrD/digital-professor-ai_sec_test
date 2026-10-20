"""
Отправка ERROR-логов в чат MAX. Топиков в MAX нет: ERROR_LOG_TOPIC_ID не читается.
"""
import html
import logging
import traceback
import asyncio
import time
import os
from typing import Optional
from datetime import datetime
import pytz

from digital_professor.max_compat.client import MaxClient


class TimezoneFormatter(logging.Formatter):
    """Кастомный форматтер для логов с учетом часового пояса"""
    
    def __init__(self, fmt=None, datefmt=None, timezone='UTC'):
        super().__init__(fmt, datefmt)
        self.timezone = pytz.timezone(timezone)
    
    def formatTime(self, record, datefmt=None):
        """Переопределяем форматирование времени для использования нашего часового пояса"""
        ct = datetime.fromtimestamp(record.created, tz=self.timezone)
        if datefmt:
            return ct.strftime(datefmt)
        else:
            return ct.strftime('%Y-%m-%d %H:%M:%S')


class TelegramLogHandler(logging.Handler):
    """Handler для отправки ошибок в чат MAX. Имя класса сохранено для совместимости."""
    
    # Библиотеки, ошибки от которых не нужно отправлять в Telegram (не критичные)
    IGNORED_LOGGERS = {
        'backoff',    
        'posthog',    
        'urllib3',   
        'httpx',      
        'httpcore',
    }
    
    def __init__(self, chat_id: int, topic_id: Optional[int] = None, bot_token: Optional[str] = None):
        super().__init__()
        self.chat_id = chat_id
        self.topic_id = None
        self.bot_token = bot_token or os.getenv('MAX_BOT_TOKEN')
        self.setLevel(logging.ERROR)
        self._last_send_time = 0
        self._lock = None
        self._client: Optional[MaxClient] = None
        
    def _get_lock(self):
        """Получает или создает asyncio.Lock"""
        if self._lock is None:
            try:
                loop = asyncio.get_running_loop()
                self._lock = asyncio.Lock()
            except RuntimeError:
                return None
        return self._lock
        
    def _client_get(self) -> MaxClient:
        if self._client is None:
            self._client = MaxClient(self.bot_token)
        return self._client

    async def _send_message_async(self, message: str):
        """Асинхронная отправка сообщения в чат MAX."""
        if not self.chat_id or not self.bot_token:
            return

        lock = self._get_lock()
        if lock:
            async with lock:
                current_time = time.time()
                time_since_last = current_time - self._last_send_time
                if time_since_last < 5.0:
                    await asyncio.sleep(5.0 - time_since_last)

                try:
                    safe = html.escape(message)
                    await self._client_get().request(
                        "POST",
                        "/messages",
                        params={"chat_id": self.chat_id},
                        json_body={
                            "text": f"❌ <b>Ошибка</b>\n\n<blockquote>{safe}</blockquote>",
                            "format": "html",
                        },
                    )
                    self._last_send_time = time.time()
                except Exception as e:
                    logging.getLogger(__name__).debug(f"Не удалось отправить ошибку в MAX: {e}")
    
    def emit(self, record: logging.LogRecord):
        """Отправляет ошибку в Telegram с задержкой"""
        if not self.chat_id or not self.bot_token:
            return
        
        # Пропускаем ошибки от некритичных библиотек
        logger_name = record.name.split('.')[0]  # Берем первый компонент имени логгера
        if logger_name in self.IGNORED_LOGGERS:
            return
        
        try:
            message = self.format(record)
            if record.exc_info:
                message += '\n\n' + ''.join(traceback.format_exception(*record.exc_info))
            
            # Ограничиваем длину сообщения (Telegram максимум 4096 символов, но для удобства ограничим до 500)
            if len(message) > 500:
                message = message[:500] + "\n\n... (сообщение обрезано)"
            
            # Используем asyncio для отправки
            try:
                loop = asyncio.get_running_loop()
                asyncio.create_task(self._send_message_async(message))
            except RuntimeError:
                # Если нет event loop, пытаемся создать новый
                try:
                    loop = asyncio.new_event_loop()
                    asyncio.set_event_loop(loop)
                    loop.run_until_complete(self._send_message_async(message))
                    loop.close()
                except Exception as e:
                    logging.getLogger(__name__).debug(f"Не удалось отправить ошибку в MAX (нет event loop): {e}")
        except Exception as e:
            # Логируем ошибку обработки, но не падаем
            logging.getLogger(__name__).debug(f"Ошибка в emit TelegramLogHandler: {e}")


def setup_telegram_error_logging() -> Optional[TelegramLogHandler]:
    """
    Настраивает отправку ошибок в чат MAX.
    Требует переменные окружения:
    - ERROR_LOG_CHAT_ID: id чата MAX
    - MAX_BOT_TOKEN: токен бота
    Топики не используются.
    """
    error_chat_id = os.getenv('ERROR_LOG_CHAT_ID')
    bot_token = os.getenv('MAX_BOT_TOKEN')

    if not error_chat_id or not bot_token:
        return None

    try:
        chat_id = int(error_chat_id) if error_chat_id else None
        if not chat_id:
            return None

        telegram_handler = TelegramLogHandler(chat_id, None, bot_token)
        
        # Используем тот же форматтер, что и у существующих handlers
        logger = logging.getLogger()
        existing_formatter = None
        for handler in logger.handlers:
            if hasattr(handler, 'formatter') and handler.formatter:
                existing_formatter = handler.formatter
                break
        
        if existing_formatter:
            telegram_handler.setFormatter(existing_formatter)
        
        logger.addHandler(telegram_handler)
        logging.info(f"MAX handler для ошибок настроен (chat_id={chat_id})")
        return telegram_handler
    except (ValueError, TypeError) as e:
        logging.warning(f"ERROR_LOG_CHAT_ID имеет неверный формат: {e}")
        return None

