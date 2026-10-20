from __future__ import annotations

"""
Утилиты для настройки LLM-провайдера в рамках офлайн-оценки.

Важно: мы НЕ меняем продовый бот, а только конфигурируем раннер в текущем процессе.
"""


def force_paid_openrouter_only() -> None:
    """
    Принудительно отключает FREE модели OpenRouter для текущего процесса,
    чтобы прогоны были стабильнее (без 429 и переключений на free).
    """
    # 1) Отключаем free-модели на уровне key manager
    import digital_professor.providers.openrouter_key_manager as okm
    # отключаем free модели (через env и class var — для надёжности в текущем процессе)
    import os
    os.environ["OPENROUTER_DISABLE_FREE"] = "1"
    okm.OpenRouterKeyManager.FREE_MODELS = []
    okm._key_manager = None

    # 2) Сбрасываем singleton LLMProvider, чтобы он пересоздался и подхватил новый key manager
    import digital_professor.providers.llm_provider as lp
    lp._llm_provider = None


def configure_eval_llm(
    *,
    openrouter_model: str,
    disable_yandex_fallback: bool = True,
) -> None:
    """
    Конфигурирует процесс для офлайн-оценки:
    - строго фиксируем одну модель OpenRouter (обычно платную)
    - отключаем free
    - опционально отключаем fallback на Yandex (чтобы ошибки были явными)
    """
    import os
    os.environ["OPENROUTER_FORCE_MODEL"] = openrouter_model
    os.environ["OPENROUTER_DISABLE_FREE"] = "1"
    if disable_yandex_fallback:
        os.environ["LLM_DISABLE_YANDEX_FALLBACK"] = "1"

    # сброс singletons чтобы настройки подхватились
    import digital_professor.providers.openrouter_key_manager as okm
    okm._key_manager = None
    import digital_professor.providers.llm_provider as lp
    lp._llm_provider = None


