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
& $py 'F:\LAPIS\lapis.py' start
& $py 'F:\LAPIS\lapis.py' turn <任务ID> '我想筛选电解液……'
& $py 'F:\LAPIS\lapis.py' turn <任务ID> '确认'
# 若上一条命令未收到结果，复用它已打印的 operation_id 重试同一输入：
& $py 'F:\LAPIS\lapis.py' turn <任务ID> '确认' --operation-id <原操作标识>
& $py 'F:\LAPIS\lapis.py' show <任务ID>
& $py 'F:\LAPIS\lapis.py' propose-design <任务ID> '研究设计.json'
& $py 'F:\LAPIS\lapis.py' approve-design <任务ID> <设计版本> --reviewer '研究人员姓名'
& $py 'F:\LAPIS\lapis.py' freeze <任务ID> <设计版本>
```

`turn` 使用现有 Instructor 进行结构化受理；设置 `LAPIS_API_KEY` 或 `DEEPSEEK_API_KEY`，可另设 `LAPIS_BASE_URL`、`LAPIS_MODEL`。LangGraph 仅编排受理中的**追问、人工确认、跨进程暂停和继续**，使用同一 PostgreSQL 实例保存 checkpoint。研究请求、审批、作业和审计以 LAPIS 表为准。命令在调用 LLM 前打印 `operation_id`；若响应丢失或进程失败，重试**同一输入**时传回原 ID。图节点重放会查操作记录，不重复写入已提交的轮次；同一 ID 用于不同输入会被拒绝，同一任务的并发输入也会被拒绝。若出现“已恢复上一轮受理”，先用上一轮 ID 查询/重试，再提交新输入。计算提交、质量判断和结论均由确定性代码及人工审核控制，不由图节点直接决定。

受理草案分别呈现研究目的、研究对象、应用场景、工作条件、目标性能、约束条件、研究范围和材料功能，并保留来源与用户原话。未知条件保持未知；系统建议经用户确认后保留原建议出处。`ready_for_design` 只表示整份 v2 研究请求经用户确认，可进入研究设计；数据库任务状态仍为 `request_confirmed`。旧版请求可读取，进入新版设计前须重新确认。设计草案可不完整，审批时必须符合 [执行计划契约](lapis_core.py)：候选、实验、条件与单位、方法和模型版本、参数、质量规则、预期原始文件与资源上限。冻结是幂等的，不启动真实计算。结构校验不能代替对模型适用性和质量阈值的科研审核。

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

本地 Git 基线提交为 `dd657fb`；尚未配置或推送远端。源码和设计材料受版本控制，`.gitignore` 排除本地凭据、数据库程序与数据、Conda 环境、备份、模拟产物和 `references/`。复现基础检查：

```powershell
git status --short
& 'F:\LAPIS\.conda-lapis\python.exe' 'F:\LAPIS\lapis.py' migrate
$env:LAPIS_TEST_PG='1'
$env:LAPIS_DB_NAME='lapis_test'
& 'F:\LAPIS\.conda-lapis\python.exe' -X utf8 -m unittest discover -s 'F:\LAPIS' -p 'test_*.py'
```
