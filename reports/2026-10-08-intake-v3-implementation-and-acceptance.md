# LAPIS 第一模块实施与验收报告

日期：2026-10-08。工作区：F:\LAPIS。
分支：codex/intake-v3-development。固定实现提交：ab9b372。

## 1. 交付结论

第一模块已实现：不完整输入 → 来源明确的有限推荐 → 显式选择 → 局部修改 → 完整草稿审查 → 绑定版本确认 → 有效研究请求。修改撤销进入新研究设计的资格，历史请求保留；提出设计、审批与冻结均再次检查有效版本。

- 软件回归：51 通过，0 失败，0 错误，0 跳过。
- 固定实现上的真实模型对话：14 轮通过，0 失败/错误。
- 同提交 mock 对照：14 轮通过，无外部 API 调用。
- 真实模型最终运行：10 次 HTTP 响应，1 次重试，32912 tokens，总耗时 28.955 秒。
- 本轮验收的是受理软件，不是真实材料计算、机制或器件性能。
- 正式库未应用 migration 005，不能把本报告当作正式部署完成证明。

**已实现且验证**：下文矩阵中的受理行为、隔离迁移、历史保留、CLI 恢复、故障重试、并发拒绝和有效版本门槛。
**未验证**：正式库升级/完整备份恢复、新增实际非呋喃目录方向、供应商更换后的行为、广泛自然语言输入的通过率。
**未实现**：真实计算适配器、轨迹分析、科研质量判定、证据报告发布、可信审批身份认证和统一生产成本计量。

## 2. 基线与依据

- 既有基线：8c9b63a。
- 未验收上传快照：34cea0c；快照说明由本报告补充，不代表最终状态。
- 固定实现：ab9b372；完整 ID 见验收汇总 JSON。
- 最终真实与 mock 记录保存实际提交、六个代码文件的规范换行 SHA-256、提示词 SHA-256、目录 SHA-256及运行标识；最终代码指纹已逐项核对。
- 只同步开发分支，不自动合并 main。

**材料已明确**：V2 的五段流程、材料实际用途、研究意图与研究请求的区分、设计阶段独立形成可执行输入、结论不越过证据。原 JSON 示例不是实算证明。

**用户已批准范围**：第一模块的可扩展推荐、确定性冲突规则、混合动作、历史保留、设计资格门槛及持久验收材料。用户另明确允许向已配置 DeepSeek 发送新建合成测试对话、公开目录和提示词。

**实现选择／有限推断**：沿用现有 Conda、Instructor、LangGraph、PostgreSQL；本轮没有另建环境、安装依赖或更换数据库。推荐采用本地版本目录和关键词匹配，未添加 RAG、向量库、多 Agent 或消息队列。有限规则不能证明所有化学同义词、逻辑关系和科研适用性。

**待科研审查**：FEC 配方、力场、模型、采样及质量阈值；任何新目录项的科学依据。未用示例填补空缺。

## 3. 要求矩阵与代码位置

行号对应 ab9b372。代码和测试的完整差异由该 Git 提交保留。

