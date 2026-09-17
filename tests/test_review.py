"""处方安全复核服务测试。"""

from __future__ import annotations

import json
import unittest
from datetime import datetime, timedelta, timezone
from itertools import count
from pathlib import Path

from domain import (
    Actor,
    ActorRole,
    CosignOrderError,
    DisclosurePurpose,
    HerbNormalizer,
    PatientContext,
    ReviewService,
    ReviewState,
    RoleNotAllowed,
    RuleCategory,
    Severity,
    SnapshotTerminal,
    parse_amount,
    review_timeline,
)
from domain.rules import evaluate_all, merge_findings

FIXTURE = Path(__file__).resolve().parent.parent / "fixtures" / "prescription_review.json"
RX_ID = "rx-safe-18"

PHYSICIAN = Actor("dr-li", ActorRole.PHYSICIAN, "李医师")
PHARMACIST = Actor("ph-wang", ActorRole.PHARMACIST, "王药师")


def deterministic_clock():
    ticks = count()
    base = datetime(2026, 9, 17, 9, 0, tzinfo=timezone.utc)
    return lambda: base + timedelta(minutes=next(ticks))


def fixture_data() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def build_service(versions: int = 3) -> tuple[ReviewService, list]:
    """按 fixtures/prescription_review.json 建立服务并摄入前 versions 个快照。"""
    data = fixture_data()
    service = ReviewService(clock=deterministic_clock())
    service.register_patient_context(
        RX_ID,
        flags=data["patientFlags"],
        concurrent_medications=data["concurrentMedication"],
    )
    records = [
        service.add_snapshot(RX_ID, snap["herbs"]) for snap in data["snapshots"][:versions]
    ]
    return service, records


def dual_sign(service: ReviewService, snapshot_id: str) -> None:
    service.record_opinion(PHARMACIST, snapshot_id, ReviewState.VERIFY, "核对患者过敏史与并用药")
    service.record_opinion(PHARMACIST, snapshot_id, ReviewState.ADJUST, "建议医师确认十八反同用依据")
    service.record_opinion(
        PHYSICIAN, snapshot_id, ReviewState.EXPLAINED, "引《金匮要略》方义，监制同用，确认无误"
    )
    service.record_opinion(PHARMACIST, snapshot_id, ReviewState.RELEASED, "复核通过，放行调配")


class TestNormalization(unittest.TestCase):
    def setUp(self):
        self.normalizer = HerbNormalizer()

    def test_alias_mapping(self):
        self.assertEqual(self.normalizer.normalize_name("姜半夏"), "BANXIA")
        self.assertEqual(self.normalizer.normalize_name("制川乌"), "CHUANWU")
        self.assertEqual(self.normalizer.normalize_name("炙甘草"), "GANCAO")
        self.assertEqual(self.normalizer.normalize_name("华法林"), "WARFARIN")

    def test_unknown_name_kept_for_verification(self):
        herb = self.normalizer.normalize("神秘草")
        self.assertIsNone(herb.normalized_code)
        self.assertEqual(herb.source_name, "神秘草")
        self.assertTrue(any("待核实" in issue for issue in herb.issues))

    def test_amount_units(self):
        self.assertEqual(parse_amount("9g"), (9.0, None))
        self.assertEqual(parse_amount("9克"), (9.0, None))
        self.assertEqual(parse_amount("3钱"), (9.0, None))
        self.assertEqual(parse_amount("0.5两"), (15.0, None))
        self.assertEqual(parse_amount("500mg"), (0.5, None))
        grams, issue = parse_amount("一把")
        self.assertIsNone(grams)
        self.assertIn("待核实", issue)

    def test_inline_amount_and_mapping_entry(self):
        herb = self.normalizer.normalize("制川乌6g")
        self.assertEqual(herb.normalized_code, "CHUANWU")
        self.assertEqual(herb.amount_grams, 6.0)
        herb2 = self.normalizer.normalize({"name": "姜半夏", "amount": "9克"})
        self.assertEqual(herb2.normalized_code, "BANXIA")
        self.assertEqual(herb2.amount_grams, 9.0)


