import tempfile
import unittest
from pathlib import Path

from pydantic import ValidationError

from lapis_core import (ExecutionPlan, check_attempt_transition, digest,
                        intake_is_confirmed, verify_artifact)


class CoreGateTest(unittest.TestCase):
    def test_intake_ready_is_not_computation_approval(self):
        state = {"turns": ["筛选电解液"], "stage": None}
        result = {"ready": True, "calculation_status": "pending_research_design"}
        self.assertFalse(intake_is_confirmed(state, result))
        state["turns"].append("确认")
        self.assertTrue(intake_is_confirmed(state, result))

    def test_incomplete_design_cannot_be_approved(self):
        with self.assertRaises(ValidationError):
            ExecutionPlan.model_validate({"request_version": 1, "purpose": "筛选", "candidates": []})

    def test_placeholder_is_not_an_executable_method(self):
        with self.assertRaises(ValidationError):
            ExecutionPlan.model_validate({
                "request_version": 1, "purpose": "筛选", "candidates": [
                    {"id": "A", "description": "候选 A", "composition": {"X": "1"}}],
                "experiments": [{"id": "e", "candidate_id": "A", "observable": "扩散系数",
                                 "observable_unit": "m2/s", "conditions": {"T": {"value": 298, "unit": "K"}},
                                 "method": "待确认", "method_version": "1", "model": "M", "model_version": "1",
                                 "parameters": {"time": "1 ns"}, "quality_rules": ["检查"],
                                 "expected_raw_files": ["raw.log"], "max_walltime_minutes": 1}],
            })

    def test_artifact_is_hashed_and_confined(self):
        with tempfile.TemporaryDirectory() as base:
            root = Path(base) / "artifacts"
            root.mkdir()
            file = root / "raw.log"
            file.write_bytes(b"real bytes")
            relative, size, sha = verify_artifact(file, root)
            self.assertEqual((relative, size), ("raw.log", 10))
            self.assertEqual(len(sha), 64)
            outside = Path(base) / "outside"
            outside.write_bytes(b"x")
            with self.assertRaises(ValueError):
                verify_artifact(outside, root)

    def test_digest_is_order_independent(self):
        self.assertEqual(digest({"a": 1, "b": 2}), digest({"b": 2, "a": 1}))

    def test_attempt_state_machine_rejects_shortcuts(self):
        check_attempt_transition("queued", "running")
        check_attempt_transition("running", "failed")
        with self.assertRaises(ValueError):
            check_attempt_transition("queued", "succeeded")
        with self.assertRaises(ValueError):
            check_attempt_transition("succeeded", "running")


if __name__ == "__main__":
    unittest.main()
