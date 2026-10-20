# rag/database.py
"""
Модуль для работы с ChromaDB и эмбеддингами.
Поддерживает автоматическую миграцию с Yandex на OpenAI embeddings.
"""

import os
import shutil
import hashlib
import logging
from pathlib import Path
from typing import List, Optional

from langchain.docstore.document import Document
from langchain_community.vectorstores import Chroma
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

# Маркер файл для отслеживания миграции
MIGRATION_MARKER_FILENAME = ".embeddings_migrated_to_openai"


def get_embeddings_instance():
    """
    Возвращает экземпляр эмбеддингов из нового провайдера.
    Langchain-совместимый интерфейс.
    """
    from digital_professor.providers import get_embeddings_provider
    return get_embeddings_provider().get_embeddings_instance()


def needs_migration(persist_directory: str) -> bool:
    """
    Проверяет, нужна ли миграция ChromaDB на OpenAI embeddings.
    
    Миграция нужна если:
    1. База существует (есть файлы внутри)
    2. Нет маркера миграции
    
    Args:
        persist_directory: Путь к директории ChromaDB
        
    Returns:
        True если нужна миграция
    """
    db_path = Path(persist_directory)
    marker_path = db_path / MIGRATION_MARKER_FILENAME
    
    # Если директории нет - миграция не нужна
    if not db_path.exists():
        logger.info(f"База {persist_directory} не существует - миграция не нужна")
        return False
    
    # Проверяем есть ли файлы базы
    has_db_files = any(
        f.name.endswith(('.sqlite3', '.bin')) 
        for f in db_path.rglob('*') 
        if f.is_file()
    )
    
    if not has_db_files:
        logger.info(f"База {persist_directory} пуста - миграция не нужна")
        return False
    
    # Если маркер есть - миграция уже выполнена
    if marker_path.exists():
        logger.info(f"Маркер миграции найден в {persist_directory} - миграция уже выполнена")
        return False
    
    # База существует, файлы есть, но маркера нет - нужна миграция
    logger.info(f"База {persist_directory} требует миграции на OpenAI embeddings")
    return True


def perform_migration(persist_directory: str):
    """
    Выполняет миграцию ChromaDB: удаляет старую базу.
    Новая база будет создана автоматически при следующей загрузке документов.
    
    Args:
        persist_directory: Путь к директории ChromaDB
    """
    db_path = Path(persist_directory)
    
    if not db_path.exists():
        return
    
    logger.warning("=" * 60)
    logger.warning("🔄 МИГРАЦИЯ CHROMADB: Yandex Embeddings → OpenAI Embeddings")
    logger.warning("=" * 60)
    logger.warning(f"📁 База данных: {persist_directory}")
    logger.warning("⚠️  Старые эмбеддинги (Yandex, размерность 256) несовместимы")
    logger.warning("⚠️  с новыми эмбеддингами (OpenAI, размерность 1536)")
    logger.warning("🗑️  Удаление старой базы для переиндексации...")
    
    try:
        # Удаляем старую базу (с Yandex embeddings размерностью 256)
        shutil.rmtree(persist_directory)
        
        # Создаём директорию заново
        db_path.mkdir(parents=True, exist_ok=True)
        
        logger.info("✅ Старая ChromaDB база успешно удалена")
        logger.info("📝 Новая база будет создана при загрузке документов")
        
    except Exception as e:
        logger.error("=" * 60)
        logger.error("❌ ОШИБКА МИГРАЦИИ CHROMADB")
        logger.error("=" * 60)
        logger.error(f"Путь: {persist_directory}")
        logger.error(f"Ошибка: {e}")
        logger.error("Попробуйте удалить директорию вручную и перезапустить")
        raise


def mark_migration_complete(persist_directory: str):
    """
    Создаёт маркер завершения миграции.
    
    Args:
        persist_directory: Путь к директории ChromaDB
    """
    import datetime
    
    db_path = Path(persist_directory)
    db_path.mkdir(parents=True, exist_ok=True)
    
    marker_path = db_path / MIGRATION_MARKER_FILENAME
    timestamp = datetime.datetime.now().isoformat()
    
    marker_path.write_text(
        f"Migration to OpenAI embeddings completed.\n"
        f"Timestamp: {timestamp}\n"
        f"Model: text-embedding-3-small\n"
        f"Dimension: 1536\n"
        f"Provider: OpenAI via ProxyAPI\n"
        f"Do not delete this file!\n"
    )
    
    logger.info("=" * 60)
    logger.info("✅ МИГРАЦИЯ CHROMADB ЗАВЕРШЕНА УСПЕШНО")
    logger.info("=" * 60)
    logger.info(f"📁 База данных: {persist_directory}")
    logger.info(f"🔢 Новая размерность эмбеддингов: 1536")
    logger.info(f"🤖 Модель: text-embedding-3-small (OpenAI)")
    logger.info(f"📝 Маркер миграции: {marker_path}")
    logger.info("=" * 60)


