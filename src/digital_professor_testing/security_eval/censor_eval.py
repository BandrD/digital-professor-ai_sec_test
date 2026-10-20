from __future__ import annotations

import json
import re
from typing import Tuple

from digital_professor.llm_censor import CENSOR_SYSTEM_PROMPT

from .llm_client import call_llm_eval


def _parse_censor_json(response_text: str) -> dict | None:
    cleaned = re.sub(r"^```(?:json)?\s*", "", (response_text or "").strip(), flags=re.IGNORECASE)
    cleaned = re.sub(r"\s*```$", "", cleaned, flags=re.IGNORECASE).strip()

    # Попытка 1: JSON целиком
    try:
        return json.loads(cleaned)
    except Exception:
        pass

    # Попытка 2: JSON по скобкам
    start_idx, end_idx = cleaned.find("{"), cleaned.rfind("}")
    if start_idx != -1 and end_idx != -1 and end_idx > start_idx:
        try:
            return json.loads(cleaned[start_idx : end_idx + 1])
        except Exception:
            pass
    return None


async def check_user_query_eval(
    user_query: str,
    *,
    user_id: int,
    temperature: float = 0.0,
    top_p: float = 1.0,
    max_tokens: int = 160,
) -> Tuple[bool, str]:
    """
    Eval-версия llm_censor.check_user_query:
    - фиксированные параметры генерации
    - вызов только через LLMProvider (без call_yandex_gpt)
    """
    messages = [
        {"role": "system", "text": CENSOR_SYSTEM_PROMPT},
        {"role": "user", "text": f"Запрос пользователя:\n{user_query}"},
    ]
    response_text = await call_llm_eval(
        messages,
        temperature=temperature,
        top_p=top_p,
        max_tokens=max_tokens,
        usage_event="security_eval_llm_censor",
        user_id=user_id,
    )

    parsed = _parse_censor_json(response_text)
    if not parsed or "is_allowed" not in parsed:
        # fail-open как в боевой версии
        return True, ""

    is_allowed = bool(parsed["is_allowed"])
    reason = parsed.get("reason", "") or ""
    return is_allowed, reason


