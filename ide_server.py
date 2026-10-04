import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

# Import your existing LLM client
from server import get_client, OLLAMA_MODEL

class IDERequestHandler(BaseHTTPRequestHandler):
    def _send_cors_headers(self):
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')

    def do_OPTIONS(self):
        self.send_response(200)
        self._send_cors_headers()
        self.end_headers()

    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path == "/api/inline_chat":
            self.handle_inline_chat()
        elif parsed.path == "/api/autocomplete":
            self.handle_autocomplete()
        else:
            self.send_error(404, "Not Found")

    def handle_autocomplete(self):
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length)
        
        try:
            req = json.loads(body.decode("utf-8"))
        except json.JSONDecodeError:
            self.send_error(400, "Invalid JSON")
            return

        prefix = req.get("prefix", "")
        suffix = req.get("suffix", "")
        
        # Build a completion prompt
        # If the model supports FIM (Fill-in-the-Middle) like CodeLlama or DeepSeek:
        # prompt = f"<｜fim begin｜>{prefix}<｜fim hole｜>{suffix}<｜fim end｜>"
        # For a generic model, we just use the prefix.
        # We limit context to last 2000 chars to keep it fast.
        prompt = prefix[-2000:]
        
        self.send_response(200)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self._send_cors_headers()
        self.end_headers()
        
        try:
            # Use raw completions endpoint for code completion
            response = get_client().completions.create(
                model=OLLAMA_MODEL,
                prompt=prompt,
                max_tokens=60,
                temperature=0.2,
                stop=["\n\n", "def ", "class "]
            )
            completion_text = response.choices[0].text
            result = {"completion": completion_text}
            self.wfile.write(json.dumps(result).encode("utf-8"))
        except Exception as e:
            self.wfile.write(json.dumps({"error": str(e)}).encode("utf-8"))

    def handle_inline_chat(self):
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length)
        
        try:
            req = json.loads(body.decode("utf-8"))
        except json.JSONDecodeError:
            self.send_error(400, "Invalid JSON")
            return

        file_path = req.get("file_path", "Unknown")
        selected_text = req.get("selected_text", "")
        prompt = req.get("prompt", "")
        start_line = req.get("start_line", 0)
        end_line = req.get("end_line", 0)
        
        # Optionally, read surrounding context if file exists
        surrounding_context = "Not available."
        if os.path.exists(file_path):
            try:
                with open(file_path, "r", encoding="utf-8") as f:
                    lines = f.readlines()
                    start_idx = max(0, start_line - 30)
                    end_idx = min(len(lines), end_line + 30)
                    surrounding_context = "".join(lines[start_idx:end_idx])
            except Exception:
                pass

        system_prompt = (
            "You are an expert developer helping with an inline chat request in an IDE. "
            "Your task is to refactor, explain, or rewrite the selected code based on the user's instructions.\n\n"
            f"File: {file_path}\n"
            f"Surrounding Context:\n```\n{surrounding_context}\n```\n\n"
            "CRITICAL RULES:\n"
            "1. Output ONLY the replacement code. Do not wrap it in markdown block quotes (e.g. ```python).\n"
            "2. Do not include any explanations, pleasantries, or introductory text.\n"
            "3. Your output will be directly injected into the editor to replace the user's selection."
        )

        user_message = f"Selected Code (lines {start_line}-{end_line}):\n{selected_text}\n\nUser Prompt: {prompt}"

        # We'll use streaming to give real-time feedback (Ghost Text style)
        self.send_response(200)
        self.send_header('Content-Type', 'text/plain; charset=utf-8')
        self._send_cors_headers()
        self.end_headers()

        try:
            response = get_client().chat.completions.create(
                model=OLLAMA_MODEL,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_message}
                ],
                stream=True
            )
            
            for chunk in response:
                if chunk.choices[0].delta.content is not None:
                    chunk_text = chunk.choices[0].delta.content
                    self.wfile.write(chunk_text.encode("utf-8"))
                    self.wfile.flush()
        except Exception as e:
            self.wfile.write(f"\n[Inline Chat Error: {e}]".encode("utf-8"))


def run_ide_server(host="127.0.0.1", port=11435):
    server = ThreadingHTTPServer((host, port), IDERequestHandler)
    print(f"IDE Backend Server running on http://{host}:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nIDE Backend Server shutting down.")
    finally:
        server.server_close()

if __name__ == "__main__":
    run_ide_server()
