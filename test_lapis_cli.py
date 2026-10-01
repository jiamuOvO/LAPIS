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
             patch("lapis.get_task"), \
             patch("lapis.OpenAI"), \
             patch("lapis.instructor.from_openai"), \
             patch("lapis.run_intake_turn", return_value=output) as turn, \
             patch("builtins.input", side_effect=["比较电解液", "/exit"]), \
             redirect_stdout(printed):
            run_chat(None, "researcher")
        self.assertEqual(turn.call_args.args[:3], ("task-1", "比较电解液", "researcher"))
        self.assertIn("任务 ID：task-1", printed.getvalue())
        self.assertIn("LAPIS> 请限定研究对象。", printed.getvalue())


if __name__ == "__main__":
    unittest.main()
