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
    """`ready` alone is insufficient: a whole-draft confirmation is required."""
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
