"""版本化规则包与命中评估。

六类规则各自携带版本号、生效日期与来源文献，命中后生成 RuleEvidence，
使药师能看到“规则为何命中、出自哪一版依据”。本模块只做提示与归并，
不替代医师与药师的临床判断。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from itertools import combinations

from .contracts import (
    Finding,
    MergedAlert,
    PatientContext,
    RuleCategory,
    RuleEvidence,
    Severity,
)
from .normalization import HerbNormalizer, NormalizedHerb


@dataclass(frozen=True)
class RulePackMeta:
    category: RuleCategory
    version: str
    valid_from: date
    source_reference: str


ALIAS_PACK = RulePackMeta(
    RuleCategory.ALIAS,
    "alias-2025.1",
    date(2025, 1, 1),
    "《中国药典》2020年版一部 品名与炮制品规范",
)
DOSE_UNIT_PACK = RulePackMeta(
    RuleCategory.DOSE_UNIT,
    "dose-2025.1",
    date(2025, 1, 1),
    "《中国药典》2020年版一部 用法与用量；现代衡制 1钱=3g",
)
INCOMPATIBILITY_PACK = RulePackMeta(
    RuleCategory.INCOMPATIBILITY,
    "incomp-2025.1",
    date(2025, 1, 1),
    "十八反歌诀（《儒门事亲》）；《中国药典》2020年版一部“注意”项",
)
SPECIAL_POPULATION_PACK = RulePackMeta(
    RuleCategory.SPECIAL_POPULATION,
    "pop-2025.1",
    date(2025, 1, 1),
    "《中国药典》2020年版一部 妊娠禁忌与慎用项",
)
ALLERGY_PACK = RulePackMeta(
    RuleCategory.ALLERGY,
    "allergy-2025.1",
    date(2025, 1, 1),
    "医疗机构药事管理规定 过敏史核对规范",
)
CONCURRENT_MED_PACK = RulePackMeta(
    RuleCategory.CONCURRENT_MED,
    "ddi-2025.1",
    date(2025, 1, 1),
    "《临床用药须知》2020年版；华法林药物相互作用共识",
)

ALL_PACKS: tuple[RulePackMeta, ...] = (
    ALIAS_PACK,
    DOSE_UNIT_PACK,
    INCOMPATIBILITY_PACK,
    SPECIAL_POPULATION_PACK,
    ALLERGY_PACK,
    CONCURRENT_MED_PACK,
)

# ---- 规则数据 --------------------------------------------------------------

WUTOU = frozenset({"CHUANWU", "CAOWU", "FUZI"})
ZHU_SHEN = frozenset(
    {"RENSHEN", "DANGSHEN", "DANSHEN", "XUANSHEN", "NANSHASHEN", "BEISHASHEN", "KUSHEN"}
)

# (rule_id, 甲类编码, 乙类编码, 说明)
INCOMPATIBILITY_RULES: tuple[tuple[str, frozenset[str], frozenset[str], str], ...] = (
    (
        "incomp-18fan-wutou",
        WUTOU,
        frozenset({"BANXIA", "GUALOU", "TIANHUAFEN", "BEIMU", "BAILIAN", "BAIJI"}),
        "十八反：乌头类（川乌/草乌/附子）反半夏、瓜蒌、天花粉、贝母、白蔹、白及",
    ),
    (
        "incomp-18fan-gancao",
        frozenset({"GANCAO"}),
        frozenset({"HAIZAO", "JINGDAJI", "GANSUI", "YUANHUA"}),
        "十八反：甘草反海藻、京大戟、甘遂、芫花",
    ),
    (
        "incomp-18fan-lilu",
        frozenset({"LILU"}),
        ZHU_SHEN | {"XIXIN", "SHAOYAO"},
        "十八反：藜芦反诸参、细辛、芍药",
    ),
)

# (rule_id, 并用药编码, 中药编码集, 严重度, 说明)
CONCURRENT_MED_RULES: tuple[tuple[str, str, frozenset[str], Severity, str], ...] = (
    (
        "ddi-warfarin-wutou",
        "WARFARIN",
        WUTOU,
        Severity.MAJOR,
        "乌头类与华法林并用：增加出血与心律失常风险，需医师确认并监测",
    ),
    (
        "ddi-warfarin-gancao",
        "WARFARIN",
        frozenset({"GANCAO"}),
        Severity.MAJOR,
        "甘草可干扰华法林抗凝稳定性，需监测 INR",
    ),
    (
        "ddi-warfarin-danshen",
        "WARFARIN",
        frozenset({"DANSHEN"}),
        Severity.MAJOR,
        "丹参可增强华法林抗凝作用，增加出血风险",
    ),
)

# (rule_id, 患者标记, 中药编码集, 严重度, 说明)
SPECIAL_POPULATION_RULES: tuple[tuple[str, str, frozenset[str], Severity, str], ...] = (
    ("pop-pregnancy-wutou", "pregnant", WUTOU, Severity.CRITICAL, "妊娠期禁用乌头类毒性药"),
    (
        "pop-pregnancy-poison",
        "pregnant",
        frozenset({"GANSUI", "JINGDAJI", "YUANHUA"}),
        Severity.CRITICAL,
        "妊娠期禁用峻下逐水药",
    ),
    ("pop-elderly-wutou", "elderly", WUTOU, Severity.MINOR, "老年患者用乌头类宜减量并监测"),
    (
        "pop-hepatic-wutou",
        "hepatic-impairment",
        WUTOU,
        Severity.MAJOR,
        "肝功能不全者慎用乌头类",
    ),
)

# (rule_id, 标准编码, 一日最大量 g, 说明)
DOSE_LIMITS: tuple[tuple[str, str, float, str], ...] = (
    ("dose-chuanwu-max", "CHUANWU", 3.0, "制川乌一日量超过药典上限 3g"),
    ("dose-caowu-max", "CAOWU", 3.0, "制草乌一日量超过药典上限 3g"),
    ("dose-fuzi-max", "FUZI", 15.0, "附子一日量超过药典上限 15g"),
    ("dose-banxia-max", "BANXIA", 9.0, "半夏炮制品一日量超过药典上限 9g"),
    ("dose-gancao-max", "GANCAO", 10.0, "甘草一日量超过药典上限 10g"),
    ("dose-xixin-max", "XIXIN", 3.0, "细辛一日量超过 3g（“细辛不过钱”）"),
)


def _evidence(pack: RulePackMeta, rule_id: str, affected: tuple[str, ...]) -> RuleEvidence:
    return RuleEvidence(
        rule_id=rule_id,
        version=pack.version,
        valid_from=pack.valid_from,
        source_reference=pack.source_reference,
        affected_codes=affected,
    )


# ---- 各类规则评估 ----------------------------------------------------------


def evaluate_alias(herbs: list[NormalizedHerb]) -> list[Finding]:
    findings = []
    for herb in herbs:
        if herb.normalized_code is None:
            findings.append(
                Finding(
                    category=RuleCategory.ALIAS,
                    severity=Severity.INFO,
                    summary=f"药名“{herb.source_name}”未能归一，需人工核实是否别名或错别字",
                    evidence=(_evidence(ALIAS_PACK, "alias-unmapped", ()),),
                    involved_names=(herb.source_name,),
                    needs_verification=True,
                )
            )
    return findings


def evaluate_dose_unit(herbs: list[NormalizedHerb]) -> list[Finding]:
    findings = []
    totals: dict[str, float] = {}
    names_by_code: dict[str, list[str]] = {}
    for herb in herbs:
        if herb.normalized_code is None:
            continue
        if herb.source_amount and herb.amount_grams is None:
            findings.append(
                Finding(
                    category=RuleCategory.DOSE_UNIT,
                    severity=Severity.INFO,
                    summary=f"“{herb.source_name}”的剂量“{herb.source_amount}”无法标准化，待核实",
                    evidence=(
                        _evidence(DOSE_UNIT_PACK, "dose-unit-unparsed", (herb.normalized_code,)),
                    ),
                    involved_names=(herb.source_name,),
                    needs_verification=True,
                )
            )
        if herb.amount_grams is not None:
            totals[herb.normalized_code] = totals.get(herb.normalized_code, 0.0) + herb.amount_grams
            names_by_code.setdefault(herb.normalized_code, []).append(herb.source_name)
    for rule_id, code, max_grams, text in DOSE_LIMITS:
        total = totals.get(code)
        if total is not None and total > max_grams:
            findings.append(
                Finding(
                    category=RuleCategory.DOSE_UNIT,
                    severity=Severity.MAJOR,
                    summary=f"{text}（当前合计 {total:g}g）",
                    evidence=(_evidence(DOSE_UNIT_PACK, rule_id, (code,)),),
                    involved_names=tuple(names_by_code[code]),
                )
            )
    return findings


def evaluate_incompatibility(herbs: list[NormalizedHerb]) -> list[Finding]:
    # 按处方行产出命中：同一归一编码的多个药味（如 半夏 与 姜半夏）各自命中，
    # 由 merge_findings 在展示层合并，避免弹窗疲劳且依据不丢。
    coded = [herb for herb in herbs if herb.normalized_code is not None]
    findings = []
    for rule_id, group_a, group_b, text in INCOMPATIBILITY_RULES:
        for herb_a, herb_b in combinations(coded, 2):
            if herb_a.normalized_code == herb_b.normalized_code:
                continue
            pair = frozenset({herb_a.normalized_code, herb_b.normalized_code})
            if (pair & group_a) and (pair & group_b):
                findings.append(
                    Finding(
                        category=RuleCategory.INCOMPATIBILITY,
                        severity=Severity.CRITICAL,
                        summary=f"{text}：{herb_a.source_name} 与 {herb_b.source_name} 同方出现",
                        evidence=(
                            _evidence(INCOMPATIBILITY_PACK, rule_id, tuple(sorted(pair))),
                        ),
                        involved_names=(herb_a.source_name, herb_b.source_name),
                    )
                )
    return findings


def evaluate_special_population(
    herbs: list[NormalizedHerb], patient: PatientContext
) -> list[Finding]:
    codes = {h.normalized_code for h in herbs if h.normalized_code is not None}
    findings = []
    flags = set(patient.flags)
    for rule_id, flag, herb_codes, severity, text in SPECIAL_POPULATION_RULES:
        if flag not in flags:
            continue
        hit = sorted(codes & herb_codes)
        if hit:
            findings.append(
                Finding(
                    category=RuleCategory.SPECIAL_POPULATION,
                    severity=severity,
                    summary=f"{text}（患者标记 {flag}）",
                    evidence=(
                        _evidence(SPECIAL_POPULATION_PACK, rule_id, tuple(hit)),
                    ),
                    involved_names=tuple(
                        h.source_name for h in herbs if h.normalized_code in hit
                    ),
                )
            )
    return findings


def evaluate_allergy(herbs: list[NormalizedHerb], patient: PatientContext) -> list[Finding]:
    findings = []
    flags = set(patient.flags)
    if "allergy-unverified" in flags:
        findings.append(
            Finding(
                category=RuleCategory.ALLERGY,
                severity=Severity.INFO,
                summary="过敏史标记为“未核实”，发药前需向患者或医师确认",
                evidence=(_evidence(ALLERGY_PACK, "allergy-unverified", ()),),
                needs_verification=True,
            )
        )
    by_code: dict[str, list[str]] = {}
    for herb in herbs:
        if herb.normalized_code is not None:
            by_code.setdefault(herb.normalized_code, []).append(herb.source_name)
    for flag in sorted(flags):
        if flag.startswith("allergy:"):
            code = flag.split(":", 1)[1]
            if code in by_code:
                findings.append(
                    Finding(
                        category=RuleCategory.ALLERGY,
                        severity=Severity.CRITICAL,
                        summary=f"患者已知对 {code} 过敏，方中含该药味",
                        evidence=(_evidence(ALLERGY_PACK, "allergy-known", (code,)),),
                        involved_names=tuple(by_code[code]),
                    )
                )
    return findings


def evaluate_concurrent_med(
    herbs: list[NormalizedHerb],
    patient: PatientContext,
    normalizer: HerbNormalizer,
) -> list[Finding]:
    by_code: dict[str, list[str]] = {}
    for herb in herbs:
        if herb.normalized_code is not None:
            by_code.setdefault(herb.normalized_code, []).append(herb.source_name)
    findings = []
    for med in patient.concurrent_medications:
        med_code = normalizer.normalize_name(med)
        if med_code is None:
            findings.append(
                Finding(
                    category=RuleCategory.CONCURRENT_MED,
                    severity=Severity.INFO,
                    summary=f"并用药“{med}”未能归一，相互作用核对待人工完成",
                    evidence=(_evidence(CONCURRENT_MED_PACK, "ddi-med-unmapped", ()),),
                    involved_names=(med,),
                    needs_verification=True,
                )
            )
            continue
        for rule_id, rule_med, herb_codes, severity, text in CONCURRENT_MED_RULES:
            if med_code != rule_med:
                continue
            hit = sorted(set(by_code) & herb_codes)
            if hit:
                findings.append(
                    Finding(
                        category=RuleCategory.CONCURRENT_MED,
                        severity=severity,
                        summary=f"{text}（并用药：{med}）",
                        evidence=(
                            _evidence(
                                CONCURRENT_MED_PACK,
                                rule_id,
                                tuple([med_code, *hit]),
                            ),
                        ),
                        involved_names=tuple(
                            [med]
                            + [n for code in hit for n in by_code[code]]
                        ),
                    )
                )
    return findings


# ---- 汇总与合并 ------------------------------------------------------------


def evaluate_all(
    herbs: list[NormalizedHerb],
    patient: PatientContext,
    normalizer: HerbNormalizer,
) -> list[Finding]:
    return (
        evaluate_alias(herbs)
        + evaluate_dose_unit(herbs)
        + evaluate_incompatibility(herbs)
        + evaluate_special_population(herbs, patient)
        + evaluate_allergy(herbs, patient)
        + evaluate_concurrent_med(herbs, patient, normalizer)
    )


def merge_findings(findings: list[Finding]) -> list[MergedAlert]:
    """紧急程度与涉及编码相同的重复命中合并展示，依据全部保留。"""
    groups: dict[tuple, MergedAlert] = {}
    for finding in findings:
        affected = tuple(
            sorted({code for ev in finding.evidence for code in ev.affected_codes})
        )
        key = (finding.category, finding.severity, affected)
        if key not in groups:
            groups[key] = MergedAlert(
                category=finding.category,
                severity=finding.severity,
                summary=finding.summary,
                evidences=(),
                involved_names=(),
                hit_count=0,
                needs_verification=finding.needs_verification,
            )
        current = groups[key]
        merged_evidences = list(current.evidences)
        for ev in finding.evidence:
            if ev not in merged_evidences:
                merged_evidences.append(ev)
        merged_names = list(current.involved_names)
        for name in finding.involved_names:
            if name not in merged_names:
                merged_names.append(name)
        groups[key] = MergedAlert(
            category=current.category,
            severity=current.severity,
            summary=current.summary,
            evidences=tuple(merged_evidences),
            involved_names=tuple(merged_names),
            hit_count=current.hit_count + 1,
            needs_verification=current.needs_verification or finding.needs_verification,
        )
    severity_rank = {
        Severity.CRITICAL: 0,
        Severity.MAJOR: 1,
        Severity.MINOR: 2,
        Severity.INFO: 3,
    }
    return sorted(groups.values(), key=lambda a: (severity_rank[a.severity], a.category))
