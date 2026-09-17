"""复核服务：快照接入、意见会签与放行闸门。

核心不变量：
- 医师修改任何药味 → 新快照 + 全量重算，旧快照意见不得沿用；
- 新意见只能落在当前（最新）快照上，旧快照不可补签；
- 会签角色来自签章注册表，调用方无法冒充，医师说明与药师放行不得同人；
- 只有当前快照上取得有效双向会签（医师说明 + 药师放行）才允许调配。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from domain.catalog import DEFAULT_CATALOG, HerbCatalog, parse_amount
from domain.contracts import HerbLine, PrescriptionSnapshot, ReviewOpinion, ReviewState
from domain.rules import Alert, PatientContext, evaluate

ROLE_PHYSICIAN = "physician"
ROLE_PHARMACIST = "pharmacist"

# 各状态允许的签署角色：医师说明只能医师签，放行/拒绝/建议调整只能药师签，
# 待核实双方均可标记。角色取自注册表而非调用方声明，防止代签。
_STATE_ROLES: dict[ReviewState, frozenset[str]] = {
    ReviewState.EXPLAINED: frozenset({ROLE_PHYSICIAN}),
    ReviewState.RELEASED: frozenset({ROLE_PHARMACIST}),
    ReviewState.REJECTED: frozenset({ROLE_PHARMACIST}),
    ReviewState.ADJUST: frozenset({ROLE_PHARMACIST}),
    ReviewState.VERIFY: frozenset({ROLE_PHYSICIAN, ROLE_PHARMACIST}),
}

STATE_LABELS = {
    ReviewState.VERIFY: "待核实",
    ReviewState.ADJUST: "建议调整",
    ReviewState.EXPLAINED: "医师说明",
    ReviewState.RELEASED: "药师放行",
    ReviewState.REJECTED: "拒绝调配",
}


class ReviewError(Exception):
    """复核流程违规（角色、时序、快照状态等）。"""


@dataclass(frozen=True)
class DispensingGate:
    """放行闸门：对某一快照能否调配的判定。"""

    snapshot_id: str
    is_current: bool
    allowed: bool
    physician_explained: bool
    pharmacist_released: bool
    rejected: bool
    blocking_reasons: tuple[str, ...]


class ReviewService:
    """处方复核会话（内存实现；持久化可在外层适配，不变量不变）。"""

    def __init__(self, catalog: HerbCatalog = DEFAULT_CATALOG) -> None:
        self._catalog = catalog
        self._signers: dict[str, str] = {}  # author_id -> role
        self._patients: dict[str, PatientContext] = {}  # prescription_id -> context
        self._snapshots: dict[str, PrescriptionSnapshot] = {}
        self._by_prescription: dict[str, list[str]] = {}
        self._alerts: dict[str, tuple[Alert, ...]] = {}
        self._opinions: dict[str, list[ReviewOpinion]] = {}
        self._opinion_authors: dict[str, str] = {}  # opinion_id -> author_id
        self._opinion_seq = 0

    # ------------------------------------------------------------ 基础登记

    def register_signer(self, author_id: str, role: str) -> None:
        if role not in (ROLE_PHYSICIAN, ROLE_PHARMACIST):
            raise ReviewError(f"未知角色：{role}")
        existing = self._signers.get(author_id)
        if existing is not None and existing != role:
            raise ReviewError(f"{author_id} 已注册为 {existing}，不得兼任")
        self._signers[author_id] = role

    def set_patient_context(self, prescription_id: str, context: PatientContext) -> None:
        self._patients[prescription_id] = context

    # ------------------------------------------------------------ 快照接入

    def submit_snapshot(
        self,
        prescription_id: str,
        herbs: list,
        authored_at: datetime | None = None,
    ) -> PrescriptionSnapshot:
        """接入一版处方。医师每次修改药味都调用本方法产生新快照并重算。

        herbs 元素为药名字符串或 {"name": ..., "amount": ...} 字典；
        药名归一与剂量折算在此完成，原始写法与归一结果并存于 HerbLine。
        """
        version = len(self._by_prescription.get(prescription_id, ())) + 1
        lines = []
        for item in herbs:
            if isinstance(item, dict):
                name, amount = item["name"], item.get("amount", "")
            else:
                name, amount = str(item), ""
            profile = self._catalog.find(name)
            lines.append(HerbLine(
                source_name=name,
                normalized_code=profile.code if profile else None,
                source_amount=amount,
                amount_grams=parse_amount(amount),
            ))
        snapshot = PrescriptionSnapshot(
            snapshot_id=f"{prescription_id}@v{version}",
            prescription_id=prescription_id,
            version=version,
            authored_at=authored_at or datetime.now(timezone.utc),
            herb_lines=tuple(lines),
        )
        self._snapshots[snapshot.snapshot_id] = snapshot
        self._by_prescription.setdefault(prescription_id, []).append(snapshot.snapshot_id)
        patient = self._patients.get(prescription_id, PatientContext(patient_ref=""))
        # 新快照全量重算；旧快照的提示与意见原样保留在历史中，但不沿用。
        self._alerts[snapshot.snapshot_id] = tuple(
            evaluate(snapshot, patient, self._catalog))
        self._opinions[snapshot.snapshot_id] = []
        return snapshot

    # ------------------------------------------------------------ 查询

    @property
    def catalog(self) -> HerbCatalog:
        return self._catalog

    def patient_context(self, prescription_id: str) -> PatientContext | None:
        return self._patients.get(prescription_id)

    def snapshot(self, snapshot_id: str) -> PrescriptionSnapshot:
        return self._snapshots[snapshot_id]

    def snapshots_of(self, prescription_id: str) -> list[PrescriptionSnapshot]:
        return [self._snapshots[sid] for sid in self._by_prescription[prescription_id]]

    def current_snapshot(self, prescription_id: str) -> PrescriptionSnapshot:
        return self._snapshots[self._by_prescription[prescription_id][-1]]

    def alerts_for(self, snapshot_id: str) -> tuple[Alert, ...]:
        return self._alerts[snapshot_id]

    def opinions_for(self, snapshot_id: str) -> tuple[ReviewOpinion, ...]:
        return tuple(self._opinions.get(snapshot_id, ()))

    def opinion_author(self, opinion_id: str) -> str:
        return self._opinion_authors[opinion_id]

    # ------------------------------------------------------------ 会签

    def record_opinion(
        self,
        snapshot_id: str,
        state: ReviewState,
        author_id: str,
        reason: str,
        recorded_at: datetime | None = None,
    ) -> ReviewOpinion:
        """在当前快照上记录一条会签意见。

        角色从签章注册表解析（调用方无法代签）；旧快照与已终止快照拒签。
        """
        snapshot = self._snapshots.get(snapshot_id)
        if snapshot is None:
            raise ReviewError(f"快照不存在：{snapshot_id}")
        if self._by_prescription[snapshot.prescription_id][-1] != snapshot_id:
            raise ReviewError("只能对当前（最新）快照签署意见，旧版意见不得沿用或补签")
        if any(o.state == ReviewState.REJECTED for o in self._opinions[snapshot_id]):
            raise ReviewError("本快照已被拒绝调配，流程终止；须由医师修改后产生新快照")
        role = self._signers.get(author_id)
        if role is None:
            raise ReviewError(f"未注册的签署人：{author_id}")
        if role not in _STATE_ROLES[state]:
            raise ReviewError(
                f"{STATE_LABELS[state]}不得由角色 {role} 签署（不能代签）")
        if not reason.strip():
            raise ReviewError("意见必须填写理由，便于追溯")
        self._opinion_seq += 1
        opinion = ReviewOpinion(
            opinion_id=f"op-{snapshot.prescription_id}-{self._opinion_seq}",
            snapshot_id=snapshot_id,
            state=state,
            author_role=role,
            reason=reason.strip(),
            recorded_at=recorded_at or datetime.now(timezone.utc),
        )
        self._opinions[snapshot_id].append(opinion)
        self._opinion_authors[opinion.opinion_id] = author_id
        return opinion

    # ------------------------------------------------------------ 放行闸门

    def snapshot_gate(self, snapshot_id: str) -> DispensingGate:
        """判定某一快照能否调配。旧版放行无法解锁新版处方，反之亦然。"""
        snapshot = self._snapshots[snapshot_id]
        is_current = self._by_prescription[snapshot.prescription_id][-1] == snapshot_id
        opinions = self._opinions.get(snapshot_id, [])
        explainers = {self._opinion_authors[o.opinion_id] for o in opinions
                      if o.state == ReviewState.EXPLAINED
                      and o.author_role == ROLE_PHYSICIAN}
        releasers = {self._opinion_authors[o.opinion_id] for o in opinions
                     if o.state == ReviewState.RELEASED
                     and o.author_role == ROLE_PHARMACIST}
        rejected = any(o.state == ReviewState.REJECTED for o in opinions)
        reasons: list[str] = []
        if not is_current:
            reasons.append("该快照已被更新的处方版本取代，旧版会签不得沿用")
        if rejected:
            reasons.append("药师已拒绝调配，本快照流程终止")
        if not explainers:
            reasons.append("缺少医师在本快照上的说明（双向会签之一）")
        if not releasers:
            reasons.append("缺少药师在本快照上的放行意见（双向会签之二）")
        if explainers & releasers:
            reasons.append("医师说明与药师放行不得由同一人签署")
        allowed = (is_current and not rejected and bool(explainers)
                   and bool(releasers) and not (explainers & releasers))
        return DispensingGate(
            snapshot_id=snapshot_id,
            is_current=is_current,
            allowed=allowed,
            physician_explained=bool(explainers),
            pharmacist_released=bool(releasers),
            rejected=rejected,
            blocking_reasons=tuple(reasons),
        )

    def dispensing_gate(self, prescription_id: str) -> DispensingGate:
        """当前快照的放行闸门——发药前唯一有效的判定入口。"""
        return self.snapshot_gate(self._by_prescription[prescription_id][-1])
