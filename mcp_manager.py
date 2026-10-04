import subprocess
import threading
import json
import queue
import time
import os
import uuid
from typing import Dict, Any, List, Optional

class MCPClient:
    def __init__(self, name: str, command: str, args: List[str], env: Optional[Dict[str, str]] = None):
        self.name = name
        self.command = command
        self.args = args
        
        # Merge environment variables
        self.env = os.environ.copy()
        if env:
            self.env.update(env)
            
        self.process = None
        self.running = False
        
        # Store pending requests waiting for a response
        self._pending_requests: Dict[str, queue.Queue] = {}
        
        # Cache for capabilities
        self.tools = []
        self.resources = []
        self.prompts = []

    def start(self):
        """Start the MCP server process and the reader thread."""
        self.process = subprocess.Popen(
            [self.command] + self.args,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=self.env,
            text=True,
            bufsize=1  # Line buffered
        )
        self.running = True
        
        # Start reader thread
        self.reader_thread = threading.Thread(target=self._read_stdout, daemon=True)
        self.reader_thread.start()
        
        # Start error reader thread (optional, for logging)
        self.stderr_thread = threading.Thread(target=self._read_stderr, daemon=True)
        self.stderr_thread.start()
        
        # Initialize connection
        self._initialize()

    def _initialize(self):
        """Send initialize request to the server."""
        response = self.send_request("initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {
                "roots": {"listChanged": True},
                "sampling": {}
            },
            "clientInfo": {
                "name": "pythonai-mcp-client",
                "version": "1.0.0"
            }
        })
        
        # Send initialized notification
        self.send_notification("notifications/initialized", {})
        
        # Fetch tools
        tools_resp = self.send_request("tools/list", {})
        if tools_resp and "tools" in tools_resp:
            self.tools = tools_resp["tools"]

    def _read_stdout(self):
        """Continuously read lines from the server's stdout."""
        while self.running and self.process and self.process.poll() is None:
            try:
                line = self.process.stdout.readline()
                if not line:
                    break
                line = line.strip()
                if line:
                    self._handle_message(json.loads(line))
            except Exception as e:
                print(f"[MCP {self.name}] stdout read error: {e}")
                break

    def _read_stderr(self):
        """Continuously read lines from the server's stderr for logging."""
        while self.running and self.process and self.process.poll() is None:
            try:
                line = self.process.stderr.readline()
                if not line:
                    break
                print(f"[MCP {self.name} LOG] {line.strip()}")
            except Exception:
                break

    def _handle_message(self, message: Dict[str, Any]):
        """Handle incoming JSON-RPC messages."""
        if "id" in message and ("result" in message or "error" in message):
            # This is a response to a request we sent
            msg_id = str(message["id"])
            if msg_id in self._pending_requests:
                self._pending_requests[msg_id].put(message)
        elif "method" in message:
            # This is a notification or request from the server
            method = message["method"]
            # Ignore for now in this simple client
            pass

    def send_request(self, method: str, params: Dict[str, Any], timeout: float = 30.0) -> Dict[str, Any]:
        """Send a JSON-RPC request and wait for the response."""
        msg_id = str(uuid.uuid4())
        request = {
            "jsonrpc": "2.0",
            "id": msg_id,
            "method": method,
            "params": params
        }
        
        q = queue.Queue()
        self._pending_requests[msg_id] = q
        
        try:
            req_str = json.dumps(request) + "\n"
            self.process.stdin.write(req_str)
            self.process.stdin.flush()
            
            # Wait for response
            response = q.get(timeout=timeout)
            if "error" in response:
                raise Exception(f"MCP Error: {response['error']}")
            return response.get("result", {})
        finally:
            if msg_id in self._pending_requests:
                del self._pending_requests[msg_id]

    def send_notification(self, method: str, params: Dict[str, Any]):
        """Send a JSON-RPC notification (no response expected)."""
        notification = {
            "jsonrpc": "2.0",
            "method": method,
            "params": params
        }
        notif_str = json.dumps(notification) + "\n"
        self.process.stdin.write(notif_str)
        self.process.stdin.flush()

    def call_tool(self, name: str, arguments: Dict[str, Any]) -> Any:
        """Call a specific tool exposed by the MCP server."""
        return self.send_request("tools/call", {
            "name": name,
            "arguments": arguments
        })

    def stop(self):
        """Stop the MCP server process gracefully."""
        self.running = False
        if self.process:
            self.process.terminate()
            try:
                self.process.wait(timeout=3.0)
            except subprocess.TimeoutExpired:
                self.process.kill()


