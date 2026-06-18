"""Publication pipeline — extracted from review.observation.ObservationMixin.

Live publishing of autonomous review summaries to GitHub PRs (with transient-
failure retry and shadow-mode handling), plus resolution of PR context for an
autonomous run."""

from __future__ import annotations

import subprocess
import time as _time
from typing import Any, Dict, Optional, Tuple

from bluei.common.models import LiveRolloutMode
from bluei.review.models import PublishStatus


class PublicationMixin:
    def _post_summary_to_github(
        self,
        summary_text: str,
        run_id: str,
        prior_publish: Dict[str, Any],
        target_pr_number: Optional[int] = None,
    ) -> Optional[str]:
        if not self.repo.config.github.get("live_actions", False):
            return None

        runs_entries = prior_publish.setdefault("runs", {})
        existing = runs_entries.get(run_id, {})
        if existing.get("comment_url"):
            return existing["comment_url"]

        if run_id not in runs_entries:
            runs_entries[run_id] = {}

        if target_pr_number is None:
            target_pr_number, resolution_reason = self._resolve_target_pr_for_run(
                prior_publish,
            )
        else:
            resolution_reason = f"explicit-{target_pr_number}"

        live_rollout_mode, _ = self._get_live_rollout_mode()
        if live_rollout_mode == LiveRolloutMode.SHADOW:
            runs_entries[run_id]["status"] = PublishStatus.PENDING.value
            runs_entries[run_id]["shadow"] = True
            runs_entries[run_id]["shadow_summary_text"] = summary_text
            runs_entries[run_id]["targeted_pr_number"] = target_pr_number
            runs_entries[run_id]["rollout_mode"] = LiveRolloutMode.SHADOW.value

            open_prs = self._find_open_prs()
            open_numbers = {p["number"] for p in open_prs}
            if target_pr_number is None:
                targeting_outcome = "no-target-pr"
            elif open_prs and target_pr_number not in open_numbers:
                targeting_outcome = f"target-pr-{target_pr_number}-not-open"
            else:
                targeting_outcome = f"target-pr-{target_pr_number}-would-post"

            self.state.append_review_event(
                self.repo.config.name,
                {
                    "event": "autonomous-review-shadow-published",
                    "run_id": run_id,
                    "provider": "github",
                    "details": {
                        "pr": target_pr_number,
                        "rollout_mode": LiveRolloutMode.SHADOW.value,
                        "summary_length": len(summary_text),
                        "targeting_outcome": targeting_outcome,
                        "open_prs": list(open_numbers) if open_prs else [],
                        "action": "shadow-would-have-published",
                    },
                },
            )
            return None

        if target_pr_number is None:
            runs_entries[run_id]["status"] = PublishStatus.FAILED.value
            runs_entries[run_id]["error"] = f"target-refused:{resolution_reason}"
            runs_entries[run_id]["targeted_pr_number"] = None
            self.state.append_review_event(
                self.repo.config.name,
                {
                    "event": "autonomous-review-publish-refused",
                    "run_id": run_id,
                    "provider": "github",
                    "details": {
                        "reason": resolution_reason,
                        "action": "stayed-local-only",
                    },
                },
            )
            return None

        open_prs = self._find_open_prs()
        open_numbers = {p["number"] for p in open_prs}
        if open_prs and target_pr_number not in open_numbers:
            runs_entries[run_id]["status"] = PublishStatus.FAILED.value
            runs_entries[run_id]["error"] = f"target-pr-{target_pr_number}-not-open"
            runs_entries[run_id]["targeted_pr_number"] = None
            self.state.append_review_event(
                self.repo.config.name,
                {
                    "event": "autonomous-review-publish-refused",
                    "run_id": run_id,
                    "provider": "github",
                    "details": {
                        "reason": f"target-pr-{target_pr_number}-not-open",
                        "targeted_pr": target_pr_number,
                        "open_prs": list(open_numbers),
                        "action": "stayed-local-only",
                    },
                },
            )
            return None

        owner = (
            self.repo.config.github.get("owner") or self.repo.config.name.split("/")[0]
        )
        repo_name = (
            self.repo.config.github.get("repo") or self.repo.config.name.split("/")[1]
        )

        base_delay_seconds = min(
            float(self.repo.config.review_care.get("retry_delay_minutes", 15)) * 60.0,
            4.0,
        )
        max_retries = 2

        def _is_transient_failure(returncode: int, stderr: str, stdout: str) -> bool:
            if returncode == 124:
                return True
            combined = (stderr + stdout).lower()
            if any(
                kw in combined
                for kw in [
                    "rate limit",
                    "rate_limit",
                    "429",
                    "too many requests",
                    "secondary rate limit",
                    "abuse rate limit",
                ]
            ):
                return True
            if any(
                kw in combined
                for kw in [
                    "connection reset",
                    "connection refused",
                    "name or service not known",
                    "network is unreachable",
                    "temporary failure in name resolution",
                    "could not resolve host",
                    "ssl",
                    "tls",
                ]
            ):
                return True
            return False

        last_err = ""
        published_url: Optional[str] = None
        try:
            for attempt in range(max_retries + 1):
                try:
                    result = subprocess.run(
                        [
                            "gh",
                            "pr",
                            "comment",
                            str(target_pr_number),
                            "--repo",
                            f"{owner}/{repo_name}",
                            "--body",
                            summary_text,
                        ],
                        capture_output=True,
                        text=True,
                        timeout=20,
                    )
                except Exception as exc:
                    last_err = str(exc)
                    if attempt < max_retries:
                        delay = base_delay_seconds * (2**attempt)
                        self.state.append_review_event(
                            self.repo.config.name,
                            {
                                "event": "autonomous-review-publish-retry",
                                "run_id": run_id,
                                "provider": "github",
                                "details": {
                                    "pr": target_pr_number,
                                    "attempt": attempt + 1,
                                    "max_retries": max_retries + 1,
                                    "delay_seconds": delay,
                                    "error": last_err,
                                },
                            },
                        )

                        _time.sleep(delay)
                        continue
                    last_err = f"gh-call-exception:{last_err}"
                    if run_id in runs_entries:
                        runs_entries[run_id]["status"] = PublishStatus.FAILED.value
                        runs_entries[run_id]["error"] = last_err
                        runs_entries[run_id]["targeted_pr_number"] = target_pr_number
                    published_url = None
                    break

                if result.returncode == 0:
                    published_url = result.stdout.strip()
                    if run_id in runs_entries:
                        runs_entries[run_id]["status"] = PublishStatus.PUBLISHED.value
                        runs_entries[run_id]["comment_url"] = published_url
                        runs_entries[run_id]["targeted_pr_number"] = target_pr_number
                    self.state.append_review_event(
                        self.repo.config.name,
                        {
                            "event": "autonomous-review-published",
                            "run_id": run_id,
                            "provider": "github",
                            "details": {
                                "pr": target_pr_number,
                                "comment_url": published_url,
                                "resolution": resolution_reason,
                            },
                        },
                    )
                    break

                last_err = (
                    result.stderr.strip()
                    or f"gh-pr-comment-failed (code {result.returncode})"
                )
                if attempt < max_retries and _is_transient_failure(
                    result.returncode, last_err, result.stdout.strip()
                ):
                    delay = base_delay_seconds * (2**attempt)
                    self.state.append_review_event(
                        self.repo.config.name,
                        {
                            "event": "autonomous-review-publish-retry",
                            "run_id": run_id,
                            "provider": "github",
                            "details": {
                                "pr": target_pr_number,
                                "attempt": attempt + 1,
                                "max_retries": max_retries + 1,
                                "delay_seconds": delay,
                                "error": last_err,
                            },
                        },
                    )

                    _time.sleep(delay)
                    continue

                if run_id in runs_entries:
                    runs_entries[run_id]["status"] = PublishStatus.FAILED.value
                    runs_entries[run_id]["error"] = last_err
                    runs_entries[run_id]["targeted_pr_number"] = target_pr_number
                self.state.append_review_event(
                    self.repo.config.name,
                    {
                        "event": "autonomous-review-publish-failed",
                        "run_id": run_id,
                        "provider": "github",
                        "details": {
                            "pr": target_pr_number,
                            "error": last_err,
                            "retries_exhausted": attempt >= max_retries,
                        },
                    },
                )
                published_url = None
                break
        except Exception as exc:
            last_err = str(exc)
            if run_id in runs_entries:
                runs_entries[run_id]["status"] = PublishStatus.FAILED.value
                runs_entries[run_id]["error"] = last_err
                runs_entries[run_id]["targeted_pr_number"] = target_pr_number
            published_url = None

        return published_url

    def _resolve_pr_context_for_autonomous_run(
        self,
        prior_publish: Dict[str, Any],
    ) -> Tuple[Optional[int], str]:
        configured_pr = self.repo.config.github.get("pr_number")
        if configured_pr is not None:
            try:
                pr_num = int(configured_pr)
                return (pr_num, f"explicit-config-pr-{pr_num}")
            except (TypeError, ValueError):
                pass

        if self.repo.config.github.get("live_actions", False):
            pr_num, reason = self._resolve_target_pr_for_run(prior_publish)
            if pr_num is not None:
                return (pr_num, f"resolved-{reason}")
            return (None, reason)

        return (None, "live-actions-disabled")
