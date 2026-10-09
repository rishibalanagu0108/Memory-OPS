from hashlib import sha256
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.exc import DBAPIError

from memory_ops.config import Settings
from memory_ops.knowledge import (
    DocumentUpload,
    KnowledgeIngestionService,
    KnowledgeScope,
)
from memory_ops.persistence import TenantDatabase, create_database_engine, upgrade_database
from memory_ops.security import (
    AuthenticatedPrincipal,
    MachinePolicy,
    ProhibitedContent,
    SecurityBoundary,
    Unauthenticated,
    WorkspaceGrant,
)
from memory_ops.storage import S3ObjectStorage, StorageEncryptionError


class FakeS3Client:
    def __init__(self, confirm_encryption: bool = True) -> None:
        self.confirm_encryption = confirm_encryption
        self.puts: list[dict] = []
        self.deletes: list[dict] = []

    def put_object(self, **request: object) -> dict[str, str]:
        self.puts.append(request)
        response = {"ETag": f'"etag-{len(self.puts)}"'}
        if self.confirm_encryption:
            response["ServerSideEncryption"] = "AES256"
        return response

    def delete_object(self, **request: object) -> None:
        self.deletes.append(request)


@pytest.fixture(scope="module")
def knowledge_context() -> tuple[
    Engine,
    TenantDatabase,
    KnowledgeScope,
    UUID,
    UUID,
]:
    settings = Settings.from_environment()
    engine = create_database_engine(settings.database_url)
    upgrade_database(engine)
    tenant_id, other_tenant_id = uuid4(), uuid4()
    workspace_id = uuid4()
    with engine.begin() as connection:
        connection.execute(
            text("INSERT INTO tenants (id) VALUES (:tenant), (:other)"),
            {"tenant": tenant_id, "other": other_tenant_id},
        )
        connection.execute(
            text("INSERT INTO workspaces (id, tenant_id) VALUES (:id, :tenant)"),
            {"id": workspace_id, "tenant": tenant_id},
        )
    yield (
        engine,
        TenantDatabase(engine),
        KnowledgeScope(tenant_id=tenant_id, workspace_id=workspace_id),
        uuid4(),
        other_tenant_id,
    )
    engine.dispose()


def service_for(
    database: TenantDatabase,
    scope: KnowledgeScope,
    principal_id: UUID,
    client: FakeS3Client,
) -> KnowledgeIngestionService:
    principal = AuthenticatedPrincipal(
        tenant_id=scope.tenant_id,
        principal_id=principal_id,
        workspace_grants=(
            WorkspaceGrant(
                workspace_id=scope.workspace_id,
                actions=frozenset({"knowledge:upload"}),
            ),
        ),
    )
    security = SecurityBoundary(
        credential_resolver=lambda credential: principal if credential == "valid" else None,
        policy=MachinePolicy(
            version="knowledge-policy-1",
            allowed_actions=frozenset({"knowledge:upload"}),
        ),
    )
    return KnowledgeIngestionService(
        database,
        S3ObjectStorage(client, "knowledge-private"),
        security,
    )


def upload_for(
    scope: KnowledgeScope,
    content: bytes,
    *,
    source_id: str = "operations-handbook",
) -> DocumentUpload:
    return DocumentUpload(
        scope=scope,
        source_id=source_id,
        title="Operations handbook",
        media_type="text/markdown",
        content=content,
        publication_status="published",
        access_policy_version="knowledge-policy-1",
    )


