"""Background worker process bootstrap."""

import logging
from threading import Event

from memory_ops.config import get_settings


def main() -> None:
    settings = get_settings()
    logging.basicConfig(level=logging.INFO)
    logging.getLogger(__name__).info(
        "worker ready",
        extra={"service": settings.service_name, "environment": settings.environment},
    )
    Event().wait()


if __name__ == "__main__":
    main()

