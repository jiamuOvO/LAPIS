"""Fixed proposal output; dynamic content is never treated as verified science."""
from copy import deepcopy
import json
import os
from uuid import uuid4
from typing import Literal
import re

from pydantic import BaseModel, ConfigDict, Field, model_validator
from lapis_contract import content_hash, FIELDS, domain_from_fields
from lapis_guidance import recommendations as reference_recommendations

class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

class ScalarProposal(StrictModel):
    status: Literal["specified", "unknown", "none", "open", "unclear"]
    value: str | None = None

    @model_validator(mode="after")
    def valid_state(self):
        if self.status not in {"specified", "unknown", "none", "open", "unclear"}:
            raise ValueError("Invalid field state")
        if self.status == "specified" and not (self.value or "").strip():
            raise ValueError("Specified field requires a value")
        return self

class GoalProposal(ScalarProposal):
    value: str | None = Field(description="性能名称必须显式填写；specified不能为null，unknown可以为null")
    direction: str | None = None

    @model_validator(mode="after")
    def valid_goal(self):
        if self.status == "specified" and not self.direction:
            raise ValueError("Goal requires direction")
        return self

class ConstraintProposal(ScalarProposal):
    strength: Literal["hard", "preference"] | None = None

    @model_validator(mode="after")
    def valid_strength(self):
        if self.status == "specified" and self.strength not in {"hard", "preference"}:
            raise ValueError("Constraint requires hard/preference")
        return self

class ProposalFields(StrictModel):
    purpose: ScalarProposal | None = None
    research_object: ScalarProposal | None = None
    application: ScalarProposal | None = None
    work_conditions: ScalarProposal | None = None
    target_performance: list[GoalProposal] | None = Field(default=None, min_length=1, max_length=5)
    constraints: list[ConstraintProposal] | None = Field(default=None, min_length=1, max_length=5)
    research_scope: ScalarProposal | None = None
    material_function: ScalarProposal | None = None

class Direction(StrictModel):
    label: str = Field(min_length=1, max_length=120)
    fields: ProposalFields
    reason: str = Field(min_length=1)
    assumptions: list[str]
    limitations: list[str] = Field(min_length=1)
    clarifications: list[str]
    source_refs: list[str]

class Guidance(StrictModel):
    explanation: str = Field(min_length=1)
    options: list[Direction] = Field(max_length=3)

GUIDANCE_PROMPT = """你是LAPIS材料研究规约助手。根据本轮意图和当前八项草稿动态提出研究方向，
不限于参考目录中的材料。不是计算执行器，也不提供已验证性能、最优配方或实算结论。
初次方向推荐或明确换方向时给2到3个可选择方向；context.focus_field非空时只针对该缺口给简洁选择，fields只补focus_field，不重开已有方向。已有对象/用途/目标、已采用指标与硬约束保持。解释已有问题可以只给解释和空options。
方向必须是材料应用研究；排除药物筛选。基础性质问题帮助限定材料用途。
fields只能补充八项相关字段，其余不要填。不要为了完整而生成温度、电压、结构、浓度、阈值。
不要复制无关旧场景。保留用户的硬约束；已拒绝方向不要仅换个名字再提出。
研究对象允许有边界的类别；功能表达拟承担的作用，不断言已具备。
目标允许多目标和定性方向。性价比须拆成本口径、预算、性能优先项，不能保证便宜或最优。
方向给reason、assumptions、limitations、clarifications；不确定信息保持unknown/open/unclear。
status描述研究意图是否明确，不表示科学真实性已验证。方向中明确提出的purpose、application、
research_scope、material_function和目标通常用specified；这些只是待用户采用的意图。
未知科学效果放limitations，不要因此把明确的研究行动标open。条件和参数不确定时保持unknown/open。
所有指定项都必须有value：包括列表中的每个目标和约束。目标value是性能名称，direction是比较/考察/提高等方向，不能只填direction。示例target_performance=[{"status":"specified","value":"性能名称","direction":"比较"}]。未知目标value显式为null。约束指定hard/preference。
source_refs只可引用输入提供的资料ID；无资料也可提出未核验研究提案，使用空数组。
不要生成文献名称、DOI或URL，不得自称资料已核查、计算已完成或性能已证明。
已提供的资料只支持其明示对象与范围，不可推广。所有模型新提案均未核验，
用户选择只采用研究意图。输出固定JSON，不能增加键。
"""

