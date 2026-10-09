"""Include the policy package tests in the legacy unittest discovery command."""
from agent_execution.tests import test_policy, test_process, test_approvals, test_auth_transport, test_sessions, test_streaming, test_delivery, test_tasks
from agent_execution.tests import test_budgets
from agent_execution.tests import test_results
from agent_execution.tests import test_hook_policies


def load_tests(loader, tests, pattern):
    tests.addTests(loader.loadTestsFromModule(test_policy))
    tests.addTests(loader.loadTestsFromModule(test_process))
    tests.addTests(loader.loadTestsFromModule(test_approvals))
    tests.addTests(loader.loadTestsFromModule(test_auth_transport))
    tests.addTests(loader.loadTestsFromModule(test_sessions))
    tests.addTests(loader.loadTestsFromModule(test_streaming))
    tests.addTests(loader.loadTestsFromModule(test_delivery))
    tests.addTests(loader.loadTestsFromModule(test_tasks))
    tests.addTests(loader.loadTestsFromModule(test_budgets))
    tests.addTests(loader.loadTestsFromModule(test_results))
    tests.addTests(loader.loadTestsFromModule(test_hook_policies))
    return tests
