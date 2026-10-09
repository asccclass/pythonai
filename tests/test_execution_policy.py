"""Include the policy package tests in the legacy unittest discovery command."""
from agent_execution.tests import test_policy, test_process


def load_tests(loader, tests, pattern):
    tests.addTests(loader.loadTestsFromModule(test_policy))
    tests.addTests(loader.loadTestsFromModule(test_process))
    return tests
