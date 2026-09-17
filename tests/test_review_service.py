"""处方安全复核服务测试：python -m unittest discover -s tests -v"""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from domain.catalog import DEFAULT_CATALOG, parse_amount
from domain.contracts import ReviewState
from domain.queries import build_review_board
from domain.review import ReviewError, ReviewService
from domain.rules import PatientContext, Severity

FIXTURE = Path(__file__).resolve().parent.parent / "fixtures" / "prescription_review.json"
PID = "rx-safe-18"


def make_service(**context_kwargs) -> ReviewService:
    service = ReviewService()
    service.register_signer("dr-chen", "physician")
    service.register_signer("rph-li", "pharmacist")
    service.set_patient_context(PID, PatientContext(
        patient_ref="P-7F3A9C", **context_kwargs))
    return service


def alerts_of(service, snapshot, category=None, severity=None):
    alerts = service.alerts_for(snapshot.snapshot_id)
    if category is not None:
        alerts = [a for a in alerts if a.category == category]
    if severity is not None:
        alerts = [a for a in alerts if a.severity == severity]
    return list(alerts)


class AmountParsingTest(unittest.TestCase):
    def test_arabic_units(self):
        self.assertEqual(parse_amount("6g"), 6.0)
        self.assertEqual(parse_amount("6 g"), 6.0)
        self.assertEqual(parse_amount("10克"), 10.0)
        self.assertEqual(parse_amount("1.5钱"), 4.5)
        self.assertEqual(parse_amount("3分"), 0.9)

    def test_chinese_numerals(self):
        self.assertEqual(parse_amount("三钱"), 9.0)
        self.assertEqual(parse_amount("一钱半"), 4.5)
        self.assertEqual(parse_amount("半钱"), 1.5)
        self.assertEqual(parse_amount("二两"), 60.0)
        self.assertEqual(parse_amount("十五克"), 15.0)
        self.assertEqual(parse_amount("二十克"), 20.0)

    def test_unparseable_returns_none_for_manual_check(self):
        self.assertIsNone(parse_amount("6"))  # 无单位不猜测
        self.assertIsNone(parse_amount("适量"))
        self.assertIsNone(parse_amount(""))
        self.assertIsNone(parse_amount(None))


class AliasNormalizationTest(unittest.TestCase):
    def test_processed_variants_keep_family(self):
        self.assertEqual(DEFAULT_CATALOG.find("姜半夏").family, "半夏类")
        self.assertEqual(DEFAULT_CATALOG.find("半夏").standard_name, "法半夏")  # 处方应付
        self.assertEqual(DEFAULT_CATALOG.find("黑顺片").family, "乌头类")  # 附子别名
        self.assertEqual(DEFAULT_CATALOG.find("炙草").family, "甘草类")

    def test_unknown_name_returns_none(self):
        self.assertIsNone(DEFAULT_CATALOG.find("不存在药"))


