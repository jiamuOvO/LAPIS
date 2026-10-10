"""LAPIS intake: incomplete intent, bounded guidance, review and versioned confirmation."""
from __future__ import annotations

from copy import deepcopy
import json
import os
import re
import time
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator
from lapis_contract import (CONTRACT_VERSION, RULE_VERSION, FIELDS, LIST_FIELDS, CONFIRM_WORDS,
                            content_hash, domain_from_fields, request_issues,
                            validate_current_request)
from lapis_semantics import (build_context, proposal_echo, check_operations, prepare_summaries, adopt_summaries, runtime_fingerprint)
from lapis_proposals import generate_recommendations, GUIDANCE_PROMPT, direction_fingerprint

class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

REQUEST_FIELDS = FIELDS
SCALAR_FIELDS = tuple(x for x in FIELDS if x not in LIST_FIELDS)
TASK_TYPES = {"screening", "comparison", "mechanism_validation", "mechanism_exploration", "other"}
LABELS = dict(zip(FIELDS, ("研究目的", "研究对象", "应用场景", "工作条件", "目标性能", "约束条件", "研究范围", "材料功能")))
QUESTIONS = {
    "purpose": "本轮希望筛选、比较还是探索什么判断？",
    "research_object": "请限定研究的材料或具体体系。",
    "application": "这些材料用于什么使用或服役场景？不确定时可以请求推荐。",
    "research_scope": "本轮包含哪些对象或变化，暂不研究什么？",
    "material_function": "材料在所选场景里承担什么作用？可以先请求解释或推荐。",
    "target_performance": "希望比较或改善什么性能？定性方向也可以。",
    "constraints": "请澄清冲突要求、含义，以及哪些是硬约束或偏好。",
    "work_conditions": "请澄清工作条件的数值、单位或是否比较多种条件。",
}


class Temperature(StrictModel):
    value: str
    unit: Literal["K", "℃", "°C"]


class Predicate(StrictModel):
    target: str
    operator: Literal["require", "forbid", "allow"]
    scope: str = "current"


class Update(StrictModel):
    field: Literal["task_type", "purpose", "research_object", "application", "work_conditions",
                   "target_performance", "constraints", "research_scope", "material_function", "reference_note"]
    status: Literal["specified", "unknown", "none", "open", "unclear"]
    value: str | None = None
    quote: str = Field(min_length=1)
    action: Literal["add", "update", "replace", "remove"] = "add"
    intent_ref: str | None = None
    basis_refs: list[str] = Field(default_factory=list)
    scope_mode: Literal["unspecified", "current_scope", "no_extra_exclusions", "expand"] = "unspecified"
    change_timing: Literal["current", "future"] = "current"
    change_relation: Literal["unspecified", "restatement", "refinement", "switch", "uncertain"] = "unspecified"
    target_id: str | None = None
    target_value: str | None = None
    direction: str | None = None
    strength: Literal["hard", "preference"] | None = None
    category: Literal["hypothesis", "method", "parameter", "other"] | None = None
    predicate: Predicate | None = None
    temperatures: list[Temperature] = Field(default_factory=list)

    @model_validator(mode="after")
    def qualifiers(self):
        partial_update = self.action == "update" and (self.target_id or self.target_value)
        if self.status == "specified" and self.action != "remove" and not partial_update:
            if not (self.value or "").strip():
                raise ValueError("明确字段必须填写value，不能只写方向")
            if self.field == "target_performance" and not self.direction:
                raise ValueError("性能须有定性方向")
            if self.field == "constraints" and not self.strength:
                raise ValueError("约束须区分硬约束与偏好")
        return self


class Issue(StrictModel):
    field: str
    message: str
    quote: str
    kind: Literal["ambiguity", "conflict"] = "ambiguity"
    entry_ids: list[str] = Field(default_factory=list)


class Intent(StrictModel):
    id: str
    kind: Literal["answer", "inform", "edit", "adopt", "reject", "quote", "delegate", "progress", "confirm", "pause"]
    quote: str = Field(min_length=1)
    question_ref: str | None = None
    target_refs: list[str] = Field(default_factory=list)


class Extraction(StrictModel):
    summary: str = ""
    intents: list[Intent] = Field(default_factory=list)
    context_change: Literal["unspecified", "restatement", "refinement", "switch", "uncertain"] = "unspecified"
    pause_scope: Literal["conversation", "approval"] = Field(default="conversation", description="pause时区分停止交流conversation和暂不批准但继续解释approval")
    guidance_mode: Literal["auto", "fill_gap", "new_direction", "explain"] = "auto"
    actions: list[Literal["quote", "delegate", "inform", "recommend", "explain", "unsure", "select", "reject",
                         "edit", "pause", "confirm", "reaffirm", "progress"]] = Field(default_factory=lambda: ["inform"])
    updates: list[Update] = Field(default_factory=list)
    selected_option: int | None = None
    selection_mode: Literal["full", "partial"] = "full"
    clarification_field: str | None = None
    clarification_question: str | None = None
    domain: Literal["materials_application", "drug_discovery", "basic_research", "non_research", "uncertain"] | None = None
    domain_quote: str | None = None
    issues: list[Issue] = Field(default_factory=list)
    resolve_issue_ids: list[str] = Field(default_factory=list)
    reaffirm_fields: list[str] = Field(default_factory=list)


# Kept as a readable compatibility type, not an unconstrained scientific recommender.
class SuggestedGoal(BaseModel):
    value: str
    direction: str


class IntakeSuggestion(BaseModel):
    material_function: str
    target_performance: list[SuggestedGoal]
    limitation: str


