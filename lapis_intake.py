"""LAPIS module 1: turn incomplete intent into a reviewable research request."""

from __future__ import annotations

import argparse
from copy import deepcopy
import json
import os
from typing import Literal

import instructor
from openai import OpenAI
from pydantic import BaseModel, Field, model_validator

from lapis_core import validate_research_request_v2


SCALAR_FIELDS = (
    "purpose", "research_object", "application", "work_conditions",
    "research_scope", "material_function",
)
LIST_FIELDS = ("target_performance", "constraints")
REQUEST_FIELDS = (
    "purpose", "research_object", "application", "work_conditions",
    "target_performance", "constraints", "research_scope", "material_function",
)
FieldName = Literal[
    "task_type", "purpose", "research_object", "application", "work_conditions",
    "target_performance", "constraints", "research_scope", "material_function",
    "reference_note",
]
Status = Literal["specified", "none", "open", "unclear"]
Action = Literal["add", "replace", "remove"]
TASK_TYPES = {"screening", "comparison", "mechanism_validation", "mechanism_exploration", "other"}
CONFIRM_WORDS = {"确认", "确认继续", "按此继续", "就按这个", "同意"}
LABELS = {
    "purpose": "研究目的", "research_object": "研究对象", "application": "应用场景",
    "work_conditions": "工作条件", "target_performance": "目标性能",
    "constraints": "约束条件", "research_scope": "研究范围",
    "material_function": "材料功能",
}
QUESTIONS = {
    "purpose": "本轮具体希望作出什么研究判断：筛选、比较、探索原因，还是其他目标？",
    "research_object": "本轮先研究哪类材料或体系？请尽量限定到可讨论的对象。",
    "application": "这些材料用于什么使用或服役场景？纯基础研究目前先保留草稿。",
    "research_scope": "本轮研究哪些对象或变化范围，明确暂不研究什么？",
    "constraints": "这项要求是必须满足的硬约束，还是偏好？",
    "target_performance": "请澄清本轮关注的性能目标或取消的目标；定性方向也可以。",
    "task_type": "你提出了不同的研究任务；本轮先聚焦哪一个对象和目的？其他目标会保留在原话中。",
}
BARE_REPLIES = {"没有", "无", "暂无"}


class Update(BaseModel):
    field: FieldName
    status: Status
    value: str | None = None
    quote: str = Field(min_length=1, description="当前用户原话里的连续片段")
    action: Action = "add"
    direction: str | None = None
    strength: Literal["hard", "preference"] | None = None
    category: Literal["hypothesis", "method", "parameter", "other"] | None = None

    @model_validator(mode="after")
    def required_qualifiers(self):
        if self.field == "target_performance" and self.status == "specified" and not self.direction:
            raise ValueError("明确的目标性能须有方向；比较差异可填“比较”")
        if self.field == "constraints" and self.status == "specified" and not self.strength:
            raise ValueError("明确的约束须区分硬约束与偏好")
        return self


class Extraction(BaseModel):
    updates: list[Update] = Field(default_factory=list)


class SuggestedGoal(BaseModel):
    value: str = Field(min_length=1)
    direction: str = Field(min_length=1)


class IntakeSuggestion(BaseModel):
    material_function: str = Field(min_length=1)
    target_performance: list[SuggestedGoal] = Field(min_length=1, max_length=3)
    limitation: str = Field(min_length=1)


