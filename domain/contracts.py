"""处方快照、规则证据与会签意见。"""

from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum


class ReviewState(StrEnum):
    VERIFY = "verify"
    ADJUST = "adjust"
    EXPLAINED = "explained"
    RELEASED = "released"
    REJECTED = "rejected"


@dataclass(frozen=True)
class HerbLine:
    source_name: str
    normalized_code: str | None
    source_amount: str
    amount_grams: float | None


@dataclass(frozen=True)
class PrescriptionSnapshot:
    snapshot_id: str
    prescription_id: str
    version: int
    authored_at: datetime
    herb_lines: tuple[HerbLine, ...]


@dataclass(frozen=True)
class RuleEvidence:
    rule_id: str
    version: str
    valid_from: date
    source_reference: str
    affected_codes: tuple[str, ...]


@dataclass(frozen=True)
class ReviewOpinion:
    opinion_id: str
    snapshot_id: str
    state: ReviewState
    author_role: str
    reason: str
    recorded_at: datetime
