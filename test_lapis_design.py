import unittest
from unittest.mock import Mock

from pydantic import ValidationError

from lapis_design import (DESIGN_PROMPT, DesignDraft, generate_design,
                          saved_design_payload)
from lapis_core import digest


def example():
    return {
        "paradigm": "comparison",
        "rationale": "请求明确提出比较目标；此分类仍待研究人员复核。",
        "candidates": [{
            "id": "control", "description": "请求中的对照候选",
            "composition": {}, "basis_fields": ["research_scope"],
        }],
        "experiments": [{
            "id": "compare-1", "question": "候选之间的目标观察量是否不同？",
            "candidate_ids": ["control"], "observable": "请求中的目标性能",
            "unit": None, "conditions": {}, "method_suggestion": None,
            "model_suggestion": None, "quality_checks_to_define": ["定义适用的质量判据"],
            "basis_fields": ["target_performance"],
        }],
        "hypotheses": [],
        "open_questions": [{
            "field": "method", "question": "需要选择并审核具体方法与模型。",
            "blocks_execution": True,
        }],
        "limitations": ["该草案未经科研审核，不构成计算结果或结论。"],
    }


class DesignDraftTest(unittest.TestCase):
    def test_rejects_unknown_basis_and_candidate_references(self):
        payload = example()
        payload["candidates"][0]["basis_fields"] = ["invented_field"]
        with self.assertRaises(ValidationError):
            DesignDraft.model_validate(payload)
        payload = example()
        payload["experiments"][0]["candidate_ids"] = ["missing"]
        with self.assertRaises(ValidationError):
            DesignDraft.model_validate(payload)

    def test_generator_uses_strict_response_model_and_saves_provenance(self):
        client = Mock()
        client.create.return_value = DesignDraft.model_validate(example())
        request = {"contract_version": 5, "purpose": "比较两个候选"}
        draft = generate_design(client, "test-model", request)
        client.create.assert_called_once()
        self.assertIs(client.create.call_args.kwargs["response_model"], DesignDraft)
        saved = saved_design_payload(draft, 2, "test-model", "op-1", request)
        self.assertEqual(saved["evidence_status"], "unverified")
        self.assertEqual(saved["generation"]["request_sha256"], digest(request))
        self.assertEqual(saved["generation"]["prompt_sha256"], digest(DESIGN_PROMPT))
        self.assertEqual(saved["generation"]["operation_id"], "op-1")


if __name__ == "__main__":
    unittest.main()