class TestRules(unittest.TestCase):
    def setUp(self):
        self.normalizer = HerbNormalizer()

    def evaluate(self, herbs, flags=(), meds=()):
        normalized = [self.normalizer.normalize(h) for h in herbs]
        patient = PatientContext(flags=tuple(flags), concurrent_medications=tuple(meds))
        return evaluate_all(normalized, patient, self.normalizer)

    def test_eighteen_incompatibilities_hit(self):
        findings = self.evaluate(["制川乌", "半夏"])
        critical = [f for f in findings if f.severity == Severity.CRITICAL]
        self.assertEqual(len(critical), 1)
        self.assertEqual(critical[0].category, RuleCategory.INCOMPATIBILITY)
        self.assertEqual(critical[0].evidence[0].rule_id, "incomp-18fan-wutou")
        self.assertEqual(critical[0].evidence[0].version, "incomp-2025.1")
        self.assertIn("十八反", critical[0].evidence[0].source_reference)
        self.assertEqual(critical[0].evidence[0].affected_codes, ("BANXIA", "CHUANWU"))

    def test_concurrent_warfarin_hits(self):
        findings = self.evaluate(["制川乌", "姜半夏", "炙甘草"], meds=["华法林"])
        ddi = {f.evidence[0].rule_id for f in findings if f.category == RuleCategory.CONCURRENT_MED}
        self.assertEqual(ddi, {"ddi-warfarin-wutou", "ddi-warfarin-gancao"})

    def test_allergy_unverified_is_pending(self):
        findings = self.evaluate(["制川乌", "半夏"], flags=["allergy-unverified"])
        pending = [f for f in findings if f.needs_verification]
        self.assertTrue(any(f.category == RuleCategory.ALLERGY for f in pending))

    def test_known_allergy_is_critical(self):
        findings = self.evaluate(["炙甘草"], flags=["allergy:GANCAO"])
        critical = [f for f in findings if f.severity == Severity.CRITICAL]
        self.assertEqual(len(critical), 1)
        self.assertEqual(critical[0].evidence[0].rule_id, "allergy-known")

    def test_special_population(self):
        findings = self.evaluate(["制川乌"], flags=["pregnant"])
        critical = [f for f in findings if f.severity == Severity.CRITICAL]
        self.assertEqual(critical[0].evidence[0].rule_id, "pop-pregnancy-wutou")

    def test_dose_limit(self):
        findings = self.evaluate(["制川乌6g", "甘草"])
        dose = [f for f in findings if f.category == RuleCategory.DOSE_UNIT]
        self.assertEqual(len(dose), 1)
        self.assertEqual(dose[0].severity, Severity.MAJOR)
        self.assertEqual(dose[0].evidence[0].rule_id, "dose-chuanwu-max")
        self.assertIn("6g", dose[0].summary)

    def test_unmapped_name_and_amount_pending(self):
        findings = self.evaluate(["神秘草", {"name": "甘草", "amount": "一把"}])
        pending = {(f.category, f.severity) for f in findings if f.needs_verification}
        self.assertIn((RuleCategory.ALIAS, Severity.INFO), pending)
        self.assertIn((RuleCategory.DOSE_UNIT, Severity.INFO), pending)

    def test_merge_same_severity_keeps_evidence(self):
        # 半夏与姜半夏归一后同码，与制川乌各命中一次：同严重度重复命中应合并。
        findings = self.evaluate(["半夏", "姜半夏", "制川乌"])
        alerts = merge_findings(findings)
        critical = [a for a in alerts if a.severity == Severity.CRITICAL]
        self.assertEqual(len(critical), 1)
        merged = critical[0]
        self.assertEqual(merged.hit_count, 2)
        self.assertIn("半夏", merged.involved_names)
        self.assertIn("姜半夏", merged.involved_names)
        # 依据不丢：规则证据仍在，且涉及编码完整。
        self.assertEqual(merged.evidences[0].rule_id, "incomp-18fan-wutou")
        self.assertEqual(set(merged.evidences[0].affected_codes), {"BANXIA", "CHUANWU"})

    def test_merge_keeps_distinct_severities_separate(self):
        findings = self.evaluate(["制川乌", "半夏"], meds=["华法林"])
        alerts = merge_findings(findings)
        severities = {a.severity for a in alerts}
        self.assertIn(Severity.CRITICAL, severities)
        self.assertIn(Severity.MAJOR, severities)


