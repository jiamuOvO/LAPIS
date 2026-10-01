import os
from concurrent.futures import ThreadPoolExecutor
from threading import Event
import unittest
from unittest.mock import patch
from psycopg.pq import TransactionStatus

import lapis_graph
from lapis_graph import run_intake_turn
from lapis_store import create_task, get_task, initialize_schema


@unittest.skipUnless(os.getenv("LAPIS_TEST_PG") == "1" and
                     os.getenv("LAPIS_DB_NAME") == "lapis_test", "requires isolated lapis_test PostgreSQL")
class GraphIntakeTest(unittest.TestCase):
    def test_checkpoint_setup_starts_after_lock_transaction_ends(self):
        initialize_schema()
        task_id = create_task("test")
        real_connect = lapis_graph.connect
        lock_connection = None
        case = self

        def tracked_connect():
            nonlocal lock_connection
            lock_connection = real_connect()
            return lock_connection

        class CheckpointProbe:
            def __enter__(self):
                return self

            def __exit__(self, *_):
                pass

            def setup(self):
                case.assertEqual(lock_connection.info.transaction_status, TransactionStatus.IDLE)
                raise RuntimeError("checkpoint probe")

        with patch("lapis_graph.connect", side_effect=tracked_connect), \
             patch("lapis_graph.PostgresSaver.from_conn_string", return_value=CheckpointProbe()):
            with self.assertRaisesRegex(RuntimeError, "checkpoint probe"):
                run_intake_turn(task_id, "测试", "test", None, "fake", "prompt-test")

    @staticmethod
    def _needs_confirmation(client, model, state, text):
        state["turns"].append(text)
        state["stage"] = "confirmation"
        return {"intake_status": "needs_confirmation", "ready": False,
                "request": {}, "next_question": "请确认草案"}

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

    def test_commit_before_checkpoint_failure_replays_once(self):
        initialize_schema()
        task_id = create_task("test")
        operation_id = "fault-" + task_id
        real_save_turn = lapis_graph.save_turn

        def commit_then_fail(*args):
            real_save_turn(*args)
            raise RuntimeError("注入：数据库提交后、图检查点前失败")

        with patch("lapis_graph.handle_turn", side_effect=self._needs_confirmation), \
             patch("lapis_graph.save_turn", side_effect=commit_then_fail):
            with self.assertRaisesRegex(RuntimeError, "图检查点前失败"):
                run_intake_turn(task_id, "研究意图", "test", None, "fake", "prompt-test", operation_id)
        self.assertEqual(get_task(task_id)["revision"], 1)
        with patch("lapis_graph.handle_turn", side_effect=AssertionError("不得重复调用 LLM")):
            result = run_intake_turn(task_id, "研究意图", "test", None, "fake", "prompt-test", operation_id)
        self.assertEqual(result["graph_status"], "waiting_for_user")
        self.assertEqual(get_task(task_id)["revision"], 1)
        with self.assertRaisesRegex(ValueError, "不同输入"):
            run_intake_turn(task_id, "另一个意图", "test", None, "fake", "prompt-test", operation_id)

    def test_same_task_concurrent_input_is_rejected(self):
        initialize_schema()
        task_id = create_task("test")
        entered, release = Event(), Event()

        def slow_handle(*args):
            entered.set()
            self.assertTrue(release.wait(10))
            return self._needs_confirmation(*args)

        with patch("lapis_graph.handle_turn", side_effect=slow_handle):
            with ThreadPoolExecutor(max_workers=2) as pool:
                first = pool.submit(run_intake_turn, task_id, "第一轮", "test", None,
                                    "fake", "prompt-test", "concurrent-a-" + task_id)
                self.assertTrue(entered.wait(10))
                try:
                    with self.assertRaisesRegex(ValueError, "正在处理另一轮"):
                        run_intake_turn(task_id, "并发轮", "test", None,
                                        "fake", "prompt-test", "concurrent-b-" + task_id)
                finally:
                    release.set()
                first.result(timeout=10)
        self.assertEqual(get_task(task_id)["revision"], 1)

    def test_retry_same_operation_after_failure_before_commit(self):
        initialize_schema()
        task_id = create_task("test")
        operation_id = "retry-" + task_id
        attempts = 0

        def fail_once(*args):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise RuntimeError("注入：写入前失败")
            return self._needs_confirmation(*args)

        with patch("lapis_graph.handle_turn", side_effect=fail_once):
            with self.assertRaisesRegex(RuntimeError, "写入前失败"):
                run_intake_turn(task_id, "研究意图", "test", None, "fake", "prompt-test", operation_id)
            self.assertEqual(get_task(task_id)["revision"], 0)
            result = run_intake_turn(task_id, "研究意图", "test", None,
                                     "fake", "prompt-test", operation_id)
        self.assertEqual(result["graph_status"], "waiting_for_user")
        self.assertEqual(get_task(task_id)["revision"], 1)


    def test_v2_confirm_resumes_and_keeps_request_confirmed_status(self):
        from lapis_intake import Extraction, Update
        initialize_schema()
        task_id = create_task("test")
        text = "仅比较高电压电池的碳酸酯电解液稳定性"
        updates = Extraction(updates=[
            Update(field="purpose", status="specified", value="比较电解液稳定性", quote="比较"),
            Update(field="research_object", status="specified", value="碳酸酯电解液", quote="碳酸酯电解液"),
            Update(field="application", status="specified", value="高电压电池", quote="高电压电池"),
            Update(field="research_scope", status="specified", value="仅比较碳酸酯电解液", quote="仅比较高电压电池的碳酸酯电解液"),
            Update(field="target_performance", status="specified", value="稳定性",
                   direction="比较", quote="稳定性"),
            Update(field="material_function", status="specified", value="保持电解液稳定",
                   quote="电解液稳定性"),
        ])
        with patch("lapis_intake.extract", return_value=updates):
            first = run_intake_turn(task_id, text, "test", None, "fake", "prompt-v2")
        self.assertEqual(first["graph_status"], "waiting_for_user")
        self.assertIsNone(first["request_version"])
        second = run_intake_turn(task_id, "确认", "test", None, "fake", "prompt-v2")
        self.assertEqual(second["graph_status"], "idle")
        self.assertEqual(second["request_version"], 1)
        saved = get_task(task_id)
        self.assertEqual(saved["status"], "request_confirmed")
        self.assertEqual(saved["intake_result"]["request"]["contract_version"], 2)
        self.assertEqual(len(saved["intake_result"]["request"]["fields"]), 8)


if __name__ == "__main__":
    unittest.main()
