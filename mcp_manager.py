import subprocess
import threading
import json
import queue
import time
import os
import uuid
import hashlib
import re
from concurrent.futures import ThreadPoolExecutor
from agent_execution.extensions.schema import validator,validate
from agent_execution.extensions.remote import RemoteMCPClient
from typing import Dict, Any, List, Optional

class MCPClient:
    def __init__(
        self,
        name: str,
        command: str,
        args: List[str],
        env: Optional[Dict[str, str]] = None,
        cwd: Optional[str] = None,
    ):
        self.name = name
        self.command = command
        self.args = args
        self.cwd = cwd
        
        # Merge environment variables
        self.env = os.environ.copy()
        if env:
            self.env.update(env)
            
        self.process = None
        self.running = False
        self.write_lock=threading.Lock()
        self.request_lock=threading.Lock()
        self.job=None
        
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
            cwd=self.cwd,
            text=True,
            encoding='utf-8',errors='strict',
            start_new_session=os.name!='nt',
            creationflags=(subprocess.CREATE_NO_WINDOW|0x4) if os.name=='nt' else 0,
            bufsize=1  # Line buffered
        )
        if os.name=='nt':
            from agent_execution.process import WindowsJob
            self.job=WindowsJob(self.process);self.job.resume(self.process)
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
            "capabilities": {},
            "clientInfo": {
                "name": "pythonai-mcp-client",
                "version": "1.0.0"
            }
        })
        
        # Send initialized notification
        self.send_notification("notifications/initialized", {})
        
        # Fetch tools
        self.tools=[];cursor=None
        for _ in range(10):
            tools_resp=self.send_request('tools/list',{'cursor':cursor} if cursor else {})
            self.tools.extend(tools_resp.get('tools',[]))
            if len(self.tools)>256:raise ValueError('MCP tool capacity exceeded')
            cursor=tools_resp.get('nextCursor')
            if not cursor:break
        else:raise ValueError('MCP pagination limit')

    def _read_stdout(self):
        """Continuously read lines from the server's stdout."""
        while self.running and self.process and self.process.poll() is None:
            try:
                line = self.process.stdout.readline(1024*1024+1)
                if not line:
                    break
                if len(line)>1024*1024:raise ValueError('MCP line exceeds limit')
                line = line.strip()
                if line:
                    self._handle_message(json.loads(line))
            except Exception as e:
                break
        self.running=False
        with self.request_lock:
            for pending in self._pending_requests.values():
                try:pending.put_nowait({'error':{'message':'MCP disconnected'}})
                except queue.Full:pass

    def _read_stderr(self):
        """Continuously read lines from the server's stderr for logging."""
        while self.running and self.process and self.process.poll() is None:
            try:
                line = self.process.stderr.readline(65536)
                if not line:
                    break
                # Third-party diagnostics may include credentials; drain without logging.
            except Exception:
                break

    def _handle_message(self, message: Dict[str, Any]):
        """Handle incoming JSON-RPC messages."""
        if not isinstance(message,dict) or message.get('jsonrpc')!='2.0':raise ValueError('Invalid MCP message')
        if "id" in message and ("result" in message or "error" in message):
            # This is a response to a request we sent
            msg_id = str(message["id"])
            with self.request_lock:
                pending=self._pending_requests.get(msg_id)
                if pending:
                    try:pending.put_nowait(message)
                    except queue.Full:pass
        elif "method" in message:
            # This is a notification or request from the server
            method = message["method"]
            # Ignore for now in this simple client
            pass

    def send_request(self, method: str, params: Dict[str, Any], timeout: float = 10.0) -> Dict[str, Any]:
        """Send a JSON-RPC request and wait for the response."""
        msg_id = str(uuid.uuid4())
        request = {
            "jsonrpc": "2.0",
            "id": msg_id,
            "method": method,
            "params": params
        }
        
        if not self.running or not self.process:raise ConnectionError('MCP is not running')
        if not 0<timeout<=30:raise ValueError('MCP timeout exceeds limit')
        q = queue.Queue(maxsize=1)
        with self.request_lock:
            if len(self._pending_requests)>=32:raise RuntimeError('MCP concurrency limit')
            self._pending_requests[msg_id] = q
        
        try:
            req_str = json.dumps(request) + "\n"
            if len(req_str.encode())>1024*1024:raise ValueError('MCP request exceeds limit')
            self._write(req_str,timeout)
            
            # Wait for response
            from agent_execution.policy import current_context
            deadline=time.monotonic()+timeout
            while True:
                cancelled=current_context().check_cancelled
                if cancelled and cancelled():raise InterruptedError('MCP request cancelled')
                remaining=deadline-time.monotonic()
                if remaining<=0:raise TimeoutError('MCP request timed out; not replayed')
                try:response=q.get(timeout=min(.1,remaining));break
                except queue.Empty:pass
            if "error" in response:
                raise RuntimeError('MCP request failed')
            return response.get("result", {})
        finally:
            with self.request_lock:self._pending_requests.pop(msg_id,None)

    def send_notification(self, method: str, params: Dict[str, Any]):
        """Send a JSON-RPC notification (no response expected)."""
        notification = {
            "jsonrpc": "2.0",
            "method": method,
            "params": params
        }
        notif_str = json.dumps(notification) + "\n"
        self._write(notif_str,10)

    def _write(self,data,timeout):
        """A non-reading server must not block the host on a full pipe."""
        done=threading.Event();failures=[]
        def write():
            try:
                if not self.write_lock.acquire(timeout=timeout):raise TimeoutError('MCP writer busy')
                try:self.process.stdin.write(data);self.process.stdin.flush()
                finally:self.write_lock.release()
            except Exception as error:failures.append(error)
            finally:done.set()
        thread=threading.Thread(target=write,daemon=True);thread.start()
        if not done.wait(timeout):
            self.stop();thread.join(3)
            raise TimeoutError('MCP blocked pipe; outcome is uncertain')
        if failures:raise failures[0]

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
            self.process.wait(timeout=3)
            if self.job:self.job.close();self.job=None
            if os.name!='nt':
                from agent_execution.process import terminate_tree
                terminate_tree(self.process)
            for stream in (self.process.stdin,self.process.stdout,self.process.stderr):
                if stream:stream.close()


