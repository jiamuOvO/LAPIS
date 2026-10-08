"""Current intake contract and small deterministic semantic gates."""
from __future__ import annotations

from decimal import Decimal
import hashlib
import json
import re

CONTRACT_VERSION = 4
RULE_VERSION = "intake-rules-4.0"
FIELDS = ("purpose", "research_object", "application", "work_conditions",
          "target_performance", "constraints", "research_scope", "material_function")
LIST_FIELDS = {"target_performance", "constraints"}
CONFIRM_WORDS = {"确认", "确认继续", "按此继续", "就按这个", "同意"}


def content_hash(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                   separators=(",", ":")).encode()).hexdigest()


def temperature_values(text):
    values = []
    for number, unit in re.findall(r"(-?\d+(?:\.\d+)?)\s*(摄氏度|℃|°C|°c|[Kk])", text or ""):
        amount = Decimal(number)
        if unit not in {"K", "k"}:
            amount += Decimal("273.15")
        values.append(str(amount.normalize()))
    return values


def component_predicate(entry):
    """Prefer explicit language over model predicates for current composition rules."""
    text = entry.get("value") or ""
    patterns = [
        ("forbid", r"(?:不得使用|禁止使用|不使用|不能使用|不能用|禁用|不含|不得含|不用)\s*(.+)"),
        ("require", r"(?:必须使用|必须含有|必须含|要求使用|必需使用)\s*(.+)"),
        ("allow", r"(?:允许使用|允许含有)\s*(.+)"),
    ]
    for operator, pattern in patterns:
        match = re.search(pattern, text)
        if match:
            target = re.split(r"[，。；,;]|是硬|作为|这个|这一", match.group(1))[0].strip()
            if target:
                return {"target": target.casefold().replace(" ", ""), "operator": operator,
                        "scope": (entry.get("predicate") or {}).get("scope", "current")}
    predicate = entry.get("predicate")
    if predicate and predicate.get("target") and predicate.get("operator") in {"require", "forbid", "allow"}:
        return {**predicate, "target": predicate["target"].casefold().replace(" ", ""),
                "scope": predicate.get("scope", "current")}
    return None


def domain_from_fields(fields, proposed=None):
    text = " ".join((fields.get(k) or {}).get("value") or ""
                    for k in ("purpose", "research_object", "application", "research_scope"))
    # These are research-purpose combinations, not a rejection of every occurrence of 药.
    if (re.search(r"药物|药效|drug", text, re.I) and
            re.search(r"先导|药物候选|药效|靶蛋白.*结合|结合.*靶蛋白|药物.*筛选", text)):
        return "drug_discovery"
    if proposed:
        return proposed
    if re.search(r"电池|电解液|合金|聚合物|涂层|催化剂|包装|复合材料", text):
        return "materials_application"
    return "uncertain"


def request_issues(payload):
    fields = payload.get("fields", {})
    issues = []
    def add(code, field, message, entries=()):
        issues.append({"id": code + ":" + field, "kind": code, "field": field,
                       "message": message, "entry_ids": list(entries), "status": "open"})
    domain = payload.get("domain", "uncertain")
    if domain != "materials_application":
        add("domain", "application" if fields.get("research_object", {}).get("value") else "research_object", "首版不受理药物筛选。" if domain == "drug_discovery"
            else "请澄清研究对象与材料用途；无应用场景的基础研究保持草稿。")
    for field in FIELDS:
        value = fields.get(field)
        entries = value if isinstance(value, list) else [value]
        if not entries or any(not isinstance(x, dict) for x in entries):
            add("shape", field, "字段记录不完整。")
            continue
        for entry in entries:
            if entry.get("status") == "unclear" or entry.get("needs_review"):
                add("ambiguity", field, "这项信息需要先澄清或重新核对。", [entry.get("id")])
            if entry.get("status") == "specified":
                if not (entry.get("value") or "").strip():
                    add("missing", field, "明确的信息不能为空。")
                if field == "constraints" and entry.get("strength") not in {"hard", "preference"}:
                    add("qualifier", field, "请区分硬约束与偏好。", [entry.get("id")])
                if payload.get("contract_version", 0) >= 4 and field == "target_performance" and "性价比" in (entry.get("value") or ""):
                    add("ambiguity", field, "性价比需要拆分：按什么口径评估成本，以及优先比较传输、稳定性还是其他性能？", [entry.get("id")])
                if field == "target_performance" and not (entry.get("direction") or "").strip():
                    add("qualifier", field, "请明确性能方向，定性方向也可以。", [entry.get("id")])
        if field in {"purpose", "research_object", "application", "research_scope", "material_function"}:
            entry = entries[0]
            if entry.get("status") != "specified" or entry.get("source") not in {"user", "confirmed_suggestion"}:
                add("missing", field, "请明确或采用这项研究信息。")
    goals = fields.get("target_performance") or []
    if not any(x.get("status") == "specified" for x in goals if isinstance(x, dict)):
        add("missing", "target_performance", "请明确本轮性能目标及方向。")
    conditions = fields.get("work_conditions") or {}
    text = (conditions.get("value") or "") + " " + (conditions.get("quote") or "")
    temperatures = set(temperature_values(text))
    if len(temperatures) > 1 and re.search(r"或|还是|可能|不确定|either", text, re.I):
        add("temperature", "work_conditions", "温度候选及单位不同；请选择或说明要比较这些条件。")
    for quantity in conditions.get("temperatures", []):
        declared = temperature_values(str(quantity.get("value")) + str(quantity.get("unit")))
        quoted = temperature_values(conditions.get("quote") or "")
        if quoted and declared and not set(declared) <= set(quoted):
            add("temperature", "work_conditions", "结构化温度与用户原话不一致。")
    predicates = {}
    for entry in fields.get("constraints") or []:
        if entry.get("status") != "specified" or entry.get("strength") != "hard":
            continue
        p = component_predicate(entry)
        if p:
            key = (p["target"], p["scope"])
            predicates.setdefault(key, []).append((p["operator"], entry.get("id")))
    for (target, scope), values in predicates.items():
        operators = {x[0] for x in values}
        if "forbid" in operators and operators & {"require", "allow"}:
            add("conflict", "constraints", "同一范围内组分“" + target + "”同时被要求/允许与禁用。",
                [x[1] for x in values])
    for issue in payload.get("issues", []):
        if issue.get("status", "open") != "resolved":
            issues.append(issue)
    return issues


