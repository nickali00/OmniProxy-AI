from fastapi.testclient import TestClient

from app.config import settings
from app.main import app


def test_public_release_hides_and_blocks_builder(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "database_path", tmp_path / "gateway.sqlite3")
    monkeypatch.setattr(settings, "build_enabled", False)

    with TestClient(app, base_url="http://127.0.0.1") as client:
        dashboard = client.get("/")
        blocked = client.get("/api/build/projects")

    assert dashboard.status_code == 200
    assert 'data-build-enabled="false"' in dashboard.text
    assert 'data-view-target="build"' not in dashboard.text
    assert 'data-view-panel="build"' not in dashboard.text
    assert 'id="build-project-dialog"' not in dashboard.text
    assert blocked.status_code == 404
    assert blocked.json()["error"] == "not_found"
    assert blocked.headers["cache-control"] == "no-store"


def test_public_dashboard_exposes_four_language_selector(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(settings, "database_path", tmp_path / "gateway.sqlite3")
    monkeypatch.setattr(settings, "build_enabled", False)

    with TestClient(app, base_url="http://127.0.0.1") as client:
        response = client.get("/")
        translations = client.get("/static/dashboard-i18n.js")

    assert response.status_code == 200
    assert 'id="language-select"' in response.text
    for language in ("en", "it", "es", "fr"):
        assert f'<option value="{language}">' in response.text
    assert "dashboard-i18n.js" in response.text
    assert translations.status_code == 200
    assert 'new Set(["en", "it", "es", "fr"])' in translations.text


def test_removed_onboarding_copy_is_not_rendered(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "database_path", tmp_path / "gateway.sqlite3")
    monkeypatch.setattr(settings, "build_enabled", False)

    with TestClient(app, base_url="http://127.0.0.1") as client:
        response = client.get("/")

    assert response.status_code == 200
    assert "VS Code non è richiesto" not in response.text
    assert "Architettura autonoma" not in response.text
    assert "Nessuna dipendenza da VS Code" not in response.text
    assert "Avanzamento" not in response.text
