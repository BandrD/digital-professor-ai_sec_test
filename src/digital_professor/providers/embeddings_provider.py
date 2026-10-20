# providers/embeddings_provider.py
"""
Унифицированный провайдер эмбеддингов с fallback логикой.
Приоритет: OpenAI (через ProxyAPI) → Yandex Embeddings

Важно: OpenAI embeddings имеют размерность 1536, Yandex - 256.
При смене провайдера требуется пересоздание ChromaDB.
"""

import os
import logging
import hashlib
from pathlib import Path
from typing import List, Optional
import openai

logger = logging.getLogger(__name__)


# Маркер файл для отслеживания миграции
MIGRATION_MARKER_FILENAME = ".embeddings_migrated_to_openai"


class OpenAIEmbeddings:
    """
    Класс эмбеддингов через OpenAI API (ProxyAPI).
    Совместим с интерфейсом Langchain Embeddings.
    """
    
    OPENAI_BASE_URL = "https://api.proxyapi.ru/openai/v1"
    MODEL = "text-embedding-3-small"
    EMBEDDING_DIMENSION = 1536
    
    def __init__(self, api_key: str):
        self.api_key = api_key
        self.client = openai.OpenAI(
            api_key=api_key,
            base_url=self.OPENAI_BASE_URL,
        )
        self.logger = logging.getLogger(__name__)
    
    def _get_embedding(self, text: str) -> List[float]:
        """Получает эмбеддинг для одного текста"""
        # Очищаем текст от лишних пробелов и переносов
        fixed_text = ' '.join(text.split())
        
        if not fixed_text:
            return [0.0] * self.EMBEDDING_DIMENSION
        
        try:
            response = self.client.embeddings.create(
                input=fixed_text,
                model=self.MODEL,
            )
            
            embedding = response.data[0].embedding
            
            # Логируем использование токенов
            if hasattr(response, 'usage') and response.usage:
                total_tokens = getattr(response.usage, 'total_tokens', 0)
                if total_tokens > 0:
                    self._log_token_usage(total_tokens)
            
            return embedding
            
        except Exception as e:
            self.logger.error(f"Ошибка получения OpenAI эмбеддинга: {e}")
            raise
    
    def _log_token_usage(self, total_tokens: int):
        """Логирует использование токенов для эмбеддингов"""
        self.logger.info(
            f"📊 Embedding: 💰 PAID | openai/{self.MODEL} | tokens={total_tokens}"
        )
        try:
            from digital_professor.database import add_llm_token_usage
            add_llm_token_usage(
                event="embedding_generation",
                model=self.MODEL,
                total_tokens=total_tokens,
                input_tokens=total_tokens,
                output_tokens=None,
                user_id=None,
                provider='openai'
            )
        except Exception as e:
            self.logger.error(f"Ошибка логирования токенов эмбеддинга: {e}")
    
    def embed_documents(self, texts: List[str]) -> List[List[float]]:
        """
        Генерирует эмбеддинги для списка документов.
        Langchain-совместимый интерфейс.
        """
        embeddings = []
        for text in texts:
            try:
                emb = self._get_embedding(text)
                embeddings.append(emb)
            except Exception as e:
                self.logger.error(f"Ошибка эмбеддинга документа: {e}")
                # Возвращаем нулевой вектор при ошибке
                embeddings.append([0.0] * self.EMBEDDING_DIMENSION)
        return embeddings
    
    def embed_query(self, text: str) -> List[float]:
        """
        Генерирует эмбеддинг для поискового запроса.
        Langchain-совместимый интерфейс.
        """
        return self._get_embedding(text)


class YandexEmbeddings:
    """
    Класс эмбеддингов через Yandex Cloud (fallback).
    Совместим с интерфейсом Langchain Embeddings.
    """
    
    EMBEDDING_DIMENSION = 256
    
    def __init__(self, api_key: str, folder_id: str):
        self.api_key = api_key
        self.folder_id = folder_id
        self.client = openai.OpenAI(
            api_key=api_key,
            base_url="https://llm.api.cloud.yandex.net/v1/",
        )
        self.logger = logging.getLogger(__name__)
    
    def _get_embedding(self, text: str, model_type: str) -> List[float]:
        """Получает эмбеддинг от Yandex"""
        fixed_text = ' '.join(text.split())
        
        if not fixed_text:
            return [0.0] * self.EMBEDDING_DIMENSION
        
        try:
            response = self.client.embeddings.create(
                input=fixed_text,
                model=f"emb://{self.folder_id}/{model_type}/latest",
                encoding_format="float",
            )
            self.logger.info(
                f"📊 Embedding: 🔄 FALLBACK | yandex/{model_type}"
            )
            return response.data[0].embedding
            
        except Exception as e:
            self.logger.error(f"Ошибка получения Yandex эмбеддинга: {e}")
            return [0.0] * self.EMBEDDING_DIMENSION
    
    def embed_documents(self, texts: List[str]) -> List[List[float]]:
        """Для документов используем text-search-doc"""
        return [self._get_embedding(text, "text-search-doc") for text in texts]
    
    def embed_query(self, text: str) -> List[float]:
        """Для запросов используем text-search-query"""
        return self._get_embedding(text, "text-search-query")


