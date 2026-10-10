import json
import os
import subprocess
import sys
from pathlib import Path
from uuid import UUID

import httpx


os.environ.setdefault(
    "MEMORY_OPS_DATABASE_URL", "postgresql://compat:compat@localhost/compat"
)
os.environ.setdefault(
    "MEMORY_OPS_MIGRATION_DATABASE_URL",
    "postgresql://compat:compat@localhost/compat",
)

from memory_ops.api import app


ROOT = Path(__file__).parents[2]
sys.path.insert(0, str(ROOT / "sdk/python/src"))

from memory_ops_sdk import (  # noqa: E402
    CorrectMemoryRequest,
    MemoryOpsClient,
    RememberMemoryRequest,
)


TENANT = UUID("30000000-0000-0000-0000-000000000001")
WORKSPACE = UUID("30000000-0000-0000-0000-000000000010")
SUBJECT = UUID("30000000-0000-0000-0000-000000000100")
MEMORY = UUID("30000000-0000-0000-0000-000000000300")
VERSION = UUID("30000000-0000-0000-0000-000000000400")
OPERATION = UUID("30000000-0000-0000-0000-000000000500")


def receipt() -> dict[str, object]:
    return {
        "memory_id": str(MEMORY),
        "version_id": str(VERSION),
        "operation_id": str(OPERATION),
        "operation_status": "pending",
        "replayed": False,
    }


def test_published_openapi_matches_runtime_and_required_memory_operations() -> None:
    published = json.loads((ROOT / "openapi/openapi.json").read_text())
    assert published == app.openapi()

    collection = "/v1/tenants/{tenant_id}/workspaces/{workspace_id}/memories"
    item = f"{collection}/{{memory_id}}"
    assert {"get", "post"} <= published["paths"][collection].keys()
    assert {"get", "delete"} <= published["paths"][item].keys()
    assert "post" in published["paths"][f"{item}/corrections"]


def test_python_sdk_matches_mutating_openapi_paths() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(201 if request.method == "POST" else 202, json=receipt())

    with MemoryOpsClient(
        "https://memory.example",
        "test-token",
        transport=httpx.MockTransport(handler),
    ) as client:
        client.remember(
            TENANT,
            WORKSPACE,
            RememberMemoryRequest(
                subject_id=SUBJECT,
                semantic_type="preference",
                statement="The user prefers concise answers.",
                purpose="assistant_context",
            ),
            idempotency_key="remember-1",
        )
        client.correct(
            TENANT,
            WORKSPACE,
            MEMORY,
            CorrectMemoryRequest(
                subject_id=SUBJECT,
                statement="The user prefers direct answers.",
            ),
            idempotency_key="correct-1",
        )
        client.forget(
            TENANT,
            WORKSPACE,
            MEMORY,
            SUBJECT,
            idempotency_key="forget-1",
        )

    assert [(request.method, request.url.path) for request in requests] == [
        (
            "POST",
            f"/v1/tenants/{TENANT}/workspaces/{WORKSPACE}/memories",
        ),
        (
            "POST",
            f"/v1/tenants/{TENANT}/workspaces/{WORKSPACE}/memories/{MEMORY}/corrections",
        ),
        (
            "DELETE",
            f"/v1/tenants/{TENANT}/workspaces/{WORKSPACE}/memories/{MEMORY}",
        ),
    ]
    assert [request.headers["Idempotency-Key"] for request in requests] == [
        "remember-1",
        "correct-1",
        "forget-1",
    ]
    assert requests[-1].url.params["subject_id"] == str(SUBJECT)


def test_typescript_sdk_builds_and_runs_compatibility_suite() -> None:
    result = subprocess.run(
        ["npm", "test"],
        cwd=ROOT / "sdk/typescript",
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stdout + result.stderr
