# 中药处方安全复核

门诊中药房处方安全复核服务。系统围绕剂量与单位标准化、别名归一、配伍禁忌、
特殊人群、过敏史、并用药建立**版本化规则**，命中后只向药师呈现“规则为何命中、
哪些信息尚待核实”，复核结论由医师与药师通过规范会签完成，系统不替代临床判断。

## 结构

- `domain/contracts.py` — 处方快照、规则证据、会签意见及命中/合并提示等契约
- `domain/normalization.py` — 版本化别名表归一；剂量单位标准化（g/克/钱/两/分/毫克）
- `domain/rules.py` — 六类版本化规则包（各自携带版本号、生效日期、来源文献）与命中合并
- `domain/service.py` — 快照摄入重算、会签状态机、放行闸门
- `domain/queries.py` — 最终查询：来源版本、双方意见、处方演变、最小范围披露
- `fixtures/prescription_review.json` — 一张被医师修改两次的脱敏处方（v1→v3）
- `tests/test_review.py` — 单元与流程测试
- `scripts/demo.py` — 端到端演示

## 关键约束

- **快照即版本**：医师修改任何药味 → `add_snapshot` 产生新快照并重新计算全部规则；
  会签意见绑定具体快照，旧意见（含放行）对新快照一律不得沿用。
- **双向会签**：有规则命中的快照须先取得医师说明（explained）、再由药师放行（released），
  两者都落在**当前快照**上才可调配；角色不能代签（`RoleNotAllowed`），
  顺序不能颠倒（`CosignOrderError`）；被拒绝调配的快照为终态（`SnapshotTerminal`）。
- **合并展示**：紧急程度与涉及药味相同的重复命中合并为一条提示，
  `hit_count` 与全部规则证据保留，不漏依据也不制造弹窗疲劳。
- **最小范围披露**：`review_timeline` 默认按审计口径隐去患者敏感信息，
  仅 `purpose=clinical_review` 时返回并用药与过敏标记。

## 运行

```bash
python -m compileall domain          # 语法检查
python -m unittest discover -s tests # 测试（含“旧版放行无法解锁新版处方”的证明）
python scripts/demo.py               # 端到端演示 fixtures 中的三次快照与会签
```

项目面向 Python 3.11，仅使用标准库。
