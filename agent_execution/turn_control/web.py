"""Readable, bounded web evidence for the model rather than HTML navigation chrome."""
from urllib.parse import urlsplit
import re


def fetch_target(url):
    parsed = urlsplit(url)
    parts = parsed.path.strip('/').split('/')
    if parsed.hostname == 'github.com' and len(parts) == 2 and all(re.fullmatch(r'[\w.-]+', p) for p in parts):
        owner, repo = parts
        return f'https://api.github.com/repos/{owner}/{repo.removesuffix(".git")}/readme'
    return url


def _bounded(content, limit):
    if len(content) <= limit:
        return content
    marker = ('\n[Content truncated; middle omitted. Use fetch_url with contains set to the '
              'required name or phrase before making factual claims.]\n')
    available = max(0, limit - len(marker))
    head = int(available * .6)
    tail = available - head
    return content[:head] + marker + (content[-tail:] if tail else '')


def _focused(content, contains, limit):
    """Return bounded, merged excerpts around every exact case-insensitive match."""
    needle = contains.casefold()
    folded = content.casefold()
    starts = []
    offset = 0
    while len(starts) < 12:
        found = folded.find(needle, offset)
        if found < 0:
            break
        starts.append(found)
        offset = found + max(1, len(needle))
    if not starts:
        note = f'\n[Requested focus text not found: {contains}]'
        return _bounded(content, max(0, limit-len(note))) + note
    radius = max(600, min(2400, limit // max(2, len(starts) * 2)))
    windows = []
    for found in starts:
        start, end = max(0, found-radius), min(len(content), found+len(contains)+radius)
        if windows and start <= windows[-1][1]:
            windows[-1] = (windows[-1][0], max(windows[-1][1], end))
        else:
            windows.append((start, end))
    excerpts = '\n\n[...non-matching content omitted...]\n\n'.join(content[start:end] for start,end in windows)
    return _bounded(f'[Focused excerpts containing: {contains}]\n' + excerpts, limit)


def readable_content(content, url, limit=24000, contains=''):
    if re.search(r'<(?:!doctype html|html|body|article)\b', content[:2000], re.I):
        from bs4 import BeautifulSoup
        import markdownify
        soup = BeautifulSoup(content, 'html.parser')
        main = soup.select_one('article.markdown-body') or soup.find('article') or soup.find('main') or soup.body or soup
        for element in main(['script', 'style', 'nav', 'header', 'footer', 'noscript']):
            element.decompose()
        content = markdownify.markdownify(str(main), heading_style='ATX').strip()
    if contains:
        return _focused(content, contains, limit)
    return _bounded(content, limit)