def direction_fingerprint(option):
    return content_hash({"label": option.get("label"), "fields": option.get("fields")})


def generate_recommendations(client, model, state, text, operation_id=None):
    try:
        reference = reference_recommendations(state, text)
    except FileNotFoundError:
        reference = None
    references = reference["sources"] if reference else {}
    focus = state.get("asked_field") if any(h.get("status")=="accepted" for h in state.get("recommendation_history",[])) and state.get("guidance_mode") != "new_direction" else None
    context = {"focus_field": focus, "input": text, "fields": state["fields"], "domain": state["domain"],
               "issues": state["issues"], "asked_field": state["asked_field"],
               "current_options": (state.get("recommendation_set") or {}).get("options", []),
               "rejected": [e for e in state["recommendation_history"] if e["status"] == "rejected"],
               "reference_material": references}
    options = {"extra_body": {"thinking": {"type": "disabled"}}} if os.getenv(
        "LAPIS_BASE_URL", "https://api.deepseek.com").startswith("https://api.deepseek.com") else {}
    output = client.create(model=model, response_model=Guidance,
                           messages=[{"role": "system", "content": GUIDANCE_PROMPT},
                                     {"role": "user", "content": json.dumps(context, ensure_ascii=False)}],
                           max_retries=2, max_tokens=2600, temperature=0, **options)
    # Instructor output is revalidated so test doubles and future clients cannot bypass the schema.
    output = Guidance.model_validate(output.model_dump() if isinstance(output, BaseModel) else output)
    if re.search(r"https?://|doi\.org|10\.\d{4,9}/", output.explanation, re.I):
        raise ValueError("模型解释含未提供的文献或链接")
    rec_id = str(uuid4())
    directions = []
    for index, option in enumerate(output.options, 1):
        if any(ref not in references for ref in option.source_refs):
            raise ValueError("模型提案引用了未提供或未核查的资料 ID")
        fields = option.fields.model_dump(exclude_none=True)
        if focus:
            fields = {focus: fields[focus]} if focus in fields else {}
        if not fields:
            raise ValueError("研究方向没有提出任何八项字段补充")
        if domain_from_fields({**state["fields"], **fields}) == "drug_discovery":
            raise ValueError("模型方向超出材料应用受理范围")
        if any(field not in FIELDS for field in fields):
            raise ValueError("研究方向含未知字段")
        prose = json.dumps(option.model_dump(exclude={"source_refs"}), ensure_ascii=False)
        if re.search(r"https?://|doi\.org|10\.\d{4,9}/", prose, re.I):
            raise ValueError("模型提案含未提供的文献或链接，需重新生成")
        directions.append({"id": rec_id + "-option-" + str(index), **option.model_dump(exclude={"fields"}),
                           "fields": fields, "suggestion_origin": "model", "evidence_status": "unverified"})
    rejected = {e.get("fingerprint") or direction_fingerprint(e) for e in state["recommendation_history"] if e["status"] == "rejected"}
    reconsider = bool(re.search(r"重新(?:考虑|采用)|撤销拒绝", text)) and not bool(
        re.search(r"(?:不|别|不要|暂不|先不)\s*(?:重新(?:考虑|采用)|撤销拒绝)", text))
    filtered = [d for d in directions if direction_fingerprint(d) not in rejected or reconsider]
    if len(filtered) != len(directions):
        output.explanation += " 已排除与您拒绝内容完全相同的方向；请说明希望换哪种角度。"
    directions = filtered
    for direction in directions:
        direction["reconsidered"] = reconsider and direction_fingerprint(direction) in rejected
    used = {ref for d in directions for ref in d["source_refs"]}
    return {"id": rec_id, "version": state.get("recommendation_revision", 0) + 1,
            "draft_id": state["draft_id"], "explanation": output.explanation,
            "generation": {"origin": "model", "model": model, "prompt_sha256": content_hash(GUIDANCE_PROMPT),
                           "operation_id": operation_id},
            "focus_field": focus, "options": directions, "sources": {ref: deepcopy(references[ref]) for ref in used},
            "catalog_version": reference["catalog_version"] if reference else None}
