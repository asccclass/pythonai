"""Opt-in tests against a running Docker daemon, not simulated sandbox checks."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from sandbox import SandboxExecution


@unittest.skipUnless(os.environ.get("PYTHONAI_DOCKER_TEST") == "1", "Set PYTHONAI_DOCKER_TEST=1 for Docker integration")
class DockerIntegrationTests(unittest.TestCase):
    def test_real_container_limits_secrets_network_writes_and_exports_artifacts(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".env").write_text("SECRET=do-not-copy", encoding="utf-8")
            (root / "private.key").write_text("do-not-copy", encoding="utf-8")
            (root / "check.py").write_text('''
import os, socket
from pathlib import Path
assert not Path('/workspace/.env').exists()
assert not Path('/workspace/private.key').exists()
assert 'TEST_PROVIDER_SECRET' not in os.environ
for destination in ('/workspace/unsafe.txt', '/etc/unsafe.txt'):
    try:
        Path(destination).write_text('unsafe')
    except OSError:
        pass
    else:
        raise AssertionError('Unexpected write access')
try:
    socket.create_connection(('1.1.1.1', 80), timeout=1)
except OSError:
    pass
else:
    raise AssertionError('Unexpected network access')
cgroup = Path('/sys/fs/cgroup')
if (cgroup / 'memory.max').exists():
    assert (cgroup / 'memory.max').read_text().strip() == '268435456'
    assert (cgroup / 'pids.max').read_text().strip() == '64'
    assert (cgroup / 'cpu.max').read_text().split() == ['100000', '100000']
else:
    assert (cgroup / 'memory/memory.limit_in_bytes').read_text().strip() == '268435456'
    assert (cgroup / 'pids/pids.max').read_text().strip() == '64'
    assert (cgroup / 'cpu/cpu.cfs_quota_us').read_text().strip() == '100000'
    assert (cgroup / 'cpu/cpu.cfs_period_us').read_text().strip() == '100000'
Path('/artifacts/report.txt').write_text('verified')
print('isolation verified')
''', encoding="utf-8")
            sandbox = SandboxExecution(root)
            self.assertTrue(sandbox.has_docker)
            sandbox.setup()
            try:
                result = sandbox.run(["python", "/workspace/check.py"],
                                     env={**os.environ, "TEST_PROVIDER_SECRET": "private"}, timeout=20)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("isolation verified", result.stdout)
                sandbox.export_artifacts(root / "results")
                self.assertEqual((root / "results" / "report.txt").read_text(), "verified")
                self.assertFalse((root / "unsafe.txt").exists())
            finally:
                sandbox.cleanup()

    def test_real_container_timeout_leaves_no_container(self):
        with tempfile.TemporaryDirectory() as directory:
            sandbox = SandboxExecution(Path(directory))
            sandbox.setup()
            try:
                result = sandbox.run(["python", "-c", "import time; time.sleep(30)"], timeout=1)
                self.assertEqual(result.returncode, 124, result.stderr)
                inspect = subprocess.run(["docker", "inspect", sandbox.container_name], capture_output=True, text=True)
                self.assertNotEqual(inspect.returncode, 0)
            finally:
                sandbox.cleanup()
