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
    id: str | None = None
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
    hypothesis_ids: list[str] = Field(default_factory=list)


class DesignQuestion(StrictModel):
    field: str = Field(min_length=1)
    question: str = Field(min_length=1)
    blocks_execution: bool
    id: str | None = None
    status: str | None = None


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
        for hypothesis in self.hypotheses:
            if not hypothesis.id:
                hypothesis.id = "hyp-" + digest(hypothesis.statement)[:12]
        hypothesis_ids = [item.id for item in self.hypotheses]
        if len(hypothesis_ids) != len(set(hypothesis_ids)):
            raise ValueError("机制假设 ID 重复")
        ids = [candidate.id for candidate in self.candidates]
        experiment_ids = [experiment.id for experiment in self.experiments]
        if len(ids) != len(set(ids)) or len(experiment_ids) != len(set(experiment_ids)):
            raise ValueError("候选或实验 ID 重复")
        known = set(ids)
        if any(not set(experiment.candidate_ids) <= known for experiment in self.experiments):
            raise ValueError("实验引用了不存在的候选")
        if any(not set(experiment.hypothesis_ids) <= set(hypothesis_ids) for experiment in self.experiments):
            raise ValueError("实验引用了不存在的机制假设")
        if self.paradigm == "mechanism_verification" and not self.hypotheses:
            raise ValueError("机制验证设计须包含可观测预测和反驳条件")
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

REVISION_PROMPT = """你为 LAPIS 已保存的研究设计提出修订建议。只输出给定严格结构中的完整设计草案。
保留用户已确认的研究请求事实和来源；不得把建议改写成用户事实。只处理用户明确提出的修改。
不得补造力场、计算参数、阈值、文献或科研结论。未知内容仍写入 open_questions。
你提出的是待审阅建议，不得声称已采用、已审核或可执行；不得改变 task、request_version 或来源标识。
纯筛选/比较不需机制假设；机制验证必须有可观测预测和可反驳条件。"""


def design_input_sha256(request: dict, model: str, provider_sha256: str) -> str:
    return digest({"request_sha256": digest(request), "prompt_sha256": digest(DESIGN_PROMPT),
                   "model": model, "provider_sha256": provider_sha256})


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
                         operation_id: str, request: dict,
                         provider_sha256: str | None = None) -> dict:
    provider_sha256 = provider_sha256 or digest("https://api.deepseek.com")
    payload = {
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
            "provider_sha256": provider_sha256,
            "input_sha256": design_input_sha256(request, model, provider_sha256),
            "operation_id": operation_id,
        },
    }
    return normalize_design_gaps(payload)


def normalize_design_gaps(payload: dict) -> dict:
    """Give legacy and newly generated open questions stable review IDs."""
    for item in payload.get("hypotheses", []):
        if isinstance(item, dict) and not item.get("id"):
            item["id"] = "hyp-" + digest(item.get("statement", ""))[:12]
    for item in payload.get("open_questions", []):
        if not item.get("id"):
            item["id"] = "gap-" + digest({"field": item.get("field"), "question": item.get("question")})[:12]
        item.setdefault("status", "unresolved")
    return payload


def generate_design_revision(client, model: str, request: dict, parent: dict, instruction: str) -> DesignDraft:
    payload = {"research_request": request, "saved_design": parent, "revision_instruction": instruction,
               "allowed_basis_fields": sorted(FIELDS), "status_boundary": "仅生成待审阅建议，不修改已保存设计"}
    result = client.create(
        model=model, response_model=DesignDraft,
        messages=[{"role": "system", "content": REVISION_PROMPT},
                  {"role": "user", "content": json.dumps(payload, ensure_ascii=False)}],
        max_retries=2, max_tokens=3200, temperature=0,
    )
    return DesignDraft.model_validate(result.model_dump() if isinstance(result, BaseModel) else result)


def changed_paths(before: object, after: object, prefix: str = "") -> list[str]:
    if isinstance(before, dict) and isinstance(after, dict):
        result = []
        for key in sorted(set(before) | set(after)):
            path = f"{prefix}/{key}"
            if key not in before or key not in after:
                result.append(path)
            else:
                result.extend(changed_paths(before[key], after[key], path))
        return result
    if isinstance(before, list) and isinstance(after, list):
        result = []
        for index in range(max(len(before), len(after))):
            path = f"{prefix}/{index}"
            if index >= len(before) or index >= len(after):
                result.append(path)
            else:
                result.extend(changed_paths(before[index], after[index], path))
        return result
    return [] if before == after else [prefix or "/"]


