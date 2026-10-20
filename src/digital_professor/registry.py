# courses/registry.py
import inspect
import importlib
from typing import Callable, Dict, Any

# Значение: ключ -> {"func": callable, "title": str, "description": str}
COURSE_CREATORS: Dict[str, Dict[str, Any]] = {}

def course_creator(key: str, title: str = None, description: str = None):
    """
    Декоратор для регистрации функции-создателя курса.
    Usage:
        @course_creator("vibe_pm", title="Вайб-кодинг для PM")
        def create_course_vibe_coding_pm(created_by: int = None): ...
    """
    def deco(func: Callable):
        COURSE_CREATORS[key] = {
            "func": func,
            "title": title or getattr(func, "__name__", key),
            "description": description or (func.__doc__ or "")
        }
        return func
    return deco

def discover_creators_from_module(module_name: str):
    """
    Автоматически находит в модуле все функции, имя которых начинается с create_course_
    и добавляет их в COURSE_CREATORS под ключом после префикса.
    Не перезаписывает уже зарегистрированные через декоратор.
    """
    mod = importlib.import_module(module_name)
    for name, func in inspect.getmembers(mod, inspect.isfunction):
        if name.startswith("create_course_"):
            key = name[len("create_course_"):]
            if key in COURSE_CREATORS:
                continue
            COURSE_CREATORS[key] = {
                "func": func,
                "title": getattr(func, "__doc__", name).splitlines()[0] if func.__doc__ else name,
                "description": func.__doc__ or ""
            }
    return COURSE_CREATORS
