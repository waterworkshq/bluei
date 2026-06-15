"""OnboardEngine orchestrator and onboarding data classes."""

import logging
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from bluei.engine.models import Finding
from ..models import (
    LanguageInfo,
    RepoConfig,
    Repo,
    RepoStatus,
    Baseline,
    HealthScore,
    SafetyMode,
    SafetyProfile,
    generate_id,
    now_iso,
    ONBOARDING_VERSION,
)
from ..preflight import PreflightResult
from ..registry import RepoRegistry
from ..health import HealthEngine
from ..state import StateManager
from ..plugins import PluginLoader
from . import detection as _detection
from . import inference as _inference
from . import templates as _templates
from .detection import detect_frameworks

_logger = logging.getLogger(__name__)


@dataclass
class OnboardOptions:
    """Options for onboarding."""

    name: Optional[str] = None
    language: Optional[str] = None
    framework: Optional[str] = None
    plugin_id: Optional[str] = None
    template: Optional[str] = None
    capture_baseline: bool = True
    mode: str = SafetyMode.OBSERVE.value
    profile: str = SafetyProfile.CONSERVATIVE.value
    allow_dirty_worktree: bool = False
    skip_preflight: bool = False
    fix_engine: Optional[str] = None
    # E1: reuse a prior preflight run — skip detect_language(), select_plugin(),
    # and the inline preflight gate when supplied.
    preflight_result: Optional[PreflightResult] = None


@dataclass
class ReviewItem:
    """A single review item produced during onboarding.

    Replaces the previous plain-string review items so callers can react to
    severity/category instead of substring-matching free-form text.
    """

    message: str
    severity: str = "info"  # "info" | "warn" | "action-required"
    category: str = "config"  # "safety" | "config" | "tool"
    suggested_action: Optional[str] = None

    def to_string(self) -> str:
        """Backward-compatible single-line rendering."""
        return f"[{self.severity}] {self.message}"

    def __str__(self) -> str:
        return self.message


@dataclass
class OnboardResult:
    """Result of onboarding."""

    repo: Repo
    baseline: Optional[Baseline]
    health: Optional[HealthScore]
    language: LanguageInfo
    plugin_id: str
    findings_count: int
    template: Optional[str] = None
    suggested_checks: List[List[str]] = field(default_factory=list)
    review_items: List[ReviewItem] = field(default_factory=list)


