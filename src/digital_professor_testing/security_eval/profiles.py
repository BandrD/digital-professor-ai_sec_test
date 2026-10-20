from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

PromptVariant = Literal["prod", "no_security"]


@dataclass(frozen=True)
class DefenseProfile:
    """
    Профиль включения слоёв защиты для оценки.

    Порядок слоёв (как в текущем боте для пользовательского текста):
    1) regex/input_validator
    2) system prompt (prompt-defense)
    3) llm_censor

    Отдельного 'sequence analyzer' в коде сейчас нет (можно добавить как экспериментальный слой в раннере).
    """

    name: str
    enable_regex: bool = True
    enable_system_prompt: bool = True
    enable_llm_censor: bool = True
    prompt_variant: PromptVariant = "prod"


PROFILES: dict[str, DefenseProfile] = {
    # Абляции (накопительным способом)
    "none": DefenseProfile(name="none", enable_regex=False, enable_system_prompt=False, enable_llm_censor=False, prompt_variant="no_security"),
    # regex-only прогоняем с no_security промптом, чтобы в системном промпте не было AI-sec / sandwich подсказок
    "regex_only": DefenseProfile(name="regex_only", enable_regex=True, enable_system_prompt=False, enable_llm_censor=False, prompt_variant="no_security"),
    "regex_prompt": DefenseProfile(name="regex_prompt", enable_regex=True, enable_system_prompt=True, enable_llm_censor=False, prompt_variant="prod"),
    "full": DefenseProfile(name="full", enable_regex=True, enable_system_prompt=True, enable_llm_censor=True, prompt_variant="prod"),
}


