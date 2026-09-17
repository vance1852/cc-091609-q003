"""端到端演示：python -m domain.demo

按 fixtures/prescription_review.json 走完整流程：
v1（制川乌+半夏）→ 药师建议调整 → v2（半夏改姜半夏）→ 双向会签放行
→ 医师再加炙甘草成 v3 → 证明旧版放行无法解锁新版 → v3 重新会签 → 看板查询。
"""

from __future__ import annotations

import json
from pathlib import Path

from domain.contracts import ReviewState
from domain.queries import ReviewBoard, build_review_board
from domain.review import ReviewService
from domain.rules import PatientContext, Severity

FIXTURE = Path(__file__).resolve().parent.parent / "fixtures" / "prescription_review.json"

_SEVERITY_LABEL = {Severity.CRITICAL: "严重", Severity.WARNING: "警告", Severity.INFO: "提示"}


def _print_alerts(service: ReviewService, snapshot_id: str) -> None:
    for alert in service.alerts_for(snapshot_id):
        print(f"  [{_SEVERITY_LABEL[alert.severity]}] {alert.summary}")
        for detail in alert.details[1:]:
            print(f"      - {detail}")
        for pending in alert.pending:
            print(f"      待核实：{pending}")
        for ev in alert.evidence:
            print(f"      依据：{ev.rule_id} v{ev.version}（{ev.source_reference}）"
                  f" 涉及 {', '.join(ev.affected_codes)}")


def _print_gate(gate, title: str) -> None:
    verdict = "允许调配" if gate.allowed else "禁止调配"
    print(f"  {title}：{verdict}")
    for reason in gate.blocking_reasons:
        print(f"      阻断原因：{reason}")


def _print_board(board: ReviewBoard) -> None:
    print(f"处方 {board.prescription_id}  当前快照 {board.current_snapshot_id}"
          f"  规则集 {board.ruleset}")
    print(f"患者标识（脱敏）：{board.patient_ref}")
    print(f"最小披露的患者信息：{list(board.disclosed_context)}")
    print("\n-- 当前提示（含来源版本）--")
    for view in board.alerts:
        print(f"  [{_SEVERITY_LABEL[view.severity]}][{view.category_label}] "
              f"{view.summary}（状态：{view.status_label}）")
        for src in view.rule_sources:
            print(f"      来源：{src.rule_id} v{src.version} — {src.source_reference}")
        for pending in view.pending:
            print(f"      待核实：{pending}")
    print("\n-- 当前快照双方会签 --")
    for opinion in board.opinions:
        print(f"  {opinion.state_label}（{opinion.author_role}）：{opinion.reason}")
    print("\n-- 处方演变 --")
    for snap in board.evolution:
        diff = []
        if snap.added:
            diff.append(f"新增 {list(snap.added)}")
        if snap.removed:
            diff.append(f"移除 {list(snap.removed)}")
        print(f"  v{snap.version} {snap.snapshot_id}：{list(snap.herbs)}"
              f"  提示(严重{snap.critical_count}/警告{snap.warning_count}/提示{snap.info_count})"
              f"  会签{list(snap.opinion_labels) or '无'}"
              + (f"  变更：{'，'.join(diff)}" if diff else ""))
    print("\n-- 放行闸门 --")
    _print_gate(board.gate, f"当前快照 {board.gate.snapshot_id}")


def main() -> None:
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    pid = fixture["prescriptionId"]

    service = ReviewService()
    service.register_signer("dr-chen", "physician")
    service.register_signer("rph-li", "pharmacist")
    service.set_patient_context(pid, PatientContext(
        patient_ref="P-7F3A9C",  # 脱敏标识，不含姓名/证件号
        concurrent_medications=tuple(fixture["concurrentMedication"]),
        allergy_flags=tuple(fixture["patientFlags"]),
    ))

    print("=" * 72)
    print("v1：医师初方（制川乌 + 半夏）")
    print("=" * 72)
    snap1 = service.submit_snapshot(pid, fixture["snapshots"][0]["herbs"])
    _print_alerts(service, snap1.snapshot_id)
    service.record_opinion(snap1.snapshot_id, ReviewState.ADJUST, "rph-li",
                           "川乌反半夏（十八反），建议医师确认配伍依据或调整药味")
    service.record_opinion(snap1.snapshot_id, ReviewState.EXPLAINED, "dr-chen",
                           "拟改姜半夏缓性后再议，本版作废")

    print("\n" + "=" * 72)
    print("v2：医师修改（半夏 → 姜半夏）——新快照、全量重算，v1 意见不沿用")
    print("=" * 72)
    snap2 = service.submit_snapshot(pid, fixture["snapshots"][1]["herbs"])
    _print_alerts(service, snap2.snapshot_id)
    service.record_opinion(snap2.snapshot_id, ReviewState.EXPLAINED, "dr-chen",
                           "乌头与半夏同用宗《金匮要略》赤丸方义，姜半夏炮制后配伍，"
                           "已按院超常规用药备案（备案号 2026-018），监测不良反应")
    service.record_opinion(snap2.snapshot_id, ReviewState.RELEASED, "rph-li",
                           "经典方义与备案齐全，同意调配，交代先煎与随访")
    _print_gate(service.snapshot_gate(snap2.snapshot_id), "v2 双向会签后闸门")

    print("\n" + "=" * 72)
    print("v3：医师再加炙甘草——旧版放行不得解锁新版")
    print("=" * 72)
    snap3 = service.submit_snapshot(pid, fixture["snapshots"][2]["herbs"])
    _print_alerts(service, snap3.snapshot_id)
    print("\n  [证明] 旧版放行无法解锁新版处方：")
    _print_gate(service.snapshot_gate(snap2.snapshot_id), "v2 快照闸门（已被取代）")
    _print_gate(service.dispensing_gate(pid), "v3 当前闸门（会签前）")

    service.record_opinion(snap3.snapshot_id, ReviewState.EXPLAINED, "dr-chen",
                           "加炙甘草调和诸药，知悉华法林联用风险，已嘱每周监测 INR")
    service.record_opinion(snap3.snapshot_id, ReviewState.RELEASED, "rph-li",
                           "核对 INR 监测计划后放行，发药时交代煎法与出血征兆")
    print()
    _print_gate(service.dispensing_gate(pid), "v3 双向会签后闸门")

    print("\n" + "=" * 72)
    print("最终查询：复核看板")
    print("=" * 72)
    _print_board(build_review_board(service, pid))


if __name__ == "__main__":
    main()
