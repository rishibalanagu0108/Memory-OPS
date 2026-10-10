from dataclasses import dataclass
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.exc import DBAPIError

from memory_ops.config import Settings
from memory_ops.knowledge import (
    DocumentUnavailable,
    DocumentUpload,
    KnowledgeIngestionService,
    KnowledgeScope,
)
from memory_ops.knowledge.search import KnowledgeSearchService
from memory_ops.persistence import TenantDatabase, create_database_engine, upgrade_database
from memory_ops.security import (
    AuthenticatedPrincipal,
    MachinePolicy,
    SecurityBoundary,
    WorkspaceGrant,
)
from memory_ops.storage import StoredObject
from memory_ops.workers import KnowledgeParsingWorker, KnowledgeSourceSyncWorker


class MemoryStorage:
    bucket = "knowledge-private"

    def __init__(self) -> None:
        self.objects: dict[tuple[str, str], bytes] = {}

    def put(
        self,
        key: str,
        content: bytes,
        media_type: str,
        content_hash: str,
    ) -> StoredObject:
        self.objects[(self.bucket, key)] = content
        return StoredObject(self.bucket, key, content_hash, "AES256")

    def delete(self, key: str) -> None:
        self.objects.pop((self.bucket, key), None)

    def read(self, bucket: str, key: str) -> bytes:
        return self.objects[(bucket, key)]


@dataclass(frozen=True)
class Context:
    engine: Engine
    database: TenantDatabase
    scope: KnowledgeScope
    other_tenant_id: UUID
    owner_id: UUID
    reader_id: UUID
    denied_id: UUID
    storage: MemoryStorage
    ingestion: KnowledgeIngestionService


@pytest.fixture(scope="module")
def context() -> Context:
    settings = Settings.from_environment()
    engine = create_database_engine(settings.database_url)
    upgrade_database(engine)
    tenant_id, other_tenant_id, workspace_id = uuid4(), uuid4(), uuid4()
    owner_id, reader_id, denied_id = uuid4(), uuid4(), uuid4()
    with engine.begin() as connection:
        connection.execute(
            text("INSERT INTO tenants (id) VALUES (:tenant), (:other)"),
            {"tenant": tenant_id, "other": other_tenant_id},
        )
        connection.execute(
            text("INSERT INTO workspaces (id, tenant_id) VALUES (:id, :tenant)"),
            {"id": workspace_id, "tenant": tenant_id},
        )
    database = TenantDatabase(engine)
    scope = KnowledgeScope(tenant_id=tenant_id, workspace_id=workspace_id)
    principal = AuthenticatedPrincipal(
        tenant_id=tenant_id,
        principal_id=owner_id,
        workspace_grants=(
            WorkspaceGrant(workspace_id, frozenset({"knowledge:upload"})),
        ),
    )
    security = SecurityBoundary(
        credential_resolver=lambda credential: principal if credential == "valid" else None,
        policy=MachinePolicy("knowledge-policy-1", frozenset({"knowledge:upload"})),
    )
    storage = MemoryStorage()
    yield Context(
        engine,
        database,
        scope,
        other_tenant_id,
        owner_id,
        reader_id,
        denied_id,
        storage,
        KnowledgeIngestionService(database, storage, security),
    )
    engine.dispose()


def _upload_and_parse(
    context: Context,
    source_id: str,
    content: str,
    *,
    readers: tuple[UUID, ...] = (),
):
    receipt = context.ingestion.upload(
        DocumentUpload(
            scope=context.scope,
            source_id=source_id,
            title=f"Document {source_id}",
            media_type="text/markdown",
            content=content.encode(),
            publication_status="published",
            access_policy_version="knowledge-policy-1",
            access_principal_ids=readers,
        ),
        "valid",
    )
    parser = KnowledgeParsingWorker(context.database, context.storage.read, "knowledge-v1")
    event = next(
        event
        for event in parser.outbox.claim(context.scope.tenant_id)
        if event.id == receipt.operation_id
    )
    parser.process(context.scope.tenant_id, event)
    return receipt


def _search(context: Context, principal_id: UUID, query: str):
    return KnowledgeSearchService(context.database).search(
        context.scope,
        principal_id,
        query,
    )


