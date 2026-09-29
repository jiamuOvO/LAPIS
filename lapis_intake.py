"""LAPIS module 1: turn a researcher's words into a reviewable request."""

from __future__ import annotations

import argparse
from copy import deepcopy
import json
import os
from typing import Literal

import instructor
from openai import OpenAI
from pydantic import BaseModel, Field, model_validator


FieldName = Literal[
    "task_type", "purpose", "application", "material", "metric", "direction",
    "conditions", "candidates", "constraints", "hypothesis", "controls",
]
Status = Literal["specified", "none", "open", "unclear"]
TASK_TYPES = {"screening", "comparison", "mechanism_validation"}
INTENT_ISSUES = {"mechanism_exploration", "multiple_tasks", "out_of_scope"}

LABELS = {
    "task_type": "任务目的",
    "purpose": "研究目的",
    "application": "应用场景",
    "material": "材料体系",
    "metric": "目标指标",
    "direction": "目标方向",
    "conditions": "工作条件",
    "candidates": "候选范围",
    "constraints": "其他约束",
    "hypothesis": "机制假设",
    "controls": "对照对象",
}
COMMON = ("task_type", "purpose", "material", "application", "conditions")
BY_TASK = {
    "screening": ("metric", "direction", "candidates"),
    "comparison": ("candidates", "metric"),
    "mechanism_validation": ("hypothesis", "metric", "controls"),
}
QUESTIONS = {
    "task_type": "这次最终希望得到什么结果？例如选出候选、比较差异、验证具体机制；其他目标也可以直接描述。",
    "metric": "你更在意材料的哪方面表现？例如稳定性、传输性能或合成可行性；也可以由系统先推荐一个具体指标。",
    "material": "这次研究的材料、分子或体系是什么？",
    "purpose": "这次希望筛选候选、比较已有候选，还是验证一个机制？",
    "application": "研究对象将用于什么应用场景？没有具体应用也可以说明。",
    "candidates": "你已有候选对象或允许探索的范围吗？没有也可以由系统提出。",
    "conditions": "有哪些需要固定或比较的工作条件？没有预设条件也可以直接说。",
    "direction": "目标指标是越高越好、越低越好，还是希望落在某个区间？",
    "hypothesis": "你希望检验的具体机制主张是什么？例如“X通过Y影响Z”。",
    "controls": "有指定的对照对象吗？没有也可以，由研究设计阶段提出。",
}
BARE_REPLIES = {"没有", "无", "暂无", "越多越好", "越高越好", "越大越好"}


class Update(BaseModel):
    field: FieldName
    status: Status
    value: str | None = None
    quote: str = Field(description="当前用户原话中支持此更新的连续片段")


class Extraction(BaseModel):
    updates: list[Update] = Field(default_factory=list)


class EvaluationCriterion(BaseModel):
    aspect: str
    metric: str
    direction: str
    reason: str
    calculation_role: Literal["direct", "proxy"] = Field(description="相对用户目标性能：直接评价或仅为计算代理量")
    calculation_route: str = Field(description="拟采用的方法类别，以及所需输入或条件；具体参数由研究设计确定")


class EvaluationProposal(BaseModel):
    application_assumption: str
    assumptions: list[str] = Field(max_length=3)
    criteria: list[EvaluationCriterion] = Field(min_length=1, max_length=4)
    comparison_rule: str
    limitation: str

    @model_validator(mode="after")
    def primary_metric_is_concrete(self):
        metric = self.criteria[0].metric
        if any(phrase in metric for phrase in ("例如", "如", "或", "等", "需确认", "待确认", "关键性能指标", "综合性能")):
            raise ValueError("首选 metric 必须只写一个具体可比较量，例如‘载流子迁移率’，不能列举选项或要求用户再选")
        return self


