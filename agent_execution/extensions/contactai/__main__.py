"""Read-only ContactAI authentication and MCP discovery check."""
import argparse
import getpass
import json
import os
from pathlib import Path

from ..remote import RemoteMCPClient


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', default='mcp_config.json')
    parser.add_argument('--prompt', action='store_true', help='Prompt for MemAuth email/password without saving')
    args = parser.parse_args()
    client = None
    previous = {}
    try:
        source = Path(args.config)
        if source.stat().st_size > 1024 * 1024:
            raise ValueError('MCP configuration exceeds limit')
        config = json.loads(source.read_text(encoding='utf-8'))['mcpServers']['contactai']
        if args.prompt:
            auth = config['memauth']
            values = {auth.get('email_env', 'CONTACTAI_EMAIL'): input('MemAuth email: ').strip(),
                      auth.get('password_env', 'CONTACTAI_PASSWORD'): getpass.getpass('MemAuth password: '),
                      auth.get('token_env', 'CONTACTAI_MEMAUTH_TOKEN'): ''}
            for name, value in values.items():
                previous[name] = os.environ.get(name)
                os.environ[name] = value
        client = RemoteMCPClient('contactai', config)
        client.start()
        health = client.health()
        print(json.dumps({'ready': health['ready'], 'tools': [tool['name'] for tool in client.tools]}, ensure_ascii=False))
        return 0 if health['ready'] else 1
    except Exception as error:
        print(json.dumps({'ready': False, 'error_type': type(error).__name__}))
        return 1
    finally:
        if client:
            client.stop()
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


if __name__ == '__main__':
    raise SystemExit(main())
