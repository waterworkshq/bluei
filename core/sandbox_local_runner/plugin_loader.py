"""plugin_loader — Bridge between sandbox_local_runner and the qa-agent plugin system.

Loads language-specific plugins from the plugins/ directory and runs their
discover() method during cycle execution. Integrates with discover_findings()
in orchestrator.py to merge plugin findings with detector findings.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

try:
    from .models import Finding
except ImportError:
    # Fall back to absolute import when not running as part of the package
    import sys
    from pathlib import Path
    _self_dir = Path(__file__).resolve().parent
    if str(_self_dir.parent.parent) not in sys.path:
        sys.path.insert(0, str(_self_dir.parent.parent))
    from core.sandbox_local_runner.models import Finding


# ── Detect repo languages without importing qa_agent ────────────
LANGUAGE_MARKERS: Dict[str, List[str]] = {
    'python': ['setup.py', 'pyproject.toml', 'requirements.txt', 'Pipfile', 'setup.cfg'],
    'typescript': ['tsconfig.json'],
    'javascript': ['package.json'],
    'go': ['go.mod', 'go.sum'],
    'rust': ['Cargo.toml', 'Cargo.lock'],
}


def detect_repo_languages(repo_path: Path, min_score: int = 1) -> List[str]:
    """Detect languages present in a repo by checking marker files."""
    scores: Dict[str, int] = {}
    for lang, markers in LANGUAGE_MARKERS.items():
        score = sum(1 for m in markers if (repo_path / m).exists())
        if score > 0:
            scores[lang] = score

    # Refine JavaScript/TypeScript using package.json
    pkg_json = repo_path / 'package.json'
    if pkg_json.exists():
        try:
            pkg = json.loads(pkg_json.read_text())
            deps = {**pkg.get('dependencies', {}), **pkg.get('devDependencies', {})}
            if 'typescript' in deps or any(k.startswith('@types/') for k in deps):
                scores['typescript'] = scores.get('typescript', 0) + 5
        except (json.JSONDecodeError, OSError):
            pass

    return sorted(
        [lang for lang, s in scores.items() if s >= min_score],
        key=lambda l: scores[l],
        reverse=True,
    )


# ── Plugin discovery ────────────────────────────────────────────
def _find_plugins_dir(repo_path: Path) -> Optional[Path]:
    """Locate the qa-agent plugins/ directory relative to the given repo path."""
    # Traverse up from repo_path to find the qa-agent root
    candidate = repo_path.resolve()
    for _ in range(10):
        plugins_candidate = candidate / 'plugins'
        if plugins_candidate.exists() and plugins_candidate.is_dir():
            # Verify it has at least one plugin.yaml
            if list(plugins_candidate.rglob('plugin.yaml')):
                return plugins_candidate
        parent = candidate.parent
        if parent == candidate:
            break
        candidate = parent

    # Fall back to the known location relative to this file
    here = Path(__file__).resolve().parent  # sandbox_local_runner/
    agent_root = here.parent.parent  # qa-agent/
    known = agent_root / 'plugins'
    if known.exists():
        return known

    return None


def _load_plugin_module(plugin_id: str, plugin_dir: Path) -> Optional[Any]:
    """Import a plugin from its plugin.py file."""
    plugin_file = plugin_dir / 'plugin.py'
    if not plugin_file.exists():
        return None

    try:
        spec = importlib.util.spec_from_file_location(
            f'loaded_plugin_{plugin_id}',
            plugin_file,
        )
        if not spec or not spec.loader:
            return None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        plugin_class = getattr(module, 'Plugin', None)
        if plugin_class is None:
            return None
        return plugin_class()
    except Exception:
        return None


def _get_plugin_manifest(plugin_dir: Path) -> Optional[Dict[str, Any]]:
    """Read plugin.yaml manifest."""
    manifest_file = plugin_dir / 'plugin.yaml'
    if not manifest_file.exists():
        return None
    try:
        import yaml
        with open(manifest_file) as f:
            return yaml.safe_load(f)
    except ImportError:
        # yaml not available — try JSON fallback (very rare)
        return None
    except Exception:
        return None


def discover_plugins(plugins_dir: Path) -> Dict[str, Dict[str, Any]]:
    """Scan plugins directory and return manifests keyed by plugin ID."""
    manifests: Dict[str, Dict[str, Any]] = {}
    if not plugins_dir.exists():
        return manifests

    for entry in sorted(plugins_dir.iterdir()):
        if not entry.is_dir():
            continue
        manifest = _get_plugin_manifest(entry)
        if manifest and manifest.get('id'):
            plugin_id = manifest['id']
            manifest['_path'] = str(entry)
            manifests[plugin_id] = manifest

    return manifests


def load_plugin(plugin_id: str, plugins_dir: Path) -> Optional[Any]:
    """Load a specific plugin instance by ID."""
    plugin_dir = plugins_dir / plugin_id
    if not plugin_dir.exists():
        # Try to find it by scanning manifests
        manifests = discover_plugins(plugins_dir)
        manifest = manifests.get(plugin_id)
        if not manifest:
            return None
        plugin_dir = Path(manifest['_path'])

    return _load_plugin_module(plugin_id.replace('plugin-', ''), plugin_dir)


def load_applicable_plugins(repo_path: Path, plugins_dir: Optional[Path] = None) -> List[Any]:
    """Load all plugins that are applicable to the given repository.

    Detects repo languages, scans plugins, and returns instantiated plugin
    objects for the matching plugins.
    """
    if plugins_dir is None:
        found_dir = _find_plugins_dir(repo_path)
        if found_dir is None:
            return []
        plugins_dir = found_dir

    manifests = discover_plugins(plugins_dir)
    if not manifests:
        return []

    languages = detect_repo_languages(repo_path)

    loaded: List[Any] = []
    for plugin_id, manifest in manifests.items():
        plugin_languages = [l.lower() for l in (manifest.get('languages') or [])]
        # Match if the plugin supports any detectable language
        matches = any(lang.lower() in plugin_languages for lang in languages)
        if not matches:
            continue

        # Direct ID-based loading
        plugin_instance = load_plugin(plugin_id, plugins_dir)
        if plugin_instance is None:
            # Fall back to inferring path from manifest
            plugin_dir = Path(manifest['_path'])
            plugin_instance = _load_plugin_module(plugin_id, plugin_dir)

        if plugin_instance is not None and hasattr(plugin_instance, 'detect'):
            if not plugin_instance.detect(repo_path):
                continue

        if plugin_instance is not None:
            loaded.append(plugin_instance)

    return loaded


def run_plugin_discovery(
    repo_path: Path,
    plugins_dir: Optional[Path] = None,
    config: Optional[Dict[str, Any]] = None,
) -> List[Finding]:
    """Run discovery against all applicable plugins and return findings.

    This is the main integration point called from discover_findings().
    It loads plugins matching the repo's languages and calls each
    plugin's discover() method.
    """
    if config is None:
        config = {}

    plugins = load_applicable_plugins(repo_path, plugins_dir)
    if not plugins:
        return []

    all_findings: List[Finding] = []
    for plugin in plugins:
        try:
            plugin_config = config.get(plugin.id, {})
            findings = plugin.discover(repo_path, plugin_config)
            if findings:
                all_findings.extend(findings)
        except Exception:
            continue

    return all_findings