SYSTEM_PROMPT = """你是 LAPIS 的研究请求信息提取器。请只返回 JSON。
从本轮用户原话提取字段更新，不生成研究方案或科学事实。
字段：task_type任务目的、purpose研究目的、application应用场景、material材料体系、
metric具体可比较指标、direction目标方向、conditions工作条件、candidates候选范围、
constraints其他约束、hypothesis机制假设、controls对照对象。
支持的task_type：screening（筛选优先候选）、comparison（比较指定对象的差异）、
mechanism_validation（检验一个具体机制主张）。根据最终想获得的判断分类，不仅凭动词；
“比较并选出最优”属于screening。
其他明确意图：mechanism_exploration（探索未知原因或反应路径，尚无待检验假设）、
multiple_tasks（一句话提出多个独立研究目标，不能归入单个任务）、
out_of_scope（写综述、科普问答、开发软件等非本模块的计算研究请求）。
意图信息不足时输出task_type且status=unclear；不要把明确的其他意图强塞进三类。
只有用户给出“X通过Y影响Z”这样的具体作用路径并要求检验，才是mechanism_validation。
“FEC为什么能提升循环寿命”只描述了观察到的效果，没有提出作用路径，属于mechanism_exploration；
“验证FEC是否通过改变溶剂化结构提升循环寿命”才是mechanism_validation。
先检查是否属于材料、分子或计算科研任务；“筛选基金”“比较电脑价格”虽含筛选或比较，仍是out_of_scope。
status: specified=明确给定；none=明确没有预设值；open=交由系统选择或尽可能多；
unclear=提到该字段但无法确定具体含义。未提及的字段不要输出。
quote必须是当前原话里的连续片段。不得补造温度、候选名称、计算方法或默认值。
“传输性能更好”属于尚未明确的metric；“越高越好”若指性能，只是direction。
“结合能力”“稳定性”等宽泛性能也属于metric=unclear，除非用户已给出具体可比较量。
比较任务中的“两个/三个/若干候选”只给数量，未给具体身份，candidates=unclear。
验证任务须提取可检验的hypothesis；仅说“研究机制”时hypothesis=unclear。
“没有”须结合上一句问题确定指向；没有上一句问题时不要猜字段。
“越多越好”若指候选数量，应是candidates=open；不要解释成无限计算预算。
上一句询问指标时，若用户说不懂具体指标、希望综合评价，应输出metric=open，
表示评价维度和指标交由研究设计提出，不要反复要求用户报出指标名称。
示例输入：我想找一种传输性能更好的电解液
示例输出 JSON：{"updates":[
 {"field":"task_type","status":"specified","value":"screening","quote":"找一种传输性能更好的电解液"},
 {"field":"purpose","status":"specified","value":"筛选传输性能更好的电解液","quote":"找一种传输性能更好的电解液"},
 {"field":"material","status":"specified","value":"电解液","quote":"电解液"},
 {"field":"metric","status":"unclear","value":"传输性能","quote":"传输性能"}]}
若上一句询问候选，用户答“没有”，输出 JSON：{"updates":[{"field":"candidates","status":"none","value":null,"quote":"没有"}]}"""

PROPOSAL_PROMPT = """你是 LAPIS 的研究方案草案助手。用户尚未指定具体评价指标时，主动提出一份可修改的初步建议。
依据用户原话、已确认字段和反馈，给出 1 至 4 个与研究目标相关、在后续计算设计中有可行计算路线的指标；数量由研究需要决定，不要为凑数添加指标。criteria 第一个是优先指标。metric 中只能填一个具体可比较量，绝不能写“如A、B或C”“关键性能指标”“需用户确认具体指标”这类泛称。每个指标填写 calculation_route（方法类别及至少一项必要输入或条件；不要预先固定具体泛函、基组、采样参数）和 calculation_role：相对用户想要的实际性能，direct 是直接评价，proxy 是计算代理量。计算代理量须在 reason 中说明不能单独代表实际性能。无法提出合理计算路线的实验指标不要列入。优先考虑同一批候选结构能用相近计算流程得到的指标；不同方法若可能显著增加成本，在 limitation 中说明。说明各自方向和用途；若评价方向依赖具体应用或目标区间，direction 应写“待研究设计确定目标区间”，不要武断设为越大或越小越好。综合评价时逐项比较，不要无依据地加权成一个总分。最多写 3 条简短假设。
应用场景未给出时，选一个合理的示例场景写入 application_assumption（只写用途名称，尽量不超过20字），并在 assumptions 中明确这只是待用户确认的假设。
不要把建议当成用户已给的事实；不要编造具体候选、数值门槛、计算结果或已验证的科学结论。计算路线只是待研究设计核验的建议，尚不保证可执行；科学依据尚未经过文献核对，应在 limitation 中说明。
用户指出要修改的地方时，针对上一版方案修改。若材料类别过宽，在 limitation 里说明需要缩小范围。"""


