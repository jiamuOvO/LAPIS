"""Generic model proposals: schema, adoption, trust and rollback gates."""
import json
import os
from uuid import uuid4
import unittest
from copy import deepcopy
from unittest.mock import Mock, patch
from pydantic import ValidationError
from lapis_contract import validate_current_request
from lapis_intake import Extraction, Update, handle_turn, new_state, normalize_state
from lapis_proposals import Guidance, generate_recommendations
from test_lapis_store import confirmed_v3_request


def proposal():
    return {"explanation": "这是待审查的研究方向，不能推断已具备性能。", "options": [{
        "label": "陶瓷散热基板比较", "reason": "将材料用途限定到散热基板。",
        "assumptions": ["用户愿意比较陶瓷基板"], "limitations": ["未核验材料性能，不提供实算结论"],
        "clarifications": ["温度范围和成本口径待定"], "source_refs": [], "fields": {
            "purpose": {"status": "specified", "value": "比较陶瓷基板"},
            "research_object": {"status": "specified", "value": "陶瓷基板"},
            "application": {"status": "specified", "value": "电子器件散热"},
            "research_scope": {"status": "specified", "value": "仅比较候选基板材料，不验证器件"},
            "material_function": {"status": "specified", "value": "拟传导热量"},
            "target_performance": [{"status": "specified", "value": "热传导", "direction": "比较"}],
            "work_conditions": {"status": "open", "value": None},
            "constraints": [{"status":"unknown"}]}}]}


class DynamicProposalTest(unittest.TestCase):
    def advice(self, state=None):
        state = state if state is not None else new_state()
        client = Mock()
        client.create.return_value = Guidance.model_validate(proposal())
        with patch("lapis_intake.extract", return_value=Extraction(actions=["recommend"])), \
             patch("lapis_proposals.reference_recommendations", return_value=None):
            result = handle_turn(client, "test-model", state, "请推荐陶瓷材料用途", operation_id="test-operation")
        return state, result, client

    def test_unlisted_material_without_sources_can_be_adopted_and_confirmed(self):
        state, advice, client = self.advice()
        self.assertEqual(client.create.call_count, 1)
        self.assertEqual(state["fields"]["application"]["status"], "unknown")
        rec = advice["recommendations"]
        self.assertEqual(rec["generation"]["operation_id"], "test-operation")
        self.assertEqual(rec["options"][0]["evidence_status"], "unverified")
        with patch("lapis_intake.extract", return_value=Extraction(actions=["select"], selected_option=1)):
            selected = handle_turn(None, "test-model", state, "采用第一个方向", advice["input_context"])
        self.assertEqual(selected["intake_status"], "needs_confirmation")
        self.assertFalse(selected["ready_for_design"])
        confirmed = handle_turn(None, "test-model", state, "确认", selected["input_context"])
        validate_current_request(confirmed["request"])
        self.assertTrue(confirmed["ready_for_design"])
        obj = confirmed["request"]["fields"]["research_object"]
        self.assertEqual(obj["source_refs"], [])
        self.assertEqual(obj["suggestion_origin"], "model")
        self.assertEqual(confirmed["calculation_status"], "pending_research_design")
        forged = deepcopy(confirmed["request"])
        forged["fields"]["research_object"]["evidence_status"] = "verified"
        with self.assertRaises(ValueError):
            validate_current_request(forged)

    def test_unknown_keys_types_and_qualifiers_are_rejected(self):
        for mutation in (lambda p: p.update(extra=1),
                         lambda p: p["options"][0]["fields"].update(forcefield="fake"),
                         lambda p: p["options"][0]["fields"]["purpose"].update(value=7),
                         lambda p: p["options"][0]["fields"]["target_performance"][0].pop("direction"),
                         lambda p: p["options"][0]["fields"].update(constraints=[{"status":"specified","value":"便宜"}])):
            payload = proposal()
            mutation(payload)
            with self.assertRaises(ValidationError):
                Guidance.model_validate(payload)
        with self.assertRaises(ValidationError):
            Extraction.model_validate({"actions":["inform"], "made_up":True})

    def test_unprovided_source_or_link_is_rejected_without_partial_turn(self):
        for mutation in (lambda p: p["options"][0].update(source_refs=["invented-source"]),
                         lambda p: p["options"][0]["fields"]["purpose"].update(value="见 https://fake.invalid"),
                         lambda p: p.update(explanation="见10.9999/fabricated")):
            payload = proposal(); mutation(payload)
            client = Mock(); client.create.return_value = payload
            state = new_state(); before = deepcopy(state)
            with patch("lapis_intake.extract", return_value=Extraction(actions=["recommend"], updates=[
                    Update(field="research_object", status="specified", value="陶瓷", quote="陶瓷")])), \
                 patch("lapis_proposals.reference_recommendations", return_value=None):
                with self.assertRaises(ValueError):
                    handle_turn(client, "test", state, "陶瓷请推荐")
            self.assertEqual(state, before)

    def test_failed_provider_rolls_back_and_rejected_content_is_supplied(self):
        state, advice, _ = self.advice()
        before = deepcopy(state)
        bad = Mock(); bad.create.side_effect = TimeoutError("test timeout")
        with patch("lapis_intake.extract", return_value=Extraction(actions=["reject", "recommend"], selected_option=1)):
            with self.assertRaises(TimeoutError):
                handle_turn(bad, "test", state, "拒绝第一个，请换方向", advice["input_context"])
        self.assertEqual(state, before)
        client = Mock(); client.create.return_value = Guidance.model_validate(proposal())
        with patch("lapis_intake.extract", return_value=Extraction(actions=["reject", "recommend"], selected_option=1)):
            handle_turn(client, "test", state, "拒绝第一个，请换方向", advice["input_context"])
        context = json.loads(client.create.call_args.kwargs["messages"][1]["content"])
        self.assertEqual(context["rejected"][0]["fields"], advice["recommendations"]["options"][0]["fields"])

    def test_stale_selection_cannot_promote_model_expanded_text_to_user_fact(self):
        state, advice, _ = self.advice()
        with patch("lapis_intake.extract", return_value=Extraction(actions=["select"],selected_option=1,
                updates=[Update(field="application",status="specified",value="旧模型虚构的用途",quote="采用第一个方向")])):
            result = handle_turn(None,"test",state,"采用第一个方向",{"draft_id":"stale", "recommendation_ref":{"id":"old","version":1}})
        self.assertEqual(state["fields"]["application"]["status"],"unknown")
        self.assertIn("推荐已经变化",result["notice"])
        self.assertFalse(result["ready_for_design"])

    def test_selection_does_not_swallow_cost_removal_against_unknown_proposal(self):
        state = new_state()
        state["fields"]["constraints"] = [
            {"id":"cost", "status":"specified", "value":"成本尽量低", "strength":"preference", "source":"user", "quote":"成本尽量低", "turn":1},
            {"id":"hard", "status":"specified", "value":"不能使用铅", "strength":"hard", "source":"user", "quote":"不能使用铅", "turn":1}]
        state, advice, _ = self.advice(state)
        with patch("lapis_intake.extract", return_value=Extraction(actions=["select","edit"], selected_option=1,
                updates=[Update(field="constraints",status="none",quote="先不考虑成本",action="remove",target_id="cost")])):
            handle_turn(None,"test",state,"采用第一个方向，但先不考虑成本",advice["input_context"])
        self.assertFalse(any(e.get("id")=="cost" for e in state["fields"]["constraints"]))
        self.assertTrue(any(e.get("id")=="hard" for e in state["fields"]["constraints"]))

    def test_hard_constraint_survives_adoption_and_old_v3_requires_review(self):
        state = new_state()
        state["fields"]["constraints"] = [{"id":"hard-1", "status":"specified", "value":"不能使用铅", "strength":"hard", "source":"user", "quote":"不能使用铅", "turn":1}]
        state, advice, _ = self.advice(state)
        with patch("lapis_intake.extract", return_value=Extraction(actions=["select"], selected_option=1)):
            handle_turn(None, "test", state, "选第一个", advice["input_context"])
        self.assertEqual(state["fields"]["constraints"][0]["id"], "hard-1")
        self.assertEqual(state["fields"]["constraints"][0]["strength"], "hard")
        old = confirmed_v3_request(); frozen = deepcopy(old)
        legacy = {"contract_version":3,"fields":deepcopy(old["fields"]),"turns":["旧请求"],"stage":"ready_for_design"}
        normalize_state(legacy)
        self.assertEqual(legacy["contract_version"],5)
        self.assertEqual(legacy["stage"],"clarifying")
        self.assertTrue(legacy["fields"]["purpose"]["needs_review"])
        self.assertEqual(old, frozen)

