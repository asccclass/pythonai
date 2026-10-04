import json
import urllib.request
import urllib.parse
from bs4 import BeautifulSoup
import markdownify

def web_research(query: str, max_results: int = 3) -> str:
    """
    Perform deep web research on a specific topic.
    Searches DuckDuckGo, visits the top search results, and parses the webpage contents into Markdown.
    """
    try:
        from ddgs import DDGS
    except ImportError:
        return "Error: ddgs package not found. Please install it with 'pip install ddgs'."

    results = []
    try:
        # 1. Search DuckDuckGo
        with DDGS() as ddgs:
            search_results = list(ddgs.text(query, max_results=max_results))
        
        if not search_results:
            return f"No results found for query: {query}"
        
        output = [f"# Deep Web Research Report: {query}\n"]
        
        # 2. Fetch and parse each result
        for idx, result in enumerate(search_results, 1):
            url = result.get('href')
            title = result.get('title')
            snippet = result.get('body')
            
            output.append(f"## {idx}. {title}")
            output.append(f"**URL:** {url}")
            output.append(f"**Snippet:** {snippet}\n")
            
            try:
                # Fetch webpage
                req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'})
                with urllib.request.urlopen(req, timeout=10) as response:
                    html_content = response.read().decode('utf-8', errors='ignore')
                
                # Parse HTML with BeautifulSoup
                soup = BeautifulSoup(html_content, 'html.parser')
                
                # Remove script, style, header, footer, nav to clean up content
                for element in soup(["script", "style", "header", "footer", "nav", "aside", "noscript"]):
                    element.decompose()
                
                # Convert to Markdown
                md_text = markdownify.markdownify(str(soup.body if soup.body else soup), heading_style="ATX").strip()
                
                # Truncate to avoid exploding context window (limit to ~4000 characters per page)
                if len(md_text) > 4000:
                    md_text = md_text[:4000] + "\n\n...[Content Truncated]..."
                    
                output.append("### Content:")
                output.append(md_text)
                output.append("\n---\n")
                
            except Exception as fetch_error:
                output.append(f"*(Failed to fetch full page content: {fetch_error})*\n\n---\n")
                
        return "\n".join(output)
        
    except Exception as e:
        return f"Web research failed: {str(e)}"
