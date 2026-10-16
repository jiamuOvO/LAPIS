import tempfile
import unittest
from pathlib import Path

from pydantic import ValidationError

from lapis_core import (ExecutionPlan, check_attempt_transition, digest,
                        intake_is_confirmed, verify_artifact)


class CoreGateTest(unittest.TestCase):
    @staticmethod
    def executable_plan(temperature=298, temperature_unit="K", observable="扩散系数",
                        observable_unit="m2/s", conditions=None, **experiment_fields):
        experiment = {
            "id": "e", "candidate_id": "A", "observable": observable,
            "observable_unit": observable_unit,
            "conditions": conditions or {"temperature": {"value": temperature, "unit": temperature_unit}},
            "method": "molecular dynamics", "method_version": "1",
            "model": "reviewed model", "model_version": "1", "parameters": {"sampling": "1 ns"},
            "quality_rules": ["检查采样稳定性"], "expected_raw_files": ["raw.log"],
            "max_walltime_minutes": 1,
            **experiment_fields,
        }
        return {"request_version": 1, "purpose": "比较候选", "candidates": [
            {"id": "A", "description": "候选 A", "composition": {"A": "1"}}],
            "experiments": [experiment]}

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

    def test_execution_units_reject_nonfinite_values_and_invalid_absolute_temperature(self):
        for value in (float("nan"), float("inf"), float("-inf")):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                ExecutionPlan.model_validate(self.executable_plan(temperature=value))
        with self.assertRaises(ValidationError):
            ExecutionPlan.model_validate(self.executable_plan(temperature=-298))
        with self.assertRaises(ValidationError):
            ExecutionPlan.model_validate(self.executable_plan(temperature=-273.16, temperature_unit="°C"))
        with self.assertRaises(ValidationError):
            ExecutionPlan.model_validate(self.executable_plan(temperature=298, temperature_unit="bananas"))
        ExecutionPlan.model_validate(self.executable_plan(temperature=0, temperature_unit="°C"))
        ExecutionPlan.model_validate(self.executable_plan(temperature=32, temperature_unit="°F"))

    def test_known_observable_and_condition_units_must_match_dimensions(self):
        with self.assertRaises(ValidationError):
            ExecutionPlan.model_validate(self.executable_plan(temperature=298, temperature_unit="bar"))
        with self.assertRaises(ValidationError):
            ExecutionPlan.model_validate(self.executable_plan(observable_unit="s"))
        with self.assertRaises(ValidationError):
            ExecutionPlan.model_validate(self.executable_plan(
                conditions={"custom condition": {"value": 1, "unit": "bar"}}))
        with self.assertRaises(ValidationError):
            ExecutionPlan.model_validate(self.executable_plan(
                observable="temperature", observable_unit="bananas",
                observable_dimension="temperature",
                observable_unit_review="研究人员已核对自定义温标"))
        with self.assertRaises(ValidationError):
            ExecutionPlan.model_validate(self.executable_plan(
                observable="温度", observable_unit="bananas"))
        with self.assertRaises(ValidationError):
            ExecutionPlan.model_validate(self.executable_plan(
                observable="measurement", observable_unit="bananas",
                observable_dimension="temperature",
                observable_unit_review="研究人员已核对自定义温标"))
        with self.assertRaises(ValidationError):
            ExecutionPlan.model_validate(self.executable_plan(
                observable="measurement", observable_unit="bananas",
                observable_dimension="Temperature",
                observable_unit_review="研究人员已核对自定义温标"))
        ExecutionPlan.model_validate(self.executable_plan(
            observable="temperature", observable_unit="°C"))

    def test_custom_units_are_allowed_only_with_explicit_dimension_and_review(self):
        conditions = {"pressure": {"value": 1, "unit": "bar"}}
        plan = self.executable_plan(
            observable="custom response", observable_unit="a.u.",
            observable_dimension="dimensionless",
            observable_unit_review="研究人员已核对该响应量的定义和单位",
            conditions={"pressure": {"value": 1, "unit": "bananas", "dimension": "pressure",
                                     "unit_review": "研究人员已核对该自定义压力单位"}},
        )
        ExecutionPlan.model_validate(plan)
        with self.assertRaises(ValidationError):
            ExecutionPlan.model_validate(self.executable_plan(
                observable="custom response", observable_unit="a.u.",
                conditions=conditions))

    def test_common_compound_quantities_and_unrecognized_names_remain_usable(self):
        ExecutionPlan.model_validate(self.executable_plan(
            observable="molar energy", observable_unit="kJ/mol",
            conditions={"mass fraction": {"value": 1.0, "unit": "wt%"}},
        ))
        ExecutionPlan.model_validate(self.executable_plan(
            observable="relative permittivity", observable_unit="1",
            observable_dimension="dimensionless",
            conditions={"salt concentration": {"value": 1.0, "unit": "M"}},
        ))

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
