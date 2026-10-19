"""Private persistent profile worker; never expose this executable as a model tool."""
import ipaddress
import json
from pathlib import Path
import socket
import sys
import uuid
from urllib.parse import urlsplit
from filelock import FileLock


def error_summary(error):
    """Return diagnostics useful to the caller without serialising a traceback."""
    detail=' '.join(str(error).split())[:512]
    return f'{type(error).__name__}: {detail}' if detail else type(error).__name__


def allowed_url(url,hosts):
    parsed=urlsplit(url)
    if parsed.scheme not in {'http','https'} or not parsed.hostname or parsed.username or parsed.password:raise ValueError('Browser requires HTTP(S) without URL credentials')
    if parsed.hostname in hosts:return
    addresses=socket.getaddrinfo(parsed.hostname,parsed.port or (443 if parsed.scheme=='https' else 80))
    if not addresses or any(not ipaddress.ip_address(value[4][0]).is_global for value in addresses):raise PermissionError('Private browser network destination denied')


def actions(page,items,artifacts):
    for item in items:
        if not isinstance(item,dict):raise ValueError('Action must be an object')
        kind=item.get('action');selector=item.get('selector','')
        if not isinstance(selector,str) or len(selector)>512:raise ValueError('Selector limit')
        locator=page.locator(selector) if selector else None
        if kind=='click':locator.click(timeout=5000)
        elif kind=='type':
            text=item.get('text','')
            if not isinstance(text,str) or len(text)>8192:raise ValueError('Form input limit')
            locator.fill(text,timeout=5000)
        elif kind=='wait_for':locator.wait_for(state='visible',timeout=5000)
        elif kind=='wait':
            import time
            ms=item.get('time',1000)
            if not isinstance(ms,(int,float)) or not 0<ms<=10000:raise ValueError('Wait time must be 1-10000 ms')
            time.sleep(ms/1000)
        elif kind=='assert_text':
            from playwright.sync_api import expect
            expect(locator).to_have_text(item['text'],timeout=5000)
        elif kind=='assert_url':
            if page.url!=item['url']:raise AssertionError('Browser URL assertion failed')
        elif kind=='download':
            with page.expect_download(timeout=5000) as pending:locator.click(timeout=5000)
            download=pending.value
            path=Path(download.path())
            if path.stat().st_size>10*1024*1024:raise ValueError('Download exceeds 10 MiB')
            destination=artifacts/(uuid.uuid4().hex+'.download');download.save_as(destination)
        else:raise ValueError('Unsupported browser action')


def main(request,response):
    value=json.loads(Path(request).read_text(encoding='utf-8'));profile=Path(value['profile']);artifacts=Path(value['artifacts'])
    result={'success':False,'error':'Browser action failed'}
    try:
        from playwright.sync_api import sync_playwright
        allowed_url(value['url'],value['allowed_hosts'])
        with FileLock(str(profile)+'.lock',timeout=2),sync_playwright() as playwright:
            if profile.exists() and sum(p.stat().st_size for p in profile.rglob('*') if p.is_file())>64*1024*1024:raise ValueError('Browser profile exceeds 64 MiB')
            context=playwright.chromium.launch_persistent_context(str(profile),headless=True,accept_downloads=True,service_workers='block')
            try:
                state_file=profile/'pythonai-state.json'
                if state_file.is_symlink():raise PermissionError('Browser state symlink rejected')
                if state_file.is_file():
                    if state_file.stat().st_size>1024*1024:raise ValueError('Browser state capacity exceeded')
                    context.add_cookies(json.loads(state_file.read_text(encoding='utf-8'))['cookies'])
                def route(route):
                    try:allowed_url(route.request.url,value['allowed_hosts']);route.continue_()
                    except Exception:route.abort()
                context.route('**/*',route)
                context.route_web_socket('**/*',lambda socket:socket.close())
                page=context.pages[0] if context.pages else context.new_page()
                page.goto(value['url'],wait_until='networkidle',timeout=30000)
                actions(page,value['actions'],artifacts)
                if value['screenshot']:page.screenshot(path=str(artifacts/'page.jpg'),type='jpeg',quality=70)
                result={'success':True,'url':page.url,'text':page.locator('body').inner_text(timeout=5000)[:65536],
                        'artifacts':[str(p) for p in artifacts.iterdir() if p.is_file()]}
                state=context.storage_state()
                serialized=json.dumps(state)
                if len(serialized.encode())>1024*1024:raise ValueError('Browser state capacity exceeded')
                state_file.write_text(serialized,encoding='utf-8')
            finally:context.close()
    except Exception as error:result={'success':False,'error':error_summary(error)}
    Path(response).write_text(json.dumps(result,ensure_ascii=False),encoding='utf-8')


if __name__=='__main__':main(sys.argv[1],sys.argv[2])
