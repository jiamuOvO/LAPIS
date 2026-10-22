import json
import io
import os
import unittest
from contextlib import redirect_stdout
from copy import deepcopy
from pathlib import Path
import sys
import tempfile
from unittest.mock import Mock, patch
from pydantic import ValidationError

from lapis import main, run_design_revision_suggestion, run_simulation
from lapis_core import digest
from lapis_design import DesignDraft, render_design
from lapis_store import (approve_design, check_design, connect, create_task, design_edit_document,
                         execution_plan_template, freeze_execution, get_design, get_request_version, get_task,
                         initialize_schema, preview_design_revision, propose_design,
                         propose_design_revision, propose_execution_plan, save_turn, verify_artifacts)
from test_lapis_store import confirmed_v4_request


def create_confirmed_task(actor="module2-test"):
    task = create_task(actor)
    request = confirmed_v4_request()
    state = {"contract_version": 5, "draft_id": request["draft_id"], "fields": request["fields"],
             "turns": ["合成测试研究意图", "确认"], "stage": "ready_for_design"}
    result = {"ready": True, "ready_for_design": True, "confirmation_event": True,
              "intake_status": "ready_for_design", "request": request,
              "calculation_status": "pending_research_design"}
    save_turn(task, 0, state, result, actor, "offline-fixture")
    return task


def source_design(paradigm="comparison", gaps=True):
    hypotheses = []
    if paradigm == "mechanism_verification":
        hypotheses = [{"id": "h1", "statement": "合成待检验假设", "predicted_observations": ["指标 M 改变"],
                       "falsifier": "指标 M 未出现预测变化", "alternatives": ["替代解释"],
                       "basis_fields": ["purpose"]}]
    return {"schema": "lapis.design-draft.v1", "request_version": 1, "paradigm": paradigm,
            "rationale": "仅用于验证 LAPIS 软件流程，不是材料研究结论。",
            "candidates": [{"id": "A", "description": "测试候选 A", "composition": {"fixture": "A"},
                           "basis_fields": ["research_object"]},
                          {"id": "B", "description": "测试候选 B", "composition": {"fixture": "B"},
                           "basis_fields": ["research_object"]}],
            "experiments": [{"id": "e1", "question": "测试实验问题", "candidate_ids": ["A", "B"],
                             "observable": "位移", "unit": "m", "conditions": {"temperature": "298 K"},
                             "method_suggestion": None, "model_suggestion": None,
                             "quality_checks_to_define": ["需研究人员定义"], "basis_fields": ["purpose"]}],
            "hypotheses": hypotheses, "open_questions": ([{"field": "model", "question": "测试计划需填模型来源",
                                                               "blocks_execution": True}] if gaps else []),
            "limitations": ["离线软件测试数据，不构成科研证据。"], "evidence_status": "unverified"}


def ready_plan(task, source_version, *, scope="software_test", paradigm="comparison"):
    plan = execution_plan_template(task, source_version)
    plan["provenance"]["operation_id"] = "offline-plan-" + task
    plan["execution_scope"] = scope
    plan["budget"] = {"estimated_resources": {"cpu": "software test only"}, "total_runs": 2,
                      "max_walltime_minutes": 5, "scope": "仅限隔离软件测试，不授权科研计算"}
    for candidate in plan["candidates"]:
        if not candidate["composition"]:
            candidate["composition"] = {"fixture": candidate["id"]}
    for index, experiment in enumerate(plan["experiments"]):
        experiment.update(observable_unit=experiment.get("observable_unit") or "m",
                          conditions={"temperature": {"value": 298, "unit": "K"}},
                          method="offline workflow fixture", method_version="1", model="fixture model",
                          model_version="1", parameters={"steps": "10"}, quality_rules=["fixture status only"],
                          expected_raw_files=["simulation.json"], max_walltime_minutes=1)
        if paradigm == "mechanism_verification":
            experiment["hypothesis_ids"] = ["h1"]
    if plan["gap_dispositions"]:
        plan["gap_dispositions"][0].update(status="resolved", reason="只在软件测试方案中填写的模型占位。",
                                           basis_fields=["research_object"], plan_pointer="/experiments/0/model")
    if paradigm == "screening":
        plan["screening_criteria"] = ["fixture criterion"]
    elif paradigm == "mechanism_verification":
        plan["hypothesis_tests"] = {"h1": [item["id"] for item in plan["experiments"]]}
    return plan