SYSTEM_PROMPT = """你是 LAPIS 第一模块的用户原话提取器，只返回符合结构的 JSON。
只提取本轮用户明确表达的信息，不补造候选、数值、方法或科学事实。quote 必须是原话连续片段。
八项字段：purpose 研究目的；research_object 研究对象；application 应用场景；
work_conditions 工作条件；target_performance 目标性能；constraints 约束条件；
research_scope 本轮包含/排除的范围；material_function 材料在场景中承担的作用。
target_performance 可为定性方向，如提高稳定性、比较传输能力；无需强求可计算代理指标。
研究目的必须是本轮研究行动或判断；“设计高电压电池”只是上层意图，不是已明确的筛选/比较任务。
研究对象是被研究的材料或体系，不要把整个器件误当成已确定的材料对象。
例如“想设计高电压电池”：application=高电压电池，purpose=unclear，research_object 未提及；
“高电压”是应用/工作背景，除非明确要提升工作电压，不要另造一个目标性能“提高电压”。
后续“先只研究碳酸酯电解液，比较配方……”应更新 research_object 和 purpose，
即使先前字段已有不准确或宽泛的解释。material_function 只提取用户明确说出的材料作用，
不能把“电解液用于电池”改写成已指定的材料功能；不确定时让建议助手提出待确认解释。
多个目标或约束各输出一条 update。明确的目标性能必须写 direction；
只比较差异时 direction=比较，没有方向且不能判断时 status=unclear。
明确的约束必须标 hard 或 preference，拿不准时 status=unclear。
用户说“不使用”“必须”“不得”时不要颠倒含义。用户说“不知道数值”时保留已知的定性目标，
工作条件数值可标 open。未提及的字段不要输出，不要把未知解释成明确没有预设值。
status: specified=明确表达；none=明确说没有预设；open=交给后续研究设计；
unclear=提到但含义有歧义。action: add=补充；replace=用户改成或只保留新内容；
remove=明确撤回已有内容。用户修改列表时，同一轮可给多条 replace，系统会一次替换整个列表。
task_type 仅作辅助分类：screening、comparison、mechanism_validation、
mechanism_exploration（未知机制探索）、other、multiple_tasks（不同对象或目的的独立任务）、
out_of_scope（非材料研究）。同一研究范围内的多项性能目标不是 multiple_tasks。
只有用户给出可检验的作用路径，才分类为 mechanism_validation；没有假设仍可探索。
用户主动给出的假设、计算方法或参数，作为 reference_note 记录，category 标明类别；
它们不是已核验的研究设计。不要将方法建议放进八项请求字段。
上一句问题对应字段和已保存原话仅用于理解短回复，不要把它们冒充本轮原话。"""

PROPOSAL_PROMPT = """你是 LAPIS 第一模块的草稿建议助手，只提出材料功能和目标性能的
暂定解释供用户修改或确认。保留用户的应用目标，目标性能可定性、多目标；为每项目标写方向
（提高、降低、保持、比较或探索）。不要提出计算方法、物理模型、代理描述符、力场、
具体参数、阈值、科研结果或已验证结论。不要把建议写成用户已给事实。
如果用户目标与建议之间有推断跳跃，在 limitation 中说明。只返回结构化 JSON。"""


def _unknown() -> dict:
    return {"status": "unknown", "value": None, "source": None, "quote": None, "turn": None}


def _new_fields() -> dict:
    return {key: ([_unknown()] if key in LIST_FIELDS else _unknown())
            for key in REQUEST_FIELDS}


def new_state() -> dict:
    return {
        "contract_version": 2, "fields": _new_fields(), "task_type": None,
        "asked_field": None, "turns": [], "reference_notes": [], "history": [],
        "stage": "clarifying", "draft": None, "suggestion": None,
    }


def normalize_state(state: dict) -> dict:
    """Read old JSONB states without rewriting old request versions."""
    if state.get("contract_version") == 2:
        for key, value in _new_fields().items():
            state.setdefault("fields", {}).setdefault(key, value)
        state.setdefault("reference_notes", [])
        state.setdefault("history", [])
        return state
    legacy = deepcopy(state)
    converted = new_state()
    converted["turns"] = list(legacy.get("turns") or [])
    converted["legacy_fields"] = legacy.get("fields", {})
    old = legacy.get("fields", {})
    for old_key, new_key in (
        ("purpose", "purpose"), ("material", "research_object"),
        ("application", "application"), ("conditions", "work_conditions"),
    ):
        if old_key in old:
            converted["fields"][new_key] = {
                **deepcopy(old[old_key]), "source": old[old_key].get("source", "legacy_unknown")
            }
    if "metric" in old:
        goal = {**deepcopy(old["metric"]), "source": old["metric"].get("source", "legacy_unknown")}
        goal["direction"] = (old.get("direction") or {}).get("value")
        converted["fields"]["target_performance"] = [goal]
    if "constraints" in old:
        note = {**deepcopy(old["constraints"]), "source": old["constraints"].get("source", "legacy_unknown")}
        converted["fields"]["constraints"] = [note]
    converted["task_type"] = (old.get("task_type") or {}).get("value")
    state.clear()
    state.update(converted)
    return state


