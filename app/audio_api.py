import re
from typing import Annotated

from fastapi import APIRouter, File, Form, Request, UploadFile
from fastapi.responses import JSONResponse, PlainTextResponse

from app import token_budget
from app.config import settings
from app.errors import GatewayError
from app.file_uploads import classify, extract_content, file_error
from app.request_lifecycle import await_connected

router = APIRouter()


@router.post("/v1/audio/transcriptions")
async def create_transcription(request: Request, file: Annotated[UploadFile, File()], model: Annotated[str, Form()] = "transcription", response_format: Annotated[str, Form()] = "json", language: Annotated[str | None, Form()] = None):
    try:
        if model != "transcription" or response_format not in {"json", "text"} or (language is not None and re.fullmatch(r"[a-z]{2,3}", language) is None):
            raise GatewayError(400, "Parametri di trascrizione non supportati.", code="invalid_transcription_request")
        form = await request.form()
        if set(form) - {"file", "model", "response_format", "language"}:
            raise GatewayError(400, "Opzione di trascrizione non supportata.", code="unsupported_transcription_feature")
        _, kind, _ = classify(file.filename)
        if kind not in {"audio", "video"}:
            raise file_error("file_type_not_supported", 415)
        content = await file.read(settings.file_max_bytes + 1)
    finally:
        await file.close()
    if not content or len(content) > settings.file_max_bytes:
        raise file_error("file_extraction_limit", 413)
    result = await await_connected(request, extract_content(request.app, int(request.state.api_key_id), content, kind, "text", language))
    transcript = result["parts"][0]["text"].removeprefix("[audio transcript]\n")
    metadata = result["metadata"]["transcription"]
    headers = {"X-OmniProxy-Tokens-Estimated": "true", "X-OmniProxy-Output-Truncated": str(metadata["truncated"]).lower()}
    budget = await token_budget.status(int(request.state.api_key_id))
    if budget["remaining_tokens"] is not None:
        headers["X-OmniProxy-Budget-Remaining"] = str(budget["remaining_tokens"])
        headers["X-OmniProxy-Budget-Reset"] = budget["resets_at"]
    if response_format == "text":
        return PlainTextResponse(transcript, headers=headers)
    return JSONResponse({"text": transcript, **metadata}, headers=headers)
