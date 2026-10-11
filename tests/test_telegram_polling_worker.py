import tempfile
import io
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from communication_adapters.telegram_polling import PollingLease, TelegramPollingConflict

from communication_adapters.telegram_adapter import TelegramAdapter
from communication_store import CommunicationStore
from communication_worker import CommunicationWorker
from telegram_polling_worker import TelegramPollingWorker, create_telegram_polling_worker


def telegram_update(text="hello", update_id=100, message_id=7, sender_id=123):
    return {
        "update_id": update_id,
        "message": {
            "message_id": message_id,
            "from": {"id": sender_id, "is_bot": False, "first_name": "Ada"},
            "chat": {"id": 456, "type": "private"},
            "date": 1,
            "text": text,
        },
    }


class TelegramPollingWorkerTests(unittest.TestCase):
    def setUp(self):
        mode = patch.dict("os.environ", {"COMM_WEBHOOK_MODE": "development"})
        mode.start()
        self.addCleanup(mode.stop)
        locks=tempfile.TemporaryDirectory();self.addCleanup(locks.cleanup)
        env=patch.dict(os.environ,{'AGENT_TELEGRAM_POLL_LOCK_DIR':locks.name})
        env.start();self.addCleanup(env.stop)

    def test_adapter_get_updates_calls_telegram_api(self):
        calls = []

        def http_get(url, params):
            calls.append((url, params))
            return {"ok": True, "result": [telegram_update()]}

        adapter = TelegramAdapter(bot_token="token", webhook_secret="", http_get=http_get)

        updates = adapter.get_updates(offset=10, timeout=5, limit=2)

        self.assertEqual(len(updates), 1)
        self.assertIn("/bottoken/getUpdates", calls[0][0])
        self.assertEqual(calls[0][1]["offset"], 10)
        self.assertEqual(calls[0][1]["timeout"], 5)
        self.assertEqual(calls[0][1]["limit"], 2)

    def test_poll_once_enqueues_and_processes_updates(self):
        sent = []
        updates = [[telegram_update("hello", update_id=100)]]

        with tempfile.TemporaryDirectory() as temp_dir:
            store = CommunicationStore(Path(temp_dir) / "communication.db")
            adapter = TelegramAdapter(
                bot_token="token",
                webhook_secret="",
                http_get=lambda url, params: {"ok": True, "result": updates.pop(0)},
                http_post=lambda url, payload: sent.append(payload),
            )

            def runner(command):
                return f"reply to {command.text}"

            worker = TelegramPollingWorker(
                store=store,
                adapter=adapter,
                worker=CommunicationWorker(store, {"telegram": adapter}, runner),
                poll_timeout_seconds=1,
            )

            result = worker.poll_once()
            messages = store.recent_messages(limit=10)

        self.assertEqual(result, {"updates": 1, "queued": 1, "processed": 1})
        self.assertEqual(worker.offset, 101)
        self.assertEqual(sent[0]["text"], "reply to hello")
        self.assertIn("outbound", {message["direction"] for message in messages})

    def test_poll_once_enforces_allowed_senders(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = CommunicationStore(Path(temp_dir) / "communication.db")
            adapter = TelegramAdapter(
                bot_token="token",
                webhook_secret="",
                http_get=lambda url, params: {"ok": True, "result": [telegram_update(sender_id=123)]},
            )
            worker = TelegramPollingWorker(
                store=store,
                adapter=adapter,
                worker=CommunicationWorker(store, {"telegram": adapter}, lambda command: "ok"),
                allowed_senders={"telegram:999"},
            )

            with self.assertRaises(PermissionError):
                worker.poll_once()

            self.assertEqual(store.pending_commands(), [])

    def test_create_polling_worker_reads_env(self):
        with (
            patch.dict(
                "os.environ",
                {
                    "COMM_ALLOWED_SENDERS": "telegram:123",
                    "TELEGRAM_POLL_TIMEOUT": "3",
                    "TELEGRAM_POLL_IDLE_SLEEP": "0.05",
                    "TELEGRAM_POLL_ERROR_SLEEP": "0.5",
                },
                clear=False,
            ),
            patch("telegram_polling_worker.create_agent_runtime", return_value=object()),
            patch("telegram_polling_worker.CommunicationStore"),
        ):
            worker = create_telegram_polling_worker()

        self.assertEqual(worker.allowed_senders, {"telegram:123"})
        self.assertEqual(worker.poll_timeout_seconds, 3)
        self.assertEqual(worker.idle_sleep_seconds, 0.05)
        self.assertEqual(worker.error_sleep_seconds, 0.5)

    def test_run_forever_retries_transient_polling_http_errors(self):
        calls = []
        sleeps = []

        with tempfile.TemporaryDirectory() as temp_dir:
            store = CommunicationStore(Path(temp_dir) / "communication.db")
            adapter = TelegramAdapter(bot_token="token", webhook_secret="",
                http_get=lambda url,params:{'ok':True,'result':{'url':''}})
            worker = TelegramPollingWorker(
                store=store,
                adapter=adapter,
                worker=CommunicationWorker(store, {"telegram": adapter}, lambda command: "ok"),
                error_sleep_seconds=0.25,
                sleep=lambda seconds: sleeps.append(seconds),
            )

            def poll_once():
                calls.append("poll")
                if len(calls) == 1:
                    raise HTTPError("https://api.telegram.org", 502, "Bad Gateway", hdrs=None, fp=None)
                raise KeyboardInterrupt

            worker.poll_once = poll_once
            with patch("builtins.print") as print_mock:
                with self.assertRaises(KeyboardInterrupt):
                    worker.run_forever()

        self.assertEqual(calls, ["poll", "poll"])
        self.assertEqual(sleeps, [0.25])
        self.assertIn("Telegram polling warning:", print_mock.call_args.args[0])

    def test_http_and_json_conflicts_classify_without_disclosing_provider_text(self):
        for description,kind in [('Conflict: terminated by other getUpdates request','poller'),
                ("Conflict: can't use getUpdates method while webhook is active",'webhook'),('unrecognized','unknown')]:
            for transport in ('http','json'):
                with self.subTest(kind=kind,transport=transport):
                    payload={'ok':False,'error_code':409,'description':description+' private-token https://secret.test/path'}
                    stream=io.BytesIO(json.dumps(payload).encode())
                    def get(url,params):
                        if transport=='http':raise HTTPError(url,409,'Conflict',None,stream)
                        return payload
                    adapter=TelegramAdapter(bot_token='private-token',http_get=get)
                    with self.assertRaises(TelegramPollingConflict) as raised:adapter.get_updates()
                    self.assertEqual(raised.exception.kind,kind)
                    self.assertNotIn('private-token',str(raised.exception));self.assertNotIn('secret.test',str(raised.exception))
                    if transport=='http':self.assertTrue(stream.closed)

    def test_conflict_stops_loop_once_without_retry_and_releases_lock(self):
        from unittest.mock import Mock
        adapter=TelegramAdapter(bot_token='token',http_get=lambda url,params:{'ok':True,'result':{'url':''}})
        worker=TelegramPollingWorker(None,adapter,Mock(),sleep=Mock())
        worker.poll_once=Mock(side_effect=TelegramPollingConflict('poller'))
        with self.assertRaises(TelegramPollingConflict):worker.run_forever()
        worker.poll_once.assert_called_once();worker.sleep.assert_not_called()
        with PollingLease('token'):pass

    def test_real_http_409_body_is_classified_and_not_retried(self):
        from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
        import threading
        from communication_adapters.telegram_adapter import _json_get
        calls=[]
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def do_GET(self):
                calls.append(self.path)
                payload=json.dumps({'ok':False,'error_code':409,'description':'Conflict: terminated by other getUpdates request'}).encode()
                self.send_response(409);self.send_header('Content-Type','application/json')
                self.send_header('Content-Length',str(len(payload)));self.end_headers();self.wfile.write(payload)
        server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        self.addCleanup(server.server_close);self.addCleanup(server.shutdown)
        threading.Thread(target=server.serve_forever,daemon=True).start()
        adapter=TelegramAdapter(bot_token='token',http_get=lambda url,params:_json_get(f'http://127.0.0.1:{server.server_port}/getUpdates',params))
        with self.assertRaises(TelegramPollingConflict) as raised:adapter.get_updates(timeout=1)
        self.assertEqual(raised.exception.kind,'poller');self.assertEqual(len(calls),1)

    def test_webhook_preflight_refuses_polling_without_deleting_webhook(self):
        from unittest.mock import Mock
        calls=[]
        adapter=TelegramAdapter(bot_token='token',http_get=lambda url,params:(calls.append(url) or
            {'ok':True,'result':{'url':'https://private.test/webhook'}}),http_post=Mock())
        worker=TelegramPollingWorker(None,adapter,Mock(),sleep=Mock());worker.poll_once=Mock()
        with self.assertRaises(TelegramPollingConflict) as raised:worker.run_forever()
        self.assertEqual(raised.exception.kind,'webhook');self.assertEqual(len(calls),1)
        self.assertTrue(calls[0].endswith('/getWebhookInfo'));worker.poll_once.assert_not_called()
        adapter.http_post.assert_not_called();worker.sleep.assert_not_called()

    def test_native_lock_blocks_second_process_and_recovers_after_crash(self):
        code="from communication_adapters.telegram_polling import PollingLease; import time; lock=PollingLease('123:private'); lock.__enter__(); print('locked',flush=True); time.sleep(30)"
        process=subprocess.Popen([sys.executable,'-c',code],stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
        try:
            self.assertEqual(process.stdout.readline().strip(),'locked')
            with self.assertRaises(TelegramPollingConflict):
                with PollingLease('123:rotated'):pass
            with PollingLease('456:other'):pass
        finally:
            process.kill();process.communicate(timeout=5)
        with PollingLease('123:private') as lease:
            self.assertNotIn('private',str(lease.path));self.assertNotIn('123:',str(lease.path))

    def test_main_conflict_exits_nonzero_and_cleans_background_workers(self):
        import telegram_polling_worker as module
        from unittest.mock import Mock
        worker=Mock();worker.run_forever.side_effect=TelegramPollingConflict('poller')
        with patch.object(module,'create_telegram_polling_worker',return_value=worker),patch.object(module,'scheduler_worker') as scheduler,patch('builtins.print') as output:
            with self.assertRaises(SystemExit) as raised:module.main()
        self.assertEqual(raised.exception.code,1);scheduler.stop.assert_called_once()
        worker.worker.drain.assert_called_once();worker.runtime.memory_worker.stop.assert_called_once()
        self.assertIn('Another getUpdates consumer',output.call_args.args[0])


if __name__ == "__main__":
    unittest.main()