SYSTEM_PROMPT = """你是 LAPIS 第一模块的结构化语义解释器。一次调用输出简短可检查的理解、意图和拟修改，只输出语义结论和引用，不输出推理过程、不虚构科学事实。一句话可以同时明确对象、用途和目标；application只需基本用途，不要求具体器件细分。材料名中清楚的用途限定词也要分拆为application，而不是再索取更细用途；务必分拆隐含但清楚的使用背景，不只把整个短语塞进研究对象后再问用户已说过的用途。明确命名其他字段是独立inform/edit，不要绑到正在回答的另一问题。固定八项：purpose研究行动/判断，research_object材料类别/体系，application基本用途，work_conditions条件，target_performance关注点和方向，constraints硬约束/偏好，research_scope本轮边界，material_function拟承担作用。
所有原话quote必须来自本轮连续片段。上下文用于解释省略，不冒充原话。intents每条有id/kind/quote；回答必须以question_ref指向current_question.id；修改指向target_refs。updates逐条intent_ref，引用当前条目ID或字段作为basis_refs。不重复输出未修改字段。
kind:answer回答当前问题；inform新信息；edit主动修改/撤回；adopt采用提案；reject拒绝；quote引用原提案；delegate委托建议；progress查询下一步；confirm明确批准；pause暂停。用户混合编辑可以有多条意图；回答某字段不能改另一个字段，独立主动编辑须另列意图。actions维持inform/edit/select/recommend/explain/unsure/reaffirm/reject/progress/confirm/pause/quote/delegate调用分工。
“比较吧”回答目标方向时，更新现有目标direction，不改value，不删除具体指标；也可独立明确研究目的但不要再重复追问已知目标。研究/考察/探索本身是合法目的，无需强选筛选或比较。定性关注点可以进入研究设计，无需具体指标、公式、单位、模型或计算路线。
add增加，update唯一指向现有项只改明确属性，remove需撤回意图并指向target_refs，replace只在明确整个字段替换时使用。已采用的目标/指标默认保留。仅选推荐中一子目标不加入未选提案目标，但不能删除此前用户已选项。推荐之外的新指标可以add，即使当前追问其他字段；用inform或edit独立意图。
“都研究/都包含”结合实际问题理解，范围回答不能修改目标。指代多个层级则不修改并问具体差别；已选范围内“无额外排除”要归纳有界范围，basis_refs指已有对象/用途/目标，并标scope_mode=current_scope或no_extra_exclusions；扩到新对象/用途为expand。真正指代歧义不输出updates。不写脱离上下文的短句，不扩大到未选方向。
quote区分复制拟研究/假设/限制与采用；原提案内容保持来源，不能写成用户constraints或reference_note。只有用户明确将某项作为要求才是用户约束。“随便你”委托提出方案，不代表撤回约束；“之前不是选过了吗”核对采用记录，不一次解除所有冲突。重贴已采用内容不是重新select陈旧推荐。
change_timing=current表示现在修改本轮规约，future表示之后另一轮才考虑的研究方向。仅提到“之后/后续打算转到另一用途”时标future，不能立即改当前用途或把未来目标并入当前范围；本轮计划中的未来计算不属于这种延期变更。对象/用途/范围变化标change_relation:restatement同义，refinement兼容细化/补未知，switch真正改变，uncertain不确定。首次明确用途不应要求重核原目标。真正换用途时重核受影响项，但原值保留。context_change用于完整方向选择；不按字符串不同判断换场景。
selected_option按当前推荐序号/名称/功能定位。selection_mode=full只明确整个方向采用；partial仅局部。采用方向的字段来自提案，不提取成用户updates。拒绝/采用的引用必须来自当前展示版本；多个匹配问差别不能冒选。
推荐初次方向可以完整；已采用后guidance_mode=fill_gap仅补当前缺口，用户明确换方向才new_direction。解释可explain。“可以下一步了吗”用progress，不自动confirm。“先别确认，先解释/给方案/展示假设限制”是继续问答，用explain/recommend，不是pause；pause_scope=conversation才停止交流；暂不批准但继续提问时pause_scope=approval并用explain/recommend，不能停止回答问题。请求展示提案不是复制引用。确认伴真正内容修改先展示新版本；仅重述已展示的相同内容不算修改。粗研究目的已有值时，“筛选还是探索未定”不等于撤回研究目的；未知的是方法分类，保留现有目的。
unknown没提供或本人尚不知道，none明确无预设，open交后续设计确定，unclear是已给内容存在两种互斥解释，绝不用于单纯缺信息。“具体条件不清楚/不知道”保持unknown；“条件待定/后续再说”用open，不能要求第一模块给数值单位。不能因为科学效果未核验把清楚意图标unclear。约束区分hard/preference；没有约束不自动撤回现有硬约束。性价比需要成本口径和性能关注点，不能保证最优。
完整度：对象/基本用途/关注点清楚即可整理研究目的、粗范围和拟功能；候选/基体/条件/评价方法留研究设计。用户说详细指标、阻隔对象或测试标准未知时，保留已经确定的粗关注点，不能把target_performance改为unknown/unclear，也不输出找不到目标的update。不要添加“必须明确指标/候选才能继续”的issue。独立矛盾、对象用途歧义和未同意扩大仍阻断。resolve_issue_ids只指已解决的问题；reaffirm_fields只对应明确重核，不由progress自动批准。
温度temperatures数值单位来自原话，禁止混用℃/K；组分要求predicate保留否定。具体机制/方法/参数主动输入可reference_note未核验，引用提案不能变用户参考。
domain按对象+用途+目的：materials_application材料用途；drug_discovery药物先导/药效/靶蛋白筛选首版不支持；basic_research无用途保持草稿；uncertain缺信息。不能凭单一材料词下领域结论。task_type只辅助，可other，不强加范式。
clarification_field/question点名实际缺口并给一个能回答的问题；不索取已可后续设计的细节。输出固定JSON，不追加科学结果或文献。"""

PROPOSAL_PROMPT = GUIDANCE_PROMPT


def _unknown(id=None):
    result = {"status": "unknown", "value": None, "source": None, "quote": None, "turn": None}
    if id:
        result["id"] = id
    return result


