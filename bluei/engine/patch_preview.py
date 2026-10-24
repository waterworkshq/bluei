"""bluei.engine.patch_preview — bounded local patch preview (first-run proof).

Produces a persisted, inspectable ``fix.patch`` + ``report.json`` for the
F401 (unused-import) Ruff rule on ONE tracked regular Python file of a
registered repository, without modifying the source checkout and without any
remote, LLM, PR, or learned-state activity. Trusted fixed tool on trusted
code — not a sandbox. Boundary details: docs/plans/FIRST-RUN-READINESS.md
and docs/reference/CLI_REFERENCE.md § fix-preview. Safety invariants:

- Source worktree must be clean and committed. Only four read-only git
  commands touch it (toplevel, HEAD, ``status --porcelain``, one
  ``ls-files -s -z``), each with a whitelisted environment (every inherited
  ``GIT_*`` variable dropped; user/system config /dev/null; optional locks
  disabled) and ``-c core.hooksPath= -c core.fsmonitor=false`` overrides, so
  no repository/user-configured hook, filter, or fsmonitor can execute.
- The fix runs in a disposable ``git clone --local --no-hardlinks
  --template=<empty>`` — a local object copy with no transport (no
  upload-pack), no hooks directory, no repo-local config. The clone HEAD is
  pinned via ``checkout --detach <baseline HEAD>`` and verified; a
  concurrent source move is rejected, never silently mis-patched.
- Discovery runs in the clone over the explicitly listed tracked regular
  ``*.py`` files only (NUL-parsed paths). Fix command is pinned:
  ``ruff check --isolated --no-cache --fix --no-unsafe-fixes --select F401
  -- <file>``. git and Ruff are resolved to absolute executable paths before
  the clone exists (relative PATH entries are rejected), and every child
  PATH is stripped of relative/empty entries so a bare tool name can never
  resolve against the clone's directory — a tracked file can never be
  executed as a tool. Diff uses ``--no-ext-diff --no-textconv`` and is
  persisted as raw bytes via exclusive create, before the clone is removed.
  On artifact write failure the clone is retained as recovery evidence and
  the command exits non-zero — the only copy of a verified fix is never
  silently lost.
"""

from __future__ import annotations

import ast
import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from bluei.engine.models import now_iso
from bluei.engine.state_io import atomic_json_write

# ── Public constants ────────────────────────────────────────────────────────

EXIT_OK = 0  # patch produced, or verified no-op (no findings)
EXIT_ERROR = 1  # tool failure, validation failure, artifact write failure
EXIT_REJECTED = 2  # precondition/usage rejection — nothing was attempted

STATUS_PATCH_PRODUCED = "patch_produced"
STATUS_NO_FINDINGS = "no_findings"
STATUS_NO_SAFE_FIX = "no_safe_fix"
STATUS_VALIDATION_FAILED = "validation_failed"
STATUS_TOOL_ERROR = "tool_error"
STATUS_ARTIFACT_WRITE_FAILED = "artifact_write_failed"

# The first proof is F401 (unused-import) only: one rule with a long-stable,
# safe-class deterministic autofix in Ruff. Anything else is rejected before
# any tool runs — a CLI-provided rule is never passed through to Ruff
# unvetted. Widening this allowlist is an explicit product decision.
ALLOWED_RULES: Dict[str, str] = {"F401": "unused-import"}

_RULE_RE = re.compile(r"^[A-Z]{1,4}[0-9]{3,4}$")

# Keep the first proof bounded: repositories larger than this are rejected.
MAX_TRACKED_PY_FILES = 5000
# Discovery is invoked with explicit tracked-file arguments; chunk the
# argument list so command length stays bounded on large repositories.
_DISCOVER_CHUNK_FILES = 100
_DISCOVER_CHUNK_BYTES = 100_000

_TOOL_TIMEOUT = 120  # seconds, per git/ruff invocation (module-level: tests override)
REPORT_SCHEMA = "bluei.fix-preview.report/1"


class PatchPreviewError(Exception):
    """Precondition rejection — nothing was attempted against the repo."""

    def __init__(self, message: str, exit_code: int = EXIT_REJECTED):
        super().__init__(message)
        self.exit_code = exit_code


class _ToolFailure(Exception):
    """The fixed tool operation failed unexpectedly (ruff/git crash, timeout)."""


