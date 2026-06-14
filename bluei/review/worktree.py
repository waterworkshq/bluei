"""Worktree — extracted from review.cycle for single-responsibility.

Worktree lifecycle: prep, remediation, validation, commit/push, cleanup.
"""

from __future__ import annotations

import json
import logging
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional

_logger = logging.getLogger(__name__)

from bluei.app.models import now_iso, ReviewMode
from bluei.review.types import ReviewCycleResult
from bluei.engine.worktree import get_worktree_branch
from bluei.engine.safety_gates import check_push_allowed


class WorktreeMixin:
    def _prepare_worktree(
        self, snapshot: Dict[str, Any], dry_run: bool
    ) -> Dict[str, Any]:
        pr_number = int(snapshot["pr_number"])
        branch = str(snapshot.get("branch") or "")
        path = self._review_worktree_path(pr_number)
        local_branch = f"qa-review-pr-{pr_number}"
        pr_ref = f"refs/remotes/origin/pr/{pr_number}/head"
        if dry_run:
            return {
                "worktree_path": str(path),
                "local_branch": local_branch,
                "prepared": False,
                "dry_run": True,
            }
        if path.exists():
            worktree_branch = get_worktree_branch(path)
            if worktree_branch != local_branch:
                _logger.warning(
                    f"worktree-stale: path={path} expected_branch={local_branch} "
                    f"actual_branch={worktree_branch} — force-recreating"
                )
                self._run_repo_cmd(
                    ["git", "worktree", "remove", "--force", str(path)],
                    cwd=self.provider.repo_path,
                )
            else:
                status_output = self._run_repo_cmd(
                    ["git", "status", "--porcelain"], cwd=path, check=False
                )
                if status_output.strip():
                    _logger.warning(f"worktree-dirty: path={path} — force-recreating")
                    self._run_repo_cmd(
                        ["git", "worktree", "remove", "--force", str(path)],
                        cwd=self.provider.repo_path,
                    )

        if not path.exists():
            fetched = False
            try:
                self._run_repo_cmd(
                    [
                        "git",
                        "fetch",
                        "origin",
                        f"pull/{pr_number}/head:{pr_ref}",
                    ],
                    cwd=self.provider.repo_path,
                )
                fetched = True
            except Exception:
                pass

            start_point = pr_ref
            if not fetched:
                self._run_repo_cmd(
                    ["git", "fetch", "origin", branch], cwd=self.provider.repo_path
                )
                start_point = f"origin/{branch}"

            try:
                self._run_repo_cmd(
                    [
                        "git",
                        "worktree",
                        "add",
                        "-B",
                        local_branch,
                        str(path),
                        start_point,
                    ],
                    cwd=self.provider.repo_path,
                )
            except Exception:
                if start_point != f"origin/{branch}" and branch:
                    self._run_repo_cmd(
                        ["git", "fetch", "origin", branch], cwd=self.provider.repo_path
                    )
                    self._run_repo_cmd(
                        [
                            "git",
                            "worktree",
                            "add",
                            "-B",
                            local_branch,
                            str(path),
                            f"origin/{branch}",
                        ],
                        cwd=self.provider.repo_path,
                    )
                else:
                    raise
        return {
            "worktree_path": str(path),
            "local_branch": local_branch,
            "prepared": True,
            "dry_run": False,
        }

    def _render_remediation_prompt(
        self, snapshot: Dict[str, Any], attempts_used: int
    ) -> str:
        actionables = snapshot.get("actionable_comments", [])
        mnemo_context = self._build_mnemo_review_context(snapshot=snapshot)
        lines = [
            "# QA-Agent Review Remediation Task",
            "",
            f"Repository: {self.repo.config.name}",
            f"PR: #{snapshot.get('pr_number')}",
            f"URL: {snapshot.get('pr_url')}",
            f"Branch: {snapshot.get('branch')}",
            f"Attempt: {attempts_used + 1}/{int(self.repo.config.review_care.get('max_attempts', 3))}",
            "",
            "## Actionable review feedback",
        ]
        if actionables:
            for item in actionables:
                lines.append(
                    f"- ({item.get('author', 'reviewer')}) {item.get('body', '')}"
                )
        else:
            lines.append(
                "- Reviewer requested changes; inspect unresolved review context carefully."
            )
        lines.extend(
            [
                "",
                "## Constraints",
                "- Make the smallest safe change that resolves the review feedback.",
                "- Preserve existing behavior unless the review feedback explicitly requires change.",
                "- Run relevant tests/build/type checks before finishing.",
                "- Do NOT rebase or force-push.",
                "- Exit non-zero if the feedback cannot be resolved safely.",
                "",
                "## Current mode",
                "- This prompt is generated as part of bounded review remediation planning.",
            ]
        )
        if mnemo_context:
            lines.extend(["", mnemo_context.rstrip()])
        return "\n".join(lines) + "\n"

    def _plan_remediation(
        self, snapshot: Dict[str, Any], review_record: Dict[str, Any], dry_run: bool
    ) -> Optional[Dict[str, Any]]:
        attempts_used = int(review_record.get("attempts_used", 0))
        prompt_path = self._prompt_path(int(snapshot["pr_number"]))
        if not dry_run:
            prompt_path.parent.mkdir(parents=True, exist_ok=True)
            prompt_path.write_text(
                self._render_remediation_prompt(snapshot, attempts_used),
                encoding="utf-8",
            )
        backend = self._render_backend_command(prompt_path)
        worktree = self._prepare_worktree(snapshot, dry_run=dry_run)
        return {
            "status": "retry_prepared" if worktree.get("prepared") else "retry_planned",
            "prompt_file": str(prompt_path),
            "planned_at": snapshot["fetched_at"],
            "attempt_number": attempts_used + 1,
            "backend_command": backend,
            "worktree": worktree,
        }

    def _run_shell(
        self, command: str, cwd: Path, timeout_seconds: int = 900
    ) -> Dict[str, Any]:
        try:
            result = subprocess.run(
                ["bash", "-lc", command],
                cwd=str(cwd),
                text=True,
                capture_output=True,
                check=False,
                timeout=timeout_seconds,
            )
            return {
                "returncode": result.returncode,
                "stdout": (result.stdout or "").strip(),
                "stderr": (result.stderr or "").strip(),
                "timed_out": False,
            }
        except subprocess.TimeoutExpired as exc:
            return {
                "returncode": 124,
                "stdout": (exc.stdout or "").strip()
                if isinstance(exc.stdout, str)
                else "",
                "stderr": (exc.stderr or "").strip()
                if isinstance(exc.stderr, str)
                else "",
                "timed_out": True,
            }

    def _collect_changed_files(self, cwd: Path) -> List[str]:
        out = self._run_repo_cmd(["git", "status", "--porcelain"], cwd=cwd, check=False)
        files: List[str] = []
        for line in out.splitlines():
            line = line.strip()
            if not line:
                continue
            files.append(line[3:] if len(line) > 3 else line)
        return files

    def _run_git_result(self, args: List[str], cwd: Path) -> Dict[str, Any]:
        result = subprocess.run(
            ["git", *args],
            cwd=str(cwd),
            text=True,
            capture_output=True,
            check=False,
        )
        return {
            "command": ["git", *args],
            "returncode": result.returncode,
            "stdout": (result.stdout or "").strip()[-2000:],
            "stderr": (result.stderr or "").strip()[-2000:],
        }

    def _cleanup_worktree(self, worktree_path: Path) -> Dict[str, Any]:
        result = subprocess.run(
            ["git", "worktree", "remove", "--force", str(worktree_path)],
            cwd=str(self.provider.repo_path),
            text=True,
            capture_output=True,
            check=False,
        )
        return {
            "returncode": result.returncode,
            "stdout": (result.stdout or "").strip()[-2000:],
            "stderr": (result.stderr or "").strip()[-2000:],
            "removed": result.returncode == 0,
        }

    def _apply_commit_push_boundary(
        self,
        worktree_path: Path,
        snapshot: Dict[str, Any],
        changed_files: List[str],
        allow_review_push: bool,
    ) -> Dict[str, Any]:
        branch = str(snapshot.get("branch") or "")
        result: Dict[str, Any] = {
            "status": "pending_operator_confirmation",
            "allow_review_push": allow_review_push,
            "target_branch": branch,
            "changed_files": list(changed_files),
        }
        if not allow_review_push:
            return result
        if not changed_files:
            result["status"] = "no_changes"
            return result

        add_result = self._run_git_result(
            ["add", "--", *changed_files], cwd=worktree_path
        )
        result["git_add"] = add_result
        if add_result["returncode"] != 0:
            result["status"] = "commit_failed"
            return result

        commit_message = (
            f"bluei: address review feedback for PR #{int(snapshot['pr_number'])}"
        )
        commit_result = self._run_git_result(
            ["commit", "-m", commit_message], cwd=worktree_path
        )
        result["git_commit"] = commit_result
        if commit_result["returncode"] != 0:
            result["status"] = "commit_failed"
            return result

        # Safety gate: block direct pushes to protected branches (F1 coverage)
        safety_config = getattr(self.repo.config, "safety", None) or {}
        push_allowed, block_reason = check_push_allowed(branch, safety_config)
        if not push_allowed:
            result["status"] = "blocked_by_safety_gate"
            result["block_reason"] = block_reason
            result["target_branch"] = branch
            return result

        push_result = self._run_git_result(
            ["push", "origin", f"HEAD:{branch}"], cwd=worktree_path
        )
        result["git_push"] = push_result
        if push_result["returncode"] != 0:
            result["status"] = "push_failed"
            return result

        result["status"] = "pushed"
        if self.repo.config.review_care.get("cleanup_worktrees_after_push", True):
            result["cleanup"] = self._cleanup_worktree(worktree_path)
        return result

    def _run_validation(self, worktree_path: Path) -> Dict[str, Any]:
        commands = self.repo.config.baseline_checks or self._guess_validation_commands()
        if not commands:
            return {"ok": True, "results": [], "reason": "no-validation-configured"}
        results = []
        ok = True
        for cmd in commands:
            result = subprocess.run(
                cmd, cwd=str(worktree_path), text=True, capture_output=True, check=False
            )
            results.append(
                {
                    "command": cmd,
                    "returncode": result.returncode,
                    "stdout": (result.stdout or "").strip()[-2000:],
                    "stderr": (result.stderr or "").strip()[-2000:],
                }
            )
            if result.returncode != 0:
                ok = False
                break
        return {"ok": ok, "results": results, "reason": "completed"}

    def _execute_prepared_remediation(
        self,
        remediation_plan: Dict[str, Any],
        review_record: Dict[str, Any],
        dry_run: bool,
        allow_review_push: bool = False,
        snapshot: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        if not remediation_plan:
            return {"status": "no_plan"}
        attempts_used_prior = int(review_record.get("attempts_used", 0))
        max_attempts = int(self.repo.config.review_care.get("max_attempts", 3))
        if attempts_used_prior >= max_attempts:
            return {
                "status": "retry_exhausted",
                "executed": False,
                "attempts_used": attempts_used_prior,
            }
        worktree_path = Path(
            (remediation_plan.get("worktree") or {}).get("worktree_path", "")
        )
        if dry_run or not worktree_path:
            return {
                "status": remediation_plan.get("status", "retry_planned"),
                "executed": False,
                "attempts_used": attempts_used_prior,
            }
        shell_result = self._run_shell(
            remediation_plan["backend_command"], worktree_path
        )
        changed_files = self._collect_changed_files(worktree_path)
        validation = (
            self._run_validation(worktree_path)
            if shell_result["returncode"] == 0
            else {"ok": False, "results": [], "reason": "backend-failed"}
        )
        attempts_used = attempts_used_prior + 1
        status = "retry_executed"
        push_result: Optional[Dict[str, Any]] = None
        if shell_result.get("timed_out"):
            status = "retry_failed_timeout"
        elif shell_result["returncode"] != 0:
            status = "retry_failed"
        elif not validation.get("ok", False):
            status = "retry_failed_validation"
        elif not changed_files:
            status = "retry_no_changes"
        else:
            push_result = self._apply_commit_push_boundary(
                worktree_path,
                snapshot or {},
                changed_files,
                allow_review_push=allow_review_push,
            )
            if push_result.get("status") == "pending_operator_confirmation":
                status = "retry_pending_push"
            elif push_result.get("status") == "pushed":
                status = "retry_pushed"
            elif push_result.get("status") in {"commit_failed", "push_failed"}:
                status = "retry_failed_push"
        return {
            "status": status,
            "executed": True,
            "attempts_used": attempts_used,
            "backend_result": shell_result,
            "changed_files": changed_files,
            "validation": validation,
            "push_result": push_result,
        }

    def _persist_review_state(
        self,
        active_state: Dict[str, Any],
        review_state: Dict[str, Any],
        result: ReviewCycleResult,
    ) -> None:
        active_state = dict(active_state or {})
        review_state = dict(review_state or {})
        active_state["prs"] = dict(active_state.get("prs", {}))
        review_state["prs"] = dict(review_state.get("prs", {}))
        self.state.save_active_prs(self.repo.config.name, active_state)
        self.state.save_review_state(self.repo.config.name, review_state)
        self._update_status_artifact(result)

    def _update_status_artifact(self, result: ReviewCycleResult) -> None:
        status = self.state.load_state(self.repo.config.name)
        status_file = self.state._get_state_dir(self.repo.config.name) / "status.json"
        data: Dict[str, Any] = {}
        if status_file.exists():
            try:
                with open(status_file) as f:
                    data = json.load(f)
            except Exception:
                data = {}
        data["review_care"] = {
            "enabled": bool(self.repo.config.review_care.get("enabled", True)),
            "provider_order": list(
                self.repo.config.review_care.get("provider_order", ["github"])
            ),
            "active_managed_prs": result.active_prs,
            "review_blocked_prs": result.blocked_prs,
            "retry_eligible_prs": result.retry_eligible_prs,
            "retry_planned_prs": result.retry_planned_prs,
            "retry_prepared_prs": result.retry_prepared_prs,
            "retry_executed_prs": result.retry_executed_prs,
            "retry_failed_prs": result.retry_failed_prs,
            "retry_exhausted_prs": result.retry_exhausted_prs,
            "merge_ready_prs": result.merge_ready_prs,
            "paused_prs": result.paused_prs,
            "last_review_cycle_at": now_iso(),
        }
        status_file.parent.mkdir(parents=True, exist_ok=True)
        with open(status_file, "w") as f:
            json.dump(data, f, indent=2)

    def _run_remediation_cycle(
        self, dry_run: bool = True, allow_review_push: bool = False
    ) -> ReviewCycleResult:
        if not dry_run:
            self.state.append_review_event(
                self.repo.config.name,
                {
                    "event": "remediation-not-implemented",
                    "provider": "github",
                    "details": {
                        "mode": ReviewMode.REMEDIATION.value,
                        "message": (
                            "remediation mode is not yet implemented; "
                            "no observation logic was run"
                        ),
                    },
                },
            )
        return ReviewCycleResult()
