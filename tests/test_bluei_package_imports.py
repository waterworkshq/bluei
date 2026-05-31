from pathlib import Path


def test_bluei_top_level_exports_version():
    import bluei

    assert bluei.__version__


def test_app_modules_import_from_canonical_package():
    from bluei.app.config import ConfigManager
    from bluei.app.models import RepoConfig
    from bluei.app.runner import RunEngine
    from bluei.app.state import StateManager

    assert ConfigManager
    assert RepoConfig
    assert RunEngine
    assert StateManager


def test_engine_modules_import_from_canonical_package():
    from bluei.engine.cli import main
    from bluei.engine.constants import DETECTOR_CATALOG, DEFAULT_BATCH_RULES_PATH
    from bluei.engine.models import Finding
    from bluei.engine.recipe_engine import builtin_recipe_dir, staged_recipe_dir

    assert main
    assert DETECTOR_CATALOG
    assert Finding
    assert DEFAULT_BATCH_RULES_PATH.exists()
    assert builtin_recipe_dir().exists()
    assert staged_recipe_dir().exists()


def test_review_and_campaign_modules_import_from_canonical_packages():
    from bluei.review.cycle import ReviewCycleEngine
    from bluei.campaigns.executor import CampaignExecutor
    from bluei.campaigns.planner import CampaignPlanner
    from bluei.campaigns.state import CampaignStateManager
    from bluei.campaigns.types import CampaignStatus

    assert ReviewCycleEngine
    assert CampaignExecutor
    assert CampaignPlanner
    assert CampaignStateManager
    assert CampaignStatus.planning


def test_engine_resource_files_are_colocated_with_bluei_engine():
    import bluei.engine.constants as constants
    import bluei.engine.recipe_engine as recipe_engine

    engine_dir = Path(constants.__file__).resolve().parent
    assert constants.DEFAULT_BATCH_RULES_PATH == engine_dir / "batch_rules.yaml"
    assert (engine_dir / "llm_fixable_rules.yaml").exists()
    assert recipe_engine.builtin_recipe_dir() == engine_dir / "recipes" / "built-in"
    assert recipe_engine.staged_recipe_dir() == engine_dir / "recipes" / "staged"