class EmbeddingsProvider:
    """
    Унифицированный провайдер эмбеддингов с автоматическим fallback.
    
    Порядок:
    1. OpenAI (через ProxyAPI) - основной
    2. Yandex Embeddings - fallback
    
    Важно: При смене провайдера размерность меняется (1536 vs 256),
    поэтому требуется пересоздание ChromaDB базы.
    """
    
    def __init__(self):
        self._api_key = os.getenv("API_KEY")
        self._yandex_api_key = os.getenv("AUTH_KEY")
        self._yandex_folder_id = os.getenv("FOLDER_ID")
        
        self._openai_embeddings: Optional[OpenAIEmbeddings] = None
        self._yandex_embeddings: Optional[YandexEmbeddings] = None
        
        self._current_provider: Optional[str] = None
        self._initialize()
    
    def _initialize(self):
        """Инициализирует доступные провайдеры"""
        # Пробуем OpenAI
        if self._api_key:
            try:
                self._openai_embeddings = OpenAIEmbeddings(self._api_key)
                self._current_provider = "openai"
                logger.info("EmbeddingsProvider: OpenAI инициализирован (основной)")
            except Exception as e:
                logger.warning(f"EmbeddingsProvider: Не удалось инициализировать OpenAI: {e}")
        
        # Инициализируем Yandex как fallback
        if self._yandex_api_key and self._yandex_folder_id:
            try:
                self._yandex_embeddings = YandexEmbeddings(
                    self._yandex_api_key, 
                    self._yandex_folder_id
                )
                if not self._current_provider:
                    self._current_provider = "yandex"
                logger.info("EmbeddingsProvider: Yandex инициализирован (fallback)")
            except Exception as e:
                logger.warning(f"EmbeddingsProvider: Не удалось инициализировать Yandex: {e}")
        
        if not self._current_provider:
            logger.error("EmbeddingsProvider: Нет доступных провайдеров эмбеддингов!")
    
    def get_embeddings_instance(self):
        """
        Возвращает экземпляр эмбеддингов для использования с ChromaDB.
        Langchain-совместимый интерфейс.
        """
        if self._openai_embeddings:
            return self._openai_embeddings
        if self._yandex_embeddings:
            return self._yandex_embeddings
        raise RuntimeError("Нет доступных провайдеров эмбеддингов")
    
    def embed_query(self, text: str) -> List[float]:
        """Получает эмбеддинг для запроса с автоматическим fallback"""
        # Пробуем OpenAI
        if self._openai_embeddings:
            try:
                return self._openai_embeddings.embed_query(text)
            except Exception as e:
                logger.warning(f"⚠️ OpenAI embedding failed: {e}")
                logger.info("🔄 Переключение на Yandex Embeddings (fallback)")
        
        # Fallback на Yandex
        if self._yandex_embeddings:
            return self._yandex_embeddings.embed_query(text)
        
        raise RuntimeError("Нет доступных провайдеров эмбеддингов")
    
    def embed_documents(self, texts: List[str]) -> List[List[float]]:
        """Получает эмбеддинги для документов с автоматическим fallback"""
        # Пробуем OpenAI
        if self._openai_embeddings:
            try:
                return self._openai_embeddings.embed_documents(texts)
            except Exception as e:
                logger.warning(f"⚠️ OpenAI embeddings failed: {e}")
                logger.info("🔄 Переключение на Yandex Embeddings (fallback)")
        
        # Fallback на Yandex
        if self._yandex_embeddings:
            return self._yandex_embeddings.embed_documents(texts)
        
        raise RuntimeError("Нет доступных провайдеров эмбеддингов")
    
    def get_current_provider(self) -> str:
        """Возвращает текущий активный провайдер"""
        return self._current_provider or "none"
    
    def get_embedding_dimension(self) -> int:
        """Возвращает размерность эмбеддингов текущего провайдера"""
        if self._current_provider == "openai":
            return OpenAIEmbeddings.EMBEDDING_DIMENSION  # 1536
        elif self._current_provider == "yandex":
            return YandexEmbeddings.EMBEDDING_DIMENSION  # 256
        return 0
    
    def is_available(self) -> bool:
        """Проверяет, доступен ли хотя бы один провайдер"""
        return self._current_provider is not None
    
    @staticmethod
    def needs_migration(persist_directory: str) -> bool:
        """
        Проверяет, нужна ли миграция ChromaDB на OpenAI embeddings.
        
        Миграция нужна если:
        1. База существует
        2. Нет маркера миграции
        
        Args:
            persist_directory: Путь к директории ChromaDB
            
        Returns:
            True если нужна миграция
        """
        db_path = Path(persist_directory)
        marker_path = db_path / MIGRATION_MARKER_FILENAME
        
        # Если директории нет - миграция не нужна (будет создана новая база)
        if not db_path.exists():
            return False
        
        # Если маркер есть - миграция уже выполнена
        if marker_path.exists():
            return False
        
        # База существует, но маркера нет - нужна миграция
        return True
    
    @staticmethod
    def mark_migration_complete(persist_directory: str):
        """
        Создаёт маркер завершения миграции.
        
        Args:
            persist_directory: Путь к директории ChromaDB
        """
        db_path = Path(persist_directory)
        db_path.mkdir(parents=True, exist_ok=True)
        
        marker_path = db_path / MIGRATION_MARKER_FILENAME
        marker_path.write_text(
            f"Migration to OpenAI embeddings completed.\n"
            f"Model: {OpenAIEmbeddings.MODEL}\n"
            f"Dimension: {OpenAIEmbeddings.EMBEDDING_DIMENSION}\n"
        )
        logger.info(f"Маркер миграции создан: {marker_path}")


# Глобальный экземпляр провайдера
_embeddings_provider: Optional[EmbeddingsProvider] = None


def get_embeddings_provider() -> EmbeddingsProvider:
    """Возвращает глобальный экземпляр провайдера эмбеддингов"""
    global _embeddings_provider
    if _embeddings_provider is None:
        _embeddings_provider = EmbeddingsProvider()
    return _embeddings_provider

