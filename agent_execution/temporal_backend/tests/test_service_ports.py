"""Test-server port handoff must not collide with CLI bind(:0) allocation."""
from unittest.mock import MagicMock,Mock,patch
import pytest
from agent_execution.temporal_backend.tests.test_integration import LocalServer,frontend_port


def test_frontend_skips_busy_candidates_and_never_requests_ephemeral_zero():
    sock=MagicMock()
    sock.__enter__.return_value=sock
    sock.bind.side_effect=[OSError('busy'),None]
    with patch('agent_execution.temporal_backend.tests.test_integration.random.SystemRandom') as random,patch(
            'agent_execution.temporal_backend.tests.test_integration.socket.socket',return_value=sock):
        random.return_value.sample.return_value=[21000,21001]
        assert frontend_port()==21001
    assert [c.args for c in sock.bind.call_args_list]==[(('127.0.0.1',21000),),(('127.0.0.1',21001),)]


def test_frontend_exhaustion_is_bounded_and_fails_before_launch():
    sock=MagicMock()
    sock.__enter__.return_value=sock
    sock.bind.side_effect=OSError('occupied')
    with patch('agent_execution.temporal_backend.tests.test_integration.random.SystemRandom') as random,patch(
            'agent_execution.temporal_backend.tests.test_integration.socket.socket',return_value=sock):
        random.return_value.sample.return_value=list(range(20000,20100))
        with pytest.raises(RuntimeError,match='No isolated'):frontend_port()
    assert sock.bind.call_count==100


def test_service_restart_keeps_the_reviewed_frontend_address(tmp_path):
    process=Mock()
    process.wait.return_value=0
    with patch('agent_execution.temporal_backend.tests.test_integration.frontend_port',return_value=23456) as port,patch(
            'agent_execution.temporal_backend.tests.test_integration.subprocess.Popen',return_value=process) as launch:
        server=LocalServer('test-temporal',tmp_path)
        for _ in range(2):
            server.start()
            server.stop()
        port.assert_called_once()
        assert server.address=='127.0.0.1:23456'
        assert all(c.args[0][c.args[0].index('--port')+1]=='23456' for c in launch.call_args_list)
