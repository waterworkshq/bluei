"""Tests for bluei/app/brand.py — term mapping, display helpers, mode constants."""

from bluei.app.brand import (
    MODE_CHOICES_ALL,
    MODE_CHOICES_BRAND,
    MODE_CHOICES_OLD,
    display_health,
    display_mode,
    t,
)


class TestTermTranslation:
    def test_known_mappings(self):
        assert t("finding") == "speck"
        assert t("findings") == "specks"
        assert t("health_score") == "vitality"
        assert t("health score") == "vitality"
        assert t("Health Score") == "Vitality"
        assert t("safety_mode") == "care_level"
        assert t("safety mode") == "care level"
        assert t("Safety Mode") == "Care Level"
        assert t("baseline") == "starting point"
        assert t("snapshot") == "check-in"
        assert t("onboard") == "register"
        assert t("repo") == "project"
        assert t("repository") == "project"

    def test_unknown_term_passthrough(self):
        assert t("unknown_term") == "unknown_term"
        assert t("") == ""
        assert t("health") == "health"


class TestDisplayHealth:
    def test_formats_score(self):
        assert display_health(82.3) == "Vitality: 82.3/100"
        assert display_health(100.0) == "Vitality: 100.0/100"
        assert display_health(0.0) == "Vitality: 0.0/100"
        assert display_health(50.567) == "Vitality: 50.6/100"


class TestDisplayMode:
    def test_known_brand_modes(self):
        for mode in MODE_CHOICES_BRAND:
            result = display_mode(mode)
            assert isinstance(result, str)
            assert len(result) > 0


class TestModeConstants:
    def test_old_modes(self):
        assert "observe" in MODE_CHOICES_OLD
        assert "issue-only" in MODE_CHOICES_OLD
        assert "pr" in MODE_CHOICES_OLD
        assert "merge" in MODE_CHOICES_OLD

    def test_brand_modes(self):
        assert "watch-only" in MODE_CHOICES_BRAND
        assert "note-only" in MODE_CHOICES_BRAND
        assert "offer-fixes" in MODE_CHOICES_BRAND
        assert "full-care" in MODE_CHOICES_BRAND

    def test_all_combines_old_and_brand(self):
        assert set(MODE_CHOICES_ALL) == set(MODE_CHOICES_OLD + MODE_CHOICES_BRAND)
        assert len(MODE_CHOICES_ALL) == len(MODE_CHOICES_OLD) + len(MODE_CHOICES_BRAND)
