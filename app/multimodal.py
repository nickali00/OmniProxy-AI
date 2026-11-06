from __future__ import annotations

import base64
import binascii
import re
from dataclasses import dataclass


MAX_IMAGE_BYTES = 4 * 1024 * 1024
MAX_IMAGES_PER_REQUEST = 8
MAX_TOTAL_IMAGE_BYTES = 8 * 1024 * 1024

_DATA_IMAGE_URL = re.compile(
    r"^data:(image/(?:jpeg|png|webp));base64,([A-Za-z0-9+/]+={0,2})$"
)


@dataclass(frozen=True)
class ImagePayload:
    mime_type: str
    base64_data: str
    data: bytes


def parse_image_data_url(value: object) -> ImagePayload:
    """Validate and decode an inline image without performing network I/O."""
    if not isinstance(value, str):
        raise ValueError("image_url.url deve essere una stringa.")

    match = _DATA_IMAGE_URL.fullmatch(value)
    if match is None:
        raise ValueError(
            "Sono supportate solo immagini JPEG, PNG o WebP inline "
            "nel formato data:image/...;base64."
        )

    mime_type, encoded = match.groups()
    if len(encoded) % 4 != 0:
        raise ValueError("Il contenuto base64 dell'immagine non e' valido.")
    try:
        decoded = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError(
            "Il contenuto base64 dell'immagine non e' valido."
        ) from exc

    if not decoded or len(decoded) > MAX_IMAGE_BYTES:
        raise ValueError(
            f"Ogni immagine deve essere compresa tra 1 byte e "
            f"{MAX_IMAGE_BYTES // (1024 * 1024)} MiB."
        )
    if not _matches_declared_type(mime_type, decoded):
        raise ValueError(
            "Il formato dichiarato in image_url non coincide con i dati."
        )
    return ImagePayload(mime_type, encoded, decoded)


def _matches_declared_type(mime_type: str, data: bytes) -> bool:
    if mime_type == "image/jpeg":
        return (
            len(data) >= 4
            and data.startswith(b"\xff\xd8\xff")
            and data.endswith(b"\xff\xd9")
        )
    if mime_type == "image/png":
        return data.startswith(b"\x89PNG\r\n\x1a\n")
    if mime_type == "image/webp":
        return (
            len(data) >= 12
            and data.startswith(b"RIFF")
            and data[8:12] == b"WEBP"
        )
    return False
