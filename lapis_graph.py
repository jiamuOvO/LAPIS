"""Durable human-in-the-loop intake; PostgreSQL remains the domain source of truth."""

from __future__ import annotations

import os
from typing import TypedDict
from uuid import uuid4

from langgraph.checkpoint.postgres import PostgresSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt
from psycopg.conninfo import make_conninfo

from lapis_intake import handle_turn
from lapis_store import db_config, get_intake_operation, get_task, save_turn


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


def run_intake_turn(task_id: str, text: str, actor: str, client, model: str, prompt_hash: str) -> dict:
    if not text.strip():
        raise ValueError("输入不能为空")
    # Checkpoint data shares the local PostgreSQL instance but never authorizes a calculation.
    os.environ["LANGGRAPH_STRICT_MSGPACK"] = "true"
    with PostgresSaver.from_conn_string(make_conninfo(**db_config())) as checkpointer:
        checkpointer.setup()
        graph = build_intake_graph(client, model, prompt_hash, checkpointer)
        config = {"configurable": {"thread_id": task_id}}
        pending = graph.get_state(config).next
        operation_id = str(uuid4())
        if pending == ("await_user",):
            graph.invoke(Command(resume={"text": text, "operation_id": operation_id,
                                         "actor": actor}), config)
        elif pending:
            raise ValueError("图中存在待恢复的步骤，请先检查该任务状态")
        else:
            graph.invoke({"task_id": task_id, "user_text": text, "operation_id": operation_id,
                          "actor": actor}, config)
        waiting = bool(graph.get_state(config).next)
    task = get_task(task_id)
    return {"result": task["intake_result"], "request_version": task["request_version"],
            "graph_status": "waiting_for_user" if waiting else "idle"}
