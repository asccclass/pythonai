import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mcp_manager import MCPManager


class MCPManagerTests(unittest.TestCase):
    def test_load_config_passes_cwd_to_client(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "mcp_config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "mcpServers": {
                            "aisms": {
                                "command": "D:\\myprograms\\aisms\\ais-mcp.exe",
                                "args": [],
                                "cwd": "D:\\myprograms\\aisms",
                                "env": {
                                    "DB_PATH": "D:\\myprograms\\aisms\\data\\isms.db"
                                },
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )

            with patch("mcp_manager.MCPClient") as client_class:
                manager = MCPManager(str(config_path))
                manager.load_config_and_start()

            client_class.assert_called_once_with(
                "aisms",
                "D:\\myprograms\\aisms\\ais-mcp.exe",
                [],
                {"DB_PATH": "D:\\myprograms\\aisms\\data\\isms.db"},
                "D:\\myprograms\\aisms",
            )
            client_class.return_value.start.assert_called_once_with()

    def test_load_config_skips_failed_server_without_printing_error(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "mcp_config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "mcpServers": {
                            "aisms": {
                                "command": "missing-mcp.exe",
                                "args": [],
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )

            with (
                patch("mcp_manager.MCPClient") as client_class,
                patch("builtins.print") as print_mock,
            ):
                client_class.return_value.start.side_effect = FileNotFoundError(
                    2,
                    "系統找不到指定的檔案。",
                    "missing-mcp.exe",
                )
                manager = MCPManager(str(config_path))
                manager.load_config_and_start()

            self.assertEqual(manager.clients, {})
            self.assertIn("aisms", manager.skipped_servers)
            self.assertIn("missing-mcp.exe", manager.skipped_servers["aisms"])
            print_mock.assert_not_called()
            client_class.return_value.stop.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
