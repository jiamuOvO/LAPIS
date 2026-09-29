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
from lapis_intake import PROPOSAL_PROMPT, SYSTEM_PROMPT
from lapis_graph import run_intake_turn
from lapis_store import (ROOT, approve_design, artifact_root, create_simulation_attempt, create_task,
                         db_config, freeze_execution, get_task, initialize_schema,
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


def main():
    parser = argparse.ArgumentParser(description="LAPIS 研究规约与执行门槛")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("setup-db")
    sub.add_parser("migrate")
    start = sub.add_parser("start")
    start.add_argument("--actor", default="researcher")
    turn = sub.add_parser("turn")
    turn.add_argument("task_id")
    turn.add_argument("text")
    turn.add_argument("--actor", default="researcher")
    turn.add_argument("--operation-id", help="重试时复用同一标识，避免重复记录同一轮输入")
    show = sub.add_parser("show")
    show.add_argument("task_id")
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
        elif args.command == "show":
            print(json.dumps(get_task(args.task_id), ensure_ascii=False, indent=2))
        elif args.command == "turn":
            operation_id = args.operation_id or str(uuid4())
            key = os.getenv("LAPIS_API_KEY") or os.getenv("DEEPSEEK_API_KEY")
            if not key:
                raise ValueError("请先设置 LAPIS_API_KEY 或 DEEPSEEK_API_KEY")
            model = os.getenv("LAPIS_MODEL", "deepseek-flash")
            client = instructor.from_openai(OpenAI(
                api_key=key, base_url=os.getenv("LAPIS_BASE_URL", "https://api.deepseek.com"), timeout=40,
            ), mode=instructor.Mode.JSON)
            prompt_hash = digest({"extract": SYSTEM_PROMPT, "proposal": PROPOSAL_PROMPT})
            print(f"operation_id={operation_id}", flush=True)
            result = run_intake_turn(args.task_id, args.text, args.actor, client, model, prompt_hash,
                                     operation_id)
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