# ── Process helpers ─────────────────────────────────────────────────────────


def _sanitized_path() -> str:
    """PATH for child processes: absolute entries only.

    Relative and empty PATH entries are stripped: a child running with
    ``cwd`` inside the clone would resolve bare tool names against the
    clone's directory — executing a tracked file. Applied to both git and
    Ruff children (git may itself spawn PATH-lookup subprocesses).
    """
    parts = (os.environ.get("PATH") or "").split(os.pathsep)
    return os.pathsep.join(p for p in parts if p and os.path.isabs(p))


def _git_env() -> Dict[str, str]:
    """Isolated environment for every git invocation.

    Whitelist approach: ALL inherited ``GIT_*`` variables are dropped
    (GIT_CONFIG_COUNT/KEY_*/VALUE_*/PARAMETERS config injection,
    GIT_EXTERNAL_DIFF, GIT_EXEC_PATH, GIT_SSH*, *_OBJECT_DIRECTORY, and any
    future addition), PATH is stripped of relative/empty entries, then the
    four values we want are set explicitly.
    """
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env["PATH"] = _sanitized_path()
    env["GIT_CONFIG_GLOBAL"] = os.devnull
    env["GIT_CONFIG_SYSTEM"] = os.devnull
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_OPTIONAL_LOCKS"] = "0"
    return env


def _tool_env() -> Dict[str, str]:
    """Environment for Ruff invocations (absolute-entry PATH only)."""
    env = dict(os.environ)
    env["PATH"] = _sanitized_path()
    return env


def _resolve_tool(name: str) -> str:
    """Resolve a tool to an ABSOLUTE executable path, or reject.

    A relative PATH entry makes ``shutil.which`` return a relative path that
    a child process would re-resolve against its own cwd — inside the clone
    that would execute a tracked file. Unresolvable or non-absolute
    resolutions are rejected outright.
    """
    found = shutil.which(name)
    if found is None:
        raise PatchPreviewError(f"{name} is not available on PATH")
    if not os.path.isabs(found):
        raise PatchPreviewError(
            f"{name} resolved to the non-absolute path {found!r} — "
            "relative PATH entries are not trusted by fix-preview"
        )
    return found


# Command-line config overrides (highest precedence) so repository-local or
# otherwise inherited config cannot enable hooks or fsmonitor execution.
_GIT_C_OVERRIDES = ("-c", "core.hooksPath=", "-c", "core.fsmonitor=false")


