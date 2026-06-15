"""Tests for H3 (config schema versioning) and H4 (new language templates)."""


class TestConfigSchema:
    """H3: Formal config schema versioning."""

    def test_schemas_defined_for_v1_and_v2(self):
        from bluei.app.config_schema import SCHEMAS

        assert 1 in SCHEMAS
        assert 2 in SCHEMAS

    def test_latest_version_is_2(self):
        from bluei.app.config_schema import get_latest_version

        assert get_latest_version() == 2

    def test_validate_valid_v2_config(self):
        from bluei.app.config_schema import validate_config

        config = {
            "id": "repo-test",
            "name": "test",
            "path": "/tmp/test",
            "language": "python",
            "meta": {"onboarding_version": 2},
        }
        errors = validate_config(config)
        assert errors == []

    def test_validate_missing_required_field(self):
        from bluei.app.config_schema import validate_config

        config = {
            "name": "test",
            "path": "/tmp/test",
            "language": "python",
        }
        errors = validate_config(config)
        assert any("id" in e for e in errors)

    def test_validate_wrong_type(self):
        from bluei.app.config_schema import validate_config

        config = {
            "id": "repo-test",
            "name": "test",
            "path": "/tmp/test",
            "language": "python",
            "enabled": "yes",  # Should be bool, not str
        }
        errors = validate_config(config)
        assert any("enabled" in e and "bool" in e for e in errors)

    def test_validate_unknown_version(self):
        from bluei.app.config_schema import validate_config

        config = {"id": "x", "name": "x", "path": "/x", "language": "x"}
        errors = validate_config(config, version=99)
        assert any("Unknown schema version" in e for e in errors)

    def test_apply_defaults_fills_missing_fields(self):
        from bluei.app.config_schema import apply_defaults

        config = {"id": "x", "name": "x", "path": "/x", "language": "python"}
        result = apply_defaults(config, version=2)
        assert "safety" in result
        assert result["safety"]["mode"] == "observe"
        assert result["fix_engine"] == "auto"
        assert result["enabled"] is True

    def test_apply_defaults_does_not_mutate_input(self):
        from bluei.app.config_schema import apply_defaults

        config = {"id": "x", "name": "x", "path": "/x", "language": "python"}
        apply_defaults(config, version=2)
        assert "safety" not in config  # Original unchanged

    def test_migration_path_documented(self):
        from bluei.app.config_schema import get_migration_path

        path = get_migration_path(1, 2)
        assert path is not None
        assert "V1→V2" in path

    def test_v2_schema_has_safety_and_github(self):
        from bluei.app.config_schema import SCHEMA_V2

        optional = SCHEMA_V2["optional_top_level"]
        assert "safety" in optional
        assert "github" in optional
        assert "limits" in optional
        assert "plugin_id" in optional


class TestNewLanguageTemplates:
    """H4: Java, Ruby, PHP templates and detection."""

    def test_java_maven_template_exists(self):
        from pathlib import Path

        template = (
            Path(__file__).resolve().parents[1]
            / "templates"
            / "repos"
            / "java-maven.yaml"
        )
        assert template.exists(), "java-maven.yaml should exist"

    def test_java_gradle_template_exists(self):
        from pathlib import Path

        template = (
            Path(__file__).resolve().parents[1]
            / "templates"
            / "repos"
            / "java-gradle.yaml"
        )
        assert template.exists(), "java-gradle.yaml should exist"

    def test_ruby_bundler_template_exists(self):
        from pathlib import Path

        template = (
            Path(__file__).resolve().parents[1]
            / "templates"
            / "repos"
            / "ruby-bundler.yaml"
        )
        assert template.exists(), "ruby-bundler.yaml should exist"

    def test_php_composer_template_exists(self):
        from pathlib import Path

        template = (
            Path(__file__).resolve().parents[1]
            / "templates"
            / "repos"
            / "php-composer.yaml"
        )
        assert template.exists(), "php-composer.yaml should exist"

    def test_java_markers_in_language_detect(self):
        """LANGUAGE_MARKERS should include java/ruby/php entries."""
        from bluei.engine.language_detect import LANGUAGE_MARKERS

        assert "java" in LANGUAGE_MARKERS
        assert "pom.xml" in LANGUAGE_MARKERS["java"]
        assert "ruby" in LANGUAGE_MARKERS
        assert "Gemfile" in LANGUAGE_MARKERS["ruby"]
        assert "php" in LANGUAGE_MARKERS
        assert "composer.json" in LANGUAGE_MARKERS["php"]

    def test_select_template_java_maven(self, tmp_path):
        """H4: Maven project should select java-maven template."""
        from bluei.app.onboarding.templates import select_template
        from bluei.app.models import LanguageInfo

        (tmp_path / "pom.xml").write_text("<project></project>")
        lang = LanguageInfo(name="java")
        template = select_template(tmp_path, lang, None)
        assert template == "java-maven"

    def test_select_template_java_gradle(self, tmp_path):
        """H4: Gradle project should select java-gradle template."""
        from bluei.app.onboarding.templates import select_template
        from bluei.app.models import LanguageInfo

        (tmp_path / "build.gradle").write_text("apply plugin: 'java'")
        lang = LanguageInfo(name="java")
        template = select_template(tmp_path, lang, None)
        assert template == "java-gradle"

    def test_select_template_ruby(self, tmp_path):
        """H4: Ruby project should select ruby-bundler template."""
        from bluei.app.onboarding.templates import select_template
        from bluei.app.models import LanguageInfo

        (tmp_path / "Gemfile").write_text("source 'https://rubygems.org'")
        lang = LanguageInfo(name="ruby")
        template = select_template(tmp_path, lang, None)
        assert template == "ruby-bundler"

    def test_select_template_php(self, tmp_path):
        """H4: PHP project should select php-composer template."""
        from bluei.app.onboarding.templates import select_template
        from bluei.app.models import LanguageInfo

        (tmp_path / "composer.json").write_text('{"require": {}}')
        lang = LanguageInfo(name="php")
        template = select_template(tmp_path, lang, None)
        assert template == "php-composer"
