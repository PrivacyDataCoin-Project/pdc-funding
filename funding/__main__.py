"""python -m funding"""

from __future__ import annotations

import uvicorn

from funding.config import get_settings


def main() -> None:
    settings = get_settings()
    uvicorn.run(
        "funding.app:app",
        host=settings.bind_host,
        port=settings.bind_port,
        reload=False,
    )


if __name__ == "__main__":
    main()