class RuleEngineTest(unittest.TestCase):
    def test_eighteen_incompatibilities_hit(self):
        service = make_service()
        snap = service.submit_snapshot(PID, ["制川乌", "半夏"])
        hits = alerts_of(service, snap, "compatibility", Severity.CRITICAL)
        self.assertEqual(len(hits), 1)
        self.assertIn("十八反", hits[0].summary)
        self.assertEqual(hits[0].evidence[0].rule_id, "R-COMP-18FAN")
        self.assertEqual(hits[0].evidence[0].version, "2020.1")

    def test_processed_banxia_still_hits(self):
        # 姜半夏是炮制品，但配伍族不变，十八反仍命中
        service = make_service()
        snap = service.submit_snapshot(PID, ["制川乌", "姜半夏"])
        self.assertEqual(len(alerts_of(service, snap, "compatibility")), 1)

    def test_alias_drives_rule(self):
        # 黑顺片（附子别名）+ 半夏同样命中十八反
        service = make_service()
        snap = service.submit_snapshot(PID, ["黑顺片", "法半夏"])
        hits = alerts_of(service, snap, "compatibility", Severity.CRITICAL)
        self.assertEqual(len(hits), 1)
        self.assertIn("附子", hits[0].summary)

    def test_gancao_pairs(self):
        service = make_service()
        snap = service.submit_snapshot(PID, ["炙甘草", "甘遂"])
        self.assertEqual(len(alerts_of(service, snap, "compatibility")), 1)

    def test_dose_limit_exceeded(self):
        service = make_service()
        snap = service.submit_snapshot(
            PID, [{"name": "制川乌", "amount": "二钱"}])  # 6g > 3g
        hits = alerts_of(service, snap, "dose", Severity.CRITICAL)
        self.assertEqual(len(hits), 1)
        self.assertIn("6g", hits[0].summary)

    def test_missing_dose_is_pending_verification(self):
        service = make_service()
        snap = service.submit_snapshot(PID, ["制川乌"])
        hits = alerts_of(service, snap, "dose", Severity.INFO)
        self.assertTrue(any("待核实" not in h.summary for h in hits))
        pending = [p for h in hits for p in h.pending]
        self.assertTrue(any("日剂量" in p for p in pending))
        self.assertTrue(any("先煎" in p for p in pending))

    def test_unknown_herb_flagged_not_guessed(self):
        service = make_service()
        snap = service.submit_snapshot(PID, ["秘制乌头散"])
        hits = alerts_of(service, snap, "normalization")
        self.assertEqual(len(hits), 1)
        self.assertEqual(snap.herb_lines[0].normalized_code, None)

    def test_same_severity_hits_merge_without_losing_evidence(self):
        # 制川乌 反 半夏、瓜蒌：两条 CRITICAL 命中合并为一条展示
        service = make_service()
        snap = service.submit_snapshot(PID, ["制川乌", "半夏", "全瓜蒌"])
        hits = alerts_of(service, snap, "compatibility", Severity.CRITICAL)
        self.assertEqual(len(hits), 1)  # 合并展示
        self.assertEqual(len(hits[0].details), 2)  # 明细不丢
        self.assertEqual(len(hits[0].evidence), 2)  # 依据不丢
        self.assertIn("合并", hits[0].summary)

    def test_warfarin_gancao_interaction(self):
        service = make_service(concurrent_medications=("华法林",))
        snap = service.submit_snapshot(PID, ["炙甘草"])
        hits = alerts_of(service, snap, "interaction", Severity.WARNING)
        self.assertEqual(len(hits), 1)
        self.assertIn("华法林", hits[0].summary)
        self.assertIn("并用药：华法林", hits[0].patient_facts)

    def test_allergy_unverified_is_pending(self):
        service = make_service(allergy_flags=("allergy-unverified",))
        snap = service.submit_snapshot(PID, ["炙甘草"])
        hits = alerts_of(service, snap, "allergy", Severity.INFO)
        self.assertEqual(len(hits), 1)
        self.assertTrue(any("核实" in p for p in hits[0].pending))

    def test_known_allergy_blocks(self):
        service = make_service(allergy_flags=("allergy:半夏类",))
        snap = service.submit_snapshot(PID, ["姜半夏"])
        self.assertEqual(len(alerts_of(service, snap, "allergy", Severity.CRITICAL)), 1)

    def test_pregnancy_contraindication(self):
        service = make_service(populations=("pregnancy",))
        snap = service.submit_snapshot(PID, ["制川乌"])
        self.assertEqual(len(alerts_of(service, snap, "population", Severity.CRITICAL)), 1)


