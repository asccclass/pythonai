"""Tests for MCP readonly tool reuse logic in TurnProgress."""
import pytest
from types import SimpleNamespace
from agent_execution.turn_control import TurnProgress


# ---------------------------------------------------------------------------
# is_mcp_readonly detection
# ---------------------------------------------------------------------------

class TestIsMcpReadonly:
    def test_list_action(self):
        assert TurnProgress.is_mcp_readonly('mcp_aisms_list_firewall_requests')

    def test_get_action(self):
        assert TurnProgress.is_mcp_readonly('mcp_aisms_get_user')

    def test_read_action(self):
        assert TurnProgress.is_mcp_readonly('mcp_someserver_read_document')

    def test_search_action(self):
        assert TurnProgress.is_mcp_readonly('mcp_aisms_search_records')

    def test_fetch_action(self):
        assert TurnProgress.is_mcp_readonly('mcp_someserver_fetch_data')

    def test_query_action(self):
        assert TurnProgress.is_mcp_readonly('mcp_someserver_query_items')

    def test_list_at_end(self):
        # edge case: ends with _list (no trailing segment)
        assert TurnProgress.is_mcp_readonly('mcp_someserver_list')

    def test_create_action_not_readonly(self):
        assert not TurnProgress.is_mcp_readonly('mcp_aisms_create_record')

    def test_delete_action_not_readonly(self):
        assert not TurnProgress.is_mcp_readonly('mcp_aisms_delete_entry')

    def test_update_action_not_readonly(self):
        assert not TurnProgress.is_mcp_readonly('mcp_aisms_update_user')

    def test_non_mcp_prefix_not_readonly(self):
        assert not TurnProgress.is_mcp_readonly('read_file')

    def test_non_mcp_prefix_search(self):
        assert not TurnProgress.is_mcp_readonly('search_web')

    def test_partial_mcp_prefix_not_readonly(self):
        # Does not start with 'mcp_'
        assert not TurnProgress.is_mcp_readonly('notmcp_server_list_items')


# ---------------------------------------------------------------------------
# before() reuse behaviour for MCP readonly tools
# ---------------------------------------------------------------------------

class _FakeResult:
    def __init__(self, success, output='', error=''):
        self.success = success
        self.output = output
        self.error = error
        self.evidence = {}


class TestMcpReadonlyReuse:
    def _make_progress(self):
        return TurnProgress()

    def test_first_call_returns_none(self):
        """First call with a fresh MCP readonly tool should NOT be blocked."""
        progress = self._make_progress()
        result = progress.before('mcp_aisms_list_firewall_requests', {'keyword': '桌機'})
        assert result is None

    def test_second_call_after_success_returns_reuse_message(self):
        """Second identical call after success should return a reuse message."""
        progress = self._make_progress()
        ok = _FakeResult(True, '[]')
        progress.observe('mcp_aisms_list_firewall_requests', {'keyword': '桌機'}, ok)
        msg = progress.before('mcp_aisms_list_firewall_requests', {'keyword': '桌機'})
        assert msg is not None
        assert 'MCP read already succeeded' in msg or 'already succeeded' in msg.lower()

    def test_second_call_after_success_contains_reuse_guidance(self):
        """Reuse message should instruct to reuse result, not query again."""
        progress = self._make_progress()
        ok = _FakeResult(True, '[{"id":1}]')
        progress.observe('mcp_aisms_list_firewall_requests', {'keyword': 'test'}, ok)
        msg = progress.before('mcp_aisms_list_firewall_requests', {'keyword': 'test'})
        assert msg is not None
        lower = msg.lower()
        assert 'reuse' in lower or 'do not query' in lower

    def test_different_args_not_blocked(self):
        """Call with different args should NOT be reused/blocked."""
        progress = self._make_progress()
        ok = _FakeResult(True, '[]')
        progress.observe('mcp_aisms_list_firewall_requests', {'keyword': '桌機'}, ok)
        result = progress.before('mcp_aisms_list_firewall_requests', {'keyword': '筆電'})
        assert result is None

    def test_second_call_after_failure_not_reused(self):
        """After a failed call, repeat should NOT trigger the reuse path (success=False)."""
        progress = self._make_progress()
        fail = _FakeResult(False, '', 'not found')
        progress.observe('mcp_aisms_list_firewall_requests', {'keyword': '桌機'}, fail)
        msg = progress.before('mcp_aisms_list_firewall_requests', {'keyword': '桌機'})
        # Should not return the MCP reuse message (success path)
        if msg is not None:
            assert 'already succeeded' not in msg.lower()

    def test_non_readonly_mcp_not_reused(self):
        """Write MCP tools should not be treated as reusable reads."""
        progress = self._make_progress()
        ok = _FakeResult(True, 'created')
        progress.observe('mcp_aisms_create_record', {'data': 'x'}, ok)
        result = progress.before('mcp_aisms_create_record', {'data': 'x'})
        # Should not hit the MCP readonly reuse path; may be None or blocked by repeat logic
        if result is not None:
            assert 'already succeeded' not in result.lower()

    def test_mcp_list_different_server_reused(self):
        """Reuse logic applies regardless of MCP server name."""
        progress = self._make_progress()
        ok = _FakeResult(True, '[]')
        progress.observe('mcp_otherserver_list_items', {'q': 'foo'}, ok)
        msg = progress.before('mcp_otherserver_list_items', {'q': 'foo'})
        assert msg is not None
        assert 'already succeeded' in msg.lower()

