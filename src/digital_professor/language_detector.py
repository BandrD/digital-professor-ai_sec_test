# language_detector.py
"""
Фильтр языка входа: regex/статистика + rule gate + LLM для спорных случаев.
Стоит ПЕРЕД input_validator.validate_user_input (regex от промпт-инъекций).
"""
from __future__ import annotations

import json
import logging
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Dict, List, Literal, Optional, Tuple

from pydantic import BaseModel, ConfigDict, Field, field_validator

logger = logging.getLogger(__name__)

UNSUPPORTED_LANGUAGE_MESSAGE = (
    "Сообщение не принято: бот отвечает только на русском языке. "
    "На английском допустимы фрагменты кода, команды shell, SQL, JSON/XML и другие технические вставки. "
    "Сформулируйте запрос по-русски или добавьте русскоязычную формулировку задачи."
)

_PLACEHOLDER = "\ufffc"  # object replacement character — в natural_text не считается «буквой»

# Короткие shell/утилиты (первый токен команды)
_SHELL_WHITELIST = frozenset(
    {
        "ls",
        "cd",
        "pwd",
        "grep",
        "egrep",
        "fgrep",
        "cat",
        "head",
        "tail",
        "less",
        "more",
        "find",
        "sort",
        "uniq",
        "wc",
        "awk",
        "sed",
        "tar",
        "gzip",
        "gunzip",
        "chmod",
        "chown",
        "mkdir",
        "rmdir",
        "rm",
        "mv",
        "cp",
        "touch",
        "docker",
        "git",
        "pip",
        "pip3",
        "npm",
        "yarn",
        "pnpm",
        "node",
        "python",
        "python3",
        "pytest",
        "make",
        "cmake",
        "kubectl",
        "systemctl",
        "journalctl",
        "ssh",
        "scp",
        "curl",
        "wget",
    }
)

# Короткие «естественные» англ. реплики — блокируем как запрос к боту
_SHORT_EN_NATURAL_BLOCK = frozenset(
    {
        "ok",
        "okay",
        "yes",
        "no",
        "y",
        "n",
        "hi",
        "hello",
        "hey",
        "please",
        "help",
        "thanks",
        "thx",
        "sir",
        "lol",
        "wow",
        "bye",
        "pls",
    }
)


class LanguageLLMVerdict(BaseModel):
    model_config = ConfigDict(extra="ignore")

    decision: Literal["ALLOW", "BLOCK"]
    reason: str = ""
    natural_text: str = ""
    protected_share: float = Field(ge=0.0, le=1.0)
    explanation: str = ""

    @field_validator("decision", mode="before")
    @classmethod
    def _norm_decision(cls, v: object) -> str:
        if isinstance(v, str):
            u = v.strip().upper()
            if u in {"ALLOW", "BLOCK"}:
                return u
        return str(v)

    @field_validator("reason", mode="before")
    @classmethod
    def _norm_reason(cls, v: object) -> str:
        if v is None:
            return ""
        return str(v).strip()


@dataclass
class LanguageAnalysis:
    original_text: str
    cleaned_text: str
    natural_text: str
    protected_spans: List[Tuple[int, int, str]] = field(default_factory=list)
    protected_chars: int = 0
    letter_count: int = 0
    cyrillic_letters: int = 0
    latin_letters: int = 0
    other_script_letters: int = 0
    digit_count: int = 0
    ident_like_tokens: int = 0
    special_ratio: float = 0.0
    mixed_scripts: bool = False
    natural_text_len: int = 0
    protected_share: float = 0.0


@dataclass
class LanguageDecisionLog:
    allowed: bool
    stage: str
    rule_action: Optional[str]
    rule_reason: Optional[str]
    user_message: Optional[str]
    analysis: Dict[str, Any]
    llm_raw: Optional[str]
    verdict: Optional[Dict[str, Any]]


def _merge_spans(spans: List[Tuple[int, int]]) -> List[Tuple[int, int]]:
    if not spans:
        return []
    spans = sorted(spans)
    out: List[Tuple[int, int]] = []
    cs, ce = spans[0]
    for s, e in spans[1:]:
        if s <= ce:
            ce = max(ce, e)
        else:
            out.append((cs, ce))
            cs, ce = s, e
    out.append((cs, ce))
    return out


