"""最终查询：复核看板。

一次查询呈现：每条提示的规则来源版本、双方会签意见、处方版本演变；
患者敏感信息按最小范围返回——只披露被提示实际引用的患者事实，
其余（如与本案无关的病史标记）不出现在任何输出中。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from domain.contracts import ReviewState
from domain.review import DispensingGate, ReviewService, STATE_LABELS
from domain.rules import CATEGORY_LABELS, RULESET_ID, RULESET_VERSION, Severity


@dataclass(frozen=True)
class RuleSourceView:
    rule_id: str
    version: str
    source_reference: str


@dataclass(frozen=True)
class AlertView:
    alert_id: str
    category_label: str
    severity: Severity
    summary: str
    details: tuple[str, ...]  # 合并前的全部命中明细（不丢依据）
    matched_names: tuple[str, ...]
    pending: tuple[str, ...]  # 尚待核实
    rule_sources: tuple[RuleSourceView, ...]  # 每条提示的来源版本
    evidence_count: int
    status_label: str  # 当前快照会签状态下本提示所处的阶段


@dataclass(frozen=True)
class OpinionView:
    state_label: str
    author_role: str
    reason: str
    recorded_at: datetime


@dataclass(frozen=True)
class SnapshotSummary:
    snapshot_id: str
    version: int
    authored_at: datetime
    herbs: tuple[str, ...]  # "处方写法（归一标准名）" 或 "处方写法（未识别）"
    added: tuple[str, ...]  # 相对上一版新增（按处方写法）
    removed: tuple[str, ...]  # 相对上一版移除
    critical_count: int
    warning_count: int
    info_count: int
    opinion_labels: tuple[str, ...]  # 本版上已记录的会签


@dataclass(frozen=True)
class ReviewBoard:
    prescription_id: str
    current_snapshot_id: str
    patient_ref: str  # 不透明脱敏标识
    disclosed_context: tuple[str, ...]  # 最小披露：仅提示实际引用的患者事实
    ruleset: str
    alerts: tuple[AlertView, ...]
    opinions: tuple[OpinionView, ...]  # 当前快照上的双方意见
    evolution: tuple[SnapshotSummary, ...]
    gate: DispensingGate


def _snapshot_status(opinions) -> ReviewState:
    """由当前快照会签意见推导提示所处阶段（默认待核实）。"""
    for state in (ReviewState.REJECTED, ReviewState.RELEASED,
                  ReviewState.EXPLAINED, ReviewState.ADJUST):
        if any(o.state == state for o in opinions):
            return state
    return ReviewState.VERIFY


def build_review_board(service: ReviewService, prescription_id: str) -> ReviewBoard:
    snapshots = service.snapshots_of(prescription_id)
    current = snapshots[-1]
    current_opinions = service.opinions_for(current.snapshot_id)
    status_label = STATE_LABELS[_snapshot_status(current_opinions)]

    alert_views = []
    disclosed: list[str] = []
    for alert in service.alerts_for(current.snapshot_id):
        sources = tuple(dict.fromkeys(
            RuleSourceView(e.rule_id, e.version, e.source_reference)
            for e in alert.evidence
        ))
        alert_views.append(AlertView(
            alert_id=alert.alert_id,
            category_label=CATEGORY_LABELS[alert.category],
            severity=alert.severity,
            summary=alert.summary,
            details=alert.details,
            matched_names=alert.matched_names,
            pending=alert.pending,
            rule_sources=sources,
            evidence_count=len(alert.evidence),
            status_label=status_label,
        ))
        for fact in alert.patient_facts:
            if fact not in disclosed:
                disclosed.append(fact)

    evolution = []
    previous_names: tuple[str, ...] = ()
    for snapshot in snapshots:
        names = tuple(line.source_name for line in snapshot.herb_lines)
        herbs = []
        for line in snapshot.herb_lines:
            profile = service.catalog.find(line.source_name)
            if profile is None:
                herbs.append(f"{line.source_name}（未识别）")
            elif profile.standard_name != line.source_name:
                herbs.append(f"{line.source_name}（归一：{profile.standard_name}）")
            else:
                herbs.append(line.source_name)
        alerts = service.alerts_for(snapshot.snapshot_id)
        count = {s: sum(1 for a in alerts if a.severity == s) for s in Severity}
        evolution.append(SnapshotSummary(
            snapshot_id=snapshot.snapshot_id,
            version=snapshot.version,
            authored_at=snapshot.authored_at,
            herbs=tuple(herbs),
            added=tuple(n for n in names if n not in previous_names),
            removed=tuple(n for n in previous_names if n not in names),
            critical_count=count[Severity.CRITICAL],
            warning_count=count[Severity.WARNING],
            info_count=count[Severity.INFO],
            opinion_labels=tuple(STATE_LABELS[o.state]
                                 for o in service.opinions_for(snapshot.snapshot_id)),
        ))
        previous_names = names

    patient = service.patient_context(prescription_id)
    return ReviewBoard(
        prescription_id=prescription_id,
        current_snapshot_id=current.snapshot_id,
        patient_ref=patient.patient_ref if patient else "",
        disclosed_context=tuple(disclosed),
        ruleset=f"{RULESET_ID}@{RULESET_VERSION}",
        alerts=tuple(alert_views),
        opinions=tuple(OpinionView(STATE_LABELS[o.state], o.author_role,
                                   o.reason, o.recorded_at)
                       for o in current_opinions),
        evolution=tuple(evolution),
        gate=service.dispensing_gate(prescription_id),
    )