def initialize_vectorstore(
    persist_directory: str, 
    collection_name: str, 
    embeddings=None
) -> Chroma:
    """
    Инициализирует или загружает существующую ChromaDB.
    Автоматически выполняет миграцию при необходимости.
    
    Args:
        persist_directory: Путь к директории ChromaDB
        collection_name: Имя коллекции
        embeddings: Экземпляр эмбеддингов (если None - создаётся автоматически)
        
    Returns:
        Объект Chroma
    """
    # Проверяем необходимость миграции
    if needs_migration(persist_directory):
        perform_migration(persist_directory)
    
    # Получаем эмбеддинги
    if embeddings is None:
        embeddings = get_embeddings_instance()
    
    # Инициализируем ChromaDB
    vectorstore = Chroma(
        collection_name=collection_name,
        embedding_function=embeddings,
        persist_directory=persist_directory,
    )
    
    logger.info(f"ChromaDB инициализирована: {persist_directory}, коллекция: {collection_name}")
    return vectorstore


def populate_vectorstore(vectorstore: Chroma, documents: List[Document]) -> None:
    """
    Наполняет ChromaDB документами (идемпотентно).
    После успешного наполнения создаёт маркер миграции.
    
    Args:
        vectorstore: Объект Chroma
        documents: Список документов для добавления
    """
    if not documents:
        logger.info("Нет документов для добавления в ChromaDB")
        return
    
    logger.info(f"Добавление {len(documents)} документов в ChromaDB...")
    
    # Дедупликация документов по контенту
    unique_docs: List[Document] = []
    unique_ids: List[str] = []
    seen_ids = set()
    
    for doc in documents:
        # Генерируем стабильный ID на основе источника и контента
        try:
            src = str((doc.metadata or {}).get('source_file', '')) if hasattr(doc, 'metadata') else ''
        except Exception:
            src = ''
        
        content = doc.page_content or ''
        
        h = hashlib.sha1()
        h.update(src.encode('utf-8'))
        h.update(b'|')
        h.update(content.encode('utf-8'))
        doc_id = h.hexdigest()
        
        if doc_id in seen_ids:
            continue
        
        seen_ids.add(doc_id)
        unique_docs.append(doc)
        unique_ids.append(doc_id)
    
    logger.info(f"После дедупликации: {len(unique_docs)} уникальных документов")
    
    # Удаляем существующие документы с такими же ID (upsert логика)
    try:
        if unique_ids:
            vectorstore.delete(ids=unique_ids)
    except Exception:
        # Если delete не поддерживается - продолжаем
        pass
    
    # Добавляем документы
    if unique_docs:
        vectorstore.add_documents(documents=unique_docs, ids=unique_ids)
    
    # Сохраняем изменения
    vectorstore.persist()
    
    # Создаём маркер миграции (означает что база создана с новыми эмбеддингами)
    persist_dir = vectorstore._persist_directory
    if persist_dir:
        mark_migration_complete(persist_dir)
    
    logger.info("ChromaDB успешно наполнена и сохранена")


# === Legacy классы для обратной совместимости ===

class YandexEmbeddings:
    """
    Legacy класс для обратной совместимости.
    Рекомендуется использовать get_embeddings_instance() вместо этого.
    """
    
    def __init__(self, api_key: str, folder_id: str):
        logger.warning(
            "YandexEmbeddings используется напрямую. "
            "Рекомендуется использовать get_embeddings_instance() из rag.database"
        )
        import openai
        self.api_key = api_key
        self.folder_id = folder_id
        self.client = openai.OpenAI(
            api_key=api_key,
            base_url="https://llm.api.cloud.yandex.net/v1/"
        )
        self.logger = logging.getLogger(__name__)
    
    def _get_embedding(self, text: str, model_type: str) -> List[float]:
        fixed_text = ' '.join(text.split())
        try:
            response = self.client.embeddings.create(
                input=fixed_text,
                model=f"emb://{self.folder_id}/{model_type}/latest",
                encoding_format="float",
            )
            return response.data[0].embedding
        except Exception as e:
            self.logger.error(f"Ошибка получения эмбеддинга: {e}")
            return [0.0] * 256

    def embed_documents(self, texts: List[str]) -> List[List[float]]:
        return [self._get_embedding(text, "text-search-doc") for text in texts]

    def embed_query(self, text: str) -> List[float]:
        return self._get_embedding(text, "text-search-query")
