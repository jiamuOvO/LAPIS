"""Durable human-in-the-loop intake; PostgreSQL remains the domain source of truth."""

from __future__ import annotations

import os
import hashlib
from typing import TypedDict
from uuid import uuid4

from langgraph.checkpoint.postgres import PostgresSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt
from psycopg.conninfo import make_conninfo

from lapis_intake import handle_turn
from lapis_store import connect, db_config, get_intake_operation, get_task, save_turn


class IntakeState(TypedDict, total=False):
    task_id: str
    actor: str
    user_text: str | None
    operation_id: str | None
    next_question: str | None
    status: str
    request_version: int | None


def build_intake_graph(client, model: str, prompt_hash: str, checkpointer):
    graph = StateGraph(IntakeState)

    def process(state: IntakeState) -> dict:
        task_id, operation_id = state["task_id"], state["operation_id"]
        if not operation_id or not state.get("user_text"):
            raise ValueError("缺少本轮输入或操作标识")
        previous = get_intake_operation(operation_id)
        if previous:
            if previous["task_id"] != task_id:
                raise ValueError("操作标识已属于其他任务")
            result, version = previous["result"], previous["request_version"]
        else:
            task = get_task(task_id)
            intake_state = task["intake_state"]
            result = handle_turn(client, model, intake_state, state["user_text"])
            version = save_turn(task_id, task["revision"], intake_state, result,
                                state["actor"], model, prompt_hash, operation_id)
        return {"user_text": None, "operation_id": None, "status": result["intake_status"],
                "next_question": result.get("next_question"), "request_version": version}

    def route(state: IntakeState) -> str:
        return "await_user" if state.get("next_question") and state.get("status") != "ready" else END

    def await_user(state: IntakeState) -> dict:
        answer = interrupt({"task_id": state["task_id"], "question": state["next_question"]})
        if not isinstance(answer, dict) or not answer.get("text") or not answer.get("operation_id"):
            raise ValueError("恢复输入必须包含非空文本和操作标识")
        return {"user_text": answer["text"], "operation_id": answer["operation_id"],
                "actor": answer["actor"]}

    graph.add_node("process", process)
    graph.add_node("await_user", await_user)
    graph.add_edge(START, "process")
    graph.add_conditional_edges("process", route)
    graph.add_edge("await_user", "process")
    return graph.compile(checkpointer=checkpointer)


def run_intake_turn(task_id: str, text: str, actor: str, client, model: str, prompt_hash: str,
                    operation_id: str | None = None) -> dict:
    if not text.strip():
        raise ValueError("输入不能为空")
    text = text.strip()
    operation_id = operation_id or str(uuid4())
    # Checkpoint data shares the local PostgreSQL instance but never authorizes a calculation.
    os.environ["LANGGRAPH_STRICT_MSGPACK"] = "true"
    lock_key = hashlib.sha256(task_id.encode("utf-8")).digest()
    lock_parts = tuple(int.from_bytes(lock_key[offset:offset + 4], "big", signed=True)
                       for offset in (0, 4))
    with connect() as lock_db:
        acquired = lock_db.execute("SELECT pg_try_advisory_lock(%s,%s)", lock_parts).fetchone()[0]
        if not acquired:
            raise ValueError("此任务正在处理另一轮输入，请稍后重试")
        with PostgresSaver.from_conn_string(make_conninfo(**db_config())) as checkpointer:
            checkpointer.setup()
            graph = build_intake_graph(client, model, prompt_hash, checkpointer)
            config = {"configurable": {"thread_id": task_id}}
            pending = graph.get_state(config).next
            previous = get_intake_operation(operation_id)
            if previous and previous["task_id"] != task_id:
                raise ValueError("操作标识已属于其他任务")
            if previous and previous["input_sha256"] and previous["input_sha256"] != hashlib.sha256(text.encode("utf-8")).hexdigest():
                raise ValueError("相同操作标识不能用于不同输入")
            if pending == ("process",):
                # The previous process node may have committed before its checkpoint failed.
                graph.invoke(None, config)
                previous = get_intake_operation(operation_id)
                if previous and previous["task_id"] != task_id:
                    raise ValueError("操作标识已属于其他任务")
                if previous and previous["input_sha256"] and previous["input_sha256"] != hashlib.sha256(text.encode("utf-8")).hexdigest():
                    raise ValueError("相同操作标识不能用于不同输入")
                if not previous:
                    raise ValueError("已恢复上一轮受理；本轮输入尚未处理，请用原操作标识重试")
            elif not previous:
                if pending == ("await_user",):
                    graph.invoke(Command(resume={"text": text, "operation_id": operation_id,
                                                 "actor": actor}), config)
                elif pending:
                    raise ValueError("图中存在未知的待恢复步骤")
                else:
                    graph.invoke({"task_id": task_id, "user_text": text, "operation_id": operation_id,
                                  "actor": actor}, config)
            waiting = bool(graph.get_state(config).next)
        task = get_task(task_id)
    result = previous["result"] if previous else task["intake_result"]
    version = previous["request_version"] if previous else task["request_version"]
    return {"operation_id": operation_id, "result": result, "request_version": version,
            "graph_status": "waiting_for_user" if waiting else "idle"}
