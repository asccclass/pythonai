"""Include the policy package tests in the legacy unittest discovery command."""
from agent_execution.tests import test_policy


def load_tests(loader, tests, pattern):
    return loader.loadTestsFromModule(test_policy)
