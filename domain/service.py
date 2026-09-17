"""处方复核服务：快照摄入、会签意见与放行闸门。

核心约束：
- 医师修改任何药味都会产生新快照并重新计算规则命中；
- 会签意见绑定具体快照，旧快照上的意见（包括放行）对新快照一律无效；
- 只有当前快照上取得医师说明 + 药师放行的双向会签才可调配；
- 角色不能代签：意见作者角色只能来自操作者本人。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Iterable, Mapping

from .contracts import (
    Actor,
    ActorRole,
    Finding,
    HerbLine,
    MergedAlert,
    PatientContext,
    PrescriptionSnapshot,
    ReviewOpinion,
    ReviewState,
)
from .normalization import HerbNormalizer
from .rules import evaluate_all, merge_findings


class ReviewError(Exception):
    """复核流程错误基类。"""


class UnknownPrescription(ReviewError):
    pass


class UnknownSnapshot(ReviewError):
    pass


class RoleNotAllowed(ReviewError):
    """角色不能代签：该状态不允许由此角色记录。"""


class CosignOrderError(ReviewError):
    """双向会签顺序错误：药师放行前须先有当前快照上的医师说明。"""


class SnapshotTerminal(ReviewError):
    """快照已被拒绝调配，属于终态，只能由医师修改产生新快照。"""


# 各角色允许记录的意见状态——这是“不能代签”的唯一判定来源。
ROLE_STATES: dict[ActorRole, frozenset[ReviewState]] = {
    ActorRole.PHARMACIST: frozenset(
        {ReviewState.VERIFY, ReviewState.ADJUST, ReviewState.RELEASED, ReviewState.REJECTED}
    ),
    ActorRole.PHYSICIAN: frozenset({ReviewState.EXPLAINED}),
}


@dataclass(frozen=True)
class SnapshotRecord:
    snapshot: PrescriptionSnapshot
    findings: tuple[Finding, ...]
    alerts: tuple[MergedAlert, ...]


@dataclass(frozen=True)
class CosignStatus:
    snapshot_id: str
    physician_explained: bool
    pharmacist_released: bool
    rejected: bool

    @property
    def dual_signed(self) -> bool:
        return self.physician_explained and self.pharmacist_released and not self.rejected


@dataclass(frozen=True)
class DispenseDecision:
    prescription_id: str
    snapshot_id: str
    version: int
    allowed: bool
    missing: tuple[str, ...]
    notes: tuple[str, ...]


class ReviewService:
    def __init__(
        self,
        normalizer: HerbNormalizer | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._normalizer = normalizer or HerbNormalizer()
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._patients: dict[str, PatientContext] = {}
        self._records: dict[str, list[SnapshotRecord]] = {}
        self._opinions: dict[str, list[ReviewOpinion]] = {}
        self._opinion_seq = 0

    # ---- 摄入 ----------------------------------------------------------------

    def register_patient_context(
        self,
        prescription_id: str,
        flags: Iterable[str] = (),
        concurrent_medications: Iterable[str] = (),
    ) -> None:
        self._patients[prescription_id] = PatientContext(
            flags=tuple(flags),
            concurrent_medications=tuple(concurrent_medications),
        )

    def add_snapshot(
        self,
        prescription_id: str,
        herbs: Iterable[str | Mapping[str, str]],
        authored_at: datetime | None = None,
    ) -> SnapshotRecord:
        """医师每次修改药味都产生新快照并重新计算全部规则。"""
        records = self._records.setdefault(prescription_id, [])
        version = len(records) + 1
        normalized = [self._normalizer.normalize(entry) for entry in herbs]
        snapshot = PrescriptionSnapshot(
            snapshot_id=f"{prescription_id}@v{version}",
            prescription_id=prescription_id,
            version=version,
            authored_at=authored_at or self._clock(),
            herb_lines=tuple(
                HerbLine(
                    source_name=herb.source_name,
                    normalized_code=herb.normalized_code,
                    source_amount=herb.source_amount,
                    amount_grams=herb.amount_grams,
                )
                for herb in normalized
            ),
        )
        patient = self._patients.get(prescription_id, PatientContext())
        findings = evaluate_all(normalized, patient, self._normalizer)
        record = SnapshotRecord(
            snapshot=snapshot,
            findings=tuple(findings),
            alerts=tuple(merge_findings(findings)),
        )
        records.append(record)
        return record

    # ---- 查询基础 -------------------------------------------------------------

    def _records_for(self, prescription_id: str) -> list[SnapshotRecord]:
        records = self._records.get(prescription_id)
        if not records:
            raise UnknownPrescription(f"无处方记录：{prescription_id}")
        return records

    def current_record(self, prescription_id: str) -> SnapshotRecord:
        return self._records_for(prescription_id)[-1]

    def record_for(self, snapshot_id: str) -> SnapshotRecord:
        for records in self._records.values():
            for record in records:
                if record.snapshot.snapshot_id == snapshot_id:
                    return record
        raise UnknownSnapshot(f"无快照：{snapshot_id}")

    def patient_context(self, prescription_id: str) -> PatientContext:
        self._records_for(prescription_id)
        return self._patients.get(prescription_id, PatientContext())

    def opinions_for(self, snapshot_id: str) -> tuple[ReviewOpinion, ...]:
        self.record_for(snapshot_id)
        return tuple(self._opinions.get(snapshot_id, ()))

    # ---- 会签 -----------------------------------------------------------------

    def record_opinion(
        self,
        actor: Actor,
        snapshot_id: str,
        state: ReviewState,
        reason: str,
    ) -> ReviewOpinion:
        record = self.record_for(snapshot_id)
        existing = self._opinions.setdefault(snapshot_id, [])
        state = ReviewState(state)

        if any(op.state == ReviewState.REJECTED for op in existing):
            raise SnapshotTerminal(
                f"快照 {snapshot_id} 已被拒绝调配，医师须修改药味产生新快照后再复核"
            )
        allowed = ROLE_STATES[actor.role]
        if state not in allowed:
            raise RoleNotAllowed(
                f"角色不能代签：{actor.role} 无权记录“{state}”意见"
            )
        if (
            state == ReviewState.RELEASED
            and record.alerts
            and not any(
                op.state == ReviewState.EXPLAINED and op.author_role == ActorRole.PHYSICIAN
                for op in existing
            )
        ):
            raise CosignOrderError(
                f"快照 {snapshot_id} 尚无医师说明，药师不能先行放行（双向会签顺序）"
            )

        self._opinion_seq += 1
        opinion = ReviewOpinion(
            opinion_id=f"op-{self._opinion_seq:04d}",
            snapshot_id=record.snapshot.snapshot_id,
            state=state,
            author_role=str(actor.role),
            reason=reason,
            recorded_at=self._clock(),
        )
        existing.append(opinion)
        return opinion

    def cosign_status(self, snapshot_id: str) -> CosignStatus:
        opinions = self.opinions_for(snapshot_id)
        return CosignStatus(
            snapshot_id=snapshot_id,
            physician_explained=any(
                op.state == ReviewState.EXPLAINED and op.author_role == ActorRole.PHYSICIAN
                for op in opinions
            ),
            pharmacist_released=any(
                op.state == ReviewState.RELEASED and op.author_role == ActorRole.PHARMACIST
                for op in opinions
            ),
            rejected=any(op.state == ReviewState.REJECTED for op in opinions),
        )

    # ---- 放行闸门 ---------------------------------------------------------------

    def dispense_decision(self, prescription_id: str) -> DispenseDecision:
        """只有当前快照上的有效双向会签才能放行；旧快照意见不得沿用。"""
        records = self._records_for(prescription_id)
        current = records[-1]
        snapshot = current.snapshot
        status = self.cosign_status(snapshot.snapshot_id)

        missing: list[str] = []
        notes: list[str] = []

        if status.rejected:
            missing.append("当前快照已被药师拒绝调配，须由医师修改产生新快照")
        else:
            requires_dual = bool(current.alerts)
            if requires_dual and not status.physician_explained:
                missing.append("当前快照缺少医师说明（explained）")
            if not status.pharmacist_released:
                missing.append("当前快照缺少药师放行（released）")

        # 明确指出旧快照上的会签对当前快照无效。
        superseded_releases = [
            rec.snapshot.version
            for rec in records[:-1]
            if self.cosign_status(rec.snapshot.snapshot_id).pharmacist_released
        ]
        for old_version in superseded_releases:
            notes.append(
                f"v{old_version} 上的放行意见仅绑定快照 v{old_version}，"
                f"对当前快照 v{snapshot.version} 无效"
            )

        return DispenseDecision(
            prescription_id=prescription_id,
            snapshot_id=snapshot.snapshot_id,
            version=snapshot.version,
            allowed=not missing,
            missing=tuple(missing),
            notes=tuple(notes),
        )
