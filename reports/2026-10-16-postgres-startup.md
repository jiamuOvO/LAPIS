# PostgreSQL 启动故障修复

## 已验证的情况

- 用户报告启动脚本提示程序或数据目录缺失，随后应用连接超时。
- 当前现场检查确认 pg_ctl.exe、pg_isready.exe、PG_VERSION 均存在，Windows PowerShell 5 也返回存在。原“缺失”错误未复现，不能认定文件被删除或路径已变更。
- 数据库最初未运行。启动原有实例后自动恢复，旧的 PGDATA/server.log 出现 sharing violation，日志明确记录重试 30 秒，随后恢复完成。

## 最小修复

- 启动前分别检查所需文件，错误指出具体缺失或不可访问文件，不自动初始化。
- 用 pg_isready 判断是否接受连接；恢复中等待，已运行实例不重复启动。端口监听不再被当成就绪。
- 将新启动日志改为数据目录外的 %LOCALAPPDATA%/LAPIS/postgres-server.log，保留旧日志和所有数据库数据。

## 现场验证

1. 默认应用数据库 lapis 连接成功，原有 research_tasks 记录数为 8；无其他活跃查询时正常停止原实例，应用新日志路径。
2. 修复脚本在 Windows PowerShell 5 从停止状态成功启动；pg_ctl 报 server started，pg_isready 报 accepting connections。
3. 常规 Windows PowerShell 再次执行脚本直接报告就绪，无重启；应用数据库任务数仍为 8。
4. 在 lapis_test 用已有合成任务 35446c0b-3f6f-4261-b217-aaed53e60bcb 执行 chat 的 /show、/exit，成功恢复和展示完整规约。没有创建生产任务或发送 DeepSeek 请求。

未重装数据库、未重新初始化、未删除数据、未修改凭据或权限。没有人为制造数据库崩溃来测试恢复；恢复等待来自本次现场的实际观察。
