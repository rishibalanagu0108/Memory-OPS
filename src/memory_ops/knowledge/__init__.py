"""Authorized, versioned organizational document ingestion."""

from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from typing import Annotated, Literal
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import text

from memory_ops.knowledge.parsing import parse_document
from memory_ops.persistence import TenantDatabase
from memory_ops.security import ResourceScope, SecurityBoundary, enforce_content_admission
from memory_ops.storage import ObjectStorage


MediaType = Literal[
    "text/plain",
    "text/markdown",
    "application/json",
    "application/pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
]
PublicationStatus = Literal["draft", "published", "archived"]
BoundedText = Annotated[str, Field(min_length=1, max_length=255)]


class DocumentIdentityConflict(ValueError):
    """A stable source identity was reused with different metadata."""


class DocumentUnavailable(ValueError):
    """A document no longer accepts versions."""


class UploadRollbackError(RuntimeError):
    """A failed metadata write could not clean up its uploaded object."""


class KnowledgeModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class KnowledgeScope(KnowledgeModel):
    tenant_id: UUID
    workspace_id: UUID


class DocumentUpload(KnowledgeModel):
    scope: KnowledgeScope
    source_id: BoundedText
    title: BoundedText
    media_type: MediaType
    # ponytail: controlled uploads cap memory use; add streaming if the limit grows.
    content: Annotated[bytes, Field(min_length=1, max_length=10_000_000)]
    publication_status: PublicationStatus = "draft"
    effective_from: datetime = Field(default_factory=lambda: datetime.now(UTC))
    effective_to: datetime | None = None
    source_modified_at: datetime | None = None
    access_policy_version: BoundedText
    access_principal_ids: tuple[UUID, ...] = ()

    @field_validator("source_id", "title", "access_policy_version")
    @classmethod
    def require_nonblank_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("document metadata must contain text")
        return value

    @field_validator("effective_from", "effective_to", "source_modified_at")
    @classmethod
    def require_timezone(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.utcoffset() is None:
            raise ValueError("document timestamps must include a timezone")
        return value

    @model_validator(mode="after")
    def validate_effective_time(self) -> "DocumentUpload":
        if self.media_type in {"text/plain", "text/markdown", "application/json"}:
            try:
                self.content.decode("utf-8")
            except UnicodeDecodeError as error:
                raise ValueError("text documents must be UTF-8") from error
        if self.effective_to is not None and self.effective_to <= self.effective_from:
            raise ValueError("effective_to must be later than effective_from")
        return self


@dataclass(frozen=True)
class DocumentUploadReceipt:
    document_id: UUID
    version_id: UUID
    version_number: int
    operation_id: UUID
    operation_status: Literal["pending"]
    content_hash: str
    object_key: str


class KnowledgeIngestionService:
    def __init__(
        self,
        database: TenantDatabase,
        storage: ObjectStorage,
        security: SecurityBoundary,
    ) -> None:
        self.database = database
        self.storage = storage
        self.security = security

    def upload(
        self,
        request: DocumentUpload,
        credential: str | None,
    ) -> DocumentUploadReceipt:
        scope = request.scope
        principal = self.security.authorize(
            credential,
            ResourceScope(scope.tenant_id, scope.workspace_id),
            "knowledge:upload",
        )
        if request.media_type in {"text/plain", "text/markdown", "application/json"}:
            admission_text = request.content.decode("utf-8")
        else:
            parsed = parse_document(request.content, request.media_type, request.source_id)
            admission_text = "\n".join(chunk.text for chunk in parsed.chunks)
        enforce_content_admission(admission_text)

        document_id = uuid5(
            NAMESPACE_URL,
            f"memory-ops:{scope.tenant_id}:{scope.workspace_id}:{request.source_id}",
        )
        version_id = uuid4()
        acl_revision_id = uuid4()
        operation_id = uuid4()
        content_hash = sha256(request.content).hexdigest()
        object_key = (
            f"tenants/{scope.tenant_id}/workspaces/{scope.workspace_id}/"
            f"documents/{document_id}/versions/{version_id}"
        )
        uploaded = False
        try:
            with self.database.transaction(scope.tenant_id) as connection:
                connection.execute(
                    text(
                        """
                        INSERT INTO knowledge_documents (
                            id, tenant_id, workspace_id, source_id, title,
                            owner_principal_id, lifecycle, ingestion_status,
                            current_acl_revision_id
                        ) VALUES (
                            :id, :tenant_id, :workspace_id, :source_id, :title,
                            :owner_principal_id, 'active', 'pending',
                            :current_acl_revision_id
                        )
                        ON CONFLICT (tenant_id, workspace_id, source_id) DO NOTHING
                        """
                    ),
                    {
                        "id": document_id,
                        "tenant_id": scope.tenant_id,
                        "workspace_id": scope.workspace_id,
                        "source_id": request.source_id,
                        "title": request.title,
                        "owner_principal_id": principal.principal_id,
                        "current_acl_revision_id": acl_revision_id,
                    },
                )
                document = connection.execute(
                    text(
                        """
                        SELECT id, title, lifecycle, current_acl_revision_id
                        FROM knowledge_documents
                        WHERE workspace_id = :workspace_id AND source_id = :source_id
                        FOR UPDATE
                        """
                    ),
                    {
                        "workspace_id": scope.workspace_id,
                        "source_id": request.source_id,
                    },
                ).one()
                if document.id != document_id or document.title != request.title:
                    raise DocumentIdentityConflict(
                        "source identity belongs to a different document"
                    )
                if document.lifecycle != "active":
                    raise DocumentUnavailable("document no longer accepts versions")
                if document.current_acl_revision_id == acl_revision_id:
                    connection.execute(
                        text(
                            """
                            INSERT INTO knowledge_document_acl_revisions (
                                id, tenant_id, document_id, revision_number,
                                policy_version
                            ) VALUES (
                                :id, :tenant_id, :document_id, 1, :policy_version
                            )
                            """
                        ),
                        {
                            "id": acl_revision_id,
                            "tenant_id": scope.tenant_id,
                            "document_id": document_id,
                            "policy_version": request.access_policy_version,
                        },
                    )
                    connection.execute(
                        text(
                            """
                            INSERT INTO knowledge_document_acl_grants (
                                tenant_id, document_id, acl_revision_id,
                                principal_id
                            ) VALUES (
                                :tenant_id, :document_id, :acl_revision_id,
                                :principal_id
                            )
                            """
                        ),
                        [
                            {
                                "tenant_id": scope.tenant_id,
                                "document_id": document_id,
                                "acl_revision_id": acl_revision_id,
                                "principal_id": principal_id,
                            }
                            for principal_id in {
                                principal.principal_id,
                                *request.access_principal_ids,
                            }
                        ],
                    )
                version_number = connection.execute(
                    text(
                        """
                        SELECT COALESCE(max(version_number), 0) + 1
                        FROM knowledge_document_versions
                        WHERE document_id = :document_id
                        """
                    ),
                    {"document_id": document_id},
                ).scalar_one()

                stored = self.storage.put(
                    object_key,
                    request.content,
                    request.media_type,
                    content_hash,
                )
                uploaded = True
                if stored.key != object_key or not stored.encryption:
                    raise RuntimeError("object storage returned invalid metadata")
                connection.execute(
                    text(
                        """
                        INSERT INTO knowledge_document_versions (
                            id, tenant_id, document_id, version_number,
                            storage_bucket, object_key, storage_etag,
                            storage_encryption, media_type, byte_size,
                            content_hash, publication_status, effective_from,
                            effective_to, source_modified_at,
                            access_policy_version
                        ) VALUES (
                            :id, :tenant_id, :document_id, :version_number,
                            :storage_bucket, :object_key, :storage_etag,
                            :storage_encryption, :media_type, :byte_size,
                            :content_hash, :publication_status, :effective_from,
                            :effective_to, :source_modified_at,
                            :access_policy_version
                        )
                        """
                    ),
                    {
                        "id": version_id,
                        "tenant_id": scope.tenant_id,
                        "document_id": document_id,
                        "version_number": version_number,
                        "storage_bucket": stored.bucket,
                        "object_key": stored.key,
                        "storage_etag": stored.etag,
                        "storage_encryption": stored.encryption,
                        "media_type": request.media_type,
                        "byte_size": len(request.content),
                        "content_hash": content_hash,
                        "publication_status": request.publication_status,
                        "effective_from": request.effective_from,
                        "effective_to": request.effective_to,
                        "source_modified_at": request.source_modified_at,
                        "access_policy_version": request.access_policy_version,
                    },
                )
                connection.execute(
                    text(
                        """
                        UPDATE knowledge_documents
                        SET current_version_id = :version_id,
                            ingestion_status = 'pending'
                        WHERE id = :document_id
                        """
                    ),
                    {"document_id": document_id, "version_id": version_id},
                )
                connection.execute(
                    text(
                        """
                        INSERT INTO outbox_events (
                            id, tenant_id, event_type, resource_type,
                            resource_id, resource_version
                        ) VALUES (
                            :id, :tenant_id, 'knowledge.document.uploaded',
                            'knowledge_document', :document_id, :version_id
                        )
                        """
                    ),
                    {
                        "id": operation_id,
                        "tenant_id": scope.tenant_id,
                        "document_id": document_id,
                        "version_id": version_id,
                    },
                )
        except Exception:
            if uploaded:
                try:
                    self.storage.delete(object_key)
                except Exception as cleanup_error:
                    raise UploadRollbackError(
                        "metadata write failed and object cleanup failed"
                    ) from cleanup_error
            raise

        return DocumentUploadReceipt(
            document_id=document_id,
            version_id=version_id,
            version_number=version_number,
            operation_id=operation_id,
            operation_status="pending",
            content_hash=content_hash,
            object_key=object_key,
        )
