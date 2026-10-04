import json

with open('base.py', 'r', encoding='utf-8') as f:
    text = f.read()

old_func = '''def search_web(query: str, max_results: int = 5) -> str:
    try:
        from duckduckgo_search import DDGS
        results = DDGS().text(query, max_results=max_results)
        import json
        return json.dumps(results, ensure_ascii=False, indent=2)
    except Exception as e:
        return f"Search failed: {e}"'''

new_func = '''def search_web(query: str, max_results: int = 5) -> str:
    try:
        import urllib.request
        import urllib.parse
        import json
        
        url = f"https://en.wikipedia.org/w/api.php?action=query&list=search&srsearch={urllib.parse.quote(query)}&utf8=&format=json"
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req) as response:
            data = json.loads(response.read().decode())
        
        results = data.get("query", {}).get("search", [])[:max_results]
        formatted = [{"title": r["title"], "snippet": r["snippet"]} for r in results]
        return json.dumps(formatted, ensure_ascii=False, indent=2)
    except Exception as e:
        return f"Search failed: {e}"'''

if old_func in text:
    print("Found exact old func!")
    text = text.replace(old_func, new_func)
else:
    print("Exact old func not found, trying without exact match")
    # manual replacement by finding the start and end
    start_idx = text.find('def search_web(query: str, max_results: int = 5) -> str:')
    end_idx = text.find('def browse_webpage(url: str) -> str:')
    if start_idx != -1 and end_idx != -1:
        text = text[:start_idx] + new_func + '\n\n' + text[end_idx:]

with open('base.py', 'w', encoding='utf-8') as f:
    f.write(text)

