"""Harness² BenchmarkAdapter seam (requires the separately installed harness2)."""
from pathlib import Path

from .runner import ProbeRunner, read_json, trajectory, validate_harness, validate_task


class PythonAISubstrate:
    name = 'pythonai'

    def surface(self):
        from harness2.core.tree import SurfaceSpec
        return SurfaceSpec(
            name=self.name,
            always_on=('systemprompt.md', 'guardrails.md', 'memory.md', 'tool_guidance.md'),
            always_on_max=100000, always_on_cap_reason='Bounded probe prompt',
            skill_dir='skills', skill_name_rule='lowercase', trigger_hint='',
            autotrigger_file='tool_guidance.md', trigger_line='',
            subagent_requires_mode=False, plugin_dir=None, hooks_file=None,
        )


class PythonAIAdapter:
    """Task manifest + execution bridge; scoring stays with an external judge."""

    def __init__(self, manifest, root, *, channels, timeout=300, judge=None, python=None):
        from harness2.core.spec import BenchPrompts, BenchSpec
        manifest = Path(manifest).resolve()
        rows = read_json(manifest)
        if not isinstance(rows, list) or not rows:
            raise ValueError('Manifest must contain a nonempty task list')
        self._tasks = {}
        for row in rows:
            validate_task(row)
            row = dict(row)
            if row['id'] in self._tasks:
                raise ValueError('Duplicate task id: ' + row['id'])
            if row.get('files_dir'):
                row['files_dir'] = str((manifest.parent / row['files_dir']).resolve())
            self._tasks[row['id']] = row
        self.root = Path(root).resolve()
        self.timeout = timeout
        self.judge = judge
        self.substrate = PythonAISubstrate()
        self.spec = BenchSpec(name='pythonai', runs_root=self.root / 'runs',
                              install_mode='workspace_config',
                              prompts=BenchPrompts(work='complete the requested professional task'),
                              channels=channels)
        self.runner = ProbeRunner(self.root / 'executions', **({'python': python} if python else {}))
        base = self.root / 'base_harness'
        base.mkdir(parents=True, exist_ok=True)
        for name in self.substrate.surface().always_on:
            path = base / name
            if not path.exists():
                path.write_text('', encoding='utf-8')

    def tasks(self, split='all'):
        return [key for key, row in self._tasks.items() if split == 'all' or row.get('split', 'all') == split]

    def domain_of(self, task_id):
        return self._tasks[task_id].get('domain', 'general')

    def task_prompt(self, task_id):
        return self._tasks[task_id]['prompt']

    def task_files(self, task_id):
        value = self._tasks[task_id].get('files_dir')
        return Path(value) if value else None

    def base_harness(self):
        return self.root / 'base_harness'

    def validate_tree(self, tree):
        validate_harness({name: value.encode('utf-8') for name, value in tree.items()})

    def execute(self, task_id, harness_dir, run_id, *, timeout=None):
        return self.runner.execute(self._tasks[task_id],
                                   self.base_harness() if harness_dir is None else harness_dir,
                                   run_id, timeout=self.timeout if timeout is None else timeout)

    def delivered(self, run_dir):
        result = read_json(Path(run_dir) / 'result.json')
        return result['status'] == 'completed' and bool(result['final_output'].strip() or result['artifacts'])

    def trajectory(self, run_dir, *, obs_trunc):
        return trajectory(run_dir, obs_trunc=obs_trunc)

    def deliverable_text(self, run_dir):
        return read_json(Path(run_dir) / 'result.json')['final_output']

    def deliverable_files(self, run_dir):
        # Binary artifacts remain available on disk with hashes in result.json.
        result = read_json(Path(run_dir) / 'result.json')
        files = {'answer.txt': result['final_output']} if result['final_output'] else {}
        for entry in result['artifacts']:
            path = (Path(run_dir) / 'workspace' / entry['path']).resolve()
            if not path.is_relative_to((Path(run_dir) / 'workspace').resolve()):
                raise ValueError('Artifact escaped workspace')
            try:
                files['workspace/' + entry['path']] = path.read_text(encoding='utf-8')
            except UnicodeError:
                continue
        return files

    def score(self, run_dir, task_id):
        return self.judge(Path(run_dir), task_id) if self.judge else None
