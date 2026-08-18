"""Normalize common polite commands before Home Assistant intent matching."""

from __future__ import annotations

import re

_ITALIAN_POLITE_COMMAND = re.compile(
    r"^\s*(?:(?:per\s+favore|gentilmente)[,\s]+)?"
    r"(?:mi\s+)?(?:puoi|potresti)\s+"
    r"(accendere|spegnere|impostare|aprire|chiudere|alzare|abbassare)\b",
    re.IGNORECASE,
)
_ITALIAN_IMPERATIVES = {
    "accendere": "accendi",
    "spegnere": "spegni",
    "impostare": "imposta",
    "aprire": "apri",
    "chiudere": "chiudi",
    "alzare": "alza",
    "abbassare": "abbassa",
}
_CONTROL_WORDS = re.compile(
    r"\b(?:accendi|accendere|spegni|spegnere|imposta|impostare|apri|aprire|"
    r"chiudi|chiudere|alza|alzare|abbassa|abbassare)\b",
    re.IGNORECASE,
)


def local_intent_candidates(text: str) -> tuple[str, ...]:
    """Return the original phrase and a safe canonical Italian variant."""
    stripped = text.strip()
    if not stripped:
        return (text,)

    match = _ITALIAN_POLITE_COMMAND.match(stripped)
    if match is None:
        return (stripped,)

    imperative = _ITALIAN_IMPERATIVES[match.group(1).lower()]
    normalized = f"{imperative}{stripped[match.end() :]}".strip()
    if normalized.casefold() == stripped.casefold():
        return (stripped,)
    return (stripped, normalized)


def looks_like_control_command(text: str) -> bool:
    """Return whether a request appears to ask for a device action."""
    return _CONTROL_WORDS.search(text) is not None
