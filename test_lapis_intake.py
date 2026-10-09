import unittest
from copy import deepcopy
from unittest.mock import patch

from lapis_core import intake_is_confirmed
from lapis_contract import validate_current_request
from lapis_guidance import load_catalog, recommendations
from lapis_intake import Extraction, Update, handle_turn, new_state, normalize_state, process_turn

TEXT = "在高电压电池中，仅比较碳酸酯电解液的稳定性和传输，不用含氟添加剂，尽量低成本"
BASE = [
    Update(field="task_type", status="specified", value="comparison", quote="比较"),
    Update(field="purpose", status="specified", value="比较电解液配方", quote="比较"),
    Update(field="research_object", status="specified", value="碳酸酯电解液", quote="碳酸酯电解液"),
    Update(field="application", status="specified", value="高电压电池", quote="高电压电池"),
    Update(field="research_scope", status="specified", value="仅研究碳酸酯电解液", quote="仅比较碳酸酯电解液"),
]


def full_updates():
    return Extraction(updates=BASE + [
        Update(field="target_performance", status="specified", value="稳定性", direction="比较", quote="稳定性"),
        Update(field="target_performance", status="specified", value="离子传输", direction="比较", quote="传输"),
        Update(field="constraints", status="specified", value="不使用含氟添加剂", strength="hard", quote="不用含氟添加剂"),
        Update(field="constraints", status="specified", value="成本尽量低", strength="preference", quote="尽量低成本"),
        Update(field="material_function", status="specified", value="提供离子传输并保持稳定", quote="稳定性和传输"),
    ])


def catalogue_fixture(client, model, state, text, operation_id=None):
    """Historical catalogue fixture ONLY; production always calls the model."""
    return recommendations(state, text)


def turn(state, text, extraction, context=None):
    with patch("lapis_intake.extract", return_value=extraction), patch("lapis_intake.generate_recommendations", side_effect=catalogue_fixture):
        return handle_turn(None, "mock", state, text, input_context=context)


def guide(state=None):
    state = state if state is not None else new_state()
    result = turn(state, "我想研究呋喃基，但不了解用途，请推荐", Extraction(actions=["inform", "recommend"],
                  updates=[Update(field="research_object", status="specified", value="呋喃基", quote="呋喃基")]))
    return state, result


