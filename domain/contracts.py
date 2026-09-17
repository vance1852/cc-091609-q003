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


class Severity(StrEnum):
    """规则命中紧急程度；合并展示以它为分组维度之一。"""

    CRITICAL = "critical"
    MAJOR = "major"
    MINOR = "minor"
    INFO = "info"


class RuleCategory(StrEnum):
    """六类版本化规则。"""

    DOSE_UNIT = "dose_unit"
    ALIAS = "alias"
    INCOMPATIBILITY = "incompatibility"
    SPECIAL_POPULATION = "special_population"
    ALLERGY = "allergy"
    CONCURRENT_MED = "concurrent_med"


class ActorRole(StrEnum):
    """会签角色；意见作者角色只能来自操作者本人，不能代签。"""

    PHYSICIAN = "physician"
    PHARMACIST = "pharmacist"


@dataclass(frozen=True)
class Actor:
    actor_id: str
    role: ActorRole
    display_name: str = ""


@dataclass(frozen=True)
class PatientContext:
    """患者敏感信息，查询时按最小范围披露。"""

    flags: tuple[str, ...] = ()
    concurrent_medications: tuple[str, ...] = ()


@dataclass(frozen=True)
class Finding:
    """单条规则命中。needs_verification 标记“尚待核实”的提示。"""

    category: RuleCategory
    severity: Severity
    summary: str
    evidence: tuple[RuleEvidence, ...]
    involved_names: tuple[str, ...] = ()
    needs_verification: bool = False


@dataclass(frozen=True)
class MergedAlert:
    """紧急程度与涉及药味相同的重复命中合并后的展示形态，依据全部保留。"""

    category: RuleCategory
    severity: Severity
    summary: str
    evidences: tuple[RuleEvidence, ...]
    involved_names: tuple[str, ...]
    hit_count: int
    needs_verification: bool = False