class CoSignWorkflowTest(unittest.TestCase):
    def _signed_v2(self):
        """v1 建议调整 → v2 双向会签 → v3 修改前的标准场景。"""
        service = make_service(
            concurrent_medications=("华法林",), allergy_flags=("allergy-unverified",))
        s1 = service.submit_snapshot(PID, ["制川乌", "半夏"])
        service.record_opinion(s1.snapshot_id, ReviewState.ADJUST, "rph-li", "建议确认十八反")
        s2 = service.submit_snapshot(PID, ["制川乌", "姜半夏"])
        service.record_opinion(s2.snapshot_id, ReviewState.EXPLAINED, "dr-chen",
                               "宗《金匮要略》赤丸方义，已备案")
        service.record_opinion(s2.snapshot_id, ReviewState.RELEASED, "rph-li", "同意调配")
        return service, s1, s2

    def test_role_cannot_sign_for_other(self):
        service = make_service()
        snap = service.submit_snapshot(PID, ["制川乌", "半夏"])
        with self.assertRaises(ReviewError):  # 医师不能代签放行
            service.record_opinion(snap.snapshot_id, ReviewState.RELEASED, "dr-chen", "x")
        with self.assertRaises(ReviewError):  # 药师不能代签医师说明
            service.record_opinion(snap.snapshot_id, ReviewState.EXPLAINED, "rph-li", "x")
        with self.assertRaises(ReviewError):  # 未注册签署人
            service.record_opinion(snap.snapshot_id, ReviewState.VERIFY, "nobody", "x")

    def test_release_requires_bidirectional_cosign(self):
        service = make_service()
        snap = service.submit_snapshot(PID, ["制川乌", "半夏"])
        self.assertFalse(service.dispensing_gate(PID).allowed)
        service.record_opinion(snap.snapshot_id, ReviewState.EXPLAINED, "dr-chen", "经典方义")
        self.assertFalse(service.dispensing_gate(PID).allowed)  # 只有医师一侧
        service.record_opinion(snap.snapshot_id, ReviewState.RELEASED, "rph-li", "放行")
        self.assertTrue(service.dispensing_gate(PID).allowed)  # 双向会签齐全

    def test_same_person_cannot_complete_both_sides(self):
        service = make_service()
        # 同一 author_id 只能持有一个角色，不得改签兼任
        with self.assertRaises(ReviewError):
            service.register_signer("dr-chen", "pharmacist")
        snap = service.submit_snapshot(PID, ["炙甘草"])
        service.record_opinion(snap.snapshot_id, ReviewState.EXPLAINED, "dr-chen", "说明")
        # 医师身份无法签署药师放行，双向会签无法由一人完成
        with self.assertRaises(ReviewError):
            service.record_opinion(snap.snapshot_id, ReviewState.RELEASED, "dr-chen", "放行")
        gate = service.dispensing_gate(PID)
        self.assertFalse(gate.allowed)
        self.assertFalse(gate.pharmacist_released)

    def test_old_snapshot_rejects_new_opinions(self):
        service, s1, s2 = self._signed_v2()
        with self.assertRaises(ReviewError):  # 旧版不可补签
            service.record_opinion(s1.snapshot_id, ReviewState.RELEASED, "rph-li", "补签")

    def test_old_release_cannot_unlock_new_version(self):
        """核心证明：v2 已双向会签放行，医师加味成 v3 后——
        v3 闸门关闭（旧意见不沿用），且 v2 自身也因被取代而不可调配。"""
        service, s1, s2 = self._signed_v2()
        self.assertTrue(service.snapshot_gate(s2.snapshot_id).allowed)  # v2 当时可放行
        s3 = service.submit_snapshot(PID, ["制川乌", "姜半夏", "炙甘草"])
        gate_v3 = service.dispensing_gate(PID)
        self.assertFalse(gate_v3.allowed)  # 新版未被旧放行解锁
        self.assertFalse(gate_v3.physician_explained)
        self.assertFalse(gate_v3.pharmacist_released)
        gate_v2 = service.snapshot_gate(s2.snapshot_id)
        self.assertFalse(gate_v2.allowed)  # 旧版被取代后同样不可调配
        self.assertFalse(gate_v2.is_current)
        self.assertTrue(any("取代" in r for r in gate_v2.blocking_reasons))
        # v3 重新双向会签后才放行
        service.record_opinion(s3.snapshot_id, ReviewState.EXPLAINED, "dr-chen", "加味说明")
        service.record_opinion(s3.snapshot_id, ReviewState.RELEASED, "rph-li", "复核放行")
        self.assertTrue(service.dispensing_gate(PID).allowed)

    def test_rejection_terminates_snapshot(self):
        service = make_service()
        snap = service.submit_snapshot(PID, ["制川乌", "半夏"])
        service.record_opinion(snap.snapshot_id, ReviewState.REJECTED, "rph-li",
                               "十八反无经典依据，拒绝调配")
        self.assertFalse(service.dispensing_gate(PID).allowed)
        with self.assertRaises(ReviewError):  # 终止后不可再签
            service.record_opinion(snap.snapshot_id, ReviewState.RELEASED, "rph-li", "x")
        # 医师修改产生新快照后流程重启
        s2 = service.submit_snapshot(PID, ["制川乌", "炙甘草"])
        service.record_opinion(s2.snapshot_id, ReviewState.EXPLAINED, "dr-chen", "已去半夏")
        service.record_opinion(s2.snapshot_id, ReviewState.RELEASED, "rph-li", "放行")
        self.assertTrue(service.dispensing_gate(PID).allowed)

    def test_reason_required(self):
        service = make_service()
        snap = service.submit_snapshot(PID, ["炙甘草"])
        with self.assertRaises(ReviewError):
            service.record_opinion(snap.snapshot_id, ReviewState.EXPLAINED, "dr-chen", "  ")


