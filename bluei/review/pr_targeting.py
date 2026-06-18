"""PR targeting — extracted from review.observation.ObservationMixin.

Discovery of open PRs via the GitHub CLI and resolution of the target PR for
an autonomous run."""

from __future__ import annotations

import json
import subprocess
from typing import Any, Dict, List, Optional, Tuple


class PrTargetingMixin:
    def _find_open_prs(self) -> List[Dict[str, Any]]:
        try:
            owner = (
                self.repo.config.github.get("owner")
                or self.repo.config.name.split("/")[0]
                if "/" in self.repo.config.name
                else ""
            )
            repo_name = (
                self.repo.config.github.get("repo")
                or self.repo.config.name.split("/")[1]
                if "/" in self.repo.config.name
                else self.repo.config.name
            )
            result = subprocess.run(
                [
                    "gh",
                    "pr",
                    "list",
                    "--json",
                    "number,title,updatedAt",
                    "--repo",
                    f"{owner}/{repo_name}",
                    "--state",
                    "open",
                    "--limit",
                    "10",
                ],
                capture_output=True,
                text=True,
                timeout=15,
            )
            if result.returncode != 0:
                return []
            prs = json.loads(result.stdout)
            prs.sort(key=lambda p: p.get("updatedAt", ""), reverse=True)
            return prs
        except Exception:
            return []

    def _resolve_target_pr_for_run(
        self,
        prior_publish: Dict[str, Any],
    ) -> Tuple[Optional[int], str]:
        runs_entries = prior_publish.get("runs", {})
        prior_targeted: List[int] = []
        for rid, rentry in runs_entries.items():
            tpn = rentry.get("targeted_pr_number")
            if tpn is not None:
                try:
                    prior_targeted.append(int(tpn))
                except (TypeError, ValueError):
                    pass

        if len(set(prior_targeted)) == 1:
            confirmed = prior_targeted[0]
            open_prs = self._find_open_prs()
            open_numbers = {p["number"] for p in open_prs}
            if open_numbers and confirmed not in open_numbers:
                return (
                    None,
                    f"prior-targeted-pr-{confirmed}-now-closed",
                )
            return (
                confirmed,
                f"prior-targeted-pr-{confirmed}-reused",
            )

        try:
            managed_prs = self.provider.list_managed_prs()
        except Exception:
            managed_prs = []
        if len(managed_prs) == 1:
            pr_number = int(managed_prs[0]["number"])
            return (pr_number, f"single-managed-pr-{pr_number}")
        if len(managed_prs) > 1:
            return (
                None,
                f"multiple-managed-prs-{len(managed_prs)}-refused",
            )

        open_prs = self._find_open_prs()
        if len(open_prs) == 1:
            pr_number = int(open_prs[0]["number"])
            return (pr_number, f"single-open-pr-{pr_number}")
        if len(open_prs) > 1:
            return (
                None,
                f"multiple-open-prs-{len(open_prs)}-refused",
            )

        return (None, "no-open-prs")
