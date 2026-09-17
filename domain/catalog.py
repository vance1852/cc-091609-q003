"""饮片目录：别名归一与剂量单位标准化。

处方手写名（含炮制品别名）归一到《中国药典》标准名与配伍族；
剂量写法（克/g/钱/两/分，阿拉伯或中文数字）统一折算为克。
无法归一或无法折算的一律返回 None，交由规则引擎标记为“待核实”，
系统不做猜测性判断。
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class HerbProfile:
    code: str  # 规范编码，如 HB-ZHI-CHUANWU
    standard_name: str  # 药典标准名
    family: str  # 配伍族（十八反按族判定，炮制品不改族）
    aliases: tuple[str, ...] = ()
    toxicity: str = "none"  # none | toxic（有毒） | highly_toxic（大毒）
    max_daily_grams: float | None = None  # 药典日用量上限
    requires_pre_decoct: bool = False  # 需先煎


def _p(
    code: str,
    name: str,
    family: str,
    aliases: tuple[str, ...] = (),
    toxicity: str = "none",
    max_g: float | None = None,
    pre: bool = False,
) -> HerbProfile:
    return HerbProfile(code, name, family, aliases, toxicity, max_g, pre)


# 乌头类（反半夏、瓜蒌、贝母、白蔹、白及）
# 半夏类（处方写“半夏”按应付常规归一到法半夏；炮制不改变配伍族）
_PROFILES: tuple[HerbProfile, ...] = (
    _p("HB-ZHI-CHUANWU", "制川乌", "乌头类", ("制川乌", "制川乌头"), "toxic", 3.0, True),
    _p("HB-CHUANWU", "川乌", "乌头类", ("川乌", "生川乌", "川乌头"), "highly_toxic"),
    _p("HB-ZHI-CAOWU", "制草乌", "乌头类", ("制草乌", "制草乌头"), "toxic", 3.0, True),
    _p("HB-CAOWU", "草乌", "乌头类", ("草乌", "生草乌", "草乌头"), "highly_toxic"),
    _p("HB-FUZI", "附子", "乌头类", ("附子", "黑顺片", "白附片", "炮附片", "熟附片", "制附子"), "toxic", 15.0, True),
    _p("HB-FABANXIA", "法半夏", "半夏类", ("半夏", "法半夏", "制半夏")),
    _p("HB-SHENGBANXIA", "生半夏", "半夏类", ("生半夏",), "toxic", 9.0),
    _p("HB-JIANGBANXIA", "姜半夏", "半夏类", ("姜半夏", "姜夏")),
    _p("HB-QINGBANXIA", "清半夏", "半夏类", ("清半夏", "清夏")),
    _p("HB-BANXIAQU", "半夏曲", "半夏类", ("半夏曲",)),
    _p("HB-GUALOU", "瓜蒌", "瓜蒌类", ("瓜蒌", "全瓜蒌", "栝楼")),
    _p("HB-GUALOUPI", "瓜蒌皮", "瓜蒌类", ("瓜蒌皮", "栝楼皮")),
    _p("HB-GUALOUREN", "瓜蒌子", "瓜蒌类", ("瓜蒌子", "瓜蒌仁")),
    _p("HB-TIANHUAFEN", "天花粉", "瓜蒌类", ("天花粉", "栝楼根")),
    _p("HB-CHUANBEI", "川贝母", "贝母类", ("川贝母", "川贝")),
    _p("HB-ZHEBEI", "浙贝母", "贝母类", ("浙贝母", "浙贝", "大贝母")),
    _p("HB-PINGBEI", "平贝母", "贝母类", ("平贝母",)),
    _p("HB-YIBEI", "伊贝母", "贝母类", ("伊贝母",)),
    _p("HB-HUBEIBEI", "湖北贝母", "贝母类", ("湖北贝母",)),
    _p("HB-BAILIAN", "白蔹", "白蔹类", ("白蔹",)),
    _p("HB-BAIJI", "白及", "白及类", ("白及",)),
    _p("HB-GANCAO", "甘草", "甘草类", ("甘草", "生甘草", "粉甘草"), max_g=10.0),
    _p("HB-ZHIGANCAO", "炙甘草", "甘草类", ("炙甘草", "蜜炙甘草", "炙草"), max_g=10.0),
    _p("HB-HAIZAO", "海藻", "海藻类", ("海藻",)),
    _p("HB-JINGDAJI", "京大戟", "大戟类", ("京大戟", "大戟"), "toxic", 3.0),
    _p("HB-HONGDAJI", "红大戟", "大戟类", ("红大戟", "红芽大戟"), "toxic", 3.0),
    _p("HB-GANSUI", "甘遂", "甘遂类", ("甘遂", "制甘遂", "醋甘遂"), "toxic", 1.5),
    _p("HB-YUANHUA", "芫花", "芫花类", ("芫花", "醋芫花"), "toxic", 3.0),
    _p("HB-LILU", "藜芦", "藜芦类", ("藜芦",), "toxic"),
    _p("HB-RENSHEN", "人参", "参类", ("人参", "生晒参", "红参")),
    _p("HB-DANGSHEN", "党参", "参类", ("党参",)),
    _p("HB-NANSHASHEN", "南沙参", "参类", ("南沙参",)),
    _p("HB-BEISHASHEN", "北沙参", "参类", ("北沙参",)),
    _p("HB-DANSHEN", "丹参", "参类", ("丹参", "紫丹参")),
    _p("HB-XUANSHEN", "玄参", "参类", ("玄参", "元参")),
    _p("HB-KUSHEN", "苦参", "参类", ("苦参",)),
    _p("HB-XIXIN", "细辛", "辛类", ("细辛", "辽细辛"), max_g=3.0),
    _p("HB-BAISHAO", "白芍", "芍类", ("白芍", "杭白芍", "白芍药")),
    _p("HB-CHISHAO", "赤芍", "芍类", ("赤芍", "赤芍药")),
)


@dataclass(frozen=True)
class HerbCatalog:
    profiles: tuple[HerbProfile, ...]

    def __post_init__(self) -> None:
        index: dict[str, HerbProfile] = {}
        for profile in self.profiles:
            for name in (profile.standard_name, *profile.aliases):
                index.setdefault(name, profile)
        object.__setattr__(self, "_index", index)

    def find(self, source_name: str) -> HerbProfile | None:
        """按处方写法归一；未收录返回 None（待人工核实，不猜测）。"""
        return self._index.get(source_name.strip())


DEFAULT_CATALOG = HerbCatalog(_PROFILES)


# ---------------------------------------------------------------- 剂量折算

_UNIT_TO_GRAMS = {"克": 1.0, "g": 1.0, "G": 1.0, "钱": 3.0, "两": 30.0, "分": 0.3}
_CN_DIGIT = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
             "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}

_ARABIC_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*(克|g|G|钱|两|分)\s*$")
_CHINESE_RE = re.compile(r"^(半)?([零一二两三四五六七八九十]*)(克|钱|两|分)(半)?$")


def _cn_number(text: str) -> int | None:
    """解析 1–99 的中文数词（十、十五、二十、二十五），非法返回 None。"""
    if not text:
        return None
    if "十" not in text:
        return _CN_DIGIT.get(text)
    left, _, right = text.partition("十")
    tens = _CN_DIGIT.get(left, 1) if left else 1
    ones = _CN_DIGIT.get(right, 0) if right else 0
    if tens is None or ones is None or tens == 0:
        return None
    return tens * 10 + ones


def parse_amount(source_amount: str | None) -> float | None:
    """把处方剂量写法折算为克；无法折算返回 None（待核实）。

    支持 "6g"、"6克"、"三钱"、"一钱半"、"半钱"、"二两"、"十五克" 等；
    无单位或无法识别的写法不猜测，返回 None。
    """
    if not source_amount:
        return None
    text = source_amount.strip()
    match = _ARABIC_RE.match(text)
    if match:
        return round(float(match.group(1)) * _UNIT_TO_GRAMS[match.group(2)], 4)
    match = _CHINESE_RE.match(text)
    if match:
        lead_half, numeral, unit, tail_half = match.groups()
        value = _cn_number(numeral) if numeral else None
        if value is None and not (lead_half or tail_half):
            return None
        grams = (value or 0) * _UNIT_TO_GRAMS[unit]
        if lead_half or tail_half:
            grams += 0.5 * _UNIT_TO_GRAMS[unit]
        return round(grams, 4)
    return None
