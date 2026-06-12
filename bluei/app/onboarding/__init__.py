"""Repository onboarding engine.

Re-exports for backward compatibility — all logic lives in submodules.
"""

import logging

_logger = logging.getLogger(__name__)

from .detection import detect_git_remote as detect_git_remote
from .detection import detect_frameworks as detect_frameworks
from .engine import OnboardEngine as OnboardEngine
from .engine import OnboardOptions as OnboardOptions
from .engine import OnboardResult as OnboardResult


def detect_language(path):
    """Detect the primary language of a repository by scanning file extensions.

    Returns one of: 'python', 'typescript', 'javascript', or 'unknown'.
    This is a lightweight standalone function useful for quick auto-detection.
    """
    from .detection import detect_language_simple

    return detect_language_simple(path)
