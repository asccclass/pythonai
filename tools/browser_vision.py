import json
import base64
import time
import traceback
from pathlib import Path
from server import get_client, OLLAMA_MODEL
from base import resolve_workspace_path

def execute_playwright_actions(page, actions):
    """Executes a list of generic actions on the page."""
    for action in actions:
        act_type = action.get("action")
        selector = action.get("selector")
        
        try:
            if act_type == "click":
                page.click(selector, timeout=5000)
            elif act_type == "type":
                page.fill(selector, action.get("text", ""), timeout=5000)
            elif act_type == "wait":
                time.sleep(action.get("time", 1))
            elif act_type == "scroll":
                page.evaluate("window.scrollBy(0, window.innerHeight)")
            elif act_type == "wait_for":
                page.wait_for_selector(selector, timeout=5000)
        except Exception as e:
            print(f"[BrowserVision] Action {act_type} on {selector} failed: {e}")

def run_browser_vision(url: str, actions_json: str = "[]", vision_prompt: str = "Describe this page in detail.") -> str:
    """
    Opens a URL, performs optional UI actions (click, type), and sends a screenshot to the Vision LLM.
    If the LLM doesn't support vision, it falls back to extracting the DOM text.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return "Error: Playwright is not installed. Run 'pip install playwright' and 'playwright install chromium'."
        
    try:
        actions = json.loads(actions_json)
    except Exception:
        actions = []
        
    screenshot_bytes = None
    dom_text = ""
    
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page()
            
            # Go to URL
            page.goto(url, wait_until="domcontentloaded", timeout=15000)
            
            # Execute interactive steps
            if actions:
                execute_playwright_actions(page, actions)
                
            # Wait a bit for animations
            time.sleep(1)
            
            # Capture DOM text as fallback
            dom_text = page.evaluate("document.body.innerText")
            
            # Capture Screenshot
            screenshot_bytes = page.screenshot(type="jpeg", quality=80)
            browser.close()
            
    except Exception as e:
        return f"Browser automation failed: {e}"

    if not screenshot_bytes:
        return "Failed to capture screenshot."
        
    # Convert screenshot to Base64
    base64_image = base64.b64encode(screenshot_bytes).decode('utf-8')
    image_url = f"data:image/jpeg;base64,{base64_image}"
    
    # Save a local copy for debugging
    scratch_dir = resolve_workspace_path("temp")
    scratch_dir.mkdir(exist_ok=True)
    (scratch_dir / "last_screenshot.jpg").write_bytes(screenshot_bytes)
    
    # Call VLM
    try:
        client = get_client()
        response = client.chat.completions.create(
            model=OLLAMA_MODEL,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": vision_prompt},
                        {
                            "type": "image_url",
                            "image_url": {"url": image_url}
                        }
                    ]
                }
            ],
            max_tokens=1000
        )
        return f"[Vision Analysis Result]:\n{response.choices[0].message.content}"
        
    except Exception as e:
        # Fallback if the LLM does not support vision (e.g. raises BadRequestError or TypeError)
        error_str = str(e).lower()
        print(f"[BrowserVision] VLM Error caught: {e}. Falling back to text-only mode.")
        
        fallback_prompt = (
            f"The Vision API failed because the current model '{OLLAMA_MODEL}' likely does not support images.\n"
            f"Please analyze the following extracted DOM text instead, based on this prompt: '{vision_prompt}'\n\n"
            f"--- DOM TEXT ---\n{dom_text[:4000]}\n--- END DOM TEXT ---"
        )
        
        try:
            fallback_response = client.chat.completions.create(
                model=OLLAMA_MODEL,
                messages=[{"role": "user", "content": fallback_prompt}],
                max_tokens=1000
            )
            return f"[Text-Fallback Analysis Result (Vision Disabled)]:\n{fallback_response.choices[0].message.content}"
        except Exception as fallback_e:
            return f"Fatal Error during LLM fallback analysis: {fallback_e}\nDOM Extract: {dom_text[:500]}"
