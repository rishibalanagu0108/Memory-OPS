from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy import Engine, text
from sqlalchemy.exc import DBAPIError

from memory_ops.config import Settings
from memory_ops.persistence import (
    TenantDatabase,
    create_database_engine,
    upgrade_database,
)
from memory_ops.user_memory import (
    CanonicalMemoryVersion,
    DerivedArtifactIdentity,
    GovernanceMetadata,
    MemoryScope,
)


@pytest.fixture(scope="module")
def canonical_store() -> tuple[Engine, UUID, UUID, UUID, UUID]:
    engine = create_database_engine(Settings().database_url)
    upgrade_database(engine)
    tenant_a, tenant_b = uuid4(), uuid4()
    workspace_a, workspace_b = uuid4(), uuid4()
    with engine.begin() as connection:
        connection.execute(
            text("INSERT INTO tenants (id) VALUES (:a), (:b)"),
            {"a": tenant_a, "b": tenant_b},
        )
        connection.execute(
            text(
                "INSERT INTO workspaces (id, tenant_id) "
                "VALUES (:workspace_a, :tenant_a), (:workspace_b, :tenant_b)"
            ),
            {
                "workspace_a": workspace_a,
                "tenant_a": tenant_a,
                "workspace_b": workspace_b,
                "tenant_b": tenant_b,
            },
        )
    yield engine, tenant_a, tenant_b, workspace_a, workspace_b
    engine.dispose()


def test_domain_model_preserves_meaning_governance_and_bitemporal_time() -> None:
    now = datetime.now(UTC)
    scope = MemoryScope(
        tenant_id=uuid4(), workspace_id=uuid4(), subject_id=uuid4()
    )
    version = CanonicalMemoryVersion(
        id=uuid4(),
        memory_id=uuid4(),
        version_number=1,
        scope=scope,
        semantic_type="preference",
        original_statement="I prefer window seats.",
        normalized_subject="user",
        normalized_predicate="prefers_seat",
        normalized_value="window",
        valid_from=now,
        recorded_at=now + timedelta(seconds=1),
        governance=GovernanceMetadata(
            sensitivity="internal",
            lifetime="durable",
            origin="user",
            purpose="travel assistance",
            policy_version="2026-10",
        ),
    )

    assert version.original_statement == "I prefer window seats."
    assert version.normalized_value == "window"
    assert version.valid_from < version.recorded_at

    with pytest.raises(ValidationError, match="valid_to must be later"):
        version.model_copy(update={"valid_to": now - timedelta(seconds=1)}).model_validate(
            version.model_copy(update={"valid_to": now - timedelta(seconds=1)})
        )


def test_logical_memory_points_to_an_immutable_version_with_evidence(
    canonical_store: tuple[Engine, UUID, UUID, UUID, UUID],
) -> None:
    engine, tenant_a, _, workspace_a, _ = canonical_store
    database = TenantDatabase(engine)
    memory_id, version_id, evidence_id, subject_id = uuid4(), uuid4(), uuid4(), uuid4()

    with database.transaction(tenant_a) as connection:
        connection.execute(
            text(
                """
                INSERT INTO user_memories
                    (id, tenant_id, workspace_id, subject_id, semantic_type)
                VALUES (:id, :tenant, :workspace, :subject, 'preference')
                """
            ),
            {
                "id": memory_id,
                "tenant": tenant_a,
                "workspace": workspace_a,
                "subject": subject_id,
            },
        )
        connection.execute(
            text(
                """
                INSERT INTO user_memory_versions
                    (id, tenant_id, memory_id, version_number, original_statement,
                     normalized_subject, normalized_predicate, normalized_value,
                     valid_from, sensitivity, lifetime, origin, purpose, policy_version)
                VALUES
                    (:id, :tenant, :memory, 1, :statement, 'user', 'prefers_seat',
                     CAST(:value AS jsonb), now(), 'internal', 'durable', 'user',
                     'travel assistance', '2026-10')
                """
            ),
            {
                "id": version_id,
                "tenant": tenant_a,
                "memory": memory_id,
                "statement": "I prefer window seats.",
                "value": '"window"',
            },
        )
        connection.execute(
            text(
                "UPDATE user_memories SET current_version_id = :version "
                "WHERE id = :memory"
            ),
            {"version": version_id, "memory": memory_id},
        )
        connection.execute(
            text(
                """
                INSERT INTO user_memory_evidence
                    (id, tenant_id, memory_version_id, evidence_type, reference_id)
                VALUES (:id, :tenant, :version, 'conversation_message', 'message-42')
                """
            ),
            {"id": evidence_id, "tenant": tenant_a, "version": version_id},
        )

    with database.transaction(tenant_a) as connection:
        row = connection.execute(
            text(
                """
                SELECT m.current_version_id, v.original_statement, e.reference_id
                FROM user_memories m
                JOIN user_memory_versions v ON v.id = m.current_version_id
                JOIN user_memory_evidence e ON e.memory_version_id = v.id
                WHERE m.id = :memory
                """
            ),
            {"memory": memory_id},
        ).one()

    assert tuple(row) == (version_id, "I prefer window seats.", "message-42")

    with pytest.raises(DBAPIError, match="permission denied"):
        with database.transaction(tenant_a) as connection:
            connection.execute(
                text(
                    "UPDATE user_memory_versions SET original_statement = 'changed' "
                    "WHERE id = :id"
                ),
                {"id": version_id},
            )

    with pytest.raises(DBAPIError, match="versions are immutable"):
        with engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE user_memory_versions SET original_statement = 'changed' "
                    "WHERE id = :id"
                ),
                {"id": version_id},
            )


def test_rls_and_foreign_keys_prevent_cross_tenant_links(
    canonical_store: tuple[Engine, UUID, UUID, UUID, UUID],
) -> None:
    engine, tenant_a, tenant_b, _, workspace_b = canonical_store
    database = TenantDatabase(engine)

    with pytest.raises(DBAPIError):
        with database.transaction(tenant_a) as connection:
            connection.execute(
                text(
                    """
                    INSERT INTO user_memories
                        (id, tenant_id, workspace_id, subject_id, semantic_type)
                    VALUES (:id, :tenant, :workspace, :subject, 'fact')
                    """
                ),
                {
                    "id": uuid4(),
                    "tenant": tenant_a,
                    "workspace": workspace_b,
                    "subject": uuid4(),
                },
            )

    with database.transaction(tenant_b) as connection:
        assert connection.execute(text("SELECT count(*) FROM user_memories")).scalar_one() == 0


def test_derived_artifacts_require_canonical_and_generation_identity() -> None:
    identity = DerivedArtifactIdentity(
        memory_id=uuid4(),
        canonical_version_id=uuid4(),
        index_generation="generation-7",
        model_version="embedding-v3",
    )
    assert identity.index_generation == "generation-7"

    with pytest.raises(ValidationError):
        DerivedArtifactIdentity.model_validate(
            {
                "memory_id": uuid4(),
                "canonical_version_id": uuid4(),
                "index_generation": "generation-7",
            }
        )