def new_state():
    return {"contract_version": CONTRACT_VERSION, "rule_version": RULE_VERSION,
            "fields": {k: [_unknown(k + "-unknown")] if k in LIST_FIELDS else _unknown() for k in FIELDS},
            "turns": [], "task_type": None, "domain": "uncertain", "asked_field": None,
            "history": [], "reference_notes": [], "issues": [], "sources": {},
            "recommendation_set": None, "recommendation_history": [], "recommendation_revision": 0,
            "proposals": {}, "stage": "clarifying",
            "draft_revision": 0, "draft_id": "D0", "draft": None, "confirmed_request": None}


def normalize_state(state):
    if state.get("contract_version") == CONTRACT_VERSION:
        for key, value in new_state().items():
            state.setdefault(key, value)
        return state
    old = deepcopy(state)
    converted = new_state()
    converted["turns"] = list(old.get("turns", []))
    converted["legacy_fields"] = deepcopy(old.get("fields", {}))
    converted["legacy_contract_version"] = old.get("contract_version", 1)
    names = {"material": "research_object", "conditions": "work_conditions", "metric": "target_performance"}
    for key, value in old.get("fields", {}).items():
        field = names.get(key, key)
        if field not in FIELDS:
            continue
        items = value if isinstance(value, list) else [value]
        for i, entry in enumerate(items):
            entry.setdefault("source", "legacy_unknown")
            if field in LIST_FIELDS:
                entry.setdefault("id", field + "-legacy-" + str(i))
            if entry.get("status") == "specified":
                entry["needs_review"] = True
            if entry.get("source") == "confirmed_suggestion" and not entry.get("recommendation_ref"):
                entry["source"] = "legacy_suggestion"
        converted["fields"][field] = items if field in LIST_FIELDS else items[0]
    converted["reference_notes"] = deepcopy(old.get("reference_notes", []))
    converted["sources"] = deepcopy(old.get("sources", {}))
    converted["proposals"] = deepcopy(old.get("proposals", {}))
    converted["history"] = deepcopy(old.get("history", []))
    converted["issues"] = deepcopy(old.get("issues", []))
    converted["task_type"] = old.get("task_type")
    for value in converted["fields"].values():
        for entry in value if isinstance(value, list) else [value]:
            if entry.get("source") == "confirmed_suggestion":
                entry["legacy_provenance"] = deepcopy(entry)
                entry["source"] = "legacy_suggestion"
    converted["domain"] = domain_from_fields(converted["fields"])
    state.clear()
    state.update(converted)
    return state


def extract(client, model, state, text):
    context = build_context(state, text)
    options = {"extra_body": {"thinking": {"type": "disabled"}}} if os.getenv(
        "LAPIS_BASE_URL", "https://api.deepseek.com").startswith("https://api.deepseek.com") else {}
    return client.create(model=model, response_model=Extraction,
                         messages=[{"role": "system", "content": SYSTEM_PROMPT},
                                   {"role": "user", "content": json.dumps(context, ensure_ascii=False)}],
                         max_retries=2, max_tokens=2600, temperature=0, **options)


def contextual_reply(state, text):
    if proposal_echo(state, text):
        return Extraction(actions=["quote"], summary="引用原提案；来源及当前已采用内容保持不变")
    field = state.get("asked_field")
    if field in FIELDS and text in {"没有", "无", "暂无", "不知道", "不清楚", "不确定", "没想好", "交给研究设计确定"}:
        status = "none" if text in {"没有", "无", "暂无"} else "open"
        return Extraction(actions=["unsure"] if status == "open" else ["inform"],
                          updates=[Update(field=field, status=status, quote=text, action="update")])
    return None


def _fingerprint(state):
    return content_hash({k: state[k] for k in ("fields", "reference_notes", "domain", "issues", "sources", "task_type", "proposals")})


def _touch(state, before):
    changed = _fingerprint(state) != before
    if changed:
        state["draft_revision"] += 1
        state["draft_id"] = "D" + str(state["draft_revision"]) + "-" + _fingerprint(state)[:12]
        state["stage"] = "clarifying"
        state["draft"] = None
    return changed


def _record(item, turn, id=None, state=None):
    entry = {"status": item.status, "value": item.value, "quote": item.quote, "source": "user", "turn": turn}
    if id:
        entry["id"] = id
    if item.field == "target_performance":
        entry["direction"] = item.direction
    if item.field == "constraints":
        entry["strength"] = item.strength
        if item.predicate:
            entry["predicate"] = item.predicate.model_dump()
    if item.basis_refs:
        entry["basis_refs"] = list(item.basis_refs)
    if item.temperatures:
        entry["temperatures"] = [x.model_dump() for x in item.temperatures]
    if state and item.status == "specified":
        rec=state.get("recommendation_set") or {}
        option=next((o for o in rec.get("options",[]) if o["id"] in item.basis_refs),None)
        if option and not any(i.id == item.intent_ref and i.kind == "edit" for i in state.get("_intents",[])) and item.value is not None and item.value not in item.quote:
            _remember_proposal(state,rec,option)
            entry.update(source="system_suggestion",quote=None,turn=None,suggestion_origin=option.get("suggestion_origin","model"),evidence_status=option.get("evidence_status","unverified"),recommendation_ref={"id":rec["id"],"version":rec["version"],"direction_id":option["id"]},source_refs=list(option.get("source_refs",[])))
            if not any(h.get("direction_id")==option["id"] and h.get("status") in {"accepted","focused"} for h in state["recommendation_history"]):
                state["recommendation_history"].append({"set_id":rec["id"],"version":rec["version"],"direction_id":option["id"],"status":"focused","quote":state["turns"][-1]})
    return entry


