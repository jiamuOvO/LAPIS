"""Isolated, reproducible intake checks. Never executes scientific calculations."""
import argparse
from contextlib import redirect_stdout
from copy import deepcopy
import io
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from unittest.mock import patch
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import httpx
import instructor
from openai import OpenAI
import lapis_intake
from lapis import render_intake_result
from lapis_contract import RULE_VERSION, content_hash
from lapis_core import digest
from lapis_guidance import load_catalog
from lapis_graph import run_intake_turn
from lapis_intake import Extraction, SYSTEM_PROMPT, PROPOSAL_PROMPT
from lapis_store import create_task, get_task, initialize_schema


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--case", choices=["furan", "edits", "boundaries"], required=True)
    parser.add_argument("--mode", choices=["real", "mock"], default="mock")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if os.getenv("LAPIS_TEST_PG") != "1" or os.getenv("LAPIS_DB_NAME") != "lapis_test":
        parser.error("Requires LAPIS_TEST_PG=1 and LAPIS_DB_NAME=lapis_test.")
    initialize_schema()
    model = os.getenv("LAPIS_MODEL", "deepseek-flash") if args.mode == "real" else "controlled-extraction"
    events, extracted = [], []
    def response_event(response):
        item = {"status_code": response.status_code}
        if response.status_code == 200:
            response.read()
            try:
                body = response.json()
                item.update(provider_model=body.get("model"), usage=body.get("usage"), completion_id=body.get("id"))
            except ValueError:
                pass
        events.append(item)
    client = None
    if args.mode == "real":
        key = os.getenv("LAPIS_API_KEY") or os.getenv("DEEPSEEK_API_KEY")
        if not key:
            parser.error("No API credential. Real validation was not performed.")
        client = instructor.from_openai(OpenAI(
            api_key=key, base_url=os.getenv("LAPIS_BASE_URL", "https://api.deepseek.com"),
            max_retries=0, timeout=40, http_client=httpx.Client(event_hooks={"response": [response_event]}),
        ), mode=instructor.Mode.JSON)
    original_extract = lapis_intake.extract
    controlled = None
    def traced_extract(client, model, state, text):
        value = original_extract(client, model, state, text) if args.mode == "real" else controlled
        if value is None:
            raise ValueError("Missing controlled extraction")
        extracted.append(value.model_dump())
        return value
    prompt_hash = digest({"extract": SYSTEM_PROMPT, "proposal": PROPOSAL_PROMPT})
    catalog = load_catalog()
    run_id = str(uuid4())
    baseline = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    code_hashes = {name: hashlib.sha256((ROOT / name).read_bytes().replace(b"\r\n", b"\n")).hexdigest()
                   for name in ("lapis.py", "lapis_intake.py", "lapis_contract.py", "lapis_guidance.py", "lapis_graph.py", "lapis_store.py")}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    counts = {"passed": 0, "failed": 0, "errors": 0}
    task_id, context = None, {}
    def reset():
        nonlocal task_id, context
        task_id, context = create_task("acceptance-" + args.mode), {}
    def step(name, text, expectation, predicate, mock=None):
        nonlocal context, controlled
        events.clear()
        extracted.clear()
        controlled = Extraction.model_validate(mock) if mock is not None else None
        before_context = deepcopy(context)
        operation_id = str(uuid4())
        started = time.perf_counter()
        record = {"run_id": run_id, "case": args.case, "step": name, "mode": args.mode,
                  "task_id": task_id, "operation_id": operation_id, "input": text,
                  "input_context": before_context, "expectation": expectation,
                  "model": model, "model_pinning": "provider_alias_unpinned",
                  "prompt_sha256": prompt_hash, "rule_version": RULE_VERSION,
                  "contract_version": 3, "catalog_version": catalog["version"],
                  "catalog_sha256": content_hash(catalog), "baseline_commit": baseline, "code_sha256": code_hashes}
        try:
            with patch("lapis_intake.extract", side_effect=traced_extract):
                result = run_intake_turn(task_id, text, "acceptance-" + args.mode, client, model,
                                         prompt_hash, operation_id, input_context=before_context)
            state = get_task(task_id)
            record.update(output=result, active_request_version=state["active_request_version"])
            shown = io.StringIO()
            with redirect_stdout(shown):
                render_intake_result(result["result"])
            record["displayed_output"] = shown.getvalue()
            context = result["result"].get("input_context", {})
            record["judgment"] = "passed" if predicate(result["result"], state) else "failed"
            counts[record["judgment"]] += 1
        except Exception as error:
            record.update(judgment="error", error_type=type(error).__name__)
            counts["errors"] += 1
        record.update(elapsed_seconds=round(time.perf_counter() - started, 3),
                      extraction=deepcopy(extracted), http_calls=deepcopy(events),
                      retry_count=max(0, len(events) - 1), llm_called=bool(events))
        with args.output.open("a", encoding="utf-8") as output:
            output.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
        print(name, record["judgment"], record["elapsed_seconds"], flush=True)
    def update(field, value, quote=None, **kwargs):
        return dict(field=field, status="specified", value=value, quote=quote or value, **kwargs)

    if args.case == "furan":
        reset()
        step("original-question", "我想知道呋喃基 分子有什么特性", "Keep object; no material use means blocked.",
             lambda r,s: not r["ready_for_design"] and "呋喃" in str(r["request"]["fields"]["research_object"]["value"]),
             {"domain": "basic_research", "domain_quote": "呋喃基", "updates": [update("research_object", "呋喃基")]})
        step("help-before-use", "我不太了解，你有什么推荐吗", "Show two bounded source-backed directions; do not adopt.",
             lambda r,s: r["intake_status"] == "needs_guidance" and len(r["recommendations"]["options"]) == 2
             and r["request"]["fields"]["application"]["status"] == "unknown", {"actions": ["unsure", "recommend"]})
        step("select-plus-remove", "采用第二个方向，但先不考虑成本", "Adopt sourced direction and process cost withdrawal.",
             lambda r,s: r["intake_status"] == "needs_confirmation" and
             r["request"]["fields"]["research_object"]["source"] == "confirmed_suggestion",
             {"actions": ["select", "edit"], "selected_option": 2, "updates": [
                 dict(field="constraints", status="none", quote="先不考虑成本", action="remove", target_value="成本")]})
        step("confirm-current", "确认", "Only design eligibility; active version 1.",
             lambda r,s: r["ready_for_design"] and s["active_request_version"] == 1)
        step("repeat-confirm", "确认", "No extra immutable version.",
             lambda r,s: r.get("already_confirmed", False) and s["request_version"] == 1)
        step("recommend-plus-new-object", "把研究对象改成铝合金，我不了解，有什么推荐？",
             "Keep new object, no irrelevant furan advice; revoke active version.",
             lambda r,s: "铝合金" in r["request"]["fields"]["research_object"]["value"] and not r["ready_for_design"]
             and r["recommendations"] is None and s["active_request_version"] is None,
             {"actions": ["edit", "recommend", "unsure"], "updates": [update("research_object", "铝合金")]})
    elif args.case == "edits":
        reset()
        text = "我想在高电压电池中比较碳酸酯电解液，只研究电解液，功能是传导锂离子；比较氧化稳定性和离子传输，成本必须低于预算，不用含氟添加剂"
        initial = [
            update("purpose", "比较电解液", "比较碳酸酯电解液"), update("research_object", "碳酸酯电解液"),
            update("application", "高电压电池"), update("research_scope", "只研究电解液"),
            update("material_function", "传导锂离子"), update("target_performance", "氧化稳定性", direction="比较"),
            update("target_performance", "离子传输", direction="比较"),
            update("constraints", "成本必须低于预算", strength="hard"),
            update("constraints", "不使用含氟添加剂", "不用含氟添加剂", strength="hard")]
        step("complete-request", text, "Complete bounded draft.",
             lambda r,s: r["intake_status"] == "needs_confirmation",
             {"updates": initial, "domain": "materials_application", "domain_quote": "高电压电池"})
        step("confirm-initial", "确认", "Active request version 1.",
             lambda r,s: r["ready_for_design"] and s["active_request_version"] == 1)
        step("goal-removal-paraphrase", "氧化稳定性这轮先放下，只保留离子传输这个目标",
             "Remove exact goal, keep other fields, revoke current eligibility.",
             lambda r,s: [x["value"] for x in r["request"]["fields"]["target_performance"]] == ["离子传输"]
             and not r["ready_for_design"] and s["active_request_version"] is None,
             {"actions": ["edit"], "updates": [update("target_performance", "氧化稳定性", "氧化稳定性这轮先放下", action="remove")]})
        step("confirm-plus-preference", "确认，但成本不再是硬要求，只是偏好", "Revise hard cost without confirming.",
             lambda r,s: not r["ready_for_design"] and any(x.get("strength") == "preference"
             and "成本" in (x.get("value") or "") for x in r["request"]["fields"]["constraints"]),
             {"actions": ["confirm", "edit"], "updates": [update("constraints", "成本必须低于预算",
              "成本不再是硬要求，只是偏好", strength="preference", action="update", target_value="成本必须低于预算")]})
        step("confirm-revised", "确认", "New version 2; old version remains.",
             lambda r,s: r["ready_for_design"] and s["active_request_version"] == 2 and s["request_version"] == 2)
    else:
        reset()
        step("two-conflicts", "比较电池碳酸酯电解液，仅研究电解液，作用是传导离子，比较扩散能力；温度是25 K或25℃，必须使用含氟添加剂，同时不得使用含氟添加剂",
             "Temperature and composition independently block confirmation.",
             lambda r,s: not r["ready_for_design"] and {"temperature", "conflict"} <= {x["kind"] for x in r["blocking_issues"]},
             {"updates": [update("purpose", "比较电解液", "比较"), update("research_object", "碳酸酯电解液"),
               update("application", "电池"), update("research_scope", "仅研究电解液"), update("material_function", "传导离子"),
               update("target_performance", "扩散能力", direction="比较"), update("work_conditions", "25 K或25℃"),
               update("constraints", "必须使用含氟添加剂", strength="hard"),
               update("constraints", "不得使用含氟添加剂", strength="hard")]})
        step("confirm-cannot-resolve", "确认", "Conflicts cannot be approved by saying confirm.",
             lambda r,s: not r["ready_for_design"])
        reset()
        step("drug-purpose", "我想筛选小分子药物先导，应用是靶蛋白结合，比较亲和力，只看呋喃类候选",
             "Drug discovery remains outside first-release materials scope.",
             lambda r,s: r["intake_status"] == "unsupported" and s["active_request_version"] is None,
             {"domain": "drug_discovery", "domain_quote": "小分子药物先导", "updates": [
                 update("purpose", "筛选药物先导", "筛选小分子药物先导"), update("research_object", "呋喃类候选"),
                 update("application", "靶蛋白结合"), update("research_scope", "只看呋喃类候选"),
                 update("target_performance", "亲和力", direction="比较")]})
    print(json.dumps(counts), flush=True)
    return int(counts["failed"] > 0 or counts["errors"] > 0)


if __name__ == "__main__":
    raise SystemExit(main())
