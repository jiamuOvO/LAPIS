import io
import os
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

from lapis import run_chat


class ChatTest(unittest.TestCase):
    def test_chat_asks_next_question_and_keeps_task(self):
        output = {"result": {"intake_status": "needs_clarification",
                             "next_question": "请限定研究对象。"}}
        printed = io.StringIO()
        with patch.dict(os.environ, {"LAPIS_API_KEY": "test"}), \
             patch("lapis.create_task", return_value="task-1"), \
             patch("lapis.get_task", return_value={"intake_result": None}), \
             patch("lapis.OpenAI"), \
             patch("lapis.instructor.from_openai"), \
             patch("lapis.run_intake_turn", return_value=output) as turn, \
             patch("builtins.input", side_effect=["比较电解液", "/exit"]), \
             redirect_stdout(printed):
            run_chat(None, "researcher")
        self.assertEqual(turn.call_args.args[:3], ("task-1", "比较电解液", "researcher"))
        self.assertIn("任务 ID：task-1", printed.getvalue())
        self.assertIn("LAPIS> 请限定研究对象。", printed.getvalue())


    def test_chat_resumes_saved_question_draft_and_confirmation(self):
        cases = [
            ({"intake_status": "needs_clarification", "next_question": "用于什么场景？"},
             "LAPIS> 用于什么场景？"),
            ({"intake_status": "needs_confirmation", "next_question": "请确认草案。",
              "request": {"fields": {"research_object": {"value": "电解液"}}}},
             '"value": "电解液"'),
            ({"intake_status": "ready_for_design", "ready_for_design": True},
             "研究请求已保存"),
        ]
        for saved, expected in cases:
            with self.subTest(status=saved["intake_status"]):
                printed = io.StringIO()
                with patch.dict(os.environ, {"LAPIS_API_KEY": "test"}), \
                     patch("lapis.create_task") as create, \
                     patch("lapis.get_task", return_value={"intake_result": saved}), \
                     patch("lapis.OpenAI"), \
                     patch("lapis.instructor.from_openai"), \
                     patch("lapis.run_intake_turn") as turn, \
                     patch("builtins.input", return_value="/exit"), \
                     redirect_stdout(printed):
                    run_chat("saved-task", "researcher")
                self.assertIn(expected, printed.getvalue())
                self.assertNotIn("请描述你本轮", printed.getvalue())
                create.assert_not_called()
                turn.assert_not_called()

    def test_confirmation_context_survives_retry_without_refresh(self):
        saved = {"intake_status": "needs_confirmation", "next_question": "确认当前草稿", "request": {"fields": {}},
                 "input_context": {"draft_id": "D7", "recommendation_ref": {"id": "r1", "version": 2}}}
        output = {"result": {"intake_status": "ready_for_design", "ready_for_design": True}}
        with patch.dict(os.environ, {"LAPIS_API_KEY": "test"}), \
             patch("lapis.get_task", return_value={"intake_result": saved}), \
             patch("lapis.OpenAI"), patch("lapis.instructor.from_openai"), \
             patch("lapis.run_intake_turn", side_effect=[RuntimeError("temporary"), output]) as turn, \
             patch("builtins.input", side_effect=["确认", "y", "/exit"]), redirect_stdout(io.StringIO()):
            run_chat("task-1", "researcher")
        self.assertEqual(turn.call_count, 2)
        self.assertEqual(turn.call_args_list[0].args, turn.call_args_list[1].args)
        self.assertEqual(turn.call_args_list[0].kwargs["input_context"], saved["input_context"])
        self.assertEqual(turn.call_args_list[1].kwargs["input_context"], saved["input_context"])


@unittest.skipUnless(os.getenv("LAPIS_TEST_PG") == "1" and os.getenv("LAPIS_DB_NAME") == "lapis_test", "requires isolated PostgreSQL")
class ChatDatabaseTest(unittest.TestCase):
    def test_recommend_select_exit_resume_confirm_uses_persisted_context(self):
        from lapis_intake import Extraction, Update
        from lapis_store import create_task, get_task, initialize_schema
        initialize_schema()
        task = create_task("cli-test")
        replies = [Extraction(actions=["recommend"], updates=[
            Update(field="research_object", status="specified", value="呋喃基", quote="呋喃基")]),
            Extraction(actions=["select", "edit"], selected_option=2, updates=[
                Update(field="constraints", status="none", quote="不考虑成本", action="remove", target_value="成本")])]
        printed = io.StringIO()
        with patch.dict(os.environ, {"LAPIS_API_KEY": "test"}), \
             patch("lapis.OpenAI"), patch("lapis.instructor.from_openai"), \
             patch("lapis_intake.extract", side_effect=replies), \
             patch("lapis_intake.generate_recommendations", side_effect=__import__("test_lapis_intake").catalogue_fixture), \
             patch("builtins.input", side_effect=["呋喃基有什么推荐", "采用第二个，不考虑成本", "/exit"]), redirect_stdout(printed):
            run_chat(task, "cli-test")
        saved = get_task(task)
        self.assertEqual(saved["intake_result"]["intake_status"], "needs_confirmation")
        self.assertIn("限制：", printed.getvalue())
        self.assertIn("来源", saved["intake_result"]["next_question"])
        with patch.dict(os.environ, {"LAPIS_API_KEY": "test"}), \
             patch("lapis.OpenAI"), patch("lapis.instructor.from_openai"), \
             patch("lapis_intake.extract", side_effect=AssertionError("literal confirmation needs no model")), \
             patch("builtins.input", side_effect=["确认", "/exit"]), redirect_stdout(io.StringIO()):
            run_chat(task, "cli-test")
        self.assertEqual(get_task(task)["active_request_version"], 1)



if __name__ == "__main__":
    unittest.main()
