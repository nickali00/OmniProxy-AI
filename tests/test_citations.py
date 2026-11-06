import hashlib
import io
import json

import test_files as files
from PIL import Image, ImageDraw, ImageFont

from app.database import _connect, create_api_key

client = files.client
HEADERS = files.HEADERS


def citation(extraction, text, unit=0):
    selected = extraction["units"][unit]
    start = selected["text"].index(text)
    return {"file_id": extraction["file_id"], "unit_id": selected["id"], "mode": extraction["mode"], "source_sha256": extraction["source_sha256"], "text_sha256": selected["sha256"], "start": start, "end": start + len(text), "quote": text}


def multi_page_pdf():
    def page(contents):
        return f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 7 0 R >> >> /Contents {contents} 0 R >>".encode()
    def stream(text):
        content = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode()
        return b"<< /Length " + str(len(content)).encode() + b" >>\nstream\n" + content + b"\nendstream"
    objects = [b"<< /Type /Catalog /Pages 2 0 R >>", b"<< /Type /Pages /Kids [3 0 R 5 0 R] /Count 2 >>", page(4), stream("BLUE"), page(6), stream("GREEN"), b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    output, offsets = bytearray(b"%PDF-1.4\n"), [0]
    for index, value in enumerate(objects, 1):
        offsets.append(len(output))
        output.extend(f"{index} 0 obj\n".encode() + value + b"\nendobj\n")
    position = len(output)
    output.extend(f"xref\n0 {len(objects)+1}\n0000000000 65535 f \n".encode())
    for offset in offsets[1:]:
        output.extend(f"{offset:010d} 00000 n \n".encode())
    output.extend(f"trailer\n<< /Size {len(objects)+1} /Root 1 0 R >>\nstartxref\n{position}\n%%EOF\n".encode())
    return bytes(output)


def test_unicode_ranges_roundtrip_idempotency_and_explicit_verification(client):
    content = "😀 Riga uno\nIl colore è BLU.\n".encode()
    file_id = files.upload(client, "notes.txt", content).json()["id"]
    extraction = client.get(f"/v1/files/{file_id}/extraction", headers=HEADERS).json()
    assert extraction["source_sha256"] == hashlib.sha256(content).hexdigest()
    assert extraction["offset_unit"] == "unicode_codepoints"
    value = citation(extraction, "BLU")
    created = client.post("/v1/citations", headers=HEADERS, json=value)
    assert created.status_code == 201, created.text
    replay = client.post("/v1/citations", headers=HEADERS, json=value)
    assert replay.status_code == 200 and replay.json()["id"] == created.json()["id"]
    assert client.post("/v1/citations/verify", headers=HEADERS, json=value).json()["valid"]
    retrieved = client.get("/v1/citations/" + created.json()["id"], headers=HEADERS)
    assert retrieved.json()["quote"] == "BLU" and retrieved.json()["page"] is None
    assert retrieved.headers["Cache-Control"] == "no-store"


def test_pdf_page_identity_prevents_quoting_a_different_page(client):
    file_id = files.upload(client, "two-pages.pdf", multi_page_pdf()).json()["id"]
    extraction = client.get(f"/v1/files/{file_id}/extraction", headers=HEADERS).json()
    assert [unit["page"] for unit in extraction["units"]] == [1, 2]
    value = citation(extraction, "GREEN", 1)
    assert client.post("/v1/citations/verify", headers=HEADERS, json=value).json()["page"] == 2
    wrong = {**citation(extraction, "BLUE"), "unit_id": "page-2", "text_sha256": extraction["units"][1]["sha256"]}
    refused = client.post("/v1/citations/verify", headers=HEADERS, json=wrong)
    assert refused.status_code == 422 and refused.json()["error"]["code"] == "citation_quote_mismatch"


def test_real_ocr_is_an_explicit_citable_extraction(client):
    image = Image.new("RGB", (600, 180), "white")
    ImageDraw.Draw(image).text((30, 30), "BLUE", font=ImageFont.load_default(size=72), fill="black")
    content = io.BytesIO()
    image.save(content, format="PNG")
    file_id = files.upload(client, "scan.png", content.getvalue()).json()["id"]
    assert client.get(f"/v1/files/{file_id}/extraction", headers=HEADERS).json()["units"] == []
    extraction = client.get(f"/v1/files/{file_id}/extraction?mode=text", headers=HEADERS).json()
    value = citation(extraction, "BLUE")
    response = client.post("/v1/citations", headers=HEADERS, json=value)
    assert response.status_code == 201 and response.json()["mode"] == "text"


def test_wrong_owner_hash_range_and_quote_are_rejected_without_echo(client):
    file_id = files.upload(client, "notes.txt", b"BLUE").json()["id"]
    extraction = client.get(f"/v1/files/{file_id}/extraction", headers=HEADERS).json()
    value = citation(extraction, "BLUE")
    _, other_key = client.portal.call(create_api_key, "Other owner")
    other = {"Authorization": "Bearer " + other_key}
    assert client.get(f"/v1/files/{file_id}/extraction", headers=other).status_code == 404
    assert client.post("/v1/citations", headers=other, json=value).status_code == 404
    created = client.post("/v1/citations", headers=HEADERS, json=value).json()
    assert client.get("/v1/citations/" + created["id"], headers=other).status_code == 404
    assert client.delete("/v1/citations/" + created["id"], headers=other).status_code == 404
    marker = "PRIVATE_INVENTED_QUOTE"
    for changes, code in [({"source_sha256": "0" * 64}, "citation_source_changed"), ({"text_sha256": "0" * 64}, "citation_source_changed"), ({"quote": marker}, "citation_quote_mismatch"), ({"end": 100}, "citation_quote_mismatch")]:
        response = client.post("/v1/citations/verify", headers=HEADERS, json={**value, **changes})
        assert response.json()["error"]["code"] == code and marker not in response.text


async def sql(statement, parameters):
    async with _connect() as db:
        await db.execute(statement, parameters)
        await db.commit()


def test_citation_detects_changed_cache_and_source_bytes(client):
    file_id = files.upload(client, "notes.txt", b"BLUE").json()["id"]
    extraction = client.get(f"/v1/files/{file_id}/extraction", headers=HEADERS).json()
    created = client.post("/v1/citations", headers=HEADERS, json=citation(extraction, "BLUE")).json()
    client.portal.call(sql, "UPDATE file_extractions SET data_json=? WHERE file_id=?", (json.dumps({"parts": [{"type": "text", "text": "X"}], "metadata": {"kind": "text"}, "warnings": []}), file_id))
    response = client.get("/v1/citations/" + created["id"], headers=HEADERS)
    assert response.status_code == 409 and response.json()["error"]["code"] == "citation_source_changed"
    client.portal.call(sql, "UPDATE uploaded_files SET content=? WHERE id=?", (b"CORRUPTED", file_id))
    assert client.get(f"/v1/files/{file_id}/extraction", headers=HEADERS).status_code == 409


def test_expiry_and_source_deletion_invalidate_citations(client):
    file_id = files.upload(client, "notes.txt", b"BLUE").json()["id"]
    extraction = client.get(f"/v1/files/{file_id}/extraction", headers=HEADERS).json()
    created = client.post("/v1/citations", headers=HEADERS, json=citation(extraction, "BLUE")).json()
    client.portal.call(sql, "UPDATE uploaded_files SET expires_at=0 WHERE id=?", (file_id,))
    assert client.get("/v1/citations/" + created["id"], headers=HEADERS).status_code == 404
    client.portal.call(sql, "DELETE FROM uploaded_files WHERE id=?", (file_id,))
    assert client.get("/v1/citations/" + created["id"], headers=HEADERS).json()["error"]["code"] == "citation_not_found"
