"""Pipe into the built gateway image; use ephemeral DB and synthetic files only.

docker run --rm -i --network host --read-only \
  --tmpfs /tmp:rw,noexec,nosuid,size=64m omni-proxy-ai-gateway \
  python - < scripts/verify_container.py
"""

import io
import json
import logging
import subprocess
import tempfile
from functools import partial
from pathlib import Path

from fastapi.testclient import TestClient
from PIL import Image

from app.config import settings
from app.database import create_gateway_api
from app.identity import provision
from app.main import app


def verify():
    logging.getLogger("httpx").setLevel(logging.WARNING)
    with tempfile.TemporaryDirectory(prefix="omniproxy-image-smoke-") as temporary:
        settings.database_url = ""
        settings.admin_auth_required = False
        settings.dashboard_auth_required = False
        settings.require_https = False
        settings.otel_enabled = False
        settings.database_path = Path(temporary) / "gateway.sqlite3"
        settings.bootstrap_api_key = "sk-local-container-test-key-with-thirty-two-characters"
        settings.ollama_base_url = "http://127.0.0.1:11434"
        settings.ollama_model = "qwen3.5:2b"
        headers = {"Authorization": f"Bearer {settings.bootstrap_api_key}"}
        fixtures = []
        for name, encoding, mime in [("red.png", "PNG", "image/png"), ("red.pdf", "PDF", "application/pdf")]:
            content = io.BytesIO()
            Image.new("RGB", (96, 96), "red").save(content, format=encoding)
            fixtures.append((name, content.getvalue(), mime))
        video = Path(temporary) / "red.mp4"
        subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=c=red:s=96x96:d=2", "-c:v", "mpeg4", "-y", str(video)], check=True)
        fixtures.append(("red.mp4", video.read_bytes(), "video/mp4"))
        with TestClient(app, base_url="http://127.0.0.1") as client:
            assert client.get("/healthz").status_code == 200
            assert 'id="api-input-unlimited"' in client.get("/").text
            for name, content, mime in fixtures:
                response = client.post("/v1/files", headers=headers, files={"file": (name, content, mime)})
                assert response.status_code == 201, (name, response.text)
                assert client.delete(f"/v1/files/{response.json()['id']}", headers=headers).status_code == 200
            voice = client.post("/v1/audio/speech", headers=headers, json={"input": "Il colore è blu.", "voice": "it", "response_format": "wav"})
            assert voice.status_code == 200, voice.text
            assert voice.content.startswith(b"RIFF") and len(voice.content) > 100
            record, key = client.portal.call(partial(create_gateway_api, name="Container limits", provider="ollama", model=settings.ollama_model, reasoning_effort="off", limits={"input_tokens": 32, "output_tokens": 8, "token_budget": 200, "budget_period": "daily"}))
            limited = {"Authorization": f"Bearer {key}"}
            rejected = client.post("/v1/chat/completions", headers=limited, json={"model": record.slug, "messages": [{"role": "user", "content": "oversized input " * 40}]})
            assert rejected.status_code == 400 and rejected.json()["error"]["code"] == "input_token_limit_exceeded"
            assert client.get("/v1/usage", headers=limited).json()["usage"]["used_tokens"] == 0
            response = client.post("/v1/chat/completions", headers=limited, json={"model": record.slug, "messages": [{"role": "user", "content": "Say OK."}], "max_completion_tokens": 64})
            assert response.status_code == 200, response.text
            assert 0 < response.json()["usage"]["completion_tokens"] <= 8
            usage = client.get("/v1/usage", headers=limited).json()["usage"]
            assert usage["reserved_tokens"] == 0 and usage["used_tokens"] == response.json()["usage"]["total_tokens"]
            note = client.post("/v1/files", headers=headers, files={"file": ("citation.txt", b"BLUE", "text/plain")})
            assert note.status_code == 201, note.text
            file_id = note.json()["id"]
            extraction = client.get(f"/v1/files/{file_id}/extraction", headers=headers).json()
            unit = extraction["units"][0]
            citation = client.post("/v1/citations", headers=headers, json={"file_id": file_id, "unit_id": unit["id"], "mode": "auto", "source_sha256": extraction["source_sha256"], "text_sha256": unit["sha256"], "start": 0, "end": 4, "quote": "BLUE"})
            assert citation.status_code == 201, citation.text
            citation_id = citation.json()["id"]
            assert client.get(f"/v1/citations/{citation_id}", headers=headers).json()["valid"]
            assert client.delete(f"/v1/files/{file_id}", headers=headers).status_code == 200
            assert client.get(f"/v1/citations/{citation_id}", headers=headers).status_code == 404
            dashboard = {"Origin": "http://127.0.0.1", "X-OmniProxy-Request": "dashboard"}
            group = client.post("/api/gateway-apis/groups", headers=dashboard, json={"name": "Studio"})
            assert group.status_code == 201, group.text
            group_id = group.json()["id"]
            assert client.put(f"/api/gateway-apis/{record.id}/group", headers=dashboard, json={"group_id": group_id}).status_code == 200
            assert client.patch(f"/api/gateway-apis/groups/{group_id}", headers=dashboard, json={"collapsed": True}).status_code == 200
            assert client.get("/api/gateway-apis/groups").json()["data"][0]["collapsed"]
            assert client.delete(f"/api/gateway-apis/groups/{group_id}", headers=dashboard).status_code == 200
            assert client.get("/v1/models", headers=limited).status_code == 200
            settings.admin_auth_required = True
            operator = client.portal.call(partial(provision, "Container operator", operator=True))
            admin = {"Authorization": "Bearer " + operator["token"]}
            assert client.get("/api/gateway-apis").status_code == 401
            assert client.get("/admin", headers=admin).status_code == 200
            tenant = client.post("/admin/v1/tenants", headers=admin, json={"name": "Container team"})
            assert tenant.status_code == 201, tenant.text
            project = client.post(f"/admin/v1/tenants/{tenant.json()['id']}/projects", headers=admin, json={"name": "Container project"})
            assert project.status_code == 201, project.text
            project_id = project.json()["id"]
            published = client.post(f"/admin/v1/projects/{project_id}/profiles", headers=admin, json={"name": "Local profile", "provider": "ollama", "model": settings.ollama_model, "reasoning_effort": "off"})
            assert published.status_code == 201, published.text
            minted = client.post(f"/admin/v1/projects/{project_id}/keys", headers=admin, json={"name": "Container application", "profile_id": published.json()["id"], "limits": {"output_tokens": 8}})
            assert minted.status_code == 201, minted.text
            application = {"Authorization": "Bearer " + minted.json()["api_key"]}
            assert client.get("/v1/models", headers=application).json()["data"][0]["id"] == published.json()["slug"]
            answer = client.post("/v1/chat/completions", headers=application, json={"model": published.json()["slug"], "messages": [{"role": "user", "content": "Say OK."}]})
            assert answer.status_code == 200, answer.text
            assert client.delete(f"/admin/v1/projects/{project_id}/keys/{minted.json()['id']}", headers=admin).status_code == 204
            assert client.get("/v1/models", headers=application).status_code == 401
            assert client.get("/admin/v1/diagnostics", headers=admin).status_code == 200
        print(json.dumps({"scope": "built_image_ephemeral_database", "non_root_read_only": True, "image_pdf_video_parsers": True, "speech_generation": True, "input_output_limits": True, "real_ollama": True, "budget_accounted": True, "citations": True, "api_groups_keep_keys_valid": True, "team_identity_profile_key_revocation": True, "passed": True}, indent=2))


if __name__ == "__main__":
    verify()
