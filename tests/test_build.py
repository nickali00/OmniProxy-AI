import hashlib
import json
import sqlite3

import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.main import (
    _apply_unified_diff,
    _collect_maintenance_commands,
    _parse_builder_commands,
    app,
)


@pytest.fixture(autouse=True)
def enable_experimental_build_for_build_tests(monkeypatch):
    """I test dedicati coprono Builder anche se la release pubblica lo disabilita."""
    monkeypatch.setattr(settings, "build_enabled", True)


def _dashboard_headers() -> dict[str, str]:
    return {
        "Origin": "http://127.0.0.1",
        "X-OmniProxy-Request": "dashboard",
    }


def _catalog() -> list[dict[str, object]]:
    return [
        {
            "id": "ollama",
            "name": "Ollama",
            "connected": True,
            "models": [
                {
                    "id": "qwen-build:latest",
                    "display_name": "Qwen Build",
                    "description": "Fixture locale",
                    "is_default": True,
                    "reasoning_efforts": ["off", "on"],
                    "default_reasoning_effort": "off",
                }
            ],
        }
    ]


def _project_payload() -> dict[str, object]:
    model = {
        "provider": "ollama",
        "model": "qwen-build:latest",
        "reasoning_effort": "on",
    }
    return {
        "name": "Workspace test",
        "folder_name": "progetto-test",
        "idea": "Costruire una dashboard privata e sicura.",
        "analyst": model,
        "builder": model,
        "files": [
            {"path": "app/main.py", "content": "print('safe')"},
            {"path": ".env", "content": "SECRET=must-not-be-stored"},
            {
                "path": "node_modules/package/index.js",
                "content": "ignored dependency",
            },
        ],
    }


def test_unified_diff_recovers_unique_changed_core_from_stale_context():
    original = (
        "<main>\n"
        "  <div>Contesto reale</div>\n"
        "  <section class=\"artifacts\">\n"
        "    <h1>Piano</h1>\n"
        "  </section>\n"
        "</main>"
    )
    diff = (
        "--- a/page.html\n"
        "+++ b/page.html\n"
        "@@ -40,5 +40,5 @@\n"
        "   <div>Contesto precedente non più presente</div>\n"
        "-  <section class=\"artifacts\">\n"
        "+  <section class=\"artifacts viewer\">\n"
        "     <h1>Piano</h1>\n"
        "   </section>"
    )

    updated = _apply_unified_diff(original, diff, path="page.html")

    assert '<section class="artifacts viewer">' in updated
    assert '<section class="artifacts">' not in updated


def test_unified_diff_rejects_ambiguous_changed_core():
    original = "before\nsame\nmiddle\nsame\nafter"
    diff = (
        "--- a/file.txt\n"
        "+++ b/file.txt\n"
        "@@ -20,3 +20,3 @@\n"
        " missing context\n"
        "-same\n"
        "+changed\n"
        " other missing context"
    )

    with pytest.raises(ValueError, match="non corrisponde"):
        _apply_unified_diff(original, diff, path="file.txt")


def test_builder_maintenance_commands_are_strictly_allowlisted():
    allowed, error = _parse_builder_commands(
        "<omniproxy-commands>"
        '{"commands":["docker compose up -d --build --force-recreate"]}'
        "</omniproxy-commands>"
    )
    rejected, rejected_error = _parse_builder_commands(
        "<omniproxy-commands>"
        '{"commands":["docker compose down -v"]}'
        "</omniproxy-commands>"
    )

    assert error is None
    assert allowed == ["docker compose up -d --build --force-recreate"]
    assert rejected == []
    assert rejected_error == (
        "Comando Builder non consentito: docker compose down -v"
    )


def test_maintenance_commands_are_collected_from_every_phase():
    commands, error = _collect_maintenance_commands(
        [
            (
                "<omniproxy-commands>"
                '{"commands":["docker compose up -d --build '
                '--force-recreate"]}'
                "</omniproxy-commands>"
            ),
            "Fase finale senza comandi.",
        ]
    )

    assert error is None
    assert commands == [
        "docker compose up -d --build --force-recreate"
    ]


