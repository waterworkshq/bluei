"""Generation — extracted from review.cycle for single-responsibility.

Backend candidate generation: LLM backend invocation, local stub fallback.
"""

from __future__ import annotations

import json
import logging
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional

_logger = logging.getLogger(__name__)

from bluei.app.models import now_iso
from bluei.review.models import (
    FindingSource,
    FindingActionability,
    FindingSeverity,
)
from bluei.review.types import CandidateValidationError
from bluei.review.normalization import normalize_candidate


class GenerationMixin:
    def _generate_from_backend(
        self,
        run_id: str,
        pr_context: Optional[Dict[str, Any]] = None,
    ) -> List[Dict[str, Any]]:
        candidates: List[Dict[str, Any]] = []

        claude_template = self.repo.config.review_claude_template
        opencode_template = self.repo.config.review_opencode_template

        if not claude_template and not opencode_template:
            self.state.append_review_event(
                self.repo.config.name,
                {
                    "event": "backend-generation skipped",
                    "run_id": run_id,
                    "reason": "no_review_backend_configured",
                    "provider": "local-stub",
                },
            )
            return candidates

        backend = self._resolve_backend()
        if backend not in ("claude", "opencode"):
            self.state.append_review_event(
                self.repo.config.name,
                {
                    "event": "backend-generation skipped",
                    "run_id": run_id,
                    "reason": f"backend_{backend}_not_available",
                    "provider": "local-stub",
                },
            )
            return candidates

        template = (
            claude_template
            if backend == "claude" and claude_template
            else opencode_template
        )

        prompts_dir = self.state.get_review_prompts_dir(self.repo.config.name)
        prompts_dir.mkdir(parents=True, exist_ok=True)
        prompt_artifact_path = prompts_dir / f"backend-candidates-{run_id}.md"

        prompt_artifact_content = self._build_candidate_prompt_artifact(
            pr_context=pr_context
        )
        prompt_artifact_path.write_text(prompt_artifact_content, encoding="utf-8")

        try:
            raw_output = self._run_backend_candidate_command(
                backend=backend,
                template=template,
                prompt_file=prompt_artifact_path,
            )
        except Exception as exc:
            self.state.append_review_event(
                self.repo.config.name,
                {
                    "event": "backend-generation failed",
                    "run_id": run_id,
                    "backend": backend,
                    "error": str(exc),
                    "provider": "local-stub",
                    "details": {
                        "prompt_artifact": str(prompt_artifact_path),
                        "fallback": "local_stub_engaged",
                    },
                },
            )
            return candidates

        if not raw_output.strip():
            self.state.append_review_event(
                self.repo.config.name,
                {
                    "event": "backend-generation empty",
                    "run_id": run_id,
                    "backend": backend,
                    "provider": "local-stub",
                    "details": {
                        "prompt_artifact": str(prompt_artifact_path),
                        "fallback": "local_stub_engaged",
                    },
                },
            )
            return candidates

        try:
            parsed = json.loads(raw_output)
        except json.JSONDecodeError as jexc:
            self.state.append_review_event(
                self.repo.config.name,
                {
                    "event": "backend-generation invalid-json",
                    "run_id": run_id,
                    "backend": backend,
                    "json_error": str(jexc),
                    "provider": "local-stub",
                    "details": {
                        "prompt_artifact": str(prompt_artifact_path),
                        "fallback": "local_stub_engaged",
                    },
                },
            )
            return candidates

        raw_candidates: List[Dict[str, Any]] = []
        if isinstance(parsed, dict):
            raw_candidates = parsed.get("findings", [])
            if not isinstance(raw_candidates, list):
                raw_candidates = []
        elif isinstance(parsed, list):
            raw_candidates = parsed
        else:
            self.state.append_review_event(
                self.repo.config.name,
                {
                    "event": "backend-generation unexpected-type",
                    "run_id": run_id,
                    "backend": backend,
                    "type": type(parsed).__name__,
                    "provider": "local-stub",
                    "details": {
                        "prompt_artifact": str(prompt_artifact_path),
                        "fallback": "local_stub_engaged",
                    },
                },
            )
            return candidates

        validated: List[Dict[str, Any]] = []
        for raw in raw_candidates:
            try:
                normalized = normalize_candidate(raw)
                validated.append(normalized)
            except CandidateValidationError:
                continue

        if not validated:
            self.state.append_review_event(
                self.repo.config.name,
                {
                    "event": "backend-generation no-valid-candidates",
                    "run_id": run_id,
                    "backend": backend,
                    "raw_count": len(raw_candidates),
                    "provider": "local-stub",
                    "details": {
                        "prompt_artifact": str(prompt_artifact_path),
                        "fallback": "local_stub_engaged",
                    },
                },
            )
            return candidates

        self.state.append_review_event(
            self.repo.config.name,
            {
                "event": "backend-generation succeeded",
                "run_id": run_id,
                "backend": backend,
                "candidate_count": len(validated),
                "provider": "backend",
                "details": {
                    "prompt_artifact": str(prompt_artifact_path),
                },
            },
        )

        return validated

    def _build_candidate_prompt_artifact(
        self,
        pr_context: Optional[Dict[str, Any]] = None,
    ) -> str:
        repo_name = self.repo.config.name
        repo_path = self.repo.config.path
        language = self.repo.config.language or "unknown"

        pr_section = ""
        if pr_context and pr_context.get("pr_number") is not None:
            pr_num = pr_context["pr_number"]
            pr_section = (
                f"\n"
                f"## PR Context\n"
                f"- PR Number: #{pr_num}\n"
                f"- This review run is targeting PR #{pr_num}.\n"
                f"- Focus analysis on files changed in this PR and their interactions.\n"
            )
        mnemo_section = self._build_mnemo_review_context(pr_context=pr_context)

        return (
            f"# Autonomous Review Candidate Generation\n"
            f"\n"
            f"## Repository\n"
            f"- Name: {repo_name}\n"
            f"- Path: {repo_path}\n"
            f"- Language: {language}\n"
            f"{pr_section}"
            f"{mnemo_section}"
            f"\n"
            f"## Task\n"
            f"Scan the repository at `{repo_path}` and identify candidate findings "
            f"suitable for autonomous review. Each finding should represent a "
            f"verifiable quality or correctness issue.\n"
            f"\n"
            f"## Output Format\n"
            f"Output ONLY a valid JSON array of finding objects. Each object must "
            f"have these fields:\n"
            f"- repo (string): repository name\n"
            f"- path (string): relative file path\n"
            f"- line (integer): line number\n"
            f"- header (string): short finding type identifier\n"
            f"- snippet (string): relevant code/text excerpt (max 200 chars)\n"
            f"- source (string): one of linter, ai, manual, unknown\n"
            f"- actionability (string): informational, low, medium, high\n"
            f"- severity (string): none, low, medium, high, critical\n"
            f"- confidence (float): 0.0 to 1.0\n"
            f"- safe_to_autofix (boolean): whether auto-fix is safe\n"
            f"- discovered_at (string): ISO timestamp\n"
            f"\n"
            f"Example:\n"
            f"```json\n"
            f'[{{"repo": "{repo_name}", "path": "src/main.ts", "line": 10, '
            f'"header": "outstanding-todo", "snippet": "# TODO: fix this", '
            f'"source": "linter", "actionability": "medium", '
            f'"severity": "low", "confidence": 0.7, "safe_to_autofix": false, '
            f'"discovered_at": "2026-03-29T00:00:00Z"}}]\n'
            f"```\n"
            f"\n"
            f"Output nothing else. Start directly with the JSON array."
        )

    def _run_backend_candidate_command(
        self,
        backend: str,
        template: str,
        prompt_file: Path,
    ) -> str:
        cmd_str = template.format(prompt_file=str(prompt_file))

        result = subprocess.run(
            cmd_str,
            shell=True,
            cwd=str(self.provider.repo_path),
            text=True,
            capture_output=True,
            check=False,
        )

        if result.returncode != 0:
            stderr = (result.stderr or "").strip()
            stdout = (result.stdout or "").strip()
            raise RuntimeError(
                f"backend command exited {result.returncode}; "
                f"stderr: {stderr or 'none'}; stdout: {stdout or 'none'}"
            )

        return result.stdout or ""

    def _generate_local_candidates(self) -> List[Dict[str, Any]]:
        candidates: List[Dict[str, Any]] = []
        repo_path = Path(self.repo.config.path)
        if not repo_path.exists():
            return candidates

        for py_file in repo_path.rglob("*.py"):
            try:
                for i, line in enumerate(
                    py_file.read_text(encoding="utf-8").splitlines(), start=1
                ):
                    stripped = line.strip()
                    marker = None
                    if stripped.startswith("# TODO:"):
                        marker = "outstanding-todo"
                    elif stripped.startswith("# FIXME:"):
                        marker = "unresolved-fixme"
                    elif stripped.startswith("# BUG:") or stripped.startswith("# BUG:"):
                        marker = "documented-bug"
                    elif "raise NotImplementedError" in stripped:
                        marker = "not-implemented-raise"
                    elif "pass  # TODO" in stripped or "..." in stripped:
                        if i > 1:
                            marker = "code-placeholder"

                    if marker:
                        snippet = stripped[:120]
                        candidates.append(
                            {
                                "repo": self.repo.config.name,
                                "path": str(py_file.relative_to(repo_path)),
                                "line": i,
                                "header": marker,
                                "snippet": snippet,
                                "source": FindingSource.LINTER.value,
                                "actionability": FindingActionability.MEDIUM.value,
                                "severity": FindingSeverity.LOW.value,
                                "confidence": 0.7,
                                "safe_to_autofix": False,
                                "discovered_at": now_iso(),
                            }
                        )
            except (OSError, UnicodeDecodeError):
                continue

        for src_file in repo_path.rglob("*.py"):
            try:
                lines = src_file.read_text(encoding="utf-8").splitlines()
                for i, line in enumerate(lines, start=1):
                    if len(line) > 120 and not line.strip().startswith("#"):
                        candidates.append(
                            {
                                "repo": self.repo.config.name,
                                "path": str(src_file.relative_to(repo_path)),
                                "line": i,
                                "header": "excessively-long-line",
                                "snippet": line[:120],
                                "source": FindingSource.LINTER.value,
                                "actionability": FindingActionability.LOW.value,
                                "severity": FindingSeverity.LOW.value,
                                "confidence": 0.6,
                                "safe_to_autofix": True,
                                "discovered_at": now_iso(),
                            }
                        )
            except (OSError, UnicodeDecodeError):
                continue

        return candidates
