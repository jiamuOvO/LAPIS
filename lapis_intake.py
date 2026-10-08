"""LAPIS intake: incomplete intent, bounded guidance, review and versioned confirmation."""
from __future__ import annotations

from copy import deepcopy
import json
import os
import re
import time
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, Field, model_validator
from lapis_contract import (CONTRACT_VERSION, RULE_VERSION, FIELDS, LIST_FIELDS, CONFIRM_WORDS,
                            content_hash, domain_from_fields, request_issues,
                            validate_research_request_v3)
from lapis_guidance import recommendations, explain_field

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


class Temperature(BaseModel):
    value: str
    unit: Literal["K", "℃", "°C"]


class Predicate(BaseModel):
    target: str
    operator: Literal["require", "forbid", "allow"]
    scope: str = "current"


class Update(BaseModel):
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
        if self.status == "specified" and self.action != "remove":
            if self.field == "target_performance" and not self.direction:
                raise ValueError("性能须有定性方向")
            if self.field == "constraints" and not self.strength:
                raise ValueError("约束须区分硬约束与偏好")
        return self


class Issue(BaseModel):
    field: str
    message: str
    quote: str
    kind: Literal["ambiguity", "conflict"] = "ambiguity"
    entry_ids: list[str] = Field(default_factory=list)


class Extraction(BaseModel):
    actions: list[Literal["inform", "recommend", "explain", "unsure", "select", "reject",
                         "edit", "pause", "confirm", "reaffirm"]] = Field(default_factory=lambda: ["inform"])
    updates: list[Update] = Field(default_factory=list)
    selected_option: int | None = None
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
select采用显示过的方向、reject拒绝、edit修改、pause先不确认、confirm确认、reaffirm重新核对。
混合表达不能只保留一个动作：“采用第二个方向但先不考虑成本”=select+edit；
“不研究氧化稳定性其他保留”只删除该目标；“确认但成本改为偏好”=confirm+edit。
selected_option对应已有推荐的序号，采用时不要把推荐文本提取为用户原话字段。
每项quote必须是本轮原话连续片段。未提及的字段不输出。已有字段用于理解不能冒充本轮原话。
研究目的应是本轮行动。“设计高电压电池”只是上层意图：应用高电压电池，目的还不清楚，
不要把整个电池当材料，不要造目标“提高电压”。
目标可定性如比较稳定性，无需强求描述符或计算方法。每个目标和约束各一条。
指定目标给direction，明确约束给hard/preference；不确定给unclear。
status：none明确无预设，open交研究设计确定，unclear已表达但有歧义。未提供保持unknown。
action：add新增、update更新指定条目、remove撤回；replace仅用户明确替换整个列表。
局部修改用target_id或target_value指向已有条目，保留其他项。删除无需重新填方向/强度。
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
PROPOSAL_PROMPT = "仅从已核查本地目录展示有来源、有限制的待选择研究方向；不自由生成文献。"


def _unknown(id=None):
    result = {"status": "unknown", "value": None, "source": None, "quote": None, "turn": None}
    if id:
        result["id"] = id
    return result


def new_state():
    return {"contract_version": 3, "rule_version": RULE_VERSION,
            "fields": {k: [_unknown(k + "-unknown")] if k in LIST_FIELDS else _unknown() for k in FIELDS},
            "turns": [], "task_type": None, "domain": "uncertain", "asked_field": None,
            "history": [], "reference_notes": [], "issues": [], "sources": {},
            "recommendation_set": None, "recommendation_history": [], "stage": "clarifying",
            "draft_revision": 0, "draft_id": "D0", "draft": None, "confirmed_request": None}


def normalize_state(state):
    if state.get("contract_version") == 3:
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
    return content_hash({k: state[k] for k in ("fields", "reference_notes", "domain", "issues", "sources")})


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


def _merge(state, text, extraction):
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
                        _edit_issue(state, field, "撤回项无法唯一匹配，请指定条目 ID 或完整名称。", item.quote)
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
                    _edit_issue(state, field, "更新项无法唯一匹配，请指定条目 ID。", item.quote)
                    continue
                if item.action == "update" and not matches:
                    _edit_issue(state, field, "没有找到要更新的项，请选择条目或明确新增。", item.quote)
                    continue
                id = matches[0].get("id") if matches else field + "-" + uuid4().hex[:10]
                entry = _record(item, turn, id)
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
        if field in FIELDS:
            values = state["fields"][field]
            for entry in values if isinstance(values, list) else [values]:
                if entry.get("needs_review"):
                    entry.pop("needs_review")
                    entry["review_quote"] = text
                    entry["review_turn"] = turn
    for issue in state["issues"]:
        if issue["id"] in extraction.resolve_issue_ids and grouped:
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


