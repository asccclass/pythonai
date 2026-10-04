import os
import json
import sqlite3
from pathlib import Path
import math

def cosine_similarity(first: list[float], second: list[float]) -> float:
    if not first or not second or len(first) != len(second):
        return 0.0
    numerator = sum(left * right for left, right in zip(first, second))
    first_norm = math.sqrt(sum(value * value for value in first))
    second_norm = math.sqrt(sum(value * value for value in second))
    if first_norm == 0.0 or second_norm == 0.0:
        return 0.0
    return numerator / (first_norm * second_norm)

def search_codebase(query: str, limit: int = 5) -> str:
    workspace_dir = Path(os.getcwd())
    db_path = workspace_dir / 'workspace' / 'codebase_index.db'
    
    if not db_path.exists():
        return "Error: Codebase index does not exist. Run build_codebase_index.py first."
        
    try:
        import sys
        sys.path.append(os.path.abspath(workspace_dir))
        from server import get_client, OLLAMA_EMBEDDING_MODEL
        from vector_search import OpenAICompatibleEmbeddingProvider
        
        provider = OpenAICompatibleEmbeddingProvider(get_client, OLLAMA_EMBEDDING_MODEL)
        query_vector = provider.embed(query)
    except Exception as e:
        return f"Error embedding query: {e}"
        
    conn = sqlite3.connect(str(db_path))
    cursor = conn.cursor()
    cursor.execute("SELECT file_path, node_type, name, start_line, end_line, docstring, embedding FROM code_index")
    rows = cursor.fetchall()
    conn.close()
    
    results = []
    for row in rows:
        file_path, node_type, name, start_line, end_line, docstring, emb_str = row
        try:
            emb = json.loads(emb_str)
        except:
            continue
            
        if not emb:
            continue
            
        score = cosine_similarity(query_vector, emb)
        results.append({
            'score': score,
            'file_path': file_path,
            'node_type': node_type,
            'name': name,
            'start_line': start_line,
            'end_line': end_line,
            'docstring': docstring
        })
        
    results.sort(key=lambda x: x['score'], reverse=True)
    top_results = results[:limit]
    
    output = []
    for i, res in enumerate(top_results):
        score_percent = res['score'] * 100
        output.append(f"[{i+1}] Score: {score_percent:.1f}%\n"
                      f"File: {res['file_path']} (Lines {res['start_line']}-{res['end_line']})\n"
                      f"Type: {res['node_type']}, Name: {res['name']}\n"
                      f"Docstring: {res['docstring']}\n")
                      
    if not output:
        return "No relevant codebase nodes found."
        
    return "\n".join(output)
