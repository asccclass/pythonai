import os
import sqlite3
import json
from pathlib import Path

def init_db(db_path: str):
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS code_index (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            file_path TEXT NOT NULL,
            node_type TEXT NOT NULL,
            name TEXT NOT NULL,
            start_line INTEGER,
            end_line INTEGER,
            docstring TEXT,
            embedding TEXT,
            UNIQUE(file_path, name)
        )
    """)
    conn.commit()
    return conn

def extract_nodes_from_file(file_path: Path):
    try:
        import tree_sitter_python as tspython
        from tree_sitter import Language, Parser
    except ImportError:
        print("tree_sitter not installed")
        return []
        
    code_bytes = file_path.read_bytes()
    PY_LANGUAGE = Language(tspython.language())
    parser = Parser(PY_LANGUAGE)
    tree = parser.parse(code_bytes)
    
    nodes = []
    
    def traverse(node):
        if node.type in ['function_definition', 'class_definition']:
            name = ""
            docstring = ""
            # find identifier
            for child in node.children:
                if child.type == 'identifier':
                    name = child.text.decode('utf8')
                elif child.type == 'block':
                    # Check first statement for docstring (expression_statement -> string)
                    if len(child.children) > 0 and child.children[0].type == 'expression_statement':
                        expr = child.children[0]
                        if len(expr.children) > 0 and expr.children[0].type == 'string':
                            docstring = expr.children[0].text.decode('utf8').strip('\'\" \n')

            nodes.append({
                "type": node.type,
                "name": name,
                "start_line": node.start_point[0] + 1,
                "end_line": node.end_point[0] + 1,
                "docstring": docstring
            })
            
        for child in node.children:
            traverse(child)
            
    traverse(tree.root_node)
    return nodes

def build_index(workspace_dir: str):
    import sys
    sys.path.append(os.path.abspath(workspace_dir))
    
    from server import get_client, OLLAMA_EMBEDDING_MODEL
    from vector_search import OpenAICompatibleEmbeddingProvider
    
    provider = OpenAICompatibleEmbeddingProvider(get_client, OLLAMA_EMBEDDING_MODEL)
    
    db_path = Path(workspace_dir) / 'workspace' / 'codebase_index.db'
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = init_db(str(db_path))
    cursor = conn.cursor()
    
    root_path = Path(workspace_dir)
    count = 0
    for py_file in root_path.rglob("*.py"):
        if 'venv' in py_file.parts or '.git' in py_file.parts or '__pycache__' in py_file.parts:
            continue
            
        rel_path = str(py_file.relative_to(root_path)).replace("\\", "/")
        print(f"Indexing {rel_path}...")
        
        try:
            nodes = extract_nodes_from_file(py_file)
        except Exception as e:
            print(f"Failed parsing {rel_path}: {e}")
            continue
            
        for node in nodes:
            # Prepare text for embedding
            text_to_embed = f"File: {rel_path}\nType: {node['type']}\nName: {node['name']}\nDescription: {node['docstring']}"
            
            try:
                embedding = provider.embed(text_to_embed)
                emb_json = json.dumps(embedding)
            except Exception as e:
                print(f"Embedding failed for {node['name']}: {e}")
                emb_json = "[]"
                
            cursor.execute("""
                INSERT INTO code_index (file_path, node_type, name, start_line, end_line, docstring, embedding)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(file_path, name) DO UPDATE SET
                    start_line=excluded.start_line,
                    end_line=excluded.end_line,
                    docstring=excluded.docstring,
                    embedding=excluded.embedding
            """, (rel_path, node['type'], node['name'], node['start_line'], node['end_line'], node['docstring'], emb_json))
            count += 1
            
    conn.commit()
    conn.close()
    print(f"Indexed {count} functions/classes successfully.")

if __name__ == "__main__":
    build_index(".")
