"""Compatibility entry point for the authenticated IDE proposal service."""
from agent_execution.ide.server import IDERequestHandler, run_ide_server

if __name__ == "__main__":
    run_ide_server()