class BoardQueryTest(unittest.TestCase):
    def _full_flow(self):
        service = make_service(
            concurrent_medications=("华法林",),
            allergy_flags=("allergy-unverified", "hiv-positive"),  # 后者与本案无关
        )
        fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
        snaps = [service.submit_snapshot(PID, s["herbs"]) for s in fixture["snapshots"]]
        service.record_opinion(snaps[2].snapshot_id, ReviewState.EXPLAINED, "dr-chen",
                               "宗《金匮要略》赤丸方义，INR 已嘱监测")
        service.record_opinion(snaps[2].snapshot_id, ReviewState.RELEASED, "rph-li",
                               "核对后放行")
        return service, snaps

    def test_board_shows_source_versions_and_opinions(self):
        service, snaps = self._full_flow()
        board = build_review_board(service, PID)
        self.assertEqual(board.current_snapshot_id, snaps[2].snapshot_id)
        comp = next(a for a in board.alerts if a.category_label == "配伍禁忌")
        self.assertEqual(comp.rule_sources[0].rule_id, "R-COMP-18FAN")
        self.assertEqual(comp.rule_sources[0].version, "2020.1")
        self.assertIn("药典", comp.rule_sources[0].source_reference)
        states = {o.state_label for o in board.opinions}
        self.assertEqual(states, {"医师说明", "药师放行"})  # 双方意见呈现
        self.assertTrue(board.gate.allowed)

    def test_board_shows_evolution(self):
        service, snaps = self._full_flow()
        board = build_review_board(service, PID)
        self.assertEqual([s.version for s in board.evolution], [1, 2, 3])
        v2, v3 = board.evolution[1], board.evolution[2]
        self.assertEqual(v2.added, ("姜半夏",))
        self.assertEqual(v2.removed, ("半夏",))
        self.assertEqual(v3.added, ("炙甘草",))
        # v3 新增华法林×甘草相互作用提示
        self.assertEqual(v3.warning_count, v2.warning_count + 1)

    def test_patient_info_minimal_disclosure(self):
        service, snaps = self._full_flow()
        board = build_review_board(service, PID)
        text = str(board)
        self.assertIn("并用药：华法林", board.disclosed_context)  # 被提示引用才披露
        self.assertIn("过敏史：未核实", board.disclosed_context)
        self.assertNotIn("hiv-positive", text)  # 无关敏感标记不出现
        self.assertNotIn("allergy-unverified", text)  # 内部标记原文不外泄
        self.assertEqual(board.patient_ref, "P-7F3A9C")  # 仅脱敏标识

    def test_fixture_end_to_end(self):
        fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
        service = make_service(
            concurrent_medications=tuple(fixture["concurrentMedication"]),
            allergy_flags=tuple(fixture["patientFlags"]))
        snaps = [service.submit_snapshot(fixture["prescriptionId"], s["herbs"])
                 for s in fixture["snapshots"]]
        # 三版均命中十八反（姜半夏不豁免）
        for snap in snaps:
            self.assertEqual(len(alerts_of(service, snap, "compatibility")), 1)
        # 仅 v3 命中华法林相互作用
        self.assertEqual(len(alerts_of(service, snaps[0], "interaction")), 0)
        self.assertEqual(len(alerts_of(service, snaps[2], "interaction")), 1)
        # 每版都有过敏史待核实
        for snap in snaps:
            self.assertEqual(len(alerts_of(service, snap, "allergy")), 1)


if __name__ == "__main__":
    unittest.main()
