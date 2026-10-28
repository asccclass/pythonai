import json
from unittest.mock import patch

import httpx
import pytest

from agent_execution.operations.alerts import check, run


def test_probe_failures_emit_only_fixed_codes():
    with patch('agent_execution.operations.doctor.readiness',side_effect=OSError('private-path')), \
            patch('agent_execution.operations.metrics.snapshot',return_value={'available':False}):
        report=check()
    assert report['alerts']==['readiness_failed','trace_store_unavailable']
    assert 'private-path' not in json.dumps(report)


def test_healthy_and_preview_never_deliver():
    with patch('agent_execution.operations.alerts.check',return_value={'ready':True,'alerts':[]}), \
            patch('agent_execution.operations.alerts.httpx.Client') as client:
        assert run(True)['delivery']=='no_alerts'
        assert run()['delivery']=='not_requested'
        client.assert_not_called()


@pytest.mark.parametrize('status,expected',[(204,'accepted'),(302,'failed'),(500,'failed')])
def test_explicit_delivery_is_single_attempt_and_never_follows_redirect(status,expected):
    requests=[]
    def handle(request):
        requests.append(request)
        return httpx.Response(status,headers={'Location':'https://other.invalid/secret'},text='private-response')
    client=httpx.Client(transport=httpx.MockTransport(handle),follow_redirects=False)
    with patch('agent_execution.operations.alerts.check',return_value={'ready':False,'alerts':['readiness_failed']}), \
            patch.dict('os.environ',{'AGENT_OPS_ALERT_WEBHOOK_URL':'https://receiver.invalid/private','AGENT_OPS_ALERT_TOKEN':'private-token'}), \
            patch('agent_execution.operations.alerts.httpx.Client',return_value=client):
        report=run(True)
    assert report['delivery']==expected
    assert len(requests)==1
    assert requests[0].headers['Authorization']=='Bearer private-token'
    assert json.loads(requests[0].content)=={'component':'pythonai','alerts':['readiness_failed']}
    assert all(value not in json.dumps(report) for value in ('receiver.invalid','private-response','private-token'))


@pytest.mark.parametrize('url',['','http://receiver.invalid/','https://user:pass@receiver.invalid/','https://receiver.invalid:bad/'])
def test_invalid_delivery_configuration_fails_without_network(url):
    with patch('agent_execution.operations.alerts.check',return_value={'ready':False,'alerts':['readiness_failed']}), \
            patch.dict('os.environ',{'AGENT_OPS_ALERT_WEBHOOK_URL':url}), \
            patch('agent_execution.operations.alerts.httpx.Client') as client:
        assert run(True)['delivery']=='failed'
        client.assert_not_called()


def test_timeout_has_no_automatic_retry_or_private_error_details():
    requests=[]
    def handle(request):
        requests.append(request)
        raise httpx.ReadTimeout('private-endpoint',request=request)
    client=httpx.Client(transport=httpx.MockTransport(handle))
    with patch('agent_execution.operations.alerts.check',return_value={'ready':False,'alerts':['readiness_failed']}), \
            patch.dict('os.environ',{'AGENT_OPS_ALERT_WEBHOOK_URL':'https://receiver.invalid/'}), \
            patch('agent_execution.operations.alerts.httpx.Client',return_value=client):
        report=run(True)
    assert report['delivery']=='failed' and report['error_type']=='ReadTimeout'
    assert len(requests)==1 and 'private-endpoint' not in json.dumps(report)


def test_cli_unhealthy_exit_is_nonzero_and_delivery_requires_flag(capsys):
    from agent_execution.operations.__main__ import main
    with patch('sys.argv',['operations','alerts']), \
            patch('agent_execution.operations.alerts.run',return_value={'ready':False,'alerts':['readiness_failed']}) as probe:
        with pytest.raises(SystemExit) as stopped:
            main()
    assert stopped.value.code==1
    probe.assert_called_once_with(False)
    assert json.loads(capsys.readouterr().out)['alerts']==['readiness_failed']
