首次带新进程恢复的替身验收完成呋喃意图、推荐、选择、修改和确认五轮后中止：子进程默认 Windows 编码输出，父进程按 UTF-8 读取，产生 UnicodeDecodeError，随后 stdout 为 None 导致 TypeError。该问题属于验收脚本，不算产品通过；原五轮记录保存在 ui-mock.jsonl 的首个运行中。修复为子进程显式 python -X utf8 后继续验收。
