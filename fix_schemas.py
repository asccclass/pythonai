import json

with open('base.py', 'r', encoding='utf-8') as f:
    content = f.read()

# find TOOL_SCHEMAS = [
schemas_start = content.find('TOOL_SCHEMAS = [')
if schemas_start != -1:
    end_bracket = content.find(']', schemas_start)
    
    new_schemas = """    },
    {
        "type": "function",
        "function": {
                "name": "edit_file",
                "description": "Edit an existing file by replacing a specific string. Use this carefully.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": "The workspace-relative path to the file"
                        },
                        "old_str": {
                            "type": "string",
                            "description": "The exact string to be replaced"
                        },
                        "new_str": {
                            "type": "string",
                            "description": "The string to replace it with"
                        }
                    },
                    "required": ["path", "old_str", "new_str"]
                }
        }
    },
    {
        "type": "function",
        "function": {
                "name": "search_web",
                "description": "Search the web using DuckDuckGo.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "The search query"
                        },
                        "max_results": {
                            "type": "integer",
                            "description": "Maximum number of results to return (default: 5)"
                        }
                    },
                    "required": ["query"]
                }
        }
    },
    {
        "type": "function",
        "function": {
                "name": "browse_webpage",
                "description": "Browse a webpage and return its text content.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "url": {
                            "type": "string",
                            "description": "The URL of the webpage to browse"
                        }
                    },
                    "required": ["url"]
                }
        }
    },
    {
        "type": "function",
        "function": {
                "name": "analyze_image",
                "description": "Analyze an image using the vision model and return a description. Provide the workspace-relative path to the image.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "image_path": {
                            "type": "string",
                            "description": "The workspace-relative path to the image file"
                        },
                        "prompt": {
                            "type": "string",
                            "description": "Optional prompt or question about the image (default: 'Describe this image in detail.')"
                        }
                    },
                    "required": ["image_path"]
                }
        }
    },
    {
        "type": "function",
        "function": {
                "name": "create_dynamic_tool",
                "description": "Create a new Python tool dynamically. The tool will be saved and hot-reloaded into the environment immediately.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "name": {
                            "type": "string",
                            "description": "The name of the tool (must be a valid Python identifier)"
                        },
                        "code": {
                            "type": "string",
                            "description": "The complete Python code for the tool."
                        },
                        "schema_json": {
                            "type": "string",
                            "description": "The JSON string representation of the tool's schema, strictly adhering to the OpenAI tool calling format."
                        }
                    },
                    "required": ["name", "code", "schema_json"]
                }
        }
"""
    
    content = content[:end_bracket] + new_schemas + "\n" + content[end_bracket:]

with open('base.py', 'w', encoding='utf-8') as f:
    f.write(content)
