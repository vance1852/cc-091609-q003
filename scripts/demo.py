"""用 fixtures/prescription_review.json 演示完整复核流程。

运行：python3 scripts/demo.py
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from itertools import count
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from domain import (  # noqa: E402
    Actor,
    ActorRole,
    DisclosurePurpose,
    ReviewService,
    ReviewState,
    review_timeline,
)

FIXTURE = Path(__file__).resolve().parent.parent / "fixtures" / "prescription_review.json"

PHYSICIAN = Actor("dr-li", ActorRole.PHYSICIAN, "李医师")
PHARMACIST = Actor("ph-wang", ActorRole.PHARMACIST, "王药师")


def clock():
    ticks = count()
    base = datetime(2026, 9, 17, 9, 0, tzinfo=timezone.utc)
    return lambda: base + timedelta(minutes=next(ticks))


def main() -> None:
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    rx_id = data["prescriptionId"]
    service = ReviewService(clock=clock())
    service.register_patient_context(
        rx_id,
        flags=data["patientFlags"],
        concurrent_medications=data["concurrentMedication"],
    )

    # v1：制川乌 + 半夏 —— 十八反命中，走完双向会签后放行。
    v1 = service.add_snapshot(rx_id, data["snapshots"][0]["herbs"])
    print(f"== v{v1.snapshot.version} 命中 ==")
    for alert in v1.alerts:
        print(f"  [{alert.severity}] {alert.summary} (规则版本 {alert.evidences[0].version})")
    snap = v1.snapshot.snapshot_id
    service.record_opinion(PHARMACIST, snap, ReviewState.VERIFY, "核对患者过敏史与并用药华法林")
    service.record_opinion(PHARMACIST, snap, ReviewState.ADJUST, "建议医师确认乌头-半夏同用依据")
    service.record_opinion(PHYSICIAN, snap, ReviewState.EXPLAINED, "引《金匮要略》方义，监制同用")
    service.record_opinion(PHARMACIST, snap, ReviewState.RELEASED, "复核通过，放行")
    print(f"v1 放行决定: {service.dispense_decision(rx_id).allowed}\n")

    # v2 / v3：医师两次修改药味，旧版放行无法解锁新版。
    for snap_data in data["snapshots"][1:]:
        record = service.add_snapshot(rx_id, snap_data["herbs"])
        decision = service.dispense_decision(rx_id)
        print(f"== v{record.snapshot.version} 医师修改后 ==")
        print(f"  放行: {decision.allowed}; 缺口: {list(decision.missing)}")
        for note in decision.notes:
            print(f"  注: {note}")
        service.record_opinion(
            PHYSICIAN, record.snapshot.snapshot_id, ReviewState.EXPLAINED, "沿用方义，确认修改"
        )
        service.record_opinion(
            PHARMACIST, record.snapshot.snapshot_id, ReviewState.RELEASED, "复核通过，放行"
        )
        print(f"  当前快照双向会签后放行: {service.dispense_decision(rx_id).allowed}\n")

    # 最终查询：审计视角（患者敏感信息按最小范围隐去）。
    timeline = review_timeline(service, rx_id, purpose=DisclosurePurpose.AUDIT)
    print("== 最终查询（audit，最小范围披露）==")
    print(json.dumps(timeline, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
