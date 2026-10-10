"""Compatibility entry point for the packaged incremental index."""
import json
from agent_execution.code_index import CodeIndex
from agent_execution.code_index.syntax import extract
from agent_execution import resolve_path

def extract_nodes_from_file(file_path):
    return extract(str(file_path),resolve_path(file_path).read_bytes())[0]

def build_index(workspace_dir='.'):
    index=CodeIndex()
    if resolve_path(workspace_dir)!=index.root:raise ValueError('Index the configured workspace root')
    result=index.refresh();print(json.dumps(result,ensure_ascii=False));return result

if __name__=='__main__':build_index()
