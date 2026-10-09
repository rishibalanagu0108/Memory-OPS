"""Add stable knowledge documents and immutable encrypted versions."""

import sqlalchemy as sa
from alembic import op

revision = "0012_knowledge_documents"
down_revision = "0011_agent_lesson_versions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "knowledge_documents",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("source_id", sa.String(255), nullable=False),
        sa.Column("title", sa.String(255), nullable=False),
        sa.Column("owner_principal_id", sa.Uuid(), nullable=False),
        sa.Column("lifecycle", sa.String(20), nullable=False),
        sa.Column("ingestion_status", sa.String(20), nullable=False),
        sa.Column("current_version_id", sa.Uuid()),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "workspace_id"],
            ["workspaces.tenant_id", "workspaces.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "tenant_id", "id", name="uq_knowledge_documents_tenant_id_id"
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "workspace_id",
            "source_id",
            name="uq_knowledge_documents_source",
        ),
        sa.CheckConstraint(
            "length(btrim(source_id)) > 0 AND length(btrim(title)) > 0",
            name="ck_knowledge_documents_identity",
        ),
        sa.CheckConstraint(
            "lifecycle IN ('active', 'revoked', 'deleted')",
            name="ck_knowledge_documents_lifecycle",
        ),
        sa.CheckConstraint(
            "ingestion_status IN ('pending', 'processing', 'ready', 'failed')",
            name="ck_knowledge_documents_ingestion_status",
        ),
    )
    op.create_index(
        "ix_knowledge_documents_scope",
        "knowledge_documents",
        ["tenant_id", "workspace_id", "lifecycle", "ingestion_status"],
    )

    op.create_table(
        "knowledge_document_versions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("document_id", sa.Uuid(), nullable=False),
        sa.Column("version_number", sa.Integer(), nullable=False),
        sa.Column("storage_bucket", sa.String(255), nullable=False),
        sa.Column("object_key", sa.String(1024), nullable=False),
        sa.Column("storage_etag", sa.String(255)),
        sa.Column("storage_encryption", sa.String(50), nullable=False),
        sa.Column("media_type", sa.String(100), nullable=False),
        sa.Column("byte_size", sa.BigInteger(), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("publication_status", sa.String(20), nullable=False),
        sa.Column("effective_from", sa.DateTime(timezone=True), nullable=False),
        sa.Column("effective_to", sa.DateTime(timezone=True)),
        sa.Column("source_modified_at", sa.DateTime(timezone=True)),
        sa.Column("access_policy_version", sa.String(255), nullable=False),
        sa.Column(
            "recorded_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "document_id"],
            ["knowledge_documents.tenant_id", "knowledge_documents.id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "tenant_id",
            "document_id",
            "version_number",
            name="uq_knowledge_document_versions_number",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "document_id",
            "id",
            name="uq_knowledge_document_versions_pointer",
        ),
        sa.UniqueConstraint(
            "tenant_id", "object_key", name="uq_knowledge_document_versions_object"
        ),
        sa.CheckConstraint(
            "version_number >= 1", name="ck_knowledge_document_versions_number"
        ),
        sa.CheckConstraint(
            "byte_size > 0", name="ck_knowledge_document_versions_size"
        ),
        sa.CheckConstraint(
            "content_hash ~ '^[0-9a-f]{64}$'",
            name="ck_knowledge_document_versions_hash",
        ),
        sa.CheckConstraint(
            "storage_encryption = 'AES256'",
            name="ck_knowledge_document_versions_encryption",
        ),
        sa.CheckConstraint(
            "media_type IN ('text/plain', 'text/markdown', 'application/json')",
            name="ck_knowledge_document_versions_media_type",
        ),
        sa.CheckConstraint(
            "publication_status IN ('draft', 'published', 'archived')",
            name="ck_knowledge_document_versions_publication",
        ),
        sa.CheckConstraint(
            "effective_to IS NULL OR effective_to > effective_from",
            name="ck_knowledge_document_versions_effective_time",
        ),
    )
    op.create_index(
        "ix_knowledge_document_versions_document",
        "knowledge_document_versions",
        ["tenant_id", "document_id", "version_number"],
    )
    op.create_foreign_key(
        "fk_knowledge_documents_current_version",
        "knowledge_documents",
        "knowledge_document_versions",
        ["tenant_id", "id", "current_version_id"],
        ["tenant_id", "document_id", "id"],
        deferrable=True,
        initially="DEFERRED",
    )

    for table in ("knowledge_documents", "knowledge_document_versions"):
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"""
            CREATE POLICY {table}_tenant_isolation ON {table}
            USING (
                tenant_id = NULLIF(
                    current_setting('memory_ops.tenant_id', true), ''
                )::uuid
            )
            WITH CHECK (
                tenant_id = NULLIF(
                    current_setting('memory_ops.tenant_id', true), ''
                )::uuid
            )
            """
        )

    op.execute(
        """
        CREATE FUNCTION reject_knowledge_document_version_mutation()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'knowledge document versions are immutable';
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER knowledge_document_versions_immutable
        BEFORE UPDATE OR DELETE ON knowledge_document_versions
        FOR EACH ROW EXECUTE FUNCTION reject_knowledge_document_version_mutation()
        """
    )

    op.execute(
        "REVOKE ALL ON knowledge_documents, knowledge_document_versions FROM PUBLIC"
    )
    op.execute(
        "GRANT SELECT, INSERT ON knowledge_documents, knowledge_document_versions "
        "TO memory_ops_runtime"
    )
    op.execute(
        "GRANT UPDATE (current_version_id, lifecycle, ingestion_status) "
        "ON knowledge_documents TO memory_ops_runtime"
    )


def downgrade() -> None:
    op.execute(
        "DROP TRIGGER knowledge_document_versions_immutable "
        "ON knowledge_document_versions"
    )
    op.execute("DROP FUNCTION reject_knowledge_document_version_mutation")
    op.drop_constraint(
        "fk_knowledge_documents_current_version",
        "knowledge_documents",
        type_="foreignkey",
    )
    op.drop_table("knowledge_document_versions")
    op.drop_table("knowledge_documents")
