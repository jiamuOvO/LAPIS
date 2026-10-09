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
                            validate_research_request_v4)
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
    status: Literal["specified", "none", "open", "unclear"]
    value: str | None = None
    quote: str = Field(min_length=1)
    action: Literal["add", "update", "replace", "remove"] = "add"
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


class Extraction(StrictModel):
    actions: list[Literal["inform", "recommend", "explain", "unsure", "select", "reject",
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


SYSTEM_PROMPT = """你是 LAPIS 第一模块的结构化对话提取器。只从本轮原话提取，不编造事实。
八项字段：purpose研究行动/判断；research_object具体材料或体系；application实际用途；
work_conditions服役条件；target_performance性能与定性方向；constraints硬约束/偏好；
research_scope包含/排除范围；material_function材料承担的作用。
actions可同时有多个：inform、recommend请求推荐、explain请求解释、unsure不知道、
select选择方向（整体或局部）、reject拒绝、edit修改、pause先不确认、confirm确认、reaffirm重新核对。
混合表达不能只保留一个动作：“采用第二个方向但先不考虑成本”=select+edit；
“不研究氧化稳定性其他保留”只删除该目标；“确认但成本改为偏好”=confirm+edit。
selected_option由上下文识别序号、名称或功能，不能猜测多个匹配。selection_mode=full仅用户明确采用整个方向；短答或只选功能必须partial，不能自动采用推荐所有用途、对象、假设和其他目标。partial用updates只记录本轮已明确的意图，未明确字段保持原状。
短答要结合当前推荐解释；只选多目标中的一项时用replace目标列表，排除未选择的目标。定性功能如“耐磨作用”已足以将目标性能specified（比较/考察），没有量化指标不应标open。区分研究目的（探索/比较/筛选）、性能（欲考察性质）、材料功能（用途中的角色）和用途（具体场景）。上下文足以明确探索行动时更新旧unclear目的，并resolve_issue_ids；不可仅更新性能而遗留已解决目的问题。不能将选定研究方向当作科学事实。
例如旧目的“研究某材料的生活用途”是unclear，本轮从推荐选定某个功能时，目的可明确为“探索该材料在所选功能中的应用”，这只是行动意图；必须更新purpose、material_function和单一目标，并解决旧目的issue。“包装材料的抗氧化应用”“表面涂层的耐磨应用”已经足以限定第一模块用途，不强求具体包装品种、基材或配方；更细条件交研究设计。不要反复要求用户在未选择的其他功能（如阻隔或再加工）之间重新选择。具体用途尚未选时仍追问，不要把所有候选用途写成specified。不要等用户再次说“探索”才更新已明确的目的。
多个匹配不能select，给出clarification_field及clarification_question，点名可选差别。仍缺具体用途时不自动填所有用途，提问具体用途选择；问题只能采用当前推荐和已知意图，不添加事实断言。clarification_question是一个可回答的问题；不能泛称“这项信息”。若仍有问题，clarification_field对应当前最优先实质缺口，clarification_question带已知意图和具体可选差别，例如材料用途仍宽泛时给已有推荐中的用途选项。
用户在回答asked_field时优先解释为该字段的补充；研究对象可为有边界的材料类别，不要求此时确定小分子/聚合物/具体配方。不因缺具体模型或形态反复新增首模块歧义。功能问题的回答“拟提高某性能”应更新material_function，不仅更新性能或目的。带“待验证意图/不假定有效”的说明是证据限制，不是用户硬约束，不写constraints。必要时用reference_note记录。
“可以下一步了吗”等进度询问用progress（不是confirm，不自动确认）。
selected_option用于定位当前方向；“用途采用刚才的X”“功能沿用X”是字段补充或reaffirm，不是重新select。
采用方向时不要把推荐文本提取为用户原话字段。
每项quote必须是本轮原话连续片段。未提及的字段不输出。已有字段用于理解不能冒充本轮原话。
研究目的应是本轮行动。“设计高电压电池”只是上层意图：应用高电压电池，目的还不清楚，
不要把整个电池当材料，不要造目标“提高电压”。
目标可定性如比较稳定性，无需强求描述符或计算方法。每个目标和约束各一条。
“性价比高”不是单一可计算性能：成本作为待澄清偏好，性能保持unclear，询问优先性能和成本口径；不能自动认定最便宜配方。
指定目标给direction，明确约束给hard/preference；不确定给unclear。
status：none明确无预设，open交研究设计确定，unclear已表达但有歧义。未提供保持unknown。
action：add新增、update更新指定条目、remove撤回；replace仅用户明确替换整个列表。
局部修改用target_id或target_value指向已有条目，保留其他项。删除无需重新填方向/强度。
指定信息必须有value，不能把目标名称只放direction。仅修改已有非空条目的方向/强度可省value。
“目标只比较X/只保留X”是明确替换整个目标列表，用replace，value=X，direction=比较。
“先不考虑X”明确撤回本轮X，不需要询问是否永久撤回。不存在该约束时不新增歧义。
“暂不设置X限制”是本轮撤回X，不是新增X偏好。不要仅因其他限制未知就新增issues。
当前issues若已由本轮明确修改解决，输出其ID到resolve_issue_ids；不能漏掉已解决的旧问题。
数值温度写temperatures，unit必须来自原话；25 K和25℃不能擅自选择或混用。
允许/必须/禁用组分写predicate(target,operator,scope)，保留原始否定句。
任何无法可靠结构化的歧义或矛盾写issues，不用“确认”解决矛盾。
reaffirm_fields仅在用户明确重核或沿用那些字段时给出；不能自行把旧条件认为还适用。
具体机制、方法、力场、参数作为reference_note，category注明，未核验，不是执行许可。
domain根据对象+目的+用途判断：材料应用materials_application；药物先导、药效、药物候选
与靶蛋白结合筛选drug_discovery首版排除；不能仅凭“分子”判材料或“药”误拒医用材料。
缺材料用途的基础问题先basic_research/uncertain，可引导，不能编造用途。
task_type辅助：screening/comparison/mechanism_validation/mechanism_exploration/other。
未知原因可探索不要求假设，多性能不是多独立任务。不同独立任务可标multiple_tasks。
请求推荐/解释时保留原始对象和意图，不输出虚构推荐作为用户字段。
输出结构化JSON。"""
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
    context = {"fields": state["fields"], "issues": state["issues"],
               "asked_field": state["asked_field"], "recommendations": state["recommendation_set"],
               "reference_notes": state["reference_notes"], "input": text}
    options = {"extra_body": {"thinking": {"type": "disabled"}}} if os.getenv(
        "LAPIS_BASE_URL", "https://api.deepseek.com").startswith("https://api.deepseek.com") else {}
    return client.create(model=model, response_model=Extraction,
                         messages=[{"role": "system", "content": SYSTEM_PROMPT},
                                   {"role": "user", "content": json.dumps(context, ensure_ascii=False)}],
                         max_retries=2, max_tokens=2600, temperature=0, **options)


def contextual_reply(state, text):
    field = state.get("asked_field")
    if field in FIELDS and text in {"没有", "无", "暂无", "不知道", "不清楚", "不确定", "没想好", "交给研究设计确定"}:
        status = "none" if text in {"没有", "无", "暂无"} else "open"
        return Extraction(actions=["unsure"] if status == "open" else ["inform"],
                          updates=[Update(field=field, status=status, quote=text, action="update")])
    return None


def _fingerprint(state):
    return content_hash({k: state[k] for k in ("fields", "reference_notes", "domain", "issues", "sources", "task_type")})


def _touch(state, before):
    changed = _fingerprint(state) != before
    if changed:
        state["draft_revision"] += 1
        state["draft_id"] = "D" + str(state["draft_revision"]) + "-" + _fingerprint(state)[:12]
        state["stage"] = "clarifying"
        state["draft"] = None
    return changed


def _record(item, turn, id=None):
    entry = {"status": item.status, "value": item.value, "quote": item.quote, "source": "user", "turn": turn}
    if id:
        entry["id"] = id
    if item.field == "target_performance":
        entry["direction"] = item.direction
    if item.field == "constraints":
        entry["strength"] = item.strength
        if item.predicate:
            entry["predicate"] = item.predicate.model_dump()
    if item.temperatures:
        entry["temperatures"] = [x.model_dump() for x in item.temperatures]
    return entry


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
    major = any(field in grouped and any(
        u.status == "specified" and u.value != state["fields"][field].get("value") for u in grouped[field])
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
            entry = _record(items[-1], turn)
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
                entry = _record(item, turn, id)
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
    rec = state.get("recommendation_set")
    adopted = {}
    if "select" in extraction.actions and extraction.selection_mode == "full" and rec and extraction.selected_option and 1 <= extraction.selected_option <= len(rec["options"]):
        adopted = rec["options"][extraction.selected_option - 1]["fields"]
    retained = []
    for item in extraction.updates:
        # Partial function choices cannot silently authorize unrelated proposed context.
        if extraction.selection_mode == "partial" and item.field in {"application", "research_object", "research_scope", "work_conditions", "constraints"} and item.value and item.value not in item.quote:
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
    if not rec or expected != {"id": rec["id"], "version": rec["version"]} or rec["draft_id"] != state["draft_id"]:
        return "推荐已经变化，请请求重新展示方向后再选择。"
    index = extraction.selected_option
    if not index or not 1 <= index <= len(rec["options"]):
        return "请明确采用哪一个方向。"
    option = rec["options"][index - 1]
    if not option.get("reconsidered") and any((x["direction_id"] == option["id"] or x.get("fingerprint") == direction_fingerprint(option)) and x["status"] == "rejected"
           for x in state["recommendation_history"]):
        return "这条方向曾被拒绝，请先重新审查方向。"
    major = any(field in option["fields"] and state["fields"][field].get("status") == "specified" and
                state["fields"][field].get("value") != (option["fields"][field].get("value") if isinstance(option["fields"][field], dict) else option["fields"][field])
                for field in ("research_object", "application", "research_scope"))
    if major:
        for field in ("work_conditions", "constraints", "target_performance", "material_function"):
            for entry in state["fields"][field] if isinstance(state["fields"][field], list) else [state["fields"][field]]:
                if entry.get("status") == "specified":
                    entry["needs_review"] = True
    state["proposals"][rec["id"]] = {"version": rec["version"], "draft_id": rec["draft_id"],
        "generation": deepcopy(rec.get("generation", {"origin": "catalogue"})),
        "direction_ids": [o["id"] for o in rec["options"]], "content_sha256": content_hash(rec),
        "review": {option["id"]: {key: deepcopy(option.get(key, [])) for key in
                   ("label", "reason", "assumptions", "limitations", "clarifications", "source_refs")}}}
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
    rec = state.get("recommendation_set")
    if not rec or context.get("recommendation_ref") != {"id": rec["id"], "version": rec["version"]} or context.get("draft_id") != state["draft_id"]:
        return "请先查看当前方向或草稿，再明确拒绝哪条建议。"
    options = rec["options"]
    if extraction.selected_option and 1 <= extraction.selected_option <= len(options):
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
    if context.get("draft_id") != state["draft_id"] or (state["stage"] != "review" and context.get("reviewed_draft_id") != state["draft_id"]):
        return _result(state, notice="已展示当前完整规约，请核对后明确确认。")
    validate_research_request_v4(draft)
    state["confirmed_request"] = deepcopy(draft)
    state["stage"] = "ready_for_design"
    result = _result(state)
    result["confirmation_event"] = True
    return result


def process_turn(state, text, extraction):
    """Deterministic, testable update path without automatic confirmation."""
    normalize_state(state)
    before = _fingerprint(state)
    state["turns"].append(text.strip())
    _merge(state, text, extraction)
    return _result(state, _touch(state, before))


def _handle_turn(client, model, state, text, input_context=None, operation_id=None):
    normalize_state(state)
    text = text.strip()
    if not text:
        raise ValueError("输入不能为空")
    context = input_context or {}
    started = time.perf_counter()
    if text in CONFIRM_WORDS:
        state["turns"].append(text)
        result = _confirm(state, context)
    elif text in {"先不确认", "暂不确认", "暂停"}:
        state["turns"].append(text)
        result = _result(state, notice="草稿已保留，尚未新增确认；可以继续解释、修改或确认。")
    else:
        extraction = contextual_reply(state, text) or extract(client, model, state, text)
        extraction = _ground_actions(state, extraction)
        before = _fingerprint(state)
        state["turns"].append(text)
        notice = None
        if "select" in extraction.actions and extraction.selection_mode == "full":
            notice = _select(state, extraction, text, context)
        if "reject" in extraction.actions or text in {"不采用", "不用建议"}:
            notice = _reject(state, extraction, text, context)
        _merge(state, text, extraction, context)
        changed = _touch(state, before)
        guide = bool(set(extraction.actions) & {"recommend", "explain", "unsure", "reject"})
        generated = generate_recommendations(client, model, state, text, operation_id) if guide and state["domain"] not in {"drug_discovery", "non_research"} else None
        result = _result(state, changed, guide, text, notice, generated)
        if extraction.clarification_question and any(i.get("field") == extraction.clarification_field for i in result["blocking_issues"]) and not guide:
            state["asked_field"] = extraction.clarification_field
            result["next_question"] = LABELS.get(extraction.clarification_field, "研究信息") + "：" + extraction.clarification_question
            state["clarification_question"] = {"draft_id":state["draft_id"], "field":extraction.clarification_field, "question":result["next_question"]}
        if "progress" in extraction.actions:
            result = _result(state, changed, notice="确认规约后才进入研究设计，当前没有授权计算。" if not request_issues(make_draft(state)) else None)
        if "confirm" in extraction.actions and not extraction.updates and not changed and not (set(extraction.actions) & {"select", "edit", "reaffirm", "reject"}):
            result = _confirm(state, context)
        elif "confirm" in extraction.actions:
            if result.get("ready_for_design"):
                state["stage"] = "review"
                result = _result(state, changed)
            result["notice"] = "本轮含修改或选择，请核对更新后的完整草稿，再确认。"
    result["metadata"] = {"model": model, "rule_version": RULE_VERSION, "contract_version": CONTRACT_VERSION,
                          "elapsed_seconds": round(time.perf_counter() - started, 3),
                          "catalog_version": (state.get("recommendation_set") or {}).get("catalog_version")}
    return result


def handle_turn(client, model, state, text, input_context=None, operation_id=None):
    # Commit the turn only after extraction and proposal validation both succeed.
    working = deepcopy(state)
    result = _handle_turn(client, model, working, text, input_context, operation_id)
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
