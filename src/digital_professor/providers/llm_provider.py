# providers/llm_provider.py
"""
Унифицированный LLM провайдер с fallback логикой.
Приоритет: OpenRouter (FREE → PAID) → Yandex GPT
"""

import os
import asyncio
import aiohttp
import logging
import time
import random
from typing import Optional, List, Dict, Any, Tuple
from dataclasses import dataclass

from .openrouter_key_manager import get_key_manager, OpenRouterKeyManager

logger = logging.getLogger(__name__)


@dataclass
class LLMResponse:
    """Результат вызова LLM"""
    content: str
    provider: str  # "openrouter" или "yandex"
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    latency_ms: int = 0


class LLMProvider:
    """
    Унифицированный провайдер LLM с автоматическим fallback.
    
    Порядок вызова:
    1. OpenRouter FREE модели
    2. OpenRouter PAID модель
    3. Yandex GPT (fallback)
    """
    
    OPENROUTER_BASE_URL = "https://api.proxyapi.ru/openrouter/v1"
    
    def __init__(self):
        self._session: Optional[aiohttp.ClientSession] = None
        self._timeout = aiohttp.ClientTimeout(total=60)
        self._key_manager = get_key_manager()
        
        # Yandex fallback config
        self._yandex_folder_id = os.getenv("FOLDER_ID")
        self._yandex_api_key = os.getenv("AUTH_KEY")
        
        self._default_temperature = 0.3
        self._default_max_tokens = 3000
        self._default_top_p = 1.0

    async def aclose(self) -> None:
        """Закрывает aiohttp-сессию (полезно для офлайн-раннеров/скриптов)."""
        if self._session and not self._session.closed:
            await self._session.close()
    
    async def _ensure_session(self) -> aiohttp.ClientSession:
        """Создаёт или возвращает HTTP сессию"""
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=self._timeout)
        return self._session
    
    async def _call_single_model(
        self,
        session: aiohttp.ClientSession,
        url: str,
        api_key: str,
        model: str,
        messages: List[Dict[str, str]],
        temperature: float,
        top_p: float,
        max_tokens: int,
        timeout_seconds: float = 60.0,
    ) -> Optional[LLMResponse]:
        """Вызов одной модели OpenRouter с таймаутом"""
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "top_p": top_p,
            "max_tokens": max_tokens,
        }
        
        start_ts = time.perf_counter()
        
        try:
            # Используем короткий таймаут для бесплатных моделей
            timeout = aiohttp.ClientTimeout(total=timeout_seconds)
            async with session.post(url, json=payload, headers=headers, timeout=timeout) as resp:
                latency_ms = int((time.perf_counter() - start_ts) * 1000)
                
                if resp.status == 200:
                    data = await resp.json()
                    self._key_manager.mark_success(model)
                    
                    usage = data.get("usage", {}) or {}
                    content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
                    
                    return LLMResponse(
                        content=content,
                        provider="openrouter",
                        model=model,
                        input_tokens=int(usage.get("prompt_tokens", 0)),
                        output_tokens=int(usage.get("completion_tokens", 0)),
                        total_tokens=int(usage.get("total_tokens", 0)),
                        latency_ms=latency_ms,
                    )
                
                elif resp.status == 429:
                    self._key_manager.mark_429_error(model)
                    logger.debug(f"OpenRouter 429 для {model}")
                    return None
                
                else:
                    self._key_manager.mark_other_error(model)
                    try:
                        error_data = await resp.json()
                        logger.debug(f"OpenRouter ошибка {resp.status} для {model}: {error_data}")
                    except:
                        logger.debug(f"OpenRouter ошибка {resp.status} для {model}")
                    return None
                    
        except asyncio.TimeoutError:
            self._key_manager.mark_other_error(model)
            logger.debug(f"OpenRouter timeout для {model} (таймаут {timeout_seconds}с)")
            return None
            
        except Exception as e:
            self._key_manager.mark_other_error(model)
            logger.debug(f"OpenRouter исключение для {model}: {e}")
            return None
    
    async def _call_openrouter(
        self,
        messages: List[Dict[str, str]],
        temperature: float,
        top_p: float,
        max_tokens: int,
    ) -> Optional[LLMResponse]:
        """
        Вызов OpenRouter API с параллельными запросами к доступным моделям.
        Использует первый успешный ответ для ускорения работы.
        """
        if not self._key_manager.is_available():
            logger.debug("OpenRouter недоступен (нет API ключа)")
            return None
        
        session = await self._ensure_session()
        url = f"{self.OPENROUTER_BASE_URL}/chat/completions"
        
        # Собираем все доступные модели
        available_models = []
        api_key = self._key_manager.api_key
        
        # 1. Сначала пробуем бесплатные модели параллельно
        for model_id in self._key_manager.FREE_MODELS:
            state = self._key_manager.models.get(model_id)
            if state and not state.is_sleeping():
                available_models.append((model_id, 15.0))  # Короткий таймаут для бесплатных (15 сек)
        
        # 2. Если есть доступные бесплатные - запускаем параллельно
        if available_models:
            model_names = [m[0] for m in available_models]
            logger.info(f"🚀 Параллельный запрос к {len(available_models)} бесплатным моделям: {', '.join(model_names)}")
            tasks = {
                asyncio.create_task(
                    self._call_single_model(
                        session, url, api_key, model, messages, temperature, top_p, max_tokens, timeout
                    )
                ): model
                for model, timeout in available_models
            }
            
            # Ждем первый успешный ответ
            for task in asyncio.as_completed(tasks.keys()):
                try:
                    result = await task
                    if result:
                        # Отменяем остальные задачи и ждем их отмены
                        cancelled_count = 0
                        for t, m in tasks.items():
                            if t != task and not t.done():
                                t.cancel()
                                cancelled_count += 1
                        if cancelled_count > 0:
                            logger.debug(f"✅ Получен ответ от {result.model}, отменено {cancelled_count} других запросов")
                        # Даем время на отмену (не ждем долго)
                        await asyncio.sleep(0.1)
                        return result
                except asyncio.CancelledError:
                    # Игнорируем отмененные задачи
                    pass
                except Exception as e:
                    model_name = tasks.get(task, "unknown")
                    logger.debug(f"Ошибка при запросе к {model_name}: {e}")
            
            # Если все бесплатные не сработали, отменяем оставшиеся задачи
            for task in tasks.keys():
                if not task.done():
                    task.cancel()
                    try:
                        await task
                    except (asyncio.CancelledError, Exception):
                        pass
        
        if self._key_manager.force_model:
            forced_state = self._key_manager.models.get(self._key_manager.force_model)
            if forced_state and not forced_state.is_sleeping():
                logger.debug(f"Используем принудительно заданную OpenRouter модель: {self._key_manager.force_model}")
                result = await self._call_single_model(
                    session,
                    url,
                    api_key,
                    self._key_manager.force_model,
                    messages,
                    temperature,
                    top_p,
                    max_tokens,
                    timeout_seconds=60.0,
                )
                if result:
                    return result

        # 3. Если бесплатные не сработали - пробуем платную модель
        paid_state = self._key_manager.models.get(self._key_manager.PAID_MODEL)
        if paid_state and not paid_state.is_sleeping():
            logger.debug("Переключение на платную модель")
            result = await self._call_single_model(
                session, url, api_key, self._key_manager.PAID_MODEL,
                messages, temperature, top_p, max_tokens, timeout_seconds=60.0  # Длинный таймаут для платной
            )
            if result:
                return result
        
        logger.warning("Все модели OpenRouter недоступны или не ответили")
        return None
    
    async def _call_yandex(
        self,
        messages: List[Dict[str, str]],
        temperature: float,
        max_tokens: int,
    ) -> Optional[LLMResponse]:
        """Вызов Yandex GPT (fallback)"""
        if not self._yandex_folder_id or not self._yandex_api_key:
            logger.warning("Yandex GPT не настроен (FOLDER_ID или AUTH_KEY отсутствуют)")
            return None
        
        try:
            # Импортируем YandexChatModel из существующего llm.py
            from digital_professor.llm import YandexChatModel
            from langchain_core.messages import SystemMessage, HumanMessage, AIMessage
            
            # Создаём экземпляр модели
            yandex_model = YandexChatModel(
                folder_id=self._yandex_folder_id,
                api_key=self._yandex_api_key,
                temperature=temperature,
                max_tokens=max_tokens,
            )
            
            # Конвертируем сообщения в формат Langchain
            langchain_messages = []
            for msg in messages:
                role = msg.get("role", "user")
                content = msg.get("content", "")
                
                if role == "system":
                    langchain_messages.append(SystemMessage(content=content))
                elif role == "assistant":
                    langchain_messages.append(AIMessage(content=content))
                else:
                    langchain_messages.append(HumanMessage(content=content))
            
            start_ts = time.perf_counter()
            response = await yandex_model.ainvoke(langchain_messages)
            latency_ms = int((time.perf_counter() - start_ts) * 1000)
            
            content = response.content if hasattr(response, "content") else str(response)
            
            return LLMResponse(
                content=content,
                provider="yandex",
                model="yandexgpt",
                latency_ms=latency_ms,
            )
            
        except Exception as e:
            logger.error(f"Yandex GPT ошибка: {e}")
            return None
    
    async def call(
        self,
        messages: List[Dict[str, str]],
        *,
        temperature: Optional[float] = None,
        top_p: Optional[float] = None,
        max_tokens: Optional[int] = None,
        usage_event: str = "llm_call",
        user_id: Optional[int] = None,
    ) -> LLMResponse:
        """
        Основной метод вызова LLM с автоматическим fallback.
        
        Args:
            messages: Список сообщений [{"role": "system/user/assistant", "content": "..."}]
            temperature: Температура генерации (default: 0.3)
            max_tokens: Максимум токенов ответа (default: 3000)
            usage_event: Событие для логирования
            user_id: ID пользователя для логирования
            
        Returns:
            LLMResponse с результатом
            
        Raises:
            RuntimeError: Если все провайдеры недоступны
        """
        temp = temperature if temperature is not None else self._default_temperature
        tp = top_p if top_p is not None else self._default_top_p
        max_tok = max_tokens if max_tokens is not None else self._default_max_tokens
        
        # 1. Пробуем OpenRouter
        response = await self._call_openrouter(messages, temp, tp, max_tok)
        
        if response:
            self._log_usage(response, usage_event, user_id)
            return response
        
        # 2. Fallback на Yandex
        if (os.getenv("LLM_DISABLE_YANDEX_FALLBACK") or "").strip() in {"1", "true", "yes", "on"}:
            raise RuntimeError("OpenRouter недоступен, а Yandex fallback отключён (LLM_DISABLE_YANDEX_FALLBACK=1)")

        logger.warning("=" * 50)
        logger.warning("⚠️ OpenRouter полностью недоступен")
        logger.warning("🔄 Переключение на Yandex GPT (FALLBACK)")
        logger.warning("=" * 50)
        response = await self._call_yandex(messages, temp, max_tok)
        
        if response:
            self._log_usage(response, usage_event, user_id)
            return response
        
        # 3. Все провайдеры недоступны
        raise RuntimeError("Все LLM провайдеры недоступны")
    
    async def call_with_system(
        self,
        system_prompt: str,
        user_prompt: str,
        *,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        usage_event: str = "llm_call",
        user_id: Optional[int] = None,
    ) -> LLMResponse:
        """
        Удобный метод для вызова с системным и пользовательским промптами.
        """
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]
        return await self.call(
            messages,
            temperature=temperature,
            max_tokens=max_tokens,
            usage_event=usage_event,
            user_id=user_id,
        )
    
    def _log_usage(self, response: LLMResponse, event: str, user_id: Optional[int]):
        """Логирует использование токенов с информацией о типе модели"""
        # Определяем тип модели (FREE/PAID)
        model_type = "🆓 FREE" if ":free" in response.model else "💰 PAID"
        if response.provider == "yandex":
            model_type = "🔄 FALLBACK"
        
        logger.info(
            f"🤖 LLM вызов: {model_type} | {response.provider}/{response.model} | "
            f"event={event} | tokens={response.total_tokens} | {response.latency_ms}ms"
        )
        
        # Сохраняем в БД
        try:
            from digital_professor.database import add_llm_token_usage
            add_llm_token_usage(
                event=event,
                model=response.model,
                total_tokens=response.total_tokens,
                input_tokens=response.input_tokens if response.input_tokens else None,
                output_tokens=response.output_tokens if response.output_tokens else None,
                user_id=user_id,
                provider=response.provider,
            )
        except Exception as e:
            logger.error(f"Ошибка сохранения метрик LLM: {e}")
    
    def is_available(self) -> bool:
        """Проверяет, доступен ли хотя бы один провайдер"""
        return (
            self._key_manager.is_available() or 
            (self._yandex_folder_id is not None and self._yandex_api_key is not None)
        )
    
    async def close(self):
        """Закрывает HTTP сессию"""
        if self._session and not self._session.closed:
            await self._session.close()


# Глобальный экземпляр провайдера
_llm_provider: Optional[LLMProvider] = None


def get_llm_provider() -> LLMProvider:
    """Возвращает глобальный экземпляр LLM провайдера"""
    global _llm_provider
    if _llm_provider is None:
        _llm_provider = LLMProvider()
    return _llm_provider

