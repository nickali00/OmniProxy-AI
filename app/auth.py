import logging

from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import JSONResponse, Response

from app.database import authenticate_api_key
from app.errors import openai_error_body


logger = logging.getLogger(__name__)


class ApiKeyAuthMiddleware(BaseHTTPMiddleware):
    """
    Protect every OpenAI-compatible route with a local Bearer API key.

    Health probes remain public so Docker can monitor the process. The raw key
    is never attached to request state or written to logs.
    """

    async def dispatch(
        self,
        request: Request,
        call_next: RequestResponseEndpoint,
    ) -> Response:
        if not request.url.path.startswith("/v1/") or request.method == "OPTIONS":
            return await call_next(request)

        authorization = request.headers.get("Authorization", "")
        scheme, separator, token = authorization.partition(" ")
        if not separator or scheme.lower() != "bearer" or not token.strip():
            return self._unauthorized("Missing or malformed Bearer API key.")

        try:
            api_key = await authenticate_api_key(token.strip())
        except Exception:
            logger.exception("API key database lookup failed")
            return JSONResponse(
                status_code=503,
                content=openai_error_body(
                    "Authentication storage is temporarily unavailable.",
                    error_type="service_unavailable",
                    code="auth_storage_unavailable",
                ),
            )

        if api_key is None:
            return self._unauthorized(
                "Invalid, revoked, or paused API key."
            )

        request.state.api_key_id = api_key.id
        request.state.api_key_name = api_key.name
        return await call_next(request)

    @staticmethod
    def _unauthorized(message: str) -> JSONResponse:
        return JSONResponse(
            status_code=401,
            headers={"WWW-Authenticate": "Bearer"},
            content=openai_error_body(
                message,
                error_type="invalid_request_error",
                code="invalid_api_key",
            ),
        )
