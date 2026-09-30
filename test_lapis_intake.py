import unittest
from copy import deepcopy
from unittest.mock import patch

from lapis_core import intake_is_confirmed, validate_research_request_v2
from lapis_intake import (
    Extraction, IntakeSuggestion, SuggestedGoal, Update, handle_turn,
    new_state, normalize_state, process_turn,
)


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
        Update(field="target_performance", status="specified", value="稳定性", direction="比较",
               quote="稳定性"),
        Update(field="target_performance", status="specified", value="离子传输", direction="比较",
               quote="传输"),
        Update(field="constraints", status="specified", value="不使用含氟添加剂",
               strength="hard", quote="不用含氟添加剂"),
        Update(field="constraints", status="specified", value="成本尽量低",
               strength="preference", quote="尽量低成本"),
        Update(field="material_function", status="specified",
               value="提供离子传输并保持稳定", quote="稳定性和传输"),
    ])


class IntakeV2Test(unittest.TestCase):
    def test_eight_fields_and_explicit_unknowns(self):
        state = new_state()
        self.assertEqual(len(state["fields"]), 8)
        self.assertEqual(state["fields"]["work_conditions"]["status"], "unknown")
        self.assertEqual(state["fields"]["constraints"][0]["status"], "unknown")

    def test_multiple_goals_and_constraints_survive_confirmation(self):
        state = new_state()
        with patch("lapis_intake.extract", return_value=full_updates()):
            draft = handle_turn(None, "fake", state, TEXT)
        self.assertEqual(draft["intake_status"], "needs_confirmation")
        self.assertEqual(len(draft["proposed_request"]["fields"]["target_performance"]), 2)
        self.assertEqual(len(draft["proposed_request"]["fields"]["constraints"]), 2)
        self.assertEqual(draft["proposed_request"]["fields"]["work_conditions"]["status"], "unknown")
        self.assertFalse(intake_is_confirmed(state, draft))
        confirmed = handle_turn(None, "fake", state, "确认")
        self.assertEqual(confirmed["intake_status"], "ready_for_design")
        self.assertEqual(confirmed["calculation_status"], "pending_research_design")
        self.assertTrue(intake_is_confirmed(state, confirmed))
        validate_research_request_v2(confirmed["request"])
        constraints = confirmed["request"]["fields"]["constraints"]
        self.assertEqual({item["strength"] for item in constraints}, {"hard", "preference"})
        self.assertNotIn("calculation_route", str(confirmed["request"]))

    def test_unknown_mechanism_is_accepted_without_hypothesis(self):
        state = new_state()
        text = "探索高电压电池中碳酸酯电解液失稳的原因，只研究电解液"
        updates = Extraction(updates=[
            Update(field="task_type", status="specified", value="mechanism_exploration", quote="探索"),
            Update(field="purpose", status="specified", value="探索失稳原因", quote="探索高电压电池中碳酸酯电解液失稳的原因"),
            Update(field="research_object", status="specified", value="碳酸酯电解液", quote="碳酸酯电解液"),
            Update(field="application", status="specified", value="高电压电池", quote="高电压电池"),
            Update(field="research_scope", status="specified", value="只研究电解液", quote="只研究电解液"),
            Update(field="target_performance", status="specified", value="稳定性", direction="探索",
                   quote="失稳"),
            Update(field="material_function", status="specified", value="保持稳定",
                   quote="电解液失稳"),
        ])
        with patch("lapis_intake.extract", return_value=updates):
            result = handle_turn(None, "fake", state, text)
        self.assertEqual(result["intake_status"], "needs_confirmation")
        self.assertEqual(handle_turn(None, "fake", state, "确认")["intake_status"], "ready_for_design")

    def test_system_suggestion_keeps_origin_and_confirmation_turn(self):
        state = new_state()
        suggestion = IntakeSuggestion(
            material_function="传导锂离子并在高电压下保持稳定",
            target_performance=[SuggestedGoal(value="高电压稳定性", direction="提高")],
            limitation="这只是应用目标的暂定解释",
        )
        with patch("lapis_intake.extract", return_value=Extraction(updates=BASE)), \
             patch("lapis_intake.propose_intake", return_value=suggestion):
            result = handle_turn(None, "fake", state, TEXT)
        function = result["proposed_request"]["fields"]["material_function"]
        self.assertEqual(function["source"], "system_suggestion")
        self.assertEqual(function["suggested_after_turn"], 1)
        confirmed = handle_turn(None, "fake", state, "确认")
        function = confirmed["request"]["fields"]["material_function"]
        self.assertEqual(function["source"], "confirmed_suggestion")
        self.assertEqual(function["suggested_after_turn"], 1)
        self.assertEqual(function["confirmed_turn"], 2)
        self.assertEqual(confirmed["request"]["original_intent"], TEXT)

    def test_goal_removal_keeps_applicable_suggestion_before_and_after_confirmation(self):
        state = new_state()
        suggestion = IntakeSuggestion(
            material_function="在高电压电池中传导离子",
            target_performance=[SuggestedGoal(value="稳定性", direction="比较")],
            limitation="材料功能仅为待确认建议",
        )
        first = Extraction(updates=BASE + [
            Update(field="target_performance", status="specified", value="稳定性",
                   direction="比较", quote="稳定性"),
            Update(field="target_performance", status="specified", value="离子传输",
                   direction="比较", quote="传输"),
        ])
        with patch("lapis_intake.extract", return_value=first), \
             patch("lapis_intake.propose_intake", return_value=suggestion):
            handle_turn(None, "fake", state, TEXT)
        with patch("lapis_intake.extract", return_value=Extraction(updates=[
            Update(field="target_performance", status="specified", value="离子传输",
                   direction="比较", quote="离子传输", action="remove"),
            Update(field="target_performance", status="specified", value="稳定性",
                   direction="比较", quote="稳定性", action="replace"),
        ])), patch("lapis_intake.propose_intake", side_effect=AssertionError("不应重新提出建议")):
            revised = handle_turn(None, "fake", state, "取消离子传输目标，仍比较稳定性")
        self.assertEqual(revised["intake_status"], "needs_confirmation")
        self.assertEqual([x["value"] for x in revised["request"]["fields"]["target_performance"]],
                         ["稳定性"])
        function = revised["request"]["fields"]["material_function"]
        self.assertEqual(function["source"], "system_suggestion")
        self.assertEqual(function["suggested_after_turn"], 1)
        confirmed = handle_turn(None, "fake", state, "确认")
        self.assertEqual(confirmed["request"]["fields"]["material_function"]["source"],
                         "confirmed_suggestion")
        with patch("lapis_intake.extract", return_value=Extraction(updates=[
            Update(field="target_performance", status="specified", value="氧化稳定性",
                   direction="比较", quote="氧化稳定性", action="replace"),
        ])), patch("lapis_intake.propose_intake", side_effect=AssertionError("不应重新提出建议")):
            second = handle_turn(None, "fake", state, "改为只比较氧化稳定性")
        self.assertEqual(second["intake_status"], "needs_confirmation")
        self.assertEqual(second["request"]["fields"]["material_function"]["source"],
                         "confirmed_suggestion")

    def test_unclear_remaining_goal_blocks_confirmation(self):
        state = new_state()
        with patch("lapis_intake.extract", return_value=full_updates()):
            handle_turn(None, "fake", state, TEXT)
        with patch("lapis_intake.extract", return_value=Extraction(updates=[
            Update(field="target_performance", status="unclear", quote="指标还不明确"),
        ])):
            result = handle_turn(None, "fake", state, "指标还不明确")
        self.assertEqual(result["intake_status"], "needs_clarification")
        self.assertEqual(state["asked_field"], "target_performance")
        with self.assertRaises(ValueError):
            validate_research_request_v2(result["request"])

    def test_explicit_rejection_removes_pending_suggestion(self):
        state = new_state()
        suggestion = IntakeSuggestion(
            material_function="在高电压电池中传导离子",
            target_performance=[SuggestedGoal(value="稳定性", direction="比较")],
            limitation="待确认",
        )
        with patch("lapis_intake.extract", return_value=Extraction(updates=BASE)), \
             patch("lapis_intake.propose_intake", return_value=suggestion):
            handle_turn(None, "fake", state, TEXT)
        with patch("lapis_intake.extract", return_value=Extraction(updates=[
            Update(field="target_performance", status="specified", value="稳定性",
                   direction="比较", quote="稳定性"),
        ])):
            handle_turn(None, "fake", state, "仍比较稳定性")
        rejected = handle_turn(None, "fake", state, "不采用")
        self.assertEqual(rejected["request"]["fields"]["material_function"]["status"],
                         "unknown")

    def test_task_classification_change_does_not_erase_hard_constraint(self):
        state = new_state()
        process_turn(state, TEXT, full_updates())
        before = deepcopy(state["fields"]["constraints"])
        process_turn(state, "改成筛选同类电解液", Extraction(updates=[
            Update(field="task_type", status="specified", value="screening", quote="筛选"),
            Update(field="purpose", status="specified", value="筛选同类电解液", quote="筛选同类电解液"),
        ]))
        self.assertEqual(state["fields"]["constraints"], before)
        self.assertEqual(state["task_type"], "screening")
        self.assertEqual(state["history"][-1]["field"], "purpose")

    def test_revision_creates_new_draft_with_audit_history(self):
        state = new_state()
        with patch("lapis_intake.extract", return_value=full_updates()):
            handle_turn(None, "fake", state, TEXT)
        old = deepcopy(handle_turn(None, "fake", state, "确认")["request"])
        change = "改成只比较氧化稳定性"
        with patch("lapis_intake.extract", return_value=Extraction(updates=[
            Update(field="target_performance", status="specified", value="氧化稳定性",
                   direction="比较", quote="只比较氧化稳定性", action="replace"),
        ])):
            draft = handle_turn(None, "fake", state, change)
        self.assertEqual(draft["intake_status"], "needs_confirmation")
        self.assertEqual(len(draft["proposed_request"]["fields"]["target_performance"]), 1)
        self.assertEqual(old["fields"]["target_performance"][0]["value"], "稳定性")
        self.assertEqual(state["history"][-1]["field"], "target_performance")
        self.assertEqual(state["history"][-1]["after"][0]["turn"], 3)

    def test_method_is_only_an_unverified_reference(self):
        state = new_state()
        text = TEXT + "，希望用分子动力学"
        updates = full_updates().updates + [
            Update(field="reference_note", status="specified", value="分子动力学",
                   category="method", quote="希望用分子动力学"),
        ]
        with patch("lapis_intake.extract", return_value=Extraction(updates=updates)):
            draft = handle_turn(None, "fake", state, text)
        note = draft["proposed_request"]["reference_notes"][0]
        self.assertEqual(note["status"], "unverified_user_reference")
        self.assertEqual(note["category"], "method")
        self.assertNotIn("method", draft["proposed_request"]["fields"])

    def test_legacy_state_readable_but_requires_v2_reconfirmation(self):
        state = {"fields": {"purpose": {"status": "specified", "value": "筛选", "quote": "筛选",
                                        "turn": 1},
                            "material": {"status": "specified", "value": "电解液", "quote": "电解液",
                                         "turn": 1}},
                 "turns": ["筛选电解液"], "stage": None}
        normalize_state(state)
        self.assertEqual(state["legacy_fields"]["material"]["value"], "电解液")
        self.assertEqual(state["fields"]["research_object"]["source"], "legacy_unknown")
        self.assertEqual(state["fields"]["research_scope"]["status"], "unknown")
        with self.assertRaises(ValueError):
            validate_research_request_v2({"contract_version": 2, "original_intent": "筛选电解液",
                                          "fields": state["fields"]})

    def test_out_of_scope_and_ungrounded_update(self):
        state = new_state()
        text = "筛选手机"
        result = process_turn(state, text, Extraction(updates=[
            Update(field="task_type", status="specified", value="out_of_scope", quote="筛选"),
            Update(field="work_conditions", status="specified", value="298 K", quote="298 K"),
        ]))
        self.assertEqual(result["intake_status"], "unsupported")
        self.assertEqual(state["fields"]["work_conditions"]["status"], "unknown")


if __name__ == "__main__":
    unittest.main()
