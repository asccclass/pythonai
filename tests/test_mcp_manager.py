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


if __name__ == "__main__":
    unittest.main()
