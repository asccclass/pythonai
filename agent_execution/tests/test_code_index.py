from contextlib import closing
import json
import os
from pathlib import Path
import shutil
import sqlite3
import sys
import time
import tempfile
import unittest
from unittest.mock import patch,Mock

from agent_execution import execution_scope
from agent_execution.code_index import CodeIndex
from agent_execution.code_index.index import Embeddings,SECRET
from agent_execution.code_index.navigation import navigate
from tools.code_symbols import code_symbols
from tools.search_codebase import search_codebase
from tools.refresh_code_index import refresh_code_index
from tools.go_to_definition import go_to_definition
from tools.find_references import find_references

FIXTURES=Path(__file__).parents[1]/'code_index/fixtures'

class IndexTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)/'workspace';self.root.mkdir()
        environment=patch.dict(os.environ,{'AGENT_CODE_EMBEDDING_CONFIG':'{"kind":"hashing","dimensions":64,"revision":"1"}','AGENT_CODE_LSP_SERVERS':'{}','AGENT_CODE_INDEX_DIR':'','AGENT_TASK_DB':str(Path(self.temp.name)/'tasks.db')})
        environment.start();self.addCleanup(environment.stop)
        scope=execution_scope(workspace=self.root,task_id='code-index');scope.__enter__();self.addCleanup(lambda:scope.__exit__(None,None,None))
        self.write('sample.py','def hello():\n    return "原始資料"\n\nhello()\n')

    def write(self,name,text):
        path=self.root/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_text(text,encoding='utf-8');return path

    def test_incremental_modification_deletion_and_same_mtime(self):
        index=CodeIndex();first=index.refresh();self.assertEqual(first['changed'],1)
        original=(self.root/'sample.py').stat()
        with patch.object(Embeddings,'embed',side_effect=AssertionError('unchanged files must not embed')):self.assertEqual(index.refresh()['unchanged'],1)
        self.write('sample.py','def world():\n    return "更新資料"\n');os.utime(self.root/'sample.py',ns=(original.st_atime_ns,original.st_mtime_ns))
        self.assertEqual(index.refresh()['changed'],1);self.assertEqual(index.search('hello',mode='symbol')['results'],[])
        self.assertEqual(index.search('world',mode='symbol')['results'][0]['name'],'world')
        (self.root/'sample.py').unlink();self.assertEqual(index.refresh()['deleted'],1)
        self.assertEqual(index.search('world')['results'],[])

    def test_registered_private_directory_and_cache_are_owner_isolated(self):
        from agent_execution.workspace_lifecycle import LifecycleStore
        with execution_scope(source='websocket',identity='websocket:alice',conversation='one',session='device:one'):
            LifecycleStore().create('private','temporary')
            self.write('private/secret.py','def alice_only(): pass\n')
            alice=CodeIndex()
            self.assertTrue(alice.search('alice_only',mode='symbol')['results'])
        with execution_scope(source='websocket',identity='websocket:bob',conversation='one',session='device:one'):
            bob=CodeIndex()
            self.assertNotEqual(alice.path,bob.path)
            self.assertEqual(bob.search('alice_only',mode='symbol')['results'],[])
            with self.assertRaises(PermissionError):alice.connect()

    def test_removed_declaration_and_duplicate_scoped_names(self):
        shutil.copyfile(FIXTURES/'python_sample.py',self.root/'sample.py')
        value=json.loads(code_symbols());names=[row['qualified'] for row in value['results']]
        self.assertIn('Service.calculate',names);self.assertIn('calculate',names)
        self.write('sample.py','def calculate(x): return x\n')
        self.assertEqual(len(CodeIndex().search('calculate',mode='symbol')['results']),1)

    def test_model_revision_dimension_and_provider_identity_invalidate_vectors(self):
        CodeIndex().refresh();os.environ['AGENT_CODE_EMBEDDING_CONFIG']='{"kind":"hashing","dimensions":32,"revision":"2"}'
        index=CodeIndex();index.refresh(embed=False)
        with closing(index.connect()) as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM ci_symbols WHERE vector IS NOT NULL').fetchone()[0],0)
        self.assertEqual(index.refresh()['pending_vectors'],0)
        with closing(index.connect()) as conn:self.assertEqual(len(json.loads(conn.execute('SELECT vector FROM ci_symbols').fetchone()[0])),32)

    def test_legacy_unversioned_vectors_are_removed(self):
        with closing(sqlite3.connect(self.root/'codebase_index.db')) as conn,conn:
            conn.execute('CREATE TABLE code_index(name TEXT,embedding TEXT)');conn.execute('INSERT INTO code_index VALUES(?,?)',('stale','[1,0]'))
        CodeIndex().refresh()
        with closing(sqlite3.connect(self.root/'codebase_index.db')) as conn:self.assertIsNone(conn.execute("SELECT name FROM sqlite_master WHERE name='code_index'").fetchone())

    def test_nested_ignore_negation_and_changed_rules_purge_records(self):
        self.write('.gitignore','*.js\n!keep.js\ngenerated/\n');self.write('keep.js','function kept() {}');self.write('drop.js','function removed() {}')
        self.write('nested/.agentignore','hidden.py\n');self.write('nested/hidden.py','def hidden(): pass');self.write('generated/file.py','def generated(): pass')
        names=set(CodeIndex().inventory());self.assertIn('keep.js',names);self.assertNotIn('drop.js',names);self.assertNotIn('nested/hidden.py',names)
        CodeIndex().refresh();self.write('.gitignore','*.js\n')
        self.assertEqual(CodeIndex().search('kept',mode='symbol')['results'],[])

    def test_secret_files_content_and_binary_are_not_stored_or_embedded(self):
        self.write('.env.py','def secret_env(): pass');self.write('credentials.py','def secret_credentials(): pass')
        self.write('secret.py',"api_key = 'literal-secret-value'\ndef hidden(): pass\n");self.write('safe.txt','普通中文 exact needle')
        self.write('config.json','{"api_key":"literal-secret-value"}');self.write('config.ini','password=literalSecretValue123')
        self.write('.credentials.json','{"value":"hiddenCredentialValue"}')
        (self.root/'binary.py').write_bytes(b'\x00\xff')
        index=CodeIndex();index.refresh()
        with closing(index.connect()) as conn:paths={row[0] for row in conn.execute('SELECT path FROM ci_files')}
        self.assertEqual(paths,{'sample.py','safe.txt'});self.assertNotIn('literal-secret',json.dumps(index.search('hidden')))
        self.assertEqual(index.search('exact needle',mode='text')['results'][0]['file_path'],'safe.txt')

    def test_new_secret_and_parse_error_remove_previous_symbols(self):
        index=CodeIndex();index.refresh();self.write('sample.py','def broken(:\n')
        self.assertEqual(index.refresh()['parse_errors'],1);self.assertEqual(index.search('hello',mode='symbol')['results'],[])
        self.write('sample.py',"password = 'unsafe-value'\n")
        self.assertEqual(index.refresh()['deleted'],1);self.assertEqual(index.search('password',mode='text')['results'],[])

    def test_outside_workspace_and_windows_junction_are_rejected(self):
        from agent_execution.process import run_process
        outside=Path(self.temp.name)/'outside';outside.mkdir();(outside/'private.py').write_text('def private(): pass')
        if os.name=='nt':
            result=run_process(['cmd','/c','mklink','/J',str(self.root/'linked'),str(outside)],cwd=self.root,timeout=5)
            self.assertEqual(result.returncode,0)
        else:(self.root/'linked').symlink_to(outside,target_is_directory=True)
        self.assertFalse(any('linked' in name for name in CodeIndex().inventory()))
        with self.assertRaises(PermissionError):go_to_definition(str(outside/'private.py'),1,4)

    def test_all_languages_real_grammars_and_syntactic_references(self):
        for filename in ('python_sample.py','go_sample.go','js_sample.js','ts_sample.ts'):
            shutil.copyfile(FIXTURES/filename,self.root/filename)
        rows=json.loads(code_symbols())['results'];languages={Path(row['file_path']).suffix for row in rows}
        self.assertEqual(languages,{'.py','.go','.js','.ts'})
        self.assertIn('Service.Calculate',{row['qualified'] for row in rows})
        index=CodeIndex();value=index.navigate('go_sample.go',6,24,True)
        self.assertFalse(value['resolved']);self.assertEqual(value['name'],'Calculate');self.assertEqual(len(value['results']),3)
        javascript=index.navigate('js_sample.js',5,15,True)
        self.assertEqual(len([row for row in javascript['results'] if row['file_path']=='js_sample.js']),3)
        self.assertFalse(any(row['file_path'].endswith('.py') for row in javascript['results']))
        typescript=index.navigate('ts_sample.ts',6,19,False);self.assertEqual(typescript['results'][0]['name'] if 'name' in typescript['results'][0] else typescript['name'],'calculate')

    def test_python_jedi_resolves_actual_definition_and_references(self):
        definition=json.loads(go_to_definition('sample.py',4,0));self.assertEqual(definition['backend'],'jedi');self.assertEqual(definition['results'][0]['line'],1)
        refs=json.loads(find_references('sample.py',4,0));self.assertEqual({row['line'] for row in refs['results']},{1,4})

    def test_builtin_definition_has_no_workspace_location(self):
        self.write('sample.py','print("hello")\n')
        value=json.loads(go_to_definition('sample.py',1,0))
        self.assertEqual(value['backend'],'jedi');self.assertEqual(value['results'],[])

    def test_task_worktree_index_does_not_dirty_repository(self):
        repository=Path(self.temp.name)/'repository';repository.mkdir()
        with execution_scope(repository_workspace=repository):
            index=CodeIndex();index.refresh()
            self.assertNotIn(self.root,index.path.parents);self.assertTrue(index.path.exists())
            self.assertFalse((self.root/'codebase_index.db').exists())

    def test_external_index_storage_is_workspace_specific_and_trusted(self):
        cache=Path(self.temp.name)/'cache'
        with patch.dict(os.environ,{'AGENT_CODE_INDEX_DIR':str(cache)}):
            first=CodeIndex();first.refresh()
            other=Path(self.temp.name)/'other';other.mkdir()
            with execution_scope(workspace=other):second=CodeIndex();self.assertNotEqual(first.path,second.path)
        with patch.dict(os.environ,{'AGENT_CODE_INDEX_DIR':'relative'}),self.assertRaises(ValueError):CodeIndex()

    def configure_lsp(self,mode='normal',timeout=5):
        self.write('sample.ts',(FIXTURES/'ts_sample.ts').read_text(encoding='utf-8'))
        os.environ['AGENT_CODE_LSP_SERVERS']=json.dumps({'typescript':{'command':[sys.executable,str(FIXTURES/'lsp_server.py'),mode],'timeout':timeout}})

    def test_lsp_handshake_utf16_unicode_and_external_location_filter(self):
        self.configure_lsp();line=(self.root/'sample.ts').read_text(encoding='utf-8').splitlines()[5];col=line.index('calculate')
        value=json.loads(go_to_definition('sample.ts',6,col));self.assertEqual(value['backend'],'lsp');self.assertEqual(len(value['results']),1)
        self.assertEqual(value['results'][0]['line'],5)
        refs=json.loads(find_references('sample.ts',6,col));self.assertTrue(refs['resolved'])
        symbols=json.loads(code_symbols('sample.ts','lsp'));self.assertEqual(symbols['results'][0]['name'],'calculate')

    def test_lsp_timeout_oversize_and_invalid_locations_fail_closed(self):
        for mode in ('hang','oversize','malformed'):
            with self.subTest(mode=mode):
                self.configure_lsp(mode,1)
                with self.assertRaises(ValueError):go_to_definition('sample.ts',6,19)

    def test_remote_embeddings_are_explicit_bounded_and_no_fallback(self):
        os.environ['AGENT_CODE_EMBEDDING_CONFIG']='{"kind":"openai","dimensions":8,"revision":"1","model":"unit","max_requests":1}'
        self.write('two.py','def second(): pass\n')
        client=Mock();client.with_options.return_value=client;client.base_url='https://provider.invalid/';client.embeddings.create.side_effect=RuntimeError('provider failure')
        with patch('server.get_client',return_value=client):
            index=CodeIndex();value=index.refresh();self.assertEqual(value['embedding_requests'],1);self.assertEqual(value['embedding_failures'],1)
            index.search('hello');self.assertEqual(client.embeddings.create.call_count,1)
            with closing(index.connect()) as conn:self.assertEqual(conn.execute('SELECT COUNT(*) FROM ci_symbols WHERE vector IS NOT NULL').fetchone()[0],0)

    def test_invalid_vector_dimensions_and_configuration_are_rejected(self):
        for config in ('{"kind":"hashing","dimensions":0,"revision":"1"}','{"kind":"hashing","dimensions":64,"revision":""}'):
            with self.subTest(config=config),patch.dict(os.environ,{'AGENT_CODE_EMBEDDING_CONFIG':config}),self.assertRaises(ValueError):CodeIndex()
        with patch('vector_search.HashingEmbeddingProvider') as provider:
            provider.return_value.embed.return_value=[float('nan')]*64
            with self.assertRaises(ValueError):Embeddings().embed('query')

    def test_remote_endpoint_change_invalidates_previously_valid_vectors(self):
        from types import SimpleNamespace as NS
        os.environ['AGENT_CODE_EMBEDDING_CONFIG']='{"kind":"openai","dimensions":8,"revision":"1","model":"unit","max_requests":1}'
        client=Mock();client.with_options.return_value=client;client.base_url='https://first.invalid/'
        client.embeddings.create.return_value=NS(data=[NS(embedding=[1.0]+[0.0]*7)])
        with patch('server.get_client',return_value=client):
            self.assertEqual(CodeIndex().refresh()['pending_vectors'],0)
            client.base_url='https://second.invalid/'
            self.assertEqual(CodeIndex().refresh(embed=False)['pending_vectors'],1)

    def test_jsx_tsx_and_invalid_positions(self):
        self.write('view.tsx','export const View = () => <div>hello</div>;')
        self.write('view.jsx','export const View = () => <div>hello</div>;')
        self.assertEqual(len(CodeIndex().search('View',mode='symbol')['results']),2)
        with self.assertRaises(ValueError):CodeIndex().navigate('sample.py',1,100)

    def test_cancelled_lsp_request_exits_without_results(self):
        from agent_execution.code_index.navigation import run_process
        self.configure_lsp('hang',5)
        deadline=[None]
        def start_lsp(*args,**kwargs):
            # Cancel the LSP request, rather than racing the policy/hook setup.
            deadline[0]=time.monotonic()+0.5
            return run_process(*args,**kwargs)
        def cancelled():
            return deadline[0] is not None and time.monotonic()>deadline[0]
        with patch('agent_execution.code_index.navigation.run_process',side_effect=start_lsp) as process:
            with execution_scope(check_cancelled=cancelled),self.assertRaises((ValueError,InterruptedError)):
                go_to_definition('sample.ts',6,19)
        process.assert_called_once()

    def test_repeatable_quality_and_latency_benchmark(self):
        from agent_execution.code_index.benchmark import run_benchmark
        result=run_benchmark(350,3)
        self.assertEqual(result['symbol_recall'],1.0);self.assertGreaterEqual(result['ann_recall_at_5'],0.6)
        self.assertLessEqual(result['max_candidates'],512);self.assertGreater(result['build_ms'],0)

    def test_inventory_change_during_embedding_rolls_back_no_partial_results(self):
        index=CodeIndex();real=index.embeddings.embed
        def mutate(text):
            self.write('sample.py','def changed(): pass');return real(text)
        with patch.object(index.embeddings,'embed',side_effect=mutate),self.assertRaises(ValueError):index.refresh()
        self.assertEqual(CodeIndex().search('hello',mode='symbol')['results'],[])

    def test_edit_during_semantic_query_cannot_return_stale_rows(self):
        index=CodeIndex();index.refresh();real=index.embeddings.embed
        def edit(text):
            self.write('sample.py','def changed(): pass');return real(text)
        with patch.object(index.embeddings,'embed',side_effect=edit),self.assertRaises(ValueError):index.search('hello')

    def test_symbol_query_does_not_return_changed_source(self):
        real=CodeIndex.inventory;calls=[0]
        def edit(index):
            calls[0]+=1
            if calls[0]==3:self.write('sample.py','def changed(): pass')
            return real(index)
        with patch.object(CodeIndex,'inventory',new=edit),self.assertRaises(ValueError):code_symbols()

    def test_navigation_checks_original_snapshot_even_if_another_index_refreshes(self):
        self.configure_lsp();from agent_execution.code_index.navigation import run_process
        def edit(*args,**kwargs):
            result=run_process(*args,**kwargs)
            self.write('sample.ts','export const changed = 1;');CodeIndex().refresh(embed=False)
            return result
        with patch('agent_execution.code_index.navigation.run_process',side_effect=edit),self.assertRaises(ValueError):go_to_definition('sample.ts',6,19)

    def test_large_index_uses_persistent_bounded_ann_candidates(self):
        self.write('large.py','\n'.join(f'def feature_{number}(value): return value + {number}' for number in range(700)))
        index=CodeIndex();index.refresh()
        value=index.search('feature_234',10);self.assertEqual(value['backend'],'lsh_approximate');self.assertLessEqual(value['candidate_count'],512)
        self.assertTrue(value['approximate']);self.assertTrue(value['results'])
        with patch.object(Embeddings,'embed',wraps=Embeddings().embed) as embed:
            index.search('feature_235');self.assertEqual(embed.call_count,1)

    def test_tool_registry_and_literal_case_sensitive_search(self):
        import base
        names={schema['function']['name'] for schema in base.TOOLS_SCHEMAS}
        self.assertIn('code_symbols',names);self.assertIn('refresh_code_index',names)
        result=json.loads(search_codebase('原始資料',mode='text'));self.assertEqual(result['results'][0]['line'],2)
        self.assertEqual(json.loads(search_codebase('HELLO',mode='text'))['results'],[])
        self.assertIn('symbols',json.loads(refresh_code_index()))
