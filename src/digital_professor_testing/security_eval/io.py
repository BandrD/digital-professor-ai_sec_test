from __future__ import annotations

import csv
from pathlib import Path

from .types import DatasetRow


def _parse_bool(s: str) -> bool:
    s = (s or "").strip().lower()
    if s in {"true", "1", "yes"}:
        return True
    if s in {"false", "0", "no"}:
        return False
    raise ValueError(f"Bad bool value: {s!r}")


def load_dataset_csv(path: str | Path) -> list[DatasetRow]:
    p = Path(path)
    rows: list[DatasetRow] = []
    with p.open(newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        for r in reader:
            rows.append(
                DatasetRow(
                    conversation_id=r["conversation_id"],
                    turn_id=int(r["turn_id"]),
                    attack_id=r["attack_id"],
                    attack_type=r["attack_type"],
                    subtechnique=r["subtechnique"],
                    attack_text=r["attack_text"],
                    target=r["target"],
                    expected_block_level=r["expected_block_level"],
                    is_multiturn=_parse_bool(r["is_multiturn"]),
                )
            )
    # сортируем внутри диалогов по turn_id для корректного multi-turn прогона
    rows.sort(key=lambda x: (x.conversation_id, x.turn_id))
    return rows


