from __future__ import annotations

from urllib.parse import urlsplit

from fastapi import HTTPException, Request, status


def require_dashboard_mutation(request: Request) -> None:
    """
    Protegge le operazioni OAuth da CSRF e DNS rebinding.

    La dashboard gira solo in loopback. Ogni POST/DELETE deve provenire dalla
    stessa origin e includere un header custom che una pagina esterna non può
    inviare senza una preflight CORS (che l'applicazione non abilita).
    """

    if request.headers.get("x-omniproxy-request") != "dashboard":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Richiesta dashboard non autorizzata.",
        )

    origin = request.headers.get("origin")
    host = request.headers.get("host")
    if not origin or not host:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Origin mancante.",
        )

    parsed = urlsplit(origin)
    if (
        parsed.scheme not in {"http", "https"}
        or parsed.netloc != host
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Origin non consentita.",
        )
