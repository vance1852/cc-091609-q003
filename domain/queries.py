"""最终查询：规则来源版本、双方意见、处方演变与最小范围披露。"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from .contracts import MergedAlert, PatientContext, ReviewOpinion
from .service import DispenseDecision, ReviewService, SnapshotRecord


class DisclosurePurpose(StrEnum):
    """查询目的决定患者敏感信息的披露范围，默认最小范围。"""

    CLINICAL_REVIEW = "clinical_review"  # 医药复核：需要并用药与过敏标记
    AUDIT = "audit"  # 审计/留痕：仅知存在，不见内容


def _patient_block(patient: PatientContext, purpose: DisclosurePurpose) -> dict[str, Any]:
    if purpose == DisclosurePurpose.CLINICAL_REVIEW:
        return {
            "flags": list(patient.flags),
            "concurrent_medications": list(patient.concurrent_medications),
        }
    return {
        "redacted": True,
        "flag_count": len(patient.flags),
        "concurrent_medication_count": len(patient.concurrent_medications),
        "note": "患者敏感信息已按最小范围隐去，临床复核请使用 purpose=clinical_review",
    }


def _alert_block(alert: MergedAlert) -> dict[str, Any]:
    return {
        "category": str(alert.category),
        "severity": str(alert.severity),
        "summary": alert.summary,
        "hit_count": alert.hit_count,
        "needs_verification": alert.needs_verification,
        "involved_names": list(alert.involved_names),
        "evidence": [
            {
                "rule_id": ev.rule_id,
                "version": ev.version,
                "valid_from": ev.valid_from.isoformat(),
                "source_reference": ev.source_reference,
                "affected_codes": list(ev.affected_codes),
            }
            for ev in alert.evidences
        ],
    }


def _opinion_block(opinion: ReviewOpinion, current_snapshot_id: str) -> dict[str, Any]:
    return {
        "opinion_id": opinion.opinion_id,
        "state": str(opinion.state),
        "author_role": opinion.author_role,
        "reason": opinion.reason,
        "recorded_at": opinion.recorded_at.isoformat(),
        # 旧快照上的意见只作历史留痕，对当前快照无约束力。
        "applies_to_current": opinion.snapshot_id == current_snapshot_id,
    }


def _evolution_block(previous: SnapshotRecord | None, current: SnapshotRecord) -> dict[str, Any]:
    if previous is None:
        return {
            "added": [line.source_name for line in current.snapshot.herb_lines],
            "removed": [],
            "normalized_unchanged": False,
        }
    prev_names = [line.source_name for line in previous.snapshot.herb_lines]
    curr_names = [line.source_name for line in current.snapshot.herb_lines]
    prev_codes = {line.normalized_code for line in previous.snapshot.herb_lines}
    curr_codes = {line.normalized_code for line in current.snapshot.herb_lines}
    return {
        "added": [name for name in curr_names if name not in prev_names],
        "removed": [name for name in prev_names if name not in curr_names],
        # 原文有改动但归一后组成不变（如 半夏 -> 姜半夏），提示命中大概率延续。
        "normalized_unchanged": prev_codes == curr_codes,
    }


def _decision_block(decision: DispenseDecision) -> dict[str, Any]:
    return {
        "allowed": decision.allowed,
        "snapshot_id": decision.snapshot_id,
        "version": decision.version,
        "missing": list(decision.missing),
        "notes": list(decision.notes),
    }


def review_timeline(
    service: ReviewService,
    prescription_id: str,
    purpose: DisclosurePurpose = DisclosurePurpose.AUDIT,
) -> dict[str, Any]:
    records = service._records_for(prescription_id)
    current_id = records[-1].snapshot.snapshot_id
    decision = service.dispense_decision(prescription_id)

    snapshots = []
    previous: SnapshotRecord | None = None
    for record in records:
        snapshot = record.snapshot
        snapshots.append(
            {
                "snapshot_id": snapshot.snapshot_id,
                "version": snapshot.version,
                "authored_at": snapshot.authored_at.isoformat(),
                "is_current": snapshot.snapshot_id == current_id,
                "herbs": [
                    {
                        "source_name": line.source_name,
                        "normalized_code": line.normalized_code,
                        "source_amount": line.source_amount,
                        "amount_grams": line.amount_grams,
                    }
                    for line in snapshot.herb_lines
                ],
                "evolution_from_previous": _evolution_block(previous, record),
                "alerts": [_alert_block(alert) for alert in record.alerts],
                "pending_verifications": [
                    alert.summary for alert in record.alerts if alert.needs_verification
                ],
                "opinions": [
                    _opinion_block(opinion, current_id)
                    for opinion in service.opinions_for(snapshot.snapshot_id)
                ],
            }
        )
        previous = record

    return {
        "prescription_id": prescription_id,
        "current_version": records[-1].snapshot.version,
        "purpose": str(purpose),
        "patient": _patient_block(service.patient_context(prescription_id), purpose),
        "dispense_decision": _decision_block(decision),
        "snapshots": snapshots,
    }
