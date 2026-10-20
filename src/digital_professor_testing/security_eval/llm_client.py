from __future__ import annotations

from typing import List, Dict, Tuple

from digital_professor.providers import get_llm_provider


def _to_provider_messages(messages: List[Dict[str, str]]) -> List[Dict[str, str]]:
    """
    В проекте местами используется {"role": "...", "text": "..."}.
    LLMProvider ожидает {"role": "...", "content": "..."}.
    """
    out = []
    for m in messages:
        out.append(
            {
                "role": m.get("role", "user"),
                "content": m.get("content") or m.get("text") or "",
            }
        )
    return out


async def call_llm_eval(
    messages: List[Dict[str, str]],
    *,
    temperature: float,
    top_p: float,
    max_tokens: int,
    usage_event: str,
    user_id: int,
) -> str:
    provider = get_llm_provider()
    resp = await provider.call(
        _to_provider_messages(messages),
        temperature=float(temperature),
        top_p=float(top_p),
        max_tokens=int(max_tokens),
        usage_event=usage_event,
        user_id=user_id,
    )
    return resp.content or ""


