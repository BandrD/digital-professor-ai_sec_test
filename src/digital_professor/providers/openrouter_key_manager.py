# providers/openrouter_key_manager.py
"""
Менеджер ключей OpenRouter с поддержкой бесплатных и платных моделей.
Автоматическое переключение при rate limiting (429).
"""

import os
import time
import logging
from typing import Optional, List, Dict, Tuple
from dataclasses import dataclass, field
from enum import Enum

logger = logging.getLogger(__name__)


class KeyType(Enum):
    """Тип ключа/модели: бесплатный или платный"""
    FREE = "free"
    PAID = "paid"


@dataclass
class ModelState:
    """Состояние модели (для отслеживания rate limits)"""
    model: str
    key_type: KeyType
    consecutive_429_count: int = 0
    sleep_until: Optional[float] = None
    
    def is_sleeping(self) -> bool:
        """Проверяет, заблокирована ли модель"""
        if self.sleep_until is None:
            return False
        if time.time() >= self.sleep_until:
            # Время блокировки истекло - сбрасываем
            self.sleep_until = None
            self.consecutive_429_count = 0
            return False
        return True
    
    def mark_429_error(self):
        """Отмечает ошибку 429 (rate limit) — сразу переключаемся на следующую модель"""
        self.consecutive_429_count += 1
        # Блокируем сразу после первой ошибки 429 — переключаемся на следующую модель
        self.sleep_until = time.time() + 300  # 5 минут
        logger.warning(
            f"Модель {self.model} заблокирована на 5 минут после ошибки 429"
        )
    
    def mark_success(self):
        """Сбрасывает счетчик ошибок при успешном запросе"""
        self.consecutive_429_count = 0
    
    def mark_other_error(self):
        """Сбрасывает счетчик 429 при других ошибках"""
        self.consecutive_429_count = 0


class OpenRouterKeyManager:
    """
    Менеджер ключей и моделей OpenRouter.
    
    Приоритет выбора моделей:
    1. Бесплатные модели (FREE) - по порядку в списке
    2. Платная модель (PAID) - когда все бесплатные заблокированы
    """
    
    # Конфигурация моделей
    # По одной попытке на каждую бесплатную модель, затем платная
    FREE_MODELS = [
        "qwen/qwen3-235b-a22b:free",
        "mistralai/mistral-small-3.1-24b-instruct:free",
    ]
    PAID_MODEL = "qwen/qwen3-235b-a22b-2507"
    
    def __init__(self):
        self.api_key: Optional[str] = None
        self.force_model: Optional[str] = None
        self.disable_free: bool = False
        self.models: Dict[str, ModelState] = {}
        self._load_config()
    
    def _load_config(self):
        """Загружает API ключ из переменных окружения"""
        self.api_key = os.getenv("API_KEY")
        self.force_model = os.getenv("OPENROUTER_FORCE_MODEL") or None
        self.disable_free = (os.getenv("OPENROUTER_DISABLE_FREE") or "").strip() in {"1", "true", "yes", "on"}
        
        if not self.api_key:
            logger.warning("API_KEY не найден в переменных окружения. OpenRouter недоступен.")
            return
        
        # Инициализируем состояния моделей
        # Если задан OPENROUTER_FORCE_MODEL — используем строго его (как paid), без free/перебора
        if self.force_model:
            self.models[self.force_model] = ModelState(model=self.force_model, key_type=KeyType.PAID)
        else:
            if not self.disable_free:
                for model in self.FREE_MODELS:
                    self.models[model] = ModelState(model=model, key_type=KeyType.FREE)
            self.models[self.PAID_MODEL] = ModelState(model=self.PAID_MODEL, key_type=KeyType.PAID)
        
        logger.info(
            f"OpenRouter инициализирован: {len(self.FREE_MODELS)} бесплатных моделей, "
            f"1 платная модель"
        )
    
    def get_available_model(self) -> Optional[Tuple[str, str]]:
        """
        Возвращает доступную модель и API ключ.
        
        Returns:
            Tuple[api_key, model_id] или None если ничего не доступно
        """
        if not self.api_key:
            return None

        # Принудительный фикс модели
        if self.force_model:
            state = self.models.get(self.force_model)
            if state and not state.is_sleeping():
                return (self.api_key, self.force_model)
            # если вдруг sleeping — всё равно возвращаем её (пусть LLMProvider решает, что делать)
            return (self.api_key, self.force_model)
        
        # 1. Сначала пробуем бесплатные модели
        if not self.disable_free:
            for model_id in self.FREE_MODELS:
                state = self.models.get(model_id)
                if state and not state.is_sleeping():
                    return (self.api_key, model_id)
        
        # 2. Если все бесплатные заблокированы - используем платную
        paid_state = self.models.get(self.PAID_MODEL)
        if paid_state and not paid_state.is_sleeping():
            logger.warning("=" * 50)
            logger.warning("⚠️ Все бесплатные модели заблокированы (rate limit)")
            logger.warning(f"💰 Переключение на ПЛАТНУЮ модель: {self.PAID_MODEL}")
            logger.warning("=" * 50)
            return (self.api_key, self.PAID_MODEL)
        
        # 3. Если даже платная заблокирована - возвращаем первую бесплатную для retry
        logger.warning("⚠️ Все модели OpenRouter заблокированы, возвращаем первую для retry")
        return (self.api_key, self.FREE_MODELS[0])
    
    def mark_429_error(self, model: str):
        """Отмечает ошибку 429 для модели"""
        state = self.models.get(model)
        if state:
            state.mark_429_error()
    
    def mark_success(self, model: str):
        """Отмечает успешный запрос для модели"""
        state = self.models.get(model)
        if state:
            state.mark_success()
    
    def mark_other_error(self, model: str):
        """Отмечает другую ошибку для модели"""
        state = self.models.get(model)
        if state:
            state.mark_other_error()
    
    def is_available(self) -> bool:
        """Проверяет, доступен ли OpenRouter"""
        return self.api_key is not None
    
    def get_model_status(self) -> Dict[str, dict]:
        """Возвращает статус всех моделей (для отладки)"""
        status = {}
        for model_id, state in self.models.items():
            status[model_id] = {
                "type": state.key_type.value,
                "is_sleeping": state.is_sleeping(),
                "consecutive_429": state.consecutive_429_count,
                "sleep_until": state.sleep_until,
            }
        return status


# Глобальный экземпляр менеджера
_key_manager: Optional[OpenRouterKeyManager] = None


def get_key_manager() -> OpenRouterKeyManager:
    """Возвращает глобальный экземпляр менеджера ключей"""
    global _key_manager
    if _key_manager is None:
        _key_manager = OpenRouterKeyManager()
    return _key_manager