def _remember_proposal(state,rec,option):
    state["sources"].update(deepcopy(rec.get("sources",{})))
    record=state["proposals"].setdefault(rec["id"],{"version":rec["version"],"draft_id":rec["draft_id"],"generation":deepcopy(rec.get("generation",{"origin":"catalogue"})),"direction_ids":[o["id"] for o in rec["options"]],"content_sha256":content_hash(rec),"review":{}})
    record["review"][option["id"]]={key:deepcopy(option.get(key,[])) for key in ("label","reason","assumptions","limitations","clarifications","source_refs")}


def _same(a, b):
    return all(a.get(k) == b.get(k) for k in
               ("status", "value", "direction", "strength", "predicate", "temperatures", "source")) and not a.get("needs_review")


def _edit_issue(state, field, message, text):
    id = "edit-" + str(uuid4())
    state["issues"].append({"id": id, "field": field, "kind": "ambiguity",
                            "message": message, "quote": text, "status": "open"})


def _merge(state, text, extraction, context=None):
    updates = [u for u in extraction.updates if u.quote in text]
    grouped = {}
    for update in updates:
        grouped.setdefault(update.field, []).append(update)
    major = any(field in grouped and state["fields"][field].get("status") == "specified" and any(
        u.status == "specified" and u.value != state["fields"][field].get("value") and u.change_relation not in {"restatement", "refinement"} for u in grouped[field])
        for field in ("research_object", "application", "research_scope"))
    if major:
        for field in ("work_conditions", "target_performance", "material_function", "constraints"):
            if field not in grouped:
                value = state["fields"][field]
                for entry in value if isinstance(value, list) else [value]:
                    if entry.get("status") == "specified":
                        entry["needs_review"] = True
    turn = len(state["turns"])
    for field, items in grouped.items():
        if field == "task_type":
            value = items[-1].value
            if value in TASK_TYPES | {"multiple_tasks", "out_of_scope"}:
                state["task_type"] = value
            continue
        if field == "reference_note":
            current = state["reference_notes"]
        else:
            current = state["fields"][field]
        before = deepcopy(current)
        if field in SCALAR_FIELDS:
            entry = _record(items[-1], turn, state=state)
            if not _same(current, entry):
                state["fields"][field] = entry
        else:
            current = deepcopy(current)
            if any(item.action == "replace" for item in items):
                current = []
            for item in items:
                name = (item.target_value or item.value or "").strip().casefold()
                matches = [e for e in current if (item.target_id and e.get("id") == item.target_id)
                           or (not item.target_id and name and (e.get("value") or "").strip().casefold() == name)]
                # A single unresolved placeholder is the answer to the previous question.
                if item.action == "update" and not matches:
                    pending = [e for e in current if e.get("status") in {"unknown", "unclear", "open", "none"}]
                    if len(pending) == 1:
                        matches = pending
                if item.action == "remove":
                    if len(matches) != 1:
                        if not matches and field == "constraints" and not any(e.get("status") == "specified" for e in current):
                            continue
                        _edit_issue(state, field, "撤回项无法唯一匹配，请说明要撤回哪一项：" + "、".join(e.get("value") or "未明确项" for e in current) + "。", item.quote)
                        continue
                    matched = matches[0]
                    if field == "reference_note":
                        matched["active"] = False
                        matched["withdrawn_turn"] = turn
                        matched["withdrawal_quote"] = item.quote
                    else:
                        current.remove(matched)
                    continue
                if len(matches) > 1:
                    _edit_issue(state, field, "更新项无法唯一匹配，请说明要修改哪一项：" + "、".join(e.get("value") or "未明确项" for e in current) + "。", item.quote)
                    continue
                if item.action == "update" and not matches:
                    _edit_issue(state, field, "没有找到要更新的项，请选择条目或明确新增。", item.quote)
                    continue
                id = matches[0].get("id") if matches else field + "-" + uuid4().hex[:10]
                entry = _record(item, turn, id, state)
                if item.action == "update" and matches and item.status == "specified":
                    for key in ("value", "direction", "strength", "predicate", "temperatures"):
                        if entry.get(key) is None and key in matches[0]:
                            entry[key] = deepcopy(matches[0][key])
                if field == "reference_note":
                    entry.update(category=item.category or "other", active=True,
                                 status="unverified_user_reference")
                if matches:
                    if not _same(matches[0], entry):
                        current[current.index(matches[0])] = entry
                else:
                    current = [e for e in current if e.get("status") != "unknown"]
                    current.append(entry)
            if field == "reference_note":
                state["reference_notes"] = current
            else:
                state["fields"][field] = current or [_unknown(field + "-unknown")]
        after = state["reference_notes"] if field == "reference_note" else state["fields"][field]
        if before != after:
            state["history"].append({"field": field, "before": before, "after": deepcopy(after), "turn": turn})
    for field in extraction.reaffirm_fields:
        if (context or {}).get("draft_id") != state["draft_id"]:
            continue
        if field in FIELDS:
            values = state["fields"][field]
            before = deepcopy(values)
            for entry in values if isinstance(values, list) else [values]:
                intent_reaffirmed = field not in {"work_conditions", "constraints"} and entry.get("status") == "open" and entry.get("value") and (field != "target_performance" or entry.get("direction"))
                if entry.get("source") == "system_suggestion":
                    entry.update(source="confirmed_suggestion", selection_quote=text, confirmed_turn=turn)
                if entry.get("needs_review") or intent_reaffirmed:
                    entry.pop("needs_review", None)
                    if intent_reaffirmed:
                        entry["status"] = "specified"
                    entry["review_quote"] = text
                    entry["review_turn"] = turn
                    if (entry.get("source") or "").startswith("legacy_"):
                        entry["legacy_provenance"] = {k: entry.get(k) for k in ("source", "quote", "turn")}
                        entry.update(source="user", quote=text, turn=turn)
            if before != values:
                state["history"].append({"field": field, "before": before, "after": deepcopy(values), "turn": turn, "reason": "explicit_reaffirmation"})
    for issue in state["issues"]:
        field = issue.get("field")
        superseded = issue.get("kind") == "ambiguity" and field in SCALAR_FIELDS and field in grouped and state["fields"][field].get("status") == "specified" and any(u.status == "specified" for u in grouped[field])
        if superseded:
            issue.update(status="resolved", resolution_quote=text, resolved_turn=turn, resolution_reason="explicit_scalar_replacement")
        if issue["id"] in extraction.resolve_issue_ids and issue.get("field") in grouped and any(
                h["field"] == issue["field"] and h["turn"] == turn for h in state["history"]):
            issue["status"] = "resolved"
            issue["resolution_quote"] = text
            issue["resolved_turn"] = turn
    for issue in extraction.issues:
        if issue.quote and issue.quote in text:
            state["issues"].append({**issue.model_dump(), "id": "semantic-" + uuid4().hex,
                                    "status": "open", "turn": turn})
    proposed = extraction.domain if extraction.domain_quote and extraction.domain_quote in text else None
    state["domain"] = domain_from_fields(state["fields"], proposed or (None if major or state["domain"] in {"uncertain", "basic_research"} else state["domain"]))
    if state["task_type"] == "out_of_scope":
        state["domain"] = "non_research"


