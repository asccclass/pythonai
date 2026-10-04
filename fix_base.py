import json
import re

with open('base.py', 'r', encoding='utf-8') as f:
    content = f.read()

funcs = """
def edit_file(path: str, old_str: str, new_str: str) -> dict:
    from pathlib import Path
    target = resolve_workspace_path(path)
    if not target.exists():
        if old_str == "":
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(new_str, encoding="utf-8")
            return {"path": str(target), "action": "created_file"}
        else:
            return {"path": str(target), "action": "file not found"}
    original = target.read_text(encoding="utf-8")
    if original.find(old_str) == -1:
        return {"path": str(target), "action": "old_str not found"}
    edited = original.replace(old_str, new_str, 1)
    target.write_text(edited, encoding="utf-8")
    return {"path": str(target), "action": "edited"}

def search_web(query: str, max_results: int = 5) -> str:
    try:
        from duckduckgo_search import DDGS
        results = DDGS().text(query, max_results=max_results)
        import json
        return json.dumps(results, ensure_ascii=False, indent=2)
    except Exception as e:
        return f"Search failed: {e}"

def browse_webpage(url: str) -> str:
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page()
            page.goto(url, wait_until="networkidle", timeout=15000)
            content = page.evaluate("() => document.body.innerText")
            browser.close()
            return content[:15000]
    except Exception as e:
        return f"Browsing failed: {e}"

def analyze_image(image_path: str, prompt: str = "Describe this image in detail.") -> str:
    try:
        from server import get_client, OLLAMA_MODEL
        import base64
        target = resolve_workspace_path(image_path)
        if not target.exists(): return f"Image not found at: {image_path}"
        with open(target, "rb") as f:
            encoded = base64.b64encode(f.read()).decode("utf-8")
        ext = target.suffix.lower()
        mime = "image/jpeg"
        if ext in [".png"]: mime = "image/png"
        elif ext in [".webp"]: mime = "image/webp"
        elif ext in [".gif"]: mime = "image/gif"
        response = get_client().chat.completions.create(
            model=OLLAMA_MODEL,
            messages=[{"role": "user", "content": [{"type": "text", "text": prompt}, {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{encoded}"}}]}]
        )
        return response.choices[0].message.content
    except Exception as e:
        return f"Vision analysis failed: {e}"

DYNAMIC_TOOLS_DIR = resolve_workspace_path("tools")
DYNAMIC_TOOLS_DIR.mkdir(parents=True, exist_ok=True)

def load_dynamic_tools():
    import importlib.util, sys, json
    for py_file in DYNAMIC_TOOLS_DIR.glob("*.py"):
        tool_name = py_file.stem
        schema_file = py_file.with_suffix(".json")
        if not schema_file.exists(): continue
        try:
            schema = json.loads(schema_file.read_text(encoding="utf-8"))
            spec = importlib.util.spec_from_file_location(tool_name, py_file)
            module = importlib.util.module_from_spec(spec)
            sys.modules[tool_name] = module
            spec.loader.exec_module(module)
            func = getattr(module, tool_name)
            TOOLS[tool_name] = func
            global TOOL_SCHEMAS
            TOOL_SCHEMAS = [s for s in TOOL_SCHEMAS if s["function"]["name"] != tool_name]
            TOOL_SCHEMAS.append(schema)
        except Exception as e:
            print(f"Failed to load dynamic tool {tool_name}: {e}")

def create_dynamic_tool(name: str, code: str, schema_json: str) -> str:
    try:
        import json
        schema = json.loads(schema_json)
        if "function" not in schema or "name" not in schema["function"]:
            return "Error: Schema must contain 'function' and 'name' fields."
        py_file = DYNAMIC_TOOLS_DIR / f"{name}.py"
        schema_file = DYNAMIC_TOOLS_DIR / f"{name}.json"
        py_file.write_text(code, encoding="utf-8")
        schema_file.write_text(json.dumps(schema, indent=2), encoding="utf-8")
        load_dynamic_tools()
        return f"Tool '{name}' successfully created and hot-reloaded!"
    except Exception as e:
        return f"Failed to create tool: {e}"

"""

# Remove old TOOLS definition
tools_match = re.search(r'(?s)\nTOOLS = \{.*?\n\}', content)
if tools_match:
    content = content.replace(tools_match.group(0), '')

# Append new functions and new TOOLS definition at the end
content += "\n" + funcs + "\n"

content += """
TOOLS = {
    "read_file": read_file,
    "list_files": list_files,
    "write_file": write_file,
    "delete_file": delete_file,
    "fetch_url": fetch_url,
    "run_command": run_command,
    "run_skill": run_skill,
    "execute_python_script": execute_python_script,
    "edit_file": edit_file,
    "search_web": search_web,
    "browse_webpage": browse_webpage,
    "analyze_image": analyze_image,
    "create_dynamic_tool": create_dynamic_tool
}

load_dynamic_tools()
"""

with open('base.py', 'w', encoding='utf-8') as f:
    f.write(content)