class TestServiceWorkflow(unittest.TestCase):
    def test_snapshot_versioning_and_recompute(self):
        service, records = build_service(versions=3)
        self.assertEqual([r.snapshot.version for r in records], [1, 2, 3])
        self.assertEqual(records[0].snapshot.snapshot_id, f"{RX_ID}@v1")
        # 每个快照独立计算命中；v3 新增炙甘草后出现甘草-华法林提示。
        v1_rules = {ev.rule_id for a in records[0].alerts for ev in a.evidences}
        v3_rules = {ev.rule_id for a in records[2].alerts for ev in a.evidences}
        self.assertIn("incomp-18fan-wutou", v1_rules)
        self.assertIn("ddi-warfarin-gancao", v3_rules)
        self.assertNotIn("ddi-warfarin-gancao", v1_rules)

    def test_role_cannot_sign_for_other(self):
        service, records = build_service(versions=1)
        snap = records[0].snapshot.snapshot_id
        with self.assertRaises(RoleNotAllowed):
            service.record_opinion(PHARMACIST, snap, ReviewState.EXPLAINED, "药师代医师说明")
        with self.assertRaises(RoleNotAllowed):
            service.record_opinion(PHYSICIAN, snap, ReviewState.RELEASED, "医师代药师放行")
        with self.assertRaises(RoleNotAllowed):
            service.record_opinion(PHYSICIAN, snap, ReviewState.VERIFY, "医师代药师核实")

    def test_release_requires_physician_explained_first(self):
        service, records = build_service(versions=1)
        snap = records[0].snapshot.snapshot_id
        with self.assertRaises(CosignOrderError):
            service.record_opinion(PHARMACIST, snap, ReviewState.RELEASED, "抢跑放行")

    def test_dual_cosign_on_current_snapshot_allows_dispense(self):
        service, records = build_service(versions=1)
        dual_sign(service, records[0].snapshot.snapshot_id)
        decision = service.dispense_decision(RX_ID)
        self.assertTrue(decision.allowed)
        self.assertEqual(decision.missing, ())

    def test_old_release_cannot_unlock_new_snapshot(self):
        service, records = build_service(versions=1)
        dual_sign(service, records[0].snapshot.snapshot_id)
        self.assertTrue(service.dispense_decision(RX_ID).allowed)

        # 医师修改药味 -> 新快照，旧意见不得沿用。
        data = fixture_data()
        service.add_snapshot(RX_ID, data["snapshots"][1]["herbs"])
        decision = service.dispense_decision(RX_ID)
        self.assertFalse(decision.allowed)
        self.assertIn("当前快照缺少医师说明（explained）", decision.missing)
        self.assertIn("当前快照缺少药师放行（released）", decision.missing)
        self.assertTrue(any("v1" in note and "无效" in note for note in decision.notes))
        # 旧快照意见仍作历史留痕，但不计入当前会签。
        self.assertEqual(len(service.opinions_for(f"{RX_ID}@v1")), 4)
        self.assertEqual(service.opinions_for(f"{RX_ID}@v2"), ())
        status = service.cosign_status(f"{RX_ID}@v2")
        self.assertFalse(status.physician_explained)
        self.assertFalse(status.pharmacist_released)

    def test_explanation_on_old_snapshot_does_not_count(self):
        service, records = build_service(versions=2)
        v1, v2 = (r.snapshot.snapshot_id for r in records)
        service.record_opinion(PHYSICIAN, v1, ReviewState.EXPLAINED, "旧版说明")
        with self.assertRaises(CosignOrderError):
            service.record_opinion(PHARMACIST, v2, ReviewState.RELEASED, "引用旧说明")

    def test_rejected_snapshot_is_terminal(self):
        service, records = build_service(versions=1)
        snap = records[0].snapshot.snapshot_id
        service.record_opinion(PHARMACIST, snap, ReviewState.REJECTED, "风险不可接受，拒绝调配")
        with self.assertRaises(SnapshotTerminal):
            service.record_opinion(PHYSICIAN, snap, ReviewState.EXPLAINED, "事后解释")
        decision = service.dispense_decision(RX_ID)
        self.assertFalse(decision.allowed)
        self.assertTrue(any("拒绝调配" in item for item in decision.missing))

    def test_clean_prescription_needs_pharmacist_only(self):
        service = ReviewService(clock=deterministic_clock())
        record = service.add_snapshot("rx-clean", ["甘草6g", "人参3g"])
        self.assertEqual(record.alerts, ())
        snap = record.snapshot.snapshot_id
        service.record_opinion(PHARMACIST, snap, ReviewState.RELEASED, "无命中，常规放行")
        self.assertTrue(service.dispense_decision("rx-clean").allowed)


