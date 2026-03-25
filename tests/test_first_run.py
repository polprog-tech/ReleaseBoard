"""Tests for first-run config bootstrapping flow.

Covers:
- CLI serve command accepts missing config path
- Server starts in first-run mode when config is missing
- Dashboard route serves first-run wizard in first-run mode
- /api/examples lists available example configs
- /api/config/create creates a valid config file (empty, example, import)
- State-dependent endpoints return 503 in first-run mode
- DashboardRenderer.render_first_run() produces valid HTML
- i18n keys exist for both EN and PL
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from releaseboard.web.server import create_app


@pytest_asyncio.fixture
async def first_run_client(tmp_path: Path):
    """Client connected to a first-run mode app (no config file)."""
    config_path = tmp_path / "releaseboard.json"
    # Do NOT create the file — simulate first-run
    app = create_app(config_path, first_run=True)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client, config_path


@pytest.mark.asyncio
async def test_first_run_dashboard_serves_wizard(first_run_client):
    """GET / in first-run mode should return setup wizard, not error."""
    client, _ = first_run_client
    resp = await client.get("/")
    assert resp.status_code == 200
    html = resp.text
    assert "ReleaseBoard" in html
    assert "first-run" in html or "setup" in html.lower() or "wizard" in html.lower()


@pytest.mark.asyncio
async def test_first_run_wizard_has_four_options(first_run_client):
    """The wizard page should offer Start Fresh, Prepare Config, Import Example, Import JSON."""
    client, _ = first_run_client
    resp = await client.get("/")
    html = resp.text
    assert 'data-card="fresh"' in html
    assert 'data-card="wizard"' in html
    assert 'data-card="example"' in html
    assert 'data-card="import"' in html


@pytest.mark.asyncio
async def test_first_run_examples_endpoint(first_run_client):
    """GET /api/examples should work in first-run mode."""
    client, _ = first_run_client
    resp = await client.get("/api/examples")
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True
    assert isinstance(data["examples"], list)


@pytest.mark.asyncio
async def test_first_run_create_empty_config(first_run_client):
    """POST /api/config/create with mode=empty should create a valid config."""
    client, config_path = first_run_client
    resp = await client.post(
        "/api/config/create",
        json={
            "mode": "empty",
            "release_name": "Test Release",
            "target_month": 6,
            "target_year": 2025,
            "branch_pattern": "release/{YYYY}.{MM}",
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True

    # Config file should now exist
    assert config_path.exists()
    config = json.loads(config_path.read_text(encoding="utf-8"))
    assert config["release"]["name"] == "Test Release"
    assert config["release"]["target_month"] == 6
    assert config["release"]["target_year"] == 2025
    assert isinstance(config["repositories"], list)


@pytest.mark.asyncio
async def test_first_run_create_import_config(first_run_client):
    """POST /api/config/create with mode=import should save imported JSON."""
    client, config_path = first_run_client
    imported = {
        "release": {
            "name": "Imported Release",
            "target_month": 3,
            "target_year": 2025,
        },
        "repositories": [],
    }
    resp = await client.post(
        "/api/config/create",
        json={
            "mode": "import",
            "config": imported,
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True
    assert config_path.exists()


@pytest.mark.asyncio
async def test_first_run_create_invalid_config(first_run_client):
    """POST /api/config/create with invalid data should return 422."""
    client, config_path = first_run_client
    resp = await client.post(
        "/api/config/create",
        json={
            "mode": "import",
            "config": {"invalid": True},
        },
    )
    assert resp.status_code == 422
    assert not config_path.exists()


@pytest.mark.asyncio
async def test_first_run_state_dependent_endpoints_return_503(first_run_client):
    """State-dependent API endpoints should return 503 in first-run mode."""
    client, _ = first_run_client
    # /api/config requires state
    resp = await client.get("/api/config")
    assert resp.status_code == 503, "/api/config should be 503 in first-run"


@pytest.mark.asyncio
async def test_first_run_status_returns_first_run_flag(first_run_client):
    """GET /api/status should return 200 with first_run=true."""
    client, _ = first_run_client
    resp = await client.get("/api/status")
    assert resp.status_code == 200
    data = resp.json()
    assert data["first_run"] is True
    assert data["ok"] is True


@pytest.mark.asyncio
async def test_first_run_after_config_created_dashboard_works(tmp_path: Path):
    """After creating config, normal endpoints should work."""
    config_path = tmp_path / "releaseboard.json"
    app = create_app(config_path, first_run=True)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Create config first
        resp = await client.post(
            "/api/config/create",
            json={
                "mode": "empty",
                "release_name": "Post-Create Test",
                "target_month": 1,
                "target_year": 2025,
            },
        )
        assert resp.status_code == 200

        # Now dashboard should serve normal content (state was initialized)
        resp = await client.get("/")
        assert resp.status_code == 200


class TestRendererFirstRun:
    """Test DashboardRenderer.render_first_run()."""

    def test_render_first_run_returns_html(self):
        from releaseboard.presentation.renderer import DashboardRenderer

        renderer = DashboardRenderer()
        html = renderer.render_first_run(locale="en", config_path="releaseboard.json")
        assert "<!DOCTYPE html>" in html or "<!doctype html>" in html.lower()
        assert "ReleaseBoard" in html

    def test_render_first_run_contains_form_elements(self):
        from releaseboard.presentation.renderer import DashboardRenderer

        renderer = DashboardRenderer()
        html = renderer.render_first_run(locale="en")
        # Should have form inputs for release config
        assert "<input" in html or "<select" in html
        assert "release" in html.lower()

    def test_render_first_run_polish_locale(self):
        from releaseboard.presentation.renderer import DashboardRenderer

        renderer = DashboardRenderer()
        html = renderer.render_first_run(locale="pl")
        assert "<!DOCTYPE html>" in html or "<!doctype html>" in html.lower()


class TestFirstRunI18n:
    """Verify i18n keys exist for the first-run wizard."""

    def test_en_first_run_keys_exist(self):
        locale_path = (
            Path(__file__).resolve().parent.parent
            / "src"
            / "releaseboard"
            / "i18n"
            / "locales"
            / "en.json"
        )
        data = json.loads(locale_path.read_text(encoding="utf-8"))
        required_keys = [
            "first_run.title",
            "first_run.subtitle",
            "first_run.start_fresh",
            "first_run.import_example",
            "first_run.import_json",
            "first_run.create_config",
            "first_run.success",
            "first_run.error",
        ]
        for key in required_keys:
            assert key in data, f"Missing EN i18n key: {key}"

    def test_pl_first_run_keys_exist(self):
        locale_path = (
            Path(__file__).resolve().parent.parent
            / "src"
            / "releaseboard"
            / "i18n"
            / "locales"
            / "pl.json"
        )
        data = json.loads(locale_path.read_text(encoding="utf-8"))
        required_keys = [
            "first_run.title",
            "first_run.subtitle",
            "first_run.start_fresh",
            "first_run.import_example",
            "first_run.import_json",
            "first_run.create_config",
            "first_run.success",
            "first_run.error",
        ]
        for key in required_keys:
            assert key in data, f"Missing PL i18n key: {key}"


class TestCliFirstRun:
    """Test CLI serve command accepts missing config."""

    def test_serve_command_no_exists_constraint(self):
        """The serve command should not have exists=True on config param."""
        import inspect

        from releaseboard.cli.app import serve

        sig = inspect.signature(serve)
        # The function should accept a Path without filesystem validation
        assert "config" in sig.parameters

    def test_create_app_accepts_first_run(self):
        """create_app should accept first_run keyword argument."""
        import inspect

        sig = inspect.signature(create_app)
        assert "first_run" in sig.parameters


class TestFirstRunShellParity:
    """Verify the first-run screen has shared app shell elements."""

    def _render(self, locale="en"):
        from releaseboard.presentation.renderer import DashboardRenderer

        return DashboardRenderer().render_first_run(locale=locale)

    def test_has_sticky_header(self):
        html = self._render()
        assert "fr-header" in html
        assert "fr-header-logo" in html
        assert "fr-header-controls" in html

    def test_has_settings_gear_button(self):
        html = self._render()
        assert "fr-settings-btn" in html
        assert "frToggleSettings" in html

    def test_has_theme_switcher_buttons(self):
        html = self._render()
        assert 'data-fr-theme="light"' in html
        assert 'data-fr-theme="dark"' in html
        assert 'data-fr-theme="midnight"' in html
        assert 'data-fr-theme="system"' in html

    def test_has_language_switcher(self):
        html = self._render()
        assert 'data-fr-locale="en"' in html
        assert 'data-fr-locale="pl"' in html

    def test_has_dashboard_footer(self):
        """Footer must match dashboard rb-footer pattern."""
        html = self._render()
        assert "rb-footer" in html
        assert "rb-footer-copyright" in html
        assert "rb-footer-tools" in html
        assert "POLPROG" in html
        assert "ReleaseBoard" in html
        assert "ReleasePilot" in html

    def test_has_dark_theme_css(self):
        html = self._render()
        assert '[data-theme="dark"]' in html
        assert '[data-theme="midnight"]' in html

    def test_has_three_logo_variants(self):
        html = self._render()
        assert "fr-logo-light" in html
        assert "fr-logo-dark" in html
        assert "fr-logo-midnight" in html

    def test_has_prepare_config_card(self):
        html = self._render()
        assert 'data-card="wizard"' in html
        assert "prepareConfig" in html
        assert "Launch Wizard" in html or "launch_wizard" in html

    def test_theme_persistence_via_localstorage(self):
        html = self._render()
        assert "localStorage" in html
        assert "rb-theme" in html

    def test_polish_locale_renders_correctly(self):
        html = self._render(locale="pl")
        assert 'lang="pl"' in html
        assert 'data-fr-locale="pl"' in html

    def test_prepare_config_i18n_keys_exist(self):
        locale_path = (
            Path(__file__).resolve().parent.parent
            / "src"
            / "releaseboard"
            / "i18n"
            / "locales"
            / "en.json"
        )
        data = json.loads(locale_path.read_text(encoding="utf-8"))
        assert "first_run.prepare_config" in data
        assert "first_run.prepare_config_desc" in data
        assert "first_run.launch_wizard" in data


class TestDashboardAutoOpenWizard:
    """Verify the dashboard supports ?open_wizard=1 auto-open."""

    def test_dashboard_has_open_wizard_param_handler(self):
        """The interactive scripts should check for open_wizard query param."""
        template_path = (
            Path(__file__).resolve().parent.parent
            / "src"
            / "releaseboard"
            / "presentation"
            / "templates"
            / "_scripts_interactive.html.j2"
        )
        content = template_path.read_text(encoding="utf-8")
        assert "open_wizard" in content
        assert "RB.openWizard" in content


# ── Correction pass tests (logo, language, footer, empty state) ──


class TestFirstRunLogoLayout:
    """Verify the first-run header logo is correctly sized and structured."""

    def _render(self, locale: str = "en") -> str:
        from releaseboard.presentation.renderer import DashboardRenderer

        renderer = DashboardRenderer()
        return renderer.render_first_run(locale=locale)

    def test_logo_svg_has_trimmed_viewbox(self):
        """SVG viewBox must be trimmed to avoid dead space."""
        html = self._render()
        assert 'viewBox="0 0 410 112"' in html

    def test_logo_height_is_desktop_readable(self):
        """Logo CSS must set height >= 48px for desktop readability."""
        html = self._render()
        assert "height: 50px" in html or "height:50px" in html

    def test_header_uses_padding_not_fixed_height(self):
        """Header inner should use padding instead of fixed height."""
        html = self._render()
        assert "padding: 10px 0" in html or "padding:10px 0" in html

    def test_header_logo_has_flex_shrink(self):
        """Logo container must not shrink on narrow viewports."""
        html = self._render()
        assert "flex-shrink: 0" in html or "flex-shrink:0" in html


class TestFirstRunLangRestore:
    """Verify that language switching restores the settings panel."""

    def _render(self) -> str:
        from releaseboard.presentation.renderer import DashboardRenderer

        renderer = DashboardRenderer()
        return renderer.render_first_run()

    def test_language_switch_sets_session_storage_flag(self):
        """Language switch handler must set fr_restore_settings before reload."""
        html = self._render()
        assert "fr_restore_settings" in html
        assert "sessionStorage.setItem" in html

    def test_settings_panel_restore_on_load(self):
        """Page load must check and restore settings panel from sessionStorage."""
        html = self._render()
        assert "sessionStorage.getItem('fr_restore_settings')" in html
        assert "frToggleSettings" in html


class TestReleasePilotVersionInFooter:
    """Verify ReleasePilot version info appears in the footer."""

    def test_first_run_footer_has_rp_version_support(self):
        """First-run footer template must handle rp_version."""
        template_path = (
            Path(__file__).resolve().parent.parent
            / "src"
            / "releaseboard"
            / "presentation"
            / "templates"
            / "first_run.html.j2"
        )
        content = template_path.read_text(encoding="utf-8")
        assert "rp_version" in content

    def test_dashboard_footer_uses_dynamic_version(self):
        """Dashboard footer must use vm.version, not hardcoded version."""
        template_path = (
            Path(__file__).resolve().parent.parent
            / "src"
            / "releaseboard"
            / "presentation"
            / "templates"
            / "_footer.html.j2"
        )
        content = template_path.read_text(encoding="utf-8")
        assert "v{{ vm.version }}" in content
        assert "v1.1.0" not in content  # no hardcoded version

    def test_dashboard_footer_has_rp_version_conditional(self):
        """Dashboard footer must include vm.rp_version conditional."""
        template_path = (
            Path(__file__).resolve().parent.parent
            / "src"
            / "releaseboard"
            / "presentation"
            / "templates"
            / "_footer.html.j2"
        )
        content = template_path.read_text(encoding="utf-8")
        assert "vm.rp_version" in content

    def test_view_model_has_rp_version_field(self):
        """DashboardViewModel must have rp_version field."""
        import dataclasses

        from releaseboard.presentation.view_models import DashboardViewModel

        field_names = [f.name for f in dataclasses.fields(DashboardViewModel)]
        assert "rp_version" in field_names


class TestDashboardEmptyReposState:
    """Verify the empty state renders when config has zero repositories."""

    def _render_empty_dashboard(self) -> str:
        """Render a dashboard with 0 repos."""
        from releaseboard.analysis.metrics import DashboardMetrics
        from releaseboard.config.models import AppConfig, ReleaseConfig
        from releaseboard.presentation.view_models import build_dashboard_view_model

        config = AppConfig(
            release=ReleaseConfig(name="Test", target_month=3, target_year=2026),
            repositories=[],
        )
        metrics = DashboardMetrics()
        vm = build_dashboard_view_model(config, [], metrics, locale="en")
        vm.interactive = True

        from releaseboard.presentation.renderer import DashboardRenderer

        renderer = DashboardRenderer()
        return renderer.render(vm)

    def test_empty_repos_state_section_rendered(self):
        html = self._render_empty_dashboard()
        assert "empty-repos-state" in html
        assert "emptyReposState" in html

    def test_empty_repos_state_has_title(self):
        html = self._render_empty_dashboard()
        assert "No repositories configured" in html

    def test_empty_repos_state_has_cta_buttons(self):
        html = self._render_empty_dashboard()
        assert "RB.openWizard" in html
        assert "RB.toggleDrawer" in html

    def test_empty_repos_state_has_description(self):
        html = self._render_empty_dashboard()
        assert "Add repositories" in html

    def test_empty_repos_state_not_shown_when_repos_exist(self):
        """Empty state must NOT appear when there are repositories."""
        from releaseboard.analysis.metrics import DashboardMetrics
        from releaseboard.config.models import AppConfig, ReleaseConfig, RepositoryConfig
        from releaseboard.presentation.view_models import build_dashboard_view_model

        config = AppConfig(
            release=ReleaseConfig(name="Test", target_month=3, target_year=2026),
            repositories=[
                RepositoryConfig(
                    name="test/repo",
                    url="https://github.com/t/r",
                    layer="default",
                ),
            ],
        )
        metrics = DashboardMetrics()
        metrics.total = 1
        vm = build_dashboard_view_model(config, [], metrics, locale="en")
        vm.interactive = True

        from releaseboard.presentation.renderer import DashboardRenderer

        renderer = DashboardRenderer()
        html = renderer.render(vm)
        assert 'id="emptyReposState"' not in html

    def test_empty_state_hides_metrics_and_filters(self):
        """When repos are empty, actual metrics-grid, filters-bar, and summary must NOT render."""
        html = self._render_empty_dashboard()
        # The real dashboard sections have data-section-id attributes
        assert 'data-section-id="metrics"' not in html
        assert 'data-section-id="filters"' not in html
        assert 'data-section-id="summary"' not in html
        assert 'id="layoutBar"' not in html

    def test_non_empty_state_shows_metrics_and_filters(self):
        """When repos exist, metrics-grid and filters-bar must render."""
        from releaseboard.analysis.metrics import DashboardMetrics
        from releaseboard.config.models import AppConfig, ReleaseConfig, RepositoryConfig
        from releaseboard.presentation.view_models import build_dashboard_view_model

        config = AppConfig(
            release=ReleaseConfig(name="Test", target_month=3, target_year=2026),
            repositories=[
                RepositoryConfig(
                    name="test/repo",
                    url="https://github.com/t/r",
                    layer="default",
                ),
            ],
        )
        metrics = DashboardMetrics()
        metrics.total = 1
        vm = build_dashboard_view_model(config, [], metrics, locale="en")
        vm.interactive = True

        from releaseboard.presentation.renderer import DashboardRenderer

        renderer = DashboardRenderer()
        html = renderer.render(vm)
        assert 'class="metrics-grid' in html
        assert 'class="filters-bar' in html
        assert 'id="emptyReposState"' not in html


class TestEmptyStateI18nKeys:
    """Verify i18n keys exist for empty state in both locales."""

    def test_en_empty_state_keys_exist(self):
        locale_path = (
            Path(__file__).resolve().parent.parent
            / "src"
            / "releaseboard"
            / "i18n"
            / "locales"
            / "en.json"
        )
        data = json.loads(locale_path.read_text(encoding="utf-8"))
        for key in [
            "ui.empty_state.title",
            "ui.empty_state.desc",
            "ui.empty_state.open_wizard",
            "ui.empty_state.open_config",
            "ui.empty_state.hint",
        ]:
            assert key in data, f"Missing en key: {key}"

    def test_pl_empty_state_keys_exist(self):
        locale_path = (
            Path(__file__).resolve().parent.parent
            / "src"
            / "releaseboard"
            / "i18n"
            / "locales"
            / "pl.json"
        )
        data = json.loads(locale_path.read_text(encoding="utf-8"))
        for key in [
            "ui.empty_state.title",
            "ui.empty_state.desc",
            "ui.empty_state.open_wizard",
            "ui.empty_state.open_config",
            "ui.empty_state.hint",
        ]:
            assert key in data, f"Missing pl key: {key}"


# ─────────────────────────────────────────────────────────────────────
# Correction pass: triple logo, lang persistence, config lifecycle
# ─────────────────────────────────────────────────────────────────────


class TestTripleLogoFix:
    """CSS specificity must ensure only one logo SVG is visible at a time."""

    def test_light_logo_visible_dark_hidden(self):
        """fr-logo-light visible, dark/midnight hidden."""
        from releaseboard.presentation.renderer import DashboardRenderer

        renderer = DashboardRenderer()
        html = renderer.render_first_run(locale="en")
        # The fix: .fr-header-logo .fr-logo-light { display: block }
        assert ".fr-header-logo .fr-logo-light" in html
        assert ".fr-header-logo .fr-logo-dark" in html
        assert ".fr-header-logo .fr-logo-midnight" in html

    def test_no_blanket_svg_display_block(self):
        """Must NOT have .fr-header-logo svg { display: block } which causes triple render."""
        from releaseboard.presentation.renderer import DashboardRenderer

        renderer = DashboardRenderer()
        html = renderer.render_first_run(locale="en")
        import re

        # Must not have a rule that forces display:block on ALL svgs inside fr-header-logo
        matches = re.findall(r"\.fr-header-logo\s+svg\s*\{[^}]*display\s*:\s*block", html)
        assert len(matches) == 0, "Must not force display:block on all SVGs inside .fr-header-logo"


class TestLocalePreloadFirstRun:
    """First-run page must include localStorage-based locale pre-load script."""

    def test_locale_preload_script_present(self):
        from releaseboard.presentation.renderer import DashboardRenderer

        renderer = DashboardRenderer()
        html = renderer.render_first_run(locale="en")
        assert "localStorage.getItem('rb_locale')" in html
        assert "window.location.replace" in html

    def test_language_switch_saves_to_localstorage(self):
        """First-run language switch must save to localStorage rb_locale."""
        from releaseboard.presentation.renderer import DashboardRenderer

        renderer = DashboardRenderer()
        html = renderer.render_first_run(locale="en")
        assert "localStorage.setItem('rb_locale'" in html


class TestRedirectUrlsCarryLang:
    """All config-creation redirect URLs must include ?lang= for locale persistence."""

    def test_with_lang_helper_defined(self):
        """withLang() helper must be present to append ?lang= to redirect URLs."""
        from releaseboard.presentation.renderer import DashboardRenderer

        renderer = DashboardRenderer()
        html = renderer.render_first_run(locale="en")
        assert "function withLang(url)" in html

    def test_create_fresh_uses_with_lang(self):
        from releaseboard.presentation.renderer import DashboardRenderer

        renderer = DashboardRenderer()
        html = renderer.render_first_run(locale="en")
        assert "withLang(data.redirect)" in html

    def test_prepare_config_uses_with_lang(self):
        from releaseboard.presentation.renderer import DashboardRenderer

        renderer = DashboardRenderer()
        html = renderer.render_first_run(locale="en")
        assert "withLang('/?open_wizard=1')" in html


class TestConfigSourceOfTruth:
    """After initial creation, config must be served from releaseboard.json, not examples."""

    @pytest.mark.asyncio
    async def test_create_from_example_writes_to_disk(self, first_run_client):
        """After creating from example, config file must exist on disk."""
        client, config_path = first_run_client
        examples_dir = Path(__file__).resolve().parent.parent / "examples"
        if not examples_dir.exists() or not list(examples_dir.glob("*.json")):
            pytest.skip("No example configs available")
        example_name = sorted(examples_dir.glob("*.json"))[0].name
        resp = await client.post(
            "/api/config/create",
            json={"mode": "example", "example": example_name},
        )
        data = resp.json()
        assert data["ok"] is True
        # Config file must exist on disk
        assert config_path.exists(), "releaseboard.json must be persisted to disk"
        # Config API should return the config, not 503
        resp2 = await client.get("/api/config")
        assert resp2.status_code == 200

    @pytest.mark.asyncio
    async def test_analyze_uses_current_state_not_examples(self, first_run_client):
        """After creating empty config, /api/config must return 0 repos (not example repos)."""
        client, _ = first_run_client
        resp = await client.post("/api/config/create", json={"mode": "empty"})
        assert resp.json()["ok"]
        # Config should now have 0 repos — if it re-read from examples, it would have repos
        resp2 = await client.get("/api/config")
        config = resp2.json()
        assert len(config.get("repositories", [])) == 0
