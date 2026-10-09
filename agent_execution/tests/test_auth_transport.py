from contextlib import contextmanager, closing
from http.server import ThreadingHTTPServer
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient
from communication_models import InboundMessage
from communication_server import MultiWebhookService, create_asgi_app, create_request_handler
from communication_store import CommunicationStore
from communication_worker import CommunicationWorker
from memory import MemoryStore
from agent_execution.approvals import ApprovalStore


class AuthTransportTests(unittest.TestCase):
    @contextmanager
    def environment(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            roles = root / "roles.json"
            roles.write_text('{"api:alice":"admin","telegram:123":"admin"}', encoding="utf-8")
            with patch.dict(os.environ, {"MEMORY_DB_PATH": str(root / "memory.db"),
                    "COMMUNICATION_DB_PATH": str(root / "communication.db"),
                    "AGENT_APPROVAL_DB": str(root / "approvals.db"), "ROLES_CONFIG": str(roles),
                    "AGENT_API_TOKENS": json.dumps({"a" * 32: "alice"})}):
                MemoryStore()
                store = CommunicationStore()
                service = MultiWebhookService(store, {}, CommunicationWorker(store, {}, Mock()))
                yield service

    def test_asgi_requires_authentication_before_mutation(self):
        with self.environment() as service, patch("communication_server.scheduler_worker"), TestClient(create_asgi_app(service)) as client:
            response = client.post("/api/memories", json={"subject":"x", "predicate":"uses", "object":"y"})
            self.assertEqual(response.status_code, 401)
            self.assertEqual(MemoryStore().active_semantic_memories(), [])
            headers = {"Authorization": "Bearer " + "a" * 32}
            self.assertEqual(client.get("/api/memories", headers=headers).status_code, 200)

    def test_communication_decision_bypasses_model_and_binds_sender(self):
        with self.environment() as service:
            key = ApprovalStore().request("telegram:123", "telegram:456", "tool", {})
            adapter = Mock()
            runner = Mock()
            worker = CommunicationWorker(service.store, {"telegram": adapter}, runner)
            service.store.ingest_inbound_message(InboundMessage("telegram", "1", "456", "123", "/approve " + key))
            worker.process_next()
            runner.assert_not_called()
            ApprovalStore().consume(key, "telegram:123", "telegram:456", "tool", {})
            self.assertIn("saved", adapter.send_message.call_args.args[0].text)

    @unittest.skipUnless(os.environ.get("PYTHONAI_BROWSER_TEST") == "1", "opt-in live Chromium test")
    def test_dashboard_login_human_approval_and_replay(self):
        from playwright.sync_api import sync_playwright
        with self.environment() as service:
            httpd = ThreadingHTTPServer(("127.0.0.1", 0), create_request_handler(service))
            thread = threading.Thread(target=httpd.serve_forever, daemon=True)
            thread.start()
            try:
                with sync_playwright() as playwright:
                    browser = playwright.chromium.launch(headless=True)
                    try:
                        page = browser.new_page()
                        page.goto(f"http://127.0.0.1:{httpd.server_port}")
                        page.wait_for_function("document.getElementById('status').textContent === 'unauthorized'")
                        page.fill("#api-token", "a" * 32)
                        page.click("#login")
                        page.wait_for_function("document.getElementById('status').textContent.includes('Loaded')")
                        page.fill("#subject", "project")
                        page.fill("#predicate", "uses")
                        page.fill("#object", "SQLite")
                        page.locator("#memory-form button[type=submit]").click()
                        page.wait_for_function("document.getElementById('status').textContent === 'Loaded 1 memories'")
                        page.on("dialog", lambda dialog: dialog.accept())
                        page.evaluate("archiveMemory(1)")
                        self.assertIsNotNone(MemoryStore().semantic_memory(1)["archived_at"])
                        with closing(ApprovalStore().connect()) as conn:
                            row = conn.execute("SELECT * FROM approvals WHERE state='consumed'").fetchone()
                        replay = page.request.delete(f"http://127.0.0.1:{httpd.server_port}/api/memories/1",
                            headers={"Authorization": "Bearer " + "a" * 32,
                                     "X-Agent-Session": row["session"], "X-Agent-Approval": row["id"]},
                            data=json.loads(row["arguments"])["body"])
                        self.assertEqual(replay.status, 403)
                        self.assertIsNone(page.evaluate("localStorage.getItem('api-token')"))
                    finally:
                        browser.close()
            finally:
                httpd.shutdown()
                thread.join(5)
                httpd.server_close()
