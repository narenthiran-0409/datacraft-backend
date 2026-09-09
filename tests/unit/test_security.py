import time
import uuid

import jwt
import pytest

from app.core.config import settings
from app.core.security import create_access_token, decode_token, hash_password, verify_password


def test_hash_password_round_trip() -> None:
    password = "Str0ng!Passw0rd"
    hashed = hash_password(password)

    assert hashed != password
    assert verify_password(password, hashed)
    assert not verify_password("wrong-password", hashed)


def test_hash_password_uses_argon2() -> None:
    hashed = hash_password("Str0ng!Passw0rd")
    assert hashed.startswith("$argon2")


def test_create_access_token_round_trip() -> None:
    user_id = uuid.uuid4()
    token, jti = create_access_token(user_id)

    payload = decode_token(token)
    assert payload["sub"] == str(user_id)
    assert payload["jti"] == jti
    assert payload["token_type"] == "access"
    assert payload["exp"] > payload["iat"]


def test_decode_token_rejects_tampered_signature() -> None:
    token, _ = create_access_token(uuid.uuid4())
    # Tamper a character away from the very end: the last base64url
    # character of a 256-bit HMAC-SHA256 signature only encodes 4
    # significant bits plus 2 discarded padding bits, so flipping it is a
    # silent no-op on the decoded bytes ~6.6% of the time (empirically
    # confirmed), making the test flaky. A character further in avoids the
    # padding boundary and deterministically changes the decoded signature.
    idx = -10
    replacement = "A" if token[idx] != "A" else "B"
    tampered = token[:idx] + replacement + token[idx + 1 :]

    with pytest.raises(jwt.PyJWTError):
        decode_token(tampered)


def test_decode_token_rejects_expired_token() -> None:
    payload = {
        "sub": str(uuid.uuid4()),
        "iat": int(time.time()) - 100,
        "exp": int(time.time()) - 1,
        "jti": str(uuid.uuid4()),
        "token_type": "access",
    }
    expired_token = jwt.encode(payload, settings.JWT_SECRET_KEY, algorithm=settings.JWT_ALGORITHM)

    with pytest.raises(jwt.ExpiredSignatureError):
        decode_token(expired_token)


def test_access_token_does_not_embed_roles_or_permissions() -> None:
    token, _ = create_access_token(uuid.uuid4())
    payload = decode_token(token)

    assert set(payload.keys()) == {"sub", "iat", "exp", "jti", "token_type"}
