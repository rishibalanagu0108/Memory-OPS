"""Execute every M1 holdout case against the real HTTP and PostgreSQL boundaries."""

import json
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text

from memory_ops.api import create_app
from memory_ops.config import Settings
from memory_ops.persistence import TenantDatabase, create_database_engine, upgrade_database
from memory_ops.security import (
    AuthenticatedPrincipal,
    MachinePolicy,
    SecurityBoundary,
    WorkspaceGrant,
)


ROOT = Path(__file__).resolve().parents[2]
CASES = json.loads((ROOT / "evals/m1/holdout.json").read_text())["cases"]


@pytest.fixture(scope="module")
def holdout_api() -> tuple[TestClient, Engine, UUID, UUID, UUID, UUID, UUID]:
    settings = Settings.from_environment()
    engine = create_database_engine(settings.database_url)
    upgrade_database(engine)
    tenant_id, workspace_id = uuid4(), uuid4()
    foreign_tenant_id, foreign_workspace_id = uuid4(), uuid4()
    subject_id = uuid4()
    with engine.begin() as connection:
        connection.execute(
            text("INSERT INTO tenants (id) VALUES (:local), (:foreign)"),
            {"local": tenant_id, "foreign": foreign_tenant_id},
        )
        connection.execute(
            text(
                "INSERT INTO workspaces (id, tenant_id) "
                "VALUES (:local_workspace, :local_tenant), "
                "(:foreign_workspace, :foreign_tenant)"
            ),
            {
                "local_workspace": workspace_id,
                "local_tenant": tenant_id,
                "foreign_workspace": foreign_workspace_id,
                "foreign_tenant": foreign_tenant_id,
            },
        )
    principal = AuthenticatedPrincipal(
        tenant_id=tenant_id,
        principal_id=uuid4(),
        workspace_grants=(
            WorkspaceGrant(
                workspace_id, frozenset({"memory:read", "memory:write"})
            ),
        ),
    )
    security = SecurityBoundary(
        credential_resolver=lambda credential: principal if credential == "holdout" else None,
        policy=MachinePolicy(
            "policy-2026-10", frozenset({"memory:read", "memory:write"})
        ),
    )
    app = create_app(
        settings.model_copy(update={"environment": "test"}),
        security,
        TenantDatabase(engine),
    )
    with TestClient(app, raise_server_exceptions=False) as client:
        yield (
            client,
            engine,
            tenant_id,
            workspace_id,
            foreign_tenant_id,
            foreign_workspace_id,
            subject_id,
        )
    engine.dispose()


def memory_path(tenant_id: UUID, workspace_id: UUID) -> str:
    return f"/v1/tenants/{tenant_id}/workspaces/{workspace_id}/memories"


def headers(key: str) -> dict[str, str]:
    return {"Authorization": "Bearer holdout", "Idempotency-Key": key}


def body(case: dict, subject_id: UUID, statement: str | None = None) -> dict:
    values = case["input"]
    return {
        "subject_id": str(subject_id),
        "semantic_type": values.get("semantic_type", "preference"),
        "statement": statement or values.get("statement", "Holdout scoped memory."),
        "sensitivity": "normal",
        "lifetime": "durable",
        "purpose": "assistant_context",
        "evidence": [
            {"evidence_type": "conversation_message", "reference_id": case["id"]}
        ],
    }


def post_memory(
    client: TestClient,
    engine: Engine,
    tenant_id: UUID,
    workspace_id: UUID,
    payload: dict,
    key: str,
):
    response = client.post(
        memory_path(tenant_id, workspace_id),
        json=payload,
        headers=headers(key),
    )
    if response.status_code < 300:
        with engine.connect() as connection:
            outbox_count = connection.execute(
                text(
                    "SELECT count(*) FROM outbox_events "
                    "WHERE tenant_id = :tenant_id AND resource_id = :memory_id"
                ),
                {
                    "tenant_id": tenant_id,
                    "memory_id": UUID(response.json()["memory_id"]),
                },
            ).scalar_one()
        assert outbox_count == 1, "an acknowledged write lost its outbox event"
    return response


