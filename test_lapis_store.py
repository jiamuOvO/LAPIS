import os
import unittest
from concurrent.futures import ThreadPoolExecutor
from queue import Queue
from threading import Barrier
from unittest.mock import patch
from pydantic import ValidationError

from lapis import run_design_draft, run_simulation
from lapis_design import DesignDraft, saved_design_payload
from lapis_store import (approve_design, create_task, freeze_execution, get_task,
                         connect, get_current_request, get_design, get_design_operation,
                         initialize_schema, propose_design, propose_generated_design,
                         save_turn, verify_artifacts)


def confirmed_v2_request():
    def user(value, *, direction=None):
        entry = {"status": "specified", "value": value, "source": "user",
                 "quote": "研究意图", "turn": 1}
        if direction:
            entry["direction"] = direction
        return entry
    unknown = {"status": "unknown", "value": None, "source": None, "quote": None, "turn": None}
    return {
        "contract_version": 2, "original_intent": "研究意图", "task_pattern": "comparison",
        "fields": {
            "purpose": user("比较材料"), "research_object": user("材料"),
            "application": user("储能"), "work_conditions": unknown,
            "target_performance": [user("稳定性", direction="比较")],
            "constraints": [unknown], "research_scope": user("材料 A 与 B"),
            "material_function": user("储能"),
        },
        "reference_notes": [],
    }


def confirmed_v4_request():
    from lapis_contract import RULE_VERSION, CONTRACT_VERSION
    payload = confirmed_v2_request()
    payload.update(contract_version=CONTRACT_VERSION, rule_version=RULE_VERSION, domain="materials_application",
                   draft_revision=1, draft_id="draft-1", issues=[], sources={}, proposals={})
    for field in ("target_performance", "constraints"):
        for i, entry in enumerate(payload["fields"][field]):
            entry["id"] = f"{field}-{i}"
    return payload


def design_draft(rationale):
    return DesignDraft.model_validate({
        "paradigm": "comparison", "rationale": rationale,
        "candidates": [{"id": "control", "description": "请求中的对照候选",
                        "composition": {}, "basis_fields": ["research_object"]}],
        "experiments": [{"id": "compare", "question": "对照间是否存在差异？",
                         "candidate_ids": ["control"], "observable": "目标性能", "unit": None,
                         "conditions": {}, "method_suggestion": None, "model_suggestion": None,
                         "quality_checks_to_define": ["定义质量检查"],
                         "basis_fields": ["target_performance"]}],
        "hypotheses": [], "open_questions": [], "limitations": ["草案未经科研核验。"],
    })


@unittest.skipUnless(os.getenv("LAPIS_TEST_PG") == "1" and
                     os.getenv("LAPIS_DB_NAME") == "lapis_test", "requires isolated lapis_test PostgreSQL")
