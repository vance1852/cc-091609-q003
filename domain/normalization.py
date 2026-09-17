"""药名别名归一与剂量单位标准化。

别名表与单位换算表本身就是版本化规则（见 domain.rules 中的规则包元数据），
归一结果与原始处方文本并存，任何无法归一的信息都标记为“待核实”，
交由药师人工确认，不做静默假设。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Mapping

# 别名 -> 标准编码。未收录的药名归一为 None，由规则层产生“待核实”提示。
ALIASES: dict[str, str] = {
    # 乌头类
    "川乌": "CHUANWU",
    "制川乌": "CHUANWU",
    "川乌头": "CHUANWU",
    "草乌": "CAOWU",
    "制草乌": "CAOWU",
    "附子": "FUZI",
    "炮附子": "FUZI",
    "黑顺片": "FUZI",
    "白附片": "FUZI",
    # 半夏及其炮制品
    "半夏": "BANXIA",
    "生半夏": "BANXIA",
    "姜半夏": "BANXIA",
    "法半夏": "BANXIA",
    "清半夏": "BANXIA",
    "半夏曲": "BANXIA",
    # 瓜蒌 / 天花粉 / 贝母 / 白蔹 / 白及
    "瓜蒌": "GUALOU",
    "全瓜蒌": "GUALOU",
    "瓜蒌皮": "GUALOU",
    "瓜蒌仁": "GUALOU",
    "天花粉": "TIANHUAFEN",
    "贝母": "BEIMU",
    "川贝母": "BEIMU",
    "浙贝母": "BEIMU",
    "伊贝母": "BEIMU",
    "平贝母": "BEIMU",
    "白蔹": "BAILIAN",
    "白及": "BAIJI",
    # 甘草及其反药
    "甘草": "GANCAO",
    "生甘草": "GANCAO",
    "炙甘草": "GANCAO",
    "海藻": "HAIZAO",
    "京大戟": "JINGDAJI",
    "大戟": "JINGDAJI",
    "甘遂": "GANSUI",
    "芫花": "YUANHUA",
    "醋芫花": "YUANHUA",
    # 藜芦及其反药
    "藜芦": "LILU",
    "人参": "RENSHEN",
    "党参": "DANGSHEN",
    "丹参": "DANSHEN",
    "玄参": "XUANSHEN",
    "南沙参": "NANSHASHEN",
    "北沙参": "BEISHASHEN",
    "苦参": "KUSHEN",
    "细辛": "XIXIN",
    "白芍": "SHAOYAO",
    "赤芍": "SHAOYAO",
    "芍药": "SHAOYAO",
    # 常见并用药（西药）
    "华法林": "WARFARIN",
    "warfarin": "WARFARIN",
}

# 现代 metric 换算：1 两 = 30 g，1 钱 = 3 g，1 分 = 0.3 g。
_UNIT_TO_GRAMS: dict[str, float] = {
    "g": 1.0,
    "克": 1.0,
    "钱": 3.0,
    "两": 30.0,
    "分": 0.3,
    "mg": 0.001,
    "毫克": 0.001,
}

_AMOUNT_RE = re.compile(r"^(\d+(?:\.\d+)?)\s*(毫克|克|钱|两|分|mg|g)?$", re.IGNORECASE)
# 允许处方行写成 “制川乌6g” / “姜半夏 9克” 这样的单行文本。
_INLINE_AMOUNT_RE = re.compile(r"^(?P<name>[^\d\s]+?)\s*(?P<amount>\d[\d.\s]*(?:毫克|克|钱|两|分|mg|g)?)?$", re.IGNORECASE)


@dataclass(frozen=True)
class NormalizedHerb:
    source_name: str
    normalized_code: str | None
    source_amount: str
    amount_grams: float | None
    issues: tuple[str, ...] = ()


def parse_amount(text: str) -> tuple[float | None, str | None]:
    """把剂量文本标准化为克。返回 (克数, 待核实问题)。"""
    cleaned = text.strip()
    if not cleaned:
        return None, None
    match = _AMOUNT_RE.match(cleaned)
    if not match:
        return None, f"剂量“{text}”无法标准化，待核实"
    value = float(match.group(1))
    unit = match.group(2) or "克"
    factor = _UNIT_TO_GRAMS[unit.lower() if unit.isascii() else unit]
    grams = value * factor
    if grams <= 0:
        return None, f"剂量“{text}”非正数，待核实"
    return grams, None


class HerbNormalizer:
    """基于版本化别名表的归一器。"""

    def __init__(self, aliases: Mapping[str, str] | None = None) -> None:
        self._aliases = dict(ALIASES if aliases is None else aliases)

    @property
    def alias_count(self) -> int:
        return len(self._aliases)

    def normalize_name(self, name: str) -> str | None:
        return self._aliases.get(name.strip())

    def normalize(self, entry: str | Mapping[str, str]) -> NormalizedHerb:
        """接受 “姜半夏” / “姜半夏9g” / {"name": ..., "amount": ...} 三种写法。"""
        if isinstance(entry, Mapping):
            name = str(entry.get("name", "")).strip()
            amount_text = str(entry.get("amount", "")).strip()
        else:
            match = _INLINE_AMOUNT_RE.match(entry.strip())
            if match:
                name = match.group("name").strip()
                amount_text = (match.group("amount") or "").strip()
            else:  # 兜底：整串当药名，交规则层标记待核实
                name, amount_text = entry.strip(), ""

        issues: list[str] = []
        code = self.normalize_name(name) if name else None
        if not name:
            issues.append("药名为空，待核实")
        elif code is None:
            issues.append(f"药名“{name}”未收录于别名表，待核实")

        grams, amount_issue = parse_amount(amount_text)
        if amount_issue:
            issues.append(amount_issue)

        return NormalizedHerb(
            source_name=name or entry if isinstance(entry, str) else name,
            normalized_code=code,
            source_amount=amount_text,
            amount_grams=grams,
            issues=tuple(issues),
        )
