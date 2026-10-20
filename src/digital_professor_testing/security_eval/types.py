from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Optional


BlockLayer = Literal["none", "regex", "system_prompt", "llm_censor"]


@dataclass(frozen=True)
class DatasetRow:
    conversation_id: str
    turn_id: int
    attack_id: str
    attack_type: str
    subtechnique: str
    attack_text: str
    target: str
    expected_block_level: str
    is_multiturn: bool

    @property
    def is_attack(self) -> bool:
        return self.attack_type != "benign"

    @property
    def is_payload_splitting(self) -> bool:
        return self.attack_type == "payload_splitting_multiturn"


@dataclass(frozen=True)
class TurnResult:
    row: DatasetRow
    allowed: bool
    blocked_by: BlockLayer
    reason: str = ""
    # reached flags for per-layer CatchRate (числитель/знаменатель)
    reached_regex: bool = False
    reached_system_prompt: bool = False
    reached_llm_censor: bool = False

    # удобная глубина проникновения для MAPD
    depth: int = 0  # 1..N layers, 0 = not blocked (penetration)


@dataclass(frozen=True)
class RunStats:
    profile_name: str
    total_rows: int
    attack_rows: int
    benign_rows: int
    tp: int
    fn: int
    fp: int
    tn: int
    attack_recall: float
    false_positive_rate: float
    balanced_accuracy: float
    multiturn_recall: Optional[float]


