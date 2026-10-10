"""Add immutable versioned knowledge-document chunk projections."""

import sqlalchemy as sa
from alembic import op


revision = "0013_knowledge_document_chunks"
down_revision = "0012_knowledge_documents"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint(
        "ck_knowledge_document_versions_media_type",
        "knowledge_document_versions",
        type_="check",
    )
    op.create_check_constraint(
        "ck_knowledge_document_versions_media_type",
        "knowledge_document_versions",
        "media_type IN ("
        "'text/plain', 'text/markdown', 'application/json', 'application/pdf', "
        "'application/vnd.openxmlformats-officedocument.wordprocessingml.document'"
        ")",
    )
    op.create_table(
        "knowledge_document_chunks",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("document_id", sa.Uuid(), nullable=False),
        sa.Column("document_version_id", sa.Uuid(), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("locator_kind", sa.String(50), nullable=False),
        sa.Column("locator_path", sa.String(1024), nullable=False),
        sa.Column("start_line", sa.Integer(), nullable=False),
        sa.Column("end_line", sa.Integer(), nullable=False),
        sa.Column("structure_path", sa.JSON(), nullable=False),
        sa.Column("index_generation", sa.String(255), nullable=False),
        sa.Column("projection_model", sa.String(255), nullable=False),
        sa.Column("projection_model_version", sa.String(255), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "document_id", "document_version_id"],
            [
                "knowledge_document_versions.tenant_id",
                "knowledge_document_versions.document_id",
                "knowledge_document_versions.id",
            ],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "tenant_id",
            "document_version_id",
            "ordinal",
            "index_generation",
            "projection_model",
            "projection_model_version",
            name="uq_knowledge_document_chunks_projection",
        ),
        sa.CheckConstraint("ordinal >= 0", name="ck_knowledge_chunks_ordinal"),
        sa.CheckConstraint(
            "length(content) > 0 AND content_hash ~ '^[0-9a-f]{64}$'",
            name="ck_knowledge_chunks_content",
        ),
        sa.CheckConstraint(
            "start_line >= 1 AND end_line >= start_line",
            name="ck_knowledge_chunks_lines",
        ),
        sa.CheckConstraint(
            "locator_kind IN ("
            "'plain_text_lines', 'markdown_lines', 'json_pointer', "
            "'pdf_page_lines', 'docx_paragraphs'"
            ")",
            name="ck_knowledge_chunks_locator_kind",
        ),
        sa.CheckConstraint(
            "length(btrim(locator_path)) > 0 "
            "AND length(btrim(index_generation)) > 0 "
            "AND length(btrim(projection_model)) > 0 "
            "AND length(btrim(projection_model_version)) > 0",
            name="ck_knowledge_chunks_projection_identity",
        ),
    )
    op.create_index(
        "ix_knowledge_chunks_version",
        "knowledge_document_chunks",
        [
            "tenant_id",
            "document_id",
            "document_version_id",
            "index_generation",
            "ordinal",
        ],
    )
    op.execute("ALTER TABLE knowledge_document_chunks ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE knowledge_document_chunks FORCE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY knowledge_document_chunks_tenant_isolation
        ON knowledge_document_chunks
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
        CREATE FUNCTION reject_knowledge_document_chunk_mutation()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'knowledge document chunks are immutable';
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER knowledge_document_chunks_immutable
        BEFORE UPDATE OR DELETE ON knowledge_document_chunks
        FOR EACH ROW EXECUTE FUNCTION reject_knowledge_document_chunk_mutation()
        """
    )
    op.execute("REVOKE ALL ON knowledge_document_chunks FROM PUBLIC")
    op.execute(
        "GRANT SELECT, INSERT ON knowledge_document_chunks TO memory_ops_runtime"
    )


def downgrade() -> None:
    op.execute(
        "DROP TRIGGER knowledge_document_chunks_immutable "
        "ON knowledge_document_chunks"
    )
    op.execute("DROP FUNCTION reject_knowledge_document_chunk_mutation")
    op.drop_table("knowledge_document_chunks")
