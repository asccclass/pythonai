import unittest
from unittest.mock import patch
import tempfile

from hooks import FunctionHook, HookContext, HookDecision, HookLifecycle, HookManager, HookResult
from memory import MemoryStore
import server


class HookManagerTests(unittest.TestCase):
    def test_pre_model_call_can_return_early_without_llm_call(self):
        manager = HookManager(
            [
                FunctionHook(
                    "early",
                    HookLifecycle.PRE_MODEL_CALL,
                    lambda context: HookResult.return_early("blocked before model"),
                )
            ]
        )
        with patch("server.get_client") as get_client:
            result = server.run_agent([{"role": "user", "content": "hi"}], hook_manager=manager)

        self.assertEqual(result, "blocked before model")
        get_client.assert_not_called()

    def test_post_model_call_can_append_feedback(self):
        class Message:
            tool_calls = None
            content = "draft"

        class Choice:
            message = Message()

        class Response:
            choices = [Choice()]

        class Completions:
            def __init__(self):
                self.calls = 0

            def create(self, **kwargs):
                self.calls += 1
                if self.calls == 1:
                    return Response()
                final = Response()
                final.choices[0].message.content = "fixed"
                return final

        class Chat:
            def __init__(self):
                self.completions = Completions()

        class Client:
            def __init__(self):
                self.chat = Chat()

        def feedback_once(context: HookContext):
            if context.metadata["iteration"] == 1:
                return HookResult.append_feedback("revise the answer")
            return HookResult.continue_()

        manager = HookManager([FunctionHook("critic", HookLifecycle.POST_MODEL_CALL, feedback_once)])
        client = Client()
        messages = [{"role": "user", "content": "hi"}]

        with patch("server.get_client", return_value=client):
            result = server.run_agent(messages, hook_manager=manager)

        self.assertEqual(result, "fixed")
        self.assertTrue(any(message.get("content") == "revise the answer" for message in messages))

    def test_pre_tool_execute_can_return_standard_tool_error(self):
        class Function:
            name = "list_files"
            arguments = '{"path": "."}'

        class ToolCall:
            id = "tool-1"
            function = Function()

        class ToolMessage:
            tool_calls = [ToolCall()]
            content = None

        class FinalMessage:
            tool_calls = None
            content = "done"

        class Choice:
            def __init__(self, message):
                self.message = message

        class Response:
            def __init__(self, message):
                self.choices = [Choice(message)]

        class Completions:
            def __init__(self):
                self.responses = [Response(ToolMessage()), Response(FinalMessage())]

            def create(self, **kwargs):
                return self.responses.pop(0)

        class Chat:
            def __init__(self):
                self.completions = Completions()

        class Client:
            def __init__(self):
                self.chat = Chat()

        manager = HookManager(
            [
                FunctionHook(
                    "deny-list",
                    HookLifecycle.PRE_TOOL_EXECUTE,
                    lambda context: HookResult.return_tool_error("Error: blocked by policy"),
                )
            ]
        )
        messages = [{"role": "user", "content": "list"}]

        with (
            patch("server.get_client", return_value=Client()),
            patch("server.run_tool_with_context") as run_tool,
        ):
            result = server.run_agent(messages, hook_manager=manager)

        self.assertEqual(result, "done")
        run_tool.assert_not_called()
        self.assertEqual(messages[-2]["content"], "Error: blocked by policy")

    def test_pre_tool_execute_can_mutate_tool_arguments(self):
        class Function:
            name = "list_files"
            arguments = '{"path": "before"}'

        class ToolCall:
            id = "tool-1"
            function = Function()

        class ToolMessage:
            tool_calls = [ToolCall()]
            content = None

        class FinalMessage:
            tool_calls = None
            content = "done"

        class Choice:
            def __init__(self, message):
                self.message = message

        class Response:
            def __init__(self, message):
                self.choices = [Choice(message)]

        class Completions:
            def __init__(self):
                self.responses = [Response(ToolMessage()), Response(FinalMessage())]

            def create(self, **kwargs):
                return self.responses.pop(0)

        class Chat:
            def __init__(self):
                self.completions = Completions()

        class Client:
            def __init__(self):
                self.chat = Chat()

        def normalize(context: HookContext):
            context.tool_arguments["path"] = "after"
            return HookResult.continue_(mutation_summary="path normalized")

        manager = HookManager([FunctionHook("normalize", HookLifecycle.PRE_TOOL_EXECUTE, normalize)])
        seen = {}

        def run_tool(tool_call, **kwargs):
            seen["arguments"] = tool_call.function.arguments
            return "ok"

        with (
            patch("server.get_client", return_value=Client()),
            patch("server.run_tool_with_context", side_effect=run_tool),
        ):
            result = server.run_agent([{"role": "user", "content": "list"}], hook_manager=manager)

        self.assertEqual(result, "done")
        self.assertIn('"path": "after"', seen["arguments"])

    def test_post_tool_execute_can_mutate_result_and_append_feedback(self):
        class Function:
            name = "list_files"
            arguments = '{"path": "."}'

        class ToolCall:
            id = "tool-1"
            function = Function()

        class ToolMessage:
            tool_calls = [ToolCall()]
            content = None

        class FinalMessage:
            tool_calls = None
            content = "done"

        class Choice:
            def __init__(self, message):
                self.message = message

        class Response:
            def __init__(self, message):
                self.choices = [Choice(message)]

        class Completions:
            def __init__(self):
                self.responses = [Response(ToolMessage()), Response(FinalMessage())]

            def create(self, **kwargs):
                return self.responses.pop(0)

        class Chat:
            def __init__(self):
                self.completions = Completions()

        class Client:
            def __init__(self):
                self.chat = Chat()

        def post_tool(context: HookContext):
            context.tool_result = "sanitized"
            context.record_mutation("tool_result sanitized")
            return HookResult.append_feedback("tool output failed acceptance", mutation_summary="append feedback")

        manager = HookManager([FunctionHook("acceptance", HookLifecycle.POST_TOOL_EXECUTE, post_tool)])

        with (
            patch("server.get_client", return_value=Client()),
            patch("server.run_tool_with_context", return_value="secret"),
        ):
            messages = [{"role": "user", "content": "list"}]
            result = server.run_agent(messages, hook_manager=manager)

        self.assertEqual(result, "done")
        self.assertTrue(any(message.get("content") == "tool output failed acceptance" for message in messages))
        self.assertTrue(any(message.get("content") == "sanitized" for message in messages))

    def test_priority_and_short_circuit_stop_later_hooks(self):
        calls = []

        def first(context):
            calls.append("first")
            return HookResult.block("stop")

        def second(context):
            calls.append("second")
            return HookResult.continue_()

        manager = HookManager(
            [
                FunctionHook("second", HookLifecycle.PRE_MODEL_CALL, second, priority=20),
                FunctionHook("first", HookLifecycle.PRE_MODEL_CALL, first, priority=10),
            ]
        )
        result = manager.dispatch(HookLifecycle.PRE_MODEL_CALL, HookContext(HookLifecycle.PRE_MODEL_CALL))

        self.assertEqual(result.decision, HookDecision.BLOCK)
        self.assertEqual(calls, ["first"])

    def test_hook_mutation_is_audited_in_episode_events(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = MemoryStore(f"{tmpdir}/memory.db")
            episode_id = store.start_episode()

            def mutate(context: HookContext):
                context.messages.append({"role": "system", "content": "injected"})
                return HookResult.continue_(mutation_summary="injected system message")

            manager = HookManager([FunctionHook("inject", HookLifecycle.PRE_MODEL_CALL, mutate)])
            context = HookContext(
                HookLifecycle.PRE_MODEL_CALL,
                messages=[],
                memory=store,
                episode_id=episode_id,
            )
            manager.dispatch(HookLifecycle.PRE_MODEL_CALL, context)

            events = store.episode_events(episode_id)
            hook_events = [event for event in events if event["event_type"] == "hook_execution"]
            self.assertEqual(hook_events[0]["metadata"]["hook"], "inject")
            self.assertEqual(hook_events[0]["metadata"]["mutation_summary"], "injected system message")


if __name__ == "__main__":
    unittest.main()