def _ground_actions(state, extraction):
    """Do not let model-expanded option text overwrite its catalog provenance."""
    covered = []
    rec = state.get("recommendation_set")
    if "select" in extraction.actions and extraction.selection_mode == "full" and rec and extraction.selected_option and 1 <= extraction.selected_option <= len(rec["options"]):
        proposed = rec["options"][extraction.selected_option - 1]["fields"]
        retained = []
        for item in extraction.updates:
            values = proposed.get(item.field)
            values = values if isinstance(values, list) else [values]
            same = item.action != "remove" and item.value is not None and any(isinstance(v,dict) and item.value == v.get("value") and item.status == v.get("status", "specified") and (item.field != "target_performance" or item.direction == v.get("direction")) and (item.field != "constraints" or item.strength == v.get("strength")) for v in values)
            if same:
                covered.append({"operation":item.model_dump(),"decision":"covered_by_adoption","reason":"字段随已选提案核对，保留提案来源，不另作为用户原话"})
            else:
                retained.append(item)
        extraction = extraction.model_copy(update={"updates":retained})
    extraction, decisions = check_operations(state, state.get("_input_text", ""), extraction)
    decisions = covered + decisions
    state["_operation_decisions"] = decisions
    state["_intents"] = extraction.intents
    rec = state.get("recommendation_set")
    adopted = {}
    if "select" in extraction.actions and extraction.selection_mode == "full" and rec and extraction.selected_option and 1 <= extraction.selected_option <= len(rec["options"]):
        adopted = rec["options"][extraction.selected_option - 1]["fields"]
    retained = []
    for item in extraction.updates:
        # Partial function choices cannot silently authorize unrelated proposed context.
        if not extraction.intents and extraction.selection_mode == "partial" and item.field in {"application", "research_object", "research_scope", "work_conditions", "constraints"} and item.value and item.value not in item.quote:
            if item.field == "research_object" and state.get("asked_field") == "research_object" and item.value == state["fields"]["research_object"].get("value"):
                pass
            elif item.field == "research_scope" and state.get("asked_field") == "research_scope":
                item = item.model_copy(update={"value":item.quote})
            else:
                continue
        current = state["fields"].get(item.field)
        if item.field in extraction.reaffirm_fields and isinstance(current, dict) and current.get("value") and re.search(r"沿用|保留|重核", item.quote) and (item.value is None or item.value == current.get("value")):
            continue
        proposed = adopted.get(item.field)
        values = proposed if isinstance(proposed, list) else [proposed if isinstance(proposed, dict) else {"value": proposed}] if proposed is not None else []
        selection_only = re.fullmatch(r"(?:我)?(?:采用|选择|选|就用|用|按)(?:第?[一二三四1234](?:个|条)?|这个|该|上述)(?:方向|方案|建议|研究)?", item.quote.strip())
        if "select" in extraction.actions and selection_only:
            continue
        if item.action != "remove" and item.value is not None and any(
                item.value == e.get("value") and item.status == e.get("status", "specified") and (item.field != "target_performance" or item.direction == e.get("direction"))
                and (item.field != "constraints" or item.strength == e.get("strength")) for e in values):
            continue
        if item.field == "constraints":
            match = re.search(r"(?:不再考虑|不考虑|撤回|取消)\s*([^，。；,;]+)", item.quote)
            if match:
                name = re.sub(r"(?:约束|限制|要求)$", "", match.group(1)).strip()
                current = state["fields"]["constraints"]
                exact = [e for e in current if (item.target_id and e.get("id") == item.target_id)
                         or (not item.target_id and item.target_value and e.get("value") == item.target_value)]
                proposed_entries = adopted.get("constraints") or []
                proposed_match = [e for e in proposed_entries if item.target_value and e.get("value") == item.target_value]
                if len(exact) == 1:
                    item = item.model_copy(update={"action": "remove", "status": "none", "target_id": exact[0]["id"]})
                elif len(proposed_match) == 1:
                    item = item.model_copy(update={"action": "remove", "status": "none", "target_id": None})
                else:
                    found = [e for e in current if name and name in (e.get("value") or "")]
                    item = item.model_copy(update={"action": "remove", "status": "none", "target_value": name,
                                                  "target_id": found[0]["id"] if len(found) == 1 else item.target_id})
        retained.append(item)
    return extraction.model_copy(update={"updates": retained})


