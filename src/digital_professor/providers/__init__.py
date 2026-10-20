# providers/__init__.py
"""
Модуль провайдеров для LLM и Embeddings.
Поддерживает OpenRouter и Yandex с автоматическим fallback.
"""

from .llm_provider import LLMProvider, get_llm_provider
from .embeddings_provider import EmbeddingsProvider, get_embeddings_provider
from .openrouter_key_manager import OpenRouterKeyManager, KeyType

__all__ = [
    "LLMProvider",
    "get_llm_provider",
    "EmbeddingsProvider", 
    "get_embeddings_provider",
    "OpenRouterKeyManager",
    "KeyType",
]

