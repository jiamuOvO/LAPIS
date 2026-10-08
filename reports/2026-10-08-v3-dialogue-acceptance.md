# 第一模块对话验收记录

最终实现提交：`ab9b372b1511bfdba921af41d40331704a21124e`。合同 v3；规则 intake-rules-3.1；目录 directions-1.0。

真实模型：DeepSeek `deepseek-flash`，别名未锁定供应商内部模型版本。公开来源和提示词发送已获用户明确授权；任务均新建于 lapis_test。
本记录验证研究规约的软件对话，不验证真实计算、数值收敛、机制或器件性能。

完整输入、结构化提取、程序输出、CLI 显示文本、来源、ID、版本、代码指纹、HTTP 调用与 token 用量均保存在下列 JSONL；此前失败记录没有删除。

- [真实模型完整记录](2026-10-08-v3-dialogues-real.jsonl)
- [mock 完整记录](2026-10-08-v3-dialogues-mock.jsonl)
- [验收汇总](2026-10-08-v3-acceptance-summary.json)

## 最终真实模型 14 轮

| 场景／轮次 | 真实输入 | 状态 | 有效版本 | 耗时 s | HTTP 次数／重试 | 判断 |
|---|---|---|---|---:|---:|---|
| furan/original-question | 我想知道呋喃基 分子有什么特性 | needs_clarification | None | 5.356 | 1/0 | 通过 |
| furan/help-before-use | 我不太了解，你有什么推荐吗 | needs_guidance | None | 1.81 | 1/0 | 通过 |
| furan/select-plus-remove | 采用第二个方向，但先不考虑成本 | needs_confirmation | None | 2.874 | 1/0 | 通过 |
| furan/confirm-current | 确认 | ready_for_design | 1 | 0.462 | 0/0 | 通过 |
| furan/repeat-confirm | 确认 | ready_for_design | 1 | 0.354 | 0/0 | 通过 |
| furan/recommend-plus-new-object | 把研究对象改成铝合金，我不了解，有什么推荐？ | needs_clarification | None | 2.324 | 1/0 | 通过 |
| edits/complete-request | 我想在高电压电池中比较碳酸酯电解液，只研究电解液，功能是传导锂离子；比较氧化稳定性和离子传输，成本必须低于预算，不用含氟添加剂 | needs_confirmation | None | 3.592 | 1/0 | 通过 |
| edits/confirm-initial | 确认 | ready_for_design | 1 | 0.545 | 0/0 | 通过 |
| edits/goal-removal-paraphrase | 氧化稳定性这轮先放下，只保留离子传输这个目标 | needs_confirmation | None | 3.168 | 2/1 | 通过 |
| edits/confirm-plus-preference | 确认，但成本不再是硬要求，只是偏好 | needs_confirmation | None | 1.982 | 1/0 | 通过 |
| edits/confirm-revised | 确认 | ready_for_design | 2 | 0.436 | 0/0 | 通过 |
| boundaries/two-conflicts | 比较电池碳酸酯电解液，仅研究电解液，作用是传导离子，比较扩散能力；温度是25 K或25℃，必须使用含氟添加剂，同时不得使用含氟添加剂 | needs_clarification | None | 3.727 | 1/0 | 通过 |
| boundaries/confirm-cannot-resolve | 确认 | needs_clarification | None | 0.388 | 0/0 | 通过 |
| boundaries/drug-purpose | 我想筛选小分子药物先导，应用是靶蛋白结合，比较亲和力，只看呋喃类候选 | unsupported | None | 1.937 | 1/0 | 通过 |

## 失败与修复记录

1. 首次真实推荐选择时，模型把目录选项展开为用户字段，丢失建议来源并产生无效条目更新；该轮及随后确认未通过。修复为确定性过滤选择命令的重复展开，保留目录快照与选择记录。
2. 首次重复确认的验收器因未确认结果缺少 already_confirmed 字段抛 KeyError；程序返回的是仍需澄清，未写入有效确认。验收器改用显式缺省判断，不把字段缺失视为成功。
3. 真实约束修改只返回强度和条目 ID，没有 value。旧合并清空了原描述，随后确认被拦。修复为局部更新保留已有值与 ID。
4. 最终真实模型 14 轮均通过；10 次 HTTP 响应，1 次 Instructor 重试，32912 tokens，总耗时 28.955 秒。费用没有估算；这不是固定延迟或通过率保证。
5. 最终 mock 14 轮均通过，无网络调用，总耗时 5.351 秒。mock 数据不是科研发现。

## 来源核查与适用范围

- [聚酮/呋喃/马来酰亚胺论文](https://pmc.ncbi.nlm.nih.gov/articles/PMC8069175/)：核查全文相关摘要与讨论，只支持特定聚合物网络的研究方向及副反应限制，不能推广到所有呋喃基分子。
- [非晶 PEF 的 CO2 传输论文](https://pubs.acs.org/doi/10.1021/acs.macromol.5b00333)：本轮直接打开失败后，通过出版社索引页面核对摘要；未获得全文，不指定用户体系的参数或性能。
目录的匹配是关键词匹配，新增方向需要人工核查来源；未覆盖的铝合金请求明确返回资料缺口，没有编造方向。
