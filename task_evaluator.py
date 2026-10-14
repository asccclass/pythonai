"""task_evaluator.py - Task Success Verification & Multi-dimensional Evaluator"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field
import json
import math
from pathlib import Path
import re
from typing import Any, Callable


@dataclass
class EvaluationResult:
    success: bool
    score: float  # 0.0 ~ 1.0
    feedback: str
    criteria_met: dict[str, bool] = field(default_factory=dict)
    side_effects: dict[str, Any] = field(default_factory=dict)
    verified: bool = False
    retryable: bool = True
    evidence: list[dict] = field(default_factory=list)


class TaskEvaluator:
    def __init__(
        self,
        workspace_root: Path | str,
        client: Any | None = None,
        model: str | None = None,
        run_test_suite_fn: Callable[..., str] | None = None,
        git_status_fn: Callable[[], str] | None = None,
        git_diff_fn: Callable[..., str] | None = None,
    ) -> None:
        self.workspace_root = Path(workspace_root)
        self.client = client
        self.model = model
        self._run_test_suite = run_test_suite_fn
        self._git_status = git_status_fn
        self._git_diff = git_diff_fn

    # ---------------------------------------------------------
    # Snapshot & Side-effects tracking
    # ---------------------------------------------------------
    def capture_state(self, extra_context: dict[str, Any] | None = None) -> dict[str, Any]:
        """Capture a snapshot of the workspace and custom context to verify side-effects."""
        file_snapshot: dict[str, dict[str, Any]] = {}
        try:
            for path in self.workspace_root.rglob("*"):
                # Ignore git and venv internals
                parts = path.parts
                if any(ignored in parts for ignored in (".git", "venv", "__pycache__", ".pytest_cache", ".system_generated")):
                    continue
                if path.is_file():
                    try:
                        stat = path.stat()
                        rel_path = str(path.relative_to(self.workspace_root))
                        file_snapshot[rel_path] = {"size": stat.st_size, "mtime": stat.st_mtime}
                    except OSError:
                        pass
        except Exception:
            pass

        git_status_output = self._call_git_status()
        return {
            "files": file_snapshot,
            "git_status": git_status_output,
            "extra": dict(extra_context or {}),
        }

    def compare_side_effects(self, pre_state: dict[str, Any], post_state: dict[str, Any]) -> dict[str, Any]:
        """Compare pre- and post-task snapshots to identify side-effects."""
        pre_files = pre_state.get("files", {})
        post_files = post_state.get("files", {})

        created_files = [f for f in post_files if f not in pre_files]
        deleted_files = [f for f in pre_files if f not in post_files]
        modified_files = [
            f
            for f in post_files
            if f in pre_files and (post_files[f]["size"] != pre_files[f]["size"] or post_files[f]["mtime"] != pre_files[f]["mtime"])
        ]

        extra_diff = {}
        pre_extra = pre_state.get("extra", {})
        post_extra = post_state.get("extra", {})
        for k in set(pre_extra) | set(post_extra):
            if pre_extra.get(k) != post_extra.get(k):
                extra_diff[k] = {"before": pre_extra.get(k), "after": post_extra.get(k)}

        return {
            "created_files": created_files,
            "modified_files": modified_files,
            "deleted_files": deleted_files,
            "git_status_changed": pre_state.get("git_status") != post_state.get("git_status"),
            "extra_diff": extra_diff,
        }

    # ---------------------------------------------------------
    # Rule-based Assertions
    # ---------------------------------------------------------
    def is_code_development_task(self, user_goal: str, messages: list[dict[str, Any]]) -> bool:
        """Heuristic check to determine if this task involves writing or modifying code."""
        if re.search(r'pytest|test_[\w.-]+\.py\b',user_goal,re.IGNORECASE) or re.search(
            r'(修復|fix|implement|撰寫|重構|write|建立|實作).{0,80}(程式|code|function|class|\.py\b|\.go\b|\.tsx?\b|\.jsx?\b)',user_goal,re.IGNORECASE):
            return True

        # Check tool calls in messages
        from agent_execution.acceptance import CODE_SUFFIXES
        denied_calls = {msg.get('tool_call_id') for msg in messages
                        if msg.get('role') == 'tool' and self._permission_denied(str(msg.get('content') or ''))}
        for msg in messages:
            tool_calls = msg.get("tool_calls") or []
            for tc in tool_calls:
                call_id = getattr(tc, 'id', None) if not isinstance(tc, dict) else tc.get('id')
                if call_id in denied_calls:
                    continue
                func_name = getattr(tc, "function", None)
                name = getattr(func_name, "name", "") if func_name else tc.get("function", {}).get("name", "")
                if name in {'ast_edit','run_test_suite'}:
                    return True
                if name in {'write_file','edit_file','patch'}:
                    arguments=getattr(func_name,'arguments','{}') if func_name else tc.get('function',{}).get('arguments','{}')
                    try:
                        values=json.loads(arguments)
                        path=values.get('path',values.get('file_path',''))
                        if Path(path).suffix.lower() in CODE_SUFFIXES:return True
                    except (ValueError,TypeError):
                        pass
        return False

    @staticmethod
    def _permission_denied(text: str) -> bool:
        return bool(re.search(r"^\s*(?:Error:\s*)?(?:PermissionError|PolicyDenied|Permission denied|Role\s+['\"]?\w+['\"]?\s+cannot execute)", text, re.IGNORECASE))

    def find_target_test_files(self, user_goal: str, messages: list[dict[str, Any]]) -> list[str]:
        """Locate test targets mentioned in user goal, messages, or existing in workspace."""
        targets: list[str] = []
        test_pattern = re.compile(r"(?:[\w.-]+[/\\])*test_[\w.-]+\.py\b", re.IGNORECASE)

        # 1. From user goal
        for match in test_pattern.findall(user_goal):
            clean_target = match.replace("\\", "/")
            if clean_target not in targets:
                targets.append(clean_target)

        # 2. From tool calls or arguments in messages
        for msg in messages:
            content = str(msg.get("content") or "")
            for match in test_pattern.findall(content):
                clean_target = match.replace("\\", "/")
                if clean_target not in targets:
                    targets.append(clean_target)

            tool_calls = msg.get("tool_calls") or []
            for tc in tool_calls:
                func = getattr(tc, "function", None)
                args_str = getattr(func, "arguments", "") if func else tc.get("function", {}).get("arguments", "")
                for match in test_pattern.findall(args_str):
                    clean_target = match.replace("\\", "/")
                    if clean_target not in targets:
                        targets.append(clean_target)

        return targets

    def verify_file_syntax_and_state(self, file_paths: list[str]) -> tuple[bool, list[str]]:
        """Verify that target files exist, are non-empty, and valid syntax (for Python files)."""
        issues = []
        for file_path in file_paths:
            path = self.workspace_root / file_path if not Path(file_path).is_absolute() else Path(file_path)
            path=path.resolve()
            if not path.is_relative_to(self.workspace_root.resolve()):
                issues.append(f'目標檔案超出 workspace: {file_path}')
                continue
            if not path.exists():
                issues.append(f"目標檔案不存在: {file_path}")
                continue
            if path.stat().st_size == 0:
                issues.append(f"目標檔案為空: {file_path}")
                continue
            if path.suffix == ".py":
                try:
                    code = path.read_text(encoding="utf-8")
                    ast.parse(code, filename=str(path))
                except SyntaxError as e:
                    issues.append(f"Python 語法錯誤 ({file_path}): {e}")
                except Exception as e:
                    issues.append(f"檔案讀取或解析異常 ({file_path}): {e}")
        return len(issues) == 0, issues

    def evaluate_rules(
        self,
        user_goal: str,
        messages: list[dict[str, Any]],
        side_effects: dict[str, Any] | None = None,
        test_target: str | None = None,
        test_results: dict | None = None,
    ) -> tuple[bool, dict[str, bool], list[str]]:
        """Execute multi-dimensional rule-based assertions."""
        criteria: dict[str, bool] = {}
        issues: list[str] = []
        test_results={} if test_results is None else test_results

        is_code_task = self.is_code_development_task(user_goal, messages)
        criteria["is_code_task"] = is_code_task

        # 1. 程式開發任務斷言：強制執行測試套件
        if is_code_task:
            test_targets = [test_target] if test_target else self.find_target_test_files(user_goal, messages)
            from agent_execution.tasks import _active
            active=_active.get()
            contract=active[0].get(active[1],active[2])['criteria'] if active else {}
            for check in contract.get('checks',[]):
                if check.get('kind')=='tests' and check['target'] not in test_targets:test_targets.append(check['target'])
            if active:
                for step in active[0].steps(active[1],active[2]):
                    if step['name']=='run_test_suite':
                        target=json.loads(step['input']).get('test_target')
                        if target and target not in test_targets:test_targets.append(target)
            if test_targets:
                for target in test_targets:
                    from agent_execution.results import normalize_result
                    if target not in test_results:test_results[target]=self._call_run_test_suite(target)
                    execution=normalize_result(test_results[target],'run_test_suite')
                    test_output=str(execution)
                    passed=execution.success and execution.exit_code==0 and execution.evidence.get('tests',{}).get('passed',0)>0
                    has_uncaught_exception = "Traceback (most recent call last)" in test_output
                    criteria[f"test_passed:{target}"] = passed and not has_uncaught_exception
                    if has_uncaught_exception:
                        issues.append(f"測試套件出現未捕獲異常 ({target}):\n{test_output[:400]}")
                    elif not passed:
                        issues.append(f"測試套件未通過 ({target}):\n{test_output[:400]}")
            else:
                # If it's a code task but no specific test target found, check all tests in tests/
                criteria["test_target_specified"] = False
                issues.append('程式任務未提供可驗證的測試目標。')

        # 2. 檔案與工作區狀態檢查
        files_to_check: list[str] = []
        if side_effects:
            files_to_check.extend(side_effects.get("created_files", []))
            files_to_check.extend(side_effects.get("modified_files", []))
        else:
            for msg in messages:
                tool_calls = msg.get("tool_calls") or []
                for tc in tool_calls:
                    func = getattr(tc, "function", None)
                    name = getattr(func, "name", "") if func else tc.get("function", {}).get("name", "")
                    if name in {"write_file", "ast_edit", "edit_file"}:
                        args_str = getattr(func, "arguments", "") if func else tc.get("function", {}).get("arguments", "{}")
                        try:
                            args_dict = json.loads(args_str)
                            fp = args_dict.get("file_path") or args_dict.get("path")
                            if fp and fp not in files_to_check:
                                files_to_check.append(fp)
                        except Exception:
                            pass

        if files_to_check:
            syntax_ok, syntax_issues = self.verify_file_syntax_and_state(files_to_check)
            criteria["syntax_valid"] = syntax_ok
            if not syntax_ok:
                issues.extend(syntax_issues)
        else:
            criteria["syntax_valid"] = True

        # 3. 工作區狀態檢查
        git_status_str = self._call_git_status()
        criteria["git_inspected"] = bool(git_status_str and "Error" not in git_status_str)

        all_rules_passed = len(issues) == 0
        return all_rules_passed, criteria, issues

    # ---------------------------------------------------------
    # LLM-as-a-Judge Track
    # ---------------------------------------------------------
    def evaluate_llm_judge(
        self,
        user_goal: str,
        messages: list[dict[str, Any]],
        final_reply: str,
    ) -> tuple[bool, float, str]:
        """Invoke an independent LLM-as-a-Judge to evaluate task completeness."""
        if self.client is None or not self.model:
            return True, 1.0, "LLM-as-a-Judge 未配置，略過此項。"

        conversation_summary = []
        for msg in messages[-8:]:  # last few exchanges
            role = msg.get("role", "unknown")
            content = str(msg.get("content") or "")[:300]
            conversation_summary.append(f"[{role}]: {content}")

        judge_prompt = f"""You are an impartial and rigorous AI Task Evaluator & Judge.