def test_acl_revision_immediately_removes_old_access_and_is_immutable(
    context: Context,
) -> None:
    receipt = _upload_and_parse(
        context,
        "rotation.md",
        "Credential rotation occurs every 30 days.",
        readers=(context.reader_id,),
    )
    assert len(_search(context, context.owner_id, "credential rotation").passages) == 1
    assert len(_search(context, context.reader_id, "credential rotation").passages) == 1
    assert _search(context, context.denied_id, "credential rotation").passages == ()

    changed = KnowledgeSourceSyncWorker(context.database).process(
        context.scope,
        receipt.document_id,
        principal_ids=(context.reader_id,),
        policy_version="knowledge-policy-2",
        lifecycle="active",
    )

    assert changed.changed is True
    assert changed.acl_revision_number == 2
    assert _search(context, context.owner_id, "credential rotation").passages == ()
    result = _search(context, context.reader_id, "credential rotation")
    assert len(result.passages) == 1
    assert result.passages[0].citation.access_policy_version == "knowledge-policy-2"
    with pytest.raises(DBAPIError, match="knowledge ACL revisions are immutable"):
        with context.engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE knowledge_document_acl_revisions "
                    "SET policy_version = 'changed' WHERE id = :id"
                ),
                {"id": changed.acl_revision_id},
            )


def test_revoked_and_deleted_documents_never_return(
    context: Context,
) -> None:
    receipt = _upload_and_parse(
        context,
        "emergency.md",
        "Use the approved emergency procedure.",
        readers=(context.reader_id,),
    )
    worker = KnowledgeSourceSyncWorker(context.database)
    worker.process(
        context.scope,
        receipt.document_id,
        principal_ids=(context.reader_id,),
        policy_version="knowledge-policy-1",
        lifecycle="revoked",
    )
    revoked = _search(context, context.reader_id, "emergency procedure")
    assert revoked.passages == ()
    assert revoked.warnings == (
        "stale_source_filtered",
        "insufficient_authorized_evidence",
    )

    worker.process(
        context.scope,
        receipt.document_id,
        principal_ids=(context.reader_id,),
        policy_version="knowledge-policy-1",
        lifecycle="active",
    )
    assert len(_search(context, context.reader_id, "emergency procedure").passages) == 1
    worker.process(
        context.scope,
        receipt.document_id,
        principal_ids=(context.reader_id,),
        policy_version="knowledge-policy-1",
        lifecycle="deleted",
    )
    assert _search(context, context.reader_id, "emergency procedure").passages == ()
    with pytest.raises(DocumentUnavailable, match="cannot be restored"):
        worker.process(
            context.scope,
            receipt.document_id,
            principal_ids=(context.reader_id,),
            policy_version="knowledge-policy-1",
            lifecycle="active",
        )


def test_noncurrent_projection_is_filtered_and_reported(context: Context) -> None:
    first = _upload_and_parse(
        context,
        "retention.md",
        "Incident retention is 90 days.",
        readers=(context.reader_id,),
    )
    second = _upload_and_parse(
        context,
        "retention.md",
        "Incident retention is 365 days.",
        readers=(context.reader_id,),
    )
    result = _search(context, context.reader_id, "incident retention")

    assert first.document_id == second.document_id
    assert [passage.content for passage in result.passages] == [
        "Incident retention is 365 days."
    ]
    assert result.warnings == ("stale_source_filtered",)


def test_conflicting_current_sources_are_returned_and_labelled(context: Context) -> None:
    _upload_and_parse(
        context,
        "finance.md",
        "Purchases above 500 USD require finance approval.",
        readers=(context.reader_id,),
    )
    _upload_and_parse(
        context,
        "procurement.md",
        "Purchases above 1000 USD require finance approval.",
        readers=(context.reader_id,),
    )
    result = _search(context, context.reader_id, "purchases finance approval")

    assert {passage.content for passage in result.passages} == {
        "Purchases above 500 USD require finance approval.",
        "Purchases above 1000 USD require finance approval.",
    }
    assert result.warnings == ("unresolved_conflict",)


def test_acl_rows_remain_tenant_isolated(context: Context) -> None:
    with context.database.transaction(context.other_tenant_id) as connection:
        assert connection.execute(
            text("SELECT count(*) FROM knowledge_document_acl_revisions")
        ).scalar_one() == 0
        assert connection.execute(
            text("SELECT count(*) FROM knowledge_document_acl_grants")
        ).scalar_one() == 0