def _select(state, extraction, text, context):
    rec = state.get("recommendation_set")
    expected = context.get("recommendation_ref")
    if rec and extraction.selected_option and 1 <= extraction.selected_option <= len(rec["options"]):
        option = rec["options"][extraction.selected_option - 1]
        # Reaffirming the live direction keeps later refinements; stale new choices still fail below.
        if context.get("draft_id") == state["draft_id"] and expected == {"id":rec["id"],"version":rec["version"]} and all((state["fields"][field].get("recommendation_ref") or {}).get("direction_id") == option["id"] for field in ("research_object", "application")):
            return None
    if not rec or expected != {"id": rec["id"], "version": rec["version"]} or rec["draft_id"] != state["draft_id"]:
        return "推荐已经变化，请请求重新展示方向后再选择。"
    index = extraction.selected_option
    if not index or not 1 <= index <= len(rec["options"]):
        return "请明确采用哪一个方向。"
    option = rec["options"][index - 1]
    if not option.get("reconsidered") and any((x["direction_id"] == option["id"] or x.get("fingerprint") == direction_fingerprint(option)) and x["status"] == "rejected"
           for x in state["recommendation_history"]):
        return "这条方向曾被拒绝，请先重新审查方向。"
    major = extraction.context_change not in {"restatement", "refinement"} and any(field in option["fields"] and state["fields"][field].get("status") == "specified" and
                state["fields"][field].get("value") != (option["fields"][field].get("value") if isinstance(option["fields"][field], dict) else option["fields"][field])
                for field in ("research_object", "application", "research_scope"))
    if major:
        for field in ("work_conditions", "constraints", "target_performance", "material_function"):
            for entry in state["fields"][field] if isinstance(state["fields"][field], list) else [state["fields"][field]]:
                if entry.get("status") == "specified":
                    entry["needs_review"] = True
    _remember_proposal(state,rec,option)
    state["sources"].update(deepcopy(rec["sources"]))
    for field, value in option["fields"].items():
        before = deepcopy(state["fields"][field])
        entries = value if isinstance(value, list) else [value if isinstance(value, dict) else {"value": value}]
        converted = []
        for item in entries:
            entry = {**item, "status": item.get("status", "specified"), "value": item.get("value"), "source": "confirmed_suggestion",
                     "suggestion_origin": option.get("suggestion_origin", "catalogue"),
                     "evidence_status": option.get("evidence_status", "source_checked"),
                     "quote": None, "turn": None, "suggested_after_turn": len(state["turns"]) - 1,
                     "selection_quote": text, "confirmed_turn": len(state["turns"]),
                     "recommendation_ref": {"id": rec["id"], "version": rec["version"], "direction_id": option["id"]},
                     "source_refs": list(option["source_refs"])}
            if field in LIST_FIELDS:
                entry.update(id=field + "-" + uuid4().hex[:10], direction=item.get("direction"))
            converted.append(entry)
        if field in LIST_FIELDS:
            preserved = [e for e in before if e.get("status") in {"specified", "unclear"} and e.get("source") in {"user", "confirmed_suggestion"}]
            converted = preserved + [e for e in converted if (not preserved or e.get("status") != "unknown") and not any(p.get("value") == e.get("value") for p in preserved)]
        elif before.get("status") == "specified" and field == "work_conditions" and before.get("source") == "user":
            converted = [before]
        state["fields"][field] = converted if field in LIST_FIELDS else converted[0]
        state["history"].append({"field": field, "before": before, "after": deepcopy(state["fields"][field]),
                                 "turn": len(state["turns"]), "reason": "recommendation_selected"})
        for issue in state["issues"]:
            entries = before if isinstance(before, list) else [before]
            if issue.get("status") != "resolved" and issue.get("kind") == "ambiguity" and issue.get("field") == field and all(e.get("status") != "specified" for e in entries):
                issue.update(status="resolved", resolution_quote=text, resolved_turn=len(state["turns"]),
                             resolution_reason="adopted_reviewed_direction")
    state["domain"] = "materials_application"
    state["recommendation_history"].append({"set_id": rec["id"], "version": rec["version"],
                                            "direction_id": option["id"], "status": "accepted", "quote": text})


def _reject(state, extraction, text, context):
    refs = {ref for i in extraction.intents if i.kind == "reject" for ref in i.target_refs}
    latest = {h["direction_id"]:h["status"] for h in state["recommendation_history"] if h.get("status") in {"accepted", "rejected"}}
    already_rejected = {ref for ref,status in latest.items() if status == "rejected"}
    if refs and refs.issubset(already_rejected):
        return None  # Restating an existing rejection does not change the reviewed request.
    rec = state.get("recommendation_set")
    if not rec or context.get("recommendation_ref") != {"id": rec["id"], "version": rec["version"]} or context.get("draft_id") != state["draft_id"]:
        return "请先查看当前方向或草稿，再明确拒绝哪条建议。"
    options = rec["options"]
    rejection_intents = [i for i in extraction.intents if i.kind == "reject"]
    if rejection_intents:
        refs = {ref for i in rejection_intents for ref in i.target_refs}
        options = [o for o in options if o["id"] in refs or rec["id"] in refs]
        if not options:
            return "拒绝未指向当前方向；请说明哪项不用，已有选择已保留。"
    elif extraction.selected_option and 1 <= extraction.selected_option <= len(options):
        options = [options[extraction.selected_option - 1]]
    rejected = {x["id"] for x in options}
    for option in options:
        state["recommendation_history"].append({"set_id": rec["id"], "version": rec["version"],
                                                "direction_id": option["id"], "status": "rejected", "quote": text,
                                                "label": option["label"], "fields": deepcopy(option["fields"]), "fingerprint": direction_fingerprint(option)})
    for field, value in state["fields"].items():
        before = deepcopy(value)
        entries = value if isinstance(value, list) else [value]
        kept = [x for x in entries if (x.get("recommendation_ref") or {}).get("direction_id") not in rejected]
        state["fields"][field] = (kept or [_unknown(field + "-unknown")]) if field in LIST_FIELDS else (kept[0] if kept else _unknown())
        if before != state["fields"][field]:
            state["history"].append({"field": field, "before": before, "after": deepcopy(state["fields"][field]),
                                     "turn": len(state["turns"]), "reason": "recommendation_rejected"})
    if rejected == {o["id"] for o in rec["options"]}:
        state["recommendation_set"] = None