def _validate_request(payload, version, rule):
    if not isinstance(payload, dict) or payload.get("contract_version") != version:
        raise ValueError("进入新版研究设计前须重新核对并确认当前版本研究请求")
    if set(payload.get("fields", {})) != set(FIELDS) or not payload.get("original_intent"):
        raise ValueError("研究请求必须有原始意图及八项输出")
    if payload.get("rule_version") != rule:
        raise ValueError("研究请求需要按当前规则重新核对")
    for field in FIELDS:
        values = payload["fields"][field]
        if field in LIST_FIELDS:
            if not isinstance(values, list) or not values or not all(isinstance(e, dict) for e in values):
                raise ValueError("列表字段记录无效")
            ids = [e.get("id") for e in values]
            if any(not i for i in ids) or len(ids) != len(set(ids)):
                raise ValueError("条目缺少唯一 ID")
        elif not isinstance(values, dict):
            raise ValueError("标量字段记录无效")
    if domain_from_fields(payload["fields"], payload.get("domain")) != "materials_application":
        raise ValueError("研究对象与用途不在首版材料应用范围")
    issues = request_issues(payload)
    if issues:
        raise ValueError(issues[0]["message"])
    sources = payload.get("sources", {})
    for field, value in payload["fields"].items():
        for entry in value if isinstance(value, list) else [value]:
            if entry.get("status") not in {"unknown", "none", "open", "specified", "unclear"}:
                raise ValueError("字段状态无效")
            if entry.get("source") == "system_suggestion":
                raise ValueError("建议尚未采用")
            if entry.get("source") == "user" and (not entry.get("quote") or not isinstance(entry.get("turn"), int)):
                raise ValueError("缺少用户原话或轮次")
            if entry.get("source") == "confirmed_suggestion":
                if not entry.get("recommendation_ref") or not entry.get("selection_quote") or not isinstance(entry.get("confirmed_turn"), int):
                    raise ValueError("缺少推荐选择记录")
                refs = entry.get("source_refs", [])
                if version == 4:
                    proposal = payload.get("proposals", {}).get(entry["recommendation_ref"]["id"])
                    if not proposal or proposal.get("version") != entry["recommendation_ref"].get("version") or entry["recommendation_ref"].get("direction_id") not in proposal.get("direction_ids", []):
                        raise ValueError("缺少匹配的提案生成记录")
                    if entry.get("suggestion_origin") == "model":
                        if entry.get("evidence_status") != "unverified" or proposal.get("generation", {}).get("origin") != "model":
                            raise ValueError("模型研究提案不得标为已核验")
                        if not proposal["generation"].get("model") or not proposal["generation"].get("prompt_sha256"):
                            raise ValueError("提案缺少模型和提示词版本")
                    elif entry.get("suggestion_origin") != "catalogue" or entry.get("evidence_status") != "source_checked":
                        raise ValueError("提案来源类别无效")
                if (not refs and (version == 3 or entry.get("suggestion_origin") != "model")) or any(ref not in sources for ref in refs):
                    raise ValueError("缺少可追溯的推荐依据")
                for ref in refs:
                    source = sources[ref]
                    body = {k: v for k, v in source.items() if k not in {"content_sha256", "catalog_version"}}
                    if source.get("verification") != "source_claim_checked" or not source.get("limitations") or source.get("content_sha256") != content_hash(body):
                        raise ValueError("推荐来源未经核查或快照校验失败")
    if not isinstance(payload.get("draft_revision"), int) or not payload.get("draft_id"):
        raise ValueError("请求缺少草稿修订标识")


def validate_research_request_v3(payload):
    """Historical validator remains pinned to its original contract."""
    _validate_request(payload, 3, "intake-rules-3.1")

def validate_research_request_v4(payload):
    allowed = {"contract_version", "rule_version", "original_intent", "draft_revision", "draft_id",
               "fields", "task_pattern", "domain", "issues", "reference_notes", "sources", "proposals"}
    if not isinstance(payload, dict) or set(payload) - allowed:
        raise ValueError("研究请求含未知字段")
    records_allowed = {"status", "value", "source", "quote", "turn", "id", "direction", "strength",
                       "predicate", "temperatures", "suggested_after_turn", "selection_quote", "confirmed_turn",
                       "recommendation_ref", "source_refs", "suggestion_origin", "evidence_status", "needs_review",
                       "review_quote", "review_turn", "legacy_provenance"}
    for field, values in payload.get("fields", {}).items():
        for record in values if isinstance(values, list) else [values]:
            if not isinstance(record, dict) or set(record) - records_allowed:
                raise ValueError("字段记录含未知键")
            if record.get("value") is not None and not isinstance(record["value"], str):
                raise ValueError("字段值必须为字符串或空值")
            if record.get("status") == "specified" and record.get("source") not in {"user", "confirmed_suggestion"}:
                raise ValueError("明确字段缺少有效来源")
            if record.get("source") not in {None, "user", "confirmed_suggestion"}:
                raise ValueError("字段来源未经重新核对")
    _validate_request(payload, CONTRACT_VERSION, RULE_VERSION)
