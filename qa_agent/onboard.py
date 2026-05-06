#!/usr/bin/env python3
"""Repository onboarding engine."""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional
import json
import shutil
import subprocess

from .models import (
    LanguageInfo, RepoConfig, Repo, RepoStatus,
    Finding, Baseline, HealthScore, SafetyMode, SafetyProfile,
    generate_id, now_iso
)
from .config import ConfigManager
from .registry import RepoRegistry
from .health import HealthEngine
from .state import StateManager
from .plugins import PluginLoader


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
    review_items: List[str] = field(default_factory=list)


class OnboardEngine:
    """Handles repository onboarding workflow."""
    
    # Language detection patterns
    LANGUAGE_MARKERS = {
        'python': ['setup.py', 'pyproject.toml', 'requirements.txt', 'Pipfile', 'setup.cfg'],
        'typescript': ['tsconfig.json'],
        'javascript': ['package.json', '.eslintrc', '.prettierrc'],
        'go': ['go.mod', 'go.sum'],
        'rust': ['Cargo.toml', 'Cargo.lock'],
        'java': ['pom.xml', 'build.gradle', 'build.gradle.kts'],
        'ruby': ['Gemfile', 'Rakefile'],
        'php': ['composer.json'],
    }
    
    # Framework detection patterns
    FRAMEWORK_MARKERS = {
        'python': {
            'django': ['manage.py', 'settings.py', 'urls.py'],
            'flask': ['app.py', 'flask'],
            'fastapi': ['fastapi', 'main.py'],
            'pytest': ['pytest.ini', 'conftest.py'],
        },
        'typescript': {
            'react': ['react', 'jsx', 'tsx'],
            'vue': ['vue'],
            'angular': ['angular', '@angular'],
            'next': ['next'],
            'nestjs': ['@nestjs'],
        },
        'javascript': {
            'react': ['react', 'jsx'],
            'vue': ['vue'],
            'express': ['express'],
            'next': ['next'],
        },
        'go': {
            'gin': ['gin-gonic'],
            'echo': ['labstack/echo'],
        },
        'rust': {
            'actix': ['actix'],
            'rocket': ['rocket'],
        },
    }
    
    def __init__(self, 
                 registry: RepoRegistry,
                 plugin_loader: PluginLoader,
                 health_engine: HealthEngine,
                 state_manager: StateManager):
        self.registry = registry
        self.plugin_loader = plugin_loader
        self.health = health_engine
        self.state = state_manager
    
    def detect_all_languages(self, repo_path: Path) -> List[tuple[str, int]]:
        """Detect all languages present in a repository with scores.

        Returns languages sorted by score descending. Useful for mixed-language
        repos (e.g. Zulip-style: Python backend + TypeScript frontend).
        A language must have a minimum score to be included (avoids false positives
        from lock files or generic configs).
        """
        repo_path = Path(repo_path).resolve()
        scores: Dict[str, int] = {}

        # Check for marker files
        for language, markers in self.LANGUAGE_MARKERS.items():
            score = 0
            for marker in markers:
                if (repo_path / marker).exists():
                    score += 1
            scores[language] = score

        # Check package.json for more specific detection
        package_json = repo_path / 'package.json'
        if package_json.exists():
            try:
                with open(package_json) as f:
                    pkg = json.load(f)

                deps = {**pkg.get('dependencies', {}),
                        **pkg.get('devDependencies', {})}

                # TypeScript indicators
                if 'typescript' in deps or any('@types/' in d for d in deps):
                    scores['typescript'] = scores.get('typescript', 0) + 5

                # Check for framework markers in dependencies
                for fw, markers in self.FRAMEWORK_MARKERS.get('typescript', {}).items():
                    for marker in markers:
                        if marker in deps:
                            scores['typescript'] = scores.get('typescript', 0) + 2
            except:
                pass

        # Check requirements.txt for Python
        requirements = repo_path / 'requirements.txt'
        if requirements.exists():
            try:
                content = requirements.read_text().lower()
                for fw, markers in self.FRAMEWORK_MARKERS.get('python', {}).items():
                    for marker in markers:
                        if marker in content:
                            scores['python'] = scores.get('python', 0) + 2
            except:
                pass

        # Filter: require minimum score to count as present (avoids lock-file false positives)
        MIN_SCORE = 1
        return sorted(
            [(lang, s) for lang, s in scores.items() if s >= MIN_SCORE],
            key=lambda x: x[1],
            reverse=True,
        )

    def detect_language(self, repo_path: Path) -> LanguageInfo:
        """Detect the primary language of a repository.

        For mixed-language repos, the secondary languages are also recorded
        on the returned LanguageInfo so that baseline checks and plugins
        can be extended accordingly.
        """
        ranked = self.detect_all_languages(repo_path)
        if not ranked:
            return LanguageInfo(name='unknown')

        best_language = ranked[0][0]
        secondary = [lang for lang, _ in ranked[1:] if self._is_actionable_language(lang)]

        version = self._get_language_version(repo_path, best_language)

        return LanguageInfo(
            name=best_language,
            version=version,
            package_manager=self.detect_package_manager(repo_path, best_language),
            build_tool=self.detect_build_tool(repo_path, best_language),
            secondary_languages=secondary,
        )

    def _is_actionable_language(self, language: str) -> bool:
        """Return True if a detected language has actionable plugin coverage."""
        return language in {'python', 'typescript', 'javascript', 'go', 'rust'}
    
    def detect_package_manager(self, repo_path: Path, language: str) -> Optional[str]:
        """Infer the package manager used by the repository."""
        if language in ('typescript', 'javascript'):
            if (repo_path / 'pnpm-lock.yaml').exists():
                return 'pnpm'
            if (repo_path / 'yarn.lock').exists():
                return 'yarn'
            if (repo_path / 'bun.lock').exists() or (repo_path / 'bun.lockb').exists():
                return 'bun'
            if (repo_path / 'package-lock.json').exists() or (repo_path / 'package.json').exists():
                return 'npm'
        if language == 'python':
            if (repo_path / 'poetry.lock').exists():
                return 'poetry'
            if (repo_path / 'uv.lock').exists():
                return 'uv'
            if (repo_path / 'Pipfile').exists():
                return 'pipenv'
            if (repo_path / 'requirements.txt').exists() or (repo_path / 'pyproject.toml').exists():
                return 'pip'
        return None

    def detect_build_tool(self, repo_path: Path, language: str) -> Optional[str]:
        """Infer the primary build/test tool."""
        if language in ('typescript', 'javascript') and (repo_path / 'package.json').exists():
            return 'package-scripts'
        if language == 'python':
            if (repo_path / 'pyproject.toml').exists():
                return 'pyproject'
            if (repo_path / 'setup.py').exists():
                return 'setuptools'
        if language == 'go' and (repo_path / 'go.mod').exists():
            return 'go'
        if language == 'rust' and (repo_path / 'Cargo.toml').exists():
            return 'cargo'
        return None

    def infer_baseline_checks(self, repo_path: Path, language: LanguageInfo) -> List[List[str]]:
        """Infer sensible baseline validation commands for a repo.

        For mixed-language repos, checks are inferred for ALL detected languages
        (primary + secondary), so that e.g. a Python+TypeScript repo gets both
        pytest and npm test commands.
        """
        commands: List[List[str]] = []

        # Infer checks for primary language
        for lang in [language.name] + language.secondary_languages:
            commands.extend(self._infer_baseline_checks_for_language(repo_path, lang))

        # Deduplicate while preserving order
        deduped: List[List[str]] = []
        seen = set()
        for cmd in commands:
            key = tuple(cmd)
            if key not in seen:
                deduped.append(cmd)
                seen.add(key)
        return deduped

    def _infer_baseline_checks_for_language(
        self, repo_path: Path, language_name: str, package_manager: Optional[str] = None
    ) -> List[List[str]]:
        """Infer baseline checks for a single language (used for primary + secondary)."""
        commands: List[List[str]] = []
        pkg_mgr = package_manager or self.detect_package_manager(repo_path, language_name)

        if language_name in ('typescript', 'javascript'):
            if not (repo_path / 'package.json').exists():
                return commands
            try:
                pkg = json.loads((repo_path / 'package.json').read_text())
                scripts = pkg.get('scripts', {})
                runner = pkg_mgr or 'npm'
                if 'test' in scripts:
                    commands.append(['npm', 'test'] if runner == 'npm' else [runner, 'test'])
                if 'lint' in scripts:
                    commands.append(['npm', 'run', 'lint'] if runner == 'npm' else [runner, 'lint'])
                if 'build' in scripts:
                    commands.append(['npm', 'run', 'build'] if runner == 'npm' else [runner, 'build'])
                if 'typecheck' in scripts:
                    commands.append(['npm', 'run', 'typecheck'] if runner == 'npm' else [runner, 'typecheck'])
            except Exception:
                pass
            return commands

        if language_name == 'python':
            has_tests = (
                (repo_path / 'tests').exists()
                or (repo_path / 'pytest.ini').exists()
                or (repo_path / 'pyproject.toml').exists()
            )
            has_ruff = (
                (repo_path / '.ruff.toml').exists()
                or (repo_path / 'ruff.toml').exists()
                or (repo_path / 'pyproject.toml').exists()
            )
            has_mypy = (
                (repo_path / 'mypy.ini').exists()
                or (repo_path / '.mypy.ini').exists()
                or (repo_path / 'pyproject.toml').exists()
            )

            if pkg_mgr == 'poetry':
                if has_tests:
                    commands.append(['poetry', 'run', 'pytest', '-q'])
                if has_ruff:
                    commands.append(['poetry', 'run', 'ruff', 'check', '.'])
                if has_mypy:
                    commands.append(['poetry', 'run', 'mypy', '.'])
            elif pkg_mgr == 'uv':
                if has_tests:
                    commands.append(['uv', 'run', 'pytest', '-q'])
                if has_ruff:
                    commands.append(['uv', 'run', 'ruff', 'check', '.'])
                if has_mypy:
                    commands.append(['uv', 'run', 'mypy', '.'])
            else:
                if has_tests:
                    commands.append(['pytest', '-q'])
                if has_ruff:
                    commands.append(['ruff', 'check', '.'])
                if has_mypy:
                    commands.append(['mypy', '.'])
            return commands

        if language_name == 'go' and (repo_path / 'go.mod').exists():
            commands.append(['go', 'test', './...'])
            return commands

        if language_name == 'rust' and (repo_path / 'Cargo.toml').exists():
            commands.append(['cargo', 'test'])
            return commands

        return commands

    def detect_monorepo(self, repo_path: Path, language: LanguageInfo) -> Dict[str, Any]:
        """Detect common monorepo/workspace patterns."""
        result = {
            'is_monorepo': False,
            'kind': None,
            'package_dirs': [],
        }
        if language.name not in {'typescript', 'javascript'}:
            return result

        package_json = repo_path / 'package.json'
        if not package_json.exists():
            return result
        try:
            pkg = json.loads(package_json.read_text())
        except Exception:
            return result

        workspaces = pkg.get('workspaces')
        if workspaces:
            result['is_monorepo'] = True
            result['kind'] = 'workspaces'
            if isinstance(workspaces, list):
                result['package_dirs'] = workspaces
            elif isinstance(workspaces, dict) and isinstance(workspaces.get('packages'), list):
                result['package_dirs'] = workspaces['packages']
            return result

        if (repo_path / 'pnpm-workspace.yaml').exists():
            result['is_monorepo'] = True
            result['kind'] = 'pnpm-workspace'
            return result

        return result

    def infer_discovery_config(self, repo_path: Path, language: LanguageInfo) -> Dict[str, Any]:
        """Infer discovery mode and execution hints."""
        docker_files = [repo_path / 'Dockerfile', repo_path / 'docker-compose.yml', repo_path / 'docker-compose.yaml']
        use_docker = any(p.exists() for p in docker_files)
        monorepo = self.detect_monorepo(repo_path, language)
        config: Dict[str, Any] = {
            'internal': True,
            'external_script': None,
            'skip_internal': False,
            'use_docker': use_docker,
            'monorepo': monorepo.get('is_monorepo', False),
        }
        if monorepo.get('is_monorepo'):
            config['monorepo_kind'] = monorepo.get('kind')
            config['package_dirs'] = monorepo.get('package_dirs', [])
        if use_docker:
            config['docker_hint'] = 'review container/service name before first live run'
        if language.name in ('typescript', 'javascript'):
            config['ecosystem'] = 'node'
        elif language.name:
            config['ecosystem'] = language.name
        return config

    def infer_fix_strategy(self, repo_path: Path, language: LanguageInfo) -> Dict[str, Any]:
        """Infer local fix backend strategy."""
        available = []
        if shutil.which('claude'):
            available.append('claude')
        if shutil.which('opencode'):
            available.append('opencode')
        available.append('deterministic')

        fix_engine = 'auto' if len(available) > 1 else available[0]
        claude_template = ''
        opencode_template = ''
        if 'claude' in available:
            claude_template = (
                'claude --dangerously-skip-permissions --print '
                '"Read {prompt_file} and apply the minimal safe fix for finding {finding_id}. '
                'Run relevant tests/build checks, keep the diff small, and exit non-zero on failure."'
            )
        if 'opencode' in available:
            opencode_template = (
                'opencode run "Read {prompt_file} and apply the minimal safe fix for finding {finding_id}. '
                'Run relevant tests/build checks, keep the diff small, and exit non-zero on failure."'
            )
        return {
            'fix_engine': fix_engine,
            'fallback_engines': available,
            'claude_template': claude_template,
            'opencode_template': opencode_template,
        }

    def infer_safety_policy(self, repo_path: Path, options: OnboardOptions) -> Dict[str, Any]:
        """Infer and normalize safety policy for a repo."""
        mode = options.mode if options.mode in {m.value for m in SafetyMode} else SafetyMode.OBSERVE.value
        profile = options.profile if options.profile in {p.value for p in SafetyProfile} else SafetyProfile.CONSERVATIVE.value

        protected = ['main', 'master']
        head_proc = None
        try:
            head_proc = subprocess.run(
                ['bash', '-lc', 'git symbolic-ref --short refs/remotes/origin/HEAD 2>/dev/null | sed "s#^origin/##"'],
                cwd=str(repo_path), text=True, capture_output=True
            )
        except Exception:
            head_proc = None
        if head_proc and head_proc.returncode == 0 and head_proc.stdout.strip():
            default_branch = head_proc.stdout.strip()
            if default_branch not in protected:
                protected.insert(0, default_branch)

        notes: List[str] = []
        if mode == SafetyMode.OBSERVE.value:
            notes.append('Observe mode blocks non-dry-run execution.')
        elif mode == SafetyMode.ISSUE_ONLY.value:
            notes.append('Issue-only mode allows issue discovery/creation but blocks PR/merge execution.')
        elif mode == SafetyMode.PR.value:
            notes.append('PR mode allows issue and PR execution but blocks merge execution.')
        elif mode == SafetyMode.MERGE.value:
            notes.append('Merge mode allows all phases; auto-merge remains separately controlled.')

        if profile == SafetyProfile.CONSERVATIVE.value:
            notes.append('Conservative profile keeps small diffs and low concurrency caps.')
        elif profile == SafetyProfile.AGGRESSIVE.value:
            notes.append('Aggressive profile raises caps; review before enabling on important repos.')

        return {
            'mode': mode,
            'profile': profile,
            'require_clean_worktree': not options.allow_dirty_worktree,
            'protected_branches': protected,
            'allow_live_on_dirty_tree': options.allow_dirty_worktree,
            'notes': notes,
        }

    def apply_safety_profile(self, config: RepoConfig) -> RepoConfig:
        """Adjust limits and GitHub settings according to the selected safety profile/mode."""
        profile = config.safety.get('profile', SafetyProfile.CONSERVATIVE.value)
        mode = config.safety.get('mode', SafetyMode.OBSERVE.value)

        if profile == SafetyProfile.CONSERVATIVE.value:
            config.limits.update({
                'open_issues_cap': 10,
                'open_prs_cap': 2,
                'max_prs_per_run': 1,
                'max_issues_per_run': 5,
                'max_files_changed': 3,
                'max_loc_diff': 120,
                'max_fix_attempts': 2,
            })
        elif profile == SafetyProfile.BALANCED.value:
            config.limits.update({
                'open_issues_cap': 20,
                'open_prs_cap': 5,
                'max_prs_per_run': 2,
                'max_issues_per_run': 10,
                'max_files_changed': 5,
                'max_loc_diff': 200,
                'max_fix_attempts': 3,
            })
        elif profile == SafetyProfile.AGGRESSIVE.value:
            config.limits.update({
                'open_issues_cap': 30,
                'open_prs_cap': 8,
                'max_prs_per_run': 3,
                'max_issues_per_run': 15,
                'max_files_changed': 8,
                'max_loc_diff': 350,
                'max_fix_attempts': 4,
            })

        config.github['live_actions'] = mode in {
            SafetyMode.ISSUE_ONLY.value,
            SafetyMode.PR.value,
            SafetyMode.MERGE.value,
        }
        # Never default auto-merge on during onboarding.
        config.github['auto_merge'] = False
        return config

    def build_review_items(self, repo_path: Path, language: LanguageInfo, config: RepoConfig) -> List[str]:
        """Highlight manual review items after onboarding."""
        items: List[str] = []
        if not config.baseline_checks:
            items.append('No baseline validation commands were inferred; review baseline_checks before live runs.')
        if config.discovery.get('use_docker'):
            items.append('Docker-backed discovery was inferred; verify the correct container/service context.')
        if config.discovery.get('monorepo'):
            items.append('Monorepo/workspace layout detected; verify whether root onboarding or package-level onboarding is safer.')
            if config.discovery.get('package_dirs'):
                items.append('Workspace package patterns detected: ' + ', '.join(config.discovery.get('package_dirs', [])))
        if not config.github.get('live_actions', False):
            items.append('GitHub live actions default to off; enable only after reviewing safety gates.')
        if (repo_path / '.github' / 'workflows').exists():
            items.append('Repo has GitHub Actions workflows; consider adding build/test commands that mirror CI.')
        if language.name == 'unknown':
            items.append('Language detection returned unknown; verify plugin and rules before enabling.')
        if language.secondary_languages:
            items.append(
                f'Mixed-language repo detected (primary={language.name}, secondary={language.secondary_languages}); '
                'baseline checks were inferred for all languages but verify plugin coverage for secondary languages.'
            )
        if config.safety.get('mode') == SafetyMode.OBSERVE.value:
            items.append('Observe mode is active; non-dry-run execution will be blocked until mode is raised.')
        if config.safety.get('require_clean_worktree', True):
            items.append('Clean working tree is required for live runs.')
        if config.safety.get('profile') == SafetyProfile.AGGRESSIVE.value:
            items.append('Aggressive profile selected; verify caps are appropriate for this repository.')
        return items

    def _get_language_version(self, repo_path: Path, language: str) -> Optional[str]:
        """Get language version from version files."""
        version_files = {
            'python': ['.python-version', 'runtime.txt'],
            'typescript': ['.nvmrc', '.node-version'],
            'javascript': ['.nvmrc', '.node-version'],
            'go': ['go.mod'],
            'rust': ['Cargo.toml'],
        }
        
        for vf in version_files.get(language, []):
            version_file = repo_path / vf
            if version_file.exists():
                try:
                    content = version_file.read_text()
                    if language == 'python':
                        return content.strip().split()[0] if content.strip() else None
                    elif language in ('typescript', 'javascript'):
                        return content.strip()
                    elif language == 'go':
                        # Extract go version from go.mod
                        for line in content.split('\n'):
                            if line.startswith('go '):
                                return line.split()[1]
                except:
                    pass
        
        return None
    
    def detect_framework(self, repo_path: Path, language: str) -> Optional[str]:
        """Detect framework for a language."""
        repo_path = Path(repo_path).resolve()
        
        if language not in self.FRAMEWORK_MARKERS:
            return None
        
        markers = self.FRAMEWORK_MARKERS[language]
        
        # Check package.json for JS/TS
        package_json = repo_path / 'package.json'
        if package_json.exists() and language in ('typescript', 'javascript'):
            try:
                with open(package_json) as f:
                    pkg = json.load(f)
                
                deps = {**pkg.get('dependencies', {}), 
                        **pkg.get('devDependencies', {})}
                
                for framework, indicators in markers.items():
                    for indicator in indicators:
                        if indicator in deps:
                            return framework
            except:
                pass
        
        # Check for Python frameworks
        if language == 'python':
            for framework, indicators in markers.items():
                for indicator in indicators:
                    # Check file existence
                    if (repo_path / indicator).exists():
                        return framework
                    # Check requirements.txt
                    req_file = repo_path / 'requirements.txt'
                    if req_file.exists():
                        try:
                            content = req_file.read_text().lower()
                            if indicator in content:
                                return framework
                        except:
                            pass
        
        return None
    
    def select_plugin(self, language: str, framework: Optional[str] = None) -> Optional[str]:
        """Select appropriate plugin for language/framework."""
        # Ensure plugins are discovered
        self.plugin_loader.discover()
        
        # Try to find plugin for this language
        plugin = self.plugin_loader.get_for_language(language)
        if plugin:
            return plugin.id
        
        return None

    def select_template(self, repo_path: Path, language: LanguageInfo, framework: Optional[str]) -> Optional[str]:
        """Select a repo template when confidence is high."""
        if language.name in {'typescript', 'javascript'}:
            package_json = repo_path / 'package.json'
            scripts = {}
            deps = {}
            monorepo = self.detect_monorepo(repo_path, language)
            if package_json.exists():
                try:
                    pkg = json.loads(package_json.read_text())
                    scripts = pkg.get('scripts', {})
                    deps = {**pkg.get('dependencies', {}), **pkg.get('devDependencies', {})}
                except Exception:
                    pass

            if monorepo.get('is_monorepo'):
                if {'build', 'test'} & set(scripts.keys()):
                    return 'node-workspace-root'
                return 'node-workspace-root'
            if framework == 'next' or 'next' in deps:
                return 'next-app'
            if framework == 'react' or 'react' in deps:
                return 'react-app'
            if framework in {'express', 'nestjs'} or 'express' in deps or '@nestjs' in deps:
                return 'node-api'
            if {'build', 'test'} & set(scripts.keys()):
                return 'node-library'
            return 'node-library' if package_json.exists() else None

        if language.name == 'python':
            if framework == 'django':
                return 'django-app'
            if framework == 'fastapi':
                return 'fastapi-app'
            if framework in {'flask'}:
                return 'python-api'
            if (repo_path / 'manage.py').exists():
                return 'django-app'
            if (repo_path / 'app.py').exists() or (repo_path / 'main.py').exists():
                return 'python-api'
            if (repo_path / 'setup.py').exists() or (repo_path / 'pyproject.toml').exists():
                return 'python-library'
            return 'python-library'

        if language.name == 'go':
            return 'go-service'

        if language.name == 'rust':
            return 'rust-crate'

        return None
    
    def generate_config(self, 
                        repo_path: Path, 
                        name: str,
                        language: LanguageInfo,
                        framework: Optional[str],
                        plugin_id: str,
                        template_name: Optional[str] = None) -> RepoConfig:
        """Generate repository configuration."""
        baseline_checks = self.infer_baseline_checks(repo_path, language)
        discovery = self.infer_discovery_config(repo_path, language)
        fix_strategy = self.infer_fix_strategy(repo_path, language)
        template_name = template_name or self.select_template(repo_path, language, framework)

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
            fix_engine=fix_strategy['fix_engine'],
            fallback_engines=fix_strategy['fallback_engines'],
            claude_template=fix_strategy['claude_template'],
            opencode_template=fix_strategy['opencode_template'],
            github={
                'live_actions': False,
                'auto_merge': False,
            },
            meta={
                'onboarding_version': 2,
                'template': template_name,
                'inferred_by': 'template' if template_name else 'heuristic',
                'secondary_languages': language.secondary_languages,
            },
        )
        merged.id = generate_id('repo')
        return merged
    
    def run_discovery(self, 
                      repo_path: Path, 
                      config: RepoConfig) -> List[Finding]:
        """Run discovery using plugin."""
        plugin = self.plugin_loader.get(config.plugin_id)
        if not plugin:
            return []
        
        return plugin.discover(repo_path, config.discovery)
    
    def onboard(self, 
                repo_path: Path, 
                options: OnboardOptions) -> OnboardResult:
        """Execute full onboarding workflow."""
        repo_path = Path(repo_path).resolve()
        
        # 1. Determine name
        name = options.name or repo_path.name
        
        # 2. Check if already onboarded
        existing = self.registry.find_by_path(repo_path)
        if existing:
            raise ValueError(f"Repo already onboarded: {existing.config.name}")
        
        # 3. Detect language
        language = self.detect_language(repo_path)
        if options.language:
            language.name = options.language
        
        # 4. Detect framework
        framework = options.framework or self.detect_framework(repo_path, language.name)
        
        # 5. Select plugin
        plugin_id = options.plugin_id or self.select_plugin(language.name, framework)
        if not plugin_id:
            raise ValueError(f"No plugin available for language: {language.name}")
        
        selected_template = options.template or self.select_template(repo_path, language, framework)

        # 6. Generate config
        config = self.generate_config(repo_path, name, language, framework, plugin_id, template_name=selected_template)
        config.safety = self.infer_safety_policy(repo_path, options)
        config = self.apply_safety_profile(config)

        # Enforce onboarding safety gate
        if config.safety.get('require_clean_worktree', True):
            status_proc = subprocess.run(['bash', '-lc', 'git status --porcelain'], cwd=str(repo_path), text=True, capture_output=True)
            if status_proc.returncode == 0 and status_proc.stdout.strip() and config.github.get('live_actions', False):
                raise ValueError('Refusing live-enabled onboarding on a dirty worktree; commit/stash changes or use --allow-dirty-worktree with observe mode first.')
        
        review_items = self.build_review_items(repo_path, language, config)

        # 7. Create repo in registry
        repo = self.registry.create(config)
        
        # 8. Run discovery
        findings = self.run_discovery(repo_path, config)
        
        # 9. Calculate health
        health_score = self.health.calculate(findings)
        
        # 10. Create baseline
        baseline = None
        if options.capture_baseline:
            findings_file = str(self.state.get_findings_file(name))
            self.state.append_findings(name, findings)
            baseline = self.health.create_baseline(
                repo_id=config.id,
                findings=findings,
                health=health_score,
                findings_file=findings_file
            )
            self.state.save_baseline(name, baseline.to_dict())
        
        # 11. Update repo state
        self.registry.update(name, {
            'status': RepoStatus.READY.value,
            'onboarded_at': now_iso(),
            'current_findings_count': len(findings),
            'current_health_score': health_score.score,
        })
        
        # 12. Save health snapshot
        self.health.save_health_snapshot(
            name, 
            health_score, 
            len(findings),
            self.state._get_state_dir(name)
        )
        
        # 13. Return result
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
