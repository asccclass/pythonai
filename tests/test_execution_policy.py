"""Include the policy package tests in the legacy unittest discovery command."""
from agent_execution.tests import test_policy, test_process, test_approvals, test_auth_transport


def load_tests(loader, tests, pattern):
    tests.addTests(loader.loadTestsFromModule(test_policy))
    tests.addTests(loader.loadTestsFromModule(test_process))
    tests.addTests(loader.loadTestsFromModule(test_approvals))
    tests.addTests(loader.loadTestsFromModule(test_auth_transport))
    return tests