class OnboardEngine:
    """Handles repository onboarding workflow."""

    def __init__(
        self,
        registry: RepoRegistry,
        plugin_loader: PluginLoader,
        health_engine: HealthEngine,
        state_manager: StateManager,
    ):
        self.registry = registry
        self.plugin_loader = plugin_loader
        self.health = health_engine
        self.state = state_manager

    def detect_all_languages(self, repo_path: Path) -> List[tuple[str, int]]:
        return _detection.detect_all_languages(repo_path)

    def detect_language(self, repo_path: Path) -> LanguageInfo:
        return _detection.detect_language_info(repo_path)

    def detect_git_remote(self, repo_path: Path) -> Dict[str, str]:
        return _detection.detect_git_remote(repo_path)

    def detect_package_manager(self, repo_path: Path, language: str) -> Optional[str]:
        return _detection.detect_package_manager(repo_path, language)

    def detect_build_tool(self, repo_path: Path, language: str) -> Optional[str]:
        return _detection.detect_build_tool(repo_path, language)

    def detect_monorepo(
        self, repo_path: Path, language: LanguageInfo
    ) -> Dict[str, Any]:
        return _detection.detect_monorepo(repo_path, language)

    def _get_language_version(self, repo_path: Path, language: str) -> Optional[str]:
        return _detection.get_language_version(repo_path, language)

    def _is_actionable_language(self, language: str) -> bool:
        return _detection.is_actionable_language(language)

    def infer_baseline_checks(
        self, repo_path: Path, language: LanguageInfo
    ) -> List[List[str]]:
        return _inference.infer_baseline_checks(repo_path, language)

    def _infer_baseline_checks_for_language(
        self, repo_path: Path, language_name: str, package_manager: Optional[str] = None
    ) -> List[List[str]]:
        return _inference._infer_baseline_checks_for_language(
            repo_path, language_name, package_manager
        )

    def infer_discovery_config(
        self, repo_path: Path, language: LanguageInfo
    ) -> Dict[str, Any]:
        return _inference.infer_discovery_config(repo_path, language)

    def infer_fix_strategy(
        self, repo_path: Path, language: LanguageInfo
    ) -> Dict[str, Any]:
        return _inference.infer_fix_strategy(repo_path, language)

    def infer_safety_policy(
        self, repo_path: Path, options: OnboardOptions
    ) -> Dict[str, Any]:
        return _inference.infer_safety_policy(
            repo_path, options.mode, options.profile, options.allow_dirty_worktree
        )

    def apply_safety_profile(self, config: RepoConfig) -> RepoConfig:
        return _inference.apply_safety_profile(config)

    def build_review_items(
        self, repo_path: Path, language: LanguageInfo, config: RepoConfig
    ) -> List[ReviewItem]:
        """Highlight manual review items after onboarding."""
        items: List[ReviewItem] = []
        if not config.baseline_checks:
            items.append(
                ReviewItem(
                    message=(
                        "No baseline validation commands were inferred; review "
                        "baseline_checks before live runs."
                    ),
                    severity="warn",
                    category="config",
                )
            )
        # E4: Check if selected plugin requires a container
        plugin_id = config.plugin_id or ""
        if plugin_id:
            import shutil
            from bluei.engine.plugin_loader import discover_plugins

            plugins_dir = self.plugin_loader.plugins_dir
            manifests = discover_plugins(plugins_dir)
            manifest = manifests.get(plugin_id, {})
            if manifest.get("requires_container", False):
                docker_path = shutil.which("docker")
                if docker_path:
                    items.append(
                        ReviewItem(
                            message=(
                                f"Plugin '{plugin_id}' requires a container; "
                                f"docker found at {docker_path}."
                            ),
                            severity="info",
                            category="tool",
                        )
                    )
                else:
                    items.append(
                        ReviewItem(
                            message=(
                                f"Plugin '{plugin_id}' requires a container but "
                                "docker is not installed; discovery will fall back "
                                "to text scanning."
                            ),
                            severity="warn",
                            category="tool",
                            suggested_action=("Install docker or use --skip-preflight"),
                        )
                    )
        if config.discovery.get("use_docker"):
            items.append(
                ReviewItem(
                    message=(
                        "Docker-backed discovery was inferred; verify the correct "
                        "container/service context."
                    ),
                    severity="info",
                    category="config",
                )
            )
        if config.discovery.get("monorepo"):
            items.append(
                ReviewItem(
                    message=(
                        "Monorepo/workspace layout detected; verify whether root "
                        "onboarding or package-level onboarding is safer."
                    ),
                    severity="info",
                    category="config",
                )
            )
            if config.discovery.get("package_dirs"):
                items.append(
                    ReviewItem(
                        message=(
                            "Workspace package patterns detected: "
                            + ", ".join(config.discovery.get("package_dirs", []))
                        ),
                        severity="info",
                        category="config",
                    )
                )
        if not config.github.get("live_actions", False):
            items.append(
                ReviewItem(
                    message=(
                        "GitHub live actions default to off; enable only after "
                        "reviewing safety gates."
                    ),
                    severity="info",
                    category="safety",
                )
            )
        if (repo_path / ".github" / "workflows").exists():
            items.append(
                ReviewItem(
                    message=(
                        "Repo has GitHub Actions workflows; consider adding "
                        "build/test commands that mirror CI."
                    ),
                    severity="info",
                    category="config",
                )
            )
        if language.name == "unknown":
            items.append(
                ReviewItem(
                    message=(
                        "Language detection returned unknown; verify plugin and "
                        "rules before enabling."
                    ),
                    severity="warn",
                    category="config",
                )
            )
        if language.secondary_languages:
            items.append(
                ReviewItem(
                    message=(
                        f"Mixed-language repo detected (primary={language.name}, "
                        f"secondary={language.secondary_languages}); baseline checks "
                        "were inferred for all languages but verify plugin coverage "
                        "for secondary languages."
                    ),
                    severity="info",
                    category="config",
                )
            )
        if config.safety.get("mode") == SafetyMode.OBSERVE.value:
            items.append(
                ReviewItem(
                    message=(
                        "Observe mode is active; non-dry-run execution will be "
                        "blocked until mode is raised."
                    ),
                    severity="info",
                    category="safety",
                )
            )
        if config.safety.get("require_clean_worktree", True):
            items.append(
                ReviewItem(
                    message="Clean working tree is required for live runs.",
                    severity="info",
                    category="safety",
                )
            )
        if config.safety.get("profile") == SafetyProfile.AGGRESSIVE.value:
            items.append(
                ReviewItem(
                    message=(
                        "Aggressive profile selected; verify caps are appropriate "
                        "for this repository."
                    ),
                    severity="warn",
                    category="safety",
                )
            )
        return items

    def detect_framework(self, repo_path: Path, language: str) -> Optional[str]:
        frameworks = detect_frameworks(repo_path, language)
        return frameworks[0] if frameworks else None

    def select_plugin(
        self, language: str, framework: Optional[str] = None
    ) -> Optional[str]:
        """Select appropriate plugin for language/framework."""
        self.plugin_loader.discover()

        plugin = self.plugin_loader.get_for_language(language)
        if plugin:
            return plugin.id

        return None

    def select_template(
        self, repo_path: Path, language: LanguageInfo, framework: Optional[str]
    ) -> Optional[str]:
        return _templates.select_template(repo_path, language, framework)

    def select_rule_pack(self, template_name: Optional[str]) -> Optional[str]:
        return _templates.select_rule_pack(template_name)

    def generate_config(
        self,
        repo_path: Path,
        name: str,
        language: LanguageInfo,
        framework: Optional[str],
        plugin_id: str,
        template_name: Optional[str] = None,
        fix_engine_override: Optional[str] = None,
    ) -> RepoConfig:
        """Generate repository configuration."""
        baseline_checks = self.infer_baseline_checks(repo_path, language)
        discovery = self.infer_discovery_config(repo_path, language)
        fix_strategy = self.infer_fix_strategy(repo_path, language)
        if fix_engine_override:
            fix_strategy["fix_engine"] = fix_engine_override
        template_name = template_name or self.select_template(
            repo_path, language, framework
        )

        render = self.registry.config.render_config_from_template
        merged = render(
            name=name,
            path=str(repo_path.resolve()),
            language=language.name,
            template_name=template_name,
            framework=framework,
            plugin_id=plugin_id,
            enabled=True,
            discovery=discovery,
            baseline_checks=baseline_checks,
            fix_engine=fix_strategy["fix_engine"],
            fallback_engines=fix_strategy["fallback_engines"],
            claude_template=fix_strategy["claude_template"],
            opencode_template=fix_strategy["opencode_template"],
            github={
                "live_actions": False,
                "auto_merge": False,
            },
            meta={
                "onboarding_version": ONBOARDING_VERSION,
                "template": template_name,
                "inferred_by": "template" if template_name else "heuristic",
                "secondary_languages": language.secondary_languages,
                "detected_frameworks": detect_frameworks(repo_path, language.name),
                "framework_detection_date": now_iso(),
            },
        )
        merged.id = generate_id("repo")
        return merged

    def run_discovery(self, repo_path: Path, config: RepoConfig) -> List[Finding]:
        """Run discovery using plugin."""
        plugin = self.plugin_loader.get(config.plugin_id)
        if not plugin:
            return []

        return plugin.discover(repo_path, config.discovery)

    def onboard(self, repo_path: Path, options: OnboardOptions) -> OnboardResult:
        """Execute full onboarding workflow."""
        repo_path = Path(repo_path).resolve()

        name = options.name or repo_path.name

        existing = self.registry.find_by_path(repo_path)
        if existing:
            raise ValueError(f"Repo already onboarded: {existing.config.name}")

        # ── E1: language detection ──
        # Reuse preflight_result if supplied; otherwise detect inline.
        if options.preflight_result is not None:
            language = LanguageInfo(
                name=options.preflight_result.language,
                secondary_languages=list(options.preflight_result.secondary_languages),
            )
        else:
            language = self.detect_language(repo_path)
        if options.language:
            language.name = options.language

        framework = options.framework or self.detect_framework(repo_path, language.name)

        # ── E1: plugin selection ──
        # Prefer explicit override → preflight_result → heuristic select_plugin().
        if options.plugin_id:
            plugin_id = options.plugin_id
        elif (
            options.preflight_result is not None and options.preflight_result.plugin_id
        ):
            plugin_id = options.preflight_result.plugin_id
        else:
            plugin_id = self.select_plugin(language.name, framework)
        if not plugin_id:
            raise ValueError(f"No plugin available for language: {language.name}")

        # ── Preflight gate (D6) ──
        # Skip when caller supplied a preflight_result (already verified) or
        # explicitly opted out via skip_preflight.
        if options.preflight_result is None and not options.skip_preflight:
            from bluei.app.preflight import run_preflight

            preflight = run_preflight(repo_path, run_validation=False)
            missing = preflight.missing_tools
            if missing:
                raise ValueError(
                    f"Preflight failed: missing tools for {language.name}: "
                    f"{', '.join(missing)}. Install them or use --skip-preflight."
                )

        selected_template = options.template or self.select_template(
            repo_path, language, framework
        )

        config = self.generate_config(
            repo_path,
            name,
            language,
            framework,
            plugin_id,
            template_name=selected_template,
            fix_engine_override=options.fix_engine,
        )
        config.safety = self.infer_safety_policy(repo_path, options)
        config = self.apply_safety_profile(config)

        if config.safety.get("require_clean_worktree", True):
            status_proc = subprocess.run(
                ["bash", "-lc", "git status --porcelain"],
                cwd=str(repo_path),
                text=True,
                capture_output=True,
            )
            if (
                status_proc.returncode == 0
                and status_proc.stdout.strip()
                and config.github.get("live_actions", False)
            ):
                raise ValueError(
                    "Refusing live-enabled onboarding on a dirty worktree; commit/stash changes or use --allow-dirty-worktree with observe mode first."
                )

        review_items = self.build_review_items(repo_path, language, config)

        repo = self.registry.create(config)

        findings = self.run_discovery(repo_path, config)

        health_score = self.health.calculate(findings)

        baseline = None
        if options.capture_baseline:
            findings_file = str(self.state.get_findings_file(name))
            self.state.append_findings(name, findings)
            baseline = self.health.create_baseline(
                repo_id=config.id,
                findings=findings,
                health=health_score,
                findings_file=findings_file,
            )
            self.state.save_baseline(name, baseline.to_dict())

        self.registry.update(
            name,
            {
                "status": RepoStatus.READY.value,
                "onboarded_at": now_iso(),
                "current_findings_count": len(findings),
                "current_health_score": health_score.score,
            },
        )

        self.health.save_health_snapshot(
            name, health_score, len(findings), self.state._get_state_dir(name)
        )

        return OnboardResult(
            repo=self.registry.read(name),
            baseline=baseline,
            health=health_score,
            language=language,
            plugin_id=plugin_id,
            findings_count=len(findings),
            template=selected_template,
            suggested_checks=config.baseline_checks,
            review_items=review_items,
        )

    def upgrade_config(self, repo_name: str) -> Dict[str, Any]:
        config = self.registry.config.load_repo_config(repo_name)
        if not config:
            raise ValueError(f"repo not found: {repo_name}")
        meta = config.meta or {}
        old_version = meta.get("onboarding_version", 1)
        if old_version >= ONBOARDING_VERSION:
            return {
                "repo": repo_name,
                "old_version": old_version,
                "new_version": old_version,
                "upgraded": False,
            }
        repo_path = Path(config.path).resolve()
        language = self.detect_language(repo_path)
        framework = self.detect_framework(repo_path, language.name)
        new_config = self.generate_config(
            repo_path,
            repo_name,
            language,
            framework,
            config.plugin_id,
            template_name=meta.get("template"),
        )
        new_config.safety = config.safety
        new_config = self.apply_safety_profile(new_config)
        new_config.meta["onboarding_version"] = ONBOARDING_VERSION
        new_config.meta["upgraded_from"] = old_version
        self.registry.config.save_repo_config(new_config)
        self.registry.update(repo_name, {"status": RepoStatus.READY.value})
        return {
            "repo": repo_name,
            "old_version": old_version,
            "new_version": ONBOARDING_VERSION,
            "upgraded": True,
        }
