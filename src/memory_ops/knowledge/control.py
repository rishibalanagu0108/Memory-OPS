"""Trusted synchronization of document ACL and lifecycle source state."""

from dataclasses import dataclass
from typing import Literal
from uuid import UUID, uuid4

from sqlalchemy import text

from memory_ops.knowledge import DocumentUnavailable, KnowledgeScope
from memory_ops.persistence import TenantDatabase


DocumentLifecycle = Literal["active", "revoked", "deleted"]


@dataclass(frozen=True)
class KnowledgeControlReceipt:
    document_id: UUID
    acl_revision_id: UUID
    acl_revision_number: int
    lifecycle: DocumentLifecycle
    changed: bool


class KnowledgeDocumentControlService:
    def __init__(self, database: TenantDatabase) -> None:
        self.database = database

    def synchronize(
        self,
        scope: KnowledgeScope,
        document_id: UUID,
        *,
        principal_ids: tuple[UUID, ...],
        policy_version: str,
        lifecycle: DocumentLifecycle,
    ) -> KnowledgeControlReceipt:
        if not policy_version.strip() or len(policy_version) > 255:
            raise ValueError("policy version must contain 1 to 255 characters")
        principals = tuple(sorted(set(principal_ids), key=str))
        with self.database.transaction(scope.tenant_id) as connection:
            document = connection.execute(
                text(
                    """
                    SELECT d.id, d.lifecycle, d.current_acl_revision_id,
                           a.revision_number, a.policy_version
                    FROM knowledge_documents d
                    JOIN knowledge_document_acl_revisions a
                      ON a.tenant_id = d.tenant_id
                     AND a.document_id = d.id
                     AND a.id = d.current_acl_revision_id
                    WHERE d.id = :document_id
                      AND d.workspace_id = :workspace_id
                    FOR UPDATE OF d
                    """
                ),
                {
                    "document_id": document_id,
                    "workspace_id": scope.workspace_id,
                },
            ).one_or_none()
            if document is None:
                raise DocumentUnavailable("document is unavailable")
            if document.lifecycle == "deleted" and lifecycle != "deleted":
                raise DocumentUnavailable("deleted document cannot be restored")
            current_principals = frozenset(
                connection.execute(
                    text(
                        """
                        SELECT principal_id
                        FROM knowledge_document_acl_grants
                        WHERE document_id = :document_id
                          AND acl_revision_id = :revision_id
                        ORDER BY principal_id
                        """
                    ),
                    {
                        "document_id": document_id,
                        "revision_id": document.current_acl_revision_id,
                    },
                ).scalars()
            )
            acl_changed = (
                frozenset(principals) != current_principals
                or policy_version != document.policy_version
            )
            revision_id = document.current_acl_revision_id
            revision_number = document.revision_number
            if acl_changed:
                revision_id = uuid4()
                revision_number += 1
                connection.execute(
                    text(
                        """
                        INSERT INTO knowledge_document_acl_revisions (
                            id, tenant_id, document_id, revision_number,
                            policy_version
                        ) VALUES (
                            :id, :tenant_id, :document_id, :revision_number,
                            :policy_version
                        )
                        """
                    ),
                    {
                        "id": revision_id,
                        "tenant_id": scope.tenant_id,
                        "document_id": document_id,
                        "revision_number": revision_number,
                        "policy_version": policy_version,
                    },
                )
                if principals:
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
                                "acl_revision_id": revision_id,
                                "principal_id": principal_id,
                            }
                            for principal_id in principals
                        ],
                    )
            lifecycle_changed = lifecycle != document.lifecycle
            if acl_changed or lifecycle_changed:
                connection.execute(
                    text(
                        """
                        UPDATE knowledge_documents
                        SET current_acl_revision_id = :acl_revision_id,
                            lifecycle = :lifecycle
                        WHERE id = :document_id
                        """
                    ),
                    {
                        "acl_revision_id": revision_id,
                        "lifecycle": lifecycle,
                        "document_id": document_id,
                    },
                )
        return KnowledgeControlReceipt(
            document_id,
            revision_id,
            revision_number,
            lifecycle,
            acl_changed or lifecycle_changed,
        )