def make_draft(state):
    return {"contract_version": CONTRACT_VERSION, "rule_version": RULE_VERSION,
            "original_intent": state["turns"][0] if state["turns"] else "",
            "draft_revision": state["draft_revision"], "draft_id": state["draft_id"],
            "fields": deepcopy(state["fields"]), "task_pattern": state["task_type"],
            "domain": state["domain"], "issues": deepcopy(state["issues"]),
            "reference_notes": deepcopy(state["reference_notes"]), "sources": deepcopy(state["sources"]),
            "proposals": deepcopy(state["proposals"])}


def _result(state, changed=False, guide=False, text="", notice=None, generated=None):
    draft = make_draft(state)
    issues = request_issues(draft)
    if state.get("task_type") == "multiple_tasks":
        issues.insert(0, {"kind": "ambiguity", "field": "purpose", "message": "请先聚焦一个对象和研究目的。"})
    if state["domain"] in {"drug_discovery", "non_research"}:
        status = "unsupported"
        question = "首版只受理材料应用研究，暂不支持药物筛选；原意图已保留。"
    elif issues:
        status = "needs_clarification"
        actionable = [x for x in issues if x["kind"] != "missing"]
        issue = (actionable or issues)[0]
        state["asked_field"] = issue["field"]
        known = state["fields"].get(issue["field"])
        entries = known if isinstance(known, list) else [known or {}]
        values = "、".join(e.get("value") or "" for e in entries if e.get("value"))
        question = LABELS.get(issue["field"], "研究信息") + ("（当前：" + values + "）" if values else "") + "：" + QUESTIONS.get(issue["field"], issue["message"])
        if issue["kind"] not in {"missing", "domain", "ambiguity"} or issue.get("kind") == "ambiguity" and not issue["message"].startswith("请澄清"):
            question = issue["message"] + " " + question
    else:
        status = "ready_for_design" if state["stage"] == "ready_for_design" else "needs_confirmation"
        question = None if status == "ready_for_design" else "请核对以下八项规约及提案假设、限制，确认后进入研究设计；也可以直接修改。"
        state["asked_field"] = None
        if status == "needs_confirmation":
            state["stage"] = "review"
            state["draft"] = deepcopy(draft)
    rec = None
    if guide and status != "unsupported":
        rec = generated
        state["recommendation_set"] = rec
        if rec:
            state["recommendation_revision"] = rec["version"]
            question = rec.get("explanation", "") + (" 您可以选择、修改或拒绝其中的方向。" if rec.get("options") else "")
            if status != "ready_for_design":
                status = "needs_guidance"
                state["stage"] = "clarifying"
                state["draft"] = None
    pending_question = state.get("clarification_question") or {}
    if issues and not guide and pending_question.get("draft_id") == state["draft_id"] and any(i.get("field") == pending_question.get("field") for i in issues):
        state["asked_field"] = pending_question["field"]
        question = pending_question["question"]
    result = {"intake_status": status, "ready": status == "ready_for_design",
              "ready_for_design": status == "ready_for_design", "request": draft,
              "calculation_status": "pending_research_design", "next_question": question,
              "blocking_issues": issues, "recommendations": rec, "content_changed": changed,
              "input_context": {"draft_id": state["draft_id"]},
              "changes": [h["field"] for h in state["history"] if changed and h["turn"] == len(state["turns"])],
              "needs_review_fields": []}
    result["needs_review_fields"] = [field for field, value in state["fields"].items()
        if any(e.get("needs_review") for e in (value if isinstance(value, list) else [value]))]
    if state.get("recommendation_set"):
        r = state["recommendation_set"]
        result["input_context"]["recommendation_ref"] = {"id": r["id"], "version": r["version"]}
    if status == "ready_for_design":
        result["request"] = deepcopy(state["confirmed_request"])
        result["already_confirmed"] = True
    if notice:
        result["notice"] = notice
    return result


def _confirm(state, context):
    if re.search(r"(?:不|别|不要|暂不|先不|不想)\s*(?:再|先|要|想|准备|打算|进行)?(?:确认|同意|继续)", state["turns"][-1]):
        return _result(state, notice="本轮未授权确认，草稿已保留。")
    if state["stage"] == "ready_for_design":
        return _result(state, notice=None if context.get("draft_id") == state["draft_id"] else "当前请求已经确认，本轮没有新增确认。")
    draft = make_draft(state)
    if request_issues(draft):
        return _result(state, notice="当前规约仍有下述缺口，补充后再核对确认；可用 /show 查看草稿。")
    if context.get("review_hash") and context["review_hash"] != state.get("review_hash"):
        return _result(state, notice="展示版本已经变化，请核对当前规约。")
    if context.get("draft_id") != state["draft_id"] or (state["stage"] != "review" and context.get("reviewed_draft_id") != state["draft_id"]):
        return _result(state, notice="已展示当前完整规约，请核对后明确确认。")
    adopt_summaries(state, state["turns"][-1])
    draft = make_draft(state)
    validate_current_request(draft)
    state["confirmed_request"] = deepcopy(draft)
    state["stage"] = "ready_for_design"
    result = _result(state)
    result["confirmation_event"] = True
    return result


def process_turn(state, text, extraction):
    """Deterministic, testable update path without automatic confirmation."""
    normalize_state(state)
    state["_input_text"] = text
    extraction = _ground_actions(state, extraction)
    before = _fingerprint(state)
    state["turns"].append(text.strip())
    _merge(state, text, extraction)
    prepare_summaries(state, None)
    return _result(state, _touch(state, before))