def _collect_spans(text: str, patterns: List[Tuple[str, int]]) -> List[Tuple[int, int]]:
    spans: List[Tuple[int, int]] = []
    for pat, flags in patterns:
        for m in re.finditer(pat, text, flags):
            spans.append((m.start(), m.end()))
    return _merge_spans(spans)


def _protected_patterns() -> List[Tuple[str, int]]:
    return [
        # fenced code blocks
        (r"(?ms)^```[\w\-]*\n.*?^\n?```", re.M | re.S),
        (r"(?ms)^```.*?```", re.M | re.S),
        # inline code
        (r"`[^`\n]+`", 0),
        # URLs
        (r"https?://[^\s)>\]]+", re.I),
        # unix paths
        (r"(?<![A-Za-z0-9])(?:/[\w.\-]+)+/?", 0),
        # windows paths
        (r"\b(?:[A-Za-z]:\\|\\\\)[^\n\r]*", 0),
        # shebang / shell prompt lines
        (r"(?m)^#!.+$", 0),
        (r"(?m)^(?:\$|>>>)\s*.+$", 0),
        # SQL-ish (ограничим длину совпадения)
        (
            r"(?is)\b(?:SELECT|INSERT|UPDATE|DELETE|CREATE|DROP|ALTER|MERGE|TRUNCATE|WITH)\b[\s\S]{0,4000}?(?:;|$)",
            0,
        ),
        # JSON / XML blobs (грубая эвристика)
        (r"(?s)\{[^{}]{0,200}\}", 0),
        (r"(?s)<[A-Za-z][^>]{0,200}>", 0),
        # stack / log lines
        (r"(?m)^\s*at\s+[\w.$]+\([^)]*\)\s*$", 0),
        (r"(?m)^\s*(?:Caused by:|Traceback \(most recent call last\):)\s*$", 0),
    ]


