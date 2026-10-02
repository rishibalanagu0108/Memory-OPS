"""Add logical user memories, immutable versions, and evidence references."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0004_user_memory_model"
down_revision = "0003_runtime_role_membership"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_unique_constraint(
        "uq_workspaces_tenant_id_id", "workspaces", ["tenant_id", "id"]
    )
    op.create_table(
        "user_memories",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("subject_id", sa.Uuid(), nullable=False),
        sa.Column("agent_id", sa.Uuid()),
        sa.Column("semantic_type", sa.String(20), nullable=False),
        sa.Column("lifecycle", sa.String(20), nullable=False, server_default="active"),
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
        sa.UniqueConstraint("tenant_id", "id", name="uq_user_memories_tenant_id_id"),
        sa.CheckConstraint(
            "semantic_type IN ('fact', 'preference', 'goal', 'constraint', 'episode')",
            name="ck_user_memories_semantic_type",
        ),
        sa.CheckConstraint(
            "lifecycle IN ('active', 'superseded', 'expired', 'revoked', 'deleted')",
            name="ck_user_memories_lifecycle",
        ),
    )
    op.create_index(
        "ix_user_memories_current",
        "user_memories",
        ["tenant_id", "workspace_id", "subject_id", "semantic_type", "lifecycle"],
    )

    op.create_table(
        "user_memory_versions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("memory_id", sa.Uuid(), nullable=False),
        sa.Column("version_number", sa.Integer(), nullable=False),
        sa.Column("original_statement", sa.Text(), nullable=False),
        sa.Column("normalized_subject", sa.String(255)),
        sa.Column("normalized_predicate", sa.String(255)),
        sa.Column("normalized_value", postgresql.JSONB()),
        sa.Column(
            "qualifiers",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("valid_from", sa.DateTime(timezone=True), nullable=False),
        sa.Column("valid_to", sa.DateTime(timezone=True)),
        sa.Column(
            "recorded_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("sensitivity", sa.String(20), nullable=False),
        sa.Column("lifetime", sa.String(20), nullable=False),
        sa.Column("origin", sa.String(20), nullable=False),
        sa.Column("purpose", sa.String(255), nullable=False),
        sa.Column("policy_version", sa.String(255), nullable=False),
        sa.Column(
            "access_scope",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("retention_until", sa.DateTime(timezone=True)),
        sa.ForeignKeyConstraint(
            ["tenant_id", "memory_id"],
            ["user_memories.tenant_id", "user_memories.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "tenant_id", "memory_id", "version_number",
            name="uq_user_memory_versions_number",
        ),
        sa.UniqueConstraint(
            "tenant_id", "memory_id", "id",
            name="uq_user_memory_versions_pointer",
        ),
        sa.UniqueConstraint(
            "tenant_id", "id", name="uq_user_memory_versions_tenant_id_id"
        ),
        sa.CheckConstraint(
            "version_number >= 1", name="ck_user_memory_versions_number"
        ),
        sa.CheckConstraint(
            "length(btrim(original_statement)) > 0",
            name="ck_user_memory_versions_statement",
        ),
        sa.CheckConstraint(
            "valid_to IS NULL OR valid_to > valid_from",
            name="ck_user_memory_versions_valid_time",
        ),
        sa.CheckConstraint(
            "sensitivity IN ('public', 'internal', 'confidential', 'restricted')",
            name="ck_user_memory_versions_sensitivity",
        ),
        sa.CheckConstraint(
            "lifetime IN ('session', 'temporary', 'durable')",
            name="ck_user_memory_versions_lifetime",
        ),
        sa.CheckConstraint(
            "origin IN ('user', 'agent', 'import', 'inferred')",
            name="ck_user_memory_versions_origin",
        ),
    )
    op.create_foreign_key(
        "fk_user_memories_current_version",
        "user_memories",
        "user_memory_versions",
        ["tenant_id", "id", "current_version_id"],
        ["tenant_id", "memory_id", "id"],
        deferrable=True,
        initially="DEFERRED",
    )

    op.create_table(
        "user_memory_evidence",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("memory_version_id", sa.Uuid(), nullable=False),
        sa.Column("evidence_type", sa.String(30), nullable=False),
        sa.Column("reference_id", sa.String(255), nullable=False),
        sa.Column("locator", sa.String(255)),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "memory_version_id"],
            ["user_memory_versions.tenant_id", "user_memory_versions.id"],
        ),
        sa.CheckConstraint(
            "evidence_type IN "
            "('conversation_message', 'document', 'event', 'external_record')",
            name="ck_user_memory_evidence_type",
        ),
        sa.CheckConstraint(
            "length(btrim(reference_id)) > 0",
            name="ck_user_memory_evidence_reference_id",
        ),
    )
    op.create_index(
        "ix_user_memory_evidence_version",
        "user_memory_evidence",
        ["tenant_id", "memory_version_id"],
    )

    for table in ("user_memories", "user_memory_versions", "user_memory_evidence"):
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
        CREATE FUNCTION reject_user_memory_version_mutation()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'user memory versions are immutable';
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER user_memory_versions_immutable
        BEFORE UPDATE OR DELETE ON user_memory_versions
        FOR EACH ROW EXECUTE FUNCTION reject_user_memory_version_mutation()
        """
    )

    op.execute(
        "REVOKE ALL ON user_memories, user_memory_versions, "
        "user_memory_evidence FROM PUBLIC"
    )
    op.execute(
        "GRANT SELECT, INSERT, UPDATE ON user_memories TO memory_ops_runtime"
    )
    op.execute(
        "GRANT SELECT, INSERT ON user_memory_versions, user_memory_evidence "
        "TO memory_ops_runtime"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER user_memory_versions_immutable ON user_memory_versions")
    op.execute("DROP FUNCTION reject_user_memory_version_mutation")
    op.drop_table("user_memory_evidence")
    op.drop_constraint(
        "fk_user_memories_current_version", "user_memories", type_="foreignkey"
    )
    op.drop_table("user_memory_versions")
    op.drop_table("user_memories")
    op.drop_constraint(
        "uq_workspaces_tenant_id_id", "workspaces", type_="unique"
    )