class StoreGateTest(unittest.TestCase):
    def test_generated_draft_is_persisted_replayed_and_cannot_be_approved(self):
        initialize_schema()
        task = create_task("test")
        request = confirmed_v4_request()
        state = {"contract_version": 5, "draft_id": request["draft_id"], "fields": request["fields"],
                 "turns": ["研究意图", "确认"], "stage": "ready_for_design"}
        result = {"ready": True, "ready_for_design": True, "confirmation_event": True,
                  "intake_status": "ready_for_design", "request": request,
                  "calculation_status": "pending_research_design"}
        save_turn(task, 0, state, result, "test", "fake")
        version, current = get_current_request(task)
        self.assertEqual(version, 1)
        self.assertEqual(current["draft_id"], request["draft_id"])

        payload = saved_design_payload(design_draft("测试生成草案"), 1, "deepseek-flash",
                                       "design-operation-1", current)
        design_version, persisted, reused = propose_generated_design(task, payload, "test")
        self.assertFalse(reused)
        self.assertEqual(persisted, payload)
        replay_version, replay_payload, reused = propose_generated_design(task, payload, "test")
        self.assertTrue(reused)
        self.assertEqual((replay_version, replay_payload), (design_version, payload))
        self.assertEqual(get_design_operation(task, "design-operation-1")["version"], design_version)
        self.assertEqual(get_design(task, design_version)["payload"], payload)
        with self.assertRaisesRegex(ValueError, "不能直接审批"):
            approve_design(task, design_version, "test-reviewer", "检查当前请求、设计内容和研究结论边界")

        with patch("lapis.get_current_request", return_value=(1, current)), \
             patch("lapis.get_design_operation", return_value={
                 "version": design_version, "payload": payload,
             }), \
             patch("lapis.OpenAI") as openai, \
             patch("lapis.generate_design") as generate:
            replay = run_design_draft(task, "test", "design-operation-1")
        self.assertTrue(replay["reused"])
        self.assertEqual(replay["payload"], payload)
        openai.assert_not_called()
        generate.assert_not_called()

    def test_concurrent_generation_uses_canonical_saved_result_and_conflicts_on_new_input(self):
        initialize_schema()
        task = create_task("test")
        request = confirmed_v4_request()
        state = {"contract_version": 5, "draft_id": request["draft_id"], "fields": request["fields"],
                 "turns": ["研究意图", "确认"], "stage": "ready_for_design"}
        result = {"ready": True, "ready_for_design": True, "confirmation_event": True,
                  "intake_status": "ready_for_design", "request": request,
                  "calculation_status": "pending_research_design"}
        save_turn(task, 0, state, result, "test", "fake")

        barrier = Barrier(2)
        drafts = Queue()
        drafts.put(design_draft("并发提案 A"))
        drafts.put(design_draft("并发提案 B"))

        def generate_after_both_lookup(*_):
            barrier.wait(timeout=10)
            return drafts.get_nowait()

        with patch.dict(os.environ, {"LAPIS_API_KEY": "test", "LAPIS_MODEL": "test-model"}), \
             patch("lapis.OpenAI"), patch("lapis.instructor.from_openai", return_value=object()), \
             patch("lapis.generate_design", side_effect=generate_after_both_lookup) as generate:
            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = [pool.submit(run_design_draft, task, "test", "same-operation") for _ in range(2)]
                results = [future.result(timeout=20) for future in futures]
            self.assertEqual(generate.call_count, 2)

            self.assertEqual(results[0]["version"], results[1]["version"])
            self.assertEqual(results[0]["payload"], results[1]["payload"])
            self.assertCountEqual([item["reused"] for item in results], [False, True])
            stored = get_design(task, results[0]["version"])["payload"]
            self.assertEqual(stored, results[0]["payload"])

            replay = run_design_draft(task, "test", "same-operation")
            self.assertTrue(replay["reused"])
            self.assertEqual(replay["payload"], stored)
            self.assertEqual(generate.call_count, 2)

            with patch.dict(os.environ, {"LAPIS_MODEL": "different-model"}), \
                 self.assertRaisesRegex(ValueError, "不同研究请求、模型或提示词"):
                run_design_draft(task, "test", "same-operation")
            with patch("lapis.get_current_request", return_value=(1, {**request, "original_intent": "不同输入"})), \
                 self.assertRaisesRegex(ValueError, "不同研究请求、模型或提示词"):
                run_design_draft(task, "test", "same-operation")

    def test_invalid_execution_quantities_cannot_be_approved_or_frozen(self):
        initialize_schema()
        task = create_task("test")
        request = confirmed_v4_request()
        state = {"contract_version": 5, "draft_id": request["draft_id"], "fields": request["fields"],
                 "turns": ["研究意图", "确认"], "stage": "ready_for_design"}
        result = {"ready": True, "ready_for_design": True, "confirmation_event": True,
                  "intake_status": "ready_for_design", "request": request,
                  "calculation_status": "pending_research_design"}
        save_turn(task, 0, state, result, "test", "fake")

        def plan(value, unit):
            return {
                "request_version": 1, "purpose": "验证执行量纲门槛",
                "candidates": [{"id": "A", "description": "候选 A", "composition": {"A": "1"}}],
                "experiments": [{
                    "id": "e", "candidate_id": "A", "observable": "扩散系数", "observable_unit": "m2/s",
                    "conditions": {"temperature": {"value": value, "unit": unit}},
                    "method": "test-method", "method_version": "1", "model": "test-model",
                    "model_version": "1", "parameters": {"steps": "100"},
                    "quality_rules": ["检查稳定性"], "expected_raw_files": ["raw.log"],
                    "max_walltime_minutes": 1,
                }],
            }

        for design in (plan(298, "bananas"), plan(-298, "K")):
            version = propose_design(task, design, "test")
            with self.assertRaises(ValueError):
                approve_design(task, version, "test-reviewer", "检查方法、条件、单位与质量规则")
            with self.assertRaisesRegex(ValueError, "经审核"):
                freeze_execution(task, version, "test")

        with self.assertRaisesRegex(ValueError, "有限值"):
            propose_design(task, {"request_version": 1, "parameter": float("inf")}, "test")

    def test_freeze_revalidates_legacy_approved_design_before_returning_existing_id(self):
        initialize_schema()
        task = create_task("test")
        request = confirmed_v4_request()
        state = {"contract_version": 5, "draft_id": request["draft_id"], "fields": request["fields"],
                 "turns": ["研究意图", "确认"], "stage": "ready_for_design"}
        result = {"ready": True, "ready_for_design": True, "confirmation_event": True,
                  "intake_status": "ready_for_design", "request": request,
                  "calculation_status": "pending_research_design"}
        save_turn(task, 0, state, result, "test", "fake")
        invalid = {
            "request_version": 1, "purpose": "验证历史审批设计冻结门槛",
            "candidates": [{"id": "A", "description": "候选 A", "composition": {"A": "1"}}],
            "experiments": [{
                "id": "e", "candidate_id": "A", "observable": "扩散系数", "observable_unit": "m2/s",
                "conditions": {"temperature": {"value": -298, "unit": "K"}},
                "method": "test-method", "method_version": "1", "model": "test-model",
                "model_version": "1", "parameters": {"steps": "100"},
                "quality_rules": ["检查稳定性"], "expected_raw_files": ["raw.log"],
                "max_walltime_minutes": 1,
            }],
        }
        version = propose_design(task, invalid, "test")
        with connect() as db:
            db.execute("UPDATE design_versions SET status='approved',reviewer='legacy-reviewer' "
                       "WHERE task_id=%s AND version=%s", (task, version))
            payload_sha256 = db.execute(
                "SELECT payload_sha256 FROM design_versions WHERE task_id=%s AND version=%s",
                (task, version),
            ).fetchone()[0]
            existing_id = f"legacy-execution-{task}"
            db.execute(
                "INSERT INTO execution_requests (id,task_id,design_version,payload_sha256,approved_by,status) "
                "VALUES (%s,%s,%s,%s,%s,'frozen')",
                (existing_id, task, version, payload_sha256, "legacy-reviewer"),
            )
        with self.assertRaises(ValueError):
            freeze_execution(task, version, "test")
        with connect() as db:
            count = db.execute("SELECT count(*) FROM execution_requests WHERE task_id=%s", (task,)).fetchone()[0]
        self.assertEqual(count, 1)

    def test_persistence_and_execution_gate(self):
        initialize_schema()
        task = create_task("test")
        self.assertIsNone(get_task(task)["request_version"])
        request = confirmed_v4_request()
        state = {"contract_version": 5, "draft_id": request["draft_id"], "fields": request["fields"],
                 "turns": ["研究意图"], "stage": "clarifying"}
        draft_result = {"ready": False, "ready_for_design": False,
                        "intake_status": "needs_confirmation", "request": request}
        self.assertIsNone(save_turn(task, 0, state, draft_result, "test", "fake"))
        self.assertIsNone(get_task(task)["request_version"])
        state["turns"].append("确认")
        state["stage"] = "ready_for_design"
        result = {"ready": True, "ready_for_design": True, "confirmation_event": True,
                  "intake_status": "ready_for_design", "request": request,
                  "calculation_status": "pending_research_design"}
        self.assertEqual(save_turn(task, 1, state, result, "test", "fake"), 1)
        self.assertEqual(get_task(task)["request_version"], 1)
        self.assertEqual(get_task(task)["status"], "request_confirmed")

        incomplete = propose_design(task, {"request_version": 1}, "test")
        with self.assertRaises(ValueError):
            approve_design(task, incomplete, "test-reviewer", "检查方法、条件、单位与质量规则")
        with self.assertRaises(ValueError):
            freeze_execution(task, incomplete, "test")
        plan = {
            "request_version": 1, "purpose": "验证状态机",
            "candidates": [{"id": "candidate-a", "description": "候选 A", "composition": {"X": "1"}}],
            "experiments": [{
                "id": "experiment-a", "candidate_id": "candidate-a", "observable": "位移",
                "observable_unit": "m", "conditions": {"temperature": {"value": 298, "unit": "K"}},
                "method": "random_walk", "method_version": "1", "model": "random_walk", "model_version": "1",
                "parameters": {"sampling": "100 steps"}, "quality_rules": ["步骤数至少 100"],
                "expected_raw_files": ["test.log"], "max_walltime_minutes": 1,
            }],
        }
        version = propose_design(task, plan, "test")
        with self.assertRaisesRegex(ValueError, "v2 来源契约"):
            approve_design(task, version, "test-reviewer", "检查方法、条件、单位与质量规则")
        with self.assertRaises(ValueError):
            freeze_execution(task, version, "test")
        with self.assertRaises(ValueError):
            save_turn(task, 0, state, result, "test", "fake")

    def test_legacy_request_is_readable_but_cannot_enter_new_design(self):
        task = create_task("test")
        state = {"fields": {}, "turns": ["旧意图", "确认"], "stage": None}
        result = {"ready": True, "intake_status": "ready", "request": {"purpose": "旧请求"},
                  "calculation_status": "pending_research_design"}
        self.assertEqual(save_turn(task, 0, state, result, "test", "fake"), 1)
        self.assertEqual(get_task(task)["request_version"], 1)
        with self.assertRaisesRegex(ValueError, "确认版本"):
            propose_design(task, {"request_version": 1}, "test")

    def test_replayed_operation_does_not_duplicate_a_turn(self):
        task = create_task("test")
        state = {"fields": {}, "turns": ["输入"], "stage": None}
        result = {"ready": False, "intake_status": "needs_clarification", "request": {},
                  "next_question": "补充材料"}
        self.assertIsNone(save_turn(task, 0, state, result, "test", "fake", operation_id="test-" + task))
        self.assertIsNone(save_turn(task, 0, state, result, "test", "fake", operation_id="test-" + task))
        self.assertEqual(get_task(task)["revision"], 1)


if __name__ == "__main__":
    unittest.main()


def confirmed_v3_request():
    payload = confirmed_v4_request()
    payload.update(contract_version=3, rule_version="intake-rules-3.1")
    payload.pop("proposals")
    return payload
