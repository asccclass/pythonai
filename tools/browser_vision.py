from agent_execution import policy_tool
from agent_execution.media.browser import browse
from agent_execution.results import ToolResult
import json
import base64
from pathlib import Path

@policy_tool("browser_vision")
def browser_vision(url: str, actions_json: str = "[]", vision_prompt: str = "Describe this page in detail.") -> str:
    """
    Opens a URL, performs optional UI actions (click, type), and sends a screenshot to the Vision LLM.
    If the LLM doesn't support vision, it falls back to extracting the DOM text.
    """
    try:
        actions = json.loads(actions_json)
    except (TypeError, ValueError, json.JSONDecodeError):
        return ToolResult(False, error='Invalid browser actions JSON')
    if not isinstance(actions, list):
        return ToolResult(False, error='Browser actions must be a JSON array')
    try:
        observed = browse(url, actions, screenshot=True)
        screenshot = next((Path(path) for path in observed['artifacts'] if Path(path).suffix.lower() in {'.jpg', '.jpeg'}), None)
        if screenshot is None:
            return ToolResult(False, error='Browser did not produce a screenshot artifact')
        screenshot_bytes = screenshot.read_bytes()
        dom_text = observed['text']
    except (OSError, RuntimeError, ValueError, PermissionError) as error:
        return ToolResult(False, error=f'Browser automation failed ({type(error).__name__}): {str(error)[:512]}')
        
    # Convert screenshot to Base64
    base64_image = base64.b64encode(screenshot_bytes).decode('utf-8')
    image_url = f"data:image/jpeg;base64,{base64_image}"
    
    # Call VLM
    try:
        from server import get_client, OLLAMA_MODEL
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
        from agent_execution.budgets import BudgetExhausted
        if isinstance(e,BudgetExhausted):raise
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
