# rag_pipeline_retriever.py

import os
from typing import List, Tuple, Dict, Any
from langchain.docstore.document import Document
from langchain_community.vectorstores import Chroma
# from your_embedding_module import YandexEmbeddings # Импортируйте, если не в этом файле
# from rag_pipeline_db import YandexEmbeddings # Или отсюда
from langchain_community.retrievers import BM25Retriever
# Для простого реранжирования по метаданным или длине, можно не использовать LLM
# Если нужен реранжинг через YandexGPT, потребуется интеграция

class HybridRetriever:
    """
    Гибридный ретривер, сочетающий векторный и ключевой (BM25) поиск.
    """
    def __init__(self, vectorstore: Chroma, documents: List[Document], k_semantic: int = 5, k_keyword: int = 5, k_final: int = 5):
        """
        Args:
            vectorstore: Инициализированный объект Chroma.
            documents: Список всех документов, использованных для создания Chroma и BM25.
            k_semantic: Количество результатов от векторного поиска.
            k_keyword: Количество результатов от BM25 поиска.
            k_final: Количество финальных результатов после реранжирования.
        """
        self.vectorstore = vectorstore
        self.documents = documents
        self.k_semantic = k_semantic
        self.k_keyword = k_keyword
        self.k_final = k_final
        
        # Создаем BM25 ретривер один раз при инициализации
        print("Инициализация BM25 ретривера...")
        self.bm25_retriever = BM25Retriever.from_documents(documents=self.documents, k=self.k_keyword)
        print("BM25 ретривер готов.")

    def retrieve(self, query: str) -> List[Document]:
        """
        Выполняет гибридный поиск и реранжирование.

        Args:
            query: Запрос пользователя.

        Returns:
            Список финальных релевантных документов.
        """
        print(f"Начало извлечения для запроса: '{query}'")
        
        # 1. Векторный поиск (семантический)
        print("  1. Выполнение векторного поиска...")
        semantic_results = self.vectorstore.similarity_search(query, k=self.k_semantic)
        print(f"     Найдено {len(semantic_results)} результатов по семантике.")

        # 2. Ключевой поиск (BM25)
        print("  2. Выполнение BM25 поиска...")
        # Обновляем поиск для конкретного запроса
        self.bm25_retriever.k = self.k_keyword
        keyword_results = self.bm25_retriever.invoke(query)
        print(f"     Найдено {len(keyword_results)} результатов по ключевым словам.")

        # 3. Объединение результатов
        print("  3. Объединение результатов...")
        # Используем словарь для дедупликации по содержимому (или по ID, если есть)
        combined_dict = {}
        for doc in semantic_results:
            # Используем page_content как ключ для дедупликации (можно улучшить)
            combined_dict[doc.page_content] = doc
        for doc in keyword_results:
            combined_dict[doc.page_content] = doc # Перезаписывает, если уже есть, но это ок для объединения
        
        combined_results = list(combined_dict.values())
        print(f"     Объединено в {len(combined_results)} уникальных результатов.")

        # 4. Реранжирование
        print("  4. Реранжирование...")
        # --- Простое реранжирование ---
        # Можно использовать более сложные методы, например, пересчет релевантности
        # с помощью маленькой модели или эвристик.
        # Пока просто возвращаем первые k_final из объединенного списка.
        # Для более продвинутого реранжирования можно:
        # a) Использовать YandexGPT для оценки релевантности каждого документа по отношению к запросу.
        # b) Учитывать метаданные (например, совпадение `source_file` или `section` с предыдущими запросами).
        # c) Учитывать длину документа (слишком короткие или слишком длинные могут быть менее релевантны).
        
        # Реранжирование с приоритетом векторному поиску
        # Сначала берём все результаты из семантического поиска (они приоритетнее)
        reranked_results = []
        
        # Добавляем семантические результаты в первую очередь (приоритет)
        for doc in semantic_results:
            if len(reranked_results) >= self.k_final:
                break
            if doc not in reranked_results:
                reranked_results.append(doc)
        
        # Добавляем BM25 результаты только если не хватает семантических
        for doc in keyword_results:
            if len(reranked_results) >= self.k_final:
                break
            if doc not in reranked_results:
                reranked_results.append(doc)
        
        final_results = reranked_results[:self.k_final]
        print(f"     Выбрано {len(final_results)} финальных результатов.")
        return final_results