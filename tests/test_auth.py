import pyotp
from auth import (
    verify_password,
    get_password_hash,
    generate_mfa_secret,
    get_totp_uri,
    verify_totp,
    _hash_api_key,
    create_api_key,
)


def test_password_hashing():
    password = "SuperSecretPassword123!"
    hashed = get_password_hash(password)

    assert hashed != password
    assert verify_password(password, hashed) is True
    assert verify_password("WrongPassword", hashed) is False


def test_mfa_secret_generation():
    secret = generate_mfa_secret()
    assert isinstance(secret, str)
    assert len(secret) >= 16


def test_totp_uri():
    secret = "JBSWY3DPEHPK3PXP"
    username = "admin"
    uri = get_totp_uri(secret, username)
    assert uri.startswith("otpauth://totp/")
    assert "admin" in uri


def test_verify_totp():
    secret = pyotp.random_base32()
    totp = pyotp.TOTP(secret)
    current_code = totp.now()

    assert verify_totp(secret, current_code) is True
    assert verify_totp(secret, "000000") is False


def test_api_key_generation(db_session):
    from models import User

    # create a dummy user
    u = User(username="test_api_key", hashed_password="dummy")
    db_session.add(u)
    db_session.commit()
    db_session.refresh(u)

    raw_key, api_key = create_api_key(db_session, u.id, "test_key")
    assert raw_key.startswith("nbak_")
    assert len(raw_key) > 30
    assert api_key.name == "test_key"
    assert api_key.key_hash == _hash_api_key(raw_key)


def test_api_key_hashing():
    raw_key = "nbak_1234567890abcdef"
    hashed = _hash_api_key(raw_key)

    # Hashed version should not be the raw key
    assert hashed != raw_key
    # Hashing should be deterministic
    assert _hash_api_key(raw_key) == hashed
