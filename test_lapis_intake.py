import unittest
from unittest.mock import patch

from lapis_intake import (
    EvaluationCriterion, EvaluationProposal, Extraction, Update,
    contextual_reply, handle_turn, make_draft, new_state, process_turn,
)


class IntakeTest(unittest.TestCase):
    def test_missing_then_explicit_none(self):
        state = new_state()
        result = process_turn(state, "我想找传输性能更好的电解液", Extraction(updates=[
            Update(field="task_type", status="specified", value="screening", quote="找传输性能更好的电解液"),
            Update(field="material", status="specified", value="电解液", quote="电解液"),
            Update(field="metric", status="unclear", value="传输性能", quote="传输性能"),
        ]))
        self.assertIn("先推荐一个具体指标", result["next_question"])
        self.assertNotIn("conditions", result["request"])

        state["asked_field"] = "candidates"
        result = process_turn(state, "没有", Extraction(updates=[
            Update(field="candidates", status="none", quote="没有"),
        ]))
        self.assertEqual(result["request"]["candidates"]["status"], "none")

    def test_open_and_ungrounded_values(self):
        state = new_state()
        state["asked_field"] = "candidates"
        result = process_turn(state, "越多越好", Extraction(updates=[
            Update(field="candidates", status="open", value="尽可能多", quote="越多越好"),
            Update(field="conditions", status="specified", value="298 K", quote="298 K"),
        ]))
        self.assertEqual(result["request"]["candidates"]["status"], "open")
        self.assertNotIn("conditions", result["request"])

    def test_bare_reply_without_context(self):
        state = new_state()
        result = process_turn(state, "没有", Extraction(updates=[
            Update(field="candidates", status="none", quote="没有"),
        ]))
        self.assertIn("具体是指", result["next_question"])
        self.assertEqual(result["request"], {})

    def test_contextual_open_reply(self):
        state = new_state()
        state["asked_field"] = "candidates"
        extraction = contextual_reply(state, "越多越好")
        result = process_turn(state, "越多越好", extraction)
        self.assertEqual(result["request"]["candidates"]["status"], "open")

    def test_open_without_value_is_preserved(self):
        state = new_state()
        result = process_turn(state, "候选越多越好", Extraction(updates=[
            Update(field="candidates", status="open", quote="候选越多越好"),
        ]))
        self.assertEqual(result["request"]["candidates"]["status"], "open")

    def test_comprehensive_evaluation_advances_past_metric_and_direction(self):
        state = new_state()
        process_turn(state, "我想要找到最先进的呋喃材料", Extraction(updates=[
            Update(field="task_type", status="specified", value="screening", quote="找到最先进的呋喃材料"),
            Update(field="purpose", status="specified", value="找到最先进的呋喃材料", quote="找到最先进的呋喃材料"),
            Update(field="material", status="specified", value="呋喃材料", quote="呋喃材料"),
            Update(field="metric", status="unclear", value="最先进", quote="最先进"),
        ]))
        reply = "我也不懂这些，我觉得需要综合评价"
        result = process_turn(state, reply, contextual_reply(state, reply))
        self.assertEqual(result["request"]["metric"]["status"], "open")
        self.assertIn("目标指标", result["deferred_to_research_design"])
        self.assertEqual(state["asked_field"], "application")
        self.assertNotIn("目标方向", result["deferred_to_research_design"])

    def test_proposal_bundle_defaults_and_confirmation(self):
        state = new_state()
        proposal = EvaluationProposal(
            application_assumption="包装材料",
            assumptions=["暂以包装材料为应用场景"],
            criteria=[
                EvaluationCriterion(aspect="阻隔性", metric="氧气透过率", direction="越低越好", reason="评估阻隔性能", calculation_role="direct", calculation_route="基于给定薄膜结构与条件的渗透模拟"),
                EvaluationCriterion(aspect="力学性能", metric="拉伸强度", direction="越高越好", reason="评估承载能力", calculation_role="direct", calculation_route="基于给定结构的拉伸模拟"),
            ],
            comparison_rule="先核对可比条件，再分别比较指标",
            limitation="应用场景仍需确认",
        )
        utterance = "我想要找到最先进的呋喃材料"
        extraction = Extraction(updates=[
            Update(field="task_type", status="specified", value="screening", quote="找到最先进的呋喃材料"),
            Update(field="purpose", status="specified", value="找到最先进的呋喃材料", quote="找到最先进的呋喃材料"),
            Update(field="material", status="specified", value="呋喃材料", quote="呋喃材料"),
            Update(field="metric", status="unclear", value="最先进", quote="最先进"),
        ])
        with patch("lapis_intake.extract", return_value=extraction), patch("lapis_intake.propose_evaluation", return_value=proposal):
            result = handle_turn(None, "", state, utterance)
        self.assertFalse(result["ready"])
        self.assertEqual(result["proposal"]["criteria"][0]["metric"], "氧气透过率")
        self.assertIn("氧气透过率", result["next_question"])
        self.assertIn("拉伸强度", result["next_question"])
        self.assertIn("候选范围", result["next_question"])
        self.assertIn("工作条件", result["next_question"])
        self.assertNotIn("综合评价", result["next_question"])

        result = handle_turn(None, "", state, "按建议继续")
        self.assertEqual(result["intake_status"], "needs_confirmation")
        self.assertFalse(result["ready"])
        self.assertEqual(result["proposed_request"]["metric"]["value"], "氧气透过率")
        self.assertEqual(result["proposed_request"]["candidates"]["status"], "open")
        self.assertEqual(result["proposed_request"]["conditions"]["status"], "open")
        self.assertEqual(result["proposed_request"]["application"]["source"], "system_suggestion")

        result = handle_turn(None, "", state, "确认")
        self.assertTrue(result["ready"])
        self.assertEqual(result["request"]["metric"]["status"], "specified")
        self.assertEqual(result["request"]["application"]["value"], "包装材料")
        self.assertEqual(result["request"]["metric"]["source"], "confirmed_suggestion")
        self.assertIn("evaluation_plan", result)

    def test_bundle_reply_can_supply_multiple_fields(self):
        state = new_state()
        state["fields"] = {
            "task_type": {"status": "specified", "value": "comparison", "quote": "比较", "turn": 1},
            "purpose": {"status": "specified", "value": "比较两个材料", "quote": "比较", "turn": 1},
            "material": {"status": "specified", "value": "呋喃材料", "quote": "呋喃", "turn": 1},
            "metric": {"status": "specified", "value": "拉伸强度", "quote": "拉伸强度", "turn": 1},
        }
        state["stage"] = "bundle"
        text = "用于包装，比较A和B，没有预设条件"
        updates = Extraction(updates=[
            Update(field="application", status="specified", value="包装", quote="包装"),
            Update(field="candidates", status="specified", value="A和B", quote="A和B"),
            Update(field="conditions", status="none", quote="没有预设条件"),
        ])
        with patch("lapis_intake.extract", return_value=updates):
            result = handle_turn(None, "", state, text)
        self.assertEqual(result["intake_status"], "needs_confirmation")
        self.assertEqual(result["proposed_request"]["application"]["value"], "包装")
        self.assertEqual(result["proposed_request"]["candidates"]["value"], "A和B")
        self.assertEqual(result["proposed_request"]["conditions"]["status"], "none")
        self.assertTrue(handle_turn(None, "", state, "确认")["ready"])

    def test_proposal_rejects_vague_primary_metric(self):
        with self.assertRaises(ValueError):
            EvaluationProposal(
                application_assumption="有机电子器件", assumptions=[],
                criteria=[
                    EvaluationCriterion(aspect="性能", metric="载流子迁移率或光电效率", direction="越高越好", reason="筛选", calculation_role="direct", calculation_route="基于结构与条件的模拟"),
                    EvaluationCriterion(aspect="稳定性", metric="热分解温度", direction="越高越好", reason="筛选", calculation_role="direct", calculation_route="基于结构与条件的模拟"),
                ],
                comparison_rule="分别比较", limitation="尚未核验",
            )

    def test_comprehensive_request_keeps_multiple_calculation_metrics(self):
        state = new_state()
        state["fields"] = {
            "task_type": {"status": "specified", "value": "screening"},
            "purpose": {"status": "specified", "value": "筛选呋喃材料"},
            "material": {"status": "specified", "value": "呋喃材料"},
            "metric": {"status": "open", "value": None, "quote": "我不懂具体指标，想综合评价"},
        }
        state["proposal"] = EvaluationProposal(
            application_assumption="有机光电材料", assumptions=[],
            criteria=[
                EvaluationCriterion(aspect="电子结构", metric="HOMO-LUMO 能隙", direction="待研究设计确定目标区间", reason="只是光电性能代理量", calculation_role="proxy", calculation_route="由分子结构进行 DFT 计算"),
                EvaluationCriterion(aspect="吸收", metric="振子强度", direction="越高越好", reason="只是吸收性能代理量", calculation_role="proxy", calculation_route="由分子结构进行 TD-DFT 计算"),
            ],
            comparison_rule="逐项比较", limitation="方法尚需核验",
        ).model_dump()
        draft = make_draft(state)
        self.assertEqual(draft["metric"]["value"], "HOMO-LUMO 能隙；振子强度")
        self.assertEqual(draft["direction"]["status"], "open")

    def test_no_preferred_metric_is_deferred(self):
        state = new_state()
        state["fields"] = {
            field: {"status": "specified", "value": field, "quote": field, "turn": 1}
            for field in ("purpose", "material", "application", "conditions", "candidates", "direction")
        }
        state["fields"]["task_type"] = {
            "status": "specified", "value": "screening", "quote": "筛选", "turn": 1
        }
        state["asked_field"] = "metric"
        result = process_turn(state, "没有", contextual_reply(state, "没有"))
        self.assertTrue(result["ready"])
        self.assertEqual(result["deferred_to_research_design"], ["目标指标"])

    def test_comparison_does_not_require_direction_or_hypothesis(self):
        state = new_state()
        text = "比较A和B在298 K下的扩散系数，用于动力电池电解液研究"
        result = process_turn(state, text, Extraction(updates=[
            Update(field="task_type", status="specified", value="comparison", quote="比较A和B"),
            Update(field="purpose", status="specified", value="比较A和B", quote="比较A和B"),
            Update(field="material", status="specified", value="电解液", quote="电解液"),
            Update(field="application", status="specified", value="动力电池", quote="动力电池"),
            Update(field="conditions", status="specified", value="298 K", quote="298 K"),
            Update(field="candidates", status="specified", value="A和B", quote="A和B"),
            Update(field="metric", status="specified", value="扩散系数", quote="扩散系数"),
            Update(field="controls", status="specified", value="A", quote="A"),
        ]))
        self.assertTrue(result["ready"])
        self.assertNotIn("controls", result["request"])

    def test_mechanism_requires_hypothesis(self):
        state = new_state()
        result = process_turn(state, "验证催化剂机制", Extraction(updates=[
            Update(field="task_type", status="specified", value="mechanism_validation", quote="验证"),
        ]))
        self.assertEqual(result["next_question"], "你希望检验的具体机制主张是什么？例如“X通过Y影响Z”。")

    def test_unsupported_intent_does_not_become_ready(self):
        state = new_state()
        result = process_turn(state, "请帮我写一篇综述", Extraction(updates=[
            Update(field="task_type", status="specified", value="out_of_scope", quote="写一篇综述"),
        ]))
        self.assertEqual(result["intake_status"], "unsupported")
        self.assertFalse(result["ready"])
        self.assertIn("当前模块受理", result["next_question"])

    def test_exploration_and_multiple_goals_have_distinct_responses(self):
        state = new_state()
        result = process_turn(state, "研究催化剂的反应路径", Extraction(updates=[
            Update(field="task_type", status="specified", value="mechanism_exploration", quote="研究催化剂的反应路径"),
        ]))
        self.assertEqual(result["intake_status"], "unsupported")
        self.assertIn("探索未知机制", result["next_question"])

        result = process_turn(state, "既要筛选电解液，也要验证机制", Extraction(updates=[
            Update(field="task_type", status="specified", value="multiple_tasks", quote="既要筛选电解液，也要验证机制"),
        ]))
        self.assertEqual(result["intake_status"], "needs_clarification")
        self.assertEqual(state["asked_field"], "task_type")

    def test_switching_goal_clears_old_fields(self):
        state = new_state()
        process_turn(state, "筛选电解液，目标越高越好", Extraction(updates=[
            Update(field="task_type", status="specified", value="screening", quote="筛选电解液"),
            Update(field="material", status="specified", value="电解液", quote="电解液"),
            Update(field="direction", status="specified", value="越高越好", quote="越高越好"),
        ]))
        result = process_turn(state, "改成比较药物A和B", Extraction(updates=[
            Update(field="task_type", status="specified", value="comparison", quote="比较药物A和B"),
            Update(field="material", status="specified", value="药物", quote="药物"),
        ]))
        self.assertEqual(result["request"]["task_type"]["value"], "comparison")
        self.assertEqual(result["request"]["material"]["value"], "药物")
        self.assertNotIn("direction", result["request"])


if __name__ == "__main__":
    unittest.main()
