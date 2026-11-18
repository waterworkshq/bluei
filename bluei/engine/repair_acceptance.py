"""Minimum candidate checks shared by repair lanes; acceptance still owns durability."""

import hashlib
import os
import re
import resource
import subprocess
import tempfile
from pathlib import Path
from uuid import uuid4

from bluei.engine.constants import QA_FIX_PROMPT_FILENAME
from bluei.engine.git_ops import diff_stats
from bluei.engine.validation import (
    build_target_checks,
    choose_validation_baseline,
    run_named_checks,
    run_validation_gate,
)
from bluei.engine.verify import verify_fix_closed


def prepare_baseline(*, worktree_path, source_repo, checks, repo_baseline_results,
                     allow_unchanged_baseline_failures, log_file):
    observed = run_named_checks(
        repo_path=worktree_path, checks=checks, log_file=log_file,
        phase="worktree-baseline", environment_repo=source_repo,
    )
    baseline = choose_validation_baseline(
        repo_baseline_results=repo_baseline_results,
        worktree_baseline_results=observed, log_file=log_file,
    )
    gate = run_validation_gate(
        repo_path=worktree_path, worktree_path=worktree_path, checks=checks,
        baseline_results=baseline, post_fix_results=baseline, target_results={},
        allow_unchanged_baseline_failures=allow_unchanged_baseline_failures,
        log_file=log_file,
    )
    return baseline, gate


def validate_candidate(*, worktree_path, source_repo, candidate_base, findings,
                       checks, baseline_results, allow_unchanged_baseline_failures,
                       max_files_changed, max_loc_diff, log_file,
                       docs_index_file=None, discovery_scope="repository"):
    result = {"passed": False, "qualified": False, "message": "",
              "files_changed": 0, "loc_diff": 0}
    if not findings or not candidate_base:
        result["message"] = "candidate identity unavailable"
        return result
    try:
        current_head = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=worktree_path,
            capture_output=True, text=True, timeout=60, check=True,
        ).stdout.strip()
        if current_head != candidate_base:
            result["message"] = "candidate-changed-head"
            return result
        files_changed, loc_diff = diff_stats(worktree_path)
    except (OSError, ValueError, subprocess.SubprocessError):
        result["message"] = "candidate-diff-unavailable"
        return result
    result.update(files_changed=files_changed, loc_diff=loc_diff)
    if files_changed == 0 and loc_diff == 0:
        result["message"] = "candidate-no-change"
        return result
    if files_changed > max_files_changed or loc_diff > max_loc_diff:
        result["message"] = "scope-limit-exceeded"
        return result
    try:
        candidate_fingerprint = candidate_signature(worktree_path)
    except (OSError, ValueError, subprocess.SubprocessError):
        result["message"] = "candidate-diff-unavailable"
        return result
    post = run_named_checks(
        repo_path=worktree_path, checks=checks, log_file=log_file,
        phase="post-fix", environment_repo=source_repo,
    )
    targets = {}
    for index, finding in enumerate(findings):
        for name, command in build_target_checks(finding).items():
            targets[f"{index}:{name}"] = command
    target_results = run_named_checks(
        repo_path=worktree_path, checks=targets, log_file=log_file,
        phase="target-check", environment_repo=source_repo,
    ) if targets else {}
    gate = run_validation_gate(
        repo_path=worktree_path, worktree_path=worktree_path, checks=checks,
        baseline_results=baseline_results, post_fix_results=post,
        target_results=target_results,
        allow_unchanged_baseline_failures=allow_unchanged_baseline_failures,
        log_file=log_file,
    )
    result.update(qualified=bool(gate.get("qualified")), validation=gate)
    if not gate.get("passed") or not gate.get("qualified"):
        result["message"] = gate.get("message") or "validation-unqualified"
        return result
    for finding in findings:
        closure = verify_fix_closed(
            worktree_path, finding, log_file, docs_index_file=docs_index_file,
            discovery_scope=discovery_scope,
        )
        if not closure.is_closed:
            result["message"] = "detector-still-firing"
            return result
    try:
        if candidate_signature(worktree_path) != candidate_fingerprint:
            result["message"] = "candidate-changed-during-validation"
            return result
    except (OSError, ValueError, subprocess.SubprocessError):
        result["message"] = "candidate-diff-unavailable"
        return result
    result.update(passed=True, message=gate.get("message", ""), candidate_signature=candidate_fingerprint)
    return result


def candidate_signature(worktree_path):
    """Pin the checked tracked bytes, index and HEAD across validators/rescans."""
    return _capture_candidate(worktree_path)[0]


def _capture_candidate(worktree_path):
    signature = hashlib.sha256()
    captured = []
    for command in (
        ["git", "rev-parse", "HEAD"],
        ["git", "diff", "--no-ext-diff", "--no-textconv", "--binary", "HEAD"],
        ["git", "diff", "--no-ext-diff", "--no-textconv", "--binary", "--cached"],
    ):
        with tempfile.TemporaryFile() as output:
            subprocess.run(
                command, cwd=worktree_path, stdout=output,
                stderr=subprocess.DEVNULL, timeout=60, check=True,
                preexec_fn=lambda: resource.setrlimit(resource.RLIMIT_FSIZE, ((1 << 20) + 1,) * 2),
            )
            output.seek(0)
            data = output.read((1 << 20) + 1)
        if len(data) > (1 << 20):
            raise ValueError("candidate exceeds bounded patch representation")
        signature.update(len(data).to_bytes(8, "big"))
        signature.update(data)
        captured.append(data)
    return signature.hexdigest(), captured[1]


def retain_candidate_patch(*, worktree_path, output_dir, identity, prefix="local-fix",
                           expected_signature=None):
    if expected_signature is not None and candidate_signature(worktree_path) != expected_signature:
        raise ValueError("candidate changed after validation")
    if not re.fullmatch(r"[a-z][a-z0-9-]*", prefix):
        raise ValueError("invalid patch prefix")
    untracked = subprocess.run(
        ["git", "ls-files", "--others", "-z"], cwd=worktree_path,
        capture_output=True, timeout=60, check=True,
    )
    if any(path for path in untracked.stdout.split(b"\0")
           if path and path != QA_FIX_PROMPT_FILENAME.encode()):
        raise ValueError("repair contains new files not represented by its tracked-file patch")
    # Capture and fingerprint the same patch bytes that will be persisted.
    captured_signature, patch = _capture_candidate(worktree_path)
    if not patch.strip():
        raise ValueError("repair patch is empty")
    if expected_signature is not None and captured_signature != expected_signature:
        raise ValueError("candidate changed during patch retention")
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    key = hashlib.sha256(identity.encode()).hexdigest()
    path = output_dir / f"{prefix}-{key}-{uuid4().hex}.patch"
    with path.open("xb") as stream:
        stream.write(patch)
        stream.flush()
        os.fsync(stream.fileno())
    directory = os.open(output_dir, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)
    return path