def render_design(record: dict, request: dict | None = None) -> str:
    payload = normalize_design_gaps(dict(record["payload"]))
    fields = (request or {}).get("fields", {})
    def field_value(name):
        value = fields.get(name)
        if isinstance(value, list):
            return "; ".join(item.get("value") or item.get("status", "unknown") for item in value)
        if isinstance(value, dict):
            return value.get("value") or value.get("status", "unknown")
        return "未指定"
    def sources(item):
        return ", ".join(f"{name}={field_value(name)}"
                          for name in item.get("basis_fields", [])) or "未绑定请求字段"
    lines = [f"研究设计 v{record['version']}  |  状态：{record['status']}  |  请求版本：{record['request_version']}",
             f"研究范式：{payload.get('paradigm', '未指定')}",
             f"研究问题：{payload.get('rationale') or payload.get('purpose') or '未填写'}"]
    if payload.get("generation"):
        generation = payload["generation"]
        lines.append(f"最初来源：{generation.get('origin', 'unknown')}；模型={generation.get('model', '未记录')}；"
                     f"请求哈希={generation.get('request_sha256', '未记录')}；提示词哈希={generation.get('prompt_sha256', '未记录')}")
    if payload.get("revision"):
        revision = payload["revision"]
        lines.append(f"修订来源：父版本 v{revision.get('parent_version')}；理由={revision.get('reason')}；"
                     f"修改者声明={revision.get('actor')}；变更字段={', '.join(revision.get('changed_paths', []))}")
    if payload.get("provenance"):
        provenance = payload["provenance"]
        lines.append(f"执行计划来源：请求 v{provenance.get('request_version')} sha256={provenance.get('request_sha256')}；"
                     f"设计 v{provenance.get('source_design_version')} "
                     f"sha256={provenance.get('source_design_sha256')}")
        lines.append(f"执行范围：{payload.get('execution_scope', '未指定')}；资源预算：{payload.get('budget', '未指定')}")
        lines.append(f"候选比较对：{payload.get('comparison_pairs') or '无'}")
        lines.append(f"筛选准则：{payload.get('screening_criteria') or '无'}")
        lines.append(f"机制假设—检验实验映射：{payload.get('hypothesis_tests') or '无'}")
    if request:
        lines.append("请求来源：" + (request.get("original_intent") or "（原始意图未提供）"))
        lines.append("依据字段：" + ", ".join(f"{name}={field_value(name)}"
                                               for name in (payload.get("basis_fields") or [])))
    lines.append("候选：")
    for candidate in payload.get("candidates", []):
        lines.append(f"  - {candidate.get('id')}: {candidate.get('description')}；组成={candidate.get('composition') or '待核实'}；"
                     f"依据={sources(candidate)}")
    lines.append("实验：")
    for experiment in payload.get("experiments", []):
        if payload.get("provenance"):
            lines.append(
                f"  - {experiment.get('id')}: 候选={experiment.get('candidate_id')}；来源实验={experiment.get('source_experiment_id')}；"
                f"观察量={experiment.get('observable')} [{experiment.get('observable_unit')}]；"
                f"条件={experiment.get('conditions')}；方法={experiment.get('method')} v{experiment.get('method_version')}；"
                f"模型={experiment.get('model')} v{experiment.get('model_version')}；"
                f"参数={experiment.get('parameters')}；质量规则={experiment.get('quality_rules')}；"
                f"预期原始文件={experiment.get('expected_raw_files')}；"
                f"单实验最长时限={experiment.get('max_walltime_minutes')} 分钟；"
                f"机制假设={experiment.get('hypothesis_ids') or '无'}"
            )
            continue
        lines.append(f"  - {experiment.get('id')}: {experiment.get('question') or experiment.get('observable')}; "
                     f"候选={', '.join(experiment.get('candidate_ids', [experiment.get('candidate_id', '')]))}; "
                     f"指标={experiment.get('observable') or '未指定'} {experiment.get('unit') or experiment.get('observable_unit') or ''}; "
                     f"条件={experiment.get('conditions') or '未定'}; "
                     f"方法={experiment.get('method_suggestion') or experiment.get('method') or '待定'}; "
                     f"质量检查={experiment.get('quality_checks_to_define') or experiment.get('quality_rules') or '未定'}; "
                     f"依据={sources(experiment)}")
    if payload.get("hypotheses"):
        lines.append("机制假设：")
        for hypothesis in payload["hypotheses"]:
            lines.append(f"  - {hypothesis.get('statement')}；预测={'; '.join(hypothesis.get('predicted_observations', []))}；"
                         f"反驳条件={hypothesis.get('falsifier')}；依据={sources(hypothesis)}")
    if payload.get("quality_rules") or payload.get("limitations"):
        lines.append("质量检查／限制：" + "; ".join(payload.get("quality_rules", []) + payload.get("limitations", [])))
    questions = payload.get("open_questions", [])
    lines.append(f"未定项（{len(questions)}）：")
    for question in questions:
        lines.append(f"  - {question.get('id')}: {question.get('question')} [{question.get('status', 'unresolved')}] "
                     f"阻断执行={question.get('blocks_execution', True)}")
    for disposition in payload.get("gap_dispositions", []):
        lines.append(f"缺口处置：{disposition.get('gap_id')} [{disposition.get('status')}]；"
                     f"理由={disposition.get('reason') or '未填写'}；依据路径={disposition.get('plan_pointer') or '未绑定'}")
    lines.append("科研适用性：待研究人员审核；此设计不代表计算结果或计算授权。")
    return "\n".join(lines)
