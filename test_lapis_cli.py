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


if __name__ == "__main__":
    unittest.main()