@unittest.skipUnless(os.getenv("LAPIS_TEST_PG") == "1" and os.getenv("LAPIS_DB_NAME") == "lapis_test",
                     "requires isolated lapis_test PostgreSQL")
class DesignReviewTest(unittest.TestCase):
    def setUp(self):
        initialize_schema()

    def save_source(self, task, paradigm="comparison", gaps=True):
        return propose_design(task, source_design(paradigm, gaps), "offline-fixture")

    def test_readable_check_export_and_revision_are_non_destructive_and_idempotent(self):
        task = create_confirmed_task()
        version = self.save_source(task)
        record = get_design(task, version)
        self.assertTrue(record["payload"]["open_questions"][0]["id"].startswith("gap-"))
        output = render_design(record, get_request_version(task, 1)["payload"])
        self.assertIn("科研适用性：待研究人员审核", output)
        self.assertIn("依据=research_object=材料", output)
        with connect() as db:
            events_before = db.execute("SELECT count(*) FROM audit_events WHERE task_id=%s", (task,)).fetchone()[0]
        report = check_design(task, version)
        self.assertFalse(report["approval_ready"])
        self.assertTrue(any("阻断" in issue for issue in report["issues"]))
        with connect() as db:
            events_after = db.execute("SELECT count(*) FROM audit_events WHERE task_id=%s", (task,)).fetchone()[0]
        self.assertEqual(events_before, events_after)

        edit = design_edit_document(task, version, "reviewer")
        edit["operation_id"] = "revision-op-1"
        edit["reason"] = "明确了测试设计的研究问题"
        edit["payload"]["rationale"] = "修改后的离线流程测试问题。"
        before = get_design(task, version)["payload"]
        preview = preview_design_revision(task, edit)
        self.assertFalse(preview["adopted"])
        self.assertEqual(get_design(task, version)["payload"], before)

        new_version, saved, reused = propose_design_revision(task, edit, "reviewer")
        self.assertFalse(reused)
        self.assertEqual(saved["revision"]["parent_version"], version)
        replay_version, replay, reused = propose_design_revision(task, edit, "reviewer")
        self.assertTrue(reused)
        self.assertEqual((replay_version, replay), (new_version, saved))
        self.assertEqual(get_design(task, version)["payload"], before)
        changed_replay = deepcopy(edit)
        changed_replay["payload"]["rationale"] = "同一操作标识提交不同修订内容。"
        with self.assertRaisesRegex(ValueError, "不同修订输入"):
            propose_design_revision(task, changed_replay, "reviewer")

        stale = deepcopy(edit)
        stale["operation_id"] = "stale-op"
        with self.assertRaisesRegex(ValueError, "不是最新|已过期"):
            preview_design_revision(task, stale)

    def test_fec_style_unresolved_scientific_gaps_block_plan_and_approval(self):
        task = create_confirmed_task()
        source = self.save_source(task, gaps=True)
        template = execution_plan_template(task, source)
        template["provenance"]["operation_id"] = "fec-still-blocked"
        with self.assertRaisesRegex(ValueError, "未解决的执行阻断缺口"):
            propose_execution_plan(task, template, "offline-fixture")
        template["gap_dispositions"][0].update(status="resolved", reason="设置完成标志", basis_fields=["purpose"],
                                               plan_pointer="/experiments/999/model")
        with self.assertRaisesRegex(ValueError, "不存在或为空"):
            propose_execution_plan(task, template, "offline-fixture")
        self.assertEqual(get_task(task)["active_request_version"], 1)

    def test_supplied_fec_case_remains_unverified_and_all_open_science_items_block(self):
        case = json.loads(Path("cases/fec_screening_draft.json").read_text(encoding="utf-8"))
        self.assertEqual(case["origin"], "demo")
        self.assertFalse(case["execution_allowed"])
        self.assertIsNone(case["scientific_result"])
        task = create_confirmed_task()
        questions = [{"field": "formulation", "question": question, "blocks_execution": True}
                     for question in case["open_questions"]]
        questions.extend([
            {"field": "model", "question": "覆盖 LiPF6、EC、DMC、FEC 的完整力场未审定", "blocks_execution": True},
            {"field": "quality", "question": "轨迹质量与收敛判据未获科研审核", "blocks_execution": True},
        ])
        fec = {"schema": "lapis.design-draft.v1", "request_version": 1, "paradigm": "comparison",
               "rationale": "基于提供的演示案例作软件审阅；所有数值均是待审查建议。",
               "candidates": [{"id": item["id"], "description": json.dumps(item, ensure_ascii=False),
                               "composition": {"system": "EC/DMC-LiPF6; proposed FEC=" + item["fec"]},
                               "basis_fields": ["research_object"]} for item in case["candidates"]],
               "experiments": [{"id": "diffusion", "question": "比较 Li+ 自扩散系数",
                                "candidate_ids": [item["id"] for item in case["candidates"]],
                                "observable": case["observable"]["name"], "unit": None,
                                "conditions": {}, "method_suggestion": None, "model_suggestion": None,
                                "quality_checks_to_define": ["待研究人员定义轨迹质量与收敛规则"],
                                "basis_fields": ["target_performance"]}],
               "hypotheses": [], "open_questions": questions,
               "limitations": ["演示案例不是已批准的研究方案或计算结果。"], "evidence_status": "unverified"}
        version = propose_design(task, fec, "offline-fec-review")
        report = check_design(task, version)
        self.assertFalse(report["approval_ready"])
        self.assertEqual(len(get_design(task, version)["payload"]["open_questions"]), len(questions))
        template = execution_plan_template(task, version)
        template["provenance"]["operation_id"] = "fec-case-blocked"
        with self.assertRaisesRegex(ValueError, "未解决的执行阻断缺口"):
            propose_execution_plan(task, template, "offline-fec-review")

    def test_comparison_plan_requires_sources_explicit_review_and_test_scope_for_simulator(self):
        task = create_confirmed_task()
        source = self.save_source(task, gaps=True)
        plan = ready_plan(task, source)
        bad_relation = deepcopy(plan)
        bad_relation["comparison_pairs"] = [["A", "missing"]]
        with self.assertRaisesRegex(ValueError, "候选比较关系"):
            propose_execution_plan(task, bad_relation, "offline-fixture")
        version, stored, reused = propose_execution_plan(task, plan, "offline-fixture")
        self.assertFalse(reused)
        replay_version, replay, reused = propose_execution_plan(task, plan, "offline-fixture")
        self.assertTrue(reused)
        self.assertEqual((replay_version, replay), (version, stored))
        changed_plan = deepcopy(plan)
        changed_plan["purpose"] = "同一 operation_id 的不同内容"
        with self.assertRaisesRegex(ValueError, "不同执行计划"):
            propose_execution_plan(task, changed_plan, "offline-fixture")
        self.assertTrue(check_design(task, version)["approval_ready"])
        with self.assertRaisesRegex(ValueError, "审核声明"):
            approve_design(task, version, "reviewer", "太短")
        approve_design(task, version, "reviewer", "已检查来源候选、比较条件、预算和软件测试边界")
        execution = freeze_execution(task, version, "offline-fixture")
        self.assertEqual(execution, freeze_execution(task, version, "offline-fixture"))
        with self.assertRaises(RuntimeError):
            run_simulation(execution, "offline-simulator", fail=True)
        run_simulation(execution, "offline-simulator", fail=False)
        manifest = verify_artifacts(execution)
        self.assertEqual(len(manifest), 1)
        self.assertEqual(manifest[0]["origin"], "simulation")
        artifact = Path("artifacts-test") / manifest[0]["relative_path"]
        self.assertIsNone(json.loads(artifact.read_text(encoding="utf-8"))["scientific_result"])
        with connect() as db:
            event = db.execute("SELECT details FROM audit_events WHERE task_id=%s AND action='design_approved' "
                               "ORDER BY id DESC LIMIT 1", (task,)).fetchone()[0]
        self.assertEqual(event["payload_sha256"], digest(stored))
        self.assertFalse(event["identity_authenticated"])

    def test_screening_and_mechanism_verification_contracts(self):
        for paradigm in ("screening", "mechanism_verification"):
            with self.subTest(paradigm=paradigm):
                task = create_confirmed_task()
                source = self.save_source(task, paradigm=paradigm, gaps=False)
                plan = ready_plan(task, source, paradigm=paradigm)
                if paradigm == "screening":
                    plan["screening_criteria"] = []
                    with self.assertRaisesRegex(ValueError, "筛选范式"):
                        propose_execution_plan(task, plan, "offline-fixture")
                    plan["screening_criteria"] = ["fixture criterion"]
                else:
                    plan["hypothesis_tests"] = {}
                    with self.assertRaisesRegex(ValueError, "检验实验"):
                        propose_execution_plan(task, plan, "offline-fixture")
                    plan["hypothesis_tests"] = {"h1": [item["id"] for item in plan["experiments"]]}
                version, _, _ = propose_execution_plan(task, plan, "offline-fixture")
                self.assertTrue(check_design(task, version)["approval_ready"])

    def test_plan_tampering_units_missing_fields_and_budget_limits_are_rejected(self):
        task = create_confirmed_task()
        source = self.save_source(task, gaps=False)
        valid = ready_plan(task, source)
        bad = deepcopy(valid)
        bad["provenance"]["source_design_sha256"] = "0" * 64
        with self.assertRaisesRegex(ValueError, "来源设计哈希不一致"):
            propose_execution_plan(task, bad, "offline-fixture")

        bad = deepcopy(valid)
        bad["experiments"][0]["conditions"]["temperature"]["unit"] = "bananas"
        with self.assertRaises(ValidationError):
            propose_execution_plan(task, bad, "offline-fixture")
        bad = deepcopy(valid)
        bad["experiments"][0]["conditions"]["temperature"]["unit"] = "bar"
        with self.assertRaises(ValidationError):
            propose_execution_plan(task, bad, "offline-fixture")
        bad = deepcopy(valid)
        bad["experiments"][0]["quality_rules"] = []
        with self.assertRaises(ValidationError):
            propose_execution_plan(task, bad, "offline-fixture")
        bad = deepcopy(valid)
        bad["budget"]["max_walltime_minutes"] = 1
        with self.assertRaisesRegex(ValueError, "总最长时限"):
            propose_execution_plan(task, bad, "offline-fixture")
        bad = deepcopy(valid)
        bad["experiments"][0]["source_experiment_id"] = "missing-source"
        with self.assertRaisesRegex(ValueError, "来源设计中的实验 ID"):
            propose_execution_plan(task, bad, "offline-fixture")
        bad = deepcopy(valid)
        bad["candidates"].append(deepcopy(bad["candidates"][0]))
        with self.assertRaises(ValidationError):
            propose_execution_plan(task, bad, "offline-fixture")

    def test_bound_plan_temperature_observable_accepts_kelvin_and_rejects_unknown_units(self):
        for unit in ("K", "bananas", "bar"):
            with self.subTest(unit=unit):
                task = create_confirmed_task()
                source = source_design(gaps=False)
                source["experiments"][0].update(observable="temperature", unit=unit)
                version = propose_design(task, source, "offline-temperature-fixture")
                plan = ready_plan(task, version)
                if unit == "K":
                    proposed, _, _ = propose_execution_plan(task, plan, "offline-temperature-fixture")
                    self.assertTrue(check_design(task, proposed)["approval_ready"])
                else:
                    with self.assertRaises(ValidationError):
                        propose_execution_plan(task, plan, "offline-temperature-fixture")

    def test_model_revision_suggestion_is_not_saved_until_explicit_adoption(self):
        task = create_confirmed_task()
        source = self.save_source(task)
        before = get_design(task, source)
        suggestion = {"paradigm": "comparison", "rationale": "模型建议待审阅",
                      "candidates": before["payload"]["candidates"], "experiments": before["payload"]["experiments"],
                      "hypotheses": [], "open_questions": before["payload"]["open_questions"],
                      "limitations": before["payload"]["limitations"]}
        with patch.dict(os.environ, {"LAPIS_API_KEY": "offline-only"}), \
             patch("lapis.OpenAI", return_value=Mock()), patch("lapis.instructor.from_openai", return_value=Mock()), \
             patch("lapis.generate_design_revision", return_value=DesignDraft.model_validate(suggestion)):
            envelope = run_design_revision_suggestion(task, source, "改写目的", "suggestion-op", "researcher")
        self.assertEqual(envelope["proposal_origin"], "model_suggestion")
        self.assertEqual(get_design(task, source), before)
        with patch("lapis.generate_design_revision", side_effect=AssertionError("replay must not call model")):
            replay = run_design_revision_suggestion(task, source, "改写目的", "suggestion-op", "researcher")
        self.assertEqual(replay, envelope)
        with self.assertRaisesRegex(ValueError, "不同修订建议输入"):
            run_design_revision_suggestion(task, source, "另一个要求", "suggestion-op", "researcher")

    def test_revision_of_approved_plan_requires_new_approval_and_old_freeze_is_blocked(self):
        task = create_confirmed_task()
        source = self.save_source(task, gaps=False)
        plan = ready_plan(task, source)
        plan["gap_dispositions"] = []
        version, _, _ = propose_execution_plan(task, plan, "offline-fixture")
        approve_design(task, version, "reviewer", "已审核测试计划来源、比较关系、资源范围和软件边界")
        envelope = design_edit_document(task, version, "reviewer")
        envelope.update(operation_id="plan-revision-op", reason="修正软件测试步数")
        envelope["payload"]["experiments"][0]["parameters"]["steps"] = "20"
        new_version, _, _ = propose_design_revision(task, envelope, "reviewer")
        self.assertGreater(new_version, version)
        self.assertFalse(check_design(task, version)["approval_ready"])
        with self.assertRaisesRegex(ValueError, "更新的设计"):
            freeze_execution(task, version, "researcher")
        with self.assertRaisesRegex(ValueError, "审核"):
            freeze_execution(task, new_version, "researcher")
        approve_design(task, new_version, "reviewer", "已重新审核修订后的测试计划及其来源哈希")
        self.assertTrue(freeze_execution(task, new_version, "researcher"))

    def test_cli_design_plan_review_freeze_and_simulation_path(self):
        task = create_confirmed_task()
        source = self.save_source(task, gaps=False)

        def cli(*argv):
            output = io.StringIO()
            with patch.object(sys, "argv", ["lapis.py", *map(str, argv)]), redirect_stdout(output):
                main()
            return output.getvalue()

        readable = cli("show-design", task, source)
        self.assertIn("研究范式", readable)
        self.assertIn("待研究人员审核", readable)
        with tempfile.TemporaryDirectory(prefix="lapis-module2-cli-") as directory:
            plan_path = Path(directory) / "execution-plan.json"
            cli("export-plan", task, source, plan_path)
            plan = json.loads(plan_path.read_text(encoding="utf-8"))
            plan["execution_scope"] = "software_test"
            plan["provenance"]["operation_id"] = "cli-plan-operation"
            plan["budget"] = {"estimated_resources": {"cpu": "isolated test"}, "total_runs": 2,
                              "max_walltime_minutes": 5, "scope": "仅供 CLI 软件流程验收"}
            for experiment in plan["experiments"]:
                experiment.update(conditions={"temperature": {"value": 298, "unit": "K"}},
                                  method="offline fixture", method_version="1", model="fixture model",
                                  model_version="1", parameters={"steps": "10"}, quality_rules=["软件状态检查"],
                                  expected_raw_files=["simulation.json"], max_walltime_minutes=1)
            plan_path.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
            response = json.loads(cli("propose-plan", task, plan_path))
            version = response["version"]
            checked = cli("check-design", task, version, "--json")
            self.assertTrue(json.loads(checked)["approval_ready"])
            cli("approve-design", task, version, "--reviewer", "cli-reviewer", "--statement",
                "已审核来源设计、比较关系、执行参数和软件测试边界")
            self.assertEqual(json.loads(cli("show-design", task, version, "--debug"))["status"], "approved")
            execution = cli("freeze", task, version).strip()
            cli("simulate", execution)
            manifest = json.loads(cli("verify-artifacts", execution))
            self.assertEqual(manifest[0]["origin"], "simulation")
            artifact = Path("artifacts-test") / manifest[0]["relative_path"]
            self.assertIsNone(json.loads(artifact.read_text(encoding="utf-8"))["scientific_result"])


if __name__ == "__main__":
    unittest.main()
