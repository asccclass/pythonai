import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from agent_runtime import AgentRuntime, AgentTurnResult, determine_episode_status, run_agent_turn
from memory import MemoryStore
from memory_review import process_memory_review_candidates
import server
from task_evaluator import EvaluationResult, TaskEvaluator


class TaskEvaluatorTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temp_dir.name)

    def tearDown(self):
        self.temp_dir.cleanup()

    # =========================================================
    # 1. 規則型斷言測試 (Rule-based Assertions)
    # =========================================================
    def test_evaluator_code_task_with_passing_tests(self):
        evaluator = TaskEvaluator(
            workspace_root=self.workspace,
            run_test_suite_fn=lambda target: "Exit Code: 0\n\n✅ All Tests Passed!\n1 passed in 0.05s",
            git_status_fn=lambda: "On branch main\nnothing to commit",
        )
        res = evaluator.evaluate(
            user_goal="請修復 tests/test_calc.py 的除以零錯誤",
            messages=[{"role": "user", "content": "修復 tests/test_calc.py"}],
            final_reply="已經修正 tests/test_calc.py 中的錯誤。",
            test_target="tests/test_calc.py",
        )
        self.assertTrue(res.success)
        self.assertEqual(res.score, 1.0)
        self.assertTrue(res.criteria_met.get("test_passed:tests/test_calc.py"))
        self.assertIn("驗收通過", res.feedback)

    def test_evaluator_code_task_with_failing_tests(self):
        evaluator = TaskEvaluator(
            workspace_root=self.workspace,
            run_test_suite_fn=lambda target: "Exit Code: 1\n\n❌ Tests Failed!\nAssertionError: 1 != 2",
            git_status_fn=lambda: "On branch main",
        )
        res = evaluator.evaluate(
            user_goal="寫一個相加函式並測試 tests/test_add.py",
            messages=[{"role": "user", "content": "測試 tests/test_add.py"}],
            final_reply="完成撰寫。",
            test_target="tests/test_add.py",
        )
        self.assertFalse(res.success)
        self.assertEqual(res.score, 0.0)
        self.assertFalse(res.criteria_met.get("test_passed:tests/test_add.py"))
        self.assertIn("測試套件未通過", res.feedback)

    def test_evaluator_detects_uncaught_exception(self):
        evaluator = TaskEvaluator(
            workspace_root=self.workspace,
            run_test_suite_fn=lambda target: "Exit Code: 1\n\nTraceback (most recent call last):\n  NameError: name 'foo' is not defined",
        )
        res = evaluator.evaluate(
            user_goal="執行 tests/test_err.py",
            messages=[{"role": "user", "content": "tests/test_err.py"}],
            final_reply="執行結束",
            test_target="tests/test_err.py",
        )
        self.assertFalse(res.success)
        self.assertIn("未捕獲異常", res.feedback)

    def test_verify_file_syntax_valid(self):
        py_file = self.workspace / "valid.py"
        py_file.write_text("def add(a: int, b: int) -> int:\n    return a + b\n", encoding="utf-8")

        evaluator = TaskEvaluator(workspace_root=self.workspace)
        ok, issues = evaluator.verify_file_syntax_and_state(["valid.py"])
        self.assertTrue(ok)
        self.assertEqual(len(issues), 0)

    def test_verify_file_syntax_invalid(self):
        py_file = self.workspace / "broken.py"
        py_file.write_text("def broken_syntax(:\n", encoding="utf-8")

        evaluator = TaskEvaluator(workspace_root=self.workspace)
        ok, issues = evaluator.verify_file_syntax_and_state(["broken.py"])
        self.assertFalse(ok)
        self.assertTrue(any("語法錯誤" in issue for issue in issues))

    def test_verify_file_missing_and_empty(self):
        empty_file = self.workspace / "empty.py"
        empty_file.write_text("", encoding="utf-8")

        evaluator = TaskEvaluator(workspace_root=self.workspace)
        ok, issues = evaluator.verify_file_syntax_and_state(["empty.py", "nonexistent.py"])
        self.assertFalse(ok)
        self.assertTrue(any("為空" in issue for issue in issues))
        self.assertTrue(any("不存在" in issue for issue in issues))

    def test_side_effects_capture_and_compare(self):
        evaluator = TaskEvaluator(
            workspace_root=self.workspace,
            git_status_fn=lambda: "clean",
        )
        pre_state = evaluator.capture_state(extra_context={"db_records": 10})

        # Create and modify files
        new_file = self.workspace / "created.py"
        new_file.write_text("print('hello')", encoding="utf-8")

        post_state = evaluator.capture_state(extra_context={"db_records": 11})
        diff = evaluator.compare_side_effects(pre_state, post_state)

        self.assertIn("created.py", diff["created_files"])
        self.assertEqual(diff["extra_diff"]["db_records"]["before"], 10)
        self.assertEqual(diff["extra_diff"]["db_records"]["after"], 11)

    # =========================================================
    # 2. LLM-as-a-Judge 驗收機制測試
    # =========================================================
    def test_llm_judge_evaluates_success(self):
        class MockChoice:
            def __init__(self, content):
                self.message = MagicMock(content=content)

        class MockResponse:
            def __init__(self, content):
                self.choices = [MockChoice(content)]

        mock_client = MagicMock()
        mock_client.chat.completions.create.return_value = MockResponse(
            json.dumps({"success": True, "score": 0.95, "feedback": "Goal fully achieved without errors."})
        )

        evaluator = TaskEvaluator(
            workspace_root=self.workspace,
            client=mock_client,
            model="test-judge-model",
            git_status_fn=lambda: "clean",
        )
        res = evaluator.evaluate(
            user_goal="寫一份說明文件並給出範例",
            messages=[{"role": "user", "content": "寫一份說明文件"}],
            final_reply="這是說明文件與範例程式碼。",
        )
        self.assertTrue(res.success)
        self.assertTrue(res.criteria_met.get("llm_judge_passed"))
        self.assertEqual(res.score, 1.0)

    def test_llm_judge_evaluates_failure(self):
        class MockChoice:
            def __init__(self, content):
                self.message = MagicMock(content=content)

        class MockResponse:
            def __init__(self, content):
                self.choices = [MockChoice(content)]

        mock_client = MagicMock()
        mock_client.chat.completions.create.return_value = MockResponse(
            json.dumps({"success": False, "score": 0.3, "feedback": "Failed to provide error handling."})
        )

        evaluator = TaskEvaluator(
            workspace_root=self.workspace,
            client=mock_client,
            model="test-judge-model",
            git_status_fn=lambda: "clean",
        )
        res = evaluator.evaluate(
            user_goal="實作包含例外處理的下載器",
            messages=[{"role": "user", "content": "實作包含例外處理的下載器"}],
            final_reply="這是一個基礎下載器。",
        )
        self.assertFalse(res.success)
        self.assertFalse(res.criteria_met.get("llm_judge_passed"))
        self.assertIn("LLM Judge 判定未通過", res.feedback)

    def test_llm_judge_fallback_on_client_error(self):
        mock_client = MagicMock()
        mock_client.chat.completions.create.side_effect = RuntimeError("API connection failure")

        evaluator = TaskEvaluator(
            workspace_root=self.workspace,
            client=mock_client,
            model="test-judge-model",
            git_status_fn=lambda: "clean",
        )
        passed, score, feedback = evaluator.evaluate_llm_judge("user goal", [], "reply")
        self.assertTrue(passed)
        self.assertIn("降級略過", feedback)

    # =========================================================
    # 3. Critic-Correction 自省修復迴圈測試 (server.run_agent)
    # =========================================================
    def test_run_agent_critic_correction_succeeds_on_retry(self):
        eval_attempts = 0

        def custom_evaluate(*args, **kwargs):
            nonlocal eval_attempts
            eval_attempts += 1
            if eval_attempts == 1:
                return EvaluationResult(success=False, score=0.4, feedback="測試未通過: AssertionError")
            return EvaluationResult(success=True, score=1.0, feedback="驗收通過。")

        mock_evaluator = MagicMock()
        mock_evaluator.capture_state.return_value = {}
        mock_evaluator.evaluate.side_effect = custom_evaluate

        # Mock LLM completions: turn 1 returns initial answer, turn 2 returns corrected answer
        responses = [
            MagicMock(choices=[MagicMock(message=MagicMock(tool_calls=None, content="Initial attempt"))]),
            MagicMock(choices=[MagicMock(message=MagicMock(tool_calls=None, content="Fixed attempt"))]),
        ]

        mock_client = MagicMock()
        mock_client.chat.completions.create.side_effect = responses

        messages = [{"role": "user", "content": "修復程式"}]
        store = MemoryStore(self.workspace / "memory.db")
        episode_id = store.start_episode()

        with patch("server.get_client", return_value=mock_client):
            result = server.run_agent(
                messages=messages,
                memory=store,
                episode_id=episode_id,
                task_evaluator=mock_evaluator,
                max_eval_retries=2,
            )

        self.assertEqual(result, "Fixed attempt")
        self.assertEqual(eval_attempts, 2)
        # Verify critic prompt was appended to messages
        self.assertTrue(any("任務驗收未通過 (Critic Feedback" in str(m.get("content")) for m in messages))

    def test_run_agent_reaches_max_eval_retries_and_triggers_hitl(self):
        mock_evaluator = MagicMock()
        mock_evaluator.capture_state.return_value = {}
        mock_evaluator.evaluate.return_value = EvaluationResult(
            success=False, score=0.2, feedback="持續發生 SyntaxError 無法修復"
        )

        mock_response = MagicMock(choices=[MagicMock(message=MagicMock(tool_calls=None, content="Still broken"))])
        mock_client = MagicMock()
        mock_client.chat.completions.create.return_value = mock_response

        messages = [{"role": "user", "content": "修復錯誤"}]
        store = MemoryStore(self.workspace / "memory.db")
        episode_id = store.start_episode()

        with patch("server.get_client", return_value=mock_client):
            result = server.run_agent(
                messages=messages,
                memory=store,
                episode_id=episode_id,
                task_evaluator=mock_evaluator,
                max_eval_retries=2,
            )

        self.assertIn("HITL", result)
        self.assertIn("已轉交人工審核", result)
        self.assertIn("已達最大修正次數 2", result)

        events = store.episode_events(episode_id)
        hitl_events = [e for e in events if e["event_type"] == "hitl_handover"]
        self.assertEqual(len(hitl_events), 1)

    # =========================================================
    # 4. Episode 狀態機與 Procedural Memory 沉澱防護測試
    # =========================================================
    def test_finish_episode_stores_refined_statuses(self):
        store = MemoryStore(self.workspace / "memory.db")

        ep1 = store.start_episode()
        store.finish_episode(ep1, status="verified_completed", summary="Task verified successfully")
        self.assertEqual(store.get_episode(ep1)["status"], "verified_completed")

        ep2 = store.start_episode()
        store.finish_episode(ep2, status="rejected", summary="Rejected by user or guard")
        self.assertEqual(store.get_episode(ep2)["status"], "rejected")

        ep3 = store.start_episode()
        store.finish_episode(ep3, status="needs_review", summary="HITL handover")
        self.assertEqual(store.get_episode(ep3)["status"], "needs_review")

        ep4 = store.start_episode()
        store.finish_episode(ep4, status="failed", summary="Unhandled exception")
        self.assertEqual(store.get_episode(ep4)["status"], "failed")

    def test_verified_completed_episode_saves_procedural_memory(self):
        store = MemoryStore(self.workspace / "memory.db")
        episode_id = store.start_episode()
        store.add_event(episode_id, "tool_call", metadata={"name": "run_command", "arguments": '{"cmd": "ls"}'})
        store.add_event(episode_id, "memory_review_candidate", metadata={"memory_kind": "procedure", "confidence": 0.8})

        store.finish_episode(episode_id, status="verified_completed")

        results = process_memory_review_candidates(store, episode_id)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["memory_kind"], "procedure")
        self.assertEqual(results[0]["action"], "created")

        procedures = store.active_procedures("run_command")
        self.assertEqual(len(procedures), 1)

    def test_unverified_episode_blocks_procedural_memory(self):
        for unverified_status in ("rejected", "needs_review", "failed"):
            with self.subTest(status=unverified_status):
                store = MemoryStore(self.workspace / f"memory_{unverified_status}.db")
                episode_id = store.start_episode()
                store.add_event(episode_id, "tool_call", metadata={"name": "buggy_tool", "arguments": '{"bad": true}'})
                store.add_event(episode_id, "memory_review_candidate", metadata={"memory_kind": "procedure", "confidence": 0.9})

                # Mark episode as unverified
                store.finish_episode(episode_id, status=unverified_status)

                results = process_memory_review_candidates(store, episode_id)
                self.assertEqual(len(results), 1)
                self.assertEqual(results[0]["action"], "rejected")
                self.assertIn("only verified_completed", results[0]["reason"])

                # Ensure NO procedural memory was created
                procedures = store.active_procedures("buggy_tool")
                self.assertEqual(len(procedures), 0)

    def test_run_agent_turn_marks_needs_review_on_hitl_handover(self):
        store = MemoryStore(self.workspace / "memory.db")
        hitl_reply = "⛔ 任務驗收未通過 (已達最大修正次數 2)，已轉交人工審核 (HITL)。\n未通過原因: 語法錯誤"

        runtime = AgentRuntime(
            messages=[],
            guard=MagicMock(assess=lambda inp: MagicMock(available=True, needs_confirmation=False, risk=0.1)),
            memory=store,
            memory_searcher=MagicMock(embedding_provider=None),
            skill_matcher=MagicMock(match=lambda inp: []),
            memory_worker=None,
            memory_classifier_factory=None,
            semantic_extractor=None,
            procedure_matcher=None,
            run_agent=lambda messages, **kwargs: hitl_reply,
        )

        result = run_agent_turn("修復程式", runtime)
        self.assertIn("HITL", result.reply)

        episode = store.get_episode(result.episode_id)
        self.assertEqual(episode["status"], "needs_review")


if __name__ == "__main__":
    unittest.main()
