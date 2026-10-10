"""Add immutable current ACL revisions for knowledge documents."""

import sqlalchemy as sa
from alembic import op


revision = "0014_knowledge_acl_revisions"
down_revision = "0013_knowledge_document_chunks"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "knowledge_documents",
        sa.Column("current_acl_revision_id", sa.Uuid()),
    )
    op.create_table(
        "knowledge_document_acl_revisions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("document_id", sa.Uuid(), nullable=False),
        sa.Column("revision_number", sa.Integer(), nullable=False),
        sa.Column("policy_version", sa.String(255), nullable=False),
        sa.Column(
            "created_at",
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
            "revision_number",
            name="uq_knowledge_acl_revision_number",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "document_id",
            "id",
            name="uq_knowledge_acl_revision_pointer",
        ),
        sa.CheckConstraint(
            "revision_number >= 1 AND length(btrim(policy_version)) > 0",
            name="ck_knowledge_acl_revision_identity",
        ),
    )
    op.create_table(
        "knowledge_document_acl_grants",
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("document_id", sa.Uuid(), nullable=False),
        sa.Column("acl_revision_id", sa.Uuid(), nullable=False),
        sa.Column("principal_id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(
            ["tenant_id", "document_id", "acl_revision_id"],
            [
                "knowledge_document_acl_revisions.tenant_id",
                "knowledge_document_acl_revisions.document_id",
                "knowledge_document_acl_revisions.id",
            ],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("tenant_id", "acl_revision_id", "principal_id"),
    )
    op.create_index(
        "ix_knowledge_acl_grants_principal",
        "knowledge_document_acl_grants",
        ["tenant_id", "principal_id", "document_id", "acl_revision_id"],
    )
    op.create_foreign_key(
        "fk_knowledge_documents_current_acl_revision",
        "knowledge_documents",
        "knowledge_document_acl_revisions",
        ["tenant_id", "id", "current_acl_revision_id"],
        ["tenant_id", "document_id", "id"],
        deferrable=True,
        initially="DEFERRED",
    )

    op.execute(
        """
        INSERT INTO knowledge_document_acl_revisions (
            id, tenant_id, document_id, revision_number, policy_version
        )
        SELECT md5(d.id::text || ':acl:1')::uuid,
               d.tenant_id, d.id, 1, v.access_policy_version
        FROM knowledge_documents d
        JOIN knowledge_document_versions v
          ON v.tenant_id = d.tenant_id
         AND v.document_id = d.id
         AND v.id = d.current_version_id
        """
    )
    op.execute(
        """
        INSERT INTO knowledge_document_acl_grants (
            tenant_id, document_id, acl_revision_id, principal_id
        )
        SELECT d.tenant_id, d.id,
               md5(d.id::text || ':acl:1')::uuid,
               d.owner_principal_id
        FROM knowledge_documents d
        """
    )
    op.execute(
        """
        UPDATE knowledge_documents
        SET current_acl_revision_id = md5(id::text || ':acl:1')::uuid
        """
    )
    op.execute(
        "SET CONSTRAINTS fk_knowledge_documents_current_acl_revision IMMEDIATE"
    )
    op.alter_column(
        "knowledge_documents",
        "current_acl_revision_id",
        nullable=False,
    )

    for table in (
        "knowledge_document_acl_revisions",
        "knowledge_document_acl_grants",
    ):
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
        CREATE FUNCTION reject_knowledge_acl_mutation()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'knowledge ACL revisions are immutable';
        END;
        $$
        """
    )
    for table in (
        "knowledge_document_acl_revisions",
        "knowledge_document_acl_grants",
    ):
        op.execute(
            f"""
            CREATE TRIGGER {table}_immutable
            BEFORE UPDATE OR DELETE ON {table}
            FOR EACH ROW EXECUTE FUNCTION reject_knowledge_acl_mutation()
            """
        )

    op.execute(
        "REVOKE ALL ON knowledge_document_acl_revisions, "
        "knowledge_document_acl_grants FROM PUBLIC"
    )
    op.execute(
        "GRANT SELECT, INSERT ON knowledge_document_acl_revisions, "
        "knowledge_document_acl_grants TO memory_ops_runtime"
    )
    op.execute(
        "GRANT UPDATE (current_acl_revision_id) "
        "ON knowledge_documents TO memory_ops_runtime"
    )


def downgrade() -> None:
    op.drop_constraint(
        "fk_knowledge_documents_current_acl_revision",
        "knowledge_documents",
        type_="foreignkey",
    )
    op.drop_column("knowledge_documents", "current_acl_revision_id")
    for table in (
        "knowledge_document_acl_grants",
        "knowledge_document_acl_revisions",
    ):
        op.execute(f"DROP TRIGGER {table}_immutable ON {table}")
    op.execute("DROP FUNCTION reject_knowledge_acl_mutation")
    op.drop_table("knowledge_document_acl_grants")
    op.drop_table("knowledge_document_acl_revisions")
