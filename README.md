# LAPIS：研究规约与执行门槛

当前可运行范围：**问题受理与研究规约 → 机制与研究设计 → 模拟执行记录**。真实计算适配器、结果分析和证据报告尚未接入；模拟产物不含科研数值，也不能用于肯定结论。

## 环境

使用 `F:\LAPIS\.conda-lapis`（Python 3.11）。[environment.yml](environment.yml) 和 [requirements.txt](requirements.txt) 记录依赖；本地 Instructor 源码固定在提交 `e12f8b49203b0c1f253d27c1e709d0a09b9fc5a8`。`references/` 不纳入源码仓库。

```powershell
$env:CONDA_PKGS_DIRS='F:\LAPIS\.conda-pkgs'
& 'D:\anaconda3\Library\bin\conda.bat' create --prefix 'F:\LAPIS\.conda-lapis' python=3.11 pip -y
git clone https://github.com/567-labs/instructor.git 'F:\LAPIS\references\instructor'
git -C 'F:\LAPIS\references\instructor' checkout e12f8b49203b0c1f253d27c1e709d0a09b9fc5a8
Set-Location 'F:\LAPIS'
& '.\.conda-lapis\python.exe' -m pip install -r requirements.txt
```

## 本机 PostgreSQL

采用 EDB 提供的 [PostgreSQL 17.11 Windows 二进制 ZIP](https://get.enterprisedb.com/postgresql/postgresql-17.11-3-windows-x64-binaries.zip)，解压后使 `pg_ctl.exe` 位于忽略的 `.postgres-server/pgsql/bin/`；数据目录为 `%LOCALAPPDATA%\LAPIS\postgres-data`，仅监听 `127.0.0.1:5433`。当前是用户级实例，没有安装 Windows 服务；重启后运行启动脚本。官方 [Windows 下载说明](https://www.postgresql.org/download/windows/) 提供 ZIP 路线。

首次部署（不要对已有数据目录再次运行 `bootstrap_pg.py`）：

```powershell
& 'F:\LAPIS\.conda-lapis\python.exe' 'F:\LAPIS\scripts\bootstrap_pg.py'
& 'F:\LAPIS\scripts\start_pg.ps1'
& 'F:\LAPIS\.conda-lapis\python.exe' 'F:\LAPIS\lapis.py' setup-db
```

`bootstrap_pg.py` 生成超级用户密码并保存在忽略的 `.postgres-server/superuser.json`；`setup-db` 创建 `lapis`、`lapis_test` 和应用账户，应用凭据在 `.lapis-pg.json`。两个凭据文件都不得提交或共享。后续运行 `lapis.py migrate` 应用 [`migrations/`](migrations/) 中连续编号的 SQL：每版单独事务、加 PostgreSQL advisory lock、记录统一换行后的文件 SHA-256；重复运行跳过已应用版本，修改历史迁移会报错。升级前运行 `scripts/backup_pg.py`，升级后运行 `scripts/verify_backup_pg.py`。原 MySQL 实例的数据保留作本机回退备份，不再被 LAPIS 代码读取。

## 受理与设计

```powershell
$py='F:\LAPIS\.conda-lapis\python.exe'
& $py 'F:\LAPIS\lapis.py' chat
# 退出后可用恢复命令（或 /debug 中的任务 ID）继续对话：
& $py 'F:\LAPIS\lapis.py' chat --task-id <任务ID>
& $py 'F:\LAPIS\lapis.py' start
& $py 'F:\LAPIS\lapis.py' turn <任务ID> '我想筛选电解液……'
& $py 'F:\LAPIS\lapis.py' turn <任务ID> '确认' --draft-id <上轮草稿ID>
# 若上一条命令未收到结果，复用它已打印的 operation_id 重试同一输入：
& $py 'F:\LAPIS\lapis.py' turn <任务ID> '确认' --draft-id <上轮草稿ID> --operation-id <原操作标识>
& $py 'F:\LAPIS\lapis.py' show <任务ID>
& $py 'F:\LAPIS\lapis.py' propose-design <任务ID> '研究设计.json'
& $py 'F:\LAPIS\lapis.py' approve-design <任务ID> <设计版本> --reviewer '研究人员姓名'
& $py 'F:\LAPIS\lapis.py' freeze <任务ID> <设计版本>
```

`turn` 使用现有 Instructor 进行结构化受理；设置 `LAPIS_API_KEY` 或 `DEEPSEEK_API_KEY`，可另设 `LAPIS_BASE_URL`、`LAPIS_MODEL`。LangGraph 仅编排受理中的**追问、人工确认、跨进程暂停和继续**，使用同一 PostgreSQL 实例保存 checkpoint。研究请求、审批、作业和审计以 LAPIS 表为准。单轮技术命令 `turn` 在调用 LLM 前打印 `operation_id`；`chat` 仅在失败定位时显示它；若响应丢失或进程失败，重试**同一输入**时传回原 ID。图节点重放会查操作记录，不重复写入已提交的轮次；同一 ID 用于不同输入或不同确认／推荐上下文会被拒绝，同一任务的并发输入也会被拒绝。若出现“已恢复上一轮受理”，先用上一轮 ID 查询/重试，再提交新输入。计算提交、质量判断和结论均由确定性代码及人工审核控制，不由图节点直接决定。

受理草案分别呈现研究目的、研究对象、应用场景、工作条件、目标性能、约束条件、研究范围和材料功能，并保留来源与用户原话。未知条件保持未知；系统建议经用户确认后保留原建议出处。`ready_for_design` 只表示整份 v4 研究请求经用户确认，可进入研究设计；数据库任务状态仍为 `request_confirmed`。旧版 v1/v2/v3 请求可读取；转换成草稿时指定字段标为待重新核对，进入新版设计前须重新核对并确认。修改当前请求立即清除 `active_request_version`，历史 `request_version` 仍可读取，不能用于新设计。设计草案可不完整，审批时必须符合 [执行计划契约](lapis_core.py)：候选、实验、条件与单位、方法和模型版本、参数、质量规则、预期原始文件与资源上限。冻结是幂等的，不启动真实计算。结构校验不能代替对模型适用性和质量阈值的科研审核。

当前 `approve-design --reviewer` 只记录调用者填写的字符串，**没有身份认证**。这个记录可用于软件流程测试，不能单独作为真实高成本计算的可信授权。接入真实适配器前必须建立可验证的研究人员身份、审批权限和审批记录，并在提交点再次核验。

[FEC 研究设计草案](cases/fec_design_proposal.md) 仍待审查。EC/DMC 比例、盐浓度、FEC 质量基准、力场、采样和质量判据不是已获批准的计算输入。

## 测试、模拟和备份

```powershell
$env:LAPIS_TEST_PG='1'
$env:LAPIS_DB_NAME='lapis_test'
& 'F:\LAPIS\.conda-lapis\python.exe' -X utf8 -m unittest discover -s 'F:\LAPIS' -p 'test_*.py'
Remove-Item Env:LAPIS_DB_NAME
& 'F:\LAPIS\.conda-lapis\python.exe' 'F:\LAPIS\scripts\backup_pg.py'
& 'F:\LAPIS\.conda-lapis\python.exe' 'F:\LAPIS\scripts\verify_backup_pg.py'
```

测试库 `lapis_test` 和 `artifacts-test/` 与主库隔离。`lapis.py simulate <执行请求ID>` 仅演练失败、重试、状态与文件哈希；文件标记 `origin=simulation`、`scientific_result=null`。`verify-artifacts` 可重算哈希。备份脚本使用 `pg_dump` 自定义格式，恢复演练在临时数据库比对业务及 LangGraph checkpoint 表记录数后清理临时库。恢复演练应在备份后立即运行，避免其间新增任务导致记录数变化。

真实科研验收仍需获批的模型和力场、真实输入与原始轨迹、解析版本、质量检查、可追溯观察值和人工审核结论。

## 开发基线

项目仓库为 [jiamuOvO/LAPIS](https://github.com/jiamuOvO/LAPIS)。`origin` 已连接该仓库；`main` 保存已提交基线，未完成验证的改动使用 `codex/` 开发分支，验证通过后再合并。后续开发通过 Git 提交和推送保存。源码和设计材料受版本控制，`.gitignore` 排除本地凭据、数据库程序与数据、Conda 环境、备份、模拟产物和 `references/`。复现基础检查：

```powershell
git status --short
& 'F:\LAPIS\.conda-lapis\python.exe' 'F:\LAPIS\lapis.py' migrate
$env:LAPIS_TEST_PG='1'
$env:LAPIS_DB_NAME='lapis_test'
& 'F:\LAPIS\.conda-lapis\python.exe' -X utf8 -m unittest discover -s 'F:\LAPIS' -p 'test_*.py'
```

## v4 动态受理与验收

当前使用八字段 v4 契约及 `intake-rules-4.0`。复用原数据库与迁移 005，不新增服务或 SQL 迁移。已有数据库若尚未应用迁移 005，仍需先备份再迁移。

推荐由 Instructor 调用模型动态生成，材料类别不依赖本地目录白名单。`data/research_directions.json` 只提供可选、已核查的参考上下文；目录未覆盖时仍能提出研究提案。提案展示理由、假设、限制、待澄清事项，并标记 `suggestion_origin=model`、`evidence_status=unverified`。用户采用只确认研究意图，不核验性质、文献或计算结果。模型不能自行生成来源 ID、URL 或 DOI；有参考资料也不会把整项提案升级为已验证。

提案用推荐集合版本、草稿 ID 和方向 ID 绑定选择。确认后的请求记录模型、提示词哈希、生成操作标识和提案快照哈希；完整原提案保存在对应 `intake_operations.result`，无需新事实库。修改清除有效请求指针，旧版请求保持历史可读，使用前需要重新审查。抽取或生成失败整轮回滚；重试沿用原操作 ID。推荐多一次模型调用，耗时及费用会增加。

`chat` 自动携带当前草稿和推荐上下文。单轮 `turn` 示例：

```powershell
& $py 'F:\LAPIS\lapis.py' turn <任务ID> '采用第二个，但先不考虑成本' --draft-id <草稿ID> --recommendation-id <推荐集合ID> --recommendation-version <版本>
& $py 'F:\LAPIS\lapis.py' turn <任务ID> '确认' --draft-id <最新草稿ID>
```

隔离验收：

```powershell
$env:LAPIS_TEST_PG='1'
$env:LAPIS_DB_NAME='lapis_test'
& $py -X utf8 -m unittest discover -v
& $py .\scripts\verify_intake_v4.py --mode mock --output .\reports\local-v4-mock.jsonl
# 经授权用 --mode real；会发送合成对话、提示词和公开参考上下文。
```

`scripts/verify_intake_v3.py` 及 v3 报告仅记录旧版目录方案的验收；复现旧版应检出其报告中的基线，不用于评价动态推荐。测试替身及真实模型对话均不是材料计算或科研结论的验收。

## 自然对话与规约核对

`chat` 普通轮只说明已理解／修改的重点，并追问一个最影响边界的问题；不会每轮显示八项未知占位、技术 JSON 或操作 ID。达到可审查程度时集中呈现八项自然语言规约，用户可确认或直接修改。确认时附带修改必须再核对新规约；推荐或解释视图不能替代完整核对。

- `/show`：完整当前规约及已采用提案的必要假设、限制。
- `/sources`：完整提案说明、待核查事项及参考资料支持范围。
- `/debug`：技术 JSON 与任务标识；`/exit` 显示恢复命令。
- `lapis.py show <任务ID>` 为人类可读视图；加 `--debug` 查看数据库技术记录。`turn` 保持显式的单轮技术 JSON 接口。

已经采用的硬约束继续保留，除非用户明确撤回或替换；改变对象／用途时重新核对。完全相同的被拒方向按名称与字段内容指纹过滤，不因换 UUID 再出现；用户明确重新考虑时才重新提供。该规则不保证识别语义相近的改写。

新选择把必要审核说明快照保存在现有 JSONB 请求摘要中，完整原提案仍由不可变操作引用追溯；旧摘要从原操作加载并核验哈希，无新数据库表。科学核验状态保持未核验。短展示可截断提案说明，完整文字在 `/sources`，八项规约与硬约束不截断。

本轮逐轮引导验收脚本：

```powershell
$env:LAPIS_TEST_PG='1'
$env:LAPIS_DB_NAME='lapis_test'
& $py .\scripts\verify_intake_ui.py --mode mock --output .\reports\local-ui-mock.jsonl
# 真实模型模式沿用合成评测授权；必须三个场景均确认，不能只验证阻断。
```