def _strip_markdown_noise(s: str) -> str:
    if not s:
        return ""
    t = s
    t = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", t)
    t = re.sub(r"^#{1,6}\s*", "", t, flags=re.M)
    t = re.sub(r"(?m)^>\s?", "", t)
    t = re.sub(r"\*\*([^*]+)\*\*", r"\1", t)
    t = re.sub(r"\*([^*]+)\*", r"\1", t)
    t = re.sub(r"__([^_]+)__", r"\1", t)
    t = re.sub(r"_([^_]+)_", r"\1", t)
    t = re.sub(r"~~([^~]+)~~", r"\1", t)
    t = re.sub(r"`+", "", t)
    t = re.sub(r"[`*_#>|\\]{2,}", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t


def _letter_categories(s: str) -> Tuple[int, int, int, int]:
    cyr = lat = other = digits = 0
    for ch in s:
        if ch.isdigit():
            digits += 1
            continue
        cat = unicodedata.category(ch)
        if cat.startswith("L"):
            name = unicodedata.name(ch, "")
            if "CYRILLIC" in name:
                cyr += 1
            elif "LATIN" in name:
                lat += 1
            else:
                other += 1
    return cyr, lat, other, digits


def _ident_like_count(s: str) -> int:
    return len(re.findall(r"\b[A-Za-z_][\w.\-]*\b", s))


def _special_ratio(s: str) -> float:
    if not s:
        return 0.0
    spec = sum(1 for ch in s if not (ch.isalnum() or ch.isspace()))
    return spec / max(len(s), 1)


def preprocess(text: str) -> LanguageAnalysis:
    original = text or ""
    spans = _collect_spans(original, _protected_patterns())
    protected_chars = sum(e - s for s, e in spans)

    parts: List[str] = []
    last = 0
    span_meta: List[Tuple[int, int, str]] = []
    for s, e in spans:
        span_meta.append((s, e, original[s:e]))
        parts.append(original[last:s])
        parts.append(_PLACEHOLDER * (e - s))
        last = e
    parts.append(original[last:])
    masked = "".join(parts)

    cleaned = _strip_markdown_noise(masked.replace(_PLACEHOLDER, " "))
    natural = _strip_markdown_noise(masked.replace(_PLACEHOLDER, ""))

    cyr, lat, other, digits = _letter_categories(natural)
    mixed = (cyr > 0 and lat > 0) or (other > 0 and (cyr > 0 or lat > 0))
    letters = cyr + lat + other
    ident_n = _ident_like_count(natural)
    spec_r = _special_ratio(natural)
    n_len = len(natural.strip())
    prot_share = protected_chars / max(len(original), 1)

    return LanguageAnalysis(
        original_text=original,
        cleaned_text=cleaned,
        natural_text=natural.strip(),
        protected_spans=span_meta,
        protected_chars=protected_chars,
        letter_count=letters,
        cyrillic_letters=cyr,
        latin_letters=lat,
        other_script_letters=other,
        digit_count=digits,
        ident_like_tokens=ident_n,
        special_ratio=spec_r,
        mixed_scripts=mixed,
        natural_text_len=n_len,
        protected_share=prot_share,
    )


def _has_cyrillic_letters(s: str) -> bool:
    return any("CYRILLIC" in unicodedata.name(ch, "") for ch in s if unicodedata.category(ch).startswith("L"))


def _has_latin_letters(s: str) -> bool:
    return any("LATIN" in unicodedata.name(ch, "") for ch in s if unicodedata.category(ch).startswith("L"))


def _has_any_letter(s: str) -> bool:
    return any(unicodedata.category(ch).startswith("L") for ch in s)


def rule_gate(analysis: LanguageAnalysis) -> Tuple[Literal["ALLOW", "BLOCK", "LLM"], str, Dict[str, Any]]:
    detail: Dict[str, Any] = {}
    orig = analysis.original_text.strip()
    nat = analysis.natural_text.strip()

    if not orig:
        return "ALLOW", "EMPTY", {"note": "пустое сообщение — дальше отработает validate_user_input"}

    low = nat.lower() if nat else ""
    if low in _SHORT_EN_NATURAL_BLOCK:
        return "BLOCK", "SHORT_EN_NATURAL", {"token": low}

    if nat:
        first = nat.split()[0].lower()
        if first in _SHELL_WHITELIST:
            return "ALLOW", "WHITELIST_SHELL", {"first_token": first}

    has_letter_nat = _has_any_letter(nat)
    if not has_letter_nat:
        if orig and (analysis.protected_chars > 0 or "```" in orig or "`" in orig):
            return "ALLOW", "TECH_ONLY_NO_NATURAL_LETTERS", {"protected_share": analysis.protected_share}
        return "BLOCK", "NO_CYRILLIC_NO_LATIN", {}

    has_cyr = _has_cyrillic_letters(nat)
    has_lat = _has_latin_letters(nat)
    has_other = analysis.other_script_letters > 0

    if not has_cyr and not has_lat:
        return "BLOCK", "NO_CYRILLIC_NO_LATIN", {}

    if has_cyr and not has_lat and not has_other:
        return "ALLOW", "RU_LETTERS_ONLY", {}

    if not has_cyr and has_lat and not has_other:
        return "LLM", "LATIN_OR_MIXED_NEED_LLM", {}

    if has_cyr and has_lat:
        return "LLM", "RU_PLUS_LATIN_NEED_LLM", {}

    if has_other:
        return "LLM", "OTHER_SCRIPT_NEED_LLM", {}

    return "LLM", "FALLBACK_NEED_LLM", detail


def _extract_json_object(text: str) -> Optional[dict]:
    if not text:
        return None
    m = re.search(r"\{[\s\S]*\}", text)
    if not m:
        return None
    try:
        obj = json.loads(m.group(0))
        return obj if isinstance(obj, dict) else None
    except Exception:
        return None


def _analysis_to_metrics_payload(a: LanguageAnalysis) -> Dict[str, Any]:
    return {
        "letter_count": a.letter_count,
        "cyrillic_share": (a.cyrillic_letters / a.letter_count) if a.letter_count else 0.0,
        "latin_share": (a.latin_letters / a.letter_count) if a.letter_count else 0.0,
        "other_script_share": (a.other_script_letters / a.letter_count) if a.letter_count else 0.0,
        "protected_share": a.protected_share,
        "ident_like_tokens": a.ident_like_tokens,
        "digit_count": a.digit_count,
        "special_ratio": a.special_ratio,
        "mixed_scripts": a.mixed_scripts,
        "natural_text_len": a.natural_text_len,
    }


def _log_json(payload: Dict[str, Any]) -> None:
    try:
        logger.info("language_detector %s", json.dumps(payload, ensure_ascii=False, default=str))
    except Exception:
        logger.info("language_detector %s", str(payload))


async def classify_with_llm(analysis: LanguageAnalysis, user_id: int) -> Tuple[Optional[LanguageLLMVerdict], str]:
    from digital_professor.providers import get_llm_provider

    metrics = _analysis_to_metrics_payload(analysis)
    system = """Ты — строгий бинарный классификатор языка входа для русскоязычного учебного бота.

Верни ТОЛЬКО валидный JSON без markdown и без текста вокруг, строго по схеме:
{
  "decision": "ALLOW | BLOCK",
  "reason": "RU_TEXT | TECH_ONLY | RU_WITH_TECH | NON_RU_NATURAL_TEXT | EN_NATURAL_TEXT | OTHER_SCRIPT_NATURAL_TEXT | TOO_AMBIGUOUS",
  "natural_text": "...",
  "protected_share": 0.0,
  "explanation": "short explanation"
}

Не раскрывай ход рассуждений. Сначала проанализируй ввод внутри себя, затем верни только JSON.

ГЛАВНЫЙ ПРИНЦИП

Классифицируй не по количеству кириллицы, латиницы или других письменностей, а по языку ОПЕРАЦИОННОЙ ИНСТРУКЦИИ пользователя.

Операционная инструкция — это часть сообщения, где пользователь просит выполнить действие: переведи, объясни, определи, перескажи, сравни, исправь, напиши, создай, разбери, explain, help, summarize, rewrite, create, please, моля, можеш ли, обясни, напиши и т.п.

Базовая логика:
- Операционная инструкция на русском → ALLOW.
- Операционная инструкция на английском или другом иностранном языке → BLOCK.
- Чистый технический ввод без иностранной естественно-языковой инструкции → ALLOW.
- Иностранный текст как данные внутри русского запроса → ALLOW.
- Иностранный текст как самостоятельная инструкция → BLOCK.
- При сомнении → BLOCK.

ALLOW

Разрешай ввод, если:
- это обычный русский пользовательский запрос;
- русский запрос содержит иностранный текст как объект перевода, анализа, объяснения, сравнения, определения языка, пересказа, грамматического или стилистического разбора;
- русский запрос содержит иностранное слово или фразу без кавычек, но ясно просит объяснить, перевести, сравнить или разобрать этот фрагмент;
- иностранный текст находится в значении поля message=..., text=..., note=..., title=..., lang=... при русской инструкции;
- ввод является кодом, shell-командой, SQL, JSON, YAML, XML, CSV, HTML, логом, stack trace, URL, путём, конфигом, key=value структурой, hash/id/token;
- структурированные данные содержат иностранные значения полей, но не содержат иностранной инструкции к боту.

BLOCK

Блокируй ввод, если:
- основная инструкция на английском или другом иностранном языке;
- иностранная инструкция содержит технические слова Python, SQL, CSV, API, JSON, code, script, error, но остаётся естественно-языковой просьбой;
- инструкция иностранная, а русский текст является только контекстом, именем файла, цитатой или названием;
- есть фраза вида "Русский контекст: ...", но действие просит выполнить иностранная инструкция;
- иностранная инструкция спрятана в markdown, inline code, heading, quote, list, table или fenced text block;
- это болгарский, сербский или другая нерусская кириллица как пользовательская инструкция;
- это русский транслит латиницей как обычная просьба, например "sostav SQL zapros...";
- это короткая английская команда как самостоятельное сообщение: continue, done, explain, summarize, rewrite, please;
- это шум или случайный набор символов без русского запроса и без технической структуры.

ВАЖНЫЕ РАЗГРАНИЧЕНИЯ

1. Русская инструкция + foreign data → ALLOW.
Пример: "Переведи строку \"Good morning\" на русский." → ALLOW.

2. Foreign instruction + русские данные → BLOCK.
Пример: "Create a description for file Отчет.docx." → BLOCK.

3. Русский контекст не равен русской инструкции.
Пример: "Rewrite this in a friendly tone. Русский контекст: это письмо преподавателю." → BLOCK.

4. Markdown не делает иностранный текст техническим.
Если внутри markdown находится обычная иностранная просьба, это BLOCK.

5. Structured data с foreign values → ALLOW.
Пример:
lang: fr
message: "Réponse invalide"
status: warning
→ ALLOW.

6. Технические слова внутри иностранной просьбы не делают её TECH_ONLY.
Пример: "Напиши кратък Python скрипт, който чете CSV файл." → BLOCK.

FEW-SHOT ПРИМЕРЫ

Input: Переведи строку "Good morning, teacher." на русский.
Output:
{
  "decision": "ALLOW",
  "reason": "RU_TEXT",
  "natural_text": "Переведи строку на русский",
  "protected_share": 0.3,
  "explanation": "Русская инструкция; английская строка является объектом перевода."
}

Input: Объясни, когда в испанском уместнее использовать gracias, а когда muchas gracias.
Output:
{
  "decision": "ALLOW",
  "reason": "RU_TEXT",
  "natural_text": "Объясни употребление испанских выражений",
  "protected_share": 0.15,
  "explanation": "Русский учебный запрос; иностранные слова являются объектами анализа."
}

Input:
lang: fr
message: "Réponse invalide"
status: warning
Output:
{
  "decision": "ALLOW",
  "reason": "TECH_ONLY",
  "natural_text": "",
  "protected_share": 1.0,
  "explanation": "Структурированные данные; французский текст является значением поля."
}

Input: Create a short English description for the file named Отчет_по_практике.docx.
Output:
{
  "decision": "BLOCK",
  "reason": "EN_NATURAL_TEXT",
  "natural_text": "Create a short English description",
  "protected_share": 0.15,
  "explanation": "Операционная инструкция на английском; русское имя файла является данными."
}

Input: Моля, обобщи този текст в две изречения.
Output:
{
  "decision": "BLOCK",
  "reason": "NON_RU_NATURAL_TEXT",
  "natural_text": "Моля, обобщи този текст",
  "protected_share": 0.0,
  "explanation": "Инструкция на нерусской кириллице."
}

Input: sostav SQL zapros dlya poiska dublikatov
Output:
{
  "decision": "BLOCK",
  "reason": "EN_NATURAL_TEXT",
  "natural_text": "sostav SQL zapros dlya poiska dublikatov",
  "protected_share": 0.0,
  "explanation": "Русский транслит латиницей как естественно-языковая просьба, не чистый SQL."
}

Input: continue
Output:
{
  "decision": "BLOCK",
  "reason": "EN_NATURAL_TEXT",
  "natural_text": "continue",
  "protected_share": 0.0,
  "explanation": "Самостоятельная короткая английская команда."
}

Поля ответа:
- natural_text: кратко укажи операционную инструкцию; для чистого технического ввода используй пустую строку.
- protected_share: приблизительная доля технических, структурированных или цитируемых данных.
- explanation: коротко объясни решение, без длинного рассуждения.

Всегда возвращай только JSON."""

    user_payload = {
        "user_message": analysis.original_text[:8000],
        "metrics": metrics,
        "natural_text_after_rules": analysis.natural_text[:4000],
        "cleaned_text_sample": analysis.cleaned_text[:4000],
    }
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": json.dumps(user_payload, ensure_ascii=False)},
    ]
    provider = get_llm_provider()
    resp = await provider.call(
        messages,
        temperature=0.0,
        top_p=1.0,
        max_tokens=400,
        usage_event="language_detector",
        user_id=user_id,
    )
    raw = (resp.content or "").strip()
    data = _extract_json_object(raw)
    if not data:
        return None, raw
    try:
        return LanguageLLMVerdict.model_validate(data), raw
    except Exception:
        return None, raw