@unittest.skipUnless(os.getenv("LAPIS_TEST_PG") == "1" and os.getenv("LAPIS_DB_NAME") == "lapis_test", "requires isolated PostgreSQL")
class DynamicPersistenceTest(unittest.TestCase):
    def test_generated_snapshot_replay_and_adoption_are_traceable(self):
        from lapis_graph import run_intake_turn
        from lapis_store import create_task, get_task, get_intake_operation, initialize_schema
        initialize_schema()
        task = create_task("dynamic-test")
        operation = str(uuid4())
        client = Mock(); client.create.return_value = Guidance.model_validate(proposal())
        with patch("lapis_intake.extract", return_value=Extraction(actions=["recommend"])):
            first = run_intake_turn(task,"推荐陶瓷用途","test",client,"test-model","test-prompt",operation)
        rec = first["result"]["recommendations"]
        self.assertEqual(rec["generation"]["operation_id"],operation)
        snapshot = get_intake_operation(operation)
        self.assertEqual(snapshot["result"]["recommendations"],rec)
        client.create.side_effect = AssertionError("must not replay generation")
        with patch("lapis_intake.extract", side_effect=AssertionError("must not replay extraction")):
            again = run_intake_turn(task,"推荐陶瓷用途","test",client,"test-model","test-prompt",operation)
        self.assertEqual(again["result"],first["result"])
        with patch("lapis_intake.extract", return_value=Extraction(actions=["select"],selected_option=1)):
            selected = run_intake_turn(task,"采用第一个","test",client,"test-model","test-prompt",input_context=first["result"]["input_context"])
        confirmed = run_intake_turn(task,"确认","test",client,"test-model","test-prompt",input_context=selected["result"]["input_context"])
        self.assertTrue(confirmed["result"]["ready_for_design"])
        self.assertEqual(get_task(task)["active_request_version"],1)
        self.assertEqual(confirmed["result"]["request"]["proposals"][rec["id"]]["generation"]["operation_id"],operation)

if __name__ == "__main__":
    unittest.main()