def question_for(field: str, task_type: str | None) -> str:
    if field == "candidates" and task_type == "comparison":
        return "你想比较哪些具体对象？如果还没有对象，也可以交给研究设计阶段提出。"
    if field == "metric" and task_type == "mechanism_validation":
        return "希望观察哪些量来支持或削弱这个机制？没有预设指标也可以说明。"
    return QUESTIONS[field]


def new_state() -> dict:
    return {"fields": {}, "asked_field": None, "turns": [], "proposal": None,
            "evaluation_plan": None, "stage": None, "draft": None}


def extract(client, model: str, state: dict, text: str) -> Extraction:
    context = {
        "已有字段": state["fields"],
        "上一句问题对应字段": state["asked_field"],
        "本轮用户原话": text,
    }
    options = {}
    if os.getenv("LAPIS_BASE_URL", "https://api.deepseek.com").startswith("https://api.deepseek.com"):
        options["extra_body"] = {"thinking": {"type": "disabled"}}
    return client.create(
        model=model,
        response_model=Extraction,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": json.dumps(context, ensure_ascii=False)},
        ],
        max_retries=2,
        max_tokens=2000,
        temperature=0,
        **options,
    )


def propose_evaluation(client, model: str, state: dict, feedback: str = "") -> EvaluationProposal:
    context = {
        "用户原话": state["turns"],
        "已确认与待定字段": state["fields"],
        "上一版方案": state.get("proposal"),
        "用户修改意见": feedback,
    }
    options = {}
    if os.getenv("LAPIS_BASE_URL", "https://api.deepseek.com").startswith("https://api.deepseek.com"):
        options["extra_body"] = {"thinking": {"type": "disabled"}}
    return client.create(
        model=model,
        response_model=EvaluationProposal,
        messages=[
            {"role": "system", "content": PROPOSAL_PROMPT},
            {"role": "user", "content": json.dumps(context, ensure_ascii=False)},
        ],
        max_retries=2,
        max_tokens=1600,
        temperature=0,
        **options,
    )


def contextual_reply(state: dict, text: str) -> Extraction | None:
    """Resolve short answers from the last question without an LLM guess."""
    field = state["asked_field"]
    text = text.strip()
    if not field:
        return None
    if text in {"没有", "无", "暂无"}:
        return Extraction(updates=[Update(field=field, status="none", quote=text)])
    if field in {"metric", "direction", "candidates", "application", "conditions", "controls"}:
        brief = text.strip("。！？，!? ")
        uncertain = any(phrase in text for phrase in ("不懂", "不知道", "不清楚", "不确定", "没想好"))
        broad_evaluation = any(phrase in text for phrase in ("综合评价", "全面评价", "多维度评价"))
        if brief in {"不知道", "不清楚", "不确定", "没想好", "我也不懂这些", "你来定", "交给系统"} or (
            field == "metric" and broad_evaluation and (
                uncertain or brief in {"综合评价", "全面评价", "多维度评价", "需要综合评价", "我觉得需要综合评价"}
            )
        ):
            return Extraction(updates=[Update(
                field=field, status="open",
                value="综合评价；指标由研究设计确定" if field == "metric" and broad_evaluation else "由研究设计确定",
                quote=text,
            )])
    if field == "task_type" and text in {"筛选", "比较", "机制验证", "机制探索"}:
        task_type = {"筛选": "screening", "比较": "comparison", "机制验证": "mechanism_validation", "机制探索": "mechanism_exploration"}[text]
        return Extraction(updates=[Update(
            field=field, status="specified", value=task_type, quote=text
        )])
    if field == "candidates" and text == "越多越好":
        return Extraction(updates=[Update(
            field=field, status="open", value="尽可能多的可行候选", quote=text
        )])
    if field == "direction" and text in {"越高越好", "越大越好", "越多越好"}:
        return Extraction(updates=[Update(field=field, status="specified", value=text, quote=text)])
    if field == "metric" and text in {"越高越好", "越大越好", "越多越好"}:
        return Extraction(updates=[Update(
            field=field, status="unclear", value="仅给出优化方向，未给出指标", quote=text
        )])
    return None


