"""版本化复核规则与规则引擎。

每条规则携带 rule_id / version / valid_from / source_reference，
命中后生成 RuleEvidence（见 contracts.py），提示只作为人工复核依据。
同一规则、同一紧急程度的重复命中合并为一条提示展示，
但全部命中明细与证据完整保留（合并展示，不丢依据）。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from enum import StrEnum

from domain.catalog import DEFAULT_CATALOG, HerbCatalog, HerbProfile
from domain.contracts import HerbLine, PrescriptionSnapshot, RuleEvidence


class Severity(StrEnum):
    CRITICAL = "critical"  # 必须处理后方可继续
    WARNING = "warning"  # 需要医师/药师重点关注
    INFO = "info"  # 提示与待核实事项


CATEGORY_LABELS = {
    "compatibility": "配伍禁忌",
    "dose": "剂量与单位",
    "normalization": "别名归一",
    "population": "特殊人群",
    "allergy": "过敏史",
    "interaction": "并用药",
}


@dataclass(frozen=True)
class PatientContext:
    """复核用患者信息（内部完整持有，对外按最小范围披露）。"""

    patient_ref: str  # 不透明脱敏标识
    concurrent_medications: tuple[str, ...] = ()
    allergy_flags: tuple[str, ...] = ()  # "allergy-unverified" 或 "allergy:<配伍族>"
    populations: tuple[str, ...] = ()  # "pregnancy" | "elderly" | "child"


@dataclass(frozen=True)
class RuleHit:
    """单条规则命中（未合并）。"""

    rule_id: str
    rule_version: str
    valid_from: date
    source_reference: str
    category: str
    severity: Severity
    detail: str  # 为何命中
    matched_codes: tuple[str, ...]
    matched_names: tuple[str, ...]
    pending: tuple[str, ...] = ()  # 尚待核实
    patient_facts: tuple[str, ...] = ()  # 本命中引用的患者信息（用于最小披露）

    def to_evidence(self) -> RuleEvidence:
        return RuleEvidence(
            rule_id=self.rule_id,
            version=self.rule_version,
            valid_from=self.valid_from,
            source_reference=self.source_reference,
            affected_codes=self.matched_codes,
        )


@dataclass(frozen=True)
class Alert:
    """合并后的复核提示：同规则同紧急程度的多条命中合并展示，依据全保留。"""

    alert_id: str
    snapshot_id: str
    rule_id: str
    category: str
    severity: Severity
    summary: str
    details: tuple[str, ...]
    matched_names: tuple[str, ...]
    pending: tuple[str, ...]
    patient_facts: tuple[str, ...]
    evidence: tuple[RuleEvidence, ...]


# ---------------------------------------------------------------- 规则数据
# 十八反：乌头类反半夏/瓜蒌/贝母/白蔹/白及；甘草类反海藻/大戟/甘遂/芫花；
# 藜芦类反诸参/细辛/芍药。按配伍族判定，炮制品（如姜半夏）不例外。
_EIGHTEEN_PAIRS: tuple[tuple[str, str], ...] = tuple(
    ("乌头类", other)
    for other in ("半夏类", "瓜蒌类", "贝母类", "白蔹类", "白及类")
) + tuple(
    ("甘草类", other) for other in ("海藻类", "大戟类", "甘遂类", "芫花类")
) + tuple(
    ("藜芦类", other) for other in ("参类", "辛类", "芍类")
)

_PREGNANCY_BANNED_FAMILIES = ("乌头类", "甘遂类", "大戟类", "芫花类", "藜芦类")
_POPULATION_LABELS = {"pregnancy": "妊娠", "elderly": "老年", "child": "儿童"}

# 并用药相互作用：(药品别名集, 标准名, [(匹配维度, 目标, 严重度, 说明)])
_INTERACTIONS: tuple[tuple[tuple[str, ...], str, tuple[tuple[str, str, Severity, str], ...]], ...] = (
    (
        ("华法林", "华法林钠", "华法令", "warfarin", "Warfarin"),
        "华法林",
        (
            ("family", "甘草类", Severity.WARNING,
             "甘草酸可致 INR 波动，联用华法林需监测凝血指标"),
            ("code", "HB-DANSHEN", Severity.WARNING,
             "丹参增强华法林抗凝作用，出血风险增加"),
        ),
    ),
)

# 规则版本元数据：(rule_id, version, valid_from, source_reference)
_RULE_META = {
    "R-COMP-18FAN": ("2020.1", date(2020, 12, 30),
                     "《中国药典》2020年版一部·十八反配伍禁忌（歌诀原出《儒门事亲》）"),
    "R-DOSE-TOXIC": ("2020.1", date(2020, 12, 30),
                     "《中国药典》2020年版一部·毒性饮片用法用量"),
    "R-NORM-ALIAS": ("2025.1", date(2025, 3, 1),
                     "本院《中药饮片别名与处方应付对照表》2025版"),
    "R-POP-SPECIAL": ("2020.1", date(2020, 12, 30),
                      "《中国药典》2020年版一部·妊娠禁忌及特殊人群用药注意"),
    "R-ALLERGY": ("2024.1", date(2024, 6, 1),
                  "本院药事管理委员会《中药过敏史核查规程》2024修订"),
    "R-INT-MED": ("2023.2", date(2023, 9, 15),
                  "院药学部《抗凝治疗患者中药联用药学监护要点》v2023.2"),
}

RULESET_ID = "herbal-review-rules"
RULESET_VERSION = "2026.09"  # 规则集整体版本，随任何规则版本变更而更新


def _meta(rule_id: str) -> tuple[str, date, str]:
    return _RULE_META[rule_id]


def _resolved(snapshot: PrescriptionSnapshot, catalog: HerbCatalog):
    """逐味归一，产出 (HerbLine, HerbProfile|None) 列表。"""
    return [(line, catalog.find(line.source_name)) for line in snapshot.herb_lines]


def _hit(rule_id: str, category: str, severity: Severity, detail: str,
         matched_codes, matched_names, pending=(), patient_facts=()) -> RuleHit:
    version, valid_from, source = _meta(rule_id)
    return RuleHit(rule_id, version, valid_from, source, category, severity,
                   detail, tuple(matched_codes), tuple(matched_names),
                   tuple(pending), tuple(patient_facts))


# ---------------------------------------------------------------- 各项规则

def check_normalization(snapshot: PrescriptionSnapshot, catalog: HerbCatalog) -> list[RuleHit]:
    """别名归一：未收录药名需人工核对，系统不猜测。"""
    hits = []
    for line, profile in _resolved(snapshot, catalog):
        if profile is None:
            hits.append(_hit(
                "R-NORM-ALIAS", "normalization", Severity.INFO,
                f"药名“{line.source_name}”未收录于本院别名对照表，无法自动归一",
                (), (line.source_name,),
                pending=(f"“{line.source_name}”需人工核对正名与应付品种后再评估配伍",),
            ))
    return hits


def check_dose(snapshot: PrescriptionSnapshot, catalog: HerbCatalog) -> list[RuleHit]:
    """剂量与单位：毒性饮片超量拦截；剂量缺失/无法折算列为待核实。"""
    hits = []
    for line, profile in _resolved(snapshot, catalog):
        if profile is None:
            continue
        codes, names = (profile.code,), (line.source_name,)
        if profile.toxicity == "highly_toxic":
            hits.append(_hit(
                "R-DOSE-TOXIC", "dose", Severity.WARNING,
                f"{profile.standard_name}为大毒饮片，内服须用炮制品并专项审批",
                codes, names,
                pending=(f"核实“{line.source_name}”炮制规格与用法（一般应改用制品）",),
            ))
        if profile.max_daily_grams is not None:
            if line.amount_grams is None:
                hits.append(_hit(
                    "R-DOSE-TOXIC", "dose", Severity.INFO,
                    f"{profile.standard_name}用量未标注或无法折算（药典日上限 "
                    f"{profile.max_daily_grams:g}g）",
                    codes, names,
                    pending=(f"核实“{line.source_name}”日剂量是否超过 "
                             f"{profile.max_daily_grams:g}g",),
                ))
            elif line.amount_grams > profile.max_daily_grams:
                hits.append(_hit(
                    "R-DOSE-TOXIC", "dose", Severity.CRITICAL,
                    f"{profile.standard_name} {line.amount_grams:g}g 超过药典日上限 "
                    f"{profile.max_daily_grams:g}g",
                    codes, names,
                ))
        if profile.requires_pre_decoct:
            hits.append(_hit(
                "R-DOSE-TOXIC", "dose", Severity.INFO,
                f"{profile.standard_name}需先煎 0.5–1 小时以减毒",
                codes, names,
                pending=(f"核实处方是否注明“{line.source_name}”先煎",),
            ))
    return hits


def check_compatibility(snapshot: PrescriptionSnapshot, catalog: HerbCatalog) -> list[RuleHit]:
    """配伍禁忌：十八反，按配伍族判定（炮制品不豁免）。"""
    by_family: dict[str, list[tuple[HerbLine, HerbProfile]]] = {}
    for line, profile in _resolved(snapshot, catalog):
        if profile is not None:
            by_family.setdefault(profile.family, []).append((line, profile))
    hits = []
    for family_a, family_b in _EIGHTEEN_PAIRS:
        if family_a not in by_family or family_b not in by_family:
            continue
        for line_a, profile_a in by_family[family_a]:
            for line_b, profile_b in by_family[family_b]:
                hits.append(_hit(
                    "R-COMP-18FAN", "compatibility", Severity.CRITICAL,
                    f"十八反：{profile_a.standard_name}（{family_a}）反"
                    f"{profile_b.standard_name}（{family_b}），"
                    f"处方写作“{line_a.source_name}”“{line_b.source_name}”",
                    (profile_a.code, profile_b.code),
                    (line_a.source_name, line_b.source_name),
                ))
    return hits


def check_population(snapshot: PrescriptionSnapshot, patient: PatientContext,
                     catalog: HerbCatalog) -> list[RuleHit]:
    """特殊人群：妊娠禁用毒性/攻逐类；老年、儿童慎用乌头类。"""
    resolved = _resolved(snapshot, catalog)
    hits = []
    if "pregnancy" in patient.populations:
        for line, profile in resolved:
            if profile is not None and profile.family in _PREGNANCY_BANNED_FAMILIES:
                hits.append(_hit(
                    "R-POP-SPECIAL", "population", Severity.CRITICAL,
                    f"妊娠期禁用{profile.family}饮片（{profile.standard_name}）",
                    (profile.code,), (line.source_name,),
                    patient_facts=("特殊人群：妊娠",),
                ))
    if {"elderly", "child"} & set(patient.populations):
        labels = "、".join(_POPULATION_LABELS[p] for p in patient.populations
                          if p in ("elderly", "child"))
        for line, profile in resolved:
            if profile is not None and profile.family == "乌头类":
                hits.append(_hit(
                    "R-POP-SPECIAL", "population", Severity.WARNING,
                    f"{labels}患者慎用乌头类（{profile.standard_name}），需减量并监护",
                    (profile.code,), (line.source_name,),
                    patient_facts=tuple(f"特殊人群：{_POPULATION_LABELS[p]}"
                                        for p in patient.populations
                                        if p in ("elderly", "child")),
                ))
    return hits


def check_allergy(snapshot: PrescriptionSnapshot, patient: PatientContext,
                  catalog: HerbCatalog) -> list[RuleHit]:
    """过敏史：未核实须发药前确认；已知过敏族命中即拦截。"""
    hits = []
    if "allergy-unverified" in patient.allergy_flags:
        hits.append(_hit(
            "R-ALLERGY", "allergy", Severity.INFO,
            "患者过敏史标记为“未核实”",
            (), (),
            pending=("发药前向患者核实中药/食物过敏史并补录",),
            patient_facts=("过敏史：未核实",),
        ))
    known = {flag.split(":", 1)[1] for flag in patient.allergy_flags
             if flag.startswith("allergy:")}
    if known:
        for line, profile in _resolved(snapshot, catalog):
            if profile is not None and profile.family in known:
                hits.append(_hit(
                    "R-ALLERGY", "allergy", Severity.CRITICAL,
                    f"患者对{profile.family}有过敏记录，方中含{profile.standard_name}",
                    (profile.code,), (line.source_name,),
                    patient_facts=tuple(f"过敏史：{family}过敏" for family in sorted(known)),
                ))
    return hits


def check_interaction(snapshot: PrescriptionSnapshot, patient: PatientContext,
                      catalog: HerbCatalog) -> list[RuleHit]:
    """并用药：抗凝药等与方中饮片的相互作用。"""
    hits = []
    resolved = _resolved(snapshot, catalog)
    for aliases, standard_med, entries in _INTERACTIONS:
        if not any(med.strip() in aliases for med in patient.concurrent_medications):
            continue
        for dimension, target, severity, message in entries:
            for line, profile in resolved:
                if profile is None:
                    continue
                matched = (profile.family == target if dimension == "family"
                           else profile.code == target)
                if matched:
                    hits.append(_hit(
                        "R-INT-MED", "interaction", severity,
                        f"并用{standard_med}：{message}（方中{profile.standard_name}）",
                        (profile.code,), (line.source_name,),
                        pending=(f"核实患者{standard_med}剂量与最近 INR/凝血指标",),
                        patient_facts=(f"并用药：{standard_med}",),
                    ))
    return hits


_CHECKS = ("normalization", "dose", "compatibility", "population", "allergy", "interaction")


def evaluate(snapshot: PrescriptionSnapshot, patient: PatientContext,
             catalog: HerbCatalog = DEFAULT_CATALOG) -> list[Alert]:
    """对快照运行全部规则并合并展示；结果按紧急程度排序。"""
    hits: list[RuleHit] = []
    hits += check_normalization(snapshot, catalog)
    hits += check_dose(snapshot, catalog)
    hits += check_compatibility(snapshot, catalog)
    hits += check_population(snapshot, patient, catalog)
    hits += check_allergy(snapshot, patient, catalog)
    hits += check_interaction(snapshot, patient, catalog)
    return merge_hits(snapshot.snapshot_id, hits)


def merge_hits(snapshot_id: str, hits: list[RuleHit]) -> list[Alert]:
    """同规则、同紧急程度的命中合并为一条提示，明细与证据全部保留。"""
    groups: dict[tuple[str, Severity], list[RuleHit]] = {}
    for hit in hits:
        groups.setdefault((hit.rule_id, hit.severity), []).append(hit)
    alerts = []
    for (rule_id, severity), group in groups.items():
        details = tuple(h.detail for h in group)
        label = CATEGORY_LABELS[group[0].category]
        summary = (details[0] if len(details) == 1
                   else f"{label}共 {len(details)} 项命中（同规则同紧急程度，合并展示）")
        alerts.append(Alert(
            alert_id=f"{snapshot_id}#{rule_id}:{severity}",
            snapshot_id=snapshot_id,
            rule_id=rule_id,
            category=group[0].category,
            severity=severity,
            summary=summary,
            details=details,
            matched_names=tuple(dict.fromkeys(n for h in group for n in h.matched_names)),
            pending=tuple(dict.fromkeys(p for h in group for p in h.pending)),
            patient_facts=tuple(dict.fromkeys(f for h in group for f in h.patient_facts)),
            evidence=tuple(h.to_evidence() for h in group),
        ))
    order = {Severity.CRITICAL: 0, Severity.WARNING: 1, Severity.INFO: 2}
    alerts.sort(key=lambda a: (order[a.severity], _CHECKS.index(a.category)))
    return alerts