def extract(client, model: str, state: dict, text: str) -> Extraction:
    context = {
        "已有字段": state["fields"], "上一句问题": state.get("asked_field"),
        "本轮用户原话": text,
    }
    options = {}
    if os.getenv("LAPIS_BASE_URL", "https://api.deepseek.com").startswith("https://api.deepseek.com"):
        options["extra_body"] = {"thinking": {"type": "disabled"}}
    return client.create(
        model=model, response_model=Extraction,
        messages=[{"role": "system", "content": SYSTEM_PROMPT},
                  {"role": "user", "content": json.dumps(context, ensure_ascii=False)}],
        max_retries=2, max_tokens=1800, temperature=0, **options,
    )


def propose_intake(client, model: str, state: dict) -> IntakeSuggestion:
    context = {"用户原话": state["turns"], "已有字段": state["fields"]}
    options = {}
    if os.getenv("LAPIS_BASE_URL", "https://api.deepseek.com").startswith("https://api.deepseek.com"):
        options["extra_body"] = {"thinking": {"type": "disabled"}}
    return client.create(
        model=model, response_model=IntakeSuggestion,
        messages=[{"role": "system", "content": PROPOSAL_PROMPT},
                  {"role": "user", "content": json.dumps(context, ensure_ascii=False)}],
        max_retries=2, max_tokens=1000, temperature=0, **options,
    )


def contextual_reply(state: dict, text: str) -> Extraction | None:
    field = state.get("asked_field")
    answer = text.strip()
    if not field or field not in REQUEST_FIELDS:
        return None
    if answer in BARE_REPLIES:
        return Extraction(updates=[Update(field=field, status="none", quote=answer)])
    if answer in {"不知道", "不清楚", "不确定", "没想好", "交给研究设计确定"}:
        return Extraction(updates=[Update(field=field, status="open", quote=answer)])
    return None


def _record(update: Update, turn: int) -> dict:
    entry = {
        "status": update.status, "value": update.value, "source": "user",
        "quote": update.quote, "turn": turn,
    }
    if update.field == "target_performance":
        entry["direction"] = update.direction
    if update.field == "constraints":
        entry["strength"] = update.strength
    return entry


def _valid_updates(state: dict, text: str, extraction: Extraction) -> dict[str, list[Update]]:
    grouped: dict[str, list[Update]] = {}
    bare = text in BARE_REPLIES
    for item in extraction.updates:
        if item.quote not in text or (bare and state.get("asked_field") and item.field != state["asked_field"]):
            continue
        if item.status == "specified" and not item.value:
            continue
        grouped.setdefault(item.field, []).append(item)
    return grouped


def _merge(state: dict, text: str, extraction: Extraction) -> None:
    if state.get("stage") == "review" and state.get("draft"):
        state["fields"] = deepcopy(state["draft"]["fields"])
    grouped = _valid_updates(state, text, extraction)
    turn = len(state["turns"])
    stale = ({"material_function", "target_performance"} if
             {"research_object", "application"} & grouped.keys() else
             {"target_performance"} if {"purpose", "research_scope"} & grouped.keys() else set())
    for field in stale:
        if field in grouped:
            continue
        before = deepcopy(state["fields"][field])
        entries = before if isinstance(before, list) else [before]
        kept = [x for x in entries if x.get("source") not in {"system_suggestion", "confirmed_suggestion"}]
        after = (kept or [_unknown()]) if isinstance(before, list) else (kept[0] if kept else _unknown())
        if after != before:
            state["fields"][field] = after
            state["history"].append({"field": field, "before": before,
                                     "after": deepcopy(after), "turn": turn,
                                     "reason": "research_context_changed"})
    for field, updates in grouped.items():
        if field == "task_type":
            value = updates[-1].value
            if value in TASK_TYPES | {"multiple_tasks", "out_of_scope"}:
                state["task_type"] = value
            continue
        if field == "reference_note":
            for item in updates:
                state["reference_notes"].append({
                    "value": item.value, "category": item.category or "other",
                    "status": "unverified_user_reference", "quote": item.quote, "turn": turn,
                })
            continue
        if any(item.action != "remove" for item in updates):
            state["unresolved_edits"] = [x for x in state.get("unresolved_edits", [])
                                         if x["field"] != field]
        before = deepcopy(state["fields"][field])
        if field in SCALAR_FIELDS:
            state["fields"][field] = _record(updates[-1], turn)
        else:
            current = [x for x in state["fields"][field] if x["status"] != "unknown"]
            replacing = any(item.action == "replace" for item in updates)
            if replacing:
                current = []
            for item in updates:
                if item.action == "remove":
                    if replacing:
                        continue
                    target = (item.value or "").casefold().strip()
                    matches = [x for x in current if target and target in (x.get("value") or "").casefold()]
                    if not matches:
                        state.setdefault("unresolved_edits", []).append({"field": field, "quote": item.quote})
                    current = [x for x in current if x not in matches]
                else:
                    entry = _record(item, turn)
                    if not any(x.get("value") == entry["value"] and x.get("status") == entry["status"]
                               and x.get("direction") == entry.get("direction") for x in current):
                        current.append(entry)
            state["fields"][field] = current or [_unknown()]
        state["history"].append({"field": field, "before": before,
                                 "after": deepcopy(state["fields"][field]), "turn": turn})
    if grouped:
        state["draft"] = None
        state["suggestion"] = None
        state["stage"] = "clarifying"