class MCPManager:
    def __init__(self, config_path: str = "mcp_config.json"):
        self.config_path = config_path
        self.clients: Dict[str, MCPClient] = {}
        
    def load_config_and_start(self):
        """Load the config file and start all configured MCP servers."""
        if not os.path.exists(self.config_path):
            with open(self.config_path, "w", encoding="utf-8") as f:
                json.dump({"mcpServers": {}}, f, indent=4)
            return

        with open(self.config_path, "r", encoding="utf-8") as f:
            config = json.load(f)
            
        servers = config.get("mcpServers", {})
        for name, server_config in servers.items():
            command = server_config.get("command")
            args = server_config.get("args", [])
            env = server_config.get("env", {})
            
            client = MCPClient(name, command, args, env)
            print(f"Starting MCP Server: {name}")
            try:
                client.start()
                self.clients[name] = client
            except Exception as e:
                print(f"Failed to start MCP Server {name}: {e}")

    def get_all_tools(self) -> List[Dict[str, Any]]:
        """Get all tools from all registered MCP servers, formatted for OpenAI API."""
        tools = []
        for server_name, client in self.clients.items():
            for tool in client.tools:
                # Transform MCP tool format to OpenAI function calling format
                openai_tool = {
                    "type": "function",
                    "function": {
                        "name": f"mcp_{server_name}_{tool['name']}",
                        "description": tool.get("description", ""),
                        "parameters": tool.get("inputSchema", {})
                    }
                }
                tools.append(openai_tool)
        return tools

    def call_tool(self, tool_name: str, arguments: Dict[str, Any]) -> str:
        """Route the tool call to the appropriate MCP server."""
        if not tool_name.startswith("mcp_"):
            return f"Error: {tool_name} is not an MCP tool."
            
        parts = tool_name.split("_", 2)
        if len(parts) < 3:
            return "Error: Invalid MCP tool name format."
            
        server_name = parts[1]
        original_tool_name = parts[2]
        
        if server_name not in self.clients:
            return f"Error: MCP Server '{server_name}' not found."
            
        try:
            result = self.clients[server_name].call_tool(original_tool_name, arguments)
            
            # Format the output from MCP content blocks to a string
            if "content" in result and isinstance(result["content"], list):
                outputs = []
                for content in result["content"]:
                    if content.get("type") == "text":
                        outputs.append(content.get("text", ""))
                    else:
                        outputs.append(f"[{content.get('type')} content]")
                return "\n".join(outputs)
            
            return json.dumps(result)
        except Exception as e:
            return f"Error executing MCP tool: {str(e)}"

    def shutdown(self):
        """Shutdown all MCP servers."""
        for name, client in self.clients.items():
            print(f"Shutting down MCP Server: {name}")
            client.stop()

# Global manager instance
_mcp_manager = None

def get_mcp_manager() -> MCPManager:
    global _mcp_manager
    if _mcp_manager is None:
        _mcp_manager = MCPManager()
        _mcp_manager.load_config_and_start()
    return _mcp_manager

def get_mcp_tools():
    """Helper for base.py to inject tools."""
    manager = get_mcp_manager()
    return manager.get_all_tools()

def execute_mcp_tool(tool_name: str, arguments: Dict[str, Any]) -> str:
    """Helper to route execution."""
    manager = get_mcp_manager()
    return manager.call_tool(tool_name, arguments)