def _select(state, extraction, text, context):
    rec = state.get("recommendation_set")
    expected = context.get("recommendation_ref")
    if not rec or expected != {"id": rec["id"], "version": rec["version"]} or rec["draft_id"] != state["draft_id"]:
        _edit_issue(state, "research_object", "推荐已经变化，请基于重新展示的方向选择。", text)
        return
    index = extraction.selected_option
    if not index or not 1 <= index <= len(rec["options"]):
        _edit_issue(state, "research_object", "请明确采用哪一个方向。", text)
        return
    option = rec["options"][index - 1]
    if any(x["direction_id"] == option["id"] and x["status"] == "rejected"
           for x in state["recommendation_history"]):
        _edit_issue(state, "research_object", "这条方向曾被拒绝；请明确说明要重新采用。", text)
        return
    state["sources"].update(deepcopy(rec["sources"]))
    for field, value in option["fields"].items():
        before = deepcopy(state["fields"][field])
        entries = value if isinstance(value, list) else [{"value": value}]
        converted = []
        for item in entries:
            entry = {"status": "specified", "value": item["value"], "source": "confirmed_suggestion",
                     "quote": None, "turn": None, "suggested_after_turn": len(state["turns"]) - 1,
                     "selection_quote": text, "confirmed_turn": len(state["turns"]),
                     "recommendation_ref": {"id": rec["id"], "version": rec["version"], "direction_id": option["id"]},
                     "source_refs": list(option["source_refs"])}
            if field in LIST_FIELDS:
                entry.update(id=field + "-" + uuid4().hex[:10], direction=item.get("direction"))
            converted.append(entry)
        state["fields"][field] = converted if field in LIST_FIELDS else converted[0]
        state["history"].append({"field": field, "before": before, "after": deepcopy(state["fields"][field]),
                                 "turn": len(state["turns"]), "reason": "recommendation_selected"})
    state["domain"] = "materials_application"
    state["recommendation_history"].append({"set_id": rec["id"], "version": rec["version"],
                                            "direction_id": option["id"], "status": "accepted", "quote": text})


def _reject(state, extraction, text):
    rec = state.get("recommendation_set")
    if not rec:
        return
    options = rec["options"]
    if extraction.selected_option and 1 <= extraction.selected_option <= len(options):
        options = [options[extraction.selected_option - 1]]
    rejected = {x["id"] for x in options}
    for option in options:
        state["recommendation_history"].append({"set_id": rec["id"], "version": rec["version"],
                                                "direction_id": option["id"], "status": "rejected", "quote": text})
    for field, value in state["fields"].items():
        entries = value if isinstance(value, list) else [value]
        kept = [x for x in entries if (x.get("recommendation_ref") or {}).get("direction_id") not in rejected]
        state["fields"][field] = (kept or [_unknown(field + "-unknown")]) if field in LIST_FIELDS else (kept[0] if kept else _unknown())
    state["recommendation_set"] = None


def make_draft(state):
    return {"contract_version": 3, "rule_version": RULE_VERSION,
            "original_intent": state["turns"][0] if state["turns"] else "",
            "draft_revision": state["draft_revision"], "draft_id": state["draft_id"],
            "fields": deepcopy(state["fields"]), "task_pattern": state["task_type"],
            "domain": state["domain"], "issues": deepcopy(state["issues"]),
            "reference_notes": deepcopy(state["reference_notes"]), "sources": deepcopy(state["sources"])}


def _result(state, changed=False, guide=False, text="", notice=None):
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
        question = issue["message"] + " " + QUESTIONS.get(issue["field"], "")
    else:
        status = "ready_for_design" if state["stage"] == "ready_for_design" else "needs_confirmation"
        question = None if status == "ready_for_design" else "请核对八项、来源、限制及草稿 " + state["draft_id"] + "，确认只允许进入研究设计。"
        state["asked_field"] = None
        if status == "needs_confirmation":
            state["stage"] = "review"
            state["draft"] = deepcopy(draft)
    rec = None
    if guide and status != "unsupported":
        rec = recommendations(state, text)
        state["recommendation_set"] = rec
        explanation = explain_field(state.get("asked_field"))
        if rec:
            question = explanation + " 已提供有来源的待选择方向，可采用序号、修改或拒绝；尚未写入研究需求。"
            status = "needs_guidance"
        else:
            question = explanation + " 当前已核查资料没有覆盖合适方向；可以先限定具体分子/材料类别，说明已有用途或提供可核查资料。我不会据此编造性质或来源。"
    result = {"intake_status": status, "ready": status == "ready_for_design",
              "ready_for_design": status == "ready_for_design", "request": draft,
              "calculation_status": "pending_research_design", "next_question": question,
              "blocking_issues": issues, "recommendations": rec, "content_changed": changed,
              "input_context": {"draft_id": state["draft_id"]}}
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
    if state["stage"] == "ready_for_design":
        return _result(state)
    if context.get("draft_id") != state["draft_id"] or state["stage"] != "review":
        return _result(state, notice="请先查看当前完整草稿，再确认它的版本。")
    draft = make_draft(state)
    if request_issues(draft):
        return _result(state)
    validate_research_request_v3(draft)
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


def handle_turn(client, model, state, text, input_context=None):
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
        before = _fingerprint(state)
        state["turns"].append(text)
        if "select" in extraction.actions:
            _select(state, extraction, text, context)
        if "reject" in extraction.actions or text in {"不采用", "不用建议"}:
            _reject(state, extraction, text)
        _merge(state, text, extraction)
        changed = _touch(state, before)
        guide = bool(set(extraction.actions) & {"recommend", "explain", "unsure", "reject"})
        result = _result(state, changed, guide, text)
        if "confirm" in extraction.actions and not extraction.updates and not changed and not (set(extraction.actions) & {"select", "edit", "reaffirm", "reject"}):
            result = _confirm(state, context)
        elif "confirm" in extraction.actions:
            result["notice"] = "本轮含修改或选择，请核对更新后的完整草稿，再确认。"
    result["metadata"] = {"model": model, "rule_version": RULE_VERSION, "contract_version": 3,
                          "elapsed_seconds": round(time.perf_counter() - started, 3),
                          "catalog_version": (state.get("recommendation_set") or {}).get("catalog_version")}
    return result


def main():
    from lapis import run_chat
    run_chat(None, "researcher")


if __name__ == "__main__":
    main()
