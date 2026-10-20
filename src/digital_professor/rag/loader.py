# rag_pipeline_loader.py
import json
import os
from typing import List, Dict, Any
from langchain.docstore.document import Document # pip install langchain

def flatten_metadata(metadata: Dict[str, Any], separator: str = ", ") -> Dict[str, Any]:
    """
    Преобразует сложные метаданные (списки) в простые типы (строки).
    Списки преобразуются в строки, соединенные `separator`.
    Можно расширить для обработки вложенных словарей и других типов.

    Args:
        metadata: Исходный словарь метаданных.
        separator: Строка-разделитель для объединения элементов списка.

    Returns:
        Новый словарь с простыми типами значений.
    """
    flattened = {}
    for key, value in metadata.items():
        if isinstance(value, list):
            # Преобразуем список в строку. Можно выбрать первый элемент, если он один, или все.
            # Здесь соединяем все элементы через запятую.
            # Если список пустой, записываем пустую строку или None.
            if value: # Если список не пуст
                 # Простой способ: соединить все элементы в строку
                 # Убедимся, что все элементы - строки
                 str_values = [str(v) for v in value]
                 flattened[key] = separator.join(str_values)
            else:
                 flattened[key] = "" # или None, если предпочтительнее
        elif isinstance(value, dict):
            # Для словарей можно сделать рекурсивное "выпрямление" или преобразовать в строку
            # Пока просто преобразуем в строковое представление
            flattened[key] = json.dumps(value, ensure_ascii=False) # Или str(value)
        elif isinstance(value, (str, int, float, bool)) or value is None:
            # Уже простой тип
            flattened[key] = value
        else:
            # Неизвестный тип, преобразуем в строку
            flattened[key] = str(value)
    return flattened

def load_all_chunks(json_files: List[str]) -> List[Dict[str, Any]]:
    """
    Загружает чанки из всех указанных JSON-файлов.

    Args:
        json_files: Список путей к JSON-файлам.

    Returns:
        Список словарей, представляющих чанки.
    """
    all_chunks = []
    for file_path in json_files:
        if os.path.exists(file_path):
            try:
                with open(file_path, 'r', encoding='utf-8') as f:
                    # Предполагаем, что каждый файл содержит массив чанков
                    chunks = json.load(f)
                    print(f"Загружено {len(chunks)} чанков из {file_path}")
                    all_chunks.extend(chunks)
            except Exception as e:
                print(f"Ошибка при загрузке {file_path}: {e}")
        else:
            print(f"Файл {file_path} не найден.")
    print(f"Всего загружено чанков: {len(all_chunks)}")
    return all_chunks

def convert_chunks_to_documents(chunks: List[Dict[str, Any]]) -> List[Document]:
    """
    Преобразует список чанков в список объектов Document для LangChain/Chroma.
    Обрабатывает метаданные, преобразуя сложные типы.

    Args:
        chunks: Список словарей чанков.

    Returns:
        Список объектов Document.
    """
    documents = []
    for chunk in chunks:
        content = chunk.get("content", "")
        raw_metadata = chunk.get("metadata", {})
        
        # --- ОБНОВЛЕНИЕ: Обработка метаданных здесь ---
        # Используем вставленную функцию для "выпрямления" метаданных
        processed_metadata = flatten_metadata(raw_metadata)
        # ---------------------------
        
        # Создаем Document с обработанными метаданными
        doc = Document(page_content=content, metadata=processed_metadata)
        documents.append(doc)
    print(f"Преобразовано в {len(documents)} документов LangChain.")
    return documents