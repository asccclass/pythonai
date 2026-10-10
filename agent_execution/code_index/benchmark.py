"""Reproducible offline quality/latency baseline: python -m ...benchmark."""
from contextlib import closing
import argparse
import json
import os
from pathlib import Path
import shutil
import statistics
import tempfile
import time

from agent_execution import execution_scope
from .index import CodeIndex

def run_benchmark(symbols=1000,queries=10):
    if type(symbols) is not int or not 300<=symbols<=5000 or type(queries) is not int or not 1<=queries<=50:raise ValueError('Benchmark requires 300..5000 symbols / 1..50 queries')
    config=os.environ.get('AGENT_CODE_EMBEDDING_CONFIG');os.environ['AGENT_CODE_EMBEDDING_CONFIG']='{"kind":"hashing","dimensions":64,"revision":"1"}'
    try:
        with tempfile.TemporaryDirectory(prefix='pythonai-index-benchmark-') as directory,execution_scope(workspace=directory):
            root=Path(directory);fixtures=Path(__file__).parent/'fixtures'
            for name in ('python_sample.py','go_sample.go','js_sample.js','ts_sample.ts'):shutil.copyfile(fixtures/name,root/name)
            (root/'synthetic.py').write_text('\n'.join(f'def feature_{number}(value): return value + {number}' for number in range(symbols)),encoding='utf-8')
            index=CodeIndex();build=index.refresh();warm=index.refresh();latencies=[];recalls=[];candidates=[]
            with closing(index.connect()) as conn:rows=[dict(row) for row in conn.execute('SELECT * FROM ci_symbols WHERE vector IS NOT NULL')]
            samples=[row for row in rows if row['path']=='synthetic.py'][::max(1,symbols//queries)][:queries]
            for sample in samples:
                vector=json.loads(sample['vector'])
                truth=sorted(rows,key=lambda row:(-sum(a*b for a,b in zip(vector,json.loads(row['vector']))),row['path'],row['line']))[:5]
                expected={(row['path'],row['line']) for row in truth}
                found=index.search(sample['chunk'],5)
                actual={(row['file_path'],row['line']) for row in found['results']}
                recalls.append(len(actual&expected)/5);latencies.append(found['elapsed_ms']);candidates.append(found['candidate_count'])
            names={'python_sample.py':['Service.calculate','calculate'],'go_sample.go':['Service.Calculate','Calculate'],
                'js_sample.js':['Service.calculate','calculate'],'ts_sample.ts':['Service.calculate','calculate','Result']}
            hits=0;total=0
            for filename,expected in names.items():
                available={row['qualified'] for row in rows if row['path']==filename}
                hits+=len(set(expected)&available);total+=len(expected)
            return {'version':1,'dataset':'four-language-fixtures-and-deterministic-python-functions','symbols':build['symbols'],'queries':len(samples),
                'embedding':'hashing-64-revision-1','backend':'lsh_approximate','build_ms':build['elapsed_ms'],'unchanged_refresh_ms':warm['elapsed_ms'],
                'symbol_recall':hits/total,'ann_recall_at_5':statistics.mean(recalls),'query_p50_ms':statistics.median(latencies),
                'query_max_ms':max(latencies),'max_candidates':max(candidates),'candidate_limit':512,
                'scope':'offline syntactic symbols and approximate retrieval; not production LSP or model semantic quality'}
    finally:
        if config is None:os.environ.pop('AGENT_CODE_EMBEDDING_CONFIG',None)
        else:os.environ['AGENT_CODE_EMBEDDING_CONFIG']=config

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--symbols',type=int,default=1000);parser.add_argument('--queries',type=int,default=10);parser.add_argument('--output',type=Path)
    args=parser.parse_args();value=run_benchmark(args.symbols,args.queries)
    if args.output:args.output.write_text(json.dumps(value,indent=2)+'\n',encoding='utf-8')
    print(json.dumps(value,indent=2))