def test_dashboard_can_queue_fixed_docker_rebuild(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(settings, "database_path", tmp_path / "gateway.sqlite3")

    async def fake_catalog(_app):
        return _catalog()

    submitted: list[list[str]] = []

    async def fake_submit(_request, project, commands):
        assert project["folder_name"] == settings.maintenance_workspace_name
        submitted.append(commands)
        return {
            "id": "3deaa50d-4fab-4606-947a-567caf444234",
            "status": "queued",
            "commands": commands,
        }

    monkeypatch.setattr("app.main._model_catalog", fake_catalog)
    monkeypatch.setattr("app.main._submit_maintenance_job", fake_submit)
    payload = _project_payload()
    payload["folder_name"] = settings.maintenance_workspace_name

    with TestClient(app, base_url="http://127.0.0.1") as client:
        created = client.post(
            "/api/build/projects",
            headers=_dashboard_headers(),
            json=payload,
        )
        response = client.post(
            f"/api/build/projects/{created.json()['id']}/maintenance/rebuild",
            headers=_dashboard_headers(),
        )

    assert response.status_code == 202
    assert response.json()["status"] == "queued"
    assert submitted == [
        ["docker compose up -d --build --force-recreate"]
    ]


def test_build_project_snapshot_is_scoped_and_csrf_protected(
    tmp_path,
    monkeypatch,
):
    database_path = tmp_path / "gateway.sqlite3"
    monkeypatch.setattr(settings, "database_path", database_path)

    async def fake_catalog(_app):
        return _catalog()

    monkeypatch.setattr("app.main._model_catalog", fake_catalog)

    with TestClient(app, base_url="http://127.0.0.1") as client:
        rejected = client.post("/api/build/projects", json=_project_payload())
        assert rejected.status_code == 403

        created = client.post(
            "/api/build/projects",
            headers=_dashboard_headers(),
            json=_project_payload(),
        )
        assert created.status_code == 201
        project_id = created.json()["id"]
        assert created.json()["file_count"] == 1
        assert "must-not-be-stored" not in created.text

        detail = client.get(f"/api/build/projects/{project_id}")
        assert detail.status_code == 200
        assert [item["path"] for item in detail.json()["files"]] == [
            "app/main.py"
        ]
        assert "print('safe')" not in detail.text

    connection = sqlite3.connect(database_path)
    stored = connection.execute(
        "SELECT path, content FROM build_project_files"
    ).fetchall()
    connection.close()
    assert stored == [("app/main.py", "print('safe')")]


def test_build_pipeline_and_role_chat_are_persisted(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "database_path", tmp_path / "gateway.sqlite3")

    async def fake_catalog(_app):
        return _catalog()

    async def fake_pipeline(*_args, **_kwargs):
        return [
            ("analysis", "Analisi persistita"),
            ("builder_brief", "Brief persistito"),
            ("roadmap", "Fase 1\nFase 2"),
            ("future_features", "Feature futura"),
        ]

    async def fake_chat(*_args, **kwargs):
        assert kwargs["lane"] == "builder"
        return "Risposta Builder persistita"

    monkeypatch.setattr("app.main._model_catalog", fake_catalog)
    monkeypatch.setattr(
        "app.main._run_build_planning_pipeline",
        fake_pipeline,
    )
    monkeypatch.setattr("app.main._run_build_chat", fake_chat)

    with TestClient(app, base_url="http://127.0.0.1") as client:
        created = client.post(
            "/api/build/projects",
            headers=_dashboard_headers(),
            json=_project_payload(),
        )
        project_id = created.json()["id"]

        planned = client.post(
            f"/api/build/projects/{project_id}/plan",
            headers=_dashboard_headers(),
            json={"idea": "Una nuova idea sufficientemente dettagliata."},
        )
        assert planned.status_code == 200
        assert [item["artifact_type"] for item in planned.json()["artifacts"]] == [
            "analysis",
            "builder_brief",
            "roadmap",
            "future_features",
        ]

        chatted = client.post(
            f"/api/build/projects/{project_id}/chat",
            headers=_dashboard_headers(),
            json={"lane": "builder", "message": "Avvia la fase 1"},
        )
        assert chatted.status_code == 200
        assert chatted.json()["content"] == "Risposta Builder persistita"

        detail = client.get(f"/api/build/projects/{project_id}").json()
        assert [message["role"] for message in detail["messages"]] == [
            "user",
            "assistant",
        ]
        assert detail["idea"] == "Una nuova idea sufficientemente dettagliata."

        deleted = client.delete(
            f"/api/build/projects/{project_id}",
            headers=_dashboard_headers(),
        )
        assert deleted.status_code == 200
        assert client.get(
            f"/api/build/projects/{project_id}"
        ).status_code == 404


def test_build_reports_the_disconnected_role_without_api_wording(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(settings, "database_path", tmp_path / "gateway.sqlite3")
    connection_state = {"connected": True}

    async def fake_catalog(_app):
        return [
            {
                "id": "gemini",
                "name": "Gemini",
                "connected": connection_state["connected"],
                "models": [
                    {
                        "id": "gemini-test",
                        "display_name": "Gemini Test",
                        "description": "Fixture",
                        "is_default": True,
                        "reasoning_efforts": ["auto"],
                        "default_reasoning_effort": "auto",
                    }
                ],
            }
        ]

    monkeypatch.setattr("app.main._model_catalog", fake_catalog)
    model = {
        "provider": "gemini",
        "model": "gemini-test",
        "reasoning_effort": "auto",
    }
    payload = {
        "name": "Gemini Build",
        "folder_name": "",
        "idea": "Idea iniziale sufficientemente dettagliata.",
        "analyst": model,
        "builder": model,
        "files": [],
    }

    with TestClient(app, base_url="http://127.0.0.1") as client:
        created = client.post(
            "/api/build/projects",
            headers=_dashboard_headers(),
            json=payload,
        )
        assert created.status_code == 201

        connection_state["connected"] = False
        rejected = client.patch(
            f"/api/build/projects/{created.json()['id']}",
            headers=_dashboard_headers(),
            json=payload,
        )

    assert rejected.status_code == 409
    assert rejected.json()["error"] == "build_provider_not_connected"
    assert "Gemini" in rejected.json()["message"]
    assert "Analista idea" in rejected.json()["message"]
    assert "creare questa API" not in rejected.json()["message"]


def test_analyst_request_is_really_handed_to_builder(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(settings, "database_path", tmp_path / "gateway.sqlite3")

    async def fake_catalog(_app):
        return _catalog()

    calls: list[str] = []
    activity_roles: list[str | None] = []

    async def fake_chat(*_args, **kwargs):
        lane = kwargs["lane"]
        calls.append(lane)
        project_id = int(kwargs["project"]["id"])
        activity_roles.append(
            app.state.build_activities.get(project_id, {}).get("role")
        )
        if lane == "analyst":
            return "Brief operativo prodotto dall’Analista."
        assert any(
            message.get("message_type") == "handoff"
            for message in kwargs["history"]
        )
        return "Consegna ricevuta. Avvio la prima fase."

    monkeypatch.setattr("app.main._model_catalog", fake_catalog)
    monkeypatch.setattr("app.main._run_build_chat", fake_chat)

    with TestClient(app, base_url="http://127.0.0.1") as client:
        created = client.post(
            "/api/build/projects",
            headers=_dashboard_headers(),
            json=_project_payload(),
        )
        project_id = created.json()["id"]

        response = client.post(
            f"/api/build/projects/{project_id}/chat",
            headers=_dashboard_headers(),
            json={
                "lane": "analyst",
                "message": "Passa questa modifica al Builder e avvialo.",
            },
        )
        detail = client.get(f"/api/build/projects/{project_id}").json()
        inactive = client.get(
            f"/api/build/projects/{project_id}/activity"
        ).json()

    assert response.status_code == 200
    assert response.json()["handoff"]["status"] == "completed"
    assert calls == ["analyst", "builder"]
    assert activity_roles == ["analyst", "builder"]
    assert inactive == {"active": False}
    assert [
        (message["lane"], message["role"], message["message_type"])
        for message in detail["messages"]
    ] == [
        ("analyst", "user", "chat"),
        ("analyst", "assistant", "chat"),
        ("builder", "user", "handoff"),
        ("builder", "assistant", "chat"),
    ]
    handoff = detail["messages"][2]
    assert handoff["source_message_id"] == detail["messages"][1]["id"]
    assert "Brief operativo prodotto dall’Analista." in handoff["content"]


def test_builder_returns_a_validated_writable_change_proposal(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(settings, "database_path", tmp_path / "gateway.sqlite3")

    async def fake_catalog(_app):
        return _catalog()

    patch_payload = {
        "patches": [
            {
                "path": "app/main.py",
                "diff": (
                    "--- a/app/main.py\n"
                    "+++ b/app/main.py\n"
                    "@@ -1,1 +1,1 @@\n"
                    "-print('safe')\n"
                    "+print('changed')"
                ),
            }
        ]
    }

    async def fake_chat(*_args, **kwargs):
        assert kwargs["lane"] == "builder"
        return (
            "Modifica pronta per la verifica.\n"
            "<omniproxy-changes>"
            f"{json.dumps(patch_payload)}"
            "</omniproxy-changes>"
        )

    monkeypatch.setattr("app.main._model_catalog", fake_catalog)
    monkeypatch.setattr("app.main._run_build_chat", fake_chat)

    with TestClient(app, base_url="http://127.0.0.1") as client:
        created = client.post(
            "/api/build/projects",
            headers=_dashboard_headers(),
            json=_project_payload(),
        )
        project_id = created.json()["id"]
        response = client.post(
            f"/api/build/projects/{project_id}/chat",
            headers=_dashboard_headers(),
            json={
                "lane": "builder",
                "message": "Modifica app/main.py e procedi.",
            },
        )
        persisted = client.get(
            f"/api/build/projects/{project_id}/builder-proposal"
        )
        marked = client.post(
            f"/api/build/projects/{project_id}/builder-proposals/"
            f"{response.json()['id']}/applied",
            headers=_dashboard_headers(),
        )
        cleared = client.get(
            f"/api/build/projects/{project_id}/builder-proposal"
        )

    assert response.status_code == 200
    proposal = response.json()["changes"]
    assert response.json()["change_error"] is None
    assert proposal == [
        {
            "path": "app/main.py",
            "content": "print('changed')",
            "base_sha256": hashlib.sha256(
                b"print('safe')"
            ).hexdigest(),
            "result_sha256": hashlib.sha256(
                b"print('changed')"
            ).hexdigest(),
            "operation": "update",
        }
    ]
    assert persisted.status_code == 200
    assert persisted.json()["message_id"] == response.json()["id"]
    assert persisted.json()["changes"] == proposal
    assert marked.status_code == 200
    assert marked.json()["status"] == "applied"
    assert cleared.json() == {
        "message_id": None,
        "changes": [],
        "change_error": None,
    }


def test_builder_short_followup_inherits_referenced_files_from_history(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(settings, "database_path", tmp_path / "gateway.sqlite3")

    async def fake_catalog(_app):
        return _catalog()

    contexts: list[str] = []

    async def fake_chat(*_args, **kwargs):
        contexts.append(kwargs["project_context"])
        if len(contexts) == 1:
            return (
                "Servono `app/templates/dashboard.html`, "
                "`app/static/dashboard.css` e `app/static/dashboard.js`."
            )
        return "Snapshot completo ricevuto."

    monkeypatch.setattr("app.main._model_catalog", fake_catalog)
    monkeypatch.setattr("app.main._run_build_chat", fake_chat)
    payload = _project_payload()
    payload["files"] = [
        {"path": "README.md", "content": "R" * 79_000},
        {
            "path": "app/templates/dashboard.html",
            "content": "<main>HTML_TARGET</main>",
        },
        {
            "path": "app/static/dashboard.css",
            "content": ".layout { content: 'CSS_TARGET'; }",
        },
        {
            "path": "app/static/dashboard.js",
            "content": "const JS_TARGET = true;",
        },
    ]

    with TestClient(app, base_url="http://127.0.0.1") as client:
        created = client.post(
            "/api/build/projects",
            headers=_dashboard_headers(),
            json=payload,
        )
        project_id = created.json()["id"]
        client.post(
            f"/api/build/projects/{project_id}/chat",
            headers=_dashboard_headers(),
            json={"lane": "builder", "message": "Valuta l’interfaccia."},
        )
        response = client.post(
            f"/api/build/projects/{project_id}/chat",
            headers=_dashboard_headers(),
            json={"lane": "builder", "message": "procedi"},
        )

    assert response.status_code == 200
    assert len(contexts) == 2
    assert "HTML_TARGET" in contexts[1]
    assert "CSS_TARGET" in contexts[1]
    assert "JS_TARGET" in contexts[1]
    assert "FILE TRONCATO" not in contexts[1]
    assert "R" * 100 not in contexts[1]


def test_current_builder_paths_override_unrelated_historical_paths(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(settings, "database_path", tmp_path / "gateway.sqlite3")

    async def fake_catalog(_app):
        return _catalog()

    contexts: list[str] = []

    async def fake_chat(*_args, **kwargs):
        contexts.append(kwargs["project_context"])
        if len(contexts) == 1:
            return (
                "Il vecchio brief cita `app/main.py`, "
                "`app/templates/dashboard.html`, `app/static/dashboard.css` "
                "e `app/static/dashboard.js`."
            )
        return "Contesto mirato ricevuto."

    monkeypatch.setattr("app.main._model_catalog", fake_catalog)
    monkeypatch.setattr("app.main._run_build_chat", fake_chat)
    payload = _project_payload()
    payload["files"] = [
        {"path": "app/main.py", "content": "MAIN_MUST_NOT_BE_INCLUDED"},
        {
            "path": "app/templates/dashboard.html",
            "content": "<main>HTML_ONLY</main>",
        },
        {
            "path": "app/static/dashboard.css",
            "content": ".layout { content: 'CSS_ONLY'; }",
        },
        {
            "path": "app/static/dashboard.js",
            "content": "const JS_ONLY = true;",
        },
    ]

    with TestClient(app, base_url="http://127.0.0.1") as client:
        created = client.post(
            "/api/build/projects",
            headers=_dashboard_headers(),
            json=payload,
        )
        project_id = created.json()["id"]
        client.post(
            f"/api/build/projects/{project_id}/chat",
            headers=_dashboard_headers(),
            json={"lane": "builder", "message": "Leggi il vecchio brief."},
        )
        response = client.post(
            f"/api/build/projects/{project_id}/chat",
            headers=_dashboard_headers(),
            json={
                "lane": "builder",
                "message": (
                    "Procedi su app/templates/dashboard.html, "
                    "app/static/dashboard.css e app/static/dashboard.js."
                ),
            },
        )

    assert response.status_code == 200
    assert "HTML_ONLY" in contexts[1]
    assert "CSS_ONLY" in contexts[1]
    assert "JS_ONLY" in contexts[1]
    assert "MAIN_MUST_NOT_BE_INCLUDED" not in contexts[1]


def test_explicit_handoff_is_idempotent_for_same_analyst_answer(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(settings, "database_path", tmp_path / "gateway.sqlite3")

    async def fake_catalog(_app):
        return _catalog()

    builder_calls = 0

    async def fake_chat(*_args, **kwargs):
        nonlocal builder_calls
        if kwargs["lane"] == "analyst":
            return "Specifica pronta per la consegna."
        builder_calls += 1
        return "Builder pronto."

    monkeypatch.setattr("app.main._model_catalog", fake_catalog)
    monkeypatch.setattr("app.main._run_build_chat", fake_chat)

    with TestClient(app, base_url="http://127.0.0.1") as client:
        created = client.post(
            "/api/build/projects",
            headers=_dashboard_headers(),
            json=_project_payload(),
        )
        project_id = created.json()["id"]
        client.post(
            f"/api/build/projects/{project_id}/chat",
            headers=_dashboard_headers(),
            json={"lane": "analyst", "message": "Prepara il brief."},
        )

        first = client.post(
            f"/api/build/projects/{project_id}/handoff",
            headers=_dashboard_headers(),
            json={"instruction": "Applica questa specifica."},
        )
        repeated = client.post(
            f"/api/build/projects/{project_id}/handoff",
            headers=_dashboard_headers(),
            json={"instruction": "Applica questa specifica."},
        )

    assert first.status_code == 201
    assert first.json()["status"] == "completed"
    assert repeated.status_code == 200
    assert repeated.json()["status"] == "already_completed"
    assert builder_calls == 1


def test_schematic_plan_advances_builder_one_phase_after_apply(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(settings, "database_path", tmp_path / "gateway.sqlite3")

    async def fake_catalog(_app):
        return _catalog()

    builder_calls = 0

    async def fake_chat(*_args, **kwargs):
        nonlocal builder_calls
        if kwargs["lane"] == "analyst":
            return (
                "Cambio riassunto in due passaggi."
                "<omniproxy-plan>"
                '{"phases":['
                '{"title":"Prima fase","instruction":"Modifica app/main.py."},'
                '{"title":"Seconda fase","instruction":"Crea app/next.py."}'
                "]}</omniproxy-plan>"
            )
        builder_calls += 1
        history_text = "\n".join(
            str(message["content"]) for message in kwargs["history"]
        )
        assert "Cambio riassunto in due passaggi." not in history_text
        if builder_calls == 1:
            payload = {
                "patches": [
                    {
                        "path": "app/main.py",
                        "diff": (
                            "--- a/app/main.py\n"
                            "+++ b/app/main.py\n"
                            "@@ -1,1 +1,1 @@\n"
                            "-print('safe')\n"
                            "+print('phase-1')"
                        ),
                    }
                ]
            }
        else:
            payload = {
                "patches": [
                    {
                        "path": "app/next.py",
                        "diff": (
                            "--- /dev/null\n"
                            "+++ b/app/next.py\n"
                            "@@ -0,0 +1,1 @@\n"
                            "+print('phase-2')"
                        ),
                    }
                ]
            }
        return (
            f"Fase {builder_calls} pronta."
            "<omniproxy-changes>"
            f"{json.dumps(payload)}"
            "</omniproxy-changes>"
        )

    monkeypatch.setattr("app.main._model_catalog", fake_catalog)
    monkeypatch.setattr("app.main._run_build_chat", fake_chat)
    payload = _project_payload()
    payload["analyst_mode"] = "schematic"

    with TestClient(app, base_url="http://127.0.0.1") as client:
        created = client.post(
            "/api/build/projects",
            headers=_dashboard_headers(),
            json=payload,
        )
        project_id = created.json()["id"]
        first = client.post(
            f"/api/build/projects/{project_id}/chat",
            headers=_dashboard_headers(),
            json={
                "lane": "analyst",
                "message": "Riassumi e passa al Builder.",
            },
        )
        first_message_id = first.json()["handoff"]["builder_message"]["id"]
        waiting = client.get(
            f"/api/build/projects/{project_id}"
        ).json()["phases"]
        advanced = client.post(
            f"/api/build/projects/{project_id}/builder-proposals/"
            f"{first_message_id}/applied",
            headers=_dashboard_headers(),
        )
        phases = client.get(
            f"/api/build/projects/{project_id}"
        ).json()["phases"]

    assert first.status_code == 200
    assert first.json()["content"] == "Cambio riassunto in due passaggi."
    assert [phase["status"] for phase in waiting] == [
        "awaiting_apply",
        "pending",
    ]
    assert advanced.status_code == 200
    assert advanced.json()["next_phase"]["status"] == "completed"
    assert advanced.json()["next_phase"]["changes"][0]["path"] == "app/next.py"
    assert [phase["status"] for phase in phases] == [
        "completed",
        "awaiting_apply",
    ]
    assert builder_calls == 2


def test_builder_recovers_missing_indexed_files_without_user_sync(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(settings, "database_path", tmp_path / "gateway.sqlite3")

    async def fake_catalog(_app):
        return _catalog()

    contexts: list[str] = []
    patch_payload = {
        "patches": [
            {
                "path": "app/main.py",
                "diff": (
                    "--- a/app/main.py\n"
                    "+++ b/app/main.py\n"
                    "@@ -1,1 +1,1 @@\n"
                    "-print('safe')\n"
                    "+print('recovered')"
                ),
            }
        ]
    }

    async def fake_chat(*_args, **kwargs):
        assert kwargs["lane"] == "builder"
        contexts.append(kwargs["project_context"])
        if len(contexts) == 1:
            return (
                "`app/database.py` è FILE TRONCATO e mancano i contenuti di "
                "`app/main.py` e `tests/test_build.py`."
            )
        assert "DATABASE_COMPLETE" in contexts[-1]
        assert "print('safe')" in contexts[-1]
        assert "TEST_COMPLETE" in contexts[-1]
        return (
            "Contesto recuperato; patch pronta."
            "<omniproxy-changes>"
            f"{json.dumps(patch_payload)}"
            "</omniproxy-changes>"
        )

    monkeypatch.setattr("app.main._model_catalog", fake_catalog)
    monkeypatch.setattr("app.main._run_build_chat", fake_chat)
    payload = _project_payload()
    payload["files"] = [
        {"path": "app/database.py", "content": "DATABASE_COMPLETE"},
        {"path": "app/main.py", "content": "print('safe')"},
        {"path": "tests/test_build.py", "content": "TEST_COMPLETE"},
    ]

    with TestClient(app, base_url="http://127.0.0.1") as client:
        created = client.post(
            "/api/build/projects",
            headers=_dashboard_headers(),
            json=payload,
        )
        project_id = created.json()["id"]
        response = client.post(
            f"/api/build/projects/{project_id}/chat",
            headers=_dashboard_headers(),
            json={"lane": "builder", "message": "procedi"},
        )
        messages = client.get(
            f"/api/build/projects/{project_id}"
        ).json()["messages"]

    assert response.status_code == 200
    assert response.json()["changes"][0]["content"] == "print('recovered')"
    assert len(contexts) == 2
    assert len(messages) == 2
    assert "FILE TRONCATO" not in messages[-1]["content"]


def test_blocked_phase_is_retried_before_later_phases(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(settings, "database_path", tmp_path / "gateway.sqlite3")

    async def fake_catalog(_app):
        return _catalog()

    builder_calls = 0

    async def fake_chat(*_args, **kwargs):
        nonlocal builder_calls
        if kwargs["lane"] == "analyst":
            return (
                "Piano pronto."
                "<omniproxy-plan>"
                '{"phases":['
                '{"title":"Fase bloccabile","instruction":"Modifica app/main.py."},'
                '{"title":"Fase successiva","instruction":"Crea app/next.py."}'
                "]}</omniproxy-plan>"
            )
        builder_calls += 1
        if builder_calls == 1:
            return "Non è stata prodotta alcuna patch."
        patch_payload = {
            "patches": [
                {
                    "path": "app/main.py",
                    "diff": (
                        "--- a/app/main.py\n"
                        "+++ b/app/main.py\n"
                        "@@ -1,1 +1,1 @@\n"
                        "-print('safe')\n"
                        "+print('retry-ok')"
                    ),
                }
            ]
        }
        return (
            "Retry completato."
            "<omniproxy-changes>"
            f"{json.dumps(patch_payload)}"
            "</omniproxy-changes>"
        )

    monkeypatch.setattr("app.main._model_catalog", fake_catalog)
    monkeypatch.setattr("app.main._run_build_chat", fake_chat)

    with TestClient(app, base_url="http://127.0.0.1") as client:
        created = client.post(
            "/api/build/projects",
            headers=_dashboard_headers(),
            json=_project_payload(),
        )
        project_id = created.json()["id"]
        first = client.post(
            f"/api/build/projects/{project_id}/chat",
            headers=_dashboard_headers(),
            json={
                "lane": "analyst",
                "message": "Prepara e passa al Builder.",
            },
        )
        retried = client.post(
            f"/api/build/projects/{project_id}/handoff",
            headers=_dashboard_headers(),
            json={"instruction": ""},
        )
        phases = client.get(
            f"/api/build/projects/{project_id}"
        ).json()["phases"]

    assert first.json()["handoff"]["status"] == "blocked"
    assert retried.status_code == 201
    assert retried.json()["status"] == "completed"
    assert [phase["status"] for phase in phases] == [
        "awaiting_apply",
        "pending",
    ]
    assert builder_calls == 2


def test_resume_from_analyst_preserves_completed_phases(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(settings, "database_path", tmp_path / "gateway.sqlite3")

    async def fake_catalog(_app):
        return _catalog()

    analyst_calls = 0
    builder_calls = 0

    async def fake_chat(*_args, **kwargs):
        nonlocal analyst_calls, builder_calls
        if kwargs["lane"] == "analyst":
            analyst_calls += 1
            if analyst_calls == 1:
                return (
                    "Piano iniziale."
                    "<omniproxy-plan>"
                    '{"phases":['
                    '{"title":"Fase uno","instruction":"Modifica app/main.py."},'
                    '{"title":"Fase due","instruction":"Crea app/second.py."},'
                    '{"title":"Fase tre","instruction":"Crea app/third.py."}'
                    "]}</omniproxy-plan>"
                )
            return (
                "Correzione mirata per la fase due."
                "<omniproxy-plan>"
                '{"phases":[{"title":"Piano da ignorare",'
                '"instruction":"Non sostituire la checklist."}]}'
                "</omniproxy-plan>"
            )

        builder_calls += 1
        if builder_calls == 2:
            return "La fase due si è bloccata senza produrre patch."
        if builder_calls == 3:
            history_text = "\n".join(
                str(message["content"]) for message in kwargs["history"]
            )
            assert "Correzione mirata per la fase due." in history_text
            path = "app/second.py"
            value = "phase-2-retried"
        else:
            path = "app/main.py"
            value = "phase-1"
        payload = {
            "patches": [
                {
                    "path": path,
                    "diff": (
                        "--- /dev/null\n"
                        f"+++ b/{path}\n"
                        "@@ -0,0 +1,1 @@\n"
                        f"+print('{value}')"
                    )
                    if path != "app/main.py"
                    else (
                        "--- a/app/main.py\n"
                        "+++ b/app/main.py\n"
                        "@@ -1,1 +1,1 @@\n"
                        "-print('safe')\n"
                        "+print('phase-1')"
                    ),
                }
            ]
        }
        return (
            f"Patch Builder {builder_calls}."
            "<omniproxy-changes>"
            f"{json.dumps(payload)}"
            "</omniproxy-changes>"
        )

    monkeypatch.setattr("app.main._model_catalog", fake_catalog)
    monkeypatch.setattr("app.main._run_build_chat", fake_chat)

    with TestClient(app, base_url="http://127.0.0.1") as client:
        created = client.post(
            "/api/build/projects",
            headers=_dashboard_headers(),
            json=_project_payload(),
        )
        project_id = created.json()["id"]
        first = client.post(
            f"/api/build/projects/{project_id}/chat",
            headers=_dashboard_headers(),
            json={
                "lane": "analyst",
                "message": "Crea il piano e passa al Builder.",
            },
        )
        first_message_id = first.json()["handoff"]["builder_message"]["id"]
        advanced = client.post(
            f"/api/build/projects/{project_id}/builder-proposals/"
            f"{first_message_id}/applied",
            headers=_dashboard_headers(),
        )
        before_resume = client.get(
            f"/api/build/projects/{project_id}"
        ).json()["phases"]
        resumed = client.post(
            f"/api/build/projects/{project_id}/resume",
            headers=_dashboard_headers(),
        )
        after_resume = client.get(
            f"/api/build/projects/{project_id}"
        ).json()["phases"]

    assert advanced.json()["next_phase"]["status"] == "blocked"
    assert [phase["status"] for phase in before_resume] == [
        "completed",
        "blocked",
        "pending",
    ]
    assert resumed.status_code == 200
    assert resumed.json()["handoff"]["status"] == "completed"
    assert [phase["id"] for phase in after_resume] == [
        phase["id"] for phase in before_resume
    ]
    assert [phase["status"] for phase in after_resume] == [
        "completed",
        "awaiting_apply",
        "pending",
    ]
    assert resumed.json()["analyst_message"]["content"] == (
        "Correzione mirata per la fase due."
    )
    assert analyst_calls == 2
    assert builder_calls == 3