def _good_scalar(entry: dict, *, user_only: bool = False) -> bool:
    sources = {"user"} if user_only else {"user", "confirmed_suggestion"}
    return entry.get("status") == "specified" and bool(entry.get("value")) and entry.get("source") in sources


def _critical_gap(state: dict) -> str | None:
    if state.get("task_type") == "multiple_tasks":
        return "task_type"
    for field in ("purpose", "research_object", "application", "research_scope"):
        if not _good_scalar(state["fields"][field], user_only=True):
            return field
    if state.get("unresolved_edits"):
        return state["unresolved_edits"][-1]["field"]
    for entry in state["fields"]["constraints"]:
        if entry["status"] == "unclear":
            return "constraints"
    if any(item["status"] == "unclear" or
           (item["status"] == "specified" and (not item.get("value") or not item.get("direction")))
           for item in state["fields"]["target_performance"]):
        return "target_performance"
    return None


def _needs_suggestion(fields: dict) -> bool:
    function = fields["material_function"]
    goals = fields["target_performance"]
    return not (_good_scalar(function) or
                function.get("status") == "specified" and function.get("source") == "system_suggestion" and function.get("value")) or not any(
        item["status"] == "specified" and item.get("source") in {"user", "system_suggestion", "confirmed_suggestion"} and item.get("direction") and item.get("value")
        for item in goals
    )


def _suggested(value: str, turn: int, direction: str | None = None) -> dict:
    entry = {"status": "specified", "value": value, "source": "system_suggestion",
             "quote": None, "turn": None, "suggested_after_turn": turn}
    if direction is not None:
        entry["direction"] = direction
    return entry


def make_draft(state: dict) -> dict:
    fields = deepcopy(state["fields"])
    suggestion = state.get("suggestion")
    if suggestion:
        if fields["material_function"].get("status") != "specified" or not fields["material_function"].get("value"):
            fields["material_function"] = _suggested(suggestion["material_function"], len(state["turns"]))
        if not any(item["status"] == "specified" and item.get("value") and item.get("direction")
                   for item in fields["target_performance"]):
            current = [x for x in fields["target_performance"] if x["status"] != "unknown"]
            current.extend(_suggested(goal["value"], len(state["turns"]), goal["direction"])
                           for goal in suggestion["target_performance"])
            fields["target_performance"] = current
    return {
        "contract_version": 2, "original_intent": state["turns"][0],
        "task_pattern": state.get("task_type"), "fields": fields,
        "reference_notes": deepcopy(state["reference_notes"]),
    }


def process_turn(state: dict, text: str, extraction: Extraction) -> dict:
    normalize_state(state)
    text = text.strip()
    state["turns"].append(text)
    _merge(state, text, extraction)
    if state.get("task_type") == "out_of_scope":
        state["asked_field"] = None
        return {"intake_status": "unsupported", "ready": False, "ready_for_design": False,
                "request": make_draft(state), "next_question": "首版只受理材料应用研究；请描述材料研究问题。"}
    gap = _critical_gap(state)
    state["asked_field"] = gap
    return {
        "intake_status": "needs_clarification" if gap else "draft_available",
        "ready": False, "ready_for_design": False, "request": make_draft(state),
        "next_question": QUESTIONS.get(gap) if gap else None,
    }


