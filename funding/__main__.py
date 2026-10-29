"""python -m funding"""

from __future__ import annotations

import uvicorn

from funding.config import get_settings

# Public bind + a known dev secret lets anyone forge an admin session.
_PLACEHOLDER_SECRETS = frozenset(
    {
        "dev-only-change-me",
        "change-me-to-a-long-random-string",
        "changeme",
        "secret",
        "test",
        "test-secret",
    }
)


def _loopback(host: str) -> bool:
    return host.strip().lower() in {"127.0.0.1", "::1", "localhost"}


def ensure_bind_allowed(settings) -> None:
    secret = settings.secret_key.strip().lower()
    if not _loopback(settings.bind_host) and secret in _PLACEHOLDER_SECRETS:
        raise SystemExit(
            "Refusing to bind a non-loopback host with a placeholder SECRET_KEY"
        )


def main() -> None:
    settings = get_settings()
    ensure_bind_allowed(settings)
    uvicorn.run(
        "funding.app:app",
        host=settings.bind_host,
        port=settings.bind_port,
        reload=False,
    )


if __name__ == "__main__":
    main()