def process_turn(state: dict, text: str, extraction: Extraction) -> dict:
    """Merge only updates that are grounded in this turn's exact words."""
    text = text.strip()
    asked = state["asked_field"]
    state["turns"].append(text)
    if text in BARE_REPLIES and asked is None:
        return {
            "intake_status": "needs_clarification",
            "ready": False,
            "request": state["fields"],
            "deferred_to_research_design": [],
            "next_question": f"你说的“{text}”具体是指哪个方面？",
        }

    new_task_type = next((item.value for item in extraction.updates
                          if item.field == "task_type" and item.status == "specified"
                          and item.value in TASK_TYPES | INTENT_ISSUES
                          and item.quote and item.quote in text), None)
    old_task_type = state["fields"].get("task_type", {}).get("value")
    if old_task_type and new_task_type and new_task_type != old_task_type:
        state["fields"].clear()
        state["proposal"] = None
        state["evaluation_plan"] = None
        state["stage"] = None
        state["draft"] = None

    for item in extraction.updates:
        if not item.quote or item.quote not in text:
            continue
        if text in BARE_REPLIES and asked and item.field != asked:
            continue
        if item.status == "specified" and not item.value:
            continue
        if item.field == "task_type" and item.status == "specified" and item.value not in TASK_TYPES | INTENT_ISSUES:
            continue
        state["fields"][item.field] = {
            "status": item.status,
            "value": item.value,
            "quote": item.quote,
            "turn": len(state["turns"]),
        }

    task_type = state["fields"].get("task_type", {}).get("value")
    if task_type in INTENT_ISSUES:
        status, question = {
            "mechanism_exploration": (
                "unsupported",
                "我理解你想探索未知机制。当前原型尚未定义这类任务的研究规约；如果要检验一个具体机制，请说出待检验的主张。",
            ),
            "multiple_tasks": (
                "needs_clarification",
                "你提出了多个独立研究目标。请先选一个作为本次任务；其他目标可以另开任务。",
            ),
            "out_of_scope": (
                "unsupported",
                "当前模块受理材料研究的筛选、比较和具体机制验证。请描述一个属于这些范围的研究目标。",
            ),
        }[task_type]
        state["asked_field"] = "task_type" if task_type == "multiple_tasks" else None
        return {
            "intake_status": status,
            "ready": False,
            "request": state["fields"],
            "deferred_to_research_design": [],
            "next_question": question,
        }
    if task_type != "mechanism_validation":
        state["fields"].pop("hypothesis", None)
        state["fields"].pop("controls", None)
    required = ("task_type",) if task_type not in TASK_TYPES else (
        "task_type", *BY_TASK[task_type], *COMMON[1:]
    )
    if task_type == "screening" and state["fields"].get("metric", {}).get("status") in {"open", "none"}:
        required = ("task_type", "purpose", "material", "application", "candidates", "conditions", "metric")
    must_specify = {"task_type", "material", "purpose", "hypothesis"}
    missing = [
        field for field in required
        if field not in state["fields"] or state["fields"][field]["status"] == "unclear"
        or (field in must_specify
            and state["fields"][field]["status"] != "specified")
    ]
    state["asked_field"] = missing[0] if missing else None
    return {
        "intake_status": "ready" if not missing else "needs_clarification",
        "ready": not missing,
        "request": state["fields"],
        "deferred_to_research_design": [
            LABELS[field] for field in required
            if field in state["fields"] and state["fields"][field]["status"] in ("none", "open")
        ],
        "next_question": question_for(missing[0], task_type) if missing else None,
    }


