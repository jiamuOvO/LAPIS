"""Local LAPIS CLI. No command dispatches a scientific calculation."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import secrets
from uuid import uuid4

import instructor
import psycopg
from psycopg import sql
from openai import OpenAI

from lapis_core import digest
from lapis_intake import LABELS, PROPOSAL_PROMPT, SYSTEM_PROMPT, preview_intake
from lapis_graph import run_intake_turn
from lapis_store import (ROOT, approve_design, artifact_root, create_simulation_attempt, create_task,
                         db_config, freeze_execution, get_task, get_intake_operation, initialize_schema,
                         propose_design, register_artifact, transition_attempt,
                         verify_artifacts)


def setup_database():
    config = db_config()
    local = ROOT / ".lapis-pg.json"
    root_secret_file = ROOT / ".postgres-server" / "superuser.json"
    if local.exists():
        raise ValueError("本地数据库配置已存在；为避免覆盖凭据，未重复创建账户")
    password = secrets.token_urlsafe(32)
    admin_secret = json.loads(root_secret_file.read_text(encoding="utf-8"))["password"]
    with psycopg.connect(
        host=config["host"], port=config["port"], user="postgres",
        password=admin_secret, dbname="postgres", autocommit=True,
    ) as db:
        with db.cursor() as cursor:
            cursor.execute("SELECT 1 FROM pg_roles WHERE rolname='lapis'")
            if cursor.fetchone():
                cursor.execute(sql.SQL("ALTER ROLE lapis PASSWORD {}").format(sql.Literal(password)))
            else:
                cursor.execute(sql.SQL("CREATE ROLE lapis LOGIN PASSWORD {}").format(sql.Literal(password)))
            for name in ("lapis", "lapis_test"):
                cursor.execute("SELECT 1 FROM pg_database WHERE datname=%s", (name,))
                if not cursor.fetchone():
                    cursor.execute(sql.SQL("CREATE DATABASE {} OWNER lapis ENCODING 'UTF8'").format(sql.Identifier(name)))
    local.write_text(json.dumps({"host": config["host"], "port": config["port"],
                                 "user": "lapis", "password": password, "dbname": "lapis"}, indent=2), encoding="utf-8")
    initialize_schema()
    print("PostgreSQL 数据库和本机应用账户已创建；凭据保存在忽略的 .lapis-pg.json")


def run_simulation(execution_id: str, actor: str, fail: bool) -> str:
    attempt_id = create_simulation_attempt(execution_id, actor)
    transition_attempt(attempt_id, "running", actor)
    try:
        if fail:
            raise RuntimeError("主动注入的模拟作业失败")
        directory = artifact_root() / execution_id / attempt_id
        directory.mkdir(parents=True, exist_ok=False)
        path = directory / "simulation.json"
        with path.open("x", encoding="utf-8") as output:
            json.dump({"origin": "simulation", "scientific_result": None,
                       "message": "仅验证软件状态与产物链"}, output, ensure_ascii=False)
            output.flush()
            os.fsync(output.fileno())
        register_artifact(attempt_id, "simulation_log", path, "simulation", actor)
        transition_attempt(attempt_id, "succeeded", actor)
    except Exception as error:
        transition_attempt(attempt_id, "failed", actor, str(error))
        raise
    return attempt_id


def field_text(field, value):
    entries = value if isinstance(value, list) else [value or {}]
    result = []
    for entry in entries:
        status = entry.get("status", "specified" if entry.get("value") else "unknown")
        text = entry.get("value") or ""
        if status == "unknown":
            text = "尚未指定，研究设计阶段确定" if field in {"work_conditions", "constraints"} else "尚未明确"
        elif status == "none":
            text = "本轮无预设" + ("约束" if field == "constraints" else "条件")
        elif status == "open":
            text = (text + "；" if text else "") + ("留待研究设计阶段确定" if field in {"work_conditions", "constraints", "target_performance"} else "待您明确")
        elif status == "unclear":
            text = (text or "已表达但不明确") + "（待澄清）"
        if status == "specified" and field == "constraints":
            text = ("硬约束：" if entry.get("strength") == "hard" else "偏好：") + text
        if status == "specified" and field == "target_performance":
            text += "（" + (entry.get("direction") or "方向待澄清") + "）"
        if entry.get("needs_review"):
            text += "（场景变化后需重新核对）"
        result.append(text)
    return "；".join(result)


def hydrate_review(result):
    """Old summaries resolve their immutable operation; new requests carry small review snapshots."""
    from copy import deepcopy
    result = deepcopy(result)
    request = (result or {}).get("request") or {}
    for proposal in request.get("proposals", {}).values():
        operation = proposal.get("generation", {}).get("operation_id")
        if proposal.get("review") or not operation:
            continue
        stored = get_intake_operation(operation)
        rec = ((stored or {}).get("result") or {}).get("recommendations") or {}
        if not rec or digest(rec) != proposal.get("content_sha256"):
            raise ValueError("旧提案快照缺失或校验失败，请先恢复原始操作记录")
        proposal["review"] = {o["id"]: {key: o.get(key, []) for key in
            ("label", "reason", "assumptions", "limitations", "clarifications", "source_refs")}
            for o in rec.get("options", [])}
    return result


def short_text(value, limit=100):
    text = str(value).replace("\n", " ")
    return text if len(text) <= limit else text[:limit] + "…"


def render_sources(result, detail=True):
    detail_view = detail
    request = (result or {}).get("request") or {}
    active = {(e.get("recommendation_ref") or {}).get("direction_id") for value in request.get("fields", {}).values()
              for e in (value if isinstance(value, list) else [value])}
    shown = False
    if detail:
        for option in ((result or {}).get("recommendations") or {}).get("options", []):
            print("当前提案：" + option["label"])
            for key, label in (("reason", "理由"), ("assumptions", "假设"), ("limitations", "限制"), ("clarifications", "待澄清")):
                value = option.get(key)
                if value:
                    print(label + "：" + (value if isinstance(value, str) else "；".join(value)))
            shown = True
    for proposal in request.get("proposals", {}).values():
        for direction, detail in proposal.get("review", {}).items():
            if direction not in active:
                continue
            shown = True
            print("已采用提案：" + str(detail.get("label", "")))
            for key, label in (("assumptions", "假设"), ("limitations", "限制"), ("clarifications", "待研究设计核查")):
                if detail.get(key):
                    print(label + "：" + ("；".join(detail[key]) if detail_view else "；".join(short_text(x, 85) for x in detail[key][:2])))
    for note in request.get("reference_notes", []):
        if note.get("active", True) and note.get("value"):
            print("用户研究参考（未核验）：" + (note["value"] if detail_view else short_text(note["value"], 100)))
            shown = True
    sources = {**request.get("sources", {}), **((result or {}).get("recommendations") or {}).get("sources", {})}
    if sources and not detail_view:
        print("参考引用：" + str(len(sources)) + " 条；用 /sources 查看支持范围与限制。")
        shown = True
    for source in sources.values() if detail_view else []:
        shown = True
        print("参考：" + source.get("title", "") + " " + source.get("url", ""))
        print("支持范围：" + source.get("claim", ""))
        limitation = source.get("limitations", [])
        print("限制：" + (limitation if isinstance(limitation, str) else "；".join(limitation)))
    if not shown:
        print("暂无可核查引用；研究提案尚未科学核验。")


def render_intake_result(result, full=False):
    if not result:
        print("LAPIS> 请描述你本轮想研究的材料问题。")
        return
    request = result.get("request") or {}
    status = result.get("intake_status")
    if full or status == "needs_confirmation":
        print("当前研究规约：")
        for field, label in LABELS.items():
            print(label + "：" + field_text(field, request.get("fields", {}).get(field)))
        render_sources(result, detail=False)
    elif status == "ready_for_design":
        print("LAPIS> 研究请求已保存，可进入研究设计；确认规约不授权计算。")
    elif result.get("changes"):
        fields = request.get("fields", {})
        changed = list(dict.fromkeys(result["changes"]))
        parts = []
        for field in changed:
            if field not in fields:
                continue
            entries = fields[field] if isinstance(fields[field], list) else [fields[field]]
            if not any(e.get("value") or e.get("source") == "user" and e.get("status") in {"none", "open"} for e in entries):
                continue
            parts.append(LABELS[field] + "为" + field_text(field, fields[field]))
        parts = parts[:3]
        if parts:
            print("LAPIS> 已更新：" + "；".join(parts) + "。")
    if result.get("needs_review_fields"):
        print("需重核：" + "、".join(LABELS.get(f,f) for f in result["needs_review_fields"]) + "，原信息已保留。")
    if result.get("notice"):
        print("LAPIS> " + result["notice"])
    rec = result.get("recommendations") or {}
    if rec.get("options"):
        print("可选方向（模型提案尚未科学核验）：")
        for i, option in enumerate(rec["options"], 1):
            print(f"{i}. {option['label']}：{short_text(option.get('reason', ''), 90)}")
            purpose = option.get("fields", {}).get("purpose")
            if isinstance(purpose, dict) and purpose.get("value"):
                print("拟研究：" + short_text(purpose["value"], 100))
            for key, label in (("assumptions", "假设"), ("limitations", "限制")):
                if option.get(key):
                    print(label + "：" + "；".join(short_text(x, 75) for x in option[key][:2]))
    if result.get("next_question") and status != "ready_for_design":
        print("LAPIS> " + ("你想采用哪个方向，或修改哪些内容？" if rec.get("options") else short_text(result["next_question"], 180)))


def run_chat(task_id: str | None, actor: str) -> None:
    key = os.getenv("LAPIS_API_KEY") or os.getenv("DEEPSEEK_API_KEY")
    if not key:
        raise ValueError("请先设置 LAPIS_API_KEY 或 DEEPSEEK_API_KEY")
    task_id = task_id or create_task(actor)
    task = get_task(task_id)
    model = os.getenv("LAPIS_MODEL", "deepseek-flash")
    client = instructor.from_openai(OpenAI(
        api_key=key, base_url=os.getenv("LAPIS_BASE_URL", "https://api.deepseek.com"), timeout=40, max_retries=0,
    ), mode=instructor.Mode.JSON)
    prompt_hash = digest({"extract": SYSTEM_PROMPT, "proposal": PROPOSAL_PROMPT})
    print("输入 /show 查看完整规约，/sources 查看依据，/debug 查看技术记录，/exit 退出。", flush=True)
    result = task.get("intake_result")
    if task.get("intake_state") and task["intake_state"].get("contract_version") != 4:
        result = preview_intake(task["intake_state"])
    display = True
    reviewed_draft_id = None
    while True:
        if display:
            result = hydrate_review(result)
            render_intake_result(result)
            display = False
        input_context = dict((result or {}).get("input_context", {}))
        if reviewed_draft_id and reviewed_draft_id == input_context.get("draft_id"):
            input_context["reviewed_draft_id"] = reviewed_draft_id
        try:
            text = input("你> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return
        if text in {"/exit", "退出"}:
            print("恢复此对话：lapis.py chat --task-id " + task_id)
            return
        if text in {"/show", "/sources", "/debug"}:
            if text == "/show":
                render_intake_result(result, full=True)
                reviewed_draft_id = ((result or {}).get("request") or {}).get("draft_id")
            elif text == "/sources":
                render_sources(result)
            else:
                print(json.dumps({"task_id": task_id, "result": result}, ensure_ascii=False, indent=2))
            # Commands neither call the model nor count as research dialogue turns.
            continue
        if not text:
            continue
        operation_id = str(uuid4())

        while True:
            try:
                output = run_intake_turn(task_id, text, actor, client, model, prompt_hash, operation_id,
                                         input_context=input_context)
                break
            except Exception as error:
                print(f"本轮失败，草稿未新增提交：{type(error).__name__}。定位操作：{operation_id}")
                try:
                    retry = input("用同一操作 ID 重试本轮？[Y/n] ").strip().casefold()
                except (EOFError, KeyboardInterrupt):
                    print()
                    return
                if retry not in {"", "y", "yes", "是"}:
                    return
        result = output["result"]
        display = True


def main():
    parser = argparse.ArgumentParser(description="LAPIS 研究规约与执行门槛")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("setup-db")
    sub.add_parser("migrate")
    start = sub.add_parser("start")
    start.add_argument("--actor", default="researcher")
    chat = sub.add_parser("chat", help="逐轮澄清并持久保存研究请求")
    chat.add_argument("--task-id", help="继续已有任务；省略时新建任务")
    chat.add_argument("--actor", default="researcher")
    turn = sub.add_parser("turn")
    turn.add_argument("task_id")
    turn.add_argument("text")
    turn.add_argument("--actor", default="researcher")
    turn.add_argument("--operation-id", help="重试时复用同一标识，避免重复记录同一轮输入")
    turn.add_argument("--draft-id", help="从上轮输出复制草稿 ID")
    turn.add_argument("--recommendation-id", help="从上轮输出复制推荐集合 ID")
    turn.add_argument("--recommendation-version", type=int, help="推荐集合版本")
    show = sub.add_parser("show")
    show.add_argument("task_id")
    show.add_argument("--debug", action="store_true", help="显示完整技术 JSON")
    design = sub.add_parser("propose-design")
    design.add_argument("task_id")
    design.add_argument("file", type=Path)
    design.add_argument("--actor", default="researcher")
    approve = sub.add_parser("approve-design")
    approve.add_argument("task_id")
    approve.add_argument("version", type=int)
    approve.add_argument("--reviewer", required=True)
    freeze = sub.add_parser("freeze")
    freeze.add_argument("task_id")
    freeze.add_argument("version", type=int)
    freeze.add_argument("--actor", default="researcher")
    simulate = sub.add_parser("simulate", help="仅演练软件流程，不生成科研结果")
    simulate.add_argument("execution_id")
    simulate.add_argument("--fail", action="store_true")
    simulate.add_argument("--actor", default="test-adapter")
    verify = sub.add_parser("verify-artifacts")
    verify.add_argument("execution_id")
    reconcile = sub.add_parser("reconcile-attempt")
    reconcile.add_argument("attempt_id")
    reconcile.add_argument("--actor", required=True)
    cancel = sub.add_parser("cancel-attempt")
    cancel.add_argument("attempt_id")
    cancel.add_argument("--actor", required=True)
    args = parser.parse_args()

    try:
        if args.command == "setup-db":
            setup_database()
        elif args.command == "migrate":
            applied = initialize_schema()
            print("数据库迁移已完成：" + (", ".join(f"{version:03d}" for version in applied) if applied else "无待应用版本"))
        elif args.command == "start":
            print(create_task(args.actor))
        elif args.command == "chat":
            run_chat(args.task_id, args.actor)
        elif args.command == "show":
            task = get_task(args.task_id)
            if args.debug:
                print(json.dumps(task, ensure_ascii=False, indent=2))
            else:
                render_intake_result(hydrate_review(task.get("intake_result")), full=True)
        elif args.command == "turn":
            operation_id = args.operation_id or str(uuid4())
            key = os.getenv("LAPIS_API_KEY") or os.getenv("DEEPSEEK_API_KEY")
            if not key:
                raise ValueError("请先设置 LAPIS_API_KEY 或 DEEPSEEK_API_KEY")
            model = os.getenv("LAPIS_MODEL", "deepseek-flash")
            client = instructor.from_openai(OpenAI(
                api_key=key, base_url=os.getenv("LAPIS_BASE_URL", "https://api.deepseek.com"), timeout=40, max_retries=0,
            ), mode=instructor.Mode.JSON)
            prompt_hash = digest({"extract": SYSTEM_PROMPT, "proposal": PROPOSAL_PROMPT})

            context = {}
            if args.draft_id:
                context["draft_id"] = args.draft_id
            if args.recommendation_id or args.recommendation_version is not None:
                if not args.recommendation_id or args.recommendation_version is None:
                    raise ValueError("推荐 ID 和版本须同时提供")
                context["recommendation_ref"] = {"id": args.recommendation_id, "version": args.recommendation_version}
            result = run_intake_turn(args.task_id, args.text, args.actor, client, model, prompt_hash,
                                     operation_id, input_context=context)
            print(json.dumps(result, ensure_ascii=False, indent=2))
        elif args.command == "propose-design":
            payload = json.loads(args.file.read_text(encoding="utf-8"))
            print(propose_design(args.task_id, payload, args.actor))
        elif args.command == "approve-design":
            approve_design(args.task_id, args.version, args.reviewer)
            print("研究设计已审核")
        elif args.command == "freeze":
            print(freeze_execution(args.task_id, args.version, args.actor))
        elif args.command == "simulate":
            print(run_simulation(args.execution_id, args.actor, args.fail))
        elif args.command == "verify-artifacts":
            print(json.dumps(verify_artifacts(args.execution_id), ensure_ascii=False, indent=2))
        elif args.command == "reconcile-attempt":
            transition_attempt(args.attempt_id, "needs_reconciliation", args.actor)
        elif args.command == "cancel-attempt":
            transition_attempt(args.attempt_id, "cancelled", args.actor)
    except (ValueError, RuntimeError, psycopg.Error) as error:
        parser.exit(1, f"错误：{error}\n")


if __name__ == "__main__":
    main()
