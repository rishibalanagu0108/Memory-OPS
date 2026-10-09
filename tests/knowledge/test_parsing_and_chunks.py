from io import BytesIO
from uuid import UUID, uuid4
from zipfile import ZIP_DEFLATED, ZipFile

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.exc import DBAPIError

from memory_ops.config import Settings
from memory_ops.knowledge import DocumentUpload, KnowledgeIngestionService, KnowledgeScope
from memory_ops.knowledge import parsing
from memory_ops.knowledge.parsing import PARSER_NAME, PARSER_VERSION, parse_document
from memory_ops.persistence import TenantDatabase, create_database_engine, upgrade_database
from memory_ops.security import (
    AuthenticatedPrincipal,
    MachinePolicy,
    SecurityBoundary,
    WorkspaceGrant,
)
from memory_ops.storage import StoredObject
from memory_ops.workers import DocumentContentMismatch, KnowledgeParsingWorker


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


@pytest.fixture(scope="module")
def knowledge_context() -> tuple[
    Engine,
    TenantDatabase,
    KnowledgeScope,
    UUID,
    MemoryStorage,
    KnowledgeIngestionService,
]:
    settings = Settings.from_environment()
    engine = create_database_engine(settings.database_url)
    upgrade_database(engine)
    tenant_id, other_tenant_id, workspace_id, principal_id = (
        uuid4(),
        uuid4(),
        uuid4(),
        uuid4(),
    )
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
        principal_id=principal_id,
        workspace_grants=(
            WorkspaceGrant(
                workspace_id=workspace_id,
                actions=frozenset({"knowledge:upload"}),
            ),
        ),
    )
    security = SecurityBoundary(
        credential_resolver=(
            lambda credential: principal if credential == "valid" else None
        ),
        policy=MachinePolicy(
            version="knowledge-policy-1",
            allowed_actions=frozenset({"knowledge:upload"}),
        ),
    )
    storage = MemoryStorage()
    yield (
        engine,
        database,
        scope,
        other_tenant_id,
        storage,
        KnowledgeIngestionService(database, storage, security),
    )
    engine.dispose()


def upload(
    scope: KnowledgeScope,
    source_id: str,
    content: bytes,
    media_type: str = "text/markdown",
) -> DocumentUpload:
    return DocumentUpload(
        scope=scope,
        source_id=source_id,
        title=f"Document {source_id}",
        media_type=media_type,
        content=content,
        publication_status="published",
        access_policy_version="knowledge-policy-1",
    )


def docx_bytes() -> bytes:
    document = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:body>
    <w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr><w:r><w:t>Retention</w:t></w:r></w:p>
    <w:p><w:r><w:t>Keep records for 365 days.</w:t></w:r></w:p>
    <w:p><w:r><w:t>Archive approved incidents.</w:t></w:r></w:p>
  </w:body>
