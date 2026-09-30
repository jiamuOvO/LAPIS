"""Small, deterministic gates between intake, research design, and execution."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, model_validator


class Candidate(BaseModel):
    id: str = Field(min_length=1)
    description: str = Field(min_length=1)
    composition: dict[str, str] = Field(min_length=1)


class Quantity(BaseModel):
    value: float
    unit: str = Field(min_length=1)


class Experiment(BaseModel):
    id: str = Field(min_length=1)
    candidate_id: str = Field(min_length=1)
    observable: str = Field(min_length=1)
    observable_unit: str = Field(min_length=1)
    conditions: dict[str, Quantity] = Field(min_length=1)
    method: str = Field(min_length=1)
    method_version: str = Field(min_length=1)
    model: str = Field(min_length=1)
    model_version: str = Field(min_length=1)
    parameters: dict[str, str] = Field(min_length=1)
    quality_rules: list[str] = Field(min_length=1)
    expected_raw_files: list[str] = Field(min_length=1)
    max_walltime_minutes: int = Field(gt=0)


class ExecutionPlan(BaseModel):
    """A reviewed proposal. This does not dispatch a job by itself."""

    request_version: int = Field(gt=0)
    purpose: str = Field(min_length=1)
    candidates: list[Candidate] = Field(min_length=1)
    experiments: list[Experiment] = Field(min_length=1)
    source: Literal["researcher"] = "researcher"

    @model_validator(mode="after")
    def references_exist(self):
        ids = [candidate.id for candidate in self.candidates]
        experiment_ids = [experiment.id for experiment in self.experiments]
        if len(ids) != len(set(ids)) or len(experiment_ids) != len(set(experiment_ids)):
            raise ValueError("候选或实验 ID 重复")
        if any(experiment.candidate_id not in ids for experiment in self.experiments):
            raise ValueError("实验引用了不存在的候选")
        def check(value):
            if isinstance(value, str) and (not value.strip() or any(
                marker in value.casefold() for marker in ("待定", "待确认", "tbd", "todo", "unknown", "test-only")
            )):
                raise ValueError("执行设计包含占位或未定内容")
            if isinstance(value, dict):
                for item in value.values():
                    check(item)
            if isinstance(value, list):
                for item in value:
                    check(item)
        check(self.model_dump())
        return self


def canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value: object) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


REQUEST_FIELDS_V2 = {
    "purpose", "research_object", "application", "work_conditions",
    "target_performance", "constraints", "research_scope", "material_function",
}


def validate_research_request_v2(payload: dict) -> None:
    """A confirmed request may enter research design; it never authorizes computation."""
    if not isinstance(payload, dict) or payload.get("contract_version") != 2:
        raise ValueError("进入研究设计前须重新确认 v2 研究请求")
    if not isinstance(payload.get("original_intent"), str) or not payload["original_intent"].strip():
        raise ValueError("研究请求缺少用户原始意图")
    if payload.get("task_pattern") == "out_of_scope":
        raise ValueError("非材料研究不在首版范围")
    fields = payload.get("fields")
    if not isinstance(fields, dict) or set(fields) != REQUEST_FIELDS_V2:
        raise ValueError("研究请求必须分别包含八项输出")
    for key in REQUEST_FIELDS_V2:
        entries = fields[key] if key in {"target_performance", "constraints"} else [fields[key]]
        if not isinstance(entries, list) or not entries or not all(isinstance(x, dict) for x in entries):
            raise ValueError(f"{key} 缺少可检查的状态")
        for entry in entries:
            if entry.get("status") not in {"specified", "none", "open", "unclear", "unknown"}:
                raise ValueError(f"{key} 状态无效")
            source = entry.get("source")
            if source == "system_suggestion":
                raise ValueError(f"{key} 的系统建议尚未获得用户确认")
            if source in {"user", "confirmed_suggestion"} and (
                not isinstance(entry.get("turn"), int) and source == "user"
                or source == "user" and not entry.get("quote")
                or source == "confirmed_suggestion" and not isinstance(entry.get("confirmed_turn"), int)
            ):
                raise ValueError(f"{key} 缺少来源或确认轮次")
    for key in ("purpose", "research_object", "application", "research_scope"):
        entry = fields[key]
        if entry.get("status") != "specified" or not entry.get("value") or entry.get("source") != "user":
            raise ValueError(f"{key} 尚不足以界定研究任务")
    function = fields["material_function"]
    if function.get("status") != "specified" or not function.get("value") or function.get("source") not in {
        "user", "confirmed_suggestion"
    }:
        raise ValueError("材料功能尚未确认")
    goals = fields["target_performance"]
    if any(goal.get("status") == "unclear" or
           goal.get("status") == "specified" and (not goal.get("value") or not goal.get("direction"))
           for goal in goals):
        raise ValueError("目标性能含糊或缺少方向")
    if not any(goal.get("status") == "specified" and goal.get("value") and goal.get("direction")
               and goal.get("source") in {"user", "confirmed_suggestion"} for goal in goals):
        raise ValueError("目标性能方向尚未确认")
    if any(item.get("status") == "unclear" for item in fields["constraints"]):
        raise ValueError("含糊的约束须先澄清")


ATTEMPT_TRANSITIONS = {
    "queued": {"running", "cancelled"},
    "running": {"succeeded", "failed", "cancelled", "needs_reconciliation"},
    "needs_reconciliation": {"running", "failed", "cancelled"},
    "succeeded": set(), "failed": set(), "cancelled": set(),
}


def check_attempt_transition(current: str, target: str) -> None:
    if target not in ATTEMPT_TRANSITIONS.get(current, set()):
        raise ValueError(f"非法作业状态转换：{current} → {target}")


def intake_is_confirmed(state: dict, result: dict) -> bool:
    """Only explicit review confirmation creates a versioned research request."""
    request = result.get("request")
    if isinstance(request, dict) and request.get("contract_version") == 2:
        try:
            validate_research_request_v2(request)
        except ValueError:
            return False
        return bool(result.get("ready_for_design") and result.get("confirmation_event")
                    and result.get("calculation_status") == "pending_research_design"
                    and state.get("stage") == "ready_for_design" and state.get("turns")
                    and state["turns"][-1] in {"确认", "确认继续", "按此继续", "就按这个", "同意"})
    # Historical v1 requests stay readable. New design proposals reject them until v2 reconfirmation.
    return bool(result.get("ready") and result.get("calculation_status") == "pending_research_design"
                and state.get("stage") is None and state.get("turns")
                and state["turns"][-1] in {"确认", "确认继续", "按此继续", "就按这个", "同意", "采用方案"})


def verify_artifact(path: Path, root: Path) -> tuple[str, int, str]:
    """Hash an existing regular file under the controlled artifact directory."""
    root = root.resolve(strict=True)
    path = path.resolve(strict=True)
    if not path.is_relative_to(root) or not path.is_file():
        raise ValueError("产物必须是受控目录内的普通文件")
    before = path.stat()
    hasher = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            hasher.update(block)
    after = path.stat()
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise ValueError("产物在计算哈希期间被修改")
    return path.relative_to(root).as_posix(), after.st_size, hasher.hexdigest()
