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


def readable_content(content, url, limit=24000):
    if re.search(r'<(?:!doctype html|html|body|article)\b', content[:2000], re.I):
        from bs4 import BeautifulSoup
        import markdownify
        soup = BeautifulSoup(content, 'html.parser')
        main = soup.select_one('article.markdown-body') or soup.find('article') or soup.find('main') or soup.body or soup
        for element in main(['script', 'style', 'nav', 'header', 'footer', 'noscript']):
            element.decompose()
        content = markdownify.markdownify(str(main), heading_style='ATX').strip()
    if len(content) > limit:
        content = content[:limit] + '\n[Content truncated; fetch a specific source section/file if needed.]'
    return content
