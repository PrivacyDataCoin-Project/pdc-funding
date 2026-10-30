"""HTTP login helpers that complete TOTP like production."""

from __future__ import annotations

from funding.totp import current_code, derive_kek, encrypt_secret

TEST_TOTP_KEK = "test-funding-totp-kek"
TEST_TOTP_SECRET = "JBSWY3DPEHPK3PXP"


def enable_totp(svc, user_id: int, secret: str = TEST_TOTP_SECRET) -> None:
    kek = derive_kek(svc.settings.funding_totp_kek)
    svc.db.set_totp_secret(user_id, encrypt_secret(secret, kek), enabled=True)


def login_with_totp(client, username: str, password: str, secret: str = TEST_TOTP_SECRET):
    r = client.post(
        "/login",
        data={"username": username, "password": password},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers.get("location") == "/login/2fa"
    r2 = client.post(
        "/login/2fa",
        data={"code": current_code(secret)},
        follow_redirects=False,
    )
    assert r2.status_code == 303
    assert r2.headers.get("location") == "/"
    return r2