def _run(
    cmd: List[str],
    cwd: Path,
    *,
    env: Dict[str, str],
    timeout: Optional[int] = None,
    text: bool = True,
):
    """Run a command with an argument list (never a shell string)."""
    if timeout is None:
        timeout = _TOOL_TIMEOUT
    try:
        res = subprocess.run(
            cmd,
            cwd=str(cwd),
            env=env,
            capture_output=True,
            text=text,
            errors="replace" if text else None,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise _ToolFailure(f"timeout after {timeout}s: {cmd[0]}") from exc
    except OSError as exc:
        raise _ToolFailure(f"failed to execute {cmd[0]}: {exc}") from exc
    out: Any = res.stdout or ("" if text else b"")
    err: Any = res.stderr or ("" if text else b"")
    return res.returncode, out, err


def _git(git_bin: str, args: List[str], cwd: Path) -> Tuple[int, str, str]:
    return _run([git_bin, *_GIT_C_OVERRIDES, *args], cwd, env=_git_env())


def _git_bytes(git_bin: str, args: List[str], cwd: Path) -> Tuple[int, bytes, bytes]:
    return _run(
        [git_bin, *_GIT_C_OVERRIDES, *args], cwd, env=_git_env(), text=False
    )


def _ruff_version(ruff_bin: str) -> str:
    """Bare Ruff version (e.g. ``0.15.7``), or ``unknown``."""
    try:
        rc, out, _ = _run(
            [ruff_bin, "--version"], Path.cwd(), env=_tool_env(), timeout=15
        )
        version = out.strip() if rc == 0 else ""
    except _ToolFailure:
        return "unknown"
    if version.lower().startswith("ruff"):
        version = version[4:].strip()
    return version or "unknown"


# ── Validation helpers (pure — no subprocesses) ─────────────────────────────


def validate_rule(rule: str) -> str:
    """Return the rule if well-formed and allowlisted, else raise."""
    if not isinstance(rule, str) or not rule:
        raise PatchPreviewError("no rule provided (required: --rule <code>)")
    if not _RULE_RE.match(rule):
        raise PatchPreviewError(
            f"malformed rule {rule!r}: expected a Ruff code like F401"
        )
    if rule not in ALLOWED_RULES:
        allowed = ", ".join(sorted(ALLOWED_RULES))
        raise PatchPreviewError(
            f"rule {rule} is not supported for fix-preview; allowlist: {allowed}"
        )
    return rule


def validate_output_dir(output_dir: Path, source_root: Path) -> Path:
    """Validate the artifact directory: required, must not exist, outside source."""
    if output_dir is None or str(output_dir) == "":
        raise PatchPreviewError(
            "no output directory provided (required: --output-dir <new-dir>)"
        )
    resolved = Path(output_dir).expanduser()
    try:
        resolved = resolved.resolve()
    except OSError as exc:
        raise PatchPreviewError(
            f"cannot resolve output directory {output_dir}: {exc}"
        ) from exc
    if resolved.exists():
        raise PatchPreviewError(f"output directory already exists: {resolved}")
    source = source_root.resolve()
    if resolved == source or source in resolved.parents:
        raise PatchPreviewError(
            f"output directory must be outside the source checkout: {resolved}"
        )
    return resolved


def _write_patch_artifact(path: Path, content: bytes) -> None:
    """Create the patch file exclusively — artifacts are never overwritten."""
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    with os.fdopen(fd, "wb") as fh:
        fh.write(content)
        fh.flush()
        os.fsync(fh.fileno())


# ── Source repository checks (read-only) ────────────────────────────────────


def _source_root_and_head(repo_path: Path, git_bin: str) -> Tuple[Path, str]:
    """Validate the registered path; return (worktree root, HEAD sha)."""
    repo_path = Path(repo_path).expanduser()
    if not repo_path.is_dir():
        raise PatchPreviewError(f"registered path is not a directory: {repo_path}")
    rc, out, _ = _git(git_bin, ["rev-parse", "--show-toplevel"], repo_path)
    if rc != 0 or not out.strip():
        raise PatchPreviewError(f"not a git worktree: {repo_path}")
    root = Path(out.strip()).resolve()
    if not (root / ".git").exists():
        raise PatchPreviewError(f"no .git found at repository root: {root}")
    rc, out, _ = _git(git_bin, ["rev-parse", "HEAD"], root)
    if rc != 0 or not out.strip():
        raise PatchPreviewError(f"cannot resolve HEAD (empty repository?): {root}")
    return root, out.strip()


def _require_clean(root: Path, git_bin: str) -> None:
    rc, out, _ = _git(git_bin, ["status", "--porcelain"], root)
    if rc != 0:
        raise PatchPreviewError(f"cannot inspect worktree status: {root}")
    if out.strip():
        raise PatchPreviewError(
            "source worktree is dirty — commit or stash before running fix-preview"
        )


def _tracked_listing(root: Path, git_bin: str) -> Tuple[List[str], bool]:
    """One NUL-parsed ``ls-files -s`` listing.

    Returns (tracked regular *.py relative paths sorted, submodules_present).
    NUL parsing keeps quoted/non-ASCII paths verbatim (no core.quotePath
    mangling). Modes 120000 (symlinks) and 160000 (gitlinks) are recorded:
    gitlinks or a tracked .gitmodules mean submodules → rejected.
    """
    rc, out, _ = _git_bytes(git_bin, ["ls-files", "-s", "-z"], root)
    if rc != 0:
        raise PatchPreviewError(f"cannot list tracked files: {root}")
    py_files: List[str] = []
    submodules = False
    for record in out.split(b"\0"):
        if not record:
            continue
        meta, _, raw_path = record.partition(b"\t")
        mode = meta.split(b" ", 1)[0].decode("ascii", "replace")
        rel = raw_path.decode("utf-8", "surrogateescape")
        if mode == "160000":
            submodules = True
        if rel == ".gitmodules":
            submodules = True
        if mode in ("100644", "100755") and rel.endswith(".py"):
            py_files.append(rel)
    return sorted(py_files), submodules


# ── Disposable clone ────────────────────────────────────────────────────────


def _make_clone(root: Path, temp_root: Path, head: str, git_bin: str) -> Path:
    """Local object-database copy: no transport (no upload-pack/hooks), no
    shared hardlinks, no template hooks. HEAD is pinned to the recorded
    baseline and verified — a concurrent source move is rejected."""
    template_dir = temp_root / "empty-template"
    template_dir.mkdir()
    clone_dir = temp_root / "clone"
    rc, _, err = _git(
        git_bin,
        [
            "clone",
            "--local",
            "--no-hardlinks",
            f"--template={template_dir}",
            str(root),
            str(clone_dir),
        ],
        Path.cwd(),
    )
    if rc != 0:
        raise _ToolFailure(f"git clone failed: {err.strip()[:400]}")
    rc, _, err = _git(
        git_bin,
        ["-c", "advice.detachedHead=false", "checkout", "--detach", head],
        clone_dir,
    )
    if rc != 0:
        raise _ToolFailure(f"baseline checkout failed: {err.strip()[:400]}")
    rc, out, _ = _git(git_bin, ["rev-parse", "HEAD"], clone_dir)
    if rc != 0 or out.strip() != head:
        raise _ToolFailure(
            f"clone HEAD {out.strip()[:12]} does not match baseline "
            f"{head[:12]} — source moved during clone"
        )
    return clone_dir


def _discover(
    ruff_bin: str, clone_dir: Path, rule: str, tracked: List[str]
) -> Dict[str, int]:
    """Map of tracked file -> finding count, discovered INSIDE the clone by
    passing the explicit tracked file list to Ruff (chunked for command
    length). Only tracked regular files are ever examined."""
    counts: Dict[str, int] = {}
    chunk: List[str] = []
    chunk_bytes = 0

    def flush() -> None:
        if not chunk:
            return
        cmd = [
            ruff_bin,
            "check",
            "--isolated",
            "--no-cache",
            "--select",
            rule,
            "--output-format",
            "json",
            "--",
            *chunk,
        ]
        rc, out, err = _run(cmd, clone_dir, env=_tool_env())
        if rc not in (0, 1):
            raise _ToolFailure(
                f"ruff discovery failed (rc={rc}): {err.strip()[:400] or out.strip()[:400]}"
            )
        if out.strip():
            try:
                entries = json.loads(out)
            except json.JSONDecodeError as exc:
                raise _ToolFailure(
                    f"ruff discovery produced unparseable output: {exc}"
                ) from exc
            tracked_set = set(chunk)
            for entry in entries:
                if not isinstance(entry, dict) or entry.get("code") != rule:
                    continue
                try:
                    rel = str(Path(str(entry.get("filename", ""))).relative_to(clone_dir))
                except ValueError:
                    continue
                if rel in tracked_set:
                    counts[rel] = counts.get(rel, 0) + 1
        chunk.clear()

    for rel in tracked:
        if len(chunk) >= _DISCOVER_CHUNK_FILES or chunk_bytes + len(rel) > _DISCOVER_CHUNK_BYTES:
            flush()
            chunk_bytes = 0
        chunk.append(rel)
        chunk_bytes += len(rel) + 1
    flush()
    return counts


# ── Entry point ─────────────────────────────────────────────────────────────


def run_patch_preview(
    repo_path: Path,
    rule: str,
    output_dir: Path,
    *,
    repo_name: Optional[str] = None,
    ruff_bin: Optional[str] = None,
) -> Dict[str, Any]:
    """Run the bounded fix-preview proof.

    Returns a result dict with ``status``, ``exit_code``, ``summary``,
    ``report`` and optional ``stderr_lines``. Raises
    :class:`PatchPreviewError` for precondition rejections — nothing is
    written and no output directory is created in that case.
    """
    rule = validate_rule(rule)
    git_bin = _resolve_tool("git")
    root, head = _source_root_and_head(repo_path, git_bin)
    _require_clean(root, git_bin)
    tracked, submodules = _tracked_listing(root, git_bin)
    if submodules:
        raise PatchPreviewError(
            "submodules are not supported by fix-preview "
            "(gitlink entry or tracked .gitmodules found)"
        )
    if len(tracked) > MAX_TRACKED_PY_FILES:
        raise PatchPreviewError(
            f"repository has {len(tracked)} tracked Python files "
            f"(limit {MAX_TRACKED_PY_FILES}); fix-preview is intentionally bounded"
        )
    out_dir = validate_output_dir(output_dir, root)

    ruff = ruff_bin or _resolve_tool("ruff")
    if not os.path.isabs(ruff):
        raise PatchPreviewError(
            f"ruff resolved to the non-absolute path {ruff!r} — "
            "relative tool paths are not trusted by fix-preview"
        )
    version = _ruff_version(ruff)

    # From here on the output directory exists and every outcome persists
    # report.json into it.
    try:
        out_dir.mkdir(parents=True, exist_ok=False)
    except FileExistsError:
        raise PatchPreviewError(f"output directory already exists: {out_dir}")
    except OSError as exc:
        raise PatchPreviewError(f"cannot create output directory {out_dir}: {exc}")
    report_path = out_dir / "report.json"
    patch_path = out_dir / "fix.patch"

    def done(
        status: str,
        exit_code: int,
        *,
        target_file: Optional[str] = None,
        findings_before: int = 0,
        findings_remaining: int = 0,
        changed_files: Optional[List[str]] = None,
        validation: Optional[Dict[str, str]] = None,
        fix_patch: Optional[Path] = None,
        recovery_dir: Optional[Path] = None,
        notes: Optional[List[str]] = None,
        stderr_lines: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        report: Dict[str, Any] = {
            "schema": REPORT_SCHEMA,
            "generated_at": now_iso(),
            "status": status,
            "repo": {"name": repo_name, "path": str(repo_path)},
            "rule": rule,
            "tool": {"name": "ruff", "version": version},
            "baseline_head": head,
            "boundary": {"llm_calls": 0, "prs_created": 0, "remote_operations": 0,
                         "source_worktree_mutations": 0},
            "scope": {"tracked_python_files": len(tracked), "target_file": target_file,
                      "max_files_fixed": 1},
            "findings": {"before_in_target_file": findings_before,
                         "remaining_in_target_file": findings_remaining},
            "changed_files": changed_files or [],
            "validation": validation or {"ruff_recheck": "skipped", "ast_compile": "skipped"},
            "artifacts": {"fix_patch": str(fix_patch) if fix_patch else None,
                          "report": str(report_path)},
            "notes": notes or [],
        }
        if recovery_dir is not None:
            report["recovery_dir"] = str(recovery_dir)
        stderr = list(stderr_lines or [])
        try:
            atomic_json_write(report_path, report)
        except OSError as exc:
            # The patch-and-report contract is broken without the report:
            # never report success, keep whatever artifact was persisted.
            stderr.append(f"fix-preview: could not write report.json: {exc}")
            exit_code = EXIT_ERROR
        if status == STATUS_PATCH_PRODUCED:
            summary = (
                f"fix-preview: status=patch_produced rule={rule} "
                f"file={target_file} patch={fix_patch}"
            )
        else:
            summary = f"fix-preview: status={status} rule={rule}"
        return {
            "status": status,
            "exit_code": exit_code,
            "summary": summary,
            "report": report,
            "stderr_lines": stderr,
        }

    def tool_error(
        note: str, *, status: str = STATUS_TOOL_ERROR, **fields: Any
    ) -> Dict[str, Any]:
        """Terminal failure retaining the clone as recovery evidence."""
        nonlocal retain_temp
        retain_temp = True
        return done(
            status,
            EXIT_ERROR,
            recovery_dir=temp_root,
            notes=[note],
            stderr_lines=[
                "fix-preview: recovery directory retained: " + str(temp_root)
            ],
            **fields,
        )

    temp_root = Path(tempfile.mkdtemp(prefix="bluei-fix-preview-"))
    retain_temp = False
    try:
        try:
            clone_dir = _make_clone(root, temp_root, head, git_bin)
        except _ToolFailure as exc:
            return done(
                STATUS_TOOL_ERROR, EXIT_ERROR, notes=[f"clone failed: {exc}"]
            )

        try:
            counts = _discover(ruff, clone_dir, rule, tracked)
        except _ToolFailure as exc:
            return done(
                STATUS_TOOL_ERROR,
                EXIT_ERROR,
                notes=[f"discovery failed: {exc}"],
            )
        if not counts:
            return done(
                STATUS_NO_FINDINGS,
                EXIT_OK,
                notes=[
                    "no findings for the requested rule in tracked Python files "
                    "(tracked symlinks are not candidates)"
                ],
            )
        target_file = sorted(counts.keys())[0]  # bounded: exactly one file
        findings_before = counts[target_file]

        # ── Fixed tool operation ──
        fix_cmd = [
            ruff,
            "check",
            "--isolated",
            "--no-cache",
            "--fix",
            "--no-unsafe-fixes",
            "--select",
            rule,
            "--",
            target_file,
        ]
        try:
            rc, out, err = _run(fix_cmd, clone_dir, env=_tool_env())
            if rc not in (0, 1):
                return tool_error(
                    f"ruff fix failed (rc={rc}): {err.strip()[:400]}",
                    target_file=target_file,
                    findings_before=findings_before,
                )
            rc, diff, err = _git_bytes(
                git_bin,
                ["diff", "--no-ext-diff", "--no-textconv", "--", target_file],
                clone_dir,
            )
        except _ToolFailure as exc:
            return tool_error(  # the clone may already hold the sole fix
                f"tool failure during fix: {exc}",
                target_file=target_file,
                findings_before=findings_before,
            )
        if rc != 0:
            return tool_error(
                f"git diff failed: {err.decode('utf-8', 'replace').strip()[:400]}",
                target_file=target_file,
                findings_before=findings_before,
            )
        if not diff.strip():
            return done(
                STATUS_NO_SAFE_FIX,
                EXIT_OK,
                target_file=target_file,
                findings_before=findings_before,
                notes=[
                    "findings exist but no safe fix was applicable "
                    "(unsafe fixes are never applied by fix-preview)"
                ],
            )

        # ── Validation: Ruff re-check + AST compile (no execution) ──
        recheck_cmd = [
            ruff,
            "check",
            "--isolated",
            "--no-cache",
            "--select",
            rule,
            "--output-format",
            "json",
            "--",
            target_file,
        ]
        remaining = 0
        try:
            rc, out, err = _run(recheck_cmd, clone_dir, env=_tool_env())
        except _ToolFailure as exc:
            return tool_error(
                f"recheck failed: {exc}",
                target_file=target_file,
                findings_before=findings_before,
            )
        if rc == 0:
            recheck = "pass"
        elif rc == 1:
            recheck = "fail"
            try:
                entries = json.loads(out) if out.strip() else []
                remaining = len(
                    [e for e in entries if isinstance(e, dict) and e.get("code") == rule]
                )
            except json.JSONDecodeError:
                remaining = -1
        else:
            return tool_error(
                f"ruff recheck failed (rc={rc}): {err.strip()[:400]}",
                target_file=target_file,
                findings_before=findings_before,
            )

        try:
            ast.parse((clone_dir / target_file).read_text(encoding="utf-8"))
            compile_ok = True
        except (SyntaxError, ValueError, OSError, UnicodeDecodeError):
            compile_ok = False

        validation = {
            "ruff_recheck": recheck,
            "ast_compile": "pass" if compile_ok else "fail",
        }
        if recheck != "pass" or not compile_ok:
            return done(
                STATUS_VALIDATION_FAILED,
                EXIT_ERROR,
                target_file=target_file,
                findings_before=findings_before,
                findings_remaining=remaining,
                changed_files=[target_file],
                validation=validation,
                notes=["fix was discarded — validation did not pass"],
            )

        # ── Persist artifacts (raw diff bytes) BEFORE cleanup ──
        try:
            _write_patch_artifact(patch_path, diff)
        except OSError as exc:
            return tool_error(  # never silently destroy the only verified fix
                f"failed to persist fix.patch: {exc}",
                status=STATUS_ARTIFACT_WRITE_FAILED,
                target_file=target_file,
                findings_before=findings_before,
                changed_files=[target_file],
                validation=validation,
            )

        return done(
            STATUS_PATCH_PRODUCED,
            EXIT_OK,
            target_file=target_file,
            findings_before=findings_before,
            changed_files=[target_file],
            validation=validation,
            fix_patch=patch_path,
            notes=[
                "patch is a preview artifact — nothing was committed, pushed, or PR'd"
            ],
        )
    finally:
        if not retain_temp:
            shutil.rmtree(temp_root, ignore_errors=True)
