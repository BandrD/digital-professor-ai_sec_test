# rag/pipeline.py
"""
Основной класс RAG-пайплайна с поддержкой автоматической миграции эмбеддингов.
"""

import os
import logging
from typing import List
from pathlib import Path

from langchain.docstore.document import Document
from dotenv import load_dotenv

from .loader import load_all_chunks, convert_chunks_to_documents
from .database import (
    initialize_vectorstore, 
    populate_vectorstore, 
    get_embeddings_instance,
    needs_migration,
)
from .retriever import HybridRetriever

load_dotenv()

logger = logging.getLogger(__name__)


class RagPipeline:
    """
    Основной класс RAG-пайплайна.
    
    Автоматически:
    - Мигрирует ChromaDB при смене провайдера эмбеддингов
    - Использует OpenAI embeddings (через ProxyAPI) как основной провайдер
    - Fallback на Yandex embeddings при недоступности OpenAI
    """
    
    def __init__(
        self, 
        persist_directory: str = "./data/chroma_db_gitverse", 
        collection_name: str = "Gitverse"
    ):
        """
        Инициализирует RAG-пайплайн.
        
        Args:
            persist_directory: Путь к директории ChromaDB
            collection_name: Имя коллекции
        """
        self.persist_directory = persist_directory
        self.collection_name = collection_name
        self.documents: List[Document] = []
        self.vectorstore = None
        self.hybrid_retriever = None
        self.is_initialized = False

        # Получаем провайдер эмбеддингов
        self._init_embeddings_provider()
    
    def _init_embeddings_provider(self):
        """Инициализирует провайдер эмбеддингов"""
        try:
            from digital_professor.providers import get_embeddings_provider
            self._embeddings_provider = get_embeddings_provider()
            self._embeddings = self._embeddings_provider.get_embeddings_instance()
            
            logger.info(
                f"RAG Pipeline: эмбеддинги инициализированы, "
                f"провайдер: {self._embeddings_provider.get_current_provider()}, "
                f"размерность: {self._embeddings_provider.get_embedding_dimension()}"
            )
        except Exception as e:
            logger.error(f"Ошибка инициализации провайдера эмбеддингов: {e}")
            # Fallback на legacy
            self._embeddings_provider = None
            self._embeddings = self._get_legacy_embeddings()
    
    def _get_legacy_embeddings(self):
        """Legacy метод получения эмбеддингов (для обратной совместимости)"""
        from .database import YandexEmbeddings
        
        api_key = os.getenv('AUTH_KEY')
        folder_id = os.getenv('FOLDER_ID')
        
        if not api_key or not folder_id:
            raise ValueError("AUTH_KEY или FOLDER_ID не найдены в переменных окружения")
        
        return YandexEmbeddings(api_key=api_key, folder_id=folder_id)

    def load_and_prepare_data(self, json_files: List[str]):
        """Загружает данные из JSON файлов и преобразует их в документы."""
        logger.info("Загрузка данных из JSON файлов...")
        all_chunks_data = load_all_chunks(json_files)
        self.documents = convert_chunks_to_documents(all_chunks_data)
        logger.info(f"Загружено и преобразовано документов: {len(self.documents)}")

    def setup_database(self, force_recreate: bool = False):
        """
        Инициализирует или загружает ChromaDB.
        Автоматически выполняет миграцию при смене провайдера эмбеддингов.
        
        Args:
            force_recreate: Если True, удаляет старую БД и создает новую
        """
        logger.info("Настройка ChromaDB...")
        
        # Принудительное пересоздание
        if force_recreate and os.path.exists(self.persist_directory):
            import shutil
            logger.warning(f"Принудительное пересоздание: удаление {self.persist_directory}")
            shutil.rmtree(self.persist_directory)

        # Проверяем нужна ли миграция (выполняется внутри initialize_vectorstore)
        if needs_migration(self.persist_directory):
            logger.info("Требуется миграция на OpenAI embeddings")
        
        # Инициализируем vectorstore
        self.vectorstore = initialize_vectorstore(
            self.persist_directory, 
            self.collection_name, 
            self._embeddings
        )
        
        logger.info("ChromaDB настроена")

    def populate_database(self):
        """Наполняет ChromaDB документами, если она пуста."""
        if self.vectorstore is None or not self.documents:
            logger.error("Vectorstore или документы не загружены")
            return

        # Проверяем, нужно ли наполнять базу
        db_path = Path(self.persist_directory)
        if not db_path.exists() or not any(db_path.iterdir()):
            logger.info("ChromaDB пуста, наполняем...")
            populate_vectorstore(self.vectorstore, self.documents)
            return

        # Проверяем есть ли документы в базе
        try:
            test_docs = self.vectorstore.similarity_search("test", k=1)
            if not test_docs:
                raise Exception("База пуста")
            logger.info("ChromaDB уже наполнена, пропускаем")
        except Exception as e:
            logger.info(f"ChromaDB пуста или ошибка: {e}. Наполняем...")
            populate_vectorstore(self.vectorstore, self.documents)

    def setup_retriever(self):
        """Инициализирует гибридный ретривер."""
        if self.vectorstore is None or not self.documents:
            logger.error("Vectorstore или документы не загружены. Невозможно настроить ретривер.")
            return

        logger.info("Настройка Hybrid Retriever...")
        self.hybrid_retriever = HybridRetriever(
            vectorstore=self.vectorstore,
            documents=self.documents,
            k_semantic=5,
            k_keyword=5,
            k_final=3
        )
        logger.info("Hybrid Retriever готов")
        self.is_initialized = True

    async def initialize(self, json_files: List[str], force_recreate: bool = False):
        """
        Асинхронная инициализация всего пайплайна.
        
        Args:
            json_files: Список путей к JSON файлам с данными
            force_recreate: Принудительно пересоздать базу
        """
        try:
            self.load_and_prepare_data(json_files)
            self.setup_database(force_recreate=force_recreate)
            self.populate_database()
            self.setup_retriever()
            
            # Логируем информацию о провайдере
            if self._embeddings_provider:
                logger.info(
                    f"RAG Pipeline инициализирован. "
                    f"Эмбеддинги: {self._embeddings_provider.get_current_provider()}"
                )
            else:
                logger.info("RAG Pipeline инициализирован (legacy режим)")
                
        except Exception as e:
            logger.error(f"Ошибка инициализации RAG Pipeline: {e}", exc_info=True)
            raise

    def query(self, user_question: str) -> List[Document]:
        """
        Выполняет поиск релевантных документов по вопросу пользователя.
        
        Args:
            user_question: Вопрос пользователя
            
        Returns:
            Список релевантных документов
        """
        if not self.is_initialized or not self.hybrid_retriever:
            logger.error("RAG Pipeline не инициализирован")
            return []
        
        try:
            results = self.hybrid_retriever.retrieve(user_question)
            logger.info(f"Найдено {len(results)} документов для запроса: '{user_question[:50]}...'")
            return results
        except Exception as e:
            logger.error(f"Ошибка поиска для запроса '{user_question[:50]}...': {e}", exc_info=True)
            return []

    def get_embeddings_info(self) -> dict:
        """Возвращает информацию о текущем провайдере эмбеддингов"""
        if self._embeddings_provider:
            return {
                "provider": self._embeddings_provider.get_current_provider(),
                "dimension": self._embeddings_provider.get_embedding_dimension(),
                "available": self._embeddings_provider.is_available(),
            }
        return {
            "provider": "legacy_yandex",
            "dimension": 256,
            "available": True,
        }
