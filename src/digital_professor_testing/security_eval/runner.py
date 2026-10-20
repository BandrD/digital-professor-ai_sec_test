from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import List

from digital_professor.input_validator import validate_user_input

from .profiles import DefenseProfile
from .types import BlockLayer, DatasetRow, TurnResult
from .censor_eval import check_user_query_eval
from .judge import judge_prompt_layer_blocked
from .llm_client import call_llm_eval
from .system_prompts import build_messages


LAYER_DEPTH: dict[BlockLayer, int] = {
    "regex": 1,
    "system_prompt": 2,
    "llm_censor": 3,
    "none": 0,
}


@dataclass
class EvalContext:
    """
    Контекст прогона: общий user_id для логирования и стабильных решений.
    """

    user_id: int = 123456


def default_context(user_id: int = 123456) -> EvalContext:
    return EvalContext(user_id=user_id)


async def eval_turn(row: DatasetRow, profile: DefenseProfile, ctx: EvalContext) -> TurnResult:
    text = row.attack_text

    reached_regex = profile.enable_regex
    reached_prompt = False
    reached_censor = False

    # 1) Regex / input_validator
    if profile.enable_regex:
        ok, err = validate_user_input(text)
        if not ok:
            return TurnResult(
                row=row,
                allowed=False,
                blocked_by="regex",
                reason=err or "blocked",
                reached_regex=True,
                reached_system_prompt=False,
                reached_llm_censor=False,
                depth=LAYER_DEPTH["regex"],
            )

    # 2) System prompt defense (prompt-layer)
    if profile.enable_system_prompt:
        reached_prompt = True
        messages = build_messages(text, profile.prompt_variant)
        assistant_answer = await call_llm_eval(
            messages,
            temperature=0.0,
            top_p=1.0,
            max_tokens=256,
            usage_event="security_eval_prompt_layer",
            user_id=ctx.user_id,
        )
        if judge_prompt_layer_blocked(row, assistant_answer):
            return TurnResult(
                row=row,
                allowed=False,
                blocked_by="system_prompt",
                reason="prompt_layer_refusal_or_no_leak",
                reached_regex=reached_regex,
                reached_system_prompt=True,
                reached_llm_censor=False,
                depth=LAYER_DEPTH["system_prompt"],
            )

    # 3) LLM censor
    if profile.enable_llm_censor:
        reached_censor = True
        ok, reason = await check_user_query_eval(
            text,
            user_id=ctx.user_id,
            temperature=0.0,
            top_p=1.0,
            max_tokens=160,
        )
        if not ok:
            return TurnResult(
                row=row,
                allowed=False,
                blocked_by="llm_censor",
                reason=reason or "blocked",
                reached_regex=reached_regex,
                reached_system_prompt=reached_prompt,
                reached_llm_censor=True,
                depth=LAYER_DEPTH["llm_censor"],
            )

    return TurnResult(
        row=row,
        allowed=True,
        blocked_by="none",
        reason="",
        reached_regex=reached_regex,
        reached_system_prompt=reached_prompt,
        reached_llm_censor=reached_censor,
        depth=0,
    )


async def run_dataset(
    rows: List[DatasetRow],
    profile: DefenseProfile,
    user_id: int = 123456,
    sleep_between_turns_s: float = 0.0,
) -> List[TurnResult]:
    """
    Прогоняет датасет в порядке (conversation_id, turn_id).

    Для multi-turn важно: один и тот же user_id используется на всём прогоне, чтобы rate limiter и цензор
    логировали одинаково. Если нужно эмулировать много пользователей — добавим режим позже.
    """
    ctx = default_context(user_id=user_id)
    out: list[TurnResult] = []
    for r in rows:
        out.append(await eval_turn(r, profile, ctx))
        if sleep_between_turns_s:
            await asyncio.sleep(float(sleep_between_turns_s))
    return out


