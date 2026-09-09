import fakeredis
import pytest
from cryptography.fernet import Fernet

from app.core.exceptions import CredentialVaultError
from app.modules.connections.credential_vault import LocalRedisVaultClient


@pytest.fixture
def vault() -> LocalRedisVaultClient:
    redis_client = fakeredis.FakeStrictRedis()
    return LocalRedisVaultClient(redis_client, Fernet.generate_key().decode())


def test_store_and_resolve_round_trip(vault: LocalRedisVaultClient) -> None:
    secret = {"username": "svc_account", "password": "sup3r-s3cret"}

    credential_ref = vault.store(secret)
    resolved = vault.resolve(credential_ref)

    assert resolved == secret


def test_credential_ref_is_opaque_uuid_not_the_secret(vault: LocalRedisVaultClient) -> None:
    credential_ref = vault.store({"username": "u", "password": "p"})

    assert "u" != credential_ref
    assert "p" not in credential_ref


def test_resolve_unknown_ref_raises(vault: LocalRedisVaultClient) -> None:
    with pytest.raises(CredentialVaultError):
        vault.resolve("00000000-0000-0000-0000-000000000000")


def test_stored_value_is_encrypted_at_rest() -> None:
    redis_client = fakeredis.FakeStrictRedis()
    vault = LocalRedisVaultClient(redis_client, Fernet.generate_key().decode())
    secret = {"username": "svc_account", "password": "sup3r-s3cret"}

    credential_ref = vault.store(secret)

    raw = redis_client.get(f"vault:credential:{credential_ref}")
    assert b"sup3r-s3cret" not in raw