def handle_turn(client, model: str, state: dict, text: str) -> dict:
    normalize_state(state)
    text = text.strip()
    if not text:
        raise ValueError("输入不能为空")
    if state.get("stage") == "review" and text in CONFIRM_WORDS:
        draft = deepcopy(state["draft"])
        confirmed_turn = len(state["turns"]) + 1
        for value in draft["fields"].values():
            entries = value if isinstance(value, list) else [value]
            for entry in entries:
                if entry.get("source") == "system_suggestion":
                    entry["source"] = "confirmed_suggestion"
                    entry["confirmed_turn"] = confirmed_turn
        validate_research_request_v2(draft)
        state["turns"].append(text)
        state["fields"] = deepcopy(draft["fields"])
        state["draft"] = None
        state["stage"] = "ready_for_design"
        state["asked_field"] = None
        return {
            "intake_status": "ready_for_design", "ready": True, "ready_for_design": True,
            "confirmation_event": True, "calculation_status": "pending_research_design",
            "request": draft, "next_question": None,
        }

    if state.get("stage") == "review" and text in {"不采用", "不用建议", "先不确认"}:
        for field in ("material_function", "target_performance"):
            value = state["fields"][field]
            if isinstance(value, list):
                kept = [x for x in value if x.get("source") != "system_suggestion"]
                state["fields"][field] = kept or [_unknown()]
            elif value.get("source") == "system_suggestion":
                state["fields"][field] = _unknown()
        state["suggestion"] = None
        state["draft"] = None
        state["stage"] = "clarifying"
        state["turns"].append(text)
        state["asked_field"] = "material_function"
        return {"intake_status": "needs_clarification", "ready": False,
                "ready_for_design": False, "request": make_draft(state),
                "next_question": "请修改材料功能或目标性能的建议，再由我展示完整草稿。"}

    extraction = contextual_reply(state, text) or extract(client, model, state, text)
    result = process_turn(state, text, extraction)
    if result["intake_status"] == "unsupported" or result["next_question"]:
        return result
    if _needs_suggestion(state["fields"]):
        state["suggestion"] = propose_intake(client, model, state).model_dump()
    draft = make_draft(state)
    state["draft"] = draft
    state["stage"] = "review"
    state["asked_field"] = None
    return {
        "intake_status": "needs_confirmation", "ready": False, "ready_for_design": False,
        "calculation_status": "pending_research_design", "request": draft,
        "proposed_request": draft, "next_question": (
            "请核对八项内容、原话来源、系统建议和未定事项。回复“确认”进入研究设计；"
            "如需修改，请直接写出修改内容。此确认不批准任何计算。"
        ),
        "suggestion_limitation": (state.get("suggestion") or {}).get("limitation"),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="LAPIS 研究请求澄清原型")
    parser.add_argument("--text", help="只处理一轮输入；省略后进入交互模式")
    args = parser.parse_args()
    key = os.getenv("LAPIS_API_KEY") or os.getenv("DEEPSEEK_API_KEY")
    if not key:
        parser.error("请设置 LAPIS_API_KEY 或 DEEPSEEK_API_KEY")
    client = instructor.from_openai(
        OpenAI(api_key=key, base_url=os.getenv("LAPIS_BASE_URL", "https://api.deepseek.com"), timeout=40),
        mode=instructor.Mode.JSON,
    )
    model = os.getenv("LAPIS_MODEL", "deepseek-flash")
    state = new_state()
    pending_text = None
    while True:
        try:
            text = pending_text if pending_text is not None else (
                args.text if args.text is not None else input("研究需求> ")
            )
            pending_text = None
        except (EOFError, KeyboardInterrupt):
            break
        if not text.strip():
            if args.text is not None:
                parser.error("输入不能为空")
            continue
        result = handle_turn(client, model, state, text)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        if args.text is not None:
            break
        if result["ready_for_design"]:
            print("研究请求已确认，可进入研究设计；如需修改继续输入，直接回车结束。")
            try:
                pending_text = input("修改> ")
                if not pending_text.strip():
                    break
            except (EOFError, KeyboardInterrupt):
                break
        else:
            print(result["next_question"])


if __name__ == "__main__":
    main()
