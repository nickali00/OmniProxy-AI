import io
import subprocess
import zipfile

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from app.config import settings
from app.database import _connect, authenticate_api_key, create_api_key
from app.main import app
from app.providers.ollama import OllamaProvider

KEY = "sk-local-files-test-key-with-thirty-two-characters"
HEADERS = {"Authorization": f"Bearer {KEY}"}


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "database_path", tmp_path / "files.sqlite3")
    monkeypatch.setattr(settings, "bootstrap_api_key", KEY)
    with TestClient(app, base_url="http://127.0.0.1") as instance:
        yield instance


def upload(client, name, data, mime="application/octet-stream"):
    return client.post("/v1/files", headers=HEADERS, files={"file": (name, data, mime)}, data={"purpose": "user_data"})


def office_file(entries):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, data in entries.items():
            archive.writestr(name, data)
    return output.getvalue()


def pdf_file(text="PDF_MARKER_872"):
    stream = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode()
    objects = [b"<< /Type /Catalog /Pages 2 0 R >>", b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>", b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>", b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>", b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream"]
    output = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for index, content in enumerate(objects, 1):
        offsets.append(len(output))
        output.extend(f"{index} 0 obj\n".encode() + content + b"\nendobj\n")
    cross_reference = len(output)
    output.extend(f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode())
    for offset in offsets[1:]:
        output.extend(f"{offset:010d} 00000 n \n".encode())
    output.extend(f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{cross_reference}\n%%EOF\n".encode())
    return bytes(output)


def test_file_upload_ownership_download_and_delete(client):
    response = upload(client, "notes.txt", b"PRIVATE_FILE_MARKER_82")
    assert response.status_code == 201, response.text
    record = response.json()
    assert record["media_kind"] == "text" and record["expires_at"] > record["created_at"]
    file_id = record["id"]
    _, other_key = client.portal.call(create_api_key, "other")
    other = {"Authorization": f"Bearer {other_key}"}
    assert client.get(f"/v1/files/{file_id}", headers=other).status_code == 404
    assert client.get(f"/v1/files/{file_id}/content", headers=other).status_code == 404
    assert client.delete(f"/v1/files/{file_id}", headers=other).status_code == 404
    assert client.get("/v1/files", headers=other).json()["data"] == []
    downloaded = client.get(f"/v1/files/{file_id}/content", headers=HEADERS)
    assert downloaded.content == b"PRIVATE_FILE_MARKER_82"
    assert downloaded.headers["Content-Disposition"].startswith("attachment;")
    assert client.delete(f"/v1/files/{file_id}", headers=HEADERS).json()["deleted"] is True
    assert client.get(f"/v1/files/{file_id}", headers=HEADERS).status_code == 404


@pytest.mark.parametrize("name,data,marker", [
    ("notes.txt", b"TEXT_MARKER_93", "TEXT_MARKER_93"),
    ("document.pdf", pdf_file(), "PDF_MARKER_872"),
    ("document.docx", office_file({"word/document.xml": '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>WORD_MARKER_83</w:t></w:r></w:p></w:body></w:document>'}), "WORD_MARKER_83"),
    ("sheet.xlsx", office_file({"xl/sharedStrings.xml": '<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><si><t>EXCEL_MARKER_43</t></si></sst>', "xl/worksheets/sheet1.xml": '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData><row r="1"><c r="A1" t="s"><v>0</v></c><c r="B1"><f>1+2</f><v>3</v></c></row></sheetData></worksheet>'}), "EXCEL_MARKER_43"),
    ("slides.pptx", office_file({"ppt/slides/slide1.xml": '<p:sld xmlns:p="urn:p" xmlns:a="urn:a"><a:p><a:t>SLIDE_MARKER_65</a:t></a:p></p:sld>'}), "SLIDE_MARKER_65"),
])
def test_real_extractors_expand_files_before_model(client, monkeypatch, name, data, marker):
    async def require(_app):
        return object()

    async def complete(self, request, model):
        text = "\n".join(message.text_content() for message in request.messages)
        assert marker in text
        if name.endswith(".pdf"):
            assert "[page 1]" in text
        if name.endswith(".xlsx"):
            assert "not recalculated" in text
        return "attachment understood"

    monkeypatch.setattr("app.main._require_ollama_client", require)
    monkeypatch.setattr(OllamaProvider, "complete", complete)
    record = upload(client, name, data)
    assert record.status_code == 201, record.text
    response = client.post("/v1/chat/completions", headers=HEADERS, json={"model": "local", "messages": [{"role": "user", "content": [{"type": "text", "text": "Analyze the attachment"}, {"type": "file", "file": {"file_id": record.json()["id"]}}]}]})
    assert response.status_code == 200
    assert response.json()["choices"][0]["message"]["content"] == "attachment understood"


def test_image_file_id_in_responses_uses_vision_content(client, monkeypatch):
    output = io.BytesIO()
    Image.new("RGB", (32, 32), "red").save(output, format="PNG")
    record = upload(client, "red.png", output.getvalue(), "image/png")
    assert record.status_code == 201

    async def require(_app):
        return object()

    async def complete(self, request, model):
        images = request.messages[-1].image_payloads()
        assert len(images) == 1 and images[0].mime_type == "image/jpeg"
        return "RED"

    monkeypatch.setattr("app.main._require_ollama_client", require)
    monkeypatch.setattr(OllamaProvider, "complete", complete)
    response = client.post("/v1/responses", headers=HEADERS, json={"model": "local", "input": [{"role": "user", "content": [{"type": "input_image", "file_id": record.json()["id"]}]}]})
    assert response.status_code == 200 and response.json()["output_text"] == "RED"


def test_malformed_documents_zip_limits_and_xml_entities_are_rejected(client):
    assert upload(client, "bad.pdf", b"not a pdf").status_code == 400
    assert upload(client, "bad.exe", b"text").status_code == 415
    assert upload(client, "bomb.docx", office_file({"word/document.xml": "x" * 11_000_000})).status_code == 400
    malicious = office_file({"word/document.xml": '<!DOCTYPE x [<!ENTITY leak SYSTEM "file:///etc/passwd">]><x>&leak;</x>'})
    response = upload(client, "entity.docx", malicious)
    assert response.status_code == 400 and "root:" not in response.text


def test_file_storage_quota_and_extracted_input_limit(client, monkeypatch):
    monkeypatch.setattr(settings, "file_storage_bytes_per_key", 1024)
    response = upload(client, "large.txt", b"word " * 300)
    assert response.status_code == 429
    monkeypatch.setattr(settings, "file_storage_bytes_per_key", 1024 * 1024)
    record = upload(client, "long.txt", b"word " * 300)
    assert record.status_code == 201
    async def set_limit():
        owner = await authenticate_api_key(KEY)
        async with _connect() as db:
            await db.execute("UPDATE api_keys SET limits_json=? WHERE id=?", ('{"input_tokens":32}', owner.id))
            await db.commit()
    client.portal.call(set_limit)
    response = client.post("/v1/chat/completions", headers=HEADERS, json={"model": "local", "messages": [{"role": "user", "content": [{"type": "file", "file": {"file_id": record.json()["id"]}}]}]})
    assert response.status_code == 400 and response.json()["error"]["code"] == "input_token_limit_exceeded"


def test_video_is_sampled_into_bounded_vision_frames(client, tmp_path, monkeypatch):
    source = tmp_path / "red.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=c=red:s=64x64:r=4:d=1", "-an", "-c:v", "libx264", "-threads", "1", "-pix_fmt", "yuv420p", "-y", str(source)], check=True)
    record = upload(client, "red.mp4", source.read_bytes(), "video/mp4")
    assert record.status_code == 201, record.text
    assert record.json()["processing"]["metadata"]["kind"] == "video"
    assert len(record.json()["processing"]["metadata"]["sampled_times_seconds"]) == 4

    async def require(_app):
        return object()

    async def complete(self, request, model):
        assert len(request.messages[-1].image_payloads()) == 4
        assert "sampled" in request.messages[-1].text_content()
        return "red video frames"

    monkeypatch.setattr("app.main._require_ollama_client", require)
    monkeypatch.setattr(OllamaProvider, "complete", complete)
    response = client.post("/v1/chat/completions", headers=HEADERS, json={"model": "local", "messages": [{"role": "user", "content": [{"type": "file", "file": {"file_id": record.json()["id"]}}]}]})
    assert response.status_code == 200
    hostile = upload(client, "playlist.mp4", b"#EXTM3U\nhttp://127.0.0.1/private\n", "video/mp4")
    assert hostile.status_code == 400


def test_processing_timeout_is_reported_and_does_not_store_file(client, monkeypatch):
    monkeypatch.setattr(settings, "file_processing_timeout_seconds", 0.001)
    response = upload(client, "note.txt", b"text")
    assert response.status_code == 422 and response.json()["error"]["code"] == "file_processing_timeout"
    assert client.get("/v1/files", headers=HEADERS).json()["data"] == []
