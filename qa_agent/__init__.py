"""QA Agent - Autonomous Repository Quality Assurance."""

__version__ = "2.5.0"
__author__ = "Red"

from .models import (
    RepoStatus,
    AgentStatus,
    LanguageInfo,
    RepoConfig,
    Finding,
    HealthScore,
    Baseline,
    Repo,
    Run,
    generate_id,
    now_iso,
)
from .config import ConfigManager, WORKSPACE
from .registry import RepoRegistry
from .health import HealthEngine, HealthWeights
from .state import StateManager
from .plugins import PluginLoader, DiscoveryPlugin
from .onboard import OnboardEngine, OnboardOptions, OnboardResult
from .runner import RunEngine, RunOptions, RunResult
from .report import ReportGenerator

__all__ = [
    # Models
    'RepoStatus',
    'AgentStatus',
    'LanguageInfo',
    'RepoConfig',
    'Finding',
    'HealthScore',
    'Baseline',
    'Repo',
    'Run',
    'generate_id',
    'now_iso',
    # Config
    'ConfigManager',
    'WORKSPACE',
    # Registry
    'RepoRegistry',
    # Health
    'HealthEngine',
    'HealthWeights',
    # State
    'StateManager',
    # Plugins
    'PluginLoader',
    'DiscoveryPlugin',
    # Onboarding
    'OnboardEngine',
    'OnboardOptions',
    'OnboardResult',
    # Runner
    'RunEngine',
    'RunOptions',
    'RunResult',
    # Report
    'ReportGenerator',
]
