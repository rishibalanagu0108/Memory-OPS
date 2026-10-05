"""Add deletion tombstones and idempotent per-target purge receipts."""

import sqlalchemy as sa
from alembic import op

revision = "0007_purge_receipts"
down_revision = "0006_temporal_corrections"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "memory_deletion_tombstones",
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("memory_id", sa.Uuid(), nullable=False),
        sa.Column("deleted_version_id", sa.Uuid(), nullable=False),
        sa.Column("deletion_generation", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("reason", sa.String(20), nullable=False),
        sa.Column("requested_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("tenant_id", "memory_id"),
        sa.UniqueConstraint(
            "tenant_id",
            "memory_id",
            "deletion_generation",
            name="uq_memory_deletion_tombstone_generation",
        ),
        sa.CheckConstraint("deletion_generation >= 1", name="ck_memory_deletion_generation"),
        sa.CheckConstraint("reason IN ('forgotten', 'expired')", name="ck_memory_deletion_reason"),
    )
    op.create_table(
        "memory_purge_receipts",
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("memory_id", sa.Uuid(), nullable=False),
        sa.Column("deletion_generation", sa.Integer(), nullable=False),
        sa.Column("target", sa.String(30), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="pending"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column("last_error_code", sa.String(100)),
        sa.ForeignKeyConstraint(
            ["tenant_id", "memory_id", "deletion_generation"],
            [
                "memory_deletion_tombstones.tenant_id",
                "memory_deletion_tombstones.memory_id",
                "memory_deletion_tombstones.deletion_generation",
            ],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("tenant_id", "memory_id", "deletion_generation", "target"),
        sa.CheckConstraint(
            "target IN ('keyword', 'vector', 'graph', 'summary', 'cache', 'evidence', 'canonical')",
            name="ck_memory_purge_receipts_target",
        ),
        sa.CheckConstraint("status IN ('pending', 'completed')", name="ck_memory_purge_receipts_status"),
        sa.CheckConstraint("attempts >= 0", name="ck_memory_purge_receipts_attempts"),
    )

    for table in ("memory_deletion_tombstones", "memory_purge_receipts"):
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"""
            CREATE POLICY {table}_tenant_isolation ON {table}
            USING (
                tenant_id = NULLIF(current_setting('memory_ops.tenant_id', true), '')::uuid
            )
            WITH CHECK (
                tenant_id = NULLIF(current_setting('memory_ops.tenant_id', true), '')::uuid
            )
            """
        )

    op.execute(
        "GRANT SELECT, INSERT, UPDATE ON memory_deletion_tombstones, "
        "memory_purge_receipts TO memory_ops_runtime"
    )
    op.execute(
        "GRANT DELETE ON user_memories, user_memory_versions, "
        "user_memory_evidence TO memory_ops_runtime"
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION reject_user_memory_version_mutation()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF TG_OP = 'DELETE' AND EXISTS (
                SELECT 1 FROM user_memories
                WHERE id = OLD.memory_id
                  AND tenant_id = OLD.tenant_id
                  AND lifecycle IN ('expired', 'revoked')
            ) THEN
                RETURN OLD;
            END IF;
            RAISE EXCEPTION 'user memory versions are immutable';
        END;
        $$
        """
    )


def downgrade() -> None:
    op.execute(
        """
        CREATE OR REPLACE FUNCTION reject_user_memory_version_mutation()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'user memory versions are immutable';
        END;
        $$
        """
    )
    op.execute(
        "REVOKE DELETE ON user_memories, user_memory_versions, "
        "user_memory_evidence FROM memory_ops_runtime"
    )
    op.drop_table("memory_purge_receipts")
    op.drop_table("memory_deletion_tombstones")
