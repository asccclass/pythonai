import os
from pathlib import Path
from base import workspace_root
import json

def project_scaffold(project_name: str, framework: str) -> str:
    """
    Scaffold a new project structure based on the requested framework.
    """
    try:
        project_dir = workspace_root() / project_name
        if project_dir.exists():
            return f"Error: Directory {project_dir} already exists."
            
        project_dir.mkdir(parents=True)
        
        scaffold_map = {
            "python_basic": _scaffold_python_basic,
            "fastapi": _scaffold_fastapi,
            "node_express": _scaffold_node_express,
            "react_vite": _scaffold_react_vite
        }
        
        if framework not in scaffold_map:
            return f"Error: Unknown framework '{framework}'."
            
        # Execute specific scaffolder
        files_created = scaffold_map[framework](project_dir, project_name)
        
        return f"Successfully scaffolded {framework} project '{project_name}' at {project_dir}.\n\nCreated files:\n- " + "\n- ".join(files_created)
        
    except Exception as e:
        return f"Scaffolding failed: {str(e)}"

def _write_file(project_dir: Path, rel_path: str, content: str) -> str:
    file_path = project_dir / rel_path
    file_path.parent.mkdir(parents=True, exist_ok=True)
    with open(file_path, "w", encoding="utf-8") as f:
        f.write(content)
    return rel_path

def _scaffold_python_basic(project_dir: Path, name: str) -> list[str]:
    files = []
    files.append(_write_file(project_dir, "README.md", f"# {name}\n\nA basic Python project.\n"))
    files.append(_write_file(project_dir, "requirements.txt", ""))
    files.append(_write_file(project_dir, "main.py", 'def main():\n    print("Hello World!")\n\nif __name__ == "__main__":\n    main()\n'))
    files.append(_write_file(project_dir, ".gitignore", "venv/\n__pycache__/\n*.pyc\n"))
    return files

def _scaffold_fastapi(project_dir: Path, name: str) -> list[str]:
    files = []
    files.append(_write_file(project_dir, "README.md", f"# {name}\n\nFastAPI microservice.\n"))
    files.append(_write_file(project_dir, "requirements.txt", "fastapi\nuvicorn\n"))
    files.append(_write_file(project_dir, "app/main.py", 'from fastapi import FastAPI\n\napp = FastAPI()\n\n@app.get("/")\ndef read_root():\n    return {"Hello": "World"}\n'))
    files.append(_write_file(project_dir, "app/__init__.py", ""))
    files.append(_write_file(project_dir, "tests/test_main.py", 'from fastapi.testclient import TestClient\nfrom app.main import app\n\nclient = TestClient(app)\n\ndef test_read_main():\n    response = client.get("/")\n    assert response.status_code == 200\n'))
    files.append(_write_file(project_dir, "tests/__init__.py", ""))
    files.append(_write_file(project_dir, ".gitignore", "venv/\n__pycache__/\n*.pyc\n.env\n"))
    return files

def _scaffold_node_express(project_dir: Path, name: str) -> list[str]:
    files = []
    files.append(_write_file(project_dir, "README.md", f"# {name}\n\nNode.js Express project.\n"))
    
    pkg_json = {
        "name": name,
        "version": "1.0.0",
        "description": "",
        "main": "src/index.js",
        "scripts": {
            "start": "node src/index.js",
            "dev": "nodemon src/index.js"
        },
        "dependencies": {
            "express": "^4.18.2"
        },
        "devDependencies": {
            "nodemon": "^3.0.1"
        }
    }
    files.append(_write_file(project_dir, "package.json", json.dumps(pkg_json, indent=2)))
    files.append(_write_file(project_dir, "src/index.js", "const express = require('express');\nconst app = express();\nconst port = process.env.PORT || 3000;\n\napp.get('/', (req, res) => {\n  res.send('Hello World!');\n});\n\napp.listen(port, () => {\n  console.log(`App listening on port ${port}`);\n});\n"))
    files.append(_write_file(project_dir, ".gitignore", "node_modules/\n.env\n"))
    return files

def _scaffold_react_vite(project_dir: Path, name: str) -> list[str]:
    files = []
    files.append(_write_file(project_dir, "README.md", f"# {name}\n\nReact project using Vite.\n"))
    
    pkg_json = {
        "name": name,
        "private": True,
        "version": "0.0.0",
        "type": "module",
        "scripts": {
            "dev": "vite",
            "build": "vite build",
            "preview": "vite preview"
        },
        "dependencies": {
            "react": "^18.2.0",
            "react-dom": "^18.2.0"
        },
        "devDependencies": {
            "@vitejs/plugin-react": "^4.2.1",
            "vite": "^5.0.0"
        }
    }
    files.append(_write_file(project_dir, "package.json", json.dumps(pkg_json, indent=2)))
    files.append(_write_file(project_dir, "vite.config.js", "import { defineConfig } from 'vite'\nimport react from '@vitejs/plugin-react'\n\nexport default defineConfig({\n  plugins: [react()],\n})\n"))
    files.append(_write_file(project_dir, "index.html", f'<!doctype html>\n<html lang="en">\n  <head>\n    <meta charset="UTF-8" />\n    <meta name="viewport" content="width=device-width, initial-scale=1.0" />\n    <title>{name}</title>\n  </head>\n  <body>\n    <div id="root"></div>\n    <script type="module" src="/src/main.jsx"></script>\n  </body>\n</html>\n'))
    files.append(_write_file(project_dir, "src/main.jsx", "import React from 'react'\nimport ReactDOM from 'react-dom/client'\nimport App from './App.jsx'\n\nReactDOM.createRoot(document.getElementById('root')).render(\n  <React.StrictMode>\n    <App />\n  </React.StrictMode>,\n)\n"))
    files.append(_write_file(project_dir, "src/App.jsx", "function App() {\n  return (\n    <div>\n      <h1>Hello React + Vite</h1>\n    </div>\n  )\n}\n\nexport default App\n"))
    files.append(_write_file(project_dir, ".gitignore", "node_modules/\ndist/\n.env\n"))
    return files