Evaluate whether the Assistant successfully achieved the User's Goal without errors or omissions.

[User Goal]:
{user_goal}

[Execution Summary]:
{chr(10).join(conversation_summary)}

[Final Assistant Reply]:
{final_reply}

Please output your decision strictly in JSON format with keys:
- "success": true if user goal is completely fulfilled, false otherwise
- "score": float between 0.0 and 1.0
- "feedback": concise explanation of what was achieved or what failed / was missed.
"""
        try:
            from agent_execution.budgets import budget_client
            response = budget_client(self.client).chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": "You are a JSON-only evaluation judge."},
                    {"role": "user", "content": judge_prompt},
                ],
                temperature=0.0,
            )
            raw_text = response.choices[0].message.content or "{}"
            json_match = re.search(r"\{.*\}", raw_text, re.DOTALL)
            if json_match:
                data = json.loads(json_match.group(0))
                score=data.get('score')
                if type(data.get('success')) is not bool or type(score) not in {int,float} or not math.isfinite(score) or not 0<=score<=1 or not isinstance(data.get('feedback'),str):
                    return False,0.0,'Judge unavailable: invalid response schema'
                return data['success'],float(score),data['feedback']
            return False, 0.0, 'Judge unavailable: invalid JSON'
        except Exception as e:
            from agent_execution.budgets import BudgetExhausted
            if isinstance(e,BudgetExhausted):raise
            return False, 0.0, f'Judge unavailable: {type(e).__name__}'

    # ---------------------------------------------------------
    # Unified Evaluation
    # ---------------------------------------------------------
    def evaluate(
        self,
        user_goal: str,
        messages: list[dict[str, Any]],
        final_reply: str,
        pre_state: dict[str, Any] | None = None,
        post_state: dict[str, Any] | None = None,
        test_target: str | None = None,
    ) -> EvaluationResult:
        """Run double-track evaluation: Rule-based Assertions + LLM-as-a-Judge."""
        side_effects = {}
        if pre_state and post_state:
            side_effects = self.compare_side_effects(pre_state, post_state)

        # Track 1: Rule Assertions
        test_results={}
        rules_passed, criteria, issues = self.evaluate_rules(
            user_goal=user_goal,
            messages=messages,
            side_effects=side_effects,
            test_target=test_target,
            test_results=test_results,
        )
        from agent_execution.tasks import _active
        from agent_execution.acceptance import verify_contract
        active=_active.get()
        contract=active[0].get(active[1],active[2])['criteria'] if active else {'version':1,'checks':[]}
        if 'version' not in contract:contract={'version':1,'checks':[]}
        def run_once(target):
            if target not in test_results:test_results[target]=self._call_run_test_suite(target)
            return test_results[target]
        checks,contract_issues,evidence=verify_contract(contract,self.workspace_root,run_once)
        criteria.update(checks)
        issues.extend(contract_issues)
        if active:
            # Executor checkpoints are trusted facts; model prose is never evidence.
            steps=active[0].steps(active[1],active[2])
            for step in steps:
                if step['state']!='completed':
                    issues.append(f"Tool step not successful: {step['id']}")
                    try:
                        result = json.loads(step.get('result') or '{}')
                    except (ValueError, TypeError):
                        result = {}
                    if isinstance(result, dict) and self._permission_denied(str(result.get('error') or '')):
                        issues.append(f"工具權限不足 ({step['name']}): {result['error']}")
        permission_denied = any('PermissionError' in issue or '工具權限不足' in issue or self._permission_denied(issue) for issue in issues) or any(
            msg.get('role') == 'tool' and self._permission_denied(str(msg.get('content') or ''))
            for msg in messages)
        if permission_denied and not any('工具權限不足' in issue for issue in issues):
            issues.append('工具權限不足：請檢查後端 ROLES_CONFIG 的連線身份與角色；權限拒絕無法透過重試修復。')
        rules_passed=rules_passed and not issues

        # Track 2: LLM-as-a-Judge
        llm_passed, llm_score, llm_feedback = self.evaluate_llm_judge(user_goal, messages, final_reply)
        criteria["llm_judge_passed"] = llm_passed

        # Composite Result
        overall_success = rules_passed and llm_passed
        rule_score = 1.0 if rules_passed else (0.5 if not issues else 0.0)
        overall_score = round((rule_score * 0.6) + (llm_score * 0.4), 2) if overall_success else min(rule_score, llm_score)

        feedback_parts = []
        if not rules_passed:
            feedback_parts.extend(issues)
        if not llm_passed:
            feedback_parts.append(f"LLM Judge 判定未通過: {llm_feedback}")

        final_feedback = "驗收通過。" if overall_success else "\n".join(feedback_parts)

        return EvaluationResult(
            success=overall_success,
            score=1.0 if overall_success else overall_score,
            feedback=final_feedback,
            criteria_met=criteria,
            side_effects=side_effects,
            verified=overall_success and bool(checks) and all(checks.values()),
            retryable=not llm_feedback.startswith('Judge unavailable:') and not permission_denied,
            evidence=evidence,
        )

    # ---------------------------------------------------------
    # Tool Invocation Helpers
    # ---------------------------------------------------------
    def _call_run_test_suite(self, target: str) -> str:
        if self._run_test_suite:
            return self._run_test_suite(target)
        try:
            from tools.run_test_suite import run_test_suite
            return run_test_suite(target)
        except Exception as e:
            return f"Error executing run_test_suite: {e}"

    def _call_git_status(self) -> str:
        if self._git_status:
            return self._git_status()
        try:
            from tools.git_status import git_status
            return git_status()
        except Exception:
            return ""

    def _call_git_diff(self, staged: bool = False, file_path: str = "") -> str:
        if self._git_diff:
            return self._git_diff(staged=staged, file_path=file_path)
        try:
            from tools.git_diff import git_diff
            return git_diff(staged=staged, file_path=file_path)
        except Exception:
            return ""
