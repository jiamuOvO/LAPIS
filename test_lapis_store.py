import os
import unittest

from lapis import run_simulation
from lapis_store import (approve_design, create_task, freeze_execution, get_task,
                         initialize_schema, propose_design, save_turn, verify_artifacts)


@unittest.skipUnless(os.getenv("LAPIS_TEST_PG") == "1" and
                     os.getenv("LAPIS_DB_NAME") == "lapis_test", "requires isolated lapis_test PostgreSQL")
class StoreGateTest(unittest.TestCase):
    def test_persistence_and_execution_gate(self):
        initialize_schema()
        task = create_task("test")
        self.assertEqual(get_task(task)["request_version"], None)
        state = {"fields": {"purpose": {"status": "specified", "value": "test"}},
                 "turns": ["研究意图"], "stage": None}
        result = {"ready": True, "intake_status": "ready", "request": state["fields"],
                  "calculation_status": "pending_research_design"}
        self.assertIsNone(save_turn(task, 0, state, result, "test", "fake"))
        self.assertIsNone(get_task(task)["request_version"])
        state["turns"].append("确认")
        self.assertEqual(save_turn(task, 1, state, result, "test", "fake"), 1)
        self.assertEqual(get_task(task)["request_version"], 1)
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