def test_authorized_upload_preserves_stable_identity_and_immutable_versions(
    knowledge_context: tuple[Engine, TenantDatabase, KnowledgeScope, UUID, UUID],
) -> None:
    engine, database, scope, principal_id, _ = knowledge_context
    client = FakeS3Client()
    service = service_for(database, scope, principal_id, client)

    first_content = b"# Retention\nKeep incident records for 90 days."
    second_content = b"# Retention\nKeep incident records for 365 days."
    first = service.upload(upload_for(scope, first_content), "valid")
    second = service.upload(upload_for(scope, second_content), "valid")

    assert first.document_id == second.document_id
    assert (first.version_number, second.version_number) == (1, 2)
    assert first.version_id != second.version_id
    assert first.object_key != second.object_key
    assert first.content_hash == sha256(first_content).hexdigest()
    assert second.operation_status == "pending"
    assert all(request["ServerSideEncryption"] == "AES256" for request in client.puts)
    assert all(request["Bucket"] == "knowledge-private" for request in client.puts)

    with database.transaction(scope.tenant_id) as connection:
        document = connection.execute(
            text(
                """
                SELECT source_id, title, owner_principal_id, lifecycle,
                       ingestion_status, current_version_id
                FROM knowledge_documents WHERE id = :id
                """
            ),
            {"id": first.document_id},
        ).one()
        versions = connection.execute(
            text(
                """
                SELECT id, version_number, content_hash, storage_encryption,
                       publication_status, access_policy_version
                FROM knowledge_document_versions
                WHERE document_id = :id ORDER BY version_number
                """
            ),
            {"id": first.document_id},
        ).all()
        outbox_count = connection.execute(
            text(
                "SELECT count(*) FROM outbox_events "
                "WHERE resource_id = :id AND event_type = 'knowledge.document.uploaded'"
            ),
            {"id": first.document_id},
        ).scalar_one()

    assert tuple(document) == (
        "operations-handbook",
        "Operations handbook",
        principal_id,
        "active",
        "pending",
        second.version_id,
    )
    assert [(row.id, row.version_number) for row in versions] == [
        (first.version_id, 1),
        (second.version_id, 2),
    ]
    assert all(row.storage_encryption == "AES256" for row in versions)
    assert all(row.publication_status == "published" for row in versions)
    assert all(row.access_policy_version == "knowledge-policy-1" for row in versions)
    assert outbox_count == 2

    with pytest.raises(DBAPIError, match="knowledge document versions are immutable"):
        with engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE knowledge_document_versions "
                    "SET publication_status = 'archived' WHERE id = :id"
                ),
                {"id": first.version_id},
            )


def test_authorization_and_secret_admission_run_before_object_storage(
    knowledge_context: tuple[Engine, TenantDatabase, KnowledgeScope, UUID, UUID],
) -> None:
    _, database, scope, principal_id, _ = knowledge_context
    client = FakeS3Client()
    service = service_for(database, scope, principal_id, client)

    with pytest.raises(Unauthenticated):
        service.upload(upload_for(scope, b"ordinary content", source_id="denied"), "missing")
    with pytest.raises(ProhibitedContent):
        service.upload(
            upload_for(
                scope,
                b"api_key=sk-test-only-prohibited-sentinel",
                source_id="secret",
            ),
            "valid",
        )

    assert client.puts == []
    with database.transaction(scope.tenant_id) as connection:
        assert connection.execute(
            text(
                "SELECT count(*) FROM knowledge_documents "
                "WHERE source_id IN ('denied', 'secret')"
            )
        ).scalar_one() == 0


def test_unconfirmed_encryption_rolls_back_metadata_and_tenant_rls_hides_rows(
    knowledge_context: tuple[Engine, TenantDatabase, KnowledgeScope, UUID, UUID],
) -> None:
    _, database, scope, principal_id, other_tenant_id = knowledge_context
    client = FakeS3Client(confirm_encryption=False)
    service = service_for(database, scope, principal_id, client)

    with pytest.raises(StorageEncryptionError, match="did not confirm encryption"):
        service.upload(
            upload_for(scope, b"content without encryption confirmation", source_id="unsafe"),
            "valid",
        )

    assert len(client.puts) == len(client.deletes) == 1
    with database.transaction(scope.tenant_id) as connection:
        assert connection.execute(
            text("SELECT count(*) FROM knowledge_documents WHERE source_id = 'unsafe'")
        ).scalar_one() == 0
        visible = connection.execute(
            text("SELECT count(*) FROM knowledge_documents")
        ).scalar_one()
    with database.transaction(other_tenant_id) as connection:
        hidden = connection.execute(
            text("SELECT count(*) FROM knowledge_documents")
        ).scalar_one()
    assert visible >= 1
    assert hidden == 0
