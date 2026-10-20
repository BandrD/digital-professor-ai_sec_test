from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from langchain_core.runnables import RunnableLambda

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))


DEFAULT_OPENROUTER_MODEL = "mistralai/mistral-small-2603"
MODEL_ALIASES = {
    "openrouter/mistral-small-2603": DEFAULT_OPENROUTER_MODEL,
}


@dataclass(frozen=True)
class DetectorResult:
    decision: str


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as f:
        for line_no, line in enumerate(f, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError as e:
                raise ValueError(f"{path}: invalid JSON on line {line_no}: {e}") from e
            if not isinstance(value, dict):
                raise ValueError(f"{path}: line {line_no} must contain a JSON object")
            rows.append(value)
    return rows


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def get_input_text(row: dict[str, Any], row_idx: int) -> str:
    text = row.get("user_input")
    if text is None:
        text = row.get("text")
    if text is None:
        raise ValueError(f"row {row_idx}: expected `user_input` or `text` field")
    return str(text)


def normalize_label(value: Any, *, row_idx: int) -> str:
    label = str(value or "").strip().upper()
    if label not in {"ALLOW", "BLOCK"}:
        raise ValueError(f"row {row_idx}: unsupported gold_label {value!r}; expected ALLOW or BLOCK")
    return label


def safe_div(numerator: int, denominator: int) -> float | None:
    return numerator / denominator if denominator else None


def normalize_openrouter_model(model: str) -> str:
    model = model.strip()
    return MODEL_ALIASES.get(model, model)


def f1_score(precision: float | None, recall: float | None) -> float | None:
    if precision is None or recall is None:
        return None
    denominator = precision + recall
    return 2 * precision * recall / denominator if denominator else 0.0


def build_report(rows: list[dict[str, Any]]) -> dict[str, Any]:
    tp = fn = fp = tn = 0
    block_rows = allow_rows = 0

    for row_idx, row in enumerate(rows, start=1):
        gold = normalize_label(row.get("gold_label"), row_idx=row_idx)
        pred = normalize_label(row.get("lang_detector_decision"), row_idx=row_idx)

        if gold == "BLOCK":
            block_rows += 1
            if pred == "BLOCK":
                tp += 1
            else:
                fn += 1
        else:
            allow_rows += 1
            if pred == "BLOCK":
                fp += 1
            else:
                tn += 1

    block_recall = safe_div(tp, tp + fn)
    block_precision = safe_div(tp, tp + fp)
    block_f1 = f1_score(block_precision, block_recall)

    allow_recall = safe_div(tn, tn + fp)
    allow_precision = safe_div(tn, tn + fn)
    allow_f1 = f1_score(allow_precision, allow_recall)

    recalls = [value for value in (block_recall, allow_recall) if value is not None]
    balanced_accuracy = sum(recalls) / len(recalls) if len(recalls) == 2 else None

    return {
        "stats": {
            "total_rows": len(rows),
            "block_rows": block_rows,
            "allow_rows": allow_rows,
            "positive_class": "BLOCK",
            "tp": tp,
            "fn": fn,
            "fp": fp,
            "tn": tn,
            "recall": block_recall,
            "precision": block_precision,
            "balanced_accuracy": balanced_accuracy,
            "f1": block_f1,
            "block_recall": block_recall,
            "block_precision": block_precision,
            "block_f1": block_f1,
            "allow_recall": allow_recall,
            "allow_precision": allow_precision,
            "allow_f1": allow_f1,
        }
    }


async def classify_row(row_payload: dict[str, Any]) -> DetectorResult:
    from digital_professor.language_detector import check_user_language

    row = row_payload["row"]
    row_idx = int(row_payload["row_idx"])
    user_id = int(row_payload["user_id"])
    text = get_input_text(row, row_idx)

    allowed, _, _ = await check_user_language(text, user_id=user_id)
    return DetectorResult(decision="ALLOW" if allowed else "BLOCK")


async def run_eval(
    *,
    dataset_path: Path,
    result_out: Path,
    report_out: Path,
    max_concurrency: int,
    user_id: int,
    openrouter_model: str,
) -> dict[str, Any]:
    try:
        from dotenv import load_dotenv

        load_dotenv()
    except Exception:
        pass

    from digital_professor_testing.security_eval.llm_setup import configure_eval_llm

    configure_eval_llm(
        openrouter_model=normalize_openrouter_model(openrouter_model),
        disable_yandex_fallback=True,
    )

    rows = load_jsonl(dataset_path)
    inputs = [
        {
            "row": row,
            "row_idx": row_idx,
            "user_id": user_id + row_idx - 1,
        }
        for row_idx, row in enumerate(rows, start=1)
    ]

    runnable = RunnableLambda(classify_row)
    try:
        detector_results = await runnable.abatch(
            inputs,
            config={"max_concurrency": max(1, int(max_concurrency))},
        )

        result_rows: list[dict[str, Any]] = []
        for row, detector_result in zip(rows, detector_results):
            result_rows.append(
                {
                    **row,
                    "lang_detector_decision": detector_result.decision,
                }
            )

        report = build_report(result_rows)
        write_jsonl(result_out, result_rows)
        write_json(report_out, report)
        return report
    finally:
        from digital_professor.providers import get_llm_provider

        try:
            await get_llm_provider().aclose()
        except Exception:
            pass


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Evaluate language_detector on a JSONL dataset")
    parser.add_argument("--dataset", required=True, type=Path, help="Input JSONL dataset path")
    parser.add_argument("--result-out", required=True, type=Path, help="Output JSONL with lang_detector_decision")
    parser.add_argument("--report-out", required=True, type=Path, help="Output JSON report with metrics")
    parser.add_argument("--max-concurrency", type=int, default=16, help="LangChain abatch max concurrency")
    parser.add_argument("--user-id", type=int, default=123456, help="Base user id for LLM usage logging")
    parser.add_argument("--openrouter-model", default=DEFAULT_OPENROUTER_MODEL, help="Forced OpenRouter model")
    return parser.parse_args()


async def main_async() -> None:
    args = parse_args()
    report = await run_eval(
        dataset_path=args.dataset,
        result_out=args.result_out,
        report_out=args.report_out,
        max_concurrency=args.max_concurrency,
        user_id=args.user_id,
        openrouter_model=args.openrouter_model,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))


def main() -> None:
    asyncio.run(main_async())


if __name__ == "__main__":
    main()