| 要求 | 实现位置 | 验收结果 |
|---|---|---|
| 八字段、unknown/none/open/unclear 区分 | [lapis_intake.py](../lapis_intake.py#L142)、[lapis_contract.py](../lapis_contract.py#L68) | 初始未知、明确无预设、交设计确定分别保存；通过 |
| 原始意图、来源、条目 ID 和修订 | [lapis_intake.py](../lapis_intake.py#L454)、[lapis_store.py](../lapis_store.py#L158) | 原始意图不改、列表 ID 稳定、历史请求保留；通过 |
| 缺必填字段也可解释与推荐 | [lapis_intake.py](../lapis_intake.py#L463)、[lapis_guidance.py](../lapis_guidance.py#L23) | 实际呋喃问题 → 求推荐，不再机械重复应用问题；通过 |
| 推荐有范围/限制且可扩展 | [data/research_directions.json](../data/research_directions.json)、[lapis_guidance.py](../lapis_guidance.py#L12) | 两条已核查目录；独立 mock 目录增加铝合金项无需改代码，明确非科研来源。真实目录无覆盖则诚实返回缺口 |
| 选择绑定当前推荐与草稿 | [lapis_intake.py](../lapis_intake.py#L388) | 过期选择被拒、保留集合/版本/方向/选择原话/来源快照；通过 |
| 采用方向＋局部修改 | [lapis_intake.py](../lapis_intake.py#L362)、[lapis_intake.py](../lapis_intake.py#L244) | 实际“采用第二个方向，但先不考虑成本”；重复展开不抹来源，两动作保留；通过 |
| 局部目标和约束编辑 | [lapis_intake.py](../lapis_intake.py#L244) | 精确 ID/名称、不唯一时澄清、保留其他项、方向和强度更新、不清空未提供的原值；通过 |
| 暂不确认、拒绝、撤回区别 | [lapis_intake.py](../lapis_intake.py#L432)、[lapis_intake.py](../lapis_intake.py#L538) | 暂停保留草稿、拒绝记忆不重复、建议撤回不抹独立用户目标；受控测试通过 |
| 推荐＋新对象 | [lapis_intake.py](../lapis_intake.py#L538)、[lapis_guidance.py](../lapis_guidance.py#L23) | 实际改成铝合金：保存新对象、撤销旧资格、不沿用无关呋喃推荐；通过 |
| 确认＋修改 | [lapis_intake.py](../lapis_intake.py#L538) | 实际硬约束改偏好：显示草稿，下一轮才确认；通过 |
| 否定确认不能变授权 | [lapis_intake.py](../lapis_intake.py#L511) | 故意误提取 confirm，“我不准备确认”仍不批准；受控测试通过 |
| 温度/单位与组分矛盾独立阻断 | [lapis_contract.py](../lapis_contract.py#L22)、[lapis_contract.py](../lapis_contract.py#L68) | 实际 25 K或25℃＋必须/不得用同组分：两项独立检查，“确认”不消解；通过 |
| 领域边界与未知机制 | [lapis_contract.py](../lapis_contract.py#L54)、[lapis_intake.py](../lapis_intake.py#L79) | 实际药物先导拒绝；医用材料不因单个“药”字拒绝；机制探索不强制预知假设 |
| 来源快照校验 | [lapis_contract.py](../lapis_contract.py#L130) | 来源 claim 被改但哈希未变时拒绝；通过 |
| 有效版本和旧设计门槛 | [lapis_store.py](../lapis_store.py#L227) | 修改清 active_request_version，旧提出/审批/冻结不能越过；通过 |
| CLI 展示、重试和退出恢复 | [lapis.py](../lapis.py#L76)、[lapis.py](../lapis.py#L110) | 实际图/隔离数据库＋mock 提取：推荐→选择→退出→另一 CLI 调用确认；显示来源限制、上下文保留；通过 |
| 单轮 CLI 显式上下文 | [lapis.py](../lapis.py#L156) | --draft-id、--recommendation-id、--recommendation-version；chat 自动携带，turn 显式带回 |
| 幂等、故障和并发 | [lapis_graph.py](../lapis_graph.py#L71)、[lapis_store.py](../lapis_store.py#L158) | 写前失败、提交后检查点前失败、同 ID 重放、上下文冲突、同任务并发；通过 |
| v1/v2 保留并重审 | [lapis_intake.py](../lapis_intake.py#L151)、[migration 005](../migrations/005_intake_validity.sql) | 升级前后两份旧 payload 相等，指针 NULL；转换草稿需重核；通过 |
| 审计与持久对话证据 | [lapis_store.py](../lapis_store.py#L409)、[验收脚本](../scripts/verify_intake_v3.py) | 操作 ID、模型/提示词/规则/来源版本、耗时、错误；脚本有实际 HTTP 次数和 token |

## 4. 契约、状态和科学边界

合同 v3；规则 intake-rules-3.1；目录 directions-1.0。草稿修订 draft_revision/draft_id 与数据库对话 revision 分离。目标与约束有唯一 ID，方向和强度独立存储；方法、机制、参数参考保持未核验，撤回有记录，不是执行许可。

建议展示前不写用户字段；采用后保存 confirmed_suggestion、选择原话、集合 ID/版本、方向 ID、来源引用及核查范围/限制/哈希。完整请求需要当前草稿确认上下文；重复确认复用版本，混合修改等待再次确认。

状态：needs_clarification / needs_guidance / needs_confirmation / ready_for_design / unsupported；暂停保持草稿。数据库确认状态叫 request_confirmed。历史 MAX(request_version) 只是读取信息；新设计还要求 active_request_version、当前状态、当前草稿和契约都有效。

领域、歧义、温度、组分矛盾与来源校验各自判断。确定性规则不由 LLM 的“已完成”替代。第一模块就绪不等于可计算；更不等于作业结束、收敛、物理可靠、机制支持或器件验证。

对象/用途/范围变化时对旧条件、性能、功能和约束进行依赖审查；这不是完整科学相容性检查。实际模型与力场仍需要研究设计审核。

## 5. 迁移兼容与正式部署状态

005 增加 research_tasks.active_request_version，外键指向同任务请求；intake_operations 增加 input_context JSONB。历史指针默认 NULL，历史上下文默认空对象。不改 001—004，不重写科研请求。

隔离迁移验证：重跑幂等、历史校验和兼容、失败回滚、旧 payload 不变。迁移测试新建 lapis_migration_test_* 临时库并清理。

正式库只读核对：迁移 001—004；3 个任务；0 请求版本、0 设计版本、0 执行请求。隔离库为 001—005。正式库没有在本轮升级。

切换前先备份与恢复核验，再确认目标库迁移；以下正式部署动作本轮未执行：

    Set-Location F:\LAPIS
    Remove-Item Env:LAPIS_DB_NAME -ErrorAction SilentlyContinue
    Remove-Item Env:LAPIS_TEST_PG -ErrorAction SilentlyContinue
    & .\.conda-lapis\python.exe .\scripts\backup_pg.py
    & .\.conda-lapis\python.exe .\scripts\verify_backup_pg.py
    & .\.conda-lapis\python.exe .\lapis.py migrate
    & .\.conda-lapis\python.exe -X utf8 .\lapis.py chat

未升级时不要直接启动新版正式对话。本轮没有用自动迁移掩盖环境差异。

## 6. 实际验收命令、次数和失败

完整软件命令：

    $env:LAPIS_TEST_PG='1'
    $env:LAPIS_DB_NAME='lapis_test'
    & .\.conda-lapis\python.exe -X utf8 -m unittest discover -s . -p 'test_*.py' -v

| 运行 | 结果 | 原始材料 |
|---|---|---|
| 首轮非提升权限局部测试 | 30 个：28 通过、1 临时目录权限错误、1 跳过 | WinError 5；随后完整隔离库回归覆盖该项，不靠跳过验收 |
| 初始完整隔离回归 | 41 通过，0 失败/错误/跳过 | [日志](2026-10-08-v3-regression-initial.log) |
| 扩展完整隔离回归 | 47 通过，0 失败/错误/跳过 | [日志](2026-10-08-v3-regression-expanded.log) |
| 最终完整隔离回归 | 51 通过，0 失败/错误/跳过；13.319 秒 | [日志](2026-10-08-v3-regression-final.log) |

有效旧覆盖保留：八字段、未知机制、多目标/约束、来源、撤回、分类修改、历史兼容、工具参考和故障恢复。旧自由建议测试改为来源目录测试，不是仅删掉失败测试；版本期待改为 v3，失败迁移探针改为 006，避免与新 005 撞号。

对话命令：

    $env:LAPIS_TEST_PG='1'
    $env:LAPIS_DB_NAME='lapis_test'
    foreach ($case in @('furan','edits','boundaries')) {
        & .\.conda-lapis\python.exe -X utf8 .\scripts\verify_intake_v3.py --case $case --mode mock --output .\reports\local-mock.jsonl
    }
    # 获得用户授权后，用 --mode real 验证 DeepSeek。

脚本拒绝非 lapis_test 环境，新建测试任务，不读正式研究历史。真实模式发送范围明确获准后执行。最终三组共 14 轮：

- [便于审查的逐轮对话表](2026-10-08-v3-dialogue-acceptance.md)
- [真实输入输出与此前失败](2026-10-08-v3-dialogues-real.jsonl)
- [明确标为 mock 的对照](2026-10-08-v3-dialogues-mock.jsonl)
- [版本、指纹、耗时和用量统计](2026-10-08-v3-acceptance-summary.json)

所有真实尝试合计 40 通过、4 行为验收失败、1 验收器错误；最终固定提交的 14 轮全通过。没有删除此前失败或把它们混入最终通过数。

## 7. 真实模型发现的缺陷及修复

1. 选择时模型把目录选项重复展开为用户原创字段，目标指向旧占位 ID。确定性层过滤重复展开和纯选择命令，保留建议来源；对应受控测试及真实重跑通过。
2. “先不考虑成本”被提取为偏好。依据明确撤回原话处理，唯一匹配才能按 ID 撤回，不唯一保持澄清。
3. 仅修改约束强度时 value=null 清空描述。局部更新现在保留未重述的值和 ID，实际重跑通过。
4. 重复确认验收器访问不存在的 already_confirmed 抛 KeyError。改为显式缺省判断；原错误保留。程序那轮返回需澄清，没有成功确认，不是 API 错误。
5. 否定确认、过期上下文和“确认＋无有效变化”另有受控保护测试。不把提取结果直接当最终授权。

## 8. 环境、来源、维护与限制

既有 Conda Python 3.11.16；Instructor 1.17.1；LangGraph 1.2.12；
langgraph-checkpoint-postgres 3.1.2；psycopg 3.3.6；Pydantic 2.13.5；
OpenAI SDK 3.3.0；HTTPX 0.28.1。没有新增服务，契约和目录使用标准库/既有 Pydantic。

- DeepSeek deepseek-flash 是供应商别名，未锁定内部权重。模型/提示词变更需重跑固定对话，单次验收不是长期通过率保证。
- 来源一核对 [PMC 原文](https://pmc.ncbi.nlm.nih.gov/articles/PMC8069175/)；来源二直接打开失败后核对 [ACS 出版社摘要](https://pubs.acs.org/doi/10.1021/acs.macromol.5b00333)，未取得全文。仅支持特定对象的研究方向，不给用户材料性能或计算参数。
- 当前实际目录只有两条呋喃材料方向。代码扩展验证使用独立 mock 来源；新增真实非呋喃目录仍待人工来源审查。
- 同任务数据库会话锁适用当前单机团队；本轮不能证明多机高并发吞吐。
- 普通审计保存操作尝试、错误类型和耗时；SDK 内部重试/用量未统一暴露。验收 HTTP hooks 记录实际响应与 tokens；没有编造费用。
- approve-design --reviewer 仍是调用者填写字符串，未做可信身份认证。不能单独作为真实高成本执行授权。
- 正式库迁移、完整生产备份恢复、正式交互部署未验证。
- 原示例、软件模拟产物、mock 提取与真实模型对话都不是真实材料计算。结果分析和证据报告没有新增实现。
- 本轮未增加技能运行服务、RAG、向量库、多 Agent、消息队列；出现实际需求再评审。

## 9. 后续审查与决策

请审计混合输入、来源保留、过期资格撤销和部署前置条件；完整失败与重跑记录可直接读取。
新研究方向需要可核查资料与人工审查；FEC 计算方案仍待独立科研审批。
下一步再接真实适配器和科学质量规则，不用受理流程通过替代科研验收。