class IntakeV3Test(unittest.TestCase):
    def test_eight_fields_unknown_and_no_automatic_confirmation(self):
        state = new_state()
        self.assertEqual(len(state["fields"]), 8)
        self.assertEqual(state["fields"]["work_conditions"]["status"], "unknown")
        result = turn(state, TEXT, full_updates())
        self.assertEqual(result["intake_status"], "needs_confirmation")
        self.assertFalse(intake_is_confirmed(state, result))
        self.assertEqual(len(result["request"]["fields"]["target_performance"]), 2)

    def test_confirmation_is_bound_to_displayed_draft_and_duplicate_is_stable(self):
        state = new_state()
        draft = turn(state, TEXT, full_updates())
        stale = handle_turn(None, "mock", state, "确认", input_context={"draft_id": "old"})
        self.assertFalse(stale["ready_for_design"])
        confirmed = handle_turn(None, "mock", state, "确认", input_context=draft["input_context"])
        self.assertTrue(intake_is_confirmed(state, confirmed))
        validate_current_request(confirmed["request"])
        repeated = handle_turn(None, "mock", state, "确认", input_context=confirmed["input_context"])
        self.assertEqual(repeated["request"], confirmed["request"])
        self.assertFalse(repeated.get("confirmation_event", False))
        self.assertEqual(confirmed["calculation_status"], "pending_research_design")

    def test_negated_confirmation_cannot_be_misclassified_into_approval(self):
        state = new_state()
        draft = turn(state, TEXT, full_updates())
        result = turn(state, "我不准备确认", Extraction(actions=["confirm"]), draft["input_context"])
        self.assertFalse(result["ready_for_design"])
        self.assertFalse(result.get("confirmation_event", False))

    def test_source_snapshot_tampering_is_rejected(self):
        state, advice = guide()
        selected = turn(state, "采用第二个", Extraction(actions=["select"], selected_option=2), advice["input_context"])
        payload = deepcopy(selected["request"])
        payload["sources"]["pef-co2-2015"]["claim"] = "changed without provenance"
        with self.assertRaisesRegex(ValueError, "快照校验"):
            validate_current_request(payload)


    def test_unknown_mechanism_does_not_require_hypothesis(self):
        state = new_state()
        extraction = full_updates()
        extraction.updates[0].value = "mechanism_exploration"
        extraction.updates[1].value = "探索稳定性原因"
        result = turn(state, TEXT, extraction)
        self.assertEqual(result["intake_status"], "needs_confirmation")
        self.assertNotIn("hypothesis", result["request"])

    def test_help_before_required_fields_is_source_backed_and_not_adopted(self):
        state, result = guide()
        self.assertEqual(result["intake_status"], "needs_guidance")
        self.assertEqual(len(result["recommendations"]["options"]), 2)
        self.assertEqual(state["fields"]["application"]["status"], "unknown")
        for source in result["recommendations"]["sources"].values():
            self.assertTrue(source["limitations"])
            self.assertEqual(len(source["content_sha256"]), 64)
        self.assertFalse(handle_turn(None, "mock", state, "确认", input_context=result["input_context"])["ready_for_design"])

    def test_selection_plus_cost_removal_keeps_both_actions_and_source(self):
        state, advice = guide()
        selected = turn(state, "采用第二个方向，但先不考虑成本", Extraction(actions=["select", "edit"], selected_option=2,
                        updates=[Update(field="constraints", status="none", quote="先不考虑成本", action="remove", target_value="成本")]),
                        advice["input_context"])
        self.assertEqual(selected["intake_status"], "needs_confirmation")
        obj = selected["request"]["fields"]["research_object"]
        self.assertEqual(obj["source"], "confirmed_suggestion")
        self.assertEqual(obj["recommendation_ref"]["direction_id"], "pef-packaging")
        self.assertEqual(obj["selection_quote"], "采用第二个方向，但先不考虑成本")
        self.assertIn("pef-co2-2015", obj["source_refs"])
        confirmed = handle_turn(None, "mock", state, "确认", input_context=selected["input_context"])
        self.assertTrue(confirmed["ready_for_design"])
        self.assertEqual(confirmed["request"]["original_intent"], "我想研究呋喃基，但不了解用途，请推荐")

    def test_model_expanded_option_does_not_erase_catalog_provenance(self):
        state, advice = guide()
        option = advice["recommendations"]["options"][1]
        updates = []
        for field, value in option["fields"].items():
            entries = value if isinstance(value, list) else [{"value": value}]
            for entry in entries:
                updates.append(Update(field=field, status="specified", value=entry["value"],
                                      direction=entry.get("direction"), quote="采用第二个方向", action="update",
                                      target_id="target_performance-unknown" if field == "target_performance" else field))
        updates.append(Update(field="constraints", status="specified", value="先不考虑成本", quote="先不考虑成本", strength="preference"))
        selected = turn(state, "采用第二个方向，先不考虑成本", Extraction(actions=["select", "edit"], selected_option=2, updates=updates), advice["input_context"])
        self.assertEqual(selected["intake_status"], "needs_confirmation")
        for field in option["fields"]:
            values = state["fields"][field]
            self.assertTrue(all(e["source"] == "confirmed_suggestion" for e in values if isinstance(values, list)) if isinstance(values, list) else values["source"] == "confirmed_suggestion")
        self.assertEqual(state["fields"]["constraints"][0]["status"], "unknown")

    def test_cost_opt_out_removes_only_uniquely_named_constraint(self):
        state = new_state()
        turn(state, TEXT, full_updates())
        result = turn(state, "先不考虑成本", Extraction(actions=["edit"], updates=[
            Update(field="constraints", status="specified", value="先不考虑成本", strength="preference", quote="先不考虑成本")]))
        self.assertEqual(len(result["request"]["fields"]["constraints"]), 1)
        self.assertEqual(result["request"]["fields"]["constraints"][0]["strength"], "hard")


    def test_battery_liquid_guidance_and_economic_ambiguity(self):
        state = new_state()
        first = turn(state, "我想要找到一种性价比高的电池液", Extraction(updates=[
            Update(field="purpose",status="specified",value="找电池液",quote="找到一种性价比高的电池液"),
            Update(field="research_object",status="specified",value="电池液",quote="电池液"),
            Update(field="target_performance",status="specified",value="性价比高",direction="提高",quote="性价比高")]))
        self.assertTrue(any("性价比需要拆分" in x["message"] for x in first["blocking_issues"]))
        advice = turn(state, "你帮我推荐吧", Extraction(actions=["recommend"]))
        self.assertEqual(advice["intake_status"],"needs_guidance")
        self.assertEqual([x["id"] for x in advice["recommendations"]["options"]],["lithium-electrolyte-transport"])
        self.assertIn("不证明", advice["recommendations"]["sources"]["electrolyte-transport-2015"]["limitations"])
        selected = turn(state,"采用第一个",Extraction(actions=["select"],selected_option=1),advice["input_context"])
        self.assertEqual(selected["request"]["fields"]["research_object"]["source"],"confirmed_suggestion")
        self.assertIn("成本口径", selected["request"]["fields"]["research_scope"]["value"])

    def test_battery_direction_not_offered_for_explicit_lead_acid(self):
        state = new_state()
        state["fields"]["research_object"].update(status="specified",value="铅酸电池液")
        self.assertIsNone(recommendations(state,"推荐"))


    def test_stale_selection_cannot_adopt_or_poison_new_draft(self):
        state, advice = guide()
        changed = turn(state, "改成铝合金，请推荐", Extraction(actions=["edit", "recommend"],
                       updates=[Update(field="research_object", status="specified", value="铝合金", quote="铝合金")]))
        stale = turn(state, "采用第二个", Extraction(actions=["select"], selected_option=2), advice["input_context"])
        self.assertEqual(state["fields"]["research_object"]["value"], "铝合金")
        self.assertIn("推荐已经变化", stale["notice"])
        self.assertIsNone(changed["recommendations"])
        self.assertFalse(state["issues"])

    def test_extensible_catalog_supports_new_material_without_code_branch(self):
        state = new_state()
        state["fields"]["research_object"].update(status="specified", value="铝合金")
        catalog = deepcopy(load_catalog())
        option = deepcopy(catalog["directions"][0])
        catalog["sources"]["mock-source"] = {"title": "TEST FIXTURE ONLY", "claim": "No scientific claim", "limitations": "Software test only", "origin": "simulation"}
        option.update(id="mock-aluminum", aliases=["铝合金"], label="测试目录合金方向",
                      source_refs=["mock-source"], fields={"research_object": "测试铝合金"})
        catalog["directions"].append(option)
        result = recommendations(state, "有什么方向", catalog)
        self.assertEqual([x["id"] for x in result["options"]], ["mock-aluminum"])
        self.assertNotIn("furan", str(result["options"][0]["fields"]))

    def test_pause_keeps_recommendations_and_rejection_does_not_repeat(self):
        state, advice = guide()
        paused = handle_turn(None, "mock", state, "先不确认", input_context=advice["input_context"])
        self.assertEqual(state["recommendation_history"], [])
        self.assertEqual(paused["input_context"]["recommendation_ref"], advice["input_context"]["recommendation_ref"])
        rejected = turn(state, "都不采用，请换一批", Extraction(actions=["reject", "recommend"]), paused["input_context"])
        self.assertIsNone(rejected["recommendations"])
        self.assertEqual(len(state["recommendation_history"]), 2)
        self.assertTrue(all(x["status"] == "rejected" for x in state["recommendation_history"]))

    def test_accepted_suggestion_can_be_rejected_without_erasing_user_goal(self):
        state, advice = guide()
        selected = turn(state, "用第二个", Extraction(actions=["select"], selected_option=2), advice["input_context"])
        edited = turn(state, "仍比较气体阻隔能力", Extraction(updates=[
            Update(field="target_performance", status="specified", value="气体阻隔能力", direction="比较", quote="气体阻隔能力")]),
            selected["input_context"])
        rejected = turn(state, "不采用这个方向", Extraction(actions=["reject"], selected_option=2), edited["input_context"])
        self.assertEqual(state["fields"]["research_object"]["status"], "unknown")
        self.assertEqual(state["fields"]["target_performance"][0]["source"], "user")
        self.assertTrue(any(x.get("reason") == "recommendation_rejected" for x in state["history"]))
        self.assertFalse(rejected["ready_for_design"])

    def test_confirm_plus_edit_requires_another_confirmation(self):
        state = new_state()
        draft = turn(state, TEXT, full_updates())
        confirmed = handle_turn(None, "mock", state, "确认", input_context=draft["input_context"])
        old = deepcopy(confirmed["request"])
        cost = state["fields"]["constraints"][1]
        changed = turn(state, "确认，但成本改为硬约束", Extraction(actions=["confirm", "edit"], updates=[
            Update(field="constraints", status="specified", value="成本尽量低", strength="hard", quote="成本改为硬约束",
                   action="update", target_id=cost["id"])]), confirmed["input_context"])
        self.assertFalse(changed["ready_for_design"])
        self.assertFalse(changed.get("confirmation_event", False))
        self.assertEqual(old["fields"]["constraints"][1]["strength"], "preference")
        self.assertEqual(state["fields"]["constraints"][1]["strength"], "hard")
        self.assertNotEqual(changed["input_context"]["draft_id"], confirmed["input_context"]["draft_id"])

    def test_mixed_confirmation_with_no_effective_edit_still_requires_review(self):
        state = new_state()
        draft = turn(state, TEXT, full_updates())
        confirmed = handle_turn(None, "mock", state, "确认", input_context=draft["input_context"])
        cost = state["fields"]["constraints"][1]
        result = turn(state, "确认但成本仍是偏好", Extraction(actions=["confirm", "edit"], updates=[
            Update(field="constraints", status="specified", value=cost["value"], quote="成本仍是偏好", strength="preference", action="update", target_id=cost["id"])]), confirmed["input_context"])
        self.assertFalse(result["ready_for_design"])
        self.assertEqual(result["intake_status"], "needs_confirmation")


    def test_qualifier_only_update_preserves_existing_value_and_id(self):
        state = new_state()
        turn(state, TEXT, full_updates())
        cost = deepcopy(state["fields"]["constraints"][1])
        revised = turn(state, "确认，但成本是硬约束", Extraction(actions=["confirm", "edit"], updates=[
            Update(field="constraints", status="specified", quote="成本是硬约束", strength="hard", action="update", target_id=cost["id"])]))
        new = state["fields"]["constraints"][1]
        self.assertEqual(new["value"], cost["value"])
        self.assertEqual(new["id"], cost["id"])
        self.assertEqual(new["strength"], "hard")
        self.assertFalse(revised["ready_for_design"])
        self.assertEqual(revised["intake_status"], "needs_confirmation")


    def test_goal_direction_update_preserves_id_and_other_goal(self):
        state = new_state()
        turn(state, TEXT, full_updates())
        first = state["fields"]["target_performance"][0]
        turn(state, "稳定性改为提高", Extraction(updates=[
            Update(field="target_performance", status="specified", value="稳定性", direction="提高",
                   quote="稳定性改为提高", action="update", target_id=first["id"])]))
        self.assertEqual(len(state["fields"]["target_performance"]), 2)
        self.assertEqual(state["fields"]["target_performance"][0]["id"], first["id"])
        self.assertEqual(state["fields"]["target_performance"][0]["direction"], "提高")

    def test_removal_is_exact_and_preserves_other_goal(self):
        state = new_state()
        turn(state, TEXT, full_updates())
        result = turn(state, "不研究离子传输，其余保留", Extraction(actions=["edit"], updates=[
            Update(field="target_performance", status="specified", value="离子传输", action="remove", quote="不研究离子传输")]))
        self.assertEqual([x["value"] for x in state["fields"]["target_performance"]], ["稳定性"])
        self.assertEqual(result["intake_status"], "needs_confirmation")
        unresolved = turn(state, "删掉稳定", Extraction(updates=[
            Update(field="target_performance", status="specified", value="稳定", action="remove", quote="删掉稳定")]))
        self.assertEqual(len(state["fields"]["target_performance"]), 1)
        self.assertEqual(unresolved["intake_status"], "needs_clarification")

    def test_unclear_placeholder_is_replaced_by_clarification(self):
        state = new_state()
        turn(state, TEXT, full_updates())
        turn(state, "整体目标替换为未明确指标", Extraction(updates=[
            Update(field="target_performance", status="unclear", quote="整体目标替换为未明确指标", action="replace")]))
        clarified = turn(state, "改为比较稳定性", Extraction(updates=[
            Update(field="target_performance", status="specified", value="稳定性", direction="比较",
                   quote="比较稳定性", action="update")]))
        self.assertEqual(clarified["intake_status"], "needs_confirmation")
        self.assertEqual(len(state["fields"]["target_performance"]), 1)

    def test_object_change_marks_old_context_for_review_and_requires_display_binding(self):
        state = new_state()
        draft = turn(state, TEXT, full_updates())
        changed = turn(state, "改成聚合物包装材料", Extraction(updates=[
            Update(field="research_object", status="specified", value="聚合物包装材料", quote="聚合物包装材料")]))
        self.assertTrue(state["fields"]["constraints"][0]["needs_review"])
        stale = turn(state, "沿用原约束", Extraction(actions=["reaffirm"], reaffirm_fields=["constraints"]), draft["input_context"])
        self.assertTrue(state["fields"]["constraints"][0]["needs_review"])
        turn(state, "沿用原约束", Extraction(actions=["reaffirm"], reaffirm_fields=["constraints"]), stale["input_context"])
        self.assertNotIn("needs_review", state["fields"]["constraints"][0])
        self.assertFalse(changed["ready_for_design"])

    def test_method_reference_withdrawal_is_logged_not_execution_permission(self):
        state = new_state()
        note = Update(field="reference_note", status="specified", value="分子动力学", category="method", quote="分子动力学")
        draft = turn(state, TEXT + "，用分子动力学", Extraction(updates=full_updates().updates + [note]))
        ref = draft["request"]["reference_notes"][0]
        self.assertEqual(ref["status"], "unverified_user_reference")
        turn(state, "撤回分子动力学", Extraction(updates=[
            Update(field="reference_note", status="specified", value="分子动力学", quote="撤回分子动力学", action="remove")]))
        self.assertFalse(state["reference_notes"][0]["active"])
        self.assertNotIn("method", draft["request"]["fields"])

    def test_task_classification_change_retains_constraints_and_changes_draft(self):
        state = new_state()
        draft = turn(state, TEXT, full_updates())
        before = deepcopy(state["fields"]["constraints"])
        process_turn(state, "改成筛选", Extraction(updates=[
            Update(field="task_type", status="specified", value="screening", quote="筛选")]))
        self.assertEqual(state["fields"]["constraints"], before)
        self.assertNotEqual(state["draft_id"], draft["input_context"]["draft_id"])

    def test_legacy_state_is_readable_and_requires_re_review(self):
        from test_lapis_store import confirmed_v2_request
        payload = confirmed_v2_request()
        state = {"contract_version": 2, "fields": deepcopy(payload["fields"]), "turns": ["旧意图"], "stage": "ready_for_design"}
        normalize_state(state)
        self.assertEqual(payload["contract_version"], 2)
        self.assertEqual(state["legacy_fields"], payload["fields"])
        self.assertTrue(state["fields"]["research_object"]["needs_review"])
        self.assertFalse(handle_turn(None, "mock", state, "确认")["ready_for_design"])

    def test_unknown_open_none_and_ungrounded_updates(self):
        state = new_state()
        result = process_turn(state, "筛选手机", Extraction(updates=[
            Update(field="task_type", status="specified", value="out_of_scope", quote="筛选"),
            Update(field="work_conditions", status="specified", value="298 K", quote="298 K")]))
        self.assertEqual(result["intake_status"], "unsupported")
        self.assertEqual(state["fields"]["work_conditions"]["status"], "unknown")
        state = new_state()
        state["asked_field"] = "work_conditions"
        handle_turn(None, "mock", state, "没有")
        self.assertEqual(state["fields"]["work_conditions"]["status"], "none")
        state["asked_field"] = "work_conditions"
        with patch("lapis_intake.generate_recommendations", side_effect=catalogue_fixture):
            handle_turn(None, "mock", state, "交给研究设计确定")
        self.assertEqual(state["fields"]["work_conditions"]["status"], "open")


if __name__ == "__main__":
    unittest.main()
