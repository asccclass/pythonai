import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from agent_execution.policy import execution_scope
from agent_execution.workspace_lifecycle import LifecycleStore
from websocket_server import app,reset_hub_for_tests


@unittest.skipUnless(os.name=='nt','native Windows cleanup')
class LifecycleWebSocketTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.home=Path(self.temp.name);self.root=self.home/'workspace';self.root.mkdir()
        roles=self.home/'roles.json';roles.write_text('{"websocket:alice":"viewer","websocket:bob":"viewer"}')
        env=patch.dict(os.environ,{'AGENT_TASK_DB':str(self.home/'tasks.db'),'AGENT_APPROVAL_DB':str(self.home/'approvals.db'),
            'COMPANION_EVENT_DB':str(self.home/'events.db'),'SCHEDULER_DB_PATH':str(self.home/'schedule.db'),
            'AGENT_TENANT_ROOT':str(self.home/'tenants'),'AGENT_PROJECT_WORKSPACES':json.dumps({'default':str(self.root)}),
            'COMPANION_WS_TOKENS':'alice-token:alice,bob-token:bob','ROLES_CONFIG':str(roles),'AGENT_WORKSPACE_CLEANUP':'1'})
        env.start();self.addCleanup(env.stop);reset_hub_for_tests();self.addCleanup(reset_hub_for_tests)

    def scope(self,conversation='one',identity='websocket:alice',device='a'):
        return execution_scope(source='websocket',identity=identity,project='default',session='default:'+device+':'+conversation,
            conversation=conversation,workspace=self.root)

    def hello(self,ws,token='alice-token',device='a',conversation='one'):
        ws.send_json({'v':1,'type':'hello','payload':{'token':token,'device_id':device,'project':'default',
            'conversation_id':conversation,'last_seq':0}})
        return ws.receive_json()

    def send(self,ws,kind,payload=None,key='request'):
        ws.send_json({'v':1,'type':kind,'id':key,'payload':payload or {}})
        return ws.receive_json()

    def test_owner_confirmation_purge_two_devices_and_reconnect_tombstone(self):
        with self.scope():
            store=LifecycleStore();directory=store.create('temporary','temporary')
            (self.root/'temporary/source.txt').write_text('private')
            store.create('retained','retained')
        self.root.joinpath('legacy').mkdir()
        with TestClient(app) as client:
            with client.websocket_connect('/ws/v1') as first,client.websocket_connect('/ws/v1') as second:
                welcome=self.hello(first);other=self.hello(second,device='b')
                self.assertEqual(welcome['payload']['features']['workspace_lifecycle']['session'],other['payload']['features']['workspace_lifecycle']['session'])
                preview=self.send(first,'session.delete.preview')['payload']
                self.assertEqual([item['id'] for item in preview['items']],[directory])
                self.assertEqual(preview['unmanaged_count'],1)
                result=self.send(first,'session.delete.confirm',{'plan_id':preview['plan_id']},'confirmed')
                self.assertEqual(result['payload']['state'],'deleted',result)
                self.assertEqual(second.receive_json()['payload']['state'],'deleted')
                self.assertFalse(self.root.joinpath('temporary').exists())
                self.assertTrue(self.root.joinpath('retained').exists());self.assertTrue(self.root.joinpath('legacy').exists())
                duplicate=self.send(first,'session.delete.confirm',{'plan_id':preview['plan_id']},'confirmed')
                self.assertEqual(duplicate['payload']['state'],'deleted')
                second.receive_json()
                late=self.send(first,'chat.send',{'text':'do not execute'},'late')
                self.assertEqual(late['payload']['code'],'session_deleted')
            with client.websocket_connect('/ws/v1') as ws:
                self.assertEqual(self.hello(ws)['payload']['features']['conversation_state'],'deleted')

    def test_other_user_cannot_confirm_plan_and_retention_invalidates_it(self):
        with self.scope():
            store=LifecycleStore();key=store.create('temporary','temporary')
        with TestClient(app) as client:
            with client.websocket_connect('/ws/v1') as alice,client.websocket_connect('/ws/v1') as bob:
                self.hello(alice);self.hello(bob,'bob-token',device='c')
                plan=self.send(alice,'session.delete.preview')['payload']['plan_id']
                result=self.send(bob,'session.delete.confirm',{'plan_id':plan})
                self.assertEqual(result['type'],'error')
                self.assertTrue(self.root.joinpath('temporary').exists())
                self.send(alice,'workspace.retain',{'directory_id':key})
                result=self.send(alice,'session.delete.confirm',{'plan_id':plan})
                self.assertEqual(result['type'],'error')
                self.assertTrue(self.root.joinpath('temporary').exists())
