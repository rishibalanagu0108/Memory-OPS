"""Export the canonical OpenAPI document."""

import json
from pathlib import Path

from memory_ops.api import create_app
from memory_ops.config import get_settings


def export_openapi(destination: Path) -> None:
    document = create_app(get_settings()).openapi()
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n")


if __name__ == "__main__":
    export_openapi(Path("openapi/openapi.json"))
