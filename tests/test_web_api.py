import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from communication_models import InboundMessage
from communication_store import CommunicationStore
from memory import MemoryStore
from web_api import handle_api_request


class WebApiTests(unittest.TestCase):
    def test_memory_crud_api_creates_updates_archives_and_restores_memory(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            with self._isolated_databases(temp_dir):
                create_status, create_payload = handle_api_request(
                    "/api/memories",
                    method="POST",
                    body=json.dumps(
                        {
                            "subject": "project",
                            "predicate": "uses",
                            "object": "SQLite",
                            "confidence": 0.8,
                            "memory_type": "technology",
                            "scope": "repo",
                        }
                    ),
                )
                memory_id = create_payload["memory"]["id"]

                update_status, update_payload = handle_api_request(
                    f"/api/memories/{memory_id}",
                    method="PUT",
                    body=json.dumps({"object": "SQLite database", "confidence": 0.95}),
                )
                archive_status, archive_payload = handle_api_request(
                    f"/api/memories/{memory_id}",
                    method="DELETE",
                    body=json.dumps({"reason": "test archive"}),
                )
                list_status, list_payload = handle_api_request(
                    "/api/memories?include_archived=1&limit=10",
                    method="GET",
                )
                restore_status, restore_payload = handle_api_request(
                    f"/api/memories/{memory_id}/restore",
                    method="POST",
                )

        self.assertEqual(create_status, 201)
        self.assertEqual(create_payload["memory"]["subject"], "project")
        self.assertEqual(update_status, 200)
        self.assertEqual(update_payload["memory"]["object"], "SQLite database")
        self.assertEqual(update_payload["memory"]["confidence"], 0.95)
        self.assertEqual(archive_status, 200)
        self.assertEqual(archive_payload["memory"]["archive_reason"], "test archive")
        self.assertEqual(list_status, 200)
        self.assertIn(memory_id, [memory["id"] for memory in list_payload])
        self.assertEqual(restore_status, 200)
        self.assertIsNone(restore_payload["memory"]["archived_at"])
        self.assertIsNone(restore_payload["memory"]["archive_reason"])

    def test_memory_api_rejects_invalid_confidence(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            with self._isolated_databases(temp_dir):
                status, payload = handle_api_request(
                    "/api/memories",
                    method="POST",
                    body=json.dumps(
                        {
                            "subject": "project",
                            "predicate": "uses",
                            "object": "SQLite",
                            "confidence": 2,
                        }
                    ),
                )

        self.assertEqual(status, 400)
        self.assertEqual(payload["error"], "bad_request")
        self.assertIn("confidence", payload["message"])

    def test_stats_and_tasks_api_use_configured_databases(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            with self._isolated_databases(temp_dir) as communication_store:
                communication_store.ingest_inbound_message(
                    InboundMessage(
                        platform="telegram",
                        platform_message_id="42",
                        conversation_id="1",
                        sender_id="2",
                        text="review memory",
                    )
                )

                stats_status, stats_payload = handle_api_request("/api/stats", method="GET")
                tasks_status, tasks_payload = handle_api_request("/api/tasks?limit=5", method="GET")

        self.assertEqual(stats_status, 200)
        self.assertEqual(stats_payload["pending_tasks"], 1)
        self.assertEqual(tasks_status, 200)
        self.assertEqual(tasks_payload[0]["text"], "review memory")

    def _isolated_databases(self, temp_dir):
        memory_path = str(Path(temp_dir) / "memory.db")
        communication_path = str(Path(temp_dir) / "communication.db")
        env = {
            "MEMORY_DB_PATH": memory_path,
            "COMMUNICATION_DB_PATH": communication_path,
        }

        class Context:
            def __enter__(self):
                self.patch = patch.dict("os.environ", env, clear=False)
                self.patch.__enter__()
                MemoryStore(Path(memory_path))
                self.communication_store = CommunicationStore(Path(communication_path))
                return self.communication_store

            def __exit__(self, exc_type, exc, tb):
                self.patch.__exit__(exc_type, exc, tb)

        return Context()


if __name__ == "__main__":
    unittest.main()
