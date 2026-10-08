import os
import unittest

from lapis import run_simulation
from lapis_store import (approve_design, create_task, freeze_execution, get_task,
                         initialize_schema, propose_design, save_turn, verify_artifacts)


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


def confirmed_v3_request():
    from lapis_contract import RULE_VERSION
    payload = confirmed_v2_request()
    payload.update(contract_version=3, rule_version=RULE_VERSION, domain="materials_application",
                   draft_revision=1, draft_id="draft-1", issues=[], sources={})
    for field in ("target_performance", "constraints"):
        for i, entry in enumerate(payload["fields"][field]):
            entry["id"] = f"{field}-{i}"
    return payload


@unittest.skipUnless(os.getenv("LAPIS_TEST_PG") == "1" and
                     os.getenv("LAPIS_DB_NAME") == "lapis_test", "requires isolated lapis_test PostgreSQL")
class StoreGateTest(unittest.TestCase):
    def test_persistence_and_execution_gate(self):
        initialize_schema()
        task = create_task("test")
        self.assertIsNone(get_task(task)["request_version"])
        request = confirmed_v3_request()
        state = {"contract_version": 3, "draft_id": request["draft_id"], "fields": request["fields"],
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
            approve_design(task, incomplete, "test-reviewer")
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
        approve_design(task, version, "test-reviewer")
        execution = freeze_execution(task, version, "test")
        self.assertEqual(execution, freeze_execution(task, version, "test"))
        with self.assertRaises(RuntimeError):
            run_simulation(execution, "test-adapter", fail=True)
        run_simulation(execution, "test-adapter", fail=False)
        manifest = verify_artifacts(execution)
        self.assertEqual(len(manifest), 1)
        self.assertTrue(manifest[0]["valid"])
        self.assertEqual(manifest[0]["origin"], "simulation")
        with self.assertRaises(ValueError):
            run_simulation(execution, "test-adapter", fail=False)
        with self.assertRaises(ValueError):
            save_turn(task, 0, state, result, "test", "fake")

    def test_legacy_request_is_readable_but_cannot_enter_new_design(self):
        task = create_task("test")
        state = {"fields": {}, "turns": ["旧意图", "确认"], "stage": None}
        result = {"ready": True, "intake_status": "ready", "request": {"purpose": "旧请求"},
                  "calculation_status": "pending_research_design"}
        self.assertEqual(save_turn(task, 0, state, result, "test", "fake"), 1)
        self.assertEqual(get_task(task)["request_version"], 1)
        with self.assertRaisesRegex(ValueError, "v3"):
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