@pytest.mark.parametrize("case", CASES, ids=[case["id"] for case in CASES])
def test_m1_holdout_case(
    holdout_api: tuple[TestClient, Engine, UUID, UUID, UUID, UUID, UUID],
    case: dict,
) -> None:
    (
        client,
        engine,
        tenant_id,
        workspace_id,
        foreign_tenant_id,
        foreign_workspace_id,
        default_subject_id,
    ) = holdout_api
    case_id = case["id"]
    expected = case["expected"]
    collection = memory_path(tenant_id, workspace_id)

    if case_id.startswith("holdout-semantic-"):
        subject_id = uuid4()
        created = post_memory(
            client,
            engine,
            tenant_id,
            workspace_id,
            body(case, subject_id),
            f"{case_id}-{uuid4()}",
        )
        assert (created.status_code < 300) is expected["accepted"]
        inspected = client.get(
            f"{collection}/{created.json()['memory_id']}", headers=headers("unused")
        )
        assert inspected.status_code == 200
        assert inspected.json()["semantic_type"] == expected["semantic_type"]
        return

    if case_id == "holdout-canonical-round-trip":
        subject_id = uuid4()
        created = post_memory(
            client,
            engine,
            tenant_id,
            workspace_id,
            body(case, subject_id),
            f"{case_id}-{uuid4()}",
        )
        memory_id = UUID(created.json()["memory_id"])
        inspected = client.get(
            f"{collection}/{memory_id}", headers=headers("unused")
        )
        with engine.connect() as connection:
            counts = connection.execute(
                text(
                    "SELECT "
                    "(SELECT count(*) FROM user_memory_versions WHERE memory_id = :id), "
                    "(SELECT count(*) FROM outbox_events WHERE resource_id = :id)"
                ),
                {"id": memory_id},
            ).one()
        assert inspected.status_code == 200
        assert inspected.json()["statement"] == case["input"]["statement"]
        assert tuple(counts) == (
            expected["current_version_count"],
            expected["outbox_event_count"],
        )
        return

    if case_id == "holdout-scope-authorized":
        subject_id = uuid4()
        created = post_memory(
            client,
            engine,
            tenant_id,
            workspace_id,
            body(case, subject_id),
            f"{case_id}-{uuid4()}",
        )
        listed = client.get(
            collection,
            params={"subject_id": str(subject_id), "purpose": "assistant_context"},
            headers=headers("unused"),
        )
        assert listed.status_code == 200
        items = listed.json()["items"]
        foreign_resource_count = sum(
            item["memory_id"] != created.json()["memory_id"] for item in items
        )
        assert foreign_resource_count == expected["foreign_resource_count"]
        return

    if case_id == "holdout-scope-cross-tenant":
        denied = client.get(
            memory_path(foreign_tenant_id, foreign_workspace_id),
            headers=headers("unused"),
        )
        safe_error = {
            "code": "permission_denied",
            "message": "permission denied",
        }
        assert denied.status_code == 403
        assert denied.json() == safe_error
        assert (denied.json() != safe_error) is expected["content_disclosed"]
        assert (denied.status_code != 403) is expected["existence_disclosed"]
        return

    if case_id == "holdout-scope-subject-filter":
        target_subject, other_subject = uuid4(), uuid4()
        target = post_memory(
            client,
            engine,
            tenant_id,
            workspace_id,
            body(case, target_subject),
            f"{case_id}-target-{uuid4()}",
        )
        post_memory(
            client,
            engine,
            tenant_id,
            workspace_id,
            body(case, other_subject),
            f"{case_id}-other-{uuid4()}",
        )
        listed = client.get(
            collection,
            params={"subject_id": str(target_subject)},
            headers=headers("unused"),
        )
        items = listed.json()["items"]
        assert target.json()["memory_id"] in {item["memory_id"] for item in items}
        other_subject_count = sum(item["subject_id"] != str(target_subject) for item in items)
        assert other_subject_count == expected["other_subject_count"]
        return

    if case_id == "holdout-atomic-outbox-failure":
        trigger = f"reject_{uuid4().hex}"
        with TenantDatabase(engine).transaction(tenant_id) as connection:
            before = connection.execute(
                text(
                    "SELECT "
                    "(SELECT count(*) FROM user_memories), "
                    "(SELECT count(*) FROM user_memory_versions), "
                    "(SELECT count(*) FROM outbox_events)"
                )
            ).one()
        with engine.begin() as connection:
            connection.execute(
                text(
                    f"CREATE FUNCTION {trigger}() RETURNS trigger LANGUAGE plpgsql AS $$ "
                    f"BEGIN IF NEW.tenant_id = '{tenant_id}'::uuid THEN "
                    "RAISE EXCEPTION 'simulated holdout outbox failure'; "
                    "END IF; RETURN NEW; END; $$"
                )
            )
            connection.execute(
                text(
                    f"CREATE TRIGGER {trigger} BEFORE INSERT ON outbox_events "
                    f"FOR EACH ROW EXECUTE FUNCTION {trigger}()"
                )
            )
        try:
            failed = client.post(
                collection,
                json=body(case, default_subject_id),
                headers=headers(f"{case_id}-{uuid4()}"),
            )
        finally:
            with engine.begin() as connection:
                connection.execute(text(f"DROP TRIGGER {trigger} ON outbox_events"))
                connection.execute(text(f"DROP FUNCTION {trigger}()"))
        with TenantDatabase(engine).transaction(tenant_id) as connection:
            after = connection.execute(
                text(
                    "SELECT "
                    "(SELECT count(*) FROM user_memories), "
                    "(SELECT count(*) FROM user_memory_versions), "
                    "(SELECT count(*) FROM outbox_events)"
                )
            ).one()
        assert failed.status_code == 500
        partial_commit_count = sum(max(current - prior, 0) for prior, current in zip(before, after))
        assert partial_commit_count == expected["partial_commit_count"]
        assert (failed.status_code < 300) is expected["acknowledged"]
        return

    if case_id == "holdout-retry-identical":
        key = f"{case_id}-{uuid4()}"
        payload = body(case, uuid4())
        first = post_memory(client, engine, tenant_id, workspace_id, payload, key)
        replay = post_memory(client, engine, tenant_id, workspace_id, payload, key)
        memory_id = UUID(first.json()["memory_id"])
        with engine.connect() as connection:
            counts = connection.execute(
                text(
                    "SELECT "
                    "(SELECT count(*) FROM user_memories WHERE id = :id), "
                    "(SELECT count(*) FROM user_memory_versions WHERE memory_id = :id)"
                ),
                {"id": memory_id},
            ).one()
        assert replay.status_code == 200
        assert replay.json()["replayed"] is expected["same_receipt"]
        assert replay.json()["memory_id"] == first.json()["memory_id"]
        assert tuple(counts) == (
            expected["logical_memory_count"],
            expected["memory_version_count"],
        )
        return

    if case_id == "holdout-retry-conflict":
        key = f"{case_id}-{uuid4()}"
        initial = body(case, uuid4(), "The user prefers concise answers.")
        first = post_memory(client, engine, tenant_id, workspace_id, initial, key)
        conflict = post_memory(
            client,
            engine,
            tenant_id,
            workspace_id,
            {**initial, "statement": "The user prefers detailed answers."},
            key,
        )
        with engine.connect() as connection:
            version_count = connection.execute(
                text("SELECT count(*) FROM user_memory_versions WHERE memory_id = :id"),
                {"id": UUID(first.json()["memory_id"])},
            ).scalar_one()
        assert conflict.status_code == 409
        assert conflict.json()["code"] == expected["error_code"]
        assert version_count == expected["memory_version_count"]
        return

    if case_id == "holdout-prohibited-secret":
        key = f"{case_id}-{uuid4()}"
        prohibited = post_memory(
            client,
            engine,
            tenant_id,
            workspace_id,
            body(case, uuid4()),
            key,
        )
        with engine.connect() as connection:
            persisted = connection.execute(
                text("SELECT count(*) FROM idempotency_records WHERE idempotency_key = :key"),
                {"key": key},
            ).scalar_one()
        assert prohibited.status_code == 400
        assert prohibited.json()["code"] == expected["error_code"]
        assert (persisted > 0) is expected["persisted"]
        assert (case["input"]["statement"] in prohibited.text) is expected["echoed"]
        return

    raise AssertionError(f"unimplemented holdout case: {case_id}")