class TestQueries(unittest.TestCase):
    def test_timeline_presents_sources_opinions_and_evolution(self):
        service, records = build_service(versions=3)
        for record in records:
            dual_sign(service, record.snapshot.snapshot_id)
        timeline = review_timeline(service, RX_ID, purpose=DisclosurePurpose.CLINICAL_REVIEW)

        self.assertEqual(timeline["current_version"], 3)
        self.assertTrue(timeline["dispense_decision"]["allowed"])

        snapshots = timeline["snapshots"]
        self.assertEqual([s["version"] for s in snapshots], [1, 2, 3])

        # 每个提示带来源版本与文献。
        v1_alert = next(a for a in snapshots[0]["alerts"] if a["severity"] == "critical")
        evidence = v1_alert["evidence"][0]
        self.assertEqual(evidence["version"], "incomp-2025.1")
        self.assertIn("十八反", evidence["source_reference"])
        self.assertEqual(evidence["valid_from"], "2025-01-01")

        # 双方意见齐全且仅当前快照上的意见有效。
        v1_roles = {(o["author_role"], o["state"]) for o in snapshots[0]["opinions"]}
        self.assertIn(("physician", "explained"), v1_roles)
        self.assertIn(("pharmacist", "released"), v1_roles)
        self.assertTrue(all(not o["applies_to_current"] for o in snapshots[0]["opinions"]))
        self.assertTrue(all(o["applies_to_current"] for o in snapshots[2]["opinions"]))

        # 处方演变：v2 半夏->姜半夏（归一后组成不变），v3 新增炙甘草。
        evo2 = snapshots[1]["evolution_from_previous"]
        self.assertEqual(evo2["added"], ["姜半夏"])
        self.assertEqual(evo2["removed"], ["半夏"])
        self.assertTrue(evo2["normalized_unchanged"])
        evo3 = snapshots[2]["evolution_from_previous"]
        self.assertEqual(evo3["added"], ["炙甘草"])
        self.assertFalse(evo3["normalized_unchanged"])

        # 待核实信息透出。
        self.assertTrue(snapshots[0]["pending_verifications"])

    def test_sensitive_info_minimal_by_default(self):
        service, records = build_service(versions=1)
        audit_view = review_timeline(service, RX_ID)
        patient = audit_view["patient"]
        self.assertTrue(patient["redacted"])
        self.assertEqual(patient["concurrent_medication_count"], 1)
        self.assertNotIn("华法林", json.dumps(patient, ensure_ascii=False))

        clinical_view = review_timeline(
            service, RX_ID, purpose=DisclosurePurpose.CLINICAL_REVIEW
        )
        self.assertEqual(clinical_view["patient"]["concurrent_medications"], ["华法林"])
        self.assertEqual(clinical_view["patient"]["flags"], ["allergy-unverified"])

    def test_timeline_proves_old_release_cannot_unlock_new_version(self):
        service, records = build_service(versions=1)
        dual_sign(service, records[0].snapshot.snapshot_id)
        data = fixture_data()
        for snap in data["snapshots"][1:]:
            service.add_snapshot(RX_ID, snap["herbs"])

        timeline = review_timeline(service, RX_ID)
        decision = timeline["dispense_decision"]
        self.assertFalse(decision["allowed"])
        self.assertEqual(decision["version"], 3)
        self.assertTrue(any("v1" in note and "无效" in note for note in decision["notes"]))

        # 当前快照完成双向会签后才放行。
        dual_sign(service, f"{RX_ID}@v3")
        final = review_timeline(service, RX_ID)
        self.assertTrue(final["dispense_decision"]["allowed"])


if __name__ == "__main__":
    unittest.main()
