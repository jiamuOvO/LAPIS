"""LLM-assisted research design drafts; every generated item remains unverified."""

from __future__ import annotations

import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from lapis_core import digest
from lapis_contract import FIELDS


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class CandidateDraft(StrictModel):
    id: str = Field(min_length=1)
    description: str = Field(min_length=1)
    composition: dict[str, str]
    basis_fields: list[str] = Field(min_length=1)


class HypothesisDraft(StrictModel):
    statement: str = Field(min_length=1)
    predicted_observations: list[str] = Field(min_length=1)
    falsifier: str = Field(min_length=1)
    alternatives: list[str]
    basis_fields: list[str] = Field(min_length=1)


class ExperimentDraft(StrictModel):
    id: str = Field(min_length=1)
    question: str = Field(min_length=1)
    candidate_ids: list[str] = Field(min_length=1)
    observable: str = Field(min_length=1)
    unit: str | None = None
    conditions: dict[str, str]
    method_suggestion: str | None = None
    model_suggestion: str | None = None
    quality_checks_to_define: list[str] = Field(min_length=1)
    basis_fields: list[str] = Field(min_length=1)


class DesignQuestion(StrictModel):
    field: str = Field(min_length=1)
    question: str = Field(min_length=1)
    blocks_execution: bool


class DesignDraft(StrictModel):
    paradigm: Literal["screening", "mechanism_verification", "comparison", "undetermined"]
    rationale: str = Field(min_length=1)
    candidates: list[CandidateDraft] = Field(min_length=1, max_length=8)
    experiments: list[ExperimentDraft] = Field(min_length=1, max_length=12)
    hypotheses: list[HypothesisDraft] = Field(max_length=8)
    open_questions: list[DesignQuestion]
    limitations: list[str] = Field(min_length=1)

    @model_validator(mode="after")
    def references_and_basis_are_valid(self):
        ids = [candidate.id for candidate in self.candidates]
        experiment_ids = [experiment.id for experiment in self.experiments]
        if len(ids) != len(set(ids)) or len(experiment_ids) != len(set(experiment_ids)):
            raise ValueError("候选或实验 ID 重复")
        known = set(ids)
        if any(not set(experiment.candidate_ids) <= known for experiment in self.experiments):
            raise ValueError("实验引用了不存在的候选")
        groups = [item for item in self.candidates + self.experiments + self.hypotheses]
        if any(not set(item.basis_fields) <= set(FIELDS) for item in groups):
            raise ValueError("依据只能引用研究请求的八个字段")
        return self


DESIGN_PROMPT = """你是材料计算研究设计助手。根据已确认研究请求，提出可审核的研究设计草案。
所有输出均是未核验的建议，绝不表示材料性质已知、计算已完成或结论成立。
只使用输入请求中明示的信息作为事实；新增配方、候选、方法、模型、条件、单位或数值都必须作为建议，
并在 open_questions 中要求研究人员核查。不要虚构文献、来源、许可证、力场参数或质量阈值。
候选的 basis_fields、实验和假设的 basis_fields 只能引用以下八个键：
purpose, research_object, application, work_conditions, target_performance, constraints, research_scope, material_function。
只为请求涉及的对象提出候选；缺少组分时可以给有边界的候选类别，并列出组成待核实问题。
纯筛选或比较不需要编造机制假设；机制验证必须给可观测预测和可反驳条件。
方法未定时 method_suggestion 可为 null；观测单位未知时 unit 为 null。
条件字典只抄录用户已确认条件；不得把推荐值或常识写成用户条件。缺项写入 open_questions。
quality_checks_to_define 写需要制定的检查类型，不要伪造数值阈值。
输出严格符合给定 JSON 结构。"""


def generate_design(client, model: str, request: dict) -> DesignDraft:
    payload = {
        "research_request": request,
        "allowed_basis_fields": sorted(FIELDS),
        "status_boundary": "研究请求已确认，只准形成设计草案，不授权真实计算",
    }
    result = client.create(
        model=model,
        response_model=DesignDraft,
        messages=[
            {"role": "system", "content": DESIGN_PROMPT},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
        ],
        max_retries=2,
        max_tokens=3200,
        temperature=0,
    )
    return DesignDraft.model_validate(result.model_dump() if isinstance(result, BaseModel) else result)


def saved_design_payload(draft: DesignDraft, request_version: int, model: str,
                         operation_id: str, request: dict) -> dict:
    return {
        "schema": "lapis.design-draft.v1",
        "request_version": request_version,
        "paradigm": draft.paradigm,
        "rationale": draft.rationale,
        "candidates": [item.model_dump() for item in draft.candidates],
        "experiments": [item.model_dump() for item in draft.experiments],
        "hypotheses": [item.model_dump() for item in draft.hypotheses],
        "open_questions": [item.model_dump() for item in draft.open_questions],
        "limitations": draft.limitations,
        "evidence_status": "unverified",
        "generation": {
            "origin": "model_proposal",
            "model": model,
            "prompt_sha256": digest(DESIGN_PROMPT),
            "request_sha256": digest(request),
            "operation_id": operation_id,
        },
    }
