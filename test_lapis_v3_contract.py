import os
import unittest
from copy import deepcopy
from uuid import uuid4

from lapis_contract import RULE_VERSION, request_issues, validate_research_request_v3
from lapis_store import create_task, get_task, initialize_schema, propose_design, save_turn
from test_lapis_store import confirmed_v2_request


def v3_request():
    request = confirmed_v2_request()
    request.update(contract_version=3, rule_version=RULE_VERSION, domain="materials_application",
                   draft_revision=1, draft_id="draft-1", issues=[], sources={})
    return request


class V3ContractTest(unittest.TestCase):
    def test_unknown_open_and_none_are_distinct_and_allowed(self):
        for status in ("unknown", "open", "none"):
            request = v3_request()
            request["fields"]["work_conditions"]["status"] = status
            validate_research_request_v3(request)

    def test_temperature_units_and_component_rules_do_not_depend_on_model_issue_flags(self):
        request = v3_request()
        request["fields"]["work_conditions"].update(status="specified", value="25 K或25℃",
                                                   source="user", quote="25 K或25℃", turn=1)
        request["fields"]["constraints"] = [
            {"id": "c1", "status": "specified", "value": "必须使用含氟添加剂", "source": "user",
             "quote": "必须使用含氟添加剂", "turn": 1, "strength": "hard"},
            {"id": "c2", "status": "specified", "value": "不得使用含氟添加剂", "source": "user",
             "quote": "不得使用含氟添加剂", "turn": 1, "strength": "hard"},
        ]
        self.assertTrue({"temperature", "conflict"} <= {x["kind"] for x in request_issues(request)})
        with self.assertRaises(ValueError):
            validate_research_request_v3(request)

    def test_drug_research_is_excluded_without_rejecting_all_medical_materials(self):
        from lapis_contract import domain_from_fields
        fields = v3_request()["fields"]
        fields["application"]["value"] = "小分子药物先导筛选"
        self.assertEqual(domain_from_fields(fields, "materials_application"), "drug_discovery")
        fields["application"]["value"] = "医用聚合物涂层"
        fields["research_object"]["value"] = "聚合物"
        self.assertEqual(domain_from_fields(fields, "materials_application"), "materials_application")


@unittest.skipUnless(os.getenv("LAPIS_TEST_PG") == "1" and
                     os.getenv("LAPIS_DB_NAME") == "lapis_test", "requires isolated lapis_test PostgreSQL")
class V3StoreTest(unittest.TestCase):
    def test_edit_revokes_active_version_and_old_records_remain_readable(self):
        initialize_schema()
        task = create_task("test")
        request = v3_request()
        state = {"contract_version": 3, "fields": deepcopy(request["fields"]),
                 "draft_id": "draft-1", "draft_revision": 1, "stage": "ready_for_design",
                 "turns": ["研究意图", "确认"]}
        result = {"request": request, "intake_status": "ready_for_design", "ready_for_design": True,
                  "confirmation_event": True, "calculation_status": "pending_research_design"}
        self.assertEqual(save_turn(task, 0, state, result, "test", "fake", operation_id=str(uuid4())), 1)
        self.assertEqual(get_task(task)["active_request_version"], 1)
        propose_design(task, {"request_version": 1}, "test")
        state.update(stage="clarifying", draft_id="draft-2", draft_revision=2)
        state["turns"].append("对象还不确定")
        changed = {"intake_status": "needs_clarification", "ready_for_design": False,
                   "content_changed": True, "request": request}
        save_turn(task, 1, state, changed, "test", "fake")
        self.assertIsNone(get_task(task)["active_request_version"])
        self.assertEqual(get_task(task)["request_version"], 1)
        with self.assertRaisesRegex(ValueError, "v3"):
            propose_design(task, {"request_version": 1}, "test")


if __name__ == "__main__":
    unittest.main()
