from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from cryptography.fernet import Fernet, InvalidToken

from app.config import settings


class ProviderVaultError(RuntimeError):
    pass


class ProviderVault:
    """
    Cifra configurazioni OAuth e token prima che raggiungano SQLite.

    La chiave può essere iniettata con PROVIDER_VAULT_KEY. Per l'installazione
    locale viene creata una chiave 0600 accanto al database: il file va incluso
    nei backup sicuri, ma non deve essere esposto dalla dashboard.
    """

    def __init__(self) -> None:
        self._fernet: Fernet | None = None

    def encrypt_json(self, payload: dict[str, Any]) -> str:
        raw = json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        return self._cipher().encrypt(raw).decode("ascii")

    def decrypt_json(self, ciphertext: str) -> dict[str, Any]:
        try:
            raw = self._cipher().decrypt(ciphertext.encode("ascii"))
            value = json.loads(raw)
        except (InvalidToken, UnicodeError, ValueError, TypeError) as exc:
            raise ProviderVaultError(
                "Le credenziali provider non possono essere decifrate."
            ) from exc
        if not isinstance(value, dict):
            raise ProviderVaultError(
                "Il contenuto cifrato del provider non è valido."
            )
        return value

    def _cipher(self) -> Fernet:
        if self._fernet is not None:
            return self._fernet

        configured_key = settings.provider_vault_key.strip()
        if configured_key:
            key = configured_key.encode("ascii")
        else:
            key = self._load_or_create_key_file()
        try:
            self._fernet = Fernet(key)
        except (ValueError, TypeError) as exc:
            raise ProviderVaultError(
                "PROVIDER_VAULT_KEY non contiene una chiave Fernet valida."
            ) from exc
        return self._fernet

    @staticmethod
    def _key_path() -> Path:
        if settings.provider_vault_key_path is not None:
            return settings.provider_vault_key_path
        return Path(settings.database_path).parent / ".omniproxy-vault.key"

    def _load_or_create_key_file(self) -> bytes:
        path = self._key_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            return path.read_bytes().strip()
        except FileNotFoundError:
            pass
        except OSError as exc:
            raise ProviderVaultError(
                "La chiave locale delle credenziali non è leggibile."
            ) from exc

        generated = self._legacy_key_for_migration(path)
        if generated is None:
            generated = Fernet.generate_key()
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        try:
            descriptor = os.open(path, flags, 0o600)
        except FileExistsError:
            try:
                return path.read_bytes().strip()
            except OSError as exc:
                raise ProviderVaultError(
                    "La chiave locale delle credenziali non è leggibile."
                ) from exc
        except OSError as exc:
            raise ProviderVaultError(
                "La chiave locale delle credenziali non può essere creata."
            ) from exc

        try:
            with os.fdopen(descriptor, "wb") as key_file:
                key_file.write(generated)
                key_file.write(b"\n")
        except OSError as exc:
            raise ProviderVaultError(
                "La chiave locale delle credenziali non può essere salvata."
            ) from exc
        return generated

    @staticmethod
    def _legacy_key_for_migration(target: Path) -> bytes | None:
        """
        Copia in modo non distruttivo la vecchia chiave accanto a SQLite.

        Il Compose attuale usa un volume vault separato. Le installazioni
        precedenti continuano a decifrare i dati già presenti senza eliminare
        automaticamente la copia originale.
        """

        if settings.provider_vault_key_path is None:
            return None
        legacy = (
            Path(settings.database_path).parent / ".omniproxy-vault.key"
        )
        if legacy == target:
            return None
        try:
            value = legacy.read_bytes().strip()
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise ProviderVaultError(
                "La precedente chiave delle credenziali non è leggibile."
            ) from exc
        return value or None