</w:document>"""
    buffer = BytesIO()
    with ZipFile(buffer, "w", ZIP_DEFLATED) as archive:
        archive.writestr("word/document.xml", document)
    return buffer.getvalue()


def test_parser_preserves_markdown_structure_json_pointers_and_line_ranges() -> None:
    markdown = b"# Retention\n\nKeep records for 365 days.\n\n## Escalation\nPage the lead.\nImmediately."
    parsed = parse_document(markdown, "text/markdown", "policies/handbook.md")

    assert [
        (chunk.text, chunk.locator.start_line, chunk.locator.end_line)
        for chunk in parsed.chunks
    ] == [
        ("Keep records for 365 days.", 3, 3),
        ("Page the lead.\nImmediately.", 6, 7),
    ]
    assert parsed.chunks[0].locator.path == "policies/handbook.md#retention"
    assert parsed.chunks[1].locator.structure_path == ("Retention", "Escalation")

    structured = parse_document(
        b'{\n  "policy": {\n    "days": 365,\n    "active": true\n  }\n}',
        "application/json",
        "policies/retention.json",
    )
    assert [chunk.locator.path for chunk in structured.chunks] == [
        "policies/retention.json#/policy/days",
        "policies/retention.json#/policy/active",
    ]
    assert [
        (chunk.locator.start_line, chunk.locator.end_line)
        for chunk in structured.chunks
    ] == [
        (3, 3),
        (4, 4),
    ]


def test_parser_preserves_pdf_pages_and_docx_paragraphs(monkeypatch: pytest.MonkeyPatch) -> None:
    class Page:
        def __init__(self, content: str) -> None:
            self.content = content

        def extract_text(self) -> str:
            return self.content

    class Reader:
        pages = [Page("Approval required.\nAbove 500 USD."), Page("Final note.")]

    monkeypatch.setattr(parsing, "PdfReader", lambda _: Reader())
    pdf = parse_document(b"synthetic-pdf", "application/pdf", "finance-policy.pdf")
    assert [chunk.locator.kind for chunk in pdf.chunks] == [
        "pdf_page_lines",
        "pdf_page_lines",
    ]
    assert [chunk.locator.path for chunk in pdf.chunks] == [
        "finance-policy.pdf#page=1",
        "finance-policy.pdf#page=2",
    ]
    assert (pdf.chunks[0].locator.start_line, pdf.chunks[0].locator.end_line) == (1, 2)

    docx = parse_document(
        docx_bytes(),
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "operations-handbook.docx",
    )
    assert [chunk.locator.path for chunk in docx.chunks] == [
        "operations-handbook.docx#paragraph=2",
        "operations-handbook.docx#paragraph=3",
    ]
    assert all(chunk.locator.kind == "docx_paragraphs" for chunk in docx.chunks)
    assert all(chunk.locator.structure_path == ("Retention",) for chunk in docx.chunks)


def test_worker_persists_immutable_versioned_chunks_idempotently(
    knowledge_context: tuple[
        Engine,
        TenantDatabase,
        KnowledgeScope,
        UUID,
        MemoryStorage,
        KnowledgeIngestionService,
    ],
) -> None:
    engine, database, scope, other_tenant_id, storage, service = knowledge_context
    first = service.upload(
        upload(
            scope,
            "policies/handbook.md",
            b"# Retention\n\nKeep records for 90 days.\n\n## Escalation\nPage the lead.",
        ),
        "valid",
    )
    second = service.upload(
        upload(
            scope,
            "policies/handbook.md",
            b"# Retention\n\nKeep records for 365 days.",
        ),
        "valid",
    )
    worker = KnowledgeParsingWorker(database, storage.read, "knowledge-v1")
    events = worker.outbox.claim(scope.tenant_id)
    selected = [
        event
        for event in events
        if event.id in {first.operation_id, second.operation_id}
    ]
    assert len(selected) == 2
    artifacts = [worker.process(scope.tenant_id, event) for event in selected]
    worker.process(scope.tenant_id, selected[0])

    assert all(artifact is not None for artifact in artifacts)
    assert sorted(artifact.chunk_count for artifact in artifacts if artifact) == [1, 2]
    assert all(artifact.projection_model == PARSER_NAME for artifact in artifacts if artifact)
    assert all(
        artifact.projection_model_version == PARSER_VERSION
        for artifact in artifacts
        if artifact
    )

    with database.transaction(scope.tenant_id) as connection:
        rows = connection.execute(
            text(
                """
                SELECT id, document_version_id, ordinal, content, locator_kind,
                       locator_path, start_line, end_line, structure_path,
                       index_generation, projection_model,
                       projection_model_version
                FROM knowledge_document_chunks
                WHERE document_id = :document_id
                ORDER BY document_version_id, ordinal
                """
            ),
            {"document_id": first.document_id},
        ).all()
        status = connection.execute(
            text("SELECT ingestion_status FROM knowledge_documents WHERE id = :id"),
            {"id": first.document_id},
        ).scalar_one()
    assert len(rows) == 3
    assert {row.document_version_id for row in rows} == {first.version_id, second.version_id}
    assert all(row.locator_kind == "markdown_lines" for row in rows)
    assert all(row.index_generation == "knowledge-v1" for row in rows)
    assert all(row.projection_model == PARSER_NAME for row in rows)
    assert all(row.projection_model_version == PARSER_VERSION for row in rows)
    assert status == "ready"

    with pytest.raises(DBAPIError, match="knowledge document chunks are immutable"):
        with engine.begin() as connection:
            connection.execute(
                text("UPDATE knowledge_document_chunks SET content = 'changed' WHERE id = :id"),
                {"id": rows[0].id},
            )
    with database.transaction(other_tenant_id) as connection:
        assert connection.execute(
            text("SELECT count(*) FROM knowledge_document_chunks")
        ).scalar_one() == 0


def test_worker_projects_docx_with_exact_paragraph_locators(
    knowledge_context: tuple[
        Engine,
        TenantDatabase,
        KnowledgeScope,
        UUID,
        MemoryStorage,
        KnowledgeIngestionService,
    ],
) -> None:
    _, database, scope, _, storage, service = knowledge_context
    receipt = service.upload(
        upload(
            scope,
            "operations-handbook.docx",
            docx_bytes(),
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        ),
        "valid",
    )
    worker = KnowledgeParsingWorker(database, storage.read, "knowledge-v1")
    event = next(
        event
        for event in worker.outbox.claim(scope.tenant_id)
        if event.id == receipt.operation_id
    )
    artifact = worker.process(scope.tenant_id, event)

    assert artifact is not None
    assert artifact.chunk_count == 2
    with database.transaction(scope.tenant_id) as connection:
        rows = connection.execute(
            text(
                """
                SELECT locator_kind, locator_path, start_line, end_line,
                       structure_path
                FROM knowledge_document_chunks
                WHERE document_version_id = :version_id
                ORDER BY ordinal
                """
            ),
            {"version_id": receipt.version_id},
        ).all()
    assert [row.locator_path for row in rows] == [
        "operations-handbook.docx#paragraph=2",
        "operations-handbook.docx#paragraph=3",
    ]
    assert all(row.locator_kind == "docx_paragraphs" for row in rows)
    assert all(row.structure_path == ["Retention"] for row in rows)


def test_worker_rejects_changed_object_bytes(
    knowledge_context: tuple[
        Engine,
        TenantDatabase,
        KnowledgeScope,
        UUID,
        MemoryStorage,
        KnowledgeIngestionService,
    ],
) -> None:
    _, database, scope, _, storage, service = knowledge_context
    receipt = service.upload(
        upload(scope, "tampered.md", b"# Safe\n\nOriginal content."),
        "valid",
    )
    event = next(
        event
        for event in KnowledgeParsingWorker(database, storage.read, "knowledge-v1")
        .outbox.claim(scope.tenant_id)
        if event.id == receipt.operation_id
    )
    storage.objects[(storage.bucket, receipt.object_key)] = b"changed after upload"
    worker = KnowledgeParsingWorker(database, storage.read, "knowledge-v1")

    with pytest.raises(DocumentContentMismatch, match="content hash changed"):
        worker.process(scope.tenant_id, event)
    with database.transaction(scope.tenant_id) as connection:
        assert connection.execute(
            text(
                "SELECT count(*) FROM knowledge_document_chunks "
                "WHERE document_version_id = :version_id"
            ),
            {"version_id": receipt.version_id},
        ).scalar_one() == 0
