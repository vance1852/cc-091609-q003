# 中药处方安全复核

该项目保存处方快照、规则来源和医药沟通意见。药名规范化结果与原始处方并存，规则提示只为人工复核提供依据。

领域结构位于 `domain/contracts.py`，`fixtures/prescription_review.json` 含一张经过两次修订的脱敏处方。项目面向 Python 3.11，使用 `python -m compileall domain` 检查语法。
