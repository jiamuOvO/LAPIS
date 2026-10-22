# LAPIS 第二模块实施与验收记录

日期：2026-10-22（Asia/Shanghai）
基线：`6d395742f21a0aafe6ac39e2f3761a2f67a24d07`
范围：研究设计可读审阅、版本化修订、来源绑定执行计划、明确审核和冻结的软件闭环。

## 结果

M0→M4 软件交付已完成。未新增依赖、数据库迁移、网页、队列或新编排框架；第一模块保持现有行为，第三模块仍只提供明确标注为模拟的软件测试适配器。

最终隔离库全量测试 **163 项通过、0 失败、0 错误、0 跳过**，耗时 32.831 秒。完整日志见 [最终测试日志](2026-10-22-module2-final-release-tests.log)。M0 基线在原提交上为 **153 项通过、0 跳过**，见 [基线日志](2026-10-22-module2-baseline-tests.log)。第二模块场景判定见 [场景 JSON](2026-10-22-module2-scenarios.json)，源码 SHA-256 见 [源码指纹](2026-10-22-module2-source.json)。

## 实现

- `show-design` 默认显示研究问题、范式、候选、条件、观测指标、方法建议、质量检查、限制、来源字段、哈希、未定项和科研审核边界；`--debug` 保留原始 JSON。研究假设和未定项获得稳定 ID。
- `check-design` 不调用模型，检查当前确认请求、设计哈希、父来源、缺口、范式引用及执行计划契约，并分别报告软件完整性和仍须由研究人员判断的科研适用性。
- `export-design` 生成绑定父版本、父 SHA-256 和请求版本的编辑文件。`preview-revision` 只展示差异；`revise-design` 才保存新版本。修订保留旧版本，拒绝过期父版本、同一操作 ID 的不同内容和伪造来源。已审核计划修改后生成新版本，旧审批不能冻结新内容。
- 可选的 `suggest-design-revision` 用已配置模型给出完整结构化建议；建议先记录在审计事件，不改写设计版本。相同输入和 operation ID 可重放取回已保存建议；只有用户执行 `revise-design` 才采用。模型建议仅用离线替身验证，未进行收费调用。
- `export-plan` 从设计版本导出 `lapis.execution-plan.v2` 模板；`propose-plan` 保存经校验的新版本，绑定当前请求哈希、来源设计版本及其哈希。候选、执行实验、机制假设引用、比较关系、筛选标准、预算和缺口处置均由确定性校验检查。
- 阻断缺口只能通过指向非空执行计划字段并提供理由/请求依据，或以具体理由和依据声明“不适用”来处置；单改状态标记不能通过。条件与单位、预算、质量规则、来源哈希、请求版本和各范式必需字段均在审批与冻结时复核。
- 审批须提供 `--reviewer` 和 `--statement`。审计事件记录声明、时间、请求版本和被审核计划哈希。CLI 的审核人名称尚未认证，不能作为高成本真实计算授权的身份凭证。
- 模拟器现在只接受 `execution_scope=software_test`。报告中的模拟产物仍标记 `origin=simulation`、`scientific_result=null`；`research` 范围尚无真实计算适配器。

## 场景验收

- **FEC 案例：**读取现有 demo 输入并保留 `origin=demo`、`execution_allowed=false`、`scientific_result=null`。组分比例、盐浓度、FEC 质量基准、完整力场、采样和质量判据保留为缺口；尝试形成执行计划时正确阻断。没有从示例数值推导科研结论。
- **软件成功路径：**无模型合成方案经补全、来源校验、显式审核、冻结后进入模拟器；哈希产物验证通过，科学结果字段仍为 null。
- **研究范式：**筛选须提供评价标准；比较须给出有效候选关系；机制验证须有来源假设、可观测预测、反驳条件和对应检验实验。
- **失败路径：**错误来源哈希、错误或未知温度单位、非法量纲、重复 ID、缺失质量规则、无效实验引用、不足资源预算、未解决缺口、过期请求、过期父版本和同一操作 ID 的不同内容均被拒绝。
- **生命周期：**修订预览不写数据库；保存后响应重放返回同一版本；旧计划审批不会沿用到修订版本；冻结旧版本被阻断；新版本必须重新审核。现有生成并发/回放回归仍通过。

## M0 环境说明

机器原应用配置引用的数据目录 `C:\Users\98439\AppData\Local\LAPIS\postgres-data` 不存在，默认沙箱对 `127.0.0.1:5433` 的访问也被拒绝。为避免触碰用户主库，在系统临时目录创建了一次性 PostgreSQL 测试集群，使用临时端口 `55432` 和独立 `lapis_test` 库。迁移测试还要求 `postgres` 管理角色，因此仅在临时集群内补建该角色。首轮临时基线因缺该测试角色有 2 项错误；补齐后 M0 的 153 项全量基线通过。以上环境失败没有算入产品测试通过数。

## 兼容性与未验收边界

- 没有新增数据库列或迁移；来源、修订和缺口保存在现有不可变 JSONB 设计版本中，建议使用现有审计事件保存。历史设计仍可读取；未带 v2 来源契约的旧设计不能新审批或冻结，需从当前请求/设计重新导出并补全。
- FEC 的配方、力场、采样、阈值和资源预算仍未获研究人员批准；软件结构校验不能验证这些科学选择。
- 真实模型修订质量评测等待单独批准的新增费用预算；本轮全程没有收费模型请求。
- 没有运行 OpenMM、MDAnalysis 或其他真实材料计算，没有真实原始产物，也没有科研发现或器件性能结论。
- 审核身份认证、真实计算许可及调度、真实模型/力场可信度仍未验收。

## 可复现入口

在当前数据库配置就绪后，运行：

```powershell
$py='F:\LAPIS\.conda-lapis\python.exe'
& $py -X utf8 .\lapis.py show-design <任务ID> <版本>
& $py -X utf8 .\lapis.py check-design <任务ID> <版本>
& $py -X utf8 .\lapis.py export-design <任务ID> <版本> .\revision.json
& $py -X utf8 .\lapis.py preview-revision <任务ID> .\revision.json
& $py -X utf8 .\lapis.py revise-design <任务ID> .\revision.json
& $py -X utf8 .\lapis.py export-plan <任务ID> <版本> .\execution-plan.json
& $py -X utf8 .\lapis.py propose-plan <任务ID> .\execution-plan.json
& $py -X utf8 .\lapis.py approve-design <任务ID> <计划版本> --reviewer '姓名' --statement '填写本次审核范围和限制'
& $py -X utf8 .\lapis.py freeze <任务ID> <计划版本>
```

测试日志命令使用 `LAPIS_TEST_PG=1`、`LAPIS_DB_NAME=lapis_test` 和本次临时端口 `55432`；临时集群不属于 LAPIS 正常运行配置。