def required_fields(task_type: str) -> tuple[str, ...]:
    return ("task_type", *BY_TASK[task_type], *COMMON[1:])


def make_draft(state: dict) -> dict:
    """Keep the user's words separate from clearly marked system suggestions."""
    draft = deepcopy(state["fields"])
    task_type = draft["task_type"]["value"]
    proposal = state.get("proposal")
    suggestions = {
        "application": (proposal["application_assumption"], "specified") if proposal else
                       ("应用场景暂不限定，由研究设计明确", "open"),
        "metric": ("由研究设计确定具体可比较指标", "open"),
        "direction": ("由研究设计依据指标确定方向", "open"),
        "candidates": ("在已给材料范围内寻找可行候选，由研究设计限定搜索范围", "open"),
        "conditions": ("暂不指定具体数值，由研究设计设置统一可比的条件", "open"),
        "controls": ("由研究设计提出合适的对照对象", "open"),
    }
    if proposal:
        primary = proposal["criteria"][0]
        original_metric = (draft.get("metric", {}).get("value") or "") + (draft.get("metric", {}).get("quote") or "")
        selected = proposal["criteria"] if any(word in original_metric for word in ("综合评价", "全面评价", "多维度评价")) else [primary]
        suggestions.update({"metric": ("；".join(item["metric"] for item in selected), "specified"),
                            "direction": ("；".join(item["direction"] for item in selected),
                                          "open" if any("待研究设计" in item["direction"] for item in selected) else "specified")})
    for field in required_fields(task_type):
        current = draft.get(field, {})
        if field not in suggestions or current.get("status") == "specified" or (
            current.get("status") in {"none", "open"} and field not in {"metric", "direction"}
        ):
            continue
        value, status = suggestions[field]
        draft[field] = {"status": status, "value": value, "source": "system_suggestion"}
    return draft


def offer_confirmation(state: dict, result: dict) -> dict:
    draft = make_draft(state)
    hard_missing = [field for field in ("purpose", "material", "hypothesis")
                    if field in required_fields(draft["task_type"]["value"])
                    and draft.get(field, {}).get("status") != "specified"]
    if hard_missing:
        return offer_bundle(state, result, hard_missing)
    state["draft"] = draft
    state["stage"] = "confirmation"
    state["asked_field"] = None
    result.update(intake_status="needs_confirmation", ready=False,
                  proposed_request=draft,
                  calculation_status="pending_research_design",
                  next_question="请核对上面的完整草稿。回复“确认”进入研究设计；需要调整时一次写出修改内容。")
    if state.get("proposal"):
        result["proposal"] = state["proposal"]
    return result


