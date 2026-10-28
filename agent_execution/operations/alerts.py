"""One-shot operator health alerts; delivery requires explicit CLI opt-in."""
import os
from urllib.parse import urlsplit

import httpx


def check():
    from .doctor import readiness
    from .metrics import snapshot
    alerts = []
    for probe, code in ((readiness, 'readiness_failed'), (snapshot, 'trace_store_unavailable')):
        try:
            report = probe()
            healthy = report.get('ready', False) if code == 'readiness_failed' else report.get('available', False)
        except Exception:
            healthy = False
        if not healthy:
            alerts.append(code)
    return {'ready': not alerts, 'alerts': alerts, 'scope': 'local operator health checks'}


def run(deliver=False):
    report = check()
    report['delivery'] = 'not_requested'
    if not deliver or not report['alerts']:
        if deliver:
            report['delivery'] = 'no_alerts'
        return report
    url = os.environ.get('AGENT_OPS_ALERT_WEBHOOK_URL', '')
    token = os.environ.get('AGENT_OPS_ALERT_TOKEN', '')
    try:
        parsed = urlsplit(url)
        parsed.port  # Validate malformed/out-of-range ports before opening a connection.
        if (parsed.scheme != 'https' or not parsed.hostname or parsed.username is not None
                or parsed.password is not None or parsed.fragment or '\r' in token or '\n' in token):
            raise ValueError('Invalid webhook configuration')
        headers = {'Authorization': 'Bearer ' + token} if token else {}
        # Only fixed codes leave the host. Never include probe details, URLs, or identities.
        payload = {'component': 'pythonai', 'alerts': report['alerts']}
        with httpx.Client(timeout=5, follow_redirects=False, trust_env=False) as client:
            with client.stream('POST', url, json=payload, headers=headers) as response:
                response.raise_for_status()
        report['delivery'] = 'accepted'
    except (ValueError, httpx.InvalidURL, httpx.HTTPError) as error:
        report['delivery'] = 'failed'
        report['error_type'] = type(error).__name__
    return report
