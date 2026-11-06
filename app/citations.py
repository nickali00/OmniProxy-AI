"""Verifiable references to immutable uploaded bytes and extracted text units."""

import hashlib
import json
import time
import uuid
from typing import Literal

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.database import _connect, record_schema_version
from app.errors import GatewayError
from app.file_uploads import extracted
from app.request_lifecycle import await_connected

router = APIRouter()
SCHEMA = """
CREATE TABLE IF NOT EXISTS document_citations (
 id TEXT PRIMARY KEY, api_key_id INTEGER NOT NULL REFERENCES api_keys(id),
 file_id TEXT NOT NULL REFERENCES uploaded_files(id) ON DELETE CASCADE,
 fingerprint TEXT NOT NULL, locator_json TEXT NOT NULL, created_at REAL NOT NULL,
 UNIQUE(api_key_id,fingerprint)
);
"""


async def init_citations():
    async with _connect() as db:
        await db.executescript(SCHEMA)
        await record_schema_version(db, "citations", 1)


def failure(code, status=422):
    return GatewayError(status, "Riferimento documentale non verificabile.", code=code)


def sha(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class Citation(BaseModel):
    file_id: str = Field(pattern=r"^file-[0-9a-f]{32}$")
    unit_id: str = Field(pattern=r"^(page-[1-9][0-9]{0,3}|document-1)$")
    mode: Literal["auto", "text", "vision"] = "auto"
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    text_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    start: int = Field(ge=0, le=1_000_000)
    end: int = Field(ge=1, le=1_000_000)
    quote: str = Field(min_length=1, max_length=4000)
    model_config = ConfigDict(extra="forbid")

    @field_validator("quote")
    @classmethod
    def valid_quote(cls, value):
        value.encode("utf-8")
        return value

    @model_validator(mode="after")
    def valid_range(self):
        if self.end <= self.start or self.end - self.start > 4000:
            raise ValueError("Invalid citation range")
        return self


def units(row, extraction):
    result = []
    parts = [part["text"] for part in extraction["parts"] if part.get("type") == "text"]
    if row["kind"] == "pdf":
        expected = int(extraction["metadata"]["pages"])
        if len(parts) != expected:
            raise failure("citation_extraction_unavailable", 503)
        for page, text in enumerate(parts, 1):
            prefix = f"[page {page}]\n"
            if not text.startswith(prefix):
                raise failure("citation_extraction_unavailable", 503)
            text = text[len(prefix):]
            if text == "[no extracted text]":
                text = ""
            result.append({"id": f"page-{page}", "page": page, "text": text, "sha256": sha(text)})
    elif parts:
        text = "\n".join(parts)
        result.append({"id": "document-1", "page": None, "text": text, "sha256": sha(text)})
    return result


async def extraction(app, file_id, owner, mode):
    row, parsed = await extracted(app, file_id, owner, mode)
    if hashlib.sha256(bytes(row["content"])).hexdigest() != row["sha256"]:
        raise failure("citation_source_changed", 409)
    return row, {"file_id": file_id, "source_sha256": row["sha256"], "filename": row["filename"], "media_kind": row["kind"], "mode": mode, "units": units(row, parsed), "warnings": parsed["warnings"], "expires_at": row["expires_at"], "offset_unit": "unicode_codepoints", "content_url": f"/v1/files/{file_id}/content"}


async def verify(app, owner, value):
    row, parsed = await extraction(app, value.file_id, owner, value.mode)
    unit = next((unit for unit in parsed["units"] if unit["id"] == value.unit_id), None)
    if unit is None:
        raise failure("citation_unit_not_found", 404)
    if value.source_sha256 != row["sha256"] or value.text_sha256 != unit["sha256"]:
        raise failure("citation_source_changed", 409)
    if value.end > len(unit["text"]) or unit["text"][value.start:value.end] != value.quote:
        raise failure("citation_quote_mismatch")
    return {"valid": True, "contract_version": "1", "file_id": value.file_id, "filename": row["filename"], "source_sha256": row["sha256"], "unit_id": unit["id"], "page": unit["page"], "mode": value.mode, "text_sha256": unit["sha256"], "start": value.start, "end": value.end, "quote": value.quote, "offset_unit": "unicode_codepoints", "content_url": parsed["content_url"], "expires_at": row["expires_at"]}


@router.get("/v1/files/{file_id}/extraction")
async def get_extraction(file_id: str, request: Request, mode: Literal["auto", "text", "vision"] = "auto"):
    _, result = await await_connected(request, extraction(request.app, file_id, int(request.state.api_key_id), mode))
    return JSONResponse(result, headers={"Cache-Control": "no-store"})


@router.post("/v1/citations/verify")
async def verify_citation(payload: Citation, request: Request):
    result = await await_connected(request, verify(request.app, int(request.state.api_key_id), payload))
    return JSONResponse(result, headers={"Cache-Control": "no-store"})


@router.post("/v1/citations")
async def create_citation(payload: Citation, request: Request):
    owner = int(request.state.api_key_id)
    result = await await_connected(request, verify(request.app, owner, payload))
    locator = payload.model_dump(exclude={"quote"})
    serialized = json.dumps(locator, sort_keys=True, separators=(",", ":"))
    fingerprint = sha(serialized)
    async with _connect() as db:
        await db.execute("BEGIN IMMEDIATE")
        # Recheck source lifetime inside the transaction after extraction.
        if await (await db.execute("SELECT 1 FROM uploaded_files WHERE id=? AND api_key_id=? AND expires_at>?", (payload.file_id, owner, time.time()))).fetchone() is None:
            raise failure("file_not_found", 404)
        previous = await (await db.execute("SELECT id FROM document_citations WHERE api_key_id=? AND fingerprint=?", (owner, fingerprint))).fetchone()
        if previous:
            citation_id, created = previous["id"], False
        else:
            count = (await (await db.execute("SELECT count(*) FROM document_citations WHERE api_key_id=?", (owner,))).fetchone())[0]
            if count >= 1000:
                raise failure("citation_storage_limit", 429)
            citation_id, created = "cite_" + uuid.uuid4().hex, True
            await db.execute("INSERT INTO document_citations VALUES (?,?,?,?,?,?)", (citation_id, owner, payload.file_id, fingerprint, serialized, time.time()))
        await db.commit()
    return JSONResponse({"id": citation_id, **result}, status_code=201 if created else 200, headers={"Cache-Control": "no-store"})


@router.get("/v1/citations/{citation_id}")
async def get_citation(citation_id: str, request: Request):
    owner = int(request.state.api_key_id)
    async with _connect() as db:
        row = await (await db.execute("SELECT * FROM document_citations WHERE id=? AND api_key_id=?", (citation_id, owner))).fetchone()
    if row is None:
        raise failure("citation_not_found", 404)
    locator = json.loads(row["locator_json"])
    _, parsed = await await_connected(request, extraction(request.app, locator["file_id"], owner, locator["mode"]))
    unit = next((unit for unit in parsed["units"] if unit["id"] == locator["unit_id"]), None)
    if unit is None or unit["sha256"] != locator["text_sha256"] or locator["end"] > len(unit["text"]):
        raise failure("citation_source_changed", 409)
    value = Citation(**locator, quote=unit["text"][locator["start"]:locator["end"]])
    result = await await_connected(request, verify(request.app, owner, value))
    return JSONResponse({"id": citation_id, **result}, headers={"Cache-Control": "no-store"})


@router.delete("/v1/citations/{citation_id}", status_code=204)
async def delete_citation(citation_id: str, request: Request):
    async with _connect() as db:
        changed = await db.execute("DELETE FROM document_citations WHERE id=? AND api_key_id=?", (citation_id, int(request.state.api_key_id)))
        if changed.rowcount != 1:
            raise failure("citation_not_found", 404)
        await db.commit()
    return Response(status_code=204)