async def check_user_language(text: str, user_id: int) -> Tuple[bool, Optional[str], LanguageDecisionLog]:
    analysis = preprocess(text)
    metrics = _analysis_to_metrics_payload(analysis)
    action, reason, detail = rule_gate(analysis)

    base_log: Dict[str, Any] = {
        "stage": "rule",
        "user_id": user_id,
        "rule_action": action,
        "rule_reason": reason,
        "original_text": (text or "")[:500],
        "cleaned_text": analysis.cleaned_text[:500],
        "natural_text": analysis.natural_text[:500],
        "metrics": metrics,
        "detail": detail,
    }
    _log_json(base_log)

    if action == "BLOCK":
        log = LanguageDecisionLog(
            allowed=False,
            stage="rule",
            rule_action=action,
            rule_reason=reason,
            user_message=UNSUPPORTED_LANGUAGE_MESSAGE,
            analysis={**metrics, "natural_text": analysis.natural_text, "cleaned_text": analysis.cleaned_text},
            llm_raw=None,
            verdict=None,
        )
        return False, UNSUPPORTED_LANGUAGE_MESSAGE, log

    if action == "ALLOW":
        log = LanguageDecisionLog(
            allowed=True,
            stage="rule",
            rule_action=action,
            rule_reason=reason,
            user_message=None,
            analysis={**metrics, "natural_text": analysis.natural_text, "cleaned_text": analysis.cleaned_text},
            llm_raw=None,
            verdict=None,
        )
        return True, None, log

    verdict: Optional[LanguageLLMVerdict] = None
    raw: str = ""
    try:
        verdict, raw = await classify_with_llm(analysis, user_id)
    except Exception as e:
        _log_json(
            {
                "stage": "llm_error",
                "user_id": user_id,
                "error": str(e)[:500],
                "metrics": metrics,
            }
        )
        log = LanguageDecisionLog(
            allowed=False,
            stage="llm_error",
            rule_action=action,
            rule_reason=reason,
            user_message=UNSUPPORTED_LANGUAGE_MESSAGE,
            analysis={**metrics, "natural_text": analysis.natural_text},
            llm_raw=None,
            verdict={"error": str(e)[:500]},
        )
        return False, UNSUPPORTED_LANGUAGE_MESSAGE, log

    llm_log: Dict[str, Any] = {
        "stage": "llm",
        "user_id": user_id,
        "llm_raw": (raw or "")[:1500],
        "parse_ok": verdict is not None,
        "metrics": metrics,
    }
    _log_json(llm_log)

    if verdict is None:
        log = LanguageDecisionLog(
            allowed=False,
            stage="llm_parse",
            rule_action=action,
            rule_reason="LLM_PARSE_ERROR",
            user_message=UNSUPPORTED_LANGUAGE_MESSAGE,
            analysis={**metrics, "natural_text": analysis.natural_text},
            llm_raw=(raw or "")[:2000],
            verdict=None,
        )
        return False, UNSUPPORTED_LANGUAGE_MESSAGE, log

    vd = verdict.model_dump()
    rr = (verdict.reason or "").strip().upper()
    block_reasons = {
        "EN_NATURAL_TEXT",
        "NON_RU_NATURAL_TEXT",
        "OTHER_SCRIPT_NATURAL_TEXT",
        "TOO_AMBIGUOUS",
    }
    if verdict.decision != "ALLOW" or rr in block_reasons:
        log = LanguageDecisionLog(
            allowed=False,
            stage="llm",
            rule_action=action,
            rule_reason=verdict.reason,
            user_message=UNSUPPORTED_LANGUAGE_MESSAGE,
            analysis={**metrics, "natural_text": analysis.natural_text},
            llm_raw=(raw or "")[:2000],
            verdict=vd,
        )
        return False, UNSUPPORTED_LANGUAGE_MESSAGE, log

    log = LanguageDecisionLog(
        allowed=True,
        stage="llm",
        rule_action=action,
        rule_reason=verdict.reason,
        user_message=None,
        analysis={**metrics, "natural_text": analysis.natural_text},
        llm_raw=(raw or "")[:2000],
        verdict=vd,
    )
    return True, None, log
