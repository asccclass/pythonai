import json
from contextlib import closing
from agent_execution import policy_tool,resolve_path
from agent_execution.code_index import CodeIndex
from agent_execution.code_index.navigation import lsp

@policy_tool('code_symbols')
def code_symbols(file_path: str='',backend: str='syntax',limit: int=100) -> str:
    if type(limit) is not int or not 1<=limit<=1000 or backend not in {'syntax','lsp'}:raise ValueError('Invalid symbols options')
    index=CodeIndex();index.refresh(embed=False)
    if backend=='lsp':
        if not file_path:raise ValueError('LSP document symbols require a file')
        value=lsp(index,resolve_path(file_path),'textDocument/documentSymbol');value['results']=value['results'][:limit]
    else:
        with closing(index.connect()) as conn:
            conn.execute('BEGIN')
            snapshot={row['path']:row['hash'] for row in conn.execute('SELECT path,hash FROM ci_files')}
            query='SELECT path AS file_path,name,qualified,kind,line,col AS column,end_line FROM ci_symbols';args=[]
            if file_path:query+=' WHERE path=?';args.append(resolve_path(file_path).relative_to(index.root).as_posix())
            rows=conn.execute(query+' ORDER BY path,line LIMIT ?',[*args,limit]).fetchall()
            value={'backend':'tree_sitter','results':[dict(row) for row in rows],'index':index.stats}
            if {name:data[0] for name,data in index.inventory().items()}!=snapshot:raise ValueError('Workspace changed during symbol query; retry')
    return json.dumps(value,ensure_ascii=False)
