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
    r"chiudi|chiudere|alza|alzare|abbassa|abbassare|attiva|avvia|disattiva|"
    r"ferma)\b",
    re.IGNORECASE,
)
_ITALIAN_DIRECT_CLIMATE_COMMAND = re.compile(
    r"^\s*(?P<verb>accendi|attiva|avvia|spegni|disattiva|ferma)\s+"
    r"(?:(?:il|lo|la|un|una)\s+|l['’])?"
    r"(?P<target>.+?)\s*[?.!]*$",
    re.IGNORECASE,
)
_ITALIAN_CLIMATE_NOUN = re.compile(
    r"\b(?:clima|climatizzatore|condizionatore|temperatura|termostato)\b|"
    r"\baria\s+condizionata\b",
    re.IGNORECASE,
)
_TURN_ON_WORDS = {"accendi", "attiva", "avvia"}
_COMMON_ITALIAN_CONTROL_TYPOS = {
    "derlla": "della",
}
_ITALIAN_CLIMATE_ROOM_PHRASE = re.compile(
    r"\b(?:della|nella)\s+(?:stanza|camera)\s+di\s+",
    re.IGNORECASE,
)
_ITALIAN_LEADING_TARGET_FILLERS = re.compile(
    r"^(?:(?:di|del|dello|della|dei|degli|delle|in|nel|nello|nella|nei|"
    r"negli|nelle)\s+)+",
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


def local_climate_control_candidates(text: str) -> tuple[tuple[str, str], ...]:
    """Return target variants for Home Assistant's climate-only fallback."""
    match = _ITALIAN_DIRECT_CLIMATE_COMMAND.match(text)
    if match is None:
        return ()
    target = match.group("target").strip()
    action = (
        "turn_on"
        if match.group("verb").lower() in _TURN_ON_WORDS
        else "turn_off"
    )
    words = target.split()
    normalized_target = " ".join(
        _COMMON_ITALIAN_CONTROL_TYPOS.get(word.casefold(), word) for word in words
    )
    targets: list[str] = []
    for candidate in (
        normalized_target,
        _ITALIAN_CLIMATE_ROOM_PHRASE.sub("di ", normalized_target),
    ):
        candidate = " ".join(candidate.split()).strip()
        if candidate and candidate.casefold() not in {
            item.casefold() for item in targets
        }:
            targets.append(candidate)

        # Provider-created climate entities often have names such as
        # "Garage" or "Stanza Nicola Room Temperature". Also try the spoken
        # location without the generic appliance noun; final matching remains
        # restricted to exposed climate.* entities by Home Assistant.
        location = _ITALIAN_CLIMATE_NOUN.sub(" ", candidate)
        location = _ITALIAN_LEADING_TARGET_FILLERS.sub("", location.strip())
        location = " ".join(location.split()).strip()
        if location and location.casefold() not in {
            item.casefold() for item in targets
        }:
            targets.append(location)
    return tuple((action, candidate) for candidate in targets)
