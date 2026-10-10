"""Tree-sitter declarations and syntactic identifier occurrences, not type inference."""
from importlib import import_module
from pathlib import Path
from bisect import bisect_right

LANGUAGES={'.py':'python','.go':'go','.js':'javascript','.jsx':'javascript','.ts':'typescript','.tsx':'tsx'}
DECLARATIONS={'function_definition','class_definition','function_declaration','method_declaration',
              'method_definition','class_declaration','interface_declaration','type_alias_declaration','type_spec','variable_declarator'}
IDENTIFIERS={'identifier','type_identifier','field_identifier','property_identifier'}

def extract(path,content):
    from tree_sitter import Language,Parser
    language=LANGUAGES.get(Path(path).suffix.lower())
    if not language:return [],[]
    module=import_module('tree_sitter_'+('typescript' if language=='tsx' else language))
    capsule=getattr(module,'language_'+('tsx' if language=='tsx' else 'typescript'))() if language in {'tsx','typescript'} else module.language()
    grammar=Language(capsule);parser=Parser(grammar);tree=parser.parse(content);root=tree.root_node
    if root.has_error:raise ValueError('Source has syntax errors')
    symbols=[];occurrences=[];declared=set();stack=[(root,())]
    starts=[0]+[position+1 for position,byte in enumerate(content) if byte==10]
    def point(byte):
        row=bisect_right(starts,byte)-1
        return row+1,len(content[starts[row]:byte].decode('utf-8'))
    def text(node):return content[node.start_byte:node.end_byte].decode('utf-8')
    while stack:
        node,scope=stack.pop();inner=scope
        if node.type in DECLARATIONS:
            name=node.child_by_field_name('name')
            if name and name.type in IDENTIFIERS:
                label=text(name);prefix=scope
                receiver=node.child_by_field_name('receiver') if node.type=='method_declaration' else None
                if receiver and receiver.named_children:
                    receiver_type=receiver.named_children[0].child_by_field_name('type')
                    if receiver_type:prefix=(*scope,text(receiver_type).lstrip('*'))
                qualified='.'.join((*prefix,label));line,column=point(name.start_byte)
                symbols.append({'name':label,'qualified_name':qualified,'node_type':node.type,
                    'start_line':point(node.start_byte)[0],'end_line':point(node.end_byte)[0],
                    'line':line,'column':column,
                    'byte':name.start_byte,'text':text(node)[:4096]})
                declared.add(name.start_byte);inner=(*scope,label)
        if node.type in IDENTIFIERS:
            line,column=point(node.start_byte)
            occurrences.append({'name':text(node),'line':line,
                'column':column,
                'byte':node.start_byte,'scope':'.'.join(scope)})
        stack.extend((child,inner) for child in reversed(node.named_children))
        if len(symbols)+len(occurrences)>20000:raise ValueError('Source exceeds 20000 syntax records')
    for item in occurrences:item['declaration']=item['byte'] in declared
    return symbols,occurrences
