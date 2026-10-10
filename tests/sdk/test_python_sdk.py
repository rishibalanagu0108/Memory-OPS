import json
import sys
from pathlib import Path
from uuid import UUID

import httpx
import pytest
from pydantic import ValidationError


sys.path.insert(0, str(Path(__file__).parents[2] / "sdk/python/src"))

from memory_ops_sdk import (  # noqa: E402
    CorrectMemoryRequest,
    EvidenceReference,
    MemoryOpsClient,
    MemoryOpsError,
    RememberMemoryRequest,
)


TENANT = UUID("30000000-0000-0000-0000-000000000001")
WORKSPACE = UUID("30000000-0000-0000-0000-000000000010")
SUBJECT = UUID("30000000-0000-0000-0000-000000000100")
MEMORY = UUID("30000000-0000-0000-0000-000000000300")
VERSION = UUID("30000000-0000-0000-0000-000000000400")
OPERATION = UUID("30000000-0000-0000-0000-000000000500")


def memory_payload() -> dict:
    return {
        "memory_id": str(MEMORY),
        "version_id": str(VERSION),
        "version_number": 1,
        "tenant_id": str(TENANT),
        "workspace_id": str(WORKSPACE),
        "subject_id": str(SUBJECT),
        "agent_id": None,
        "semantic_type": "preference",
        "lifecycle": "active",
        "statement": "The user prefers concise answers.",
        "normalized_subject": "user",
        "normalized_predicate": "prefers",
        "normalized_value": "concise answers",
        "qualifiers": {},
        "valid_from": "2026-10-05T10:00:00Z",
        "valid_to": None,
        "recorded_at": "2026-10-05T10:00:00Z",
        "sensitivity": "normal",
        "lifetime": "durable",
        "origin": "explicit",
        "purpose": "assistant_context",
        "policy_version": "local-test-token-v1",
        "access_scope": [],
        "retention_until": None,
        "evidence": [
            {
                "evidence_type": "conversation_message",
                "reference_id": "message-1",
                "locator": None,
            }
        ],
    }


def test_typed_sdk_covers_supported_memory_operations() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method in {"POST", "DELETE"}:
            return httpx.Response(
                201,
                json={
                    "memory_id": str(MEMORY),
                    "version_id": str(VERSION),
                    "operation_id": str(OPERATION),
                    "operation_status": "pending",
                    "replayed": False,
                },
            )
        if request.url.path.endswith(f"/memories/{MEMORY}"):
            return httpx.Response(200, json=memory_payload())
        if request.url.path.endswith("/memories"):
            return httpx.Response(200, json={"items": [memory_payload()]})
        return httpx.Response(
            200,
            json={
                "operation_id": str(OPERATION),
                "status": "pending",
                "attempts": 0,
                "last_error_code": None,
            },
        )

    request = RememberMemoryRequest(
        subject_id=SUBJECT,
        semantic_type="preference",
        statement="The user prefers concise answers.",
        purpose="assistant_context",
        evidence=(EvidenceReference(
            evidence_type="conversation_message", reference_id="message-1"
        ),),
    )
    with MemoryOpsClient(
        "https://memory.example", "test-token", transport=httpx.MockTransport(handler)
    ) as client:
        remembered = client.remember(
            TENANT, WORKSPACE, request, idempotency_key="remember-1"
        )
        inspected = client.inspect(TENANT, WORKSPACE, remembered.memory_id)
        corrected = client.correct(
            TENANT,
            WORKSPACE,
            remembered.memory_id,
            CorrectMemoryRequest(
                subject_id=SUBJECT,
                statement="The user prefers light mode.",
            ),
            idempotency_key="correct-1",
        )
        listed = client.list_memories(
            TENANT, WORKSPACE, subject_id=SUBJECT, purpose="assistant_context", limit=10
        )
        operation = client.operation_status(
            TENANT, WORKSPACE, remembered.operation_id
        )
        forgotten = client.forget(
            TENANT,
            WORKSPACE,
            remembered.memory_id,
            SUBJECT,
            idempotency_key="forget-1",
        )

    assert remembered.memory_id == MEMORY
    assert inspected.statement == request.statement
    assert corrected.memory_id == MEMORY
    assert listed.items == (inspected,)
    assert operation.status == "pending"
    assert forgotten.memory_id == MEMORY
    assert all(item.headers["Authorization"] == "Bearer test-token" for item in requests)
    assert requests[0].headers["Idempotency-Key"] == "remember-1"
    assert json.loads(requests[0].content)["subject_id"] == str(SUBJECT)
    assert requests[2].url.path.endswith(f"/memories/{MEMORY}/corrections")
    assert requests[2].headers["Idempotency-Key"] == "correct-1"
    assert dict(requests[3].url.params) == {
        "limit": "10",
        "subject_id": str(SUBJECT),
        "purpose": "assistant_context",
    }
    assert requests[5].method == "DELETE"
    assert dict(requests[5].url.params) == {"subject_id": str(SUBJECT)}
    assert requests[5].headers["Idempotency-Key"] == "forget-1"


def test_sdk_returns_safe_structured_api_errors() -> None:
    def denied(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            401,
            json={"code": "unauthenticated", "message": "authentication required"},
        )

    with MemoryOpsClient(
        "https://memory.example", "bad-token", transport=httpx.MockTransport(denied)
    ) as client:
        with pytest.raises(MemoryOpsError) as failure:
            client.inspect(TENANT, WORKSPACE, MEMORY)

    assert failure.value.status_code == 401
    assert failure.value.code == "unauthenticated"
    assert str(failure.value) == "unauthenticated: authentication required"


def test_sdk_rejects_unsupported_evidence_types_before_http() -> None:
    with pytest.raises(ValidationError):
        EvidenceReference(
            evidence_type="manual_sdk_test",
            reference_id="message-1",
        )
