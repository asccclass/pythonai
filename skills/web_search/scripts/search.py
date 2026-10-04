import json
import sys

def search_ddg(query: str, max_results: int = 5) -> list[dict]:
    try:
        from duckduckgo_search import DDGS
        with DDGS() as ddgs:
            results = list(ddgs.text(query, max_results=max_results))
            formatted = []
            for item in results:
                formatted.append({
                    "title": item.get("title", ""),
                    "href": item.get("href", "") or item.get("link", ""),
                    "body": item.get("body", "") or item.get("snippet", "")
                })
            return formatted
    except Exception as error:
        return [{"error": f"DuckDuckGo search failed: {error}"}]

def main() -> None:
    if len(sys.argv) < 2 or not sys.argv[1].strip():
        print("請提供搜尋關鍵字")
        sys.exit(1)
    
    query = sys.argv[1].strip()
    results = search_ddg(query)
    
    if not results or "error" in results[0]:
        error_msg = results[0]["error"] if results else "查無搜尋結果"
        print(f"搜尋失敗：{error_msg}")
        return

    output_lines = [f"=== 「{query}」DuckDuckGo 搜尋結果 ==="]
    for idx, item in enumerate(results, start=1):
        output_lines.append(f"{idx}. {item['title']}")
        output_lines.append(f"   連結: {item['href']}")
        output_lines.append(f"   摘要: {item['body']}\n")
    
    print("\n".join(output_lines))

if __name__ == "__main__":
    main()
