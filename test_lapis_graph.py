import os
import unittest
from unittest.mock import patch

from lapis_graph import run_intake_turn
from lapis_store import create_task, get_task, initialize_schema


@unittest.skipUnless(os.getenv("LAPIS_TEST_PG") == "1" and
                     os.getenv("LAPIS_DB_NAME") == "lapis_test", "requires isolated lapis_test PostgreSQL")
class GraphIntakeTest(unittest.TestCase):
    def test_pause_and_resume_across_graph_instances(self):
        initialize_schema()
        task_id = create_task("test")

        def fake_handle_turn(client, model, state, text):
            state["turns"].append(text)
            if text == "确认":
                state["stage"] = None
                return {"intake_status": "ready", "ready": True,
                        "calculation_status": "pending_research_design",
                        "request": {"purpose": {"status": "specified", "value": "测试"}},
                        "next_question": None}
            state["stage"] = "confirmation"
            return {"intake_status": "needs_confirmation", "ready": False,
                    "request": {}, "next_question": "请确认草案"}

        with patch("lapis_graph.handle_turn", side_effect=fake_handle_turn):
            first = run_intake_turn(task_id, "测试意图", "test", None, "fake", "prompt-test")
            self.assertEqual(first["graph_status"], "waiting_for_user")
            self.assertIsNone(first["request_version"])
            # A new call opens a new checkpointer connection and resumes the saved interrupt.
            second = run_intake_turn(task_id, "确认", "test", None, "fake", "prompt-test")
        self.assertEqual(second["graph_status"], "idle")
        self.assertEqual(second["request_version"], 1)
        self.assertEqual(get_task(task_id)["revision"], 2)


if __name__ == "__main__":
    unittest.main()
