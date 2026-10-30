"""TOTP helpers (admin.trade-compatible: AES-GCM secret, SHA1 6-digit 30s)."""

from __future__ import annotations

import hashlib
import io
import re
import secrets
from typing import Optional

import pyotp
import qrcode
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

_HEX64 = re.compile(r"^[0-9a-fA-F]{64}$")


def derive_kek(raw: str) -> bytes:
    text = (raw or "").strip()
    if not text:
        raise ValueError("FUNDING_TOTP_KEK required")
    if _HEX64.fullmatch(text):
        return bytes.fromhex(text)
    return hashlib.sha256(text.encode("utf-8")).digest()


def encrypt_secret(plain: str, kek: bytes) -> bytes:
    if len(kek) != 32:
        raise ValueError("KEK must be 32 bytes")
    iv = secrets.token_bytes(12)
    aes = AESGCM(kek)
    ct = aes.encrypt(iv, plain.encode("utf-8"), None)
    # AESGCM encrypt returns ciphertext||tag (tag last 16)
    tag, data = ct[-16:], ct[:-16]
    return iv + tag + data


def decrypt_secret(blob: bytes, kek: bytes) -> str:
    if len(kek) != 32:
        raise ValueError("KEK must be 32 bytes")
    raw = bytes(blob)
    if len(raw) < 12 + 16 + 1:
        raise ValueError("invalid totp secret blob")
    iv, tag, data = raw[:12], raw[12:28], raw[28:]
    aes = AESGCM(kek)
    plain = aes.decrypt(iv, data + tag, None)
    return plain.decode("utf-8")


def new_secret_base32() -> str:
    # pyotp length is Base32 chars; 32 chars ≈ 160 bits (RFC 4226 minimum).
    return pyotp.random_base32(length=32)


def provisioning_uri(secret_base32: str, *, username: str, issuer: str) -> str:
    totp = pyotp.TOTP(secret_base32, digits=6, interval=30, digest=hashlib.sha1)
    return totp.provisioning_uri(name=username, issuer_name=issuer)


def verify_code(secret_base32: str, code: str, *, window: int = 1) -> bool:
    cleaned = re.sub(r"\s+", "", (code or "").strip())
    if not cleaned.isdigit() or len(cleaned) != 6:
        return False
    totp = pyotp.TOTP(secret_base32, digits=6, interval=30, digest=hashlib.sha1)
    return bool(totp.verify(cleaned, valid_window=window))


def qr_data_url(otpauth_uri: str) -> str:
    img = qrcode.make(otpauth_uri)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    import base64

    b64 = base64.b64encode(buf.getvalue()).decode("ascii")
    return f"data:image/png;base64,{b64}"


def current_code(secret_base32: str) -> str:
    """Test helper — current TOTP code."""
    totp = pyotp.TOTP(secret_base32, digits=6, interval=30, digest=hashlib.sha1)
    return totp.now()