def _handle_turn(client, model, state, text, input_context=None, operation_id=None):
    normalize_state(state)
    text = text.strip()
    if not text:
        raise ValueError("输入不能为空")
    context = input_context or {}
    state["_input_text"] = text
    state["_generation_operation_id"] = operation_id
    started = time.perf_counter()
    if text in CONFIRM_WORDS:
        state["turns"].append(text)
        result = _confirm(state, context)
    elif text in {"先不确认", "暂不确认", "暂停"}:
        state["turns"].append(text)
        result = _result(state, notice="草稿已保留，尚未新增确认；可以继续解释、修改或确认。")
    else:
        extraction = contextual_reply(state, text) or extract(client, model, state, text)
        state["_raw_interpretation"] = extraction.model_dump()
        extraction = _ground_actions(state, extraction)
        before = _fingerprint(state)
        state["turns"].append(text)
        notice = None
        if "select" in extraction.actions and extraction.selection_mode == "full":
            notice = _select(state, extraction, text, context)
        if "reject" in extraction.actions or text in {"不采用", "不用建议"}:
            notice = _reject(state, extraction, text, context)
        action_notice = notice
        _merge(state, text, extraction, context)
        prepare_summaries(state, operation_id)
        changed = _touch(state, before)
        guide = bool(set(extraction.actions) & {"recommend", "explain", "unsure", "reject", "delegate"})
        state["guidance_mode"] = extraction.guidance_mode
        if guide and any(h.get("status") in {"accepted","focused"} for h in state["recommendation_history"]) and extraction.guidance_mode != "new_direction":
            open_issues = request_issues(make_draft(state))
            if open_issues:
                state["asked_field"] = open_issues[0]["field"]
            else:
                guide = False
                notice = "已有方向已保留；当前可以核对规约。若要换方向，请明确说明。"
        if "pause" in extraction.actions and extraction.pause_scope == "conversation":
            guide = False
        generated = generate_recommendations(client, model, state, text, operation_id) if guide and state["domain"] not in {"drug_discovery", "non_research"} else None
        result = _result(state, changed, guide, text, notice, generated)
        result["interpretation"] = state.get("_raw_interpretation", extraction.model_dump())
        result["operation_decisions"] = state.get("_operation_decisions", [])
        result["important_changes"] = [LABELS.get(d["operation"]["field"],d["operation"]["field"]) + "：" + d["operation"]["action"] + "；原话：" + d["operation"]["quote"] for d in result["operation_decisions"] if d["decision"] == "accepted" and (d["operation"]["action"] in {"remove","replace"} or d["operation"]["field"] == "research_scope")]
        if "quote" in extraction.actions:
            result["notice"] = "已识别为原提案引用，未新增用户要求或重新选择方向。"
        if extraction.clarification_question and any(i.get("field") == extraction.clarification_field for i in result["blocking_issues"]) and not guide:
            state["asked_field"] = extraction.clarification_field
            result["next_question"] = extraction.clarification_question if extraction.clarification_question.startswith(LABELS.get(extraction.clarification_field, "研究信息") + "：") else LABELS.get(extraction.clarification_field, "研究信息") + "：" + extraction.clarification_question
            state["clarification_question"] = {"draft_id":state["draft_id"], "field":extraction.clarification_field, "question":result["next_question"]}
        if "progress" in extraction.actions:
            result = _result(state, changed, notice="确认规约后才进入研究设计，当前没有授权计算。" if not request_issues(make_draft(state)) else None)
        if "confirm" in extraction.actions and not changed and not action_notice and "edit" not in extraction.actions and "pause" not in extraction.actions and not any(d["decision"] == "not_applied" for d in state.get("_operation_decisions", [])):
            if re.search(r"我(?:确认|同意)(?!一下|下)|(?:确认|同意)(?:这份|本版|当前|上述|以上)?(?:研究)?(?:规约|请求|意图)|^(?:确认|同意)(?!一下|下)", text):
                result = _confirm(state, context)
            else:
                result = _result(state, notice="请明确回复‘确认’以采用本版规约；进度询问没有新增确认。")
        elif "confirm" in extraction.actions:
            if result.get("ready_for_design"):
                state["stage"] = "review"
                result = _result(state, changed)
            result["notice"] = "本轮含修改或选择，请核对更新后的完整草稿，再确认。"
    if text in {"先不确认", "暂不确认", "暂停"} or ("extraction" in locals() and "pause" in extraction.actions and extraction.pause_scope == "conversation"):
        state["stage"] = "paused"
        result.update(intake_status="paused", ready=False, ready_for_design=False, next_question=None, notice="草稿已保存，当前暂停；恢复后可继续补充或核对，没有新增确认。")
    if "extraction" in locals():
        result["interpretation"] = state.get("_raw_interpretation", extraction.model_dump())
        result["operation_decisions"] = state.get("_operation_decisions", [])
        result["important_changes"] = [LABELS.get(d["operation"]["field"],d["operation"]["field"]) + "：" + d["operation"]["action"] + "；原话：" + d["operation"]["quote"] for d in result["operation_decisions"] if d["decision"] == "accepted" and (d["operation"]["action"] in {"remove","replace"} or d["operation"]["field"] == "research_scope")]
    result["metadata"] = {"model": model, "rule_version": RULE_VERSION, "contract_version": CONTRACT_VERSION,
                          "elapsed_seconds": round(time.perf_counter() - started, 3),
                          "runtime": runtime_fingerprint(),
                          "catalog_version": (state.get("recommendation_set") or {}).get("catalog_version")}
    return result


def handle_turn(client, model, state, text, input_context=None, operation_id=None):
    # Commit the turn only after extraction and proposal validation both succeed.
    working = deepcopy(state)
    result = _handle_turn(client, model, working, text, input_context, operation_id)
    from lapis import attach_view
    attach_view(working, result)
    for key in [key for key in working if key.startswith("_")]:
        working.pop(key)
    state.clear()
    state.update(working)
    return result


def preview_intake(state):
    """Render a legacy draft without rewriting its stored historical payload."""
    preview = deepcopy(state)
    normalize_state(preview)
    return _result(preview, notice="旧版记录仍保留；进入新版设计前请逐项重新核对并确认。")


def main():
    from lapis import run_chat
    run_chat(None, "researcher")


if __name__ == "__main__":
    main()
