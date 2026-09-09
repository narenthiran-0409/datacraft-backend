"""Credential vault abstraction.

LocalRedisVaultClient is a LOCAL-DEVELOPMENT-ONLY stub:
  - Secrets are Fernet-encrypted before being stored, but the encryption key
    (VAULT_LOCAL_ENCRYPTION_KEY) lives in application config/environment,
    not a separate key-management service.
  - Storage is plain Redis with no persistence guarantee configured here —
    a Redis flush or restart without RDB/AOF persistence enabled loses every
    stored credential (and every connection created against it becomes
    unusable until its credential is re-entered).
  - This MUST be replaced by a real secrets manager (e.g. AWS Secrets
    Manager, HashiCorp Vault, Azure Key Vault) before any non-local
    deployment. Do not reuse this implementation past local development.
"""

import json
import uuid
from abc import ABC, abstractmethod

from cryptography.fernet import Fernet, InvalidToken
from redis import Redis

from app.core.exceptions import CredentialVaultError

_VAULT_KEY_PREFIX = "vault:credential:"


class CredentialVaultClient(ABC):
    @abstractmethod
    def store(self, secret: dict) -> str:
        """Persists secret, returns an opaque credential_ref."""

    @abstractmethod
    def resolve(self, credential_ref: str) -> dict:
        """Returns the original secret dict for a previously stored credential_ref."""


class LocalRedisVaultClient(CredentialVaultClient):
    def __init__(self, redis_client: Redis, encryption_key: str) -> None:
        self._redis = redis_client
        self._fernet = Fernet(encryption_key.encode())

    def store(self, secret: dict) -> str:
        credential_ref = str(uuid.uuid4())
        try:
            encrypted = self._fernet.encrypt(json.dumps(secret).encode())
            self._redis.set(f"{_VAULT_KEY_PREFIX}{credential_ref}", encrypted)
        except Exception as exc:  # noqa: BLE001
            raise CredentialVaultError(f"Failed to store credential: {exc}") from exc
        return credential_ref

    def resolve(self, credential_ref: str) -> dict:
        try:
            encrypted = self._redis.get(f"{_VAULT_KEY_PREFIX}{credential_ref}")
        except Exception as exc:  # noqa: BLE001
            raise CredentialVaultError(f"Failed to reach credential vault: {exc}") from exc

        if encrypted is None:
            raise CredentialVaultError(f"Unknown credential_ref: {credential_ref}")

        try:
            if isinstance(encrypted, str):
                encrypted = encrypted.encode()
            decrypted = self._fernet.decrypt(encrypted)
        except InvalidToken as exc:
            raise CredentialVaultError("Stored credential could not be decrypted") from exc

        return json.loads(decrypted)