def offer_bundle(state: dict, result: dict, fields: list[str] | None = None) -> dict:
    task_type = state["fields"]["task_type"]["value"]
    proposal = state.get("proposal")
    fields = fields if fields is not None else [
        field for field in required_fields(task_type)
        if field != "task_type" and state["fields"].get(field, {}).get("status") != "specified"
        and not (proposal and field in {"metric", "direction"})
    ]
    if not fields:
        return offer_confirmation(state, result)
    lead = ""
    if proposal:
        primary, *alternatives = proposal["criteria"]
        lead = (f"暂按“{proposal['application_assumption']}”这一待确认用途，建议优先用"
                f"“{primary['metric']}”（{primary['direction']}，拟用{primary['calculation_route']}）评价。")
        if alternatives:
            lead += "其他可计算选项：" + "、".join(f"“{item['metric']}”" for item in alternatives) + "。"
    state["stage"] = "bundle"
    state["asked_field"] = None
    result.update(intake_status="needs_clarification", ready=False,
                  calculation_status="pending_research_design",
                  next_question=lead + "请一次补充或修改：" + "、".join(LABELS[field] for field in fields)
                  + "。不确定的项目可以不答，我会用宽泛建议列入待确认草稿；也可回复“按建议继续”。计算路线需研究设计核验。")
    if proposal:
        result["proposal"] = proposal
    return result


def handle_turn(client, model: str, state: dict, text: str) -> dict:
    text = text.strip()
    if state.get("stage") in {"bundle", "confirmation"} and text in {"不采用", "不用方案", "先不采用"}:
        state["proposal"] = None
        return offer_confirmation(state, process_turn(state, text, Extraction()))
    if state.get("stage") == "confirmation" and text in {"确认", "确认继续", "按此继续", "就按这个", "同意", "采用方案"}:
        for field, entry in state["draft"].items():
            if entry.get("source") == "system_suggestion":
                state["fields"][field] = {**entry, "source": "confirmed_suggestion",
                                          "quote": text, "turn": len(state["turns"]) + 1}
        state["evaluation_plan"] = state.get("proposal")
        state["proposal"] = None
        state["draft"] = None
        state["stage"] = None
        result = process_turn(state, text, Extraction())
        result["calculation_status"] = "pending_research_design"
        if state.get("evaluation_plan"):
            result["evaluation_plan"] = state["evaluation_plan"]
        return result

    if state.get("stage") == "bundle" and text in {"按建议继续", "采用方案", "按这个方案", "同意"}:
        updates = Extraction()
    else:
        updates = contextual_reply(state, text) or extract(client, model, state, text)
    previous = deepcopy(state["fields"])
    result = process_turn(state, text, updates)
    task_type = state["fields"].get("task_type", {}).get("value")
    if task_type not in TASK_TYPES:
        state["stage"] = None
        return result

    metric = state["fields"].get("metric", {})
    if metric.get("status") == "specified":
        state["proposal"] = None
    elif task_type != "mechanism_validation" or state["fields"].get("hypothesis", {}).get("status") == "specified":
        changed = any(previous.get(field) != state["fields"].get(field)
                      for field in ("task_type", "material", "application", "metric"))
        if not state.get("proposal") or changed or text.startswith("修改"):
            state["proposal"] = propose_evaluation(client, model, state, text if text.startswith("修改") else "").model_dump()

    if state.get("stage") in {"bundle", "confirmation"}:
        return offer_confirmation(state, result)
    return offer_bundle(state, result)


def main() -> None:
    parser = argparse.ArgumentParser(description="LAPIS 研究请求澄清原型")
    parser.add_argument("--text", help="只处理一轮输入；省略后进入交互模式")
    args = parser.parse_args()
    key = os.getenv("LAPIS_API_KEY") or os.getenv("DEEPSEEK_API_KEY")
    if not key:
        parser.error("请设置 LAPIS_API_KEY 或 DEEPSEEK_API_KEY")
    model = os.getenv("LAPIS_MODEL", "deepseek-flash")
    base_url = os.getenv("LAPIS_BASE_URL", "https://api.deepseek.com")
    client = instructor.from_openai(
        OpenAI(api_key=key, base_url=base_url, timeout=40), mode=instructor.Mode.JSON
    )
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
        if result["ready"]:
            print("规范请求已形成；如需修改继续输入，直接回车结束。")
            try:
                pending_text = input("修改> ")
                if not pending_text.strip():
                    break
            except (EOFError, KeyboardInterrupt):
                break
            continue
        print(result["next_question"])


if __name__ == "__main__":
    main()
