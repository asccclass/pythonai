from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import sys
import unittest
from unittest.mock import patch
from agent_execution.policy import execution_scope,execute_tool
from agent_execution.operations.redaction import redact
from agent_execution.operations.tracing import TraceStore,span
from agent_execution.operations.backup import backup,restore,verify,migrate
from agent_execution.operations.doctor import readiness
from agent_execution.operations.benchmark import run_benchmark


class OperationsTests(unittest.TestCase):
    def setUp(self):
        directory=tempfile.TemporaryDirectory();self.addCleanup(directory.cleanup);self.root=Path(directory.name)
        env=patch.dict(os.environ,{'AGENT_TRACE_DB':str(self.root/'trace.db'),'AGENT_WORKSPACE_ROOT':str(self.root),'AGENT_PROJECT_WORKSPACES':'{}'})
        env.start();self.addCleanup(env.stop)
    def database(self):
        path=self.root/'source.db'
        with closing(sqlite3.connect(path)) as conn,conn:conn.execute('CREATE TABLE data(value TEXT)');conn.execute("INSERT INTO data VALUES('中文')")
        return path
    def test_nested_spans_are_owner_scoped_correlated_and_redacted(self):
        with execution_scope(task_id='task',episode_id=5),span('task','parent',{'password':'private'}) as evidence:
            execute_tool('fixture',{},lambda:'done');evidence['success']=True
        store=TraceStore();rows=store.list();self.assertEqual(len(rows),2);self.assertEqual(len({r['trace_id'] for r in rows}),1)
        child=next(r for r in rows if r['kind']=='tool');self.assertIsNotNone(child['parent_span']);self.assertEqual(child['episode'],5)
        self.assertNotIn('private',json.dumps(rows));self.assertEqual(store.summary()['count'],2)
        with execution_scope(identity='other'):self.assertEqual(store.list(),[])
    def test_redaction_hides_known_environment_values_keys_bearer_and_urls(self):
        with patch.dict(os.environ,{'PROVIDER_API_KEY':'private-value-123'}):
            value=redact({'authorization':'secret','nested':['Bearer abcdefghi','https://user:passwd@example.test/','private-value-123','api_key=another-secret']})
        text=json.dumps(value)
        for secret in ['abcdefghi','passwd@','private-value-123','another-secret']:self.assertNotIn(secret,text)
    def test_trace_storage_failure_never_repeats_real_effect(self):
        effects=[]
        with patch('agent_execution.operations.tracing.TraceStore',side_effect=OSError('private')):
            self.assertEqual(execute_tool('fixture',{},lambda:(effects.append(1) or 'done')),'done')
        self.assertEqual(effects,[1])
    def test_native_child_correlates_root_budget_task_episode_and_node(self):
        from agent_execution.budgets import TaskBudget,_budget
        from agent_execution.tasks import task_owner
        from agent_execution.policy import current_context
        from agent_execution.process import run_process
        owner=task_owner(current_context());database=self.root/'tasks.db';budget=TaskBudget(owner,'root-task',database);token=_budget.set(budget)
        try:
            with execution_scope(task_id='root-task'),span('task','root'):
                worker=Path(__file__).parents[1]/'operations'/'fixtures'/'trace_worker.py'
                result=run_process([sys.executable,str(worker),str(database),owner,'root-task'],timeout=15)
                self.assertEqual(result.returncode,0,result.stderr)
        finally:_budget.reset(token)
        rows=TraceStore().list();self.assertEqual(len({row['trace_id'] for row in rows}),1)
        child=next(row for row in rows if row['node']=='child-node');self.assertEqual(child['parent_task'],'root-task')
    def test_operations_api_is_authenticated_admin_only(self):
        from agent_execution.api import authenticated_api
        headers={'Authorization':'Bearer '+'a'*32}
        with patch.dict(os.environ,{'AGENT_API_TOKENS':json.dumps({'a'*32:'admin'})}),patch('agent_execution.api.get_user_role',return_value='admin'):
            self.assertEqual(authenticated_api('/api/ops/readiness','GET',None,{})[0],401)
            self.assertEqual(authenticated_api('/api/ops/readiness','GET',None,headers)[0],200)
            self.assertEqual(authenticated_api('/api/ops/metrics','GET',None,headers)[0],200)
            with patch('agent_execution.api.get_user_role',return_value='viewer'):
                self.assertEqual(authenticated_api('/api/ops/traces','GET',None,headers)[0],403)

    def test_metrics_are_aggregate_and_temporal_readiness_requires_pollers(self):
        from agent_execution.api import authenticated_api
        from agent_execution.temporal_backend.readiness import inspect_sync
        with execution_scope(task_id='private-metric-task'),span('workflow_activity','private-queue',{'api_key':'private-secret'}):
            pass
        headers={'Authorization':'Bearer '+'a'*32}
        with patch.dict(os.environ,{'AGENT_API_TOKENS':json.dumps({'a'*32:'admin'})}),\
                patch('agent_execution.api.get_user_role',return_value='admin'):
            status,report=authenticated_api('/api/ops/metrics','GET',None,headers)
        self.assertEqual(status,200);self.assertGreaterEqual(report['spans_total'],1)
        self.assertGreaterEqual(report['workflow_activity_spans_total'],1)
        self.assertNotIn('private-metric-task',json.dumps(report))
        self.assertNotIn('private-secret',json.dumps(report))
        with patch.dict(os.environ,{'AGENT_EXECUTION_BACKEND':'temporal'}),\
                patch('agent_execution.temporal_backend.readiness.inspect_sync',return_value={
                    'connected':True,'ready':False,'queues':[]} ) as probe:
            report=readiness()
        probe.assert_called_once()
        self.assertFalse(report['ready'])
        self.assertFalse(report['checks']['temporal_pollers'])
    def test_backup_restore_preserves_data_and_never_overwrites_or_accepts_tampering(self):
        path=self.database();destination=self.root/'backup';manifest=backup([path],destination)
        with closing(sqlite3.connect(path)) as conn,conn:conn.execute("INSERT INTO data VALUES('later')")
        result=restore(destination,self.root/'restored')
        with closing(sqlite3.connect(result['databases'][0])) as conn:self.assertEqual(conn.execute('SELECT value FROM data').fetchall(),[('中文',)])
        with self.assertRaises(FileExistsError):restore(destination,self.root/'restored')
        manifest['databases'][0]['file']='../source.db';(destination/'manifest.json').write_text(json.dumps(manifest),encoding='utf-8')
        with self.assertRaises(ValueError):verify(destination)
    def test_legacy_database_migration_is_backed_up_and_requires_offline(self):
        path=self.database()
        with self.assertRaises(ValueError):migrate('memory',path,self.root/'backup')
        result=migrate('memory',path,self.root/'backup',offline=True)
        self.assertIn('semantic_memories',result['after']['tables']);self.assertNotIn('semantic_memories',result['before']['tables'])
        self.assertTrue(verify(self.root/'backup'))
    def test_readiness_reports_corrupt_storage_without_provider_or_secret(self):
        corrupt=self.root/'corrupt.db';corrupt.write_text('not sqlite')
        for variable in ('AGENT_TASK_DB','AGENT_EXTENSION_DB','AGENT_IMPROVEMENT_DB','AGENT_TRACE_DB','AGENT_MCP_OAUTH_DB'):
            with self.subTest(variable=variable),patch.dict(os.environ,{variable:str(corrupt)}):
                report=readiness()
                self.assertFalse(report['ready']);self.assertEqual(report['provider'],'not probed');self.assertNotIn(str(corrupt),json.dumps(report))
    def test_fixed_benchmark_native_failure_injection_report(self):
        report=run_benchmark();self.assertTrue(report['passed'],report)
        self.assertEqual(report['completion_rate'],1);self.assertEqual(report['recovery_success_rate'],1)
        self.assertEqual(report['unexpected_tracked_source_changes'],0);self.assertIsNone(report['provider_cost_usd'])
        ids=[case['id'] for case in report['cases']]
        self.assertEqual(len(ids),len(set(ids)))
        self.assertTrue({'watch-run-limit','oauth-native-callback','oauth-slow-callback',
            'audio-sdk-contract','reviewed-persona-rollback'}.issubset(ids),ids)
        self.assertEqual(report['completed_cases'],report['case_count'])