class MCPManager:
    def __init__(self, config_path: str = "mcp_config.json"):
        self.config_path = config_path
        self.clients: Dict[str, MCPClient] = {}
        self.skipped_servers: Dict[str, str] = {}
        self.routes={}
        self.server_configs={}
        
    def load_config_and_start(self):
        """Load the config file and start all configured MCP servers."""
        if not os.path.exists(self.config_path):
            with open(self.config_path, "w", encoding="utf-8") as f:
                json.dump({"mcpServers": {}}, f, indent=4)
            return

        with open(self.config_path, "r", encoding="utf-8") as f:
            if os.fstat(f.fileno()).st_size>1024*1024:raise ValueError('MCP config exceeds 1 MiB')
            config = json.load(f)
            
        servers = config.get("mcpServers", {})
        if not isinstance(servers,dict) or len(servers)>32:raise ValueError('MCP server capacity exceeded')
        self.server_configs=servers
        def start_server(item):
            name,server_config=item
            client=None
            try:
                if not isinstance(server_config,dict):raise ValueError('Invalid MCP server config')
                command=server_config.get('command');args=server_config.get('args',[])
                env=server_config.get('env',{});cwd=server_config.get('cwd')
                client = RemoteMCPClient(name,server_config) if server_config.get('url') else MCPClient(name, command, args, env, cwd)
                client.start()
                self.clients[name] = client
            except Exception as e:
                self.skipped_servers[name] = str(e) if isinstance(e,FileNotFoundError) else type(e).__name__
                try:
                    if client:client.stop()
                except Exception:
                    pass
        with ThreadPoolExecutor(max_workers=4) as executor:
            list(executor.map(start_server,servers.items()))

    def get_all_tools(self) -> List[Dict[str, Any]]:
        """Get all tools from all registered MCP servers, formatted for OpenAI API."""
        tools = [];routes={};candidates=[]
        for server_name, client in self.clients.items():
            for tool in client.tools:
                try:
                    if not isinstance(tool.get('name'),str) or not 1<=len(tool['name'])<=128:raise ValueError('Invalid tool name')
                    validator(tool.get('inputSchema',{}))
                    if 'outputSchema' in tool:validator(tool['outputSchema'])
                    if len(tool.get('description',''))>8192:raise ValueError('Description limit')
                    candidates.append((server_name,client,tool))
                except Exception:continue
        counts={}
        for server,client,tool in candidates:
            raw=f"mcp_{server}_{tool['name']}";counts[raw]=counts.get(raw,0)+1
        for server_name,client,tool in candidates:
                raw=f"mcp_{server_name}_{tool['name']}"
                name=raw if counts[raw]==1 and len(raw)<=64 and re.fullmatch('[a-zA-Z0-9_-]+',raw) else 'mcp_'+hashlib.sha256(json.dumps([server_name,tool['name']]).encode()).hexdigest()[:40]
                if name in routes:raise ValueError('Duplicate MCP tool advertisement')
                routes[name]=(server_name,tool)
                # Transform MCP tool format to OpenAI function calling format
                openai_tool = {
                    "type": "function",
                    "function": {
                        "name": name,
                        "description": tool.get("description", ""),
                        "parameters": tool.get("inputSchema", {})
                    }
                }
                tools.append(openai_tool)
        self.routes=routes
        return tools

    def call_tool(self, tool_name: str, arguments: Dict[str, Any]) -> str:
        """Route the tool call to the appropriate MCP server."""
        if tool_name not in self.routes:self.get_all_tools()
        route=self.routes.get(tool_name)
        if not route:return json.dumps({'success':False,'error':'Unknown MCP route'})
        server_name,tool=route
            
        try:
            validate(arguments,tool.get('inputSchema',{}))
            result = self.clients[server_name].call_tool(tool['name'], arguments)
            validate(result,{'type':'object','required':['content'],'properties':{'content':{'type':'array','maxItems':256,'items':{'type':'object','required':['type'],'properties':{'type':{'enum':['text','image','audio','resource','resource_link']}}}},'isError':{'type':'boolean'}}})
            if 'outputSchema' in tool:
                if 'structuredContent' not in result:raise ValueError('MCP structuredContent is required')
                validate(result['structuredContent'],tool['outputSchema'])
            if result.get('isError'):return json.dumps({'success':False,'error':'MCP tool reported failure'})
            
            # Format the output from MCP content blocks to a string
            if "content" in result and isinstance(result["content"], list):
                outputs = []
                for content in result["content"]:
                    if content.get("type") == "text":
                        outputs.append(content.get("text", ""))
                    else:
                        outputs.append(f"[{content.get('type')} content]")
                return json.dumps({'success':True,'output':'\n'.join(outputs),'structuredContent':result.get('structuredContent')},ensure_ascii=False)
            
            return json.dumps({'success':True,'output':result})
        except Exception as e:
            return json.dumps({'success':False,'error':type(e).__name__})

    def health(self):
        return {name:client.health() if isinstance(client,RemoteMCPClient) else {'ready':bool(client.running and client.process and client.process.poll() is None)} for name,client in self.clients.items()}

    def reconnect(self,name):
        # Discovery may reconnect, but an uncertain tools/call is never repeated.
        config=self.server_configs[name]
        if name in self.clients:self.clients.pop(name).stop()
        client=RemoteMCPClient(name,config) if config.get('url') else MCPClient(name,config.get('command'),config.get('args',[]),config.get('env',{}),config.get('cwd'))
        try:client.start();self.clients[name]=client;self.skipped_servers.pop(name,None);self.get_all_tools()
        except Exception as error:client.stop();self.skipped_servers[name]=type(error).__name__;raise

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
    from agent_execution import execute_tool
    return execute_tool(tool_name, arguments, lambda **values: get_mcp_manager().call_tool(tool_name, values))
