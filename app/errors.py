from typing import Any


def openai_error_body(
    message: str,
    *,
    error_type: str,
    code: str | None = None,
    param: str | None = None,
) -> dict[str, Any]:
    """Build the error envelope expected by OpenAI-compatible clients."""
    return {
        "error": {
            "message": message,
            "type": error_type,
            "param": param,
            "code": code,
        }
    }


class GatewayError(Exception):
    def __init__(
        self,
        status_code: int,
        message: str,
        *,
        error_type: str = "gateway_error",
        code: str | None = None,
        param: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.message = message
        self.error_type = error_type
        self.code = code
        self.param = param
