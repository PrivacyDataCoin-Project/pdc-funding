"""HTTP login helpers that complete TOTP like production."""

from __future__ import annotations

from funding.totp import current_code, derive_kek, encrypt_secret, verify_code

TEST_TOTP_KEK = "test-funding-totp-kek"
TEST_TOTP_SECRET = "JBSWY3DPEHPK3PXP"


def enable_totp(svc, user_id: int, secret: str = TEST_TOTP_SECRET) -> None:
    kek = derive_kek(svc.settings.funding_totp_kek)
    blob = encrypt_secret(secret, kek)
    svc.db.set_totp_secret(user_id, blob, enabled=True)
    # Round-trip sanity so flaky hosts fail early with a clear error.
    stored = svc.db.get_totp_secret_enc(user_id)
    assert stored is not None
    from funding.totp import decrypt_secret

    assert decrypt_secret(stored, kek) == secret
    assert verify_code(secret, current_code(secret), window=1)


def login_with_totp(client, username: str, password: str, secret: str = TEST_TOTP_SECRET):
    r = client.post(
        "/login",
        data={"username": username, "password": password},
        follow_redirects=False,
    )
    assert r.status_code == 303, r.text
    assert r.headers.get("location") == "/login/2fa", r.headers.get("location")
    r2 = client.post(
        "/login/2fa",
        data={"code": current_code(secret)},
        follow_redirects=False,
    )
    assert r2.status_code == 303
    assert r2.headers.get("location") == "/", r2.headers.get("location")
    return r2
